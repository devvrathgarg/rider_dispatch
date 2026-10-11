"""The simulator. Owns the clock, creates orders, moves riders, logs to CSV.

It talks to the dispatch service over HTTP and is the only writer to Redis
(DECISIONS.md entry 1). Start the service first:

    uvicorn dispatch:app --host 127.0.0.1 --port 8001
    python sim.py

There is no time.sleep anywhere in here. `now` is a variable we add to, so an
hour of simulated time costs milliseconds of real time - which is what makes
running the same stream under two different policies affordable on Day 5.
"""

import csv
import random
import time
from dataclasses import dataclass, field

import httpx

import config
import state
from seed import random_point_in_city, seed_fleet
from travel import Point, step_toward

# Safety net for the drain phase. Orders retry forever by design, so an
# overloaded configuration would otherwise loop until interrupted.
MAX_TICKS = (config.RUN_SECONDS * 4) // config.TICK_SECONDS

CSV_FIELDS = (
    "order_id",
    "created",
    "assigned",
    "prep_done",
    "arrived_at_restaurant",
    "picked_up",
    "delivered",
    "rider_id",
    "restaurant_lat",
    "restaurant_lng",
    "customer_lat",
    "customer_lng",
    "attempts_no_candidate",
    "attempts_all_busy",
    "search_k",
)


@dataclass
class Order:
    """One order, with its timestamps filled in as the run proceeds.

    Not frozen, unlike Rider: a Rider is a snapshot of something Redis owns,
    whereas an Order is owned here and genuinely changes over its lifetime.

    Only raw timestamps are stored, never durations. Delivery time, dispatch
    latency and rider wait are all derivable from these, so adding a metric on
    Day 4 will not need another simulation run.
    """

    order_id: str
    created: float
    restaurant: Point
    customer: Point
    prep_done: float

    assigned: float | None = None
    arrived_at_restaurant: float | None = None
    picked_up: float | None = None
    delivered: float | None = None
    rider_id: str | None = None

    # Why this order had to wait, counted per failed attempt. Entry 18: an
    # unassigned order has two causes with two different fixes, and they are
    # indistinguishable unless counted separately.
    attempts_no_candidate: int = 0
    attempts_all_busy: int = 0

    # Which K found the rider. Above config.K_RING means the primary disk
    # was empty and the fallback had to widen (entry 20).
    search_k: int | None = None

    def as_csv_row(self) -> list:
        return [
            self.order_id,
            round(self.created, 1),
            self.assigned,
            round(self.prep_done, 1),
            self.arrived_at_restaurant,
            self.picked_up,
            self.delivered,
            self.rider_id,
            round(self.restaurant[0], 6),
            round(self.restaurant[1], 6),
            round(self.customer[0], 6),
            round(self.customer[1], 6),
            self.attempts_no_candidate,
            self.attempts_all_busy,
            self.search_k,
        ]


def make_restaurants(rng: random.Random, count: int) -> list[Point]:
    """A fixed set of restaurant locations, reused by every order."""
    return [random_point_in_city(rng) for _ in range(count)]


def make_order_stream(
    rng: random.Random,
    restaurants: list[Point],
    run_seconds: float,
    orders_per_hour: float,
) -> list[Order]:
    """The whole run's orders, generated before the clock starts.

    Arrivals are a Poisson process, produced as exponential gaps between
    consecutive orders - which is the defining property of such a process, and
    needs only random.expovariate from the standard library. The usual advice is
    numpy.random.poisson, which would be a dependency for one function.

    Generating the stream up front means the tick loop needs no randomness at
    all, and the list itself is the frozen, inspectable order stream that Day 4
    wants for comparing policies.
    """
    rate_per_second = orders_per_hour / 3600
    orders = []
    clock = 0.0

    while True:
        clock += rng.expovariate(rate_per_second)
        if clock >= run_seconds:
            return orders

        orders.append(
            Order(
                order_id="o%04d" % len(orders),
                created=clock,
                restaurant=rng.choice(restaurants),
                customer=random_point_in_city(rng),
                prep_done=clock + config.PREP_SECONDS,
            )
        )


