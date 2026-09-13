from __future__ import annotations

from decimal import Decimal

import httpx

from app.schemas.offer import (
    AvailabilityStatus,
    CancellationPolicy,
    Offer,
    SearchRequest,
)
from app.suppliers.base import (
    Reservation,
    ReservationStatus,
    SupplierAdapter,
    SupplierError,
    SupplierTimeoutError,
    SupplierUnavailableError,
)

_NOVA_AVAILABILITY_MAP = {
    "OPEN": AvailabilityStatus.AVAILABLE,
    "WAITLIST": AvailabilityStatus.LIMITED,
    "CLOSED": AvailabilityStatus.UNAVAILABLE,
}

_NOVA_STATUS_MAP = {
    "CONFIRMED": ReservationStatus.CONFIRMED,
    "CANCELLED": ReservationStatus.CANCELLED,
}


def _to_isodate(d) -> str:
    return d.isoformat()


class NovaAdapter(SupplierAdapter):
    supplier_id = "nova"

    def __init__(self, base_url: str = "http://localhost:8002", timeout: float = 5.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def search(self, request: SearchRequest) -> list[Offer]:
        try:
            resp = await self._client.post(
                "/api/v2/stays/search",
                json={
                    "destination_city": request.destination,
                    "date_from": _to_isodate(request.check_in),
                    "date_to": _to_isodate(request.check_out),
                    "guest_count": request.guests,
                    "room_count": request.rooms,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SupplierTimeoutError("nova search timed out") from exc
        except httpx.HTTPError as exc:
            raise SupplierUnavailableError(f"nova search failed: {exc}") from exc

        offers: list[Offer] = []
        for row in resp.json().get("offers", []):
            avail = _NOVA_AVAILABILITY_MAP.get(row["availability_state"], AvailabilityStatus.UNAVAILABLE)
            if avail == AvailabilityStatus.UNAVAILABLE:
                continue
            offers.append(self._row_to_offer(row, request, avail))
        return offers

    def _row_to_offer(self, row: dict, request: SearchRequest, avail: AvailabilityStatus) -> Offer:
        base = Decimal(row["base_amount"])
        tax = Decimal(row["tax_amount"])
        return Offer(
            supplier_id=self.supplier_id,
            supplier_offer_ref=row["property_id"],
            property_id=row["property_id"],
            property_name=row["property_name"],
            location=row["city"],
            room_type=row["room_name"],
            check_in=request.check_in,
            check_out=request.check_out,
            guests=request.guests,
            rooms=request.rooms,
            currency=row["currency_code"],
            base_price=base,
            taxes_and_fees=tax,
            total_price=base + tax,
            cancellation_policy=CancellationPolicy(
                refundable=row["cancellation"]["refundable"],
                free_cancellation_until=None,
                description="Refundable" if row["cancellation"]["refundable"] else "Non-refundable",
            ),
            availability=avail,
        )

    async def revalidate(self, supplier_offer_ref: str) -> Offer:
        raise NotImplementedError("wire up in Step 3 alongside the workflow")

    async def create_reservation(self, offer: Offer, client_reference: str) -> Reservation:
        try:
            resp = await self._client.post(
                "/api/v2/stays/book",
                json={
                    "property_id": offer.property_id,
                    "date_from": _to_isodate(offer.check_in),
                    "date_to": _to_isodate(offer.check_out),
                    "guest_count": offer.guests,
                    "base_amount": str(offer.base_price),
                    "tax_amount": str(offer.taxes_and_fees),
                    "idempotency_key": client_reference,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SupplierTimeoutError("nova booking timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise SupplierUnavailableError(f"nova booking failed: {exc}") from exc

        data = resp.json()
        return Reservation(
            supplier_id=self.supplier_id,
            supplier_booking_ref=data["booking_ref"],
            status=_NOVA_STATUS_MAP.get(data["state"], ReservationStatus.PENDING),
            client_reference=client_reference,
        )

    async def get_reservation_status(self, supplier_booking_ref: str) -> ReservationStatus:
        try:
            resp = await self._client.get(f"/api/v2/stays/book/{supplier_booking_ref}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SupplierError(f"nova status check failed: {exc}") from exc
        return _NOVA_STATUS_MAP.get(resp.json()["state"], ReservationStatus.PENDING)

    async def cancel_reservation(self, supplier_booking_ref: str) -> None:
        try:
            resp = await self._client.post(f"/api/v2/stays/book/{supplier_booking_ref}/cancel")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SupplierError(f"nova cancel failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
