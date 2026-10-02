"""Finite GitHub Actions operations around DailyQueue; no generation implementation.

Only this production workflow may write these artifacts. All operations share one
GitHub concurrency group. State is append-only; a missing/ambiguous claim fails closed.
"""
import argparse
import base64
import copy
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile

import requests

from config.settings import BASE_DIR, UPLOAD_TIMES, YOUTUBE_PRIVACY_STATUS
from engine.scheduling.daily_queue import CheckpointError, DailyQueue, india_now, validate_metadata
from engine.scheduling.cleanup import _eligible, _fingerprint, _validated_target
from collections import Counter

WORKFLOW = '.github/workflows/production.yml'
STATE_PREFIX = 'mystery-state-'
MEDIA_DAYS = 7
STATE_DAYS = 90
SLOT_NAMES = {'10:00': 'slot-10', '14:00': 'slot-14', '19:00': 'slot-19'}


class ProductionError(RuntimeError):
    """Safe, credential-free operator message."""


def require(condition, message):
    if not condition:
        raise ProductionError(message)


def generation_policy(day, now, event, explicit_day=False):
    """Admit scheduled generation before 10:00; explicit dispatch is recovery."""
    require(day == now.date(), 'Only today in Asia/Kolkata is allowed; historical/future generation is blocked')
    require(event in ('schedule', 'workflow_dispatch'), 'Unsupported generation event')
    if event == 'schedule':
        require(now.hour < 10,
                'Scheduled 10:00 IST generation deadline already reached; dispatch generate with an explicit current day')
        return 'scheduled'
    if now.hour >= 10:
        require(explicit_day, 'After 10:00 recovery requires an explicit current day')
    return 'manual_recovery' if explicit_day else 'manual'


def validate_state(state, day, generation_run=None):
    require(state.get('schema') == 1 and state.get('date') == day.isoformat(),
            'Wrong generation date or unsupported state schema')
    require(isinstance(state.get('generation_run_id'), int), 'Missing generation run identity')
    if generation_run is not None:
        require(state['generation_run_id'] == generation_run, 'Wrong generation run selected')
    require(state.get('privacy_status') in ('private', 'unlisted', 'public'), 'Invalid saved privacy')
    queue = state.get('queue', {})
    require(queue.get('date') == day.isoformat() and queue.get('timezone') == 'Asia/Kolkata',
            'Queue date/timezone mismatch')
    items = queue.get('videos', [])
    require([v.get('slot') for v in items] == list(UPLOAD_TIMES), 'Missing or incorrect daily slots')
    paths = []
    for item in items:
        require(item.get('upload_status') in ('pending', 'uploading', 'uploaded', 'failed', 'uncertain'),
                'Invalid upload state')
        if item.get('generation_status') == 'ready':
            validate_metadata(item.get('metadata'))
            records = item.get('generated_files', [])
            require(len(records) == 1, 'Missing media fingerprint')
            record = records[0]
            name = record.get('filename', '')
            require(re.fullmatch(r'unsolved_mysteries_[0-9]{14}\.mp4', name), 'Invalid media filename')
            require(record.get('owner') == f'{day}:{item["slot"]}' and record.get('kind') == 'rendered_video',
                    'Media owner mismatch')
            require(isinstance(record.get('artifact_id'), int) and isinstance(record.get('run_id'), int),
                    'Missing explicit media artifact/run identity')
            require(re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', ''))) and
                    type(record.get('size_bytes')) is int and record['size_bytes'] > 0 and
                    type(record.get('mtime_ns')) is int, 'Invalid fingerprint')
            paths.append(name)
    require(len(paths) == len(set(paths)), 'Shared media reference rejected')
    if queue.get('preparation') is not None:
        from engine.scheduling.preparation import validate_preparation
        validate_preparation(queue)
        require(state['privacy_status'] == 'private', 'Preparation state must be private')
    memory = state.get('idea_memory')
    require(isinstance(memory, dict) and all(isinstance(v, list) and all(isinstance(x, str) for x in v)
                                          for v in memory.values()), 'Invalid idea memory')


