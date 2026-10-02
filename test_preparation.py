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


class PolicyTests(unittest.TestCase):
    def test_tomorrow_uses_india_date_and_rejects_other_days(self):
        now=datetime(2026,10,2,20,tzinfo=timezone.utc)
        self.assertEqual(target_day(now),date(2026,10,4))
        self.assertEqual(target_day(NOW,"2026-10-03"),DAY)
        for invalid in ("2026-10-01","2026-10-02","2026-10-04"):
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
        self.assertEqual(re.findall(r"cron: '([^']+)'",s),["5 5 * * *","5 9 * * *","5 13 * * *","5 17 * * *","5 21 * * *"])
        self.assertIn('queue: max',s);self.assertIn('cancel-in-progress: false',s)
        self.assertIn('options: [prepare, cleanup]',s)
        self.assertNotIn('generation_run:',s);self.assertNotIn("['upload'",s)
        self.assertIn('YOUTUBE_PRIVACY_STATUS: private',s)

    def test_cli_explicit_target_and_ready_noop_without_generation_secrets(self):
        import run_actions
        from engine.scheduling.preparation import Preparation
        env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main",
             "GITHUB_EVENT_NAME":"workflow_dispatch","YOUTUBE_CHANNEL_ID":CHANNEL}
        with patch.dict(os.environ,env,clear=True),patch("sys.argv",["run_actions.py","prepare","--day",str(DAY)]), \
             patch.object(run_actions,"india_now",return_value=NOW),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"), \
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
        for day in ("2026-10-01","2026-10-02","2026-10-04"):
            with patch.dict(os.environ,env,clear=True),patch("sys.argv",["run_actions.py","prepare","--day",day]), \
                 patch.object(run_actions,"india_now",return_value=NOW),patch.object(run_actions,"GitHubArtifacts"), \
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
