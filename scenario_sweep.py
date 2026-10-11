"""Does matching pay off when prep time stops absorbing travel?

    python scenario_sweep.py                    8 seeds from RANDOM_SEED
    python scenario_sweep.py 20 62              20 seeds starting at 62
    python scenario_sweep.py 20 62 720,120      ...and only these prep values

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

Prep time attacks the mechanism directly. The values swept are not arbitrary
and none of them is zero - a kitchen that takes no time is not a restaurant.
Each one describes a real kind of food business:

  720 s (12 min)  a full-service kitchen. Travel is a third of prep, so it is
                  absorbed. This is the default configuration.
  300 s ( 5 min)  a quick-service counter. Travel and prep are comparable.
  120 s ( 2 min)  coffee, or pre-made items from a kiosk. Travel now EXCEEDS
                  prep, so the first leg is exposed.

A real delivery platform serves all three at once. The question is which regime
batched matching is worth deploying for.

Why these absolute numbers are defensible
-----------------------------------------
Travel here is only 1-5 minutes, which sounds unrealistically small. It is
small, and it does not matter, because what governs the result is travel
RELATIVE to prep - prep is what absorbs travel savings, so the ratio is the
governing quantity and the absolute minutes are not.

    this simulator      real city (5 km dispatch radius, ~20 km/h)
    7 km city, K=2       candidate travel 180-900 s, mean 540 s
    travel mean 230 s
      prep 720 -> 0.32     prep 900 (full-service, 15 min) -> 0.60
      prep 300 -> 0.77     prep 600 (10 min)               -> 0.90
      prep 120 -> 1.92     prep 300 (quick counter, 5 min) -> 1.80

The swept range brackets the real one. This is a scale model matched on a
dimensionless ratio, the same reason a wind tunnel tests a small wing at the
right Reynolds number rather than a full-size one.

What the ratio does NOT capture, stated because it cuts against the null result
-------------------------------------------------------------------------------
Candidate COUNT is a second governing quantity: more riders to choose among
means more for matching to optimise. A k=2 disk here holds about 19 candidates,
where a 5 km radius in a dense city would hold hundreds. So this setup probably
UNDERSTATES matching's benefit, and the null result at prep=720 may be partly an
artefact of a thin candidate pool rather than purely prep absorption. Sweeping
K_RING at fixed prep is the experiment that would separate the two.

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

# The spread a policy is choosing between: the 19 candidates in one measured
# k=2 disk ranged 56 to 324 seconds away, averaging about 230. This is what
# "comparable to prep" is measured against - it is the number prep either
# absorbs or does not.
TYPICAL_CANDIDATE_TRAVEL = 230.0

PREP_VALUES = (720, 300, 120)

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


def main(seed_count: int, start_seed: int, preps: tuple[int, ...]) -> None:
    state.require_redis()
    import dispatch  # noqa: F401  - imported for its app

    original = (config.POLICY, config.PREP_SECONDS, config.ORDERS_PER_HOUR)
    client = TestClient(dispatch.app)
    seeds = list(range(start_seed, start_seed + seed_count))

    print("second leg averages %.0f s at a %.0f km city and %.0f km/h"
          % (SECOND_LEG_SECONDS, config.CITY_SIZE_KM, config.RIDER_SPEED_KMH))
    print("ageing weight %.2f, load held at %.0f%%, %d seeds"
          % (config.AGEING_WEIGHT, 100 * TARGET_LOAD, seed_count))
    print()
    print("seeds %d-%d" % (seeds[0], seeds[-1]))
    print()
    print("  prep   orders/hr   travel/prep   the kind of restaurant")
    for prep in preps:
        note = {
            720: "full-service kitchen  (travel absorbed)",
            300: "quick-service counter (comparable)",
            120: "coffee / pre-made     (travel exceeds prep)",
        }[prep]
        print("  %4d   %9d   %11.2f   %s"
              % (prep, rate_for(prep), TYPICAL_CANDIDATE_TRAVEL / prep, note))
    print()

    results = []
    started = time.perf_counter()
    try:
        for prep in preps:
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
    print("%d runs in %.0f s" % (2 * seed_count * len(preps),
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

    # The pre-registered statistic is the MEAN paired delta, not the median.
    # With heavy ties the median cannot move however large the effect is, which
    # is what made the 8-seed pilot's test uninformative (entry 29).
    primary = [
        (prep,
         statistics.mean(metrics.paired_deltas(b, c, "p90_delivery_min")),
         sum(1 for d in metrics.paired_deltas(b, c, "p90_delivery_min") if d < 0),
         sum(1 for d in metrics.paired_deltas(b, c, "p90_delivery_min") if d > 0))
        for prep, b, c in results
    ]
    print("THE TEST")
    print("  Pre-registered (entry 29): the mean paired p90 delta must become")
    print("  at least 0.10 min MORE NEGATIVE at the lowest prep than at the")
    print("  highest, AND matching must win more seeds than it loses there.")
    print()
    print("    prep   mean p90 delta   better   worse")
    for prep, mean, better, worse in primary:
        print("    %4d s     %+9.2f min   %6d  %6d" % (prep, mean, better, worse))

    shift = primary[0][1] - primary[-1][1]
    lowest = primary[-1]
    passed = shift >= 0.10 and lowest[2] > lowest[3]
    print()
    print("  shift from highest to lowest prep : %+.2f min (need >= 0.10)" % shift)
    print("  win/loss at the lowest prep       : %d / %d (need better > worse)"
          % (lowest[2], lowest[3]))
    print()
    print("  PRE-REGISTERED TEST: %s" % ("SUPPORTED" if passed else "NOT SUPPORTED"))
    if not passed:
        print("  The explanation in entries 26-28 does not survive its own test.")
        print("  Reopen it rather than reach for another statistic.")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    start = int(sys.argv[2]) if len(sys.argv) > 2 else config.RANDOM_SEED
    chosen = (
        tuple(int(v) for v in sys.argv[3].split(","))
        if len(sys.argv) > 3
        else PREP_VALUES
    )
    main(count, start, chosen)
