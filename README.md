# rider_dispatch

A rider dispatch simulator. Greedy nearest-free-rider assignment over a Redis-backed
rider index, driven by a simulated clock.

Two processes, deliberately separate:

| Process | Role |
| --- | --- |
| **dispatch service** | FastAPI. One endpoint: takes a restaurant location, returns the id of the best free rider. Reads Redis, never writes. |
| **simulator** | Owns the clock. Ticks in 10-second steps, creates orders, moves riders, writes rider state to Redis, calls the service over HTTP, logs to CSV. |

The split is the point: the service has no clock and no memory of the run, so it can
later be pointed at real traffic without touching the simulator.

## State

Rider state lives in Redis: a hash per rider, plus a set of rider ids per H3 cell at
resolution 8 for spatial lookup. The simulator is the only writer; the service only
reads. That is safe because the simulator is single-threaded and single-process, and
it is the first thing that breaks if dispatch is ever run in parallel.

## Design decisions

Locked, with the reasoning, so they can be revisited on purpose rather than by drift:

- **Service reads, simulator writes.** No atomic claim in the service. Correct only
  under a single writer — see above.
- **Fixed k-ring candidate search.** `grid_disk(cell, K)` once, `K` configurable
  (default 2, about 2.4 km at res 8). If no free rider is in range the order goes
  unassigned and retries on a later tick. Expanding rings were rejected: the first
  ring containing *a* rider is not necessarily the ring containing the *nearest* one.
- **Riders interpolate every tick.** Each tick a moving rider advances along the
  straight line toward its target by speed x 10s (~69 m at 25 km/h) and is re-indexed
  when it crosses into a new H3 cell. Teleport-at-ETA would be less code but leaves
  in-flight positions wrong, which stops being harmless the moment anything reads them.
- **Two legs, rider stays put.** rider -> restaurant (pickup) -> customer (dropoff).
  The rider goes free at the customer's location, so riders drift toward demand.
- **Fixed prep time per restaurant** (12 minutes), starting when the order is created.
  A rider arriving before prep is done waits at the restaurant.

## Travel time

Travel time sits behind a `TravelTimeProvider` protocol so an OSRM provider can drop
in later. `HaversineProvider` is the current implementation: straight-line distance at
a configurable 25 km/h.

The interface takes **one origin and N destinations** and returns N durations. That is
the shape dispatch asks in (one restaurant, every candidate rider) and the shape OSRM's
`/table` endpoint answers in a single request; a scalar `travel_time(a, b)` would make
the OSRM provider fire N HTTP calls per assignment.

`step_toward` is separate from the provider on purpose: the provider answers *how long*,
`step_toward` answers *where the rider is now*.

## Output

One CSV row per order:

```
order_id, created, assigned, prep_done, arrived_at_restaurant, picked_up, delivered, rider_id
```

`arrived_at_restaurant` is logged so rider wait at the restaurant
(`picked_up - arrived_at_restaurant`) is derivable.

## Files check

| File | Status |
| --- | --- |
| `travel.py` | Done. `TravelTimeProvider` protocol, `HaversineProvider`, `step_toward`. Self-check: `python travel.py`. |
| `keys.py` | Pending. Redis key layout. |
| `cost.py` | Pending. Rider cost function. |
| `dispatch.py` | Pending. FastAPI service. Blocked on `keys.py` and `cost.py`. |
| `sim.py` | Pending. Clock, orders, rider movement, CSV. Blocked on `keys.py`. |

## Not in scope

No matching algorithm, no batching, no frontend, no auth. Greedy nearest-free-rider only.
