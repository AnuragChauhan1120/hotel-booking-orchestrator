# Travel Platform — Multi-Supplier Hotel Booking

A unified search + reliable booking service across two mock hotel suppliers
(Atlas, Nova), built with FastAPI, Temporal, SQLAlchemy/SQLite, and Docker
Compose.

\---

## Setup instructions

**Requires:** Docker Desktop (nothing else — Python/Temporal CLI not needed
on the host, everything runs in containers).

```bash
git clone <this repo>
cd travel-platform
docker compose up --build
```

This single command starts five containers:

|Service|Port|Role|
|-|-|-|
|`temporal` + `temporal-postgres`|7233|Temporal server (workflow engine)|
|`atlas-mock`|8001|Fake Atlas Hotels API|
|`nova-mock`|8002|Fake Nova Stays API|
|`api`|8000|FastAPI app (search + booking endpoints)|
|`worker`|—|Temporal worker executing `BookingWorkflow`|

Wait for the `worker` container to log `worker started, task\_queue=booking-task-queue` — that confirms the workflow/activities
registered correctly before you send any requests.

**Running tests** (from the host, needs Python 3.11+ and
`pip install -r requirements.txt` locally, or run inside the `api`
container):

```bash
pytest tests/ -v
```

\---

## Architecture diagram

```mermaid
flowchart TB
    Client(\[Client])

    subgraph API\["FastAPI app (api container)"]
        Search\["POST /search/hotels"]
        Book\["POST /bookings"]
        Status\["GET /bookings/id/status"]
    end

    subgraph SearchLayer\["Search layer"]
        Service\["search/service.py\\nfan-out + dedupe"]
        Ranking\["search/ranking.py\\nscoring"]
    end

    subgraph SupplierLayer\["Supplier layer"]
        AtlasAdapter\["AtlasAdapter"]
        NovaAdapter\["NovaAdapter"]
    end

    AtlasMock\[("Atlas mock API\\n:8001")]
    NovaMock\[("Nova mock API\\n:8002")]

    subgraph TemporalSys\["Temporal"]
        TemporalServer\[("Temporal server\\n:7233")]
        Worker\["worker.py"]
        Workflow\["BookingWorkflow"]
        Activities\["activities.py"]
    end

    DB\[("SQLite\\ntravel\_platform.db")]

    Client --> Search
    Client --> Book
    Client --> Status

    Search --> Service
    Service --> Ranking
    Service --> AtlasAdapter
    Service --> NovaAdapter
    AtlasAdapter --> AtlasMock
    NovaAdapter --> NovaMock
    Search --> DB

    Book --> TemporalServer
    TemporalServer --> Worker
    Worker --> Workflow
    Workflow --> Activities
    Activities --> AtlasAdapter
    Activities --> NovaAdapter
    Activities --> DB
    Status --> TemporalServer
```

\---

## API documentation

### `POST /search/hotels`

**Request**

```json
{
  "destination": "New York",
  "check\_in": "2026-12-01",
  "check\_out": "2026-12-05",
  "guests": 2,
  "rooms": 1
}
```

**Response `200`**

```json
{
  "request\_id": "6b91aa01-0432-4051-abeb-385f2ace7dc1",
  "offers": \[
    {
      "offer\_id": "0ac30aee-8c05-4319-9d94-8ea37dc33b3a",
      "offer": {
        "supplier\_id": "nova",
        "supplier\_offer\_ref": "NOVA-55",
        "property\_id": "NOVA-55",
        "property\_name": "Nova Central Suites",
        "location": "New York",
        "room\_type": "Standard Queen",
        "check\_in": "2026-12-01",
        "check\_out": "2026-12-05",
        "guests": 2,
        "rooms": 1,
        "currency": "USD",
        "base\_price": "126.05",
        "taxes\_and\_fees": "17.65",
        "total\_price": "143.70",
        "cancellation\_policy": {
          "refundable": true,
          "free\_cancellation\_until": null,
          "description": "Refundable"
        },
        "availability": "available",
        "supplier\_confidence": 1.0
      }
    }
  ],
  "suppliers\_queried": \["atlas", "nova"],
  "suppliers\_failed": \[]
}
```

`offer\_id` is the value to pass to `/bookings` — it's the DB primary key of
the persisted offer, not `supplier\_offer\_ref`, so a booking always traces
back to exactly what was quoted. `suppliers\_failed` is non-empty (rather
than the whole call erroring) when one supplier times out or errors —
partial results still return.

### `POST /bookings`

**Request**

```json
{ "offer\_id": "0ac30aee-8c05-4319-9d94-8ea37dc33b3a", "client\_reference": "test-1" }
```

