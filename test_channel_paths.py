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
