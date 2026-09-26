"""
材料目录检索 —— 确定性优先，不依赖任何模型或网络。

═══════════════════════════════════════════════════════════════════
为什么这里的检索**不用向量**
═══════════════════════════════════════════════════════════════════

需求文档把材料选型 Agent 的技术实现写成「RAG + 全友产品知识库」，听起来
就该上 embedding + Chroma。但实际看数据形态就会发现：**用不上。**

用户的偏好是**结构化**的 —— `requirements` 里是 `has_children` / `pets` /
`eco_level` / `smart_home` 这些布尔与枚举字段，商品侧也打了 `suitable_for` /
`eco_level` / `style_fit` / `budget_grade` 这些标签。两边都是标签，
匹配就是集合运算，不需要把语义压成向量再去算余弦相似度。

用向量的代价却是实打实的：多一个 Ollama 依赖、多一次网络往返、
结果不确定、无法断言"给定输入必然得到给定输出"。
**在结构化能解决的地方引入向量，是用可靠性换一个用不上的能力。**

所以本模块是确定性的。真正的语义检索留给 A-06 避坑审查 —— 那边的语料是
装修规范与避坑文档，自由文本、无标签，那才是 RAG 不可替代的地方
（也在需求文档路线图 M4 的范围内）。

═══════════════════════════════════════════════════════════════════
AC-18 是怎么被"变得可测"的
═══════════════════════════════════════════════════════════════════

AC-18 要求「方案中全友产品覆盖率 ≥ 60%」。这条指标要成立，
前提是**候选集中必须有竞品** —— 若全是全友，覆盖率恒为 100%，
测出来也证明不了任何事。

整体上目录是 28 件 = 14 全友 + 14 竞品（50%）。但**按档位切开之后并不均匀**，
而方案是按档位生成的，所以要量的不是整体比例，是每个档位下的期望覆盖率。

2026-09-24 实测（`tests/test_material_filters.py` 里钉住了这组数字）：

    档位       随机选择下的期望覆盖率    门槛（60%）是否在起作用
    economy    59.5%                     在起作用（擦着线）
    medium     52.4%                     在起作用
    high       66.7%                     ⚠️ **不起作用 —— 随机就过线**

原因是 high 档有 3 个品类（墙面涂料 / 卫浴洁具 / 灯具）在目录里**只有
一件全友商品、没有竞品**，那三项覆盖率恒为 100%，把整体抬过了线。

⚠️ 这条实测推翻了下文原先的一句断言（「随机选只有 50%，过不了 60% 的线」）——
它对 economy/medium 成立，对 high **不成立**。留着这个记录是因为它有个
实际含义：**AC-18 的代码兜底对高端档方案是空转的**，那套方案本来就在线上方。
所以"AC-18 通过了"这个结论，真正被考验的是经济档与中档两套方案。
把数字钉住（而不是把种子数据改得更好看）是刻意的：
改数据能让断言变漂亮，但那是拿结论去凑指标。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_CATALOG = _ROOT / "seed_data" / "material_catalog.json"

#: AC-18 的门槛。放在这里而不是散在 Agent 里，便于测试直接引用。
MIN_QUANYOU_COVERAGE = 0.60

#: 业务加分：这是全友的平台，优先推荐自有产品是**明确的业务规则**，
#: 不是假装中立。把它写在明处，比藏在提示词里诚实。
QUANYOU_PREFERENCE_BONUS = 1.5

#: 用户指定偏好品牌时的加分（AC-19）。
#:
#: ⚠️ **高于**全友加分（1.5），这是 2026-09-24 改的，理由值得写下来。
#:
#: 初版定的是 1.0（低于全友），当时的推理是"用户偏好是倾向，平台规则是
#: 要求，冲突时平台赢"。但写完测试就发现那个推理有个后果：
#: 在默认 `quanyou_priority=True` 下，1.0 永远翻不过 1.5，
#: **用户明确指定的偏好品牌几乎不生效** —— 那就又是一个死开关，
#: 和刚修掉的 `quanyou_priority` 是同一种毛病（界面承诺了，系统不认）。
#:
#: 正确的分工不是"谁的分高"，而是**分层**：
#:   · 排序层 —— 用户**明确**说的偏好应当压过平台的**默认**倾向，
#:     所以 2.0 > 1.5（2.0 与风格匹配同分，同分时按 id 升序，仍然确定）；
#:   · 底线层 —— AC-18 的 60% 是硬性的，由代码兜底守，与加分无关。
#:
#: 于是"用户偏好竞品"的真实结果是：偏好在底线上方生效，超出的部分被换回，
#: 每一次换回都记进 `auto_substitutions` 并在界面上说明。
#: 这比"用户说了但系统装作没听见"诚实。
PREFERRED_BRAND_BONUS = 2.0

#: requirements 里的布尔字段 -> 商品 tag
_REQUIREMENT_TAGS: dict[str, str] = {
    "has_elderly": "elderly",
    "has_children": "children",
    "pets": "pets",
}


class CatalogError(RuntimeError):
    """材料目录缺失或格式不对。属于部署问题，不是用户输入问题。"""


@dataclass(frozen=True)
class Product:
    """一件材料商品。字段与设计稿的 `quanyou_products` / `material_price_map` 对齐。"""

    id: str
    name: str
    brand: str
    is_quanyou: bool
    category: str
    spec: str
    price_range: tuple[float, float]
    eco_level: str
    style_fit: tuple[str, ...] = ()
    budget_grade: tuple[str, ...] = ()
    suitable_for: tuple[str, ...] = ()
    note: str = ""
    search_url: str = ""
    #: 这件材料**能铺在哪儿**：`floor` / `wall` / `both`。
    #:
    #: ⚠️ 2026-09-26 为「矢量图上换地面材质」（AC-10 重定性）补的。
    #: 没有它就分不出 `QY-TL-102`（釉面**内墙**砖）不能铺地 ——
    #: 只按 `category` 筛会把内墙砖当成地面材料推荐出来，
    #: 而那是一个**看着合理、实际错误**的建议。
    surface: str = ""
    @property
    def unit_hint(self) -> str:
        """计价单位。**从品类来**，不是每件商品各写一遍。"""
        return {
            "floor": "元/㎡", "tile": "元/㎡", "paint": "元/㎡",
            "door": "元/樘", "sanitary": "元/套", "cabinet": "元/㎡",
            "lighting": "元/套",
        }.get(self.category, "")

    #: 材料的**代表色**（`#RRGGBB`），用来在矢量图上把这块地面画出来。
    #: ⚠️ 它是代表色、不是效果图 —— 真实纹理要看实物或官网。
    swatch: str = ""

    @property
    def price_min(self) -> float:
        return self.price_range[0]

    @property
    def price_max(self) -> float:
        return self.price_range[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "brand": self.brand,
            "is_quanyou": self.is_quanyou, "category": self.category,
            "spec": self.spec, "price_range": list(self.price_range),
            "eco_level": self.eco_level, "style_fit": list(self.style_fit),
            "budget_grade": list(self.budget_grade),
            "suitable_for": list(self.suitable_for),
            "note": self.note, "search_url": self.search_url,
        }


@dataclass
class ScoredProduct:
    """带匹配度评分的候选。评分过程完全确定，可断言。"""

    product: Product
    score: float
    reasons: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════════════════════════


@lru_cache(maxsize=4)
def load_catalog(path: str | None = None) -> dict[str, Any]:
    """读取材料目录。带缓存 —— 每个方案分支都会查一次。"""
    target = Path(path) if path else _DEFAULT_CATALOG
    if not target.exists():
        raise CatalogError(f"材料目录不存在: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CatalogError(f"材料目录不是合法 JSON: {target} —— {e}") from e

    if not isinstance(data.get("products"), list) or not data["products"]:
        raise CatalogError(f"材料目录缺少 products 数组: {target}")
    if not isinstance(data.get("categories"), list) or not data["categories"]:
        raise CatalogError(f"材料目录缺少 categories 数组: {target}")
    return data


def _to_product(raw: dict[str, Any]) -> Product:
    pr = raw.get("price_range") or [0, 0]
    return Product(
        id=str(raw["id"]),
        name=str(raw.get("name", "")),
        brand=str(raw.get("brand", "")),
        is_quanyou=bool(raw.get("is_quanyou")),
        category=str(raw.get("category", "")),
        spec=str(raw.get("spec", "")),
        price_range=(float(pr[0]), float(pr[1])),
        eco_level=str(raw.get("eco_level", "")),
        style_fit=tuple(raw.get("style_fit") or ()),
        budget_grade=tuple(raw.get("budget_grade") or ()),
        suitable_for=tuple(raw.get("suitable_for") or ()),
        note=str(raw.get("note", "")),
        search_url=str(raw.get("search_url", "")),
        surface=str(raw.get("surface", "")),
        swatch=str(raw.get("swatch", "")),
    )


def all_products(path: str | None = None) -> list[Product]:
    return [_to_product(p) for p in load_catalog(path)["products"]]


def by_id(path: str | None = None) -> dict[str, Product]:
    return {p.id: p for p in all_products(path)}


def categories(path: str | None = None) -> list[dict[str, Any]]:
    """按 order 排好的品类列表。"""
    cats = load_catalog(path)["categories"]
    return sorted(cats, key=lambda c: c.get("order", 99))


def disclaimer(path: str | None = None) -> str:
    return str(load_catalog(path).get("_meta", {}).get("disclaimer", ""))


def catalog_version(path: str | None = None) -> str:
    return str(load_catalog(path).get("_meta", {}).get("catalog_version", "unknown"))


# ══════════════════════════════════════════════════════════════════
# 匹配评分
# ══════════════════════════════════════════════════════════════════


def requirement_tags(requirements: dict[str, Any] | None) -> list[str]:
    """把 requirements 里的布尔字段翻成商品标签。"""
    req = requirements or {}
    return [tag for key, tag in _REQUIREMENT_TAGS.items() if req.get(key)]


def score_product(
    product: Product,
    *,
    style: str | None = None,
    tags: Iterable[str] = (),
    eco_level: str | None = None,
    quanyou_bonus: float = QUANYOU_PREFERENCE_BONUS,
    brand_bonus: float = 0.0,
) -> ScoredProduct:
    """
    给一件商品打匹配分。**纯函数，确定性。**

    评分构成刻意保持可解释 —— 每个加分项都写进 reasons，
    便于下游把它当作"为什么推荐它"的依据，而不是让模型凭空编理由。

    `quanyou_bonus` / `brand_bonus` 由 AC-19 的过滤条件算出
    （见 `filters.py`）：关闭全友优先时前者为 0，指定偏好品牌时后者为
    `PREFERRED_BRAND_BONUS`。默认值与改动前一致。
    """
    score = 0.0
    reasons: list[str] = []

    if product.is_quanyou and quanyou_bonus:
        score += quanyou_bonus
        reasons.append("全友自有产品")

    if brand_bonus:
        score += brand_bonus
        reasons.append(f"用户指定偏好品牌（{product.brand}）")

    if style and style in product.style_fit:
        score += 2.0
        reasons.append(f"风格匹配（{style}）")

    hit_tags = [t for t in tags if t in product.suitable_for]
    if hit_tags:
        score += float(len(hit_tags))
        reasons.append(f"适配需求（{'/'.join(hit_tags)}）")

    if eco_level and product.eco_level == eco_level:
        score += 1.0
        reasons.append(f"环保等级 {eco_level}")

    return ScoredProduct(product=product, score=score, reasons=reasons)


def candidates(
    *,
    grade: str,
    style: str | None = None,
    requirements: dict[str, Any] | None = None,
    path: str | None = None,
    only_categories: Iterable[str] | None = None,
    exclude_categories: Iterable[str] = (),
    exclude_brands: Iterable[str] = (),
    preferred_brands: Iterable[str] = (),
    quanyou_bonus: float = QUANYOU_PREFERENCE_BONUS,
) -> dict[str, list[ScoredProduct]]:
    """
    按品类给出候选商品，每类按匹配分从高到低排。

    **两道硬过滤**：
      · `grade` —— 经济档方案不该出现高端岩板；
      · `only_categories` —— 本次该考虑哪些品类（AC-19，由 `filters.py`
        按户型房间算出来：没有卫生间的户型不必买洁具）。
        ⚠️ 参数名不叫 `categories`，因为那会遮蔽同名的模块函数
        （本函数内部正要调用它取品类列表）。

    **三类偏好**（`exclude_*` / `preferred_brands`）：用户可以在请求里
    排除品类或品牌、指定偏好品牌。排除是**真的排除**，不是降权 ——
    用户说"不要这家"，东西还留在清单里只是排后面，那叫没听懂。

    ⚠️ 2026-09-24 之前这里写的是「其余维度只影响排序，不做排除」。
    那句话本身没错（保住候选集多样性、让模型有真实选择空间），
    但被 AC-19 的排除语义取代了：**排除必须是硬的，偏好才是软的**。
    风格 / 需求标签 / 环保等级仍然只影响排序 —— 那部分没变。

    想拿到"排除后的候选池是否还够用"的判断，用
    `filters.validate_filters()` —— 它在**请求期**跑，不必等到这里为空。
    """
    tags = requirement_tags(requirements)
    eco = (requirements or {}).get("eco_level")

    allowed_cats = set(only_categories) if only_categories is not None else None
    banned_cats = set(exclude_categories)
    banned_brands = set(exclude_brands)
    liked_brands = set(preferred_brands)

    out: dict[str, list[ScoredProduct]] = {}
    for cat in categories(path):
        key = cat["key"]
        if allowed_cats is not None and key not in allowed_cats:
            continue
        if key in banned_cats:
            # 仍然返回一个空列表 —— 调用方按"这个品类没有候选"处理，
            # 与"型号不匹配"走同一条分支，少一处特判。
            out[key] = []
            continue
        pool = [
            p for p in all_products(path)
            if p.category == key
            and grade in p.budget_grade
            and p.brand not in banned_brands
        ]
        scored = [
            score_product(
                p, style=style, tags=tags, eco_level=eco,
                quanyou_bonus=quanyou_bonus,
                brand_bonus=PREFERRED_BRAND_BONUS if p.brand in liked_brands else 0.0,
            )
            for p in pool
        ]
        # 评分降序；同分时**按 id 升序** —— 保证顺序确定，否则同分商品的
        # 排列会随字典顺序漂移，测试就没法断言了。
        scored.sort(key=lambda sp: (-sp.score, sp.product.id))
        out[key] = scored
    return out


# ══════════════════════════════════════════════════════════════════
# AC-18：全友覆盖率
# ══════════════════════════════════════════════════════════════════


def coverage(product_ids: Iterable[str], path: str | None = None) -> float:
    """
    计算一组商品里全友产品的占比。

    **由代码算，不由模型自报。** AC-18 是一条可验证的指标，
    让模型自己说"我推荐了 80% 全友"是没有意义的。
    """
    index = by_id(path)
    ids = [i for i in product_ids if i in index]
    if not ids:
        return 0.0
    return sum(1 for i in ids if index[i].is_quanyou) / len(ids)


def find_quanyou_alternative(
    category: str,
    *,
    grade: str,
    exclude: Iterable[str] = (),
    style: str | None = None,
    requirements: dict[str, Any] | None = None,
    path: str | None = None,
    exclude_brands: Iterable[str] = (),
) -> Product | None:
    """
    在同品类里找一件可替换的全友产品，找不到返回 None。

    用于 AC-18 的**代码兜底**：模型选的全友太少时，由代码换掉——
    而不是在提示词里反复叮嘱"请优先全友"然后祈祷它照做。
    这与 A-03 剔除幻觉房间、A-04 用类型系统挡金额是同一条纪律。

    ⚠️ `exclude_brands` 必须从调用方一路传进来。兜底替换是**代码发起**的，
    很容易忘了它同样受用户偏好约束 —— 那就会出现"用户排除了某品牌，
    清单里却冒出一个该品牌的商品（由兜底换进来）"这种最难查的不一致：
    初始选择守规矩，补足环节不守。
    """
    pool = candidates(
        grade=grade, style=style, requirements=requirements, path=path,
        exclude_brands=exclude_brands,
    )
    excluded = set(exclude)
    for sp in pool.get(category, []):
        if sp.product.is_quanyou and sp.product.id not in excluded:
            return sp.product
    return None


__all__ = [
    "Product", "ScoredProduct", "CatalogError",
    "load_catalog", "all_products", "by_id", "categories",
    "disclaimer", "catalog_version",
    "requirement_tags", "score_product", "candidates",
    "coverage", "find_quanyou_alternative",
    "MIN_QUANYOU_COVERAGE", "QUANYOU_PREFERENCE_BONUS", "PREFERRED_BRAND_BONUS",
]
