from __future__ import annotations

from app.schemas.offer import AvailabilityStatus, Offer

_AVAILABILITY_WEIGHT = {
    AvailabilityStatus.AVAILABLE: 1.0,
    AvailabilityStatus.LIMITED: 0.6,
    AvailabilityStatus.UNAVAILABLE: 0.0,
}


def score(offer: Offer, *, min_price: float, max_price: float) -> float:
    """Lower price is better, higher confidence/availability is better.
    Price is normalized within the current result set so it's comparable
    across currencies/suppliers with wildly different absolute ranges."""
    if max_price > min_price:
        price_score = 1 - ((float(offer.total_price) - min_price) / (max_price - min_price))
    else:
        price_score = 1.0  # only one price point, nothing to compare against

    availability_score = _AVAILABILITY_WEIGHT.get(offer.availability, 0.0)

    # documented weighting: price matters most, then confidence, then availability
    return (0.5 * price_score) + (0.3 * offer.supplier_confidence) + (0.2 * availability_score)


def rank(offers: list[Offer]) -> list[Offer]:
    if not offers:
        return []
    prices = [float(o.total_price) for o in offers]
    min_price, max_price = min(prices), max(prices)
    return sorted(offers, key=lambda o: score(o, min_price=min_price, max_price=max_price), reverse=True)
