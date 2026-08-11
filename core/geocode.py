"""Reverse-geocode GPS coordinates into a human-readable address.

Uses the Google Geocoding API so the address format matches what Google
Maps itself would show (street, kelurahan/kecamatan, city, postcode) —
the same style already used in MentahanPOV captions.
"""

from __future__ import annotations

import logging

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from config import config

log = logging.getLogger(__name__)

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=15))
def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return a formatted address for (lat, lon), or None if unavailable."""
    if not config.google_maps_api_key:
        log.warning("[geocode] GOOGLE_MAPS_API_KEY empty; skipping reverse geocode")
        return None

    resp = requests.get(
        GEOCODE_URL,
        params={"latlng": f"{lat},{lon}", "key": config.google_maps_api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        log.warning(
            "[geocode] no result for %s,%s: %s", lat, lon, data.get("status")
        )
        return None

    return data["results"][0]["formatted_address"]
