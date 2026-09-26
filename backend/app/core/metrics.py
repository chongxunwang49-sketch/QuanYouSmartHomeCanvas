"""
性能采集与聚合（AC-23）。

═══════════════════════════════════════════════════════════════════
为什么是两个来源，而不是一个
═══════════════════════════════════════════════════════════════════
`/system/metrics` 要回答的问题有两类，它们的数据形态根本不同：

| 类别 | 例子 | 形态 | 来源 |
|---|---|---|---|
| **长任务** | 户型解析、方案生成 | 一次几十秒到两分钟，一天几十条 | **审计文件**（AC-14 刚接上） |
| **热路径** | HTTP 响应、矢量图渲染 | 一次几毫秒，一天几万条 | **进程内环形缓冲** |

把热路径也写进审计文件的话，光是一个两分钟的方案生成就要产生约 40 条
轮询记录，一天下来文件被淹没，而真正要查的审计事件（谁登录了、哪个任务
降级了）反而难找。所以热路径只留在内存里、有界、重启即清零 —— 那正是
"性能基线"这种**近期观测**该有的生命周期。

反过来，长任务写进内存就不够用了：它一天才几十条，但恰恰是"重启之后还想
看看上周那次生成到底多慢"的那类问题。所以它落盘、由审计文件承。

两边的口径都写在返回体里（`source` / `notes`），**哪一项没采到就说没采到**，
不给 0 也不给"暂无数据"这种含糊话。
"""

from __future__ import annotations

import json
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

#: 每个热路径指标在内存里保留多少个样本。
#:
#: 5000 条 × 每条一个 float，代价可以忽略；而 P95 只需要尾部准确，
#: 5000 个样本已经够稳（再多的边际收益很小，还会让内存无谓地涨）。
#: **有界是刻意的**：不限长度的话，一个跑几天不重启的进程会把样本攒成
#: 内存泄漏，而它长得不像泄漏 —— 看起来只是"指标越来越多"。
_RING = 5000

#: 进程内样本表：{指标名: deque[float]}。值是**毫秒**。
_samples: dict[str, deque[float]] = {}


def record(name: str, ms: float) -> None:
    """
    记一次耗时（毫秒）。热路径调用，**必须极轻**。

    只做一次 deque.append —— 没有锁，也不需要锁：CPython 里
    `deque.append` 是原子的，而这些样本本来就是"近似统计"，
    并发下丢一两条不影响 P95。
    """
    buf = _samples.get(name)
    if buf is None:
        buf = _samples[name] = deque(maxlen=_RING)
    buf.append(float(ms))


def reset() -> None:
    """清空进程内样本。**仅供测试**（与 reset_task_manager 同一个模式）。"""
    _samples.clear()


