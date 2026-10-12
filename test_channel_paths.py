"""Offline runtime namespace and immutable stock working-copy regressions."""
import tempfile, unittest, json
from pathlib import Path
from unittest.mock import patch, Mock
from config.channels import MINUTE_MYSTERIES, ChannelConfig, RuntimePaths
from engine.scheduling.cleanup import record_generated_file, _validated_target
from datetime import date

class ChannelPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        data = MINUTE_MYSTERIES.model_dump()
        data.update(channel_key="example_b", youtube_channel_id="UC" + "b"*22,
                    state_namespace=".state-b", artifact_namespace="example-b",
                    output_namespace="output/videos/example_b")
        self.other = ChannelConfig.model_validate(data)
        self.a = RuntimePaths(MINUTE_MYSTERIES, self.root)
        self.b = RuntimePaths(self.other, self.root)

    def test_same_slot_path_plans_do_not_collide(self):
        for field in ("temp", "audio", "clips", "subtitles", "output", "memory"):
            self.assertNotEqual(getattr(self.a, field), getattr(self.b, field))
        self.assertNotEqual(self.a.audio/"voice.mp3", self.b.audio/"voice.mp3")
        self.assertNotEqual(self.a.clips/"scene_0.mp4", self.b.clips/"scene_0.mp4")
        self.assertNotEqual(self.a.output/"unsolved_mysteries_20261006050000.mp4", self.b.output/"unsolved_mysteries_20261006050000.mp4")

    def test_legacy_memory_default_only(self):
        from engine.generators.idea_generator import load_memory
        self.a.prepare(); self.b.prepare()
        self.a.memory.write_text(json.dumps({"unsolved mysteries": ["Old idea"]}))
        self.assertEqual(load_memory(self.a.memory), {"unsolved mysteries": ["Old idea"]})
        self.assertEqual(load_memory(self.b.memory), {})
        self.assertEqual(self.a.output, self.root/"output/videos")

    def test_cleanup_cannot_claim_other_channel_path(self):
        self.b.prepare(); day = date(2026,10,6)
        path = self.b.output/"unsolved_mysteries_20261006050000.mp4"; path.write_bytes(b"render")
        record = record_generated_file(path,day,"10:00",channel=self.other)
        item = {"slot":"10:00", "video_path":str(path), "generated_files":[record], "channel_key":self.other.channel_key}
        with self.assertRaises(ValueError): _validated_target(item,day,self.a.output,{str(path):1})
        self.assertTrue(path.exists())
        self.assertEqual(_validated_target(item,day,self.b.output,{str(path):1},channel=self.other),path)

    def test_nondefault_cannot_use_legacy_output(self):
        data=self.other.model_dump();data["output_namespace"]="output/videos"
        with self.assertRaises(ValueError):RuntimePaths(ChannelConfig.model_validate(data),self.root)

    def test_stock_cache_returns_independent_working_copies(self):
        from engine.video import pexels_fetcher as fetch
        cache=self.root/"cache";cache.mkdir(); original=cache/"signal_0.mp4";original.write_bytes(b"stock")
        self.a.prepare();self.b.prepare()
        with patch.object(fetch,"CLIPS_LIBRARY_DIR",str(cache)),patch.object(fetch.requests,"get") as api:
            a=fetch.download_pexels_clips("signal",str(self.a.clips))
            b=fetch.download_pexels_clips("signal",str(self.b.clips))
            api.assert_not_called()
        self.assertNotEqual(a,b);Path(a[0]).write_bytes(b"edited")
        self.assertEqual(original.read_bytes(),b"stock");self.assertEqual(Path(b[0]).read_bytes(),b"stock")

    def test_pipeline_uses_resolved_paths_without_publish(self):
        from engine.pipeline import video_pipeline as pipeline
        with patch.object(pipeline,"generate_script",return_value="Script"),patch.object(pipeline,"generate_voice") as voice,patch.object(pipeline,"generate_scenes",return_value=["signal"]),patch.object(pipeline,"download_pexels_clips",return_value=["clip"]) as clips,patch.object(pipeline,"generate_subtitles",return_value=[]),patch.object(pipeline,"select_background_music",return_value=None),patch.object(pipeline,"build_short_video") as render,patch.object(pipeline,"generate_metadata",return_value={}),patch.object(pipeline,"upload_generated_video") as upload:
            result=pipeline.generate_video(idea="Known idea",publish=False,runtime_paths=self.b)
        self.assertEqual(voice.call_args.args[1],str(self.b.audio/"voice.mp3"))
        self.assertEqual(clips.call_args.args[1],str(self.b.clips))
        self.assertEqual(Path(result["video_path"]).parent,self.b.output)
        self.assertEqual(Path(render.call_args.kwargs["temp_audiofile"]).parent,self.b.audio)
        upload.assert_not_called()

if __name__ == "__main__": unittest.main()


