"""Offline import-plan tests; no services or production records are modified."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from engine.learning.historical_importer import build_plan


class HistoricalImporterTests(unittest.TestCase):
    def setUp(self):
        self.preview = json.loads((Path(__file__).parent / "phase2_5_learning_preview.json").read_text(encoding="utf-8"))

    def test_four_approved_records_and_exclusions(self):
        plan = build_plan(self.preview)
        self.assertTrue(plan["plan_valid"])
        self.assertEqual([r["table"] for r in plan["rows"]], ["channel_profiles", "generation_records", "published_videos", "content_features"])
        self.assertEqual([r["action"] for r in plan["rows"]], ["INSERT"] * 4)
        self.assertEqual(plan["excluded"]["metric_candidates"], 4)
        self.assertEqual(plan["excluded"]["learning_contexts"], "ALL")
        self.assertEqual(plan["excluded"]["unresolved_sources"], self.preview["unresolved_sources"])
        self.assertEqual(plan["excluded"]["october_1_observations"], "ALL")

    def test_proposed_ids_channel_and_linkage(self):
        rows = build_plan(self.preview)["rows"]
        ids = self.preview["proposed_storage_identities"]
        self.assertEqual([r["storage_id"] for r in rows], [ids[k] for k in ("channel", "generation", "publication", "features")])
        self.assertEqual(rows[0]["record"]["display_name"], "Minute Mysteries")
        self.assertEqual(rows[2]["record"]["generation_reference"]["record_id"], ids["generation"])
        self.assertEqual(rows[2]["record"]["privacy_status"], "private")

    def test_repeat_is_deterministic_and_nonmutating(self):
        before = copy.deepcopy(self.preview)
        self.assertEqual(build_plan(self.preview), build_plan(self.preview))
        self.assertEqual(before, self.preview)

    def test_existing_identical_records_are_skipped(self):
        existing = {r["table"]: [r["record"]] for r in build_plan(self.preview)["rows"]}
        plan = build_plan(self.preview, existing)
        self.assertTrue(plan["plan_valid"])
        self.assertEqual([r["action"] for r in plan["rows"]], ["SKIP_ALREADY_EXISTS"] * 4)

    def test_same_timestamp_in_utc_is_identical(self):
        from datetime import datetime, timezone
        existing = {r["table"]: [copy.deepcopy(r["record"])] for r in build_plan(self.preview)["rows"]}
        record = existing["generation_records"][0]
        record["generated_at"] = datetime.fromisoformat(record["generated_at"]).astimezone(timezone.utc).isoformat()
        self.assertTrue(build_plan(self.preview, existing)["plan_valid"])
        self.assertEqual(build_plan(self.preview, existing)["rows"][1]["action"], "SKIP_ALREADY_EXISTS")

    def test_duplicate_candidate_is_conflict(self):
        self.preview["generation_candidates"] *= 2
        plan = build_plan(self.preview)
        self.assertFalse(plan["plan_valid"])
        self.assertTrue(any(r["action"] == "CONFLICT" for r in plan["rows"]))

    def test_existing_conflict_never_overwritten(self):
        row = build_plan(self.preview)["rows"][1]
        changed = copy.deepcopy(row["record"])
        changed["script"] = "Conflicting script"
        plan = build_plan(self.preview, {row["table"]: [changed]})
        self.assertFalse(plan["plan_valid"])
        self.assertEqual(plan["rows"][1]["action"], "CONFLICT")
        self.assertEqual(plan["rows"][2]["action"], "INVALID")

    def test_foreign_channel_existing_record_rejected(self):
        row = build_plan(self.preview)["rows"][1]
        changed = copy.deepcopy(row["record"])
        changed["channel_context_id"] = "other-channel"
        self.assertFalse(build_plan(self.preview, {row["table"]: [changed]})["plan_valid"])

    def test_wrong_link_and_replaced_id_rejected(self):
        self.preview["publication_candidates"][0]["generation_reference"]["record_id"] = "wrong"
        self.assertFalse(build_plan(self.preview)["plan_valid"])
        self.preview["generation_candidates"][0]["generation_id"] = "random-replacement"
        self.assertFalse(build_plan(self.preview)["plan_valid"])

    def test_same_youtube_id_with_different_storage_identity_conflicts(self):
        row = build_plan(self.preview)["rows"][2]
        changed = copy.deepcopy(row["record"])
        changed["published_video_id"] = "different-storage-id"
        self.assertFalse(build_plan(self.preview, {row["table"]: [changed]})["plan_valid"])

    def test_no_database_or_network_dependencies(self):
        code = "import socket,sys; from unittest.mock import patch; guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')); guard.start(); import json; from pathlib import Path; from engine.learning.historical_importer import build_plan; p=build_plan(json.loads(Path('phase2_5_learning_preview.json').read_text(encoding='utf-8'))); assert p['plan_valid']; assert not any(n.startswith(('psycopg','engine.learning.database','engine.learning.repositories','config.settings','engine.uploader')) for n in sys.modules)"
        subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True)


class HistoricalExecutionTests(unittest.TestCase):
    def setUp(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from psycopg.pq import TransactionStatus
        from unittest.mock import Mock
        self.preview = json.loads(Path("phase2_5_learning_preview.json").read_text(encoding="utf-8"))
        self.plan = build_plan(self.preview)
        self.inventory = {}
        self.calls = []
        self.connection = Mock()
        self.connection.info.transaction_status = TransactionStatus.IDLE
        @contextmanager
        def transaction(**kwargs):
            before = copy.deepcopy(self.inventory)
            try:
                yield
            except Exception:
                self.inventory = before
                raise
            finally:
                if kwargs.get("force_rollback"):
                    self.inventory = before
        self.connection.transaction.side_effect = transaction
        self.fail_table = None
        self.repo = Mock()
        for method, spec in zip(("create_profile", "create_generation", "create_published_video", "create_features"),
                                __import__("engine.learning.historical_importer", fromlist=["SPECS"]).SPECS):
            def create(record, table=spec[1]):
                self.calls.append(table)
                if table == self.fail_table:
                    raise RuntimeError("password=secret-driver-value")
                self.inventory.setdefault(table, []).append(record.model_dump(mode="json"))
            getattr(self.repo, method).side_effect = create

    def execute(self):
        from unittest.mock import patch
        from engine.learning.historical_importer import execute_import
        with patch("engine.learning.historical_importer._destination_plan",
                   side_effect=lambda *a, **k: build_plan(self.preview, self.inventory)), \
             patch("engine.learning.repositories.LearningRepository", return_value=self.repo):
            return execute_import(self.preview, self.plan, self.connection,
                                  confirmed_channel=self.plan["channel"], accept_administrative_timestamps=True)

    def test_empty_destination_atomic_import_and_readonly_preflight(self):
        result = self.execute()
        self.assertEqual(result["transaction_result"], "COMMITTED")
        self.assertEqual(len(result["inserted"]), 4)
        self.assertEqual(self.calls, [r["table"] for r in self.plan["rows"]])
        self.assertEqual(self.connection.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))

    def test_actual_destination_queries_include_global_youtube_identity(self):
        from unittest.mock import MagicMock
        from engine.learning.historical_importer import _destination_plan
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.return_value.fetchall.return_value = []
        plan = _destination_plan(connection, self.preview, schema="learning")
        self.assertTrue(plan["plan_valid"])
        calls = cursor.execute.call_args_list
        self.assertEqual(len(calls), 4)
        query, values = calls[2].args
        self.assertIn('OR youtube_video_id = %s', query.as_string())
        self.assertEqual(values[1], "iyrVp9tEMVw")
        self.assertNotIn('channel_context_id =', query.as_string())

    def test_repeat_is_idempotent(self):
        self.execute()
        result = self.execute()
        self.assertEqual(len(result["skipped_identical"]), 4)
        self.assertEqual(result["inserted"], [])
        self.assertEqual(len(self.calls), 4)

    def test_identical_existing_record_skipped(self):
        row = self.plan["rows"][0]
        self.inventory[row["table"]] = [row["record"]]
        result = self.execute()
        self.assertEqual(len(result["inserted"]), 3)
        self.assertEqual(len(result["skipped_identical"]), 1)

    def test_conflict_rejected_without_writes(self):
        row = copy.deepcopy(self.plan["rows"][1])
        row["record"]["script"] = "conflicting evidence"
        self.inventory[row["table"]] = [row["record"]]
        result = self.execute()
        self.assertTrue(result["rejected_conflict"])
        self.assertEqual(self.calls, [])
        self.assertEqual(result["transaction_result"], "NOT_STARTED")

    def test_duplicate_candidate_rejected_before_connection(self):
        self.preview["generation_candidates"] *= 2
        self.assertTrue(self.execute()["failed"])
        self.connection.transaction.assert_not_called()

    def test_partial_failure_rolls_back_every_record_and_sanitizes(self):
        self.fail_table = "published_videos"
        result = self.execute()
        self.assertEqual(self.inventory, {})
        self.assertEqual(result["inserted"], [])
        self.assertTrue(result["failed"])
        self.assertNotIn("secret-driver-value", json.dumps(result))

    def test_foreign_channel_and_bad_reference_blocked(self):
        row = copy.deepcopy(self.plan["rows"][1])
        row["record"]["channel_context_id"] = "foreign-channel"
        self.inventory[row["table"]] = [row["record"]]
        self.assertTrue(self.execute()["rejected_conflict"])
        self.assertEqual(self.calls, [])
        self.preview["publication_candidates"][0]["generation_reference"]["record_id"] = "wrong"
        self.assertTrue(self.execute()["failed"])
        self.assertEqual(self.calls, [])

    def test_explicit_confirmation_required_and_plan_tampering_rejected(self):
        from engine.learning.historical_importer import execute_import
        result = execute_import(self.preview, self.plan, self.connection, confirmed_channel="wrong")
        self.assertTrue(result["failed"])
        self.connection.transaction.assert_not_called()
        self.plan["rows"][0]["record"]["identity"] = "changed"
        self.assertTrue(self.execute()["failed"])

    def test_destination_changes_after_preflight_rechecked_before_insert(self):
        from unittest.mock import patch
        from engine.learning.historical_importer import execute_import
        changed = copy.deepcopy(self.plan)
        changed["plan_valid"] = False
        changed["rows"][0]["action"] = "CONFLICT"
        with patch("engine.learning.historical_importer._destination_plan", side_effect=[self.plan, changed]):
            result = execute_import(self.preview, self.plan, self.connection,
                                   confirmed_channel=self.plan["channel"], accept_administrative_timestamps=True)
        self.assertTrue(result["rejected_conflict"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
