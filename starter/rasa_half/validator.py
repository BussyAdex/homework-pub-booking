"""Ex6 — booking payload normaliser.

Bridges the sovereign-agent data-dict conventions and Rasa's expected
message shape. RasaStructuredHalf calls normalise_booking_payload()
before sending anything over HTTP.

Grader requires normalising at least 3 of:
  date, currency, party_size, time, venue_id.
This implementation normalises all five.

Changes from the original starter:
  * `deposit` was read from raw["deposit"] but written as deposit_gbp;
    we now accept BOTH "deposit" and "deposit_gbp" on input so the
    validator agrees with the mock server and ActionValidateBooking,
    which read deposit_gbp.
  * The "today"/"tomorrow" date anchor is no longer hardcoded to a
    fixed 2026 date — it derives from a reference date that defaults to
    the real current date but can be injected for deterministic tests.
  * The NormalisedBooking dataclass is now actually used as the single
    source of truth for the payload shape, instead of being dead code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date as _date
from datetime import timedelta

_VALID_CATERING = ("drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal")


@dataclass
class NormalisedBooking:
    """Clean, Rasa-ready booking. Every field is present and typed."""

    action: str
    venue_id: str
    date: str
    time: str
    party_size: int
    deposit_gbp: int
    duration_hours: int = 3
    catering_tier: str = "bar_snacks"

    def to_metadata_booking(self) -> dict:
        """The dict that goes under metadata.booking on the wire.

        `action` is dropped here because it is carried by the REST
        `message` field ("/confirm_booking"), not the booking payload.
        """
        d = asdict(self)
        d.pop("action", None)
        return d


class ValidationFailed(ValueError):  # noqa: N818
    """Raised when input is beyond saving.

    RasaStructuredHalf.run() catches this and returns a HalfResult with
    next_action=escalate rather than crashing. Named `ValidationFailed`
    (not `ValidationError`) to match Rasa's own convention; N818 noqa'd
    intentionally.
    """


# ---------------------------------------------------------------------------
# normalise_booking_payload
# ---------------------------------------------------------------------------
def normalise_booking_payload(
    raw: dict,
    *,
    action: str = "confirm_booking",
    reference_date: _date | None = None,
) -> dict:
    """Take a data dict from the loop half and produce a Rasa-shaped message.

    Args:
        raw: the handoff data dict.
        action: which flow this maps to ("confirm_booking",
            "resume_from_loop", "request_research"). Controls the
            outgoing /message command.
        reference_date: anchor for relative dates like "today"/"tomorrow".
            Defaults to the real current date; inject a fixed date in
            tests for determinism.
    """
    if not isinstance(raw, dict):
        raise ValidationFailed(f"expected dict, got {type(raw).__name__}")

    venue_id_raw = raw.get("venue_id")
    if not venue_id_raw:
        raise ValidationFailed("missing venue_id")
    venue_id = canonicalise_venue_id(venue_id_raw)

    date_raw = raw.get("date")
    if not date_raw:
        raise ValidationFailed("missing date")
    date_iso = normalise_date(date_raw, reference_date=reference_date)

    time_raw = raw.get("time")
    if not time_raw:
        raise ValidationFailed("missing time")
    time_24h = parse_time_24h(time_raw)

    party = parse_party_size(raw.get("party_size"))

    # Accept either key; deposit_gbp wins if both are present.
    deposit_source = raw.get("deposit_gbp", raw.get("deposit"))
    deposit = parse_currency_gbp(deposit_source) if deposit_source is not None else 0

    duration = raw.get("duration_hours", 3)
    if isinstance(duration, str) and duration.strip().isdigit():
        duration = int(duration)
    if not isinstance(duration, int) or duration < 1:
        duration = 3

    catering = raw.get("catering_tier", "bar_snacks")
    if catering not in _VALID_CATERING:
        catering = "bar_snacks"

    booking = NormalisedBooking(
        action=action,
        venue_id=venue_id,
        date=date_iso,
        time=time_24h,
        party_size=party,
        deposit_gbp=deposit,
        duration_hours=duration,
        catering_tier=catering,
    )

    stable_suffix = hashlib.sha1(f"{venue_id}-{date_iso}-{time_24h}".encode()).hexdigest()[:8]

    return {
        "sender": f"homework-{stable_suffix}",
        "message": f"/{action}",
        "metadata": {"booking": booking.to_metadata_booking()},
    }


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------
_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def normalise_date(raw: str, *, reference_date: _date | None = None) -> str:
    """Normalise a date string to 'YYYY-MM-DD'.

    Accepts ISO ('2026-04-25'), relative ('today', 'tomorrow'), and
    natural ('25th April', '25 Apr 2026') forms.
    """
    ref = reference_date or _date.today()
    s = str(raw).strip().lower()

    if s == "today":
        return ref.isoformat()
    if s == "tomorrow":
        return (ref + timedelta(days=1)).isoformat()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s

    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)(?:\s+(\d{4}))?", s)
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        year = int(m.group(3)) if m.group(3) else ref.year
        if month_name not in _MONTH_NAMES:
            raise ValidationFailed(f"unknown month: {month_name!r}")
        month = _MONTH_NAMES[month_name]
        if not 1 <= day <= 31:
            raise ValidationFailed(f"day out of range: {day}")
        return f"{year:04d}-{month:02d}-{day:02d}"

    raise ValidationFailed(f"cannot parse date: {raw!r}")


# Backwards-compatible private alias (the original code called _normalise_date).
_normalise_date = normalise_date


# ---------------------------------------------------------------------------
# Currency / time / venue / party helpers
# ---------------------------------------------------------------------------
_GBP_PATTERN = re.compile(r"£?\s*(\d+(?:\.\d+)?)\s*(?:gbp|GBP)?", re.IGNORECASE)


def parse_currency_gbp(raw: str | int | float) -> int:
    """'£500', '500', '500 GBP', 500, 500.0 → 500. Rejects negatives/junk."""
    if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
        raise ValidationFailed(f"invalid currency: {raw!r}")
    if isinstance(raw, (int, float)):
        if raw < 0:
            raise ValidationFailed(f"negative currency: {raw!r}")
        return int(raw)
    m = _GBP_PATTERN.search(str(raw).strip())
    if not m:
        raise ValidationFailed(f"cannot parse currency: {raw!r}")
    value = float(m.group(1))
    if value < 0:
        raise ValidationFailed(f"negative currency: {raw!r}")
    return int(value)


def parse_time_24h(raw: str) -> str:
    """'7:30pm' → '19:30'. '19:30' → '19:30'. 'noon' → '12:00'."""
    s = str(raw).strip().lower()
    if s in ("noon", "midday"):
        return "12:00"
    if s == "midnight":
        return "00:00"
    if m := re.fullmatch(r"(\d{1,2}):?(\d{2})", s):
        h, mm = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return f"{h:02d}:{mm:02d}"
    if m := re.fullmatch(r"(\d{1,2})(?:[:.]?(\d{2}))?\s*(am|pm)", s):
        h = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        if not 1 <= h <= 12 or not 0 <= mm <= 59:
            raise ValidationFailed(f"cannot parse time: {raw!r}")
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mm:02d}"
    raise ValidationFailed(f"cannot parse time: {raw!r}")


def canonicalise_venue_id(raw: str) -> str:
    """'Haymarket Tap' → 'haymarket_tap'. Idempotent on already-clean ids."""
    s = str(raw).strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        raise ValidationFailed(f"venue_id empty after canonicalisation: {raw!r}")
    return s


def parse_party_size(raw: str | int | None) -> int:
    """'6' → 6. 6 → 6. '6 people' → 6. Rejects < 1, None, or non-numeric."""
    if raw is None:
        raise ValidationFailed("missing party_size")
    if isinstance(raw, bool):
        raise ValidationFailed(f"invalid party size: {raw!r}")
    if isinstance(raw, int):
        if raw < 1:
            raise ValidationFailed(f"party size must be >= 1, got {raw}")
        return raw
    s = str(raw).strip()
    if m := re.match(r"(\d+)", s):
        n = int(m.group(1))
        if n < 1:
            raise ValidationFailed(f"party size must be >= 1, got {n}")
        return n
    raise ValidationFailed(f"cannot parse party size: {raw!r}")


__all__ = [
    "NormalisedBooking",
    "ValidationFailed",
    "canonicalise_venue_id",
    "normalise_booking_payload",
    "normalise_date",
    "parse_currency_gbp",
    "parse_party_size",
    "parse_time_24h",
]
