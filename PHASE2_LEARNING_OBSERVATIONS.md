# Evidence-backed learning observation preview

`build_observations(analysis)` consumes validated content-analysis output and reuses
LearningObservation (notes, evidence, sample_count, uncertainty_notes). It revalidates
underlying records, joins and calculated output; incomplete/conflicting or altered
analysis is rejected with a sanitized error. No parallel observation model is added.
The result envelope retains the canonical analysis and original source provenance,
timestamps, knowledge labels and evidence references. The model has no standalone
observation ID, type, confidence or timestamp fields, so these are not invented.

Per-snapshot descriptions, separate feature-version descriptions, missing-data notes
and compatible cumulative temporal counter changes are evidence-backed. Temporal
changes require the same source, matching cumulative start bounds, increasing times,
window ends equal to collection times and successful/partial snapshots. Negative
counter changes are possible corrections, not fabricated new activity. Missing
values remain unknown. sample_count counts distinct publications; versions and
snapshots do not increase it. Feature text is stored evidence, not endorsed advice.

Comparative evidence remains INSUFFICIENT EVIDENCE, including multi-video datasets:
the existing analysis does not establish matched windows or statistical sufficiency.
No causal conclusions, rankings or generation recommendations are produced. An empty
inventory produces no observations rather than inventing evidence/sample counts.

Same validated input produces the same observations and SHA256 evidence digest.
Changed evidence changes the digest. Conflicting source identities are rejected;
existing observations are never overwritten. Persistence is deliberately absent:
LearningRepository only stores observations inside LearningContext, whose builder
is outside this task. No new table, migration or write API is introduced.

`preview_observations(repository)` reuses preview_analysis's repeatable-read/read-only
transaction and pagination. No create/update method is invoked. Optional explicitly
invoked read-only CLI:

`python -B -m engine.learning.learning_observations --channel-context-id <stored-context-id>`

The CLI prints validated JSON or a sanitized error. It does not authenticate with
YouTube, persist observations, construct LearningContext or integrate with prompts.
Tests use offline fixtures/mocks only. No live preview is run during implementation.
