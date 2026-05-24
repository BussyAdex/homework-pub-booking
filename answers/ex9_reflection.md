# Ex9 — Reflection

## Q1 — Planner handoff decision

### Your Answer

In my Ex7 run (sess_034213335c96), the transition to the structured half happened during the first bridge round. The strongest evidence is the handoff_to_structured tool call recorded in trace.jsonl, which included the reason: "loop half identified a candidate venue; passing to structured half for confirmation under policy rules."

The trigger for the handoff was the need for policy validation. Final confirmation required business-rule enforcement, which is the responsibility of the structured half. One interesting detail is that the preceding venue_search(Haymarket, party=12) returned no results, yet the executor still attempted to book "Haymarket Tap" for a party of 12. This suggests that the loop half proposed a venue that had not been verified through the search results, creating an inconsistency that needed to be checked.

The structured half correctly identified the issue. After the handoff, the bridge moved from the loop half to the structured half, which rejected the booking with the rejection reason party_too_large. Control then returned to the loop half, and the planner was invoked again using the rejection outcome as the new task. During the second round, the party size was reduced from 12 to 6 and a booking was made at The Royal Oak. The reservation was successfully committed with reference BK-B7655866.

This demonstrates the intended behaviour of the architecture. The loop half is responsible for exploring options and generating candidate solutions, while the structured half acts as a reliable gatekeeper that enforces business rules. When a proposal fails validation, the structured half provides a clear, machine-readable rejection that allows the system to retry with a corrected plan rather than proceeding with an invalid booking.

### Citation

- sessions/sess_034213335c96/logs/trace.jsonl (handoff_to_structured event and subsequent state transitions)
- sessions/sess_034213335c96/logs/tickets/tk_4b059f5a/raw_output.json.

---

## Q2 — Dataflow integrity catch

### Your answer

My Ex5 run (sess_7db8d0310fa7) did not produce an actual integrity-check failure, so I will describe a realistic and reproducible scenario that would trigger one. In the trace, calculate_cost(royal_oak, 8) returned a total cost of £973 and a deposit of £195. These same values were then passed to generate_flyer, and the resulting flyer.html displayed them correctly. The data flowed consistently from the tool output to the final artifact, so no integrity issue was detected.

To create a failure, I would deliberately modify the value passed to generate_flyer while leaving the original calculate_cost output unchanged. For example, the flyer's data-testid="total" could be changed to display £1,250 instead of £973. I would avoid using a value such as £975 because the difference is too small and could distract from the purpose of the test. The goal is not to show that people miss small numerical discrepancies, but rather that humans rarely recalculate event costs when reviewing a document. A figure like £1,250 still appears reasonable at a glance and could easily pass a manual review.

A dataflow integrity check approaches the problem differently. Instead of asking whether a value looks plausible, it verifies that every rendered fact can be traced back to a trusted tool output. In this scenario, the checker would compare the value shown in the flyer with the recorded result from calculate_cost and detect that £1,250 does not originate from any tool result. It would therefore return ok=False and include the total cost in the list of unverified_facts.

The test is straightforward to reproduce. Rerun the session, keep the calculate_cost output fixed at £973, modify only the value passed into generate_flyer, and then run the integrity checker. The checker should flag the rendered total as unverified because it no longer matches the authoritative source recorded in the tool log. This demonstrates how integrity checks can identify data inconsistencies that might otherwise go unnoticed during manual review

### Citation

- sessions/sess_7db8d0310fa7/logs/trace.jsonl (calculate_cost and generate_flyer events)
- sessions/sess_7db8d0310fa7/workspace/flyer.html

---

## Q3 — Removing one framework primitive

### Your answer

The first production failure I would expect in a real pub-booking deployment is a duplicate booking caused by a worker crash after the booking has been successfully submitted to the venue's system but before the completion is recorded locally. The sovereign-agent primitive that would surface this issue is the ticket state machine.

The failure occurs when a booking request is successfully processed by the venue's system, but the worker handling the request crashes before the ticket is updated to a terminal state such as "complete." When the system restarts, the ticket still appears unfinished, so it is picked up for retry. If the external booking API is not idempotent, the retry creates a second booking for the same customer, venue, date, and time slot.

It is important to note that this failure did not occur in my own session. My run contained no crashes, retries, or concurrent execution. This scenario is derived from the architecture and represents a realistic production risk rather than an observed issue from the exercise.

The ticket state machine is the key primitive because it provides a durable and auditable record of execution progress. Its forward-only state transitions make it possible to inspect exactly what happened before and after a failure. An operator reviewing the ticket history can determine whether a booking action was executed multiple times, whether a ticket became stuck between states following a restart, or whether a retry was triggered after a crash.

This audit trail is critical for diagnosing the root cause of duplicate bookings. Without the ticket state machine's transition history, it would be difficult to distinguish between a system-generated duplicate booking, a customer accidentally submitting the request twice, or an error originating from the venue's system.

In summary, the most likely early production failure is a duplicate booking caused by a crash between external booking success and local completion recording. The ticket state machine is the sovereign-agent primitive that exposes the problem by providing a clear, inspectable history of state transitions and retries.

### Citation

- sess_034213335c96/logs/trace.jsonl (handoff and state transitions)
- sessions/sess_034213335c96/logs/tickets/tk_4b059f5a/raw_output.json
