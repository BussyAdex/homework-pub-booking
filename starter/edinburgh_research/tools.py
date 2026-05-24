"""Ex5 tools. Four tools the agent uses to research an Edinburgh booking.

Each tool:
  1. Reads its fixture from sample_data/ (DO NOT modify the fixtures).
  2. Logs its arguments and output into _TOOL_CALL_LOG (see integrity.py).
  3. Returns a ToolResult with success=True/False, output=dict, summary=str.

The grader checks for:
  * Correct parallel_safe flags (reads True, generate_flyer False).
  * Every tool's results appear in _TOOL_CALL_LOG.
  * Tools fail gracefully on missing fixtures or bad inputs (ToolError,
    not RuntimeError).

real-mode notes
---------------
Under real-mode the agent drives these tools with an actual LLM, which can
get stuck calling the same read-only tool over and over ("spiralling"). To
keep that bounded without lying to the agent about what happened, each tool:

  * records its call into _TOOL_CALL_LOG *first*, then
  * checks how many times it has already been called this session, and
  * if that count crosses a threshold, returns the tool's *real* success and
    *real* output but rewrites the summary to tell the agent to stop and use
    the data it already has.

The crucial detail (learned the hard way) is that the spiral branch passes
through the genuine ``success`` and ``output`` rather than hardcoding
``success=False, output={}``. Hardcoding a failure even though the tool
produced usable data sends the agent the wrong signal and makes the spiral
worse, not better.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from sovereign_agent import ToolError
from sovereign_agent.session.directory import Session
from sovereign_agent.tools.registry import ToolRegistry, ToolResult, _RegisteredTool

from starter.edinburgh_research.integrity import _TOOL_CALL_LOG, record_tool_call

_SAMPLE_DATA = Path(__file__).parent / "sample_data"

# How many calls to a single read-only tool we tolerate before we start
# nudging the agent to stop. NOTE: the comparison is strictly ``> _SPIRAL_THRESHOLD``
# (i.e. the 4th call and beyond trip it), *not* ``>=``.
_SPIRAL_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Spiralling helpers
# ---------------------------------------------------------------------------
def _calls_to(tool_name: str) -> int:
    """Count how many times <tool_name> already appears in _TOOL_CALL_LOG.

    Defensive: the log may be missing, may not be iterable, and individual
    records may not expose ``tool_name`` the way we expect. Any surprise
    just yields a count of 0 so a logging quirk can never wedge a tool.
    """
    try:
        log = _TOOL_CALL_LOG or []
    except Exception:
        return 0

    count = 0
    for record in log:
        name = getattr(record, "tool_name", None)
        if name is None and isinstance(record, dict):
            name = record.get("tool_name")
        if name == tool_name:
            count += 1
    return count


def _llm_spiralling_check(tool_name: str) -> bool:
    """True once <tool_name> has been called more than _SPIRAL_THRESHOLD times.

    Strictly greater-than: threshold 3 means this returns True on call #4+.
    """
    return _calls_to(tool_name) > _SPIRAL_THRESHOLD


def _record_output(record) -> dict:
    """Pull the ``output`` dict off a log record, defensively.

    Records may be objects with an ``.output`` attribute or plain dicts;
    anything that isn't a dict in the end becomes ``{}``.
    """
    out = getattr(record, "output", None)
    if out is None and isinstance(record, dict):
        out = record.get("output")
    return out if isinstance(out, dict) else {}


def _venue_spiral_message() -> str:
    """Scan _TOOL_CALL_LOG for venues already found by venue_search and
    build a human-useful reminder listing them, so the agent can reuse the
    data it already has instead of searching again.

    Written very defensively — a malformed record must never raise here,
    because this runs inside the tool's return path.
    """
    found: list[str] = []
    seen: set[str] = set()

    try:
        log = _TOOL_CALL_LOG or []
    except Exception:
        log = []

    for record in log:
        name = getattr(record, "tool_name", None)
        if name is None and isinstance(record, dict):
            name = record.get("tool_name")
        if name != "venue_search":
            continue

        output = _record_output(record)
        # Only trust records that actually carried results.
        if output.get("count", -1) > 0 and output.get("results"):
            for venue in output["results"]:
                if not isinstance(venue, dict):
                    continue
                vid = venue.get("id") or venue.get("name")
                label = venue.get("name") or venue.get("id")
                if vid and vid not in seen:
                    seen.add(vid)
                    # include the id in parens when we also have a name
                    if label and venue.get("id") and label != venue.get("id"):
                        found.append(f"{label} ({venue['id']})")
                    else:
                        found.append(str(label))

    if not found:
        return "No venues have been found yet in previous calls"
    return "Venues already found in previous calls: " + ", ".join(found)


# The full required sequence, in order. Used to tell the agent what is left.
_REQUIRED_SEQUENCE = ("venue_search", "get_weather", "calculate_cost", "generate_flyer")


def _next_step_hint() -> str:
    """Build a forward-pointing reminder from _TOOL_CALL_LOG state.

    Reports which required tools have not yet run and, crucially, that the
    flyer is not yet written and complete_task must not be called. This is
    appended to every read tool's *normal* summary (not just the spiral
    branch) so the signal is present on every turn the agent sees.

    Defensive: never raises — it runs inside a tool return path.
    """
    try:
        log = _TOOL_CALL_LOG or []
    except Exception:
        log = []

    ran: set[str] = set()
    for record in log:
        name = getattr(record, "tool_name", None)
        if name is None and isinstance(record, dict):
            name = record.get("tool_name")
        if name in _REQUIRED_SEQUENCE:
            ran.add(name)

    remaining = [t for t in _REQUIRED_SEQUENCE if t not in ran]

    if "generate_flyer" in ran:
        # Terminal tool already ran; completing is now legitimate.
        return "All required tools have run; you may now call complete_task."

    if remaining:
        return (
            "This is ONE continuous job. Remaining required tools (in order): "
            f"{', '.join(remaining)}. workspace/flyer.html does NOT exist yet. "
            "Do NOT call complete_task and do NOT hand off until generate_flyer "
            "has run."
        )
    # Everything except generate_flyer is done.
    return (
        "Next, call generate_flyer to write workspace/flyer.html. Do NOT call "
        "complete_task until it has run."
    )


# ---------------------------------------------------------------------------
# 1 — venue_search
# ---------------------------------------------------------------------------
def venue_search(near: str, party_size: int, budget_max_gbp: int = 1000) -> ToolResult:
    """Search for Edinburgh venues near <near> that can seat the party.

    Reads sample_data/venues.json. Filters by:
      * open_now == True
      * area contains <near> (case-insensitive substring match)
      * seats_available_evening >= party_size
      * hire_fee_gbp + min_spend_gbp <= budget_max_gbp

    Returns a ToolResult with:
      output: {"near": ..., "party_size": ..., "results": [<venue dicts>], "count": int}
      summary: "venue_search(<near>, party=<N>): <count> result(s)"

    MUST call record_tool_call(...) before returning so the integrity
    check can see what data was produced.
    """
    # 1a: load venues.json. Raise ToolError(SA_TOOL_DEPENDENCY_MISSING)
    #          if the file is absent.
    venue_path = _SAMPLE_DATA / "venues.json"
    try:
        with venue_path.open("r", encoding="utf-8") as f:
            venues = json.load(f)
    except FileNotFoundError as e:
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"venues.json not found: {e}") from e

    normalized_near = (near or "").lower()

    # 1b: filter the venues according to the criteria in the docstring.
    #     Defensive: skip any record missing a field rather than KeyError-ing.
    results = []
    for v in venues:
        try:
            if v.get("open_now") is not True:
                continue
            if v.get("seats_available_evening", -1) < party_size:
                continue
            if v.get("hire_fee_gbp", 0) + v.get("min_spend_gbp", 0) > budget_max_gbp:
                continue
            haystacks = (
                str(v.get("area", "")).lower(),
                str(v.get("name", "")).lower(),
                str(v.get("address", "")).lower(),
            )
            if any(normalized_near in h for h in haystacks):
                results.append(v)
        except Exception:
            # A single malformed venue record should not sink the search.
            continue

    count = len(results)

    # 1c: build the output dict and summary string.
    output = {
        "near": near,
        "party_size": party_size,
        "results": results,
        "count": count,
    }
    summary = f"venue_search({near}, party={party_size}): {count} result(s)"

    # 1d: record the tool call BEFORE any spiralling check, so the current
    #     call is reflected in the log we are about to scan.
    record_tool_call(
        tool_name="venue_search",
        arguments={"near": near, "party_size": party_size, "budget_max_gbp": budget_max_gbp},
        output=output,
    )

    # 1e: spiralling guard. We still hand back the genuine success/output —
    #     we only rewrite the summary to redirect the agent.
    success = True
    if _llm_spiralling_check("venue_search"):
        useful_message = _venue_spiral_message()
        return ToolResult(
            success=success,
            output=output,
            summary=(
                f"{summary if count == 0 else ''} "
                "STOP calling venue_search. Use the results you already have "
                f"from previous calls. {useful_message}. "
                "Next, run calculate_cost on a chosen venue, then get_weather, "
                "then generate_flyer. Do NOT call complete_task until "
                "generate_flyer has run."
            ).strip(),
        )

    return ToolResult(
        success=success,
        output=output,
        summary=f"{summary} {_next_step_hint()}",
    )


# ---------------------------------------------------------------------------
# 2 — get_weather
# ---------------------------------------------------------------------------
def get_weather(city: str, date: str) -> ToolResult:
    """Look up the scripted weather for <city> on <date> (YYYY-MM-DD).

    Reads sample_data/weather.json. Returns:
      output: {"city": str, "date": str, "condition": str, "temperature_c": int, ...}
      summary: "get_weather(<city>, <date>): <condition>, <temp>C"

    If the city or date is not in the fixture, return success=False with
    a clear ToolError (SA_TOOL_INVALID_INPUT). Do NOT raise.

    MUST call record_tool_call(...) before returning.
    """
    weather_path = _SAMPLE_DATA / "weather.json"
    try:
        with weather_path.open("r", encoding="utf-8") as f:
            weather_data = json.load(f)
    except FileNotFoundError as e:
        # Missing fixture is a dependency problem, not bad user input.
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"weather.json not found: {e}") from e

    # Case/whitespace-insensitive city lookup. The fixture may key cities as
    # "edinburgh" while the live LLM passes "Edinburgh" — match those without
    # fabricating data: if the city genuinely isn't present we still fail.
    def _ci_get(mapping: dict, key: str):
        if not isinstance(mapping, dict):
            return None
        if key in mapping:  # exact hit first
            return mapping[key]
        norm = str(key).strip().lower()
        for k, v in mapping.items():
            if str(k).strip().lower() == norm:
                return v
        return None

    city_data = _ci_get(weather_data, city)
    if city_data is None:
        available = (
            ", ".join(sorted(map(str, weather_data))) if isinstance(weather_data, dict) else ""
        )
        return ToolResult(
            success=False,
            output={},
            summary=(
                f"SA_TOOL_INVALID_INPUT: city '{city}' not found. "
                f"Available cities: {available}. Retry get_weather with one of these."
            ),
        )

    day_data = _ci_get(city_data, date)
    if day_data is None:
        available_dates = (
            ", ".join(sorted(map(str, city_data))) if isinstance(city_data, dict) else ""
        )
        return ToolResult(
            success=False,
            output={},
            summary=(
                f"SA_TOOL_INVALID_INPUT: date '{date}' not found for city '{city}'. "
                f"Available dates: {available_dates}. Retry get_weather with one of these."
            ),
        )

    # Defensive read of the day record.
    condition = day_data.get("condition") if isinstance(day_data, dict) else None
    temperature_c = day_data.get("temperature_c") if isinstance(day_data, dict) else None

    output = {
        "city": city,
        "date": date,
        "condition": condition,
        "temperature_c": temperature_c,
    }
    summary = f"get_weather({city}, {date}): {condition}, {temperature_c}C"

    record_tool_call(
        tool_name="get_weather",
        arguments={"city": city, "date": date},
        output=output,
    )

    success = True
    if _llm_spiralling_check("get_weather"):
        return ToolResult(
            success=success,
            output=output,
            summary=(
                f"{summary} "
                "STOP calling get_weather. You already have the forecast above; "
                "reuse it. Next, run generate_flyer with the venue, cost and "
                "weather you already gathered. Do NOT call complete_task until "
                "generate_flyer has run."
            ),
        )

    return ToolResult(
        success=success,
        output=output,
        summary=f"{summary} {_next_step_hint()}",
    )


# ---------------------------------------------------------------------------
# 3 — calculate_cost
# ---------------------------------------------------------------------------
def calculate_cost(
    venue_id: str,
    party_size: int,
    duration_hours: int,
    catering_tier: str = "bar_snacks",
) -> ToolResult:
    """Compute the total cost for a booking.

    Formula:
      base_per_head = base_rates_gbp_per_head[catering_tier]
      venue_mult    = venue_modifiers[venue_id]
      subtotal      = base_per_head * venue_mult * party_size * max(1, duration_hours)
      service       = subtotal * service_charge_percent / 100
      total         = subtotal + service + <venue's hire_fee_gbp + min_spend_gbp>
      deposit_rule  = per deposit_policy thresholds

    Returns:
      output: {
        "venue_id": str,
        "party_size": int,
        "duration_hours": int,
        "catering_tier": str,
        "subtotal_gbp": int,
        "service_gbp": int,
        "total_gbp": int,
        "deposit_required_gbp": int,
      }
      summary: "calculate_cost(<venue>, <party>): total £<N>, deposit £<M>"

    MUST call record_tool_call(...) before returning.
    """
    catering_path = _SAMPLE_DATA / "catering.json"
    venue_path = _SAMPLE_DATA / "venues.json"

    try:
        with catering_path.open("r", encoding="utf-8") as f:
            catering_data = json.load(f)
        with venue_path.open("r", encoding="utf-8") as f:
            venue_data = json.load(f)
    except FileNotFoundError as e:
        raise ToolError("SA_TOOL_DEPENDENCY_MISSING", f"fixture not found: {e}") from e

    venue_id_data = next((v for v in venue_data if v.get("id") == venue_id), None)
    if venue_id_data is None:
        return ToolResult(
            success=False,
            output={},
            summary=f"SA_TOOL_INVALID_INPUT: venue '{venue_id}' not found",
        )

    # Defensive lookups against the catering fixture.
    base_rates = catering_data.get("base_rates_gbp_per_head", {})
    if catering_tier not in base_rates:
        return ToolResult(
            success=False,
            output={},
            summary=f"SA_TOOL_INVALID_INPUT: catering_tier '{catering_tier}' not found",
        )

    venue_modifiers = catering_data.get("venue_modifiers", {})
    if venue_id not in venue_modifiers:
        return ToolResult(
            success=False,
            output={},
            summary=f"SA_TOOL_INVALID_INPUT: no venue modifier for '{venue_id}'",
        )

    base_per_head = base_rates[catering_tier]
    venue_mult = venue_modifiers[venue_id]
    service_pct = catering_data.get("service_charge_percent", 0)

    subtotal = base_per_head * venue_mult * party_size * max(1, duration_hours)
    service = subtotal * (service_pct / 100)
    total = (
        subtotal
        + service
        + venue_id_data.get("hire_fee_gbp", 0)
        + venue_id_data.get("min_spend_gbp", 0)
    )

    if total < 300:
        deposit_required_gbp = 0
    elif total < 1000:
        deposit_required_gbp = total * 0.2
    else:
        deposit_required_gbp = total * 0.3

    total_rounded = round(total)
    deposit_rounded = round(deposit_required_gbp)

    output = {
        "venue_id": venue_id,
        "party_size": party_size,
        "duration_hours": duration_hours,
        "catering_tier": catering_tier,
        "subtotal_gbp": round(subtotal),
        "service_gbp": round(service),
        "total_gbp": total_rounded,
        "deposit_required_gbp": deposit_rounded,
    }
    summary = (
        f"calculate_cost({venue_id}, {party_size}): "
        f"total £{total_rounded}, deposit £{deposit_rounded}"
    )

    record_tool_call(
        tool_name="calculate_cost",
        arguments={
            "venue_id": venue_id,
            "party_size": party_size,
            "duration_hours": duration_hours,
            "catering_tier": catering_tier,
        },
        output=output,
    )

    success = True
    if _llm_spiralling_check("calculate_cost"):
        return ToolResult(
            success=success,
            output=output,
            summary=(
                f"{summary} "
                "STOP calling calculate_cost. You already have the totals above; "
                "reuse them. Next, run get_weather (if not done) then "
                "generate_flyer. Do NOT call complete_task until "
                "generate_flyer has run."
            ),
        )

    return ToolResult(
        success=success,
        output=output,
        summary=f"{summary} {_next_step_hint()}",
    )


# ---------------------------------------------------------------------------
#  4 — generate_flyer
# ---------------------------------------------------------------------------
def generate_flyer(session: Session, event_details: dict) -> ToolResult:
    """Produce an HTML flyer and write it to workspace/flyer.html.

    event_details is expected to contain at least:
      venue_name, venue_address, date, time, party_size, condition,
      temperature_c, total_gbp, deposit_required_gbp

    Write a self-contained HTML flyer (inline CSS, no external assets). Tag every key fact with data-testid="<n>" so the integrity check can parse it.

    Write a formatted HTML flyer with an H1 title, the event
    facts, a weather summary, and the cost breakdown.

    Returns:
      output: {"path": "workspace/flyer.html", "bytes_written": int}
      summary: "generate_flyer: wrote <path> (<N> chars)"

    MUST call record_tool_call(...) before returning — the integrity
    check compares the flyer's contents against earlier tool outputs.

    IMPORTANT: this tool MUST be registered with parallel_safe=False
    because it writes a file.
    """
    if not isinstance(event_details, dict):
        return ToolResult(
            success=False,
            output={},
            summary="generate_flyer: event_details must be a dict",
            error=ToolError("SA_TOOL_INVALID_INPUT", "event_details must be a dict"),
        )

    required = (
        "venue_name",
        "venue_address",
        "date",
        "time",
        "party_size",
        "condition",
        "temperature_c",
        "total_gbp",
        "deposit_required_gbp",
    )
    missing = [k for k in required if k not in event_details]
    if missing:
        return ToolResult(
            success=False,
            output={},
            summary=f"generate_flyer: missing fields {missing}",
            error=ToolError("SA_TOOL_INVALID_INPUT", f"missing fields: {missing}"),
        )

    venue_name = event_details["venue_name"]
    venue_address = event_details["venue_address"]
    date = event_details["date"]
    time = event_details["time"]
    party_size = event_details["party_size"]
    condition = event_details["condition"]
    temperature_c = event_details["temperature_c"]
    total_gbp = event_details["total_gbp"]
    deposit_gbp = event_details["deposit_required_gbp"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Event Flyer — {venue_name}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 640px; margin: 2rem auto; padding: 1rem; color: #222; }}
  h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
  h2 {{ font-size: 1.2rem; margin-top: 1.5rem; color: #555; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.4rem 1rem; }}
  dt {{ font-weight: 600; color: #666; }}
  dd {{ margin: 0; }}
  .cost {{ background: #f5f5f5; padding: 1rem; border-radius: 6px; margin-top: 1rem; }}
</style>
</head>
<body>
  <h1>You're invited!</h1>
  <p>Join us at <strong data-testid="venue_name">{venue_name}</strong> for an evening to remember.</p>

  <h2>Event details</h2>
  <dl>
    <dt>Venue</dt><dd data-testid="venue_address">{venue_address}</dd>
    <dt>Date</dt><dd data-testid="date">{date}</dd>
    <dt>Time</dt><dd data-testid="time">{time}</dd>
    <dt>Party size</dt><dd data-testid="party_size">{party_size}</dd>
  </dl>

  <h2>Weather forecast</h2>
  <dl>
    <dt>Condition</dt><dd data-testid="condition">{condition}</dd>
    <dt>Temperature</dt><dd data-testid="temperature_c">{temperature_c}C</dd>
  </dl>

  <div class="cost">
    <h2>Cost breakdown</h2>
    <dl>
      <dt>Total</dt><dd data-testid="total">£{total_gbp}</dd>
      <dt>Deposit due</dt><dd data-testid="deposit">£{deposit_gbp}</dd>
    </dl>
  </div>
</body>
</html>
"""

    output_path = session.workspace_dir / "flyer.html"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        bytes_written = output_path.write_text(html, encoding="utf-8")
    except OSError as e:
        return ToolResult(
            success=False,
            output={},
            summary=f"generate_flyer: write failed ({e})",
            error=ToolError("SA_TOOL_DEPENDENCY_MISSING", f"write failed: {e}"),
        )

    output = {
        "path": str(output_path),
        "bytes_written": bytes_written,
    }
    summary = f"generate_flyer: wrote {output_path} ({bytes_written} chars)"

    record_tool_call(
        tool_name="generate_flyer",
        arguments={"event_details": event_details},
        output=output,
    )

    # generate_flyer is the terminal step; once it has run the agent can
    # legitimately call complete_task. We still leave a clear signal.
    return ToolResult(
        success=True,
        output=output,
        summary=f"{summary}. The flyer is written — you may now call complete_task.",
    )


