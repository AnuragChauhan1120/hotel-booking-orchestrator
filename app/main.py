from __future__ import annotations

import asyncio
import logging
import os
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from temporalio.client import Client, WorkflowFailureError

from app.db import repo
from app.logging_config import setup_logging
from app.schemas.offer import Offer, SearchRequest
from app.search.service import search_hotels
from app.suppliers.atlas import AtlasAdapter
from app.suppliers.nova import NovaAdapter
from app.workflows.booking_workflow import BookingWorkflow, BookingWorkflowInput

setup_logging()
logger = logging.getLogger("api")

app = FastAPI(title="Travel Platform")

TASK_QUEUE = "booking-task-queue"
_temporal_client: Client | None = None


@app.on_event("startup")
async def startup() -> None:
    repo.init_db()


async def _connect_with_retry(address: str, max_attempts: int = 15, delay_seconds: float = 2.0) -> Client:
    """Same rationale as worker.py's version: Temporal's auto-setup container
    can take a while to become ready, so retry instead of failing the first
    request that happens to race it."""
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


async def _get_temporal_client() -> Client:
    global _temporal_client
    if _temporal_client is None:
        temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        _temporal_client = await _connect_with_retry(temporal_address)
    return _temporal_client


# ---- search --------------------------------------------------------------

class SearchHotelsRequest(BaseModel):
    destination: str
    check_in: str
    check_out: str
    guests: int
    rooms: int


class SearchOfferItem(BaseModel):
    offer_id: str  # DB id — pass this to /bookings, not supplier_offer_ref
    offer: Offer


class SearchHotelsResponse(BaseModel):
    request_id: str
    offers: list[SearchOfferItem]
    suppliers_queried: list[str]
    suppliers_failed: list[str]


@app.post("/search/hotels", response_model=SearchHotelsResponse)
async def search_hotels_endpoint(body: SearchHotelsRequest):
    request = SearchRequest(
        destination=body.destination,
        check_in=body.check_in,
        check_out=body.check_out,
        guests=body.guests,
        rooms=body.rooms,
    )
    request_id = str(uuid.uuid4())
    logger.info("search request received", extra={"request_id": request_id, "destination": body.destination})

    atlas_url = os.environ.get("ATLAS_URL", "http://localhost:8001")
    nova_url = os.environ.get("NOVA_URL", "http://localhost:8002")
    adapters = [AtlasAdapter(base_url=atlas_url), NovaAdapter(base_url=nova_url)]
    try:
        result = await search_hotels(request, adapters, request_id)
    finally:
        for a in adapters:
            await a.aclose()

    search_request_id = repo.save_search(
        request, result.suppliers_queried, result.suppliers_failed, result.offers, correlation_id=request_id
    )

    rows = repo.get_offers_for_search(search_request_id)
    items = [SearchOfferItem(offer_id=r.id, offer=Offer.model_validate(r.raw_offer_json)) for r in rows]

    logger.info(
        "search request completed",
        extra={"request_id": request_id, "offer_count": len(items), "suppliers_failed": result.suppliers_failed},
    )

    return SearchHotelsResponse(
        request_id=request_id,
        offers=items,
        suppliers_queried=result.suppliers_queried,
        suppliers_failed=result.suppliers_failed,
    )


# ---- booking ---------------------------------------------------------------

class BookRequest(BaseModel):
    offer_id: str           # the offer_id from the search response above
    client_reference: str   # caller-supplied idempotency key


class BookResponse(BaseModel):
    workflow_id: str
    booking_id: str
    status: str


@app.post("/bookings", response_model=BookResponse)
async def create_booking(body: BookRequest):
    offer_row = repo.get_offer(body.offer_id)
    if offer_row is None:
        raise HTTPException(status_code=404, detail="offer not found")

    # idempotency at the API layer too: same client_reference -> same
    # workflow_id, so a resubmitted request attaches to the existing
    # workflow instead of starting a duplicate one
    workflow_id = f"booking-{body.client_reference}"
    logger.info(
        "booking requested",
        extra={"workflow_id": workflow_id, "supplier": offer_row.supplier_id, "request_offer_id": body.offer_id},
    )

    client = await _get_temporal_client()
    handle = await client.start_workflow(
        BookingWorkflow.run,
        BookingWorkflowInput(
            offer_json=offer_row.raw_offer_json,
            offer_property_id=offer_row.property_id,
            supplier_id=offer_row.supplier_id,
            client_reference=body.client_reference,
        ),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    try:
        result = await handle.result()
    except WorkflowFailureError as exc:
        logger.error("booking workflow failed", extra={"workflow_id": workflow_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"booking workflow failed: {exc}") from exc

    logger.info(
        "booking completed",
        extra={"workflow_id": workflow_id, "booking_id": result.booking_id, "status": result.status},
    )
    return BookResponse(workflow_id=workflow_id, booking_id=result.booking_id, status=result.status)


@app.get("/bookings/{workflow_id}/status")
async def booking_status(workflow_id: str):
    client = await _get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    try:
        return await handle.query(BookingWorkflow.status)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"workflow not found or not queryable: {exc}") from exc


@app.post("/bookings/{workflow_id}/cancel")
async def cancel_booking(workflow_id: str):
    client = await _get_temporal_client()
    handle = client.get_workflow_handle(workflow_id)
    await handle.cancel()
    logger.info("booking cancelled", extra={"workflow_id": workflow_id})
    return {"cancelled": True}