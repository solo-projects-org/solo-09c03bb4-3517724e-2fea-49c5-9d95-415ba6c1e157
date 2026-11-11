"""API 端到端集成测试。

在同一事件循环内启动 pymodbus 内存模拟表（随机本地端口），
通过 httpx ASGI 传输调用 FastAPI，不依赖 Docker 或固定端口。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest
from pymodbus.server import StartAsyncTcpServer

from app.main import app
from simulator.meter_sim import device


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def meter_port() -> int:
    port = _free_port()
    task = asyncio.create_task(
        StartAsyncTcpServer(context=device, address=("127.0.0.1", port))
    )
    for _ in range(50):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.2
            )
            writer.close()
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        task.cancel()
        pytest.fail("模拟 Modbus 表启动超时")
    try:
        yield port
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_read_meter_happy_path(meter_port: int) -> None:
    payload = {
        "points": [
            {"address": 0, "words": 2},
            {"address": 2, "words": 2},
            {"address": 10, "words": 1},
        ],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": meter_port,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    # 0..3 一段、10 一段 -> 2 次；读 4 + 1 = 5 个寄存器
    assert data["request_count"] == 2
    assert data["total_registers"] == 5
    values = {p["index"]: p["value"] for p in data["points"]}
    assert values == {0: 100000, 1: 100, 2: 1234}
    assert data["segments"][0]["registers"] == [1, 34464, 0, 100]


async def test_infeasible_plan_returns_422_without_device_call(meter_port: int) -> None:
    payload = {
        "points": [{"address": 200, "words": 1}],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": meter_port,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 422
    assert "无可行方案" in resp.json()["detail"]


async def test_connection_failure_returns_502_no_partial_reads() -> None:
    dead_port = _free_port()  # 无人监听
    payload = {
        "points": [
            {"address": 0, "words": 1},
            {"address": 20, "words": 1},
        ],
        "range_start": 0,
        "range_end": 99,
        "max_read": 8,
        "host": "127.0.0.1",
        "port": dead_port,
        "timeout": 0.5,
    }
    async with await _client() as client:
        resp = await client.post("/read", json=payload)
    assert resp.status_code == 502
    body = resp.json()
    assert "连接" in body["detail"]
    assert "segments" not in body  # 不返回部分读数
