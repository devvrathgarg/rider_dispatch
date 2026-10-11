"""The dispatch service. A batch of restaurant locations in, rider ids out.

It reads Redis and never writes to it (DECISIONS.md entry 1), holds no state
between requests, and has no clock. That is deliberate: nothing here knows it is
being driven by a simulator, so the same service could be pointed at real
traffic with sim.py deleted.

Run it with:  uvicorn dispatch:app --host 127.0.0.1 --port 8001
Then open http://127.0.0.1:8001/docs to send a request by hand.
"""

import h3
from fastapi import FastAPI
from pydantic import BaseModel, Field

import config
import state
from travel import HaversineProvider, Point

# Policies live here rather than in the simulator, because the dispatch service
# is the dispatcher - moving the decision out would leave it a candidate-finder
# (entry 22). Validated at import so a typo in config fails at startup instead
# of halfway through a benchmark.
KNOWN_POLICIES = ("greedy",)
if config.POLICY not in KNOWN_POLICIES:
    raise SystemExit(
        f"config.POLICY is {config.POLICY!r}, which is not one of {KNOWN_POLICIES}"
    )

app = FastAPI(title="Rider dispatch")

# One origin, N destinations - the shape this endpoint asks in, and the shape
# OSRM's /table answers in one request when it replaces this.
travel = HaversineProvider(speed_kmh=config.RIDER_SPEED_KMH)


class OrderRequest(BaseModel):
    """One order needing a rider, and the restaurant to collect it from.

    The ranges are the real work here. This is the trust boundary: the data
    arrives from outside the process, so a bad value must be rejected at the
    door rather than crashing somewhere inside h3 with a confusing message.

    Note what this cannot catch: swapping lat and lng for Bangalore produces
    (77.59, 12.97), and latitude 77.59 is perfectly valid - it is in Arctic
    Norway. Consistency of the (lat, lng) convention is the only defence.
    """

    order_id: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class AssignBatchRequest(BaseModel):
    """Several orders to decide together.

    A batch rather than one order per call because a batching policy must avoid
    handing the same rider to two orders *inside its own decision*, before
    anything reaches Redis - and it cannot use Redis to track that, since the
    service never writes. A one-order-at-a-time endpoint structurally cannot
    express batching (entry 22).

    The list order is the caller's PRIORITY order. The simulator sends oldest
    first. Greedy honours that order; a matching policy will ignore it and
    optimise across the whole batch, which is exactly the difference between
    them.
    """

    orders: list[OrderRequest]


class Assignment(BaseModel):
    """What happened to one order in the batch.

    `rider_id` is None when nobody could be assigned, and the batch still
    returns HTTP 200: an order with no rider in range retries on a later tick,
    so this is designed behaviour that happens constantly. Returning an error
    status would make normal operation look like failure and teach the Day 6
    dashboards to lie.

    The counts exist because "no rider" has two different causes with two
    different fixes, and they are indistinguishable from outside:
      candidates == 0        -> nobody nearby at all: geometry
      free_candidates == 0   -> riders nearby but all busy or already committed
                                in this batch: saturation
    """

    order_id: str
    rider_id: str | None
    candidates: int
    free_candidates: int

    # Which K found the candidates. Above config.K_RING means the primary disk
    # was empty and the fallback widened - a signal that the fleet is badly
    # distributed, not that it is busy (entry 20).
    search_k: int


class AssignBatchResponse(BaseModel):
    """The assignments, and which policy produced them.

    `policy` is not decoration. config.POLICY is read by the service at import,
    so editing it and forgetting to restart uvicorn would silently benchmark
    greedy against greedy while the simulator believed otherwise. The simulator
    checks this field at startup, which turns a ruined experiment into an error.
    """

    assignments: list[Assignment]
    policy: str


def cost_of(travel_seconds: float) -> float:
    """What dispatch ranks on. Lowest wins.

    Plain travel time to the restaurant. The exact objective would be
    max(travel, prep_remaining), since the food is not collectable until prep
    finishes - but measured against this configuration it cannot rank
    differently: the K=2 disk is 341 seconds of travel against a 720 second
    prep, so every reachable rider arrives early and they all tie (entry 17).

    It would start to differ with a shorter prep time, a much larger K, or an
    order that has already waited 720+ seconds unassigned.
    """
    return travel_seconds