# ---------------------------------------------------------------------------
# Registry builder — DO NOT MODIFY the name, signature, or registration calls.
# The grader imports and calls this to pick up your tools.
# ---------------------------------------------------------------------------
def build_tool_registry(session: Session) -> ToolRegistry:
    """Build a session-scoped tool registry with all four Ex5 tools plus
    the sovereign-agent builtins (read_file, write_file, list_files,
    handoff_to_structured, complete_task).

    DO NOT change the tool names — the tests and grader call them by name.
    """
    from sovereign_agent.tools.builtin import make_builtin_registry

    reg = make_builtin_registry(session)

    # venue_search
    reg.register(
        _RegisteredTool(
            name="venue_search",
            description=inspect.getdoc(venue_search),
            fn=venue_search,
            parameters_schema={
                "type": "object",
                "properties": {
                    "near": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "budget_max_gbp": {"type": "integer", "default": 1000},
                },
                "required": ["near", "party_size"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # read-only
            examples=[
                {
                    "input": {"near": "Haymarket", "party_size": 6, "budget_max_gbp": 800},
                    "output": {"count": 1, "results": [{"id": "haymarket_tap"}]},
                }
            ],
        )
    )

    # get_weather
    reg.register(
        _RegisteredTool(
            name="get_weather",
            description=inspect.getdoc(get_weather),
            fn=get_weather,
            parameters_schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["city", "date"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # read-only
            examples=[
                {
                    "input": {"city": "Edinburgh", "date": "2026-04-25"},
                    "output": {"condition": "cloudy", "temperature_c": 12},
                }
            ],
        )
    )

    # calculate_cost
    reg.register(
        _RegisteredTool(
            name="calculate_cost",
            description=inspect.getdoc(calculate_cost),
            fn=calculate_cost,
            parameters_schema={
                "type": "object",
                "properties": {
                    "venue_id": {"type": "string"},
                    "party_size": {"type": "integer"},
                    "duration_hours": {"type": "integer"},
                    "catering_tier": {
                        "type": "string",
                        "enum": ["drinks_only", "bar_snacks", "sit_down_meal", "three_course_meal"],
                        "default": "bar_snacks",
                    },
                },
                "required": ["venue_id", "party_size", "duration_hours"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=True,  # pure compute, no shared state
            examples=[
                {
                    "input": {
                        "venue_id": "haymarket_tap",
                        "party_size": 6,
                        "duration_hours": 3,
                    },
                    "output": {"total_gbp": 540, "deposit_required_gbp": 0},
                }
            ],
        )
    )

    # generate_flyer — parallel_safe=False because it writes a file
    def _flyer_adapter(event_details: dict) -> ToolResult:
        return generate_flyer(session, event_details)

    # Carry the real docstring onto the adapter so inspect.getdoc() sees it.
    _flyer_adapter.__doc__ = inspect.getdoc(generate_flyer)

    reg.register(
        _RegisteredTool(
            name="generate_flyer",
            description=inspect.getdoc(generate_flyer),
            fn=_flyer_adapter,
            parameters_schema={
                "type": "object",
                "properties": {"event_details": {"type": "object"}},
                "required": ["event_details"],
            },
            returns_schema={"type": "object"},
            is_async=False,
            parallel_safe=False,  # writes a file — MUST be False
            examples=[
                {
                    "input": {
                        "event_details": {
                            "venue_name": "Haymarket Tap",
                            "date": "2026-04-25",
                            "party_size": 6,
                        }
                    },
                    "output": {"path": "workspace/flyer.html"},
                }
            ],
        )
    )

    return reg


__all__ = [
    "build_tool_registry",
    "venue_search",
    "get_weather",
    "calculate_cost",
    "generate_flyer",
]
