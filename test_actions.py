"""Offline production artifact tests: fake storage and media; no real services."""
import builtins
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from engine.scheduling.cleanup import cleanup_recent
from engine.scheduling.daily_queue import CheckpointError, DailyQueue
from run_actions import GitHubArtifacts, Production, ProductionError, cleanup_production, validate_state

DAY = date(2026, 9, 14)
TZ = ZoneInfo('Asia/Kolkata')


class Store:
    def __init__(self):
        self.run_id = 100
        self.saved = []
        self.deleted = []

    def artifacts(self):
        return [copy.deepcopy(a) for a in self.saved if a['id'] not in self.deleted]

    latest = GitHubArtifacts.latest
    newest_checkpoint = GitHubArtifacts.newest_checkpoint

    def trusted(self, artifact):
        return artifact

    def by_id(self, artifact_id, run_id):
        item = next((a for a in self.artifacts() if a['id'] == artifact_id), None)
        if item is None or item['workflow_run']['id'] != run_id:
            raise ProductionError('Missing artifact')
        return item

    def upload(self, name, path, retention):
        result = {'id': next(self.artifact_ids) if hasattr(self, 'artifact_ids') else len(self.saved) + 1,
                  'created_at': (datetime(2026, 9, 14, tzinfo=TZ) + timedelta(seconds=len(self.saved))).isoformat(),
                  'name': name, 'workflow_run': {'id': self.run_id},
                  'filename': Path(path).name, 'bytes': Path(path).read_bytes(), 'retention': retention}
        self.saved.append(result)
        return copy.deepcopy(result)

    def download(self, artifact, filename, destination):
        if artifact['filename'] != filename or artifact['id'] in self.deleted:
            raise ProductionError('Missing/wrong artifact')
        Path(destination).write_bytes(artifact['bytes'])

    def delete(self, artifact_id):
        if artifact_id not in self.deleted:
            self.deleted.append(artifact_id)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store()
        self.controller = Production(self.store, DAY, self.root)
        self.count = 0
        self.metadata = {'Title': 'Exact title', 'Description': 'Exact description', 'Hashtags': ['#Mystery']}
        self.success = {'success': True, 'status': 'uploaded', 'video_id': 'id123',
                        'video_url': 'https://www.youtube.com/watch?v=id123', 'privacy_status': 'private'}

    def generator(self):
        self.count += 1
        path = self.controller.output / f'unsolved_mysteries_2026091405{self.count:02d}00.mp4'
        path.write_bytes(f'fixture media {self.count}'.encode())
        return {'video_path': str(path), 'metadata': copy.deepcopy(self.metadata), 'idea': 'Saved idea', 'script': 'Saved script'}

    def generated(self):
        result = self.controller.generate(self.generator)
        self.assertEqual(result['status'], 'ready')
        return result

    def fresh(self):
        root = self.root / f'runner-{len(list(self.root.glob("runner-*"))) + 1}'
        root.mkdir()
        self.store.run_id += 1
        return Production(self.store, DAY, root)

    def upload(self, controller=None, slot='10:00', uploader=None, notifier=None):
        controller = controller or self.fresh()
        return controller.upload(slot, 100, uploader or Mock(return_value=self.success),
                                 notifier or Mock(return_value={'success': True, 'status': 'sent'}))

    def test_nonmonotonic_artifact_ids_do_not_poison_next_slot(self):
        # Actual ordering from run 36843732075: the ready receipt has a lower
        # ID than the earlier generating receipt. The old max(id) fails at slot 14.
        self.store.artifact_ids = iter([11152483137, 11152862308, 11152792280,
                                        11152796891, *range(11153000000, 11153000020)])
        self.generated()
        self.assertEqual(self.count, 3)
        self.assertFalse(self.controller.poisoned)
        first = self.controller.state['queue']['videos'][0]
        self.assertEqual(first['slot'], '10:00')
        self.assertEqual(first['generated_files'][0]['artifact_id'], 11152792280)
        self.assertEqual(self.controller.state['generation_run_id'], 100)
        self.fresh().generate(Mock(side_effect=AssertionError('Must reuse rendered MP4s')))
        self.assertEqual(sum(a['name'].startswith('mystery-media-') for a in self.store.saved), 3)

    def test_lower_id_ready_receipt_restores_existing_slot_without_generation(self):
        self.generated()
        latest = self.store.latest(DAY)
        latest['id'] = 11152796891
        latest['created_at'] = '2026-10-01T09:41:45Z'
        old = copy.deepcopy(latest)
        old.update(id=11152862308, created_at='2026-10-01T09:36:57Z')
        old_state = json.loads(old['bytes'])
        old_state['queue']['videos'][0]['generation_status'] = 'generating'
        old['bytes'] = json.dumps(old_state).encode()
        self.store.saved.extend([old, latest])
        other = self.fresh()
        with patch.object(DailyQueue, 'generate_batch', side_effect=AssertionError('No regeneration')):
            other.load(100)
            other.restore_media(['10:00'])
        self.assertEqual(other.latest_id, 11152796891)
        item = other.queue.read(DAY)['videos'][0]
        self.assertEqual(item['slot'], '10:00')
        self.assertEqual(item['generation_status'], 'ready')
        self.assertEqual(item['metadata'], self.metadata)
        self.assertEqual(Path(item['video_path']).read_bytes(), b'fixture media 1')
        self.assertEqual(other.state['generation_run_id'], 100)

    def test_three_slots_exact_metadata_and_no_second_batch(self):
        self.generated()
        self.assertEqual(self.count, 3)
        validate_state(self.controller.state, DAY, 100)
        self.assertEqual([v['slot'] for v in self.controller.state['queue']['videos']], ['10:00', '14:00', '19:00'])
        for item in self.controller.state['queue']['videos']:
            self.assertEqual(item['metadata'], self.metadata)
        self.fresh().generate(Mock(side_effect=AssertionError('Must not regenerate')))
        self.assertEqual(sum(a['name'].startswith('mystery-media-') for a in self.store.saved), 3)

    def test_restoration_rebases_paths_preserves_hash_and_mtime(self):
        self.generated()
        other = self.fresh()
        other.load(100)
        other.restore_media(['14:00'])
        item = other.queue.read(DAY)['videos'][1]
        self.assertTrue(Path(item['video_path']).is_relative_to(other.root))
        self.assertEqual(Path(item['video_path']).stat().st_mtime_ns, item['generated_files'][0]['mtime_ns'])
        self.assertEqual(item['metadata'], self.metadata)

    def test_only_selected_slot_uploaded_and_claim_is_durable_first(self):
        self.generated()
        def upload(path, metadata):
            state = json.loads(self.store.latest(DAY)['bytes'])
            self.assertEqual(state['queue']['videos'][1]['upload_status'], 'uploading')
            self.assertEqual(state['queue']['videos'][0]['upload_status'], 'pending')
            self.assertEqual(metadata, self.metadata)
            self.assertEqual(Path(path).read_bytes(), b'fixture media 2')
            return self.success
        uploaded = Mock(side_effect=upload)
        self.upload(slot='14:00', uploader=uploaded)
        uploaded.assert_called_once()

    def test_uploaded_slot_cannot_upload_again(self):
        self.generated()
        self.upload()
        uploader = Mock()
        with self.assertRaisesRegex(ProductionError, 'already attempted'):
            self.upload(uploader=uploader)
        uploader.assert_not_called()

    def test_uploading_and_uncertain_slots_block(self):
        self.generated()
        for status in ('uploading', 'uncertain'):
            controller = self.fresh()
            controller.load()
            controller.queue.checkpoint = controller.checkpoint
            controller.queue._set(DAY, '10:00', {'upload_status': status})
            uploader = Mock()
            with self.assertRaisesRegex(ProductionError, 'already attempted'):
                self.upload(uploader=uploader)
            uploader.assert_not_called()

    def test_telegram_failure_does_not_upload_twice(self):
        self.generated()
        uploaded = Mock(return_value=self.success)
        with self.assertRaisesRegex(ProductionError, 'YouTube uploaded; Telegram failed'):
            self.upload(uploader=uploaded, notifier=Mock(return_value={'success': False, 'status': 'failed'}))
        with self.assertRaises(ProductionError):
            self.upload(uploader=uploaded)
        uploaded.assert_called_once()
        state = json.loads(self.store.latest(DAY)['bytes'])
        self.assertTrue(state['queue']['videos'][0]['youtube']['success'])
        self.assertFalse(state['queue']['videos'][0]['telegram']['success'])

    def test_wrong_date_and_generation_run_rejected(self):
        self.generated()
        with self.assertRaisesRegex(ProductionError, 'Wrong generation date'):
            validate_state(self.controller.state, DAY + timedelta(days=1))
        with self.assertRaisesRegex(ProductionError, 'Wrong generation run'):
            self.fresh().load(999)

    def test_missing_state_or_media_rejected(self):
        with self.assertRaisesRegex(ProductionError, 'Missing generation'):
            self.fresh().load()
        self.store.run_id = 100
        self.generated()
        media = self.controller.state['queue']['videos'][0]['generated_files'][0]
        self.store.delete(media['artifact_id'])
        uploader = Mock()
        with self.assertRaisesRegex(ProductionError, 'Missing artifact'):
            self.upload(uploader=uploader)
        uploader.assert_not_called()

    def test_partial_generation_is_failure_with_two_attempt_bound(self):
        calls = 0
        def generate():
            nonlocal calls
            calls += 1
            if calls in (2, 4):
                raise RuntimeError('fixture generation failure')
            return self.generator()
        with self.assertRaisesRegex(ProductionError, 'slot-14'):
            self.controller.generate(generate)
        self.assertEqual(calls, 4)
        self.assertEqual(len(self.controller.media), 2)
        other = self.fresh()
        never = Mock(side_effect=AssertionError('Retry budget exhausted'))
        with self.assertRaises(ProductionError):
            other.generate(never)
        never.assert_not_called()

    def test_resume_generates_only_unfinished_slot(self):
        def generate():
            if self.count == 1:
                self.count += 1
                raise RuntimeError('temporary fixture error')
            return self.generator()
        self.controller.generate(generate)
        self.assertEqual(len(self.controller.media), 3)
        self.assertEqual(sum(a['name'].startswith('mystery-media-') for a in self.store.saved), 3)

    def test_stale_checkpoint_refused(self):
        self.generated()
        first, second = self.fresh(), self.fresh()
        first.load(); second.load()
        first.queue.checkpoint = first.checkpoint
        second.queue.checkpoint = second.checkpoint
        first.queue._set(DAY, '10:00', {'upload_status': 'uploading'})
        with self.assertRaises(CheckpointError):
            second.queue._set(DAY, '14:00', {'upload_status': 'uploading'})
        self.assertTrue(second.poisoned)

    def test_claim_checkpoint_failure_prevents_youtube(self):
        self.generated()
        uploader = Mock(return_value=self.success)
        with patch.object(self.store, 'upload', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(ProductionError):
                self.upload(uploader=uploader)
        uploader.assert_not_called()

    def test_outcome_checkpoint_failure_leaves_remote_claim(self):
        self.generated()
        original = self.store.upload
        def save(name, path, retention):
            if Path(path).name == 'state.json':
                state = json.loads(Path(path).read_text())
                if state['queue']['videos'][0]['upload_status'] == 'uploaded':
                    raise RuntimeError('outcome persistence failed')
            return original(name, path, retention)
        notify = Mock()
        with patch.object(self.store, 'upload', side_effect=save):
            with self.assertRaises(ProductionError):
                self.upload(notifier=notify)
        notify.assert_not_called()
        self.assertEqual(json.loads(self.store.latest(DAY)['bytes'])['queue']['videos'][0]['upload_status'], 'uploading')
        with self.assertRaises(ProductionError):
            self.upload()

    def test_lost_youtube_response_uncertain_no_success_notification(self):
        self.generated()
        notify = Mock()
        with self.assertRaises(ProductionError):
            self.upload(uploader=Mock(return_value={'success': False, 'status': 'failed', 'error_type': 'SSLEOFError'}), notifier=notify)
        state = json.loads(self.store.latest(DAY)['bytes'])
        self.assertEqual(state['queue']['videos'][0]['upload_status'], 'uncertain')
        notify.assert_not_called()

    def test_successful_upload_timestamp_and_24_hour_cleanup(self):
        self.generated()
        stamp = datetime(2026, 9, 14, 19, tzinfo=TZ)
        with patch('engine.scheduling.daily_queue.india_now', return_value=stamp):
            self.upload()
        item = json.loads(self.store.latest(DAY)['bytes'])['queue']['videos'][0]
        self.assertEqual(item['successful_upload_at'], stamp.isoformat())
        root = self.root / 'cleanup-runner'; root.mkdir()
        cleanup_production(self.store, root, stamp + timedelta(hours=23))
        self.assertEqual(self.store.deleted, [])
        cleanup_production(self.store, root, stamp + timedelta(hours=24))
        self.assertEqual(self.store.deleted, [item['generated_files'][0]['artifact_id']])
        state = json.loads(self.store.latest(DAY)['bytes'])
        self.assertEqual(state['queue']['videos'][0]['artifact_cleanup'], 'deleted')
        self.assertEqual(state['queue']['videos'][0]['youtube']['video_id'], 'id123')
        cleanup_production(self.store, root, stamp + timedelta(hours=25))
        self.assertEqual(len(self.store.deleted), 1)

    def test_cleanup_checkpoint_failure_retains_remote_media(self):
        self.generated()
        stamp = datetime(2026, 9, 14, 19, tzinfo=TZ)
        with patch('engine.scheduling.daily_queue.india_now', return_value=stamp):
            self.upload()
        root = self.root / 'cleanup-checkpoint'; root.mkdir()
        with patch.object(self.store, 'upload', side_effect=RuntimeError('checkpoint unavailable')):
            with self.assertRaisesRegex(ProductionError, 'Cleanup checkpoint failed'):
                cleanup_production(self.store, root, stamp + timedelta(hours=24))
        self.assertEqual(self.store.deleted, [])

    def test_legacy_missing_upload_time_is_not_cleaned(self):
        self.generated()
        self.upload()
        controller = self.fresh()
        controller.load()
        controller.queue.checkpoint = controller.checkpoint
        controller.queue._set(DAY, '10:00', {'successful_upload_at': None})
        root = self.root / 'cleanup-legacy'; root.mkdir()
        cleanup_production(self.store, root, datetime(2026, 9, 16, 19, tzinfo=TZ))
        self.assertEqual(self.store.deleted, [])

    def test_upload_does_not_import_generation_or_regenerate(self):
        self.generated()
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith(('openai', 'moviepy', 'engine.pipeline', 'engine.generators', 'engine.video', 'engine.media')):
                raise AssertionError('Upload imported generation dependency: ' + name)
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guarded), patch.object(DailyQueue, 'generate_batch', side_effect=AssertionError('Regeneration')):
            self.upload()

    def test_crash_after_artifact_deletion_can_finish_cleanup(self):
        self.generated()
        stamp = datetime(2026, 9, 14, 19, tzinfo=TZ)
        with patch('engine.scheduling.daily_queue.india_now', return_value=stamp):
            self.upload()
        root = self.root / 'cleanup-delete-crash'; root.mkdir()
        original = self.store.upload
        def save(name, path, retention):
            if Path(path).name == 'state.json':
                state = json.loads(Path(path).read_text())
                if state['queue']['videos'][0].get('artifact_cleanup') == 'deleted':
                    raise RuntimeError('receipt failed after delete')
            return original(name, path, retention)
        with patch.object(self.store, 'upload', side_effect=save):
            with self.assertRaises(CheckpointError):
                cleanup_production(self.store, root, stamp + timedelta(hours=24))
        self.assertEqual(len(self.store.deleted), 1)
        cleanup_production(self.store, root, stamp + timedelta(hours=25))
        self.assertEqual(len(self.store.deleted), 1)
        state = json.loads(self.store.latest(DAY)['bytes'])
        self.assertEqual(state['queue']['videos'][0]['artifact_cleanup'], 'deleted')

    def test_tampered_media_rejected(self):
        self.generated()
        media = next(a for a in self.store.saved if a['name'].startswith('mystery-media-'))
        media['bytes'] = b'wrong media'
        with self.assertRaisesRegex(ProductionError, 'fingerprint mismatch'):
            self.upload()

    def test_workflow_schedules_public_privacy_and_shared_concurrency(self):
        text = (Path(__file__).parent / '.github/workflows/production.yml').read_text(encoding='utf-8-sig')
        active = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
        self.assertIn('workflow_dispatch:', active)
        # Actions trigger times differ from the local scheduler's slot labels.
        expected = [
            ('5 5 * * *', 'generate', ''),
            ('5 10 * * *', 'upload', '10:00'),
            ('5 14 * * *', 'upload', '14:00'),
            ('5 19 * * *', 'upload', '19:00'),
            ('35 * * * *', 'cleanup', ''),
        ]
        schedule = active.split('  schedule:\n', 1)[1].split('\nconcurrency:', 1)[0]
        entries = [line.strip() for line in schedule.splitlines() if line.strip()]
        self.assertEqual(entries, [line for cron, _, _ in expected for line in
                                  (f"- cron: '{cron}'", 'timezone: Asia/Kolkata')])
        routing = active.split('const schedules = {', 1)[1].split('};', 1)[0]
        self.assertEqual([line.strip() for line in routing.splitlines() if line.strip()],
                         [f"'{cron}': ['{operation}', '{slot}']," for cron, operation, slot in expected])
        self.assertIn('context.eventName === \'schedule\'', active)
        self.assertIn('schedules[process.env.EVENT_SCHEDULE]', active)
        self.assertIn('group: mystery-production-state', active)
        self.assertIn('cancel-in-progress: false', active)
        self.assertNotIn('run_scheduler.py', active)
        self.assertIn('YOUTUBE_PRIVACY_STATUS: public', active)
        self.assertNotIn('pull_request:', active)


