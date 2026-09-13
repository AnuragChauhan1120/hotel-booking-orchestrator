from __future__ import annotations

from datetime import datetime
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

_ATLAS_STATUS_MAP = {
    "confirmed": ReservationStatus.CONFIRMED,
    "cancelled": ReservationStatus.CANCELLED,
}


def _to_mmddyyyy(d) -> str:
    return d.strftime("%m/%d/%Y")


class AtlasAdapter(SupplierAdapter):
    supplier_id = "atlas"

    def __init__(self, base_url: str = "http://localhost:8001", timeout: float = 5.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def search(self, request: SearchRequest) -> list[Offer]:
        try:
            resp = await self._client.post(
                "/v1/availability",
                json={
                    "city": request.destination,
                    "checkin": _to_mmddyyyy(request.check_in),
                    "checkout": _to_mmddyyyy(request.check_out),
                    "occupancy": request.guests,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SupplierTimeoutError("atlas search timed out") from exc
        except httpx.HTTPError as exc:
            raise SupplierUnavailableError(f"atlas search failed: {exc}") from exc

        offers: list[Offer] = []
        for row in resp.json().get("results", []):
            if not row["bookable"]:
                continue  # normalize "unbookable" out rather than returning junk
            offers.append(self._row_to_offer(row, request))
        return offers

    def _row_to_offer(self, row: dict, request: SearchRequest) -> Offer:
        total = Decimal(row["rate_cents"]) / Decimal(100)
        # Atlas gives one tax-inclusive rate — we don't get a real breakdown,
        # so we document the assumption instead of inventing a split.
        return Offer(
            supplier_id=self.supplier_id,
            supplier_offer_ref=row["property_id"],
            property_id=row["property_id"],
            property_name=row["property_name"],
            location=row["location"],
            room_type=row["room_category"],
            check_in=request.check_in,
            check_out=request.check_out,
            guests=request.guests,
            rooms=request.rooms,
            currency=row["currency"],
            base_price=total,
            taxes_and_fees=Decimal("0.00"),
            total_price=total,
            cancellation_policy=CancellationPolicy(
                refundable=row["free_cancel_by"] is not None,
                free_cancellation_until=None,
                description="Refundable" if row["free_cancel_by"] else "Non-refundable",
            ),
            availability=AvailabilityStatus.AVAILABLE,
        )

    async def revalidate(self, supplier_offer_ref: str) -> Offer:
        # Atlas has no single-offer re-quote endpoint in this mock; re-search
        # and pick the matching property. A real adapter would call a
        # dedicated pricing endpoint instead.
        raise NotImplementedError("wire up in Step 3 alongside the workflow")

    async def create_reservation(self, offer: Offer, client_reference: str) -> Reservation:
        try:
            resp = await self._client.post(
                "/v1/reservations",
                json={
                    "property_id": offer.property_id,
                    "checkin": _to_mmddyyyy(offer.check_in),
                    "checkout": _to_mmddyyyy(offer.check_out),
                    "occupancy": offer.guests,
                    "rate_cents": int(offer.total_price * 100),
                    "client_reference": client_reference,
                },
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise SupplierTimeoutError("atlas booking timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise SupplierUnavailableError(f"atlas booking failed: {exc}") from exc

        data = resp.json()
        return Reservation(
            supplier_id=self.supplier_id,
            supplier_booking_ref=data["reservation_id"],
            status=_ATLAS_STATUS_MAP.get(data["status"], ReservationStatus.PENDING),
            client_reference=client_reference,
        )

    async def get_reservation_status(self, supplier_booking_ref: str) -> ReservationStatus:
        try:
            resp = await self._client.get(f"/v1/reservations/{supplier_booking_ref}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SupplierError(f"atlas status check failed: {exc}") from exc
        return _ATLAS_STATUS_MAP.get(resp.json()["status"], ReservationStatus.PENDING)

    async def cancel_reservation(self, supplier_booking_ref: str) -> None:
        try:
            resp = await self._client.delete(f"/v1/reservations/{supplier_booking_ref}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SupplierError(f"atlas cancel failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
