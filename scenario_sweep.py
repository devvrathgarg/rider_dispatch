"""Does matching pay off when prep time stops absorbing travel?

    python scenario_sweep.py          8 seeds
    python scenario_sweep.py 12       more seeds, slower

Entries 17, 23, 26 and 27 together say matching cannot win in the default
configuration for one specific reason: a 12-minute prep absorbs first-leg
travel, so arriving 200 seconds sooner changes the customer's delivery time by
zero. If that reason is correct, then REMOVING the absorption should make
matching start to pay - and if it does not, the explanation was wrong.

That is the point of this script. It is a test of the explanation, not a search
for a flattering configuration.

Why prep time and not city size
-------------------------------
City size was the obvious knob and it is the wrong one. First-leg travel is
bounded by K_RING, not by the city: a k=2 disk is ~2.4 km across however large
the city is. Growing the city only makes disks emptier, giving matching FEWER
options. What grows is leg two, restaurant to customer - which is identical for
every candidate rider and therefore cannot change which one wins (Day 5 Part 2).

Prep time attacks the mechanism directly. At PREP_SECONDS = 0 the first leg
passes straight into delivery time and travel savings are real.

The confound, and the compensation
----------------------------------
Lowering prep shortens how long a rider is occupied per order, which raises
fleet capacity and so LOWERS the load factor. Less congestion means smaller
batches and less for matching to do, which would mask the very effect being
tested. So ORDERS_PER_HOUR is scaled with estimated capacity to hold the load
factor constant, leaving prep as the only thing that varies.
"""

import statistics
import sys
import time

import config
import metrics
import state
from fastapi.testclient import TestClient

# Held constant across every scenario, so only the travel-to-prep ratio moves.
TARGET_LOAD = 0.60

# Rough per-order occupancy is prep + second leg + first leg. The second leg
# averages 0.5214 x city size for two uniform random points in a square, and the
# first leg is short because K bounds it.
SECOND_LEG_SECONDS = 0.5214 * config.CITY_SIZE_KM / config.RIDER_SPEED_KMH * 3600
FIRST_LEG_SECONDS = 100.0

PREP_VALUES = (720, 240, 0)

REPORTED = (
    ("p90 delivery (min)", "p90_delivery_min"),
    ("p50 delivery (min)", "p50_delivery_min"),
    ("max dispatch wait (s)", "max_dispatch_wait_s"),
)


def rate_for(prep_seconds: float) -> float:
    """Orders per hour that holds the load factor at TARGET_LOAD."""
    occupancy = prep_seconds + SECOND_LEG_SECONDS + FIRST_LEG_SECONDS
    capacity_per_hour = config.FLEET_SIZE * 3600 / occupancy
    return round(TARGET_LOAD * capacity_per_hour)


def run_one(client, seed: int, policy: str) -> metrics.RunMetrics:
    from sim import Simulator

    config.POLICY = policy
    simulator = Simulator(client, seed)
    simulator.run()
    simulator.check_invariants()
    return metrics.compute(
        simulator.completed, config.FLEET_SIZE, config.RUN_SECONDS
    )


def main(seed_count: int) -> None:
    state.require_redis()
    import dispatch  # noqa: F401  - imported for its app

    original = (config.POLICY, config.PREP_SECONDS, config.ORDERS_PER_HOUR)
    client = TestClient(dispatch.app)
    seeds = list(range(config.RANDOM_SEED, config.RANDOM_SEED + seed_count))

    print("second leg averages %.0f s at a %.0f km city and %.0f km/h"
          % (SECOND_LEG_SECONDS, config.CITY_SIZE_KM, config.RIDER_SPEED_KMH))
    print("ageing weight %.2f, load held at %.0f%%, %d seeds"
          % (config.AGEING_WEIGHT, 100 * TARGET_LOAD, seed_count))
    print()
    print("  prep   orders/hr   travel/prep   what it tests")
    for prep in PREP_VALUES:
        ratio = "infinite" if prep == 0 else "%.2f" % (FIRST_LEG_SECONDS / prep)
        note = {720: "absorbed (default)", 240: "partly absorbed",
                0: "not absorbed at all"}[prep]
        print("  %4d   %9d   %11s   %s" % (prep, rate_for(prep), ratio, note))
    print()

    results = []
    started = time.perf_counter()
    try:
        for prep in PREP_VALUES:
            config.PREP_SECONDS = prep
            config.ORDERS_PER_HOUR = rate_for(prep)

            baseline, candidate = [], []
            for seed in seeds:
                baseline.append(run_one(client, seed, "greedy"))
                candidate.append(run_one(client, seed, "matching"))

            results.append((prep, baseline, candidate))
            deltas = metrics.paired_deltas(baseline, candidate, "p90_delivery_min")
            print("  prep %4d s done   median p90 delta %+.2f min"
                  % (prep, statistics.median(deltas)))
    finally:
        config.POLICY, config.PREP_SECONDS, config.ORDERS_PER_HOUR = original

    print()
    print("%d runs in %.0f s" % (2 * seed_count * len(PREP_VALUES),
                                 time.perf_counter() - started))
    print()

    for label, field in REPORTED:
        print("%s   (matching minus greedy; negative = matching better)" % label)
        print("   prep   better   worse    tied     median      worst      mean")
        for prep, baseline, candidate in results:
            deltas = metrics.paired_deltas(baseline, candidate, field)
            print("   %4d   %6d  %6d  %6d  %+9.2f  %+9.2f  %+9.2f"
                  % (prep,
                     sum(1 for d in deltas if d < 0),
                     sum(1 for d in deltas if d > 0),
                     sum(1 for d in deltas if d == 0),
                     statistics.median(deltas), max(deltas),
                     statistics.mean(deltas)))
        print()

    primary = [
        (prep, statistics.median(
            metrics.paired_deltas(b, c, "p90_delivery_min")))
        for prep, b, c in results
    ]
    print("THE TEST")
    print("  If prep absorbing travel is why matching could not win, the median")
    print("  p90 delta should move toward negative as prep falls:")
    for prep, median in primary:
        print("    prep %4d s -> %+.2f min" % (prep, median))
    improving = all(
        primary[i][1] >= primary[i + 1][1] for i in range(len(primary) - 1)
    )
    print()
    print("  monotonic in the predicted direction: %s" % ("YES" if improving else "NO"))
    if not improving:
        print("  The explanation in entries 26 and 27 does not survive this test")
        print("  and needs reopening rather than patching.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