class GitHubArtifacts:
    """REST reads/deletes; official Actions artifact client for uploads in an action context."""
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.run_id = int(os.environ['GITHUB_RUN_ID'])
        self.branch = os.environ['PRODUCTION_BRANCH']
        self.api = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                                     'Accept': 'application/vnd.github+json',
                                     'X-GitHub-Api-Version': '2022-11-28'})
        self.verified_runs = set()

    def api_request(self, method, endpoint, **kwargs):
        response = self.session.request(method, f'{self.api}/repos/{self.repo}/{endpoint}',
                                        timeout=(20, 120), **kwargs)
        if response.status_code not in (200, 204, 302):
            # Never print a response body, signed download URL or Authorization header.
            raise ProductionError(f'GitHub artifact API failed (HTTP {response.status_code})')
        return response

    def artifacts(self):
        result = []
        page = 1
        while True:
            batch = self.api_request('GET', f'actions/artifacts?per_page=100&page={page}').json()['artifacts']
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def trusted(self, artifact):
        run_id = artifact['workflow_run']['id']
        if run_id not in self.verified_runs:
            run = self.api_request('GET', f'actions/runs/{run_id}').json()
            require(run.get('path', '').split('@')[0] == WORKFLOW and
                    run.get('head_branch') == self.branch and
                    run.get('event') in ('workflow_dispatch', 'schedule') and
                    run.get('head_repository', {}).get('full_name') == self.repo,
                    'Artifact is not from the trusted production workflow/default branch')
            self.verified_runs.add(run_id)
        require(not artifact.get('expired'), 'Required artifact has expired; do not regenerate/reupload blindly')
        return artifact

    def newest_checkpoint(self, artifacts):
        if not artifacts:
            return None
        # GitHub artifact IDs are identities, NOT chronological sequence numbers.
        def created(artifact):
            stamp = datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
            require(stamp.utcoffset() is not None, 'Checkpoint creation time must include a timezone')
            return stamp
        newest_time = max(map(created, artifacts))
        candidates = [a for a in artifacts if created(a) == newest_time]
        if len(candidates) == 1:
            return self.trusted(candidates[0])
        # REST timestamps have second precision. Never break a tie by artifact ID:
        # use the existing predecessor chain, or fail closed on competing heads.
        parents = {}
        with tempfile.TemporaryDirectory() as directory:
            for artifact in candidates:
                path = Path(directory) / 'state.json'
                self.download(self.trusted(artifact), 'state.json', path)
                parents[artifact['id']] = json.loads(path.read_text(encoding='utf-8'))['previous_state_id']
        heads = set(parents) - set(parents.values())
        require(len(heads) == 1, 'Ambiguous checkpoint order; manual state review required')
        head = next(iter(heads))
        visited, cursor = set(), head
        while cursor in parents and cursor not in visited:
            visited.add(cursor)
            cursor = parents[cursor]
        require(len(visited) == len(parents) and cursor not in visited,
                'Ambiguous checkpoint chain; manual state review required')
        return self.trusted(next(a for a in candidates if a['id'] == head))

    def latest(self, day):
        matches = [a for a in self.artifacts() if a['name'].startswith(f'{STATE_PREFIX}{day}-')]
        return self.newest_checkpoint(matches)

    def by_id(self, artifact_id, run_id):
        artifact = self.api_request('GET', f'actions/artifacts/{artifact_id}').json()
        require(artifact['workflow_run']['id'] == run_id, 'Media artifact belongs to the wrong run')
        return self.trusted(artifact)

    def download(self, artifact, filename, destination):
        self.trusted(artifact)
        response = self.api_request('GET', f'actions/artifacts/{artifact["id"]}/zip', allow_redirects=False)
        require(response.status_code == 302, 'Missing artifact download redirect')
        # Do not forward the GitHub token to the signed blob-storage URL.
        with requests.get(response.headers['Location'], stream=True, timeout=(20, 180)) as download:
            require(download.status_code == 200, 'Artifact download failed')
            with tempfile.TemporaryFile() as archive:
                for chunk in download.iter_content(1024 * 1024):
                    archive.write(chunk)
                archive.seek(0)
                with zipfile.ZipFile(archive) as bundle:
                    require(bundle.namelist() == [filename], 'Unexpected files in artifact')
                    with bundle.open(filename) as source, Path(destination).open('wb') as target:
                        shutil.copyfileobj(source, target)

    def upload(self, name, path, retention):
        # Runtime artifact credentials are available inside actions/github-script.
        # The Node helper inherits them; they are never serialized into artifacts.
        script = r"""
const fs = require('fs');
const {DefaultArtifactClient} = require(process.env.ARTIFACT_MODULE);
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const result = await new DefaultArtifactClient().uploadArtifact(
    p.name, [p.path], require('path').dirname(p.path),
    {retentionDays: p.retention, compressionLevel: 0});
  console.log('ARTIFACT_RESULT=' + JSON.stringify({id: result.id}));
})().catch(() => { console.error('Artifact checkpoint failed'); process.exit(1); });
"""
        result = subprocess.run(['node', '-e', script], input=json.dumps({
            'name': name, 'path': str(Path(path).resolve()), 'retention': retention}),
            text=True, capture_output=True, timeout=600)
        require(result.returncode == 0, 'Artifact upload failed; stop and inspect the latest saved claim')
        lines = [line for line in result.stdout.splitlines() if line.startswith('ARTIFACT_RESULT=')]
        require(len(lines) == 1, 'Artifact upload result missing')
        artifact_id = json.loads(lines[0].split('=', 1)[1])['id']
        artifact = self.by_id(artifact_id, self.run_id)
        require(artifact['name'] == name, 'Artifact checkpoint identity mismatch')
        return artifact

    def delete(self, artifact_id):
        response = self.session.delete(f'{self.api}/repos/{self.repo}/actions/artifacts/{artifact_id}', timeout=(20, 120))
        require(response.status_code in (204, 404), 'Artifact deletion failed')


