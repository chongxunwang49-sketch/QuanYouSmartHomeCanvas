"""
pytest 根配置：确保 `backend` / `mcp_servers` 可作为顶层包导入。

═══════════════════════════════════════════════════════════════════
为什么 pytest.ini 里不能有中文
═══════════════════════════════════════════════════════════════════
`pytest.ini` 是 **iniconfig 按 locale 编码**读的，不是按 UTF-8 读的。
中文 Windows 的 locale 是 cp936，于是只要那个文件里出现一个中文字符，
`pytest` 就在**解析配置阶段**崩掉：

    UnicodeDecodeError: 'gbk' codec can't decode byte 0x80 in position 175

这个报错栈全在 iniconfig 内部，跟被测代码毫无关系，非常难往"配置文件编码"
上想。而且它是**环境相关**的：UTF-8 locale 的机器上完全正常，换一台中文
Windows 就复现 —— 属于最难查的那类 bug。

所以约定：**`pytest.ini` 保持纯 ASCII，中文说明一律放这里。**

对应的两条配置为什么长那样，记在这儿：

· `addopts` 里的 `-m "not integration"` **必须留着**。在 `[markers]` 里声明
  只等于"登记了这个标记"，并不等于"默认不跑"。少了这一条，日常跑一次
  `pytest` 会**静默地**打真实 DeepSeek API —— 慢、花钱，而且测试结果从此
  依赖网络。

· `asyncio_mode = auto` 让 `async def test_xxx` 直接可跑，不必每个都挂
  `@pytest.mark.asyncio`。接口层测试要轮询后台任务，同步写法只能靠 sleep
  硬等，很难写对。
"""

from __future__ import annotations

import asyncio
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


@pytest.fixture(autouse=True, scope="session")
def _fixed_auth_secret():
    """
    把 `AUTH_SECRET` 钉死成一个测试值。

    ⚠️ 这条是**跨测试文件**的必需品，不是洁癖：

    `core/auth.py` 在密钥未配置时**每个进程随机生成**一把。而 pytest
    整个会话跑在同一个进程里 —— 只要有一个测试重置了密钥缓存
    （`tests/test_auth.py` 的 autouse fixture 就会），
    别处已经签发、还在用的令牌就全部失效，
    表现为"下游某个用例莫名其妙 4003"，而原因离现场很远。

    钉死之后令牌在整个测试会话里稳定，`test_auth.py` 里那两条
    专门验证密钥行为的用例会**临时改回空值**来测真实路径。
    """
    from backend.app.core import auth
    from backend.app.core.config import settings

    original = settings.AUTH_SECRET
    settings.AUTH_SECRET = "conftest-fixed-secret-not-a-real-key"
    auth.reset_secret_cache()
    yield
    settings.AUTH_SECRET = original
    auth.reset_secret_cache()


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """
    每个用例丢掉 Redis 单例。

    pytest-asyncio 每个用例一个新事件循环，而 `redis.asyncio` 的客户端
    绑定在创建它的循环上 —— 不重置的话，同一文件里**第二个**碰 Redis 的
    用例就会报 `Event loop is closed`。详见 `reset_redis_client()` 的说明。
    """
    from backend.app.core.redis_client import reset_redis_client

    reset_redis_client()
    yield
    reset_redis_client()


