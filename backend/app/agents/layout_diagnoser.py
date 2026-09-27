"""
文件路径: backend/app/agents/layout_diagnoser.py
模块职责: A-02 户型诊断 —— 采光 / 通风 / 动线 / 面积利用 / 绿色环保
依赖关系: A-01（layout_parser）的产出 -> 本 Agent -> A-03（space_planner）
对应 Skill 文档: skills/02_layout_diagnoser.md
上游: A-00 MasterOrchestrator
下游: A-03 SpacePlannerAgent

对应需求文档:
  - 2.2.1 户型图解析模块（诊断内容）
  - 2.2.6 业务连续性守卫（V2.2）：降级结果不得流入诊断
  - 4.2 户型解析接口响应的 diagnosis 字段结构
  - AC-03：输出 ≥ 5 个维度的评分与建议
  - AC-33：degraded_basic 下诊断必须被拒

═══════════════════════════════════════════════════════════════════
本 Agent 的两个关键设计
═══════════════════════════════════════════════════════════════════

【设计 1】入口先过业务连续性守卫
    诊断依赖**窗户、朝向、面积**三项数据。而降级解析（degraded_basic）
    这三项全是空的 —— 此时若硬跑诊断，模型只能凭空编造评分。
    一个编造的"采光 7.5 分"比"无法诊断"糟糕得多：用户会拿它当依据，
    而它背后没有任何数据支撑。

    因此本 Agent 第一件事是 check_operation(layout, "diagnose")，
    不允许就带着明确的 reason/missing/suggestion 抛错。

【设计 2】诊断是推理，但推理不等于可以造依据
    解析是"提取图中已有的信息"，诊断是"基于信息做判断"。
    可以有权重、有偏好，但每个评分都要能说清基于哪条数据。
    所以：
      - 缺数据必须写进 data_gaps（由模型填，但代码兜底校验）
      - 承重墙提示由**代码强制生成**，不依赖模型是否想得起来
"""

from __future__ import annotations

import json
from typing import Any

from ..core.capabilities import OperationNotAllowedError, check_operation
from ..graph.state import HomeDecoState
from ..schemas.layout import LayoutDiagnosis
from .base import AgentInputError, BaseAgent

#: 详情文档进提示词时的截断长度。一份详细说明通常 1–3 KB；
#: 留 6000 字符足够覆盖，又不至于把上下文挤满（诊断本身还要放户型数据）。
HOUSE_DETAIL_MAX_CHARS = 6000


def house_detail_text(layout: dict[str, Any] | None) -> str:
    """
    取出屋主提供的户型详情正文（没有就返回空串）。

    `layout["house_detail"]` 的形状：`{"text": str, "title": str, "source": str,
    "updated_at": iso}`，由 `POST /layout/{id}/house-detail` 写入。
    """
    if not layout:
        return ""
    detail = layout.get("house_detail")
    if not isinstance(detail, dict):
        return ""
    text = (detail.get("text") or "").strip()
    return text[:HOUSE_DETAIL_MAX_CHARS]

