from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.offer import AvailabilityStatus, CancellationPolicy, Offer, SearchRequest
from app.suppliers.base import Reservation, ReservationStatus, SupplierAdapter, SupplierTimeoutError


def make_offer(**overrides) -> Offer:
    defaults = dict(
        supplier_id="fake",
        supplier_offer_ref="ref-1",
        property_id="P1",
        property_name="Test Hotel",
        location="New York",
        room_type="Standard",
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 5),
        guests=2,
        rooms=1,
        currency="USD",
        base_price=Decimal("100.00"),
        taxes_and_fees=Decimal("10.00"),
        total_price=Decimal("110.00"),
        cancellation_policy=CancellationPolicy(refundable=True, description="Refundable"),
        availability=AvailabilityStatus.AVAILABLE,
        supplier_confidence=1.0,
    )
    defaults.update(overrides)
    return Offer(**defaults)


class FakeAdapter(SupplierAdapter):
    """Returns a fixed list of offers; raises if `fail=True`."""

    def __init__(self, supplier_id: str, offers: list[Offer] | None = None, fail: bool = False):
        self.supplier_id = supplier_id
        self._offers = offers or []
        self._fail = fail
        self.booked: list[str] = []  # client_references we've "booked", for idempotency tests

    async def search(self, request: SearchRequest) -> list[Offer]:
        if self._fail:
            raise SupplierTimeoutError(f"{self.supplier_id} timed out")
        return self._offers

    async def revalidate(self, supplier_offer_ref: str) -> Offer:
        return self._offers[0]

    async def create_reservation(self, offer: Offer, client_reference: str) -> Reservation:
        if client_reference not in self.booked:
            self.booked.append(client_reference)
        return Reservation(
            supplier_id=self.supplier_id,
            supplier_booking_ref=f"{self.supplier_id}-{client_reference}",
            status=ReservationStatus.CONFIRMED,
            client_reference=client_reference,
        )

    async def get_reservation_status(self, supplier_booking_ref: str) -> ReservationStatus:
        return ReservationStatus.CONFIRMED

    async def cancel_reservation(self, supplier_booking_ref: str) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.fixture
def sample_request() -> SearchRequest:
    return SearchRequest(
        destination="New York",
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 5),
        guests=2,
        rooms=1,
    )
