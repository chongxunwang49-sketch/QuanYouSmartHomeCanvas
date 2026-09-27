"""
扫一遍前端模板，看还有没有"工程词"留在界面上。

    python scripts/probes/_check_ui_words.py

只扫**会渲染**的部分：模板里的纯文本、`{{ }}` 里的字符串字面量、
`<script>` 里的字符串字面量（它们经 `{{ }}` 插进模板）。注释一律剥掉
—— 本项目的注释大量使用这些词，那是刻意且正确的。

⚠️ 这是**提示工具，不是断言**：词表里有一半是"得看上下文"的词
（`接口`、`参数`、`渲染`…），真正的判据是"这句话是不是在讲这套系统怎么搭的"。
把它当筛子用，逐条看，别当门禁用。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

#: 基本可判定的工程词（命中就该看一眼）
HARD = [
    "Agent", "落库", "审计", "引擎", "缓存", "Token", "轮询", "队列",
    "字段", "枚举", "配置项", "环境变量", "重启", "路径", "脚本",
    "A-0", "fan-in", "chunk", "RAG", "Ollama", "Chroma", "bge",
    "deepseek", "redis", "postgres", "docker", "uvicorn",
]

#: 需要看上下文的（可能是正常产品文案）
SOFT = ["接口", "参数", "渲染", "触发", "请求", "返回", "模型", "向量", "降级"]


def strip_comments(src: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    return s


def main() -> int:
    hard_hits: list[str] = []
    soft_hits: list[str] = []
    for path in sorted(SRC.rglob("*.vue")):
        body = strip_comments(path.read_text(encoding="utf-8"))
        for i, line in enumerate(body.splitlines(), 1):
            for w in HARD:
                if w in line:
                    hard_hits.append(f"{path.name}:{i}  [{w}]  {line.strip()[:110]}")
            for w in SOFT:
                if w in line:
                    soft_hits.append(f"{path.name}:{i}  [{w}]  {line.strip()[:110]}")

    print(f"═══ 基本可判定（{len(hard_hits)} 处）═══")
    for h in hard_hits:
        print("  " + h)
    print(f"\n═══ 需要看上下文（{len(soft_hits)} 处，可能都是正常文案）═══")
    for h in soft_hits:
        print("  " + h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
