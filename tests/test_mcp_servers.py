"""
MCP 工具层（AC-11）。

本文件守的**不是业务逻辑**（那是 `test_budget_engine.py` / `test_render_svg.py`
的活），而是两件只有在这一层才会出问题的事：

【1】**协议这一层真的通。** 起子进程、列工具、传参、取回结构化结果。
     Windows 上子进程的编码是老大难（`core/mcp_client.py` 的文件头专门记了
     这条：不注入 `PYTHONUTF8=1`，JSON-RPC 报文在 GBK 下会乱码）。
     这类失败在单测里看不出来 —— 必须真的走一遍 stdio。

【2】**壳是薄的，而且可以被证明是薄的。** 这一条最重要：
     MCP 返回的预算/ SVG 必须与**进程内直接调用**逐字节/逐字段相同。
     两边各写一份实现的话，会随调优慢慢分叉 —— 而预算是拿去跟装修公司
     砍价的，分叉出来的是"用户按 A 看到 8 万、按 B 看到 10 万"。

     ⚠️ 只断言"MCP 能返回一个看起来合理的数"是不够的：那种断言在两边
     实现已经漂移之后**照样会绿**。所以下面比的是同一份输入的两种取法。

跑起来偏慢（每个用例要起一次 Python 子进程，实测单次约 1.2s），
这是走真实协议的代价，不省。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp", reason="MCP 工具层需要 mcp SDK（见 requirements.txt 的说明）")

from backend.app.core.capabilities import effective_total_area  # noqa: E402
from backend.app.core.mcp_client import MCPToolError, get_mcp_client  # noqa: E402
from backend.app.services.budget import engine  # noqa: E402
from backend.app.services.render import render_plan_for  # noqa: E402

#: 与 `scripts/mcp_smoke.py` 用同一份最小户型
LAYOUT: dict[str, Any] = {
    "layout_id": "layout_mcp_test",
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5,
         "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2,
         "bbox": [440, 80, 680, 300]},
        {"name": "厨房", "type": "kitchen", "area": 7.4,
         "bbox": [130, 370, 300, 500]},
        {"name": "卫生间", "type": "bathroom", "area": 4.8,
         "bbox": [310, 370, 420, 500]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "doors": [{"position": [250, 380], "width": 0.9}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 56.9,
    "has_north_arrow": True,
    "confidence": 0.85,
}


class TestCalcBudgetServer:
    async def test_工具能被发现(self):
        """子进程起得来、工具注册得上 —— 协议层最小的一个断言。"""
        tools = await get_mcp_client().list_tools("calc_budget")
        assert [t["name"] for t in tools] == ["calc_budget"]
        assert tools[0]["description"], "工具没有 description，模型无从选择"

    async def test_结果与进程内调用逐字段相同(self):
        """
        ⚠️ **反漂移断言：壳必须是薄的。**

        MCP 路径与 A-04 走的是同一个 `engine.calculate`，所以同一份输入
        必须给出**完全相同**的一份预算。任何一处"顺手改一下"都会在这里红。
        """
        got = await get_mcp_client().call_tool(
            "calc_budget", {"layout": LAYOUT, "grade": "medium"})

        area = effective_total_area(LAYOUT)
        expected = engine.calculate(area=area, grade="medium").model_dump()

        assert got == expected, (
            "MCP 返回的预算与进程内算的不一致 —— "
            "两条路径已经开始分叉了，用户会看到两个不同的数"
        )

    async def test_金额是引擎算的不是模型编的(self):
        """金额字段全部来自规则引擎；这里顺带钉住"确实算出来了"。"""
        got = await get_mcp_client().call_tool(
            "calc_budget", {"layout": LAYOUT, "grade": "medium"})

        assert got["computed_by"] == "rule_engine" or "rule" in got["computed_by"]
        assert len(got["lines"]) >= 7, "AC-05 要求 ≥7 个分项"
        assert got["total_min"] < got["total_max"] > 0
        assert got["area"] == pytest.approx(56.9)

    async def test_档位不合法时说清允许哪些(self):
        """拒绝要说清"允许什么"，不是只说"不合法"（capabilities 的立场）。"""
        with pytest.raises(MCPToolError) as e:
            await get_mcp_client().call_tool(
                "calc_budget", {"layout": LAYOUT, "grade": "luxury"})
        assert "economy" in str(e.value)

    async def test_没有面积时拒绝而不是算出0(self):
        """
        没有面积就算不出预算 —— 必须拒绝，绝不能返回一份 ¥0 的方案
        （AC-34 的立场：不允许静默产出零值结果）。
        """
        with pytest.raises(MCPToolError) as e:
            await get_mcp_client().call_tool(
                "calc_budget", {"layout": {"rooms": []}, "grade": "medium"})
        assert "面积" in str(e.value)


class TestRenderLayoutSvgServer:
    async def test_工具能被发现(self):
        tools = await get_mcp_client().list_tools("render_layout_svg")
        assert [t["name"] for t in tools] == ["render_layout_svg"]

    async def test_SVG与进程内渲染逐字节相同(self):
        """
        反漂移断言（同上）。渲染这条尤其要守：`render_plan_for` 的注释写着
        "约定成同一个函数，比约定成'记得用同样的参数'可靠" ——
        本用例就是那句约定的**执行者**。
        """
        got = await get_mcp_client().call_tool(
            "render_layout_svg", {"layout": LAYOUT})

        _, expected = render_plan_for(LAYOUT)
        assert got["svg"] == expected.svg
        assert got["width_px"] == expected.width_px
        assert got["height_px"] == expected.height_px

    async def test_warnings与assumptions必须一起返回(self):
        """
        ⚠️ 只给图不给 warnings，等于让调用方把一张有缺口的图当成完整的。
        assumptions 同理 —— SVG 会被单独导出/分享，假设要跟着图走。
        """
        got = await get_mcp_client().call_tool(
            "render_layout_svg", {"layout": LAYOUT})

        assert "warnings" in got and isinstance(got["warnings"], list)
        assert "assumptions" in got and isinstance(got["assumptions"], list)
        assert got["room_count"] == 4
        assert got["svg"].startswith("<svg")

    async def test_空户型拒绝而不是画一张空图(self):
        with pytest.raises(MCPToolError) as e:
            await get_mcp_client().call_tool(
                "render_layout_svg", {"layout": {"rooms": []}})
        assert "rooms" in str(e.value)


class TestServersAreSiblings:
    """
    两个 Server 都必须能被 `MCPClient` 按"工具名 = 模块名"的约定拉起 ——
    `call_tool` 的 `module` 缺省就是这么推断的。改了文件名而没改工具名
    （或反过来），拉起的会是一个不存在的模块 —— 只会在调用时报错。
    """

    def test_模块名与工具名一致(self):
        import importlib

        for module, tool in (("calc_budget", "calc_budget"),
                             ("render_layout_svg", "render_layout_svg"),
                             ("parse_house_layout", "parse_house_layout")):
            mod = importlib.import_module(f"mcp_servers.{module}")
            assert hasattr(mod, "server"), f"mcp_servers.{module} 没有 server 实例"
            assert mod.server.name == tool, (
                f"mcp_servers.{module} 的 server.name 是 {mod.server.name!r}，"
                f"而按约定应当是 {tool!r}"
            )



class TestParseHouseLayoutValidation:
    """
    第三个 Server（`parse_house_layout`）的**入参校验**路径。

    ⚠️ 只测校验、不测解析 —— 后者要真的调一次视觉模型。这里守的是
    "参数不对时说人话"：它原来抛 `ValueError`/`FileNotFoundError`，
    被 SDK 当崩溃处理，调用方只看到 `Error executing tool ...`。
    同一类问题在另外两个 Server 上也出现过，所以三个一起钉住。
    """

    async def test_两个入参都不给时说清二选一(self):
        with pytest.raises(MCPToolError) as e:
            await get_mcp_client().call_tool("parse_house_layout", {})
        assert "image_path" in str(e.value) and "image_base64" in str(e.value)

    async def test_图像路径不存在时说清是哪个路径(self):
        with pytest.raises(MCPToolError) as e:
            await get_mcp_client().call_tool(
                "parse_house_layout", {"image_path": "C:/nope/没有这张图.png"})
        assert "没有这张图" in str(e.value), (
            f"错误信息里没有带上路径，调用方无从排查：{e.value}"
        )


class TestAc30TraceIdPropagation:
    """
    AC-30：「同一请求在 API 响应、审计日志、LLM 调用日志中 `trace_id` 一致」。

    ⚠️ **MCP 这一段此前是断的。** `mcp_client.py` 全文没有任何 `trace_id`
    引用 —— 主链一路带着 trace，到了工具调用这里就没了。子进程里发生的事
    在链路追踪上完全看不见。

    补法：trace_id 走**子进程环境变量**（`QY_TRACE_ID`），不塞进工具入参 ——
    塞入参会污染工具自己的 schema，而且参数是要被校验的，多一个字段就是
    多一处会因校验失败而报错的地方。
    """

    def test_trace_id_通过环境变量传给子进程(self):
        from backend.app.core.mcp_client import MCPClient

        c = MCPClient()
        env = c._server_params("calc_budget", trace_id="abc123").env or {}
        assert env.get("QY_TRACE_ID") == "abc123", (
            f"子进程环境里没有 QY_TRACE_ID：{sorted(env)[:8]}…"
        )

    def test_没有_trace_时不塞这个变量(self):
        """
        ⚠️ 空串也要**不设**，而不是设成空串。
        设成空串的话，Server 侧 `os.environ.get("QY_TRACE_ID")` 拿到的是
        `""` —— truthy 判断为假、但要判"有没有"就会判成有。
        """
        from backend.app.core.mcp_client import MCPClient

        env = MCPClient()._server_params("calc_budget").env or {}
        assert "QY_TRACE_ID" not in env

    def test_trace_id_不进工具入参(self):
        """
        **这条守的是那条取舍本身。** 一旦有人"顺手"把 trace_id 塞进
        `arguments`，工具的 schema 就得跟着改，而改漏一个 Server 的表现是
        "那个工具报参数错误" —— 与追踪毫无关系的一个故障。
        """
        from backend.app.core.mcp_client import MCPClient
        import inspect

        src = inspect.getsource(MCPClient.call_tool)
        assert "arguments[" not in src and "arguments.update" not in src, (
            "call_tool 往 arguments 里写东西了 —— trace_id 必须走环境变量"
        )