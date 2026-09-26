"""
前后端契约的静态校验。

══════════════════════════════════════════════════════════════════════
为什么后端测试要去读前端源码
══════════════════════════════════════════════════════════════════════
2026-09-23 实测踩到一个**静默失败**：

  前端「生成参数」第 3 套发的是 `luxury`，而后端 `PlanStyle` 里没有这个值。
  下游 `space_planner.py` 用
      `_STYLE_HINTS.get(style, '按该风格的通行做法处理')`
  兜底 —— 那一套方案**完全没拿到风格引导**，但界面上仍显示「意式轻奢」。
  一次都没报错。

这类"跨端枚举对不上"的问题，两端的测试都看不见：
  · 后端测试发的是后端认识的值
  · 前端没有测试框架（见 README「已知限制」）

所以在这里补一道静态比对：**直接解析前端的源文件**，与后端的枚举逐字比。
它不优雅，但它是这个项目里唯一能拦住这类 bug 的地方。

⚠️ 前端源码不在时**跳过而不是失败** —— `deploy/Dockerfile` 不 COPY
   `frontend/`，而 `tests/` 也不进镜像；但万一有人在只有后端的目录里
   跑测试，这里不该把整个测试套件弄红。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from backend.app.schemas.plan import BudgetGrade, PlanStyle

REPO = Path(__file__).resolve().parents[1]
TYPES_TS = REPO / "frontend" / "src" / "api" / "types.ts"
GENERATE_VUE = REPO / "frontend" / "src" / "views" / "GenerateView.vue"

pytestmark = pytest.mark.skipif(
    not TYPES_TS.exists(),
    reason="前端源码不在（例如只部署了后端的目录），跳过契约校验",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


#: 「方案生成」模块的全部源码。**扫整个模块，不扫单个文件。**
#:
#: ⚠️ 2026-09-24 需求方要求这个模块拆成子目录（生成参数 / 三方案对比 /
#:    3D 装修漫游），于是 `PAIRS`、材料选项、提交字段从 `GenerateView.vue`
#:    分散到了三个子页和一个会话 composable 里 —— 三条契约用例当场全红。
#:
#:    那是**用例写得太紧**，不是代码坏了：它守的性质（"提交的字段与后端
#:    schema 一致""清单来自后端"）依然成立，只是不再集中在一个文件里。
#:    所以这里改成扫整个模块 —— 下次再拆文件时它们不会又红一遍。
def _generate_module() -> str:
    files = [
        REPO / "frontend/src/views/GenerateView.vue",
        REPO / "frontend/src/views/generate/GenerateSetupView.vue",
        REPO / "frontend/src/views/generate/GeneratePlansView.vue",
        REPO / "frontend/src/views/generate/GenerateWalkthroughView.vue",
        REPO / "frontend/src/composables/useGenerateSession.ts",
    ]
    missing = [f.name for f in files if not f.exists()]
    assert not missing, (
        f"「方案生成」模块的文件不见了：{missing} —— 契约用例需要跟着改"
    )
    return "\n".join(_read(f) for f in files)


def test_前端风格label与后端枚举逐字一致():
    """
    `STYLE_LABEL` 既是展示用的中文名表，**又是「生成参数」里风格下拉框的
    可选项来源**（`GenerateView` 里 `v-for="(v, k) in STYLE_LABEL"`）。

    所以它多一个键 = 用户能选到一个后端不支持的值；少一个键 = 某个
    后端支持的风格在界面上选不到。两边必须逐字相同。
    """
    src = _read(TYPES_TS)
    block = re.search(
        r"STYLE_LABEL:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", src, re.S
    )
    assert block, "types.ts 里找不到 STYLE_LABEL，契约测试需要跟着改"
    fe = sorted(re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M))
    be = sorted(get_args(PlanStyle))
    assert fe == be, (
        f"前端 STYLE_LABEL 与后端 PlanStyle 不一致。\n"
        f"  前端独有: {sorted(set(fe) - set(be))}\n"
        f"  后端独有: {sorted(set(be) - set(fe))}\n"
        f"前端多出的键会让用户选到后端不认识的值，而后端只会静默兜底。"
    )


def test_前端预算档label与后端枚举逐字一致():
    src = _read(TYPES_TS)
    block = re.search(
        r"GRADE_LABEL:\s*Record<string,\s*string>\s*=\s*\{(.*?)\}", src, re.S
    )
    assert block, "types.ts 里找不到 GRADE_LABEL"
    fe = sorted(re.findall(r"^\s*(\w+)\s*:", block.group(1), re.M))
    be = sorted(get_args(BudgetGrade))
    assert fe == be, f"前端 GRADE_LABEL 与后端 BudgetGrade 不一致：{fe} vs {be}"


def test_默认三套方案全都是后端认可的取值():
    """
    `PAIRS` 是「不填参数时一次生成哪三套」的默认值。
    里面任何一个值不被后端认可，那一套就会静默失去风格/档位引导 ——
    这正是 `luxury` 那个 bug 的形态。
    """
    src = _generate_module()
    block = re.search(r"const PAIRS = \[(.*?)\] as const", src, re.S)
    assert block, "「方案生成」模块里找不到 PAIRS"

    styles = re.findall(r"style:\s*'([^']+)'", block.group(1))
    grades = re.findall(r"grade:\s*'([^']+)'", block.group(1))
    assert styles and grades, f"PAIRS 解析失败：styles={styles} grades={grades}"

    assert all(s in get_args(PlanStyle) for s in styles), (
        f"PAIRS 里有后端不认识的风格："
        f"{[s for s in styles if s not in get_args(PlanStyle)]}"
    )
    assert all(g in get_args(BudgetGrade) for g in grades), (
        f"PAIRS 里有后端不认识的档位："
        f"{[g for g in grades if g not in get_args(BudgetGrade)]}"
    )
    # 按位置配对，长度不等时 zip 会截断（workflow.py 只告警不报错）
    assert len(styles) == len(grades), "PAIRS 的风格与档位数量必须相等（按位置配对）"



def test_前端能力报告的形状与后端逐键一致():
    """
    ⚠️ **这条守的是一个真实的、静默的漂移（2026-09-24 发现）。**

    前端 `Capabilities` 一度声明的是**扁平**字段
    （`can_generate_plan` / `can_estimate_budget` / `can_select_materials` /
    `can_review` / `missing` / `suggestion`），而后端
    `CapabilityReport.to_dict()` 返回的是**嵌套**的：

        {"mode": …, "reason": …, "operations": {"generate_plan": {…}}}

    那组扁平字段**一个都不存在**。更糟的是旧类型带 `[key: string]: unknown`
    索引签名，于是 `c.can_generate_plan` 取到 `undefined` **也不报类型错**。

    后果不是崩溃，而是**界面在猜**：
      · `canGenerate` 的 `typeof c.can_generate_plan === 'boolean'` 恒为假
        → 一直走"有房间、有面积就能生成"的兜底分支；
      · `blockedReason` 读 `c.suggestion` 恒为 undefined
        → 后端那句「缺少墙体信息」从来没显示给用户。
    于是"有房间有面积、只是没有墙"的户型在前端是可点的，点下去后端回 4002 ——
    界面承诺了一个做不到的操作。两端测试都看不见：后端测的是 API 层，
    前端没有测试框架（README「已知限制」）。

    所以在这里做逐键比对 —— 与这个文件里另外几条是同一个理由。
    """
    from backend.app.core.capabilities import evaluate_capabilities

    src = _read(TYPES_TS)
    block = re.search(r"export interface Capabilities \{(.*?)\n\}", src, re.S)
    assert block, "types.ts 里找不到 Capabilities 接口，契约测试需要跟着改"
    body = block.group(1)

    # 去掉注释行再取键，否则注释里的中文会被当成长相奇怪的标识符
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    fe_keys = set(re.findall(r"^\s*(\w+)\s*[?:]", body, re.M))
    # 索引签名 `[key: string]: unknown` 是这次漂移的帮凶，明确禁止它回来
    assert not re.search(r"\[\s*key\s*:\s*string\s*\]", body), (
        "Capabilities 又加了索引签名 —— 它会让『取一个不存在的字段』静默通过，"
        "正是上一版出问题的地方"
    )

    be_keys = set(evaluate_capabilities({}).to_dict())
    assert fe_keys == be_keys, (
        f"前端 Capabilities 与后端能力报告不一致。\n"
        f"  前端独有（永远是 undefined，代码在猜）: {sorted(fe_keys - be_keys)}\n"
        f"  后端独有（前端读不到）: {sorted(be_keys - fe_keys)}"
    )

    # 扁平字段一旦回来就是重蹈覆辙，单独钉一条好让报错说人话
    flat = sorted(k for k in fe_keys if k.startswith("can_"))
    assert not flat, (
        f"Capabilities 里又出现了扁平字段 {flat} —— 后端从没返回过它们，"
        f"真正的路径是 operations[op].allowed"
    )


def test_前端读能力用的是operations不是扁平字段():
    """
    类型对了还不够 —— **读法**也得对。

    上一版是"类型写错了 + 读法跟着错"两件事叠加：类型声明了一组
    不存在的字段，`useParseSession` 又照着那组字段去读。只修类型、
    不改读法的话，`vue-tsc` 会报错（好事），但如果有人顺手加回
    索引签名就又静默了。所以这里直接扫源码，钉住读法。
    """
    src = _read(REPO / "frontend" / "src" / "composables" / "useParseSession.ts")

    # ⚠️ **必须先把注释去掉再扫。**
    #    这个函数里正好有一段注释在解释"以前读的是 `c.can_generate_plan`，
    #    而那个字段根本不存在" —— 带着注释扫的话，正因为它把历史记清楚了，
    #    用例反而会红。第一版就是这么错的（还原修复后仍然失败，才发现）。
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    assert "operations?." in code or ".operations[" in code, (
        "useParseSession 没有从 operations 里取能力判定"
    )
    stale = re.findall(
        r"\.\s*(can_(?:generate_plan|estimate_budget|select_materials|review))\b", code)
    assert not stale, (
        f"useParseSession 又在读扁平字段 {sorted(set(stale))} —— "
        f"后端返回的是 operations[op].allowed"
    )


# ══════════════════════════════════════════════════════════════════
# AC-19 材料偏好
# ══════════════════════════════════════════════════════════════════


def _interface_body(name: str) -> str:
    """取 types.ts 里一个 interface 的字段体（去掉注释，只留声明）。"""
    src = _read(TYPES_TS)
    block = re.search(rf"export interface {name} \{{(.*?)\n\}}", src, re.S)
    assert block, f"types.ts 里找不到 {name} 接口，契约测试需要跟着改"
    body = re.sub(r"/\*.*?\*/", "", block.group(1), flags=re.S)
    return re.sub(r"//[^\n]*", "", body)


def _top_level_keys(body: str) -> set[str]:
    """
    只取**第一层**字段名。

    ⚠️ 不能简单地用 `^\\s*(\\w+)\\s*:` —— 嵌套对象的字段也会被匹配到。
    `MaterialOptionsData.constants` 里就有三个嵌套键，那样取出来的
    字段集合会比后端响应多出三个，测试报的错会指向完全无关的地方
    （这条的第一版就是这么错的）。
    types.ts 里第一层统一缩进两格，按这个来。
    """
    return set(re.findall(r"^  (\w+)\s*\??:", body, re.M))


def test_生成请求的字段与后端schema逐键一致():
    """
    AC-19 加了三个字段（排除品类/排除品牌/偏好品牌）。

    **漏接的代价是静默的**：前端多一个字段 → 后端 pydantic 默认
    `extra="ignore"` 会直接丢掉，用户勾了没反应；后端多一个字段 →
    界面上没有入口，功能存在但不可达。两种都不会报错。
    """
    from backend.app.api.schemas import GenerateRequest

    fe_keys = _top_level_keys(_interface_body("GenerateRequest"))
    be_keys = set(GenerateRequest.model_fields)

    assert fe_keys == be_keys, (
        f"GenerateRequest 前后端不一致。\n"
        f"  前端独有（后端会静默丢掉）: {sorted(fe_keys - be_keys)}\n"
        f"  后端独有（界面上没有入口）: {sorted(be_keys - fe_keys)}"
    )


def test_材料选项响应的形状与后端逐键一致():
    """`GET /material/options` 是 AC-19 新增的，同样逐键比。"""
    import asyncio

    from backend.app.api.routes import material_options

    fe_keys = _top_level_keys(_interface_body("MaterialOptionsData"))
    be = asyncio.run(material_options()).data
    assert fe_keys == set(be), (
        f"MaterialOptionsData 与后端不一致。\n"
        f"  前端独有: {sorted(fe_keys - set(be))}\n"
        f"  后端独有: {sorted(set(be) - fe_keys)}"
    )
    # 判据常量必须由后端给，前端各写一遍 0.6/1.5/2.0 迟早对不上
    assert {"min_quanyou_coverage", "preferred_brand_bonus",
            "quanyou_preference_bonus"} <= set(be["constants"])


def test_前端不硬编码材料清单也不硬编码60percent():
    """
    界面上的品类/品牌清单必须来自后端目录。

    硬编码的那份会随目录变更静默漂移：界面照常显示一个后端已经不认的品类，
    用户勾上，请求被 4001 退回 —— 在用户看来像是后端坏了。
    这条测试扫源码，钉住"清单从后端来"这件事。
    """
    code = re.sub(r"/\*.*?\*/", "", _generate_module(), flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)

    assert "materialOptions()" in code, "「方案生成」模块没有去取材料可选项"

    # 品类 key / 品牌名的硬编码清单：出现了就说明前端自己存了一份
    for token in ('"floor"', "'floor'", '"sanitary"', "'sanitary'",
                  '"立邦"', "'立邦'", '"东鹏"', "'东鹏'"):
        assert token not in code, (
            f"「方案生成」模块里硬编码了材料清单 {token} —— "
            f"应当从 GET /material/options 取，否则会与目录静默漂移"
        )

    # 60% 这个数字只能从后端常量来
    assert "min_quanyou_coverage" in code, (
        "界面上的全友覆盖率底线没有引用后端常量 —— 写死 0.6 会在"
        "后端调整门槛时变成一个说谎的数字"
    )


def test_前端提交时带上了三个偏好字段():
    """类型里有还不够 —— 提交时得真的发出去，否则用户勾了等于没勾。"""
    code = re.sub(
        r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", _generate_module(), flags=re.S)
    )
    for field in ("excluded_categories", "excluded_brands", "preferred_brands"):
        assert field in code, f"「方案生成」模块提交时没有带上 {field}"


@pytest.mark.parametrize("base,label", [("/parse", "户型解析"), ("/generate", "方案生成")])
def test_子页路由与导航入口逐项一致(base: str, label: str):
    """
    ⚠️ **子路由建了但侧栏没有入口 = 用户到不了那一页。**

    这是"拆子目录"这件事最容易漏的一步：路由写好了、页面写好了、
    测试全绿，而从界面上根本点不进去 —— 只有手敲 URL 才看得到。

    反过来也测：导航里写了但路由不存在，点下去是 404。
    两个方向都要一致，所以逐项比对。

    ⚠️ 两个模块都测、**不是顺手**：`/generate` 这次拆完子目录之后，
    `/parse` 就是"已经拆过一遍"的参照物 —— 如果只测新的那个，
    以后有人给 `/parse` 加子页时同样会漏，而没有任何东西拦得住。
    """
    import re as _re

    router_src = _read(REPO / "frontend/src/router/index.ts")
    nav_src = _read(REPO / "frontend/src/config/nav.ts")

    # ── 路由里的子路径 ──
    route_block = _re.search(
        rf"path:\s*'{base}',\s*component:.*?children:\s*\[(.*?)\n  \}},",
        router_src, _re.S)
    assert route_block, f"router/index.ts 里找不到 {base} 的嵌套路由"
    routes = {base}
    for m in _re.finditer(r"path:\s*'([^']*)'", route_block.group(1)):
        # 子路由是**相对**路径（`'plans'`），拼上父路径才是完整的
        sub = m.group(1).strip("/")
        routes.add(f"{base}/{sub}" if sub else base)

    # ── 导航里的子项 ──
    nav_block = _re.search(
        rf"to:\s*'{base}',.*?children:\s*\[(.*?)\n    \],", nav_src, _re.S)
    assert nav_block, f"config/nav.ts 里找不到 {base} 的 children"
    nav_to = set(_re.findall(r"to:\s*'([^']+)'", nav_block.group(1)))

    assert routes == nav_to, (
        f"「{label}」的子页与导航不一致。\n"
        f"  有路由但侧栏点不到: {sorted(routes - nav_to)}\n"
        f"  导航里写了但路由不存在: {sorted(nav_to - routes)}"
    )


def test_模板里不残留Markdown强调语法():
    """
    ⚠️ 2026-09-24 用无头浏览器打开 AC-19 的面板时**看出来的**：
    文案里写了 `**排序偏好**`，而 Vue 模板不会解析 Markdown ——
    页面上真的渲染出四个星号。静态检查和类型检查都看不见这个。

    注释里的星号是正常的（本项目大量用 `**…**` 写强调），所以只扫
    `<template>` 段，并先去掉 HTML 注释。
    """
    bad: list[str] = []
    for path in (REPO / "frontend" / "src").rglob("*.vue"):
        src = path.read_text(encoding="utf-8")
        m = re.search(r"<template>(.*)</template>", src, re.S)
        if not m:
            continue
        body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S)
        for line in body.splitlines():
            if re.search(r"\*\*[^*\n]+\*\*", line):
                bad.append(f"{path.name}: {line.strip()[:60]}")

    assert not bad, (
        "以下模板里残留了 Markdown 的 ** 强调，会被字面渲染成星号：\n  "
        + "\n  ".join(bad)
    )


def test_脚本里给模板用的字符串也不带Markdown强调():
    """
    ⚠️ **上面那条测试有个洞，这条补上。**

    上面只扫 `<template>` 里的**字面文本**。但界面文案还有一条来路：
    定义在 `<script>` 里、用 `{{ }}` 插进模板的字符串 —— 比如
    `KnowledgeView` 的 `PIPELINE[].desc`。那些字符串里的 `**` 同样会
    被字面渲染成星号，而上面那条看不见它们。

    这个洞是真的漏过东西：「知识库管理」页的检索链路说明里写着
    「检索失败返回空结果而**不抛异常**」—— **从那一页写出来那天起
    就一直在页面上渲染成星号**，直到这次补扫才发现。

    做法：先剥掉注释（本项目的注释大量使用 `**`，那是对的、也是刻意的），
    再看剩下的**字符串字面量**里有没有 `**`。
    """
    import re as _re

    bad: list[str] = []
    for path in (REPO / "frontend" / "src").rglob("*.vue"):
        src = path.read_text(encoding="utf-8")
        m = _re.search(r"<script[^>]*>(.*?)</script>", src, _re.S)
        if not m:
            continue
        body = _re.sub(r"/\*.*?\*/", "", m.group(1), flags=_re.S)
        body = _re.sub(r"//[^\n]*", "", body)
        for a, b in _re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", body):
            text = a or b
            if "**" in text:
                bad.append(f"{path.name}: {text.strip()[:70]}")

    assert not bad, (
        "以下**会显示在界面上**的脚本字符串里带了 Markdown 强调 —— "
        "它们经 `{{ }}` 插进模板，同样会被字面渲染成星号：\n  "
        + "\n  ".join(bad)
    )


def test_家具摆放响应的形状与后端逐键一致():
    """
    `GET /layout/{id}/furniture` 的形状。

    ⚠️ 这个接口的**坐标**是前端唯一直接消费的几何数据（`three/furniture.ts`
    拿 `x/y/w/d/rot_deg` 直接建体块）—— 字段名对不上不会报错，
    只会画出 51 个挤在原点的盒子。所以逐键比。
    """
    import asyncio
    import json

    from backend.app.api import store as layout_store
    from backend.app.api.routes import layout_furniture
    from tests.test_api import FULL_LAYOUT

    asyncio.run(layout_store.save("layout_contract_furn", FULL_LAYOUT))
    try:
        body = asyncio.run(layout_furniture("layout_contract_furn"))
    finally:
        layout_store.clear()

    be = body.data
    assert be is not None
    fe_keys = _top_level_keys(_interface_body("FurnitureData"))
    # 后端多两个接口层加的字段（layout_id / plan_id），前端标了可选 ——
    # 比对时把那两个排除，因为它们不是"响应形状"而是"这次请求是谁"
    extra = {"layout_id", "plan_id"}
    assert fe_keys - extra == set(be) - extra, (
        f"FurnitureData 与后端不一致。\n"
        f"  前端独有: {sorted(fe_keys - set(be))}\n"
        f"  后端独有: {sorted(set(be) - fe_keys)}"
    )

    # 一件家具的字段也要逐键比 —— 它是前端真正要读的那一层
    p = next((pl for r in be["rooms"] for pl in r["placements"]), None)
    assert p, "一件家具都没摆下，这条契约测试就没意义了"
    assert _top_level_keys(_interface_body("FurniturePlacement")) == set(p), (
        "FurniturePlacement 的键与后端给的对不上"
    )
    # 这三个是前端建体块时**直接依赖**的，缺一个就画不出来
    assert {"x", "y", "w", "d", "rot_deg", "height_m"} <= set(p)


async def _noop() -> None:
    return None


# ══════════════════════════════════════════════════════════════════
# 2026-09-26 新增的三条接口（知识库两条 + 工作台统计）
# ══════════════════════════════════════════════════════════════════


def test_知识库清单响应的形状与后端逐键一致():
    """
    `GET /knowledge/list`。**字段名对不上不会报错，只会让界面显示 undefined。**

    这三个接口是这轮新加的，而它们的前端类型是我手写的 ——
    手写的类型与后端响应之间**没有任何东西在守着**，
    所以在这里逐键比一次（与 `MaterialOptionsData` 那条同一个理由）。
    """
    import asyncio

    from backend.app.api.routes import knowledge_list
    from backend.app.core import auth

    admin = next(u for u in auth.users() if u.role == auth.ROLE_ADMIN)
    be = asyncio.run(knowledge_list(admin)).data
    fe_keys = _top_level_keys(_interface_body("KnowledgeListData"))
    assert fe_keys == set(be), (
        f"KnowledgeListData 与后端不一致。\n"
        f"  前端独有（永远是 undefined）: {sorted(fe_keys - set(be))}\n"
        f"  后端独有（前端读不到）: {sorted(set(be) - fe_keys)}"
    )
    # 文档那一层的键也要比 —— 它是前端真正渲染的那一层
    assert be["documents"], "库里一篇文档都没有，这条契约测试就没意义了"
    assert _top_level_keys(_interface_body("KnowledgeDocument")) == set(be["documents"][0]), (
        "KnowledgeDocument 的键与后端给的对不上"
    )


def test_知识库上传响应的形状与后端逐键一致():
    import asyncio

    from backend.app.api import routes
    from backend.app.api.schemas import KnowledgeUploadRequest
    from backend.app.core import auth
    from backend.app.services.knowledge import store

    admin = next(u for u in auth.users() if u.role == auth.ROLE_ADMIN)
    doc = "# 契约测试\n\n" + ("这是一段用于契约校验的正文，长到足够切出一块。" * 12)

    # ⚠️ 不写库、不调 Ollama：这条测的是**响应的形状**，不是入库本身。
    orig_embed, orig_upsert = store.embed_texts, store.upsert_chunks
    store.embed_texts = lambda texts: [[0.0] * 8 for _ in texts]
    store.upsert_chunks = lambda chunks, **kw: len(chunks)
    try:
        be = asyncio.run(routes.knowledge_upload(
            KnowledgeUploadRequest(title="契约测试", text=doc), admin)).data
    finally:
        store.embed_texts, store.upsert_chunks = orig_embed, orig_upsert

    fe_keys = _top_level_keys(_interface_body("KnowledgeUploadResult"))
    assert fe_keys == set(be), (
        f"KnowledgeUploadResult 与后端不一致。\n"
        f"  前端独有: {sorted(fe_keys - set(be))}\n"
        f"  后端独有: {sorted(set(be) - fe_keys)}"
    )


def test_工作台统计响应的形状与后端逐键一致():
    """
    ⚠️ 这条同时守着一个**已经在别处踩过的坑**：`null` 与 `0` 的区别。

    `layouts` 在库不通时是 `null`（读不到），不是 0（确实没有）。
    前端类型写的是 `number | null` —— 如果哪天有人"顺手"改成 `number`，
    `vue-tsc` 会报错（好事），但如果同时也把后端改成返回 0，
    就没有任何东西拦得住了。所以这里断言**键存在**，
    并由 `test_knowledge_api.py` 那条断言值为 None。
    """
    import asyncio

    from backend.app.api.routes import dashboard_stats
    from backend.app.core import auth

    admin = next(u for u in auth.users() if u.role == auth.ROLE_ADMIN)
    be = asyncio.run(dashboard_stats(admin)).data
    fe_keys = _top_level_keys(_interface_body("DashboardStatsData"))
    assert fe_keys == set(be), (
        f"DashboardStatsData 与后端不一致。\n"
        f"  前端独有: {sorted(fe_keys - set(be))}\n"
        f"  后端独有: {sorted(set(be) - fe_keys)}"
    )
    assert "by_action" in be and "kb" in be


def test_性能指标的source是对象不是字符串():
    """
    ⚠️ **实测踩过一次**：`MetricsData.source` 我按字符串写，模板里
    `{{ metrics.source }}` —— 而它其实是对象，渲染出来是
    `[object Object]`，**类型检查不会报错**（对象也能插值）。

    所以这里直接对着后端响应断类型：`source` 必须是 dict（对象）。
    前端类型写错时 `vue-tsc` 不一定拦得住，但这条能。
    """
    import asyncio

    from backend.app.api.routes import system_metrics
    from backend.app.core import auth

    admin = next(u for u in auth.users() if u.role == auth.ROLE_ADMIN)
    be = asyncio.run(system_metrics(days=1, user=admin)).data
    assert isinstance(be["source"], dict), (
        f"`source` 是 {type(be['source']).__name__}，而前端类型写的是对象 —— "
        f"两边必须一致，否则界面上会出现 [object Object]"
    )
    fe_body = _interface_body("MetricsData")
    assert "source: {" in fe_body, (
        "前端 MetricsData.source 不再是对象类型 —— 模板里插值会渲染成 [object Object]"
    )


# ══════════════════════════════════════════════════════════════════
# AC-10 地面材质替换（2026-09-26 重定性）
# ══════════════════════════════════════════════════════════════════


def test_地面材质响应的形状与后端逐键一致():
    """
    `GET /layout/{id}/floor-materials` 与 `POST .../floor-material`。

    ⚠️ 这条守着一个**很难自己发现的错**：`cost.unit`。
    我第一版把造价的单位写成了材料的 `unit_hint`（元/㎡），
    于是界面上显示「地面造价 3817.69–5667.09 元/㎡」——
    而那是 13.21㎡ 的**总价**。类型是 `string`，**类型检查不会报错**。
    所以这里断言单位是「元」。
    """
    import asyncio
    import json as _json

    from backend.app.api import routes
    from backend.app.api import store as layout_store
    from backend.app.api.schemas import FloorMaterialRequest

    lay = _json.loads(
        (REPO / "scripts" / "fixtures" / "golden_layout.json").read_text(encoding="utf-8"))
    lid = "layout_contract_floor"
    layout_store._memory[lid] = lay
    try:
        _run = asyncio.run
        _run(routes.set_floor_material(
            lid, FloorMaterialRequest(room_index=0, material_id="DS-FL-201")))
        be = _run(routes.floor_materials(lid)).data

        assert _top_level_keys(_interface_body("FloorMaterialsData")) == set(be), (
            f"FloorMaterialsData 与后端不一致。\n"
            f"  前端独有: {sorted(_top_level_keys(_interface_body('FloorMaterialsData')) - set(be))}\n"
            f"  后端独有: {sorted(set(be) - _top_level_keys(_interface_body('FloorMaterialsData')))}"
        )
        opt = be["eligible"][0]
        assert _top_level_keys(_interface_body("FloorMaterialOption")) == set(opt), (
            "FloorMaterialOption 的键与后端给的对不上"
        )
        sub = be["substitutions"][0]
        assert _top_level_keys(_interface_body("FloorSubstitution")) == set(sub), (
            "FloorSubstitution 的键与后端给的对不上"
        )
        assert sub["cost"]["unit"] == "元", (
            f"造价的单位是 {sub['cost']['unit']!r} —— 它是总额（已乘面积），"
            f"只能是「元」。单价在 material.price_range 里，两处各归各位。"
        )
    finally:
        layout_store.clear()


def test_热区带_room_index_且前端类型也认它():
    """
    AC-10 靠 `room_index` 把"点中的那块地面"映射到"哪间房"。

    ⚠️ 前端类型漏了这个字段的话，`h.room_index` 取到 `undefined`
    —— 而 `undefined != null` 是 false，判断会静默走进另一条分支。
    """
    import asyncio
    import json as _json

    from backend.app.services.render import hotspot_payload, render_plan_for

    lay = _json.loads(
        (REPO / "scripts" / "fixtures" / "golden_layout.json").read_text(encoding="utf-8"))
    scene, plan = render_plan_for(lay)
    be = hotspot_payload(scene, plan.projection)["hotspots"]
    assert "room_index" in be[0], "后端热区没有 room_index"

    body = _interface_body("PlanHotspot")
    assert "room_index" in body, (
        "前端 PlanHotspot 里没有 room_index —— AC-10 点地面选定房间会静默失效"
    )
    # 家具/漫游那些接口也用房间下标，命名必须一致（room_index 不是 roomIndex）
    assert "roomIndex" not in body, (
        "前端写成了 camelCase —— 后端返回的是 snake_case，取不到值"
    )