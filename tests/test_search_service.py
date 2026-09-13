from decimal import Decimal

import pytest

from app.search.ranking import rank
from app.search.service import search_hotels
from app.schemas.offer import AvailabilityStatus
from tests.conftest import FakeAdapter, make_offer


@pytest.mark.asyncio
async def test_search_merges_both_suppliers(sample_request):
    atlas = FakeAdapter("atlas", [make_offer(supplier_id="atlas", property_name="Hotel A", total_price=Decimal("100"))])
    nova = FakeAdapter("nova", [make_offer(supplier_id="nova", property_name="Hotel B", total_price=Decimal("150"))])

    result = await search_hotels(sample_request, [atlas, nova], request_id="req-1")

    assert {o.property_name for o in result.offers} == {"Hotel A", "Hotel B"}
    assert result.suppliers_queried == ["atlas", "nova"]
    assert result.suppliers_failed == []


@pytest.mark.asyncio
async def test_one_supplier_failing_still_returns_partial_results(sample_request):
    atlas = FakeAdapter("atlas", [make_offer(supplier_id="atlas", property_name="Hotel A")])
    nova = FakeAdapter("nova", fail=True)

    result = await search_hotels(sample_request, [atlas, nova], request_id="req-2")

    assert len(result.offers) == 1
    assert result.offers[0].property_name == "Hotel A"
    assert result.suppliers_failed == ["nova"]


@pytest.mark.asyncio
async def test_dedup_keeps_cheaper_duplicate(sample_request):
    # same property_name + location from two suppliers -> only the cheaper one survives
    atlas = FakeAdapter("atlas", [make_offer(supplier_id="atlas", property_name="Same Hotel",
                                              location="NYC", total_price=Decimal("200"))])
    nova = FakeAdapter("nova", [make_offer(supplier_id="nova", property_name="Same Hotel",
                                            location="NYC", total_price=Decimal("150"))])

    result = await search_hotels(sample_request, [atlas, nova], request_id="req-3")

    assert len(result.offers) == 1
    assert result.offers[0].supplier_id == "nova"
    assert result.offers[0].total_price == Decimal("150")


def test_ranking_prefers_cheaper_and_more_available_offers():
    cheap_available = make_offer(total_price=Decimal("100"), availability=AvailabilityStatus.AVAILABLE)
    expensive_limited = make_offer(total_price=Decimal("400"), availability=AvailabilityStatus.LIMITED)

    ranked = rank([expensive_limited, cheap_available])

    assert ranked[0] is cheap_available


def test_ranking_handles_single_offer_without_dividing_by_zero():
    only_offer = make_offer(total_price=Decimal("100"))
    ranked = rank([only_offer])
    assert ranked == [only_offer]


def test_ranking_empty_list():
    assert rank([]) == []
