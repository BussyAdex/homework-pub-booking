"""Rasa custom actions — reference implementation.

ActionValidateBooking reads booking data from the UserUttered message's
`metadata.booking` dict (how RasaStructuredHalf POSTs data) and validates
it against the homework's business rules.

Why metadata, not slots?
  The caller POSTs:
    {"sender": ..., "message": "/confirm_booking",
     "metadata": {"booking": {"venue_id": ..., "party_size": 6, ...}}}
  CALM turns "/confirm_booking" into StartFlow(confirm_booking) but does
  NOT read metadata into slots — this action does that explicitly, then
  sets the slots so downstream responses can reference them.

Slot naming: the domain declares booking_date / booking_time (not
date / time) to keep the collect-step / action_ask_* mapping clean in
resume_from_loop. This action writes those names.
"""

from __future__ import annotations

import hashlib
from typing import Any

from rasa_sdk import Action, Tracker
from rasa_sdk.events import SlotSet
from rasa_sdk.executor import CollectingDispatcher

MAX_PARTY_SIZE_FOR_AUTO_BOOKING = 8
MAX_DEPOSIT_FOR_AUTO_BOOKING_GBP = 300


def _read_booking(tracker: Tracker) -> dict[str, Any]:
    """Extract the booking dict from metadata (primary) or slots (fallback)."""
    latest = tracker.latest_message or {}
    meta = latest.get("metadata") or {}
    from_meta = meta.get("booking") if isinstance(meta, dict) else None
    if isinstance(from_meta, dict) and from_meta:
        return from_meta

    # Fallback: assemble from slots (used on resume_from_loop re-validation,
    # after collect steps have populated them).
    return {
        "venue_id": tracker.get_slot("venue_id"),
        "date": tracker.get_slot("booking_date"),
        "time": tracker.get_slot("booking_time"),
        "party_size": tracker.get_slot("party_size"),
        "deposit_gbp": tracker.get_slot("deposit_gbp"),
    }


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ActionValidateBooking(Action):
    """Validate the proposed booking against policy rules.

    Rules:
      * party_size > 8         → reject ("party_too_large")
      * deposit_gbp > 300      → reject ("deposit_too_high")
      * missing required field → reject ("missing_<field>")
      * otherwise              → success, set booking_reference
    """

    def name(self) -> str:
        return "action_validate_booking"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict[str, Any],
    ) -> list[dict[str, Any]]:
        booking = _read_booking(tracker)

        venue_id = booking.get("venue_id")
        date = booking.get("date")
        time_slot = booking.get("time")
        party_size = booking.get("party_size")
        # Accept either key from metadata.
        deposit_gbp = booking.get("deposit_gbp", booking.get("deposit", 0))

        # Populate slots from metadata first so responses can reference them.
        slot_events: list[dict[str, Any]] = [
            SlotSet("venue_id", str(venue_id) if venue_id is not None else None),
            SlotSet("booking_date", str(date) if date is not None else None),
            SlotSet("booking_time", str(time_slot) if time_slot is not None else None),
            SlotSet("party_size", _to_float(party_size)),
            SlotSet("deposit_gbp", _to_float(deposit_gbp) or 0.0),
        ]

        # Required-field check.
        for field_name, value in [
            ("venue_id", venue_id),
            ("date", date),
            ("time", time_slot),
            ("party_size", party_size),
        ]:
            if value is None or value == "":
                return slot_events + [SlotSet("validation_error", f"missing_{field_name}")]

        try:
            party_int = int(float(party_size))
        except (TypeError, ValueError):
            return slot_events + [SlotSet("validation_error", "invalid_party_size")]

        try:
            deposit_int = int(float(deposit_gbp)) if deposit_gbp is not None else 0
        except (TypeError, ValueError):
            return slot_events + [SlotSet("validation_error", "invalid_deposit")]

        if party_int < 1:
            return slot_events + [SlotSet("validation_error", "invalid_party_size")]
        if party_int > MAX_PARTY_SIZE_FOR_AUTO_BOOKING:
            return slot_events + [SlotSet("validation_error", "party_too_large")]
        if deposit_int > MAX_DEPOSIT_FOR_AUTO_BOOKING_GBP:
            return slot_events + [SlotSet("validation_error", "deposit_too_high")]

        ref = (
            "BK-"
            + hashlib.sha1(f"{venue_id}|{date}|{time_slot}|{party_int}".encode())
            .hexdigest()[:8]
            .upper()
        )

        return slot_events + [
            SlotSet("validation_error", None),
            SlotSet("booking_reference", ref),
        ]


class ActionRequestResearch(Action):
    """Emit a research-request signal back to the caller.

    The reverse-handoff itself (re-invoking the loop half) is the Python
    HandoffBridge's job. This action only records the reason and emits a
    custom payload that RasaStructuredHalf detects to set
    next_action="research".
    """

    def name(self) -> str:
        return "action_request_research"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: dict[str, Any],
    ) -> list[dict[str, Any]]:
        meta = (tracker.latest_message or {}).get("metadata") or {}
        reason = "exceeds_cap"
        if isinstance(meta, dict):
            reason = meta.get("research_reason", reason)

        dispatcher.utter_message(json_message={"action": "request_research", "reason": reason})
        return [SlotSet("validation_error", None)]


# ─────────────────────────────────────────────────────────────────────
# action_ask_* — only fire in resume_from_loop when the loop half handed
# off an incomplete booking. Custom actions (not utter_ask_*) so the
# question text stays in one place if you later make it dynamic.
# ─────────────────────────────────────────────────────────────────────
class ActionAskVenueId(Action):
    def name(self) -> str:
        return "action_ask_venue_id"

    def run(self, dispatcher, tracker, domain):  # type: ignore[no-untyped-def]
        dispatcher.utter_message(text="Which venue is this booking for?")
        return []


class ActionAskBookingDate(Action):
    def name(self) -> str:
        return "action_ask_booking_date"

    def run(self, dispatcher, tracker, domain):  # type: ignore[no-untyped-def]
        dispatcher.utter_message(text="What date should I book? (YYYY-MM-DD)")
        return []


class ActionAskBookingTime(Action):
    def name(self) -> str:
        return "action_ask_booking_time"

    def run(self, dispatcher, tracker, domain):  # type: ignore[no-untyped-def]
        dispatcher.utter_message(text="What time? (e.g. 19:30)")
        return []


class ActionAskPartySize(Action):
    def name(self) -> str:
        return "action_ask_party_size"

    def run(self, dispatcher, tracker, domain):  # type: ignore[no-untyped-def]
        dispatcher.utter_message(text="How many people are in the party?")
        return []
