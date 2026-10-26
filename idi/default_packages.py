"""Prepare project-default deliverables after verified official repair submission."""
import hashlib
import json
import logging
import threading
import time

from .db import dump, now, uid
from .domain import DomainError
from .identity import acting_as, require_project
from .package_content import content_fingerprint

log = logging.getLogger(__name__)
RECHECK_SECONDS = 300


class DefaultPackages:
    def __init__(self, db, domain, reporter, jobs):
        self.db, self.domain, self.reporter, self.jobs = db, domain, reporter, jobs
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._observed = {}

    def _observation(self, case_id):
        """Cheap invalidation inputs; do not assemble or hash exported content.

        Include sources and visits as well as photo revisions: they can change
        independently, and are part of the immutable delivery snapshot.
        """
        case = self.db.one('SELECT * FROM cases WHERE id=?', (case_id,))
        project = self.db.one('SELECT * FROM projects WHERE id=?', (case['project_id'],))
        tracker = self.db.one('SELECT * FROM case_default_packages WHERE case_id=?', (case_id,))
        job = self.db.one('SELECT state,result,error FROM jobs WHERE id=?', (tracker['job_id'],)) if tracker and tracker['job_id'] else None
        package = self.db.one('SELECT id,state,zip_path,zip_hash FROM packages WHERE id=?', (tracker['package_id'],)) if tracker and tracker['package_id'] else None
        file_state = None
        if package and package['zip_path']:
            try:
                stat = self.reporter.storage.resolve(package['zip_path']).stat()
                file_state = (stat.st_mode, stat.st_size, stat.st_mtime_ns)
            except OSError:
                file_state = 'missing'
        session = self.reporter.official.platform.client.status() if self.reporter.official else {}
        values = [case, project, tracker, job, package, file_state,
                  session.get('connected'), session.get('account'),
                  self.db.one('SELECT * FROM templates WHERE id=?', (project['insurer_template_id'],)),
                  self.db.one('SELECT * FROM picc_record_drafts WHERE case_id=?', (case_id,)),
                  self.db.one('SELECT * FROM platform_links WHERE case_id=?', (case_id,)),
                  self.db.all('SELECT p.*,b.* FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE p.case_id=? ORDER BY p.id', (case_id,)),
                  self.db.all('SELECT s.* FROM photo_sources s JOIN photos p ON p.id=s.photo_id WHERE p.case_id=? ORDER BY s.id', (case_id,)),
                  self.db.all('SELECT * FROM visits WHERE case_id=? ORDER BY id', (case_id,)),
                  self.db.all('SELECT s.* FROM visit_shared_checkins s JOIN visits v ON v.id=s.visit_id JOIN photos p ON p.id=s.photo_id WHERE v.case_id=? OR p.case_id=? ORDER BY s.photo_id,s.visit_id', (case_id, case_id)),
                  self.db.all('SELECT id,reuse_source_id FROM photos WHERE reuse_source_id IN (SELECT id FROM photos WHERE case_id=?) ORDER BY id', (case_id,)),
                  self.db.all('SELECT m.*,b.* FROM platform_media m JOIN blobs b ON b.hash=m.blob_hash WHERE m.case_id=? ORDER BY m.report_receiving_id,m.file_id,m.source_url', (case_id,))]
        return hashlib.sha256(dump(values).encode()).hexdigest()

    def context(self, case_id):
        case = self.domain.case(case_id)
        project = self.db.one('SELECT insurer_template_id FROM projects WHERE id=?', (case['project_id'],))
        template_id = project['insurer_template_id']
        template = self.db.one('SELECT name FROM templates WHERE id=?', (template_id,))
        link = self.db.one('SELECT snapshot,verified_at FROM platform_links WHERE case_id=?', (case_id,))
        snapshot = json.loads(link['snapshot']) if link else {}
        row = {**snapshot.get('claim', {}), **snapshot.get('row', {})}
        eligible = (case['workflow_source'] == 'platform' and case['workflow_stage'] in ('acceptance', 'settlement', 'closed')
                    and bool(snapshot.get('list_verified_at') or (link or {}).get('verified_at'))
                    and str(row.get('repairAcceptanceFlowStatus', '')) in ('1', '2', '9')
                    and str(row.get('repairAcceptanceState', '')) != '0')
        return {'case_id': case_id, 'template_id': template_id, 'template_name': template['name'] if template else '资料要求已失效',
                'eligible': bool(eligible)}

    def get(self, case_id):
        context = self.context(case_id)
        row = self.db.one('SELECT * FROM case_default_packages WHERE case_id=?', (case_id,))
        pending = self.db.one("SELECT 1 FROM default_package_requests WHERE case_id=? AND state='pending' LIMIT 1", (case_id,))
        result = {**context, 'state': ('pending' if pending else 'manual') if context['eligible'] else 'waiting', 'error': '', 'package': None}
        if not context['eligible'] or not row or row['template_id'] != context['template_id']:
            return result
        result.update(state=row['state'], error=row['error'], updated_at=row['updated_at'])
        if row['state'] == 'ready' and row['package_id']:
            package = self.db.one('SELECT state,version,zip_path FROM packages WHERE id=?', (row['package_id'],))
            if not package or package['state'] == 'deleted':
                result['state'] = 'deleted'
            elif not self.reporter.storage.resolve(package['zip_path']).is_file():
                result.update(state='failed', error='默认资料文件缺失，请重新准备')
            else:
                result['package'] = {'id': row['package_id'], 'version': package['version'],
                                     'download_url': f"/api/packages/{row['package_id']}/download"}
        return result

    def _save(self, context, state, fingerprint='', job_id=None, package_id=None, error=''):
        values = (context['template_id'], fingerprint, job_id, package_id, state, error[:3000])
        with self.db.tx() as con:
            old = con.execute('SELECT template_id,fingerprint,job_id,package_id,state,error FROM case_default_packages WHERE case_id=?', (context['case_id'],)).fetchone()
            if old and tuple(old) == values:
                return
            con.execute('INSERT INTO case_default_packages VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET '
                        'template_id=excluded.template_id,fingerprint=excluded.fingerprint,job_id=excluded.job_id,'
                        'package_id=excluded.package_id,state=excluded.state,error=excluded.error,updated_at=excluded.updated_at',
                        (context['case_id'], *values, now()))

    def prepare(self, case_id, force=False):
        # System preparation persists independently of the user who submitted.
        with self._lock, acting_as(None):
            if not force and not self.db.one("SELECT 1 FROM default_package_requests WHERE case_id=? AND state='pending' LIMIT 1", (case_id,)):
                return
            context = self.context(case_id)
            if not context['eligible']:
                self._save(context, 'waiting')
                return
            previous = self.db.one('SELECT * FROM case_default_packages WHERE case_id=?', (case_id,))
            job = self.jobs.get(previous['job_id']) if previous and previous['job_id'] else None
            if job and job['state'] in ('queued', 'running'):
                return
            if self.reporter.official and self.reporter.official.linked(case_id):
                self._prepare_official(context, previous, job, force)
                return
            try:
                with self.db.read_snapshot():
                    ids = [p['id'] for p in self.reporter.picc.all_photos(case_id) if p['media_type'] in ('image', 'video')]
                    content = self.reporter._content(case_id, ids, context['template_id'], None)
                    fingerprint = content_fingerprint(content)
            except DomainError as error:
                self._save(context, 'blocked', error=str(error))
                return
            if previous and not force:
                if previous['fingerprint'] == fingerprint and job and job['state'] in ('failed', 'partial'):
                    self._save(context, 'failed', fingerprint, job['id'], error=job['error'] or '资料生成未完成，请重试')
                    return
                package_id = previous['package_id']
                if job and job['state'] == 'completed':
                    package_id = next((p['id'] for p in (job['result'] or {}).get('packages', []) if p['case_id'] == case_id), package_id)
                if package_id:
                    package = self.db.one('SELECT * FROM packages WHERE id=?', (package_id,))
                    # Re-evaluate saved snapshots so fingerprint-rule upgrades do
                    # not enqueue duplicates or revive intentionally deleted files.
                    same_content = package and content_fingerprint(json.loads(package['manifest'])) == fingerprint
                    if package and package['state'] == 'deleted' and (same_content or previous['fingerprint'] == fingerprint):
                        self._save(context, 'deleted', fingerprint, (job or {}).get('id'), package_id)
                        return
                    if same_content and package['state'] in ('ready', 'submitted', 'accepted', 'returned'):
                        present = self.reporter.storage.resolve(package['zip_path']).is_file()
                        self._save(context, 'ready' if present else 'failed', fingerprint, (job or {}).get('id'), package_id,
                                   '' if present else '默认资料文件缺失，请重新准备')
                        return
            try:
                job = self.reporter.enqueue_case_export(self.jobs, case_id, context['template_id'], '默认资料 · ' + context['template_name'])
                package_id = next((p['id'] for p in (job['result'] or {}).get('packages', []) if p['case_id'] == case_id), None)
                self._save(context, 'ready' if package_id else 'preparing', fingerprint, job['id'], package_id)
            except DomainError as error:
                self._save(context, 'blocked', fingerprint, error=str(error))

    def _prepare_official(self, context, previous, job, force):
        case_id = context['case_id']
        ids = [p['id'] for p in self.reporter.picc.all_photos(case_id) if p['media_type'] in ('image', 'video')]
        fingerprint = self.reporter.input_fingerprint(case_id, ids, context['template_id'])
        package_id = (previous or {}).get('package_id')
        if job and job['state'] == 'completed':
            package_id = next((p['id'] for p in (job['result'] or {}).get('packages', []) if p['case_id'] == case_id), package_id)
        package = self.db.one('SELECT * FROM packages WHERE id=?', (package_id,)) if package_id else None
        if not force:
            if package and package['state'] == 'deleted':
                self._save(context, 'deleted', fingerprint, (job or {}).get('id'), package_id)
                return
            if previous and previous['fingerprint'] == fingerprint and job and job['state'] in ('failed', 'partial'):
                self._save(context, 'failed', fingerprint, job['id'], error=job['error'] or '官网照片核对或资料生成未完成，请重试')
                return
        try:
            content = self.reporter._content(case_id, ids, context['template_id'], None)
        except DomainError as error:
            # No local photos/date is recoverable by the worker's official read.
            # Preserve actionable classification errors on actual local files.
            if '分类' in str(error):
                self._save(context, 'blocked', fingerprint, error=str(error))
                return
            content = None
        if not force and package and content and package['state'] in ('ready', 'submitted', 'accepted', 'returned'):
            if content_fingerprint(json.loads(package['manifest'])) == content_fingerprint(content):
                present = self.reporter.storage.resolve(package['zip_path']).is_file()
                self._save(context, 'ready' if present else 'failed', fingerprint, (job or {}).get('id'), package_id,
                           '' if present else '默认资料文件缺失，请重新准备')
                return
        if not self.reporter.official.platform.client.status()['connected']:
            self._save(context, 'blocked', fingerprint, error='官网尚未连接，连接后自动核对并补齐照片')
            return
        job = self.reporter.enqueue_case_export(self.jobs, case_id, context['template_id'], '默认资料 · ' + context['template_name'])
        self._save(context, 'preparing', fingerprint, job['id'])

    def retry(self, case_id):
        require_project(self.domain.case(case_id)['project_id'], manage=True)
        if not self.context(case_id)['eligible']:
            raise DomainError('官网尚未确认修复成果已提交，暂不生成默认资料', 409)
        request_id = uid()
        with self.db.tx() as con:
            con.execute("INSERT INTO default_package_requests(id,case_id,source,source_id,created_at) VALUES(?,?,'manual',?,?)",
                        (request_id, case_id, request_id, now()))
        self.prepare(case_id, force=True)
        return self.get(case_id)

    def run_once(self):
        # A later read of an old official status is not a new submission.
        # Durable confirmation records provide the trigger, including after a
        # restart; a completed request never enrolls its case for later rebuilds.
        with self.db.read_snapshot():
            policy = self.db.setting('default_package_auto_policy', {})
            submissions = self.db.all("SELECT 'submission:'||r.id AS id,r.case_id,'submission' AS source,r.id AS source_id,r.confirmed_at AS created_at "
                "FROM platform_review_batches r WHERE r.stage='repair' AND r.state='verified' AND r.save_only=0 "
                "AND json_extract(r.result,'$.action')='submit' AND julianday(r.confirmed_at)>=julianday(?) "
                "AND NOT EXISTS(SELECT 1 FROM default_package_requests q WHERE q.source='submission' AND q.source_id=r.id)",
                (policy.get('activated_at'),))
            pending = self.db.all("SELECT id,case_id FROM default_package_requests WHERE state='pending' ORDER BY created_at,id")
            by_case = {}
            for request in pending + submissions:
                by_case.setdefault(request['case_id'], []).append(request['id'])
            observations = [(case_id, (tuple(request_ids), self._observation(case_id))) for case_id, request_ids in by_case.items()]
        if submissions:
            with self.db.tx() as con:
                con.executemany('INSERT OR IGNORE INTO default_package_requests(id,case_id,source,source_id,created_at) VALUES(?,?,?,?,?)',
                                [(r['id'], r['case_id'], r['source'], r['source_id'], r['created_at']) for r in submissions])
        active = set(by_case)
        self._observed = {key: value for key, value in self._observed.items() if key in active}
        for case_id, observation in observations:
            if self._stop.is_set():
                break
            previous, checked_at = self._observed.get(case_id, (None, 0))
            if previous == observation and time.monotonic() - checked_at < RECHECK_SECONDS:
                continue
            try:
                self.prepare(case_id)
                tracker = self.db.one('SELECT state FROM case_default_packages WHERE case_id=?', (case_id,))
                if tracker and tracker['state'] in ('ready', 'failed', 'deleted', 'waiting'):
                    # Only settle requests observed before the work. A new
                    # submission arriving concurrently must get its own turn.
                    with self.db.tx() as con:
                        con.executemany("UPDATE default_package_requests SET state='handled',finished_at=? WHERE id=? AND state='pending'",
                                        [(now(), request_id) for request_id in by_case[case_id]])
                # Remember only inputs seen BEFORE preparation. A concurrent
                # edit or completed job must still be noticed on the next poll.
                self._observed[case_id] = (observation, time.monotonic())
            except Exception:
                log.exception('Default package preparation failed: %s', case_id)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='idi-default-packages', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception('Default package scan failed')
            self._stop.wait(5)
