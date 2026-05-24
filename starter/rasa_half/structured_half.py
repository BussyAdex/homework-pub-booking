"""Ex6 — RasaStructuredHalf reference solution.

Two paths:
  1. Real Rasa Pro server (default when RASA_PRO_LICENSE is set).
     RasaHostLifecycle trains, spawns rasa + action-server, waits for
     health, then this half POSTs to /webhooks/rest/webhook.
  2. Stdlib mock server (when no license / --mock). Lets students
     without a license validate normalise_booking_payload + HTTP wiring
     before signing up for Rasa.

Response parsing is structured-payload-first: the custom action emits
{"action": "committed" | "rejected" | "request_research", ...} and we
branch on that. Free-text matching is kept only as a last-resort
fallback so a reworded response template doesn't silently break grading.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from sovereign_agent.discovery import DiscoverySchema
from sovereign_agent.halves import HalfResult
from sovereign_agent.halves.structured import StructuredHalf
from sovereign_agent.session.directory import Session

from starter.rasa_half.validator import normalise_booking_payload

RASA_REST_WEBHOOK_DEFAULT = "http://localhost:5005/webhooks/rest/webhook"
_SOLUTION_EX6 = Path(__file__).resolve().parent


def _classify_messages(messages: list) -> dict:
    """Reduce Rasa's REST reply to a verdict.

    Returns {"verdict": "committed"|"rejected"|"research"|"unknown",
             "reason": str|None, "booking_reference": str|None}.
    Structured `custom` payloads win; text is a fallback.
    """
    verdict = "unknown"
    reason = None
    booking_reference = None

    for m in messages:
        if not isinstance(m, dict):
            continue
        custom = m.get("custom") if isinstance(m.get("custom"), dict) else {}
        action = custom.get("action")
        text = m.get("text") or ""
        low = text.lower()

        # ── structured signal (authoritative) ──
        if action == "committed":
            verdict = "committed"
            booking_reference = custom.get("booking_reference") or booking_reference
            continue
        if action == "rejected":
            verdict = "rejected"
            reason = custom.get("reason") or reason or "rejected"
            continue
        if action == "request_research":
            verdict = "research"
            reason = custom.get("reason") or reason
            continue

        # ── text fallback (only if no structured verdict yet) ──
        if verdict == "unknown":
            if "booking confirmed" in low or "reference:" in low:
                verdict = "committed"
                if "reference:" in low and not booking_reference:
                    # Split on the lowercased copy to stay case-insensitive,
                    # then map the index back onto the original text.
                    idx = low.index("reference:") + len("reference:")
                    booking_reference = text[idx:].strip().rstrip(".").upper()
            elif "can't accept" in low or "rejected" in low:
                verdict = "rejected"
                reason = reason or (text or "rejected by rasa")

    return {"verdict": verdict, "reason": reason, "booking_reference": booking_reference}


class RasaStructuredHalf(StructuredHalf):
    """Routes booking data through Rasa CALM flows via HTTP."""

    name = "rasa"

    def __init__(
        self,
        *,
        rasa_url: str = RASA_REST_WEBHOOK_DEFAULT,
        sender_id_prefix: str = "homework",
        request_timeout_s: float = 30.0,
    ) -> None:
        super().__init__(rules=[])
        self.rasa_url = rasa_url
        self.sender_id_prefix = sender_id_prefix
        self.request_timeout_s = request_timeout_s

    def discover(self) -> DiscoverySchema:
        return {
            "name": self.name,
            "kind": "half",
            "description": "Rasa CALM-backed structured half for booking confirmation.",
            "parameters": {"type": "object"},
            "returns": {"type": "object"},
            "error_codes": ["SA_EXT_SERVICE_UNAVAILABLE", "SA_EXT_TIMEOUT"],
            "examples": [
                {
                    "input": {"data": {"action": "confirm_booking", "deposit_gbp": 200}},
                    "output": {"success": True, "next_action": "complete"},
                }
            ],
            "version": "0.2.0",
            "metadata": {"rasa_url": self.rasa_url},
        }

    async def run(self, session: Session, input_payload: dict) -> HalfResult:
        data = input_payload.get("data") if isinstance(input_payload, dict) else None
        if not data:
            return HalfResult(
                success=False,
                output={"error": "input_payload missing 'data' dict"},
                summary="no data in input_payload",
                next_action="escalate",
            )

        # Pass through the requested flow if the caller specified one.
        action = data.get("action", "confirm_booking")
        try:
            rasa_msg = normalise_booking_payload(data, action=action)
        except Exception as e:  # noqa: BLE001
            return HalfResult(
                success=False,
                output={"error": str(e), "raw": data},
                summary=f"normalisation failed: {e}",
                next_action="escalate",
            )

        booking = rasa_msg["metadata"]["booking"]
        body = json.dumps(
            {
                "sender": rasa_msg["sender"],
                "message": rasa_msg["message"],
                "metadata": {"booking": booking},
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            self.rasa_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            raw_response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: urllib_request.urlopen(req, timeout=self.request_timeout_s).read(),
            )
        except HTTPError as e:
            return HalfResult(
                success=False,
                output={
                    "error": f"rasa HTTP {e.code}",
                    "error_code": "SA_EXT_SERVICE_UNAVAILABLE",
                    "booking": booking,
                },
                summary=f"rasa returned HTTP {e.code}",
                next_action="escalate",
            )
        except TimeoutError:
            return HalfResult(
                success=False,
                output={"error": "timeout", "error_code": "SA_EXT_TIMEOUT"},
                summary="rasa request timed out",
                next_action="escalate",
            )
        except URLError as e:
            # URLError can wrap a socket timeout; classify it as such.
            if isinstance(getattr(e, "reason", None), TimeoutError):
                return HalfResult(
                    success=False,
                    output={"error": "timeout", "error_code": "SA_EXT_TIMEOUT"},
                    summary="rasa request timed out",
                    next_action="escalate",
                )
            return HalfResult(
                success=False,
                output={
                    "error": str(e),
                    "error_code": "SA_EXT_SERVICE_UNAVAILABLE",
                    "booking": booking,
                },
                summary=f"rasa unreachable: {e}",
                next_action="escalate",
            )

        try:
            messages = json.loads(raw_response)
            if not isinstance(messages, list):
                raise ValueError("expected a JSON array of messages")
        except (json.JSONDecodeError, ValueError):
            return HalfResult(
                success=False,
                output={
                    "error": "rasa returned non-JSON or unexpected shape",
                    "raw": raw_response[:200].decode("utf-8", errors="replace"),
                },
                summary="rasa response not a JSON message list",
                next_action="escalate",
            )

        v = _classify_messages(messages)

        if v["verdict"] == "committed":
            return HalfResult(
                success=True,
                output={
                    "committed": True,
                    "booking": booking,
                    "booking_reference": v["booking_reference"],
                    "rasa_response": messages,
                },
                summary=f"booking confirmed by rasa (ref={v['booking_reference']})",
                next_action="complete",
            )

        if v["verdict"] == "research":
            return HalfResult(
                success=False,
                output={
                    "research_requested": True,
                    "reason": v["reason"],
                    "rasa_response": messages,
                    "booking": booking,
                },
                summary=f"rasa requested research: {v['reason']}",
                next_action="research",
            )

        if v["verdict"] == "rejected":
            return HalfResult(
                success=False,
                output={
                    "rejected": True,
                    "reason": v["reason"],
                    "rasa_response": messages,
                    "booking": booking,
                },
                summary=f"rasa rejected: {v['reason']}",
                next_action="escalate",
            )

        return HalfResult(
            success=False,
            output={
                "rasa_response": messages,
                "note": "no committed/rejected/research signal detected",
            },
            summary="rasa returned unexpected output",
            next_action="escalate",
        )


# ─────────────────────────────────────────────────────────────────────
# Host-process Rasa orchestration (no Docker)
# ─────────────────────────────────────────────────────────────────────
class RasaHostLifecycle:
    """Spawn rasa-pro + action-server as host processes, wait for health,
    tear down. Uses the uv-managed venv's `rasa` CLI directly.

    Host-process (not Docker) because rasa-pro installs in the same venv
    as sovereign-agent; the homework teaches the *protocol* (REST webhook
    + action server), and process management is orthogonal.
    """

    def __init__(
        self,
        *,
        rasa_project_dir: Path | None = None,
        rasa_port: int = 5005,
        action_port: int = 5055,
        startup_timeout_s: float = 180.0,
        log_dir: Path | None = None,
    ) -> None:
        self.rasa_project_dir = rasa_project_dir or (
            _SOLUTION_EX6.parent.parent.parent / "rasa_project"
        )
        self.rasa_port = rasa_port
        self.action_port = action_port
        self.startup_timeout_s = startup_timeout_s
        self.log_dir = log_dir
        self._rasa_proc: subprocess.Popen | None = None
        self._action_proc: subprocess.Popen | None = None

    def _log(self, msg: str) -> None:
        print(msg, flush=True)
        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with (self.log_dir / "rasa_host.log").open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                pass

    async def __aenter__(self) -> str:
        if not os.environ.get("RASA_PRO_LICENSE"):
            raise RuntimeError(
                "RASA_PRO_LICENSE is not set. Rasa Pro refuses to start "
                "without a license. Set it in your .env, or use the mock "
                "server (spawn_mock_rasa) as a fallback."
            )
        if not self.rasa_project_dir.exists():
            raise RuntimeError(
                f"rasa_project/ not found at {self.rasa_project_dir}. "
                "Did `make educator-apply-solution` run?"
            )

        self._log(f"▶ training Rasa model in {self.rasa_project_dir}")
        train_rc = self._run_sync(
            ["rasa", "train"],
            cwd=self.rasa_project_dir,
            timeout=240,
            log_name="rasa_train.log",
        )
        if train_rc != 0:
            raise RuntimeError(f"rasa train exited {train_rc} — see {self.log_dir}/rasa_train.log")
        self._log("✓ Rasa model trained")

        self._log(f"▶ starting action server on :{self.action_port}")
        self._action_proc = self._spawn_bg(
            ["rasa", "run", "actions", "-p", str(self.action_port)],
            cwd=self.rasa_project_dir,
            log_name="rasa_actions.log",
        )

        self._log(f"▶ starting rasa server on :{self.rasa_port}")
        self._rasa_proc = self._spawn_bg(
            ["rasa", "run", "--enable-api", "--cors", "*", "-p", str(self.rasa_port)],
            cwd=self.rasa_project_dir,
            log_name="rasa_server.log",
        )

        deadline = time.monotonic() + self.startup_timeout_s
        last_err = "(no probe yet)"
        while time.monotonic() < deadline:
            try:
                with urllib_request.urlopen(
                    f"http://localhost:{self.rasa_port}/version", timeout=3
                ) as resp:
                    if resp.status == 200:
                        body = resp.read().decode("utf-8")[:120]
                        self._log(f"✓ Rasa healthy: {body}")
                        return f"http://localhost:{self.rasa_port}/webhooks/rest/webhook"
            except (URLError, HTTPError) as e:
                last_err = str(e)
                if self._rasa_proc and self._rasa_proc.poll() is not None:
                    self._log(
                        f"✗ rasa server died rc={self._rasa_proc.returncode} "
                        f"— see {self.log_dir}/rasa_server.log"
                    )
                    break
                if self._action_proc and self._action_proc.poll() is not None:
                    self._log(
                        f"✗ action server died rc={self._action_proc.returncode} "
                        f"— see {self.log_dir}/rasa_actions.log"
                    )
                    break
            await asyncio.sleep(2)

        self._log(f"✗ Rasa not healthy after {self.startup_timeout_s}s. Last error: {last_err}")
        raise TimeoutError(f"Rasa not healthy after {self.startup_timeout_s}s")

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._log("▶ tearing down Rasa + action server")
        for name, proc in (("rasa", self._rasa_proc), ("actions", self._action_proc)):
            if proc is None:
                continue
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self._log(f"  {name} exited (rc={proc.returncode})")
            except Exception as e:  # noqa: BLE001
                self._log(f"  {name} teardown failed: {e}")

    def _spawn_bg(self, cmd: list[str], cwd: Path, log_name: str) -> subprocess.Popen:
        self._log(f"  $ {' '.join(cmd)}  (cwd={cwd})")
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            fh = (self.log_dir / log_name).open("w", encoding="utf-8")
        else:
            fh = subprocess.DEVNULL
        try:
            return subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=fh,
                stderr=subprocess.STDOUT,
                env={**os.environ},
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Command not found: {cmd[0]!r}. Install rasa-pro into the "
                "venv: `uv sync --all-groups --extra rasa` or `pip install rasa-pro`"
            ) from e

    def _run_sync(self, cmd: list[str], *, cwd: Path, timeout: int, log_name: str) -> int:
        self._log(f"  $ {' '.join(cmd)}  (cwd={cwd})")
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / log_name).open("w", encoding="utf-8") as fh:
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=str(cwd),
                        stdout=fh,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        env={**os.environ},
                    )
                    return proc.returncode
                except subprocess.TimeoutExpired:
                    self._log(f"  ✗ {cmd[0]} timed out after {timeout}s")
                    return 124
        proc = subprocess.run(cmd, cwd=str(cwd), timeout=timeout)
        return proc.returncode


# ─────────────────────────────────────────────────────────────────────
# Stdlib mock server (used when no Rasa license)
# ─────────────────────────────────────────────────────────────────────
class _MockRasaHandler(BaseHTTPRequestHandler):
    """Stdlib mock of Rasa's REST webhook. Same party/deposit rules as
    ActionValidateBooking so both paths agree for a given input. Also
    mirrors /request_research so the research path is testable offline."""

    def log_message(self, fmt, *args):  # noqa: N802
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:  # noqa: BLE001
            payload = {}

        message = payload.get("message", "")
        booking = payload.get("metadata", {}).get("booking", {})

        if message == "/request_research":
            response = [
                {
                    "text": "Researching another venue.",
                    "custom": {"action": "request_research", "reason": "exceeds_cap"},
                }
            ]
            return self._send(response)

        party = booking.get("party_size")
        deposit = booking.get("deposit_gbp", 0)

        if not party:
            response = [
                {
                    "text": "Booking rejected (missing party size).",
                    "custom": {"action": "rejected", "reason": "missing_party_size"},
                }
            ]
        elif party > 8:
            response = [
                {
                    "text": "Sorry, we can't accept this booking. Reason: party_too_large",
                    "custom": {"action": "rejected", "reason": "party_too_large"},
                }
            ]
        elif deposit > 300:
            response = [
                {
                    "text": "Sorry, we can't accept this booking. Reason: deposit_too_high",
                    "custom": {"action": "rejected", "reason": "deposit_too_high"},
                }
            ]
        else:
            ref = (
                "BK-"
                + hashlib.sha1(
                    f"{booking.get('venue_id')}|{booking.get('date')}|"
                    f"{booking.get('time')}|{party}".encode()
                )
                .hexdigest()[:8]
                .upper()
            )
            response = [
                {
                    "text": f"Booking confirmed. Reference: {ref}.",
                    "custom": {"action": "committed", "booking_reference": ref},
                }
            ]
        self._send(response)

    def _send(self, response: list) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))


def spawn_mock_rasa(
    port: int = 5905,
) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockRasaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/webhooks/rest/webhook"
    return server, thread, url


__all__ = [
    "RASA_REST_WEBHOOK_DEFAULT",
    "RasaHostLifecycle",
    "RasaStructuredHalf",
    "spawn_mock_rasa",
]