class Simulator:
    """One run: a fleet, an order stream, and a clock to push them through."""

    def __init__(self, client: httpx.Client, seed: int):
        # Independent generators so changing the fleet does not perturb the
        # order stream, and vice versa.
        self.rng_fleet = random.Random(f"{seed}-fleet")
        self.rng_orders = random.Random(f"{seed}-orders")

        self.client = client
        self.now = 0.0
        self.ticks = 0
        self.km_per_tick = config.RIDER_SPEED_KMH / 3600 * config.TICK_SECONDS

        self.restaurants = make_restaurants(self.rng_fleet, config.RESTAURANT_COUNT)
        self.orders = make_order_stream(
            self.rng_orders,
            self.restaurants,
            config.RUN_SECONDS,
            config.ORDERS_PER_HOUR,
        )

        self.next_index = 0          # how far through self.orders we have got
        self.pending: list[Order] = []          # created, not yet assigned
        self.orders_by_rider: dict[str, Order] = {}   # assigned, in flight
        self.completed: list[Order] = []

    # --- the four phases of a tick ---------------------------------------

    def advance_riders(self) -> None:
        """Phases 1 and 2: move every busy rider, then apply any arrival.

        One pass rather than two, because an arrival is a direct consequence of
        the move just made. Splitting them would mean reading every busy rider
        out of Redis twice per tick for no gain.
        """
        rider_ids = sorted(self.orders_by_rider)
        if not rider_ids:
            return

        for rider in state.read_riders(rider_ids):
            order = self.orders_by_rider[rider.rider_id]
            position = rider.position

            if rider.is_moving and rider.target is not None:
                position = step_toward(rider.position, rider.target, self.km_per_tick)
                if position != rider.position:
                    state.move_rider(rider, position[0], position[1])

            self.apply_arrival(rider, order, position)

    def apply_arrival(self, rider: state.Rider, order: Order, position: Point) -> None:
        """Move one rider through whatever state transitions are now due.

        Arrival is an exact equality test on floats, which is normally wrong.
        It is correct here only because step_toward clamps the final step to
        land exactly on the target rather than near it.
        """
        arrived = rider.target is not None and position == rider.target
        current = rider.state

        if current == state.TO_RESTAURANT and arrived:
            state.mark_waiting_at_restaurant(rider.rider_id)
            order.arrived_at_restaurant = self.now
            current = state.WAITING_AT_RESTAURANT

        # Not elif: a rider that arrives after the food is already ready should
        # leave in the same tick, rather than idling for a tick that never was.
        if current == state.WAITING_AT_RESTAURANT and self.now >= order.prep_done:
            state.send_to_customer(rider.rider_id, order.customer)
            order.picked_up = self.now
            return

        if current == state.TO_CUSTOMER and arrived:
            # The rider goes free HERE, at the customer (entry 4), which is why
            # the fleet drifts toward demand over a run.
            state.release_rider(rider.rider_id)
            order.delivered = self.now
            del self.orders_by_rider[rider.rider_id]
            self.completed.append(order)

    def release_due_orders(self) -> None:
        """Phase 3: orders whose arrival time has now passed become pending."""
        while (
            self.next_index < len(self.orders)
            and self.orders[self.next_index].created <= self.now
        ):
            self.pending.append(self.orders[self.next_index])
            self.next_index += 1

    def assign_pending(self) -> None:
        """Phase 4: ask the service to assign every order still waiting.

        Oldest order first, ties broken on id. **That list order is the priority
        order**, and the service honours it - so the FIFO rule lives here, in
        the component that actually has a queue, while the decision of which
        rider stays in the dispatcher.

        One HTTP round trip for the whole batch instead of one per order. The
        service avoids handing the same rider to two orders inside the batch
        itself, which is the thing a per-order endpoint could not express
        (entry 22).
        """
        if not self.pending:
            return

        batch = sorted(self.pending, key=lambda o: (o.created, o.order_id))
        reply = self.client.post(
            "/assign_batch",
            json={
                "orders": [
                    {
                        "order_id": order.order_id,
                        "lat": order.restaurant[0],
                        "lng": order.restaurant[1],
                        # How long this order has queued. The service is told a
                        # duration rather than reading a clock.
                        "waited_seconds": self.now - order.created,
                    }
                    for order in batch
                ]
            },
        )
        reply.raise_for_status()

        orders_by_id = {order.order_id: order for order in batch}
        still_pending = []

        for assignment in reply.json()["assignments"]:
            order = orders_by_id[assignment["order_id"]]

            if assignment["rider_id"] is None:
                if assignment["candidates"] == 0:
                    order.attempts_no_candidate += 1
                else:
                    order.attempts_all_busy += 1
                still_pending.append(order)
                continue

            state.assign_to_restaurant(
                assignment["rider_id"], order.order_id, order.restaurant
            )
            order.assigned = self.now
            order.rider_id = assignment["rider_id"]
            order.search_k = assignment["search_k"]
            self.orders_by_rider[assignment["rider_id"]] = order

        self.pending = still_pending

    # --- the loop ---------------------------------------------------------

    def everything_delivered(self) -> bool:
        return (
            self.next_index >= len(self.orders)
            and not self.pending
            and not self.orders_by_rider
        )

    def run(self) -> None:
        state.clear_all()
        seed_fleet(config.FLEET_SIZE, self.rng_fleet)

        while True:
            self.now += config.TICK_SECONDS
            self.ticks += 1

            self.advance_riders()
            self.release_due_orders()
            self.assign_pending()

            if self.everything_delivered():
                return
            if self.ticks > MAX_TICKS:
                raise RuntimeError(
                    f"still running after {self.ticks} ticks "
                    f"({len(self.pending)} pending, "
                    f"{len(self.orders_by_rider)} in flight). "
                    "The fleet is almost certainly overloaded: lower "
                    "ORDERS_PER_HOUR or raise FLEET_SIZE."
                )

    def check_invariants(self) -> None:
        """The runnable check for this module: the state machine's own rules.

        If any transition is wrong, or a timestamp is recorded at the wrong
        moment, one of these fails loudly instead of producing a plausible CSV.
        """
        assert len(self.completed) == len(self.orders), "orders were lost"

        for order in self.completed:
            assert order.rider_id is not None, order
            assert order.created <= order.assigned, order
            assert order.assigned <= order.arrived_at_restaurant, order
            assert order.arrived_at_restaurant <= order.picked_up, order
            assert order.picked_up <= order.delivered, order
            # Nobody collects food before the kitchen has finished it.
            assert order.picked_up >= order.prep_done, order

        riders = state.read_riders([f"r{i}" for i in range(config.FLEET_SIZE)])
        assert len(riders) == config.FLEET_SIZE, "the fleet changed size"
        assert all(r.state == state.FREE for r in riders), "a rider ended busy"
        assert all(r.target is None for r in riders), "a free rider kept a target"


