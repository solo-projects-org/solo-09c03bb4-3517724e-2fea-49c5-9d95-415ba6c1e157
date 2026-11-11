"""SQLite 存储层：补传采样与相邻采样区间。

设计要点：

- ``samples`` 保存原始补传读数，按 ``(meter_id, ts)`` 唯一；
- ``intervals`` 保存同一计量点上相邻两次采样之间的区间，每条区间记录前值与
  后值的电表序列号、32 位无符号计数，并给出区间状态与增量；
- 所有写操作在单个 ``IMMEDIATE`` 事务中完成（采样与区间一起提交），配合
  WAL 模式，保证并发补传串行化后，任何时刻查到的区间都只连接实际相邻的采样。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

#: 32 位无符号计数取值范围，与试采同口径（高字在前拼接后的 uint32）。
UINT32_MAX = 0xFFFFFFFF

#: 区间状态：序列号相同且计数未下降，增量 = 后值 - 前值。
STATUS_OK = "ok"
#: 区间状态：换表（两端序列号不同）或计数下降（含 uint32 回卷），待人工核对，不给增量。
STATUS_PENDING = "pending_review"

#: 待核对原因：区间两端电表序列号不同（换表期间）。
REASON_METER_CHANGED = "meter_serial_changed"
#: 待核对原因：后值计数小于前值，累计量不应下降（也覆盖 32 位回卷场景）。
REASON_COUNT_DECREASED = "count_decreased"


class BackfillConflict(Exception):
    """同一计量点同一秒已存在记录且内容不同，整批拒绝。"""

    def __init__(self, meter_id: str, ts: int, existing: "Sample", incoming: "Sample") -> None:
        self.meter_id = meter_id
        self.ts = ts
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"计量点 {meter_id} 在 UTC {ts} 秒已有冲突记录："
            f"已存 serial={existing.meter_serial} count={existing.count}，"
            f"本次 serial={incoming.meter_serial} count={incoming.count}"
        )


@dataclass(frozen=True)
class Sample:
    meter_id: str
    ts: int
    meter_serial: str
    count: int


@dataclass(frozen=True)
class Interval:
    meter_id: str
    start_ts: int
    end_ts: int
    start_meter_serial: str
    end_meter_serial: str
    start_count: int
    end_count: int
    status: str
    increment: int | None
    pending_reason: str | None


@dataclass(frozen=True)
class BackfillResult:
    meter_id: str
    inserted: int
    skipped_duplicates: int
    rebuilt_intervals: int
    intervals: tuple[Interval, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    meter_id     TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    meter_serial TEXT    NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (meter_id, ts)
) STRICT;

CREATE TABLE IF NOT EXISTS intervals (
    meter_id           TEXT    NOT NULL,
    start_ts           INTEGER NOT NULL,
    end_ts             INTEGER NOT NULL,
    start_meter_serial TEXT    NOT NULL,
    end_meter_serial   TEXT    NOT NULL,
    start_count        INTEGER NOT NULL,
    end_count          INTEGER NOT NULL,
    status             TEXT    NOT NULL CHECK (status IN ('ok', 'pending_review')),
    increment          INTEGER,
    pending_reason     TEXT,
    PRIMARY KEY (meter_id, start_ts)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_intervals_meter_end
    ON intervals (meter_id, end_ts);
CREATE INDEX IF NOT EXISTS idx_samples_meter_ts
    ON samples (meter_id, ts);
"""


