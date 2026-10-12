"""Offline credential binding; no real token, API or upload is used."""
import tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch,mock_open
from config.channels import MINUTE_MYSTERIES,ChannelConfig
from engine.uploader import youtube_uploader as yt


class CredentialTests(unittest.TestCase):
    def setUp(self):
        data=MINUTE_MYSTERIES.model_dump();data.update(channel_key='example_b',youtube_channel_id='UC'+'b'*22,credential_reference='OTHER_YOUTUBE_TOKEN',state_namespace='.state-b',artifact_namespace='example-b',output_namespace='output/b')
        self.other=ChannelConfig.model_validate(data)
        self.service=Mock()
        self.service.channels.return_value.list.return_value.execute.return_value={'items':[{'id':MINUTE_MYSTERIES.youtube_channel_id}]}
        self.credentials=Mock();self.credentials.has_scopes.return_value=True
        self.stamp=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()

    def preflight(self,context):
        with patch('builtins.open',mock_open()),patch.object(yt.pickle,'load',return_value=self.credentials),patch.object(yt,'get_authenticated_service',return_value=self.service) as auth:
            yt.preparation_preflight(context.channel.youtube_channel_id,credential_context=context)
        return auth

    def test_default_legacy_path(self):
        context=yt.YouTubeCredentialContext()
        self.assertEqual(context.token_file,Path(yt.YOUTUBE_TOKEN_FILE))
        self.assertEqual(context.credential_reference,'YOUTUBE_TOKEN_PICKLE_B64')

    def test_distinct_paths_and_no_default_fallback(self):
        context=yt.YouTubeCredentialContext(self.other)
        self.assertNotEqual(context.token_file,yt.YouTubeCredentialContext().token_file)
        with patch.object(yt.os.path,'isfile',return_value=False) as exists:
            with self.assertRaises(RuntimeError):yt.get_authenticated_service(credential_context=context)
            exists.assert_called_once_with(context.token_file)

    def test_wrong_reference_rejected(self):
        with self.assertRaises(ValueError):yt.YouTubeCredentialContext(self.other,'YOUTUBE_TOKEN_PICKLE_B64')

    def test_wrong_expected_channel_rejected_before_auth(self):
        context=yt.YouTubeCredentialContext()
        with patch.object(yt,'get_authenticated_service') as auth:
            with self.assertRaises(ValueError):yt.preparation_preflight(self.other.youtube_channel_id,credential_context=context)
            auth.assert_not_called()

    def test_wrong_authenticated_channel_fails_closed(self):
        context=yt.YouTubeCredentialContext();self.service.channels.return_value.list.return_value.execute.return_value={'items':[{'id':self.other.youtube_channel_id}]}
        with self.assertRaises(ValueError):self.preflight(context)
        with self.assertRaises(yt.UploadNotStarted):context.verified_service()
        self.service.videos.assert_not_called()

    def test_preflight_upload_verify_use_same_service_without_reloading_token(self):
        context=yt.YouTubeCredentialContext();auth=self.preflight(context)
        self.service.videos.return_value.list.return_value.execute.return_value={'items':[{'id':'known','snippet':{'channelId':MINUTE_MYSTERIES.youtube_channel_id},'status':{'privacyStatus':'private','publishAt':self.stamp},'processingDetails':{'processingStatus':'succeeded'}}]}
        with patch.object(yt,'get_authenticated_service',side_effect=AssertionError('No second auth')),patch.object(yt.os.path,'isfile',return_value=True),patch.object(yt,'MediaFileUpload'),patch.object(yt,'complete_resumable_upload',return_value={'id':'known'}) as complete:
            result=yt.upload_prepared_video('saved.mp4',{'Title':'Title','Description':'Description','Hashtags':['#shorts']},self.stamp,credential_context=context)
            self.assertEqual(result['video_id'],'known')
            self.assertTrue(yt.verify_prepared_video('known',MINUTE_MYSTERIES.youtube_channel_id,self.stamp,credential_context=context)['verified'])
            complete.assert_called_once_with(self.service.videos.return_value.insert.return_value)
        auth.assert_called_once_with(credential_context=context)
        self.service.close.assert_not_called();context.close();self.service.close.assert_called_once()

    def test_unverified_context_never_inserts(self):
        context=yt.YouTubeCredentialContext()
        with patch.object(yt.os.path,'isfile',return_value=True),patch.object(yt,'get_authenticated_service') as auth:
            with self.assertRaises(yt.UploadNotStarted):yt.upload_prepared_video('saved.mp4',{'Title':'T','Description':'D','Hashtags':[]},self.stamp,credential_context=context)
            auth.assert_not_called()

    def test_cross_channel_verification_rejected(self):
        context=yt.YouTubeCredentialContext();self.preflight(context)
        with self.assertRaises(ValueError):yt.verify_prepared_video('known',self.other.youtube_channel_id,self.stamp,credential_context=context)
        self.service.videos.assert_not_called()

    def test_state_context_mismatch_rejected_before_loading(self):
        from engine.scheduling.preparation import Preparation
        from test_actions import Store
        from datetime import date
        with tempfile.TemporaryDirectory() as root:
            controller=Preparation(Store(),date(2026,10,3),root)
            with self.assertRaisesRegex(Exception,'credential/state channel mismatch'):
                controller.prepare(expected_channel=self.other.youtube_channel_id,credential_context=yt.YouTubeCredentialContext(self.other))

    def test_expired_context_token_refreshes_and_saves_only_its_path(self):
        context=yt.YouTubeCredentialContext(self.other)
        credentials=Mock(valid=False,expired=True,refresh_token='fixture')
        with patch.object(yt.os.path,'isfile',return_value=True),patch('builtins.open',mock_open()) as opened,patch.object(yt.pickle,'load',return_value=credentials),patch.object(yt.pickle,'dump') as saved,patch.object(yt,'Request'),patch.object(yt,'build'):
            yt.get_authenticated_service(credential_context=context)
        credentials.refresh.assert_called_once();saved.assert_called_once()
        self.assertEqual([c.args[:2] for c in opened.call_args_list],[(context.token_file,'rb'),(context.token_file,'wb')])

    def test_bound_fields_immutable(self):
        from dataclasses import FrozenInstanceError
        context=yt.YouTubeCredentialContext()
        with self.assertRaises(FrozenInstanceError):context.channel=self.other

    def test_nondefault_legacy_upload_has_no_default_auth_fallback(self):
        from engine.scheduling.daily_queue import DailyQueue
        from run_actions import Production
        from test_actions import Store
        from datetime import date
        with tempfile.TemporaryDirectory() as root, patch.object(yt, 'get_authenticated_service') as auth:
            queue = DailyQueue(root, channel=self.other)
            result = queue.upload_slot('10:00', date(2026,10,3))
            self.assertEqual(result['reason'], 'queue_or_state_error')
            controller = Production(Store(), date(2026,10,3), root, channel=self.other)
            with self.assertRaisesRegex(Exception, 'Legacy upload is restricted'):
                controller.upload('10:00')
            auth.assert_not_called()


