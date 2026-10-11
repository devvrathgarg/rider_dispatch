"""Metric definitions. The single place that says what a good run means.

Entries 21 and 23 settled these:

  PRIMARY    p90 delivery time. What the customer feels, and stable enough to
             mean something at ~100 orders - unlike p99, which at 90 orders is
             literally the single worst order wearing a statistic's name.

  GUARDRAILS p50 delivery time       - catches making everyone slightly worse
             max dispatch wait       - catches starving one order
             p50 rider idle at shop  - catches making riders wait instead

  DIAGNOSTIC p50/p90 dispatch wait. About 21 of a 26 minute delivery is
             policy-independent (12 prep, 9 on the second leg), so the headline
             metric is deliberately insensitive. These show whether a change in
             it came from the part the policy actually controls.

Every rate and utilisation figure names its window. Over the whole run,
utilisation reads 27.9%; over the order window, 34.1%. Neither is wrong, but an
unstated one is useless, so the convention is the order window.

This module reads attributes rather than a fixed type, so it works on sim.py's
Order objects and on rows loaded back from a CSV without either knowing about
the other - the same structural-typing idea as TravelTimeProvider.
"""

import csv
import statistics
from dataclasses import dataclass
from types import SimpleNamespace

CSV_NUMERIC_FIELDS = (
    "created",
    "assigned",
    "prep_done",
    "arrived_at_restaurant",
    "picked_up",
    "delivered",
)
CSV_INT_FIELDS = ("attempts_no_candidate", "attempts_all_busy", "search_k")


