# Why this project exists, and how we build it

A reference document. Read top to bottom once; come back to the component table and the
day plan as we go.

---

# Part 1: The problem

## The real-world problem

A food delivery company has two populations moving in a city:

- **Orders** appearing continuously at restaurants, each with a customer somewhere else.
- **Riders** moving around, some busy, some free.

Every time an order appears, something has to decide: **which rider takes it?**

That decision has to be made in milliseconds, thousands of times an hour, with
incomplete information — you do not know what orders are coming next. And getting it
wrong is expensive in three different directions at once:

| Get it wrong by | And you lose |
| --- | --- |
| Sending a far rider | Late food, refunds, churned customers |
| Leaving riders idle | You pay for capacity you did not use |
| Clustering riders badly | A whole neighbourhood has no coverage |

This single decision — *order to rider* — is the core engineering problem at Swiggy,
Zomato, Uber Eats, DoorDash, Rapido, and every company like them. It is also one of the
most common system design interview questions, usually phrased as "design Uber" or
"design a food delivery dispatch system".

## Why build a *simulator* and not the real thing

This is the most important idea in the project, so it goes first.

You cannot develop a dispatch algorithm against real traffic. Three reasons:

1. **You cannot experiment.** Trying a new algorithm on real customers means real late
   deliveries while you find out it was worse.
2. **You cannot reproduce anything.** A bug that only appears when 400 orders arrive in
   a particular order, in a particular part of the city, is not reproducible on a live
   system. It happens once and you never see it again.
3. **You cannot compare.** "Algorithm B is better than A" is unfalsifiable if A and B
   never faced the same orders. Traffic on Tuesday is not traffic on Wednesday.

A simulator fixes all three by giving you a **world you control**:

- A **virtual clock**, so an hour of simulated time runs in milliseconds.
- A **seeded random stream**, so the same seed produces the exact same orders, at the
  exact same times, at the exact same places, every single run.
- **Full observability**, because you wrote every part of the world and can log
  anything.

That turns a vague claim into a measurement:

> *"I think batched matching is better than greedy"*
> becomes
> *"On a frozen 2,000-order stream, batched matching cut p90 delivery time from 34 to 29
> minutes and raised rider utilisation from 61% to 68%."*

The second sentence is the one that gets you hired. **The whole project is a machine for
producing sentences like that.**

## What you will actually learn

The dispatch problem is the excuse. These are the backend concepts it forces you to
meet, each one in a situation where you can feel why it matters:

| Concept | Where it shows up here |
| --- | --- |
| Service boundaries | Two processes talking over HTTP instead of one process calling a function |
| External state stores | Redis holding state neither process owns alone |
| Data modelling for an access pattern | Choosing structures from "what queries do I run", not from "what does the data look like" |
| Indexing | H3 cells turning "check 5,000 riders" into "check 12 riders" |
| Interface design at the right seam | `TravelTimeProvider` isolating the system's biggest inaccuracy |
| Determinism and testability | A virtual clock instead of wall-clock time |
| Measurement and percentiles | Why p90 matters and the average lies |
| Baselines and benchmarking | Greedy existing so matching has something to beat |
| Algorithmic tradeoffs | O(n) greedy versus O(n³) optimal matching, and when the cube is worth it |
| Load testing and tracing | k6 and OpenTelemetry, if we get there |

---

# Part 2: The architecture

```
┌─────────────────────────┐                      ┌──────────────────────────┐
│   sim.py                │   HTTP POST /assign  │   dispatch.py            │
│   (simulator process)   │ ───────────────────► │   (FastAPI service)      │
│                         │                      │                          │
│  owns the clock         │ ◄─────────────────── │  owns the decision       │
│  creates orders         │      rider_id        │  stateless, no clock     │
│  moves riders           │                      │                          │
│  writes CSV             │                      │                          │
└───────────┬─────────────┘                      └────────────┬─────────────┘
            │                                                 │
            │ WRITES ONLY                        READS ONLY   │
            │                                                 │
            └──────────────►┌──────────────────┐◄─────────────┘
                            │      Redis       │
                            │                  │
                            │ hash per rider   │
                            │ set per H3 cell  │
                            └──────────────────┘
```

Three things to notice:

- **The arrows into Redis are one-directional.** The simulator writes, the service
  reads. That is a deliberate constraint (DECISIONS.md entry 1), not an accident.
- **The service has no clock.** It does not know what time it is in the simulation and
  does not care. That is what makes it pointable at real traffic later.
