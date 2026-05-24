# Ex5 — Edinburgh research loop scenario

## Your answer

The planner produced two subgoals: sg_1 (research venues near Haymarket
for a party of 6, assigned to loop) and sg_2 (produce a flyer with the
chosen venue, weather, and cost, also loop). Both ran in the same
executor session.

Turn 1 called venue_search, get_weather, and calculate_cost in parallel
— all three are parallel_safe because they only read fixtures. Turn 2
wrote the flyer via generate_flyer (parallel_safe=False because it
writes a file). Turn 3 called complete_task.

The dataflow integrity check caught one issue during development: the
template for "no deposit required" originally read "total under £300
threshold", which put £300 in the flyer prose. That value was never
returned by any tool — it's a rule threshold, not data. I simplified
the phrasing to "No deposit required for this booking." Without the
integrity check this would have slipped past review because £300 looks
like a reasonable number in the right context.

## Citations

- sessions/sess_fe7bf53d67a8/logs/trace.jsonl — tool call sequence
- sessions/sess_fe7bf53d67a8/workspace/flyer.md — the produced flyer
