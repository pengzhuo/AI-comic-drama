"""分镜规划（镜头语言）单测。"""

from novelvideo.shot_language import (
    CAMERA_ANGLES,
    CAMERA_MOVES,
    SHOT_SIZES,
    ShotLanguage,
    plan_shot_language,
    plan_shot_language_by_number,
)


def _beat(number, *, scene="sc1", audio_type="narration", speaker="", text=""):
    return {
        "beat_number": number,
        "scene_ref": {"scene_id": scene},
        "audio_type": audio_type,
        "speaker": speaker,
        "narration_segment": text,
    }


def test_empty_timeline_returns_nothing():
    assert plan_shot_language([]) == []


def test_vocabulary_is_closed():
    planned = plan_shot_language(
        [
            _beat(1, scene="a"),
            _beat(2, audio_type="dialogue", speaker="A", text="你好，你终于来了。"),
            _beat(3, audio_type="dialogue", speaker="B", text="我来晚了。"),
            _beat(4, audio_type="silence"),
        ]
    )
    for item in planned:
        assert item.shot_size in SHOT_SIZES
        assert item.shot_angle in CAMERA_ANGLES
        assert item.shot_move in CAMERA_MOVES


def test_first_beat_is_an_establishing_shot():
    planned = plan_shot_language([_beat(1, scene="a")])
    assert planned[0].shot_size == "全景"
    assert planned[0].shot_angle == "平视"
    assert planned[0].shot_move == "推"


def test_scene_change_re_establishes():
    planned = plan_shot_language(
        [
            _beat(1, scene="a", audio_type="dialogue", speaker="A", text="第一场的第一句台词。"),
            _beat(2, scene="a", audio_type="dialogue", speaker="B", text="第一场第二句台词。"),
            _beat(3, scene="b"),
        ]
    )
    # 换场景后的第一镜回到建立镜头（全景）。
    assert planned[2].shot_size == "全景"


def test_dialogue_alternates_over_the_shoulder():
    planned = plan_shot_language(
        [
            _beat(1, scene="a", audio_type="dialogue", speaker="A", text="这句话足够长，不会被当成短台词特写。"),
            _beat(2, scene="a", audio_type="dialogue", speaker="B", text="这一句也同样足够长，用来观察正反打。"),
            _beat(3, scene="a", audio_type="dialogue", speaker="A", text="第三句也依然足够长，继续观察交替。"),
        ]
    )
    angles = [item.shot_angle for item in planned]
    assert "平视" in angles and "过肩" in angles


def test_short_dialogue_gets_a_closeup():
    # 第一镜是建立镜头（全景）；第二镜是极短台词，应改特写。
    planned = plan_shot_language(
        [
            _beat(1, scene="a"),
            _beat(2, scene="a", audio_type="dialogue", speaker="A", text="不可能！"),
        ]
    )
    assert planned[1].shot_size == "特写"


def test_adjacent_shots_never_repeat_framing_and_angle():
    beats = []
    for index in range(1, 13):
        beats.append(
            _beat(
                index,
                scene="a",
                audio_type="dialogue",
                speaker="A" if index % 2 else "B",
                text="这是一句长度适中的台词，用来检验相邻镜头是否会撞景别与机位。",
            )
        )
    planned = plan_shot_language(beats)
    for prev, cur in zip(planned, planned[1:]):
        assert (prev.shot_size, prev.shot_angle) != (cur.shot_size, cur.shot_angle)


def test_silence_beats_are_wide_and_roaming():
    planned = plan_shot_language([_beat(1, scene="a"), _beat(2, scene="a", audio_type="silence")])
    assert planned[1].shot_size == "远景"
    assert planned[1].shot_move == "摇"


def test_last_beat_is_a_push_in():
    planned = plan_shot_language(
        [
            _beat(1, scene="a"),
            _beat(2, scene="a", audio_type="dialogue", speaker="A", text="收尾这句台词要够长才好判断推镜。"),
        ]
    )
    assert planned[-1].shot_move == "推"


def test_prompt_fragment_and_dict():
    item = ShotLanguage(
        beat_number=3, shot_size="中近景", shot_angle="过肩", shot_move="固定"
    )
    assert item.as_dict() == {
        "shot_size": "中近景",
        "shot_angle": "过肩",
        "shot_move": "固定",
    }
    assert item.prompt_fragment() == "中近景、过肩反打、固定机位"


def test_plan_by_number_keys_match():
    beats = [_beat(1, scene="a"), _beat(2, scene="a")]
    by_number = plan_shot_language_by_number(beats)
    assert set(by_number) == {1, 2}


def test_apply_plan_copies_beats_and_stamps_columns():
    """``apply_shot_language_plan`` 给草图阶段补齐镜头语言，且不改入参。"""

    from novelvideo.shot_language import apply_shot_language_plan

    beats = [
        _beat(1, scene="a"),
        _beat(2, audio_type="dialogue", speaker="A", text="走吧。"),
    ]
    stamped = apply_shot_language_plan(beats)

    assert [b["shot_size"] for b in stamped] == [
        item.shot_size for item in plan_shot_language(beats)
    ]
    assert [b["shot_angle"] for b in stamped] == [
        item.shot_angle for item in plan_shot_language(beats)
    ]
    # 入参不被原地修改，且返回的是新对象（任务 payload 各自独立）。
    assert "shot_size" not in beats[0]
    assert stamped[0] is not beats[0]


def test_apply_plan_supports_dataclass_and_pydantic_like_objects():
    import dataclasses

    from novelvideo.shot_language import apply_shot_language_plan

    @dataclasses.dataclass
    class _Plain:
        beat_number: int
        scene_id: str = "a"
        audio_type: str = "narration"
        narration_segment: str = ""
        shot_size: str = ""
        shot_angle: str = ""
        shot_move: str = ""

    class _Copyable:
        def __init__(self, beat_number):
            self.beat_number = beat_number

        def model_copy(self, update=None):
            clone = _Copyable(self.beat_number)
            for key, value in (update or {}).items():
                setattr(clone, key, value)
            return clone

    stamped = apply_shot_language_plan([_Plain(1), _Copyable(2)])
    assert stamped[0].shot_size  # dataclasses.replace 路径
    assert stamped[1].shot_angle  # model_copy 路径


def test_sketch_clause_is_silent_without_a_plan():
    from novelvideo.generators.prompt_strategies import _planned_shot_language_clause

    assert _planned_shot_language_clause({"visual_description": "他推门进来。"}) == ""
    assert _planned_shot_language_clause({}) == ""


def test_sketch_clause_carries_size_angle_and_reverse_angle():
    from novelvideo.generators.prompt_strategies import _planned_shot_language_clause

    clause = _planned_shot_language_clause(
        {"shot_size": "中近景", "shot_angle": "平视", "shot_move": "固定"}
    )
    assert "中近景" in clause and "平视" in clause and "固定" in clause

    reverse = _planned_shot_language_clause(
        {"shot_size": "中近景", "shot_angle": "过肩", "shot_move": "固定"}
    )
    assert "过肩反打" in reverse

