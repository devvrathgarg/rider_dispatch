# Decisions

Every non-obvious choice: the options considered, and why one won. Append, never
rewrite. A decision that changes gets a new entry superseding the old one, so the
reasoning as it stood at the time stays readable.

Entries 1-4 were chosen by Devvrath from stated options. Entries 5-8 were made by
Claude before the "ask, don't pick" rule existed; they are marked as such and open to
being reopened.

---

## 1. The simulator writes rider state; the service only reads

**Date:** 2026-09-09 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| Service claims the rider atomically | `/assign` picks the best rider and flips it free to busy in one indivisible step (Lua script, or `WATCH`/`MULTI`). Correct even with many dispatchers. Costs a Lua script to write and debug, and makes the service a writer. |
| **Service reads only, simulator writes** (chosen) | `/assign` is a pure query: read cell sets, read rider hashes, rank, return an id. The simulator marks the rider busy after it gets the answer. No atomicity machinery at all. |
| Simulator sends candidates in the request body | Service never touches Redis. Fewest moving parts, but deletes the H3 spatial index, which is the part worth learning. |

**Why**

The simulator is single-threaded and the only writer in the system, so there is no
second actor to race against. The atomic claim would defend against a concurrency that
does not exist yet, and an unused defence is a thing that rots: written now, never
exercised, then trusted later without evidence.

**What this gives up, precisely**

With two dispatchers, two concurrent `/assign` calls can read the same free rider and
both return it. The window is the gap between the service's read and the simulator's
write. The fix is known and named: move select-and-claim into one Lua script so
read-rank-write runs atomically inside Redis, or take a short-lived `SET NX` lock on the
rider id before returning it. Not built, deliberately.

**Revisit when:** more than one process assigns orders, or `/assign` is called by
anything other than the simulator.

---

## 2. Fixed k-ring candidate search, not expanding rings

**Date:** 2026-09-09 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Fixed `k`, configurable** (chosen) | One `grid_disk(cell, K)`, one `SUNION`, one code path. Constant, predictable work per request. Orders with nobody free in range go unassigned and retry on a later tick. |
| Expand until found, capped at `MAX_K` | Try k=0, then 1, 2, stopping at the first ring that yields a free rider. Fewer unassigned orders, but a loop of Redis round-trips per request and a correctness trap (below). |
| Scan every rider | Trivially correct, no index needed. Deletes the spatial-lookup exercise, and is O(riders) per order. |

**Why**

The expanding version has a bug that is easy to ship and hard to see: **the first ring
containing a rider is not the ring containing the nearest rider.** Hex-ring distance is
not metric distance. A rider at the far edge of ring 1 can be further from the
restaurant than a rider at the near edge of ring 2, so stopping at the first non-empty
ring returns a rider that is merely close enough, while reporting itself as nearest.

Fixing `K` and ranking every candidate in the whole disk sidesteps this: the search area
is decided once, and within it the ranking is honest.

**Sizing note:** at resolution 8 the average cell edge is about 0.46 km and
centre-to-centre spacing about 0.8 km, so a `k=2` disk is 19 cells reaching roughly
1.6 km centre-to-centre and about 2 km at the outer boundary. These are averages over an
unequal-area grid, so the real reach should be measured in the sim, not taken from this
file.

**What this gives up, precisely**

Orders with no free rider inside `K` stay unassigned until a later tick. That is a number
worth logging rather than hiding: unassigned attempts per tick is the signal that either
`K` is too small or the fleet is too small, and those two causes look different over
time.

**Revisit when:** the unassigned rate is high, or `K` needs to vary by area density
instead of being one global constant.

---

## 3. Riders interpolate position every tick, rather than teleporting at ETA

**Date:** 2026-09-09 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Interpolate each tick** (chosen) | Each 10s tick a moving rider advances along the straight line toward its target by speed times 10s (about 69 m at 25 km/h), and is re-indexed when it crosses into a new H3 cell. Positions are always truthful. Costs movement code plus Redis writes every tick. |
| Teleport at ETA | On assignment, compute the arrival tick; the stored position stays at the origin until then, then jumps. Much less code, no per-tick writes. Mid-trip positions are wrong. |

