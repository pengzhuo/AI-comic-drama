"""一致性门禁的「按维度决定是否重跑」与参考声线预检。

现场问题：26/39 个 beat 被标记人工复核，其中 19 个只是「景别被重构」——画面本身
（身份、服装、道具、声音）并没有坏，却每次都要再花一整轮生成去重跑。
"""

import pytest

from novelvideo.seedance2_i2v.voice_preflight import (
    beat_expects_speech,
    speech_reference_audio_gap,
)
from novelvideo.verification.video_consistency_gate import (
    DEFAULT_MAX_ATTEMPTS,
    VideoConsistencyVerdict,
    classify_reason,
    consistency_gate_config,
)


def test_default_attempts_allow_exactly_one_rerun():
    assert DEFAULT_MAX_ATTEMPTS == 2
    assert consistency_gate_config(config={}).max_attempts == 2


def test_classify_reason_maps_dimensions():
    assert classify_reason("服装或配饰与首帧不一致（疑似换装）") == "costume"
    assert classify_reason("脸型或身份与首帧不一致（疑似换脸）") == "identity"
    assert classify_reason("景别与首帧不一致（被重构为更近的镜头）") == "framing"
    assert classify_reason("肢体/姿态物理穿帮（悬空、贴地、肢体缺失或融接）") == "physical"
    assert classify_reason(
        "这一 beat 有台词/解说，但成片音轨接近静音，口型与声音不可能对上"
    ) == "audio"
    assert classify_reason("视频第0帧与首帧差异过大（相似度 0.71），画面被整体重做") == "frame0"
    assert classify_reason("画面有点灰") == "other"


def test_hard_first_frame_mismatch_reruns_instead_of_being_marked_only():
    """首帧硬失配不是「构图偏好」：它意味着这一版没从首帧开始渲染。

    衣服/化妆/道具的锁定全靠首帧，如果这种版本只标记不重跑，等于让漂移直接过闸，
    与「一致性必须保住」的目标相反。
    """

    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=["视频第0帧与首帧差异过大（相似度 0.71），画面被整体重做"],
    )

    assert verdict.should_rerun is True
    assert verdict.marked_reasons == []
    assert verdict.rerun_reasons == ["视频第0帧与首帧差异过大（相似度 0.71），画面被整体重做"]
    # 强化提示词里必须带上这条，否则重跑的这一版还是同一个毛病。
    assert "差异过大" in verdict.reinforce_prompt("原本的提示词")


def test_framing_only_failures_are_marked_but_not_rerun():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=["景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）"],
    )

    assert verdict.should_rerun is False
    assert verdict.rerun_reasons == []
    assert len(verdict.marked_reasons) == 1


def test_wardrobe_or_identity_failures_still_rerun():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=[
            "服装或配饰与首帧不一致（疑似换装）",
            "景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）",
        ],
    )

    assert verdict.should_rerun is True
    assert verdict.rerun_reasons == ["服装或配饰与首帧不一致（疑似换装）"]
    assert verdict.marked_reasons == [
        "景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）"
    ]


def test_report_splits_rerun_and_marked_reasons():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=[
            "脸型或身份与首帧不一致（疑似换脸）",
            "景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）",
        ],
    )

    payload = verdict.as_dict()

    assert payload["should_rerun"] is True
    assert payload["rerun_reasons"] == ["脸型或身份与首帧不一致（疑似换脸）"]
    assert payload["marked_reasons"] == [
        "景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）"
    ]


def test_reinforce_prompt_only_lists_rerun_reasons():
    verdict = VideoConsistencyVerdict(
        passed=False,
        reasons=[
            "脸型或身份与首帧不一致（疑似换脸）",
            "景别与首帧不一致（被重构为更近的镜头或裁掉了画面内容）",
        ],
    )

    reinforced = verdict.reinforce_prompt("原本的提示词")

    assert "脸型或身份" in reinforced
    # 景别类问题不该被写进强化提示词：那会把每一版都逼着复刻构图，反而更容易崩。
    assert "景别与首帧不一致" not in reinforced


class _Ref:
    def __init__(self, type_: str) -> None:
        self.type = type_


_DIALOGUE_BEAT = {
    "audio_type": "dialogue",
    "speaker": "许知意",
    "narration_segment": "今天是我的生日。",
    "beat_number": 12,
}


def test_dialogue_beat_without_reference_audio_is_a_gap():
    assert beat_expects_speech(_DIALOGUE_BEAT) is True

    gap = speech_reference_audio_gap(
        beat=_DIALOGUE_BEAT, beat_num=12, references=[], seedance2_config="{}"
    )

    assert "参考音频" in gap
    assert "许知意" in gap


def test_dialogue_beat_with_reference_audio_passes():
    gap = speech_reference_audio_gap(
        beat=_DIALOGUE_BEAT,
        beat_num=12,
        references=[_Ref("audio")],
        seedance2_config="{}",
    )

    assert gap == ""


def test_silent_beat_needs_no_reference_audio():
    silent = {"audio_type": "silence", "narration_segment": "", "beat_number": 3}

    assert beat_expects_speech(silent) is False
    assert (
        speech_reference_audio_gap(
            beat=silent, beat_num=3, references=[], seedance2_config="{}"
        )
        == ""
    )


def test_beat_that_does_not_generate_audio_needs_no_reference_audio():
    gap = speech_reference_audio_gap(
        beat=_DIALOGUE_BEAT,
        beat_num=12,
        references=[],
        # generate_audio 只在「用户显式设置过」时才不会被解析器改回 True。
        seedance2_config='{"generate_audio": false, "generate_audio_user_set": true}',
    )

    assert gap == ""


@pytest.mark.parametrize("audio_type", ["dialogue", "narration"])
def test_both_speaking_audio_types_are_guarded(audio_type):
    beat = {**_DIALOGUE_BEAT, "audio_type": audio_type}

    assert speech_reference_audio_gap(
        beat=beat, beat_num=12, references=[], seedance2_config="{}"
    )
