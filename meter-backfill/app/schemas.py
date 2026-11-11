"""请求 / 响应模型（Pydantic v2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .store import STATUS_OK, STATUS_PENDING

#: 与试采同口径：32 位无符号累计计数范围。
UINT32_MAX = 0xFFFFFFFF


class BackfillItem(BaseModel):
    """单条补传采样。"""

    ts: int = Field(
        ...,
        ge=0,
        description="UTC 采样时间，秒级 Unix 时间戳",
        examples=[1730000000],
    )
    meter_serial: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="电表序列号（换表后会变化）",
        examples=["METER-A-0001"],
    )
    count: int = Field(
        ...,
        ge=0,
        le=UINT32_MAX,
        description="32 位无符号累计计数，与试采同口径（uint32，高字在前拼接后的值）",
        examples=[100000],
    )


class BackfillRequest(BaseModel):
    """一批补传：每批只提交一个计量点的采样，批内顺序不限。"""

    meter_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="计量点标识（能耗库中的用电回路 / 测点编号）",
        examples=["MP-LINE-01"],
    )
    samples: list[BackfillItem] = Field(
        ...,
        min_length=1,
        description="本计量点的补传采样列表，顺序不限；同一秒重复提交相同内容会跳过",
    )


class IntervalOut(BaseModel):
    meter_id: str = Field(..., description="计量点标识")
    start_ts: int = Field(..., description="区间起点采样的 UTC 秒")
    end_ts: int = Field(..., description="区间终点采样的 UTC 秒")
    start_meter_serial: str = Field(..., description="起点采样的电表序列号")
    end_meter_serial: str = Field(..., description="终点采样的电表序列号")
    start_count: int = Field(..., description="起点采样的 32 位无符号累计计数")
    end_count: int = Field(..., description="终点采样的 32 位无符号累计计数")
    status: str = Field(
        ...,
        description=(
            f"区间状态：{STATUS_OK}=同表且计数未下降，可直接取增量；"
            f"{STATUS_PENDING}=换表或计数下降，待人工核对，不给出增量"
        ),
    )
    increment: int | None = Field(
        ...,
        description="区间用电量增量（后值减前值）；待核对区间为 null",
    )
    pending_reason: str | None = Field(
        ...,
        description="待核对原因：meter_serial_changed（换表）/ count_decreased（计数下降）；ok 区间为 null",
    )


class SampleOut(BaseModel):
    meter_id: str
    ts: int = Field(..., description="UTC 采样秒")
    meter_serial: str
    count: int


class BackfillResponse(BaseModel):
    meter_id: str
    inserted: int = Field(..., description="本批新写入的采样条数")
    skipped_duplicates: int = Field(
        ..., description="本批中被跳过的完全重复采样条数（同计量点同秒且内容相同）"
    )
    rebuilt_intervals: int = Field(
        ..., description="本批提交后重建（含新增与替换）的区间条数"
    )
    intervals: list[IntervalOut] = Field(
        ..., description="提交完成后该计量点的全部实际相邻区间"
    )


class IntervalListResponse(BaseModel):
    meter_id: str
    total: int = Field(..., description="本次返回的区间条数")
    limit: int
    offset: int
    intervals: list[IntervalOut]


class SampleListResponse(BaseModel):
    meter_id: str
    total: int
    samples: list[SampleOut]