**Why**

Teleporting is unobservable *today*, because busy riders are not dispatch candidates and
nothing reads a position mid-trip. That is exactly what makes it dangerous. The day
anything does read it (chaining a second order onto a rider already moving, reassigning
in flight, plotting the fleet, measuring utilisation) the wrong answers arrive as
plausible data rather than as a crash. A bug that surfaces as a slightly-off number is
far more expensive than one that throws.

The interpolated version is also the one that stays meaningful when the OSRM provider
lands, since real movement follows a route rather than appearing at a destination.

**What this gives up, precisely**

Per-tick writes scale with the number of moving riders: one position update per rider
per tick, plus two set operations on each cell crossing. At a few hundred riders this is
nothing. If it ever matters, the fix is to batch a tick's updates into one Redis
pipeline, not to go back to teleporting.

**Revisit when:** movement writes dominate Redis traffic.

---

## 4. Two legs, and the rider goes free where it dropped off

**Date:** 2026-09-09 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Two legs, rider stays put** (chosen) | rider to restaurant (pickup), restaurant to customer (dropoff). The rider goes free at the customer's location and waits there. |
| Two legs, then return to base | Same, plus a drive back before going free. One more leg and one more state; models a fleet that actually returns somewhere. |
| One leg, dropoff only | Travel time is restaurant to customer only; `delivered = assigned + ETA`. Least code. |

**Why**

The one-leg version removes rider-to-restaurant time, which *is* the basis for
nearest-rider dispatch. Without it every free rider has identical cost and the
assignment decision becomes arbitrary. It would simulate away the thing being simulated.

Staying put at the customer beats returning to base because it lets rider distribution
emerge from where demand actually was, instead of being reset to a fiction after every
order. That emergent drift (riders pooling in busy areas, deserts forming elsewhere) is
the behaviour worth watching in a dispatch simulator.

**Revisit when:** modelling shift ends, break locations, or deliberate repositioning of
idle riders.

---

## 5. `TravelTimeProvider` takes one origin and N destinations

**Date:** 2026-09-09 - **Decided by:** Claude, without asking. Open to reopening.

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **`travel_times(origin, [dest, ...]) -> [seconds, ...]`** (chosen) | Matches how dispatch queries: one restaurant against every candidate rider. Matches OSRM's `/table` endpoint, which answers exactly this in one request. A single pair is a one-element list. |
| `travel_time(origin, dest) -> seconds` | Simpler to read and to call. Makes the future OSRM provider fire one HTTP request per candidate rider, per assignment. |

**Why**

The interface exists so an OSRM provider can drop in later, so the signature should be
the one that lets OSRM be implemented well. The scalar form would quietly turn one
assignment into N network calls, and the only fix at that point is changing the
interface and every caller, which is the exact cost the interface was meant to avoid.

Haversine is indifferent to the shape, so the batch form costs nothing now.

**Why this should have been asked:** it constrains the signature of the cost function,
which Devvrath is writing.

**Revisit when:** now, if preferred. Changing it later means touching the cost function.

---

## 6. `Protocol`, not an abstract base class

**Date:** 2026-09-09 - **Decided by:** Claude, without asking. Open to reopening.

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **`typing.Protocol`** (chosen) | Structural typing. `HaversineProvider` inherits from nothing and satisfies the protocol by having the right method. Type checkers verify the match; nothing is enforced at runtime. |
| `abc.ABC` plus `@abstractmethod` | Nominal typing. Every provider must subclass. Python refuses to instantiate a subclass that forgot a method, which is a runtime guarantee a Protocol does not give. |

**Why**

An ABC makes providers depend on this module in order to be providers. A Protocol
inverts that: a provider is anything shaped correctly, so an OSRM class in its own file
needs no import from `travel.py`, and a test double can be a tiny local class with no
inheritance at all.

The runtime safety given up is small here, because there is exactly one method. A
forgotten `travel_times` fails on the first call, loudly, rather than lurking.

**Revisit when:** the interface grows enough methods that "did I implement all of them"
becomes a real question, or providers need shared behaviour rather than just a shared
shape. An ABC can hold common code; a Protocol cannot.

