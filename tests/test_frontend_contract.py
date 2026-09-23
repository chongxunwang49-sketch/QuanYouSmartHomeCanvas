"""
前后端契约的静态校验。

══════════════════════════════════════════════════════════════════════
为什么后端测试要去读前端源码
══════════════════════════════════════════════════════════════════════
2026-09-23 实测踩到一个**静默失败**：

  前端「生成参数」第 3 套发的是 `luxury`，而后端 `PlanStyle` 里没有这个值。
  下游 `space_planner.py` 用
      `_STYLE_HINTS.get(style, '按该风格的通行做法处理')`
  兜底 —— 那一套方案**完全没拿到风格引导**，但界面上仍显示「意式轻奢」。
  一次都没报错。

这类"跨端枚举对不上"的问题，两端的测试都看不见：
  · 后端测试发的是后端认识的值
  · 前端没有测试框架（见 README「已知限制」）

所以在这里补一道静态比对：**直接解析前端的源文件**，与后端的枚举逐字比。
它不优雅，但它是这个项目里唯一能拦住这类 bug 的地方。

⚠️ 前端源码不在时**跳过而不是失败** —— `deploy/Dockerfile` 不 COPY
   `frontend/`，而 `tests/` 也不进镜像；但万一有人在只有后端的目录里
   跑测试，这里不该把整个测试套件弄红。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from backend.app.schemas.plan import BudgetGrade, PlanStyle

REPO = Path(__file__).resolve().parents[1]
TYPES_TS = REPO / "frontend" / "src" / "api" / "types.ts"
GENERATE_VUE = REPO / "frontend" / "src" / "views" / "GenerateView.vue"

pytestmark = pytest.mark.skipif(
    not TYPES_TS.exists(),
    reason="前端源码不在（例如只部署了后端的目录），跳过契约校验",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_前端风格label与后端枚举逐字一致():
    """
    `STYLE_LABEL` 既是展示用的中文名表，**又是「生成参数」里风格下拉框的
    可选项来源**（`GenerateView` 里 `v-for="(v, k) in STYLE_LABEL"`）。

    所以它多一个键 = 用户能选到一个后端不支持的值；少一个键 = 某个
    后端支持的风格在界面上选不到。两边必须逐字相同。
    """
    src = _read(TYPES_TS)
    block = re.search(
        r"STYLE_LABEL:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", src, re.S
    )
    assert block, "types.ts 里找不到 STYLE_LABEL，契约测试需要跟着改"
    fe = sorted(re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M))
    be = sorted(get_args(PlanStyle))
    assert fe == be, (
        f"前端 STYLE_LABEL 与后端 PlanStyle 不一致。\n"
        f"  前端独有: {sorted(set(fe) - set(be))}\n"
        f"  后端独有: {sorted(set(be) - set(fe))}\n"
        f"前端多出的键会让用户选到后端不认识的值，而后端只会静默兜底。"
    )


def test_前端预算档label与后端枚举逐字一致():
    src = _read(TYPES_TS)
    block = re.search(
        r"GRADE_LABEL:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", src, re.S
    )
    assert block, "types.ts 里找不到 GRADE_LABEL"
    fe = sorted(re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M))
    be = sorted(get_args(BudgetGrade))
    assert fe == be, f"前端 GRADE_LABEL 与后端 BudgetGrade 不一致：{fe} vs {be}"


def test_默认三套方案全都是后端认可的取值():
    """
    `PAIRS` 是「不填参数时一次生成哪三套」的默认值。
    里面任何一个值不被后端认可，那一套就会静默失去风格/档位引导 ——
    这正是 `luxury` 那个 bug 的形态。
    """
    if not GENERATE_VUE.exists():
        pytest.skip("GenerateView.vue 不在")
    src = _read(GENERATE_VUE)
    block = re.search(r"const PAIRS = \[(.*?)\] as const", src, re.S)
    assert block, "GenerateView.vue 里找不到 PAIRS"

    styles = re.findall(r"style:\s*'([^']+)'", block.group(1))
    grades = re.findall(r"grade:\s*'([^']+)'", block.group(1))
    assert styles and grades, f"PAIRS 解析失败：styles={styles} grades={grades}"

    assert all(s in get_args(PlanStyle) for s in styles), (
        f"PAIRS 里有后端不认识的风格："
        f"{[s for s in styles if s not in get_args(PlanStyle)]}"
    )
    assert all(g in get_args(BudgetGrade) for g in grades), (
        f"PAIRS 里有后端不认识的档位："
        f"{[g for g in grades if g not in get_args(BudgetGrade)]}"
    )
    # 按位置配对，长度不等时 zip 会截断（workflow.py 只告警不报错）
    assert len(styles) == len(grades), "PAIRS 的风格与档位数量必须相等（按位置配对）"