class ArtifactTrustTests(unittest.TestCase):
    def setUp(self):
        self.store = GitHubArtifacts.__new__(GitHubArtifacts)
        self.store.repo, self.store.branch = 'owner/repo', 'main'
        self.store.verified_runs = set()
        self.artifact = {'id': 7, 'name': f'mystery-state-{DAY}-1-x',
                         'workflow_run': {'id': 1}, 'expired': False, 'created_at': '2026-10-01T09:36:57Z'}
        self.run = {'path': '.github/workflows/production.yml', 'head_branch': 'main',
                    'event': 'workflow_dispatch', 'head_repository': {'full_name': 'owner/repo'}}
        self.store.api_request = Mock(return_value=Mock(json=lambda: self.run))

    def test_actual_run_creation_times_override_larger_older_id(self):
        old = {**self.artifact, 'id': 11152862308}
        newest = {**self.artifact, 'id': 11152796891, 'created_at': '2026-10-01T09:41:45Z'}
        self.store.artifacts = Mock(return_value=[old, newest])
        self.assertEqual(self.store.latest(DAY)['id'], newest['id'])
        self.assertEqual(self.store.newest_checkpoint([old, newest])['id'], newest['id'])

    def test_equal_timestamps_use_predecessors_not_id_order(self):
        old = {**self.artifact, 'id': 900}
        newest = {**self.artifact, 'id': 800}
        self.store.artifacts = Mock(return_value=[old, newest])
        def download(artifact, name, path):
            Path(path).write_text(json.dumps({'previous_state_id': 900 if artifact['id'] == 800 else 700}))
        self.store.download = Mock(side_effect=download)
        self.assertEqual(self.store.latest(DAY)['id'], 800)

    def test_equal_timestamp_competing_heads_fail_closed(self):
        self.store.artifacts = Mock(return_value=[self.artifact, {**self.artifact, 'id': 6}])
        def download(artifact, name, path):
            Path(path).write_text(json.dumps({'previous_state_id': 5}))
        self.store.download = Mock(side_effect=download)
        with self.assertRaisesRegex(ProductionError, 'Ambiguous checkpoint order'):
            self.store.latest(DAY)

    def test_wrong_workflow_branch_event_or_repository_rejected(self):
        for key, value in [('path', '.github/workflows/test-video-generation.yml'),
                           ('head_branch', 'untrusted'), ('event', 'pull_request'),
                           ('head_repository', {'full_name': 'fork/repo'})]:
            with self.subTest(key=key):
                original = self.run[key]
                self.run[key] = value
                with self.assertRaisesRegex(ProductionError, 'trusted production workflow'):
                    self.store.trusted(self.artifact)
                self.run[key] = original

    def test_expired_newest_state_does_not_fall_back_to_old_ready_state(self):
        newest = {**self.artifact, 'id': 6, 'expired': True, 'created_at': '2026-10-01T09:41:45Z'}
        self.store.artifacts = Mock(return_value=[self.artifact, newest])
        with self.assertRaisesRegex(ProductionError, 'expired'):
            self.store.latest(DAY)


if __name__ == '__main__':
    unittest.main()