SYSTEM_PROMPT = """你是一名从业十年的住宅设计师，擅长从户型图数据中判断居住品质。
用户会给你一份**结构化的户型解析数据**，你要基于这些数据给出诊断。

【数据来源纪律 — 最重要的一条】
你的评分必须能追溯到给定的数据。请遵守：
1. 判断采光要看**窗户的数量、朝向、宽度**。
2. 判断通风要看**朝向与是否为南北通透/对流通风**。
3. 判断动线要看**房间之间的相邻关系和入户位置**。没有位置数据时只能做粗略推断，
   必须在 data_gaps 中说明"基于房间列表推断，未使用实际位置"。
4. 判断空间利用率要看**各房间面积及其占比**。
5. **绝对禁止编造数据里没有的信息**。不要假设"通常三室两厅会有两个卫生间"，
   不要用行业经验补全户型细节。看不出来就说看不出来。

【数据不足怎么办 —— 用 insufficient_data 这个出口】
某项数据缺失时，**不要为了填满而给一个假分数**。
把该维度的 `insufficient_data` 设为 `true`、`score` 填 0、
并在该维度的 `issues` 里写明"缺什么数据"。

举例：数据里没有窗户信息时：
  "lighting": {"score": 0, "insufficient_data": true,
               "issues": ["数据中未识别到窗户，无法评估采光"], "suggestions": []}

**一个编造的"采光 7.5 分"比"无法评估"糟糕得多** —— 用户会拿它当依据，
而它背后没有任何数据支撑。同理，`overall_score` 只应综合**能评估的维度**。

【评分尺度】
0-3 分：明显缺陷，建议改造或需重点关注
4-6 分：中规中矩，存在可优化的地方
7-8 分：良好，符合现代居住标准
9-10 分：优秀，属于该户型类型中的上乘
**不要把所有维度都打成 7-8 分**——那说明你没有认真分别评估。

【面向谁说话】
summary 是给**业主**看的，不是给设计师看的。
用"北侧卧室采光不足，建议..."而不是"北向房间采光系数偏低"。
不要堆术语，要说人话。

【屋主提供的户型详情 —— 优先采信】
如果用户消息里带了「屋主提供的户型详情」，那是**屋主自己写的房子实际情况**，
比从平面图上推断的更权威。层高、朝向、采光面、通风路径、收纳、设备这些
"二维平面图上根本读不出来"的项目，以它为准：
  · 被它覆盖到的项目**不再算数据缺口**，不要写进 data_gaps；
  · 不要因为"图上没标注"就压着评分或置信度不给 —— 现在有依据了；
  · 它与平面图冲突时以详情为准，并在 summary 里说明采信了哪一处。
⚠️ 但**也不能因为有了详情就把它没写的项目编出来**：详情没提到的，仍然按缺数据处理。

⚠️⚠️ **"平面图没标注、但详情里写了"的项目，不算缺口，也不要写进 data_gaps。**
   典型的就是窗宽、房间朝向、承重墙位置、层高、收纳尺寸 —— 这些都是详情已经
   给了的。写成"平面图未标注窗宽，本项依据来自屋主详情"会让用户以为数据还不足，
   而实际上**依据是齐全的**：来源换了一份而已，不是缺了。
   这类"换了依据来源"的说明，请写在对应维度的 issues 里（那是解释），
   **不要写进 data_gaps**（那是缺口清单）。

【置信度怎么给】
  · 详情覆盖了采光/通风/朝向/墙体/收纳这些关键项时，**置信度应当给到 0.85 以上** ——
    依据齐全却报 0.5 是过谦，而"过谦"和"编造"一样会让用户不知道该信几分。
  · data_gaps 为空时，置信度应在 0.9 上下 —— 不要习惯性地只给 0.7、0.8。

【承重墙提示】
若数据中包含 load_bearing 类型的墙体，必须在 load_bearing_warning 中明确提示
"任何拆改前必须由具备资质的专业人员现场复核"。这是安全要求，不是建议。"""


