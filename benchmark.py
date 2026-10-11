"""Run greedy and matching on the same seeds and compare them, paired.

    python benchmark.py            BENCHMARK_SEEDS seeds
    python benchmark.py 5          a quick 5-seed look

Needs Redis up. Does NOT need the dispatch service running: it drives the app
in-process through FastAPI's TestClient, which is what lets one process switch
policy between phases. The policy is still read from config.POLICY exactly as
the service reads it, so neither policy is being exercised through a different
path than a real run uses.

Paired, not two groups. Seed-to-seed variation is enormous - p90 delivery has a
standard deviation of 4.53 min across seeds, while the expected policy effect
is a few percent - so both policies are run on the SAME seed and the per-seed
difference is what gets reported. Seed 61 being a hard world affects both sides
equally and cancels out of the delta (entry 23).
"""

import csv
import statistics
import sys
import time

import config
import metrics
import state
from fastapi.testclient import TestClient

BASELINE = "greedy"
CANDIDATE = "matching"

# Lower is better for all of these, so a negative delta is an improvement.
COMPARED = (
    ("p90 delivery (min)", "p90_delivery_min", True),
    ("p50 delivery (min)", "p50_delivery_min", False),
    ("max dispatch wait (s)", "max_dispatch_wait_s", False),
    ("p90 dispatch wait (s)", "p90_dispatch_wait_s", False),
    ("p50 idle at shop (min)", "p50_idle_at_shop_min", False),
)


def run_one(client, seed: int, policy: str) -> metrics.RunMetrics:
    """One full simulation under one policy, measured."""
    from sim import Simulator

    config.POLICY = policy
    simulator = Simulator(client, seed)
    simulator.run()
    simulator.check_invariants()
    return metrics.compute(
        simulator.completed, config.FLEET_SIZE, config.RUN_SECONDS
    )


def summarise_deltas(label: str, deltas: list[float], is_primary: bool) -> None:
    wins = sum(1 for d in deltas if d < 0)
    losses = sum(1 for d in deltas if d > 0)
    ties = sum(1 for d in deltas if d == 0)

    marker = "PRIMARY " if is_primary else "        "
    print("  %s%-24s  improved %2d / lost %2d / tied %2d   median %+8.2f   worst %+8.2f"
          % (marker, label, wins, losses, ties,
             statistics.median(deltas), max(deltas)))


def main(seed_count: int) -> None:
    state.require_redis()

    import dispatch

    original_policy = config.POLICY
    client = TestClient(dispatch.app)

    seeds = list(range(config.RANDOM_SEED, config.RANDOM_SEED + seed_count))
    baseline_runs: list[metrics.RunMetrics] = []
    candidate_runs: list[metrics.RunMetrics] = []

    print("%s vs %s, %d paired seeds, fleet %d, %d orders/hour"
          % (BASELINE, CANDIDATE, seed_count, config.FLEET_SIZE,
             config.ORDERS_PER_HOUR))
    print()
    print("                p90 delivery (min)        max dispatch wait (s)")
    print("  seed  orders   greedy  matching  delta    greedy  matching   delta")

    started = time.perf_counter()
    try:
        for seed in seeds:
            before = run_one(client, seed, BASELINE)
            after = run_one(client, seed, CANDIDATE)
            baseline_runs.append(before)
            candidate_runs.append(after)

            print("  %4d  %6d   %6.1f  %8.1f  %+5.1f   %6.0f  %8.0f  %+6.0f"
                  % (seed, before.orders,
                     before.p90_delivery_min, after.p90_delivery_min,
                     after.p90_delivery_min - before.p90_delivery_min,
                     before.max_dispatch_wait_s, after.max_dispatch_wait_s,
                     after.max_dispatch_wait_s - before.max_dispatch_wait_s))
    finally:
        config.POLICY = original_policy

    elapsed = time.perf_counter() - started

    print()
    print("%d runs in %.0f s" % (2 * seed_count, elapsed))
    print()
    print("PAIRED DIFFERENCES  (%s minus %s; negative means %s is better)"
          % (CANDIDATE, BASELINE, CANDIDATE))
    print()
    for label, field, is_primary in COMPARED:
        deltas = metrics.paired_deltas(baseline_runs, candidate_runs, field)
        summarise_deltas(label, deltas, is_primary)

    primary = metrics.paired_deltas(baseline_runs, candidate_runs, "p90_delivery_min")
    wins = sum(1 for d in primary if d < 0)
    median_delta = statistics.median(primary)
    baseline_median = statistics.median(r.p90_delivery_min for r in baseline_runs)

    print()
    print("HEADLINE")
    print("  %s improved p90 delivery on %d of %d seeds."
          % (CANDIDATE, wins, seed_count))
    print("  Median change %+.2f min (%+.1f%% of a %.1f min baseline)."
          % (median_delta, 100 * median_delta / baseline_median, baseline_median))
    if max(primary) > 0:
        print("  Worst regression %+.2f min." % max(primary))
    else:
        print("  It never did worse on any seed.")

    write_csv(seeds, baseline_runs, candidate_runs)
    print()
    print("wrote %s" % config.BENCHMARK_CSV_PATH)


def write_csv(seeds, baseline_runs, candidate_runs) -> None:
    """Per-seed results, so the headline can be checked rather than believed."""
    fields = ["seed", "orders"]
    for _, field, _ in COMPARED:
        fields += [f"{BASELINE}_{field}", f"{CANDIDATE}_{field}", f"delta_{field}"]

    with open(config.BENCHMARK_CSV_PATH, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for seed, before, after in zip(seeds, baseline_runs, candidate_runs):
            row = [seed, before.orders]
            for _, field, _ in COMPARED:
                b, a = getattr(before, field), getattr(after, field)
                row += [round(b, 3), round(a, 3), round(a - b, 3)]
            writer.writerow(row)


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else config.BENCHMARK_SEEDS
    main(count)
