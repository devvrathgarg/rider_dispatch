"""Rider state in Redis. The only module that reads or writes rider data.

It exists for two reasons, both of which are the reason Redis calls are not
simply scattered around the codebase:

  1. Conversion.  Redis stores text and nothing else, so a latitude comes back
     as '12.98', not 12.98. Comparing that string against a number succeeds and
     returns a wrong answer with no error at all. This module is the single
     boundary where untyped text becomes typed Python, so nothing else in the
     project ever has to call float().

  2. The cell index.  A rider's position lives in its hash, but *finding*
     riders needs the per-cell sets. When a rider crosses a cell boundary both
     have to change together, so both changes live inside one function instead
     of being a rule someone has to remember.

Redis is the source of truth (DECISIONS.md entry 12). A Rider handed back from
here is a frozen snapshot: a local copy for data to flow through, never a second
place where state lives.
"""

from dataclasses import dataclass

import h3
import redis

import config
from keys import all_keys_pattern, cell_key, rider_key

# The four states, and the transitions between them, from DECISIONS.md entry 13.
#
#   free -> to_restaurant -> waiting_at_restaurant -> to_customer -> free
#
FREE = "free"
TO_RESTAURANT = "to_restaurant"
WAITING_AT_RESTAURANT = "waiting_at_restaurant"
TO_CUSTOMER = "to_customer"

# The only states in which a rider has somewhere to be. The mover reads this
# rather than working out from a position whether a rider has arrived.
MOVING_STATES = (TO_RESTAURANT, TO_CUSTOMER)

# Hash field names live here, not in keys.py: knowing a field is called 'lat' is
# inseparable from knowing it is a float, and the conversion is in this file.
# The last three are absent, not blank, while a rider is free.
FIELDS = ("lat", "lng", "state", "target_lat", "target_lng", "order_id")
ORDER_FIELDS = ("target_lat", "target_lng", "order_id")

# decode_responses=True so Redis returns str and never bytes. Mixing the two is
# miserable: b'free' == 'free' is False, forever, in every comparison.
client = redis.Redis(
    host=config.REDIS_HOST,
    port=config.REDIS_PORT,
    decode_responses=True,
)


@dataclass(frozen=True)
class Rider:
    """A typed snapshot of one rider, as of the moment it was read.

    Frozen on purpose. Changing a field here would change nothing in Redis, so
    the type system says what the design says: this is a copy, not the rider.
    """

    rider_id: str
    lat: float
    lng: float
    state: str
    target_lat: float | None = None
    target_lng: float | None = None
    order_id: str | None = None

    @property
    def position(self) -> tuple[float, float]:
        """(lat, lng), the shape travel.py expects."""
        return (self.lat, self.lng)

    @property
    def target(self) -> tuple[float, float] | None:
        """Where this rider is heading, or None if it is not going anywhere."""
        if self.target_lat is None or self.target_lng is None:
            return None
        return (self.target_lat, self.target_lng)

    @property
    def is_moving(self) -> bool:
        return self.state in MOVING_STATES


def cell_of(lat: float, lng: float) -> str:
    """The H3 cell containing a position.

    Derived every time rather than stored on the rider (DECISIONS.md entry 15):
    one pure function call beats a field that can drift from lat/lng.
    """
    return h3.latlng_to_cell(lat, lng, config.H3_RESOLUTION)


# --- reading -----------------------------------------------------------------


def read_rider(rider_id: str) -> Rider:
    """One rider, with real Python types. Raises if the rider does not exist."""
    row = client.hmget(rider_key(rider_id), list(FIELDS))
    return _row_to_rider(rider_id, row)


