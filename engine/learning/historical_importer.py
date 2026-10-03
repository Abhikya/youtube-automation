"""Historical import planning with opt-in execution; dry runs have no database imports."""
import argparse
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
from engine.learning.schema import ChannelProfile, GenerationRecord, PublishedVideo, ContentFeatures

SPECS = (
    ("channel_profile_candidates", "channel_profiles", ChannelProfile, "channel_context_id", "channel"),
    ("generation_candidates", "generation_records", GenerationRecord, "generation_id", "generation"),
    ("publication_candidates", "published_videos", PublishedVideo, "published_video_id", "publication"),
    ("content_feature_candidates", "content_features", ContentFeatures, "feature_record_id", "features"),
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_plan(preview, existing=None):
    """existing is an optional offline table->record-list inventory, never a connection.

    Without an inventory, INSERT is conditional on an empty destination. This
    function cannot establish the live database state and never executes a plan.
    """
    existing = {} if existing is None else existing
    channel = preview["channel_mapping"]["channel_context_id"]
    if preview["channel_mapping"]["display_name"] != "Minute Mysteries":
        raise ValueError("Expected owner-confirmed Minute Mysteries mapping")
    ids = preview["proposed_storage_identities"]
    rows, errors = [], []
    for source, table, model, id_field, identity_key in SPECS:
        candidates = preview.get(source, [])
        if len(candidates) != 1:
            errors.append(f"{source}: expected exactly one approved candidate")
        for raw in candidates:
            row = {"table": table, "action": "INVALID", "storage_id": raw.get(id_field),
                   "channel": channel, "source": raw.get("provenance", {}).get("source"),
                   "source_identity": raw.get("provenance", {}).get("source_record_id"),
                   "identity_semantics": "Proposed storage identity, not recovered historical identity",
                   "key_fields": {}, "dependencies": [], "conflict_status": "NONE"}
            rows.append(row)
            try:
                record = model.model_validate(raw).require_channel(channel)
                if getattr(record, id_field) != ids[identity_key]:
                    raise ValueError("Candidate differs from proposed storage identity")
                if not record.provenance.source or not record.provenance.source_record_id:
                    raise ValueError("Missing historical provenance")
                if model is ChannelProfile and record.display_name != "Minute Mysteries":
                    raise ValueError("Wrong channel display name")
                if model is GenerationRecord:
                    if record.title != "The Tunguska Enigma" or record.context_reference is not None:
                        raise ValueError("Only the approved Tunguska generation without learning context is allowed")
                if model is PublishedVideo:
                    if record.youtube_video_id != "iyrVp9tEMVw" or record.privacy_status != "private":
                        raise ValueError("Recorded Tunguska YouTube identity/private privacy must be preserved")
                    if record.generation_reference is None or record.generation_reference.record_id != ids["generation"]:
                        raise ValueError("Publication generation linkage mismatch")
                if model is ContentFeatures:
                    if record.generation_reference is None or record.generation_reference.record_id != ids["generation"]:
                        raise ValueError("Features generation linkage mismatch")
                    if record.video_reference is not None and record.video_reference.record_id != ids["publication"]:
                        raise ValueError("Features publication linkage mismatch")
                payload = record.model_dump(mode="json")
                row["record"] = payload
                row["key_fields"] = {k: payload[k] for k in (id_field, "display_name", "title", "youtube_video_id", "privacy_status", "generated_at", "extraction_version") if k in payload}
                if model is not ChannelProfile:
                    row["dependencies"].append({"table": "channel_profiles", "storage_id": channel})
                for key, target in (("generation_reference", "generation_records"), ("video_reference", "published_videos")):
                    if payload.get(key):
                        row["dependencies"].append({"table": target, "storage_id": payload[key]["record_id"]})
                matches = [v for v in existing.get(table, []) if v.get(id_field) == row["storage_id"]
                           or (model is PublishedVideo and v.get("youtube_video_id") == record.youtube_video_id)]
                if len(matches) > 1:
                    raise ValueError("Conflicting duplicate existing identities")
                if matches:
                    prior = model.model_validate(matches[0]).require_channel(channel)
                    if prior != record:
                        raise ValueError("Existing record conflicts with historical candidate; overwrite forbidden")
                    row["action"] = "SKIP_ALREADY_EXISTS"
                else:
                    row["action"] = "INSERT"
            except (ValidationError, ValueError, TypeError) as exc:
                row["action"] = "CONFLICT" if "existing" in str(exc).lower() or "different channel" in str(exc).lower() else "INVALID"
                row["conflict_status"] = str(exc)
                errors.append(f"{table}: {exc}")
    seen = {}
    for row in rows:
        key = (row["table"], row["storage_id"])
        if key in seen:
            for duplicate in (seen[key], row):
                duplicate.update(action="CONFLICT", conflict_status="Duplicate candidate storage identity")
            errors.append(f"{row['table']}: duplicate candidate identity")
        seen[key] = row
    for row in rows:
        for dependency in row["dependencies"]:
            parent = seen.get((dependency["table"], dependency["storage_id"]))
            if parent is None or parent["action"] in ("INVALID", "CONFLICT"):
                row.update(action="INVALID", conflict_status="Dependency missing or conflicted")
                errors.append(f"{row['table']}: dependency invalid")
    return {"mode": "DRY_RUN_ONLY", "live_database_contacted": False,
            "input_sha256": hashlib.sha256(canonical(preview).encode()).hexdigest(),
            "destination_assumption": "Provided offline inventory only; completeness not verified against Neon",
            "channel": channel, "plan_valid": not errors, "live_execution_supported": False,
            "rows": rows, "errors": errors,
            "excluded": {"metric_candidates": len(preview.get("metric_candidates", [])),
                         "learning_contexts": "ALL", "october_1_observations": "ALL",
                         "unresolved_sources": preview.get("unresolved_sources", []),
                         "skipped_sources": preview.get("skipped_sources", [])},
            "transaction_design": {"order": [s[1] for s in SPECS],
                                   "atomic": True, "executed": False,
                                   "policy": "Future authorized live import must recheck ownership/uniqueness and compare records within one transaction; insert only absent records; any conflict rolls back all inserts. Never upsert conflicting evidence."},
            "timestamp_policy": preview.get("candidate_timestamp_semantics"),
            "live_import_blockers": ["No live destination inspection; INSERT actions are conditional",
                                     "Preview administrative timestamps must be resolved before live import; no timestamps changed or invented here"]}



def _destination_plan(connection, preview, *, schema):
    """Read only the proposed identities, including globally unique YouTube IDs."""
    from psycopg import sql
    from psycopg.rows import dict_row
    from engine.learning.mapping import from_row
    inventory = {}
    for source, table, model, field, identity in SPECS:
        value = preview["proposed_storage_identities"][identity]
        clause = sql.SQL("{} = %s").format(sql.Identifier(field))
        values = [value]
        if model is PublishedVideo:
            clause += sql.SQL(" OR youtube_video_id = %s")
            values.append(preview[source][0]["youtube_video_id"])
        query = sql.SQL("SELECT * FROM {} WHERE {}").format(sql.Identifier(schema, table), clause)
        with connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(query, values).fetchall()
        # Reuse the existing adapter for PostgreSQL NUMERIC/timestamp handling.
        # Preserve actual ownership so build_plan rejects cross-channel matches.
        inventory[table] = [from_row(model, row, channel_context_id=row["channel_context_id"])
                            .model_dump(mode="json") for row in rows]
    return build_plan(preview, inventory)


def execute_import(preview, plan, connection, *, confirmed_channel, accept_administrative_timestamps=False,
                   schema="learning"):
    """Explicit execution on a borrowed idle connection; never opens a connection.

    Reports contain only approved identities and fixed status codes, never driver
    messages or record payloads. A successful receipt follows transaction commit.
    """
    report = {"inserted": [], "skipped_identical": [], "rejected_conflict": [],
              "failed": [], "transaction_result": "NOT_STARTED"}
    def identity(row):
        return {"table": row["table"], "storage_id": row["storage_id"]}
    try:
        expected = build_plan(preview)
        if plan != expected or not expected["plan_valid"]:
            report["rejected_conflict"] = [identity(r) for r in expected["rows"] if r["action"] == "CONFLICT"]
            report["failed"] = ["INVALID_OR_CHANGED_PLAN"]
            return report
        if confirmed_channel != expected["channel"] or not accept_administrative_timestamps:
            report["failed"] = ["EXPLICIT_CONFIRMATION_REQUIRED"]
            return report
        from psycopg import sql
        from psycopg.pq import TransactionStatus
        from engine.learning.repositories import LearningRepository
        if connection.info.transaction_status != TransactionStatus.IDLE:
            report["failed"] = ["IDLE_CONNECTION_REQUIRED"]
            return report
        # No writes, DDL, migrations or locks in this first destination inspection.
        with connection.transaction(force_rollback=True):
            connection.execute("SET TRANSACTION READ ONLY")
            checked = _destination_plan(connection, preview, schema=schema)
        if not checked["plan_valid"]:
            report["rejected_conflict"] = [identity(r) for r in checked["rows"] if r["action"] == "CONFLICT"]
            report["failed"] = ["DESTINATION_CONFLICT"]
            return report
        repository = LearningRepository(connection, expected["channel"], schema=schema)
        staged = []
        skipped = []
        with connection.transaction():
            # Serialize destination comparison and insertion against other writers.
            tables = sql.SQL(", ").join(sql.Identifier(schema, spec[1]) for spec in SPECS)
            connection.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(tables))
            checked = _destination_plan(connection, preview, schema=schema)
            if not checked["plan_valid"]:
                report["rejected_conflict"] = [identity(r) for r in checked["rows"] if r["action"] == "CONFLICT"]
                raise ValueError("destination conflict")
            methods = (repository.create_profile, repository.create_generation,
                       repository.create_published_video, repository.create_features)
            for row, spec, create in zip(checked["rows"], SPECS, methods):
                if row["action"] == "SKIP_ALREADY_EXISTS":
                    skipped.append(identity(row))
                else:
                    create(spec[2].model_validate(row["record"]))
                    staged.append(identity(row))
        report.update(inserted=staged, skipped_identical=skipped, transaction_result="COMMITTED")
    except Exception:
        # Includes preflight, constraint, write and commit failures; no exception text.
        report.update(inserted=[], skipped_identical=[], failed=["IMPORT_FAILED"],
                      transaction_result="ROLLED_BACK_OR_NOT_STARTED")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", default="phase2_5_learning_preview.json")
    parser.add_argument("--existing", help="Optional local JSON table inventory for idempotency/conflict simulation")
    parser.add_argument("--output", help="Local plan or execution receipt JSON output")
    parser.add_argument("--execute", action="store_true", help="Explicit live transactional import")
    parser.add_argument("--plan", default="phase2_6_import_plan.json")
    parser.add_argument("--confirm-channel")
    parser.add_argument("--accept-preview-administrative-timestamps", action="store_true")
    args = parser.parse_args()
    if args.execute:
        if args.existing or not args.confirm_channel or not args.accept_preview_administrative_timestamps:
            parser.error("Execution requires channel/timestamp confirmation and forbids offline inventory")
        try:
            preview = json.loads(Path(args.preview).read_text(encoding="utf-8"))
            saved = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            # Validate authorization and exact plan BEFORE opening the database.
            if saved != build_plan(preview) or not saved["plan_valid"] or args.confirm_channel != saved["channel"]:
                raise ValueError("invalid plan")
            from engine.learning.database import connect_database
            with connect_database() as connection:
                receipt = execute_import(preview, saved, connection, confirmed_channel=args.confirm_channel,
                                         accept_administrative_timestamps=True)
        except Exception:
            receipt = {"inserted": [], "skipped_identical": [], "rejected_conflict": [],
                       "failed": ["IMPORT_FAILED"], "transaction_result": "ROLLED_BACK_OR_NOT_STARTED"}
        rendered = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0 if receipt["transaction_result"] == "COMMITTED" else 1
    plan = build_plan(json.loads(Path(args.preview).read_text(encoding="utf-8")),
                      json.loads(Path(args.existing).read_text(encoding="utf-8")) if args.existing else None)
    rendered = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if plan["plan_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