class LayoutDiagnoserAgent(BaseAgent):
    """A-02：户型结构化数据 → 五维诊断。"""
    code = "A-02"
    name = "LayoutDiagnoserAgent"
    label = "户型诊断"
    requires_vision = False          # 纯文本推理，比 A-01 快得多
    # 诊断是纯文本调用，比多模态解析快；但实测也撞过一次 30s。
    # 放宽到 60s —— 宁可多等一会儿，也不要让用户白跑一遍 40 秒的解析。
    timeout = 60.0

    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        layout = state.get("layout")
        if not layout:
            # ⚠️ message 会**上屏**（errors[].message），所以是一句给用户的话；
            #    "上游 A-01 没产出 layout" 这类细节进日志（见 base.AgentInputError）。
            self.log.warning("状态中缺少 layout，上游 A-01 未产出，A-02 无法执行")
            raise AgentInputError(
                "这次没能拿到户型数据，诊断无法进行 —— "
                "请重新上传户型图解析一次。"
            )

        # ══ 业务连续性守卫 ══════════════════════════════════════
        # 见模块头「设计 1」：诊断依赖窗户/朝向/面积，缺了就只能编。
        # 降级解析（degraded_basic）下这三项全空，必须先在这里拦住。
        cap = check_operation(layout, "diagnose")
        if not cap.allowed:
            raise OperationNotAllowedError("diagnose", cap)

        user_prompt = self._build_prompt(layout)
        parsed, llm_result = await self._call_llm(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=LayoutDiagnosis,
        )
        assert isinstance(parsed, LayoutDiagnosis)

        payload = self._postprocess(parsed, layout)

        self.log.info(
            f"诊断完成 overall={payload['overall_score']} "
            f"confidence={payload['confidence']} "
            f"data_gaps={len(payload['data_gaps'])}"
        )

        return {
            "diagnosis": payload,
            "degraded": llm_result.degraded,
            "degrade_reasons": (
                [f"本次{self.label}由本地处理完成：{llm_result.degrade_reason}"]
                if llm_result.degrade_reason else []
            ),
            "phase": "diagnosing",
            "_llm_meta": self._last_llm_meta,
        }

    # ── 提示词构造 ────────────────────────────────────────

    @staticmethod
    def _build_prompt(layout: dict[str, Any]) -> str:
        """
        把户型数据整理成模型易读的形式。

        **刻意只传诊断用得到的字段**（房间/门窗/朝向/面积），
        不把整份 layout 塞进去——原始 JSON 里有大量与诊断无关的字段
        （image_quality_note、uncertain_points 等），既费 token 又干扰判断。

        ⚠️ **唯一例外是「屋主提供的户型详情」**（`layout["house_detail"]`，见
        `POST /layout/{id}/house-detail`）。平面图是二维的，层高、朝向、
        采光面、通风路径、收纳空间这些**图上根本看不出来** —— 这正是诊断
        一直"数据不足、置信度低"的原因。屋主补一份文字说明就把这些补齐了，
        所以要原样带上，并明确它的优先级：**详情文档 > 从图上推断**。
        """
        rooms = layout.get("rooms") or []
        windows = layout.get("windows") or []
        doors = layout.get("doors") or []
        walls = layout.get("walls") or []

        # 逐项统计，让模型一眼看出"有没有数据"，而不是自己去遍历数组
        room_lines = []
        for r in rooms:
            area = r.get("area") or 0
            area_txt = f"{area}㎡" if area > 0 else "面积未知"
            room_lines.append(
                f"  - {r.get('name', '未命名')}（{r.get('type', 'other')}）"
                f"{area_txt}，朝向：{r.get('orientation', 'unknown')}"
            )

        win_lines = [
            f"  - 宽 {w.get('width') or '未知'} 米，朝向 {w.get('orientation', 'unknown')}"
            for w in windows
        ] or ["  （数据中未识别到任何窗户）"]

        load_bearing = [w for w in walls if w.get("type") == "load_bearing"]

        # ── 屋主提供的户型详情（有就带上，它是权威输入）──
        detail_text = house_detail_text(layout)

        data_summary = {
            "房间数量": len(rooms),
            "识别到的窗户数量": len(windows),
            "识别到的门数量": len(doors),
            "识别到的墙体数量": len(walls),
            "其中承重墙数量": len(load_bearing),
            "套内总面积": layout.get("total_area") or 0,
            "入户朝向": layout.get("entrance_orientation", "unknown"),
            "图中是否有指北针": layout.get("has_north_arrow", False),
            "面积数据是否完整": all((r.get("area") or 0) > 0 for r in rooms) if rooms else False,
            "是否有屋主提供的户型详情": bool(detail_text),
        }

        detail_block = ""
        if detail_text:
            detail_block = f"""
【屋主提供的户型详情（**权威输入，优先采信**）】
{detail_text}

⚠️ 上面这份详情是屋主自己写的房子实际情况。**它说的是这房子的真实信息**，
   层高/朝向/采光面/通风路径/收纳位置这些"平面图上读不出来"的项目，
   以它为准。凡是被这份详情覆盖到的项目，就**不再是数据缺口**，
   不要写进 data_gaps，也不要因为"图上没标注"而压低评分或置信度。
   它与平面图冲突时以详情为准，并在 summary 里说明你采信了哪一处。
"""

        return f"""请对以下户型数据做诊断。

【数据概览】
{json.dumps(data_summary, ensure_ascii=False, indent=2)}

【房间列表】
{chr(10).join(room_lines) if room_lines else "  （无房间数据）"}

【窗户】
{chr(10).join(win_lines)}

【墙体】
{chr(10).join(f"  - {w.get('type')}" for w in walls) if walls else "  （数据中未识别到墙体）"}

【上游解析的自评置信度】
{layout.get('confidence', 0)}
{detail_block}
请基于以上数据给出五个维度的诊断。
**再次强调**：数据中没有的项目不要编造，把"缺什么、因此哪个评分不可靠"写进 data_gaps。"""

    # ── 后处理 ────────────────────────────────────────────

    @staticmethod
    def _postprocess(parsed: LayoutDiagnosis, layout: dict[str, Any]) -> dict[str, Any]:
        """
        代码兜底，不把关键约束交给模型的自觉。

        三件事：
        1. **承重墙提示强制生成** —— 安全要求，不能指望模型每次都记得写；
        2. **data_gaps 兜底** —— 若模型声称"一切正常"但实际数据缺失，代码补上；
        3. **屋主提供的详情文档算"有数据"** —— 见下面 `has_detail` 那一段。
        """
        payload = parsed.model_dump()

        # ⚠️ 有屋主详情文档时，采光/通风这两项**不再算数据不足**：
        #    平面图上读不出层高、朝向、采光面、通风路径，而详情文档里写着。
        #    不分这一层的话，用户明明补了资料，评分与置信度还是上不去。
        #    但来源要如实标出来 —— 这是"依据来自详情文档"，不是"我们测出来的"。
        has_detail = bool(house_detail_text(layout))

        # ── 0. 数据不足的维度：由代码强制标记，不给模型编造的机会 ──
        # 每个维度都对应一类必需数据，缺了就标"数据不足"而不是留假分数。
        #
        # 实测发现模型有时会**自己**用这个出口（例如只识别到 1 樘门时主动标记
        # 动线不可评估）——那是好事，但**不能依赖它**。关键判定必须由代码保证，
        # 否则换个模型/换个种子就退化成编造。下面逐项兜底。
        _DIM_REQUIRES: list[tuple[str, str, bool, str]] = [
            # (维度, 中文名, 数据是否具备, 缺失说明)
            ("lighting", "采光",
             bool(layout.get("windows")) or has_detail,
             "未识别到窗户，且屋主详情里也没写采光面"),
            ("ventilation", "通风",
             bool(layout.get("windows")) or has_detail,
             "未识别到窗户，且屋主详情里也没写通风路径"),
            # 门少于 2 樘就无法判断房间之间的连通关系
            ("circulation", "动线",
             len(layout.get("doors") or []) >= 2, "识别到的门不足 2 樘"),
        ]
        for dim, label, has_data, why in _DIM_REQUIRES:
            if has_data:
                continue
            payload[dim]["score"] = 0.0
            payload[dim]["insufficient_data"] = True
            gap = f"数据中{why}，{label}无法评估"
            if gap not in payload[dim]["issues"]:
                payload[dim]["issues"] = [gap] + payload[dim]["issues"]

        # 综合分不应把"数据不足"的维度算进去——否则 0 分会把整体拉垮，
        # 而那个 0 分并不代表户型差，只代表我们不知道。
        scored = [d for k, d in payload.items()
                  if isinstance(d, dict) and "score" in d and not d.get("insufficient_data")]
        if scored:
            payload["overall_score"] = round(
                sum(d["score"] for d in scored) / len(scored), 1
            )

        # ── 1. 承重墙提示：由代码判定，模型写没写都覆盖 ──
        load_bearing = [w for w in (layout.get("walls") or [])
                        if w.get("type") == "load_bearing"]
        if load_bearing:
            notice = (
                f"检测到 {len(load_bearing)} 处疑似承重墙。"
                "任何拆改前必须由具备资质的专业人员现场复核，"
                "切勿依据本系统结论直接施工。"
            )
            if notice not in payload["load_bearing_warning"]:
                payload["load_bearing_warning"] = [notice] + payload["load_bearing_warning"]
        elif not payload["load_bearing_warning"]:
            # 没识别到承重墙 ≠ 没有承重墙，这个区别对用户很重要
            payload["load_bearing_warning"] = [
                "未能明确识别承重墙。如需拆改，务必由专业人员现场确认，"
                "不可默认全部墙体为非承重。"
            ]

        # ── 2. data_gaps 兜底：代码能判定的缺失，直接补进去 ──
        gaps = list(payload["data_gaps"])
        rooms = layout.get("rooms") or []
        #: 代码判定出来的**基础数据缺口**。与模型自己写的那堆"知情说明"分开计数 ——
        #: 见第 3 步：压低置信度只该由**我们的数据缺失**触发，不该由模型的谨慎措辞触发。
        #:
        #: ⚠️ 这里存的是**中文名**，不是 `windows` / `areas` 那种内部键 ——
        #:    这些字直接进 `data_gaps` 上屏（下面第 3 步那句"因缺少 N 项基础数据"），
        #:    内部键会让界面上出现一串英文枚举。
        hard_gaps: list[str] = []

        if not (layout.get("windows") or []) and not has_detail:
            gaps.append("数据中未识别到窗户，采光与通风评分缺乏直接依据")
            hard_gaps.append("窗户")
        if not any((r.get("area") or 0) > 0 for r in rooms):
            gaps.append("房间面积数据缺失，空间利用率评分不可靠")
            hard_gaps.append("面积")
        # 朝向：详情文档里写了朝向，就不算缺口（指北针只是"从图上判断朝向"的手段之一）
        if not layout.get("has_north_arrow") and not has_detail:
            gaps.append("图中无指北针，朝向判断可能不准确")
            hard_gaps.append("朝向标注")
        if not (layout.get("walls") or []):
            gaps.append("未识别到墙体，承重墙判断无从进行")
            hard_gaps.append("墙体")

        # 去重保序
        payload["data_gaps"] = list(dict.fromkeys(gaps))

        # ── 3. 数据缺失时主动压低置信度 ──
        # 模型有时会对自己没数据也给出高置信度，这里用代码兜一道。
        #
        # ⚠️ **判据是「代码判定的基础缺口」数量，不是 data_gaps 的总条数。**
        #    原来的口径是"缺口 ≥3 就压到 0.5 以下"——那会把**模型的谨慎措辞**
        #    也算成数据不足：实测补完屋主详情之后，模型仍然列了 6 条
        #    "平面图未标注窗宽，本项依据来自屋主详情"这类知情说明，
        #    于是置信度被死死压在 0.5，用户看到的就是"补了资料还是数据不足"。
        #    现在只有真的缺基础数据（窗/面积/朝向/墙体）才压，且按缺的条数分档。
        if len(hard_gaps) >= 2:
            payload["confidence"] = min(payload["confidence"], 0.5)
            payload["data_gaps"].append(
                f"因缺少 {len(hard_gaps)} 项基础数据（{'、'.join(hard_gaps)}），"
                "本诊断整体置信度已下调至 0.5 以下"
            )
        elif len(hard_gaps) == 1:
            payload["confidence"] = min(payload["confidence"], 0.7)

        payload["model_based_on"] = {
            "rooms": len(rooms),
            "windows": len(layout.get("windows") or []),
            "has_area": any((r.get("area") or 0) > 0 for r in rooms),
            "has_house_detail": has_detail,
        }

        # ── 4. 依据来源：**逐条列出来源**，别让"这份评分是怎么来的"成为黑箱 ──
        #    （与避坑审查的"每条结论都要能引回原文"同一条纪律。）
        sources = ["平面图解析结果（房间/墙体/门窗/面积）"]
        if has_detail:
            title = (layout.get("house_detail") or {}).get("title") or "屋主提供的户型详情"
            sources.append(f"屋主提供的户型详情：{title}"
                           "（层高/朝向/采光/通风/收纳等图上读不出的项目以它为准）")
        payload["evidence_sources"] = sources

        return payload


__all__ = ["LayoutDiagnoserAgent", "house_detail_text", "HOUSE_DETAIL_MAX_CHARS"]
