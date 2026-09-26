"""
MCP 工具冒烟测试（AC-11）—— **真的起子进程、真的走 JSON-RPC**。

用法：
    python scripts/mcp_smoke.py            # 跑全部
    python scripts/mcp_smoke.py calc_budget

它验证的是**协议这一层**，不是业务逻辑：
    · 子进程能不能起来（Windows 上编码是老大难，见 mcp_client 的文件头）
    · 工具能不能被发现（list_tools）
    · 参数能不能传进去、结构化结果能不能取回来
业务正确性由各自的单测负责（tests/test_budget_engine.py 等）。

⚠️ **本脚本同时是"为什么内部流水线不走 MCP"那份判断的证据来源。**
   它会报出每次调用的墙钟耗时 —— 那是"起一个 Python 子进程"的全部代价，
   而内部 A-04/A-07 是同步函数调用（微秒级）。数字先量出来，
   结论才好写进注释；不然"MCP 更慢"只是一句想当然。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.mcp_client import get_mcp_client

#: 一份最小可用户型（4 个房间 + 墙体 + 门窗）。够算预算、够画图。
LAYOUT = {
    "layout_id": "layout_mcp_smoke",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5,
         "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2,
         "bbox": [440, 80, 680, 300]},
        {"name": "厨房", "type": "kitchen", "area": 7.4,
         "bbox": [130, 370, 300, 500]},
        {"name": "卫生间", "type": "bathroom", "area": 4.8,
         "bbox": [310, 370, 420, 500]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 56.9,
    "has_north_arrow": True,
    "confidence": 0.85,
}


# ⚠️ **必须在任何输出之前**把 stdout 切到 UTF-8。
#    Windows 控制台默认 GBK，打不出 ❌ / ⚠ 这类字符时会抛
#    `UnicodeEncodeError: 'gbk' codec can't encode character '\u274c'`——
#    而**这个异常发生在打印错误信息的路上**，于是真正的报错被它吃掉，
#    看到的是一个和实际问题无关的编码异常。
#
#    2026-09-26 实测就是这么撞上的：修 MCP 的 trace_id 之后跑这个脚本，
#    屏幕上只有一串 UnicodeEncodeError 的栈，看不出 MCP 到底出了什么事。
#    本项目在 init_db.py 与 derive_surface_palette.py 都踩过同一个坑 ——
#    这是第三次，所以这一段现在是**每个带 emoji 输出的脚本的标配**。
for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


def out(s: str = "") -> None:
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


async def check(module: str, arguments: dict) -> bool:
    """起子进程 → 列工具 → 调一次。返回是否成功。"""
    client = get_mcp_client()

    started = time.perf_counter()
    tools = await client.list_tools(module)
    t_list = time.perf_counter() - started

    names = [t["name"] for t in tools]
    out(f"  list_tools  {t_list:6.2f}s  发现 {len(tools)} 个工具：{names}")

    target = module if module in names else (names[0] if names else "")
    if not target:
        out("  ❌ 没有发现任何工具")
        return False

    started = time.perf_counter()
    result = await client.call_tool(target, arguments, module=module)
    t_call = time.perf_counter() - started

    if isinstance(result, dict):
        keys = list(result.keys())
        out(f"  call_tool   {t_call:6.2f}s  返回 {len(keys)} 个字段：{keys[:8]}")
    else:
        out(f"  call_tool   {t_call:6.2f}s  返回 {type(result).__name__}")

    out(f"  ⏱ 单次调用墙钟约 {t_call:.2f}s（含起子进程 + 加载 backend 包）")
    return True


async def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    failures: list[str] = []

    cases = [
        ("calc_budget", {"layout": LAYOUT, "grade": "medium"}, "预算测算"),
        ("render_layout_svg", {"layout": LAYOUT}, "矢量图渲染"),
    ]

    for module, args, label in cases:
        if only and only != module:
            continue
        out(f"\n──── {label}  mcp_servers.{module} ────")
        try:
            ok = await check(module, args)
        except Exception as e:  # noqa: BLE001
            out(f"  ❌ {type(e).__name__}: {e}")
            ok = False
        if not ok:
            failures.append(module)

    out()
    if failures:
        out(f"❌ 失败：{failures}")
        return 1
    out("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
