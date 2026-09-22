"""
LLMClient 与日志基础设施测试。

本文件守的是两条**安全/隐私属性**，不是普通功能：

1. **隐私模式必须在提供方选择层生效**（AC-24）
   背景：本项目实际发生过一次违约——`prefer_local=true` 只改了提示词与 Schema，
   provider 仍是 DeepSeek，户型图照样发往云端。而当时 34 个单元测试全绿，
   因为 Agent 层的 Fake 客户端**不认识 provider 概念**。
   所以这些断言必须打在 LLMClient 这一层：直接问「谁会收到这个请求」。

2. **降级链的三级顺序与两条互斥规则**（ADR-09 / ADR-12）
   - force_local=True    -> 只有本地，云端绝不参与
   - allow_degrade=False -> 只有主模型，失败即抛错
   - 两者同时给时 force_local 优先（隐私不可被其它参数绕过）
"""

from __future__ import annotations

import pytest

from backend.app.core.llm_client import (
    ImagePart,
    LLMClient,
    LLMTruncatedError,
    build_schema_instruction,
    extract_json,
)
from backend.app.schemas.layout import LayoutSchema


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


# ══════════════════════════════════════════════════════════════════
# 提供方选择 —— 隐私与降级的强制点
# ══════════════════════════════════════════════════════════════════


class TestProviderChain:
    """这些断言必须打在这一层——Agent 层的 Fake 看不见 provider。"""

    def test_force_local_excludes_cloud(self, client: LLMClient):
        """隐私模式：链路里绝不能出现 deepseek。"""
        chain = client._chain(vision=True, force_local=True)
        assert chain, "本地链路不应为空"
        assert all(p.name == "ollama" for p in chain), \
            f"隐私模式下不应包含云端提供方，实际: {[p.name for p in chain]}"

    def test_force_local_also_applies_to_text(self, client: LLMClient):
        """非视觉任务同样受隐私开关约束。"""
        chain = client._chain(vision=False, force_local=True)
        assert all(p.name == "ollama" for p in chain)

    def test_allow_degrade_false_excludes_local(self, client: LLMClient):
        """
        全量解析：链路里只应有主模型。

        理由：实测本地 minicpm-v4.6 拿完整 LayoutSchema 会编造越界 bbox
        与虚构墙体，且 JSON 语法本身是坏的。宁可显式失败（AC-26）。
        """
        chain = client._chain(vision=True, allow_degrade=False)
        assert chain, "即使禁止降级，主模型也应在链中"
        assert all(p.name != "ollama" for p in chain), \
            f"禁止降级时不应包含本地提供方，实际: {[p.name for p in chain]}"

    def test_default_chain_prefers_cloud_then_local(self, client: LLMClient):
        """默认链路：云端优先，本地兜底。"""
        names = [p.name for p in client._chain(vision=True)]
        assert names[0] == "deepseek"
        assert "ollama" in names

    def test_force_local_wins_over_allow_degrade(self, client: LLMClient):
        """
        两个开关同时给时 force_local 优先。

        这条是防御性的：将来若有人顺手加了个 allow_degrade=True，
        隐私保证不能被悄悄绕过。
        """
        chain = client._chain(vision=True, allow_degrade=True, force_local=True)
        assert all(p.name == "ollama" for p in chain)

    def test_vision_and_text_use_different_models(self, client: LLMClient):
        """视觉链走多模态模型，文本链走文本模型，不能串。"""
        vision = client._chain(vision=True, force_local=True)[0]
        text = client._chain(vision=False, force_local=True)[0]
        assert vision.model != text.model

    def test_no_key_means_no_cloud_in_chain(self, client: LLMClient, monkeypatch):
        """未配置 key 时不得把 DeepSeek 放进链里（否则必然空等一次超时）。"""
        from backend.app.core import config

        monkeypatch.setattr(config.settings, "DEEPSEEK_API_KEY", "", raising=False)
        names = [p.name for p in client._chain(vision=True, allow_degrade=False)]
        assert "deepseek" not in names


# ══════════════════════════════════════════════════════════════════
# 结构化输出 —— schema 注入与抽取
# ══════════════════════════════════════════════════════════════════