- **The service is stateless between requests.** All state lives in Redis. Restart the
  service mid-run and nothing is lost.

---

# Part 3: Every component, and why it is there

## Why two processes instead of one

| Option | Tradeoff |
| --- | --- |
| **Two processes over HTTP** (chosen) | A real network boundary: real serialisation, real latency, real error handling, real timeouts. The service can later be load-tested with k6 or pointed at real traffic, with the simulator deleted. |
| One process, direct function call | Much simpler and faster to build. Teaches nothing about services. Makes roadmap stage 3 impossible, because there is no service to load-test. |

The hidden benefit of the boundary is that it **forces honesty about coupling**. In one
process it is trivially easy for the dispatch logic to peek at the simulator's clock or
its order list. Over HTTP, anything the service needs must be in the request body or in
Redis — which means the dependencies are visible in the function signature instead of
hiding in shared memory.

## Why FastAPI

**What it is:** a Python web framework. It maps URLs to functions, parses and validates
incoming JSON into typed Python objects, serialises the return value back to JSON, and
generates interactive API documentation from the type hints.

| Alternative | Why not |
| --- | --- |
| `http.server` (standard library) | Genuinely possible, and the lazy instinct says reach for stdlib first. But you hand-write routing, JSON parsing, input validation, status codes, and error handling — roughly 100 lines of plumbing that teaches nothing about dispatch. |
| Flask | Perfectly adequate for one endpoint. Needs extra libraries for request validation, and has no built-in async. FastAPI's validation comes from pydantic for free. |
| Django | A full framework: ORM, admin, templates, migrations, auth. We need one endpoint and no relational database. Overwhelmingly more machinery than the problem has. |

**The honest version:** for a single endpoint, Flask and FastAPI are nearly
interchangeable. FastAPI wins on two specific points — **validation at the trust
boundary** comes free via pydantic (a request with a latitude of `"banana"` or `999` is
rejected before our code runs, with a useful error), and it is the default choice for
new Python services, so it is the one worth having used.

Already installed. No new dependency.

## Why Redis

**What it is:** an in-memory data store. Not just key-to-string: it has real data
structures — hashes, sets, sorted sets, lists — with commands that operate on them
directly. It executes commands one at a time on a single thread, which means **every
individual command is atomic** with no locking required.

**Why an external store is needed at all:** the simulator and the service are separate
OS processes. They cannot share Python variables. Rider state has to live somewhere both
can reach.

| Alternative | Why not |
| --- | --- |
| A Python dict | Cannot cross a process boundary. This is the whole reason we need anything. |
| A shared file (JSON/SQLite on disk) | Two processes reading and writing the same file is a race condition generator, and every update rewrites more than it changed. |
| PostgreSQL | Gives ACID transactions, joins, constraints, durability. Rider positions change **every 10 seconds for every rider** — a write-heavy stream of tiny updates where the data is worthless after a minute. Postgres charges you durability on every write for data that does not need to survive anything. PostGIS is genuinely excellent at spatial queries, but it is a much larger thing to learn and operate. |
| Memcached | In-memory and fast, but stores opaque blobs only. No sets, no hashes, so no spatial index and no field-level updates. |

