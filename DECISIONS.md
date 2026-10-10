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

**Sizing note**, measured with h3 4.5.0 rather than quoted from a doc table:

| | |
| --- | --- |
| Average edge length at res 8 | 0.531 km |
| Average cell area | 0.737 km2 |
| Centre-to-centre spacing | 0.920 km |
| `grid_disk(cell, 2)` | 19 cells |
| Reach, centre to outer centre | 1.84 km |
| Reach, outer boundary | 2.37 km |

So `k=2` covers roughly a 2.4 km radius. These are averages over a grid whose cells are
not equal in area, so the real reach still wants measuring in the sim. An earlier version
of this entry said 0.46 km edge and about 2 km reach, from memory rather than from the
library; the numbers above were run.

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

---

## 15. The Redis key layout and the rider hash schema

**Date:** 2026-10-07 - **Decided by:** Devvrath, except where noted

### The shape

```
dispatch:rider:<id>                  (hash)
    lat          float
    lng          float
    state        free | to_restaurant | waiting_at_restaurant | to_customer
    target_lat   float   - absent when free
    target_lng   float   - absent when free
    order_id     string  - absent when free

dispatch:cell:<h3 cell id>           (set of bare rider ids)
```

### The choices behind it

**A project prefix (`dispatch:`).** Costs a few bytes on every key. Buys the ability to
find or wipe exactly this project's keys, and makes the database legible to someone who
did not write it. Chosen partly because entry 10 already demonstrated the cost of
assuming an instance is yours alone.

**Cell sets hold bare rider ids, not full hash keys.** Full keys would let `SUNION`
output feed straight into `HMGET`, skipping one lookup. Rejected for two reasons: it
bakes the key format - including the prefix - into the stored data, so renaming a key
would require rewriting every set; and the dispatch endpoint returns a rider *id*, so
storing keys would mean converting to keys and then back again. The key is only needed
in the middle of the operation. Store the identity, derive the key.

**The rider's H3 cell is derived, not stored.** To move a rider between cell sets the
writer needs the old cell. Deriving it is `latlng_to_cell(old_lat, old_lng, resolution)`
- one pure function call on data the writer is already holding, because entry 12 means
the simulator reads the old position every tick anyway. Storing it as a field would be
one more thing that can drift from `lat`/`lng`. Note that **entry 12 changed the
economics of this choice**: had the simulator owned state in memory, the old cell would
have been in hand already and the trade would look different.

**No key listing all riders.** Nothing in the dispatch path needs it. Seeding generates
the ids, fleet size lives in `config.py`, and teardown or inspection can use `SCAN`. It
was considered only because it felt tidy, which is not a use case - unused structure
invites someone later to assume it is maintained.

**`keys.py` owns key names; `state.py` owns field names.** `keys.py` is about addressing
(where data lives), `state.py` about contents (what is in it and what type it is).
Knowing a field is called `lat` is inseparable from knowing it is a float, and entry 6's
string-coercion trap puts type knowledge in `state.py`. Splitting them would force two
files to agree about one thing.

**`rider_id` is not a field.** The id is already the key. Storing it again is the same
duplication rejected above.

### Availability, deliberately not built

Devvrath identified a distinction that had not been raised: **what a rider is doing** is
not the same as **whether a rider wants work**. A rider can be `free` - on no delivery -
and still not be a candidate, because they went off shift. Production systems model both
dimensions; an Uber driver is offline, or online-waiting, or online-on-trip.

Not implemented, because no rider in this simulation ever goes off shift. The field
would be `true` for every rider for the whole run, and a field that never varies cannot
be tested and cannot be wrong. The insight is recorded here; the field arrives the day
shifts are modelled.

### Destination and order id live on the hash

**Decided by:** Claude, at Devvrath's request. Open to reopening.

| Option | Tradeoff |
| --- | --- |
| **`target_lat`, `target_lng`, `order_id` on the rider hash** (chosen) | The mover reads one hash and has everything. Keeps entry 12 coherent: a rider in Redis is a *complete* rider. Lets one `HGETALL` answer "what is this rider doing and where is it going", which matters when the movement loop misbehaves. |
| Keep them in simulator memory | Less data in Redis. But a rider's state is then split - position in Redis, destination in Python - so neither place holds a whole rider. That is precisely the problem entry 12 was chosen to avoid, reintroduced indirectly. |
| Both | Rejected first. Duplication feels like insurance but adds a *new* failure mode: the two copies can disagree, with nothing to say which is right. Same shape as the `riders:free` set rejected in entry 13 and the stored cell rejected above. |

**The cost assumption that turned out to be wrong:** more fields looked like more traffic
on the hot path. It is not - the destination only changes on a **state transition**
(assignment, pickup), four writes per order, while the per-tick write still touches only
`lat` and `lng`. And since entry 12 means the rider is read every tick regardless, three
extra fields ride along in the same `HMGET` - the same single round trip, a few more
bytes. Second time in one session that a guess did not survive measurement.