@pytest.fixture(autouse=True)
async def _drain_background_tasks():
    """
    每个用例结束后**取消在飞的图任务**，再丢弃任务注册表。

    ⚠️ 这是 2026-09-23 定位到的一个**真实的测试污染**，不是洁癖。

    现象：`pytest tests/test_api.py` 偶发失败，且失败在
    `TestAuthGate` 的配额用例上，报 `assert 0 == 4006` —— 看着像权限逻辑错了。

    实测（`pytest -s`，把降级告警打出来）：
        DIAG  4 次请求全部放行，而 Redis 里的计数器只涨到 2
        WARN  [quota] Redis 不可用，本次未计数即放行：user=vip task=generate  ×2
        E     redis.exceptions.TimeoutError: Timeout reading from localhost:6379

    真因链：`/layout/parse` 与 `/design/generate` 的接口用例会**真的起图任务**，
    而它们从不被回收 —— 每个 `generate` 任务 fan-out 九路并发 Agent，
    这些协程在用例结束后仍在事件循环里打转。用例越跑越多，
    事件循环被挤到 **Redis 往返超过 `socket_timeout=3s`** →
    `check_and_consume_quota` 走降级路径（放行且不计数）→ 配额上限测不出来。

    在 HEAD 上用 `git worktree` 跑过对照：**同样的失败在改动之前就存在**。

    所以这里在用例之间把任务清干净。这既消掉了偶发失败，
    也让套件跑得更快（少了几十个白烧 CPU 的协程）。
    """
    yield
    try:
        from backend.app.api.tasks import get_task_manager, reset_task_manager

        # shutdown 会把注册表标记为"停机中"，所以之后必须 reset，
        # 否则下一个用例连 `create()` 都会被拒（见 TaskManager.create）。
        #
        # ⚠️ `timeout=1` 而不是 3：`cancel()` 是立刻发出的，这个超时只决定
        #    **等多久确认它退干净**。而图任务内部有 `asyncio.shield()` 包住的
        #    子协程（langgraph 的重试监视线），外层被取消后它们不一定会跟着退，
        #    于是几乎每个用例都要白等满整个超时。实测 `timeout=3` 把整套
        #    从 101s 拖到 228s；1s 足够拿到"取消已送达"的效果。
        await get_task_manager().shutdown(timeout=1)
        reset_task_manager()
    except Exception as e:  # noqa: BLE001 —— 清理失败不该让用例变红
        import warnings

        warnings.warn(f"清理后台任务失败（忽略）：{type(e).__name__}: {e}")


@pytest.fixture(scope="session", autouse=True)
def _restore_demo_quota():
    """
    整套测试跑完后，把演示账号的额度计数清回干净。

    ⚠️ 不加这个的话，跑一次 `pytest` 就会把 `vip`/`demo` 当天的生成额度用光
    （配额用例会故意打满），于是**演示时一打开就是"今日已用完"** ——
    而且它会在 Redis 里挂到次日零点，看着像权限配错了。

    实测：`/auth/quota` 在跑完测试后返回
        generate  used=3 limit=3   → 界面显示"今日已用完"
    而这不是代码问题，是测试留下的状态。

    session 级：只在整套跑完时清一次，不是每个用例都清（那样反而是几千次
    Redis 往返）。
    """
    yield
    try:
        import asyncio
        from datetime import date

        from backend.app.core import auth
        from backend.app.core.config import settings
        from backend.app.core.redis_client import get_redis

        async def _clear() -> bool:
            client = await get_redis().client()
            if client is None:
                return False
            today = date.today().isoformat()
            keys = [
                f"{settings.REDIS_QUOTA_PREFIX}:{kind}:{u.id}:{today}"
                for u in auth.users()
                for kind in ("parse", "generate", "review")
            ]
            await client.delete(*keys)
            return True

        asyncio.run(_clear())
    except Exception:  # noqa: BLE001 —— 清理失败不该让整轮测试算失败
        pass


@pytest.fixture(autouse=True)
def _isolated_user_overrides(tmp_path, monkeypatch):
    """
    把「运行期用户覆盖」隔离到临时文件。

    ⚠️ **这是必需的，不是洁癖。** `data/users_override.json` 是**演示状态的
    落盘位置** —— 你在界面上点一下"开通会员"，`demo` 就变成 paid 了。
    如果测试读的是那个真实文件，后果是：

        · `test_免费用户用不了付费功能_付费用户可以` 会因为找不到 free 用户
          抛 `StopIteration` —— 报错信息完全指不到"你刚才演示过"
        · 表现是"昨天还全绿，今天红了"，而且改代码怎么都不好

    测试必须只依赖 `seed_data/users.json` 这份**入库的**种子状态。
    每个用例一个空目录，写完就丢。
    """
    from backend.app.core import auth

    original = auth.OVERRIDE_PATH
    auth.OVERRIDE_PATH = tmp_path / "users_override.json"
    auth.reset_user_cache()
    try:
        yield
    finally:
        auth.OVERRIDE_PATH = original
        auth.reset_user_cache()