class Database:
    """SQLite 连接封装。进程内单连接 + 写互斥，配合 WAL 支持并发读。"""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：查询接口与补传接口可能运行在不同线程，
        # 所有写访问由 self._write_lock 串行化。
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 关闭 pysqlite 隐式事务，BEGIN 由代码显式管理
        self._conn.isolation_level = None
        self._write_lock = threading.RLock()
        with self._write_lock:
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """显式 IMMEDIATE 事务：进入即拿写锁，并发补传在此排队串行化。"""
        with self._write_lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ------------------------------------------------------------------ 内部工具

    def _get_sample(self, conn: sqlite3.Connection, meter_id: str, ts: int) -> Sample | None:
        row = conn.execute(
            "SELECT meter_id, ts, meter_serial, count FROM samples "
            "WHERE meter_id = ? AND ts = ?",
            (meter_id, ts),
        ).fetchone()
        return None if row is None else Sample(**dict(row))

    # ------------------------------------------------------------------ 区间规则

    @staticmethod
    def _classify(prev: Sample, curr: Sample) -> tuple[str, int | None, str | None]:
        """按规则计算相邻两个采样之间区间的状态。

        - 两端序列号相同且计数未下降：``ok``，增量 = 后值 - 前值；
        - 序列号不同（换表）：待核对，原因 ``meter_serial_changed``；
        - 序列号相同但计数下降（含 uint32 回卷）：待核对，原因
          ``count_decreased``。不做隐式回卷补偿，避免重复计量。
        """
        if prev.meter_serial != curr.meter_serial:
            return STATUS_PENDING, None, REASON_METER_CHANGED
        if curr.count < prev.count:
            return STATUS_PENDING, None, REASON_COUNT_DECREASED
        return STATUS_OK, curr.count - prev.count, None

    def _rebuild_window(
        self,
        conn: sqlite3.Connection,
        meter_id: str,
        min_ts: int,
        max_ts: int,
    ) -> list[Interval]:
        """重建受新区间覆盖到的区间，窗口之外的既有区间保持不变。

        新区间只会落在窗口前驱采样 ``prev``（``< min_ts`` 的最大秒）与后继
        采样 ``next``（``> max_ts`` 的最小秒）之间。需要删除的旧区间恰好是
        起点秒落在 ``[prev, max_ts]`` 的区间：

        - 中间插入：删掉被拆开的那一个旧区间（起点 = prev）；
        - 窗口两端外扩：删掉窗口内及与窗口接壤的旧区间；
        - 窗口之外的旧区间起点 ``< prev``，不受影响、原样保留。

        随后重新取 ``prev..next`` 之间的全部采样，生成实际相邻区间。
        """
        conn.execute(
            "DELETE FROM intervals WHERE meter_id = ? AND start_ts >= ("
            "    SELECT COALESCE(MAX(ts), ?) FROM samples WHERE meter_id = ? AND ts < ?"
            ") AND start_ts <= ?",
            (meter_id, min_ts, meter_id, min_ts, max_ts),
        )

        rows = conn.execute(
            "SELECT meter_id, ts, meter_serial, count FROM samples "
            "WHERE meter_id = ? AND ts >= ("
            "    SELECT COALESCE(MAX(ts), ?) FROM samples WHERE meter_id = ? AND ts < ?"
            ") AND ts <= ("
            "    SELECT COALESCE(MIN(ts), ?) FROM samples WHERE meter_id = ? AND ts > ?"
            ") ORDER BY ts ASC",
            (
                meter_id,
                min_ts, meter_id, min_ts,
                max_ts, meter_id, max_ts,
            ),
        ).fetchall()
        samples = [Sample(**dict(r)) for r in rows]

        new_intervals: list[Interval] = []
        for prev, curr in zip(samples, samples[1:]):
            status, increment, reason = self._classify(prev, curr)
            new_intervals.append(
                Interval(
                    meter_id=meter_id,
                    start_ts=prev.ts,
                    end_ts=curr.ts,
                    start_meter_serial=prev.meter_serial,
                    end_meter_serial=curr.meter_serial,
                    start_count=prev.count,
                    end_count=curr.count,
                    status=status,
                    increment=increment,
                    pending_reason=reason,
                )
            )
        conn.executemany(
            "INSERT INTO intervals (meter_id, start_ts, end_ts, "
            "start_meter_serial, end_meter_serial, start_count, end_count, "
            "status, increment, pending_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    iv.meter_id, iv.start_ts, iv.end_ts,
                    iv.start_meter_serial, iv.end_meter_serial,
                    iv.start_count, iv.end_count,
                    iv.status, iv.increment, iv.pending_reason,
                )
                for iv in new_intervals
            ],
        )
        return new_intervals

    # ------------------------------------------------------------------ 写接口

    def backfill(
        self,
        meter_id: str,
        items: Sequence[tuple[int, str, int]],
    ) -> BackfillResult:
        """提交一批同一计量点的补传采样（顺序不限），采样与区间一起原子提交。

        :param items: ``(UTC 采样秒, 电表序列号, uint32 累计计数)`` 元组序列
        :raises BackfillConflict: 同秒记录内容冲突，整批拒绝（库内无任何改动）
        """
        if not items:
            raise ValueError("补传批次不能为空")

        # 批内先按秒聚合：完全相同则去重；同秒内容冲突直接整批拒绝。
        merged: dict[int, tuple[str, int]] = {}
        batch_duplicates = 0
        for ts, serial, count in items:
            if ts in merged:
                if merged[ts] == (serial, count):
                    batch_duplicates += 1
                    continue
                raise BackfillConflict(
                    meter_id,
                    ts,
                    Sample(meter_id, ts, merged[ts][0], merged[ts][1]),
                    Sample(meter_id, ts, serial, count),
                )
            merged[ts] = (serial, count)

        with self._transaction() as conn:
            # 与库内既有记录比对：相同跳过，冲突整批回滚。
            to_insert: list[Sample] = []
            skipped = batch_duplicates
            for ts, (serial, count) in merged.items():
                existing = self._get_sample(conn, meter_id, ts)
                if existing is not None:
                    if existing.meter_serial == serial and existing.count == count:
                        skipped += 1
                        continue
                    raise BackfillConflict(
                        meter_id,
                        ts,
                        existing,
                        Sample(meter_id, ts, serial, count),
                    )
                to_insert.append(Sample(meter_id, ts, meter_serial=serial, count=count))

            if to_insert:
                conn.executemany(
                    "INSERT INTO samples (meter_id, ts, meter_serial, count) "
                    "VALUES (?, ?, ?, ?)",
                    [(s.meter_id, s.ts, s.meter_serial, s.count) for s in to_insert],
                )
                rebuilt = self._rebuild_window(
                    conn,
                    meter_id,
                    min_ts=min(s.ts for s in to_insert),
                    max_ts=max(s.ts for s in to_insert),
                )
            else:
                rebuilt = []

            all_intervals = self.list_intervals(meter_id, conn=conn)

        return BackfillResult(
            meter_id=meter_id,
            inserted=len(to_insert),
            skipped_duplicates=skipped,
            rebuilt_intervals=len(rebuilt),
            intervals=tuple(all_intervals),
        )

    # ------------------------------------------------------------------ 读接口

    def list_intervals(
        self,
        meter_id: str,
        *,
        status: str | None = None,
        start_from: int | None = None,
        start_to: int | None = None,
        limit: int = 500,
        offset: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> list[Interval]:
        """按计量点查询区间，默认按起始秒升序，只返回实际相邻采样构成的区间。"""
        clauses = ["meter_id = ?"]
        params: list[object] = [meter_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if start_from is not None:
            clauses.append("start_ts >= ?")
            params.append(start_from)
        if start_to is not None:
            clauses.append("start_ts <= ?")
            params.append(start_to)
        sql = (
            "SELECT meter_id, start_ts, end_ts, start_meter_serial, "
            "end_meter_serial, start_count, end_count, status, increment, "
            "pending_reason FROM intervals WHERE "
            + " AND ".join(clauses)
            + " ORDER BY start_ts ASC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        def _fetch(c: sqlite3.Connection) -> list[Interval]:
            return [Interval(**dict(r)) for r in c.execute(sql, params).fetchall()]

        if conn is not None:  # 事务内复用同一连接，保证读到本事务的改动
            return _fetch(conn)
        with self._write_lock:
            return _fetch(self._conn)

    def list_samples(self, meter_id: str, *, limit: int = 500, offset: int = 0) -> list[Sample]:
        """按计量点查询已保存的采样（辅助核对接口），按采样秒升序。"""
        with self._write_lock:
            rows = self._conn.execute(
                "SELECT meter_id, ts, meter_serial, count FROM samples "
                "WHERE meter_id = ? ORDER BY ts ASC LIMIT ? OFFSET ?",
                (meter_id, limit, offset),
            ).fetchall()
        return [Sample(**dict(r)) for r in rows]

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()


_default_db: Database | None = None
_db_lock = threading.Lock()


def get_database() -> Database:
    """FastAPI 依赖：返回进程内默认数据库（路径由环境变量配置）。"""
    global _default_db
    if _default_db is None:
        with _db_lock:
            if _default_db is None:
                _default_db = Database(os.getenv("BACKFILL_DB", "/data/backfill.db"))
    return _default_db


def reset_default_database() -> None:
    """测试辅助：关闭并清空默认库句柄。"""
    global _default_db
    with _db_lock:
        if _default_db is not None:
            _default_db.close()
        _default_db = None
