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
This phase does not add repositories or RLS. Evidence inside context JSONB is
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