def find_candidate_ids(restaurant_cell: str) -> tuple[list[str], int]:
    """Rider ids near a restaurant, and the K that found them.

    The primary search is the fixed `config.K_RING`, and in the normal case this
    returns after one SUNION. Widening happens ONLY when a disk contains no
    riders whatsoever, which is a different situation from "all of them are
    busy": an empty disk is permanent, because a fixed K cannot reach further
    and riders never reposition, so the order would retry forever (entry 20).

    This does not reopen entry 2's objection to expanding rings. That was about
    stopping at the first non-empty RING and calling its best rider the nearest.
    Here the whole disk is still ranked - just a larger one.

    Ids are sorted because SUNION returns a set and Python's string hashing is
    randomised per process, so set iteration order changes between runs. Without
    this, two riders with identical travel times would be chosen differently on
    different runs of the same seed, quietly undermining the reproducibility the
    Day 5 benchmark rests on.
    """
    k = config.K_RING
    while k <= config.MAX_K_RING:
        cells = h3.grid_disk(restaurant_cell, k)
        rider_ids = sorted(state.riders_in_cells(cells))
        if rider_ids:
            return rider_ids, k
        k += 1
    return [], config.MAX_K_RING


def assign_one(order: OrderRequest, committed: set[str]) -> Assignment:
    """Pick the cheapest free rider for one order, excluding committed ones."""
    restaurant: Point = (order.lat, order.lng)

    # Pure arithmetic, no network: H3 needs no lookup table to turn a
    # coordinate into a cell, or a cell into its neighbours.
    restaurant_cell = h3.latlng_to_cell(
        order.lat, order.lng, config.H3_RESOLUTION
    )

    candidate_ids, search_k = find_candidate_ids(restaurant_cell)
    riders = state.read_riders(candidate_ids)

    free_riders = [
        rider
        for rider in riders
        if rider.state == state.FREE and rider.rider_id not in committed
    ]

    if not free_riders:
        return Assignment(
            order_id=order.order_id,
            rider_id=None,
            candidates=len(riders),
            free_candidates=0,
            search_k=search_k,
        )

    seconds = travel.travel_times(restaurant, [r.position for r in free_riders])
    costs = [cost_of(s) for s in seconds]

    # Ties break on rider id so the winner is the same on every run. Exact
    # float ties are rare but two riders at one position would produce them.
    ranked = sorted(zip(costs, (rider.rider_id for rider in free_riders)))
    best_cost, best_rider_id = ranked[0]

    return Assignment(
        order_id=order.order_id,
        rider_id=best_rider_id,
        candidates=len(riders),
        free_candidates=len(free_riders),
        search_k=search_k,
    )


def greedy_batch(orders: list[OrderRequest]) -> list[Assignment]:
    """Assign orders one at a time, in the order the caller listed them.

    This is what "greedy" means, and its weakness is visible right here: the
    first order takes the nearest rider with no knowledge that a later order in
    the same batch needed it. Day 5's matching policy replaces this function and
    nothing else.

    `committed` is what makes a batch possible at all. Within one batch the
    service cannot mark a rider busy in Redis (entry 1), so it has to remember
    its own choices or it would hand one rider to two orders.
    """
    committed: set[str] = set()
    assignments = []

    for order in orders:
        assignment = assign_one(order, committed)
        if assignment.rider_id is not None:
            committed.add(assignment.rider_id)
        assignments.append(assignment)

    return assignments


@app.post("/assign_batch")
def assign_batch(request: AssignBatchRequest) -> AssignBatchResponse:
    if config.POLICY == "greedy":
        assignments = greedy_batch(request.orders)
    else:  # pragma: no cover - unreachable, validated at import
        raise AssertionError(config.POLICY)

    return AssignBatchResponse(assignments=assignments, policy=config.POLICY)