---

## 7. `step_toward` sits outside the provider interface

**Date:** 2026-09-09 - **Decided by:** Claude, without asking. Open to reopening.

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **A module-level function, not a protocol method** (chosen) | The provider answers *how long will this take*; `step_toward` answers *where is the rider right now*. Two questions, two places. |
| A third method on `TravelTimeProvider` | Each provider moves riders the way it routes them: OSRM along real road geometry, Haversine along a straight line. More faithful, but every provider must then implement movement. |

**Why**

They are only the same question if movement follows the same path the estimate assumed.
Today it does, because both are straight lines. Keeping them separate means the sim's
mover does not change when the estimator does.

**The honest tension:** when OSRM lands, this is the entry most likely to be wrong.
Straight-line movement paired with road-network travel times is a mismatch: the rider's
reported position will not be on any road, and will not be where the ETA implies.
Whether that matters depends on whether anything reads mid-trip positions for more than
display.

**Revisit when:** the OSRM provider is written. Decide then whether movement follows
route geometry. This entry gets superseded, not edited.

---

## 8. Prep starts at order creation, and `arrived_at_restaurant` is logged

**Date:** 2026-09-09 - **Decided by:** Claude, inferred rather than asked. Open to reopening.

**Context:** a fixed 12-minute prep time per restaurant. An order is not collectable
until prep is done, so a rider arriving early waits.

**Options considered - when does the prep clock start?**

| Option | Consequence |
| --- | --- |
| **At order creation** (chosen) | `prep_done = created + prep_time`. Kitchen and dispatch run in parallel, so prep overlaps the rider's approach and rider wait is only the part of prep still outstanding on arrival. |
| At assignment | Prep cannot start before a rider exists, so dispatch delay pushes delivery out twice over. Models a kitchen that waits for a courier. |
| At rider arrival | Prep and travel never overlap, so wait is always the full prep time. Worst case, and unrealistic for food. |

**Why creation:** a kitchen starts cooking when the order lands, not when a courier is
found. It also makes the two latencies independent, which is what makes them separable
in the CSV later.

**Options considered - logging the wait**

| Option | Consequence |
| --- | --- |
| **Log `arrived_at_restaurant`** (chosen) | Rider wait is `picked_up - arrived_at_restaurant`, derivable after the fact. One extra column. |
| Log only the five requested timestamps | Rider wait is *not* recoverable. `picked_up = max(arrival, prep_done)`, and `max` is lossy: given `picked_up` alone you cannot tell whether the rider waited or the kitchen did. |

**Why:** the requested metric is not derivable without it. `prep_done` is kept too, even
though it is currently just `created + 12min` and therefore redundant, because it stops
being redundant the moment prep time varies per restaurant.

**Revisit when:** prep time becomes per-restaurant or stochastic, or the kitchen can
reject orders.

---

## Open decisions

Not yet decided. Listed here so they do not get decided by accident.

| Question | Why it matters |
| --- | --- |
| Cost function signature | Whether dispatch calls `cost(seconds, rider)` per candidate and takes the min, or hands the whole candidate list to one function that picks the winner. Decides where ranking logic lives. Owner: Devvrath. |
| Does cost account for prep time? | With a 12-minute prep, riders 3 and 8 minutes out both arrive early and wait, so pure travel time ranks them differently while `max(travel, prep_remaining)` calls them equal and frees the tiebreak for something else. Decides whether dispatch needs prep state passed in at all. |
| Redis key layout | Owner: Devvrath. Must support: cell to rider-id set (for the `SUNION` over a k-ring), rider id to hash, and moving a rider between cell sets on a crossing. Sub-question: does the rider hash store its current cell, so the writer knows which set to `SREM` from without recomputing it? |
| Where shared constants live | Rider speed is needed by both processes: the sim to move riders, the service to estimate. Options are duplicating the constant, a shared `config.py`, or environment variables. Duplicating means the two processes can silently disagree about how fast a rider is. |
| Rider state machine | How many states, and are they a field on the hash or separate sets? `free` / `to_restaurant` / `waiting` / `to_customer` is the minimum the two-leg model implies. A set of free rider ids would make the dispatch filter an intersection instead of N hash reads. |
| Metric definitions | Owner: Devvrath. Which numbers define a good run, computed from the CSV. Needed before stage 1 of the roadmap, since an eval harness with no agreed metric cannot declare a winner. |

