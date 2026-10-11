"""The dispatch service. A batch of restaurant locations in, rider ids out.

It reads Redis and never writes to it (DECISIONS.md entry 1), holds no state
between requests, and has no clock. That is deliberate: nothing here knows it is
being driven by a simulator, so the same service could be pointed at real
traffic with sim.py deleted.

Two policies live here, behind one endpoint:

  greedy    assigns orders one at a time, oldest first. Each order takes its
            own cheapest rider, with no knowledge of the orders behind it.

  matching  decides the whole batch at once, minimising total travel time.

Run it with:  uvicorn dispatch:app --host 127.0.0.1 --port 8001
Then open http://127.0.0.1:8001/docs to send a request by hand.
"""

from dataclasses import dataclass, field

import h3
import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field
from scipy.optimize import linear_sum_assignment

import config
import state
from travel import HaversineProvider, Point

# Policies live here rather than in the simulator, because the dispatch service
# is the dispatcher - moving the decision out would leave it a candidate-finder
# (entry 22). Validated at import so a typo in config fails at startup instead
# of halfway through a benchmark.
KNOWN_POLICIES = ("greedy", "matching")
if config.POLICY not in KNOWN_POLICIES:
    raise SystemExit(
        f"config.POLICY is {config.POLICY!r}, which is not one of {KNOWN_POLICIES}"
    )

# A finite stand-in for "this rider cannot serve this order", used to fill the
# gaps in the cost matrix. Deliberately NOT math.inf: inf - inf is nan, and the
# row-subtraction step inside the assignment algorithm would poison the whole
# matrix with it. Large enough that a real pairing always wins.
IMPOSSIBLE_COST = 1e9

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

    # How long this order has already been waiting for a rider. Data, not a
    # clock: the service never asks what time it is, it is told how long. That
    # keeps the service clock-free (entry 12 context) while letting a policy
    # prioritise orders that have been queuing.
    waited_seconds: float = Field(ge=0, default=0.0)


class AssignBatchRequest(BaseModel):
    """Several orders to decide together.

    A batch rather than one order per call because a batching policy must avoid
    handing the same rider to two orders *inside its own decision*, before
    anything reaches Redis - and it cannot use Redis to track that, since the
    service never writes. A one-order-at-a-time endpoint structurally cannot
    express batching (entry 22).

    The list order is the caller's PRIORITY order. The simulator sends oldest
    first. Greedy honours that order; matching ignores it and optimises across
    the whole batch, which is exactly the difference between them.
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
      free_candidates == 0   -> riders nearby but all busy: saturation

    Both counts are POLICY-INDEPENDENT - they describe the riders near this
    order, not what the policy did with them - so the same number means the
    same thing when comparing two policies.
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

    `policy` is not decoration. A service started before config.POLICY changed
    would silently run the old policy while the caller believed otherwise,
    benchmarking greedy against greedy and looking like a real result. The
    simulator checks this field at startup, which turns a ruined experiment into
    an error.
    """

    assignments: list[Assignment]
    policy: str


@dataclass
class Candidates:
    """The riders available to one order, with their costs.

    Gathered by `gather_candidates` and shared by BOTH policies, so the only
    difference between greedy and matching is what they do with an identical
    set of options. If each policy found its own candidates, a difference
    between them could come from the search rather than the decision.
    """

    order: OrderRequest
    search_k: int
    candidates: int
    free_rider_ids: list[str] = field(default_factory=list)
    cost_by_rider: dict[str, float] = field(default_factory=dict)

    @property
    def restaurant(self) -> Point:
        return (self.order.lat, self.order.lng)


