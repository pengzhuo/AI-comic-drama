"""台词解析：散文里的冒号不是说话人标签。

现场问题：``nn_1021_1`` ep2 beat 8 的台词是「（平淡）认定书上写着，违规操作…」，
``finditer`` 把「认定书上写着」当成说话人，于是给一个不存在的角色去要参考声线，
整条 beat 卡在发请求之前（没有任何上游请求）。
"""

import pytest

from novelvideo.seedance2_i2v.spoken_dialogue import (
    parse_seedance2_spoken_lines,
    unique_seedance2_dialogue_speakers,
)


@pytest.mark.parametrize("separator", ["：", "，"])
def test_prose_prefix_is_not_a_speaker_label(separator):
    """「认定书上写着：/，」是散文，不是说话人；说话人回落到 beat.speaker。"""

    beat = {
        "beat_number": 8,
        "audio_type": "dialogue",
        "speaker": "班长_中年时期",
        "narration": f"（平淡）认定书上写着{separator}违规操作，防护罩是他自己拆的。厂方没责任。",
    }

    assert parse_seedance2_spoken_lines(beat) == []
    assert unique_seedance2_dialogue_speakers(beat) == ["班长_中年时期"]


def test_scripted_speaker_line_is_still_parsed():
    beat = {
        "beat_number": 1,
        "audio_type": "dialogue",
        "narration_segment": "面馆男青年（神色诧异）：现在啥事儿没有啊？",
    }

    lines = parse_seedance2_spoken_lines(beat)

    assert [(line.speaker, line.action, line.text) for line in lines] == [
        ("面馆男青年", "神色诧异", "现在啥事儿没有啊？")
    ]


def test_multi_speaker_script_lines_keep_first_spoken_order():
    """同一行里的多段脚本台词仍要按出现顺序切分（不能只认行首）。"""

    beat = {
        "beat_number": 1,
        "audio_type": "dialogue",
        "narration_segment": (
            "面馆男青年（打开易拉罐）：现在啥事儿没有啊？" "面馆女青年（抬头）：你知道杜晨吗？"
        ),
    }

    assert unique_seedance2_dialogue_speakers(beat) == ["面馆男青年", "面馆女青年"]


def test_silence_beat_parses_no_lines():
    beat = {"beat_number": 3, "audio_type": "silence", "narration": "他叹了口气。"}

    assert parse_seedance2_spoken_lines(beat) == []