---

## 9. Adding `redis-py` and `h3`

**Date:** 2026-10-03 - **Decided by:** Devvrath, after the case for each

**`redis` (redis-py 8.1.0)** - the official Python client for Redis. Opens a TCP
connection, speaks Redis's RESP wire protocol, and exposes one Python method per Redis
command (`r.hset`, `r.sunion`, `r.smembers`).

| Alternative | Why not |
| --- | --- |
| Hand-roll RESP over a raw socket | RESP is a simple protocol, so this is *possible*. But it is a project in itself: framing, type parsing, connection pooling, reconnection, pipelining. All of it plumbing, none of it dispatch. |
| `aioredis` | Merged into redis-py as its async interface, so it is no longer a separate choice. The sync client is correct here because the simulator is deliberately single-threaded. |

**`h3` (4.5.0)** - Python bindings for Uber's hexagonal geospatial index. We use two
functions: `latlng_to_cell` and `grid_disk`.

| Alternative | Why not |
| --- | --- |
| Redis `GEOADD`/`GEOSEARCH` | Genuinely simpler, and would work. Rejected for learning value, and because H3 cells double as an aggregation unit for demand and supply maps later. Logged in PLAN.md as an honest tradeoff, not a dismissal. |
| S2 (Google) | Comparable quality, squares on a projected cube. H3's uniform hexagon adjacency is the better fit for radius-style search, and its Python API is simpler. |
| Write our own grid | Reinventing H3 badly, and spending the project on geometry instead of dispatch. |

**Version note:** h3 4.x renamed the entire 3.x API. Anything written for h3 3.x
(`geo_to_h3`, `k_ring`) will not run. Recorded because it will cost an hour otherwise.

---

## 10. Redis runs on host port 6380, in its own container

**Date:** 2026-10-03 - **Decided by:** Devvrath

**How this surfaced:** the first `docker run` failed silently in a dangerous way. Port
6379 was already bound by `facultyhire-redis-1`, a container from an unrelated project,
so our container was created but never started. A connection test to 6379 answered
`PONG` anyway - from the *other project's* Redis. Had this gone unnoticed, rider state
would have been written into a different project's database, and the first symptom would
have been confusing data rather than an error.

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Own container on host port 6380** (chosen) | `-p 6380:6379`. Separate process, separate memory, separate lifecycle. The other project is never touched. Cost: every connection in this repo must say 6380, and anything defaulting to 6379 is silently wrong. |
| Share the existing Redis, use database 1 | Redis has 16 numbered databases, so keys would never collide. No new container. But `FLUSHALL` from either project wipes both, and stopping the facultyhire stack would take this project's state with it. Couples two unrelated projects' lifecycles. |
| Stop the other Redis and take 6379 | Default port, so every tutorial command works unmodified. Breaks the other project until swapped back, and relies on remembering to swap. |

**Why:** isolation is worth one non-default port. The failure mode of the shared option
is data loss caused by work on an unrelated project, which is the kind of coupling that
is invisible until it bites.

**The general lesson:** a successful connection is not proof you reached *your* service.
Port conflicts produce a working connection to the wrong thing. Verifying that a fresh
database is actually empty is what caught this.

**Revisit when:** the project moves to a compose file, where a named service and an
internal network make the host port mostly irrelevant.

---

## 11. Roadmap stage 3 moves to days 6-7 rather than compressing day 5

**Date:** 2026-10-03 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Days 6-7, nothing rushed** (chosen) | Days 1-5 stay as planned. k6 load testing and OpenTelemetry tracing get their own block. Costs 1-2 days beyond the original 5. |
| Trim to tracing only, share day 5 | OpenTelemetry on the service, no k6. Fits inside 5 days but makes day 5 long, and drops load testing entirely. |
| Drop stage 2, promote stage 3 | Day 5 becomes load testing and tracing, batched matching is cut. Rejected: the greedy-versus-matching benchmark is the strongest result in the project, and tracing infrastructure with nothing interesting to observe is a weaker outcome. |

