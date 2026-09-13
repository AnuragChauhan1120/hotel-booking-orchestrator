"""
Shared internal schema. Every supplier adapter must translate its own
response format into this. Nothing outside app/suppliers/ should ever
see a supplier-specific shape.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class CancellationPolicy(BaseModel):
    refundable: bool
    free_cancellation_until: date | None = None
    description: str = ""


class Offer(BaseModel):
    """Normalized hotel offer — the only shape the rest of the app understands."""

    # Identity
    supplier_id: str                 # e.g. "atlas", "nova"
    supplier_offer_ref: str          # supplier's own ID for this offer, needed to re-quote/book
    property_id: str
    property_name: str
    location: str

    # Stay details
    room_type: str
    check_in: date
    check_out: date
    guests: int
    rooms: int

    # Pricing — always normalized to a single currency + explicit breakdown
    currency: str                    # ISO 4217, e.g. "USD"
    base_price: Decimal
    taxes_and_fees: Decimal
    total_price: Decimal

    cancellation_policy: CancellationPolicy
    availability: AvailabilityStatus

    # Ranking inputs, filled in by the search service, not the supplier
    supplier_confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    # offers are quotes at a point in time — don't mutate, re-fetch
    model_config = ConfigDict(frozen=True)


class SearchRequest(BaseModel):
    destination: str
    check_in: date
    check_out: date
    guests: int = Field(gt=0)
    rooms: int = Field(gt=0)

    def model_post_init(self, __context) -> None:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