`client\_reference` is a caller-supplied idempotency key — sending the same
value twice returns the same booking rather than creating a duplicate.

**Response `200`**

```json
{
  "workflow\_id": "booking-test-1",
  "booking\_id": "949eb9cd-d4b4-48ab-a991-905dc597c9f1",
  "status": "confirmed"
}
```

`status` is one of `confirmed`, `failed`, or `requires\_manual\_review`
(the latter when supplier confirmation times out, or when a saga
compensation itself fails — see Key engineering decisions).

### `GET /bookings/{workflow\_id}/status`

Live query straight into the running (or completed) Temporal workflow —
not a DB read — so it reflects true in-flight state.

**Response `200`**

```json
{ "status": "confirmed", "reason": "", "booking\_id": "949eb9cd-d4b4-48ab-a991-905dc597c9f1" }
```

### `POST /bookings/{workflow\_id}/cancel`

Cancels an in-flight workflow.

**Response `200`**

```json
{ "cancelled": true }
```

\---

## Database schema

```mermaid
erDiagram
    SEARCH\_REQUESTS ||--o{ OFFERS : produces
    BOOKINGS ||--o{ BOOKING\_STATUS\_HISTORY : has

    SEARCH\_REQUESTS {
        string id PK
        string destination
        string check\_in
        string check\_out
        int guests
        int rooms
        json suppliers\_queried
        json suppliers\_failed
        datetime created\_at
    }

    OFFERS {
        string id PK
        string search\_request\_id FK
        string supplier\_id
        string supplier\_offer\_ref
        string property\_id
        string property\_name
        string room\_type
        string currency
        numeric total\_price
        json raw\_offer\_json
        datetime created\_at
    }

    BOOKINGS {
        string id PK
        string workflow\_id UK
        string offer\_id
        string client\_reference UK
        string supplier\_id
        string supplier\_booking\_ref
        string status
        datetime created\_at
        datetime updated\_at
    }

    BOOKING\_STATUS\_HISTORY {
        string id PK
        string booking\_id FK
        string status
        string reason
        datetime created\_at
    }
```

`raw\_offer\_json` stores the full normalized `Offer` (not just the
summary columns) so a booking can reconstruct the exact quote it was
built from. `BookingRecord.workflow\_id` and `client\_reference` are both
unique-indexed — that's what makes idempotent booking creation possible.
`booking\_status\_history.reason` doubles as the "failure or retry
information" field the spec asks for (e.g. `price\_drift\_exceeded`,
`compensation\_failed`, `supplier\_confirmation\_timeout`) — a failure or
retry outcome IS a status transition, so it's recorded in the same table
rather than a separate one.

\---

## Example requests and responses

**One supplier down — partial results** (stop `nova-mock`, re-run search):

```json
{
  "request\_id": "...",
  "offers": \[ /\* only atlas offers \*/ ],
  "suppliers\_queried": \["atlas", "nova"],
  "suppliers\_failed": \["nova"]
}
```

**Duplicate booking request** (same `client\_reference` sent twice):

```json
// first call
{ "workflow\_id": "booking-test-1", "booking\_id": "949eb9cd-...", "status": "confirmed" }
// second call, identical body
{ "workflow\_id": "booking-test-1", "booking\_id": "949eb9cd-...", "status": "confirmed" }
```

Same `booking\_id` both times — verified live, not just asserted.

**Structured log line** (JSON, one correlation ID traceable across
layers):

```json
{"level": "INFO", "logger": "activities", "message": "supplier reservation created",
 "correlation\_id": "booking-test-1", "supplier": "atlas", "supplier\_booking\_ref": "ATL-RES-9f3a2b1c"}
```

\---

## Automated tests

`tests/` — 8 tests, run with `pytest tests/ -v`, no external services
required (fake in-memory adapters via `conftest.py`, throwaway per-test
SQLite files):

* `test\_search\_merges\_both\_suppliers` — normalization/merge across suppliers
* `test\_one\_supplier\_failing\_still\_returns\_partial\_results`
* `test\_dedup\_keeps\_cheaper\_duplicate`
* `test\_ranking\_prefers\_cheaper\_and\_more\_available\_offers`
* `test\_ranking\_handles\_single\_offer\_without\_dividing\_by\_zero`
* `test\_ranking\_empty\_list`
* `test\_duplicate\_booking\_requests\_create\_only\_one\_row` — booking idempotency
* `test\_status\_update\_records\_history`

**Manually verified live** (via `docker compose up` + curl, not automated):
search end-to-end, booking end-to-end (`status: confirmed`), duplicate
booking requests returning the same `booking\_id`, the `/status` query
endpoint, and correlation-ID propagation through worker logs.

