# Bounded versioned learning context builder/export

This isolated layer reuses LearningContext, LearningObservation, RecordReference,
the analysis/observation builders, and LearningRepository. No prompt, generation,
production or YouTube integration is installed. Context versions are opaque build
semantics, not chronological IDs. Every separate build has a separate context_id;
repeat requests for an identical existing ID skip, while changed content conflicts.

## Explicit bounds and selection

No existing design specifies numeric context limits. Callers must supply positive
max_publications, max_observations, max_evidence_links and max_export_bytes; there
are no hidden numeric defaults. The metric analysis window is [start_at,end_at).
Unknown publication dates remain unknown; the window is not inferred from them.
Latest observed publications are selected first, with YouTube ID tie-breaking;
publications without window metrics sort last. The publication bound may select a
subset, reported in provenance. Observation/link/UTF-8 export overflow rejects the
build rather than silently removing evidence or the insufficiency statement.
Examples are ordered deduplicated direct evidence references, not fabricated text.

`build_context(analysis, context_id=..., built_at=..., start_at=..., end_at=...,
max_publications=..., max_observations=..., max_evidence_links=...,
max_export_bytes=..., context_version='descriptive-context-v1', expires_at=None)`
returns the existing LearningContext. Same evidence and parameters produce the
same selection, ordered observations/evidence and export. Required times are aware;
future source evidence and empty/invalid contexts fail safely. `export_context`
returns validated bounded JSON without writing a file.

sample_count means distinct selected publications; snapshots and feature versions
are not independent videos. The one-publication Tunguska evidence produces metric
and feature descriptions, temporal counter stability and explicit insufficient
evidence for comparative pattern. No causal or cross-video pattern claim is made.
Known/inferred labels, missing values, source timestamps and source provenance are
preserved in the context's provenance evidence envelope. data_through_at is the
latest selected metric observation, or null with no selected metric observations;
it does not imply more recent statistics. built_at is the actual supplied build
time. Evidence links point to real typed, same-channel source records.

## Preview and explicit persistence

`prepare_context(repository, persist=False, **build_parameters)` borrows the existing
repeatable-read/read-only analysis inventory. Preview does not write or run migrations.
`persist=True` invokes `persist_context` explicitly. Source records are read again and
compared before insertion; missing/changed evidence conflicts. An existing identical
context skips and remains immutable; conflicting context IDs are rejected.

The original migration stored observations/examples as JSON inside learning_contexts.
It cannot satisfy the required ordered child-row/typed-FK architecture. The additive
0002_context_children migration therefore normalizes that storage into
learning_observations and learning_evidence. Existing Python models are unchanged;
0001 remains unchanged. Legacy arrays backfill with ordinality and fail safely on
invalid/unresolved evidence. JSON columns are removed only in the same transactional
migration, after validated backfill. No live migration has been run.

create_context now inserts header, ordered observation children, ordered evidence
links and examples in one existing repository transaction, then seals the header.
Evidence roles are observation/example; positions are zero-based. Conditional
foreign keys bind each supported record type to its same-channel target table.
Sealing validates structure/order; triggers reject changes to sealed headers/children,
and deferred validation rejects committed unsealed partial headers. Database
functions capture the explicit learning-schema search path. Repository reads
reconstruct the original model in order and reject malformed child structures.
An escaping observation/evidence/read-back error rolls back the complete build.
No independent observation persistence system or duplicate Python models are added.

CLI defaults to preview; every limit and window is required:

`python -B -m engine.learning.context_builder --channel-context-id <context> --start-at <aware-time> --end-at <aware-time> --max-publications <limit> --max-observations <limit> --max-evidence-links <limit> --max-export-bytes <limit> --preview`

Replace --preview with --persist only for explicitly authorized persistence after
the migration is separately reviewed/applied. --context-id is optional at CLI level
(new UUID per invocation); the pure builder requires an explicit ID. --context-version
is optional. No database or API call runs at import time. CLI failures are sanitized.

Validation uses fixtures, simulated rollback-capable connections and actual repository
SQL construction/reconstruction. Live PostgreSQL integration tests remain gated and
are not run for this task. The new migration still needs separately authorized
PostgreSQL integration validation before any live deployment or persistence.