def cost_of(travel_seconds: float, waited_seconds: float) -> float:
    """What dispatch ranks on. Lowest wins.

    Travel time to the restaurant, discounted by how long the order has already
    waited. The discount exists because entry 26 measured pure travel cost
    starving old orders: matching turned a 44 minute p90 into 62 on one seed by
    spending dispatch promptness on travel savings that prep time absorbs.

    AGEING_WEIGHT is "seconds of travel we will spend to save a second of
    waiting", set to 1.0 on principle rather than tuned.

    Two invariances worth knowing, because both are provable rather than hoped:

      * It cannot change GREEDY at all. The discount is identical for every
        rider of a given order, so the cheapest rider for that order is the same
        rider either way.

      * It cannot change MATCHING when riders outnumber orders. Every order is
        assigned regardless, and subtracting a constant from a whole row does
        not change which assignment is optimal (Day 5 Part 2).

    It bites only when riders are scarcer than orders, where the solver must
    choose WHICH orders to serve and a discounted row is cheaper to include.
    That is exactly the starvation case.
    """
    return travel_seconds - config.AGEING_WEIGHT * waited_seconds


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
    benchmark rests on.
    """
    k = config.K_RING
    while k <= config.MAX_K_RING:
        cells = h3.grid_disk(restaurant_cell, k)
        rider_ids = sorted(state.riders_in_cells(cells))
        if rider_ids:
            return rider_ids, k
        k += 1
    return [], config.MAX_K_RING


def gather_candidates(order: OrderRequest) -> Candidates:
    """Find the free riders near one order and cost each of them."""
    restaurant_cell = h3.latlng_to_cell(
        order.lat, order.lng, config.H3_RESOLUTION
    )
    candidate_ids, search_k = find_candidate_ids(restaurant_cell)
    riders = state.read_riders(candidate_ids)

    free = [rider for rider in riders if rider.state == state.FREE]
    gathered = Candidates(
        order=order,
        search_k=search_k,
        candidates=len(riders),
        free_rider_ids=[rider.rider_id for rider in free],
    )

    if free:
        seconds = travel.travel_times(
            gathered.restaurant, [rider.position for rider in free]
        )
        gathered.cost_by_rider = {
            rider.rider_id: cost_of(second, order.waited_seconds)
            for rider, second in zip(free, seconds)
        }

    return gathered


def _assignment(gathered: Candidates, rider_id: str | None) -> Assignment:
    return Assignment(
        order_id=gathered.order.order_id,
        rider_id=rider_id,
        candidates=gathered.candidates,
        free_candidates=len(gathered.free_rider_ids),
        search_k=gathered.search_k,
    )


# --- the policies ------------------------------------------------------------


def greedy_batch(orders: list[OrderRequest]) -> list[Assignment]:
    """Assign orders one at a time, in the order the caller listed them.

    This is what "greedy" means, and its weakness is visible right here: the
    first order takes its nearest rider with no knowledge that a later order in
    the same batch needed it. Measured on a two-order example, that costs 50% in
    total distance and more than doubles the worst single order.

    `committed` is what makes a batch possible at all. Within one batch the
    service cannot mark a rider busy in Redis (entry 1), so it has to remember
    its own choices or it would hand one rider to two orders.
    """
    committed: set[str] = set()
    assignments = []

    for order in orders:
        gathered = gather_candidates(order)

        available = [
            (cost, rider_id)
            for rider_id, cost in gathered.cost_by_rider.items()
            if rider_id not in committed
        ]

        if not available:
            assignments.append(_assignment(gathered, None))
            continue

        # Ties break on rider id so the winner is the same on every run. Exact
        # float ties are rare but two riders at one position would produce them.
        best_cost, best_rider_id = min(sorted(available))
        committed.add(best_rider_id)
        assignments.append(_assignment(gathered, best_rider_id))

    return assignments


def matching_batch(orders: list[OrderRequest]) -> list[Assignment]:
    """Decide the whole batch at once, minimising total travel time.

    Builds a cost matrix - one row per order, one column per rider free for at
    least one of them - and solves the assignment problem exactly. Pairs that
    are impossible (that rider is not near that order) are filled with
    IMPOSSIBLE_COST, and any chosen pair still carrying that value is dropped
    afterwards as unassigned.

    Because IMPOSSIBLE_COST dwarfs every real cost, the solver avoids those
    cells before it economises on distance. The practical effect is that it
    maximises how many orders get a rider FIRST, and minimises total travel time
    among the ways of doing that - which is why it can assign strictly more
    orders than greedy, not merely assign them better.

    No fairness term, deliberately (entry 25): this ignores how long an order
    has waited, so it may serve a new order and leave an old one queuing. The
    `max dispatch wait` guardrail exists to measure that cost rather than
    assume it away.
    """
    if not orders:
        return []

    gathered_all = [gather_candidates(order) for order in orders]

    # Columns: every rider free for at least one order in the batch. Sorted so
    # the matrix is built identically on every run.
    rider_ids = sorted(
        {rider_id for g in gathered_all for rider_id in g.cost_by_rider}
    )

    if not rider_ids:
        return [_assignment(g, None) for g in gathered_all]

    matrix = np.array(
        [
            [g.cost_by_rider.get(rider_id, IMPOSSIBLE_COST) for rider_id in rider_ids]
            for g in gathered_all
        ]
    )

    chosen_rows, chosen_cols = linear_sum_assignment(matrix)

    rider_for_row = {}
    for row, col in zip(chosen_rows, chosen_cols):
        if matrix[row][col] < IMPOSSIBLE_COST:
            rider_for_row[int(row)] = rider_ids[col]

    return [
        _assignment(gathered, rider_for_row.get(index))
        for index, gathered in enumerate(gathered_all)
    ]


POLICIES = {"greedy": greedy_batch, "matching": matching_batch}


@app.post("/assign_batch")
def assign_batch(request: AssignBatchRequest) -> AssignBatchResponse:
    # Read at request time, not import time, so a benchmark running in one
    # process can switch policy between phases.
    policy = config.POLICY
    assignments = POLICIES[policy](request.orders)
    return AssignBatchResponse(assignments=assignments, policy=policy)


if __name__ == "__main__":
    # Exercises both policies without running a server. Needs Redis up and a
    # seeded fleet: run `python seed.py` first.
    import itertools

    from fastapi.testclient import TestClient

    state.require_redis()
    client = TestClient(app)

    CENTRE = {"lat": 12.9716, "lng": 77.5946}
    original_policy = config.POLICY

    def post(orders):
        reply = client.post("/assign_batch", json={"orders": orders})
        assert reply.status_code == 200, reply.text
        return reply.json()

    # ---- the parts that are true of both policies ----
    for policy in KNOWN_POLICIES:
        config.POLICY = policy

        body = post([dict(order_id="a", **CENTRE)])
        assert body["policy"] == policy
        first = body["assignments"][0]
        assert first["rider_id"] is not None, first
        assert first["search_k"] == config.K_RING

        # same request twice -> same answer: no hidden randomness
        again = post([dict(order_id="a", **CENTRE)])["assignments"][0]
        assert again["rider_id"] == first["rider_id"], f"{policy} is not deterministic"

        # a single order must go to the genuinely nearest free rider, under
        # either policy: with one order there is nothing to trade off.
        gathered = gather_candidates(OrderRequest(order_id="a", **CENTRE))
        nearest = min(gathered.cost_by_rider.items(), key=lambda kv: (kv[1], kv[0]))[0]
        assert first["rider_id"] == nearest, (policy, first["rider_id"], nearest)

        # two orders at one restaurant must not share a rider, even though
        # nothing was written to Redis between them
        two = post([dict(order_id="a", **CENTRE), dict(order_id="b", **CENTRE)])
        riders = [a["rider_id"] for a in two["assignments"]]
        assert None not in riders and riders[0] != riders[1], (policy, riders)
        assert [a["order_id"] for a in two["assignments"]] == ["a", "b"]

        # nowhere near the fleet: widen all the way out, find nobody, still 200
        empty = post([{"order_id": "z", "lat": 0.0, "lng": 0.0}])["assignments"][0]
        assert empty["rider_id"] is None and empty["candidates"] == 0
        assert empty["search_k"] == config.MAX_K_RING

        # an empty batch still reports the policy, which is how the simulator
        # detects a service running stale config
        assert post([]) == {"assignments": [], "policy": policy}

        print("%-9s single order -> %s, two orders -> %s and %s"
              % (policy, first["rider_id"], riders[0], riders[1]))

    # ---- the two ageing invariances, asserted rather than hoped ----
    def riders_for(policy, waits):
        config.POLICY = policy
        body = post([
            dict(order_id=name, waited_seconds=wait, **CENTRE)
            for name, wait in zip("ab", waits)
        ])
        return [a["rider_id"] for a in body["assignments"]]

    # 1. ageing cannot change greedy: the discount is identical for every rider
    #    of a given order, so that order's cheapest rider is the same either way.
    assert riders_for("greedy", (0, 0)) == riders_for("greedy", (0, 5000)), (
        "ageing changed greedy, which is mathematically impossible"
    )

    # 2. nor matching, while riders outnumber orders: every order is assigned
    #    regardless, and a row-constant shift cannot change the optimum.
    assert riders_for("matching", (0, 0)) == riders_for("matching", (0, 5000)), (
        "ageing changed matching despite riders outnumbering orders"
    )
    print("ageing changes neither policy here, exactly as the maths predicts")
    print()

    # ---- matching must never be worse than greedy on the same batch ----
    # Several real restaurant positions spread around the city.
    from travel import km_to_degrees

    _, per_km_lng = km_to_degrees(1.0, CENTRE["lat"])
    per_km_lat, _ = km_to_degrees(1.0, CENTRE["lat"])

    def spot(east_km, north_km, order_id):
        return {
            "order_id": order_id,
            "lat": CENTRE["lat"] + north_km * per_km_lat,
            "lng": CENTRE["lng"] + east_km * per_km_lng,
        }

    batches = [
        [spot(0, 0, "a"), spot(1.0, 0, "b")],
        [spot(-0.8, 0.3, "a"), spot(0.9, -0.4, "b")],
        [spot(0, 0, "a"), spot(0.7, 0.7, "b"), spot(-0.6, 0.9, "c")],
        [spot(0.2, 0.2, "a"), spot(0.3, -0.2, "b"), spot(-0.3, 0.1, "c")],
    ]

    def totals(assignments, gathered_all):
        """How many orders got a rider, and the total cost of those pairings."""
        by_id = {g.order.order_id: g for g in gathered_all}
        assigned = [a for a in assignments if a["rider_id"] is not None]
        cost = sum(by_id[a["order_id"]].cost_by_rider[a["rider_id"]] for a in assigned)
        return len(assigned), cost

    def brute_force_best(gathered_all):
        """Max assignments then min cost, by exhaustive search. Test only."""
        rider_ids = sorted({r for g in gathered_all for r in g.cost_by_rider})
        best = (-1, 0.0)
        for choice in itertools.product([None] + rider_ids, repeat=len(gathered_all)):
            used = [r for r in choice if r is not None]
            if len(used) != len(set(used)):
                continue
            if any(
                r is not None and r not in g.cost_by_rider
                for g, r in zip(gathered_all, choice)
            ):
                continue
            cost = sum(
                g.cost_by_rider[r]
                for g, r in zip(gathered_all, choice)
                if r is not None
            )
            if len(used) > best[0] or (len(used) == best[0] and cost < best[1]):
                best = (len(used), cost)
        return best

    print()
    print("  batch   greedy count/cost     matching count/cost   optimal count/cost")
    for batch in batches:
        gathered_all = [gather_candidates(OrderRequest(**o)) for o in batch]

        config.POLICY = "greedy"
        greedy_result = totals(post(batch)["assignments"], gathered_all)
        config.POLICY = "matching"
        matching_result = totals(post(batch)["assignments"], gathered_all)
        optimal = brute_force_best(gathered_all)

        print("  %5d   %5d / %8.1f s   %5d / %8.1f s   %5d / %8.1f s"
              % (len(batch), greedy_result[0], greedy_result[1],
                 matching_result[0], matching_result[1], optimal[0], optimal[1]))

        # matching must equal the exhaustive optimum: this is what tests OUR
        # matrix construction, which is where a bug would be - not scipy.
        assert matching_result[0] == optimal[0], (matching_result, optimal)
        assert abs(matching_result[1] - optimal[1]) < 1e-6, (matching_result, optimal)

        # and it must never do worse than greedy
        assert matching_result[0] >= greedy_result[0], (matching_result, greedy_result)
        if matching_result[0] == greedy_result[0]:
            assert matching_result[1] <= greedy_result[1] + 1e-6, (
                matching_result, greedy_result
            )

    config.POLICY = original_policy

    # ---- the trust boundary ----
    bad_batches = (
        {"orders": [{"order_id": "a", "lat": "banana", "lng": 77.59}]},
        {"orders": [{"order_id": "a", "lat": 999, "lng": 77.59}]},
        {"orders": [{"order_id": "a", "lat": 12.97}]},
        {"orders": [{"lat": 12.97, "lng": 77.59}]},
        {},
    )
    for bad in bad_batches:
        assert client.post("/assign_batch", json=bad).status_code == 422, bad

    print()
    print("matching equals the exhaustive optimum on every batch above")
    print("bad requests -> 422 before any Redis call")
    print("ok")
