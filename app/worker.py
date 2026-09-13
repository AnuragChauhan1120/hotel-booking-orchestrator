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


async def main() -> None:
    init_db()
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(temporal_address)
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
    logging.info("worker started, task_queue=%s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())