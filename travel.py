"""Travel time + straight-line geometry. HaversineProvider now, OSRM later."""

from math import radians, sin, cos, asin, sqrt
from typing import Protocol

Point = tuple[float, float]  # (lat, lng)
EARTH_KM = 6371.0088


class TravelTimeProvider(Protocol):
    def travel_times(self, origin: Point, destinations: list[Point]) -> list[float]:
        """Seconds from origin to each destination, same order. One origin, N
        destinations: that is the shape dispatch asks for, and the shape OSRM's
        /table endpoint answers in one request."""


def haversine_km(a: Point, b: Point) -> float:
    lat1, lng1 = radians(a[0]), radians(a[1])
    lat2, lng2 = radians(b[0]), radians(b[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_KM * asin(sqrt(h))


class HaversineProvider:
    def __init__(self, speed_kmh: float = 25.0):
        self.speed_kmh = speed_kmh  # calibration knob: real riders are not 25

    def travel_times(self, origin: Point, destinations: list[Point]) -> list[float]:
        return [haversine_km(origin, d) / self.speed_kmh * 3600 for d in destinations]


def step_toward(a: Point, b: Point, km: float) -> Point:
    """Move from a toward b by km, clamped at b. Linear lat/lng interpolation,
    which is wrong near the poles and fine at city scale."""
    total = haversine_km(a, b)
    if total == 0 or total <= km:
        return b
    f = km / total
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


if __name__ == "__main__":
    # 1 degree of longitude at the equator is ~111.19 km
    assert abs(haversine_km((0, 0), (0, 1)) - 111.19) < 0.05
    assert haversine_km((12.97, 77.59), (12.97, 77.59)) == 0

    p = HaversineProvider()
    (t,) = p.travel_times((0, 0), [(0, 1)])
    assert abs(t - 111.19 / 25 * 3600) < 10  # ~4.4 hours at 25 km/h
    assert p.travel_times((0, 0), []) == []
    assert len(p.travel_times((0, 0), [(0, 1), (1, 0), (0, 0)])) == 3

    assert step_toward((0, 0), (0, 1), 1000) == (0, 1)  # overshoot clamps
    assert step_toward((0, 0), (0, 0), 5) == (0, 0)  # zero-length no divide-by-0
    half = step_toward((0, 0), (0, 1), 111.19 / 2)
    assert abs(half[1] - 0.5) < 0.001
    print("ok")
