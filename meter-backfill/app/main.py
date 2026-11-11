"""FastAPI：电表读数断网缓存补传服务。

接口：

- ``POST /backfill``：按批提交单个计量点的补传采样（顺序不限），
  采样与相邻采样区间在同一事务内提交；
- ``GET /intervals/{meter_id}``：按计量点查询实际相邻采样构成的用电区间；
- ``GET /samples/{meter_id}``：按计量点查询已保存的原始采样（辅助核对）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import __version__
from .schemas import (
    BackfillRequest,
    BackfillResponse,
    IntervalListResponse,
    IntervalOut,
    SampleListResponse,
    SampleOut,
)
from .store import BackfillConflict, Database, get_database

BACKFILL_BODY_EXAMPLES = {
    "meter_change_demo": {
        "summary": "换表期间补传（乱序，含换表点与待核对区间）",
        "description": (
            "计量点 MP-LINE-01 在断网期间缓存的读数，乱序提交。"
            "1730000300 秒更换电表（序列号 METER-A-0001 -> METER-B-0002），"
            "换表前后的两个区间标记为 pending_review 且不给增量；"
            "同表且计数未下降的区间给出后值减前值的增量。"
        ),
        "value": {
            "meter_id": "MP-LINE-01",
            "samples": [
                {"ts": 1730000600, "meter_serial": "METER-B-0002", "count": 120},
                {"ts": 1730000000, "meter_serial": "METER-A-0001", "count": 100000},
                {"ts": 1730000300, "meter_serial": "METER-A-0001", "count": 100900},
                {"ts": 1730000450, "meter_serial": "METER-B-0002", "count": 60},
            ],
        },
    },
    "insert_middle_reading_demo": {
        "summary": "补进中间读数（拆换旧区间，只改受影响区间）",
        "description": (
            "在已存在 1730000000 -> 1730000600 区间的前提下，补入 1730000180 的"
            "中间读数：旧区间被删除并替换为两个实际相邻的新区间，其余区间不变。"
            "请先执行换表期间补传示例，再执行本例。"
        ),
        "value": {
            "meter_id": "MP-LINE-01",
            "samples": [
                {"ts": 1730000180, "meter_serial": "METER-A-0001", "count": 100540}
            ],
        },
    },
    "duplicate_and_conflict_demo": {
        "summary": "重复跳过 / 同秒冲突整批拒绝",
        "description": (
            "1730000000 秒与已存记录完全相同则跳过；若把该秒 count 改为其他值，"
            "整批返回 409 且库内无任何改动。"
        ),
        "value": {
            "meter_id": "MP-LINE-01",
            "samples": [
                {"ts": 1730000000, "meter_serial": "METER-A-0001", "count": 100000},
                {"ts": 1730000720, "meter_serial": "METER-B-0002", "count": 180},
            ],
        },
    },
}

app = FastAPI(
    title="电表读数补传与用电区间修正服务",
    version=__version__,
    description=(
        "采集网关断网时缓存电表读数，恢复后通过本服务补传。每批提交一个计量点"
        "的采样（顺序不限），每条含 UTC 采样秒、电表序列号和与试采同口径的 "
        "32 位无符号累计计数。SQLite 同时保存采样与相邻采样之间的区间：\n\n"
        "- 两端序列号相同且计数未下降：增量 = 后值 - 前值；\n"
        "- 换表（序列号不同）或计数下降：区间标记为待核对（pending_review），"
        "不给出增量，避免重复计量；\n"
        "- 补进中间读数只重建受影响窗口，被拆开的旧区间会被替换；\n"
        "- 同计量点同秒记录相同则跳过，内容冲突则整批拒绝（409）；\n"
        "- 采样与区间在同一事务内提交，并发补传后只能查到实际相邻的区间。"
    ),
)


@app.exception_handler(BackfillConflict)
async def _conflict_handler(_request, exc: BackfillConflict) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "meter_id": exc.meter_id,
            "ts": exc.ts,
            "existing": {
                "meter_serial": exc.existing.meter_serial,
                "count": exc.existing.count,
            },
            "incoming": {
                "meter_serial": exc.incoming.meter_serial,
                "count": exc.incoming.count,
            },
        },
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "meter-backfill", "version": __version__, "docs": "/docs"}


@app.post(
    "/backfill",
    response_model=BackfillResponse,
    status_code=status.HTTP_200_OK,
    summary="补传一批同一计量点的采样，并原子更新相邻采样区间",
    openapi_extra={"requestBody": {"content": {"application/json": {"examples": BACKFILL_BODY_EXAMPLES}}}},
    responses={
        409: {"description": "同计量点同秒已存在不同内容的记录，整批拒绝（无任何写入）"},
        422: {"description": "请求体校验失败（空批次、计数超出 uint32 等）"},
    },
)
def post_backfill(
    req: BackfillRequest,
    db: Database = Depends(get_database),
) -> BackfillResponse:
    items = [(s.ts, s.meter_serial, s.count) for s in req.samples]
    try:
        result = db.backfill(req.meter_id, items)
    except ValueError as exc:  # 理论上被 pydantic min_length 拦截，防御性处理
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BackfillResponse(
        meter_id=result.meter_id,
        inserted=result.inserted,
        skipped_duplicates=result.skipped_duplicates,
        rebuilt_intervals=result.rebuilt_intervals,
        intervals=[IntervalOut(**iv.__dict__) for iv in result.intervals],
    )


@app.get(
    "/intervals/{meter_id}",
    response_model=IntervalListResponse,
    summary="按计量点查询相邻采样区间（供能耗库修正用电区间）",
    responses={200: {"description": "区间按起始采样秒升序，仅包含实际相邻采样构成的区间"}},
)
def get_intervals(
    meter_id: str,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="按区间状态过滤：ok / pending_review",
        examples=["pending_review"],
    ),
    start_from: int | None = Query(
        default=None, ge=0, description="仅返回起点采样秒 >= 该值的区间"
    ),
    start_to: int | None = Query(
        default=None, ge=0, description="仅返回起点采样秒 <= 该值的区间"
    ),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_database),
) -> IntervalListResponse:
    if status_filter is not None and status_filter not in {"ok", "pending_review"}:
        raise HTTPException(
            status_code=422,
            detail="status 只允许取 ok 或 pending_review",
        )
    intervals = db.list_intervals(
        meter_id,
        status=status_filter,
        start_from=start_from,
        start_to=start_to,
        limit=limit,
        offset=offset,
    )
    return IntervalListResponse(
        meter_id=meter_id,
        total=len(intervals),
        limit=limit,
        offset=offset,
        intervals=[IntervalOut(**iv.__dict__) for iv in intervals],
    )


@app.get(
    "/samples/{meter_id}",
    response_model=SampleListResponse,
    summary="按计量点查询已保存的原始补传采样（辅助核对）",
)
def get_samples(
    meter_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    db: Database = Depends(get_database),
) -> SampleListResponse:
    samples = db.list_samples(meter_id, limit=limit, offset=offset)
    return SampleListResponse(
        meter_id=meter_id,
        total=len(samples),
        samples=[SampleOut(**s.__dict__) for s in samples],
    )
