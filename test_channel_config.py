"""Offline configuration tests; production is not switched to this registry."""
import unittest
from pydantic import ValidationError
from config.channels import ChannelConfig, MINUTE_MYSTERIES, resolve_channel_config


class ChannelConfigTests(unittest.TestCase):
    def config(self, key="other", **changes):
        data=MINUTE_MYSTERIES.model_dump()
        data.update(channel_key=key, state_namespace=f"state/{key}", artifact_namespace=key, output_namespace=f"renders/{key}")
        data.update(changes)
        return ChannelConfig.model_validate(data)

    def test_default_preserves_settings_and_identity(self):
        from config import settings
        c=resolve_channel_config("minute_mysteries")
        self.assertEqual(c.channel_name, settings.CHANNEL_NAME)
        self.assertEqual(c.youtube_channel_id, "UCR2L8UE-pc1Hd7U3AurrECQ")
        self.assertEqual(c.topic_theme, settings.BASE_CONTENT_TOPIC)
        self.assertEqual(c.timezone, settings.SCHEDULER_TIMEZONE)
        self.assertEqual(c.publication_slots, tuple(settings.UPLOAD_TIMES))
        self.assertEqual(c.publication_slots, ("10:00","14:00","19:00"))
        self.assertEqual(c.preparation_times, ("05:05","09:05","13:05","17:05","21:05"))
        self.assertEqual(c.credential_reference,"YOUTUBE_TOKEN_PICKLE_B64")
        self.assertEqual(c.telegram_chat_reference,"TELEGRAM_CHAT_ID")
        self.assertEqual(c.telegram_credential_reference,"TELEGRAM_BOT_TOKEN")

    def test_registered_space_science_identity_and_runtime_isolation(self):
        from pathlib import Path
        from config.channels import RuntimePaths, SPACE_SCIENCE
        c=resolve_channel_config("space_science")
        self.assertIs(c, SPACE_SCIENCE)
        self.assertEqual(c.channel_name,"Space and Strange Science")
        self.assertEqual(c.youtube_channel_id,"UCfs8yJirS4w54IEiwSlM9xA")
        self.assertEqual(c.credential_reference,"YOUTUBE_TOKEN_SPACE_SCIENCE_B64")
        self.assertEqual(c.topic_theme,"astronomy, space mysteries, strange science")
        self.assertEqual(c.content_instructions,"Factual, curiosity-driven, science-focused storytelling.")
        self.assertEqual(c.timezone,"Asia/Kolkata")
        self.assertEqual(c.publication_slots,("10:00","14:00","19:00"))
        self.assertIs(resolve_channel_config("minute_mysteries"),MINUTE_MYSTERIES)
        for field in ("state_namespace","artifact_namespace","output_namespace","credential_reference","telegram_chat_reference","telegram_credential_reference"):
            self.assertNotEqual(getattr(c,field),getattr(MINUTE_MYSTERIES,field))
        paths=RuntimePaths(c,Path.cwd());legacy=RuntimePaths(MINUTE_MYSTERIES,Path.cwd())
        for field in ("output","temp","audio","clips","subtitles","memory"):
            self.assertNotEqual(getattr(paths,field),getattr(legacy,field))
        self.assertEqual(paths.memory,Path.cwd()/"data"/"channels"/"space_science"/"idea_memory.json")

    def test_unknown_key(self):
        with self.assertRaisesRegex(ValueError,"Unknown channel key"):resolve_channel_config("missing")

    def test_duplicate_keys_even_when_requested_record_is_first(self):
        with self.assertRaisesRegex(ValueError,"Duplicate channel key"):
            resolve_channel_config("minute_mysteries",configs=[MINUTE_MYSTERIES,self.config("minute_mysteries")])

    def test_duplicate_namespaces(self):
        for field in ("state_namespace","artifact_namespace","output_namespace"):
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,"Duplicate or overlapping"):
                resolve_channel_config("minute_mysteries",configs=[MINUTE_MYSTERIES,self.config(**{field:getattr(MINUTE_MYSTERIES,field)})])

    def test_overlapping_namespace(self):
        with self.assertRaises(ValueError):
            resolve_channel_config("minute_mysteries",configs=[MINUTE_MYSTERIES,self.config(output_namespace="output/videos/other")])

    def test_invalid_timezone(self):
        with self.assertRaises(ValidationError):self.config(timezone="Invalid/Timezone")

    def test_duplicate_unordered_and_invalid_times(self):
        for field in ("publication_slots","preparation_times"):
            for values in (("10:00","10:00"),("19:00","10:00"),("24:00",),()):
                with self.subTest(field=field,values=values),self.assertRaises(ValidationError):self.config(**{field:values})

    def test_fourth_channel_needs_no_branch(self):
        configs=[MINUTE_MYSTERIES]+[self.config(f"example_{n}") for n in range(2,5)]
        self.assertEqual(resolve_channel_config("example_4",configs=configs),configs[3])

    def test_credential_values_and_unsafe_namespaces_rejected(self):
        for changes in ({"credential_reference":"Bearer secret"},{"state_namespace":"../state"},{"channel_key":"../channel"},{"youtube_channel_id":"invalid"}):
            with self.assertRaises(ValidationError):self.config(**changes)

    def test_immutable_and_does_not_read_environment(self):
        from unittest.mock import patch
        with patch('os.getenv',side_effect=AssertionError('No secret reads')):
            self.assertIs(resolve_channel_config("minute_mysteries"),MINUTE_MYSTERIES)
        with self.assertRaises(ValidationError):MINUTE_MYSTERIES.channel_name="Changed"


