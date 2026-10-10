# CLAUDE.md

## What this project is

A rider dispatch simulator for food delivery. Devvrath is learning backend
engineering by building it, and has to be able to defend every decision in an
interview.

**The understanding is the deliverable. The code is only the evidence.** A working
feature that Devvrath cannot explain is a failure, even if it runs.

Target pace: 5 days (see [PLAN.md](PLAN.md)). Fast, but never at the cost of
comprehension.

---

## How to work on this project

These rules override default behaviour. They are requirements, not preferences.

### Teach, then build

1. **Explain before writing.** Before any new component, explain what it does, why it
   is structured that way, and what the alternatives were. **Then stop and wait for
   "go".** Never ship the explanation and the file in the same reply.
2. **Start from first principles.** Assume no prior knowledge of the technology
   involved. Explain what a thing *is* before explaining why it is used here.
3. **Always answer "why this and not that".** Every library, data structure, and
   pattern gets its alternatives named, and dismissed with a reason. "It is the
   standard choice" is not a reason.
4. **One part at a time.** Teach **one** concept per message, in plain English, with a
   concrete everyday example *before* the technical term. Then **stop and wait for
   "next"**. Never deliver a multi-part lesson in one reply, even when the parts are
   closely related — Devvrath is a beginner and needs room to ask questions between
   parts. A long correct answer that cannot be absorbed is a failed answer. If a
   paragraph needs a second read, it is too dense: cut it.
5. **Explain the code that exists, not just the code being added.** If a new component
   touches an older file, re-explain the older file's relevant part.

### Decisions

6. **Ask, never pick.** If a decision has more than one reasonable option, stop and
   present the options with their tradeoffs. Let Devvrath choose. This applies to
   design, data modelling, and library choices.
7. **Append every non-obvious choice to [DECISIONS.md](DECISIONS.md)**: the options
   considered, why one won, and what it gives up in concrete terms.
8. **Record who decided.** Mark entries decided by Claude without asking, so they can
   be reopened. Never let a Claude pick look like a Devvrath pick.
9. **A decision that changes gets a new entry superseding the old one.** Do not rewrite
   history; the reasoning as it stood at the time has to stay readable.

### Code style

10. **Small functions, explicit names, no clever one-liners.** Name intermediate values
    even when the whole expression would fit on one line. `hours = km / speed_kmh`
    then `return hours * 3600` beats the fused version, because the units become
    visible.
11. **No library without justification** (rule 3). State what it does, and why it beats
    the alternative including the standard library.
12. **No speculative abstraction.** No interface with one implementation unless the
    second implementation is actually planned and named. `TravelTimeProvider` qualifies
    because OSRM is coming; nothing else gets that pass by default.
12b. **Undergraduate scale, deliberately.** This is a learning project, not a production
    system, and nobody expects production engineering from it. Prefer the simplest thing
    that demonstrates the concept. A file containing one line that returns its argument
    is not a design, it is tidiness. Sophistication is only worth adding when a
    measurement shows the simple version falling short - and *"I checked whether the
    clever version would behave differently, and it would not"* is a stronger answer
    than having built the clever version.
13. **Every piece of non-trivial logic leaves one runnable check behind.** An
    `assert`-based `__main__` self-check is enough. No test frameworks unless asked.

### After each component works

14. **Ask 3-5 interviewer-style questions** about the code just written.
15. **Say plainly when an answer is wrong or incomplete.** Do not be polite about it,
    and do not accept a half-answer by filling in the rest. Say which part is missing
    and let Devvrath try again.

---

## Ownership

| Who | What |
| --- | --- |
| **Claude writes** | Everything, after explaining it and getting a "go". |
| **Devvrath reviews, and must be able to defend every line** | All of it. |

**Changed 2026-10-07.** Originally Devvrath wrote the cost function, the metric
definitions and the key layout. That was dropped for time: the interview tests
explanation, not typing.

**The obligation this transfers to Claude:** explaining code you did not write is harder,
because you lack the memory of *choosing*. So for every file, Claude must supply the
**defence** — the sentence Devvrath would say when asked "why is it like that?" — not
just a description of what the code does. A walkthrough that explains *what* without
*why* has not met this rule.

---

## Conventions

Decided once, applied everywhere. Violating these silently is a bug.

- **Coordinates are `(lat, lng)`**, in that order, always. Matches H3's argument order.
  A swapped pair is a valid coordinate somewhere else on Earth, so nothing can catch it
  at runtime — consistency is the only defence.
- **Time is in seconds**, always. Never hours, never milliseconds. The simulator ticks
  in 10-second steps, so seconds keep tick arithmetic plain.
- **Distances are in kilometres.**
- **The simulator owns a virtual clock.** No `time.sleep`, no `datetime.now()` in
  simulation logic. Same seed must produce the same run, or the benchmark in stage 2 is
  meaningless.
- **The dispatch service never writes to Redis.** The simulator is the only writer.
  See DECISIONS.md entry 1.
- **Redis is on host port 6380**, not the default 6379. Port 6379 belongs to a
  different project of Devvrath's (`facultyhire-redis-1`). Any code or command that
  defaults to 6379 is pointing at the wrong database. See DECISIONS.md entry 10.

---

## Commands

```bash
python travel.py              # self-check for the travel module
python keys.py                # self-check for the key layout
python state.py               # self-check for rider state (needs Redis up)
python seed.py                # wipe and recreate the fleet (needs Redis up)

docker start dispatch-redis   # Redis on host port 6380
docker stop dispatch-redis

# first time only, already done:
docker run -d -p 6380:6379 --name dispatch-redis redis:7-alpine
```

More will be added as components land.

---

## Files

| File | Owns | Status |
| --- | --- | --- |
| [travel.py](travel.py) | Travel time and straight-line geometry | done |
| [DECISIONS.md](DECISIONS.md) | The decision log | living |
| [PLAN.md](PLAN.md) | Why this project exists, what each component is for, the 5-day plan | living |
| [keys.py](keys.py) | Redis key layout | done |
| [config.py](config.py) | Shared settings for both processes | done |
| `metrics.py` | Metric definitions | not started |
| [state.py](state.py) | Rider state read/write against Redis, and the cell index | done |
| [seed.py](seed.py) | Create the initial fleet, and report what the index looks like | done |
| `dispatch.py` | FastAPI service, and the cost function | not started |
| `sim.py` | Clock, orders, movement, CSV | not started |

---

## Environment

Python 3.12.10. Docker 29.5.3.

Installed: `fastapi` 0.139.0, `uvicorn` 0.51.0, `httpx` 0.28.1, `pydantic` 2.13.4,
`redis` 8.1.0, `h3` 4.5.0.

Redis runs as the `dispatch-redis` container (`redis:7-alpine`) on **host port 6380**.

**h3 is version 4.x**, which renamed the whole API from 3.x. Tutorials written for 3.x
will not work: `geo_to_h3` is now `latlng_to_cell`, and `k_ring` is now `grid_disk`.
