"""Travel time + straight-line geometry. HaversineProvider now, OSRM later."""

from math import asin, cos, pi, radians, sin, sqrt
from typing import Protocol

Point = tuple[float, float]  # (lat, lng)
EARTH_KM = 6371.0088


class TravelTimeProvider(Protocol):
    def travel_times(self, origin: Point, destinations: list[Point]) -> list[float]:
        """Seconds from origin to each destination, same order. One origin, N
        destinations: that is the shape dispatch asks for, and the shape OSRM's
        /table endpoint answers in one request."""


def haversine_km(a: Point, b: Point) -> float:
    """Great-circle distance in km between two (lat, lng) points.

    Trig functions take radians, so every angle is converted first. Feeding
    degrees in is silent and catastrophic: sin(90) is 0.894, not 1."""
    lat1, lng1 = radians(a[0]), radians(a[1])
    lat2, lng2 = radians(b[0]), radians(b[1])
    delta_lat = lat2 - lat1
    delta_lng = lng2 - lng1

    # Latitude degrees are the same length everywhere, so no scaling.
    north_south = sin(delta_lat / 2) ** 2
    # Longitude degrees shrink toward the poles. cos(lat) is that correction:
    # 1 degree of longitude is 111 km at the equator and 0 km at the pole.
    east_west = cos(lat1) * cos(lat2) * sin(delta_lng / 2) ** 2

    # Angle between the two points as seen from the centre of the Earth.
    central_angle = 2 * asin(sqrt(north_south + east_west))
    return EARTH_KM * central_angle


class HaversineProvider:
    def __init__(self, speed_kmh: float = 25.0):
        self.speed_kmh = speed_kmh  # calibration knob: real riders are not 25

    def travel_times(self, origin: Point, destinations: list[Point]) -> list[float]:
        return [haversine_km(origin, d) / self.speed_kmh * 3600 for d in destinations]


def km_to_degrees(km: float, at_lat: float) -> tuple[float, float]:
    """How many degrees of latitude and of longitude span `km` at this latitude.

    They are not the same number. Latitude degrees are the same length
    everywhere; longitude degrees shrink toward the poles by cos(lat) - the very
    correction haversine_km applies in its east-west term. At Bangalore's 13N
    that is about a 2.5% difference, so a square box in km is not a square box
    in degrees.
    """
    km_per_degree_lat = EARTH_KM * pi / 180
    km_per_degree_lng = km_per_degree_lat * cos(radians(at_lat))
    return (km / km_per_degree_lat, km / km_per_degree_lng)


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

    # At the equator a degree of longitude and of latitude are the same length.
    equator_lat, equator_lng = km_to_degrees(111.195, 0.0)
    assert abs(equator_lat - 1.0) < 0.001
    assert abs(equator_lng - 1.0) < 0.001

    # Away from it, the same distance spans MORE longitude than latitude.
    blr_lat, blr_lng = km_to_degrees(8.0, 12.9716)
    assert blr_lng > blr_lat
    assert abs(blr_lat - 0.0719) < 0.0005
    assert abs(blr_lng - 0.0738) < 0.0005

    assert step_toward((0, 0), (0, 1), 1000) == (0, 1)  # overshoot clamps
    assert step_toward((0, 0), (0, 0), 5) == (0, 0)  # zero-length no divide-by-0
    half = step_toward((0, 0), (0, 1), 111.19 / 2)
    assert abs(half[1] - 0.5) < 0.001
    print("ok")
