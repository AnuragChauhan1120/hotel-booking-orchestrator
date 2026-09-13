from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.activities import (
        CancelSupplierInput,
        CreateBookingRowInput,
        PollStatusInput,
        RevalidateInput,
        SupplierBookInput,
        UpdateStatusInput,
        cancel_supplier_reservation,
        create_booking_row,
        create_supplier_reservation,
        poll_supplier_status,
        revalidate_offer,
        update_booking_status,
    )

_DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=4,
)
_SHORT_TIMEOUT = timedelta(seconds=10)
_MAX_POLL_ATTEMPTS = 5


@dataclass
class BookingWorkflowInput:
    offer_json: dict          # the full quoted Offer, serialized — see activities.py note
    offer_property_id: str    # just for readable tracking in the booking row
    supplier_id: str
    client_reference: str     # idempotency key; workflow_id is derived from this


@dataclass
class BookingWorkflowResult:
    booking_id: str
    status: str
    reason: str = ""


@workflow.defn
class BookingWorkflow:
    def __init__(self) -> None:
        self._status = "pending"
        self._reason = ""
        self._booking_id: str | None = None

    @workflow.query
    def status(self) -> dict:
        """Live status query — the workflow is the source of truth while
        it's in flight, so callers query it directly instead of the DB."""
        return {"status": self._status, "reason": self._reason, "booking_id": self._booking_id}

    @workflow.run
    async def run(self, input: BookingWorkflowInput) -> BookingWorkflowResult:
        # Temporal's workflow_id doubles as our correlation ID — it's
        # already unique per booking job (booking-<client_reference>) and
        # stable across retries/restarts, so we reuse it rather than
        # generating a second identifier. Threaded into every activity
        # call below so worker/activity/DB logs for one booking can all
        # be found with a single grep on this one value.
        correlation_id = workflow.info().workflow_id

        # Step 0: create (or find, if we're resuming after a crash) the
        # internal booking row.
        self._status = "creating_booking_record"
        self._booking_id = await workflow.execute_activity(
            create_booking_row,
            CreateBookingRowInput(
                correlation_id=correlation_id,
                offer_property_id=input.offer_property_id,
                client_reference=input.client_reference,
                supplier_id=input.supplier_id,
            ),
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )

        # Step 1: revalidate price/availability
        self._status = "revalidating"
        revalidation = await workflow.execute_activity(
            revalidate_offer,
            RevalidateInput(correlation_id=correlation_id, offer_json=input.offer_json),
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        if not revalidation.ok:
            return await self._fail(correlation_id=correlation_id, reason=revalidation.reason)

        # Step 2: create the supplier reservation
        self._status = "booking_with_supplier"
        try:
            supplier_result = await workflow.execute_activity(
                create_supplier_reservation,
                SupplierBookInput(
                    correlation_id=correlation_id,
                    supplier_id=input.supplier_id,
                    offer_json=input.offer_json,
                    client_reference=input.client_reference,
                ),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:
            return await self._fail(correlation_id=correlation_id, reason="supplier_booking_failed")

        # Step 3: persist the confirmed supplier reference internally.
        # If THIS fails after the supplier already booked, compensate by
        # cancelling the supplier reservation (saga pattern) — the
        # "supplier succeeds, internal processing fails" case.
        self._status = "persisting"
        try:
            await workflow.execute_activity(
                update_booking_status,
                UpdateStatusInput(
                    correlation_id=correlation_id,
                    booking_id=self._booking_id,
                    status="supplier_confirmed",
                    reason="supplier reservation created",
                    supplier_booking_ref=supplier_result.supplier_booking_ref,
                ),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:
            return await self._compensate_and_fail(
                correlation_id=correlation_id,
                supplier_id=input.supplier_id,
                supplier_booking_ref=supplier_result.supplier_booking_ref,
            )

        # Step 4: poll for final supplier confirmation
        self._status = "awaiting_supplier_confirmation"
        confirmed = False
        for _ in range(_MAX_POLL_ATTEMPTS):
            status = await workflow.execute_activity(
                poll_supplier_status,
                PollStatusInput(
                    correlation_id=correlation_id,
                    supplier_id=input.supplier_id,
                    supplier_booking_ref=supplier_result.supplier_booking_ref,
                ),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
            if status == "confirmed":
                confirmed = True
                break
            await workflow.sleep(timedelta(seconds=2))

        if not confirmed:
            return await self._finish(
                correlation_id=correlation_id, status="requires_manual_review", reason="supplier_confirmation_timeout"
            )

        return await self._finish(correlation_id=correlation_id, status="confirmed", reason="")

    async def _fail(self, *, correlation_id: str, reason: str) -> BookingWorkflowResult:
        return await self._finish(correlation_id=correlation_id, status="failed", reason=reason)

    async def _compensate_and_fail(
        self, *, correlation_id: str, supplier_id: str, supplier_booking_ref: str
    ) -> BookingWorkflowResult:
        try:
            await workflow.execute_activity(
                cancel_supplier_reservation,
                CancelSupplierInput(
                    correlation_id=correlation_id, supplier_id=supplier_id, supplier_booking_ref=supplier_booking_ref
                ),
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
            return await self._finish(
                correlation_id=correlation_id, status="failed", reason="internal_persist_failed_compensated"
            )
        except Exception:
            # compensation itself failed — flag for a human, don't loop forever
            return await self._finish(
                correlation_id=correlation_id, status="requires_manual_review", reason="compensation_failed"
            )

    async def _finish(self, *, correlation_id: str, status: str, reason: str) -> BookingWorkflowResult:
        self._status = status
        self._reason = reason
        await workflow.execute_activity(
            update_booking_status,
            UpdateStatusInput(correlation_id=correlation_id, booking_id=self._booking_id, status=status, reason=reason),
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        return BookingWorkflowResult(booking_id=self._booking_id, status=status, reason=reason)