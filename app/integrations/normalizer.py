"""
Normalização: converte a resposta bruta e volátil da API do Mercado Livre
numa estrutura interna estável (ItemSnapshot). Os módulos de análise
dependem SÓ do snapshot, nunca do JSON cru — assim, se o ML mudar um campo,
mexemos em um lugar só.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ItemSnapshot:
    mlb_id: str
    title: str
    price: Optional[float]
    currency: Optional[str]
    category_id: Optional[str]
    listing_type: Optional[str]        # gold_pro (Premium), gold_special (Clássico)...
    condition: Optional[str]           # new / used
    sold_quantity: Optional[int]
    available_quantity: Optional[int]
    free_shipping: bool
    logistic_type: Optional[str]       # fulfillment (Full), cross_docking, drop_off...
    seller_id: Optional[int]
    thumbnail: Optional[str]
    picture_urls: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    permalink: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def picture_count(self) -> int:
        return len(self.picture_urls)


def snapshot_from_item(item: dict) -> ItemSnapshot:
    shipping = item.get("shipping") or {}
    attributes = {
        a.get("id"): a.get("value_name")
        for a in item.get("attributes", [])
        if a.get("id")
    }
    pictures = [
        p.get("secure_url") or p.get("url")
        for p in item.get("pictures", [])
        if p.get("secure_url") or p.get("url")
    ]
    return ItemSnapshot(
        mlb_id=item.get("id", ""),
        title=item.get("title", ""),
        price=item.get("price"),
        currency=item.get("currency_id"),
        category_id=item.get("category_id"),
        listing_type=item.get("listing_type_id"),
        condition=item.get("condition"),
        sold_quantity=item.get("sold_quantity"),
        available_quantity=item.get("available_quantity"),
        free_shipping=bool(shipping.get("free_shipping")),
        logistic_type=shipping.get("logistic_type"),
        seller_id=item.get("seller_id"),
        thumbnail=item.get("thumbnail"),
        picture_urls=pictures,
        attributes=attributes,
        permalink=item.get("permalink"),
        raw=item,
    )


def snapshot_from_search_result(result: dict) -> ItemSnapshot:
    """
    Itens vindos da busca têm menos campos que /items/{id}. Este snapshot
    'leve' é suficiente para benchmark e posicionamento.
    """
    shipping = result.get("shipping") or {}
    return ItemSnapshot(
        mlb_id=result.get("id", ""),
        title=result.get("title", ""),
        price=result.get("price"),
        currency=result.get("currency_id"),
        category_id=result.get("category_id"),
        listing_type=result.get("listing_type_id"),
        condition=result.get("condition"),
        sold_quantity=result.get("sold_quantity"),
        available_quantity=result.get("available_quantity"),
        free_shipping=bool(shipping.get("free_shipping")),
        logistic_type=shipping.get("logistic_type"),
        seller_id=result.get("seller", {}).get("id") if result.get("seller") else None,
        thumbnail=result.get("thumbnail"),
        picture_urls=[result["thumbnail"]] if result.get("thumbnail") else [],
        attributes={},
        permalink=result.get("permalink"),
        raw=result,
    )