class Production:
    def __init__(self, store, day, root=BASE_DIR):
        self.store, self.day = store, day
        self.root = Path(root).resolve()
        self.scratch = self.root / '.actions-work'
        self.scratch.mkdir(exist_ok=True)
        self.output = self.root / 'output' / 'videos'
        self.output.mkdir(parents=True, exist_ok=True)
        self.memory = self.root / 'data' / 'idea_memory.json'
        self.queue = DailyQueue(self.scratch / 'queues')
        self.latest_id = None
        self.state = None
        self.media = {}
        self.poisoned = False

    def load(self, generation_run=None, required=True):
        artifact = self.store.latest(self.day)
        if artifact is None:
            require(not required, 'Missing generation/state artifact for this date')
            return False
        path = self.scratch / 'restored-state.json'
        self.store.download(artifact, 'state.json', path)
        state = json.loads(path.read_text(encoding='utf-8'))
        validate_state(state, self.day, generation_run)
        self.state, self.latest_id = state, artifact['id']
        queue = copy.deepcopy(state['queue'])
        for item in queue['videos']:
            if item['generation_status'] == 'ready':
                record = item['generated_files'][0]
                self.media[item['slot']] = copy.deepcopy(record)
                item['video_path'] = record['path'] = str(self.output / record['filename'])
        self.queue.update(self.day, lambda _: queue)
        self.memory.parent.mkdir(exist_ok=True)
        self.memory.write_text(json.dumps(state['idea_memory'], ensure_ascii=False), encoding='utf-8')
        return True

    def restore_media(self, slots):
        queue = self.queue.read(self.day)
        for item in queue['videos']:
            if item['slot'] not in slots or item['generation_status'] != 'ready':
                continue
            record = item['generated_files'][0]
            artifact = self.store.by_id(record['artifact_id'], record['run_id'])
            require(artifact['name'] == record['artifact_name'], 'Wrong slot media artifact')
            path = Path(item['video_path'])
            self.store.download(artifact, record['filename'], path)
            actual = _fingerprint(path)
            require(all(actual[k] == record[k] for k in ('sha256', 'size_bytes')), 'Restored media fingerprint mismatch')
            # ZIP does not retain nanoseconds. Restore the recorded time only AFTER verifying bytes.
            os.utime(path, ns=(record['mtime_ns'], record['mtime_ns']))
            refs = Counter(v['video_path'] for v in queue['videos'] if v.get('video_path'))
            _validated_target(item, self.day, self.output, refs)

    def checkpoint(self, day, data):
        require(not self.poisoned and day == self.day, 'State checkpoint is blocked')
        try:
            current = self.store.latest(day)
            require((current['id'] if current else None) == self.latest_id,
                    'Stale state detected; concurrent writer or newer checkpoint exists')
            snapshot = copy.deepcopy(data)
            for item in snapshot['videos']:
                if item['generation_status'] != 'ready':
                    continue
                slot = item['slot']
                if slot not in self.media:
                    refs = Counter(v['video_path'] for v in snapshot['videos'] if v.get('video_path'))
                    path = _validated_target(item, day, self.output, refs)
                    require(path.is_file(), 'Generated media missing before checkpoint')
                    name = f'mystery-media-{day}-{SLOT_NAMES[slot]}-{self.store.run_id}-{uuid.uuid4().hex}'
                    artifact = self.store.upload(name, path, MEDIA_DAYS)
                    record = copy.deepcopy(item['generated_files'][0])
                    record.update(filename=path.name, artifact_id=artifact['id'],
                                  run_id=self.store.run_id, artifact_name=name)
                    self.media[slot] = record
                item['generated_files'] = [copy.deepcopy(self.media[slot])]
            memory = json.loads(self.memory.read_text(encoding='utf-8')) if self.memory.exists() else {}
            state = {'schema': 1, 'date': str(day), 'generation_run_id': self.state['generation_run_id'] if self.state else self.store.run_id,
                     'privacy_status': self.state['privacy_status'] if self.state else os.getenv('YOUTUBE_PRIVACY_STATUS', 'private'),
                     'queue': snapshot, 'idea_memory': memory, 'previous_state_id': self.latest_id}
            validate_state(state, day)
            path = self.scratch / 'state.json'
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            name = f'{STATE_PREFIX}{day}-{self.store.run_id}-{uuid.uuid4().hex}'
            artifact = self.store.upload(name, path, STATE_DAYS)
            self.latest_id, self.state = artifact['id'], state
        except Exception:
            self.poisoned = True
            raise

    def generate(self, generator=None, *, max_attempts=2, passes=2, complete=True, context=None, before_generation=None, generation_allowed=None):
        self.load(required=False)
        if self.state is None:
            # Preserve the existing idea memory across days, without restoring yesterday's queue.
            previous = [a for a in self.store.artifacts() if a['name'].startswith(STATE_PREFIX)
                        and a['name'][len(STATE_PREFIX):len(STATE_PREFIX) + 10] < str(self.day)]
            if previous:
                latest = self.store.newest_checkpoint(previous)
                path = self.scratch / 'previous-state.json'
                self.store.download(latest, 'state.json', path)
                previous_state = json.loads(path.read_text(encoding='utf-8'))
                validate_state(previous_state, date.fromisoformat(previous_state['date']))
                self.memory.parent.mkdir(exist_ok=True)
                self.memory.write_text(json.dumps(previous_state['idea_memory']), encoding='utf-8')
        else:
            self.restore_media([v['slot'] for v in self.state['queue']['videos'] if v['generation_status'] == 'ready'
                                and (not context or not (v.get('youtube') or {}).get('video_id'))])
        self.queue.checkpoint = self.checkpoint
        # Legacy operations retain their two-attempt bound; preparation supplies its bounded policy.
        for _ in range(passes):
            result = self.queue.generate_batch(self.day, generator, resume=True, max_attempts=max_attempts,
                                              context=context, before_generation=before_generation, generation_allowed=generation_allowed)
            require(result is not None, 'Daily generation failed to return saved queue state')
            if all(v['generation_status'] == 'ready' for v in result['videos']):
                break
        bad = [SLOT_NAMES[v['slot']] for v in result['videos'] if v['generation_status'] != 'ready']
        if not complete:
            return result
        require(not bad, 'Incomplete three-video batch: ' + ', '.join(bad))
        require(len(result['videos']) == 3 and len(self.media) == 3, 'Three usable media artifacts required')
        return {'status': 'ready', 'generation_run_id': self.state['generation_run_id'], 'slots': list(SLOT_NAMES.values())}

    def upload(self, slot, generation_run=None, uploader=None, notifier=None, *, catch_up=False):
        require(slot in UPLOAD_TIMES, 'Select exactly one valid upload slot')
        self.load(generation_run)
        require(self.day == india_now().date(), 'Legacy upload only accepts today in Asia/Kolkata')
        require(not self.state['queue'].get('preparation'), 'Prepared batches must publish through YouTube publishAt')
        items = self.queue.read(self.day)['videos']
        if catch_up:
            # Each item retains its original slot, claim, outcome and notification.
            candidates = [v['slot'] for v in items if v['slot'] <= slot
                          and v['generation_status'] == 'ready'
                          and v['upload_status'] == 'pending'
                          and not (v.get('youtube') or {}).get('success')
                          and not (v.get('youtube') or {}).get('video_id')]
            if not candidates:
                return {'status': 'skipped', 'reason': 'no_ready_pending_item'}
            results = []
            for original_slot in candidates:
                # Stop on an uncertain transfer or checkpoint/notification failure.
                # Earlier successes are durable and will be skipped on the next run.
                results.append({'slot': original_slot,
                                'result': self._upload_ready_slot(original_slot, uploader, notifier)})
            return {'status': 'uploaded', 'uploads': results}
        return self._upload_ready_slot(slot, uploader, notifier)

    def _upload_ready_slot(self, slot, uploader=None, notifier=None):
        require(self.day == india_now().date(), "Legacy upload only accepts today in Asia/Kolkata")
        item = next(v for v in self.queue.read(self.day)['videos'] if v['slot'] == slot)
        require(item['upload_status'] == 'pending' and not (item.get('youtube') or {}).get('video_id'),
                f'{SLOT_NAMES[slot]} already attempted ({item["upload_status"]}); no automatic retry')
        require(item['generation_status'] == 'ready', 'Selected slot is not ready')
        self.restore_media([slot])
        require(YOUTUBE_PRIVACY_STATUS == self.state['privacy_status'],
                'Configured privacy differs from generation-time privacy; restore the saved setting')
        if uploader is None:
            from engine.uploader.youtube_uploader import upload_generated_video
            uploader = upload_generated_video
        if notifier is None:
            from engine.approval.telegram_bot import notify_generated_video
            notifier = notify_generated_video

        def conservative_upload(path, metadata):
            try:
                result = uploader(path, metadata)
            except Exception as exc:
                result = {'success': False, 'status': 'uncertain', 'error_type': type(exc).__name__}
            # The existing wrapper cannot prove that a failed transfer was not accepted.
            # Fail closed instead of enabling the local explicit failed-upload retry path.
            if not isinstance(result, dict) or not result.get('success') or not result.get('video_id'):
                return {'success': False, 'status': 'uncertain',
                        'error': 'YouTube outcome unconfirmed; reconcile in Studio before any further action.',
                        'error_type': result.get('error_type', 'UploadUnconfirmed') if isinstance(result, dict) else 'InvalidResponse'}
            return result

        def success_notification(result):
            if not result['upload']['success']:
                return {'success': False, 'status': 'not_attempted'}
            return notifier(result)

        self.queue.checkpoint = self.checkpoint
        result = self.queue.upload_slot(slot, self.day, conservative_upload, success_notification)
        require(result and result.get('upload', {}).get('success') and result.get('state', {}).get('success', True),
                'Upload failed, uncertain, or state persistence failed; inspect latest claim; do not reupload')
        require(result.get('telegram', {}).get('success'), 'YouTube uploaded; Telegram failed. Do not reupload')
        return result


