import importlib
import sys

import pytest

from novelvideo.identity_prerequisites import (
    IDENTITY_CHARACTERS_REQUIRED_CODE,
    IDENTITY_CHARACTERS_REQUIRED_MESSAGE,
    IdentityCharactersRequiredError,
)
from novelvideo.novel_source import (
    NOVEL_IMPORT_REQUIRED_CODE,
    NOVEL_IMPORT_REQUIRED_MESSAGE,
    NovelImportRequiredError,
)
from novelvideo.scene_prerequisites import (
    SCENE_CATALOG_BUILDING_CODE,
    SCENE_CATALOG_BUILDING_MESSAGE,
    SceneCatalogBuildingError,
)
from novelvideo.shared.billing_errors import (
    INSUFFICIENT_CREDITS_CODE,
    INSUFFICIENT_CREDITS_MESSAGE,
    InsufficientCreditsStop,
)
from novelvideo.shared.provider_errors import (
    CONTENT_MODERATION_FAILED_CODE,
    CONTENT_MODERATION_FAILED_MESSAGE,
    COPYRIGHT_POLICY_FAILED_MESSAGE,
    INPUT_IMAGE_POLICY_FAILED_MESSAGE,
    OUTPUT_VIDEO_POLICY_FAILED_MESSAGE,
    VIDEO_MEDIA_DIMENSIONS_INVALID_CODE,
    VIDEO_MEDIA_DIMENSIONS_INVALID_MESSAGE,
)
from novelvideo.video_prompt_prerequisite import (
    VIDEO_PROMPT_PREREQUISITE_REQUIRED_CODE,
    VideoPromptPrerequisiteError,
)

pytestmark = pytest.mark.m07


def _import_celery_tasks(monkeypatch):
    monkeypatch.delenv("ST_PROJECT_TASK_TIMEOUT_S", raising=False)
    sys.modules.pop("novelvideo.task_backend.run_core", None)
    return importlib.import_module("novelvideo.task_backend.run_core")


def test_task_serialization_exposes_error_code() -> None:
    from novelvideo.api.routes.tasks import _serialize_task
    from novelvideo.task_state import TaskState

    task = TaskState(
        task_id="task_1",
        task_type="build_characters",
        username="a5",
        project="kk",
        episode=0,
        status="failed",
        metadata={"error_code": INSUFFICIENT_CREDITS_CODE},
    )

    assert _serialize_task(task)["error_code"] == INSUFFICIENT_CREDITS_CODE


def test_celery_task_failure_maps_insufficient_credits_stop(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    stop = InsufficientCreditsStop(user_id="usr_1", cost=2, balance=1)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(stop)

    assert handled is True
    assert error == INSUFFICIENT_CREDITS_MESSAGE
    assert metadata["error_code"] == INSUFFICIENT_CREDITS_CODE
    assert metadata["required"] == 2
    assert metadata["balance"] == 1


def test_task_failure_maps_novel_import_prerequisite(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        NovelImportRequiredError()
    )

    assert handled is True
    assert error == NOVEL_IMPORT_REQUIRED_MESSAGE
    assert metadata == {"error_code": NOVEL_IMPORT_REQUIRED_CODE}


def test_task_failure_maps_video_prompt_prerequisite(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)
    message = "Beat 1 缺少草图或首帧，请先生成草图或预览"

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        VideoPromptPrerequisiteError(message)
    )

    assert handled is True
    assert error == message
    assert metadata == {"error_code": VIDEO_PROMPT_PREREQUISITE_REQUIRED_CODE}


def test_task_failure_maps_identity_character_prerequisite(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        IdentityCharactersRequiredError()
    )

    assert handled is True
    assert error == IDENTITY_CHARACTERS_REQUIRED_MESSAGE
    assert metadata == {"error_code": IDENTITY_CHARACTERS_REQUIRED_CODE}


def test_task_failure_maps_scene_catalog_prerequisite(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        SceneCatalogBuildingError()
    )

    assert handled is True
    assert error == SCENE_CATALOG_BUILDING_MESSAGE
    assert metadata == {"error_code": SCENE_CATALOG_BUILDING_CODE}


