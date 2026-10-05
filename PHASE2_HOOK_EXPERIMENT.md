# Local hook-structure experiment

Production remains on the unchanged BASELINE prompt and model. No rollout flag,
workflow change, learning-context injection or database migration is introduced.

`generate_script(idea, hook_strategy="HOOK_EVENT_FIRST_V1", baseline_script=script)`
rewrites only the baseline opening; remaining text is preserved byte-for-byte.
Without baseline_script it first generates a baseline. This preserves later narration,
not necessarily the first sentence's factual equivalence: human review is required.
The baseline prompt does not require DATE-FIRST; naturally event-first baselines must
be retained and labelled as low-contrast pairs rather than forced into date-first style.

Local JSON pairs store experiment_name=hook_structure and variant=baseline or
hook_event_first_v1, source idea, full script, first sentence, word count, model,
script SHA-256, generated timestamp and an unchanged-remainder verification.
These are durable LOCAL experiment outputs, not database GenerationRecords or
production queue metadata. No experimental rendered/uploaded videos are produced.
Production experiment rollout and queue attribution remain out of scope.

Target duration remains the existing 40-second prompt target; actual duration is
unknown until rendering. Do not equate word count with observed duration.

Before any publication, review factual equivalence and reject extra claims. Keep
idea/category, metadata, CTA, voice/render settings and slots matched. Do not upload
both scripts as near-duplicates without a separate approved experiment design.

Evaluate at 24h, 48h and 7d after verified public publication, waiting for Analytics
completeness. Primary: retention 0.10/0.25, averageViewPercentage, averageViewDuration.
Secondary: retention 0.50/1.00, views, subscribers gained, Shorts source share.
Reach impressions/CTR are secondary when available; keep source exposure separate.

Pre-register at least six comparable published pairs; these three local pairs are
prompt validation only. Report paired differences, wins/ties/losses, exposure,
missing observations and counterexamples. A candidate repeated signal requires
higher early retention in at least four of six complete comparisons at both 48h
and 7d, with no consistent deterioration in midpoint/final retention. This is an
exploratory review threshold, not statistical significance or a causal rule.
Never count missing Analytics as zero. Do not change thresholds after viewing results.

## Reorder-only revision

The experimental opening must preserve every lexical token and occurrence count;
only phrase order, capitalization and punctuation may change. The validator flags
removed month/year detail, changed numeric quantities, removed capitalized entity or
location candidates, added/removed content, and changes to certainty/emotion terms.
These are heuristic flags, not named-entity recognition or factual verification.
Any lexical change blocks the experimental response. Human review remains mandatory
because word-preserving reordering can still change meaning, scope or emphasis.
The v2 local preview retains original baseline scripts and the unchanged remainder.

Six-pair future plan (not generated or published):
1 cosmic signal: baseline 10:00 / experiment 14:00
2 maritime disappearance: experiment 10:00 / baseline 14:00
3 missing-person case: baseline 14:00 / experiment 19:00
4 cosmic anomaly: experiment 14:00 / baseline 19:00
5 maritime mystery: baseline 19:00 / experiment 10:00
6 missing-person case: experiment 19:00 / baseline 10:00
Each arm occurs twice in each slot. Use distinct comparable stories within pairs to
avoid near-duplicate uploads; randomize story-to-arm assignment before generation.
Pairs are a matched exploratory comparison, not the same-content randomized trial.
Schedule each pair within the same short calendar block; do not change normal slots.
Use actual public publication timestamps, and evaluate at +24h, +48h and +7d once
Analytics is complete. API calendar-day reports do not guarantee exact hourly windows;
label day-bucket approximations and avoid claiming precise T+24h Analytics exposure.
