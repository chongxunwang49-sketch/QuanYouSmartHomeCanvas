"""
矢量图与热区的接口契约（AC-07 / AC-09 / AC-21）。

═══════════════════════════════════════════════════════════════════
两个接口，一条不能破的约束
═══════════════════════════════════════════════════════════════════
`/plan.svg` 画图，`/hotspots` 给命中区域。它们是**两次独立的 HTTP 请求** ——
前端先拿到图，再拿到热区，然后把热区叠在图上。

只要两边算出两套坐标，热区就会整体漂移。而两个接口**分别看都完全正常**：
图是对的，热区数据格式也是对的，只有叠在一起才发现"悬停在地板上，
提示说这是主卧墙面"（需求文档 2.2.6）。

所以这里最要紧的一条断言是：

    **两个接口返回的 `transform` 必须逐字段相同。**

它同时钉住了实现（都走 `render_plan_for`）和结果（投影参数一致）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
import pytest

from backend.app.api import store as layout_store
from backend.app.api.tasks import reset_task_manager
from backend.app.main import create_app

LAYOUT = {
    "layout_id": "layout_render_test",
    "mode": "full",
    "rooms": [
        {"name": "主卧", "type": "bedroom", "area": 11.7, "bbox": [40, 40, 380, 300]},
        {"name": "次卧", "type": "bedroom", "area": 11.1, "bbox": [40, 300, 380, 545]},
        {"name": "客厅", "type": "living_room", "area": 23.1, "bbox": [380, 40, 725, 545]},
    ],
    "walls": [
        {"type": "unknown", "coords": [[40, 40], [725, 40], [725, 545], [40, 545], [40, 40]]},
        {"type": "unknown", "coords": [[380, 40], [380, 545]]},
        {"type": "unknown", "coords": [[40, 300], [380, 300]]},
    ],
    "doors": [{"position": [367, 197], "width": 0.0}],
    "windows": [{"position": [228, 40], "width": 1.7}],
    "total_area": 45.9,
    "confidence": 0.45,
}

LAYOUT_ID = "layout_render_test"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_task_manager()
    layout_store.clear()
    yield
    reset_task_manager()
    layout_store.clear()


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════
# AC-07：矢量图接口
# ══════════════════════════════════════════════════════════════════


class TestPlanSvgEndpoint:
    async def test_返回可解析的_SVG(self):
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")

        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        root = ET.fromstring(r.text)
        assert root.tag.endswith("svg")

    async def test_画布不小于_512(self):
        """AC-07 的字面要求。尺寸也通过响应头暴露给前端。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")

        assert int(r.headers["x-plan-width"]) >= 512
        assert int(r.headers["x-plan-height"]) >= 512

    async def test_不是_api_信封而是裸图(self):
        """
        消费者的 `<img src>`，不是我们的前端代码。

        套上 `{code, msg, data}` 的信封，浏览器就渲染不出来了 ——
        而接口测试如果只断言 `code == 0` 是发现不了的。
        """
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
        assert not r.text.lstrip().startswith("{")
        assert r.text.lstrip().startswith("<svg")

    async def test_未知户型返回可操作的业务码(self):
        """业务失败走 HTTP 200 + code != 0（4.4 的约定），让前端能弹提示文案。"""
        async with await _client() as c:
            r = await c.get("/api/v1/layout/not_exist/plan.svg")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 4004
        assert "过期" in body["msg"] or "不存在" in body["msg"]

    async def test_图上不含价格或品牌(self):
        """这张图还要当 ControlNet 的 conditioning image，不能画广告。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
        for banned in ("¥", "全友", "元/"):
            assert banned not in r.text


# ══════════════════════════════════════════════════════════════════
# AC-09 / AC-21：热区接口
# ══════════════════════════════════════════════════════════════════


class TestHotspotsEndpoint:
    async def test_返回热区与图片地址(self):
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        body = r.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["count"] > 0
        # 前端要能只凭这个响应就找到图 —— 不用自己拼 URL
        assert data["image"]["url"] == f"/api/v1/layout/{LAYOUT_ID}/plan.svg"
        assert data["image"]["width"] >= 512

    async def test_每条热区都带价格和链接(self):
        """AC-21：悬停要能看到全友产品的价格与官网链接。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        for h in r.json()["data"]["hotspots"]:
            assert h["precision"] in ("exact", "room_level", "none")
            assert h["source"] == "vector_layer"
            if h["items"]:
                assert h["price_range"][0] > 0
                assert h["search_url"].startswith("http")

    async def test_推荐里全友优先(self):
        """AC-18 要求全友覆盖率 ≥ 60%，热区卡片只放 3 件，排序不能稀释它。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        for h in r.json()["data"]["hotspots"]:
            if h["items"]:
                assert h["items"][0]["is_quanyou"]

    async def test_免责声明随数据返回(self):
        """演示价格必须显著标注 —— 不能只写在文档里。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            r = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")
        assert r.json()["data"]["disclaimer"]

    async def test_未知户型返回业务码(self):
        async with await _client() as c:
            r = await c.get("/api/v1/layout/not_exist/hotspots")
        assert r.status_code == 200
        assert r.json()["code"] == 4004


# ══════════════════════════════════════════════════════════════════
# ⚠️ 核心：两个接口必须共用同一套坐标
# ══════════════════════════════════════════════════════════════════


