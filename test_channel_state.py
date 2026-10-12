"""Offline channel identity and legacy adoption; no production side effects."""
import copy,json,tempfile,unittest
from pathlib import Path
from datetime import date
from unittest.mock import Mock
from config.channels import MINUTE_MYSTERIES,ChannelConfig,channel_identity
from engine.scheduling.daily_queue import DailyQueue
from engine.scheduling.cleanup import record_generated_file,_validated_target
from run_actions import Production,ProductionError,validate_state
from test_actions import Store

DAY=date(2026,9,14)


class ChannelStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        data=MINUTE_MYSTERIES.model_dump();data.update(channel_key='example_b',youtube_channel_id='UC'+'a'*22,state_namespace='.actions-work-b',artifact_namespace='example-b',output_namespace='output/b')
        self.other=ChannelConfig.model_validate(data)

    def legacy_queue(self):
        return {'date':str(DAY),'timezone':'Asia/Kolkata','videos':[{'slot':slot,'generation_status':'pending','upload_status':'pending','youtube':None} for slot in MINUTE_MYSTERIES.publication_slots]}

    def test_default_legacy_queue_reads_without_copying(self):
        q=DailyQueue(self.root);path=q.path(DAY);path.write_text(json.dumps(self.legacy_queue()))
        self.assertEqual(q.read(DAY),self.legacy_queue());self.assertEqual(path.name,'daily_queue_2026-09-14.json')
        self.assertNotIn('channel_key',json.loads(path.read_text()))

    def test_nondefault_rejects_legacy_queue(self):
        q=DailyQueue(self.root,channel=self.other);q.path(DAY).write_text(json.dumps(self.legacy_queue()))
        with self.assertRaises(ValueError):q.read(DAY)

    def test_same_date_and_slot_are_independent_and_locks_stable(self):
        a=DailyQueue(self.root);b=DailyQueue(self.root,channel=self.other)
        a.update(DAY,lambda _:self.legacy_queue())
        data=self.legacy_queue();data.update(channel_identity(self.other));b.update(DAY,lambda _:data)
        a._set(DAY,'10:00',{'upload_status':'uncertain'})
        self.assertEqual(b.read(DAY)['videos'][0]['upload_status'],'pending')
        self.assertNotEqual(a.path(DAY),b.path(DAY));self.assertEqual(a.path(DAY),DailyQueue(self.root).path(DAY))
        self.assertNotEqual(a.data_dir/'daily_generation.lock',b.data_dir/'daily_generation.lock')

    def test_foreign_claim_and_item_rejected(self):
        q=DailyQueue(self.root);q.update(DAY,lambda _:self.legacy_queue())
        for fields in ({'channel_key':'example_b'},{'upload_owner':'example_b:2026-09-14:10:00'}):
            with self.assertRaises(ValueError):q._set(DAY,'10:00',fields)
        self.assertEqual(q.read(DAY)['videos'][0]['upload_status'],'pending')

    def pending_state(self):
        return {'schema':1,'date':str(DAY),'generation_run_id':100,'privacy_status':'private','queue':self.legacy_queue(),'idea_memory':{}}

    def media_state(self, channel, filename):
        from config.channels import slot_owner
        state=self.pending_state()
        if channel != MINUTE_MYSTERIES:
            state.update(channel_identity(channel))
            state['queue'].update(channel_identity(channel))
        item=state['queue']['videos'][0]
        item.update(generation_status='ready',metadata={'Title':'Title','Description':'Description','Hashtags':['#shorts']})
        item['generated_files']=[{'filename':filename,'channel_key':channel.channel_key,
            'artifact_name':channel.artifact_namespace+'-media-test','owner':slot_owner(channel,DAY,item['slot']),
            'kind':'rendered_video','artifact_id':1,'run_id':100,'sha256':'a'*64,'size_bytes':10,'mtime_ns':1}]
        return state

    def test_channel_filename_contract_accepts_legacy_and_space_science(self):
        from config.channels import resolve_channel_config
        for channel,name in ((MINUTE_MYSTERIES,'unsolved_mysteries_20260914050000.mp4'),
            (resolve_channel_config('space_science'),'astronomy,_space_mysteries,_strange_science_20261012113108.mp4')):
            with self.subTest(channel=channel.channel_key):
                validate_state(self.media_state(channel,name),DAY,channel=channel)

    def test_malformed_media_names_and_timestamps_rejected(self):
        from config.channels import resolve_channel_config
        for channel in (MINUTE_MYSTERIES,resolve_channel_config('space_science')):
            prefix=channel.topic_theme.replace(' ','_').lower()
            for name in ('arbitrary_20261012113108.mp4',prefix+'_bad.mp4',prefix+'_2026101211310.mp4',
                         prefix+'_20261012113108.mov',prefix+'_20261012113108.MP4',
                         '../'+prefix+'_20261012113108.mp4','folder/'+prefix+'_20261012113108.mp4',
                         'folder\\'+prefix+'_20261012113108.mp4',prefix+'_20260230113108.mp4',
                         prefix+'_20261012253108.mp4',prefix+'_20261012113108.mp4.extra'):
                with self.subTest(channel=channel.channel_key,name=name),self.assertRaises(ProductionError):
                    validate_state(self.media_state(channel,name),DAY,channel=channel)

    def test_cross_channel_media_filename_and_ownership_remain_rejected(self):
        from config.channels import resolve_channel_config
        channel=resolve_channel_config('space_science')
        with self.assertRaises(ProductionError):
            validate_state(self.media_state(channel,'unsolved_mysteries_20261012113108.mp4'),DAY,channel=channel)
        name='astronomy,_space_mysteries,_strange_science_20261012113108.mp4'
        for fields in ({'channel_key':'minute_mysteries'},{'artifact_name':'mystery-media-test'},
                       {'owner':'minute_mysteries:2026-09-14:10:00'}):
            state=self.media_state(channel,name)
            state['queue']['videos'][0]['generated_files'][0].update(fields)
            with self.subTest(fields=fields),self.assertRaises(ProductionError):
                validate_state(state,DAY,channel=channel)

    def test_legacy_state_only_default_and_partial_identity_rejected(self):
        state=self.pending_state();validate_state(state,DAY)
        with self.assertRaises(ProductionError):validate_state(state,DAY,channel=self.other)
        state['channel_key']='minute_mysteries'
        with self.assertRaises(ProductionError):validate_state(state,DAY)

    def test_legacy_checkpoint_resumes_and_new_checkpoint_records_identity(self):
        store=Store();p=self.root/'state.json';p.write_text(json.dumps(self.pending_state()));store.upload('mystery-state-'+str(DAY)+'-legacy',p,90)
        controller=Production(store,DAY,self.root);controller.load();controller.queue.checkpoint=controller.checkpoint
        controller.queue._set(DAY,'10:00',{'upload_status':'uncertain'})
        saved=json.loads(store.latest(DAY)['bytes'])
        self.assertEqual(saved['channel_key'],'minute_mysteries');self.assertEqual(saved['queue']['youtube_channel_id'],MINUTE_MYSTERIES.youtube_channel_id)
        self.assertEqual(saved['queue']['videos'][0]['upload_status'],'uncertain')

    def test_artifact_lookup_separates_channels(self):
        store=Store();p=self.root/'state.json';p.write_text('{}');store.upload('mystery-state-'+str(DAY)+'-old',p,90)
        self.assertIsNotNone(store.latest(DAY));store.channel=self.other;self.assertIsNone(store.latest(DAY))
        store.upload('example-b-state-'+str(DAY)+'-new',p,90);self.assertTrue(store.latest(DAY)['name'].startswith('example-b-state-'))

    def test_nondefault_cannot_select_legacy_artifact_namespace(self):
        data=self.other.model_dump();data['artifact_namespace']='mystery'
        store=Store();store.channel=ChannelConfig.model_validate(data)
        with self.assertRaises(ValueError):store.latest(DAY)
        with self.assertRaises(ValueError):Production(store,DAY,self.root,channel=store.channel)

    def test_cleanup_rejects_foreign_owner_even_matching_bytes(self):
        path=self.root/'unsolved_mysteries_20260914050000.mp4';path.write_bytes(b'render')
        record=record_generated_file(path,DAY,'10:00',channel=self.other)
        item={'slot':'10:00','video_path':str(path),'generated_files':[record],'channel_key':'example_b'}
        with self.assertRaises(ValueError):_validated_target(item,DAY,self.root,{str(path):1})
        self.assertTrue(path.exists())

    def test_legacy_render_owner_retained(self):
        path=self.root/'unsolved_mysteries_20260914050000.mp4';path.write_bytes(b'render')
        record=record_generated_file(path,DAY,'10:00');record.pop('channel_key');record['owner']=str(DAY)+':10:00'
        item={'slot':'10:00','video_path':str(path),'generated_files':[record]}
        self.assertEqual(_validated_target(item,DAY,self.root,{str(path):1}),path)
        with self.assertRaises(ValueError):_validated_target(item,DAY,self.root,{str(path):1},channel=self.other)

    def test_ready_legacy_batch_preserves_renders_and_ids_without_generation(self):
        store=Store();controller=Production(store,DAY,self.root)
        counter=[0]
        def generate():
            counter[0]+=1
            path=controller.output/f"unsolved_mysteries_2026091405{counter[0]:02d}00.mp4"
            path.write_bytes(b'render'+bytes([counter[0]]))
            return {'video_path':str(path),'metadata':{'Title':'Title','Description':'Description','Hashtags':['#shorts']},'idea':'Idea','script':'Script'}
        controller.generate(generate)
        state=copy.deepcopy(controller.state)
        for record in (state,state['queue']):
            for field in ('channel_key','youtube_channel_id','state_namespace'):record.pop(field,None)
        for item in state['queue']['videos']:
            item.pop('channel_key',None)
            item['generated_files'][0].pop('channel_key',None)
            item['generated_files'][0]['owner']=str(DAY)+':'+item['slot']
            item.update(upload_status='uploaded',youtube={'success':True,'status':'uploaded','video_id':'known-'+item['slot']})
        p=self.root/'state.json';p.write_text(json.dumps(state));store.run_id+=1
        store.upload('mystery-state-'+str(DAY)+'-legacy',p,90)
        restored=Production(store,DAY,self.root);generator=Mock(side_effect=AssertionError('No regeneration'))
        restored.generate(generator);generator.assert_not_called()
        self.assertEqual([v['youtube']['video_id'] for v in restored.queue.read(DAY)['videos']],['known-'+slot for slot in MINUTE_MYSTERIES.publication_slots])
        self.assertEqual(counter[0],3)

    def test_confirmed_id_and_uncertain_claim_not_reuploaded(self):
        q=DailyQueue(self.root);q.update(DAY,lambda _:self.legacy_queue())
        uploader=Mock()
        q._set(DAY,'10:00',{'upload_status':'uploaded','youtube':{'video_id':'confirmed','success':True}})
        q._set(DAY,'14:00',{'upload_status':'uncertain'})
        q.upload_slot('10:00',DAY,uploader);q.upload_slot('14:00',DAY,uploader)
        uploader.assert_not_called();self.assertEqual(q.read(DAY)['videos'][0]['youtube']['video_id'],'confirmed')


if __name__=='__main__':unittest.main()
