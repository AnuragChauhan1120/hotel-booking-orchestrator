import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from app.db.repo import init_db
from app.logging_config import setup_logging
from app.workflows import activities
from app.workflows.booking_workflow import BookingWorkflow

TASK_QUEUE = "booking-task-queue"

setup_logging()
logger = logging.getLogger("worker")


async def _connect_with_retry(address: str, max_attempts: int = 15, delay_seconds: float = 2.0) -> Client:
    """Temporal's auto-setup container takes time to initialize its schema
    on first run — connecting once and crashing (the old behavior here) races
    that startup and fails intermittently. Retry with a fixed delay instead
    of giving up, so the worker waits for Temporal rather than needing a
    manual restart."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await Client.connect(address)
        except RuntimeError as exc:
            last_error = exc
            logger.warning(
                "temporal not ready yet, retrying",
                extra={"attempt": attempt, "max_attempts": max_attempts, "address": address, "error": str(exc)},
            )
            await asyncio.sleep(delay_seconds)
    raise RuntimeError(f"could not connect to temporal at {address} after {max_attempts} attempts") from last_error


async def main() -> None:
    init_db()
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await _connect_with_retry(temporal_address)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[BookingWorkflow],
        activities=[
            activities.create_booking_row,
            activities.revalidate_offer,
            activities.create_supplier_reservation,
            activities.update_booking_status,
            activities.poll_supplier_status,
            activities.cancel_supplier_reservation,
        ],
    )
    logger.info("worker started", extra={"task_queue": TASK_QUEUE})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())