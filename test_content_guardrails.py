"""Offline global editorial policy and early-generation rejection regressions."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from config.channels import MINUTE_MYSTERIES, ChannelConfig, RuntimePaths
from config.content_policy import ContentGuardrailError, GLOBAL_GUARDRAIL_INSTRUCTIONS, validate_content


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class ContentGuardrailTests(unittest.TestCase):
    def test_excluded_categories(self):
        for text in ('Religious doctrine explained', 'The teachings of a religion', 'Zeus and the Greek gods',
                     'An ancient deity', 'Jesus performed a miracle', 'A prophet changed history',
                     'A miracle cured the illness', 'Caste disputes', 'Caste-based violence', 'Communal violence',
                     'Sectarian conflict', 'Racial supremacy and ethnic cleansing'):
            with self.subTest(text=text),self.assertRaises(ContentGuardrailError):validate_content(text)

    def test_safe_channel_content_and_word_boundaries(self):
        for text in ('A pulsar emits radio signals. Jupiter has many moons.',
                     'A lost expedition left detailed records.', 'The Tunguska explosion remains debated.',
                     'The signal appeared for 72 seconds.', 'A communal kitchen served explorers.',
                     'Castellated walls stand near the shipyard.', 'An odinometer prototype was tested.', 'Robert Goddard worked on rockets.',
                     'The god particle is a nickname for the Higgs boson.'):
            with self.subTest(text=text):validate_content(text)
        with self.assertRaises(ContentGuardrailError):validate_content('The god particle proves a deity exists')

    def test_generated_ideas_filtered_before_memory(self):
        from engine.generators import idea_generator
        client=Mock();client.chat.completions.create.return_value=response('A religious miracle\nA lost expedition')
        with patch.object(idea_generator,'client',client),patch.object(idea_generator,'load_memory',return_value={}),patch.object(idea_generator,'save_memory') as save:
            self.assertEqual(idea_generator.generate_video_ideas(),['A lost expedition'])
        self.assertEqual(save.call_args.args[0],{'unsolved mysteries':['A lost expedition']})
        self.assertIn(GLOBAL_GUARDRAIL_INSTRUCTIONS,client.chat.completions.create.call_args.kwargs['messages'][0]['content'])

    def test_no_safe_idea_raises_for_normal_retry_without_memory_write(self):
        from engine.generators import idea_generator
        client=Mock();client.chat.completions.create.return_value=response('A deity revealed\nSectarian violence')
        with patch.object(idea_generator,'client',client),patch.object(idea_generator,'load_memory',return_value={}),patch.object(idea_generator,'save_memory') as save:
            with self.assertRaises(ContentGuardrailError):idea_generator.generate_video_ideas()
        save.assert_not_called();self.assertEqual(client.chat.completions.create.call_count,1)

    def test_script_and_metadata_outputs_are_validated(self):
        from engine.generators import script_generator,metadata_generator
        for module,call,text in ((script_generator,lambda:script_generator.generate_script('A lost expedition'),'A miracle guided them.'),
                                (metadata_generator,lambda:metadata_generator.generate_metadata('A radio signal appeared.'),'TITLE: A deity speaks\nDESCRIPTION: Space\nHASHTAGS: #shorts')):
            client=Mock();client.chat.completions.create.return_value=response(text)
            with patch.object(module,'client',client),self.assertRaises(ContentGuardrailError):call()
            self.assertIn(GLOBAL_GUARDRAIL_INSTRUCTIONS,client.chat.completions.create.call_args.kwargs['messages'][0]['content'])

    def test_future_channel_receives_policy_without_registry_branch(self):
        from engine.generators import idea_generator
        data=MINUTE_MYSTERIES.model_dump();data.update(channel_key='future_channel',topic_theme='archaeological discoveries',
            state_namespace='.future-state',artifact_namespace='future',output_namespace='output/future')
        channel=ChannelConfig.model_validate(data)
        client=Mock();client.chat.completions.create.return_value=response('An ancient deity')
        with tempfile.TemporaryDirectory() as root,patch.object(idea_generator,'client',client):
            with self.assertRaises(ContentGuardrailError):idea_generator.generate_video_ideas(channel.topic_theme,memory_file=Path(root)/'memory.json',channel_config=channel)
        prompt=client.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn(channel.topic_theme,prompt);self.assertIn(GLOBAL_GUARDRAIL_INSTRUCTIONS,prompt)

    def test_pipeline_rejects_idea_script_or_metadata_before_any_media(self):
        from engine.pipeline import video_pipeline as pipeline
        for stage in ('idea','script','metadata'):
            with tempfile.TemporaryDirectory() as root:
                paths=RuntimePaths(MINUTE_MYSTERIES,Path(root))
                with patch.object(pipeline,'generate_video_ideas',return_value=['A deity' if stage=='idea' else 'A lost expedition']), \
                     patch.object(pipeline,'generate_script',return_value='A miracle occurred.' if stage=='script' else 'The expedition left records.'), \
                     patch.object(pipeline,'generate_metadata',return_value={'Title':'A religious figure' if stage=='metadata' else 'Expedition','Description':'Historical records','Hashtags':['#shorts']}), \
                     patch.object(pipeline,'generate_voice') as voice,patch.object(pipeline,'generate_scenes') as scenes, \
                     patch.object(pipeline,'download_pexels_clips') as stock,patch.object(pipeline,'build_short_video') as render, \
                     patch.object(pipeline,'upload_generated_video') as upload:
                    with self.assertRaises(ContentGuardrailError):pipeline.generate_video(publish=False,runtime_paths=paths)
                    for blocked in (voice,scenes,stock,render,upload):blocked.assert_not_called()

    def test_explicit_blocked_idea_rejected_before_script_call(self):
        from engine.pipeline import video_pipeline as pipeline
        with tempfile.TemporaryDirectory() as root,patch.object(pipeline,'generate_script') as script:
            with self.assertRaises(ContentGuardrailError):pipeline.generate_video(idea='Religious doctrine',publish=False,runtime_paths=RuntimePaths(MINUTE_MYSTERIES,Path(root)))
        script.assert_not_called()
