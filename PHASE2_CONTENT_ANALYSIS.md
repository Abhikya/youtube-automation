# Stored performance and content analysis

`analyze_content(channel_context_id, publications, snapshots, features)` is pure,
validated and descriptive. It reuses PublishedVideo, MetricSnapshot, ContentFeatures
and LearningObservation. The preview DTOs are not database persistence models.
No tables, learning contexts, prompts, production hooks or API calls are added.

Same-channel records join by stored YouTube/publication IDs or a shared generation
reference. Cross-channel/malformed/orphan evidence and conflicting duplicate IDs
are reported, never guessed. Feature versions stay separate with provenance and
knowledge labels intact. Missing values stay null. Each observation retains source,
status, window, timestamp and known video age. Multiple snapshots are ordered, not
summed or treated as independent videos. Like/view and comment/view ratios require
present counters, positive views and success/partial collection status. Ratios are
raw proportions, not percentages or causal estimates. No performance rankings or
cross-video pattern claims are made; comparative evidence explicitly reports
"insufficient evidence for comparative pattern". This version does not establish
matched observation windows or statistical sample sufficiency, even with multiple
videos; those require future comparison design rather than an invented threshold.

`preview_analysis(repository, page_size=100)` reads publications, snapshots and
features through the existing repository, within one repeatable-read/read-only
transaction. A real connection must be idle. Inventory failures propagate rather
than reporting incomplete data as complete. No create/update method is invoked.

Read-only preview CLI (not run during implementation):
`python -B -m engine.learning.content_analysis --channel-context-id <stored-context-id>`

The CLI prints validated JSON and exits nonzero for data issues or sanitized read
failure. It only opens PostgreSQL when explicitly invoked; it never authenticates
with YouTube, builds LearningContext or persists analysis. Offline tests use only
fixtures/mock repositories. Live validation remains separately authorized work.