def check_service(client: httpx.Client) -> None:
    """Confirm we reached OUR service, not merely something that answered.

    Entry 19: both port collisions in this project produced a working
    connection to the wrong program. Checking /docs would not help - every
    FastAPI app serves it. The title is what identifies this one.
    """
    try:
        info = client.get("/openapi.json").json()["info"]["title"]
    except httpx.ConnectError:
        raise SystemExit(
            f"No service at {config.DISPATCH_URL}. Start it with:\n"
            f"  uvicorn dispatch:app --host {config.DISPATCH_HOST} "
            f"--port {config.DISPATCH_PORT}"
        )

    if info != "Rider dispatch":
        raise SystemExit(
            f"Something else is on {config.DISPATCH_URL}: it calls itself "
            f"{info!r}, not 'Rider dispatch'."
        )

    # Which policy is the service ACTUALLY running? config.POLICY is read at
    # import, so a uvicorn started before the config changed would quietly keep
    # running the old policy while this process believed otherwise - which
    # would benchmark greedy against greedy and look like a real result. An
    # empty batch costs nothing and turns that into an error.
    reply = client.post("/assign_batch", json={"orders": []})
    reply.raise_for_status()
    running = reply.json()["policy"]
    if running != config.POLICY:
        raise SystemExit(
            f"The service is running policy {running!r} but config.POLICY is "
            f"{config.POLICY!r}. Restart uvicorn to pick up the change."
        )


def write_csv(orders: list[Order], path: str) -> None:
    # newline="" matters on Windows: without it, csv writes \r\r\n and every
    # other row of the file is blank.
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for order in sorted(orders, key=lambda o: o.order_id):
            writer.writerow(order.as_csv_row())


def summarise(simulator: Simulator, real_seconds: float) -> None:
    orders = simulator.completed
    delivery = sorted(o.delivered - o.created for o in orders)
    waits = sorted(o.picked_up - o.arrived_at_restaurant for o in orders)
    dispatch_delay = sorted(o.assigned - o.created for o in orders)

    def median(values):
        return values[len(values) // 2]

    print()
    print("orders            : %d" % len(orders))
    print("simulated time    : %.0f s (%.1f minutes, %d ticks)"
          % (simulator.now, simulator.now / 60, simulator.ticks))
    print("real time         : %.2f s" % real_seconds)
    print()
    print("delivery time     : median %.1f min, worst %.1f min"
          % (median(delivery) / 60, delivery[-1] / 60))
    print("wait for dispatch : median %.0f s, worst %.0f s"
          % (median(dispatch_delay), dispatch_delay[-1]))
    print("rider idle at shop: median %.1f min, worst %.1f min"
          % (median(waits) / 60, waits[-1] / 60))
    print()
    print("orders that waited because nobody was near : %d"
          % sum(1 for o in orders if o.attempts_no_candidate))
    print("orders that waited because all were busy   : %d"
          % sum(1 for o in orders if o.attempts_all_busy))
    print("orders that needed the widened fallback    : %d"
          % sum(1 for o in orders if o.search_k > config.K_RING))
    print()
    print("Proper metrics are Day 4. These are sanity numbers only.")


if __name__ == "__main__":
    state.require_redis()

    with httpx.Client(base_url=config.DISPATCH_URL, timeout=10.0) as client:
        check_service(client)

        simulator = Simulator(client, config.RANDOM_SEED)
        print("fleet %d, %d restaurants, %d orders over %d s"
              % (config.FLEET_SIZE, config.RESTAURANT_COUNT,
                 len(simulator.orders), config.RUN_SECONDS))

        started = time.perf_counter()
        simulator.run()
        real_seconds = time.perf_counter() - started

        simulator.check_invariants()
        write_csv(simulator.completed, config.CSV_PATH)
        summarise(simulator, real_seconds)
        print("wrote %s" % config.CSV_PATH)
        print("ok")