class TestSchemaInstruction:
    """
    这个渲染器是结构化输出的命门。

    初版只展开「直接 $ref」的字段，没有展开**数组元素里的 $ref**
    （`rooms: list[Room]` 的 JSON Schema 是 {"type":"array","items":{"$ref":...}}）。
    结果模型只被告知"rooms 是个数组"，从没见过 Room 有哪些字段、
    type 能取哪些值，于是自由发挥了 `type: "unknown"` —— 校验必然失败，
    而且重试再多次也没用（模型压根不知道错在哪）。
    """

    def test_expands_array_item_ref(self):
        hint = build_schema_instruction(LayoutSchema)
        assert "其每个元素的结构为" in hint, "数组元素的 $ref 必须展开"

    def test_enumerates_nested_enum_values(self):
        hint = build_schema_instruction(LayoutSchema)
        # Room.type 的枚举值必须全部列出，否则模型会自创取值
        assert "living_room | bedroom" in hint
        assert "load_bearing" in hint

    def test_includes_field_descriptions(self):
        hint = build_schema_instruction(LayoutSchema)
        assert "置信度" in hint and "uncertain_points" in hint

    def test_demands_json_only(self):
        hint = build_schema_instruction(LayoutSchema)
        assert "只能" in hint and "JSON" in hint

    def test_renders_nested_list(self):
        """list[list[int]] 应说人话，而不是"数组 组成的数组"。"""
        hint = build_schema_instruction(LayoutSchema)
        assert "组成的数组 组成的数组" not in hint


class TestJsonExtraction:
    def test_markdown_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around(self):
        assert extract_json('结果如下\n{"a": 1}\n完毕') == {"a": 1}

    def test_string_containing_brace(self):
        assert extract_json('{"note": "含 { 和 } 的说明"}') == {"note": "含 { 和 } 的说明"}

    def test_repairs_unquoted_enum(self):
        """实测：小模型常把枚举值裸写（orientation: north）。"""
        assert extract_json('{"orientation": north}') == {"orientation": "north"}

    def test_repairs_trailing_comma(self):
        assert extract_json('{"a": 1,}') == {"a": 1}

    def test_keeps_booleans_and_null(self):
        assert extract_json('{"ok": true, "n": 3, "z": null}') == {
            "ok": True, "n": 3, "z": None}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            extract_json("")


# ══════════════════════════════════════════════════════════════════
# 思考模型的空输出陷阱
# ══════════════════════════════════════════════════════════════════


class TestTruncatedError:
    """
    实测：deepseek-flash 单次调用 179 个 completion token 里 169 个是 reasoning。
    当 max_tokens=100 时推理吃光全部预算，content 返回**空字符串**——
    HTTP 200、无异常，排查成本极高。故必须显式检测。
    """

    def test_error_carries_actionable_hint(self):
        e = LLMTruncatedError("返回空内容，但消耗了 169 个 reasoning token")
        assert "reasoning" in str(e)

    def test_is_a_llm_error(self):
        from backend.app.core.llm_client import LLMError

        assert issubclass(LLMTruncatedError, LLMError)


# ══════════════════════════════════════════════════════════════════
# 日志 trace_id（AC-30 / ADR-13）
# ══════════════════════════════════════════════════════════════════


class TestTraceContext:
    def test_bind_and_read(self):
        from backend.app.core.logger import bind_trace_id, current_trace_id

        tid = "11111111-2222-3333-4444-555555555555"
        bind_trace_id(tid)
        assert current_trace_id() == tid

    def test_context_manager_restores(self):
        from backend.app.core.logger import bind_trace_id, current_trace_id, with_trace_id

        bind_trace_id("outer")
        with with_trace_id() as inner:
            assert inner and current_trace_id() == inner
        assert current_trace_id() == "outer", "退出上下文后必须还原"

    def test_async_task_rebind(self):
        """
        异步任务恢复场景：API 层生成 trace_id 写入 async_tasks，
        后台任务取回后重新绑定——异步上下文不会自动继承（ADR-13）。
        """
        from backend.app.core.logger import bind_trace_id, current_trace_id, new_trace_id

        tid = new_trace_id()
        bind_trace_id(tid)
        assert current_trace_id() == tid

    def test_new_trace_id_is_unique(self):
        from backend.app.core.logger import new_trace_id

        assert len({new_trace_id() for _ in range(50)}) == 50

    def test_logging_setup_is_a_compat_shim(self):
        """logging_setup 必须只是转发，不能另起一套实现。"""
        from backend.app.core import logging_setup, logger as logger_mod

        assert logging_setup.setup_logging is logger_mod.setup_logging
        assert logging_setup.logger is logger_mod.logger


# ══════════════════════════════════════════════════════════════════
# ImagePart
# ══════════════════════════════════════════════════════════════════


class TestImagePart:
    def test_data_uri_roundtrip(self):
        p = ImagePart(data_b64="QUJD", media_type="image/png")
        assert ImagePart.from_data_uri(p.to_data_uri()).data_b64 == "QUJD"

    def test_from_path_guesses_mime(self, tmp_path):
        from PIL import Image

        f = tmp_path / "x.jpg"
        Image.new("RGB", (8, 8)).save(f)
        assert ImagePart.from_path(str(f)).media_type == "image/jpeg"
