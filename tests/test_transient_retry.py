"""Leaf-level transient retry: policy, and the call sites that use it.

The one-click pipeline pauses an entire episode when a beat task fails, so the
retry policy has to be narrow enough never to repeat a paid, deterministic, or
billing failure — and wide enough to ride out an upstream blip.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from novelvideo.generators import video_generator
from novelvideo.shared import transient_retry
from novelvideo.shared.billing_errors import InsufficientCreditsError
from novelvideo.shared.transient_retry import call_with_transient_retry


def test_default_predicate_retries_only_transient_failures() -> None:
    retryable = transient_retry.is_retryable_transient

    # An HTTP status the provider either asked us to repeat or answered on the
    # server side.
    assert retryable(video_generator.NewApiVideoError("boom", status_code=503))
    assert retryable(video_generator.NewApiVideoError("boom", status_code=429))
    # httpx keeps the status on the response, not on the exception.
    request = httpx.Request("POST", "http://gateway.test/v1/audio/speech")
    assert retryable(
        httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(502, request=request)
        )
    )
    # A connection that never went out is always safe to repeat.
    assert retryable(httpx.ConnectError("no route to host"))

    # Deterministic client errors.
    assert not retryable(video_generator.NewApiVideoError("bad request", status_code=400))
    assert not retryable(video_generator.NewApiVideoError("denied", status_code=401))
    assert not retryable(
        httpx.HTTPStatusError(
            "bad request", request=request, response=httpx.Response(400, request=request)
        )
    )
    # A read timeout may already have paid work upstream.
    assert not retryable(httpx.ReadTimeout("slow"))
    # Credits do not appear by retrying.
    assert not retryable(
        InsufficientCreditsError(user_id="user-1", cost=10, balance=0)
    )


@pytest.mark.asyncio
async def test_call_with_transient_retry_recovers_on_a_later_attempt() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise video_generator.NewApiVideoError("upstream", status_code=503)
        return "ok"

    assert (
        await call_with_transient_retry(flaky, attempts=3, backoff=(0.0, 0.0)) == "ok"
    )
    assert attempts == 3


@pytest.mark.asyncio
async def test_call_with_transient_retry_gives_up_and_reraises() -> None:
    attempts = 0

    async def always_failing() -> str:
        nonlocal attempts
        attempts += 1
        raise video_generator.NewApiVideoError("upstream", status_code=502)

    with pytest.raises(video_generator.NewApiVideoError):
        await call_with_transient_retry(always_failing, attempts=2, backoff=(0.0,))
    assert attempts == 2


@pytest.mark.asyncio
async def test_call_with_transient_retry_never_repeats_a_fatal_failure() -> None:
    attempts = 0

    async def denied() -> str:
        nonlocal attempts
        attempts += 1
        raise video_generator.NewApiVideoError("denied", status_code=403)

    with pytest.raises(video_generator.NewApiVideoError):
        await call_with_transient_retry(denied, attempts=3, backoff=(0.0, 0.0))
    assert attempts == 1


class _FakeVideoResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeVideoResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeVideoSession:
    """One response per call, repeating the last entry once exhausted."""

    def __init__(self, responses: list[tuple[int, str]], calls: list[str]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self) -> "_FakeVideoSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def post(self, url: str, **_kwargs: object) -> _FakeVideoResponse:
        index = min(len(self._calls), len(self._responses) - 1)
        self._calls.append(url)
        return _FakeVideoResponse(*self._responses[index])

    def get(self, url: str, **_kwargs: object) -> _FakeVideoResponse:
        index = min(len(self._calls), len(self._responses) - 1)
        self._calls.append(url)
        return _FakeVideoResponse(*self._responses[index])


def _install_fake_video_session(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, str]],
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        video_generator.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeVideoSession(responses, calls),
    )
    monkeypatch.setattr(video_generator, "NEWAPI_VIDEO_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    return calls


def _video_generator() -> video_generator.NewApiVideoGenerator:
    return video_generator.NewApiVideoGenerator(
        api_key="test-key",
        endpoint="http://gateway.test/v1",
        model="seedance-2.0-fast",
    )


@pytest.mark.asyncio
async def test_video_submit_retries_a_transient_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_video_session(
        monkeypatch,
        [
            (503, "upstream unavailable"),
            (200, '{"id": "task-1"}'),
        ],
    )

    data = await _video_generator()._post_json(
        "http://gateway.test/v1/video/generations", {"model": "seedance-2.0-fast"}
    )

    assert data["id"] == "task-1"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_video_submit_does_not_retry_a_rejected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_video_session(
        monkeypatch,
        [(400, "invalid payload")],
    )

    with pytest.raises(video_generator.NewApiVideoError) as excinfo:
        await _video_generator()._post_json(
            "http://gateway.test/v1/video/generations", {"model": "nope"}
        )

    assert excinfo.value.status_code == 400
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_video_poll_retries_a_transient_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_video_session(
        monkeypatch,
        [
            (502, "bad gateway"),
            (200, '{"status": "running"}'),
        ],
    )

    data = await _video_generator()._get_json(
        "http://gateway.test/v1/video/generations/task-1"
    )

    assert data["status"] == "running"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_video_download_retries_a_retryable_delivery_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delivery is idempotent, so ``download_to_path``'s own retryable flag rules."""

    from novelvideo.ports.video_delivery import (
        VideoDeliveryError,
        VideoDeliveryReceipt,
    )
    from novelvideo.utils import media_download

    attempts = 0
    output = tmp_path / "out.mp4"

    async def fake_download(url: str, output_path: str, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise VideoDeliveryError("VIDEO_DOWNLOAD_FAILED", retryable=True)
        Path(output_path).write_bytes(b"video")
        return VideoDeliveryReceipt(method="download", size=5, sha256="abc")

    monkeypatch.setattr(media_download, "download_to_path", fake_download)
    monkeypatch.setattr(video_generator, "NEWAPI_VIDEO_RETRY_BACKOFF_SECONDS", (0.0,))

    receipt = await _video_generator()._download_video(
        "http://gateway.test/v1/videos/task-1/content", str(output)
    )

    assert attempts == 2
    assert receipt.size == 5


@pytest.mark.asyncio
async def test_video_download_does_not_retry_a_permanent_delivery_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.ports.video_delivery import VideoDeliveryError
    from novelvideo.utils import media_download

    attempts = 0

    async def fake_download(url: str, output_path: str, **_kwargs: object):
        nonlocal attempts
        attempts += 1
        raise VideoDeliveryError("VIDEO_DATA_URL_INVALID", retryable=False)

    monkeypatch.setattr(media_download, "download_to_path", fake_download)

    with pytest.raises(VideoDeliveryError):
        await _video_generator()._download_video(
            "data:,", str(tmp_path / "out.mp4")
        )
    assert attempts == 1


class _FakeTtsResponse:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content
        self.headers = {"content-type": "audio/mpeg"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://gateway.test/v1/audio/speech"),
                response=httpx.Response(self.status_code),
            )


@pytest.mark.asyncio
async def test_tts_speech_post_retries_a_transient_gateway_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.generators import indextts2_fal

    attempts = 0
    output = tmp_path / "beat_01.mp3"

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> _FakeTtsResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _FakeTtsResponse(500)
            return _FakeTtsResponse(200, b"audio-bytes")

    monkeypatch.setattr(indextts2_fal.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(indextts2_fal, "INDEXTTS2_RETRY_BACKOFF_SECONDS", (0.0,))

    async def fake_duration(_path: Path) -> float:
        return 1.5

    monkeypatch.setattr(indextts2_fal, "_audio_duration_seconds", fake_duration)

    client = indextts2_fal.IndexTTS2FalClient(
        provider="newapi",
        api_key="tts-key",
        endpoint="http://gateway.test/v1",
        model="index-tts-2",
    )
    result = await client._generate_via_newapi(
        prompt="hello",
        audio_url="http://gateway.test/ref.wav",
        output_path=output,
    )

    assert result.success is True
    assert attempts == 2
    assert output.read_bytes() == b"audio-bytes"


@pytest.mark.asyncio
async def test_tts_speech_post_does_not_retry_a_bad_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.generators import indextts2_fal

    attempts = 0

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> _FakeTtsResponse:
            nonlocal attempts
            attempts += 1
            return _FakeTtsResponse(400)

    monkeypatch.setattr(indextts2_fal.httpx, "AsyncClient", FakeAsyncClient)

    client = indextts2_fal.IndexTTS2FalClient(
        provider="newapi",
        api_key="tts-key",
        endpoint="http://gateway.test/v1",
        model="index-tts-2",
    )
    result = await client._generate_via_newapi(
        prompt="hello",
        audio_url="http://gateway.test/ref.wav",
        output_path=tmp_path / "beat_01.mp3",
    )

    assert result.success is False
    assert attempts == 1