@pytest.fixture(autouse=True)
def _block_real_llm(request, monkeypatch):
    """
    禁止测试发起**任何真实出网请求**。

    **这不是防御性编程，是因为真的发生过。**
    接入 fan-out 后，测试里出现了一次未被替换的 Agent 实例（`workflow._AGENTS`
    是在 import 时构造的，而 fixture 只替换了其中两个），于是三个分支
    各自打了 9-11 秒的真实 DeepSeek API —— 测试**照样全绿**，只是慢了 100 秒、
    花掉了真实 token，还顺手把测试数据发了出去。

    沉默的代价最贵：这类问题不会报错，只会让测试慢慢变慢、账单慢慢变高，
    直到某天有人发现 CI 在花钱。

    ═══════════════════════════════════════════════════════════════
    绊线设在哪一层 —— 一次真实的教训
    ═══════════════════════════════════════════════════════════════

    第一版绊线只拦了 `llm_client.httpx.AsyncClient`。它守住了 LLM 这一条路，
    但接入 A-06 之后，`services/knowledge/store.py` **自己 import 了 httpx**
    并用 `httpx.Client` 调 Ollama —— 新开了一条路，绕过了旧绊线，
    测试于是真的去打本机 Ollama（表现为某个"应该 0.6s 内完成"的测试跑了 4.9s）。

    这说明：**绊线必须守在"能力"上，而不是守在某个调用点上。**
    守调用点的话，每加一个模块就多一个漏口，而且漏口不报错、只是变慢 ——
    和这个绊线最初要防的问题一模一样。

    所以改成在 **httpx 模块级**替换 Client / AsyncClient 两个类：
    任何模块、任何写法，只要真的想出网，都会撞上。
    本地 Ollama 也在拦截范围内 —— 测试不该依赖本机是否开着模型服务。

    `@pytest.mark.integration` 的测试是唯一的例外——它们**就是**要打真实服务，
    由开发者显式 `pytest -m integration` 触发，不该被绊线误伤。
    """
    if request.node.get_closest_marker("integration"):
        return

    import httpx

    _MESSAGE = (
        "测试试图发起真实网络请求（httpx 已被 conftest 拦截）。\n"
        "常见原因：\n"
        "  1) workflow._AGENTS 里有 Agent 没被替换成 Fake LLM\n"
        "  2) 新增了走网络的模块（embedding / 向量库 / 第三方 API）\n"
        "修法：把该依赖在测试里打桩；确实需要真出网的测试请标 @pytest.mark.integration。"
    )

    def _is_in_process(client) -> bool:
        """
        这次请求是不是**只在进程内**。

        FastAPI 的 `TestClient` 用 `httpx.ASGITransport` —— 请求直接交给
        ASGI app 处理，一个字节都不出网。API 层测试全靠它，
        一刀切拦掉会让接口层完全没法测。

        判据用 **transport 的类型**而不是 base_url 之类的字符串：
        ASGITransport 是明确的"进程内"信号；字符串可以随便写，
        拿它当判据等于给自己留后门。
        """
        return type(getattr(client, "_transport", None)).__name__ == "ASGITransport"

    _real_send = httpx.Client.send
    _real_async_send = httpx.AsyncClient.send

    def _guarded_send(self, request, **kwargs):
        if _is_in_process(self):
            return _real_send(self, request, **kwargs)
        raise RuntimeError(_MESSAGE)

    async def _guarded_async_send(self, request, **kwargs):
        if _is_in_process(self):
            return await _real_async_send(self, request, **kwargs)
        raise RuntimeError(_MESSAGE)

    # 拦在 `send` 这一个出口上，而不是替换 Client 类 ——
    # 类身份保持不变，`isinstance` 与 starlette 内部的用法都不受影响。
    monkeypatch.setattr(httpx.Client, "send", _guarded_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded_async_send)