def percentile(values: list[float], p: float) -> float:
    """
    最近秩（nearest-rank）百分位。

    ⚠️ **不用插值**。P95 的常见插值算法（numpy 默认的 linear）会算出
    "两个样本之间的数"，那在小样本上会给出一个**没有观测到过**的值 ——
    而性能报告里报一个不存在的数，是这类工具最容易犯的错。
    最近秩的语义更朴素也更诚实：**"95% 的请求不慢于这个数"**，
    而这个数确实来自某一次真实测量。

    ⚠️ 样本 < 2 时返回 None：一条样本算不出"分布"，报 p95 = 那一条
    会让人误以为有统计意义。
    """
    if len(values) < 2:
        return None
    ordered = sorted(values)
    # ceil(p/100 * n) 的最近秩；clamp 到 [1, n]
    rank = max(1, min(len(ordered), int(-(-len(ordered) * p // 100))))
    return ordered[rank - 1]


def summarize(values: list[float]) -> dict[str, Any] | None:
    """一组耗时的概要。样本太少就返回 None（**不是**返回 0）。"""
    if len(values) < 2:
        return None
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 1),
        "p95": round(percentile(values, 95) or 0, 1),
        "max": round(max(values), 1),
    }


def snapshot(names: list[str] | None = None) -> dict[str, Any]:
    """热路径指标的当前快照。"""
    out: dict[str, Any] = {}
    for name, buf in sorted(_samples.items()):
        if names and name not in names:
            continue
        summary = summarize(list(buf))
        if summary is not None:
            out[name] = summary
    return out


# ══════════════════════════════════════════════════════════════════
# 从审计文件读长任务耗时
# ══════════════════════════════════════════════════════════════════

#: 每个指标最多保留多少条记录（取**最近**的）。
#: 审计文件是 DEBUG 级的，一天可能几 MB —— 全读进内存再排序没有意义，
#: 性能基线关心的是"最近怎么样"。
_MAX_RECORDS = 2000


@dataclass
class AuditSweep:
    """一次审计文件扫描的结果。**采不到东西也必须说清为什么。**"""

    files: list[str]
    lines: int
    task_seconds: dict[str, list[float]]      # kind -> [秒]
    llm_ms: dict[str, list[float]]            # agent -> [毫秒]
    llm_tokens: dict[str, list[int]]
    errors: list[str]

    def to_source(self) -> dict[str, Any]:
        return {
            "audit_files": self.files,
            "scanned_lines": self.lines,
            "task_samples": {k: len(v) for k, v in self.task_seconds.items()},
            "llm_samples": {k: len(v) for k, v in self.llm_ms.items()},
            "errors": self.errors,
        }


def sweep_audit_files(log_dir: Path | str, *, days: int = 1) -> AuditSweep:
    """
    扫最近 `days` 天的审计文件，抽出长任务耗时与 LLM 调用耗时。

    ⚠️ **这是同步文件 IO，调用方要用 `asyncio.to_thread` 包起来。**
    今天刚在 A-06 的检索上踩过同一个坑（同步 IO 把事件循环卡住 28 秒），
    不想在指标接口上再踩一次 —— 而审计文件正好是唯一可能很大的那个文件。

    ⚠️ 读不到文件**不算错误**，但必须记进 `errors` 让调用方看见。
    "没有数据"和"P95 是 0"是两件事，混起来就是本项目最防的那种谎。
    """
    root = Path(log_dir)
    sweep = AuditSweep(files=[], lines=0, task_seconds={},
                       llm_ms={}, llm_tokens={}, errors=[])

    if not root.exists():
        sweep.errors.append(
            f"审计目录不存在：{root} —— 没有性能数据可聚合。"
            f"（审计文件由 lifespan 启动时激活，见 LOG_DIR 配置）"
        )
        return sweep

    # 文件名是 app_YYYY-MM-DD.log，字典序即时间序，取最后 days 个
    files = sorted(root.glob("app_*.log"))[-max(1, days):]
    if not files:
        sweep.errors.append(
            f"{root} 下没有 app_*.log —— 服务还没跑过，或 LOG_DIR 没配。"
        )
        return sweep

    # 用 `setdefault(..., deque(maxlen=...))` 按需建桶：指标名是审计文件里
    # 读出来的，事先不知道有哪些（kind / agent 都是动态的）。
    task_seconds: dict[str, deque[float]] = {}
    llm_ms: dict[str, deque[float]] = {}
    llm_tokens: dict[str, deque[int]] = {}

    for path in files:
        sweep.files.append(path.name)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    sweep.lines += 1
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # 半行（进程被杀时可能留下）跳过
                    if not isinstance(rec, dict):
                        # ⚠️ JSON 合法但**不是对象**（日志被截断/损坏时会出现裸数字）。
                        #    不挡的话 rec.get 直接抛 AttributeError，整个指标接口 500 ——
                        #    而下面的立场写得很清楚：一个文件坏了不该让接口失败。
                        continue
                    extra = (rec.get("record") or {}).get("extra") or {}
                    event = extra.get("event")
                    if event == "task_finished":
                        kind = str(extra.get("kind") or "?")
                        secs = extra.get("elapsed_seconds")
                        if isinstance(secs, (int, float)):
                            task_seconds.setdefault(kind, deque(maxlen=_MAX_RECORDS)).append(float(secs))
                    elif extra.get("agent") and "elapsed_ms" in extra:
                        agent = str(extra["agent"])
                        ms = extra.get("elapsed_ms")
                        if isinstance(ms, (int, float)):
                            llm_ms.setdefault(agent, deque(maxlen=_MAX_RECORDS)).append(float(ms))
                            tok = extra.get("prompt_tokens", 0) + extra.get("completion_tokens", 0)
                            llm_tokens.setdefault(agent, deque(maxlen=_MAX_RECORDS)).append(int(tok))
        except OSError as e:
            # 读不了就记下来继续 —— 一个文件坏了不该让整个指标接口失败
            msg = f"读取 {path.name} 失败：{type(e).__name__}: {e}"
            logger.warning(f"[metrics] {msg}")
            sweep.errors.append(msg)

    sweep.task_seconds = {k: list(v) for k, v in task_seconds.items()}
    sweep.llm_ms = {k: list(v) for k, v in llm_ms.items()}
    sweep.llm_tokens = {k: list(v) for k, v in llm_tokens.items()}
    return sweep


__all__ = [
    "record", "reset", "snapshot", "summarize", "percentile",
    "sweep_audit_files", "AuditSweep",
]
