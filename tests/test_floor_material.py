"""
AC-10「局部替换」—— 重定性为**矢量图上换地面材质**。

══════════════════════════════════════════════════════════════════
这条 AC 原来是什么，为什么改
══════════════════════════════════════════════════════════════════
原文：「框选地板 → 输入"深色胡桃木" → 局部重绘成功」。
它的载体是 AI 出图路径（局部重绘 = Inpainting），而那条路径已随
AC-08 作废（2026-09-23：产出的是俯视家具平面图，不是 3D 等轴测渲染图）。

2026-09-26 需求方重定性为：**在矢量图上换地面材质**。
合理性在于它和已有交付物是一套的：

  · 矢量户型图（AC-07）本来就有，而**材料价格就挂在这张图上**（AC-09/21）
  · 换材质要反映到造价上才有业务价值，而规则引擎算造价是 ADR-07 的事
  · 它可测、可复现，不依赖任何生成模型

一句话：**点一块地面 → 换一种材料 → 图上换色 + 这间房的造价跟着变。**

══════════════════════════════════════════════════════════════════
这个文件守的三件事，每件都对应一个具体的错法
══════════════════════════════════════════════════════════════════
① **"能铺在地上"是数据，不是品类名。** `QY-TL-102` 的 category 是
   `tile`，但它叫「釉面**内墙**砖」—— 只按 category 筛会把它当
   地面材料推荐出来，而那是一个**看着合理、实际错误**的建议。
   所以目录里补了 `surface` 字段，筛选看它。
② **造价必须是区间，不是一个数。** 单价本身就是区间（`price_range`），
   取中位数会变成一个"看起来很确定"的数字，而它其实不确定。
③ **落库的键是字符串，读回来必须是 int。** JSON 的键只能是字符串，
   而房间下标是 int —— 转错的话 `floor_fills[idx]` 永远取不到值，
   表现是"换了材质但图上没变化"，**不报错**。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from backend.app.api import routes
from backend.app.api import store as layout_store
from backend.app.api.schemas import ApiError, FloorMaterialRequest
from backend.app.services.material import catalog
from backend.app.services.render import render_plan_for

FIXTURE = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "golden_layout.json"
LID = "layout_for_floortest"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def layout():
    """把固定户型放进 store 的内存层，用例之间互不影响。"""
    lay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    layout_store._memory[LID] = lay
    yield lay
    layout_store.clear()


# ══════════════════════════════════════════════════════════════════
# 可选材料清单
# ══════════════════════════════════════════════════════════════════


class TestEligibleMaterials:
    def test_内墙砖不能当地面材料(self):
        """
        ⚠️ **这条守的是①。** `QY-TL-102` = 「全友 釉面内墙砖 素白」，
        它的 `category` 是 `tile` —— 只按品类筛会把它推荐成地面材料。

        判据因此是 `surface`（能铺在哪儿），而不是品类名。
        """
        ids = {o["id"] for o in routes._floor_material_options()}
        assert "QY-TL-102" not in ids, (
            "「釉面内墙砖」出现在地面材料清单里 —— "
            "筛的应该是 `surface`，不是只筛 category"
        )

    def test_只收地面与瓷砖两个品类(self):
        """灯具按「元/套」、室内门按「元/樘」—— 铺不到地上。"""
        for o in routes._floor_material_options():
            p = catalog.by_id()[o["id"]]
            assert p.category in routes.FLOOR_CATEGORIES
            assert p.surface in ("floor", "both")

    def test_每种都带代表色(self):
        """没有 `swatch` 就画不出"换成这个材料了"。"""
        for o in routes._floor_material_options():
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", o["swatch"] or ""), (
                f"{o['id']} 没有可用的 swatch：{o['swatch']!r}"
            )

    def test_清单带价格区间与全友标记(self):
        """前端要显示价格、要标「全友」徽标 —— 都在后端给的清单里。"""
        opts = routes._floor_material_options()
        assert len(opts) >= 5
        for o in opts:
            assert len(o["price_range"]) == 2
            assert o["price_range"][0] <= o["price_range"][1]
            assert isinstance(o["is_quanyou"], bool)


# ══════════════════════════════════════════════════════════════════
# 替换与还原
# ══════════════════════════════════════════════════════════════════


class TestSubstitution:
    def test_换一间房的地面(self, layout):
        d = _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201"))).data
        assert len(d["substitutions"]) == 1
        s = d["substitutions"][0]
        assert s["room_index"] == 0
        assert s["material"]["id"] == "DS-FL-201"
        assert s["area_m2"] > 0
        assert s["room_name"], "没带房间名 —— 界面上只能说'第 0 间房'"

    def test_造价是区间乘以面积(self, layout):
        """
        ⚠️ **这条守的是②。** 造价 = 面积 × 单价区间，两端都要给。

        取中位数会变成一个"看起来很确定"的数 —— 而单价本身是区间，
        这是演示目录的性质，不是可以抹掉的细节。
        """
        d = _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201"))).data
        s = d["substitutions"][0]
        lo, hi = s["material"]["price_range"]
        area = s["area_m2"]
        assert s["cost"]["min"] == pytest.approx(area * lo, abs=0.02)
        assert s["cost"]["max"] == pytest.approx(area * hi, abs=0.02)
        assert s["cost"]["min"] < s["cost"]["max"], "单价有区间，造价也必须有"
        # ⚠️ **单位是「元」不是「元/㎡」。** 实测写错过一次：把 `cost.unit`
        #    填成了材料的 `unit_hint`（元/㎡），于是界面上显示
        #    「地面造价 3817.69–5667.09 元/㎡」—— 而那是 13.21㎡ 的**总价**。
        #    一个把总额标成单价的界面在说谎。单价区间在 `material.price_range`。
        assert s["cost"]["unit"] == "元", (
            f"造价的单位是 {s['cost']['unit']!r} —— 它是总额（已乘面积），"
            f"单位只能是「元」。单价在 material.price_range 里"
        )
        assert s["material"]["price_range"], "单价区间不见了 —— 单价和总价要各归各位"

    def test_还原成默认配色(self, layout):
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        d = _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id=""))).data
        assert d["substitutions"] == []
        assert _run(routes._floor_fills(LID)) == {}, (
            "还原之后渲染层还拿着颜色 —— 图上不会变回去"
        )

    def test_多间房各自独立(self, layout):
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        d = _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=3, material_id="QY-TL-101"))).data
        got = {s["room_index"]: s["material"]["id"] for s in d["substitutions"]}
        assert got == {0: "DS-FL-201", 3: "QY-TL-101"}


# ══════════════════════════════════════════════════════════════════
# 图上真的换色了吗
# ══════════════════════════════════════════════════════════════════


class TestSvgReflects:
    def test_换过的房间在图上用的是材料代表色(self, layout):
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        fills = _run(routes._floor_fills(LID))
        _scene, plan = render_plan_for(layout, floor_fills=fills)

        m = re.search(r'<polygon id="room-0"[^>]*>', plan.svg)
        assert m, "SVG 里找不到 room-0"
        tag = m.group(0)
        assert "room-replaced" in tag, "换过的房间没有打标记"
        assert "#B08A5E" in tag, f"没有用材料的代表色：{tag[:160]}"

    def test_没换的房间不受影响(self, layout):
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        fills = _run(routes._floor_fills(LID))
        _scene, plan = render_plan_for(layout, floor_fills=fills)
        m = re.search(r'<polygon id="room-1"[^>]*>', plan.svg)
        assert "room-replaced" not in m.group(0)
        assert plan.svg.count("room-replaced") == 1

    def test_没有替换时SVG与从前逐字节相同(self, layout):
        """
        ⚠️ **加了 `floor_fills` 参数不能改变默认渲染。**
        AC-32 的黄金路径比对的是 SVG 的字节 —— 默认路径上多一个空格
        都会让一致性掉下去。
        """
        a = render_plan_for(layout)[1].svg
        b = render_plan_for(layout, floor_fills=None)[1].svg
        c = render_plan_for(layout, floor_fills={})[1].svg
        assert a == b == c
        assert "room-replaced" not in a

    def test_材料下架时跳过而不是整张图画不出来(self, layout, monkeypatch):
        """
        ⚠️ 演示目录改过、某件材料被删掉时，那间房退回默认配色 ——
        **而不是让整张矢量图渲染失败**。一张画不出来的户型图，
        比"颜色没生效"严重得多。
        """
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        monkeypatch.setattr(catalog, "by_id", lambda path=None: {})
        fills = _run(routes._floor_fills(LID))
        assert fills == {}
        _scene, plan = render_plan_for(layout, floor_fills=fills)
        assert plan.svg and "room-replaced" not in plan.svg

    def test_材料下架时清单里如实说(self, layout, monkeypatch):
        """不是"假装这间房没换过"，而是说清材料没了。"""
        _run(routes.set_floor_material(
            LID, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        monkeypatch.setattr(catalog, "by_id", lambda path=None: {})
        d = _run(routes.floor_materials(LID)).data
        s = d["substitutions"][0]
        assert s["material"] is None
        assert "不在目录里" in s["note"]


# ══════════════════════════════════════════════════════════════════
# 错误路径
# ══════════════════════════════════════════════════════════════════


class TestErrors:
    def test_房间不存在时报错并列出可选项(self, layout):
        """
        ⚠️ 校验顺序是**先房间、再材料**。反过来的话，一个不存在的房间
        会拿到"材料不存在"，而排查方向整个偏掉。
        """
        with pytest.raises(ApiError) as e:
            _run(routes.set_floor_material(
                LID, FloorMaterialRequest(room_index=99, material_id="DS-FL-201")))
        assert e.value.code == 4001
        assert "99" in str(e.value)
        assert "可选的是" in str(e.value), "没告诉用户可选的房间号是多少"

    def test_内墙砖被拒且说清原因(self, layout):
        with pytest.raises(ApiError) as e:
            _run(routes.set_floor_material(
                LID, FloorMaterialRequest(room_index=0, material_id="QY-TL-102")))
        assert e.value.code == 4001
        assert "QY-TL-102" in str(e.value)

    def test_户型不存在时404(self):
        layout_store.clear()
        with pytest.raises(ApiError) as e:
            _run(routes.floor_materials("layout_不存在"))
        assert e.value.code == 4004

    def test_不存在的材料被拒(self, layout):
        with pytest.raises(ApiError) as e:
            _run(routes.set_floor_material(
                LID, FloorMaterialRequest(room_index=0, material_id="不存在")))
        assert e.value.code == 4001


# ══════════════════════════════════════════════════════════════════
# 热区带的房间号（AC-10 靠它把"点中的地面"映射到房间）
# ══════════════════════════════════════════════════════════════════


class TestHotspotRoomIndex:
    def test_地面热区带得出房间下标(self):
        from backend.app.services.render import hotspot_payload

        _scene, plan = render_plan_for(
            json.loads(FIXTURE.read_text(encoding="utf-8")))
        payload = hotspot_payload(_scene, plan.projection)
        floors = [h for h in payload["hotspots"] if h["category"] == "floor"]
        assert floors, "一个地面热区都没有"
        for h in floors:
            assert h["room_index"] is not None, (
                f"{h['label']} 没有 room_index —— 前端点中它之后不知道该换哪间房"
            )
        # 房间下标必须唯一（同一间房两块地面热区会让替换变得有歧义）
        idx = [h["room_index"] for h in floors]
        assert len(idx) == len(set(idx))

    def test_门热区没有房间下标(self):
        """门在两间房之间，不属于任何一间 —— 给 None 而不是硬塞一个。"""
        from backend.app.services.render import hotspot_payload

        _scene, plan = render_plan_for(
            json.loads(FIXTURE.read_text(encoding="utf-8")))
        payload = hotspot_payload(_scene, plan.projection)
        for h in payload["hotspots"]:
            if h["category"] == "door":
                assert h["room_index"] is None

    def test_热区里的房间下标与SVG的多边形对得上(self):
        """
        ⚠️ 两处**各自独立**地数房间：热区层用 `enumerate(scene.rooms)`，
        SVG 用 `enumerate(scene.rooms)` —— 同一个顺序，但这是**约定**
        而不是结构。约定会漂移，所以这里交叉验一次：
        地面热区的 bbox 必须落在同一个 id 的房间多边形里。
        """
        from backend.app.services.render import hotspot_payload

        _scene, plan = render_plan_for(
            json.loads(FIXTURE.read_text(encoding="utf-8")))
        payload = hotspot_payload(_scene, plan.projection)
        svg = plan.svg
        for h in payload["hotspots"]:
            if h["category"] != "floor":
                continue
            m = re.search(rf'<polygon id="room-{h["room_index"]}"[^>]*points="([^"]+)"', svg)
            assert m, f"SVG 里没有 room-{h['room_index']}"
            pts = [tuple(map(float, p.split(","))) for p in m.group(1).split()]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx = (h["bbox"][0] + h["bbox"][2]) / 2
            cy = (h["bbox"][1] + h["bbox"][3]) / 2
            assert min(xs) <= cx <= max(xs), (
                f"{h['label']} 的中心 x={cx:.1f} 不在 room-{h['room_index']} 的范围内"
                f"（[{min(xs):.1f},{max(xs):.1f}]）—— 两处数房间的顺序分叉了"
            )
            assert min(ys) <= cy <= max(ys)