class SingleVideoChannelTests(unittest.TestCase):
    def test_space_generators_receive_config_without_changing_default_prompts(self):
        from config.channels import resolve_channel_config
        from engine.generators import idea_generator,script_generator,metadata_generator
        from types import SimpleNamespace
        channel=resolve_channel_config("space_science")
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A radio anomaly"))])
        with tempfile.TemporaryDirectory() as root:
            memory=Path(root)/"ideas.json"
            for module,call in ((idea_generator,lambda:idea_generator.generate_video_ideas(channel.topic_theme,memory_file=memory,channel_config=channel)),(script_generator,lambda:script_generator.generate_script("A radio anomaly",channel_config=channel)),(metadata_generator,lambda:metadata_generator.generate_metadata("A radio anomaly",channel_config=channel))):
                client=Mock();client.chat.completions.create.return_value=response
                with patch.object(module,"client",client):call()
                prompt=client.chat.completions.create.call_args.kwargs['messages'][0]['content']
                self.assertIn(channel.channel_name,prompt);self.assertIn(channel.content_instructions,prompt)
                self.assertNotIn("Mystery Shorts",prompt)

    def workflow(self, *, before_mismatch=False, after_mismatch=False, upload=True, attempt="1"):
        import os
        from config.channels import resolve_channel_config
        from engine.pipeline import video_pipeline
        from engine.uploader import youtube_uploader as yt
        channel=resolve_channel_config("space_science")
        text=Path('.github/workflows/test-video-generation.yml').read_text()
        code=text.split("          python - <<'PY'\n")[2].split("          PY",1)[0]
        code="\n".join(line[10:] for line in code.splitlines())
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); video=root/'space.mp4';video.write_bytes(b'render')
            generated={'video_path':str(video),'metadata':{'Title':'Space','Description':'Science','Hashtags':['#shorts','#space']}}
            service=Mock()
            service.channels.return_value.list.return_value.execute.return_value={'items':[{'id':'wrong' if before_mismatch else channel.youtube_channel_id}]}
            service.videos.return_value.list.return_value.execute.return_value={'items':[{'id':'known','snippet':{'channelId':'wrong' if after_mismatch else channel.youtube_channel_id},'status':{'privacyStatus':'private'}}]}
            env={'INPUT_CHANNEL_KEY':'space_science','INPUT_UPLOAD_PRIVATE':'true' if upload else 'false','SELECTED_YOUTUBE_TOKEN':'Zml4dHVyZQ==','OPENAI_API_KEY':'fixture','PEXELS_API_KEY':'fixture','GITHUB_OUTPUT':str(root/'outputs'),'GITHUB_RUN_ATTEMPT':attempt}
            old=os.getcwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ,env),patch.object(yt,'YOUTUBE_TOKEN_FILE',str(root/'token.pickle')),patch.object(yt.pickle,'load',return_value=Mock()),patch.object(yt,'get_authenticated_service',return_value=service),patch.object(video_pipeline,'generate_video',return_value=generated) as generate,patch.object(yt,'upload_video',return_value={'id':'known'}) as insert:
                    failed=False
                    try:exec(compile(code,'actual single-video workflow','exec'),{})
                    except (RuntimeError,ValueError):failed=True
                receipt=json.loads((root/'single-video-test.json').read_text()) if (root/'single-video-test.json').exists() else None
            finally:os.chdir(old)
            return failed,generate,insert,service,receipt

    def test_actual_workflow_one_private_upload_and_verification(self):
        failed,generate,insert,service,receipt=self.workflow()
        self.assertFalse(failed);generate.assert_called_once();insert.assert_called_once()
        self.assertFalse(generate.call_args.kwargs['publish'])
        self.assertEqual(generate.call_args.kwargs['runtime_paths'].channel.channel_key,'space_science')
        self.assertNotIn('publish_at',insert.call_args.kwargs)
        self.assertEqual(insert.call_args.kwargs['credential_context'].channel.youtube_channel_id,'UCfs8yJirS4w54IEiwSlM9xA')
        self.assertTrue(receipt['upload_verified']);self.assertEqual(receipt['youtube_video_id'],'known')
        service.videos.return_value.list.assert_called_once_with(part='snippet,status',id='known')

    def test_wrong_channel_blocks_before_generation_and_upload(self):
        failed,generate,insert,_,_=self.workflow(before_mismatch=True)
        self.assertTrue(failed);generate.assert_not_called();insert.assert_not_called()

    def test_post_upload_mismatch_retains_id_and_never_retries(self):
        failed,_,insert,_,receipt=self.workflow(after_mismatch=True)
        self.assertTrue(failed);insert.assert_called_once();self.assertEqual(receipt['youtube_video_id'],'known')

    def test_default_render_only_and_upload_rerun_refusal(self):
        failed,generate,insert,service,_=self.workflow(upload=False)
        self.assertFalse(failed);generate.assert_called_once();insert.assert_not_called();service.channels.assert_not_called()
        failed,generate,insert,_,_=self.workflow(attempt='2')
        self.assertTrue(failed);generate.assert_not_called();insert.assert_not_called()