if __name__ == "__main__":
    # Exercises the endpoint without running a server. Needs Redis up and a
    # seeded fleet: run `python seed.py` first.
    from fastapi.testclient import TestClient

    state.require_redis()
    client = TestClient(app)

    CENTRE = {"lat": 12.9716, "lng": 77.5946}

    def post(orders):
        reply = client.post("/assign_batch", json={"orders": orders})
        assert reply.status_code == 200, reply.text
        return reply.json()

    # 1. one order in the middle of the fleet gets a rider
    body = post([dict(order_id="a", **CENTRE)])
    assert body["policy"] == config.POLICY
    first = body["assignments"][0]
    assert first["rider_id"] is not None, first
    assert first["search_k"] == config.K_RING, "the centre should not need widening"
    winner = first["rider_id"]
    print("one order at the centre -> %s (%d candidates, %d free, k=%d)"
          % (winner, first["candidates"], first["free_candidates"], first["search_k"]))

    # 2. the same request twice gives the same answer: no hidden randomness
    again = post([dict(order_id="a", **CENTRE)])["assignments"][0]
    assert again["rider_id"] == winner, "dispatch is not deterministic"

    # 3. the winner really is the nearest free rider, checked independently
    ids, _ = find_candidate_ids(
        h3.latlng_to_cell(CENTRE["lat"], CENTRE["lng"], config.H3_RESOLUTION)
    )
    free = [r for r in state.read_riders(ids) if r.state == state.FREE]
    nearest = min(
        free,
        key=lambda r: travel.travel_times(
            (CENTRE["lat"], CENTRE["lng"]), [r.position]
        )[0],
    )
    assert winner == nearest.rider_id, f"{winner} chosen but {nearest.rider_id} is nearer"

    # 4. THE POINT OF A BATCH: two orders at the same restaurant must not get
    #    the same rider, even though nothing was written to Redis between them.
    two = post([dict(order_id="a", **CENTRE), dict(order_id="b", **CENTRE)])
    riders = [a["rider_id"] for a in two["assignments"]]
    assert riders[0] == winner, riders
    assert riders[1] is not None and riders[1] != riders[0], riders
    # The second order sees one fewer assignable rider than the first.
    assert (two["assignments"][1]["free_candidates"]
            == two["assignments"][0]["free_candidates"] - 1), two
    print("two orders at one restaurant -> %s and %s (committed set works)"
          % (riders[0], riders[1]))

    # 5. order of the list is the priority order, and ids map back correctly
    assert [a["order_id"] for a in two["assignments"]] == ["a", "b"]

    # 6. nowhere near the fleet: the fallback widens all the way out, finds
    #    nobody, and still answers HTTP 200 rather than erroring.
    empty = post([{"order_id": "z", "lat": 0.0, "lng": 0.0}])["assignments"][0]
    assert empty == {
        "order_id": "z",
        "rider_id": None,
        "candidates": 0,
        "free_candidates": 0,
        "search_k": config.MAX_K_RING,
    }, empty
    print("middle of the ocean -> no rider, HTTP 200, widened to k=%d and still none"
          % config.MAX_K_RING)

    # 7. an empty batch is valid and still reports the policy, which is how the
    #    simulator detects a service running stale config.
    assert post([]) == {"assignments": [], "policy": config.POLICY}

    # 8. a busy rider is not a candidate: the winner should change
    state.assign_to_restaurant(winner, "selftest-order", (CENTRE["lat"], CENTRE["lng"]))
    try:
        after = post([dict(order_id="a", **CENTRE)])["assignments"][0]
        assert after["rider_id"] != winner, "a busy rider was assigned again"
        assert after["free_candidates"] == first["free_candidates"] - 1
        print("after marking %s busy -> %s (%d free)"
              % (winner, after["rider_id"], after["free_candidates"]))
    finally:
        state.release_rider(winner)

    # 9. the trust boundary rejects nonsense before our code runs
    bad_batches = (
        {"orders": [{"order_id": "a", "lat": "banana", "lng": 77.59}]},
        {"orders": [{"order_id": "a", "lat": 999, "lng": 77.59}]},
        {"orders": [{"order_id": "a", "lat": 12.97}]},
        {"orders": [{"lat": 12.97, "lng": 77.59}]},
        {},
    )
    for bad in bad_batches:
        assert client.post("/assign_batch", json=bad).status_code == 422, bad
    print("bad requests -> 422 before any Redis call")

    print("ok")