class AuthorizationCliTests(unittest.TestCase):
    def test_default_and_space_science_authorization(self):
        from config.channels import resolve_channel_config
        for key,args in (("minute_mysteries",["--authorize"]),("space_science",["--authorize","--channel-key","space_science"])):
            channel=resolve_channel_config(key)
            service=Mock()
            service.channels.return_value.list.return_value.execute.return_value={"items":[{"id":channel.youtube_channel_id,"snippet":{"title":channel.channel_name}}]}
            with tempfile.TemporaryDirectory() as root,patch.object(yt,"YOUTUBE_TOKEN_FILE",str(Path(root)/"token.pickle")),patch.object(yt,"get_authenticated_service",return_value=service) as auth,patch("builtins.print") as output:
                self.assertEqual(yt.main(args),0)
                context=auth.call_args.kwargs["credential_context"]
                self.assertEqual(context.channel,channel)
                self.assertTrue(auth.call_args.kwargs["allow_interactive"])
                expected=Path(root)/"token.pickle" if key=="minute_mysteries" else Path(root)/".credentials"/key/"token.pickle"
                self.assertEqual(context.token_file,expected)
                self.assertTrue(expected.parent.is_dir())
                output.assert_any_call("Token file:",expected)
                output.assert_any_call("Authenticated channel ID:",channel.youtube_channel_id)
            service.channels.return_value.list.assert_called_once_with(part="id,snippet",mine=True)
            service.videos.assert_not_called()
            service.close.assert_called_once()

    def test_channel_mismatch_fails_closed(self):
        service=Mock()
        service.channels.return_value.list.return_value.execute.return_value={"items":[{"id":MINUTE_MYSTERIES.youtube_channel_id}]}
        with tempfile.TemporaryDirectory() as root,patch.object(yt,"YOUTUBE_TOKEN_FILE",str(Path(root)/"token.pickle")),patch.object(yt,"get_authenticated_service",return_value=service),patch("builtins.print"):
            self.assertEqual(yt.main(["--authorize","--channel-key","space_science"]),1)
        service.videos.assert_not_called();service.close.assert_called_once()

    def test_unknown_key_fails_before_auth_or_directory_creation(self):
        with patch.object(yt,"get_authenticated_service") as auth,patch.object(Path,"mkdir") as mkdir,patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:yt.main(["--authorize","--channel-key","unknown"])
        self.assertEqual(error.exception.code,2)
        auth.assert_not_called();mkdir.assert_not_called()


if __name__=='__main__':unittest.main()
