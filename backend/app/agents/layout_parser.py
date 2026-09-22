"""
A-01 LayoutParserAgent —— 户型图多模态解析。

把一张户型图变成结构化 JSON。这是全流程的第一道关，也是错误传播的源头：
它错了，后面 10 个 Agent 全错。因此本 Agent 的核心不是"多识别几个房间"，
而是**诚实**——识别不出就说识别不出，判不准就说判不准。

Skill 文档见 skills/01_layout_parser.md
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ..core.capabilities import attach_capabilities
from ..core.llm_client import ImagePart, LLMError
from ..graph.state import HomeDecoState
from ..schemas.layout import DegradedLayout, LayoutSchema
from .base import BaseAgent

SYSTEM_PROMPT = """你是一名资深的室内设计师与建筑制图工程师，擅长解读住宅户型图。
你的任务是观察用户提供的户型图，输出结构化的户型信息。

【必须遵守的规则】
1. **绝对禁止编造**。图中没有的内容，一律留空（空数组、0、unknown），
   不允许凭经验"补全"出看起来合理的数据。尺寸、面积尤其如此——
   一个编造的 4.2 米会让后续整套预算估算全部失真。
2. **面积只做估算**。若图中没有标注尺寸，按图中比例关系估算，并在
   uncertain_points 中写明"面积为按比例估算，非实测"。
3. **承重墙必须谨慎**。只有明确看到粗实线（或图中文字标注）才判定为
   load_bearing；粗细不明确一律填 unknown，并在 uncertain_points 中说明。
   误判承重墙会导致用户砸错墙，这是安全事故。
4. **不确定的地方必须如实列出**。uncertain_points 越诚实，系统越可靠。
   宁可写满不确定项，也不要给出自信的错误答案。
5. **置信度要真实反映把握程度**。图像模糊、倾斜、有水印、只有局部时，
   confidence 必须相应降低（低于 0.5 表示结果仅供参考）。