if __name__ == '__main__':unittest.main()


class ChannelWorkflowTests(unittest.TestCase):
    def test_unknown_key_stops_before_production_work(self):
        import os, run_actions
        from unittest.mock import patch
        with patch.dict(os.environ, {"GITHUB_ACTIONS":"true", "GITHUB_REF":"refs/heads/main", "PRODUCTION_BRANCH":"main"}), patch("sys.argv", ["run_actions.py","cleanup","--channel-key","unknown"]), patch.object(run_actions,"GitHubArtifacts") as store, patch.object(run_actions,"cleanup_production") as cleanup:
            self.assertEqual(run_actions.main(),1)
            store.assert_not_called();cleanup.assert_not_called()

    def test_omitted_and_explicit_default_cleanup_use_same_context(self):
        import os, run_actions
        from unittest.mock import patch, Mock
        from config.channels import MINUTE_MYSTERIES
        for args in ([], ["--channel-key","minute_mysteries"]):
            store=Mock()
            with patch.dict(os.environ, {"GITHUB_ACTIONS":"true", "GITHUB_REF":"refs/heads/main", "PRODUCTION_BRANCH":"main"}), patch("sys.argv", ["run_actions.py","cleanup",*args]), patch.object(run_actions,"GitHubArtifacts",return_value=store) as artifacts, patch.object(run_actions,"cleanup_production",return_value={"status":"completed"}) as cleanup:
                self.assertEqual(run_actions.main(),0)
                artifacts.assert_called_once_with(channel=MINUTE_MYSTERIES)
                cleanup.assert_called_once_with(store,channel=MINUTE_MYSTERIES)

    def test_prepare_recovery_and_reconciliation_receive_same_channel(self):
        import os, run_actions
        from unittest.mock import patch, Mock
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from config.channels import MINUTE_MYSTERIES
        now=datetime(2026,10,6,5,5,tzinfo=ZoneInfo("Asia/Kolkata"))
        for explicit in (False,True):
            store=Mock();store.latest.return_value={"id":1}
            controller=Mock();controller.recoverable_slots.return_value=["14:00"]
            controller.prepare.side_effect=lambda **kwargs: {"status":"READY"}
            controller.reconcile_not_uploaded.return_value={"status":"reconciled"}
            env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","PRODUCTION_BRANCH":"main","YOUTUBE_CHANNEL_ID":MINUTE_MYSTERIES.youtube_channel_id}
            args=["run_actions.py","prepare","--channel-key","minute_mysteries"]
            if explicit:args += ["--day","2026-10-06","--confirmed-not-uploaded-slot","14:00"]
            with patch.dict(os.environ,env),patch("sys.argv",args),patch.object(run_actions,"GitHubArtifacts",return_value=store),patch.object(run_actions,"india_now",return_value=now),patch.object(run_actions,"YOUTUBE_PRIVACY_STATUS","private"),patch("engine.scheduling.preparation.Preparation",return_value=controller) as factory,patch.object(run_actions,"cleanup_production",return_value={}):
                self.assertEqual(run_actions.main(),0)
                for call in factory.call_args_list:self.assertEqual(call.kwargs["channel"],MINUTE_MYSTERIES)
                contexts=[call.kwargs["credential_context"] for call in controller.prepare.call_args_list]
                self.assertTrue(all(context is contexts[0] for context in contexts))
                self.assertEqual(contexts[0].channel,MINUTE_MYSTERIES)
                if explicit:self.assertIs(controller.reconcile_not_uploaded.call_args.kwargs["credential_context"],contexts[0])
                else:self.assertEqual(controller.prepare.call_count,2)

    def test_workflow_resolution_code_and_concurrency(self):
        from pathlib import Path
        from config.channels import resolve_channel_config
        s=Path(".github/workflows/production.yml").read_text()
        self.assertIn("default: minute_mysteries",s)
        self.assertIn("group: production-${{ inputs.channel_key || 'minute_mysteries' }}",s)
        self.assertIn("cancel-in-progress: false",s);self.assertIn("queue: max",s)
        self.assertNotIn("schedule:",s)
        self.assertIn("secrets[steps.channel.outputs.credential_reference]",s)
        self.assertNotIn("secrets.YOUTUBE_TOKEN_PICKLE_B64",s)
        self.assertEqual("production-"+resolve_channel_config("minute_mysteries").channel_key,"production-minute_mysteries")
        self.assertNotEqual("production-minute_mysteries","production-example_b")
        code=s.split("          python - <<'PY'\n",1)[1].split("          PY",1)[0]
        code="\n".join(line[10:] for line in code.splitlines())
        compile(code,"workflow channel resolution","exec")
        import os,tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"outputs"
            with patch.dict(os.environ,{"INPUT_CHANNEL_KEY":"minute_mysteries","GITHUB_OUTPUT":str(output)}):exec(code,{})
            self.assertIn("credential_reference=YOUTUBE_TOKEN_PICKLE_B64",output.read_text())
            self.assertIn("youtube_channel_id=UCR2L8UE-pc1Hd7U3AurrECQ",output.read_text())
            before=output.read_bytes()
            with patch.dict(os.environ,{"INPUT_CHANNEL_KEY":"unknown","GITHUB_OUTPUT":str(output)}):
                with self.assertRaises(ValueError):exec(code,{})
            self.assertEqual(output.read_bytes(),before)


class NamespaceCaseTests(unittest.TestCase):
    def test_case_only_namespace_collision_is_rejected(self):
        from config.channels import MINUTE_MYSTERIES, ChannelConfig, resolve_channel_config, validate_channel_scope
        for field in ("state_namespace", "artifact_namespace", "output_namespace"):
            data = MINUTE_MYSTERIES.model_dump()
            data.update(channel_key="other", state_namespace=".other", artifact_namespace="other", output_namespace="output/other")
            data[field] = getattr(MINUTE_MYSTERIES,field).upper()
            other = ChannelConfig.model_validate(data)
            with self.assertRaises(ValueError):resolve_channel_config("other",configs=[MINUTE_MYSTERIES,other])
            if field != "output_namespace":
                with self.assertRaises(ValueError):validate_channel_scope(other)
