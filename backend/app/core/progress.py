"""
文件路径: backend/app/core/progress.py
模块职责: 语义化阶段词汇表 + 进度 / 剩余时间模型
依赖关系: 无（纯数据 + 纯函数，不碰 IO、不碰 Redis）
对应需求文档:
  - 2.2.4 任务进度改为语义化 `phase` 字段（V2.2）
  - 4.4 任务状态查询接口：未完成任务返回 HTTP 200，用 phase/progress 表达

═══════════════════════════════════════════════════════════════════
为什么进度/剩余时间必须有"任务类型"这一维
═══════════════════════════════════════════════════════════════════
这里原来只有**一张全局表** `PHASE_PROGRESS`（阶段 -> 百分比）。
它在解析链路上是对的，因为那张表就是照解析链路写的：

    analyzing 15 → detecting_rooms 40 → extracting_dimensions 65 → diagnosing 85

但方案生成链路的**第一个**节点是 `diagnose_layout`，映射到 `diagnosing` = 85。
于是实测现象是：进度条一上来就冲到 85%，然后**倒回 60%**（`planning`），
再爬到 95%（`finalizing`）。一屏之内先涨后退，用户只会得出"这进度是假的"
——然后就不再看了，只盯着"已等待 N 秒"。这正是用户提的那句
「现在我只看到记录了已经等待的时间」。

**同一个阶段名在不同任务里占的分量完全不同**，所以进度不能只看阶段名，
必须带上 `kind`。这和 `api/tasks.py::_phase_for` 必须带 `kind` 是同一个道理
（`review_risks` 在报价单审查里是"正在审查报价单"，在方案链里是另一回事）。

═══════════════════════════════════════════════════════════════════
百分比不再手填，而是从"期望耗时"推出来
═══════════════════════════════════════════════════════════════════
每个阶段只声明**一个**数字：它期望花多少秒（`seconds`）。
百分比由秒数占比算出，四舍五入到 0–99（最后 1% 留给"完成"）。

这样做的理由有两个：
1. **两个数字不可能再漂移。** 手填百分比时，"进度"和"剩余时间"是两套
   互不相干的数，改了一个忘了另一个是迟早的事。
2. **进度条的含义变得可解释**：它表示"期望耗时已经用掉了多少"，
   与剩余时间同源 —— 条子走到头，倒计时也就该到 0。

═══════════════════════════════════════════════════════════════════
期望耗时是**实测**的，不是拍的
═══════════════════════════════════════════════════════════════════
下面每个 `seconds` 都标了来源（哪一次实测、各阶段各多少秒）。
改这些数字时**必须连着来源一起改** —— 一个没有出处的秒数，
下一次没人敢动它，也没人知道它是不是还成立。

═══════════════════════════════════════════════════════════════════
剩余时间为什么要"跟着实际速度缩放"
═══════════════════════════════════════════════════════════════════
固定模型只在模型标定的那一天是准的。LLM 侧延迟波动很大：同一条链路
实测过 33s 和 48s 两种；A-06 单次调用实测 20s–52s。

不缩放的话，慢一次就会出现"预计还剩 3 秒"**持续 40 秒**的画面 ——
比不给倒计时更糟，因为它消耗掉的是用户的信任。

所以按**已经走完的阶段**算一个速度系数（实际耗时 / 期望耗时），
再用它缩放剩余部分。这就是安装器上那个"剩余时间"能自己修正的原因：
刚开局只敢用先验，走过一段之后就以实际速度为准。

⚠️ 系数带上下限（0.6–3.0）。无上限的话，一次网络抖动会把剩余时间
推到几分钟后，之后再也收不回来。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "TaskKind", "Phase", "PHASE_TEXT", "PhaseStep",
    "STEPS", "steps_for", "total_seconds", "progress_at", "build_view",
    "PACE_MIN", "PACE_MAX", "PACE_PRIOR_SECONDS",
]

TaskKind = Literal["parse", "generate", "review"]

#: 语义化阶段。**前端不维护映射表**，只展示 `phase_text`（2.2.4）。
#:
#: ⚠️ 这里曾经还有 `detecting_rooms` / `extracting_dimensions` 两个阶段
#: （需求文档 11.2.4 的表格里也列着，progress 40 / 65）。
#: **它们从来没有被任何代码产出过** —— 它们描述的是一个"边解析边吐子阶段"
#: 的设计，而真实实现里 A-01 是一次调用返回整份户型。留在词汇表里的代价是：
#: 进度模型必须为两个永不发生的阶段留位置，而"某个阶段没有进度值"这种事
#: 在界面上表现为进度条跳回 0。已连同需求文档的表格一起删除。
Phase = Literal[
    "queued", "prechecking", "analyzing", "diagnosing", "retrieving",
    "planning", "checking_risks", "reviewing", "finalizing",
    "done", "degraded",
]

#: 阶段 -> 中文文案。
#:
#: 文案的措辞守则：**说的是"系统正在做什么"，不是"系统正在跑哪个进程"**。
#: 所以 `reviewing` 与 `checking_risks` 是两条不同的文案，尽管它们由同一个
#: 节点 `review_risks` 触发 —— 用户面前的东西不一样（一份报价单 / 三套方案）。
PHASE_TEXT: dict[str, str] = {
    "queued": "已接收，正在排队…",
    "prechecking": "正在检查图片质量…",
    "analyzing": "正在分析图片…",
    "diagnosing": "正在生成户型诊断…",
    # 审查链专属：查依据与调模型是两步，文案分开（见 _REVIEW_STEPS 的说明）
    "retrieving": "正在检索知识库依据…",
    "planning": "正在生成装修方案…",
    # 报价单审查：用户在看一份合同
    "reviewing": "正在审查报价单…",
    # 方案链内的避坑审查：用户在等方案，且方案**已经可以看了**
    "checking_risks": "正在复核方案风险…",
    "finalizing": "即将完成…",
    "done": "完成",
    "degraded": "已完成（降级模式）",
}


@dataclass(frozen=True)
class PhaseStep:
    """一个阶段在**某个任务类型**里的位置：文案 + 期望耗时。"""

    phase: str
    seconds: float


# ══════════════════════════════════════════════════════════════════
# 期望耗时：来源全部是实测
# ══════════════════════════════════════════════════════════════════
#
# 每条都标了它是哪几次实测的均值。**改这些数字必须连着来源一起改** ——
# 一个没有出处的秒数，下一次没人敢动它，也没人知道它是不是还成立。
#
# 采集方式：runner 每次任务结束会打一行
#     [task] xxx 阶段耗时：queued=0.0s diagnosing=20.1s planning=26.2s …
# 直接照那行改这里即可。

#: 解析链路（4.2 `/layout/parse`）。
#:
#: ⚠️ **这一条只有一个成功样本，可信度低于下面两条** —— 标出来，别装作很稳。
#:   实测 2026-09-24：整链 39.2s（A-01 视觉解析 20.4s + A-02 诊断 18.5s）
#:   同日的另几次：33s、48s 各一次；还有一次 A-01 跑了 88 秒仍未返回，
#:   撞上节点 90s 熔断而失败。**波动极大是这条链路的真实属性** ——
#:   它只有两次 LLM 调用，任何一次慢都直接是整条链慢一半。
#:   所以这里取实测值而非区间上限，让速度系数去吸收慢的情况。
_PARSE_STEPS: tuple[PhaseStep, ...] = (
    PhaseStep("queued", 0.3),        # precheck 之前的调度开销
    PhaseStep("prechecking", 0.3),   # 本地计算，无 IO
    PhaseStep("analyzing", 20.4),    # A-01 视觉解析
    PhaseStep("diagnosing", 18.5),   # A-02 五维诊断
)

#: 方案生成链路（4.3 `/design/generate`）。
#:
#: 来源：2026-09-24 三次实测的均值
#:   n=1  整链 119.7s：诊断 15.8 · fan-out 21.3 · 审查 82.4 · 汇总 0.2
#:   n=2  整链 107.4s：诊断 20.1 · fan-out 26.2 · 审查 61.1 · 汇总 0.0
#:   n=3  整链 109.5s：诊断 23.9 · fan-out 29.1 · 审查 56.5 · 汇总 0.0
#:   均值 整链 112.2s：诊断 19.9 · fan-out 25.5 · 审查 66.7 · 汇总 0.1
#:
#: 两处值得记下的事实：
#:   · fan-out 九路（3 套 × 3 个产出者）是**真并行** —— 三次实测里
#:     各路耗时 11–29s，而整段只有 21.3 / 26.2 / 29.1s，重叠得很实。
#:   · 审查占总时长 **52%–69%**，是最值得优化的那一段（它内部对每套方案
#:     各打一次 LLM，取 max）。这也是"方案先交付"改动针对的目标。
_GENERATE_STEPS: tuple[PhaseStep, ...] = (
    PhaseStep("queued", 0.3),
    PhaseStep("diagnosing", 19.9),       # A-02
    PhaseStep("planning", 25.5),         # 九路并发，取整段墙钟时间
    PhaseStep("checking_risks", 66.7),   # A-06，三套方案并发取 max
    PhaseStep("finalizing", 0.1),        # execute_fan_in，纯代码
)

#: 报价单审查（4.6 `/avoid-pit/review`）：只跑 A-06 一个节点。
#:
#: 来源：2026-09-24 实测，单次约 19s。
#: ⚠️ 同一节点在方案链里实测 33–82s，差别在于那里是对**三套**方案各审一次；
#: 这里只审一份报价单。`REVIEW_TIMEOUT=75s` 是熔断上限，不是期望值。
_REVIEW_STEPS: tuple[PhaseStep, ...] = (
    PhaseStep("queued", 0.3),
    # ⚠️ 2026-09-26 从 2 个阶段拆成 3 个。**这不是为了让数字好看。**
    #
    # 原来审查链只有一个节点（`review_risks`），它内部先查知识库、
    # 再调模型 —— 于是 `/task/status` 里只有 `queued → reviewing` 两个取值，
    # 而 AC-36 要求**至少 3 个**。那条要求因此在这条链上**结构性地达不到**。
    #
    # 处置是**把检索拆成独立节点**（`retrieve_knowledge`），而不是往词汇表里
    # 塞一个不存在的阶段：检索本来就真实发生（十几个查询、约几秒），
    # 拆出来之后 `on_chain_start` 事件会真的在那一刻触发。
    #
    # 秒数取**今天这次实测**（2026-09-26，任务 teardown 里打印的
    # `_log_phase_durations`）：`retrieving=8.1s  reviewing=21.1s`
    # （整链 29.8s，在 20–52s 的实测波动区间内）。
    #
    # ⚠️ 我先按"单次检索 1.5s × 15 个查询"估了个 3.0s，**估低了近三倍** ——
    #    检索是 13 个查询串行展开的，不是并发的。这就是"期望耗时必须实测"
    #    那条纪律的又一个例子：凭公式推出来的数会把倒计时整体算快。
    PhaseStep("retrieving", 8.1),
    PhaseStep("reviewing", 21.1),
)

STEPS: dict[TaskKind, tuple[PhaseStep, ...]] = {
    "parse": _PARSE_STEPS,
    "generate": _GENERATE_STEPS,
    "review": _REVIEW_STEPS,
}

#: 速度系数的上下限。见模块说明最后一段。
PACE_MIN = 0.6
PACE_MAX = 3.0

#: 速度系数的**先验权重**，单位是"秒"。
#:
#: 相当于先摆 10 秒"速度正常"的假证据在分母上，再由实际耗时把它挤开。
#: 它的作用是让系数从 1.0 **连续地**长出来，而不是"证据够了就突然切换"。
#:
#: ⚠️ 这个数是实测改出来的。原来写的是"累计期望耗时不足 6 秒就不调速"
#: （一个硬开关），真实跑一遍就看到它的后果：解析链的 `diagnosing`
#: 阶段里倒计时稳定在 96 秒，下一格跳到 118 秒 —— **倒计时涨了 22 秒**。
#: 因为开关在那一刻翻转，系数从 1.0 直接变成 1.33。
#: 用户看到倒计时往回涨，只会认为这个数字是编的。
PACE_PRIOR_SECONDS = 10.0

#: 判定"这个阶段超时了"的宽限倍数。
#:
#: 不留宽限的话，`checking_risks` 正常跑完的那一刻（期望 82.4s，实际 81.9s，
#: 系数 0.99）就会被判超时，界面在最后 0.2 秒闪一下"比预期久"。
#: 刚好卡在边界上的抖动不该被用户看见。
OVERRUN_GRACE = 1.1


def steps_for(kind: str) -> tuple[PhaseStep, ...]:
    """取某任务类型的阶段序列。未知类型返回空序列（而不是抛错）。"""
    return STEPS.get(kind, ())  # type: ignore[arg-type]


def total_seconds(kind: str) -> float:
    """整条链的期望总耗时。创建任务时的 `estimated_seconds` 由它来。"""
    return sum(s.seconds for s in steps_for(kind))


def progress_at(kind: str, phase: str) -> int | None:
    """
    进入某阶段时的百分比 = 该阶段之前所有阶段的耗时占比。

    右端留 1% 给"完成"（`done` / `degraded` 恒为 100）。
    不留的话，最后一个阶段（`finalizing`，0.2s）的入口值会四舍五入到 100，
    进度条在**任务还没完成时**就已经满了。

    未知阶段返回 None —— 调用方据此回退到上一次的进度，
    **不能假装成 0**（那就是"跳回起点"，比不动更让人困惑）。
    """
    if phase in ("done", "degraded"):
        return 100

    steps = steps_for(kind)
    total = sum(s.seconds for s in steps)
    if total <= 0:
        return None
    for i, step in enumerate(steps):
        if step.phase == phase:
            before = sum(s.seconds for s in steps[:i])
            return round(99 * before / total)
    return None


def _next_phase(kind: str, phase: str) -> str | None:
    steps = steps_for(kind)
    for i, step in enumerate(steps):
        if step.phase == phase and i + 1 < len(steps):
            return steps[i + 1].phase
    return None


def _expected_seconds(kind: str, phase: str) -> float:
    for step in steps_for(kind):
        if step.phase == phase:
            return step.seconds
    return 0.0


def build_view(
    *,
    kind: str,
    phase: str,
    status: str,
    started_at: float | None,
    phase_entered_at: float | None,
    fallback_progress: int = 0,
    now: float | None = None,
    finished_at: float | None = None,
) -> dict[str, Any]:
    """
    由「阶段 + 时间」推出这个轮询该看到的四个数。

    这是**唯一**一处把进度、已用时间、剩余时间算在一起的地方 ——
    内存视图（`TaskRecord.to_dict`）和 Redis 视图（`TaskManager.status`）
    都走它，因此两条路径不可能给出不一样的数。

    ⚠️ 之前不是这样：内存路径按 `PHASE_PROGRESS` 算，Redis 路径按自己那份
    …其实是同一张表，所以看不出来。但只要有人改了一边，界面就会随
    "Redis 在不在"而变 —— 那种 bug 没法在演示现场排查。

    Args:
        started_at: 任务开始（epoch 秒）。None = 不知道，此时不报剩余时间。
        phase_entered_at: 当前阶段开始（epoch 秒）。None 视为刚到。
        fallback_progress: 阶段不在模型里时用的值（通常是上一次的进度）。
        finished_at: 任务**结束**的时刻。给了它就冻结"已用时间" ——
            不给的话 `now - started_at` 会一直涨：实测一个 40 秒完成的解析，
            过了一阵再看会显示"用时 56 分 56 秒"。

    Returns:
        progress / elapsed_seconds / eta_seconds / overrun
        其中 `eta_seconds=None` 表示**估不出来**，此时前端只显示已用时间。
        给出一个假数字比不给更糟。
    """
    # ⚠️ 终态用 finished_at 当"现在"：跑完之后时间必须停住。
    clock = finished_at if finished_at is not None else (time.time() if now is None else now)
    entry = progress_at(kind, phase)

    if status == "completed" or phase in ("done", "degraded"):
        return {
            "progress": 100,
            "elapsed_seconds": _round_elapsed(started_at, clock),
            "eta_seconds": 0,
            "overrun": False,
        }

    if started_at is None:
        # 不知道什么时候开始的（例如进程重启后只从 Redis 里读到半截状态）：
        # 进度照给，时间相关的三个数一律不给。
        return {
            "progress": entry if entry is not None else fallback_progress,
            "elapsed_seconds": None,
            "eta_seconds": None,
            "overrun": False,
        }

    elapsed = max(clock - started_at, 0.0)
    entered = phase_entered_at if phase_entered_at is not None else started_at
    in_phase = max(clock - entered, 0.0)

    if entry is None:
        # 阶段不在模型里。进度停在原地，也不编剩余时间。
        return {
            "progress": fallback_progress,
            "elapsed_seconds": round(elapsed),
            "eta_seconds": None,
            "overrun": False,
        }

    pace = _pace(kind, phase, elapsed, in_phase)
    expected = _expected_seconds(kind, phase)
    eta, overrun = _eta(kind, phase, in_phase, expected, pace, status)

    return {
        "progress": _interpolated(kind, phase, entry, in_phase, expected, pace, status),
        "elapsed_seconds": round(elapsed),
        "eta_seconds": eta,
        "overrun": overrun,
    }


def _round_elapsed(started_at: float | None, clock: float) -> int | None:
    if started_at is None:
        return None
    return round(max(clock - started_at, 0.0))


def _pace(kind: str, phase: str, elapsed: float, in_phase: float) -> float:
    """
    任务整体跑得比模型标定的快还是慢。1.0 = 一致。

    算法是**带先验的比值**：

        系数 = (已走完阶段的实际耗时 + 本阶段已确定的部分 + 先验)
               / (已走完阶段的期望耗时 + 本阶段已确定的部分 + 先验)

    「先验」见 `PACE_PRIOR_SECONDS`：先摆一份"速度正常"的假证据，
    再由实际耗时把它挤开。这样系数是**连续**长出来的 —— 不会出现
    "证据攒够了、系数突然从 1.0 跳到 1.33、倒计时跟着往回涨 22 秒"。

    分子是 `elapsed`（任务从开始到现在的总耗时），分母是"这些时间里
    按模型**应当**花掉的部分"：走完的阶段按期望算，当前阶段只算
    `min(in_phase, expected)` —— 后面这一项是**封顶**的，因为一个刚开始
    跑的阶段还说明不了自己慢不慢。

    ⚠️ **封顶只加在分母上，分子用总耗时** —— 这是让系数在阶段切换处
    **连续**的关键：分母里的 `min(in_phase, expected)` 在 `in_phase`
    涨到 `expected` 时刚好不再增长，而与此同时 `basis` 恰好增加
    `expected`，两边的变化互相抵消。写成两边都封顶的话，就会出现
    "阶段结束那一刻系数突然跳一下、倒计时跟着往回涨 20 秒"。
    """
    steps = steps_for(kind)
    basis = 0.0
    for step in steps:
        if step.phase == phase:
            break
        basis += step.seconds

    current = min(max(in_phase, 0.0), _expected_seconds(kind, phase))
    denominator = basis + current + PACE_PRIOR_SECONDS
    if denominator <= 0:
        return 1.0
    observed = (max(elapsed, 0.0) + PACE_PRIOR_SECONDS) / denominator
    return min(max(observed, PACE_MIN), PACE_MAX)


def _interpolated(
    kind: str, phase: str, entry: int, in_phase: float,
    expected: float, pace: float, status: str,
) -> int:
    """
    阶段内部让条子**慢慢爬**，但只爬到下一个阶段入口的前一格。

    ⚠️ 为什么需要它：A-06 一个阶段占 82 秒（整链的 67%）。期间阶段名不变，
    条子要是完全不动，那 82 秒就是"看起来卡死了" —— 与 2026-09-23 那次
    "进度条一直是 0"的投诉是同一类观感问题。

    封顶到"下一格减一"是必须的：慢的时候条子不能冲进下一个阶段的区间，
    否则接下来会出现"条子满了但任务还在跑"。
    """
    # 任务已经结束（成功或失败）就不再爬：失败时应当**停在断点**，
    # 让用户看得出是走到哪一步断的。
    if status in ("completed", "failed") or expected <= 0:
        return entry

    next_phase = _next_phase(kind, phase)
    if next_phase is None:
        return entry
    next_entry = progress_at(kind, next_phase)
    if next_entry is None or next_entry <= entry + 1:
        return entry

    frac = min(in_phase / (expected * pace), 1.0)
    return min(round(entry + (next_entry - entry - 1) * frac), next_entry - 1)


def _eta(
    kind: str, phase: str, in_phase: float,
    expected: float, pace: float, status: str,
) -> tuple[int | None, bool]:
    """
    剩余秒数 = （本阶段尚未走完的部分 + 后续阶段全部）× 速度系数。

    返回 `(eta, overrun)`。**模型被追上的时候不给数字**（返回 None + overrun）——
    那正是"预计还剩 16 秒"要挂 70 秒的场景，而一个已经失效的数字
    比没有数字更消耗信任。界面此时改口说"比预期久，任务仍在进行"。

    ⚠️ **`overrun` 的判据是"模型整体被追上了"**，也就是：

        当前阶段超出的部分  >  后面所有阶段加起来还需要的量

    左边是 `in_phase - expected`（本阶段已经花掉的、超出预期的那些时间）；
    右边是 `after`（后续阶段期望耗时之和）。左边超过右边，意味着**光是这一段
    的超时，就已经吃掉了模型对剩余时间的全部估计** —— 再往下报任何数字都是编的。

    ⚠️ 左边刻意**不乘速度系数**，尽管上面报剩余时间时要乘。
    这是两个不同的问题：速度系数回答"照这个节奏还要多久"，
    overrun 回答"还该不该开口"。乘上去的话会自相矛盾 —— 阶段拖得越久，
    系数越大，判定门槛就被自己抬高，"超期 3 倍"反而算不出超期。

    ⚠️ 这条规则是**实测改出来的，前两版都是错的**：

    第一版：「当前阶段超过期望的 1.5 倍」—— 任务刚起 1 秒就报"比预期久"
    （`queued` 期望 0.3s，而图真正启动本来就要 1 秒）。刚开局就唱衰等于没提示。

    第二版：「当前阶段超时 **且** 后面没有实质阶段（`after < 1s`）」——
    在方案链上看着没问题，因为审查正好是最后一个实质阶段。
    但 2026-09-24 拿真实的解析链路一跑就露馅了：视觉解析本该 16 秒、
    实际跑了 88 秒直到节点超时，而**倒计时全程钉在"16 秒"** ——
    因为后面还有个 `diagnosing`（期望 16s），`after` 不满足条件。
    用户盯着一个永远不动的"预计还剩 16 秒"，比没有倒计时还糟。

    现在的判据两条都能覆盖：解析链那一刻左边（32s）大于右边（16s）→ 报超时；
    而方案链的 `planning` 慢一倍时左边（21s）远小于右边（72s）→ 不报，
    因为整体倒计时确实还有效。
    """
    if status in ("completed", "failed"):
        return (0, False) if status == "completed" else (None, False)

    steps = steps_for(kind)
    after = 0.0
    seen = False
    for step in steps:
        if seen:
            after += step.seconds
        elif step.phase == phase:
            seen = True
    if not seen:
        return None, False

    if expected > 0:
        overspent = in_phase - expected * OVERRUN_GRACE
        if overspent > after:
            return None, True

    remaining = max(expected - in_phase, 0.0) + after
    # 至少 1 秒：界面上"还剩 0 秒"就是"马上就好"，不必显示成负数或 0。
    return max(round(remaining * pace), 1), False