**Why:** the deadline is self-imposed, and the purpose of the project is understanding
rather than delivery. Compressing two unfamiliar technologies into the same day as a
benchmark would produce three things half-learned instead of three things learned.

**Revisit when:** the 5 days are up and the actual pace is known, which is better
evidence than this estimate.

---

## 12. Redis is the single source of truth; the simulator keeps no authoritative fleet in memory

**Date:** 2026-10-07 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Redis is the truth** (chosen) | The simulator holds no authoritative copy. To move a rider it reads the position from Redis, computes the next one, and writes it back. Exactly one record exists, so nothing can disagree with itself. Costs a read *and* a write on the hottest operation in the system, plus a text-to-float conversion on every read. |
| Simulator owns the fleet in memory, writes through to Redis | Movement becomes plain Python arithmetic on real objects: no read, no conversion on the hot path, half the Redis traffic, simpler simulator code. But two copies of every rider exist, and a failed write makes them drift apart silently, leaving the service to decide from a stale world. |

**Why**

In production no single process can hold the fleet in memory, because GPS pings arrive
at many servers and any of them may handle the next one. Redis has to be the
authoritative shared view. Writing the simulator to read back from Redis keeps the data
flow the same shape as the real system, rather than taking a shortcut that only works
because a simulator happens to be one process.

**A precision worth keeping:** in production Redis is *also* a copy. A rider's position
really originates on their phone; Redis holds the most recent ping anyone was told
about. That is exactly why its lack of durability is acceptable there - wipe Redis and
the next round of pings refills it in seconds, because Redis was never the origin of
the fact. "Source of truth" here means authoritative *shared view*, not origin.

**What this gives up, precisely**

Every tick, every moving rider now costs a read plus a write instead of a write alone,
and every read costs a string-to-float conversion. Two consequences follow:

- `state.py` carries more weight, since it is the single place those conversions happen.
- Pipelining (DECISIONS context: the N+1 problem) now matters on the *simulator* side
  too, not only in the dispatch service. A per-tick loop of individual reads would be
  the same N+1 mistake, just in a different process.

**Revisit when:** per-tick Redis traffic becomes the simulator's bottleneck, measured
rather than assumed. The upgrade path is to keep an in-memory write-through cache in the
simulator while leaving Redis authoritative, which is a smaller change than swapping the
ownership model outright.

---

## 13. Four rider states, stored as one `status` field on the rider hash

**Date:** 2026-10-07 - **Decided by:** Devvrath

### Part one: four states, not three

```
free  ->  to_restaurant  ->  waiting_at_restaurant  ->  to_customer  ->  free
```

| Transition | Trigger |
| --- | --- |
| `free` -> `to_restaurant` | Dispatch assigns the order |
| `to_restaurant` -> `waiting_at_restaurant` | Rider reaches the restaurant |
| `waiting_at_restaurant` -> `to_customer` | Simulated clock reaches `prep_done` |
| `to_customer` -> `free` | Rider reaches the customer |

Only `free` riders are dispatch candidates. Only `to_restaurant` and `to_customer`
riders are moved by the mover.

**The option rejected** was dropping `waiting_at_restaurant` and inferring "at the
restaurant" by comparing the rider's position against the target each tick. With the
explicit state, the mover is a lookup on one field. Without it, the mover must redo
geometry every tick to rediscover an arrival that was already known at the moment it
happened. One extra state removes guessing from the hot path.

### Part two: the status field, not a denormalised free-set

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **`status` field on the rider hash** (chosen) | The state lives in exactly one place, so the system cannot disagree with itself. Dispatch fetches all candidates in one pipeline and filters in Python. Measured at 2.643 ms per query. |
| `status` field plus a `riders:free` set | `SINTER` filters server-side, so only free riders are fetched. Measured at 1.881 ms per query - genuinely ~30% faster. But the same fact lives twice, and every transition must update both. One missed update leaves a rider in `riders:free` whose hash says `to_customer`, and dispatch hands that rider a second order. |
| Sets only, membership defines state | No duplication. But "what is rider 7 doing?" requires checking up to four sets, and `HGETALL rider:7` no longer reveals the rider's state, which makes debugging much harder. |