**A nuance worth keeping:** `order_id` is a *pointer*. The order itself, with all its
timestamps, lives in simulator memory and then the CSV. That is not duplication - Redis
holds the link, the simulator holds the order, and the dispatch service never needs the
order at all. Order timestamps are deliberately absent from Redis for the same reason.

**And a clarification that is easy to get wrong:** reading a rider from Redis into a
local variable is *not* keeping two copies. That is ordinary data flow. The problem is
only a copy that **persists across ticks** and starts being treated as authoritative.

**One consequence to implement:** on the transition back to `free`, the three fields must
be `HDEL`ed, not left in place. Otherwise a free rider carries a stale destination and a
completed order id, and the first code path that reads `target_lat` without checking
`state` sends a rider to an address it already visited. `state.py` treats absent as "no
destination".

**Revisit when:** something other than the simulator needs to know a rider's
destination, which would make the split-state option strictly worse than it already is.

---

## 16. Fleet size 75 in a 7 km city, and the difference between reproducible and representative

**Date:** 2026-10-10 - **Decided by:** Devvrath

### How this came up

Seeding 50 riders into an 8 km city was predicted to put about 11 candidates in a `k=2`
ring. The actual run reported **6**. Investigating rather than adjusting:

```
actual area of a k=2 disk  : 14.43 km2
share of a 64 km2 city     : 22.5%
expected candidates        : 11.3 of 50

over 200 different seeds:
   mean 11.1   stdev 2.8   min 5   p10 8   median 11   p90 15   max 18
```

The estimate was right. **Seed 42 simply drew an unusually sparse world** - 6 is near the
bottom of the range, where the worst of 200 seeds was 5.

### The insight worth more than the setting

**Reproducible and representative are different properties, and conflating them is an
evaluation error.**

- Determinism gives **comparability**: greedy and matching face an identical world, so a
  difference between them is real rather than noise.
- It does **not** give **representativeness**: if the frozen world is atypical, every
  absolute number from it is atypical too.

So on Day 5, *"matching cut p90 by 14%"* is a valid claim about this world.
*"Delivery takes 28 minutes"* would be a claim about a world that happens to be
rider-sparse.

**This makes multiple seeds a Day 4 requirement**, not an optional extra: report a
distribution across seeds for absolute metrics, and a paired comparison on identical
seeds for policy differences.

**And the thing deliberately not done:** the seed was not changed to produce a nicer
number. Choosing the seed that flatters the result is cherry-picking, and "how did you
pick that seed?" is a question an interviewer will ask.

### The settings

**Options considered**

| Option | Tradeoff |
| --- | --- |
| Keep 50 riders in 8 km, report the distribution | Honest, and thin rings make dispatch choices *harder*, which is more interesting. Leaves the headline ring count unrepresentative of the mean. |
| Raise `FLEET_SIZE` | Comfortable rings on any seed. Directly reduces contention. |
| Shrink `CITY_SIZE_KM` | Same density effect without more riders. |
| **Both, modestly** (chosen) | `FLEET_SIZE = 75`, `CITY_SIZE_KM = 7.0`. A `k=2` disk is then ~29% of a 49 km2 city. |

**The risk accepted, stated plainly:** fleet size is the knob that controls
**contention**, and contention is what Day 5 depends on. Batched min-cost matching only
beats greedy when free riders are scarce enough that two orders actually compete for
one rider. If free riders are always plentiful, greedy is near-optimal and the benchmark
shows nothing - not because matching is bad, but because the scenario has no conflict in
it. A denser fleet moves in that direction.

Raising the fleet and shrinking the city compounds, so both were applied modestly rather
than at the sizes first discussed.

**Revisit when:** Day 5 shows little or no gap between greedy and matching. The first
thing to check then is not the matching code but whether the scenario has enough
contention to distinguish the two - which means lowering `FLEET_SIZE` or raising the
order rate, and re-running.

---

## 17. Dispatch ranks on travel time to the restaurant, because the exact objective cannot rank differently

**Date:** 2026-10-10 - **Decided by:** Devvrath

**Options considered**

| Option | Tradeoff |
| --- | --- |
| **Travel time to the restaurant** (chosen) | The nearest free rider. One number already produced by `travel_times`. |
| `max(travel, prep_remaining)` | The *exact* objective: the customer waits for `max(arrival, prep_done)` plus the second leg, so minimising this minimises delivery time directly. Travel time is only a proxy for it. Requires prep state to be passed into the request, so the service would need to know the clock. |
| Total delivery time, both legs | Identical ranking to travel time alone. The second leg is restaurant-to-customer, which does not depend on which rider is chosen - adding the same constant to every candidate cannot change which one wins. |

**The measurement that settled it.** Run against the live 75-rider fleet with a
restaurant at the city centre:

