"""Shared geospatial utilities (Haversine distance & bounding-box pre-filter)."""

import math

EARTH_RADIUS_KM: float = 6371.0
_KM_PER_DEGREE_LAT: float = 111.32


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 coordinates in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return EARTH_RADIUS_KM * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Return ``(min_lat, max_lat, min_lon, max_lon)`` enclosing ``radius_km`` around a point.

    Used for fast SQL range pre-filtering before exact Haversine distance checks.
    """
    if radius_km < 0:
        raise ValueError("radius_km must be non-negative.")

    lat_delta = radius_km / _KM_PER_DEGREE_LAT
    min_lat = max(-90.0, lat - lat_delta)
    max_lat = min(90.0, lat + lat_delta)

    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        min_lon, max_lon = -180.0, 180.0
    else:
        lon_delta = radius_km / (_KM_PER_DEGREE_LAT * abs(cos_lat))
        min_lon = max(-180.0, lon - lon_delta)
        max_lon = min(180.0, lon + lon_delta)

    return (min_lat, max_lat, min_lon, max_lon)