**Known test gaps** (honest accounting, not covered by either the
automated suite or manual verification): supplier-timeout/malformed-
response handling at the raw HTTP layer (only the higher-level "supplier
raises `SupplierError`" case is tested), price-drift-during-revalidation
(the revalidation mechanism itself is a documented placeholder — see
Limitations), cancellation-after-supplier-confirmation, and an automated
(not just manual) worker-restart-recovery test.

\---

## Key engineering decisions

* **`Decimal`, not `float`, for money.** Floats can't represent decimal
fractions exactly — unacceptable for currency math.
* **`Offer` is immutable** (`frozen`). It represents a quote at a point in
time; the workflow explicitly re-fetches/revalidates rather than
mutating a stale object.
* **Adapter pattern for suppliers** (`SupplierAdapter` ABC). Search and
booking code depend only on the interface, never a concrete supplier —
adding a third supplier means writing one new adapter class, zero
changes elsewhere.
* **Idempotency at two layers.** Temporal's `workflow\_id`
(`booking-<client\_reference>`) prevents a duplicate *workflow* from
starting; `client\_reference` passed through to the supplier's own
idempotency field prevents a duplicate *supplier booking* even if an
Activity retries after a network blip.
* **Saga-pattern compensation.** If the supplier reservation succeeds but
persisting it internally fails, the workflow calls a compensating
`cancel\_supplier\_reservation` activity. If compensation itself fails,
the booking is marked `requires\_manual\_review` rather than retried
indefinitely — a stuck-but-visible state is safer than a silent loop.
* **Correlation ID threaded end-to-end** (the unique feature — see
below).
* **Sync repo functions, async activities.** Temporal activities here are
`async def` but call synchronous SQLAlchemy functions directly — fine
for SQLite at this scale; a Postgres-backed production version should
move DB calls to `asyncio.to\_thread(...)` or an async driver to avoid
blocking the event loop under load.
* **SQLite over Postgres.** Faster to set up for a 3-day prototype;
swapping is a one-line engine-URL change in `repo.py` since everything
goes through SQLAlchemy.
* **Dedup by (property\_name, location).** Simple, documented, and
deliberately naive — a real system would use fuzzy matching or shared
property IDs across suppliers.

## Unique feature: correlation-ID tracing across the whole booking job

Beyond the spec's required identifiers-in-logs, every booking generates
one correlation ID (the Temporal `workflow\_id`) at the moment it starts,
which is then threaded through **every** activity input dataclass and
**every** repo call — not just logged once at the API boundary. The
result: `docker compose logs worker | grep booking-test-1` shows the
complete story of one booking — creation, revalidation, supplier call,
persistence, compensation (if any), polling, and final status — as a
single readable sequence, across process boundaries (API → Temporal →
worker → DB), without needing a distributed tracing system. Verified live
(see Automated tests above).

\---

## Assumptions and known limitations

* **`revalidate\_offer` doesn't call a real supplier re-quote endpoint.**
Neither mock supplier exposes single-offer re-pricing, so the activity
currently treats the originally quoted price as still current (zero
simulated drift). The threshold-check mechanism and pass/fail result
are real and wired into the workflow; only the data source is a
documented placeholder for a real `adapter.revalidate()` call.
* **Dedup is exact-match, not fuzzy.** Two listings for the same physical
property with slightly different names won't be recognized as
duplicates.
* **Confirmation polling** uses a fixed attempt count and fixed sleep
interval, not configurable per environment.
* **No auth or rate-limiting** on the API — out of scope for a 3-day
prototype.
* **`supplier\_confidence`** is a static default (`1.0`) for both
suppliers rather than derived from real historical success-rate data.
* **Mock-supplier state is in-memory** (dict-backed) and resets whenever
that container restarts — a booking created against a supplier
reservation from before a mock-server restart would fail to look up
status/cancel correctly. Not an issue in normal operation since the
`api`/`worker` restart independently of the mocks.

## Documentation of how coding assistants were used

Built iteratively with Claude across the full session: architecture and
tradeoff discussion first (workflow design, idempotency strategy, dedup/
ranking approach), then code generated layer-by-layer bottom-up (schema →
mocks → adapters → search service → persistence → Temporal workflow →
correlation-ID tracing), with each layer actually run and tested — either
via live mock servers + `TestClient`/`pytest`, or (for the Temporal
workflow specifically) via `docker compose up` on the developer's own
machine, since Claude's sandbox couldn't run a live Temporal server
itself. Several real bugs were caught and fixed this way before being
handed off. 