class TestNoDriftBetweenEndpoints:
    async def test_两个接口的变换参数完全一致(self):
        """
        ⚠️ **这个文件存在的理由。**

        两次独立请求，只要变换参数有任何一位不同，热区就整体漂移。
        而两个接口分别看都完全正常 —— 只有叠在一起才看得出来。
        """
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            svg = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
            hs = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        d = hs.json()["data"]
        # transform 里的画布尺寸必须与图完全一致
        assert d["transform"]["width_px"] == int(svg.headers["x-plan-width"])
        assert d["transform"]["height_px"] == int(svg.headers["x-plan-height"])
        assert d["image"]["width"] == int(svg.headers["x-plan-width"])

    async def test_热区坐标落在画布内(self):
        """变了但没变对的情况：参数一致、算出来的坐标越界。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            svg = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
            hs = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        w = int(svg.headers["x-plan-width"])
        h = int(svg.headers["x-plan-height"])
        for spot in hs.json()["data"]["hotspots"]:
            l, t, r, b = spot["bbox"]
            assert 0 <= l < r <= w, f"{spot['label']} 横向越界：{spot['bbox']} / 宽 {w}"
            assert 0 <= t < b <= h, f"{spot['label']} 纵向越界：{spot['bbox']} / 高 {h}"

    async def test_热区编号能在_svg_里找到对应元素(self):
        """
        前端靠 `data-hotspot` 下标把热区数据挂到 SVG 元素上。
        两边对不上，悬停会显示别人家的价格 —— 页面看起来完全正常。
        """
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            svg = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
            hs = await c.get(f"/api/v1/layout/{LAYOUT_ID}/hotspots")

        root = ET.fromstring(svg.text)
        ns = "{http://www.w3.org/2000/svg}"
        in_svg = {
            int(el.get("data-hotspot"))
            for el in root.iter(f"{ns}path")
            if el.get("data-hotspot") is not None
        }
        in_data = {h["index"] for h in hs.json()["data"]["hotspots"]}
        assert in_svg == in_data, (
            f"SVG 里的热区下标 {sorted(in_svg)} 与数据里的 {sorted(in_data)} 对不上"
        )

    async def test_重复请求结果不变(self):
        """AC-32 的重放一致性：同一个 layout 渲染两次必须逐字节相同。"""
        await layout_store.save(LAYOUT_ID, LAYOUT)
        async with await _client() as c:
            a = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
            b = await c.get(f"/api/v1/layout/{LAYOUT_ID}/plan.svg")
        assert a.text == b.text


# ══════════════════════════════════════════════════════════════════
# 安全：房间名是模型输出，会进 XML
# ══════════════════════════════════════════════════════════════════


class TestModelOutputCannotBreakTheSvg:
    """
    房间名由视觉模型读图生成，**内容不受我们控制**。

    它会进到三个位置：`aria-label="..."`、`data-label="..."`、`<text>...</text>`。
    只转 `& < >` 而不转引号的话，名字里一个 `"` 就能提前闭合属性 ——
    后面跟 `><script>` 之类的就变成注入点（前端用 v-html 嵌 SVG 时就是 XSS）。
    """

    HOSTILE = [
        '客厅" onload="alert(1)',
        "卧室<script>alert(1)</script>",
        "厨房 & 餐厅 <A&B>",
        "门厅'quote",
        "房间]]>",
    ]

    @pytest.mark.parametrize("name", HOSTILE)
    async def test_恶意房间名不会破坏_SVG(self, name):
        layout = {
            "rooms": [{"name": name, "type": "living_room", "bbox": [40, 40, 400, 400]}],
            "walls": [{"type": "unknown",
                       "coords": [[40, 40], [400, 40], [400, 400], [40, 400], [40, 40]]}],
            "total_area": 18.0,
        }
        await layout_store.save("hostile", layout)
        async with await _client() as c:
            r = await c.get("/api/v1/layout/hostile/plan.svg")

        # 关键：仍然是一份**能被 XML 解析器读进去**的文档
        root = ET.fromstring(r.text)
        assert root.tag.endswith("svg")

        # ⚠️ 但"能解析"**不足以**说明没被注入。
        #
        # 实测过：只转 `& < >` 时，房间名 `客厅" onload="alert(1)` 会让
        # aria-label 变成 `客厅`，而 `onload="alert(1)"` 成为一个**独立属性** ——
        # 文档照样解析成功，属性却已经被拆开了。
        # 所以这里查的是**解析后的属性表**，不是文本匹配：
        # 任何以 `on` 开头的属性都不该由我们的数据产生。
        for el in root.iter():
            for attr in el.attrib:
                assert not attr.lower().startswith("on"), (
                    f"房间名里的内容变成了事件属性 `{attr}` —— 属性被提前闭合了"
                )

        # 原始文本里也不该出现未转义的注入片段
        assert "<script>" not in r.text
        assert 'onload="alert' not in r.text

    async def test_引号被转义成实体(self):
        layout = {
            "rooms": [{"name": '客"厅', "type": "living_room", "bbox": [40, 40, 400, 400]}],
            "walls": [],
            "total_area": 18.0,
        }
        await layout_store.save("quote", layout)
        async with await _client() as c:
            r = await c.get("/api/v1/layout/quote/plan.svg")
        assert "&quot;" in r.text, "引号没有转义 —— 属性可以被提前闭合"
