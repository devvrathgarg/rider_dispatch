"""Sweep the ageing weight and report the whole trade-off curve.

    python sweep.py            all BENCHMARK_SEEDS seeds, every weight
    python sweep.py 6          a quicker look with 6 seeds

Why a sweep is legitimate here, and not fishing for a flattering number: the
mechanism predicts a curve with a middle.

  W = 0    pure min-cost. Measured: starves old orders, +17.9 min on one seed.
  W = 1.0  waits reach 4669 s while travel spans 56-324 s, so the ageing term
           is ~20x the thing it is balancing against. It swamps travel entirely
           and matching degenerates into FIFO - which IS greedy. Measured: 17
           of 20 seeds tie exactly.

Both ends are known failures for known reasons, so something between them
should beat both. The honest output is therefore the ENTIRE curve, not its best
point: a reader can see the trade-off and judge the choice, rather than being
handed a number with no way to check how it was picked.

W = 0 doubles as a wiring check - it must reproduce entry 26's result exactly.

Greedy is run once per seed, not once per weight: cost_of subtracts a constant
that is identical for every rider of a given order, so it cannot change which
rider that order prefers. That is asserted in dispatch.py and was confirmed by
greedy's full-run CSV staying byte-identical.
"""

import csv
import statistics
import sys
import time

import config
import metrics
import state
from fastapi.testclient import TestClient

# Concentrated below 0.5. W=1.0 is already measured by benchmark.py (it ties
# greedy on 17 of 20 seeds), and the 4-seed smoke test showed W=0, 0.1 and
# 0.25 giving identical results - consistent with ageing only biting where
# riders are scarcer than orders. The resolution belongs where the curve
# actually turns.
WEIGHTS = (0.0, 0.05, 0.1, 0.2, 0.35, 0.5)

REPORTED = (
    ("p90 delivery (min)", "p90_delivery_min"),
    ("max dispatch wait (s)", "max_dispatch_wait_s"),
    ("p90 dispatch wait (s)", "p90_dispatch_wait_s"),
)


def run_one(client, seed: int, policy: str, weight: float) -> metrics.RunMetrics:
    from sim import Simulator

    config.POLICY = policy
    config.AGEING_WEIGHT = weight
    simulator = Simulator(client, seed)
    simulator.run()
    simulator.check_invariants()
    return metrics.compute(
        simulator.completed, config.FLEET_SIZE, config.RUN_SECONDS
    )


def summarise(deltas: list[float]) -> tuple[int, int, int, float, float]:
    return (
        sum(1 for d in deltas if d < 0),
        sum(1 for d in deltas if d > 0),
        sum(1 for d in deltas if d == 0),
        statistics.median(deltas),
        max(deltas),
    )


def main(seed_count: int) -> None:
    state.require_redis()
    import dispatch  # noqa: F401  - imported for its app

    original = (config.POLICY, config.AGEING_WEIGHT)
    client = TestClient(dispatch.app)
    seeds = list(range(config.RANDOM_SEED, config.RANDOM_SEED + seed_count))

    total_runs = seed_count * (1 + len(WEIGHTS))
    print("ageing weight sweep: %d seeds x (1 greedy + %d matching) = %d runs"
          % (seed_count, len(WEIGHTS), total_runs))
    print()

    started = time.perf_counter()
    try:
        # Greedy once per seed. It does not depend on W.
        print("running greedy baseline on %d seeds..." % seed_count)
        baseline = [run_one(client, seed, "greedy", 0.0) for seed in seeds]

        rows = []
        for weight in WEIGHTS:
            runs = [run_one(client, seed, "matching", weight) for seed in seeds]
            rows.append((weight, runs))
            print("  W = %-5s done" % weight)
    finally:
        config.POLICY, config.AGEING_WEIGHT = original

    elapsed = time.perf_counter() - started
    print()
    print("%d runs in %.0f s" % (total_runs, elapsed))
    print()

    for label, field in REPORTED:
        print("%s   (matching minus greedy; negative = matching better)" % label)
        print("      W     better   worse    tied     median      worst")
        for weight, runs in rows:
            deltas = metrics.paired_deltas(baseline, runs, field)
            better, worse, tied, median, worst = summarise(deltas)
            print("  %5.2f   %6d  %6d  %6d  %+9.2f  %+9.2f"
                  % (weight, better, worse, tied, median, worst))
        print()

    write_csv(seeds, baseline, rows)
    print("wrote %s" % config.SWEEP_CSV_PATH)
    print()
    print("Read the whole column, not the best cell. W=0 and W=1 are both known")
    print("failures for known reasons; what matters is whether the middle beats")
    print("them on the primary metric WITHOUT giving the guardrail back.")


def write_csv(seeds, baseline, rows) -> None:
    """Every seed at every weight, so the curve can be checked rather than believed."""
    with open(config.SWEEP_CSV_PATH, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["ageing_weight", "seed", "orders"]
            + [f"greedy_{f}" for _, f in REPORTED]
            + [f"matching_{f}" for _, f in REPORTED]
            + [f"delta_{f}" for _, f in REPORTED]
        )
        for weight, runs in rows:
            for seed, before, after in zip(seeds, baseline, runs):
                writer.writerow(
                    [weight, seed, before.orders]
                    + [round(getattr(before, f), 3) for _, f in REPORTED]
                    + [round(getattr(after, f), 3) for _, f in REPORTED]
                    + [round(getattr(after, f) - getattr(before, f), 3)
                       for _, f in REPORTED]
                )


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else config.BENCHMARK_SEEDS
    main(count)
