"""
Not a pytest suite yet (that's Step 5) — a quick end-to-end smoke check
you can run right now to prove the adapter layer actually normalizes
Atlas + Nova into the same shape. Run with both mock servers up:

  uvicorn app.suppliers.mock_servers.atlas_server:app --port 8001 &
  uvicorn app.suppliers.mock_servers.nova_server:app --port 8002 &
  python -m scripts.smoke_check
"""
import asyncio
from datetime import date

from app.schemas.offer import SearchRequest
from app.suppliers.atlas import AtlasAdapter
from app.suppliers.nova import NovaAdapter


async def main() -> None:
    request = SearchRequest(
        destination="New York",
        check_in=date(2026, 12, 1),
        check_out=date(2026, 12, 5),
        guests=2,
        rooms=1,
    )

    atlas = AtlasAdapter()
    nova = NovaAdapter()
    try:
        atlas_offers, nova_offers = await asyncio.gather(
            atlas.search(request), nova.search(request)
        )
    finally:
        await atlas.aclose()
        await nova.aclose()

    all_offers = atlas_offers + nova_offers
    print(f"atlas returned {len(atlas_offers)} bookable offers")
    print(f"nova returned {len(nova_offers)} available/limited offers")
    print(f"total normalized offers: {len(all_offers)}\n")

    for o in all_offers:
        print(
            f"[{o.supplier_id:5s}] {o.property_name:24s} "
            f"{o.room_type:16s} total={o.total_price} {o.currency} "
            f"avail={o.availability.value}"
        )

    # the whole point of normalization: every offer, regardless of supplier,
    # exposes the exact same fields in the exact same types
    assert all(hasattr(o, "total_price") for o in all_offers)
    assert len({type(o) for o in all_offers}) == 1
    print("\nOK: all offers share one normalized type/shape.")


if __name__ == "__main__":
    asyncio.run(main())
