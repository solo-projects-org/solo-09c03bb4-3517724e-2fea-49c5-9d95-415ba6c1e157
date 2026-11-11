"""补传存储层与 API 的测试。

覆盖：
- 区间增量规则（同表未下降给增量；换表 / 计数下降待核对不给增量）；
- 乱序批、重复跳过、同秒冲突整批拒绝且回滚；
- 中间读数只替换被拆开的旧区间，其余区间不动；
- 多计量点隔离、查询过滤；
- 并发补传后区间始终只连接实际相邻采样；
- FastAPI 接口端到端（ASGI 传输，无需网络）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.main import app
from app.store import (
    REASON_COUNT_DECREASED,
    REASON_METER_CHANGED,
    STATUS_OK,
    STATUS_PENDING,
    BackfillConflict,
    Database,
    get_database,
)


@pytest.fixture
def db() -> Database:
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(os.path.join(tmp, "test.db"))
        yield database
        database.close()


def _iv_map(database: Database, meter_id: str) -> dict[tuple[int, int], dict]:
    return {
        (iv.start_ts, iv.end_ts): {
            "status": iv.status,
            "increment": iv.increment,
            "reason": iv.pending_reason,
            "start_serial": iv.start_meter_serial,
            "end_serial": iv.end_meter_serial,
        }
        for iv in database.list_intervals(meter_id, limit=2000)
    }


def _assert_chain_is_adjacent(database: Database, meter_id: str) -> None:
    """区间链必须严格由实际相邻采样构成：连续、无缺口、无悬挂。"""
    samples = database.list_samples(meter_id, limit=2000)
    intervals = database.list_intervals(meter_id, limit=2000)
    if len(samples) < 2:
        assert intervals == []
        return
    assert len(intervals) == len(samples) - 1
    ts_list = [s.ts for s in samples]
    for iv, prev_ts, curr_ts in zip(intervals, ts_list, ts_list[1:]):
        assert iv.start_ts == prev_ts
        assert iv.end_ts == curr_ts
        assert iv.start_count == next(s.count for s in samples if s.ts == prev_ts)
        assert iv.end_count == next(s.count for s in samples if s.ts == curr_ts)


# ---------------------------------------------------------------- 区间规则


def test_first_backfill_builds_adjacent_intervals_with_increments(db: Database) -> None:
    result = db.backfill(
        "MP-1",
        [
            (300, "S1", 1000),
            (100, "S1", 100),
            (200, "S1", 400),
        ],
    )
    assert result.inserted == 3
    assert result.skipped_duplicates == 0
    assert result.rebuilt_intervals == 2
    intervals = result.intervals
    assert [(iv.start_ts, iv.end_ts) for iv in intervals] == [(100, 200), (200, 300)]
    assert all(iv.status == STATUS_OK for iv in intervals)
    assert [iv.increment for iv in intervals] == [300, 600]
    _assert_chain_is_adjacent(db, "MP-1")


def test_meter_change_marks_both_edges_pending_without_increment(db: Database) -> None:
    db.backfill(
        "MP-1",
        [
            (100, "S1", 1000),
            (200, "S1", 1200),
            (300, "S2", 0),
            (400, "S2", 50),
        ],
    )
    ivm = _iv_map(db, "MP-1")
    assert ivm[(100, 200)]["status"] == STATUS_OK
    assert ivm[(100, 200)]["increment"] == 200
    # 换表区间：两端序列号不同，待核对且不给增量
    assert ivm[(200, 300)] == {
        "status": STATUS_PENDING,
        "increment": None,
        "reason": REASON_METER_CHANGED,
        "start_serial": "S1",
        "end_serial": "S2",
    }
    # 新表自身的累计区间仍然正常给增量
    assert ivm[(300, 400)]["status"] == STATUS_OK
    assert ivm[(300, 400)]["increment"] == 50
    _assert_chain_is_adjacent(db, "MP-1")


def test_count_decrease_and_uint32_rollover_pending(db: Database) -> None:
    db.backfill(
        "MP-1",
        [
            (100, "S1", 1000),
            (200, "S1", 900),  # 同表计数下降
        ],
    )
    ivm = _iv_map(db, "MP-1")
    assert ivm[(100, 200)]["status"] == STATUS_PENDING
    assert ivm[(100, 200)]["increment"] is None
    assert ivm[(100, 200)]["reason"] == REASON_COUNT_DECREASED

    # uint32 回卷不做隐式补偿，同样待核对，避免重复计量
    db.backfill("MP-1", [(300, "S1", 4294967295), (400, "S1", 5)])
    ivm = _iv_map(db, "MP-1")
    # 900 -> 4294967295 仍在单调上升，正常给增量
    assert ivm[(200, 300)]["status"] == STATUS_OK
    assert ivm[(200, 300)]["increment"] == 4294967295 - 900
    # 回卷点 4294967295 -> 5：不自动加 2^32 补偿，待核对
    assert ivm[(300, 400)]["status"] == STATUS_PENDING
    assert ivm[(300, 400)]["increment"] is None
    assert ivm[(300, 400)]["reason"] == REASON_COUNT_DECREASED
    _assert_chain_is_adjacent(db, "MP-1")


def test_equal_count_gives_zero_increment(db: Database) -> None:
    db.backfill("MP-1", [(100, "S1", 500), (200, "S1", 500)])
    ivm = _iv_map(db, "MP-1")
    assert ivm[(100, 200)]["status"] == STATUS_OK
    assert ivm[(100, 200)]["increment"] == 0


# ---------------------------------------------------------------- 重复与冲突


def test_duplicate_same_second_same_content_skipped_in_and_across_batches(db: Database) -> None:
    r1 = db.backfill(
        "MP-1",
        [
            (100, "S1", 100),
            (100, "S1", 100),  # 批内完全重复
            (200, "S1", 300),
        ],
    )
    assert r1.inserted == 2
    assert r1.skipped_duplicates == 1

    r2 = db.backfill(
        "MP-1",
        [
            (100, "S1", 100),  # 与库内完全相同
            (300, "S1", 400),
        ],
    )
    assert r2.inserted == 1
    assert r2.skipped_duplicates == 1
    assert len(db.list_samples("MP-1")) == 3
    _assert_chain_is_adjacent(db, "MP-1")


def test_in_batch_conflict_rejects_whole_batch(db: Database) -> None:
    with pytest.raises(BackfillConflict):
        db.backfill(
            "MP-1",
            [
                (100, "S1", 100),
                (200, "S1", 300),
                (100, "S1", 101),  # 同秒内容冲突
            ],
        )
    assert db.list_samples("MP-1") == []
    assert db.list_intervals("MP-1") == []


def test_stored_conflict_rejects_whole_batch_and_rolls_back_new_rows(db: Database) -> None:
    db.backfill("MP-1", [(100, "S1", 100), (200, "S1", 300)])
    with pytest.raises(BackfillConflict):
        db.backfill(
            "MP-1",
            [
                (150, "S1", 200),  # 新读数，本应插入
                (100, "S1", 999),  # 与库内冲突
            ],
        )
    # 整批回滚：新读数不存在，旧区间保持原样
    assert [s.ts for s in db.list_samples("MP-1")] == [100, 200]
    assert [(iv.start_ts, iv.end_ts) for iv in db.list_intervals("MP-1")] == [(100, 200)]


# ---------------------------------------------------------------- 局部重建


def test_middle_reading_replaces_only_the_split_interval(db: Database) -> None:
    db.backfill("MP-1", [(100, "S1", 100), (400, "S1", 1000)])
    assert [(iv.start_ts, iv.end_ts) for iv in db.list_intervals("MP-1")] == [(100, 400)]

    result = db.backfill("MP-1", [(250, "S1", 400)])
    # 只重建 1 个受影响区间 -> 替换为 2 个
    assert result.rebuilt_intervals == 2
    ivm = _iv_map(db, "MP-1")
    assert set(ivm) == {(100, 250), (250, 400)}
    assert ivm[(100, 250)]["increment"] == 300
    assert ivm[(250, 400)]["increment"] == 600
    _assert_chain_is_adjacent(db, "MP-1")


def test_windowed_rebuild_keeps_intervals_outside_window(db: Database) -> None:
    db.backfill(
        "MP-1",
        [
            (100, "S1", 0),
            (200, "S1", 10),
            (300, "S1", 20),
            (400, "S1", 30),
            (500, "S1", 40),
        ],
    )
    # 在 200..300 之间插入读数，仅 (200,300) 被拆开，其他区间原样保留
    db.backfill("MP-1", [(250, "S1", 15)])
    ivm = _iv_map(db, "MP-1")
    assert set(ivm) == {(100, 200), (200, 250), (250, 300), (300, 400), (400, 500)}
    assert ivm[(100, 200)]["increment"] == 10
    assert ivm[(300, 400)]["increment"] == 10
    assert ivm[(400, 500)]["increment"] == 10
    _assert_chain_is_adjacent(db, "MP-1")


def test_append_on_both_ends_and_fill_gap(db: Database) -> None:
    db.backfill("MP-1", [(300, "S1", 300)])
    db.backfill("MP-1", [(100, "S1", 100), (500, "S1", 500)])
    assert [(iv.start_ts, iv.end_ts) for iv in db.list_intervals("MP-1")] == [
        (100, 300),
        (300, 500),
    ]
    db.backfill("MP-1", [(200, "S1", 200), (400, "S1", 400)])
    _assert_chain_is_adjacent(db, "MP-1")
    assert [iv.increment for iv in db.list_intervals("MP-1")] == [100, 100, 100, 100]


def test_meter_points_are_isolated(db: Database) -> None:
    db.backfill("MP-A", [(100, "S1", 10), (200, "S1", 30)])
    db.backfill("MP-B", [(100, "S9", 0), (200, "S8", 0)])  # 换表 -> 待核对
    assert len(db.list_intervals("MP-A")) == 1
    iv_b = db.list_intervals("MP-B")[0]
    assert iv_b.status == STATUS_PENDING
    assert db.list_samples("MP-A")[0].meter_id == "MP-A"


# ---------------------------------------------------------------- 查询


def test_interval_query_filters(db: Database) -> None:
    db.backfill(
        "MP-1",
        [
            (100, "S1", 0),
            (200, "S1", 10),
            (300, "S2", 0),
            (400, "S2", 5),
            (500, "S2", 9),
        ],
    )
    pending = db.list_intervals("MP-1", status=STATUS_PENDING)
    assert [(iv.start_ts, iv.end_ts) for iv in pending] == [(200, 300)]
    ok = db.list_intervals("MP-1", status=STATUS_OK)
    assert [(iv.start_ts, iv.end_ts) for iv in ok] == [(100, 200), (300, 400), (400, 500)]
    windowed = db.list_intervals("MP-1", start_from=300, start_to=400)
    assert [(iv.start_ts, iv.end_ts) for iv in windowed] == [(300, 400), (400, 500)]
    paged = db.list_intervals("MP-1", limit=2, offset=1)
    assert [(iv.start_ts, iv.end_ts) for iv in paged] == [(200, 300), (300, 400)]
    assert db.list_intervals("MP-UNKNOWN") == []


# ---------------------------------------------------------------- 并发


def test_concurrent_backfill_always_yields_adjacent_chain(db: Database) -> None:
    n_threads = 8
    per_shard = 25
    total = n_threads * per_shard  # 200 个不同采样秒
    base_ts = 1_730_000_000

    def worker(thread_idx: int) -> None:
        ts_values = [base_ts + (thread_idx + k * n_threads) * 60 for k in range(per_shard)]
        items = [(ts, "S1", 100 + (ts - base_ts)) for ts in ts_values]
        # 每线程再带一个公共重复点，验证并发跳过不报错
        items.append((base_ts, "S1", 100))
        db.backfill("MP-CONC", items)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    samples = db.list_samples("MP-CONC", limit=2000)
    assert len(samples) == total
    _assert_chain_is_adjacent(db, "MP-CONC")
    # 全部同表且计数严格递增 -> 所有区间 ok，增量之和等于首尾计数差
    intervals = db.list_intervals("MP-CONC", limit=2000)
    assert all(iv.status == STATUS_OK for iv in intervals)
    assert sum(iv.increment for iv in intervals) == samples[-1].count - samples[0].count


def test_concurrent_conflicting_batches_leave_db_consistent(db: Database) -> None:
    db.backfill("MP-1", [(100, "S1", 100), (400, "S1", 400)])
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            db.backfill(
                "MP-1",
                [
                    (200 + idx, "S1", 200 + idx),       # 各自不同的新采样
                    (100, "S1", 1000 + idx),            # 对同一秒发起冲突写入
                ],
            )
        except BackfillConflict as exc:  # 只允许冲突错误
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert len(errors) == 6
    # 所有批次都因冲突整体回滚，库内只剩最初两条采样与一个区间
    assert [s.ts for s in db.list_samples("MP-1")] == [100, 400]
    assert [(iv.start_ts, iv.end_ts) for iv in db.list_intervals("MP-1")] == [(100, 400)]
    _assert_chain_is_adjacent(db, "MP-1")


# ---------------------------------------------------------------- API 集成


@pytest.fixture
def test_db(tmp_path) -> Database:
    database = Database(str(tmp_path / "api.db"))
    app.dependency_overrides[get_database] = lambda: database
    try:
        yield database
    finally:
        app.dependency_overrides.clear()
        database.close()


async def _aclient() -> httpx.AsyncClient:
    # httpx 0.28 的 ASGITransport 仅支持异步客户端
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_api_backfill_query_and_meter_change_demo(test_db: Database) -> None:
    async def scenario() -> None:
        payload = {
            "meter_id": "MP-LINE-01",
            "samples": [
                {"ts": 1730000600, "meter_serial": "METER-B-0002", "count": 120},
                {"ts": 1730000000, "meter_serial": "METER-A-0001", "count": 100000},
                {"ts": 1730000300, "meter_serial": "METER-A-0001", "count": 100900},
                {"ts": 1730000450, "meter_serial": "METER-B-0002", "count": 60},
            ],
        }
        async with await _aclient() as client:
            resp = await client.post("/backfill", json=payload)
            assert resp.status_code == 200
            body = resp.json()
            assert body["inserted"] == 4
            assert body["rebuilt_intervals"] == 3
            statuses = [
                (iv["start_ts"], iv["status"], iv["increment"], iv["pending_reason"])
                for iv in body["intervals"]
            ]
            assert statuses == [
                (1730000000, STATUS_OK, 900, None),
                (1730000300, STATUS_PENDING, None, REASON_METER_CHANGED),
                (1730000450, STATUS_OK, 60, None),
            ]

            # 补进中间读数：旧区间被拆换
            resp2 = await client.post(
                "/backfill",
                json={
                    "meter_id": "MP-LINE-01",
                    "samples": [
                        {"ts": 1730000180, "meter_serial": "METER-A-0001", "count": 100540}
                    ],
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["rebuilt_intervals"] == 2
            keys = [(iv["start_ts"], iv["end_ts"]) for iv in resp2.json()["intervals"]]
            assert keys == [
                (1730000000, 1730000180),
                (1730000180, 1730000300),
                (1730000300, 1730000450),
                (1730000450, 1730000600),
            ]

            # 查询接口：默认全量、状态过滤、采样核对
            listed = (await client.get("/intervals/MP-LINE-01")).json()
            assert listed["total"] == 4
            pending = (
                await client.get(
                    "/intervals/MP-LINE-01", params={"status": "pending_review"}
                )
            ).json()
            assert [(iv["start_ts"], iv["end_ts"]) for iv in pending["intervals"]] == [
                (1730000300, 1730000450)
            ]
            samples = (await client.get("/samples/MP-LINE-01")).json()
            assert samples["total"] == 5

            # 重复提交相同内容：跳过且区间不变
            dup = await client.post("/backfill", json=payload)
            assert dup.status_code == 200
            assert dup.json()["inserted"] == 0
            assert dup.json()["skipped_duplicates"] == 4

    asyncio.run(scenario())


def test_api_conflict_returns_409_and_rejects_whole_batch(test_db: Database) -> None:
    async def scenario() -> None:
        async with await _aclient() as client:
            resp0 = await client.post(
                "/backfill",
                json={
                    "meter_id": "MP-1",
                    "samples": [{"ts": 100, "meter_serial": "S1", "count": 10}],
                },
            )
            assert resp0.status_code == 200
            resp = await client.post(
                "/backfill",
                json={
                    "meter_id": "MP-1",
                    "samples": [
                        {"ts": 200, "meter_serial": "S1", "count": 20},
                        {"ts": 100, "meter_serial": "S1", "count": 999},
                    ],
                },
            )
            assert resp.status_code == 409
            detail = resp.json()
            assert detail["ts"] == 100
            assert detail["existing"]["count"] == 10
            assert detail["incoming"]["count"] == 999
            # 整批回滚：200 秒的新采样不存在
            assert (await client.get("/samples/MP-1")).json()["total"] == 1

    asyncio.run(scenario())


def test_api_validation_errors(test_db: Database) -> None:
    async def scenario() -> None:
        async with await _aclient() as client:
            # 空批次
            r1 = await client.post(
                "/backfill", json={"meter_id": "MP-1", "samples": []}
            )
            assert r1.status_code == 422
            # count 超出 uint32
            r2 = await client.post(
                "/backfill",
                json={
                    "meter_id": "MP-1",
                    "samples": [{"ts": 100, "meter_serial": "S1", "count": 2 ** 32}],
                },
            )
            assert r2.status_code == 422
            # 非法 status 过滤
            r3 = await client.get("/intervals/MP-1", params={"status": "bogus"})
            assert r3.status_code == 422

    asyncio.run(scenario())
