"""Offline long-form contracts and real preparation recovery using fake external services."""
import copy
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch
from zoneinfo import ZoneInfo

from config.channels import CHANNEL_CONFIGS, MINUTE_MYSTERIES, RuntimePaths, slot_owner, rendered_media_filename
from config.formats import longform_channel, LONGFORM_GUIDANCE
from engine.scheduling.preparation import Preparation, target_day, publication_time
from engine.uploader.youtube_uploader import YouTubeCredentialContext
from run_actions import validate_state, ProductionError
from test_actions import Store

DAY = date(2026, 11, 28)
NOW = datetime(2026, 11, 27, 2, tzinfo=ZoneInfo('Asia/Kolkata'))


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class LongformTests(unittest.TestCase):
    def test_profiles_paths_and_identities_are_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            memories, outputs, owners = set(), set(), set()
            for short in CHANNEL_CONFIGS:
                before = short.model_dump()
                long = longform_channel(short)
                self.assertEqual(short.model_dump(), before)
                self.assertEqual(short.publication_slots, ('10:00', '14:00', '19:00'))
                self.assertEqual(long.publication_slots, ('21:00',))
                self.assertEqual(long.preparation_times, ('02:00',))
                for channel in (short, long):
                    paths = RuntimePaths(channel, Path(root))
                    memories.add(paths.memory); outputs.add(paths.output)
                    owners.add(slot_owner(channel, DAY, channel.publication_slots[0]))
                self.assertIn(':longform:21:00', slot_owner(long, DAY, '21:00'))
                self.assertEqual(YouTubeCredentialContext(short).token_file, YouTubeCredentialContext(long).token_file)
            self.assertEqual(len(memories), 6); self.assertEqual(len(outputs), 6); self.assertEqual(len(owners), 6)

    def test_next_calendar_day_21_ist(self):
        self.assertEqual(target_day(NOW), DAY)
        self.assertEqual(publication_time(DAY, '21:00'), '2026-11-28T15:30:00+00:00')
        self.assertEqual(target_day(datetime(2026,12,31,2,tzinfo=NOW.tzinfo)), date(2027,1,1))

    def test_independent_ideas_and_format_specific_duplicate_memory(self):
        from engine.generators import idea_generator as module
        with tempfile.TemporaryDirectory() as root:
            short = RuntimePaths(MINUTE_MYSTERIES, Path(root))
            long = RuntimePaths(longform_channel(MINUTE_MYSTERIES), Path(root))
            short.prepare(); long.prepare()
            short.memory.write_text(json.dumps({'unsolved mysteries':['The disappearance of Flight 19']}))
            original = short.memory.read_bytes()
            client = Mock(); client.chat.completions.create.return_value = response('The disappearance of Flight 19')
            with patch.object(module, 'client', client):
                self.assertEqual(module.generate_video_ideas(long.channel.topic_theme, memory_file=long.memory, channel_config=long.channel), ['The disappearance of Flight 19'])
                with self.assertRaises(ValueError):
                    module.generate_video_ideas(long.channel.topic_theme, memory_file=long.memory, channel_config=long.channel)
            self.assertEqual(original, short.memory.read_bytes())
            prompt = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
            self.assertIn('exactly ONE', prompt); self.assertIn('prior LONG-FORM stories', prompt)

    def test_channel_native_prompts_and_metadata(self):
        from engine.generators import script_generator, metadata_generator
        from config.content_policy import GLOBAL_GUARDRAIL_INSTRUCTIONS
        for short in CHANNEL_CONFIGS:
            long = longform_channel(short)
            client = Mock(); client.chat.completions.create.return_value=response('Evidence ' * 700)
            with patch.object(script_generator, 'client', client):
                script_generator.generate_script('A documented event', channel_config=long)
            prompt=client.chat.completions.create.call_args.kwargs['messages'][0]['content']
            self.assertIn(LONGFORM_GUIDANCE[short.channel_key], prompt)
            self.assertIn('650-900', prompt); self.assertIn(GLOBAL_GUARDRAIL_INSTRUCTIONS, prompt)
            self.assertNotIn('Around 80 words', prompt)
            client.chat.completions.create.return_value=response('TITLE: A documented event\nDESCRIPTION: The evidence and consequences.\nHASHTAGS: #shorts #history #evidence')
            with patch.object(metadata_generator, 'client', client):
                metadata=metadata_generator.generate_metadata('A documented event.', channel_config=long)
            self.assertEqual(metadata['Hashtags'], ['#history','#evidence'])

    def prepare_fixture(self, root, store=None):
        channel = longform_channel(MINUTE_MYSTERIES)
        store = store or Store()
        controller = Preparation(store, DAY, root, channel=channel)
        context = YouTubeCredentialContext(channel)
        def generate():
            path=controller.output / rendered_media_filename(channel,'20261127020000')
            path.write_bytes(b'landscape fixture')
            return {'video_path':str(path),'metadata':{'Title':'Evidence','Description':'Documented evidence','Hashtags':['#history']},'idea':'A documented event','script':'Narration'}
        def verify(video_id, expected, publish_at):
            return {'verified':True,'video_id':video_id,'channel_id':expected,'publish_at':publish_at,'privacy_status':'private','processing_status':'succeeded'}
        upload=Mock(return_value={'success':True,'status':'uploaded','video_id':'long-id','privacy_status':'private'})
        kwargs=dict(expected_channel=channel.youtube_channel_id,generator=Mock(side_effect=generate),uploader=upload,
            verifier=Mock(side_effect=verify),notifier=Mock(return_value={'success':True}),now=lambda:NOW,credential_context=context)
        return controller, store, kwargs

    def test_one_target_ready_and_ephemeral_rerun_does_not_upload(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'YOUTUBE_PRIVACY_STATUS':'private'}):
            controller,store,kwargs=self.prepare_fixture(Path(root)/'first')
            self.assertEqual(controller.prepare(**kwargs)['status'],'READY')
            kwargs['generator'].assert_called_once();kwargs['uploader'].assert_called_once()
            state=controller.state
            self.assertEqual(state['content_format'],'longform')
            self.assertEqual(state['queue']['content_format'],'longform')
            self.assertEqual(len(state['queue']['videos']),1)
            record=state['queue']['videos'][0]['generated_files'][0]
            self.assertIn('-longform-media-',record['artifact_name'])
            self.assertIn(':longform:21:00',record['owner'])
            with self.assertRaises(ProductionError):validate_state(state,DAY,channel=MINUTE_MYSTERIES)
            fresh=Preparation(store,DAY,Path(root)/'second',channel=controller.channel)
            self.assertTrue(fresh.prepare(**kwargs)['no_op'])
            kwargs['uploader'].assert_called_once()

    def test_uncertain_upload_fails_closed_on_rerun(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'YOUTUBE_PRIVACY_STATUS':'private'}):
            controller,store,kwargs=self.prepare_fixture(Path(root)/'first')
            kwargs['uploader'].side_effect=TimeoutError()
            result=controller.prepare(**kwargs)
            self.assertEqual(result['status'],'PREPARING')
            self.assertEqual(controller.state['queue']['videos'][0]['upload_status'],'uncertain')
            fresh=Preparation(store,DAY,Path(root)/'second',channel=controller.channel)
            fresh.prepare(**kwargs)
            kwargs['uploader'].assert_called_once();kwargs['generator'].assert_called_once()

    def test_known_id_is_verified_without_reupload(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'YOUTUBE_PRIVACY_STATUS':'private'}):
            controller,store,kwargs=self.prepare_fixture(Path(root)/'first')
            verify=kwargs['verifier'].side_effect
            kwargs['verifier'].side_effect=TimeoutError()
            controller.prepare(**kwargs)
            self.assertEqual(controller.state['queue']['videos'][0]['youtube']['video_id'],'long-id')
            kwargs['verifier'].side_effect=verify
            fresh=Preparation(store,DAY,Path(root)/'second',channel=controller.channel)
            self.assertEqual(fresh.prepare(**kwargs)['status'],'READY')
            kwargs['uploader'].assert_called_once();kwargs['generator'].assert_called_once()

    def test_generation_failure_has_no_upload_and_bounded_retry(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'YOUTUBE_PRIVACY_STATUS':'private'}):
            controller,store,kwargs=self.prepare_fixture(root)
            kwargs['generator'].side_effect=ValueError('fixture generation failure')
            self.assertEqual(controller.prepare(**kwargs)['status'],'PREPARING')
            kwargs['uploader'].assert_not_called()
            self.assertEqual(controller.state['queue']['videos'][0]['generation_attempts'],1)

    def test_legacy_slots_rejected_in_longform_queue(self):
        with tempfile.TemporaryDirectory() as root:
            controller,_,_=self.prepare_fixture(root)
            with self.assertRaises(ValueError):controller.queue.generate_batch(DAY,Mock(),slots=['10:00'])

    def test_longform_direct_publish_is_blocked(self):
        from engine.pipeline.video_pipeline import generate_video
        with tempfile.TemporaryDirectory() as root:
            paths=RuntimePaths(longform_channel(MINUTE_MYSTERIES),Path(root))
            with self.assertRaises(ValueError):generate_video(runtime_paths=paths)


    def test_renderer_dimensions_pacing_and_timed_captions(self):
        from engine.video import video_editor as renderer
        with patch.object(renderer, 'AudioFileClip') as audio:
            with self.assertRaises(ValueError):
                renderer.build_short_video(['fixture'], 'voice', 'out', content_format='longform')
            audio.assert_not_called()
        clip = Mock(duration=20, w=1920, h=1080)
        clip.subclipped.side_effect = lambda start,end: Mock(duration=end-start, w=1920, h=1080,
            resized=Mock(return_value=clip))
        clip.resized.return_value=clip; clip.cropped.return_value=clip
        with patch.object(renderer, 'VideoFileClip', return_value=clip), patch.object(renderer.random,'uniform',return_value=10) as random:
            segments=renderer._make_fast_cut_clips(['one','two'],15,1920,1080,content_format='longform')
        self.assertTrue(segments)
        self.assertIn(((8,14),), [(call.args,) for call in random.call_args_list])
        text=Mock();text.with_start.return_value=text;text.with_duration.return_value=text;text.with_position.return_value=text
        with patch.object(renderer,'TextClip',return_value=text):
            renderer._build_subtitle_clips([{'text':'Evidence','start':12.5,'end':15}],300,1920,1080,font_size=42)
        text.with_start.assert_called_once_with(12.5);text.with_duration.assert_called_once_with(2.5)

    def test_landscape_media_cache_and_query_are_separate(self):
        from engine.video import pexels_fetcher as media
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);cache=root/'cache';cache.mkdir();working=root/'working'
            (cache/'ocean_0.mp4').write_bytes(b'portrait')
            response=Mock();response.json.return_value={'videos':[{'video_files':[
                {'file_type':'video/mp4','width':720,'height':1280,'link':'portrait'},
                {'file_type':'video/mp4','width':1920,'height':1080,'link':'landscape'}]}]}
            download=MagicMock(); download.__enter__.return_value=download
            download.iter_content.return_value=[b'landscape']
            with patch.object(media,'CLIPS_LIBRARY_DIR',str(cache)),patch.object(media.requests,'get',side_effect=[response,download]) as get:
                paths=media.download_pexels_clips('ocean',str(working),orientation='landscape')
            self.assertEqual(get.call_args_list[0].kwargs['params']['orientation'],'landscape')
            self.assertEqual(get.call_args_list[1].args[0],'landscape')
            self.assertEqual(Path(paths[0]).read_bytes(),b'landscape')
            self.assertEqual((cache/'ocean_0.mp4').read_bytes(),b'portrait')

    def test_duration_gate_and_bounded_tts_chunks(self):
        from engine.media import voice_generator as voice
        audio=Mock(duration=25)
        joined=Mock()
        with tempfile.TemporaryDirectory() as root,patch.object(voice,'generate_voice') as generate, \
             patch('moviepy.AudioFileClip',return_value=audio),patch('moviepy.concatenate_audioclips',return_value=joined):
            details=voice.generate_longform_voice(('Evidence ' * 49 + 'matters. ') * 14, str(Path(root)/'voice.mp3'))
            self.assertEqual(details['duration'],350)
            self.assertTrue(all(len(call.args[0].split())<=65 for call in generate.call_args_list))
            self.assertEqual(details['subtitles'][0]['start'],0)
            self.assertEqual(details['subtitles'][-1]['end'],350)
        with tempfile.TemporaryDirectory() as root,patch.object(voice,'generate_voice'), \
             patch('moviepy.AudioFileClip',return_value=Mock(duration=1)),patch('moviepy.concatenate_audioclips',return_value=Mock()):
            with self.assertRaises(ValueError):voice.generate_longform_voice('Too short.',str(Path(root)/'voice.mp3'))


    def test_music_reuses_assets_or_creates_local_bed(self):
        from engine.media import music_selector
        import wave
        with patch.object(music_selector,'select_background_music',return_value='existing.mp3'):
            self.assertEqual(music_selector.select_longform_music('unused.wav'),'existing.mp3')
        with tempfile.TemporaryDirectory() as root,patch.object(music_selector,'select_background_music',return_value=None):
            path=music_selector.select_longform_music(Path(root)/'bed.wav')
            with wave.open(path) as stream:
                self.assertEqual(stream.getnframes()/stream.getframerate(),60)


    def test_script_length_repair_is_bounded_and_keeps_same_story(self):
        from engine.generators import script_generator as module
        client=Mock();client.chat.completions.create.side_effect=[response('Too short.'),response('Evidence ' * 750)]
        channel=longform_channel(MINUTE_MYSTERIES)
        with patch.object(module,'client',client):
            script=module.generate_script('A documented expedition',channel_config=channel)
        self.assertEqual(len(script.split()),750);self.assertEqual(client.chat.completions.create.call_count,2)
        self.assertIn('same standalone story',client.chat.completions.create.call_args.kwargs['messages'][-1]['content'])
        client=Mock();client.chat.completions.create.return_value=response('Still too short.')
        with patch.object(module,'client',client),self.assertRaises(ValueError):
            module.generate_script('A documented expedition',channel_config=channel)
        self.assertEqual(client.chat.completions.create.call_count,2)


    def test_local_resume_does_not_regenerate_story_or_audio(self):
        from engine.pipeline import video_pipeline as pipeline
        with tempfile.TemporaryDirectory() as root:
            channel=longform_channel(MINUTE_MYSTERIES);paths=RuntimePaths(channel,Path(root));paths.prepare()
            (paths.audio/'voice.mp3').write_bytes(b'voice fixture')
            saved={'channel_key':channel.channel_key,'content_format':'longform','topic':channel.topic_theme,
                'idea':'An expedition','script':'Evidence ' * 700,
                'metadata':{'Title':'An expedition','Description':'Documented evidence','Hashtags':['#history']},
                'scenes':['ocean']*6,'voice_details':{'duration':300,'subtitles':[],'chunks':12}}
            (paths.audio/'generation-progress.json').write_text(json.dumps(saved))
            with patch.object(pipeline,'generate_video_ideas') as ideas,patch.object(pipeline,'generate_script') as script, \
                 patch.object(pipeline,'generate_metadata') as metadata,patch.object(pipeline,'generate_scenes') as scenes, \
                 patch('engine.media.voice_generator.generate_longform_voice') as voice, \
                 patch('engine.media.music_selector.select_longform_music',return_value=None), \
                 patch.object(pipeline,'download_pexels_clips',side_effect=[[f'clip-{i*2}',f'clip-{i*2+1}'] for i in range(6)]), \
                 patch.object(pipeline,'build_short_video',side_effect=RuntimeError('stop before render')):
                with self.assertRaisesRegex(RuntimeError,'stop before render'):
                    pipeline.generate_video(publish=False,runtime_paths=paths,resume=True)
                for call in (ideas,script,metadata,scenes,voice):call.assert_not_called()

    def test_interrupted_landscape_download_never_publishes_partial_cache(self):
        from engine.video import pexels_fetcher as media
        with tempfile.TemporaryDirectory() as root:
            response=MagicMock();response.__enter__.return_value=response
            response.iter_content.side_effect=media.requests.exceptions.ChunkedEncodingError()
            target=Path(root)/'clip.mp4'
            with patch.object(media,'CLIPS_LIBRARY_DIR',root),patch.object(media.requests,'get',return_value=response) as get:
                with self.assertRaises(media.requests.exceptions.ChunkedEncodingError):media._download_landscape('fixture',target)
            self.assertEqual(get.call_count,2)
            self.assertFalse(target.exists());self.assertEqual(list(Path(root).iterdir()),[])


    def test_failed_landscape_transfer_is_skipped_without_partial_cache(self):
        from engine.video import pexels_fetcher as media
        with tempfile.TemporaryDirectory() as root:
            cache=Path(root)/'cache';cache.mkdir()
            response=Mock();response.json.return_value={'videos':[{'video_files':[
                {'file_type':'video/mp4','width':1920,'height':1080,'link':'fixture'}]}]}
            with patch.object(media,'CLIPS_LIBRARY_DIR',str(cache)),patch.object(media.requests,'get',return_value=response), \
                 patch.object(media,'_download_landscape',side_effect=TimeoutError()):
                self.assertEqual(media.download_pexels_clips('ocean',str(Path(root)/'work'),orientation='landscape'),[])
            self.assertEqual(list(cache.glob('*.mp4')),[])


if __name__=='__main__':unittest.main()
