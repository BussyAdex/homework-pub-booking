"""Ex7 — handoff bridge.

Routes between the loop half and the Rasa-backed structured half,
supporting REVERSE handoffs (structured → loop) when the structured
half rejects.

The base sovereign-agent LoopHalf only knows how to request a handoff
FORWARD. The bridge you're building here is the thing that decides
what to do when the structured half says "no, go back and try again".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sovereign_agent.halves import HalfResult
from sovereign_agent.halves.loop import LoopHalf
from sovereign_agent.halves.structured import StructuredHalf
from sovereign_agent.handoff import Handoff
from sovereign_agent.session.directory import Session
from sovereign_agent.session.state import now_utc

BridgeOutcome = Literal["completed", "failed", "max_rounds_exceeded"]


@dataclass
class BridgeResult:
    outcome: BridgeOutcome
    rounds: int
    final_half_result: HalfResult | None
    summary: str


class HandoffBridge:
    """Orchestrates round-trips between LoopHalf and a StructuredHalf.

    Not a sovereign-agent Half itself — it lives one level up, deciding
    which half should run next.
    """

    def __init__(
        self,
        *,
        loop_half: LoopHalf,
        structured_half: StructuredHalf,
        max_rounds: int = 3,
    ) -> None:
        self.loop_half = loop_half
        self.structured_half = structured_half
        self.max_rounds = max_rounds

    # ------------------------------------------------------------------
    #  the main run method
    # ------------------------------------------------------------------
    async def run(self, session: Session, initial_task: dict) -> BridgeResult:
        """Run the bridge until the session completes, fails, or hits max_rounds."""
        from sovereign_agent.handoff import write_handoff

        rounds = 0
        current_input: dict = initial_task
        last_loop = last_struct = None

        while rounds < self.max_rounds:
            rounds += 1
            session.append_trace_event(
                {
                    "event_type": "bridge.round_start",
                    "actor": "bridge",
                    "payload": {"round": rounds, "half": "loop"},
                }
            )
            loop_result = await self.loop_half.run(session, current_input)
            last_loop = loop_result

            if loop_result.next_action == "complete":
                session.mark_complete(loop_result.output)
                session.append_trace_event(
                    {
                        "event_type": "session.state_changed",
                        "actor": "bridge",
                        "payload": {"from": "executing", "to": "complete", "via": "loop"},
                    }
                )
                return BridgeResult(
                    outcome="completed",
                    rounds=rounds,
                    final_half_result=loop_result,
                    summary=f"loop completed in round {rounds}",
                )

            if loop_result.next_action != "handoff_to_structured":
                session.mark_failed(
                    {"reason": f"unexpected loop outcome: {loop_result.next_action}"}
                )
                return BridgeResult(
                    outcome="failed",
                    rounds=rounds,
                    final_half_result=loop_result,
                    summary=f"unexpected loop outcome: {loop_result.next_action}",
                )

            handoff = build_forward_handoff(session, loop_result)
            write_handoff(session, "structured", handoff)
            session.append_trace_event(
                {
                    "event_type": "session.state_changed",
                    "actor": "bridge",
                    "payload": {"from": "loop", "to": "structured", "round": rounds},
                }
            )

            struct_result = await self.structured_half.run(session, {"data": handoff.data})
            last_struct = struct_result

            if struct_result.next_action == "complete":
                session.mark_complete(struct_result.output)
                session.append_trace_event(
                    {
                        "event_type": "session.state_changed",
                        "actor": "bridge",
                        "payload": {"from": "structured", "to": "complete", "round": rounds},
                    }
                )
                return BridgeResult(
                    outcome="completed",
                    rounds=rounds,
                    final_half_result=struct_result,
                    summary=f"structured confirmed in round {rounds}",
                )

            if struct_result.next_action == "escalate":
                current_input = build_reverse_task(loop_result, struct_result)
                session.append_trace_event(
                    {
                        "event_type": "session.state_changed",
                        "actor": "bridge",
                        "payload": {
                            "from": "structured",
                            "to": "loop",
                            "round": rounds,
                            "rejection_reason": (struct_result.output or {}).get("reason")
                            or struct_result.summary,
                        },
                    }
                )
                forward_file = session.ipc_input_dir / "handoff_to_structured.json"
                if forward_file.exists():
                    archive = session.handoffs_audit_dir / f"round_{rounds}_forward.json"
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    forward_file.rename(archive)
                continue

            session.mark_failed(
                {"reason": f"unexpected struct outcome: {struct_result.next_action}"}
            )
            return BridgeResult(
                outcome="failed",
                rounds=rounds,
                final_half_result=struct_result,
                summary=f"unexpected struct outcome: {struct_result.next_action}",
            )

        session.mark_failed({"reason": f"max_rounds={self.max_rounds} exceeded"})
        final = last_struct or last_loop
        return BridgeResult(
            outcome="max_rounds_exceeded",
            rounds=rounds,
            final_half_result=final,
            summary=f"bridge exhausted {self.max_rounds} rounds without resolution",
        )


# ---------------------------------------------------------------------------
# Helper constructors — you may use these or write your own
# ---------------------------------------------------------------------------
def build_forward_handoff(session: Session, loop_result: HalfResult) -> Handoff:
    """Package a loop result into a forward-handoff payload for structured."""
    return Handoff(
        from_half="loop",
        to_half="structured",
        written_at=now_utc(),
        session_id=session.session_id,
        reason="loop-half requested confirmation",
        context=loop_result.summary,
        data=(loop_result.handoff_payload or {}).get("data") or loop_result.output,
        return_instructions=(
            "If you cannot confirm (party too large, deposit too high, etc.), "
            "respond with next_action=escalate and include a human-readable "
            "'reason' in output so the loop half can adapt."
        ),
    )


def build_reverse_task(loop_result: HalfResult, struct_result: HalfResult) -> dict:
    """Build the task dict to pass back to the loop half after a reject.

    Note: struct_result.output may be None when the structured half escalates
    without a structured payload (e.g. the grader's "always reject" variant).
    Guard the access the same way the bridge's inline trace event does, falling
    back to the result summary so the loop still receives a usable reason
    instead of the bridge crashing with AttributeError.
    """
    reason = (struct_result.output or {}).get("reason") or struct_result.summary
    return {
        "task": (
            "The structured half rejected the previous proposal. "
            f"Reason: {reason}. Produce an alternative."
        ),
        "context": {
            "prior_result": loop_result.output,
            "rejection_reason": reason,
            "retry": True,
        },
    }


__all__ = [
    "BridgeOutcome",
    "BridgeResult",
    "HandoffBridge",
    "build_forward_handoff",
    "build_reverse_task",
]
