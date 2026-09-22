"""
下载 M0 出图基准所需的模型权重。

本机约束（见需求文档第零章）：
- huggingface.co **直连不通**（实测 12s 超时），必须走 hf-mirror.com
- 模型统一放 E 盘（D 盘仅 29GB 可用，已用 89%）

只下载 fp16 变体与必要组件，跳过 safety_checker（可禁用）与 .bin 旧格式，
把下载量从 ~7GB 压到 ~2.5GB。

用法：
    python scripts/download_models.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 必须在 import huggingface_hub 之前设置，否则不生效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODELS_ROOT = Path("E:/quanyou/models")

# (仓库ID, 本地目录名, 文件白名单)
TARGETS: list[tuple[str, str, list[str] | None]] = [
    (
        "stable-diffusion-v1-5/stable-diffusion-v1-5",
        "sd15",
        [
            "model_index.json",
            "scheduler/*",
            "tokenizer/*",
            "feature_extractor/*",
            "text_encoder/config.json",
            "text_encoder/model.fp16.safetensors",
            "unet/config.json",
            "unet/diffusion_pytorch_model.fp16.safetensors",
            "vae/config.json",
            "vae/diffusion_pytorch_model.fp16.safetensors",
        ],
    ),
    (
        "latent-consistency/lcm-lora-sdv1-5",
        "lcm-lora-sdv15",
        ["pytorch_lora_weights.safetensors"],
    ),
    (
        "lllyasviel/control_v11p_sd15_canny",
        "controlnet_sd15_canny",
        [
            "config.json",
            "diffusion_pytorch_model.fp16.safetensors",
        ],
    ),
]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main() -> int:
    from huggingface_hub import snapshot_download

    print(f"HF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    print(f"下载目录    = {MODELS_ROOT}")
    print("=" * 66)

    ok = True
    for repo_id, local_name, patterns in TARGETS:
        dest = MODELS_ROOT / local_name
        print(f"\n▶ {repo_id}")
        print(f"  -> {dest}")

        before = dir_size(dest)
        t0 = time.perf_counter()
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(dest),
                allow_patterns=patterns,
                # 只保留实际文件，不建 blobs 软链接结构，便于直接喂给 diffusers
                local_dir_use_symlinks=False,
                max_workers=4,
                resume_download=True,
            )
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✗ 失败: {type(e).__name__}: {str(e)[:200]}")
            continue

        elapsed = time.perf_counter() - t0
        size = dir_size(dest)
        delta = size - before
        print(f"  ✓ 完成  新增 {human(delta)}  总 {human(size)}  耗时 {elapsed:.0f}s")

    print("\n" + "=" * 66)
    print("汇总:")
    total = 0
    for _, local_name, _ in TARGETS:
        s = dir_size(MODELS_ROOT / local_name)
        total += s
        flag = "✓" if s > 0 else "✗"
        print(f"  {flag} {local_name:26s} {human(s):>10s}")
    print(f"  合计 {human(total)}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
