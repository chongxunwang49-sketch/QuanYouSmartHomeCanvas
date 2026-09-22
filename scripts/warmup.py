"""
演示前预热 —— **必须在演示前 5 分钟执行**（需求文档附录 F.2）。

═══════════════════════════════════════════════════════════════════
为什么这个脚本是必须的，不是可选的
═══════════════════════════════════════════════════════════════════
SD1.5 冷启动要从磁盘加载约 2.6GB 权重，实测 20–40s。
如果跳过预热，演示时**第一次出图要干等 40 秒**——
这 40 秒的沉默比任何技术缺陷都尴尬。

预热把模型常驻显存，之后出图直接进推理。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.image.base import ImageLevel  # noqa: E402


def step(name: str, fn) -> bool:
    print(f"  ▶ {name} … ", end="", flush=True)
    t0 = time.perf_counter()
    try:
        detail = fn()
        print(f"✓ {time.perf_counter() - t0:.1f}s   {detail or ''}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"✗ {type(e).__name__}: {str(e)[:140]}")
        return False


def check_vram_headroom() -> str:
    """
    演示前的显存体检——4GB 卡上这是生死攸关的一条。

    需求文档附录 F.2：若桌面进程占用 > 1GB，说明有残留进程，
    必须先去排查（关浏览器多余标签、关其他 AI 应用）。
    """
    from backend.app.services.image.local_sd15 import read_vram

    v = read_vram()
    if not v:
        raise RuntimeError("无法读取显存（CUDA 不可用？）")
    note = f"已用 {v.driver_used_gb:.2f}GB / 空闲 {v.driver_free_gb:.2f}GB"
    if v.driver_used_gb > 1.0:
        raise RuntimeError(
            f"显存基线偏高：{note}。请先关闭浏览器多余标签页与其他 AI 应用后再试"
        )
    return note


def check_ollama() -> str:
    import urllib.request

    from backend.app.core.config import settings

    with urllib.request.urlopen(f"{settings.OLLAMA_BASE_URL}/api/tags", timeout=10) as r:
        import json

        tags = json.load(r).get("models", [])
    names = {m["name"] for m in tags}
    need = {settings.OLLAMA_VISION_MODEL, settings.OLLAMA_TEXT_MODEL}
    missing = need - names
    if missing:
        raise RuntimeError(f"缺少模型: {missing}")
    return f"{len(tags)} 个模型可用"


def check_deepseek() -> str:
    from backend.app.core.config import settings

    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY（演示将走本地降级模式）")

    import json
    import urllib.request

    req = urllib.request.Request(
        f"{settings.DEEPSEEK_BASE_URL}/models",
        headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return f"{len(data.get('data', []))} 个模型"


def check_datastores() -> str:
    """PG / Redis 连通性。任一不可用只告警不阻断（M0 阶段还没接上）。"""
    notes = []
    try:
        import redis

        from backend.app.core.config import settings

        redis.from_url(settings.REDIS_URL, socket_timeout=3).ping()
        notes.append("Redis ✓")
    except Exception:  # noqa: BLE001
        notes.append("Redis ✗（未启动）")
    return " / ".join(notes)


def warmup_sd15() -> str:
    from backend.app.services.image.local_sd15 import LocalSD15Provider

    p = LocalSD15Provider()
    p.ensure_loaded(ImageLevel.L0)
    from backend.app.services.image.local_sd15 import read_vram

    v = read_vram()
    note = f"加载 {p.load_seconds:.1f}s"
    if v:
        note += f" | 显存 已用 {v.driver_used_gb:.2f}GB 空闲 {v.driver_free_gb:.2f}GB"
        if v.driver_free_gb < 0.8:
            raise RuntimeError(
                f"预热后显存余量不足（{v.driver_free_gb:.2f}GB < 0.8GB），出图有 OOM 风险"
            )
    return note


def main() -> int:
    print("=" * 70)
    print("演示预热  |  全友·智绘家   （建议演示前 5 分钟执行）")
    print("=" * 70)

    results = {
        "显存基线": step("显存基线检查", check_vram_headroom),
        "Ollama": step("Ollama 本地模型", check_ollama),
        "DeepSeek": step("DeepSeek 连通性", check_deepseek),
        "数据存储": step("Redis / PostgreSQL", check_datastores),
        "SD1.5": step("SD1.5 权重上卡", warmup_sd15),
    }

    print("=" * 70)
    critical = ["显存基线", "SD1.5"]
    failed_critical = [k for k in critical if not results[k]]

    if failed_critical:
        print(f"✗ 关键项未通过: {failed_critical}")
        print("  → 演示前必须解决。若 SD1.5 确实起不来，")
        print("    切 IMAGE_PROVIDER=vector 用矢量图链路演示（仍满足 AC-07）。")
        return 1

    print("✓ 预热完成，模型已常驻显存。可以开始演示。")
    if not results["DeepSeek"]:
        print("  ⚠️ DeepSeek 不可用 —— 演示将走本地降级路径，")
        print("     这本身是一个演示点（能讲清降级设计），但请确认是你主动选择的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
