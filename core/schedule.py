"""Pure time logic for the bot's 📅 Jadwalkan button.

No Telegram imports here, so it's unit-testable on its own (see
tests/test_schedule.py). All inputs/outputs are timezone-aware datetimes;
callers pass "now" in the zone the user thinks in (WIB), and slot hours are
read as wall-clock hours in that same zone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

_HHMM = re.compile(r"^(?P<day>besok|lusa|hari ini)?\s*(?P<h>\d{1,2})[:.](?P<m>\d{2})$")
_RELATIVE = re.compile(r"^\+\s*(?P<n>\d{1,4})\s*(?P<unit>m|menit|j|jam|h)$")

_DAY_NAMES = ("Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min")
_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
    "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
)  # fmt: skip

# A slot closer than this is skipped: by the time you've tapped it, and the
# job has been registered, it would already be "now".
MIN_LEAD = timedelta(minutes=2)


def upcoming_slots(
    now: datetime, hours: Sequence[int], count: int = 2
) -> list[datetime]:
    """The next `count` wall-clock slots (e.g. 07:00 / 12:00 / 19:00) after now."""
    slots: list[datetime] = []
    for day_offset in range(0, 3):
        for hour in sorted(set(hours)):
            candidate = (now + timedelta(days=day_offset)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if candidate - now >= MIN_LEAD:
                slots.append(candidate)
    return slots[:count]


def parse_when(text: str, now: datetime) -> datetime | None:
    """Parse what a person types after tapping "✍️ Ketik jam".

    Accepted: `19:30`, `19.30` (today, or tomorrow if that already passed),
    `besok 07:00`, `lusa 18:00`, `hari ini 21:15`, and relative `+90m`,
    `+2j`, `+1 jam`. Returns None when it doesn't parse or isn't in the
    future (so a typo can't silently schedule "right now").
    """
    cleaned = text.strip().lower()
    result: datetime | None = None

    rel = _RELATIVE.match(cleaned)
    if rel:
        n = int(rel["n"])
        unit = rel["unit"]
        delta = timedelta(minutes=n) if unit in ("m", "menit") else timedelta(hours=n)
        result = now + delta
    else:
        m = _HHMM.match(cleaned)
        if m:
            hour, minute = int(m["h"]), int(m["m"])
            if hour > 23 or minute > 59:
                return None
            day = m["day"]
            base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day == "besok":
                result = base + timedelta(days=1)
            elif day == "lusa":
                result = base + timedelta(days=2)
            elif day == "hari ini":
                result = base
            else:
                lead = base - now
                if lead <= timedelta(0):
                    result = base + timedelta(days=1)  # "07:00" at 14:30 = tomorrow
                elif lead < MIN_LEAD:
                    return None  # "14:31" at 14:30 is too close to be meant
                else:
                    result = base

    if result is None or result - now < MIN_LEAD:
        return None
    return result.replace(second=0, microsecond=0)


def format_when(when: datetime, now: datetime | None = None) -> str:
    """'hari ini 19:00', 'besok 07:00', or 'Sen 12 Okt 19:00' further out."""
    clock = when.strftime("%H:%M")
    if now is not None:
        days = (when.date() - now.date()).days
        if days == 0:
            return f"hari ini {clock}"
        if days == 1:
            return f"besok {clock}"
    return (
        f"{_DAY_NAMES[when.weekday()]} {when.day} "
        f"{_MONTH_NAMES[when.month - 1]} {clock}"
    )