def read_riders(rider_ids) -> list[Rider]:
    """Several riders in ONE round trip, in the order given.

    Written as a pipeline rather than a loop of reads on purpose. A loop would
    be the N+1 problem: each read is a separate trip to Redis, and measured on
    this machine about 98% of that time is spent waiting on the network rather
    than on Redis doing work. Twenty riders cost ~31 ms as a loop and ~3.5 ms
    as one pipeline.
    """
    ids = list(rider_ids)
    if not ids:
        return []

    pipe = client.pipeline()
    for rider_id in ids:
        pipe.hmget(rider_key(rider_id), list(FIELDS))
    rows = pipe.execute()

    return [_row_to_rider(rider_id, row) for rider_id, row in zip(ids, rows)]


def riders_in_cells(cell_ids) -> set[str]:
    """Every rider id in any of these cells, as one SUNION.

    Returns bare ids, because that is what the cell sets hold: a key is only
    needed in the middle of an operation, so it is never stored.
    """
    keys = [cell_key(cell_id) for cell_id in cell_ids]
    if not keys:
        return set()
    return client.sunion(keys)


def _row_to_rider(rider_id: str, row) -> Rider:
    """Turn one HMGET result into a Rider. The conversion boundary.

    A missing rider raises instead of returning blanks. HMGET on a key that does
    not exist returns a list of Nones rather than an error, so without this a
    rider id present in a cell set but missing its hash would quietly become a
    rider at latitude 0.0 in the Gulf of Guinea.
    """
    values = dict(zip(FIELDS, row))

    if values["lat"] is None:
        raise KeyError(
            f"rider {rider_id!r} has no hash at {rider_key(rider_id)}. "
            "A cell set holding an id with no hash means the index has drifted."
        )

    return Rider(
        rider_id=rider_id,
        lat=float(values["lat"]),
        lng=float(values["lng"]),
        state=values["state"],
        target_lat=_optional_float(values["target_lat"]),
        target_lng=_optional_float(values["target_lng"]),
        order_id=values["order_id"],
    )


def _optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    return float(raw)


# --- writing -----------------------------------------------------------------


def create_rider(rider_id: str, lat: float, lng: float) -> None:
    """Add one free rider at a position, and index it into its cell."""
    pipe = client.pipeline()
    pipe.hset(
        rider_key(rider_id),
        mapping={"lat": lat, "lng": lng, "state": FREE},
    )
    pipe.sadd(cell_key(cell_of(lat, lng)), rider_id)
    pipe.execute()


def move_rider(rider: Rider, lat: float, lng: float) -> None:
    """Write a rider's new position, re-indexing its cell only if it changed.

    Takes the snapshot the caller already read, because the old position is what
    tells us which cell set to remove the rider from. That is only cheap because
    Redis is the source of truth, so the simulator reads the rider every tick
    anyway (entry 12) - the old cell is one function call from data already held.

    The `if` matters: a rider covers about 69 m per tick and a cell is about
    530 m across, so the great majority of ticks stay inside one cell and should
    not touch the sets at all.
    """
    old_cell = cell_of(rider.lat, rider.lng)
    new_cell = cell_of(lat, lng)

    pipe = client.pipeline()
    pipe.hset(rider_key(rider.rider_id), mapping={"lat": lat, "lng": lng})
    if new_cell != old_cell:
        pipe.srem(cell_key(old_cell), rider.rider_id)
        pipe.sadd(cell_key(new_cell), rider.rider_id)
    pipe.execute()


def assign_to_restaurant(
    rider_id: str, order_id: str, restaurant: tuple[float, float]
) -> None:
    """free -> to_restaurant. Records which order and where to go."""
    client.hset(
        rider_key(rider_id),
        mapping={
            "state": TO_RESTAURANT,
            "order_id": order_id,
            "target_lat": restaurant[0],
            "target_lng": restaurant[1],
        },
    )


def mark_waiting_at_restaurant(rider_id: str) -> None:
    """to_restaurant -> waiting_at_restaurant, on arrival at the pickup."""
    client.hset(rider_key(rider_id), "state", WAITING_AT_RESTAURANT)


def send_to_customer(rider_id: str, customer: tuple[float, float]) -> None:
    """waiting_at_restaurant -> to_customer, once the food is ready."""
    client.hset(
        rider_key(rider_id),
        mapping={
            "state": TO_CUSTOMER,
            "target_lat": customer[0],
            "target_lng": customer[1],
        },
    )


