from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.schemas.offer import Offer, SearchRequest
from app.search.ranking import rank
from app.suppliers.base import SupplierAdapter, SupplierError

logger = logging.getLogger("search")


@dataclass
class SearchResult:
    offers: list[Offer]
    suppliers_queried: list[str]
    suppliers_failed: list[str]


def _dedup_key(offer: Offer) -> tuple[str, str]:
    """Two offers are treated as 'the same property' if their normalized
    name + location match. Good enough for a prototype; a real system
    would use a property-matching service (fuzzy match / shared IDs)."""
    return (offer.property_name.strip().lower(), offer.location.strip().lower())


def _dedupe(offers: list[Offer]) -> list[Offer]:
    """When two suppliers list the same property, keep the cheaper offer."""
    best_by_key: dict[tuple[str, str], Offer] = {}
    for offer in offers:
        key = _dedup_key(offer)
        current = best_by_key.get(key)
        if current is None or offer.total_price < current.total_price:
            best_by_key[key] = offer
    return list(best_by_key.values())


async def search_hotels(
    request: SearchRequest,
    adapters: list[SupplierAdapter],
    request_id: str,
) -> SearchResult:
    async def _safe_search(adapter: SupplierAdapter) -> tuple[str, list[Offer] | Exception]:
        try:
            offers = await adapter.search(request)
            return adapter.supplier_id, offers
        except SupplierError as exc:
            logger.warning(
                "supplier search failed",
                extra={"request_id": request_id, "supplier": adapter.supplier_id, "error": str(exc)},
            )
            return adapter.supplier_id, exc

    results = await asyncio.gather(*(_safe_search(a) for a in adapters))

    all_offers: list[Offer] = []
    suppliers_queried: list[str] = []
    suppliers_failed: list[str] = []

    for supplier_id, outcome in results:
        suppliers_queried.append(supplier_id)
        if isinstance(outcome, Exception):
            suppliers_failed.append(supplier_id)
            continue
        all_offers.extend(outcome)

    deduped = _dedupe(all_offers)
    ranked = rank(deduped)

    logger.info(
        "search complete",
        extra={
            "request_id": request_id,
            "suppliers_queried": suppliers_queried,
            "suppliers_failed": suppliers_failed,
            "offer_count": len(ranked),
        },
    )

    return SearchResult(offers=ranked, suppliers_queried=suppliers_queried, suppliers_failed=suppliers_failed)
