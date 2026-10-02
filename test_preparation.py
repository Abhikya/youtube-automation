"""Offline preparation tests: fake artifacts/YouTube/Telegram, no account calls."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from filelock import FileLock, Timeout
from test_actions import Store
from run_actions import ProductionError, cleanup_production, validate_state
from engine.scheduling.preparation import Preparation, target_day, publication_time, MAX_GENERATION_ATTEMPTS
from engine.uploader import youtube_uploader as youtube

TZ = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 3)
NOW = datetime(2026, 10, 2, 9, 5, tzinfo=TZ)
CHANNEL = "minute-mysteries-channel"


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store()
        self.controller = Preparation(self.store, DAY, self.root)
        self.count = 0
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"YOUTUBE_PRIVACY_STATUS": "private"}).start()
        self.upload = Mock(side_effect=self.uploaded)
        self.verify = Mock(side_effect=self.verified)
        self.notify = Mock(return_value={"success": True})

    def generate(self):
        self.count += 1
        path = self.controller.output / f"unsolved_mysteries_2026100209{self.count:02d}00.mp4"
        path.write_bytes(f"media-{self.count}".encode())
        return {"video_path": str(path), "metadata": {"Title": "Mystery", "Description": "Saved description", "Hashtags": ["#Mystery"]},
                "idea": "Idea", "script": "Script"}

    def uploaded(self, path, metadata, publish_at):
        state = json.loads(self.store.latest(DAY)["bytes"])
        slot = next(v for v in state["queue"]["videos"] if publication_time(DAY, v["slot"]) == publish_at)
        self.assertEqual(slot["upload_status"], "uploading")
        return {"success": True, "status": "uploaded", "video_id": "id-" + slot["slot"],
                "privacy_status": "private", "video_url": "https://example.invalid/video"}

    def verified(self, video_id, channel, publish_at):
        saved = json.loads(self.store.latest(DAY)["bytes"])["queue"]
        self.assertTrue(any((v.get("youtube") or {}).get("video_id") == video_id for v in saved["videos"]))
        return {"verified": True, "video_id": video_id, "channel_id": channel,
                "privacy_status": "private", "publish_at": publish_at, "processing_status": "succeeded"}

    def run_prepare(self, generator=None, controller=None):
        return (controller or self.controller).prepare(expected_channel=CHANNEL, generator=generator or self.generate,
             uploader=self.upload, verifier=self.verify, notifier=self.notify, now=lambda: NOW)

    def fresh(self):
        self.store.run_id += 1
        path = self.root / f"runner-{self.store.run_id}"
        path.mkdir()
        self.controller = Preparation(self.store, DAY, path)
        return self.controller

    def test_logs_show_target_and_durable_upload_status(self):
        with self.assertLogs("engine.scheduling.preparation", level="INFO") as captured:
            self.run_prepare()
        lines = [line for line in captured.output if "action=verify_existing_id" in line]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn("execution_date=2026-10-02", line)
            self.assertIn("target_publication_date=2026-10-03", line)
            self.assertIn("batch=daily_queue_2026-10-03.json", line)
            self.assertIn("upload_status=uploaded", line)
            self.assertIn("youtube_id=id-", line)

    def test_stale_after_render_preserves_newer_head_and_does_not_regenerate(self):
        original=self.controller.checkpoint
        injected=[False]
        def checkpoint(day,data):
            if not injected[0] and data["videos"][0]["generation_status"] == "ready":
                injected[0]=True
                root=self.root / "other-writer";root.mkdir()
                other=Preparation(self.store,DAY,root)
                other.load();other.queue.checkpoint=other.checkpoint
                self.controller.memory.parent.mkdir(exist_ok=True)
                self.controller.memory.write_text(json.dumps({"topics":["local-completed"]}))
                other.memory.write_text(json.dumps({"topics":["newer"]}))
                other.queue.update(DAY,lambda saved:{**saved,"newer_marker":"preserve"})
            return original(day,data)
        with patch.object(self.controller,"checkpoint",side_effect=checkpoint):
            result=self.run_prepare()
        self.assertEqual(result["status"],"READY")
        self.assertEqual(self.count,3)
        self.assertEqual(self.upload.call_count,3)
        self.assertEqual(self.controller.state["queue"]["newer_marker"],"preserve")
        self.assertEqual(self.controller.state["idea_memory"]["topics"],["newer","local-completed"])
        self.assertEqual(len([a for a in self.store.saved if a["name"].startswith("mystery-media-")]),3)

    def test_stale_upload_claim_reloads_without_inserting_or_stranding_slot(self):
        self.auth_pending()
        original=self.controller.checkpoint
        injected=[False]
        def checkpoint(day,data):
            if not injected[0] and data["videos"][0]["upload_status"] == "uploading":
                injected[0]=True
                root=self.root / "other-writer";root.mkdir()
                other=Preparation(self.store,DAY,root)
                other.load();other.queue.checkpoint=other.checkpoint
                other.queue.update(DAY,lambda saved:{**saved,"newer_marker":"claim-conflict"})
            return original(day,data)
        with patch.object(self.controller,"checkpoint",side_effect=checkpoint):
            result=self.run_prepare(Mock(side_effect=AssertionError("No regeneration")))
        self.assertEqual(result["status"],"READY")
        self.assertEqual(self.upload.call_count,3)
        self.assertEqual(self.count,3)
        self.assertEqual(self.controller.state["queue"]["newer_marker"],"claim-conflict")

    def test_stale_uploaded_id_outcome_is_preserved_without_second_insertion(self):
        original=self.controller.checkpoint
        injected=[False]
        def checkpoint(day,data):
            if not injected[0] and data["videos"][0]["upload_status"] == "uploaded":
                injected[0]=True
                root=self.root / "other-writer";root.mkdir()
                other=Preparation(self.store,DAY,root)
                other.load();other.queue.checkpoint=other.checkpoint
                other.queue.update(DAY,lambda saved:{**saved,"newer_marker":"after-insert"})
            return original(day,data)
        with patch.object(self.controller,"checkpoint",side_effect=checkpoint):
            result=self.run_prepare()
        self.assertEqual(result["status"],"READY")
        self.assertEqual(self.upload.call_count,3)
        self.assertEqual(self.count,3)
        self.assertEqual(self.controller.state["queue"]["videos"][0]["youtube"]["video_id"],"id-10:00")
        self.assertEqual(self.controller.state["queue"]["newer_marker"],"after-insert")

    def test_newer_checkpoint_confirmed_id_is_never_reuploaded_on_stale_claim(self):
        self.auth_pending()
        original=self.controller.checkpoint
        injected=[False]
        def checkpoint(day,data):
            if not injected[0] and data["videos"][0]["upload_status"] == "uploading":
                injected[0]=True
                root=self.root / "other-writer";root.mkdir()
                other=Preparation(self.store,DAY,root)
                other.load();other.queue.checkpoint=other.checkpoint
                other.queue._set(DAY,"10:00",{"upload_status":"uploaded","youtube":
                    {"success":True,"status":"uploaded","video_id":"newer-confirmed-id","privacy_status":"private"}})
            return original(day,data)
        with patch.object(self.controller,"checkpoint",side_effect=checkpoint):
            result=self.run_prepare()
        self.assertEqual(result["status"],"READY")
        self.assertEqual(self.upload.call_count,2)
        self.assertEqual(self.controller.state["queue"]["videos"][0]["youtube"]["video_id"],"newer-confirmed-id")
        self.assertEqual(self.count,3)

    def test_newer_uncertain_claim_remains_protected_after_stale_conflict(self):
        self.auth_pending()
        original=self.controller.checkpoint
        injected=[False]
        def checkpoint(day,data):
            if not injected[0] and data["videos"][0]["upload_status"] == "uploading":
                injected[0]=True
                root=self.root / "other-writer";root.mkdir()
                other=Preparation(self.store,DAY,root)
                other.load();other.queue.checkpoint=other.checkpoint
                other.queue._set(DAY,"10:00",{"upload_status":"uncertain","youtube":None})
            return original(day,data)
        with patch.object(self.controller,"checkpoint",side_effect=checkpoint):
            result=self.run_prepare()
        self.assertEqual(result["status"],"PREPARING")
        self.assertEqual(self.upload.call_count,2)
        self.assertEqual(self.controller.state["queue"]["videos"][0]["upload_status"],"uncertain")
        self.assertEqual(self.count,3)

    def test_new_batch_three_private_scheduled_verified_ready(self):
        result = self.run_prepare()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(self.count, 3)
        self.assertEqual(self.upload.call_count, 3)
        self.assertEqual(self.verify.call_count, 3)
        state = json.loads(self.store.latest(DAY)["bytes"])
        validate_state(state, DAY)
        self.assertEqual(state["queue"]["batch_status"], "READY")
        self.assertEqual([c.args[2] for c in self.upload.call_args_list], [publication_time(DAY,s) for s in ("10:00","14:00","19:00")])
        self.assertTrue(self.notify.call_args.args[0]["batch_notice"])

    def test_ready_no_op_even_after_media_cleanup(self):
        self.run_prepare()
        for media in [a for a in self.store.saved if a["name"].startswith("mystery-media-")]:
            self.store.delete(media["id"])
        self.upload.reset_mock(); self.verify.reset_mock(); self.notify.reset_mock()
        generation = Mock(side_effect=AssertionError("No generation"))
        before = len(self.store.saved)
        result = self.run_prepare(generation, self.fresh())
        self.assertTrue(result["no_op"])
        self.assertEqual(len(self.store.saved), before)
        generation.assert_not_called(); self.upload.assert_not_called(); self.verify.assert_not_called(); self.notify.assert_not_called()

    def test_partial_first_complete_second_failed_third_missing_recovers(self):
        result = self.run_prepare(Mock(side_effect=[self.generate(), RuntimeError("slot2"), RuntimeError("slot3")]))
        self.assertEqual(result["status"], "PREPARING")
        self.assertEqual(self.upload.call_count, 1)
        self.run_prepare(controller=self.fresh())
        self.assertEqual(self.upload.call_count, 3)
        self.assertEqual(self.verify.call_count, 3)
        items = self.controller.queue.read(DAY)["videos"]
        self.assertEqual([v["generation_attempts"] for v in items], [1,2,2])
        self.assertEqual(items[1]["generation_attempt_history"][1]["previous_status"], "generation_failed")

    def test_generation_retry_budget_survives_restarts(self):
        failed = Mock(side_effect=RuntimeError("generation"))
        for _ in range(MAX_GENERATION_ATTEMPTS + 2):
            self.run_prepare(failed, self.fresh())
        self.assertEqual(failed.call_count, 3 * MAX_GENERATION_ATTEMPTS)
        self.upload.assert_not_called()
        self.assertTrue(all(len(v["generation_attempt_history"]) == 5 for v in self.controller.queue.read(DAY)["videos"]))

    def test_uncertain_upload_never_reuploaded_other_slots_preserved(self):
        self.upload.side_effect = [TimeoutError("lost response"), self.uploaded, self.uploaded]
        # Mock side effects containing functions do not invoke them; use explicit callback.
        def upload(path, metadata, stamp):
            if stamp == publication_time(DAY,"10:00"):
                raise TimeoutError("lost response")
            return self.uploaded(path, metadata, stamp)
        self.upload.side_effect = upload
        self.assertEqual(self.run_prepare()["status"], "PREPARING")
        calls = self.upload.call_count
        self.run_prepare(controller=self.fresh())
        self.assertEqual(self.upload.call_count, calls)
        self.assertEqual(self.controller.queue.read(DAY)["videos"][0]["upload_status"], "uncertain")

    def test_known_id_processing_retry_does_not_upload(self):
        self.verify.return_value = None
        def processing(video_id, channel, stamp):
            result = self.verified(video_id,channel,stamp)
            result.update(verified=False, processing_status="processing")
            return result
        self.verify.side_effect = processing
        self.assertEqual(self.run_prepare()["status"], "PREPARING")
        self.verify.side_effect = self.verified
        self.assertEqual(self.run_prepare(controller=self.fresh())["status"], "READY")
        self.assertEqual(self.upload.call_count, 3)
        self.assertEqual(self.count, 3)

    def test_cli_processing_only_is_success_with_ids_preserved_and_no_second_upload(self):
        import io
        import run_actions
        ids={"10:00":"wYud-PDHqSk","14:00":"_11Kh8-88Mk","19:00":"T-II7Hvqpz0"}
        def upload(path,metadata,stamp):
            value=self.uploaded(path,metadata,stamp)
            value["video_id"]=next(vid for slot,vid in ids.items() if publication_time(DAY,slot)==stamp)
            return value
        self.upload.side_effect=upload
        def processing(vid,channel,stamp):
            value=self.verified(vid,channel,stamp)
            value.update(verified=False,processing_status="processing")
            return value
        self.verify.side_effect=processing
        first=self.run_prepare()
        self.assertTrue(first["verification_pending"])
        self.fresh();self.upload.reset_mock()
        env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main","YOUTUBE_CHANNEL_ID":CHANNEL}
        def invoke():
            with patch.dict(os.environ,env,clear=True),patch("sys.argv",["run_actions.py","prepare","--day",str(DAY)]), \
                 patch.object(run_actions,"GitHubArtifacts",return_value=self.store), \
                 patch("engine.scheduling.preparation.Preparation",return_value=self.controller), \
                 patch.object(run_actions,"india_now",return_value=NOW),patch("engine.scheduling.preparation.india_now",return_value=NOW), \
                 patch.object(run_actions,"logging"),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
                 patch.object(run_actions,"restore_oauth"),patch.object(youtube,"preparation_preflight"), \
                 patch.object(youtube,"upload_prepared_video",self.upload),patch.object(youtube,"verify_prepared_video",self.verify), \
                 patch("engine.approval.telegram_bot.notify_generated_video",self.notify), \
                 patch("engine.scheduling.daily_queue.generate_only",side_effect=AssertionError("No generation")), \
                 patch.object(run_actions,"cleanup_production",return_value={}) as cleanup,patch("sys.stdout",io.StringIO()):
                code=run_actions.main()
                if self.controller.state["queue"]["batch_status"] != "READY":
                    cleanup.assert_not_called()
                return code
        self.assertEqual(invoke(),0)
        for v in self.controller.queue.read(DAY)["videos"]:
            self.assertEqual(v["upload_status"],"uploaded")
            self.assertEqual(v["youtube"]["video_id"],ids[v["slot"]])
            self.assertEqual(v["preparation_verification"]["verification_status"],"pending")
        for state in ("failed","terminated"):
            def failed(vid,channel,stamp):
                value=self.verified(vid,channel,stamp)
                value.update(verified=False,processing_status=state)
                return value
            self.verify.side_effect=failed
            self.assertEqual(invoke(),1)
        self.verify.side_effect=self.verified
        self.assertEqual(invoke(),0)
        self.assertEqual(self.controller.queue.read(DAY)["batch_status"],"READY")
        self.upload.assert_not_called()
        self.assertEqual(self.count,3)

    def test_processing_does_not_hide_wrong_channel_or_elapsed_publish_at(self):
        self.verify.side_effect=lambda vid,ch,stamp:{"verified":False,"video_id":vid,"channel_id":"wrong",
            "privacy_status":"private","publish_at":stamp,"processing_status":"processing"}
        result=self.run_prepare()
        self.assertFalse(result["verification_pending"])
        self.fresh()
        result=self.controller.prepare(expected_channel=CHANNEL,uploader=self.upload,verifier=self.verify,
            notifier=self.notify,allow_current_day=True,now=lambda:datetime(2026,10,3,19,tzinfo=TZ))
        self.assertFalse(result["verification_pending"])
        self.assertTrue(all(p["reason"] == "publish_at_not_future" for p in result["problems"]))

    def test_telegram_failure_does_not_revoke_ready(self):
        self.notify.side_effect = TimeoutError("notification")
        self.assertEqual(self.run_prepare()["status"], "READY")
        self.assertFalse(self.controller.queue.read(DAY)["batch_telegram"]["success"])
        self.assertTrue(self.run_prepare(controller=self.fresh())["no_op"])
        self.assertEqual(self.upload.call_count, 3)

    def test_ready_rejects_duplicate_ids_and_unverified_slot(self):
        self.run_prepare()
        state = copy.deepcopy(self.controller.state)
        state["queue"]["videos"][2]["preparation_verification"]["verified"] = False
        with self.assertRaises(ProductionError): validate_state(state, DAY)
        state = copy.deepcopy(self.controller.state)
        state["queue"]["videos"][2]["youtube"]["video_id"] = state["queue"]["videos"][0]["youtube"]["video_id"]
        state["queue"]["videos"][2]["preparation_verification"]["video_id"] = state["queue"]["videos"][0]["youtube"]["video_id"]
        with self.assertRaises(ProductionError): validate_state(state,DAY)

    def test_wrong_channel_rejected_before_work(self):
        self.run_prepare()
        controller = self.fresh()
        with self.assertRaises(ProductionError):
            controller.prepare(expected_channel="wrong", generator=Mock(), now=lambda:NOW)
        self.assertEqual(self.upload.call_count,3)

    def test_local_preparation_lock_prevents_concurrent_mutation(self):
        with FileLock(str(self.controller.scratch / "preparation.lock"), timeout=0):
            # A different thread represents a second local caller; FileLock is otherwise reentrant.
            import threading
            failures=[]
            def attempt():
                try: self.run_prepare()
                except Timeout: failures.append(True)
            thread=threading.Thread(target=attempt);thread.start();thread.join(5)
            self.assertEqual(failures,[True])
        self.upload.assert_not_called()

    def test_cleanup_protects_target_and_current_date_and_age(self):
        self.run_prepare()
        cleanup_production(self.store,self.root,NOW + timedelta(hours=25))
        self.assertEqual(self.store.deleted,[])  # target is now the current publication date
        cleanup_production(self.store,self.root,NOW + timedelta(days=2))
        self.assertEqual(len(self.store.deleted),3)
        self.assertEqual(self.controller.state["queue"]["batch_status"],"READY")

    def test_unready_batch_is_not_cleaned_when_old(self):
        self.verify.side_effect=lambda *args:{"verified":False}
        self.run_prepare()
        cleanup_production(self.store,self.root,NOW + timedelta(days=2))
        self.assertEqual(self.store.deleted,[])

    def test_cross_midnight_stops_without_relabeling(self):
        with self.assertRaises(ProductionError):
            self.controller.prepare(expected_channel=CHANNEL, generator=Mock(), now=lambda:NOW+timedelta(days=1))
        self.assertIsNone(self.store.latest(DAY))

    def test_claim_checkpoint_failure_does_not_call_upload(self):
        self.verify.side_effect=lambda *args:{"verified":False}
        self.upload.side_effect=TimeoutError("uncertain")
        # First create ready renders with protected claims, then an explicit fixture resets a claim.
        self.run_prepare()
        c=self.fresh();c.load();c.queue.checkpoint=c.checkpoint
        c.queue._set(DAY,"10:00",{"upload_status":"pending","youtube":None})
        self.upload.reset_mock()
        original=self.store.upload
        def save(name,path,retention):
            if Path(path).name == "state.json":
                data=json.loads(Path(path).read_text())
                if data["queue"]["videos"][0]["upload_status"] == "uploading":
                    raise RuntimeError("claim checkpoint failed")
            return original(name,path,retention)
        with patch.object(self.store,"upload",side_effect=save):
            with self.assertRaises(Exception):self.run_prepare(controller=c)
        self.upload.assert_not_called()

    def test_id_checkpoint_failure_preserves_claim_and_prevents_verify(self):
        original=self.store.upload
        def save(name,path,retention):
            if Path(path).name == "state.json":
                data=json.loads(Path(path).read_text())
                if data["queue"]["videos"][0]["upload_status"] == "uploaded":
                    raise RuntimeError("ID checkpoint failed")
            return original(name,path,retention)
        with patch.object(self.store,"upload",side_effect=save):
            with self.assertRaises(Exception):self.run_prepare()
        self.verify.assert_not_called()
        state=json.loads(self.store.latest(DAY)["bytes"])
        self.assertEqual(state["queue"]["videos"][0]["upload_status"],"uploading")
        calls=self.upload.call_count
        self.run_prepare(controller=self.fresh())
        self.assertEqual(self.upload.call_count,calls+2) # only the untouched second and third slots

    def test_wrong_verification_cannot_mark_ready(self):
        self.verify.side_effect=lambda video_id,channel,stamp:{"verified":True,"video_id":video_id,
            "channel_id":"wrong","privacy_status":"private","publish_at":stamp,"processing_status":"succeeded"}
        self.assertEqual(self.run_prepare()["status"],"PREPARING")
        self.assertEqual(self.controller.queue.read(DAY)["batch_status"],"PREPARING")

    def auth_pending(self, generator=None):
        return self.controller.prepare(expected_channel=CHANNEL, generator=generator or self.generate,
            uploader=self.upload, verifier=self.verify, notifier=self.notify, now=lambda: NOW,
            authenticate=Mock(side_effect=RuntimeError("secret must not appear")))

    def test_auth_failure_after_generation_preserves_all_pending_artifacts(self):
        result = self.auth_pending()
        self.assertEqual(result["generation_status"], "COMPLETE")
        self.assertEqual(result["upload_status"], "INCOMPLETE")
        self.assertTrue(result["recoverable"])
        self.assertEqual(result["target_date"], str(DAY))
        self.assertEqual(self.count, 3)
        self.upload.assert_not_called()
        self.verify.assert_not_called()
        for item in self.controller.queue.read(DAY)["videos"]:
            self.assertEqual(item["upload_status"], "pending")
            self.assertTrue(Path(item["video_path"]).is_file())
        self.assertNotIn("secret must not appear", json.dumps(result))
        notice = self.notify.call_args.args[0]["preparation"]
        self.assertEqual(notice["generation_status"], "COMPLETE")

    def test_auth_fixed_fresh_runner_reuses_fingerprints_and_never_calls_generation(self):
        self.auth_pending()
        before = copy.deepcopy(self.controller.state["queue"]["videos"])
        render = Mock(side_effect=AssertionError("Must not regenerate"))
        self.assertEqual(self.run_prepare(render, self.fresh())["status"], "READY")
        render.assert_not_called()
        after = self.controller.queue.read(DAY)["videos"]
        for old, new in zip(before, after):
            self.assertEqual({k:v for k,v in old["generated_files"][0].items() if k != "path"},
                             {k:v for k,v in new["generated_files"][0].items() if k != "path"})
        self.assertEqual([v["generated_at"] for v in before], [v["generated_at"] for v in after])
        self.assertEqual(self.upload.call_count, 3)

    def test_only_missing_third_render_is_generated(self):
        gen = Mock(side_effect=[self.generate(), self.generate(), RuntimeError("third missing")])
        self.auth_pending(gen)
        missing = Mock(side_effect=self.generate)
        self.run_prepare(missing, self.fresh())
        self.assertEqual(missing.call_count, 1)
        self.assertEqual([v["generation_attempts"] for v in self.controller.queue.read(DAY)["videos"]], [1, 1, 2])

    def test_one_uploaded_two_generated_resume_without_replacing_first(self):
        def upload(path, metadata, stamp):
            if stamp != publication_time(DAY, "10:00"):
                raise youtube.UploadNotStarted("auth unavailable")
            return self.uploaded(path, metadata, stamp)
        self.upload.side_effect = upload
        self.run_prepare()
        first = copy.deepcopy(self.controller.state["queue"]["videos"][0])
        self.upload.reset_mock(); self.upload.side_effect = self.uploaded
        render = Mock(side_effect=AssertionError("Do not regenerate"))
        self.run_prepare(render, self.fresh())
        self.assertEqual(self.upload.call_count, 2)
        restored = copy.deepcopy(self.controller.queue.read(DAY)["videos"][0])
        first["video_path"] = restored["video_path"]
        first["generated_files"][0]["path"] = restored["generated_files"][0]["path"]
        self.assertEqual(restored, first)
        render.assert_not_called()

    def test_existing_current_day_before_first_publication_resumes_exact_target(self):
        self.auth_pending()
        controller = self.fresh()
        render = Mock(side_effect=AssertionError("Do not regenerate"))
        result = controller.prepare(expected_channel=CHANNEL, generator=render, uploader=self.upload,
            verifier=self.verify, notifier=self.notify, now=lambda: datetime(2026,10,3,9,tzinfo=TZ),
            allow_current_day=True)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["target_date"], str(DAY))
        render.assert_not_called()
        self.assertEqual([c.args[2] for c in self.upload.call_args_list],
                         [publication_time(DAY,s) for s in ("10:00","14:00","19:00")])

    def test_current_day_elapsed_slot_is_blocked_without_claim_remaining_slots_resume(self):
        self.auth_pending()
        controller = self.fresh()
        result = controller.prepare(expected_channel=CHANNEL, generator=Mock(), uploader=self.upload,
            verifier=self.verify, notifier=self.notify, now=lambda: datetime(2026,10,3,10,tzinfo=TZ),
            allow_current_day=True)
        self.assertEqual(result["status"], "PREPARING")
        self.assertIn({"slot":"10:00","reason":"publish_at_not_future"}, result["problems"])
        self.assertEqual(self.upload.call_count, 2)
        self.assertEqual(controller.queue.read(DAY)["videos"][0]["upload_status"], "pending")

    def test_recovery_after_first_verified_slot_can_be_ready_before_remaining_slots(self):
        def first_only(path, metadata, stamp):
            if stamp != publication_time(DAY, "10:00"):
                raise youtube.UploadNotStarted()
            return self.uploaded(path, metadata, stamp)
        self.upload.side_effect = first_only
        self.run_prepare()
        self.upload.side_effect = self.uploaded
        result = self.fresh().prepare(expected_channel=CHANNEL, generator=Mock(), uploader=self.upload,
            verifier=self.verify, notifier=self.notify, now=lambda: datetime(2026,10,3,11,tzinfo=TZ),
            allow_current_day=True)
        self.assertEqual(result["status"], "READY")
        validate_state(self.controller.state, DAY)

    def test_existing_current_day_requires_explicit_recovery(self):
        self.auth_pending()
        with self.assertRaises(ProductionError):
            self.fresh().prepare(expected_channel=CHANNEL, now=lambda:NOW+timedelta(days=1))
        self.upload.assert_not_called()

    def test_new_current_day_and_old_batch_are_rejected(self):
        for now in (NOW+timedelta(days=1), NOW+timedelta(days=2)):
            with self.assertRaises(ProductionError):
                self.controller.prepare(expected_channel=CHANNEL, generator=Mock(), now=lambda:now,
                                        allow_current_day=True)
        self.assertIsNone(self.store.latest(DAY))

    def test_midnight_crossing_keeps_target_and_reuses_generated_media(self):
        self.auth_pending()
        moments = iter([datetime(2026,10,2,23,59,tzinfo=TZ)] * 2)
        def clock():
            return next(moments, datetime(2026,10,3,0,1,tzinfo=TZ))
        render = Mock(side_effect=AssertionError("No regeneration"))
        result = self.fresh().prepare(expected_channel=CHANNEL, generator=render, uploader=self.upload,
            verifier=self.verify, notifier=self.notify, now=clock)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["target_date"], str(DAY))
        self.assertIsNone(self.store.latest(DAY+timedelta(days=1)))
        render.assert_not_called()

    def test_current_day_missing_elapsed_render_is_never_generated(self):
        self.auth_pending(Mock(side_effect=RuntimeError("no renders")))
        generator = Mock(side_effect=self.generate)
        result = self.fresh().prepare(expected_channel=CHANNEL, generator=generator, uploader=self.upload,
            verifier=self.verify, notifier=self.notify, now=lambda:datetime(2026,10,3,14,tzinfo=TZ),
            allow_current_day=True)
        self.assertEqual(generator.call_count, 1)
        self.assertEqual(self.upload.call_count, 1)
        self.assertEqual(result["slots"][2]["slot"], "19:00")

    def test_default_oct_three_targets_oct_four_and_ignores_historical_media(self):
        self.auth_pending()
        old_snapshot = copy.deepcopy(self.store.latest(DAY))
        tomorrow = target_day(datetime(2026,10,3,9,tzinfo=TZ))
        self.assertEqual(tomorrow, DAY+timedelta(days=1))
        next_root = self.root / "next-day"
        next_root.mkdir()
        controller = Preparation(self.store, tomorrow, next_root)
        def generated():
            path = controller.output / f"unsolved_mysteries_2026100309{self.count:02d}00.mp4"
            self.count += 1
            path.write_bytes(b"next day render")
            return {"video_path":str(path), "metadata":{"Title":"New", "Description":"New", "Hashtags":[]}}
        uploaded = Mock(side_effect=lambda p,m,t:{"success":True,"video_id":"new-"+t,"status":"uploaded"})
        verified = lambda vid,ch,t:{"verified":True,"video_id":vid,"channel_id":ch,"privacy_status":"private",
                                   "processing_status":"succeeded","publish_at":t}
        result = controller.prepare(expected_channel=CHANNEL, generator=generated, uploader=uploaded,
            verifier=verified, notifier=self.notify, now=lambda:datetime(2026,10,3,9,tzinfo=TZ))
        self.assertEqual(result["status"], "READY")
        self.assertEqual(self.store.latest(DAY), old_snapshot)
        self.assertTrue(all(c.args[2].startswith("2026-10-04") for c in uploaded.call_args_list))

    def test_restore_corrupt_generated_artifact_blocks_upload_without_regeneration(self):
        self.auth_pending()
        media = next(a for a in self.store.saved if a["name"].startswith("mystery-media-"))
        media["bytes"] = b"corrupt"
        render = Mock()
        with self.assertRaises(ProductionError): self.run_prepare(render, self.fresh())
        render.assert_not_called()
        self.upload.assert_not_called()

    def test_default_cron_after_midnight_resumes_existing_current_day_before_expiry(self):
        """A fresh cron must recover safe pending slots as well as prepare tomorrow."""
        import io
        import run_actions
        self.auth_pending()  # Oct 2 renders for Oct 3, no upload claims or IDs.
        recovery = self.fresh()
        current = datetime(2026, 10, 3, 9, 5, tzinfo=TZ)
        tomorrow = DAY + timedelta(days=1)
        primary = Mock()
        primary.prepare.return_value = {"status": "READY", "target_date": str(tomorrow), "no_op": True}
        def factory(store, day):
            self.assertIs(store, self.store)
            if day == DAY:
                return recovery
            self.assertEqual(day, tomorrow)
            return primary
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main", "PRODUCTION_BRANCH": "main",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "YOUTUBE_CHANNEL_ID": CHANNEL}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", ["run_actions.py", "prepare"]), \
             patch.object(run_actions, "GitHubArtifacts", return_value=self.store), \
             patch("engine.scheduling.preparation.Preparation", side_effect=factory), \
             patch.object(run_actions, "india_now", return_value=current), \
             patch("engine.scheduling.preparation.india_now", return_value=current), \
             patch.object(run_actions, "logging"), patch.object(run_actions, "YOUTUBE_PRIVACY_STATUS", "private"), \
             patch.object(run_actions, "restore_oauth"), patch.object(youtube, "preparation_preflight"), \
             patch.object(youtube, "upload_prepared_video", self.upload), \
             patch.object(youtube, "verify_prepared_video", self.verify), \
             patch("engine.approval.telegram_bot.notify_generated_video", self.notify), \
             patch("engine.scheduling.daily_queue.generate_only", side_effect=AssertionError("Completed renders must be reused")), \
             patch.object(run_actions, "cleanup_production", return_value={}), patch("sys.stdout", io.StringIO()):
            run_actions.main()
        saved = json.loads(self.store.latest(DAY)["bytes"])["queue"]
        self.assertEqual(saved["batch_status"], "READY",
                         "Default cron abandoned the existing current-day batch despite future publication slots")
        self.assertEqual(self.upload.call_count, 3)
        self.assertEqual(self.count, 3)
        primary.prepare.assert_called_once()

    def test_exact_oct_three_studio_reconciliation_retries_only_fourteen(self):
        def uploaded(path, metadata, stamp):
            if stamp == publication_time(DAY,"14:00"):
                raise TimeoutError("lost outcome")
            result = self.uploaded(path,metadata,stamp)
            result["video_id"] = "wYud-PDHqSk" if stamp == publication_time(DAY,"10:00") else "T-II7Hvqpz0"
            return result
        self.upload.side_effect = uploaded
        self.run_prepare()
        self.controller.queue._set(DAY,"14:00",{"upload_status":"uploading","youtube":None})
        self.fresh()
        self.controller.reconcile_not_uploaded("14:00",expected_channel=CHANNEL,now=lambda:NOW)
        saved=self.controller.queue.read(DAY)["videos"][1]
        self.assertEqual(saved["upload_status"],"pending")
        self.assertEqual(saved["upload_reconciliation_history"][0]["previous_upload_status"],"uploading")
        self.upload.reset_mock();self.upload.side_effect=self.uploaded
        generation=Mock(side_effect=AssertionError("Do not regenerate"))
        self.assertEqual(self.run_prepare(generation,self.fresh())["status"],"READY")
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.args[2],publication_time(DAY,"14:00"))
        generation.assert_not_called()
        self.assertEqual([v["youtube"]["video_id"] for v in self.controller.queue.read(DAY)["videos"]],
                         ["wYud-PDHqSk","id-14:00","T-II7Hvqpz0"])
        for slot in ("10:00","19:00"):
            with self.assertRaises(ProductionError):
                self.controller.reconcile_not_uploaded(slot,expected_channel=CHANNEL,now=lambda:NOW)

    def test_cli_explicit_fourteen_input_reaches_reconciliation_and_reuses_exact_render(self):
        import io
        import run_actions
        def uploaded(path, metadata, stamp):
            if stamp == publication_time(DAY,"14:00"):
                raise TimeoutError("unresolved")
            result=self.uploaded(path,metadata,stamp)
            result["video_id"]="wYud-PDHqSk" if stamp == publication_time(DAY,"10:00") else "T-II7Hvqpz0"
            return result
        self.upload.side_effect=uploaded
        self.run_prepare()
        self.controller.queue._set(DAY,"14:00",{"upload_status":"uploading","youtube":None})
        self.fresh()
        self.upload.reset_mock();self.upload.side_effect=self.uploaded
        before=copy.deepcopy(self.controller.store.latest(DAY))
        env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main",
             "YOUTUBE_CHANNEL_ID":CHANNEL}
        def call(flag):
            with patch.dict(os.environ,env,clear=True), \
                 patch("sys.argv",["run_actions.py","prepare","--day",str(DAY),"--confirmed-not-uploaded-slot",flag]), \
                 patch.object(run_actions,"GitHubArtifacts",return_value=self.store), \
                 patch("engine.scheduling.preparation.Preparation",return_value=self.controller), \
                 patch.object(run_actions,"india_now",return_value=NOW), \
                 patch("engine.scheduling.preparation.india_now",return_value=NOW), \
                 patch.object(run_actions,"logging"),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
                 patch.object(run_actions,"restore_oauth"),patch.object(youtube,"preparation_preflight"), \
                 patch.object(youtube,"upload_prepared_video",self.upload),patch.object(youtube,"verify_prepared_video",self.verify), \
                 patch("engine.approval.telegram_bot.notify_generated_video",self.notify), \
                 patch("engine.scheduling.daily_queue.generate_only",side_effect=AssertionError("Do not regenerate")) as gen, \
                 patch.object(run_actions,"cleanup_production",return_value={}),patch("sys.stdout",io.StringIO()):
                code=run_actions.main()
                gen.assert_not_called()
                return code
        self.assertEqual(call(""),1)
        self.upload.assert_not_called()
        self.assertEqual(self.controller.queue.read(DAY)["videos"][1]["upload_status"],"uploading")
        self.assertEqual(call("14:00"),0)
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.args[2],publication_time(DAY,"14:00"))
        old=json.loads(before["bytes"])["queue"]["videos"]
        saved=self.controller.queue.read(DAY)["videos"]
        self.assertEqual(old[1]["generated_files"][0]["sha256"],saved[1]["generated_files"][0]["sha256"])
        self.assertEqual(saved[0]["youtube"]["video_id"],"wYud-PDHqSk")
        self.assertEqual(saved[2]["youtube"]["video_id"],"T-II7Hvqpz0")
        for index in (0,2):
            self.assertEqual(old[index]["youtube"],saved[index]["youtube"])
            self.assertEqual(old[index]["preparation_verification"],saved[index]["preparation_verification"])

    def test_cli_invalid_confirmed_slot_is_rejected_before_any_production_work(self):
        import io
        import run_actions
        with patch("sys.argv",["run_actions.py","prepare","--day",str(DAY),"--confirmed-not-uploaded-slot","22:00"]), \
             patch.object(run_actions,"GitHubArtifacts") as store,patch("sys.stderr",io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                run_actions.main()
            self.assertEqual(exc.exception.code,2)
        store.assert_not_called()

    def test_reconciliation_blocks_elapsed_slot_and_wrong_channel(self):
        self.upload.side_effect=TimeoutError("unknown")
        self.run_prepare()
        for channel,instant in (("wrong",NOW),(CHANNEL,datetime(2026,10,3,14,tzinfo=TZ))):
            before=len(self.store.saved)
            with self.assertRaises(ProductionError):
                self.controller.reconcile_not_uploaded("14:00",expected_channel=channel,now=lambda:instant)
            self.assertEqual(len(self.store.saved),before)

    def test_reconciliation_refuses_corrupt_original_render_without_releasing_claim(self):
        self.upload.side_effect=TimeoutError("unknown")
        self.run_prepare()
        record=self.controller.state["queue"]["videos"][1]["generated_files"][0]
        artifact=next(a for a in self.store.saved if a["id"] == record["artifact_id"])
        artifact["bytes"]=b"corrupt"
        self.fresh()
        before=len(self.store.saved)
        with self.assertRaises(ProductionError):
            self.controller.reconcile_not_uploaded("14:00",expected_channel=CHANNEL,now=lambda:NOW)
        self.assertEqual(len(self.store.saved),before)
        self.assertEqual(json.loads(self.store.latest(DAY)["bytes"])["queue"]["videos"][1]["upload_status"],"uncertain")

    def test_reconciliation_checkpoint_failure_retains_remote_claim(self):
        self.upload.side_effect=TimeoutError("unknown")
        self.run_prepare()
        with patch.object(self.store,"upload",side_effect=RuntimeError("checkpoint failed")):
            with self.assertRaises(Exception):
                self.controller.reconcile_not_uploaded("14:00",expected_channel=CHANNEL,now=lambda:NOW)
        remote=json.loads(self.store.latest(DAY)["bytes"])["queue"]["videos"][1]
        self.assertEqual(remote["upload_status"],"uncertain")
        self.upload.reset_mock()
        self.run_prepare(controller=self.fresh())
        self.upload.assert_not_called()

    def default_recovery_run(self, current):
        import io
        import run_actions
        primary = Mock()
        primary.prepare.return_value = {"status":"READY", "target_date":str(DAY+timedelta(days=1))}
        order = []
        def factory(store, day):
            self.assertIs(store, self.store)
            order.append(day)
            return self.controller if day == DAY else primary
        env = {"GITHUB_ACTIONS":"true", "GITHUB_REF":"refs/heads/main", "PRODUCTION_BRANCH":"main",
               "YOUTUBE_CHANNEL_ID":CHANNEL}
        output = io.StringIO()
        with patch.dict(os.environ,env,clear=True), patch("sys.argv",["run_actions.py","prepare"]), \
             patch.object(run_actions,"GitHubArtifacts",return_value=self.store), \
             patch("engine.scheduling.preparation.Preparation",side_effect=factory), \
             patch.object(run_actions,"india_now",return_value=current), \
             patch("engine.scheduling.preparation.india_now",return_value=current), \
             patch.object(run_actions,"logging"),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
             patch.object(run_actions,"restore_oauth"),patch.object(youtube,"preparation_preflight"), \
             patch.object(youtube,"upload_prepared_video",self.upload),patch.object(youtube,"verify_prepared_video",self.verify), \
             patch("engine.approval.telegram_bot.notify_generated_video",self.notify), \
             patch("engine.scheduling.daily_queue.generate_only",side_effect=AssertionError("Do not regenerate")), \
             patch.object(run_actions,"cleanup_production",return_value={}),patch("sys.stdout",output):
            code = run_actions.main()
        primary.prepare.assert_called_once()
        return code, json.loads(output.getvalue()), order

    def test_default_recovery_skips_elapsed_slot_and_prepares_tomorrow_after_future_slots(self):
        self.auth_pending()
        self.fresh()
        code, result, order = self.default_recovery_run(datetime(2026,10,3,10,5,tzinfo=TZ))
        self.assertEqual(order,[DAY,DAY+timedelta(days=1)])
        self.assertEqual(self.upload.call_count,2)
        self.assertEqual([c.args[2] for c in self.upload.call_args_list],
                         [publication_time(DAY,s) for s in ("14:00","19:00")])
        saved = self.controller.queue.read(DAY)
        self.assertEqual(saved["videos"][0]["upload_status"],"pending")
        self.assertEqual(saved["date"],str(DAY))
        self.assertEqual(self.count,3)
        self.assertEqual(result["status"],"READY")
        self.assertEqual(code,1)  # The unresolved elapsed slot remains visible in the recovery summary.

    def test_default_prepares_tomorrow_when_current_day_slots_all_elapsed(self):
        self.auth_pending()
        before = copy.deepcopy(self.store.latest(DAY))
        self.fresh()
        code,result,order = self.default_recovery_run(datetime(2026,10,3,19,tzinfo=TZ))
        self.assertEqual(code,0)
        self.assertEqual(result["current_day_recovery"]["status"],"SKIPPED")
        self.assertEqual(result["current_day_recovery"]["reason"],"no_safe_future_work")
        self.assertEqual(self.store.latest(DAY),before)
        self.upload.assert_not_called()

    def test_default_prepares_tomorrow_when_current_day_has_only_uncertain_work(self):
        self.upload.side_effect = TimeoutError("unknown insertion outcome")
        self.run_prepare()
        before = copy.deepcopy(self.store.latest(DAY))
        self.fresh(); self.upload.reset_mock()
        code,result,order = self.default_recovery_run(datetime(2026,10,3,9,5,tzinfo=TZ))
        self.assertEqual(code,0)
        self.assertEqual(result["current_day_recovery"]["status"],"SKIPPED")
        self.assertEqual(self.store.latest(DAY),before)
        self.upload.assert_not_called()

    def test_expiry_after_authentication_preserves_pending_not_uncertain_and_claim_receipt(self):
        clock = [datetime(2026,10,3,9,59,59,tzinfo=TZ)]
        self.auth_pending()
        self.fresh()
        service = Mock()
        def authenticate():
            clock[0] += timedelta(seconds=2)
            return service
        def upload(path, metadata, stamp):
            if stamp == publication_time(DAY,"10:00"):
                return youtube.upload_prepared_video(path,metadata,stamp)
            return self.uploaded(path,metadata,stamp)
        validator = youtube.validate_future_publish_at
        with patch.object(youtube,"get_authenticated_service",side_effect=authenticate), \
             patch.object(youtube,"validate_future_publish_at",side_effect=lambda stamp:validator(stamp,clock[0])), \
             patch.object(youtube,"MediaFileUpload"):
            result = self.controller.prepare(expected_channel=CHANNEL, generator=Mock(), uploader=upload,
                verifier=self.verify, notifier=self.notify, allow_current_day=True, now=lambda:clock[0])
        service.videos.return_value.insert.assert_not_called()
        item = self.controller.queue.read(DAY)["videos"][0]
        self.assertEqual(item["upload_status"],"pending")
        self.assertEqual(item["youtube"]["status"],"not_started")
        self.assertEqual(item["upload_not_started_history"][0]["error_type"],"PublicationExpired")
        self.assertIn({"slot":"10:00","reason":"publish_at_not_future"},result["problems"])
        self.assertEqual(self.count,3)

    def test_cli_manual_and_blank_cron_resume_same_batch_without_generation_secrets(self):
        import io
        import run_actions
        env = {"GITHUB_ACTIONS":"true", "GITHUB_REF":"refs/heads/main", "PRODUCTION_BRANCH":"main",
               "YOUTUBE_CHANNEL_ID":CHANNEL, "OPENAI_API_KEY":"fixture", "PEXELS_API_KEY":"fixture"}
        def factory(store, day):
            self.assertIs(store, self.store)
            self.assertEqual(day, DAY)
            return self.controller
        output = io.StringIO()
        with patch.dict(os.environ,env,clear=True), patch.object(run_actions,"GitHubArtifacts",return_value=self.store), \
             patch("engine.scheduling.preparation.Preparation",side_effect=factory), \
             patch.object(run_actions,"india_now",return_value=NOW), patch("engine.scheduling.preparation.india_now",return_value=NOW), \
             patch.object(run_actions,"logging"), patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
             patch("engine.scheduling.daily_queue.generate_only",side_effect=self.generate) as gen, \
             patch.object(youtube,"upload_prepared_video",self.upload),patch.object(youtube,"verify_prepared_video",self.verify), \
             patch("engine.approval.telegram_bot.notify_generated_video",self.notify), \
             patch.object(run_actions,"restore_oauth",side_effect=RuntimeError("credential must not be printed")) as auth, \
             patch.object(youtube,"preparation_preflight") as preflight, patch.object(run_actions,"cleanup_production",return_value={}), \
             patch("sys.stdout",output):
            with patch("sys.argv",["run_actions.py","prepare","--day",str(DAY)]):
                self.assertEqual(run_actions.main(),1)
            self.assertEqual(gen.call_count,3)
            self.assertEqual(json.loads(output.getvalue())["generation_status"],"COMPLETE")
            self.upload.assert_not_called()
            preflight.assert_not_called()
            self.fresh()
            os.environ.pop("OPENAI_API_KEY"); os.environ.pop("PEXELS_API_KEY")
            auth.side_effect = None
            output.seek(0); output.truncate()
            with patch("sys.argv",["run_actions.py","prepare"]):
                self.assertEqual(run_actions.main(),0)
            self.assertEqual(json.loads(output.getvalue())["status"],"READY")
            self.assertEqual(gen.call_count,3)
            self.assertEqual(self.upload.call_count,3)
            preflight.assert_called_once_with(CHANNEL)


class PolicyTests(unittest.TestCase):
    def test_tomorrow_uses_india_date_and_rejects_other_days(self):
        now=datetime(2026,10,2,20,tzinfo=timezone.utc)
        self.assertEqual(target_day(now),date(2026,10,4))
        self.assertEqual(target_day(NOW,"2026-10-03"),DAY)
        self.assertEqual(target_day(NOW,"2026-10-02"),NOW.date())
        for invalid in ("2026-10-01","2026-10-04"):
            with self.assertRaises(ProductionError):target_day(NOW,invalid)

    def test_timezone_conversion_all_slots_month_year_rollover(self):
        for day in (DAY,date(2027,1,1)):
            for slot,utc in (("10:00","04:30"),("14:00","08:30"),("19:00","13:30")):
                self.assertEqual(publication_time(day,slot),f"{day}T{utc}:00+00:00")

    def test_future_validation_exact_and_past_rejected(self):
        for stamp in (NOW,NOW-timedelta(seconds=1),NOW.replace(tzinfo=None)):
            with self.assertRaises(ValueError):youtube.validate_future_publish_at(stamp.isoformat(),NOW)
        youtube.validate_future_publish_at((NOW+timedelta(seconds=1)).isoformat(),NOW)

    def test_workflow_preparation_only_and_concurrency(self):
        import re
        s=Path('.github/workflows/production.yml').read_text()
        self.assertEqual(re.findall(r"cron: '([^']+)'",s),[])
        self.assertNotIn("  schedule:",s)
        self.assertIn('queue: max',s);self.assertIn('cancel-in-progress: false',s)
        self.assertIn('options: [prepare, cleanup]',s)
        self.assertIn('confirmed_not_uploaded_slot:',s)
        self.assertIn("INPUT_CONFIRMED_NOT_UPLOADED_SLOT: ${{ inputs.confirmed_not_uploaded_slot }}",s)
        self.assertIn("args.push('--confirmed-not-uploaded-slot', inputs.confirmed_not_uploaded_slot)",s)
        self.assertIn("args.push('--day', inputs.day)",s)
        self.assertNotIn('generation_run:',s);self.assertNotIn("['upload'",s)
        self.assertIn('YOUTUBE_PRIVACY_STATUS: private',s)

    def test_actual_workflow_script_uses_dispatch_event_inputs_and_omits_blank_flags(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required to execute the workflow's actual JavaScript")
        workflow = Path('.github/workflows/production.yml').read_text()
        script = workflow.split('          script: |\n',1)[1]
        script = '\n'.join(line[12:] for line in script.splitlines())
        for inputs, expected in (
            ({"operation":"prepare","day":"2026-10-03","confirmed_not_uploaded_slot":"14:00"},
             ["run_actions.py","prepare","--day","2026-10-03","--confirmed-not-uploaded-slot","14:00"]),
            ({"operation":"prepare","day":"","confirmed_not_uploaded_slot":""},["run_actions.py","prepare"]),
            ({"operation":"cleanup"},["run_actions.py","cleanup"]),
        ):
            with self.subTest(inputs=inputs):
                harness = ("const context = {eventName:'workflow_dispatch',payload:{inputs:"+json.dumps(inputs)+"}};\n"
                    "const exec = {exec:async(command,args)=>process.stdout.write(JSON.stringify({command,args}))};\n"
                    "process.env.INPUT_DAY='';process.env.INPUT_CONFIRMED_NOT_UPLOADED_SLOT='';\n"
                    "process.env.INPUT_OPERATION='wrong-environment-value';\n"
                    "(async()=>{\n"+script+"\n})().catch(error=>{process.stderr.write(error.message);process.exit(1)});")
                result = subprocess.run([node,'-e',harness],text=True,capture_output=True,check=True)
                self.assertEqual(json.loads(result.stdout),{"command":"python","args":expected})

    def test_cli_explicit_target_and_ready_noop_without_generation_secrets(self):
        import run_actions
        from engine.scheduling.preparation import Preparation
        env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main",
             "GITHUB_EVENT_NAME":"workflow_dispatch","YOUTUBE_CHANNEL_ID":CHANNEL}
        with patch.dict(os.environ,env,clear=True),patch("sys.argv",["run_actions.py","prepare","--day",str(DAY)]), \
             patch.object(run_actions,"logging") ,patch.object(run_actions,"india_now",return_value=NOW),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
             patch.object(run_actions,"GitHubArtifacts"),patch.object(run_actions,"cleanup_production",return_value={}), \
             patch.object(run_actions,"restore_oauth") as auth,patch("engine.scheduling.preparation.Preparation") as controller:
            controller.return_value.state={"queue":{"batch_status":"READY"}}
            controller.return_value.prepare.return_value={"status":"READY","no_op":True}
            self.assertEqual(run_actions.main(),0)
            self.assertEqual(controller.call_args.args[1],DAY)
            auth.assert_not_called()

    def test_cli_rejects_past_future_before_authentication(self):
        import run_actions
        env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main"}
        for day in ("2026-10-01","2026-10-04"):
            with patch.dict(os.environ,env,clear=True),patch("sys.argv",["run_actions.py","prepare","--day",day]), \
                 patch.object(run_actions,"logging") ,patch.object(run_actions,"india_now",return_value=NOW),patch.object(run_actions,"GitHubArtifacts"), \
                 patch.object(run_actions,"restore_oauth") as auth:
                self.assertEqual(run_actions.main(),1)
                auth.assert_not_called()


class YouTubePreparationTests(unittest.TestCase):
    def setUp(self):
        self.service=Mock()
        self.addCleanup(patch.stopall)
        patch.object(youtube,'get_authenticated_service',return_value=self.service).start()
        patch.object(youtube,'validate_future_publish_at',side_effect=lambda x:datetime.fromisoformat(x.replace('Z','+00:00'))).start()
        self.stamp=publication_time(DAY,'10:00')

    def video(self, **status):
        return {'id':'id','snippet':{'channelId':CHANNEL},'status':{'privacyStatus':'private','uploadStatus':'processed','publishAt':self.stamp,**status},
                'processingDetails':{'processingStatus':'succeeded'}}

    def readbacks(self,*values):
        self.service.videos.return_value.list.return_value.execute.side_effect=[{'items':[v]} for v in values]

    def test_private_publish_at_insert_and_same_resumable_request(self):
        self.service.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'id'})
        with patch.object(youtube.os.path,'isfile',return_value=True),patch.object(youtube,'MediaFileUpload'),patch.object(youtube,'YOUTUBE_PRIVACY_STATUS','public'):
            youtube.upload_video('saved.mp4','title','description',['#tag'],publish_at=self.stamp)
        body=self.service.videos.return_value.insert.call_args.kwargs['body']
        self.assertEqual(body['status']['privacyStatus'],'private')
        self.assertEqual(body['status']['publishAt'],self.stamp)
        self.service.videos.return_value.insert.assert_called_once()

    def test_correct_readback_skip_update(self):
        self.readbacks(self.video())
        self.assertTrue(youtube.verify_prepared_video('id',CHANNEL,self.stamp)['verified'])
        self.service.videos.return_value.update.assert_not_called()

    def test_wrong_or_missing_schedule_repaired_existing_id(self):
        for wrong in (None,publication_time(DAY,'14:00')):
            self.service.reset_mock();self.readbacks(self.video(publishAt=wrong),self.video())
            self.assertTrue(youtube.verify_prepared_video('id',CHANNEL,self.stamp)['verified'])
            body=self.service.videos.return_value.update.call_args.kwargs['body']
            self.assertEqual(body['id'],'id');self.assertEqual(body['status']['publishAt'],self.stamp)
            self.assertEqual(body['status']['privacyStatus'],'private')
            self.service.videos.return_value.insert.assert_not_called()

    def test_public_wrong_owner_rejected_missing_id_and_processing_failed(self):
        for v in (self.video(privacyStatus='public'),{**self.video(),'snippet':{'channelId':'other'}},self.video(uploadStatus='rejected')):
            self.readbacks(v)
            with self.assertRaises(ValueError):youtube.verify_prepared_video('id',CHANNEL,self.stamp)
        self.service.videos.return_value.list.return_value.execute.side_effect=[{'items':[]}]
        with self.assertRaises(ValueError):youtube.verify_prepared_video('id',CHANNEL,self.stamp)
        v=self.video();v['processingDetails']['processingStatus']='failed';self.readbacks(v)
        self.assertFalse(youtube.verify_prepared_video('id',CHANNEL,self.stamp)['verified'])
        self.service.videos.return_value.insert.assert_not_called()

    def test_preflight_rejects_old_scope_before_account_calls(self):
        from unittest.mock import mock_open
        creds=Mock();creds.has_scopes.return_value=False
        with patch("builtins.open",mock_open()),patch.object(youtube.pickle,"load",return_value=creds):
            with self.assertRaisesRegex(RuntimeError,"force-ssl"):youtube.preparation_preflight(CHANNEL)
        youtube.get_authenticated_service.assert_not_called()

    def test_preflight_wrong_channel_and_valid_channel(self):
        from unittest.mock import mock_open
        creds=Mock();creds.has_scopes.return_value=True
        self.service.channels.return_value.list.return_value.execute.return_value={"items":[{"id":"wrong"}]}
        with patch("builtins.open",mock_open()),patch.object(youtube.pickle,"load",return_value=creds):
            with self.assertRaises(ValueError):youtube.preparation_preflight(CHANNEL)
            self.service.channels.return_value.list.return_value.execute.return_value={"items":[{"id":CHANNEL}]}
            youtube.preparation_preflight(CHANNEL)

    def test_preinsert_auth_failure_is_retryable_without_insert_or_resumable_call(self):
        youtube.get_authenticated_service.side_effect = RuntimeError("credential secret")
        with patch.object(youtube.os.path, "isfile", return_value=True):
            with self.assertRaises(youtube.UploadNotStarted):
                youtube.upload_prepared_video("saved.mp4", {"Title":"t","Description":"d","Hashtags":[]}, self.stamp)
        self.service.videos.return_value.insert.assert_not_called()

    def test_publication_expiring_during_authentication_blocks_insert(self):
        """The deadline must still be future when insertion starts, not only before auth."""
        stamp = datetime.fromisoformat(self.stamp)
        clock = [stamp - timedelta(seconds=1)]
        def validate(value):
            parsed = datetime.fromisoformat(value)
            if parsed <= clock[0]:
                raise ValueError("Publication time is no longer future")
            return parsed
        def authenticate():
            clock[0] = stamp + timedelta(seconds=1)
            return self.service
        youtube.get_authenticated_service.side_effect = authenticate
        self.service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id":"late-id"})
        with patch.object(youtube, "validate_future_publish_at", side_effect=validate), \
             patch.object(youtube.os.path, "isfile", return_value=True), patch.object(youtube, "MediaFileUpload"):
            with self.assertRaises(youtube.PublicationExpired):
                youtube.upload_prepared_video("saved.mp4", {"Title":"t","Description":"d","Hashtags":[]}, self.stamp)
        self.service.videos.return_value.insert.assert_not_called()

    def test_initial_deadline_expiry_is_also_proven_not_started(self):
        with patch.object(youtube,"validate_future_publish_at",side_effect=ValueError("expired")), \
             patch.object(youtube.os.path,"isfile",return_value=True):
            with self.assertRaises(youtube.PublicationExpired):
                youtube.upload_prepared_video("saved.mp4",{"Title":"t","Description":"d","Hashtags":[]},self.stamp)
        youtube.get_authenticated_service.assert_not_called()
        self.service.videos.return_value.insert.assert_not_called()

    def test_initial_and_fresh_validation_both_precede_insert(self):
        order = []
        def validate(value):
            order.append("validate")
            return datetime.fromisoformat(value)
        def authenticate():
            order.append("authenticate")
            return self.service
        def insert(**kwargs):
            order.append("insert")
            request = Mock()
            request.next_chunk.return_value = (None,{"id":"id"})
            return request
        youtube.get_authenticated_service.side_effect = authenticate
        self.service.videos.return_value.insert.side_effect = insert
        with patch.object(youtube,"validate_future_publish_at",side_effect=validate), \
             patch.object(youtube.os.path,"isfile",return_value=True),patch.object(youtube,"MediaFileUpload"):
            youtube.upload_prepared_video("saved.mp4",{"Title":"t","Description":"d","Hashtags":[]},self.stamp)
        self.assertEqual(order,["validate","authenticate","validate","insert"])

    def test_error_after_insert_is_not_classified_as_not_started(self):
        self.service.videos.return_value.insert.return_value.next_chunk.side_effect = ValueError("unconfirmed transfer")
        with patch.object(youtube.os.path,"isfile",return_value=True), patch.object(youtube,"MediaFileUpload"):
            with self.assertRaises(ValueError):
                youtube.upload_prepared_video("saved.mp4", {"Title":"t","Description":"d","Hashtags":[]}, self.stamp)
        self.service.videos.return_value.insert.assert_called_once()


class PreparationNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_and_recovery_use_text_without_video_and_truthful_wording(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from engine.approval.telegram_bot import send_video_notification
        for status in ("READY","PREPARING"):
            bot=AsyncMock();bot.__aenter__.return_value=bot
            bot.send_message.return_value=SimpleNamespace(message_id=1)
            data={"batch_notice":True,"metadata":{"Title":"Batch"},"upload":{"success":False,"error":"incomplete"},
                  "preparation":{"batch_status":status,"target_date":str(DAY),"problems":[]}}
            with patch.dict(os.environ,{"TELEGRAM_BOT_TOKEN":"fixture","TELEGRAM_CHAT_ID":"123"}), \
                 patch("engine.approval.telegram_bot.Bot",return_value=bot):
                result=await send_video_notification(data)
            self.assertTrue(result["success"])
            bot.send_video.assert_not_called()
            text=bot.send_message.call_args.kwargs["text"]
            self.assertIn("READY" if status=="READY" else "incomplete",text)


if __name__ == '__main__':
    unittest.main()
