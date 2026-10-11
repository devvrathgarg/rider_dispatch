# rider_dispatch

A food-delivery dispatch simulator, built to answer one question with evidence rather
than intuition: **does batched min-cost matching beat greedy nearest-free-rider?**

The answer turned out to be no, for a reason worth more than a yes would have been.

## The result

Paired over 20 seeds — both policies facing the identical order stream and starting fleet
on each seed — the answer came in two stages.

**Stage one: pure min-cost matching lost badly.** It improved the primary metric on 1
seed of 20 and degraded it on 7, turning a 44-minute p90 delivery time into 62 minutes on
the worst seed.

Not a bug: the implementation is verified against an exhaustive brute-force optimum and
provably never assigns fewer orders than greedy. It minimises total travel time exactly
as asked. **Total travel time is the wrong objective.** A 12-minute food prep absorbs most
travel, so arriving 200 seconds sooner usually changes delivery time by zero — while the
reordering needed to save those seconds pushes the oldest waiting order further back.
Underneath it was *redistributing*: p90 dispatch wait improved on 9 seeds while max
dispatch wait degraded on 10. It took time off the middle and dumped it on the tail.

**Stage two: an ageing term fixed the harm, and matching became merely equal.**

```
                     BEFORE ageing             AFTER ageing
p90 delivery      1 win / 7 loss / 12 tie   1 win / 2 loss / 17 tie
  worst regression        +17.86 min              +0.22 min
max dispatch wait 1 win /10 loss /  9 tie   2 win / 4 loss / 14 tie
  worst regression        +2563 s                  +150 s
seed 61 p90            44.2 -> 62.1            44.2 -> 44.2
```

Discounting an order's cost by how long it has waited removed the starvation and
confirmed the diagnosis — two predictions written before the run both held. But 17 of 20
seeds are now *identical*, median change `+0.00`.

**So the conclusion is "equivalent, and therefore not worth deploying."** Of a 26-minute
delivery, ~12 minutes is prep and ~9 is the second leg; neither is touchable by any
assignment policy. There is almost nothing for a better algorithm to win, because greedy
is already near the ceiling prep time imposes.

**Where the real lever is**, from two earlier measurements: 42% of rider busy time is
spent standing at a restaurant waiting for food — which calls for a *later* assignment,
not a better one. And capacity is not the constraint, location is: one seed carried fewer
orders and used less of the fleet than another yet produced a 78-minute worst dispatch
wait against 16.

The full reasoning, with numbers, is [DECISIONS.md](DECISIONS.md) entries 26 and 27.

## Running it

```bash
docker start dispatch-redis                           # Redis on port 6380
uvicorn dispatch:app --host 127.0.0.1 --port 8001     # the dispatch service
python sim.py                                         # one full simulated hour
python analyze.py                                     # metrics for that run
python benchmark.py 20                                # greedy vs matching, paired
```

Every module has an assert-based self-check: `python travel.py`, `python keys.py`,
`python state.py`, `python metrics.py`, `python dispatch.py`.

## Architecture

Two processes, deliberately separate:

| Process | Role |
| --- | --- |
| **dispatch service** | FastAPI. One endpoint: a batch of restaurant locations in, rider ids out. Reads Redis, never writes. No clock, no state between requests. |
| **simulator** | Owns a virtual clock. Ticks in 10-second steps, creates orders, moves riders, writes rider state to Redis, calls the service over HTTP, logs to CSV. |

The split is the point: the service has no clock and no memory of the run, so it could be
pointed at real traffic with the simulator deleted. A real network boundary also forces
honesty about coupling — anything the service needs must arrive in the request or come
from Redis, rather than hiding in shared memory.

### State

Rider state lives in Redis: a hash per rider, plus a set of rider ids per H3 cell at
resolution 8 for spatial lookup. The simulator is the only writer; the service only
reads. That is safe because the simulator is single-process, and it is the first thing
that breaks if dispatch is ever run in parallel.

One `/assign_batch` call costs **two Redis round trips** regardless of how many
candidates it considers: one `SUNION` over the k-ring, and one pipelined read of every
candidate. Written as a loop instead, 20 candidates would cost 20 round trips — measured
at 31 ms against 3.5 ms, with 98% of the slower version's time spent idle on the network.

### Determinism

The same seed produces a byte-identical CSV across separate processes
(`md5 e836ef7f...`), which is what makes a policy comparison meaningful rather than
anecdotal. That required sorting candidate ids before ranking them, because `SUNION`
returns a set and Python's string hashing is randomised per process.

