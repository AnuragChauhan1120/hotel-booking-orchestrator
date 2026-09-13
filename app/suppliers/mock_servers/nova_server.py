"""
Fake Nova Stays API. Different shape from Atlas on purpose:
- prices as decimal strings, base price and tax broken out separately
- dates as ISO "YYYY-MM-DD"
- availability as a string enum ("OPEN" / "WAITLIST" / "CLOSED")
Run standalone: uvicorn app.suppliers.mock_servers.nova_server:app --port 8002
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Nova Stays API (mock)")

_PROPERTIES = [
    {"id": "NOVA-55", "name": "Nova Central Suites", "location": "New York"},
    {"id": "NOVA-56", "name": "Nova Harbor House", "location": "San Francisco"},
    {"id": "NOVA-70", "name": "Atlas Grand Central", "location": "New York"},  # intentional near-dupe w/ Atlas
]

_RESERVATIONS: dict[str, dict] = {}


class NovaSearchRequest(BaseModel):
    destination_city: str
    date_from: str  # "YYYY-MM-DD"
    date_to: str
    guest_count: int
    room_count: int = 1


class NovaBookRequest(BaseModel):
    property_id: str
    date_from: str
    date_to: str
    guest_count: int
    base_amount: str
    tax_amount: str
    idempotency_key: str


@app.post("/api/v2/stays/search")
def search(req: NovaSearchRequest):
    results = []
    for prop in _PROPERTIES:
        if req.destination_city.lower() not in prop["location"].lower():
            continue
        base = round(random.uniform(80, 260), 2)
        tax = round(base * 0.14, 2)
        state = random.choices(["OPEN", "WAITLIST", "CLOSED"], weights=[0.75, 0.15, 0.1])[0]
        results.append(
            {
                "property_id": prop["id"],
                "property_name": prop["name"],
                "city": prop["location"],
                "room_name": "Standard Queen",
                "base_amount": f"{base:.2f}",
                "tax_amount": f"{tax:.2f}",
                "currency_code": "USD",
                "availability_state": state,
                "cancellation": {"free_until": None, "refundable": state == "OPEN"},
                "date_from": req.date_from,
                "date_to": req.date_to,
            }
        )
    return {"offers": results}


@app.post("/api/v2/stays/book")
def book(req: NovaBookRequest):
    for res in _RESERVATIONS.values():
        if res["idempotency_key"] == req.idempotency_key:
            return res

    if random.random() < 0.1:
        raise HTTPException(status_code=503, detail="nova_upstream_error")

    booking_ref = f"NOVA-BK-{uuid.uuid4().hex[:8]}"
    reservation = {
        "booking_ref": booking_ref,
        "state": "CONFIRMED",
        "property_id": req.property_id,
        "base_amount": req.base_amount,
        "tax_amount": req.tax_amount,
        "idempotency_key": req.idempotency_key,
        "created_at": datetime.utcnow().isoformat(),
    }
    _RESERVATIONS[booking_ref] = reservation
    return reservation


@app.get("/api/v2/stays/book/{booking_ref}")
def status(booking_ref: str):
    if booking_ref not in _RESERVATIONS:
        raise HTTPException(status_code=404, detail="not_found")
    return _RESERVATIONS[booking_ref]


@app.post("/api/v2/stays/book/{booking_ref}/cancel")
def cancel(booking_ref: str):
    if booking_ref not in _RESERVATIONS:
        raise HTTPException(status_code=404, detail="not_found")
    _RESERVATIONS[booking_ref]["state"] = "CANCELLED"
    return _RESERVATIONS[booking_ref]