def percentile(values, p: float) -> float:
    """Nearest-rank percentile: the value at or above which p% of the data sits.

    Deliberately not interpolated. With ~100 samples, interpolating invents a
    number between two real observations and makes the result look more precise
    than the data is. Nearest-rank always returns an actual measurement.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no data")
    index = int(round(p / 100 * len(ordered))) - 1
    return ordered[max(0, min(len(ordered) - 1, index))]


@dataclass(frozen=True)
class RunMetrics:
    """One run, measured. Frozen because a measurement should not be edited."""

    orders: int

    # Primary
    p90_delivery_min: float

    # Guardrails
    p50_delivery_min: float
    max_dispatch_wait_s: float
    p50_idle_at_shop_min: float

    # Diagnostic: the part the policy actually controls
    p50_dispatch_wait_s: float
    p90_dispatch_wait_s: float

    # Reported, not judged
    max_delivery_min: float
    utilisation_pct: float
    throughput_per_hour: float
    riders_used: int
    riders_unused: int
    max_orders_one_rider: int
    orders_needing_fallback: int
    orders_delayed_by_geometry: int
    orders_delayed_by_saturation: int
    busy_time_spent_waiting_pct: float

    def as_row(self) -> dict:
        return {
            "p90_delivery_min": self.p90_delivery_min,
            "p50_delivery_min": self.p50_delivery_min,
            "max_dispatch_wait_s": self.max_dispatch_wait_s,
            "p50_idle_at_shop_min": self.p50_idle_at_shop_min,
            "p90_dispatch_wait_s": self.p90_dispatch_wait_s,
            "utilisation_pct": self.utilisation_pct,
        }


def compute(orders, fleet_size: int, window_seconds: float) -> RunMetrics:
    """Measure one run.

    `window_seconds` is the ORDER WINDOW, not the run length. The drain phase
    has no orders arriving by construction, so including it in a denominator
    drags utilisation and throughput down for a reason that has nothing to do
    with the policy.
    """
    if not orders:
        raise ValueError("no orders to measure")

    delivery_min = [(o.delivered - o.created) / 60 for o in orders]
    dispatch_wait_s = [o.assigned - o.created for o in orders]
    idle_at_shop_min = [(o.picked_up - o.arrived_at_restaurant) / 60 for o in orders]

    # Busy time clipped to the window, so a delivery finishing during the drain
    # does not contribute seconds the denominator never counted.
    busy_s = sum(
        max(0.0, min(o.delivered, window_seconds) - max(o.assigned, 0.0))
        for o in orders
    )
    waiting_s = sum(
        max(0.0, min(o.picked_up, window_seconds) - max(o.arrived_at_restaurant, 0.0))
        for o in orders
    )

    rider_loads: dict[str, int] = {}
    for order in orders:
        rider_loads[order.rider_id] = rider_loads.get(order.rider_id, 0) + 1

    return RunMetrics(
        orders=len(orders),
        p90_delivery_min=percentile(delivery_min, 90),
        p50_delivery_min=percentile(delivery_min, 50),
        max_dispatch_wait_s=max(dispatch_wait_s),
        p50_idle_at_shop_min=percentile(idle_at_shop_min, 50),
        p50_dispatch_wait_s=percentile(dispatch_wait_s, 50),
        p90_dispatch_wait_s=percentile(dispatch_wait_s, 90),
        max_delivery_min=max(delivery_min),
        utilisation_pct=100 * busy_s / (window_seconds * fleet_size),
        throughput_per_hour=len(orders) / (window_seconds / 3600),
        riders_used=len(rider_loads),
        riders_unused=fleet_size - len(rider_loads),
        max_orders_one_rider=max(rider_loads.values()),
        orders_needing_fallback=sum(1 for o in orders if o.search_k > 2),
        orders_delayed_by_geometry=sum(1 for o in orders if o.attempts_no_candidate),
        orders_delayed_by_saturation=sum(1 for o in orders if o.attempts_all_busy),
        busy_time_spent_waiting_pct=100 * waiting_s / busy_s if busy_s else 0.0,
    )


def load_csv(path: str) -> list:
    """Rows from a CSV, as objects with the attributes `compute` expects.

    SimpleNamespace rather than a second dataclass mirroring sim.Order: one
    definition of an order is enough, and `compute` only ever reads attributes.
    """
    with open(path, newline="", encoding="utf-8") as handle:
        rows = []
        for row in csv.DictReader(handle):
            values = {"order_id": row["order_id"], "rider_id": row["rider_id"]}
            for field in CSV_NUMERIC_FIELDS:
                values[field] = float(row[field])
            for field in CSV_INT_FIELDS:
                values[field] = int(row[field])
            rows.append(SimpleNamespace(**values))
        return rows


def paired_deltas(baseline: list[RunMetrics], candidate: list[RunMetrics], field: str):
    """Per-seed differences for one metric: candidate minus baseline.

    Paired rather than two group averages. Seed-to-seed variation is large -
    order counts ranged 90 to 126 across eight seeds - and both policies face
    the same world on the same seed, so that shared difficulty cancels out of
    the difference (entry 23). Comparing two averages instead would carry all of
    it into both numbers.
    """
    if len(baseline) != len(candidate):
        raise ValueError("paired comparison needs the same seeds on both sides")
    return [
        getattr(after, field) - getattr(before, field)
        for before, after in zip(baseline, candidate)
    ]


if __name__ == "__main__":
    # percentile: the properties that matter, on data small enough to check by
    # hand.
    data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(data, 50) == 5
    assert percentile(data, 90) == 9
    assert percentile(data, 100) == 10
    assert percentile(data, 1) == 1          # clamps rather than going negative
    assert percentile([7], 90) == 7          # single sample
    assert percentile([5, 1, 3], 50) == 3    # sorts its input

    # Every result is a real observation, never an invented in-between value.
    for p in range(1, 101):
        assert percentile(data, p) in data

    try:
        percentile([], 50)
    except ValueError:
        pass
    else:
        raise AssertionError("percentile of nothing must raise")

    # compute: one synthetic order whose numbers are all checkable by hand.
    one = SimpleNamespace(
        order_id="o1",
        rider_id="r1",
        created=0.0,
        assigned=10.0,
        prep_done=720.0,
        arrived_at_restaurant=100.0,
        picked_up=720.0,
        delivered=1320.0,
        search_k=2,
        attempts_no_candidate=0,
        attempts_all_busy=0,
    )
    m = compute([one], fleet_size=10, window_seconds=3600)
    assert m.orders == 1
    assert m.p90_delivery_min == 22.0                 # 1320 s
    assert m.max_dispatch_wait_s == 10.0
    assert m.p50_idle_at_shop_min == 620 / 60         # 720 - 100
    assert m.riders_used == 1 and m.riders_unused == 9
    # busy 1320 - 10 = 1310 s of 10 riders x 3600 s
    assert abs(m.utilisation_pct - 100 * 1310 / 36000) < 1e-9
    # of that busy time, 620 s was standing at the restaurant
    assert abs(m.busy_time_spent_waiting_pct - 100 * 620 / 1310) < 1e-9

    # The window is the order window, not the run length: a delivery finishing
    # after it contributes only the seconds inside it.
    clipped = compute([one], fleet_size=10, window_seconds=1000)
    assert abs(clipped.utilisation_pct - 100 * (1000 - 10) / 10000) < 1e-9

    # paired_deltas lines up by position and refuses mismatched lengths.
    a = compute([one], 10, 3600)
    assert paired_deltas([a], [a], "p90_delivery_min") == [0.0]
    try:
        paired_deltas([a], [a, a], "p90_delivery_min")
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched seed counts must raise")

    print("ok")