def cleanup_production(store, root=BASE_DIR, now=None):
    now = now or india_now()
    days = sorted({date.fromisoformat(a['name'][len(STATE_PREFIX):len(STATE_PREFIX) + 10])
                   for a in store.artifacts() if a['name'].startswith(STATE_PREFIX)})
    controllers = []
    for day in days:
        if 0 <= (now.date() - day).days < 8:
            controller = Production(store, day, root)
            controller.load()
            if controller.state['queue'].get('preparation') and controller.state['queue'].get('batch_status') != 'READY':
                continue
            items = controller.queue.read(day)['videos']
            eligible = [v['slot'] for v in items if day < now.date() and _eligible(v, now) and v.get('cleanup', {}).get('status') != 'completed']
            controller.restore_media(eligible)
            controllers.append(controller)
    if not controllers:
        return {'status': 'completed', 'items_updated': 0}
    # Restore all recent queues before shared-reference validation. Each write checkpoints its day.
    by_day = {c.day: c for c in controllers}
    queue = controllers[0].queue
    queue.checkpoint = lambda day, data: by_day[day].checkpoint(day, data)
    from engine.scheduling.cleanup import cleanup_recent
    result = cleanup_recent(queue, now=now, approved_dir=controllers[0].output)
    require(result.get('status') == 'completed', 'Local guarded cleanup failed')
    require(all(not c.poisoned for c in controllers), 'Cleanup checkpoint failed; remote media retained')
    for controller in controllers:
        if controller.day >= now.date():
            continue
        data = queue.read(controller.day)
        for item in data['videos']:
            if not _eligible(item, now):
                continue
            require(item.get('cleanup', {}).get('status') == 'completed', 'Cleanup ownership/fingerprint checks failed')
            record = item['generated_files'][0]
            # Idempotent after a crash between remote deletion and the final checkpoint.
            if item.get('artifact_cleanup') != 'deleted':
                store.delete(record['artifact_id'])
                item['artifact_cleanup'] = 'deleted'
                queue.update(controller.day, lambda _: data)
    return result


