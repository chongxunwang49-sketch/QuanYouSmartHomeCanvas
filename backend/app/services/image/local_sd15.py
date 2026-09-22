"""
本地 SD1.5 + LCM-LoRA + ControlNet Provider。

M0 范围：L0 / L1 级别（含 ControlNet @512 / @448），外加 L2（卸载 ControlNet）。
不含：风格 LoRA、Inpainting、热区集成 —— 这些是 M5 的增量。

═══════════════════════════════════════════════════════════════════
为什么是 SD1.5 而不是 SDXL
═══════════════════════════════════════════════════════════════════
本机实测可用显存 **3.22GB**（驱动口径）。SDXL 底模 fp16 就要 6.9GB，
必须 CPU offload，单图分钟级。SD1.5 全套 fp16 约 2.86GB，勉强塞得下。

为什么是 LCM-LoRA 而不是 SD-Turbo：
SD-Turbo 基于 SD 2.1，**SD1.5 生态的 ControlNet 全部不可用**。
LCM-LoRA 保持 SD1.5 架构，既能 4–8 步出图，又能用 SD1.5 的 ControlNet。

═══════════════════════════════════════════════════════════════════
显存与并发约束（必须全部满足，否则 OOM）
═══════════════════════════════════════════════════════════════════
- attention_slicing / vae_slicing / vae_tiling 三个都必须开
- **不启用** enable_model_cpu_offload：SD1.5 能全量上卡，offload 反而拖慢到不可用
- **全局串行**：受 _GEN_LOCK 保护，禁止并发调图（3 路并发必 OOM，见 ADR-08）
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from ...core.config import settings
from .base import ImageLevel, ImageProvider, ImageResult, VramReading

# 必须在任何 diffusers 导入前设置，否则 hf-mirror 不生效
import os

os.environ.setdefault("HF_ENDPOINT", settings.HF_ENDPOINT)

#: 全局串行锁 —— 3.5GB 显存不允许并发调图
_GEN_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# ⚠️ 提示词设计的关键结论（2026-09-22 画质实验，勿凭直觉改）
# ═══════════════════════════════════════════════════════════════════
# 输入是**俯视的户型平面图**。实验证明：让 SD1.5 把它变成**人眼视角的室内实景**
# 是任务错配——ControlNet 忠实复刻平面图结构，输出要么是灰块、要么是噪声纹理，
# 完全无法用于演示。俯视彩色渲染同样失败（直接复刻输入图）。
#
# **唯一成功的形态是「3D 等轴测户型渲染」**：与输入同为俯视视角，
# 只是把平面"立起来"做出墙体与家具——这正是 SD1.5 擅长的任务。
#
# 因此本项目对外交付的 AI 图是「3D 户型渲染图」，**不是「装修效果图」**。
# 这个措辞差别必须在 UI 与面试中守住，否则就是过度承诺。
# ═══════════════════════════════════════════════════════════════════

# ⚠️ 提示词经过消融实验（P1/P2/P3/P4）:
#    P1（本核心句）与 P4（加家具词）都能出正确的 3D 等轴测图；
#    P2（在核心句后堆叠大量风格形容词）会退化成平面噪声图。
# 结论：**核心句保持简短固定，风格词只加一个短后缀**，不要堆砌。
_ISOMETRIC_CORE = (
    "3d isometric floor plan render, cutaway view of apartment interior, "
    "modern furniture, soft lighting, architectural visualization, clean"
)

_STYLE_SUFFIX = {
    "modern": "modern minimalist style",
    "nordic": "nordic light wood style",
    "chinese": "modern chinese warm wood style",
    "cream": "cream warm white style",
    "wabi_sabi": "wabi-sabi natural materials style",
    "luxury": "luxury marble brass style",
}

STYLE_PROMPTS = {k: f"{_ISOMETRIC_CORE}, {v}" for k, v in _STYLE_SUFFIX.items()}

NEGATIVE_PROMPT = (
    # 实测：不压制这些词，LCM 低 CFG 下会退化成噪声纹理
    "noise, pattern, texture artifact, crosshatch, blurry, low quality, "
    "distorted, deformed, watermark, text, signature, cluttered, ugly"
)


def read_vram() -> VramReading | None:
    """
    读一次显存。**同时用两个口径**，因为它们的差异本身就是信息。

    - driver 口径（mem_get_info）：真实占用量，含 CUDA context
    - torch 口径（memory_allocated）：只统计 PyTorch 分配器，会低估
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        gb = 1024 ** 3
        return VramReading(
            driver_used_gb=(total - free) / gb,
            driver_free_gb=free / gb,
            torch_allocated_gb=torch.cuda.memory_allocated() / gb,
            torch_reserved_gb=torch.cuda.memory_reserved() / gb,
            torch_peak_gb=torch.cuda.max_memory_allocated() / gb,
        )
    except Exception:  # noqa: BLE001
        return None


