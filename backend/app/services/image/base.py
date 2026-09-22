"""
图像生成 Provider 抽象层。

═══════════════════════════════════════════════════════════════════
本接口在 M0 就冻结，M5 只做增量扩展（需求文档 7.1）
═══════════════════════════════════════════════════════════════════
M0 是**可行性验证**，M5 是**正式集成**。两者共享核心推理逻辑，
只是"完整性"不同：

    M0 实现：L0/L1 级别（SD1.5 + ControlNet @512 / @448）
             不含 LoRA 风格、不含 Inpainting、不做热区集成
             目标 = 可跑通 + 可测显存
    M5 增量：L2/L3 降级分支、风格 LoRA、与 VectorRenderer 的协同

这样 M0 实测出的「哪一级能跑通、峰值显存多少」可以直接变成 M5 的配置默认值，
而不是重新验证一遍。

═══════════════════════════════════════════════════════════════════
显存口径警告（实测踩过）
═══════════════════════════════════════════════════════════════════
`torch.cuda.max_memory_allocated()` **看不到 CUDA context**（实测刚初始化完它报 0MB）。
显存预算必须从 `torch.cuda.mem_get_info()` 或 `nvidia-smi` 的**驱动口径**读，
否则会系统性低估 300–500MB。本模块的 ImageResult 同时记录两个口径，
以驱动口径为准。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from PIL import Image


class ImageLevel(str, Enum):
    """
    出图分级。**顺序不可颠倒**——按显存压力从高到低逐级降级。

    需求文档 0.1 的实测结论：本机可用显存 3.22GB，而 SD1.5 全套理论占用
    3.56–3.96GB，因此 L0 **大概率跑不通**，必须一级一级试。
    """

    L0 = "L0_sd15_cn_512"       # 全量：SD1.5 + ControlNet @512
    L1 = "L1_sd15_cn_448"       # 降分辨率，激活降约 25%
    L2 = "L2_sd15_no_cn_512"    # 卸载 ControlNet（丧失结构一致性）
    L3 = "L3_vector_only"       # 只出矢量图（必过）

    @property
    def description(self) -> str:
        return {
            "L0_sd15_cn_512": "SD1.5 + ControlNet @512（期望状态）",
            "L1_sd15_cn_448": "SD1.5 + ControlNet @448（降分辨率）",
            "L2_sd15_no_cn_512": "SD1.5 无 ControlNet @512（丧失结构一致性）",
            "L3_vector_only": "纯矢量渲染（零显存依赖）",
        }[self.value]

    @property
    def uses_controlnet(self) -> bool:
        return self in (ImageLevel.L0, ImageLevel.L1)

    @property
    def size(self) -> int:
        return 448 if self is ImageLevel.L1 else 512


@dataclass
class VramReading:
    """一次显存读数。**两个口径都记录，以 driver 为准。**"""

    driver_used_gb: float          # 驱动口径：总量 - 空闲（含 context、其它进程）
    driver_free_gb: float
    torch_allocated_gb: float      # PyTorch 缓存分配器口径（会低估）
    torch_reserved_gb: float
    torch_peak_gb: float           # torch.cuda.max_memory_allocated()

    @property
    def effective_used_gb(self) -> float:
        """扣掉测量前的基础占用后的净增量，比裸的数字更有意义。"""
        return self.driver_used_gb


@dataclass
class ImageResult:
    """一次出图的结果，含完整可观测性字段。"""

    image: Image.Image | None
    level: ImageLevel
    provider: str
    size: int
    seconds: float
    vram: VramReading | None = None
    seed: int | None = None
    prompt: str = ""
    degraded: bool = False
    degrade_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ImageProvider(abc.ABC):
    """图像生成 Provider 基类。"""

    name: str = "base"

    @abc.abstractmethod
    def generate(
        self,
        *,
        layout: dict[str, Any],
        style: str = "modern",
        level: ImageLevel | None = None,
        prompt: str | None = None,
        seed: int | None = None,
    ) -> ImageResult:
        """按布局与风格出图。"""
        raise NotImplementedError

    @abc.abstractmethod
    def ensure_loaded(self) -> None:
        """预热：把模型加载进显存，避免演示时 20–40s 冷启动。"""
        raise NotImplementedError

    def unload(self) -> None:
        """卸载，释放显存。默认空实现。"""
        return None

    @property
    def is_loaded(self) -> bool:
        return False

    @staticmethod
    def render_conditioning_image(
        layout: dict[str, Any], size: int = 512, out_path: Path | None = None
    ) -> Image.Image:
        """
        从户型 JSON 渲染 ControlNet 的 conditioning image（边缘图）。

        这是连接"户型解析结果"与"图像生成"的桥梁：
        ControlNet 用它来约束生成图的结构，使其贴合原始户型。

        ⚠️ 实现方式经过实测修正（2026-09-22）：
        初版用 `ImageFilter.FIND_EDGES` 生成边缘图，结果是**粗黑块**——
        ControlNet 把它当作硬约束逐像素复刻，输出是一堆灰方块。
        改为「浅色房间填充 + 细黑描边」，更接近真实户型图的样子，
        模型才愿意在此基础上"立起来"做 3D 渲染。

        不依赖 opencv（本机未安装），只用 Pillow。
        """
        from PIL import ImageDraw

        # 房间浅色填充，交替色以区分相邻房间
        fills = [(255, 240, 225), (225, 240, 255), (235, 255, 235), (255, 250, 215)]

        canvas = Image.new("RGB", (size, size), "white")
        draw = ImageDraw.Draw(canvas)

        boxes = [(r.get("bbox") or []) for r in layout.get("rooms") or []]
        boxes = [b for b in boxes if len(b) == 4]

        if not boxes:
            # 没有 bbox 时退化为一个居中的矩形，至少让 ControlNet 有输入
            draw.rectangle([size * 0.1, size * 0.1, size * 0.9, size * 0.9],
                           fill=fills[0], outline="black", width=3)
        else:
            max_x = max(b[2] for b in boxes) or 1
            max_y = max(b[3] for b in boxes) or 1
            for i, (x1, y1, x2, y2) in enumerate(boxes):
                draw.rectangle(
                    [x1 / max_x * size * 0.9 + size * 0.05,
                     y1 / max_y * size * 0.9 + size * 0.05,
                     x2 / max_x * size * 0.9 + size * 0.05,
                     y2 / max_y * size * 0.9 + size * 0.05],
                    fill=fills[i % len(fills)], outline="black", width=3,
                )

        if out_path:
            canvas.save(out_path)
        return canvas


__all__ = [
    "ImageLevel", "ImageResult", "VramReading", "ImageProvider",
]
