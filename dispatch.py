"""The dispatch service. One endpoint: a restaurant location in, a rider id out.

It reads Redis and never writes to it (DECISIONS.md entry 1), holds no state
between requests, and has no clock. That is deliberate: nothing here knows it is
being driven by a simulator, so the same service could be pointed at real
traffic with sim.py deleted.

Run it with:  uvicorn dispatch:app --reload
Then open http://127.0.0.1:8000/docs to send a request by hand.
"""

import h3
from fastapi import FastAPI
from pydantic import BaseModel, Field

import config
import state
from travel import HaversineProvider, Point

app = FastAPI(title="Rider dispatch")

# One origin, N destinations - the shape this endpoint asks in, and the shape
# OSRM's /table answers in one request when it replaces this.
travel = HaversineProvider(speed_kmh=config.RIDER_SPEED_KMH)


class AssignRequest(BaseModel):
    """The restaurant an order needs collecting from.

    The ranges are the real work here. This is the trust boundary: the data
    arrives from outside the process, so a bad value must be rejected at the
    door rather than crashing somewhere inside h3 with a confusing message.

    Note what this cannot catch: swapping lat and lng for Bangalore produces
    (77.59, 12.97), and latitude 77.59 is perfectly valid - it is in Arctic
    Norway. Consistency of the (lat, lng) convention is the only defence.
    """

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class AssignResponse(BaseModel):
    """The chosen rider, plus enough to tell two failures apart.

    `rider_id` is None when nobody could be assigned, with HTTP 200 rather than
    an error: an order with no rider in range retries on a later tick, so this
    is designed behaviour and happens constantly. Returning 404 would make
    normal operation look like failure and teach the Day 6 dashboards to lie.

    The two counts exist because "no rider" has two different causes with two
    different fixes, and they are indistinguishable from outside:
      candidates == 0        -> nobody is nearby at all: K or the fleet is small
      free_candidates == 0   -> riders are nearby but all busy: the fleet is
                                saturated, which is a load problem, not a
                                geometry one
    """

    rider_id: str | None
    candidates: int
    free_candidates: int


def cost_of(travel_seconds: float) -> float:
    """What dispatch ranks on. Lowest wins.

    Plain travel time to the restaurant. The exact objective would be
    max(travel, prep_remaining), since the food is not collectable until prep
    finishes - but measured against this configuration it cannot rank
    differently: the K=2 disk is 341 seconds of travel against a 720 second
    prep, so every reachable rider arrives early and they all tie. The proxy and
    the exact objective order identically here, so the proxy wins.

    It would start to differ with a shorter prep time, a much larger K, or an
    order that has already waited 720+ seconds unassigned.
    """
    return travel_seconds


@app.post("/assign")
def assign(request: AssignRequest) -> AssignResponse:
    restaurant: Point = (request.lat, request.lng)

    # Pure arithmetic, no network: H3 needs no lookup table to turn a
    # coordinate into a cell, or a cell into its neighbours.
    restaurant_cell = h3.latlng_to_cell(
        request.lat, request.lng, config.H3_RESOLUTION
    )
    search_cells = h3.grid_disk(restaurant_cell, config.K_RING)

    # Sorted because SUNION returns a set, and Python's string hashing is
    # randomised per process - so set iteration order changes between runs.
    # Without this, two riders with identical travel times would be chosen
    # differently on different runs of the same seed, which would quietly
    # undermine the reproducibility the whole benchmark rests on.
    candidate_ids = sorted(state.riders_in_cells(search_cells))

    riders = state.read_riders(candidate_ids)
    free_riders = [rider for rider in riders if rider.state == state.FREE]

    if not free_riders:
        return AssignResponse(
            rider_id=None,
            candidates=len(riders),
            free_candidates=0,
        )

    seconds = travel.travel_times(restaurant, [r.position for r in free_riders])
    costs = [cost_of(s) for s in seconds]

    # Ties break on rider id so the winner is the same on every run. Exact
    # float ties are rare but two riders at one position would produce them.
    ranked = sorted(zip(costs, (rider.rider_id for rider in free_riders)))
    best_cost, best_rider_id = ranked[0]

    return AssignResponse(
        rider_id=best_rider_id,
        candidates=len(riders),
        free_candidates=len(free_riders),
    )


if __name__ == "__main__":
    # Exercises the endpoint without running a server. Needs Redis up and a
    # seeded fleet: run `python seed.py` first.
    from fastapi.testclient import TestClient

    client = TestClient(app)

    # 1. a restaurant in the middle of the fleet gets a rider
    reply = client.post("/assign", json={"lat": 12.9716, "lng": 77.5946})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["rider_id"] is not None, body
    assert body["candidates"] > 0
    assert body["free_candidates"] > 0
    winner = body["rider_id"]
    print("centre of the city -> %s (%d candidates, %d free)"
          % (winner, body["candidates"], body["free_candidates"]))

    # 2. the same request twice gives the same answer: no hidden randomness
    again = client.post("/assign", json={"lat": 12.9716, "lng": 77.5946}).json()
    assert again["rider_id"] == winner, "dispatch is not deterministic"

    # 3. the winner really is the nearest free rider, checked independently
    riders = state.read_riders(sorted(state.riders_in_cells(
        h3.grid_disk(h3.latlng_to_cell(12.9716, 77.5946, config.H3_RESOLUTION),
                     config.K_RING))))
    free = [r for r in riders if r.state == state.FREE]
    nearest = min(free, key=lambda r: travel.travel_times(
        (12.9716, 77.5946), [r.position])[0])
    assert winner == nearest.rider_id, f"{winner} chosen but {nearest.rider_id} is nearer"

    # 4. nowhere near the fleet: no candidates at all, still HTTP 200
    empty = client.post("/assign", json={"lat": 0.0, "lng": 0.0})
    assert empty.status_code == 200
    assert empty.json() == {"rider_id": None, "candidates": 0, "free_candidates": 0}
    print("middle of the ocean -> no rider, HTTP 200, candidates 0")

    # 5. a busy rider is not a candidate: the winner should change
    state.assign_to_restaurant(winner, "selftest-order", (12.9716, 77.5946))
    try:
        after = client.post("/assign", json={"lat": 12.9716, "lng": 77.5946}).json()
        assert after["rider_id"] != winner, "a busy rider was assigned again"
        assert after["free_candidates"] == body["free_candidates"] - 1
        print("after marking %s busy -> %s (%d free)"
              % (winner, after["rider_id"], after["free_candidates"]))
    finally:
        state.release_rider(winner)

    # 6. the trust boundary rejects nonsense before our code runs
    for bad in ({"lat": "banana", "lng": 77.59}, {"lat": 999, "lng": 77.59},
                {"lat": 12.97}, {}):
        assert client.post("/assign", json=bad).status_code == 422, bad
    print("bad requests -> 422 before any Redis call")

    print("ok")