def release_rider(rider_id: str) -> None:
    """to_customer -> free, on arrival at the customer.

    The order fields are DELETED, not blanked. A free rider left holding a stale
    destination is a trap: the first code path that reads target_lat without
    checking state first would send a rider to an address it already visited.
    Absent means "not going anywhere", which is the truth.
    """
    pipe = client.pipeline()
    pipe.hset(rider_key(rider_id), "state", FREE)
    pipe.hdel(rider_key(rider_id), *ORDER_FIELDS)
    pipe.execute()


def clear_all() -> int:
    """Delete every key this project owns. Returns how many. Uses SCAN."""
    pipe = client.pipeline()
    count = 0
    for key in client.scan_iter(all_keys_pattern()):
        pipe.delete(key)
        count += 1
    pipe.execute()
    return count


if __name__ == "__main__":
    # Ids are namespaced so this check never touches a real fleet, and it does
    # not call clear_all() for the same reason.
    A = "selftest-a"
    B = "selftest-b"

    # Start from a cell centre, so a tiny nudge is guaranteed to stay inside it.
    HOME = h3.cell_to_latlng(h3.latlng_to_cell(12.9716, 77.5946, config.H3_RESOLUTION))
    FAR = (12.9900, 77.6100)
    home_cell = cell_of(*HOME)
    far_cell = cell_of(*FAR)
    assert home_cell != far_cell, "test positions must be in different cells"

    create_rider(A, *HOME)
    create_rider(B, *HOME)

    # 1. the conversion boundary: text in Redis comes back as real numbers
    a = read_rider(A)
    assert isinstance(a.lat, float) and isinstance(a.lng, float)
    assert (a.lat, a.lng) == HOME
    assert a.state == FREE
    assert a.target is None and a.order_id is None
    assert a.is_moving is False

    # 2. the cell index found it
    assert A in riders_in_cells([home_cell])

    # 3. a pipelined multi-read keeps order and length
    both = read_riders([A, B])
    assert [r.rider_id for r in both] == [A, B]
    assert read_riders([]) == []

    # 4. moving inside one cell updates the position and leaves the sets alone
    nudged = (HOME[0] + 0.0001, HOME[1])
    assert cell_of(*nudged) == home_cell
    move_rider(a, *nudged)
    assert read_rider(A).lat == nudged[0]
    assert A in riders_in_cells([home_cell])

    # 5. moving across a boundary re-indexes both sets
    move_rider(read_rider(A), *FAR)
    assert A not in riders_in_cells([home_cell])
    assert A in riders_in_cells([far_cell])
    assert riders_in_cells([home_cell, far_cell]) >= {A, B}

    # 6. assignment records state, destination and order, all typed
    assign_to_restaurant(A, "order-1", HOME)
    a = read_rider(A)
    assert a.state == TO_RESTAURANT
    assert a.order_id == "order-1"
    assert a.target == HOME
    assert isinstance(a.target_lat, float)
    assert a.is_moving is True

    # 7. waiting at the restaurant is not a moving state
    mark_waiting_at_restaurant(A)
    assert read_rider(A).is_moving is False

    send_to_customer(A, FAR)
    assert read_rider(A).target == FAR
    assert read_rider(A).is_moving is True

    # 8. release removes the order fields rather than blanking them
    release_rider(A)
    a = read_rider(A)
    assert a.state == FREE
    assert a.target is None and a.order_id is None

    # 9. an unknown rider is loud, not silently a rider at (0.0, 0.0)
    try:
        read_rider("selftest-missing")
    except KeyError:
        pass
    else:
        raise AssertionError("a missing rider must raise, not return blanks")

    # Clean up only what this check created.
    for rider_id in (A, B):
        rider = read_rider(rider_id)
        client.srem(cell_key(cell_of(rider.lat, rider.lng)), rider_id)
        client.delete(rider_key(rider_id))

    print("ok")