【观察顺序建议】
先找指北针确定朝向 → 再识别外轮廓 → 再逐个划分房间 → 最后识别墙/门/窗与尺寸标注。"""


class LayoutParserAgent(BaseAgent):
    """A-01：户型图 → 结构化 JSON。"""

    code = "A-01"
    name = "LayoutParserAgent"
    requires_vision = True
    timeout = 45.0  # 多模态解析比纯文本慢，给更宽的熔断窗口

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        image = self._image_from_state(state)
        if image is None:
            raise ValueError("状态中缺少 image_ref，无法解析户型图")

        layout_id = state.get("layout_id") or self._make_layout_id()
        prefer_local = bool(state.get("prefer_local"))

        # ══ 路径 A：主模型全量解析 ══════════════════════════════
        # allow_degrade=False 是**刻意为之**：本地兜底模型产不出可信的结构化户型，
        # 让它拿完整 Schema 硬上只会安静地编造数据（实测见 DegradedLayout 的注释）。
        # 这里宁可显式失败，再由下面的路径 B 做能力匹配的降级。
        if not prefer_local:
            try:
                parsed, llm_result = await self._call_llm(
                    system=SYSTEM_PROMPT,
                    user=self._build_prompt(state),
                    images=[image],
                    schema=LayoutSchema,
                    allow_degrade=False,
                )
                assert isinstance(parsed, LayoutSchema)
                return self._build_full_result(parsed, llm_result, layout_id)

            except Exception as e:  # noqa: BLE001 —— 主模型不可用，转降级路径
                self.log.warning(f"主模型解析失败，转入降级路径: {type(e).__name__}: {str(e)[:200]}")
                primary_error = f"{type(e).__name__}: {str(e)[:180]}"
                cause = "primary_failed"
        else:
            primary_error = "用户选择本地处理模式，未调用云端模型"
            cause = "user_preferred_local"

        # ══ 路径 B：本地兜底，只问能力范围内的事 ══════════════════
        return await self._degraded_parse(image, layout_id, primary_error, cause)

    # ── 路径 A：全量结果 ──────────────────────────────────

    def _build_full_result(
        self, parsed: LayoutSchema, llm_result, layout_id: str
    ) -> dict[str, Any]:
        payload = parsed.model_dump()

        warnings: list[str] = []
        if not parsed.rooms:
            warnings.append("未识别出任何房间，请确认上传的是户型图而非其他图片")
        if parsed.confidence < 0.5:
            warnings.append(f"识别置信度偏低（{parsed.confidence:.2f}），结果仅供参考")
        if not parsed.has_north_arrow:
            warnings.append("图中未发现指北针，朝向判断可能不准确")
        for issue in warnings:
            self.log.warning(issue)

        payload.update({
            "layout_id": layout_id,
            "mode": "full",
            "warnings": warnings,
            "safety_notice": self._safety_notice(parsed),
            "model_used": llm_result.model_used,
        })
        # 标注这份结果能支撑哪些后续操作（前端置灰 / 后端 400 拦截的依据）
        attach_capabilities(payload)

        return {
            "layout": payload,
            "layout_id": layout_id,
            "degraded": llm_result.degraded,
            "degrade_reasons": [llm_result.degrade_reason] if llm_result.degrade_reason else [],
            "phase": "parsed",
            "_llm_meta": self._last_llm_meta,
        }

    # ── 路径 B：降级结果（只给房间名）──────────────────────

    async def _degraded_parse(
        self, image, layout_id: str, primary_error: str, cause: str
    ) -> dict[str, Any]:
        """
        本地兜底解析：只提取房间名称，结构字段一律留空。

        质量闸门在此生效：即便只是一个房间名列表，也要过最低门槛
        （至少 2 个房间），否则宁可整条链失败，也不返回空壳结果糊弄用户。
        """
        self.log.warning(f"启用本地降级解析（原因: {cause} / {primary_error}）")

        degraded, llm_result = await self._call_llm(
            system=(
                "你是一个谨慎的图像观察助手。用户会给你一张户型图，"
                "你只需列出图中能看到的房间名称（如客厅、卧室、厨房）。\n"
                "严禁推测：看不清的房间不要写，不要补充墙体、门窗、面积等任何其他信息。\n"
                "如果图中没有任何可辨认的房间名称，返回空列表。"
            ),
            user="这张户型图里能看清的房间有哪些？只回答房间名称。",
            images=[image],
            schema=DegradedLayout,
            # 降级路径按定义就是本地路径。force_local 必须在**提供方选择**层强制，
            # 否则「本地模式」只是嘴上说说，图像照样会发到云端（实测踩过，见 AC-24）。
            force_local=True,
        )
        assert isinstance(degraded, DegradedLayout)

        # ── 质量闸门 ──────────────────────────────────────
        gate_failures: list[str] = []
        if not degraded.rooms:
            gate_failures.append("未识别出任何房间名称")
        if len(degraded.rooms) < 2:
            gate_failures.append(f"仅识别出 {len(degraded.rooms)} 个房间，信息量不足")

        if gate_failures:
            raise LLMError(
                f"[A-01] 降级解析未通过质量闸门（{'；'.join(gate_failures)}）。"
                f"主模型失败原因: {primary_error}。"
                f"建议用户稍后重试或更换更清晰的户型图，不返回低质量结果。"
            )

        # 关键：结构字段全部留空，不由模型推断
        payload = {
            "layout_id": layout_id,
            "mode": "degraded_basic",
            "rooms": [
                {"name": r.name, "type": "other", "area": 0.0, "bbox": [],
                 "orientation": "unknown", "notes": ""}
                for r in degraded.rooms
            ],
            "walls": [], "doors": [], "windows": [], "dimensions": [],
            "total_area": 0.0,
            "entrance_orientation": "unknown",
            "has_north_arrow": False,
            "confidence": degraded.confidence,
            "uncertain_points": [
                "本次由本地兜底模型完成，仅能识别房间名称",
                "墙体、门窗、尺寸、面积均未识别，请勿据此做拆改或预算决策",
                degraded.uncertainty or "",
            ],
            "image_quality_note": "",
            "model_used": llm_result.model_used,
            "warnings": [
                self._degrade_headline(cause),
                f"原因：{primary_error}",
            ],
            "safety_notice": (
                "本结果未经完整解析，**无法判断承重墙**。任何拆改前必须由具备资质的"
                "专业人员现场复核，切勿依据本系统结论直接施工。"
            ),
        }
        # 降级结果只允许「查看房间名」与「导出清单」。
        # 方案生成/预算/诊断/出图全部会因缺少面积与 bbox 而被拦截——
        # 这是**刻意**的：与其让下游算出一个 ¥0 的预算，不如在入口就说清不能做。
        attach_capabilities(payload)

        return {
            "layout": payload,
            "layout_id": layout_id,
            "degraded": True,
            "degrade_reasons": [
                f"[A-01] {self._degrade_headline(cause)}（{primary_error}），"
                f"仅识别到房间名称，结构信息缺失"
            ],
            "phase": "degraded",
            "_llm_meta": self._last_llm_meta,
        }

    @staticmethod
    def _degrade_headline(cause: str) -> str:
        """
        降级原因不同，给用户的说法也应不同——用户主动选隐私模式、
        和系统被迫降级，是两回事，不能混为一谈。
        """
        if cause == "user_preferred_local":
            return "您选择了本地处理模式：户型图未离开本机，代价是无法解析墙体、门窗与尺寸"
        return "云端主模型当前不可用，已自动降级为本地处理，仅提供房间名称参考"

    @staticmethod
    def _safety_notice(parsed: LayoutSchema) -> str:
        """安全提示是强制项，两条路径都必须给出。"""
        load_bearing = [w for w in parsed.walls if w.type == "load_bearing"]
        if load_bearing:
            return (
                "图中存在疑似承重墙，任何拆改前必须由具备资质的专业人员现场复核，"
                "切勿依据本系统结论直接施工。"
            )
        return "未能明确识别承重墙，如需拆改请务必由专业人员现场确认。"

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _build_prompt(state: HomeDecoState) -> str:
        """构造用户侧提示词。detail_level 控制输出粒度以节省 token。"""
        detail = state.get("detail_level", "full")
        parts = ["请解析这张户型图，输出结构化 JSON。"]

        if detail == "basic":
            parts.append("本次只需给出房间列表、总面积与置信度，墙体/门窗/尺寸可留空。")

        req = state.get("requirements") or {}
        if req:
            # 需求会影响房间识别的关注点（如是否有老人房需求），但不影响客观识别
            parts.append(
                "补充背景（仅用于提示识别重点，不影响客观判断）："
                f"家庭成员 {req.get('family_size', '未提供')} 人"
                f"{'，有老人' if req.get('has_elderly') else ''}"
                f"{'，有儿童' if req.get('has_children') else ''}"
                "。"
            )

        if state.get("prefer_local"):
            parts.append("注意：本次由本地模型处理，请优先保证结构正确，宁可少写不要写错。")

        return "\n".join(parts)

    @staticmethod
    def _make_layout_id() -> str:
        """生成户型 ID，格式与 API 契约一致：layout_YYYYMMDD_<短uuid>。"""
        stamp = datetime.now().strftime("%Y%m%d")
        return f"layout_{stamp}_{uuid.uuid4().hex[:6]}"


__all__ = ["LayoutParserAgent"]
