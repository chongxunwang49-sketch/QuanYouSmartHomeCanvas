"""
A-01 LayoutParserAgent 测试。

分两层：
- 单元测试：不联网，用假 LLM 验证解析、校验、降级、熔断逻辑。
- 集成测试（@pytest.mark.integration）：真实调用 DeepSeek，需配置 DEEPSEEK_API_KEY。

运行：
    pytest tests/test_layout_parser.py -v                    # 只跑单元
    pytest tests/test_layout_parser.py -v -m integration     # 含真实调用
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.agents.base import BaseAgent
from backend.app.agents.layout_parser import LayoutParserAgent
from backend.app.core.llm_client import (
    LLMError,
    LLMResult,
    LLMTruncatedError,
    build_schema_instruction,
    extract_json,
)
from backend.app.graph.state import HomeDecoState, initial_state
from backend.app.schemas.layout import LayoutSchema

# ══════════════════════════════════════════════════════════════════
# extract_json —— 模型输出千奇百怪，抽取必须稳
# ══════════════════════════════════════════════════════════════════


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fence_without_lang(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around(self):
        text = '好的，这是解析结果：\n{"a": 1}\n希望有帮助。'
        assert extract_json(text) == {"a": 1}

    def test_nested_braces(self):
        text = '结果如下 {"a": {"b": [1, 2]}, "c": "}"} 完毕'
        assert extract_json(text) == {"a": {"b": [1, 2]}, "c": "}"}

    def test_string_containing_brace(self):
        """字符串里的花括号不能干扰括号配平。"""
        assert extract_json('{"note": "含 { 和 } 的说明"}') == {"note": "含 { 和 } 的说明"}

    def test_top_level_array(self):
        assert extract_json('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            extract_json("")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            extract_json("完全没有 JSON 的一段话")


# ══════════════════════════════════════════════════════════════════
# Schema 提示词生成
# ══════════════════════════════════════════════════════════════════


class TestSchemaInstruction:
    def test_contains_field_names_and_descriptions(self):
        hint = build_schema_instruction(LayoutSchema)
        assert "rooms" in hint
        assert "confidence" in hint
        assert "uncertain_points" in hint
        # 描述文本应被带出，否则模型不知道字段含义
        assert "置信度" in hint

    def test_demands_json_only(self):
        hint = build_schema_instruction(LayoutSchema)
        assert "只能" in hint and "JSON" in hint


# ══════════════════════════════════════════════════════════════════
# Schema 校验
# ══════════════════════════════════════════════════════════════════


class TestLayoutSchema:
    def test_minimal_valid(self):
        s = LayoutSchema.model_validate({"confidence": 0.9})
        assert s.rooms == [] and s.total_area == 0.0

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(Exception):
            LayoutSchema.model_validate({"confidence": 1.5})

    def test_invalid_room_type_rejected(self):
        with pytest.raises(Exception):
            LayoutSchema.model_validate({
                "confidence": 0.9,
                "rooms": [{"name": "客厅", "type": "不存在的类型"}],
            })

    def test_full_roundtrip(self):
        raw = {
            "rooms": [{"name": "客厅", "type": "living_room", "area": 28.5,
                       "bbox": [120, 80, 420, 360], "orientation": "south"}],
            "walls": [{"type": "load_bearing", "coords": [[100, 60], [700, 60]]}],
            "doors": [{"position": [250, 380], "width": 0.9, "swing": "inward"}],
            "windows": [{"position": [600, 50], "width": 1.8, "orientation": "south"}],
            "dimensions": [{"label": "4200", "value": 4.2, "unit": "m"}],
            "total_area": 89.0, "has_north_arrow": True,
            "entrance_orientation": "north", "confidence": 0.87,
        }
        s = LayoutSchema.model_validate(raw)
        assert s.rooms[0].area == 28.5
        assert s.walls[0].type == "load_bearing"
        assert s.model_dump()["windows"][0]["width"] == 1.8


# ══════════════════════════════════════════════════════════════════
# Agent 行为
# ══════════════════════════════════════════════════════════════════


class _FakeLLM:
    """假 LLM：按脚本返回，用于验证 Agent 的分支逻辑。"""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or []
        self.raises = raises
        self.calls: list[dict] = []

    async def complete_json(self, schema, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        payload = self.responses.pop(0)
        return schema.model_validate(payload), LLMResult(
            text=json.dumps(payload), provider="fake", model_used="fake-model",
            prompt_tokens=100, completion_tokens=50, reasoning_tokens=40, elapsed_ms=123,
        )

    async def complete(self, **kwargs):  # pragma: no cover
        raise AssertionError("本测试不应走纯文本路径")


def _state(**kw) -> HomeDecoState:
    return initial_state(
        task_id="t1",
        image_ref="data:image/png;base64,AAAA",
        **kw,
    )


class TestLayoutParserAgent:
    def test_happy_path(self):
        agent = LayoutParserAgent(llm=_FakeLLM([{
            "rooms": [{"name": "客厅", "type": "living_room", "area": 28.5}],
            "total_area": 89.0, "has_north_arrow": True, "confidence": 0.87,
        }]))
        out = asyncio.run(agent.execute(_state()))

        assert out["layout_id"].startswith("layout_")
        assert out["layout"]["rooms"][0]["name"] == "客厅"
        assert out["layout"]["model_used"] == "fake-model"
        assert out["degraded"] is False
        assert out["trace"][0]["ok"] is True

    def test_confidence_low_produces_warning(self):
        agent = LayoutParserAgent(llm=_FakeLLM([{
            "confidence": 0.3, "has_north_arrow": False,
        }]))
        out = asyncio.run(agent.execute(_state()))
        warnings = out["layout"]["warnings"]
        assert any("置信度偏低" in w for w in warnings)
        assert any("指北针" in w for w in warnings)

    def test_no_rooms_produces_warning(self):
        agent = LayoutParserAgent(llm=_FakeLLM([{"confidence": 0.9, "rooms": []}]))
        out = asyncio.run(agent.execute(_state()))
        assert any("未识别出任何房间" in w for w in out["layout"]["warnings"])

    def test_safety_notice_always_present(self):
        """安全提示是强制项：识别到承重墙与完全没识别到，都必须给出提示。"""
        with_wall = LayoutParserAgent(llm=_FakeLLM([{
            "confidence": 0.9,
            "walls": [{"type": "load_bearing", "coords": [[0, 0], [10, 10]]}],
        }]))
        out = asyncio.run(with_wall.execute(_state()))
        assert "承重墙" in out["layout"]["safety_notice"]
        assert "现场复核" in out["layout"]["safety_notice"]

        without = LayoutParserAgent(llm=_FakeLLM([{"confidence": 0.9}]))
        out2 = asyncio.run(without.execute(_state()))
        assert out2["layout"]["safety_notice"]

    def test_missing_image_isolated_not_raised(self):
        """缺少图像时应被熔断捕获，而不是把异常抛给调用方。"""
        agent = LayoutParserAgent(llm=_FakeLLM([{"confidence": 0.9}]))
        out = asyncio.run(agent.execute(initial_state(task_id="t")))
        assert out["degraded"] is True
        assert out["errors"][0]["agent"] == "A-01"
        assert out["trace"][0]["ok"] is False

    def test_llm_failure_isolated(self):
        agent = LayoutParserAgent(llm=_FakeLLM(raises=LLMError("所有提供方均失败")))
        out = asyncio.run(agent.execute(_state()))
        assert out["degraded"] is True
        assert "LLM 调用失败" in out["errors"][0]["message"]
        # 关键：图还能继续走
        assert out["trace"][0]["ok"] is False

    def test_timeout_is_circuit_broken(self):
        """用一个故意睡过熔断阈值的假 Agent 验证超时隔离。"""

        class _Slow(BaseAgent):
            code, name = "A-99", "SlowAgent"
            timeout = 0.2

            async def run(self, state):
                await asyncio.sleep(5)
                return {}

        out = asyncio.run(_Slow().execute(_state()))
        assert out["degraded"] is True
        assert out["errors"][0]["type"] == "timeout"
        assert out["trace"][0]["ok"] is False
        assert out["trace"][0]["elapsed_ms"] < 2000

    def test_degraded_flag_propagates_from_llm(self):
        """LLM 降级必须透传到状态，供 AC-17 校验。"""

        class _DegradedLLM(_FakeLLM):
            async def complete_json(self, schema, **kwargs):
                payload = self.responses.pop(0)
                return schema.model_validate(payload), LLMResult(
                    text=json.dumps(payload), provider="ollama", model_used="minicpm-v4.6",
                    degraded=True, degrade_reason="deepseek: HTTP 429",
                )

        agent = LayoutParserAgent(llm=_DegradedLLM([{"confidence": 0.6}]))
        out = asyncio.run(agent.execute(_state()))
        assert out["degraded"] is True
        assert out["degrade_reasons"] == ["deepseek: HTTP 429"]

    def test_detail_level_basic_shortens_prompt(self):
        fake = _FakeLLM([{"confidence": 0.9}])
        agent = LayoutParserAgent(llm=fake)
        asyncio.run(agent.execute(_state(detail_level="basic")))
        assert "只需给出房间列表" in fake.calls[0]["user"]

    def test_prefer_local_routes_to_degraded_path(self):
        """prefer_local 不做云端全量解析，转而走本地降级路径（只出房间名）。"""
        fake = _FakeLLM([{"rooms": [{"name": "客厅"}, {"name": "卧室"}], "confidence": 0.3}])
        agent = LayoutParserAgent(llm=fake)
        out = asyncio.run(agent.execute(_state(prefer_local=True)))

        assert out["layout"]["mode"] == "degraded_basic"
        assert out["degraded"] is True
        assert "本地处理模式" in out["degrade_reasons"][0]
        # 只调用了一次，且用的是降级 Schema；未尝试云端全量解析
        assert len(fake.calls) == 1

    def test_degraded_result_has_no_invented_structure(self):
        """降级结果的所有结构字段必须为空——不得由小模型推断。"""
        fake = _FakeLLM([{"rooms": [{"name": "客厅"}, {"name": "卧室"}], "confidence": 0.3}])
        out = asyncio.run(LayoutParserAgent(llm=fake).execute(_state(prefer_local=True)))
        L = out["layout"]

        assert L["walls"] == []
        assert L["doors"] == []
        assert L["windows"] == []
        assert L["dimensions"] == []
        assert L["total_area"] == 0.0
        assert L["entrance_orientation"] == "unknown"
        assert all(r["type"] == "other" and r["area"] == 0.0 and r["bbox"] == []
                   for r in L["rooms"])
        assert "无法判断承重墙" in L["safety_notice"]

    def test_quality_gate_rejects_empty_degraded(self):
        """闸门：本地模型一个房间都没读出来 -> 整条链失败，不返回空壳。"""
        fake = _FakeLLM([{"rooms": [], "confidence": 0.2}])
        out = asyncio.run(LayoutParserAgent(llm=fake).execute(_state(prefer_local=True)))
        assert out["trace"][0]["ok"] is False
        assert "质量闸门" in out["errors"][0]["message"]

    def test_quality_gate_rejects_too_few_rooms(self):
        """闸门：只读出 1 个房间，信息量不足，同样拒绝。"""
        fake = _FakeLLM([{"rooms": [{"name": "客厅"}], "confidence": 0.3}])
        out = asyncio.run(LayoutParserAgent(llm=fake).execute(_state(prefer_local=True)))
        assert out["trace"][0]["ok"] is False
        assert "信息量不足" in out["errors"][0]["message"]

    def test_full_parse_forbids_auto_degrade(self):
        """
        核心约束：主模型失败时，**不得**让本地小模型拿完整 Schema 硬编。
        实测 minicpm-v4.6 会产出越界 bbox + 编造墙体 + 坏 JSON，
        那样得到的是"看起来像结果"的垃圾，比直接失败危险得多。
        """
        seen: list[bool] = []

        class _Spy(_FakeLLM):
            async def complete_json(self, schema, **kw):
                seen.append(kw.get("allow_degrade", True))
                if len(seen) == 1:
                    raise LLMError("主模型 HTTP 500")
                return await super().complete_json(schema, **kw)

        agent = LayoutParserAgent(llm=_Spy([
            {"rooms": [{"name": "客厅"}, {"name": "卧室"}], "confidence": 0.3},
        ]))
        out = asyncio.run(agent.execute(_state()))

        assert seen[0] is False, "全量解析必须禁止自动降级"
        assert out["layout"]["mode"] == "degraded_basic"
        assert "云端主模型当前不可用" in out["degrade_reasons"][0]


# ══════════════════════════════════════════════════════════════════
# 降级链与隐私保证
#
# 这一组测试针对一个真实踩过的坑：prefer_local 最初只改了提示词，
# 没改提供方，结果户型图照样发到了云端。Agent 层的 Fake 客户端看不见
# provider，所以必须在 LLMClient 层直接断言「谁会收到这个请求」。
# ══════════════════════════════════════════════════════════════════


# 说明：提供方选择（隐私 / 降级）的测试已移至 tests/test_llm_client.py。
# 那些断言必须打在 LLMClient 层——Agent 层的 Fake 客户端看不见 provider。


# ══════════════════════════════════════════════════════════════════
# 工作流
# ══════════════════════════════════════════════════════════════════


class TestWorkflow:
    def test_graph_compiles_and_runs(self, monkeypatch):
        """编译图结构并跑一次（用假 LLM 替换真实 Agent）。"""
        from backend.app.graph import workflow

        fake = _FakeLLM([{
            "rooms": [{"name": "客厅", "type": "living_room", "area": 28.5}],
            "confidence": 0.9, "total_area": 90.0,
        }])
        monkeypatch.setitem(workflow._AGENTS, "parse_layout", LayoutParserAgent(llm=fake))

        graph = workflow.build_graph(with_checkpointer=False)
        # ⚠️ 必须用 ainvoke：LangGraph 的节点级 timeout 仅对异步节点生效，
        #    同步 invoke() 会抛 "Node timeouts are only supported for async nodes"。
        #    生产路径（FastAPI）本就是全异步，此处保持一致。
        out = asyncio.run(graph.ainvoke(_state()))

        assert out["layout"]["rooms"][0]["name"] == "客厅"
        assert out["layout_id"].startswith("layout_")
        assert len(out["trace"]) == 1
        assert out["trace"][0]["agent"] == "A-01"


# ══════════════════════════════════════════════════════════════════
# 集成测试（真实调用，需 DEEPSEEK_API_KEY）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestRealLLM:
    def test_real_vision_parse(self, tmp_path):
        """真实户型图 -> DeepSeek -> 结构化结果。这是 M0 的核心验收。"""
        from PIL import Image, ImageDraw

        from backend.app.core.config import settings

        if not settings.DEEPSEEK_API_KEY:
            pytest.skip("未配置 DEEPSEEK_API_KEY")

        img = Image.new("RGB", (600, 480), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 300, 260], outline="black", width=6)   # 客厅
        d.rectangle([330, 40, 560, 260], outline="black", width=6)  # 卧室
        d.rectangle([40, 290, 560, 440], outline="black", width=4)  # 阳台
        d.text((140, 140), "LIVING 4200x3600", fill="black")
        d.text((400, 140), "BEDROOM", fill="black")
        path = tmp_path / "plan.png"
        img.save(path)

        from backend.app.core.llm_client import ImagePart

        agent = LayoutParserAgent()
        state = initial_state(
            task_id="it-1",
            image_ref="data:image/png;base64,"
            + __import__("base64").b64encode(path.read_bytes()).decode(),
        )
        out = asyncio.run(agent.execute(state))

        assert out["trace"][0]["ok"] is True, f"失败详情: {out.get('errors')}"
        layout = out["layout"]
        assert layout["rooms"], "至少应识别出一个房间"
        assert 0.0 <= layout["confidence"] <= 1.0
        assert layout["safety_notice"]
        print(f"\n模型={layout['model_used']} 房间数={len(layout['rooms'])} "
              f"置信度={layout['confidence']} 降级={out['degraded']}")
        print(f"LLM 元信息={out['trace'][0]['llm']}")
