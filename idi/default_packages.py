"""Prepare project-default deliverables after verified official repair submission."""
import json
import logging
import threading

from .db import now
from .domain import DomainError
from .identity import acting_as, require_project
from .package_content import content_fingerprint

log = logging.getLogger(__name__)


class DefaultPackages:
    def __init__(self, db, domain, reporter, jobs):
        self.db, self.domain, self.reporter, self.jobs = db, domain, reporter, jobs
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

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
        result = {**context, 'state': 'pending' if context['eligible'] else 'waiting', 'error': '', 'package': None}
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
            context = self.context(case_id)
            if not context['eligible']:
                self._save(context, 'waiting')
                return
            previous = self.db.one('SELECT * FROM case_default_packages WHERE case_id=?', (case_id,))
            job = self.jobs.get(previous['job_id']) if previous and previous['job_id'] else None
            if job and job['state'] in ('queued', 'running'):
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

    def retry(self, case_id):
        require_project(self.domain.case(case_id)['project_id'], manage=True)
        if not self.context(case_id)['eligible']:
            raise DomainError('官网尚未确认修复成果已提交，暂不生成默认资料', 409)
        self.prepare(case_id, force=True)
        return self.get(case_id)

    def run_once(self):
        # Also revisit tracked cases so a withdrawn submission loses its ready status.
        rows = self.db.all("SELECT c.id FROM cases c WHERE c.id IN (SELECT case_id FROM case_default_packages) OR "
                           "(c.workflow_source='platform' AND c.workflow_stage IN ('acceptance','settlement','closed') "
                           "AND EXISTS (SELECT 1 FROM photos p WHERE p.case_id=c.id))")
        for row in rows:
            if self._stop.is_set():
                break
            try:
                self.prepare(row['id'])
            except Exception:
                log.exception('Default package preparation failed: %s', row['id'])

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