def restore_oauth():
    require(os.environ.get('YOUTUBE_TOKEN_PICKLE_B64'), 'Missing YOUTUBE_TOKEN_PICKLE_B64 secret')
    try:
        token = base64.b64decode(os.environ['YOUTUBE_TOKEN_PICKLE_B64'], validate=True)
        require(bool(token), 'Empty OAuth token')
        path = Path(BASE_DIR) / 'token.pickle'
        path.write_bytes(token)
        path.chmod(0o600)
        # Existing noninteractive refresh behavior; no new OAuth implementation.
        from engine.uploader.youtube_uploader import get_authenticated_service
        service = get_authenticated_service()
        service.close()
    except Exception as exc:
        raise ProductionError(f'YouTube OAuth preflight failed ({type(exc).__name__}); authorize locally and refresh the secret') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'cleanup'])
    parser.add_argument('--day', default='', help='Target publication date; tomorrow or explicit existing current-day recovery in Asia/Kolkata')
    parser.add_argument("--confirmed-not-uploaded-slot", "--confirm-not-uploaded-slot",
                        dest="confirmed_not_uploaded_slot", default="", choices=["", *UPLOAD_TIMES],
                        help="Explicit Studio confirmation of absence; requires prepare --day")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    for name in ('httpx', 'httpcore', 'googleapiclient.discovery_cache'):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        require(os.getenv('GITHUB_ACTIONS') == 'true', 'Use the manual production Actions workflow')
        require(os.environ.get('GITHUB_REF') == 'refs/heads/' + os.environ.get('PRODUCTION_BRANCH', ''),
                'Production operations must run on the default branch')
        require(not args.confirmed_not_uploaded_slot or (args.operation == "prepare" and args.day),
                "Confirmed-not-uploaded reconciliation requires prepare with an explicit --day")
        started_at = india_now()
        store = GitHubArtifacts()
        if args.operation == 'prepare':
            from engine.scheduling.preparation import Preparation, target_day
            day = target_day(started_at, args.day)
            require(YOUTUBE_PRIVACY_STATUS == 'private', 'Production preparation requires private privacy')
            require(os.getenv('YOUTUBE_CHANNEL_ID'), 'Missing expected YouTube channel identity')
            def authenticate():
                restore_oauth()
                from engine.uploader.youtube_uploader import preparation_preflight
                preparation_preflight(os.environ['YOUTUBE_CHANNEL_ID'])
            def before_render():
                require(all(os.getenv(k) for k in ('OPENAI_API_KEY', 'PEXELS_API_KEY')),
                        'Missing generation API secrets')
            recovery_result = None
            if not args.day:
                from engine.scheduling.preparation import TZ
                today = started_at.astimezone(TZ).date()
                # Inspect only today's existing checkpoint; never scan arbitrary pending history.
                if store.latest(today) is not None:
                    recovery = Preparation(store, today)
                    recovery.load()
                    if recovery.recoverable_slots(expected_channel=os.environ['YOUTUBE_CHANNEL_ID'], now=india_now()):
                        recovery_result = recovery.prepare(expected_channel=os.environ['YOUTUBE_CHANNEL_ID'],
                            authenticate=authenticate, before_render=before_render,
                            allow_current_day=True, started_at=started_at)
                    else:
                        recovery_result = {'status': 'SKIPPED', 'target_date': str(today),
                                           'reason': 'no_safe_future_work'}
            controller = Preparation(store, day)
            reconciliation = None
            if args.confirmed_not_uploaded_slot:
                reconciliation = controller.reconcile_not_uploaded(args.confirmed_not_uploaded_slot,
                    expected_channel=os.environ["YOUTUBE_CHANNEL_ID"])
            result = controller.prepare(expected_channel=os.environ['YOUTUBE_CHANNEL_ID'],
                                        authenticate=authenticate, before_render=before_render,
                                        allow_current_day=bool(args.day), started_at=started_at)
            if reconciliation is not None:
                result["reconciliation"] = reconciliation
            if recovery_result is not None:
                result['current_day_recovery'] = recovery_result
            if result['status'] == 'READY':
                result['cleanup'] = cleanup_production(store)
        else:
            result = cleanup_production(store)
        recovery = result.get('current_day_recovery', {})
        incomplete = args.operation == 'prepare' and (
            (result['status'] != 'READY' and not result.get('verification_pending')) or
            (recovery.get('status') == 'PREPARING' and not recovery.get('verification_pending')))
        pending = result.get('verification_pending') or recovery.get('verification_pending')
        outcome = 'RECOVERABLE' if incomplete else ('VERIFICATION_PENDING' if pending else 'SUCCESS')
        message = json.dumps(result, ensure_ascii=False)
        print(message)
        if os.getenv('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(f'Production {args.operation}: {outcome}\n\n```json\n{message}\n```\n')
        return 1 if incomplete else 0
    except Exception as exc:
        # Never print arbitrary network/auth exception strings (they may contain credentials).
        cause = exc.__cause__ if isinstance(exc, CheckpointError) else exc
        # ProductionError messages are deliberately credential-free. Preserve a
        # wrapped validation diagnosis, without exposing arbitrary SDK exceptions.
        message = (f'{type(exc).__name__}: {cause}' if isinstance(cause, ProductionError)
                   else f'{type(exc).__name__}; inspect saved queue/artifact state')
        print(f'Production failed: {message}')
        if os.getenv('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(f'Production FAILED: {message}\n')
        return 1


if __name__ == '__main__':
    # The preparation module shares this module's classes when invoked as a script.
    import sys
    sys.modules.setdefault('run_actions', sys.modules[__name__])
    raise SystemExit(main())
