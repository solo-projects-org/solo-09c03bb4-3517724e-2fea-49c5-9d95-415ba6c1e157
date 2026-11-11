"""读段规划单元测试（不需要设备与网络）。"""

from __future__ import annotations

import pytest

from app.planner import PlanError, plan_reads


def seg_tuples(segments):
    return [(s.start, s.end, tuple(s.point_indexes)) for s in segments]


def test_single_word_points_are_merged_when_within_limit():
    # 可读 0..99，上限 16；三个相邻单字应合并为 1 段、共 3 个寄存器
    segs = plan_reads(
        points=[(0, 1), (1, 1), (2, 1)],
        range_start=0,
        range_end=99,
        max_read=16,
    )
    assert seg_tuples(segs) == [(0, 2, (0, 1, 2))]
    assert sum(s.count for s in segs) == 3


def test_double_word_point_and_neighbors():
    # 测点 0(双字) 占 0..1；测点 2(单字)；上限 2 -> 必须两段
    segs = plan_reads(
        points=[(0, 2), (2, 1)],
        range_start=0,
        range_end=99,
        max_read=2,
    )
    assert seg_tuples(segs) == [(0, 1, (0,)), (2, 2, (1,))]
    assert [s.count for s in segs] == [2, 1]


def test_dp_beats_greedy_on_register_totals():
    # 贪心（尽量多吞测点）：第一段 0..5 含点0、点1（6 个寄存器），
    #   并入点2(双字占6..7)后为 0..7 共 8 字 > 上限 6，被迫断开；
    #   第二段 6..8 含点2、点3（3 个寄存器）-> 共 9。
    # DP 找到更优：[0..0](1, 点0) + [5..8](4, 点1..3) = 2 次共 5。
    segs = plan_reads(
        points=[(0, 1), (5, 1), (6, 2), (8, 1)],
        range_start=0,
        range_end=99,
        max_read=6,
    )
    assert len(segs) == 2
    assert seg_tuples(segs) == [(0, 0, (0,)), (5, 8, (1, 2, 3))]
    assert sum(s.count for s in segs) == 5


def test_dp_chooses_smaller_register_total_when_requests_tie():
    # 真正的反贪心场景：
    # 点 0(单字)、点 6(单字)、点 7(双字占 7..8)；上限 6，可读 0..99。
    # 贪心：[0..5](6, 含点0) + [6..8](3, 含点1、点2) = 2 次共 9。
    # DP  ：[0..0](1, 含点0) + [6..8](3, 含点1、点2) = 2 次共 4，更优。
    segs = plan_reads(
        points=[(0, 1), (6, 1), (7, 2)],
        range_start=0,
        range_end=99,
        max_read=6,
    )
    assert len(segs) == 2
    assert seg_tuples(segs) == [(0, 0, (0,)), (6, 8, (1, 2))]
    assert sum(s.count for s in segs) == 4


def test_point_outside_readable_range_rejected():
    with pytest.raises(PlanError, match="超出可读区间"):
        plan_reads(points=[(50, 1)], range_start=0, range_end=49, max_read=16)


def test_double_word_crossing_range_end_rejected():
    # 起始 49、双字占 49..50，区间止于 49
    with pytest.raises(PlanError, match="超出可读区间"):
        plan_reads(points=[(49, 2)], range_start=0, range_end=49, max_read=16)


def test_words_exceeding_max_read_rejected():
    with pytest.raises(PlanError, match="超过单次读取上限"):
        plan_reads(points=[(0, 2)], range_start=0, range_end=99, max_read=1)


def test_duplicate_points_are_covered_together():
    segs = plan_reads(
        points=[(3, 1), (3, 1), (4, 1)],
        range_start=0,
        range_end=99,
        max_read=16,
    )
    assert seg_tuples(segs) == [(3, 4, (0, 1, 2))]


def test_invalid_range_rejected():
    with pytest.raises(PlanError):
        plan_reads(points=[(0, 1)], range_start=10, range_end=5, max_read=16)


def test_unsorted_input_returns_original_indexes():
    segs = plan_reads(
        points=[(20, 1), (0, 1), (10, 2)],
        range_start=0,
        range_end=99,
        max_read=32,
    )
    # 全部并入一段（20..0 含 21 个寄存器，<= 上限 32），point_indexes 保留请求下标
    assert seg_tuples(segs) == [(0, 20, (1, 2, 0))]
