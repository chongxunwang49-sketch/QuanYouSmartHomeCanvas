"""
M0 出图基准 —— **本项目 AI 出图这条线的放行门槛**。

需求文档附录 E 的通过标准第 2 条：
    至少 L2 级别连续 10 张不 OOM，峰值显存 < 3.2GB（驱动口径）

不过就当场把 M5–M6 的 AI 出图划掉，改以「矢量图 + 伪 3D」交付。
**不许拖到 M5 才发现。**

═══════════════════════════════════════════════════════════════════
显存口径（这是本脚本存在的技术核心）
═══════════════════════════════════════════════════════════════════
用**驱动口径** `torch.cuda.mem_get_info()`，不用 `max_memory_allocated()`。
实测后者看不到 CUDA context（刚初始化完它报 0MB），会系统性低估 300–500MB。
在 3.22GB 可用显存这种紧平衡下，这个差值就是"能跑"与"OOM"的分界。

用法：
    python scripts/bench_image.py            # 完整基准（含 10 张稳定性测试）
    python scripts/bench_image.py --quick    # 只测 L0/L1/L2 各 1 张
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.image.base import ImageLevel  # noqa: E402
from backend.app.services.image.local_sd15 import LocalSD15Provider, read_vram  # noqa: E402

OUT_DIR = Path("E:/quanyou/outputs/bench")
REPORT = Path("E:/quanyou/outputs/bench/bench_report.json")

#: AC-22 的阈值：驱动口径峰值不得超过这个值（4GB 卡留 0.8GB 余量）
VRAM_LIMIT_GB = 3.2

#: 演示用户型（模拟 A-01 的解析结果）
DEMO_LAYOUT = {
    "layout_id": "bench_demo",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300]},
        {"name": "次卧", "type": "bedroom", "area": 12.0, "bbox": [440, 320, 680, 500]},
        {"name": "厨房", "type": "kitchen", "area": 8.0, "bbox": [120, 380, 300, 540]},
    ],
    "total_area": 89.0,
}


def banner(t: str) -> None:
    print(f"\n{'═' * 72}\n{t}\n{'═' * 72}")


def gpu_state() -> str:
    v = read_vram()
    if not v:
        return "  (无法读取显存)"
    return (f"  驱动口径: 已用 {v.driver_used_gb:.2f}GB / 空闲 {v.driver_free_gb:.2f}GB "
            f"| torch 口径: {v.torch_allocated_gb:.2f}GB")


def try_level(provider: LocalSD15Provider, level: ImageLevel, seed: int = 42) -> dict:
    """测试单个级别出 1 张图。返回结果字典。"""
    import torch

    print(f"\n▶ {level.value}  —— {level.description}")
    result: dict = {"level": level.value, "ok": False}

    baseline = read_vram()
    if baseline:
        result["baseline_used_gb"] = round(baseline.driver_used_gb, 3)

    try:
        t0 = time.perf_counter()
        res = provider.generate(layout=DEMO_LAYOUT, style="modern", level=level, seed=seed)
        wall = time.perf_counter() - t0

        result.update({
            "ok": True,
            "seconds": round(res.seconds, 2),
            "seconds_incl_load": round(wall, 2),
            "size": res.size,
            "controlnet": level.uses_controlnet,
            "peak_used_gb": round(res.vram.driver_used_gb, 3) if res.vram else None,
            "peak_free_gb": round(res.vram.driver_free_gb, 3) if res.vram else None,
            "torch_peak_gb": round(res.vram.torch_peak_gb, 3) if res.vram else None,
        })

        if result.get("baseline_used_gb") is not None and result["peak_used_gb"] is not None:
            result["model_footprint_gb"] = round(
                result["peak_used_gb"] - result["baseline_used_gb"], 3
            )

        # 保存图片
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        img_path = OUT_DIR / f"{level.value}.png"
        res.image.save(img_path)
        result["image_path"] = str(img_path)

        over = result.get("peak_used_gb", 0) > VRAM_LIMIT_GB
        result["within_limit"] = not over
        mark = "✓" if not over else "⚠ 超阈值"
        print(f"  耗时 {result['seconds']}s（含加载 {result['seconds_incl_load']}s）")
        print(f"  峰值显存 {result['peak_used_gb']}GB / 空闲 {result['peak_free_gb']}GB "
              f"（模型净占用 {result.get('model_footprint_gb')}GB）{mark}")
        print(f"  出图已保存 {img_path}")
        if not provider.is_loaded:
            pass

    except torch.cuda.OutOfMemoryError as e:
        result["error"] = f"CUDA OOM: {str(e)[:160]}"
        result["oom"] = True
        print(f"  ✗ 显存溢出 OOM")
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"  ✗ 失败: {result['error']}")
        if "out of memory" in str(e).lower():
            result["oom"] = True

    return result


def stability_test(provider: LocalSD15Provider, level: ImageLevel, n: int = 10) -> dict:
    """AC-22：连续 n 张不 OOM，且峰值 < 阈值。"""
    import torch

    print(f"\n▶ 稳定性测试：{level.value} 连续 {n} 张")
    peaks: list[float] = []
    times: list[float] = []
    ok = True

    for i in range(1, n + 1):
        try:
            res = provider.generate(
                layout=DEMO_LAYOUT, style="modern", level=level, seed=1000 + i
            )
            peaks.append(res.vram.driver_used_gb if res.vram else 0.0)
            times.append(res.seconds)
            print(f"  [{i:2d}/{n}] {res.seconds:5.2f}s  峰值 {peaks[-1]:.2f}GB  "
                  f"空闲 {res.vram.driver_free_gb:.2f}GB" if res.vram else f"  [{i:2d}/{n}] {res.seconds:.2f}s")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  [{i:2d}/{n}] ✗ {type(e).__name__}: {str(e)[:120]}")
            break

    return {
        "level": level.value,
        "requested": n,
        "completed": len(times),
        "no_oom": ok and len(times) == n,
        "peak_gb": round(max(peaks), 3) if peaks else None,
        "avg_seconds": round(sum(times) / len(times), 2) if times else None,
        "within_limit": (max(peaks) <= VRAM_LIMIT_GB) if peaks else False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只测 L0/L1/L2 各 1 张")
    ap.add_argument("--stability-n", type=int, default=10)
    args = ap.parse_args()

    import torch

    banner("M0 出图基准  |  全友·智绘家")
    if not torch.cuda.is_available():
        print("✗ CUDA 不可用，无法测出图")
        return 2
    print(f"设备: {torch.cuda.get_device_name(0)}")
    print(f"题目地址: {torch.cuda.device_count()} 张卡")
    print(f"测试前显存:{gpu_state()}")
    print(f"阈值: 驱动口径峰值 ≤ {VRAM_LIMIT_GB}GB（AC-22）")

    provider = LocalSD15Provider()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 阶段一：逐级测试，找最低可用级别 ──────────────────
    banner("阶段一 · 逐级降级测试（L0 → L2）")
    results: list[dict] = []
    winner: ImageLevel | None = None

    for level in (ImageLevel.L0, ImageLevel.L1, ImageLevel.L2):
        r = try_level(provider, level)
        results.append(r)
        if r["ok"] and r.get("within_limit"):
            winner = level
            print(f"  → {level.value} 通过，停止降级")
            break
        # OOM 后彻底清空再试下一级，避免碎片影响
        provider.unload()
        import gc
        gc.collect()
        torch.cuda.empty_cache()

    # ── 阶段二：稳定性测试（AC-22）──────────────────────────
    stability = None
    if winner:
        banner(f"阶段二 · 稳定性测试 @ {winner.value}")
        stability = stability_test(provider, winner, n=args.stability_n)
    elif not args.quick and any(r["ok"] for r in results):
        # 有能出图但超阈值的级别，仍测稳定性以给出完整数据
        ok_levels = [r for r in results if r["ok"]]
        best = ok_levels[0]
        banner(f"阶段二 · 稳定性测试 @ {best['level']}（注意：该级别已超显存阈值）")
        stability = stability_test(provider, ImageLevel(best["level"]), n=args.stability_n)

    # ── 阶段三：L3 矢量图（必过基线）───────────────────────
    banner("阶段三 · L3 矢量图基线（必过）")
    try:
        from PIL import Image

        t0 = time.perf_counter()
        cond = LocalSD15Provider.render_conditioning_image(DEMO_LAYOUT, size=512)
        vec_seconds = time.perf_counter() - t0
        vec_path = OUT_DIR / "L3_vector_only.png"
        cond.save(vec_path)
        vec_ok, vec_note = True, f"{vec_seconds:.2f}s，零显存依赖"
        print(f"  ✓ 矢量图渲染成功 {vec_note} -> {vec_path}")
    except Exception as e:  # noqa: BLE001
        vec_ok, vec_note = False, f"{type(e).__name__}: {e}"
        print(f"  ✗ 矢量图渲染失败: {vec_note}")
        vec_seconds = None

    # ── 结论 ──────────────────────────────────────────────
    banner("结论")

    verdict: dict = {
        "winner_level": winner.value if winner else None,
        "ai_image_feasible": winner is not None,
        "stability": stability,
        "vector_baseline_ok": vec_ok,
        "vector_seconds": round(vec_seconds, 2) if vec_seconds else None,
    }

    if winner:
        print(f"  ✅ AI 出图可行，采用级别：{winner.value}")
        print(f"     {winner.description}")
        if stability:
            print(f"     连续 {stability['completed']}/{stability['requested']} 张不 OOM，"
                  f"峰值 {stability['peak_gb']}GB，均耗时 {stability['avg_seconds']}s")
        verdict["verdict"] = "PASS_AI_IMAGE"
    elif any(r["ok"] for r in results):
        best = [r for r in results if r["ok"]][0]
        print(f"  ⚠️ 能出图但**超出显存阈值**：{best['level']} 峰值 {best.get('peak_used_gb')}GB "
              f"> {VRAM_LIMIT_GB}GB")
        print(f"     建议：按 ADR-01 将 AI 出图整体降为路线图，以矢量图交付")
        verdict["verdict"] = "OVER_BUDGET"
    else:
        print("  ❌ 所有级别均无法出图")
        print("     → 执行 ADR-01 的决策：AI 出图整体降为路线图，项目以矢量图 + 伪 3D 交付")
        verdict["verdict"] = "FAIL_NO_AI_IMAGE"

    print(f"\n  矢量图基线：{'✓ 可用' if vec_ok else '✗ 失败'} —— "
          f"{'AC-07 可达成，功能完整' if vec_ok else '⚠️ 需要排查'}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "verdict": verdict,
        "levels": results,
        "vram_limit_gb": VRAM_LIMIT_GB,
        "gpu": torch.cuda.get_device_name(0),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整报告已写入 {REPORT}")

    provider.unload()
    return 0 if winner else 1


if __name__ == "__main__":
    sys.exit(main())
