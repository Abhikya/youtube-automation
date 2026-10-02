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


if __name__ == "__main__":
    unittest.main()
