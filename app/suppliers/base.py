"""
SupplierAdapter contract. Core search/booking code depends only on this
interface, never on a concrete supplier. Add a new supplier by writing
one class here — nothing in app/search or app/workflows should change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel

from app.schemas.offer import Offer, SearchRequest


class ReservationStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SupplierError(Exception):
    """Base for all supplier-side failures. Adapters must raise this (or a
    subclass) rather than letting raw HTTP/parsing exceptions leak out —
    callers need a stable error surface to build retry policies on."""


class SupplierTimeoutError(SupplierError):
    pass


class SupplierUnavailableError(SupplierError):
    pass


class Reservation(BaseModel):
    supplier_id: str
    supplier_booking_ref: str
    status: ReservationStatus
    client_reference: str  # idempotency key WE pass to the supplier


class SupplierAdapter(ABC):
    """One instance per supplier. Must be stateless / safe to call concurrently."""

    supplier_id: str

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[Offer]:
        """Return normalized offers. Must not raise on 'no results' — return [].
        Should raise SupplierTimeoutError/SupplierUnavailableError on real failures
        so the search service can degrade gracefully (partial results)."""

    @abstractmethod
    async def revalidate(self, supplier_offer_ref: str) -> Offer:
        """Re-fetch current price/availability for a specific offer just before booking."""

    @abstractmethod
    async def create_reservation(self, offer: Offer, client_reference: str) -> Reservation:
        """client_reference is OUR idempotency key. Calling this twice with the
        same client_reference must not create two bookings — either the supplier
        enforces that (as our mocks do) or the adapter dedupes locally."""

    @abstractmethod
    async def get_reservation_status(self, supplier_booking_ref: str) -> ReservationStatus:
        ...

    @abstractmethod
    async def cancel_reservation(self, supplier_booking_ref: str) -> None:
        ...
