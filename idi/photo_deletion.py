"""Remove active photo records while retaining immutable originals and audit evidence."""
from __future__ import annotations

import json

from .db import audit, dump, now
from .domain import DomainError


def preview(domain, ids):
    photos = domain.photos(ids=ids, page_size=500)['items']
    if len(photos) != len(ids):
        raise DomainError('部分照片已不存在，请刷新列表后重新选择', 409)
    return {'items': [{key: p.get(key) for key in (
        'id', 'revision', 'original_name', 'thumbnail_url', 'case_title', 'project_name', 'short_code')}
        for p in photos], 'total': len(photos)}


def delete_photos(domain, ids, revisions=None):
    selected = set(ids)
    if revisions is not None and (not isinstance(revisions, dict) or set(revisions) != selected
            or any(type(v) is not int for v in revisions.values())):
        raise DomainError('照片版本信息不完整，请重新打开删除确认框')
    marks = ','.join('?' for _ in ids)
    stamp = now()
    with domain.db.tx() as con:
        photos = [dict(row) for row in con.execute(f'SELECT * FROM photos WHERE id IN ({marks})', ids)]
        if len(photos) != len(ids):
            raise DomainError('部分照片已不存在，请刷新列表后重新选择', 409)
        if revisions is not None and any(p['revision'] != revisions[p['id']] for p in photos):
            raise DomainError('照片信息已变化，本次未删除；请重新打开确认框核对', 409)

        # Worker claims and this deletion use the same SQLite write transaction.
        if con.execute(f"SELECT 1 FROM photo_ai_tasks WHERE photo_id IN ({marks}) AND state='running'", ids).fetchone():
            raise DomainError('所选照片正在分析，请等待完成后再删除', 409)
        jobs = []
        for row in con.execute("SELECT * FROM jobs WHERE state IN ('queued','running')"):
            payload = json.loads(row['payload'])
            related = selected.intersection(payload.get('photo_ids', []))
            if row['state'] == 'running' and (related or row['kind'] in ('import', 'export', 'archive_tree')):
                raise DomainError('相关资料正在导入、识别或导出，请等待完成后再删除', 409)
            if related:
                jobs.append((dict(row), payload))
        tasks = []
        for row in con.execute("SELECT * FROM feishu_agent_tasks WHERE state IN ('queued','running')"):
            payload = json.loads(row['input_json'])
            if selected.intersection(payload.get('photo_ids', [])):
                if row['state'] == 'running':
                    raise DomainError('所选照片正在由群助手处理，请等待完成后再删除', 409)
                tasks.append((dict(row), payload))

        for photo in photos:
            sources = [dict(r) for r in con.execute('SELECT * FROM photo_sources WHERE photo_id=?', (photo['id'],))]
            recognitions = [dict(r) for r in con.execute('SELECT * FROM recognitions WHERE photo_id=?', (photo['id'],))]
            audit(con, 'photo', photo['id'], 'delete', {**photo, 'sources': sources, 'recognitions': recognitions},
                  {'original_retained': True, 'reason': '用户删除不用的照片'})

        # Preserve histories, but detach them from active photos and prevent old
        # import/message retries from bringing the removed record back.
        con.execute(f"UPDATE import_items SET photo_id=NULL,status='deleted',error=NULL WHERE photo_id IN ({marks})", ids)
        con.execute(f"UPDATE messages SET photo_id=NULL,media_state='deleted',error=NULL WHERE photo_id IN ({marks})", ids)
        con.execute(f"UPDATE followup_issues SET photo_id=NULL,state='dismissed',resolution='照片已由用户删除',changed_at=? WHERE photo_id IN ({marks})", (stamp, *ids))

        for job, payload in jobs:
            payload['photo_ids'] = [pid for pid in payload['photo_ids'] if pid not in selected]
            con.execute('UPDATE jobs SET payload=?,state=?,finished_at=?,error=? WHERE id=?',
                        (dump(payload), 'queued' if payload['photo_ids'] else 'cancelled',
                         None if payload['photo_ids'] else stamp, '已移除用户删除的照片', job['id']))
        for task, payload in tasks:
            payload['photo_ids'] = [pid for pid in payload['photo_ids'] if pid not in selected]
            payload['total_images'] = len(payload['photo_ids'])
            con.execute('UPDATE feishu_agent_tasks SET input_json=?,state=?,finished_at=?,error=? WHERE id=?',
                        (dump(payload), 'queued' if payload['photo_ids'] else 'cancelled',
                         None if payload['photo_ids'] else stamp, '已移除用户删除的照片', task['id']))
        for row in con.execute("SELECT id,photo_ids FROM rule_draft_intents WHERE state IN ('queued','failed')").fetchall():
            if selected.intersection(json.loads(row['photo_ids'])):
                con.execute("UPDATE rule_draft_intents SET state='stale',error='照片已删除，请重新准备草稿',updated_at=? WHERE id=?", (stamp, row['id']))

        # Remove draft selections and automatic values whose evidence was deleted.
        # Manual fields and official upload receipts are deliberately preserved.
        for row in con.execute('SELECT * FROM platform_drafts').fetchall():
            files, values = json.loads(row['files_json']), json.loads(row['values_json'])
            remaining = {key: [pid for pid in entries if pid not in selected] for key, entries in files.items()}
            changed = remaining != files
            sources = con.execute("SELECT * FROM platform_draft_field_sources WHERE case_id=? AND stage=? AND origin='automatic'", (row['case_id'], row['stage'])).fetchall()
            for source in sources:
                if selected.intersection(json.loads(source['photo_ids'])):
                    if values.get(source['field']) == source['value']:
                        values.pop(source['field'])
                    con.execute('DELETE FROM platform_draft_field_sources WHERE case_id=? AND stage=? AND field=?', (row['case_id'], row['stage'], source['field']))
                    changed = True
            if changed:
                con.execute('UPDATE platform_drafts SET files_json=?,values_json=?,revision=revision+1,updated_at=? WHERE case_id=? AND stage=?',
                            (dump(remaining), dump(values), stamp, row['case_id'], row['stage']))

        case_ids = {p['case_id'] for p in photos} - {None}
        for case_id in case_ids:
            con.execute("UPDATE platform_review_batches SET state='stale' WHERE case_id=? AND state IN ('prepared','reviewed','failed')", (case_id,))
            con.execute("UPDATE automatic_upload_cases SET signature='',review_guard='',retry_at=0,updated_at=? WHERE case_id=?", (stamp, case_id))
        visit_ids = {p['visit_id'] for p in photos} - {None}
        visit_ids.update(r['visit_id'] for r in con.execute(f'SELECT visit_id FROM visit_shared_checkins WHERE photo_id IN ({marks})', ids))
        for visit_id in visit_ids:
            con.execute('UPDATE visits SET revision=revision+1 WHERE id=?', (visit_id,))
        # Deleting a copy must not reactivate the legacy virtual use of its
        # survey original. The original's archive record remains untouched.
        for photo in photos:
            if photo['reuse_source_id']:
                con.execute('DELETE FROM visit_shared_checkins WHERE photo_id=? AND visit_id=?',
                            (photo['reuse_source_id'], photo['visit_id']))
        for table in ('visit_shared_checkins', 'platform_draft_exclusions', 'feishu_agent_images',
                      'photo_quality_reviews', 'photo_rule_reviews', 'photo_ai_tasks', 'photo_pipeline',
                      'recognitions', 'photo_sources'):
            con.execute(f'DELETE FROM {table} WHERE photo_id IN ({marks})', ids)
        con.execute(f'DELETE FROM photos WHERE id IN ({marks})', ids)
    return {'deleted': ids, 'deleted_count': len(ids), 'originals_retained': True}
