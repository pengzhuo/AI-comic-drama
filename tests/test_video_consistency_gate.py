"""生成后一致性自检（D 环节）的聚焦测试。"""

import shutil
import subprocess

import pytest

from novelvideo.verification.models import VideoConsistencyIssues
from novelvideo.verification.video_consistency_gate import (
    DEFAULT_MAX_ATTEMPTS,
    GATE_ATTEMPTS_ENV,
    GATE_ENV,
    VideoConsistencyGate,
    VideoConsistencyVerdict,
    consistency_gate_config,
    extract_frame,
    frame_similarity,
)


def test_gate_config_defaults_on(monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.delenv(GATE_ATTEMPTS_ENV, raising=False)

    settings = consistency_gate_config()
    assert settings.enabled is True
    assert settings.max_attempts == DEFAULT_MAX_ATTEMPTS


def test_gate_config_payload_overrides_env(monkeypatch):
    monkeypatch.setenv(GATE_ENV, "1")

    disabled = consistency_gate_config({"video_consistency_gate": False})
    assert disabled.enabled is False

    attempts = consistency_gate_config({"video_consistency_attempts": 2})
    assert attempts.max_attempts == 2

    # 越界值收敛到 1..5，避免配置写错导致无限重跑。
    assert consistency_gate_config({"video_consistency_attempts": 99}).max_attempts == 5
    assert consistency_gate_config({"video_consistency_attempts": 0}).max_attempts == 1


def test_gate_config_env_toggle(monkeypatch):
    monkeypatch.setenv(GATE_ENV, "off")
    monkeypatch.setenv(GATE_ATTEMPTS_ENV, "4")

    settings = consistency_gate_config()
    assert settings.enabled is False
    assert settings.max_attempts == 4

    # payload 显式打开时环境变量不生效。
    enabled = consistency_gate_config({"video_consistency_gate": True})
    assert enabled.enabled is True


def test_frame_similarity_is_bounded_and_sensitive(tmp_path):
    from PIL import Image

    same_a = tmp_path / "a.png"
    same_b = tmp_path / "b.png"
    other = tmp_path / "c.png"
    Image.new("RGB", (64, 64), (200, 200, 200)).save(same_a)
    Image.new("RGB", (64, 64), (200, 200, 200)).save(same_b)
    Image.new("RGB", (64, 64), (10, 10, 10)).save(other)

    assert frame_similarity(same_a, same_b) == 1.0
    far = frame_similarity(same_a, other)
    assert far is not None and far < 0.5
    assert frame_similarity(tmp_path / "missing.png", same_b) is None


def test_reinforce_prompt_keeps_original_and_lists_issues():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=["服装或配饰与首帧不一致（疑似换装）", "肢体/姿态物理穿帮"],
    )

    reinforced = verdict.reinforce_prompt("原始提示词：人物起身。")

    assert reinforced.startswith("原始提示词：人物起身。")
    assert "服装或配饰与首帧不一致（疑似换装）" in reinforced
    assert "肢体/姿态物理穿帮" in reinforced
    # 强化句必须包含首帧锁定与支撑面两条硬要求。
    assert "严格延续首帧画面" in reinforced
    assert "支撑面" in reinforced


def test_verdict_payload_is_serializable():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=["景别不一致"],
        frame0_similarity=0.62,
        issues=VideoConsistencyIssues(framing_changed=True, summary="被重构为特写"),
        notes=["第0帧与首帧相似度 0.62"],
    )

    payload = verdict.as_dict()

    assert payload["passed"] is False
    assert payload["frame0_similarity"] == 0.62
    assert payload["issues"]["framing_changed"] is True


def test_gate_vision_model_routes_through_gateway_alias(monkeypatch):
    from novelvideo.verification import video_consistency_gate as gate_module

    captured: dict[str, object] = {}

    def fake_model(model_env, default_model, **kwargs):
        captured["env"] = model_env
        captured["default"] = default_model
        captured["capability"] = kwargs.get("capability")
        raise RuntimeError("stop")

    monkeypatch.setattr(
        "novelvideo.config.get_newapi_text_pydantic_model", fake_model
    )

    # 视觉审核必须走 CE 网关路由（默认复用已开通的视觉别名），
    # 不能落到 legacy 的 get_pydantic_model() 上导致「模型不存在」而永远跳过审核。
    with pytest.raises(RuntimeError, match="stop"):
        gate_module.VideoConsistencyGate()._get_agent()

    assert captured == {
        "env": "VIDEO_CONSISTENCY_GATE_MODEL",
        "default": "DC-video-identity-detector-LLM",
        "capability": "text.generate.agent",
    }


async def test_gate_skips_missing_files(tmp_path):
    gate = VideoConsistencyGate()

    verdict = await gate.check(
        video_path=tmp_path / "missing.mp4",
        first_frame_path=tmp_path / "missing.png",
    )

    assert verdict.passed is True
    assert verdict.reasons == []
    assert any("跳过" in note for note in verdict.notes)


