"""本地模拟 Modbus TCP 电表（pymodbus 3.16 simulator API）。

保持寄存器 0..99：
- 地址 0..1、2..3 预置 32 位无符号计数（高字在前），用于双字测点演示；
- 地址 10..13 预置单字演示值；
- 其余寄存器按确定性公式填充，便于人工核对。
"""

from __future__ import annotations

import asyncio
import logging
import os

from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

SIZE = 100
values = [(addr * 37 + 11) & 0xFFFF for addr in range(SIZE)]

# 预置演示值（高字在前）
values[0], values[1] = 0x0001, 0x86A0  # 32 位 = 100000
values[2], values[3] = 0x0000, 0x0064  # 32 位 = 100
values[10:14] = [1234, 5678, 0xFFFF, 42]

# 0 基地址：协议地址 0 对应 values[0]
device = SimDevice(
    id=1,
    simdata=[
        SimData(address=0, count=SIZE, values=values, datatype=DataType.REGISTERS)
    ],
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("METER_PORT", "502"))
    await StartAsyncTcpServer(context=device, address=("0.0.0.0", port))


if __name__ == "__main__":
    asyncio.run(main())
