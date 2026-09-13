from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from temporalio import activity

from app.db import repo
from app.schemas.offer import Offer
from app.suppliers.atlas import AtlasAdapter
from app.suppliers.base import SupplierError
from app.suppliers.nova import NovaAdapter

logger = logging.getLogger("activities")

_ATLAS_URL = os.environ.get("ATLAS_URL", "http://localhost:8001")
_NOVA_URL = os.environ.get("NOVA_URL", "http://localhost:8002")
_ADAPTERS = {
    "atlas": lambda: AtlasAdapter(base_url=_ATLAS_URL),
    "nova": lambda: NovaAdapter(base_url=_NOVA_URL),
}

PRICE_DRIFT_THRESHOLD = Decimal("0.03")  # 3%


def _adapter_for(supplier_id: str):
    cls = _ADAPTERS.get(supplier_id)
    if cls is None:
        raise ValueError(f"unknown supplier: {supplier_id}")
    return cls()


# Every activity input below carries `correlation_id` — the Temporal
# workflow_id, reused as-is rather than minting a separate value, since
# it's already a stable, unique identifier for one booking "job" end to
# end. Every activity logs it, and it's threaded into every repo call too,
# so `grep correlation_id=booking-test-1 logs` shows the ENTIRE booking's
# story across the worker, every activity, and every DB write — not just
# the API layer's request log.


@dataclass
class CreateBookingRowInput:
    correlation_id: str
    offer_property_id: str
    client_reference: str
    supplier_id: str


@activity.defn
async def create_booking_row(input: CreateBookingRowInput) -> str:
    """Idempotent: same client_reference always returns the same booking_id,
    so a retried activity (or a duplicate request) never creates two rows."""
    logger.info(
        "creating booking record",
        extra={"correlation_id": input.correlation_id, "supplier": input.supplier_id},
    )
    booking_id = repo.create_booking(
        correlation_id=input.correlation_id,
        offer_id=input.offer_property_id,
        client_reference=input.client_reference,
        supplier_id=input.supplier_id,
    )
    logger.info(
        "booking record created",
        extra={"correlation_id": input.correlation_id, "booking_id": booking_id},
    )
    return booking_id


@dataclass
class RevalidateInput:
    correlation_id: str
    offer_json: dict


@dataclass
class RevalidateResult:
    ok: bool
    reason: str


@activity.defn
async def revalidate_offer(input: RevalidateInput) -> RevalidateResult:
    """Re-checks the price we quoted is still valid before committing to book it.
    NOTE: adapter.revalidate() isn't implemented yet (our mock suppliers don't
    expose a single-offer re-quote endpoint) — this currently treats the quoted
    price as still current with zero simulated drift. The mechanism (threshold
    check, pass/fail result) is real; the data source is a documented stand-in
    for a real adapter.revalidate() call."""
    offer = Offer.model_validate(input.offer_json)
    original = offer.total_price
    current = original  # placeholder until adapter.revalidate() is wired up
    drift = abs(current - original) / original if original else Decimal(0)

    result = (
        RevalidateResult(ok=False, reason="price_drift_exceeded")
        if drift > PRICE_DRIFT_THRESHOLD
        else RevalidateResult(ok=True, reason="ok")
    )
    logger.info(
        "revalidation complete",
        extra={"correlation_id": input.correlation_id, "ok": result.ok, "reason": result.reason},
    )
    return result


@dataclass
class SupplierBookInput:
    correlation_id: str
    supplier_id: str
    offer_json: dict
    client_reference: str


@dataclass
class SupplierBookResult:
    supplier_booking_ref: str
    status: str


@activity.defn
async def create_supplier_reservation(input: SupplierBookInput) -> SupplierBookResult:
    offer = Offer.model_validate(input.offer_json)
    adapter = _adapter_for(input.supplier_id)
    logger.info(
        "calling supplier to create reservation",
        extra={"correlation_id": input.correlation_id, "supplier": input.supplier_id},
    )
    try:
        reservation = await adapter.create_reservation(offer, input.client_reference)
    except SupplierError as exc:
        logger.warning(
            "supplier reservation call failed",
            extra={"correlation_id": input.correlation_id, "supplier": input.supplier_id, "error": str(exc)},
        )
        raise  # Temporal retries per the activity's RetryPolicy
    finally:
        await adapter.aclose()

    logger.info(
        "supplier reservation created",
        extra={
            "correlation_id": input.correlation_id,
            "supplier": input.supplier_id,
            "supplier_booking_ref": reservation.supplier_booking_ref,
        },
    )
    return SupplierBookResult(
        supplier_booking_ref=reservation.supplier_booking_ref,
        status=reservation.status.value,
    )


@dataclass
class UpdateStatusInput:
    correlation_id: str
    booking_id: str
    status: str
    reason: str | None = None
    supplier_booking_ref: str | None = None


@activity.defn
async def update_booking_status(input: UpdateStatusInput) -> None:
    repo.update_booking_status(
        correlation_id=input.correlation_id,
        booking_id=input.booking_id,
        status=input.status,
        reason=input.reason,
        supplier_booking_ref=input.supplier_booking_ref,
    )
    logger.info(
        "booking status updated",
        extra={
            "correlation_id": input.correlation_id,
            "booking_id": input.booking_id,
            "status": input.status,
            "reason": input.reason,
        },
    )


@dataclass
class PollStatusInput:
    correlation_id: str
    supplier_id: str
    supplier_booking_ref: str


@activity.defn
async def poll_supplier_status(input: PollStatusInput) -> str:
    adapter = _adapter_for(input.supplier_id)
    try:
        status = await adapter.get_reservation_status(input.supplier_booking_ref)
    finally:
        await adapter.aclose()
    logger.info(
        "polled supplier status",
        extra={
            "correlation_id": input.correlation_id,
            "supplier": input.supplier_id,
            "supplier_booking_ref": input.supplier_booking_ref,
            "status": status.value,
        },
    )
    return status.value


@dataclass
class CancelSupplierInput:
    correlation_id: str
    supplier_id: str
    supplier_booking_ref: str


@activity.defn
async def cancel_supplier_reservation(input: CancelSupplierInput) -> None:
    """Compensation activity: undoes a supplier-side booking when a later
    step in the saga fails. Must be safe to call more than once."""
    adapter = _adapter_for(input.supplier_id)
    logger.warning(
        "compensating: cancelling supplier reservation",
        extra={
            "correlation_id": input.correlation_id,
            "supplier": input.supplier_id,
            "supplier_booking_ref": input.supplier_booking_ref,
        },
    )
    try:
        await adapter.cancel_reservation(input.supplier_booking_ref)
    finally:
        await adapter.aclose()