async def test_gate_reports_vision_issues(monkeypatch, tmp_path):
    from PIL import Image

    frame = tmp_path / "frame.png"
    Image.new("RGB", (64, 64), (120, 120, 120)).save(frame)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not-a-real-video")

    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.shutil.which",
        lambda name: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.extract_frame",
        lambda video_path, seconds, out_path: _write_frame(out_path),
    )
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.probe_duration_seconds",
        lambda video_path: 4.0,
    )
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.frame_similarity",
        lambda left, right, size=64: 0.95,
    )

    gate = VideoConsistencyGate()

    async def fake_verify(**kwargs):
        return VideoConsistencyIssues(
            costume_changed=True,
            physical_defect=True,
            issues=["第3帧换成了黑色西装", "腰部与地面粘连"],
            summary="换装且坐姿穿帮",
        )

    monkeypatch.setattr(gate, "_verify_with_vision", fake_verify)

    verdict = await gate.check(video_path=video, first_frame_path=frame)

    assert verdict.passed is False
    assert any("服装或配饰" in reason for reason in verdict.reasons)
    assert any("肢体/姿态物理穿帮" in reason for reason in verdict.reasons)
    assert any("第3帧换成了黑色西装" in reason for reason in verdict.reasons)
    assert verdict.frame0_similarity == 0.95


async def test_gate_passes_when_vision_unavailable(monkeypatch, tmp_path):
    from PIL import Image

    frame = tmp_path / "frame.png"
    Image.new("RGB", (64, 64), (120, 120, 120)).save(frame)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not-a-real-video")

    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.shutil.which",
        lambda name: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.extract_frame",
        lambda video_path, seconds, out_path: _write_frame(out_path),
    )
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.probe_duration_seconds",
        lambda video_path: 0.0,
    )

    gate = VideoConsistencyGate()

    async def failing_verify(**kwargs):
        raise RuntimeError("视觉模型不可用")

    async def fake_verify(**kwargs):
        try:
            return await failing_verify(**kwargs)
        except RuntimeError:
            return None

    monkeypatch.setattr(gate, "_verify_with_vision", fake_verify)

    verdict = await gate.check(video_path=video, first_frame_path=frame)

    # 自检基础设施失败不能中断产线：按通过处理并记录原因。
    assert verdict.passed is True
    assert verdict.reasons == []


async def test_runner_gate_reruns_with_reinforced_prompt(monkeypatch, tmp_path):
    # 任务侧集成：不通过 → 强化提示词重跑 → 通过后停止；报告写进 attempts。
    runners = pytest.importorskip("novelvideo.task_backend.runners.video")

    class FakeVerdict:
        def __init__(self, passed, reasons=()):
            self.passed = passed
            self.reasons = list(reasons)

        def as_dict(self):
            return {"passed": self.passed, "reasons": self.reasons}

        def reinforce_prompt(self, prompt):
            return f"{prompt}|强化"

    class FakeGate:
        def __init__(self):
            self.checked: list[int] = []

        async def check(self, **kwargs):
            self.checked.append(len(self.checked) + 1)
            if len(self.checked) == 1:
                return FakeVerdict(False, ["服装或配饰与首帧不一致（疑似换装）"])
            return FakeVerdict(True)

    class FakeResult:
        class _Status:
            value = "done"

        status = _Status()
        error = None

    class FakeGen:
        def __init__(self):
            self.prompts: list[str] = []

        async def generate(self, **kwargs):
            self.prompts.append(kwargs["prompt"])
            return FakeResult()

    gate = FakeGate()
    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.VideoConsistencyGate",
        lambda: gate,
    )

    gen = FakeGen()
    logs: list[str] = []
    result, report = await runners._apply_video_consistency_gate(
        video_gen=gen,
        generate_kwargs={"prompt": "原始提示词", "duration": 5},
        result=FakeResult(),
        frame_path=tmp_path / "frame.png",
        video_path=tmp_path / "clip.mp4",
        beat={"audio_type": "none"},
        output_dir=str(tmp_path),
        payload_config=None,
        on_log=logs.append,
    )

    assert len(gate.checked) == 2
    assert gen.prompts == ["原始提示词|强化"]
    assert report["enabled"] is True
    assert [item["passed"] for item in report["attempts"]] == [False, True]
    assert any("重跑" in message for message in logs)


async def test_runner_gate_skips_when_disabled(monkeypatch, tmp_path):
    runners = pytest.importorskip("novelvideo.task_backend.runners.video")

    class ExplodingGate:
        def __init__(self):
            raise AssertionError("关闭时不应构造自检")

    monkeypatch.setattr(
        "novelvideo.verification.video_consistency_gate.VideoConsistencyGate",
        ExplodingGate,
    )

    sentinel = object()
    result, report = await runners._apply_video_consistency_gate(
        video_gen=object(),
        generate_kwargs={},
        result=sentinel,
        frame_path=tmp_path / "frame.png",
        video_path=tmp_path / "clip.mp4",
        beat={},
        output_dir=str(tmp_path),
        payload_config={"video_consistency_gate": False},
        on_log=lambda message: None,
    )

    assert result is sentinel
    assert report == {"enabled": False}


def _write_frame(out_path) -> bool:
    from PIL import Image

    Image.new("RGB", (32, 32), (30, 30, 30)).save(out_path)
    return True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_extract_frame_with_real_ffmpeg(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=1",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )

    out = tmp_path / "frame0.png"
    assert extract_frame(video, 0.0, out) is True
    assert out.exists() and out.stat().st_size > 0