class LocalSD15Provider(ImageProvider):
    """SD1.5 + LCM-LoRA（+ 可选 ControlNet）本地出图。"""

    name = "local_sd15_lcm"

    def __init__(self) -> None:
        self._pipe = None
        self._controlnet = None
        self._loaded_level: ImageLevel | None = None
        self._load_seconds: float = 0.0

    # ── 加载 ──────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._pipe is not None

    @property
    def load_seconds(self) -> float:
        """冷启动耗时——演示前 warmup 的意义就在这里（附录 F）。"""
        return self._load_seconds

    def _model_dirs_ok(self) -> tuple[bool, list[str]]:
        missing = []
        if not (settings.SD15_MODEL_PATH / "model_index.json").exists():
            missing.append(f"SD1.5 权重缺失: {settings.SD15_MODEL_PATH}")
        if not (settings.SD15_LCM_LORA_PATH / "pytorch_lora_weights.safetensors").exists():
            missing.append(f"LCM-LoRA 缺失: {settings.SD15_LCM_LORA_PATH}")
        if not (settings.SD15_CONTROLNET_PATH / "config.json").exists():
            missing.append(f"ControlNet 缺失: {settings.SD15_CONTROLNET_PATH}")
        return (not missing), missing

    def ensure_loaded(self, level: ImageLevel = ImageLevel.L0) -> None:
        """加载模型到显存。幂等；切换 level 时按需增减 ControlNet。"""
        ok, missing = self._model_dirs_ok()
        if not ok:
            raise FileNotFoundError("模型权重不完整:\n  " + "\n  ".join(missing))

        want_controlnet = level.uses_controlnet

        # 已加载且 ControlNet 需求一致 -> 直接复用
        if self._pipe is not None:
            have_cn = self._controlnet is not None
            if have_cn == want_controlnet:
                self._loaded_level = level
                return
            # ControlNet 需求变了 -> 完全重载（不尝试热插拔，避免显存碎片）
            self.unload()

        import torch

        t0 = time.perf_counter()
        logger.info(f"加载 SD1.5（ControlNet={want_controlnet}），首次约 20–40s…")

        dtype = torch.float16
        common = dict(torch_dtype=dtype, variant="fp16", use_safetensors=True)

        if want_controlnet:
            from diffusers import ControlNetModel, StableDiffusionControlNetPipeline

            self._controlnet = ControlNetModel.from_pretrained(
                str(settings.SD15_CONTROLNET_PATH), **common
            )
            pipe = StableDiffusionControlNetPipeline.from_pretrained(
                str(settings.SD15_MODEL_PATH),
                controlnet=self._controlnet,
                safety_checker=None,
                requires_safety_checker=False,
                **common,
            )
        else:
            from diffusers import StableDiffusionPipeline

            self._controlnet = None
            pipe = StableDiffusionPipeline.from_pretrained(
                str(settings.SD15_MODEL_PATH),
                safety_checker=None,
                requires_safety_checker=False,
                **common,
            )

        # ── 步骤 1：显存优化开关 ──────────────────────────
        # ⚠️ API 版本差异：diffusers 0.40 把 VAE 的 slicing/tiling 从 pipeline
        #    挪到了 vae 对象上（`enable_vae_slicing` → `vae.enable_slicing`）。
        #    实测 pipeline 上仍保留 `enable_attention_slicing`，但另两个已移除。
        #    这里用 getattr 逐个探测，避免绑死某个 diffusers 版本。
        self._enable_quietly(pipe, "enable_attention_slicing")
        # attention slice 取最大切分——实测有效且代价可忽略
        try:
            pipe.set_attention_slice("max")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"set_attention_slice 不可用: {e}")
        vae = getattr(pipe, "vae", None)
        if vae is not None:
            self._enable_quietly(vae, "enable_slicing")
            self._enable_quietly(vae, "enable_tiling")
        # 兼容旧版本（<0.30）的写法，存在就用
        self._enable_quietly(pipe, "enable_vae_slicing")
        self._enable_quietly(pipe, "enable_vae_tiling")

        # ── 步骤 2：采样器（LCM-LoRA 默认关闭）─────────────
        # ⚠️ 实测结论：LCM 把出图从 13.4s 压到 7.7s，但**跨种子极不稳定**——
        #    同一提示词下 seed=7 出正确的 3D 等轴测图，seed=42 退化成平面噪声图。
        #    演示不能赌运气，因此默认走标准 SD1.5 采样（20 步 / CFG 7.0）。
        #    13.4s 仍在 P95 < 25s 的目标内，用 5.7s 换确定性，值得。
        if settings.SD15_USE_LCM:
            from diffusers import LCMScheduler

            pipe.load_lora_weights(str(settings.SD15_LCM_LORA_PATH), adapter_name="lcm")
            pipe.set_adapters(["lcm"], adapter_weights=[1.0])
            pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
            logger.info("已启用 LCM-LoRA（快但输出方差大，不建议用于演示）")

        # ── 步骤 3：放置到设备（**必须最后一步**）────────────
        # 实测 offload 模式既省 3GB 显存又更快，详见 _apply_offload 的注释。
        # 注意：offload 与 pipe.to("cuda") 互斥，_apply_offload 内部已处理。
        applied = self._apply_offload(pipe, settings.SD15_OFFLOAD_MODE)

        self._pipe = pipe
        self._loaded_level = level
        self._load_seconds = time.perf_counter() - t0

        vram = read_vram()
        if vram:
            logger.info(
                f"加载完成 {self._load_seconds:.1f}s | offload={applied} | "
                f"驱动占用 {vram.driver_used_gb:.2f}GB 剩余 {vram.driver_free_gb:.2f}GB | "
                f"torch 口径 {vram.torch_allocated_gb:.2f}GB"
            )

    @staticmethod
    def _enable_quietly(target: Any, method: str) -> bool:
        """
        存在则调用，不存在就跳过。

        为的是不被 diffusers 的小版本改名拖死——本机实测 0.40 就把
        `enable_vae_slicing` 从 pipeline 挪到了 `vae.enable_slicing`。
        """
        fn = getattr(target, method, None)
        if not callable(fn):
            logger.debug(f"跳过 {method}（当前 diffusers 版本无此方法）")
            return False
        try:
            fn()
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{method} 调用失败: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _apply_offload(pipe: Any, mode: str) -> str:
        """
        把管线放到设备上。**这是本项目显存表现的关键开关。**

        实测对比（SD1.5 + ControlNet @512，6 步 LCM）：

            mode="none"  全量上卡   加载后 3.63GB | 出图峰值 4.00GB 空闲 0.00GB | 6.58s
            mode="model" 模型级 offload 加载后 0.82GB | 出图峰值 0.91GB 空闲 3.09GB | 5.33s

        **offload 反而更快**：全量上卡时显存 100% 压力下分配器颠簸，
        offload 后每步只保留当前模块在卡上，压力低、流水更顺。

        注意：使用 offload 时**不能**再调用 `pipe.to("cuda")`。
        """
        if mode == "model" and hasattr(pipe, "enable_model_cpu_offload"):
            pipe.enable_model_cpu_offload()
            return "model"
        if mode == "sequential" and hasattr(pipe, "enable_sequential_cpu_offload"):
            pipe.enable_sequential_cpu_offload()
            return "sequential"
        pipe.to("cuda")
        return "none"

    def unload(self) -> None:
        """卸载全部模型并清空显存缓存。"""
        import torch

        self._pipe = None
        self._controlnet = None
        self._loaded_level = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
        logger.debug("已卸载 SD1.5 管线")

    # ── 出图 ──────────────────────────────────────────────

    def generate(
        self,
        *,
        layout: dict[str, Any],
        style: str = "modern",
        level: ImageLevel | None = None,
        prompt: str | None = None,
        seed: int | None = None,
    ) -> ImageResult:
        """
        出图。**全局串行**——同一时刻只允许一个生成任务，否则 OOM。

        注意：level 只决定分辨率与是否用 ControlNet；
        具体用哪一级由 bench_image.py 从 L0 往下试出来。
        """
        import torch

        level = level or ImageLevel.L0
        if level is ImageLevel.L3:
            raise ValueError("L3（纯矢量图）不属于本 Provider，请用 VectorRenderer")

        size = level.size
        text = prompt or STYLE_PROMPTS.get(style, STYLE_PROMPTS["modern"])
        seed = seed if seed is not None else 42
        generator = torch.Generator(device="cuda").manual_seed(seed)

        with _GEN_LOCK:
            self.ensure_loaded(level)

            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()

            kwargs: dict[str, Any] = dict(
                prompt=text,
                negative_prompt=NEGATIVE_PROMPT,
                num_inference_steps=settings.SD15_STEPS,
                guidance_scale=settings.SD15_GUIDANCE,  # LCM 必须用低 CFG
                width=size,
                height=size,
                generator=generator,
            )

            if level.uses_controlnet:
                cond = self.render_conditioning_image(layout, size=size)
                kwargs["image"] = cond
                # ⚠️ 实测：1.0 会强制逐像素复刻 conditioning 图（输出=带纹理的平面图），
                #    0.5 才让模型在保持布局的同时自由生成 3D 结构。见 ADR-01。
                kwargs["controlnet_conditioning_scale"] = settings.SD15_CONTROLNET_SCALE

            t0 = time.perf_counter()
            out = self._pipe(**kwargs)
            torch.cuda.synchronize()
            seconds = time.perf_counter() - t0

            vram = read_vram()

        return ImageResult(
            image=out.images[0],
            level=level,
            provider=self.name,
            size=size,
            seconds=seconds,
            vram=vram,
            seed=seed,
            prompt=text,
            extra={
                "steps": settings.SD15_STEPS,
                "guidance": settings.SD15_GUIDANCE,
                "controlnet": level.uses_controlnet,
                "load_seconds": self._load_seconds,
            },
        )


__all__ = ["LocalSD15Provider", "read_vram", "STYLE_PROMPTS", "NEGATIVE_PROMPT"]
