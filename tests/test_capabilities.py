"""
业务连续性守卫测试。

核心命题：**降级结果不得悄悄流向下游产生看似正常的产物。**
最典型的反例是 `calc_budget(area=0)` 返回一个 ¥0 的预算——
它不会报错，但它是垃圾，而且用户会当真。
"""

from __future__ import annotations

import pytest

from backend.app.core.capabilities import (
    OperationNotAllowedError,
    evaluate_capabilities,
)

# ── 样本 ──────────────────────────────────────────────────

FULL_LAYOUT = {
    "mode": "full",
    "rooms": [
        {"name": "客厅", "type": "living_room", "area": 28.5, "bbox": [120, 80, 420, 360]},
        {"name": "主卧", "type": "bedroom", "area": 16.2, "bbox": [440, 80, 680, 300]},
    ],
    "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
    "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
    "total_area": 89.0,
}

# 降级结果：只有房间名是真的，其余是刻意的空值
DEGRADED_LAYOUT = {
    "mode": "degraded_basic",
    "rooms": [
        {"name": "Living room", "type": "other", "area": 0.0, "bbox": [], "orientation": "unknown"},
        {"name": "Bedroom", "type": "other", "area": 0.0, "bbox": [], "orientation": "unknown"},
    ],
    "walls": [], "doors": [], "windows": [], "dimensions": [],
    "total_area": 0.0,
}


class TestFullLayout:
    def test_everything_allowed(self):
        r = evaluate_capabilities(FULL_LAYOUT)
        for op in ("view_rooms", "diagnose", "generate_plan",
                   "estimate_budget", "generate_image", "generate_hotspots", "edit_layout"):
            assert r.allows(op), f"{op} 在完整结果下应当可用"

    def test_no_missing_fields(self):
        r = evaluate_capabilities(FULL_LAYOUT)
        assert all(not c.missing for c in r.operations.values())
        assert r.mode == "full"


class TestDegradedLayout:
    """降级结果的业务边界 —— 这是本模块存在的理由。"""

    @pytest.mark.parametrize("op", ["view_rooms", "export_room_list"])
    def test_allowed_operations(self, op):
        assert evaluate_capabilities(DEGRADED_LAYOUT).allows(op)

    @pytest.mark.parametrize("op", [
        "diagnose", "generate_plan", "estimate_budget",
        "generate_image", "generate_hotspots", "edit_layout",
    ])
    def test_forbidden_operations(self, op):
        r = evaluate_capabilities(DEGRADED_LAYOUT)
        assert not r.allows(op), f"{op} 在降级结果下必须被拦截"

    def test_budget_is_blocked_not_zeroed(self):
        """
        最重要的一条：降级结果绝不允许走到预算估算。
        否则 calc_budget(area=0) 会返回一个 ¥0 的预算——
        不报错、看起来正常、但完全是垃圾。
        """
        r = evaluate_capabilities(DEGRADED_LAYOUT)
        assert not r.allows("estimate_budget")
        cap = r.operations["estimate_budget"]
        assert "套内总面积" in cap.missing
        assert cap.suggestion, "拒绝时必须给出怎么办，不能只说不行"

    def test_reason_is_actionable(self):
        r = evaluate_capabilities(DEGRADED_LAYOUT)
        cap = r.operations["generate_plan"]
        assert cap.allowed is False
        assert cap.missing, "必须说明缺哪些字段"
        assert "重新上传" in cap.suggestion

    def test_require_raises_with_payload(self):
        r = evaluate_capabilities(DEGRADED_LAYOUT)
        with pytest.raises(OperationNotAllowedError) as ei:
            r.require("generate_plan")
        payload = ei.value.to_payload()
        assert payload["operation"] == "generate_plan"
        assert payload["missing"]
        assert payload["suggestion"]

    def test_require_passes_when_allowed(self):
        r = evaluate_capabilities(DEGRADED_LAYOUT)
        assert r.require("view_rooms").allowed


class TestDataDrivenNotModeDriven:
    """
    守卫依据的是**字段里有没有数据**，不是 mode 字符串。
    这样即使某天完整模式也返回空面积（模型没估出来），拦截依然生效。
    """

    def test_full_mode_but_no_area_is_still_blocked(self):
        layout = {
            "mode": "full",
            "rooms": [{"name": "客厅", "type": "living_room", "area": 0.0, "bbox": [1, 2, 3, 4]}],
            "walls": [{"type": "load_bearing", "coords": []}],
            "windows": [],
            "total_area": 0.0,
        }
        r = evaluate_capabilities(layout)
        assert r.mode == "full"
        assert not r.allows("estimate_budget"), "mode 是 full 但没面积，照样拦"
        assert not r.allows("diagnose"), "没窗没面积，诊断也应拦"

    def test_total_area_falls_back_to_sum_of_rooms(self):
        """
        total_area 缺失但房间面积齐全时，用求和回退——这是推导，不是编造。
        """
        layout = {
            "mode": "full",
            "rooms": [
                {"name": "客厅", "area": 28.0, "bbox": [1, 2, 3, 4]},
                {"name": "主卧", "area": 16.0, "bbox": [5, 6, 7, 8]},
            ],
            "walls": [{"type": "non_load_bearing", "coords": []}],
            "windows": [{"position": [1, 1], "width": 1.0}],
            "total_area": 0.0,
        }
        assert evaluate_capabilities(layout).allows("estimate_budget")

    def test_rooms_without_bbox_block_image_generation(self):
        """有面积但没像素坐标 -> 出不了图、也算不了热区。"""
        layout = {
            "mode": "full",
            "rooms": [{"name": "客厅", "area": 28.0, "bbox": []}],
            "walls": [{"type": "x", "coords": []}],
            "windows": [{"position": [1, 1], "width": 1.0}],
            "total_area": 28.0,
        }
        r = evaluate_capabilities(layout)
        assert r.allows("generate_plan")
        assert not r.allows("generate_image")
        assert not r.allows("generate_hotspots")
        assert "房间在图中位置" in r.operations["generate_image"].missing


class TestSanity:
    def test_empty_layout_only_allows_viewing(self):
        r = evaluate_capabilities({})
        assert r.allows("view_rooms")
        assert not r.allows("generate_plan")

    def test_report_serializes(self):
        d = evaluate_capabilities(DEGRADED_LAYOUT).to_dict()
        assert d["mode"] == "degraded_basic"
        assert d["operations"]["generate_plan"]["allowed"] is False
        assert isinstance(d["operations"]["generate_plan"]["missing"], list)

    def test_attach_writes_into_payload(self):
        from backend.app.core.capabilities import attach_capabilities

        layout = dict(DEGRADED_LAYOUT)
        attach_capabilities(layout)
        assert "capabilities" in layout
        assert layout["capabilities"]["operations"]["estimate_budget"]["allowed"] is False
