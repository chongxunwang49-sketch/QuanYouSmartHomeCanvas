"""
测试共用工具。

存在的理由：解析链路的**第一跳是真实的图片预检**（AC-27），
所以任何要跑解析的用例都必须给一张能通过预检的图。用占位字符串
（`"data:image/png;base64,AAAA"`）会被正确地拦下，于是用例挂在
预检上而不是挂在它真正想测的地方。

造图逻辑放这里而不是 conftest 的 fixture 里：`_state()` 这类
**模块级辅助函数**拿不到 fixture，只能拿普通函数。
"""

from __future__ import annotations

import base64
import functools
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(ROOT))


@functools.lru_cache(maxsize=1)
def floorplan_png(width: int = 1280, height: int = 960) -> bytes:
    """
    一张**能通过图片预检**的合成户型图。

    画的是白底 + 黑线框 + 内部隔墙 + 若干标注短线。

    ⚠️ **不能用纯白图**：那会命中预检里"近乎纯色"那条规则，
    同样过不了 —— 于是用例还是挂在预检上。

    `lru_cache` 是为了别在几十个用例里重复画同一张图。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    m = int(min(width, height) * 0.09)

    # 外墙
    d.rectangle([m, m, width - m, height - m], outline="black", width=4)
    # 内部隔墙，切出几个"房间"
    d.line([width // 2, m, width // 2, height - m], fill="black", width=3)
    d.line([m, height // 2, width - m, height // 2], fill="black", width=3)
    # 门洞（留白的一段）
    d.rectangle(
        [width // 2 - 8, height // 2 - 40, width // 2 + 8, height // 2 - 10],
        fill="white",
    )
    # 尺寸标注（用短线模拟，不依赖字体）
    for i in range(6):
        y = m + 40 + i * 30
        d.line([m + 20, y, m + 220, y], fill="black", width=2)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@functools.lru_cache(maxsize=1)
def floorplan_data_uri() -> str:
    """上面那张图的 data URI —— 直接塞进 `image_ref`。"""
    return "data:image/png;base64," + base64.b64encode(floorplan_png()).decode()


__all__ = ["floorplan_png", "floorplan_data_uri"]
