from novelvideo.task_backend import limits


def test_default_video_limits_allow_more_than_one_generation_in_flight(monkeypatch):
    """视频通道的默认额度必须真的允许并行。

    这里刻意锁死「项目级 hard limit == 每用户额度 == 全局槽位」，因为三者只要有
    一个偏小，视频吞吐就会被压回单路，而且症状很隐蔽：请求照发、只是排成一条线。
    实测（39 beat / 96 次生成、单次 p50 152s）视频阶段因此铺了 3.7h 墙钟，而计算
    本身只有 4.0h——耗时几乎全是等待。
    """

    for name in (
        "ST_PROJECT_MAX_ACTIVE_VIDEO_TASKS",
        "ST_PROJECT_USER_MAX_ACTIVE_VIDEO_TASKS",
        "ST_CE_GLOBAL_MAX_ACTIVE_VIDEO_TASKS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert limits.project_lane_active_limit("video") == 4
    assert limits.project_user_lane_active_limit("video") == 4
    # 单用户项目下有效上限就是每用户额度与 hard limit 的较小者，两者都必须是 4。
    assert limits.project_lane_effective_active_limit("video", eligible_user_count=1) == 4
    assert limits.global_lane_concurrency("video") == 4
