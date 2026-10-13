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


class GuidedOnboardingTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from tools import channel_onboarding as cli
        from engine.uploader import youtube_uploader as yt
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cli=cli;self.yt=yt
        self.token=self.root/'token.pickle'
        for name in ('production.yml','test-video-generation.yml'):
            target=self.root/'.github/workflows'/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text((cli.PROJECT_ROOT/'.github/workflows'/name).read_text())
        target=self.root/'engine/pipeline/video_pipeline.py';target.parent.mkdir(parents=True)
        target.write_text((cli.PROJECT_ROOT/'engine/pipeline/video_pipeline.py').read_text())
        self.addCleanup(patch.stopall)
        patch.object(cli,'PROJECT_ROOT',self.root).start()
        patch.object(yt,'YOUTUBE_TOKEN_FILE',str(self.token)).start()

    def invoke(self,command,key='minute_mysteries',extra=()):
        output=io.StringIO()
        with redirect_stdout(output):code=self.cli.main([command,'--channel-key',key,*extra])
        return code,json.loads(output.getvalue())

    def test_status_existing_and_missing_token_without_state_mutation(self):
        self.token.write_bytes(b'NEVER_PRINT_SECRET_TOKEN')
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code,status=self.invoke('status')
        self.assertEqual(code,0);self.assertTrue(status['config_exists']);self.assertTrue(status['local_token_exists'])
        self.assertEqual(status['expected_github_secret'],'YOUTUBE_TOKEN_PICKLE_B64')
        self.assertEqual(status['github_secret_status'],'NOT_VERIFIED')
        code,status=self.invoke('status','space_science')
        self.assertFalse(status['local_token_exists']);self.assertEqual(status['readiness'],'NOT_READY')
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_authorize_delegates_and_propagates_failure(self):
        from unittest.mock import patch
        for result in (0,1):
            with patch.object(self.yt,'main',return_value=result) as authorize,redirect_stdout(io.StringIO()):
                self.assertEqual(self.cli.main(['authorize','--channel-key','space_science']),result)
            authorize.assert_called_once_with(['--authorize','--channel-key','space_science'])

    def test_real_authorization_cli_wrong_channel_remains_blocked_offline(self):
        from unittest.mock import Mock,patch
        service=Mock();service.channels.return_value.list.return_value.execute.return_value={'items':[{'id':'wrong'}]}
        with patch.object(self.yt,'get_authenticated_service',return_value=service),redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.main(['authorize','--channel-key','space_science']),1)
        service.videos.assert_not_called();service.close.assert_called_once()

    def test_secret_info_never_reads_or_prints_token(self):
        from unittest.mock import patch
        self.token.write_bytes(b'NEVER_PRINT_SECRET_TOKEN')
        with patch.object(Path,'read_bytes',side_effect=AssertionError('Token bytes must not be read')):
            code,info=self.invoke('secret-info')
        self.assertNotIn('NEVER_PRINT_SECRET_TOKEN',json.dumps(info))
        self.assertIn('Set-Clipboard',info['powershell_command'])
        self.assertEqual(info['github_secret_name'],'YOUTUBE_TOKEN_PICKLE_B64')

    def test_cron_and_private_test_info_are_templates_only(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code,template=self.invoke('cron-template','hidden_history',('--repository','Owner/Repo'))
        self.assertEqual(template['method'],'POST')
        self.assertEqual(template['body'],{'ref':'main','inputs':{'channel_key':'hidden_history','operation':'prepare'}})
        self.assertEqual(template['url'],'https://api.github.com/repos/Owner/Repo/actions/workflows/production.yml/dispatches')
        self.assertEqual(template['preparation_opportunities'],['05:05','09:05','13:05','17:05','21:05'])
        code,info=self.invoke('private-test-info','space_science')
        self.assertEqual(info['inputs'],{'channel_key':'space_science','upload_private':True})
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_readiness_missing_token_and_prepared_synthetic_channel(self):
        from unittest.mock import patch
        code,result=self.invoke('readiness')
        self.assertEqual(code,1);self.assertEqual(result['readiness'],'NOT_READY')
        data=json.loads(CHANNEL_DATA_FILE.read_text())
        data['channels'].append({'channel_key':'ocean_science','channel_name':'Ocean Science','youtube_channel_id':'UC'+'4'*22,
            'credential_reference':'YOUTUBE_TOKEN_OCEAN_SCIENCE_B64','topic_theme':'ocean science','content_instructions':'Factual ocean storytelling.'})
        file=self.root/'channels.json';file.write_text(json.dumps(data))
        configs=load_channel_configs(file)
        with patch('config.channels.CHANNEL_CONFIGS',configs),redirect_stdout(io.StringIO()) as absent:
            self.assertEqual(self.cli.main(['--config',str(file),'status','--channel-key','ocean_science']),0)
        self.assertFalse(json.loads(absent.getvalue())['local_token_exists'])
        token=self.root/'.credentials/ocean_science/token.pickle';token.parent.mkdir(parents=True);token.write_bytes(b'fixture only')
        with patch('config.channels.CHANNEL_CONFIGS',configs),redirect_stdout(io.StringIO()) as output:
            code=self.cli.main(['--config',str(file),'readiness','--channel-key','ocean_science'])
        result=json.loads(output.getvalue())
        self.assertEqual(code,0);self.assertEqual(result['readiness'],'READY_FOR_PRIVATE_TEST')
        self.assertEqual(result['github_secret_status'],'NOT_VERIFIED')
        self.assertTrue(result['global_content_policy_applies']);self.assertTrue(result['paths_namespaces_valid'])
        self.assertEqual(result['missing_steps'],[])

    def test_unknown_channel_not_ready_and_unrelated_receipt_not_accepted(self):
        code,result=self.invoke('status','unknown')
        self.assertEqual(result['readiness'],'NOT_READY');self.assertFalse(result['config_exists'])
        receipt=self.root/'single-video-test.json';receipt.write_text(json.dumps({'channel_key':'space_science','expected_channel_id':'wrong','upload_verified':True,'youtube_video_id':'id','upload_status':'uploaded'}))
        code,result=self.invoke('status','space_science')
        self.assertEqual(result['private_validation_status'],'NOT_VERIFIED')
