"""台词解析：散文里的冒号不是说话人标签。

现场问题：``nn_1021_1`` ep2 beat 8 的台词是「（平淡）认定书上写着，违规操作…」，
``finditer`` 把「认定书上写着」当成说话人，于是给一个不存在的角色去要参考声线，
整条 beat 卡在发请求之前（没有任何上游请求）。

另一处现场问题（``nn_1104_1`` ep1 beat 04/11/12）：名字里带单字动词（``苏念`` 的
``念``）被 ``looks_like_speaker_label`` 误判成散文，导致 ``required`` 台词退化
成整段旁白 + 没认出的说话人标签，``_validate_dialogue_final_prompt`` 因此永远
不通过、视频请求发不出去。
"""

import pytest

from novelvideo.seedance2_i2v.spoken_dialogue import (
    parse_seedance2_spoken_lines,
    required_seedance2_dialogue_texts,
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


def test_name_with_verb_char_is_still_a_speaker():
    """名字里的单字动词（``苏念`` 的 ``念``）不该被判成散文。

    ``nn_1104_1`` beat 04 现场：``苏念（小声，独白）：…`` 没被认出，required 退化成
    整段旁白，视频请求被自家校验挡下。
    """

    beat = {
        "beat_number": 4,
        "audio_type": "dialogue",
        "speaker": "苏念_青年时期",
        "narration": (
            "苏念背着帆布包走到信箱前，指尖捏着折好的纸条，犹豫几秒，弯腰塞进去。"
            " 苏念（小声，独白）：今天又觉得，好像没有人懂我。"
        ),
    }

    lines = parse_seedance2_spoken_lines(beat)

    assert [(line.speaker, line.action, line.text) for line in lines] == [
        ("苏念", "小声，独白", "今天又觉得，好像没有人懂我")
    ]
    assert required_seedance2_dialogue_texts(beat) == ["今天又觉得，好像没有人懂我"]


def test_multi_speaker_with_verb_name_keeps_both_lines():
    """没认出的说话人会把它的标签并进上一句台词；两个都要认出来。"""

    beat = {
        "beat_number": 11,
        "audio_type": "dialogue",
        "speaker": "江逾_青年时期",
        "narration": "江逾（轻声）：小心，别打湿了。 苏念（慌乱，耳尖发红）：啊…… 谢谢。",
    }

    assert required_seedance2_dialogue_texts(beat) == ["小心，别打湿了", "啊…… 谢谢"]
    assert unique_seedance2_dialogue_speakers(beat) == ["江逾", "苏念"]


def test_trailing_prose_after_last_line_is_dropped():
    """最后一行台词后面的动作旁白不能算必录台词。"""

    beat = {
        "beat_number": 12,
        "audio_type": "dialogue",
        "speaker": "江逾_青年时期",
        "narration": "江逾：原来，你就是一直写信的人。 苏念愣住，睁圆眼睛。",
    }

    assert required_seedance2_dialogue_texts(beat) == ["原来，你就是一直写信的人"]