```
prep time                         : 720 s
K=2 disk reach, 2.37 km at 25 km/h: 341 s

travel times of all 19 candidates : 56 s ... 324 s
option A distinct scores          : 19
option B distinct scores          : 1
```

**Every candidate ties under the exact objective.** No rider inside the searchable area
*can* arrive late, so pickup happens at 720 s whoever is sent. Option B would therefore
make dispatch choose arbitrarily, and the natural tiebreak to add is travel time - which
is option A. The proxy and the exact objective order identically here.

**The sentence this buys:** *"I ranked on travel time, and I checked whether the exact
objective would rank differently. It cannot: my search radius is 341 seconds of travel
against a 720 second prep, so every candidate arrives early and ties."* That is a
stronger answer than having implemented the more sophisticated version.

**When B would start to differ:** a shorter prep time, a much larger `K`, or an order
that has already waited 720+ seconds unassigned.

**Where it lives:** a named function in `dispatch.py`, not a `cost.py`. The function body
is one line returning its argument; a file for that is tidiness, not design (rule 12b).
Extracting it takes two minutes if Day 5 needs a second cost to compare.

**Revisit when:** `PREP_SECONDS` drops below the disk's travel reach, or `K_RING` grows
enough that the far edge of the disk exceeds prep time.

---

## 18. The `/assign` response contract

**Date:** 2026-10-10 - **Decided by:** Devvrath

### No free rider is HTTP 200 with a null id, not an error

| Option | Tradeoff |
| --- | --- |
| **200, `{"rider_id": null}`** (chosen) | The request succeeded; the answer is "nobody". The caller checks for null. |
| 404 Not Found | `/assign` exists - the thing missing is a rider, not the endpoint. |
| 503 Service Unavailable | Implies the service is broken, which it is not. |

**Why:** entry 2 already decided that an order with no rider in range **retries on a
later tick**. That makes an empty result designed behaviour that will happen constantly,
not a failure. Two consequences:

- An error status would force `sim.py` to wrap a routine outcome in exception handling.
- It would corrupt the Day 6-7 observability work: tracing would show a large error rate
  during entirely healthy operation. Teaching your own dashboard to lie is worse than
  the wrong status code.

### The response carries two counts as well as the id

```json
{"rider_id": "r70", "candidates": 19, "free_candidates": 19}
```

Entry 2 named a need this serves: an unassigned order has two different causes with two
different fixes, and they are indistinguishable from outside the service.

| Reading | Means | Fix |
| --- | --- | --- |
| `candidates == 0` | Nobody is nearby at all | `K_RING` or `FLEET_SIZE` is too small |
| `free_candidates == 0` | Riders are nearby but all busy | The fleet is saturated: a load problem, not a geometry one |

Two integers that were already computed, with a named consumer in the Day 4 metrics.
Without them that metric is undiagnosable.

### `K` comes from config, not from the request

`K_RING` is a Group 1 value by entry 14 - it describes **the experiment**, not the
machine. Changing it changes what is being measured, so the change belongs in git rather
than varying silently per request. Sweeping `K=2` against `K=3` on Day 4 means editing
one line and re-running, which is how an experiment parameter should behave.

### Candidate ids are sorted, and ties break on rider id

`SUNION` returns a set, and Python's string hashing is randomised per process, so set
iteration order **changes between runs of the same program**. Left alone, two riders with
identical travel times would be chosen differently on different runs of the same seed.

That would quietly undermine the reproducibility the entire Day 5 benchmark rests on -
and it would be very hard to find, because it only shows up on exact ties. Sorting the
candidate ids and breaking ties on rider id makes a request a pure function of the Redis
state.

**Revisit when:** the service is run as more than one process, where "deterministic given
the Redis state" stops being achievable anyway.

---

## 19. The dispatch service runs on port 8001

**Date:** 2026-10-10 - **Decided by:** Claude, forced by a collision. Open to reopening.

Port 8000 on this machine is already serving an unrelated project of Devvrath's
("CivilSpace Audit API"). `uvicorn` failed to bind, logged the error, and exited - while
`curl` kept getting answers, from the other application.

**This is the second time the same class of mistake has cost time**, after entry 10's
Redis port. Both times a connection succeeded and the reply looked plausible.

**The lesson, stated properly this time:** a working connection is not evidence you
reached *your* service. Verify **identity**, not reachability:

| Service | Weak check that lied | Check that works |
| --- | --- | --- |
| Redis | `PING` returned `PONG` | A freshly created database is empty |
| Dispatch | `/docs` returned 200 | `/openapi.json` reports the title `Rider dispatch` |

Checking `/docs` was the specific error: every FastAPI app serves it, so it proves a
FastAPI app is listening and nothing more.

Recorded in CLAUDE.md as a convention so it is checked rather than remembered.

**Revisit when:** the project moves to a compose file, where service names on an internal
network remove host port collisions entirely - which would also fix entry 10.
