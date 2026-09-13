"""
Fake Atlas Hotels API. Deliberately uses a different shape than Nova:
- prices in cents, single "rate" field (tax included)
- dates as "MM/DD/YYYY" strings
- availability as a boolean "bookable" flag
Run standalone: uvicorn app.suppliers.mock_servers.atlas_server:app --port 8001
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Atlas Hotels API (mock)")

_PROPERTIES = [
    {"id": "ATL-100", "name": "Atlas Grand Central", "location": "New York"},
    {"id": "ATL-101", "name": "Atlas Riverside", "location": "New York"},
    {"id": "ATL-204", "name": "Atlas Bayview", "location": "San Francisco"},
]

# in-memory "reservations" store — good enough for a mock
_RESERVATIONS: dict[str, dict] = {}


class AtlasSearchRequest(BaseModel):
    city: str
    checkin: str   # "MM/DD/YYYY"
    checkout: str
    occupancy: int


class AtlasBookRequest(BaseModel):
    property_id: str
    checkin: str
    checkout: str
    occupancy: int
    rate_cents: int
    client_reference: str  # used for idempotency


@app.post("/v1/availability")
def search(req: AtlasSearchRequest):
    results = []
    for prop in _PROPERTIES:
        if req.city.lower() not in prop["location"].lower():
            continue
        # simulate occasional unavailability
        bookable = random.random() > 0.15
        results.append(
            {
                "property_id": prop["id"],
                "property_name": prop["name"],
                "location": prop["location"],
                "room_category": "Deluxe King",
                "rate_cents": random.randint(9000, 28000),  # tax-inclusive, per stay
                "currency": "USD",
                "bookable": bookable,
                "free_cancel_by": None,
                "checkin": req.checkin,
                "checkout": req.checkout,
            }
        )
    return {"results": results}


@app.post("/v1/reservations")
def book(req: AtlasBookRequest):
    # idempotency: same client_reference returns the same reservation
    for res in _RESERVATIONS.values():
        if res["client_reference"] == req.client_reference:
            return res

    if random.random() < 0.1:
        raise HTTPException(status_code=503, detail="atlas_temporarily_unavailable")

    reservation_id = f"ATL-RES-{uuid.uuid4().hex[:8]}"
    reservation = {
        "reservation_id": reservation_id,
        "status": "confirmed",
        "property_id": req.property_id,
        "rate_cents": req.rate_cents,
        "client_reference": req.client_reference,
        "created_at": datetime.utcnow().isoformat(),
    }
    _RESERVATIONS[reservation_id] = reservation
    return reservation


@app.get("/v1/reservations/{reservation_id}")
def status(reservation_id: str):
    if reservation_id not in _RESERVATIONS:
        raise HTTPException(status_code=404, detail="not_found")
    return _RESERVATIONS[reservation_id]


@app.delete("/v1/reservations/{reservation_id}")
def cancel(reservation_id: str):
    if reservation_id not in _RESERVATIONS:
        raise HTTPException(status_code=404, detail="not_found")
    _RESERVATIONS[reservation_id]["status"] = "cancelled"
    return _RESERVATIONS[reservation_id]
