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

因此目录里刻意保持每个品类 2 个全友 + 2 个竞品（整体 50%）。
随机选择期望覆盖率就是 50%，**低于 60% 的线**；要过线就必须真的优先选全友。
这样这条 AC 才是一道真的门槛，而不是一句自我感觉良好的声明。
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
) -> ScoredProduct:
    """
    给一件商品打匹配分。**纯函数，确定性。**

    评分构成刻意保持可解释 —— 每个加分项都写进 reasons，
    便于下游把它当作"为什么推荐它"的依据，而不是让模型凭空编理由。
    """
    score = 0.0
    reasons: list[str] = []

    if product.is_quanyou:
        score += QUANYOU_PREFERENCE_BONUS
        reasons.append("全友自有产品")

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
) -> dict[str, list[ScoredProduct]]:
    """
    按品类给出候选商品，每类按匹配分从高到低排。

    `grade` 是**硬过滤**：经济档方案不该出现高端岩板。
    其余维度只影响排序，不做排除 —— 保住候选集的多样性，
    让模型有真实的选择空间（而不是只剩一个选项，那样"选择"就没意义了）。
    """
    tags = requirement_tags(requirements)
    eco = (requirements or {}).get("eco_level")

    out: dict[str, list[ScoredProduct]] = {}
    for cat in categories(path):
        key = cat["key"]
        pool = [
            p for p in all_products(path)
            if p.category == key and grade in p.budget_grade
        ]
        scored = [
            score_product(p, style=style, tags=tags, eco_level=eco)
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
) -> Product | None:
    """
    在同品类里找一件可替换的全友产品，找不到返回 None。

    用于 AC-18 的**代码兜底**：模型选的全友太少时，由代码换掉——
    而不是在提示词里反复叮嘱"请优先全友"然后祈祷它照做。
    这与 A-03 剔除幻觉房间、A-04 用类型系统挡金额是同一条纪律。
    """
    pool = candidates(grade=grade, style=style, requirements=requirements, path=path)
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
    "MIN_QUANYOU_COVERAGE", "QUANYOU_PREFERENCE_BONUS",
]
