"""pytest 根配置：确保 `backend` / `mcp_servers` 可作为顶层包导入。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 本机控制台默认 GBK，测试里打印中文/emoji 会 UnicodeEncodeError 崩掉
for _stream in (sys.stdout, sys.stderr):
    _rc = getattr(_stream, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 网络绊线
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _block_real_llm(request, monkeypatch):
    """
    禁止测试发起真实 LLM 请求。

    **这不是防御性编程，是因为真的发生过。**
    接入 fan-out 后，测试里出现了一次未被替换的 Agent 实例（`workflow._AGENTS`
    是在 import 时构造的，而 fixture 只替换了其中两个），于是三个分支
    各自打了 9-11 秒的真实 DeepSeek API —— 测试**照样全绿**，只是慢了 100 秒、
    花掉了真实 token，还顺手把测试数据发了出去。

    沉默的代价最贵：这类问题不会报错，只会让测试慢慢变慢、账单慢慢变高，
    直到某天有人发现 CI 在花钱。

    这里在 `httpx.AsyncClient` 这一层设绊线 —— LLMClient 的所有出站请求都
    经过它，拦住即覆盖全部路径（DeepSeek 的 /v1 与 /anthropic、Ollama 本地）。
    本地 Ollama 也一并拦掉：测试不该依赖本机是否开着模型服务。

    `@pytest.mark.integration` 的测试是唯一的例外——它们**就是**要打真实 API，
    由开发者显式 `pytest -m integration` 触发，不该被绊线误伤。
    """
    if request.node.get_closest_marker("integration"):
        return

    import backend.app.core.llm_client as _llm_client

    class _BlockedAsyncClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "测试试图发起真实 LLM 请求（httpx.AsyncClient 已被 conftest 拦截）。\n"
                "常见原因：workflow._AGENTS 里有 Agent 没被替换成 Fake LLM。\n"
                "修法：用 tests 里的 patched fixture，或显式构造带 Fake LLM 的 Agent 实例。"
            )

    monkeypatch.setattr(_llm_client.httpx, "AsyncClient", _BlockedAsyncClient)
