"""Report metrics for a run, or for a spread of seeds.

    python analyze.py              report on the last run's CSV
    python analyze.py seeds         run BENCHMARK_SEEDS seeds, report the spread
    python analyze.py seeds 8       the same, with 8 seeds

The seeds mode needs Redis and the dispatch service up, because each seed is a
real simulation. The CSV mode needs neither - it is pure arithmetic on a file,
which is the point of logging raw timestamps rather than computed durations.
"""

import statistics
import sys
import time

import httpx

import config
import metrics


def print_report(run: metrics.RunMetrics) -> None:
    print("orders measured    : %d" % run.orders)
    print()
    print("PRIMARY")
    print("  p90 delivery     : %6.1f min" % run.p90_delivery_min)
    print()
    print("GUARDRAILS")
    print("  p50 delivery     : %6.1f min" % run.p50_delivery_min)
    print("  max dispatch wait: %6.0f s     (14x unstable between seeds - entry 23)"
          % run.max_dispatch_wait_s)
    print("  p50 idle at shop : %6.1f min" % run.p50_idle_at_shop_min)
    print()
    print("DIAGNOSTIC - the part the policy actually controls")
    print("  p50 dispatch wait: %6.0f s" % run.p50_dispatch_wait_s)
    print("  p90 dispatch wait: %6.0f s" % run.p90_dispatch_wait_s)
    print()
    print("REPORTED, NOT JUDGED  (window: the order window, %d s)" % config.RUN_SECONDS)
    print("  worst delivery   : %6.1f min" % run.max_delivery_min)
    print("  utilisation      : %6.1f %%" % run.utilisation_pct)
    print("  throughput       : %6.1f orders/hour" % run.throughput_per_hour)
    print("  riders used      : %d of %d (%d never used)"
          % (run.riders_used, run.riders_used + run.riders_unused, run.riders_unused))
    print("  busiest rider    : %d orders" % run.max_orders_one_rider)
    print("  of busy time, spent waiting at a shop: %.0f %%"
          % run.busy_time_spent_waiting_pct)
    print()
    print("WHY ORDERS WAITED")
    print("  needed the widened search (geometry) : %d" % run.orders_needing_fallback)
    print("  delayed, nobody nearby at all        : %d" % run.orders_delayed_by_geometry)
    print("  delayed, nearby riders all busy      : %d" % run.orders_delayed_by_saturation)


def run_seeds(count: int) -> list[metrics.RunMetrics]:
    """Run `count` seeds of the current policy and measure each.

    Imported here rather than at module scope so the CSV mode does not need
    Redis, httpx or a running service just to read a file.
    """
    from sim import Simulator, check_service
    import state

    state.require_redis()
    results = []

    with httpx.Client(base_url=config.DISPATCH_URL, timeout=10.0) as client:
        check_service(client)

        print("policy %r, %d seeds, fleet %d, %d orders/hour"
              % (config.POLICY, count, config.FLEET_SIZE, config.ORDERS_PER_HOUR))
        print()
        print("  seed  orders   p90 del   p50 del   p90 disp   max disp   util")

        started = time.perf_counter()
        for seed in range(config.RANDOM_SEED, config.RANDOM_SEED + count):
            simulator = Simulator(client, seed)
            simulator.run()
            simulator.check_invariants()

            run = metrics.compute(
                simulator.completed, config.FLEET_SIZE, config.RUN_SECONDS
            )
            results.append(run)
            print("  %4d  %6d   %7.1f   %7.1f   %8.0f   %8.0f   %4.1f%%"
                  % (seed, run.orders, run.p90_delivery_min, run.p50_delivery_min,
                     run.p90_dispatch_wait_s, run.max_dispatch_wait_s,
                     run.utilisation_pct))

        elapsed = time.perf_counter() - started

    print()
    print("%d runs in %.0f s (%.1f s each)" % (count, elapsed, elapsed / count))
    return results


def print_spread(results: list[metrics.RunMetrics]) -> None:
    """How much each metric moves when only the seed changes.

    This is the number that decides whether a policy difference is detectable:
    an improvement smaller than this spread cannot be distinguished from luck
    in a single run, which is why Day 5 pairs by seed instead.
    """
    print()
    print("SPREAD ACROSS SEEDS - the noise floor a policy has to beat")
    print()
    print("  %-22s %8s %8s %8s %8s" % ("metric", "min", "median", "max", "stdev"))

    for label, field in (
        ("p90 delivery (min)", "p90_delivery_min"),
        ("p50 delivery (min)", "p50_delivery_min"),
        ("p90 dispatch wait (s)", "p90_dispatch_wait_s"),
        ("max dispatch wait (s)", "max_dispatch_wait_s"),
        ("p50 idle at shop (min)", "p50_idle_at_shop_min"),
        ("utilisation (%)", "utilisation_pct"),
        ("orders", "orders"),
    ):
        values = [getattr(r, field) for r in results]
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        print("  %-22s %8.2f %8.2f %8.2f %8.2f"
              % (label, min(values), statistics.median(values), max(values), stdev))

    p90s = [r.p90_delivery_min for r in results]
    noise = statistics.stdev(p90s) if len(p90s) > 1 else 0.0
    print()
    print("p90 delivery varies by %.2f min (1 stdev) on seed alone." % noise)
    print("A policy change smaller than roughly %.2f min is indistinguishable" % (2 * noise))
    print("from luck in a single run. Paired comparison (entry 23) is what")
    print("makes a smaller effect detectable.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "csv"

    if mode == "csv":
        orders = metrics.load_csv(config.CSV_PATH)
        print("reading %s" % config.CSV_PATH)
        print()
        print_report(metrics.compute(orders, config.FLEET_SIZE, config.RUN_SECONDS))

    elif mode == "seeds":
        count = int(sys.argv[2]) if len(sys.argv) > 2 else config.BENCHMARK_SEEDS
        print_spread(run_seeds(count))

    else:
        raise SystemExit(__doc__)