# ══════════════════════════════════════════════════════════════════
# 知识库打桩
# ══════════════════════════════════════════════════════════════════

#: 打桩用的假 chunk。**故意做成两条** —— 这样"引用映射"才测得出东西：
#: 模型引用 S1 应映射到第一条，引用 S9 应被剔除。
STUB_KNOWLEDGE_CHUNKS = [
    {
        "text": "只有单价没有总价的项目，都是为了让合同价看起来低。量房之后所有能精确测量的项目都应一次性算进预算。",
        "source": "zhuangxiu-skills/装修报价审核/references/预算陷阱避坑.md",
        "headings": "四、装修预算只有单价坑你没商量",
        "doc_type": "avoid_pit",
        "tags": ["报价审核", "单价陷阱"],
        "similarity": 0.81,
    },
    {
        "text": "定金是付款的担保，具有法律约束力，买方不履行合同卖方可以不退还；订金一般视为预付款，可以退。",
        "source": "zhuangxiu-skills/装修合同审核/references/定金订金类.md",
        "headings": "一、定金和订金的法律区别 / 1. 定金（不能退）",
        "doc_type": "avoid_pit",
        "tags": ["合同", "定金"],
        "similarity": 0.78,
    },
]


@pytest.fixture
def stub_knowledge(monkeypatch):
    """
    把知识库检索打桩。

    **为什么不让它走真实路径**：A-06 的检索要调 Ollama 算向量，
    而上面的 httpx 绊线会拦住它 —— 于是每个工作流测试都会走"知识库不可用"
    的降级分支，正常路径反而没被测到。

    这个 fixture 提供确定的假 chunk，让引用映射（S1→真实出处、S9→被剔除）
    成为可断言的行为，而不是依赖本机是否开着 Ollama。
    """
    from backend.app.services.knowledge import retriever

    def _stub(queries, **kwargs):   # 接受 top_k_each / doc_type / max_total 等任何参数
        chunks = [
            retriever.KnowledgeChunk(
                text=c["text"], source=c["source"], headings=c["headings"],
                doc_type=c["doc_type"], tags=list(c["tags"]),
                similarity=c["similarity"],
            )
            for c in STUB_KNOWLEDGE_CHUNKS
        ]
        return retriever.RetrievalResult(
            query=" | ".join(queries) if isinstance(queries, list) else str(queries),
            chunks=chunks,
        )

    monkeypatch.setattr(retriever, "search_many", _stub)
    return _stub


# ══════════════════════════════════════════════════════════════════
# 测试用户型图
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def floorplan_bytes() -> bytes:
    """
    一张**能通过图片预检**的合成户型图（AC-27）。

    为什么不能再用 `image_ref="data:image/png;base64,AAAA"` 这种占位值：
    解析链路的第一跳现在是真实的图片预检，占位值会被它正确地拦下来 ——
    于是用例挂在预检上，而不是挂在它真正想测的那件事上。

    实现在 `tests/helpers.py`：那里的函数是普通函数，**模块级的 `_state()`
    之类的辅助函数也能用**，而 fixture 不行。
    """
    from tests.helpers import floorplan_png

    return floorplan_png()


@pytest.fixture(scope="session")
def floorplan_data_uri() -> str:
    """上面那张图的 data URI 形式 —— 可直接塞进 `image_ref`。"""
    from tests.helpers import floorplan_data_uri as _uri

    return _uri()
