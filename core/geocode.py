"""Reverse-geocode GPS coordinates into a human-readable address.

Uses OpenStreetMap's free Nominatim service — no API key, no billing
account required (Google's Geocoding API needs a billing account attached
even within its free tier, which this project intentionally avoids).
Address formatting is close to, but not identical to, Google Maps' style.
"""

from __future__ import annotations

import logging

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim's usage policy requires a descriptive User-Agent identifying the
# calling app — anonymous/browser-like UAs get rate-limited or blocked.
_HEADERS = {"User-Agent": "MentahanPOV-pipeline/1.0 (personal content pipeline)"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return a formatted address for (lat, lon), or None if unavailable."""
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
        return None
    return address