**The measurement, and what it did not settle**

Measured over 300 runs with 20 candidates, 4 of them free:

```
fetch all 20, filter in Python : 2.643 ms
SINTER first, fetch only 4     : 1.881 ms
difference                     : 0.762 ms  (1.52 s over a 2000-order run)
```

Worth recording that this **contradicted the expectation**. The prediction was that
pipelining would make the two approaches roughly equal, since both are one round trip
for the fetch. It did not - server-side filtering still wins by about 30%.

**Why the slower option was chosen anyway**

1. **The win is in the wrong currency.** 1.5 seconds of offline simulation time is
   traded for a permanent obligation to keep two copies of one fact in sync, forever,
   across every code path.
2. **The failure mode is silent and distant.** A drifted free-set does not crash. It
   produces a rider holding two orders, surfacing later as a strange CSV row. A bug that
   lies is worth far more than 1.5 seconds.
3. **It is addable later, not removable later.** `status` on the hash stays
   authoritative in both designs, so the free-set is a pure optimisation that can be
   added once a measurement demands it, with evidence rather than a guess.

**The distinction that decides it:** these dispatch calls are not on a customer's
critical path - they are part of a batch simulation. In a production service with a p99
latency budget, 0.76 ms on every call is real and could well justify the free-set.

**Revisit when:** dispatch latency per call starts to matter, which means either the
candidate pool grows well beyond 20 or the service is put in front of real traffic.

---

## 14. One `config.py`, reading environment variables with defaults

**Date:** 2026-10-07 - **Decided by:** Devvrath

**The failure being prevented:** both processes need the rider speed. If `sim.py` moves
riders at 25 km/h while `dispatch.py` estimates at 20 km/h, everything still runs and
nothing errors - but dispatch ranks riders using a wrong model of the world, and the
Day 5 greedy-versus-matching benchmark compares against a baseline that was broken the
whole time. A bug with no symptom that silently invalidates the results.

### The distinction that decides it

Shared values are two different kinds of thing, and conflating them is the actual
mistake:

| Group | What it is | Where it belongs | Values |
| --- | --- | --- | --- |
| **1. The experiment** | Facts about the world being simulated. Changing one changes what is being measured, so the change should be visible in git. | **In code** | rider speed, H3 resolution, `K`, tick length, prep time |
| **2. The machine** | Facts about where the code happens to be running. Nothing to do with dispatch. | **Outside code** | Redis host and port, service URL |

**Options considered**

| Option | Tradeoff |
| --- | --- |
| Plain `config.py` of constants | Simplest, one file, real Python types. But changing the Redis port means editing code, which treats Group 2 as though it were Group 1. |
| `os.getenv` wherever each value is needed | Fully configurable from outside. Two real problems: every value arrives as a **string** (the same trap as entry 6's string coercion - `os.getenv("H3_RESOLUTION")` is `'8'`, not `8`), and scattered `getenv` calls mean scattered defaults, which is the duplication the decision was meant to eliminate. |
| **`config.py` reading env vars with defaults** (chosen) | One import site for both processes. Group 1 stays plainly in code. Group 2 is overridable without editing anything. The string-to-int conversion happens in exactly one place, which is the same principle that justifies `state.py`. Costs about five lines over the plain version. |

**Why:** it is the only option that respects the Group 1 / Group 2 split. The phrasing
worth keeping: *settings that describe the experiment live in code so changes show up in
git; settings that describe the machine come from the environment.*

**What this gives up:** a little ceremony, and the discipline of remembering which group
a new constant belongs to when adding one. Getting that wrong is the likely future
mistake - putting rider speed behind an env var would make an experiment's parameters
invisible in git history.

**Revisit when:** the two processes are deployed separately, at which point Group 1
values also need a shared source that is not a local file - a config service, or baking
them into a shared artifact.
