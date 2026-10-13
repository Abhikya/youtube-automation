"""Offline data-driven onboarding; all edits stay in temporary registries."""
import copy,json,tempfile,unittest,io
from contextlib import redirect_stdout
from pathlib import Path
from config.channels import CHANNEL_DATA_FILE, load_channel_configs, parse_channel_configs, resolve_channel_config, RuntimePaths

EXPECTED_EXISTING = [{'channel_key': 'minute_mysteries',
  'channel_name': 'Minute Mysteries',
  'youtube_channel_id': 'UCR2L8UE-pc1Hd7U3AurrECQ',
  'credential_reference': 'YOUTUBE_TOKEN_PICKLE_B64',
  'topic_theme': 'unsolved mysteries',
  'content_instructions': 'Documented unsolved mysteries; curiosity-driven Shorts. Strong hook in '
                          'first 3 seconds; around 80 words and 40 seconds; simple storytelling. '
                          'Distinguish known facts from theories and unresolved questions; label '
                          'speculation and folklore explicitly. Never invent evidence, quotes or a '
                          'definitive solution. Keep titles and descriptions faithful to the '
                          'script.',
  'timezone': 'Asia/Kolkata',
  'publication_slots': ['10:00', '14:00', '19:00'],
  'preparation_times': ['05:05', '09:05', '13:05', '17:05', '21:05'],
  'state_namespace': '.actions-work',
  'artifact_namespace': 'mystery',
  'output_namespace': 'output/videos',
  'telegram_chat_reference': 'TELEGRAM_CHAT_ID',
  'telegram_credential_reference': 'TELEGRAM_BOT_TOKEN'},
 {'channel_key': 'space_science',
  'channel_name': 'Space and Strange Science',
  'youtube_channel_id': 'UCfs8yJirS4w54IEiwSlM9xA',
  'credential_reference': 'YOUTUBE_TOKEN_SPACE_SCIENCE_B64',
  'topic_theme': 'astronomy, space mysteries, strange science',
  'content_instructions': 'Factual, curiosity-driven, science-focused storytelling.',
  'timezone': 'Asia/Kolkata',
  'publication_slots': ['10:00', '14:00', '19:00'],
  'preparation_times': ['05:05', '09:05', '13:05', '17:05', '21:05'],
  'state_namespace': '.actions-work-space-science',
  'artifact_namespace': 'space-science',
  'output_namespace': 'output/space_science',
  'telegram_chat_reference': 'TELEGRAM_CHAT_SPACE_SCIENCE_ID',
  'telegram_credential_reference': 'TELEGRAM_BOT_SPACE_SCIENCE_TOKEN'},
 {'channel_key': 'hidden_history',
  'channel_name': 'hiddenhistory',
  'youtube_channel_id': 'UCWsMUjiZ7q07Bb-MKhXojDw',
  'credential_reference': 'YOUTUBE_TOKEN_HIDDEN_HISTORY_B64',
  'topic_theme': 'forgotten history, unusual historical incidents, strange moments from history',
  'content_instructions': 'Factual, concise, curiosity-driven historical storytelling. Focus on '
                          'lesser-known real historical events and unusual incidents. Avoid '
                          'sensational claims that are not historically supportable.',
  'timezone': 'Asia/Kolkata',
  'publication_slots': ['10:00', '14:00', '19:00'],
  'preparation_times': ['05:05', '09:05', '13:05', '17:05', '21:05'],
  'state_namespace': '.actions-work-hidden-history',
  'artifact_namespace': 'hidden-history',
  'output_namespace': 'output/hidden_history',
  'telegram_chat_reference': 'TELEGRAM_CHAT_HIDDEN_HISTORY_ID',
  'telegram_credential_reference': 'TELEGRAM_BOT_HIDDEN_HISTORY_TOKEN'}]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads(CHANNEL_DATA_FILE.read_text())
        self.new={'channel_key':'channel_four','channel_name':'Channel Four','youtube_channel_id':'UC'+'4'*22,
                  'credential_reference':'YOUTUBE_TOKEN_CHANNEL_FOUR_B64','topic_theme':'inventions',
                  'content_instructions':'Factual invention history.'}

    def test_existing_three_are_identical(self):
        self.assertEqual([c.model_dump(mode='json') for c in load_channel_configs()],EXPECTED_EXISTING)

    def test_data_only_fourth_channel_and_guardrails(self):
        from config.content_policy import ContentGuardrailError
        self.data['channels'].append(self.new)
        registry=parse_channel_configs(json.dumps(self.data))
        c=resolve_channel_config('channel_four',configs=registry)
        self.assertEqual(c.state_namespace,'.actions-work-channel-four')
        paths=RuntimePaths(c,Path.cwd())
        self.assertEqual(paths.memory,Path.cwd()/'data/channels/channel_four/idea_memory.json')
        from unittest.mock import Mock,patch
        from types import SimpleNamespace
        from engine.generators import idea_generator
        client=Mock();client.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='A religious doctrine'))])
        with tempfile.TemporaryDirectory() as root,patch.object(idea_generator,'client',client):
            with self.assertRaises(ContentGuardrailError):
                idea_generator.generate_video_ideas(c.topic_theme,memory_file=Path(root)/'memory.json',channel_config=c)
        self.assertEqual(len({v.output_namespace for v in registry}),4)

    def test_duplicate_key_id_and_derived_namespace_conflicts(self):
        for change in ({'channel_key':'space_science'},{'youtube_channel_id':EXPECTED_EXISTING[0]['youtube_channel_id']},{'channel_key':'mystery'},{'channel_key':'videos'}):
            data=copy.deepcopy(self.data);data['channels'].append({**self.new,**change})
            with self.subTest(change=change),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))

    def test_missing_unsafe_inputs_and_legacy_override_rejected(self):
        for change in ({'channel_key':'../escape'},{'credential_reference':'secret value'},{'legacy_namespaces':{'output_namespace':'../escape'}},{'timezone':'bad/timezone'}):
            data=copy.deepcopy(self.data);data['channels'].append({**self.new,**change})
            with self.subTest(change=change),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))
        data=copy.deepcopy(self.data);del data['channels'][0]['topic_theme']
        with self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))

    def test_registry_schema_and_duplicate_json_members_rejected(self):
        for change in ({'schema':2},{'channels':[]},{'unexpected':'value'}):
            data={**self.data,**change}
            with self.subTest(change=change),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))
        with self.assertRaises(ValueError):parse_channel_configs('{"schema":1,"schema":2}')
        data=copy.deepcopy(self.data);data['channels'][0]['legacy_namespaces']['state_namespace']='../escape'
        with self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))

    def test_unsupported_schedules_rejected_in_defaults_and_overrides(self):
        for field,value in (('timezone','UTC'),('publication_slots',['08:00','12:00','18:00']),
                            ('preparation_times',['06:05'])):
            for target in ('defaults','channel'):
                data=copy.deepcopy(self.data)
                record=data['defaults'] if target=='defaults' else data['channels'][1]
                record[field]=value
                with self.subTest(field=field,target=target),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))
        data=copy.deepcopy(self.data);data['channels'][1].update(data['defaults'])
        self.assertEqual([c.model_dump(mode='json') for c in parse_channel_configs(json.dumps(data))],EXPECTED_EXISTING)

    def test_legacy_mapping_required_and_immutable(self):
        data=copy.deepcopy(self.data);del data['channels'][0]['legacy_namespaces']
        with self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))
        for field in ('state_namespace','artifact_namespace','output_namespace','idea_memory_path','token_path'):
            data=copy.deepcopy(self.data);data['channels'][0]['legacy_namespaces'][field]='changed'
            with self.subTest(field=field),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))
        for field in ('idea_memory_path','token_path'):
            data=copy.deepcopy(self.data);data['channels'][0][field]='changed'
            with self.subTest(field=field),self.assertRaises(ValueError):parse_channel_configs(json.dumps(data))

    def test_duplicate_credential_reference_rejected(self):
        self.data['channels'].append({**self.new,'credential_reference':'YOUTUBE_TOKEN_PICKLE_B64'})
        with self.assertRaisesRegex(ValueError,'Duplicate credential reference'):parse_channel_configs(json.dumps(self.data))

    def test_cli_rejects_unsupported_schedule_without_writing(self):
        from tools.channel_onboarding import main
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'channels.json';path.write_text(json.dumps(self.data));before=path.read_bytes()
            args=['--config',str(path),'add']
            for key,value in self.new.items():args.extend(['--'+key.replace('_','-'),value])
            with redirect_stdout(io.StringIO()):self.assertEqual(main(args+['--timezone','UTC']),1)
            self.assertEqual(path.read_bytes(),before)

    def test_cli_atomic_add_list_validate_and_duplicate_refusal(self):
        from tools.channel_onboarding import main
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'channels.json';path.write_text(json.dumps(self.data))
            args=['--config',str(path),'add']
            for key,value in self.new.items():args.extend(['--'+key.replace('_','-'),value])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(args),0)
                before=path.read_bytes()
                self.assertEqual(main(args),1)
                self.assertEqual(main(['--config',str(path),'validate']),0)
                self.assertEqual(main(['--config',str(path),'list']),0)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(len(load_channel_configs(path)),4)