Reproducible is not the same as representative: seed 42 turned out to be the minimum of
60 seeds for both rider density and order count, so absolute numbers are reported across
seeds and policy differences are reported paired.

## What is measured

| Role | Metric |
| --- | --- |
| **Primary** | p90 delivery time |
| Guardrail | p50 delivery time — catches making everyone slightly worse |
| Guardrail | max dispatch wait — catches starving one order |
| Guardrail | p50 rider idle at restaurant — catches making riders wait instead |
| Diagnostic | p50/p90 dispatch wait — the part the policy actually controls |

p99 is deliberately not used: at ~120 orders it is the single worst order wearing a
statistic's name.

Every rate and utilisation figure names its window. The same run reads 27.9% utilisation
over its whole length and 34.1% over the order window, because the drain phase has no
orders arriving by construction.

### Findings along the way

- **42% of a rider's busy time is spent standing at a restaurant** waiting for food. That
  is the cost of assigning immediately, and the reason production systems delay
  assignment so the rider arrives as the food does.
- **Capacity is not the constraint; location is.** One seed carried *fewer* orders and
  used *less* of the fleet than another, yet produced a 78-minute worst-case dispatch
  wait against 16. A third of the fleet idle is entirely compatible with a customer
  waiting twenty minutes.
- **Three individually sound decisions produced an unserviceable order.** Fixed `K`
  could not reach further, riders stay where they drop off so nothing repositioned them,
  and orders never give up — so an order whose restaurant had no rider nearby retried
  forever and the run could not terminate.

## Travel time

Travel time sits behind a `TravelTimeProvider` protocol so an OSRM provider can drop in
later. `HaversineProvider` is the current implementation: straight-line distance at a
configurable 25 km/h.

The interface takes **one origin and N destinations** and returns N durations — the shape
dispatch asks in, and the shape OSRM's `/table` endpoint answers in a single request. A
scalar `travel_time(a, b)` would make the OSRM provider fire N HTTP calls per assignment.

`step_toward` is separate from the provider on purpose: the provider answers *how long*,
`step_toward` answers *where the rider is now*.

The error budget is why this is the only interface in the project. Sphere-versus-ellipsoid
error is ~15 m on a 3 km trip; straight-line-versus-roads is ~900 m, and a constant speed
can be 50% wrong. Chasing geometric precision here would be wasted work — the dominant
error lives entirely inside this one method, which is what makes the seam worth having.

## Output

One CSV row per order, raw timestamps rather than computed durations, so a new metric
never needs a new simulation run:

```
order_id, created, assigned, prep_done, arrived_at_restaurant, picked_up, delivered,
rider_id, restaurant_lat, restaurant_lng, customer_lat, customer_lng,
attempts_no_candidate, attempts_all_busy, search_k
```

`arrived_at_restaurant` is logged because `picked_up = max(arrival, prep_done)` is lossy:
without it you cannot tell whether the rider waited or the kitchen did.

## Decisions

Every non-obvious choice is in [DECISIONS.md](DECISIONS.md) with the options considered,
why one won, and what it gives up — including which were later measured to be wrong.
[PLAN.md](PLAN.md) covers why the project exists and what each component is for.

Three entries are superseded by later measurement rather than edited away, which is the
point of keeping the log.

## Files

| File | Owns |
| --- | --- |
| [config.py](config.py) | Settings, split into experiment values (in code) and machine values (from environment) |
| [keys.py](keys.py) | The Redis key layout — Redis has no schema, so this file *is* the schema |
| [state.py](state.py) | The only module touching rider data: the text-to-float boundary, and the cell index |
| [travel.py](travel.py) | Travel time, straight-line geometry |
| [seed.py](seed.py) | Creates the fleet and verifies it is reachable through the index |
| [dispatch.py](dispatch.py) | The FastAPI service, the cost function, and both policies |
| [sim.py](sim.py) | The virtual clock, orders, rider movement, CSV |
| [metrics.py](metrics.py) | Metric definitions and the paired comparison |
| [analyze.py](analyze.py) | Metric reports, for one run or across seeds |
| [benchmark.py](benchmark.py) | The paired greedy-vs-matching comparison |

## Not in scope

No frontend, no auth, no persistence beyond the CSV. An OSRM travel-time provider is
designed for but not built — it needs a routing server with real map data.

Load testing with k6 and OpenTelemetry tracing are planned next.
