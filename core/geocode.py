"""Reverse-geocode GPS coordinates into a human-readable address.

Uses OpenStreetMap's free Nominatim service — no API key, no billing
account required (Google's Geocoding API needs a billing account attached
even within its free tier, which this project intentionally avoids).
Address formatting is close to, but not identical to, Google Maps' style.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim's usage policy requires a descriptive User-Agent identifying the
# calling app — anonymous/browser-like UAs get rate-limited or blocked.
_HEADERS = {"User-Agent": "MentahanPOV-pipeline/1.0 (personal content pipeline)"}


# Upper bound for the on-video label; longer names fall back to the area.
LABEL_MAX_CHARS = 34

_ROAD_PREFIXES = (("Jalan ", "Jl. "), ("Gang ", "Gg. "))
_CITY_PREFIXES = ("Kota ", "Kabupaten ", "Kecamatan ")


def short_label(address: dict) -> str | None:
    """A sign-sized place name from Nominatim's structured address.

    "Jl. Basuki Rahmat · Malang" for a street, "Klojen · Malang" when the
    street name would be too long, just the city when that's all there is.
    Built locally (no Gemini), so the posting copy can be rendered before
    the caption exists.
    """
    spot = address.get("road") or address.get("pedestrian")
    for long, short in _ROAD_PREFIXES:
        if spot and spot.startswith(long):
            spot = short + spot[len(long) :]
    area = (
        address.get("neighbourhood")
        or address.get("suburb")
        or address.get("village")
        or address.get("quarter")
    )
    city = (
        address.get("city")
        or address.get("town")
        or address.get("municipality")
        or address.get("county")
    )
    for prefix in _CITY_PREFIXES:
        if city and city.startswith(prefix):
            city = city[len(prefix) :]
    for first in (spot, area):
        if first and city and first != city:
            label = f"{first} · {city}"
            if len(label) <= LABEL_MAX_CHARS:
                return label
    label = city or area or spot
    return label if label and len(label) <= LABEL_MAX_CHARS else None


@lru_cache(maxsize=32)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
def reverse_geocode_details(lat: float, lon: float) -> tuple[str | None, str | None]:
    """(full address, short on-video label) for (lat, lon); Nones if unknown.

    Cached so the caption step and the label step share one Nominatim call.
    """
    resp = requests.get(
        NOMINATIM_URL,
        params={
            "format": "jsonv2",
            "lat": lat,
            "lon": lon,
            "zoom": 18,
            "addressdetails": 1,
        },
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    address = data.get("display_name")
    if not address:
        log.warning(
            "[geocode] no result for %s,%s: %s", lat, lon, data.get("error")
        )
        return None, None
    return address, short_label(data.get("address") or {})


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return a formatted address for (lat, lon), or None if unavailable."""
    return reverse_geocode_details(lat, lon)[0]