def test_celery_task_failure_maps_output_moderation(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    exc = RuntimeError(
        'HTTP 400: body={"error":{"message":"output_moderation",'
        '"code":"output_moderation"}}'
    )

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

    assert handled is True
    assert error == CONTENT_MODERATION_FAILED_MESSAGE
    assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE
    assert "output_moderation" in metadata["provider_error"]


def test_celery_task_failure_maps_output_video_policy_violation(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    for request_id in ("request-id-a", "request-id-b"):
        exc = RuntimeError(
            "[OutputVideoSensitiveContentDetected.PolicyViolation] The request failed because "
            "the output video may be related to copyright restrictions. "
            f"Request id: {request_id}"
        )

        error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

        assert handled is True
        assert error == OUTPUT_VIDEO_POLICY_FAILED_MESSAGE
        assert metadata["message"] == OUTPUT_VIDEO_POLICY_FAILED_MESSAGE
        assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE
        assert "OutputVideoSensitiveContentDetected.PolicyViolation" in metadata["provider_error"]
        assert request_id in metadata["provider_error"]


def test_celery_task_failure_maps_gateway_output_video_sensitive_text(monkeypatch) -> None:
    """DramaClawAPI 判「输出视频敏感」时只回英文自由文本，也必须归一成人话。

    线上实例：``nn_1021_1`` ep6 beat 12 的 ``single_video`` 任务失败时，``error``
    就是这段网关原文的 Python repr——既没有 ``*Detected`` 枚举，也没有 ``VIDEO_*``
    码，缺这条归类就只能把英文原文连同 request id 原样显示给用户。
    """

    celery_tasks = _import_celery_tasks(monkeypatch)

    exc = RuntimeError(
        "{'code': 'upstream_task_failed', 'message': 'The request failed because "
        "the output video may contain sensitive information. "
        "Request id: 02179272371111700000000000000000000ffffac1446b2ec1f50', "
        "'retryable': False}"
    )

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

    assert handled is True
    assert error == OUTPUT_VIDEO_POLICY_FAILED_MESSAGE
    assert metadata["message"] == OUTPUT_VIDEO_POLICY_FAILED_MESSAGE
    assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE
    assert "may contain sensitive information" in metadata["provider_error"]


def test_celery_task_failure_maps_input_image_policy_violation(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    exc = RuntimeError(
        "[InputImageSensitiveContentDetected.PolicyViolation] The request failed because "
        "the input image may be related to copyright restrictions. "
        "Request id: dynamic-request-id"
    )

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

    assert handled is True
    assert error == INPUT_IMAGE_POLICY_FAILED_MESSAGE
    assert metadata["message"] == INPUT_IMAGE_POLICY_FAILED_MESSAGE
    assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE
    assert "InputImageSensitiveContentDetected.PolicyViolation" in metadata["provider_error"]
    assert "dynamic-request-id" in metadata["provider_error"]


def test_celery_task_failure_maps_bare_ark_sensitive_codes(monkeypatch) -> None:
    """2026-11-06 线上实例：图像上游只回光杆 ``*SensitiveContentDetected``。

    换到 aiqy ``doubao-seedream-5.0-flash`` 后，Ark 的拒绝不再带
    ``.PolicyViolation`` 子类（``InputText`` / ``InputImage`` / ``OutputImage`` 三态都是
    光杆码）。缺这条归类时 ``chat/director_auto.py`` 认不出是审核类，只能拿同一套参数
    （同一张参考图 + 同一段提示词）盲重试到预算耗尽。
    """

    celery_tasks = _import_celery_tasks(monkeypatch)

    cases = (
        (
            "InputTextSensitiveContentDetected",
            "the input text may contain sensitive information",
            CONTENT_MODERATION_FAILED_MESSAGE,
        ),
        (
            "InputImageSensitiveContentDetected",
            "the input image may contain sensitive information",
            INPUT_IMAGE_POLICY_FAILED_MESSAGE,
        ),
        (
            "OutputImageSensitiveContentDetected",
            "the output image may contain sensitive information",
            CONTENT_MODERATION_FAILED_MESSAGE,
        ),
    )
    for code, message_fragment, expected_message in cases:
        exc = RuntimeError(
            "DramaClawAPI Images 未返回图像数据: HTTP 400: body={"
            f'"error":{{"message":"The request failed because {message_fragment}. '
            'Request id: 021793970277069d2ec6088cd57f16be69183642d63bc7748cbb0",'
            f'"type":"BadRequest","param":"","code":"{code}"}}}}'
        )

        error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

        assert handled is True, code
        assert error == expected_message, code
        assert metadata["message"] == expected_message, code
        assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE, code
        assert code in metadata["provider_error"], code


def test_celery_task_failure_maps_privacy_information_rejection(monkeypatch) -> None:
    """``.PrivacyInformation`` 也是「输入参考图被拒」，文案同为「换参考图」。"""

    celery_tasks = _import_celery_tasks(monkeypatch)

    exc = RuntimeError(
        "HTTP 400: body={\"error\":{\"message\":\"input image may contain real person\","
        "\"code\":\"InputImageSensitiveContentDetected.PrivacyInformation\"}}"
    )

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(exc)

    assert handled is True
    assert error == INPUT_IMAGE_POLICY_FAILED_MESSAGE
    assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE


def test_celery_task_failure_maps_soft_time_limit(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)
    from celery.exceptions import SoftTimeLimitExceeded

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        SoftTimeLimitExceeded()
    )

    assert handled is True
    assert error == "任务已连续 30 分钟无进度更新，判定为卡死并暂停"
    assert metadata == {"error_code": "TASK_TIMEOUT", "timeout_seconds": 30 * 60}


def test_project_task_timeout_is_independent_from_celery_hard_limit(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    assert celery_tasks._project_task_timeout_seconds() == 30 * 60


def test_celery_task_failure_maps_cooperative_task_timeout(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)
    from novelvideo.task_backend.cancel import TaskTimedOut

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        TaskTimedOut(timeout_seconds=30 * 60)
    )

    assert handled is True
    assert error == "任务已连续 30 分钟无进度更新，判定为卡死并暂停"
    assert metadata == {"error_code": "TASK_TIMEOUT", "timeout_seconds": 30 * 60}


def test_celery_task_failure_reraises_non_business_base_exception(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        celery_tasks._project_task_failure_for_exception(KeyboardInterrupt())


@pytest.mark.parametrize(
    "text",
    [
        "freezone video generation failed: VIDEO_MEDIA_DIMENSIONS_INVALID",
        (
            "freezone video generation failed: {'code': 'VIDEO_MEDIA_DIMENSIONS_INVALID', "
            "'message': '素材尺寸不符合要求，宽度和高度需在 300 至 6000 像素之间。'}"
        ),
    ],
)
def test_celery_task_failure_maps_video_media_dimensions_invalid(
    monkeypatch, text: str
) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        RuntimeError(text)
    )

    assert handled is True
    assert error == VIDEO_MEDIA_DIMENSIONS_INVALID_MESSAGE
    assert metadata["error_code"] == VIDEO_MEDIA_DIMENSIONS_INVALID_CODE
    assert metadata["message"] == VIDEO_MEDIA_DIMENSIONS_INVALID_MESSAGE
    assert "VIDEO_MEDIA_DIMENSIONS_INVALID" in metadata["provider_error"]


def test_celery_task_failure_maps_provider_copyright_code_to_moderation(
    monkeypatch,
) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        RuntimeError("freezone video generation failed: VIDEO_CONTENT_COPYRIGHT_RESTRICTED")
    )

    assert handled is True
    assert error == COPYRIGHT_POLICY_FAILED_MESSAGE
    assert metadata["error_code"] == CONTENT_MODERATION_FAILED_CODE
    assert metadata["message"] == COPYRIGHT_POLICY_FAILED_MESSAGE


def test_celery_task_failure_leaves_unknown_egress_code_unmapped(monkeypatch) -> None:
    celery_tasks = _import_celery_tasks(monkeypatch)

    error, metadata, handled = celery_tasks._project_task_failure_for_exception(
        RuntimeError("freezone video generation failed: EGRESS_OPERATION_UNKNOWN")
    )

    assert handled is False
    assert metadata == {}
    assert "EGRESS_OPERATION_UNKNOWN" in error
