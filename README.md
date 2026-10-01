# youtube-automation
Youtube automation


## Phase 2 PostgreSQL foundation

The learning database is separate from generation, scheduling, upload, Telegram,
and GitHub recovery checkpoints. It uses psycopg 3; no SQLite fallback exists.
Set DATABASE_URL in the ignored root .env or process environment. Connections are
explicit, preserve SSL parameters, and do not print credentials. Neon URLs must
specify sslmode=require, verify-ca, or verify-full. Prefer verify-full when configured.

Migrations use a dedicated learning schema, a transactional advisory lock, and a
version/name/SHA-256 ledger. Applied migration files are immutable. Use a direct
PostgreSQL connection and a DDL-capable role; migrations never run automatically.
To initialize the configured database explicitly:

```powershell
.\venv\Scripts\python.exe -B -m engine.learning.migrations
```

Six tables map the six existing Pydantic models. Schema IDs remain TEXT;
internal row IDs use UUID. Timestamps use TIMESTAMPTZ. Flexible configurations,
provenance, features, observations, evidence references and metrics use JSONB.
Unknown metrics remain absent/null, without zero defaults. New context builds
should receive distinct context IDs to retain history. Field adapters revalidate
against schema.py. Scoped foreign keys protect generation/publication/metric/feature
links. Queries must require channel_context_id; from_row also verifies it.
Phase 2.3 did not add repositories or RLS; Phase 2.4 adds repositories below. Evidence inside context JSONB is
schema-validated, but target existence is not yet enforced by database foreign keys.

Offline checks never require DATABASE_URL:

```powershell
.\venv\Scripts\python.exe -B -m unittest test_learning_database
```

Explicit live integration checks use DATABASE_URL, create a unique temporary schema
inside a rollback-only transaction, and leave no learning tables or fixture data.
Use a development database with schema-creation privileges, never a production
upload operation. A missing URL or connection failure fails rather than silently skips:

```powershell
$env:RUN_LEARNING_DB_TESTS = "1"
.\venv\Scripts\python.exe -B -m unittest test_learning_database.PostgreSQLIntegrationTests
Remove-Item Env:RUN_LEARNING_DB_TESTS
```

Use separate development/test/production credentials. Never commit real URLs,
passwords, tokens or secret-bearing response payloads. No learning or collection
algorithm is included in this foundation.


## Phase 2.4 persistence repositories

`engine/learning/repositories.py` provides one `LearningRepository` bound to a
required channel_context_id and a borrowed connection from `connect_database()`.
It reuses `mapping.py` and the existing schemas. No migrations run automatically,
and the production pipeline does not import or use the repositories.

Operations include profile create/get/upsert; generation create/get/history;
publication create/upsert and ID/YouTube/generation lookup; insert-only snapshot
history; feature retrieval by generation/publication; and immutable context builds
with latest/history lookup. The profile schema has no active flag, so no active
channel listing is invented. All list queries are scoped and paginated (default
100 rows, maximum 1000); date ranges are inclusive start/exclusive end. Snapshot
history is oldest first; generation/publication/context history is newest first.
Latest context means newest build, without silently filtering freshness or expiry.

```python
from engine.learning.database import connect_database
from engine.learning.repositories import LearningRepository

with connect_database() as connection:
    repository = LearningRepository(connection, channel_context_id="your-context-id")
    history = repository.recent_generations(limit=10)
    # Use repository.transaction() to group related writes atomically.
```

Each operation uses a transaction/savepoint. Grouped writes roll back together when
an exception escapes `repository.transaction()`. Profile/publication upserts preserve
creation time and reject stale updates; publication record IDs cannot be reassigned
to a different YouTube video. Duplicate snapshot/context IDs fail instead of replacing
history. Database exceptions are reported without driver credential details.
Channel filtering plus existing scoped foreign keys provide isolation; no RLS is
added. Context evidence target existence remains the Phase 2.3 limitation.

Run `python -B -m unittest test_learning_repositories` for offline tests.
Set RUN_LEARNING_DB_TESTS=1 explicitly to include live repository tests. They reuse
the existing migration/connection infrastructure, create a unique test schema and
roll back all fixtures and schema changes. Use development/test credentials.
