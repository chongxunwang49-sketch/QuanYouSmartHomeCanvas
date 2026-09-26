"""
后端**用户可见文案**里的 Markdown —— 静态扫描。

══════════════════════════════════════════════════════════════════
为什么后端要管这件事
══════════════════════════════════════════════════════════════════
界面上的文字不全是前端写的。这几处的原文都来自**后端**，前端只是把它
原样显示出来：

  · `ApiError(code, message)` 的 message   → 弹窗 / 提示条
  · `issues` / `notes` / `warnings` 列表项  → 3D 页的问题清单、方案的降级说明
  · 家具拒绝原因 `"reason"`                 → 3D 页 HUD 的悬停提示

而 Vue 模板**不解析 Markdown**。所以后端的 `**强调**` 到了界面上就是
四个字面的星号。

══════════════════════════════════════════════════════════════════
实测踩过两次，所以各钉一条
══════════════════════════════════════════════════════════════════
**第一次** 前端：`GenerateView.vue` 的模板里手写了 `**排序偏好**`，
用无头浏览器打开时看到页面上真的渲染出星号。→
`test_frontend_contract.py::test_模板里不残留Markdown强调语法` 扫 `.vue`。

**第二次** 后端（2026-09-24）：`routes.py` 里那句 4004 写着
「此时**不会退回默认摆放**，因为那会让你以为这套 3D 是按你选的方案摆的」。
它是**在家具报错条上被看见的** —— 而那句话本身正在解释一个错误，
却自带四个星号。

前端那条扫不到后端，所以补这一条。

⚠️ 只扫"用户可见"的位置，不扫注释与文档字符串：这个项目大量用 `**…**`
   写中文强调注释，那是对的、也是有价值的。扫全文件会把它们全判成违规。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"

#: 用户可见文案的落点。**只认这些调用**，别的 `**` 一律不管。
_SINKS = (
    "issues.append",
    "notes.append",
    "warnings.append",
    "problems.append",
)


def _balanced(src: str, open_idx: int) -> str:
    """从 `(` 开始取到配对的 `)` —— 文案里带括号、跨多行都要吃得下。"""
    depth, i = 0, open_idx
    in_str: str | None = None
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1: i]
        i += 1
    raise AssertionError("括号不配对 —— 这个扫描器需要跟着改")


def _string_literals(blob: str) -> list[str]:
    return re.findall(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', blob)


def _joined_text(blob: str) -> str:
    """把若干字面量拼成一整句（Python 的隐式拼接就是这个语义）。"""
    parts: list[str] = []
    for a, b in _string_literals(blob):
        parts.append(a or b)
    return "".join(parts)


def _violations() -> list[tuple[str, int, str, str]]:
    out: list[tuple[str, int, str, str]] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        src = path.read_text(encoding="utf-8")

        # ── ApiError(code, "…") ──
        for m in re.finditer(r"\bApiError\s*\(", src):
            blob = _balanced(src, m.end() - 1)
            text = _joined_text(blob)
            if "**" in text:
                out.append((str(path.relative_to(REPO)),
                            src[:m.start()].count("\n") + 1, "ApiError", text))

        # ── issues/notes/warnings.append("…") ──
        for sink in _SINKS:
            for m in re.finditer(re.escape(sink) + r"\s*\(", src):
                blob = _balanced(src, m.end() - 1)
                text = _joined_text(blob)
                if "**" in text:
                    out.append((str(path.relative_to(REPO)),
                                src[:m.start()].count("\n") + 1, sink, text))

        # ── 家具拒绝原因 {"reason": "…"} ──
        for m in re.finditer(r'"reason"\s*:\s*(.*)', src):
            line = m.group(1)
            for a, b in _string_literals(line):
                text = a or b
                if "**" in text:
                    out.append((str(path.relative_to(REPO)),
                                src[:m.start()].count("\n") + 1, '"reason"', text))
    return out


def test_扫描器抓得到东西():
    """
    ⚠️ **先证明这条测试不是空跑的。**

    正则写歪了的话 `_violations()` 永远是空列表，下面那条一路绿灯 ——
    而它本该抓到的东西照样溜过去。这里拿一段**构造的**源码验证落点
    确实被识别（不依赖仓库里当前有没有违规）。
    """
    fake = '''
def f():
    issues.append("这条**不该**带星号")
    raise ApiError(4001, "这条也**不该**")
    rows.append({"reason": "还有**这条**"})
'''
    found: list[str] = []
    for m in re.finditer(r"\bApiError\s*\(", fake):
        found.append(_joined_text(_balanced(fake, m.end() - 1)))
    for sink in _SINKS:
        for m in re.finditer(re.escape(sink) + r"\s*\(", fake):
            found.append(_joined_text(_balanced(fake, m.end() - 1)))
    for m in re.finditer(r'"reason"\s*:\s*(.*)', fake):
        for a, b in _string_literals(m.group(1)):
            found.append(a or b)

    with_stars = [t for t in found if "**" in t]
    assert len(with_stars) >= 3, (
        f"构造的样本文案里应当抓到 3 处带 ** 的，实际抓到 {len(with_stars)} 处：{found}"
    )


def test_后端的用户可见文案里没有Markdown强调():
    bad = _violations()
    assert not bad, (
        "以下**会显示在界面上**的文案里带了 Markdown 强调 —— "
        "Vue 模板不解析 Markdown，`**x**` 会字面渲染成四个星号：\n"
        + "\n".join(
            f"  {path}:{line}  [{sink}]  {text[:90]}"
            for path, line, sink, text in bad
        )
        + "\n\n前端模板同类问题由 "
          "test_frontend_contract.py::test_模板里不残留Markdown强调语法 守；"
          "\n后端这几处（ApiError / issues / notes / warnings / reason）"
          "原来没人管 —— 实测 2026-09-24 在家具报错条上看到过。"
    )
