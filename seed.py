"""Create the starting fleet in Redis. Run this before the simulator.

Seeding is destructive on purpose: it clears every key this project owns first.
Seeding on top of an old fleet would leave riders behind in cell sets with no
one tracking them, which is exactly the index drift state.py raises on.
"""

import random

import h3

import config
import state
from travel import km_to_degrees

RIDERS_PER_CELL_WARNING = 1


def random_point_in_city(rng: random.Random) -> tuple[float, float]:
    """A uniformly random (lat, lng) inside the city box.

    Takes an rng rather than using the `random` module directly, so the whole
    run descends from one seed and nothing else in the process can perturb it.
    The global `random` is shared state: any library that calls random.random()
    would silently change our results and break reproducibility.

    sim.py will import this for restaurant and customer placement too.
    """
    centre_lat, centre_lng = config.CITY_CENTRE
    half_lat, half_lng = km_to_degrees(config.CITY_SIZE_KM / 2, centre_lat)

    lat = rng.uniform(centre_lat - half_lat, centre_lat + half_lat)
    lng = rng.uniform(centre_lng - half_lng, centre_lng + half_lng)
    return (lat, lng)


def seed_fleet(fleet_size: int, rng: random.Random) -> list[str]:
    """Create `fleet_size` free riders at random positions. Returns their ids."""
    rider_ids = []
    for index in range(fleet_size):
        rider_id = f"r{index}"
        lat, lng = random_point_in_city(rng)
        # One pipeline per rider, so this is fleet_size round trips rather than
        # one. Deliberate: seeding runs once, at roughly 0.9 ms a trip, so 50
        # riders cost about 45 ms. Optimising a one-off path is not worth a bulk
        # function nothing else would use. If the fleet reaches thousands, add
        # one to state.py then.
        state.create_rider(rider_id, lat, lng)
        rider_ids.append(rider_id)
    return rider_ids


def describe_fleet(rider_ids: list[str]) -> dict:
    """Read the fleet back and report what the index actually looks like.

    This reads from Redis rather than reporting what we just wrote, because the
    useful question is not "did the loop run" but "is the fleet findable".
    """
    riders = state.read_riders(rider_ids)

    riders_by_cell: dict[str, list[str]] = {}
    for rider in riders:
        cell = state.cell_of(rider.lat, rider.lng)
        riders_by_cell.setdefault(cell, []).append(rider.rider_id)

    centre_cell = state.cell_of(*config.CITY_CENTRE)
    centre_ring = h3.grid_disk(centre_cell, 2)

    return {
        "riders": riders,
        "riders_by_cell": riders_by_cell,
        "indexed_ids": state.riders_in_cells(riders_by_cell.keys()),
        "centre_ring_candidates": state.riders_in_cells(centre_ring),
    }


if __name__ == "__main__":
    rng = random.Random(config.RANDOM_SEED)

    cleared = state.clear_all()
    print(f"cleared {cleared} existing keys")

    rider_ids = seed_fleet(config.FLEET_SIZE, rng)
    print(f"created {len(rider_ids)} riders")

    report = describe_fleet(rider_ids)
    riders = report["riders"]
    riders_by_cell = report["riders_by_cell"]

    # Every rider exists, is free, and is carrying nothing.
    assert len(riders) == config.FLEET_SIZE
    assert all(rider.state == state.FREE for rider in riders)
    assert all(rider.target is None for rider in riders)
    assert all(rider.order_id is None for rider in riders)

    # Every rider is reachable through the cell index, not just through its own
    # key. A rider that exists but is not indexed is invisible to dispatch
    # forever, and nothing else in the system would ever notice.
    assert report["indexed_ids"] == set(rider_ids), (
        "a rider exists but is missing from its cell set: the index has drifted"
    )

    per_cell = sorted(len(ids) for ids in riders_by_cell.values())
    candidates = len(report["centre_ring_candidates"])

    print(f"spread over {len(riders_by_cell)} cells")
    print(f"riders per occupied cell: min {per_cell[0]}, max {per_cell[-1]}")
    print(f"a k=2 ring at the city centre sees {candidates} candidates")

    # The number that decides whether k=2 is sized correctly for this fleet.
    if candidates == 0:
        print("  WARNING: no candidates in the centre ring. Dispatch would find")
        print("  nobody. Raise FLEET_SIZE, raise K, or shrink CITY_SIZE_KM.")
    elif candidates < 5:
        print("  thin: ranking has little to choose between. Consider a bigger fleet.")

    print("ok")
