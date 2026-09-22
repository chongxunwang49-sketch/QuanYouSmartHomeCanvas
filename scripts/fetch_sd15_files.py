"""
用直连 HTTP 逐文件下载 SD1.5 权重。

为什么不用 snapshot_download：
实测 `snapshot_download` 对本仓库稳定报 LocalEntryNotFoundError
（而 LCM-LoRA / ControlNet 两个仓库都正常）。逐文件 HEAD 探测显示
**所有文件其实都可达**（unet fp16 = 1.72GB，HTTP 200）。
因此绕开 hub 库的元数据/并发机制，改为逐个文件直连下载，
支持断点续传与单文件重试，更可控。

用法：
    python scripts/fetch_sd15_files.py
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
REPO_ID = "stable-diffusion-v1-5/stable-diffusion-v1-5"
DEST = Path("E:/quanyou/models/sd15")

FILES = [
    "model_index.json",
    "scheduler/scheduler_config.json",
    "tokenizer/vocab.json",
    "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json",
    "text_encoder/config.json",
    "text_encoder/model.fp16.safetensors",
    "unet/config.json",
    "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.fp16.safetensors",
]


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def url_for(path: str) -> str:
    return f"{HF_ENDPOINT}/{REPO_ID}/resolve/main/{path}"


def remote_size(url: str, timeout: int = 25) -> int | None:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        return None


def download_one(rel: str, max_attempts: int = 5) -> bool:
    """下载单个文件。支持断点续传与重试。"""
    dest = DEST / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = url_for(rel)

    expected = remote_size(url)
    if expected is None:
        print(f"  ✗ {rel}: 无法获取远端大小")
        return False

    if dest.exists() and dest.stat().st_size == expected:
        print(f"  ⏭ {rel:52s} 已完整 {human(expected)}")
        return True

    for attempt in range(1, max_attempts + 1):
        have = dest.stat().st_size if dest.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "ab") as fh:
                total = have
                last = time.time()
                while True:
                    chunk = r.read(1 << 20)  # 1MB
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
                    now = time.time()
                    if now - last > 2.0:
                        pct = total / expected * 100 if expected else 0
                        print(f"\r     {rel} {human(total)}/{human(expected)} "
                              f"({pct:.0f}%)", end="", flush=True)
                        last = now

            if dest.stat().st_size == expected:
                print(f"\r  ✓ {rel:52s} {human(expected)}          ")
                return True
            print(f"\r     {rel}: 大小不符（{dest.stat().st_size} vs {expected}），重试")
        except Exception as e:  # noqa: BLE001
            print(f"\r  ! {rel}: 第 {attempt} 次失败 {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2 * attempt)

    return False


def main() -> int:
    print(f"HF_ENDPOINT = {HF_ENDPOINT}")
    print(f"目标目录    = {DEST}")
    print("=" * 70)

    failed = []
    for rel in FILES:
        if not download_one(rel):
            failed.append(rel)

    print("=" * 70)
    total = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file()) if DEST.exists() else 0
    print(f"已下载总计: {human(total)}")
    if failed:
        print(f"✗ 失败 {len(failed)} 个:")
        for f in failed:
            print(f"    {f}")
        return 1
    print("✓ 全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