**Why Redis fits precisely:** rider live state is **ephemeral, high-churn, and
latency-critical**. A rider's position from 30 seconds ago has no value, so durability is
not a requirement — which removes the main reason to pay for a real database. Reads are
sub-millisecond, and we make several per dispatch decision. This is not a toy choice:
real dispatch systems use Redis for exactly this, and keep Postgres for the things that
must survive (orders, payments, riders' identities).

**What we give up:** if Redis dies, live rider state is gone. In this project that is
fine — the simulator can reseed. In production you would rebuild state from the riders'
next location pings.

## Why a hash per rider

A rider has several fields that change at different rates: position (every tick),
status (a few times per order), current order (once per order).

| Alternative | Why not |
| --- | --- |
| **A hash** (chosen) | `HSET rider:7 lat 12.9 lng 77.6` updates named fields in place. `HGET`/`HMGET` reads only the fields you need. Field-level access, no serialisation. |
| A JSON string | Changing the latitude means read the whole blob, parse it, edit one field, serialise, write it back. That read-modify-write cycle is both wasteful and a race condition in any multi-writer world. |
| Separate keys per field (`rider:7:lat`) | One round trip per field. A hash is one round trip for all of them, and keeps related data together. |

Exact field names and key format are **yours to write** in `keys.py`.

## Why a set per H3 cell — and the alternative you should know

The query the service needs is: *"which riders are in this cell?"*

| Alternative | Why not |
| --- | --- |
| **A set per cell** (chosen) | `SADD cell:8928…  rider:7`. `SUNION` across the 19 cells of a k-ring returns every candidate in **one command**. Adding and removing a rider is O(1). |
| Scan every rider | O(total riders) per order. At 5,000 riders and 2,000 orders that is 10 million hash reads for work an index does in thousands. |
| A sorted set keyed by distance | Distance from *what*? There is no fixed reference point — every order has a different restaurant. A sorted set needs one ordering; we need a different one per query. |

**The alternative worth being able to name: Redis has built-in geospatial commands.**
`GEOADD` and `GEOSEARCH ... BYRADIUS 2 km` would do "find riders within 2 km" directly,
with no H3 at all, in less code. Internally it is a sorted set scored by geohash.

Honest reasons H3 still wins *for this project*:

1. **You learn the index pattern explicitly** instead of calling a black box. "I built a
   spatial index" is a better interview answer than "Redis has a command for it".
2. **H3 cells are a reusable unit of aggregation.** Orders per cell per hour is a demand
   heatmap. Riders per cell is a supply map. `GEOSEARCH` gives you a radius query and
   nothing else.
3. **H3 is hierarchical.** A resolution-8 cell has a resolution-7 parent, so the same
   data rolls up to coarser zoom levels for free.

If this were production code with no learning goal, `GEOSEARCH` would be the lazier and
probably correct choice. That is a real tradeoff, logged honestly.

## Why H3

**What it is:** Uber's open-source geospatial indexing system. It covers the globe in
**hexagons** at 16 nested resolutions. Two functions carry this project:

- `latlng_to_cell(lat, lng, resolution)` → the id of the cell containing that point.
- `grid_disk(cell, k)` → that cell plus every cell within `k` rings of it.

**Why hexagons rather than squares.** This is a genuinely good interview answer.

In a square grid, a cell has 8 neighbours but they are not equivalent: 4 share an edge
(distance `d`) and 4 only touch at a corner (distance `d√2`, about 41% further). So
"adjacent" is ambiguous, and expanding outward in rings produces a square-ish region
that approximates a circle badly.

In a hexagonal grid, **every neighbour shares an edge and sits at the same distance from
the centre.** There are exactly 6 of them, all equivalent. That makes `k` rings out a
much better approximation of "within radius r", which is exactly the query we are
building.

| Alternative | Why not |
| --- | --- |
| Geohash | Encodes position as a string whose prefix is its containing cell, so prefix matching gives containment cheaply. But cells are rectangles whose aspect ratio distorts badly with latitude, and it has the notorious **edge problem**: two points metres apart across a cell boundary can share almost no prefix, so prefix search misses obvious neighbours. |
| S2 (Google) | Also excellent, uses squares on a projected cube. Comparable quality; H3's hexagon adjacency is the nicer property for radius-style search, and H3's Python library is simpler. |
| Plain grid of my own | You would reinvent H3, badly, and spend the project on geometry instead of dispatch. |

**Hexagon caveat worth knowing:** you cannot tile a sphere with hexagons alone. H3 has
**12 pentagons** at fixed positions (deliberately placed in the ocean). Code that assumes
every cell has exactly 6 neighbours is wrong at those 12 cells. It will never matter for
one city, but knowing it demonstrates you understand the structure rather than just the
API.

**Why resolution 8.** H3 resolutions run 0 (continent-sized) to 15 (under a square
metre). Resolution 8 cells average ~0.53 km edge and ~0.74 km² area — roughly a few city
blocks. The tradeoff:

- **Finer** (res 9, 10): more precise filtering, fewer irrelevant candidates — but more
  cells per k-ring, and riders cross cell boundaries constantly, so index churn rises.
- **Coarser** (res 7, 6): fewer cells to query and less churn — but each cell returns
  more riders who turn out to be too far, so the ranking step does more wasted work.

Resolution 8 at `k=2` gives 19 cells reaching about 2.4 km, which is a sensible
delivery radius. Both numbers are tunable and worth measuring rather than trusting.

## Why a virtual clock instead of real time

**What it means:** the simulator holds a variable, `now = 0`, and adds 10 to it each
tick. It never calls `time.sleep()` and never asks the operating system what time it is.

Two payoffs, and the second is the real one:

1. **Speed.** An hour of simulated time is 360 ticks, which run in milliseconds. With
   `sleep(10)` an hour of simulation takes an hour.
2. **Determinism.** Same seed → same order arrival times → same positions → same
   dispatch decisions → byte-identical CSV. **This is what makes benchmarking possible
   at all.** Without it, comparing greedy against matching compares two different
   worlds, and any difference you measure could be noise.

This generalises well beyond simulators: **business logic that reads the wall clock
cannot be tested deterministically.** Pass time in as a value rather than reading it from
the environment, and suddenly "what happens at a month boundary" is a test instead of a
wait.

## Why CSV for output

One row per order, appended as it completes:

```
order_id, created, assigned, prep_done, arrived_at_restaurant, picked_up, delivered, rider_id
```

**Why CSV:** the standard library has a `csv` module, the output is human-readable, it
opens in Excel, and pandas reads it in one line. The analysis is offline and one-shot —
load the file, compute percentiles — which is exactly what CSV is for.

| Alternative | Why not |
| --- | --- |
| A database table | Needs a schema, a connection, and a query layer to support an analysis that is four lines of pandas. |
| JSON lines | Equally fine, slightly better for nested data. We have flat rows, so CSV is simpler. |
| Logging to stdout | Not structured. You would write a parser to get it back. |

**The design point that matters more than the format:** we log **raw timestamps, not
computed durations**. From the seven timestamps you can derive delivery time, dispatch
latency, rider wait, and kitchen wait. If we logged `delivery_time_minutes` instead, every
new metric would need a new simulation run. Log the facts; compute the metrics later.

## Why greedy nearest-free-rider first

Greedy is the simplest thing that works: for each order, look at the free riders nearby,
pick the cheapest one, assign it.

It has an obvious flaw. It is **myopic** — it optimises each order in isolation, with no
knowledge of the order arriving two seconds later. Assigning your only nearby rider to
order A can leave order B, which that rider was perfect for, with nobody at all.

**We build it first precisely because it is flawed.** It is the **baseline**. Roadmap
stage 2 builds batched min-cost matching, which fixes exactly this myopia by deciding a
whole batch of orders together. And the only way to claim that is an improvement is to
run both on the identical frozen stream and show the numbers move.

That arc — *baseline, then measure, then improve, then prove* — is how real systems work,
and telling it is more impressive than having only the clever algorithm.

---

# Part 4: The 5-day plan

Assumes roughly 4-6 focused hours per day. Each day ends with something that runs, and a
set of interview questions.

## Day 1 — Redis and rider state

**Concepts taught:** what Redis is, hashes versus strings versus sets, why single-threaded
execution makes commands atomic, connection handling, what `decode_responses` does and why
it matters. The rider state machine as a modelling exercise.

**Decisions you make:** the Redis key layout (`keys.py`, yours). The rider state machine
— how many states, stored as a hash field or as separate sets. Where shared constants live.

**Built:** `keys.py` (yours, I review). `state.py` — read and write a rider, move a rider
between cell sets. `seed.py` — create N riders at random positions in a bounding box and
index them. A small inspection script so you can see Redis contents without guessing.

**Runs at the end of the day:** `python seed.py` then an inspection command showing riders
in cells.

**What you can claim:** "I modelled live rider state in Redis using a hash per entity and
a set per spatial cell, chosen against the access pattern rather than the data shape."

## Day 2 — The dispatch service

**Concepts taught:** H3 in practice — `latlng_to_cell`, `grid_disk`, what a cell id
actually is. How an index narrows and a metric orders. FastAPI request models and why
validation belongs at the trust boundary. Why N+1 round trips are the default failure mode
of this kind of endpoint, and what pipelining does about it.

**Decisions you make:** the cost function (`cost.py`, yours) and whether it accounts for
prep time. What the endpoint returns when nobody is free — an error, or a success with a
null rider.

**Built:** `cost.py` (yours, I review). `dispatch.py` — the FastAPI service: request model,
cell lookup, k-ring, `SUNION`, candidate hashes, free filter, `travel_times`, cost, min.

**Runs at the end of the day:** `uvicorn dispatch:app`, then a `curl` with a restaurant
location returning a rider id against the seeded fleet.

**What you can claim:** "I built the spatial candidate search with H3 k-rings over Redis
sets, keeping filtering and ranking as separate stages for separate reasons."

## Day 3 — The simulator, and first end-to-end run

**Concepts taught:** the tick loop as a design pattern. Poisson arrivals and why order
timing is random but reproducible. Seeded RNG. The full order lifecycle as a state
machine. Why `max(arrival, prep_done)` is the pickup time and why that `max` is lossy.

**Decisions you make:** order arrival rate and fleet size. The city bounding box.
Restaurant and customer placement — uniform, or clustered to make demand realistic.
What happens to an order that finds no rider.

**Built:** `sim.py` — virtual clock, tick loop, order generation, rider movement with cell
re-indexing, prep-time waiting, the HTTP call to dispatch, and the CSV writer.

**Runs at the end of the day:** a full simulated hour producing a CSV with rows. **This is
the first day the whole system exists.**

**What you can claim:** "I built a deterministic discrete-time simulator with a virtual
clock, so the same seed reproduces a run exactly."

## Day 4 — Metrics and the evaluation harness (roadmap stage 1)

**Concepts taught:** why the average hides the problem and percentiles do not. p50 versus
p90 versus p99 and who each one describes. Utilisation as a metric and how it misleads.
What a policy interface is, and why freezing the order stream is the thing that makes
comparison valid.

**Decisions you make:** the metric definitions (`metrics.py`, yours) — which numbers define
a good run. This has to be settled before any benchmark, because a harness with no agreed
metric cannot declare a winner.

**Built:** `metrics.py` (yours, I review). `analyze.py` — read the CSV, compute the metrics.
The policy interface, so an assignment strategy becomes a pluggable component. A frozen
seeded order stream saved to disk, so every future run faces identical input.

**Runs at the end of the day:** a metrics report for the greedy baseline, from a frozen
stream that will not change again.

**What you can claim:** "I built an offline evaluation harness over a frozen stream, so
policies are comparable rather than merely different."

## Day 5 — Batched min-cost matching, benchmarked (roadmap stage 2)

**Concepts taught:** why batching beats greedy, and what it costs — you deliberately wait,
trading a little latency for better global assignment. The assignment problem as
bipartite matching. The Hungarian algorithm at a conceptual level, and why the naive
version is O(n³). Candidate capping as the practical fix.

**Decisions you make:** the batching window length. The candidate cap. Whether to add
`scipy` for `linear_sum_assignment` or hand-roll matching — a library decision I will
argue both sides of before you choose.

**Built:** a second policy implementing batched min-cost matching behind the day-4
interface. A benchmark run of both policies on the identical frozen stream.

**Runs at the end of the day:** a side-by-side table — greedy versus matching, same orders,
same riders, same seed, different numbers.

**What you can claim:** the sentence from Part 1 — with your own real numbers in it.

---

# Part 5: What does not fit in 5 days

Being honest about the schedule is part of the plan.

**Roadmap stage 3 — k6 load testing and OpenTelemetry tracing — does not fit.** It is a
day's work of its own: a load generator script, a tracing SDK, a collector to receive
spans, and the reading to understand what a span and a trace context actually are. Pushing
it into day 5 would mean rushing both.

Three options, your call:

1. **Day 6-7.** Keep days 1-5 as planned, add stage 3 after. Nothing gets rushed.
2. **Drop stage 2, promote stage 3.** Day 5 becomes load testing and tracing, and batched
   matching is cut. I would argue against this: the greedy-versus-matching benchmark is the
   strongest story in the project, and stage 3 without it is infrastructure with nothing
   interesting to observe.
3. **Trim stage 3 to tracing only.** OpenTelemetry on the service, no k6. Roughly half a
   day, so it could share day 5. You get distributed tracing on the dispatch path, which
   is the more transferable of the two skills.

**Also not in 5 days, and deliberately:** the OSRM travel time provider. It needs a routing
server with real map data, which is a container, a map extract, and a preprocessing step.
The interface is ready for it; the implementation is a later project.

---

# Part 6: Decisions needed before Day 1

Carried over from DECISIONS.md so they are in one place:

| # | Decision | Owner | Status |
| --- | --- | --- | --- |
| 1 | Confirm or reopen entry 5 — provider takes one origin and N destinations | You | **open** |
| 2 | Confirm or reopen entry 6 — `Protocol` rather than `abc.ABC` | You | **open** |
| 3 | Rewrite `haversine_km` with named intermediate variables | You | done |
| 4 | The Redis key layout | You, I review | **open, blocks Day 1** |
| 5 | The rider state machine — how many states, and where stored | You, after my Day 1 explanation | **open, blocks Day 1** |
| 6 | Where shared constants live — duplicated, a `config.py`, or environment variables | You, after my Day 1 explanation | **open, blocks Day 1** |
| 7 | Which of the three stage-3 options above | You | done — days 6-7, entry 11 |
| 8 | Add `redis` and `h3` | You approve | done — entry 9 |
| 9 | Which Redis instance this project uses | You | done — port 6380, entry 10 |
