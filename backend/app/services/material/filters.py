"""
材料偏好过滤 —— AC-19「自由选项组合」的材料侧。

═══════════════════════════════════════════════════════════════════
为什么是「偏好/排除」而不是「勾选具体商品」
═══════════════════════════════════════════════════════════════════
AC-19 的原文是「用户勾选风格/材料/预算后实时生成方案」。把它读成
「用户从 28 件商品里逐件挑」是能实现的，但会让 AC-18 变成一句空话：

    AC-18 要求「方案中全友产品覆盖率 ≥ 60%」—— 这是**系统**的验收指标。
    用户挑了一堆竞品，系统要么违约，要么无视用户的选择把东西换掉。
    两条路都是界面在说谎。

所以这里的语义是：**用户表达偏好与排除，系统负责在剩下的池子里满足
AC-18**。用户说「不要瓷砖」「墙面别用竞品」，A-05 就在过滤后的候选里选，
覆盖率仍由代码兜底。谁负责什么，分得清楚。

═══════════════════════════════════════════════════════════════════
为什么校验必须在**请求期**做
═══════════════════════════════════════════════════════════════════
「排除到候选池为空」这件事，如果不在这里拦，会在两个地方以两种形态炸：

  · A-05 的池子为空 → `material_agent.py` 抛 ValueError → 整个方案生成失败，
    而此时已经烧掉了 A-02 诊断 + 九路并发的 40 秒，以及一次付费额度；
  · 只有**某个档位**的池子为空（经济档某品类全是竞品、被排除了）→
    更糟：那套方案照出，只是那个品类**静默少了一项**，
    用户看到的是一份"看起来正常、少了一行"的清单。

第二种正是本项目最防的「看起来合理的错误」。所以校验一次性把
「品类 × 档位」的组合全部试一遍，在提交前就把矛盾说清楚。

═══════════════════════════════════════════════════════════════════
`quanyou_priority` 曾经是个死开关
═══════════════════════════════════════════════════════════════════
2026-09-24 发现：前端有这个复选框，API 与 state 都接了线，
**但没有任何 Agent 读它** —— 全友加分与 AC-18 兜底永远无条件执行。
界面在承诺一个后端不认的选项，与 `3d492c5` 修的是同一类缺陷。

现在的语义（写清楚，因为它容易被误读）：

  · 开关控制的是**软偏好** —— 同等条件下是否给全友加分（`catalog.py`
    的 `QUANYOU_PREFERENCE_BONUS`），以及提示词里那句"优先选全友"。
  · **AC-18 的 60% 保底不受这个开关影响。** 它是平台级要求，不是用户偏好；
    能被用户关掉的验收指标是测不了的。关掉开关后，覆盖率会从接近 100%
    回落到贴着 60% 的线，`auto_substitutions` 条数上升 —— 这才是这个
    开关**可观测、可断言**的效果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import catalog

__all__ = [
    "MaterialFilters", "FilterProblem", "applicable_categories",
    "validate_filters", "QUANYOU_BRAND",
]

#: 全友在目录里的品牌名。
#:
#: 写死在这里是有意的：AC-18 判断的是「这件商品是不是我们自己的」，
#: 属于平台事实，不该靠"某个字符串出现在用户输入里"来推断。
QUANYOU_BRAND = "全友"

#: 品类适用性：**只有缺了对应房间就毫无意义的品类**才登记在这里。
#:
#: 目前只有卫浴洁具 —— 没有卫生间的户型买一套洁具是纯噪音。
#: 这不是新发明：`capabilities.py` 给 `select_materials` 定门槛时就写了
#: 「选材要知道有哪些空间才能判断该选哪些品类（有卫生间才需要瓷砖与洁具）」，
#: 只是这句意图一直没实现（A-05 无条件遍历全部 7 个品类）。
#:
#: ⚠️ 「瓷砖」被认真考虑过，最终**没有**登记 —— 值得写下理由：
#:    瓷砖在国内住宅里大量用于客厅/餐厅**地面**，不只是厨卫墙面。
#:    拿它当条件，会在"没识别出厨房"的户型上把一个完全合理的选择误删掉。
#:    误删比多买一项更难发现：多买看得见，少买只会让清单短一行。
_CATEGORY_ROOMS: dict[str, tuple[str, ...]] = {
    "sanitary": ("卫生间", "卫浴", "洗手间", "浴室", "bathroom", "toilet"),
}


def applicable_categories(
    room_names: Iterable[str],
    *,
    path: str | None = None,
) -> tuple[str, ...]:
    """
    给定房间名，返回**本次选材该考虑**的品类。

    ⚠️ 一个房间名都没拿到时返回**全部品类**，不做删减 ——
    缺数据不等于"没有那个房间"。这与 `capabilities.py` 的立场一致
    （数据驱动，而不是模式驱动）。
    """
    # ⚠️ 大小写不敏感：房间名来自视觉模型，`Bathroom` / `BATHROOM` / `bathroom`
    #    都出现过（降级路径下英文字面量尤其常见，见 schemas/layout.py 里
    #    `_accept_flat_string_list` 的实测记录）。按大小写敏感匹配会**静默漏判** ——
    #    漏判的后果是没有卫生间也去买洁具，看不出错。
    names = [str(n).casefold() for n in room_names if n]
    out: list[str] = []
    for key in (c["key"] for c in catalog.categories(path)):
        need = _CATEGORY_ROOMS.get(key)
        if not need:
            out.append(key)
            continue
        if not names or any(
            kw.casefold() in name for kw in need for name in names
        ):
            out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class MaterialFilters:
    """
    一次生成请求里的材料偏好。

    `frozen` 是刻意的：它会经 fan-out 分发给三个并行分支，
    任何一处就地修改都会造成分支间行为不一致，且极难查。
    """

    excluded_categories: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()
    preferred_brands: tuple[str, ...] = ()
    quanyou_priority: bool = True

    # ── 构造 ──────────────────────────────────────────────

    @classmethod
    def from_payload(cls, raw: dict[str, Any] | None) -> MaterialFilters:
        """
        从请求体（或 state 里的 dict）构造。

        **去重 + 排序**：同一组过滤条件必须得到同一个对象，
        否则候选池的遍历顺序会随用户输入顺序漂移，测试就没法断言。
        """
        raw = raw or {}

        def _clean(values: Any) -> tuple[str, ...]:
            if isinstance(values, str):        # 容忍前端传单个字符串
                values = [values]
            seen = {str(v).strip() for v in (values or []) if str(v).strip()}
            return tuple(sorted(seen))

        return cls(
            excluded_categories=_clean(raw.get("excluded_categories")),
            excluded_brands=_clean(raw.get("excluded_brands")),
            preferred_brands=_clean(raw.get("preferred_brands")),
            quanyou_priority=bool(raw.get("quanyou_priority", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """进 state / 进 API 响应的形态。必须是纯 JSON 可序列化的 ——
        state 会随 checkpointer 落盘，放 dataclass 会直接序列化失败。"""
        return {
            "excluded_categories": list(self.excluded_categories),
            "excluded_brands": list(self.excluded_brands),
            "preferred_brands": list(self.preferred_brands),
            "quanyou_priority": self.quanyou_priority,
        }

    # ── 判定 ──────────────────────────────────────────────

    def allows(self, product: catalog.Product) -> bool:
        """这件商品能不能进候选池。"""
        if product.category in self.excluded_categories:
            return False
        return product.brand not in self.excluded_brands

    @property
    def quanyou_bonus(self) -> float:
        """软偏好关掉时，全友加分归零（AC-18 保底不受影响，见模块说明）。"""
        return catalog.QUANYOU_PREFERENCE_BONUS if self.quanyou_priority else 0.0

    def brand_bonus(self, brand: str) -> float:
        return catalog.PREFERRED_BRAND_BONUS if brand in self.preferred_brands else 0.0

    def candidate_kwargs(
        self, *, only_categories: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """
        翻成 `catalog.candidates()` 的关键字参数。

        **只在这个地方做这一次翻译** —— A-05 的初始选材、AC-18 的兜底替换、
        请求期的池子校验，三处必须用同一套语义。各写各的转译，
        迟早会出现"初始选择守规矩、补足环节不守"的不一致。
        """
        kwargs: dict[str, Any] = {
            "exclude_categories": self.excluded_categories,
            "exclude_brands": self.excluded_brands,
            "preferred_brands": self.preferred_brands,
            "quanyou_bonus": self.quanyou_bonus,
        }
        if only_categories is not None:
            kwargs["only_categories"] = tuple(only_categories)
        return kwargs

    def basis(self) -> list[str]:
        """把过滤条件翻成一句句人话，用于在产物里说明"这份清单是怎么来的"。"""
        out: list[str] = []
        if self.excluded_categories:
            labels = {c["key"]: c["label"] for c in catalog.categories()}
            out.append(
                "按用户要求排除品类："
                + "、".join(labels.get(c, c) for c in self.excluded_categories)
            )
        if self.excluded_brands:
            out.append("按用户要求排除品牌：" + "、".join(self.excluded_brands))
        if self.preferred_brands:
            out.append("按用户要求优先品牌：" + "、".join(self.preferred_brands))
        if not self.quanyou_priority:
            out.append("用户关闭了全友优先（平台保底 60% 覆盖率不受影响）")
        return out


#: 默认（无任何偏好）—— 与改动前的行为完全一致。
DEFAULT_FILTERS = MaterialFilters()


@dataclass(frozen=True)
class FilterProblem:
    """
    一条过滤条件的问题。

    带 `code` 是为了让测试断言"是哪一类问题"，而不是去匹配一句中文 ——
    文案会改，判据不该跟着改。
    """

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def validate_filters(
    filters: MaterialFilters,
    *,
    grades: Iterable[str],
    room_names: Iterable[str] = (),
    path: str | None = None,
) -> list[FilterProblem]:
    """
    检查一组过滤条件是否**可满足**。返回问题列表，空列表 = 通过。

    调用方（`routes.py`）在提交任务前调用它，有问
    题就 4001 拒掉 —— 见模块说明里"为什么必须在请求期做"。
    """
    problems: list[FilterProblem] = []

    cat_keys = {str(c["key"]) for c in catalog.categories(path)}
    cat_labels = {str(c["key"]): str(c["label"]) for c in catalog.categories(path)}
    all_brands = {p.brand for p in catalog.all_products(path)}

    # ── 取值合法性 ────────────────────────────────────────
    unknown_cats = [c for c in filters.excluded_categories if c not in cat_keys]
    if unknown_cats:
        problems.append(FilterProblem(
            "unknown_category",
            f"不认识的品类：{unknown_cats}",
        ))

    touched_brands = sorted(set(filters.excluded_brands) | set(filters.preferred_brands))
    unknown_brands = [b for b in touched_brands if b not in all_brands]
    if unknown_brands:
        problems.append(FilterProblem(
            "unknown_brand",
            f"不认识的品牌：{unknown_brands}",
        ))

    # ── 自相矛盾 ──────────────────────────────────────────
    both = sorted(set(filters.excluded_brands) & set(filters.preferred_brands))
    if both:
        problems.append(FilterProblem(
            "contradictory_brand",
            f"同一品牌不能既排除又优先：{both}",
        ))

    # ── 与平台保底冲突 ────────────────────────────────────
    if QUANYOU_BRAND in filters.excluded_brands:
        problems.append(FilterProblem(
            "quanyou_excluded",
            f"不能排除「{QUANYOU_BRAND}」：AC-18 要求方案中全友产品覆盖率不低于 "
            f"{catalog.MIN_QUANYOU_COVERAGE:.0%}，这是平台级要求，不是可选项。"
            f"如果你希望少推全友，请改用 preferred_brands 指定其它偏好品牌。",
        ))

    # ── 排除到无品类可选 ──────────────────────────────────
    applicable = applicable_categories(room_names, path=path)
    remaining = [c for c in applicable if c not in filters.excluded_categories]
    if not remaining:
        problems.append(FilterProblem(
            "no_category_left",
            f"排除后没有剩下任何可选的品类。本户型涉及的品类有 "
            f"{[cat_labels.get(c, c) for c in applicable]}，至少要保留一项。",
        ))
        # 后面按品类试池子已经没有意义了
        return problems

    # ── 候选池为空（逐个档位试）────────────────────────────
    # 只试涉及到的档位：不同档位的商品池不同，经济档可能全被排除，
    # 高端档却还有货 —— 这种"只有一套方案静默少一项"是最难发现的。
    empty_by_grade: dict[str, list[str]] = {}
    for grade in dict.fromkeys(grades):
        pool = catalog.candidates(
            grade=grade, path=path,
            **filters.candidate_kwargs(only_categories=remaining),
        )
        empty = [cat_labels.get(c, c) for c in remaining if not pool.get(c)]
        if empty:
            empty_by_grade[str(grade)] = empty

    if empty_by_grade:
        detail = "；".join(
            f"{g} 档：{'、'.join(cats)}" for g, cats in empty_by_grade.items()
        )
        problems.append(FilterProblem(
            "empty_pool",
            f"以下品类在排除后没有剩下任何候选商品 —— {detail}。"
            f"请减少排除项，或换一个预算档位。",
        ))

    return problems
