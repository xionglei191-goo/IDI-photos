from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .db import Database, audit, dump, now, uid
from .domain import Domain, DomainError
from .platform_client import PlatformClient, external_id, unsubmitted_report
from .platform_contract import STAGES, VERSION, contract, visible
from .platform_prefill import canonical_value, input_signature, stage_suggestions, without_automatic_remote
from .platform_photos import active_attachment
from .storage import Storage, safe_name
from .stage_access import stage_access
from .workflow import remote_stage


def _empty(value):
    return value is None or value == "" or value == []


def _text(value):
    return "" if value is None else str(value)


def node_hint(snapshot):
    row = snapshot.get("row", {})
    current = remote_stage(snapshot)
    if current == 'historical_unverified':
        return '历史记录缺少受理结果，待核实'
    if current == 'insurer_intake':
        return '待保司处理，维保受理尚未开始'
    if current == 'insurer_review':
        return '受理结果待保司复核'
    if unsubmitted_report(row, snapshot.get('scope')):
        return "官网报案待提交"
    if row.get("interceptFlag") == "1":
        return "已拦截，需在官网核实"
    if row.get("repairMethod") in ("3", "4"):
        return "零注拒流程"
    if snapshot.get("scope") == "intake" and not row.get("siteSurveyId"):
        return "理赔受理列表，办理节点以官网为准"
    if row.get("siteSurveyState") != "1":
        return "现场查勘"
    return {"1": "维修成果验收", "2": "申请结算", "9": "流程结束", "0": "修复成果", "": "修复成果"}.get(_text(row.get("repairAcceptanceFlowStatus")), "节点待核实")


class PlatformPreparation:
    def __init__(self, db: Database, domain: Domain, storage: Storage, client: PlatformClient):
        self.db, self.domain, self.storage, self.client = db, domain, storage, client
        self.workflow = None
        self.material_lock = threading.RLock()
        from .platform_media import PlatformMedia
        self.media = PlatformMedia(self)
        from .attachment_removals import AttachmentRemovals
        self.removals = AttachmentRemovals(self)

    def dictionaries(self):
        initial = json.loads(Path(__file__).with_name("platform_dictionary.json").read_text(encoding="utf-8"))
        return self.db.setting("platform_dictionaries", initial)

    def prefill_defaults(self):
        return self.db.setting('platform_prefill_defaults', {})

    def save_prefill_defaults(self, data):
        key = 'estimateRestorationCostCode'
        if not isinstance(data, dict) or set(data) - {key}:
            raise DomainError('预填默认设置包含不支持的字段')
        value = data.get(key)
        if value not in (None, '') and (not isinstance(value, str) or
                value not in {o['code'] for o in self.dictionaries()['items'].get('estimate_restoration_cost', [])}):
            raise DomainError('请选择官网有效的修复预估档位')
        with self.material_lock, self.db.tx() as con:
            before = self.prefill_defaults()
            after = {key: value} if value else {}
            con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('platform_prefill_defaults', dump(after)))
            audit(con, 'settings', 'platform_prefill_defaults', 'update', before, after)
        return after

    def contract(self):
        data = self.dictionaries()
        return {**contract(data["items"], self.client.write_enabled), "dictionary_verified_at": data["verified_at"]}

    def refresh_dictionaries(self):
        values = self.client.dictionaries()
        self.db.set_setting("platform_dictionaries", {"items": values, "verified_at": now()})
        return self.contract()

    def link(self, case_id):
        self.domain.case(case_id)
        row = self.db.one("SELECT * FROM platform_links WHERE case_id=?", (case_id,))
        if not row:
            return {"case_id": case_id, "report_receiving_id": "", "platform_report_code": "", "insurer_report_number": "",
                    "snapshot": {}, "verified_at": None, "revision": 0, "updated_at": None, "node": "尚未核对官网案件"}
        row["snapshot"] = json.loads(row["snapshot"])
        row["node"] = node_hint(row["snapshot"]) if row["verified_at"] or row["snapshot"].get('list_verified_at') else "编号已记录，尚未在线核对"
        return row

    def save_link(self, case_id, data, snapshot=None):
        self.domain.case(case_id)
        report_id = data.get("report_receiving_id", "")
        if report_id:
            external_id(report_id)
        elif not isinstance(report_id, str):
            raise DomainError("官网记录 ID 应为文字")
        codes = {}
        for key in ("platform_report_code", "insurer_report_number"):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > 160:
                raise DomainError("官网编号应为不超过160字的文字")
            codes[key] = value.strip()
        try:
            with self.db.tx() as con:
                before = con.execute("SELECT * FROM platform_links WHERE case_id=?", (case_id,)).fetchone()
                revision = before["revision"] if before else 0
                if data.get("revision") != revision:
                    raise DomainError("官网关联已被修改，请刷新后重试", 409)
                if before and before['verified_at'] and report_id != before['report_receiving_id']:
                    raise DomainError('已经核对的官网关联不能用手动编号替换，请重新在线核对', 409)
                stamp = now()
                con.execute("""INSERT INTO platform_links VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(case_id) DO UPDATE SET report_receiving_id=excluded.report_receiving_id,
                    platform_report_code=excluded.platform_report_code,insurer_report_number=excluded.insurer_report_number,
                    snapshot=excluded.snapshot,verified_at=excluded.verified_at,revision=excluded.revision,updated_at=excluded.updated_at""",
                    (case_id, report_id, codes["platform_report_code"], codes["insurer_report_number"], dump(snapshot or {}),
                     stamp if snapshot else None, revision + 1, stamp))
                con.execute("UPDATE cases SET platform_report_code=?,report_number=COALESCE(NULLIF(report_number,''),?),revision=revision+1,updated_at=? WHERE id=?",
                            (codes['platform_report_code'] or None, codes['insurer_report_number'] or None, stamp, case_id))
                audit(con, "platform_link", case_id, "readback" if snapshot else "record_identifiers",
                      before={"report_receiving_id": before["report_receiving_id"]} if before else None,
                      after={"report_receiving_id": report_id, **codes, "verified": bool(snapshot)})
        except sqlite3.IntegrityError as error:
            raise DomainError("这个官网案件已经关联了另一个本地案件，请核对后再关联", 409) from error
        return self.link(case_id)

    def bind(self, case_id, data):
        self.domain.case(case_id)
        snapshot = self.client.snapshot(data.get("report_receiving_id"), data.get("scope", "repair"))
        row = snapshot["row"]
        if self.workflow:
            if data.get('revision') != self.link(case_id)['revision']:
                raise DomainError('官网关联已变化，请刷新后重新选择', 409)
            self.workflow.apply_snapshot(snapshot, case_id)
            return self.link(case_id)
        return self.save_link(case_id, {"revision": data.get("revision"),
            "report_receiving_id": str(row["reportReceivingId"]),
            "platform_report_code": _text(row.get("reportCode")),
            "insurer_report_number": _text(row.get("reportNumber"))}, snapshot)

    def _stage(self, stage):
        if stage not in STAGES:
            raise DomainError("官网环节不存在", 404)
        return STAGES[stage]

    def require_editable(self, case_id, stage, con=None):
        self._stage(stage)
        if con:
            row = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
            if not row:
                raise DomainError('案件不存在', 404)
            case = dict(row)
        else:
            case = self.domain.case(case_id)
        access = stage_access(case, stage)
        if not access['editable']:
            raise DomainError(access['reason'] + '请刷新官网进度后在当前环节办理。', 409)
        return access

    def _remote(self, link, stage):
        snapshot = link["snapshot"]
        claim, survey, repair, row = [snapshot.get(key, {}) for key in ("claim", "survey", "repair", "row")]
        if stage == "intake":
            return dict(claim)
        if stage == "survey":
            return dict(survey)
        if stage == "repair":
            result = {**claim, **survey, **repair}
            # Check-in arrays belong to this stage. Never reuse the survey check-in.
            result["buildingFileInfoList"] = repair.get("buildingFileInfoList", [])
            result["houseFileInfoList"] = repair.get("houseFileInfoList", [])
            # A saved empty list is authoritative. Only an absent repair field
            # falls back to the survey mapping used by the official add form.
            if "preRepairFileInfo" not in repair:
                result["preRepairFileInfo"] = survey.get("fileInfoList", survey.get("preRepairFileInfo", claim.get("preRepairFileInfo", [])))
            result["repairMethod"] = row.get("repairMethod") or survey.get("repairMethod") or ""
            return result
        return dict(repair)

    def removed_files(self, link, stage):
        snapshot = {**link['snapshot'], **{section: link['snapshot'].get('removed_attachments', {}).get(section, {})
                                          for section in ('claim', 'survey', 'repair')}}
        return self._remote({**link, 'snapshot': snapshot}, stage)

    def draft(self, case_id, stage):
        definition = self._stage(stage)
        link = self.link(case_id)
        row = self.db.one("SELECT * FROM platform_drafts WHERE case_id=? AND stage=?", (case_id, stage))
        if row:
            return {"case_id": case_id, "stage": stage, "values": json.loads(row["values_json"]),
                    "files": json.loads(row["files_json"]), "revision": row["revision"],
                    "target_report_id": row["target_report_id"], "updated_at": row["updated_at"]}
        remote = self._remote(link, stage)
        values = {f["key"]: _text(remote[f["key"]]) for f in definition["fields"]
                  if f["kind"] not in ("files", "readonly") and f["key"] in remote and not _empty(remote[f["key"]])}
        if stage == "repair":
            # User preference for new drafts; never replace a saved draft or
            # the crew count already returned by the official record.
            values.setdefault("repairCrewNumber", "1")
        candidates = self._photos(case_id)
        selections = {f['key']: [p['id'] for p in candidates if (p['stage'] == f['suggest'][0] or (f['suggest'][0] == 'work' and p.get('shared_checkin_visit_id') and not p.get('reuse_copy_id'))) and p['role'] == f['suggest'][1]
                                 and p['recognition_state'] != 'conflict' and ('.' + p['original_name'].rsplit('.', 1)[-1].lower()) in f['extensions']]
                      for f in definition['fields'] if f.get('suggest') and not f.get('readonly')}
        return {"case_id": case_id, "stage": stage, "values": values, "files": selections, "revision": 0,
                "target_report_id": link["report_receiving_id"], "updated_at": None}

    def _normalize(self, case_id, stage, data):
        definition = self._stage(stage)
        self.domain.case(case_id)
        values, selections = data.get("values", {}), data.get("files", {})
        if not isinstance(values, dict) or not isinstance(selections, dict):
            raise DomainError("官网草稿字段格式无效")
        editable = {f["key"]: f for f in definition["fields"] if f["kind"] not in ("files", "readonly")}
        file_fields = {f["key"]: f for f in definition["fields"] if f["kind"] == "files" and not f.get("readonly")}
        if set(values) - editable.keys() or set(selections) - file_fields.keys():
            raise DomainError("草稿包含不属于此环节的字段，或试图修改官网带入字段")
        clean_values, clean_files = {}, {}
        for key, value in values.items():
            if value is None:
                value = ""
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise DomainError("字段值应为文字或数字")
            value = str(value).strip()
            if len(value) > (10000 if editable[key]["kind"] == "textarea" else 1000):
                raise DomainError(editable[key]["label"] + "内容过长")
            clean_values[key] = value
        total = 0
        for key, ids in selections.items():
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise DomainError("请选择本地案件中的文件")
            ids = list(dict.fromkeys(ids))
            total += len(ids)
            if total > 500:
                raise DomainError("每份官网草稿最多选择500个文件")
            for photo_id in ids:
                photo = self.db.one("SELECT case_id FROM photos WHERE id=?", (photo_id,))
                if not photo or photo["case_id"] != case_id:
                    raise DomainError("所选文件不属于当前案件，未保存或生成资料", 409)
            clean_files[key] = ids
        return clean_values, clean_files

    def field_sources(self, case_id, stage):
        result = {}
        for row in self.db.all('SELECT * FROM platform_draft_field_sources WHERE case_id=? AND stage=?', (case_id, stage)):
            result[row['field']] = {**row, 'photo_ids': json.loads(row['photo_ids']), 'needs_review': bool(row['needs_review'])}
        return result

    def manual_file_selections(self, case_id, stage):
        """Return current, field-scoped choices without changing photo labels."""
        return {(r['field'], r['photo_id']) for r in self.db.all(
            'SELECT m.field,m.photo_id FROM platform_manual_file_selections m '
            'JOIN photos p ON p.id=m.photo_id AND p.case_id=m.case_id '
            'AND p.revision=m.photo_revision AND p.blob_hash=m.blob_hash '
            'JOIN platform_drafts d ON d.case_id=m.case_id AND d.stage=m.stage '
            'AND d.target_report_id=m.target_report_id '
            'JOIN platform_links l ON l.case_id=m.case_id AND l.report_receiving_id=m.target_report_id '
            'WHERE m.case_id=? AND m.stage=? AND m.target_report_id<>\'\'',
            (case_id, stage))}

    def prefill_draft(self, case_id, stage, allowed_photo_ids):
        """Refresh automatic values from current evidence, retaining manual input."""
        with self.material_lock:
            plan = self.preview(case_id, stage)
            if stage not in ('survey', 'repair') or not plan['access']['editable']:
                return {'plan': plan, 'changed': False}
            case, link, photos, draft = plan['case'], plan['link'], plan['photos'], plan['draft']
            fields = {f['key']: f for f in plan['fields'] if f['kind'] not in ('files', 'readonly')}
            sources = self.field_sources(case_id, stage)
            values = deepcopy(draft['values'])
            files = deepcopy(draft['files']) if draft['revision'] else {}
            owned = {key: source for key, source in sources.items() if key in fields and source['origin'] == 'automatic'
                     and canonical_value(fields[key], values.get(key)) == canonical_value(fields[key], source['value'])}
            for key in owned:
                values.pop(key, None)
            evidence_link = without_automatic_remote(link, stage, owned, fields)
            suggestions, notes = stage_suggestions(stage, case, evidence_link,
                [p for p in photos if p['id'] in allowed_photo_ids], files, values, self.dictionaries()['items'], self.prefill_defaults())
            signature = input_signature(case, photos, self.prefill_defaults())
            automatic_sources = {}
            for key, field in fields.items():
                # Existing draft keys, including explicit blanks, are protected.
                if key in values or sources.get(key, {}).get('origin') == 'manual' or not visible(field, values):
                    continue
                suggestion = suggestions.get(key)
                if not suggestion:
                    continue
                values[key] = canonical_value(field, suggestion['value'])
                automatic_sources[key] = {**suggestion, 'value': values[key], 'input_signature': signature}
            old_sources = {key: {k: source[k] for k in ('value', 'source', 'photo_ids', 'needs_review', 'input_signature')}
                           for key, source in sources.items() if source['origin'] == 'automatic'}
            changed = values != draft['values'] or automatic_sources != old_sources
            if changed:
                plan = self.save_draft(case_id, stage, {'values': values, 'files': files, 'revision': draft['revision'], 'link_revision': link['revision']},
                    actor='automatic_prefill', automatic_sources=automatic_sources,
                    photo_set_guard=sorted((p['id'], p['revision'], p['blob_hash']) for p in photos),
                    case_guard={k: case[k] for k in ('revision', 'project_id', 'platform_report_code', 'short_code', 'workflow_stage')})
            # Explanations use the same allowed evidence as the automatic values.
            plan['field_suggestion_notes'] = notes
            return {'plan': plan, 'changed': changed}

    def save_draft(self, case_id, stage, data, *, actor='local', photo_guards=None, case_guard=None, automatic_sources=None, photo_set_guard=None):
        with self.db.tx() as con:
            # Check under the write lock so a simultaneous stage advance cannot
            # leave a newly edited draft in a completed stage.
            self.require_editable(case_id, stage, con)
            if case_guard:
                current_case = con.execute('SELECT * FROM cases WHERE id=?', (case_id,)).fetchone()
                if any(current_case[k] != value for k, value in case_guard.items()):
                    raise DomainError('案件编号、项目或阶段已变化，未采用旧选材', 409)
            if photo_set_guard is not None:
                actual = sorted((p['id'], p['revision'], p['blob_hash']) for p in con.execute('SELECT id,revision,blob_hash FROM photos WHERE case_id=?', (case_id,)))
                if actual != photo_set_guard:
                    raise DomainError('自动填写时照片发生变化，等待重新核对', 409)
            values, selections = self._normalize(case_id, stage, data)
            for photo in photo_guards or []:
                current = con.execute('SELECT case_id,revision,blob_hash FROM photos WHERE id=?', (photo['id'],)).fetchone()
                if not current or current['case_id'] != case_id or current['revision'] != photo['revision'] or current['blob_hash'] != photo['blob_hash']:
                    raise DomainError('照片归属、用途或版本已变化，未采用旧选材', 409)
            current_link = con.execute("SELECT * FROM platform_links WHERE case_id=?", (case_id,)).fetchone()
            if data.get("link_revision") != (current_link["revision"] if current_link else 0):
                raise DomainError("官网关联已变化，请刷新并核对后保存草稿", 409)
            before = con.execute("SELECT * FROM platform_drafts WHERE case_id=? AND stage=?", (case_id, stage)).fetchone()
            revision = before["revision"] if before else 0
            if data.get("revision") != revision:
                raise DomainError("本地草稿已被修改，请刷新后重试", 409)
            previous_files = json.loads(before['files_json']) if before else self.draft(case_id, stage)['files']
            if actor == 'local':
                if stage == 'repair':
                    from .checkin_copies import CHECKIN_FIELDS, copy_for_work
                    remote = self._remote(self.link(case_id), stage)
                    repair_date = values.get('approachStartWorkDate') or remote.get('approachStartWorkDate')
                    for field, role in CHECKIN_FIELDS.items():
                        copied = []
                        for photo_id in selections.get(field, []):
                            source = con.execute('SELECT stage,photo_group,role FROM photos WHERE id=?', (photo_id,)).fetchone()
                            if source['stage'] == 'survey' and source['photo_group'] == 'survey_checkin' and source['role'] == role:
                                photo_id, _ = copy_for_work(con, photo_id, case_id, repair_date=repair_date)
                            copied.append(photo_id)
                        if field in selections:
                            selections[field] = list(dict.fromkeys(copied))
                for field, ids in previous_files.items():
                    for photo_id in set(ids) - set(selections.get(field, [])):
                        con.execute('INSERT OR IGNORE INTO platform_draft_exclusions VALUES(?,?,?,?,?)', (case_id, stage, field, photo_id, now()))
                for field, ids in selections.items():
                    for photo_id in ids:
                        con.execute('DELETE FROM platform_draft_exclusions WHERE case_id=? AND stage=? AND field=? AND photo_id=?', (case_id, stage, field, photo_id))
                        con.execute('DELETE FROM platform_attachment_blocks WHERE case_id=? AND stage=? AND field=? '
                                    'AND blob_hash=(SELECT blob_hash FROM photos WHERE id=?)', (case_id, stage, field, photo_id))
            else:
                for field, ids in selections.items():
                    for photo_id in set(ids) - set(previous_files.get(field, []) if before else []):
                        if con.execute('SELECT 1 FROM platform_draft_exclusions WHERE case_id=? AND stage=? AND field=? AND photo_id=?', (case_id, stage, field, photo_id)).fetchone():
                            raise DomainError('此附件已被人工排除，请刷新后核对', 409)
                        if con.execute('SELECT 1 FROM platform_attachment_blocks WHERE case_id=? AND stage=? AND field=? '
                                       'AND blob_hash=(SELECT blob_hash FROM photos WHERE id=?)', (case_id, stage, field, photo_id)).fetchone():
                            raise DomainError('此照片已被人工撤回，不能自动重新加入', 409)
            con.execute("""INSERT INTO platform_drafts VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(case_id,stage) DO UPDATE SET values_json=excluded.values_json,files_json=excluded.files_json,
                target_report_id=excluded.target_report_id,revision=excluded.revision,updated_at=excluded.updated_at""",
                (case_id, stage, dump(values), dump(selections), current_link["report_receiving_id"] if current_link else "", revision + 1, now()))
            if actor == 'local':
                for field, ids in selections.items():
                    for photo_id in ids:
                        photo = con.execute('SELECT revision,blob_hash FROM photos WHERE id=?', (photo_id,)).fetchone()
                        con.execute('INSERT INTO platform_manual_file_selections VALUES(?,?,?,?,?,?,?,?) '
                                    'ON CONFLICT(case_id,stage,field,photo_id) DO UPDATE SET '
                                    'photo_revision=excluded.photo_revision,blob_hash=excluded.blob_hash,'
                                    'target_report_id=excluded.target_report_id,created_at=excluded.created_at',
                                    (case_id, stage, field, photo_id, photo['revision'], photo['blob_hash'],
                                     current_link['report_receiving_id'] if current_link else '', now()))
            if automatic_sources is not None:
                con.execute("DELETE FROM platform_draft_field_sources WHERE case_id=? AND stage=? AND origin='automatic'", (case_id, stage))
                for key, source in automatic_sources.items():
                    if key not in values or values[key] != source['value']:
                        raise DomainError('自动填写值与来源不一致，未保存草稿', 409)
                    con.execute('INSERT INTO platform_draft_field_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (case_id, stage, key, 'automatic', values[key], source['source'], dump(source['photo_ids']), int(source['needs_review']), source['input_signature'], now()))
            elif actor == 'local':
                # Saving the form explicitly preserves all current values and clears.
                previous_values = json.loads(before['values_json']) if before else {}
                for key in set(values) | set(previous_values):
                    con.execute("INSERT INTO platform_draft_field_sources VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(case_id,stage,field) DO UPDATE SET origin='manual',value=excluded.value,source=excluded.source,photo_ids='[]',needs_review=0,input_signature='',updated_at=excluded.updated_at",
                        (case_id, stage, key, 'manual', values.get(key, ''), '人工保存的字段', '[]', 0, '', now()))
            audit(con, "platform_draft", case_id, "local_save", after={"stage": stage, "revision": revision + 1,
                "values": values, "files": selections, "remote_written": False,
                **({'automatic_sources': automatic_sources} if automatic_sources is not None else {})}, actor=actor)
        return self.preview(case_id, stage)

    def _photos(self, case_id):
        page, photos = 1, []
        while True:
            result = self.domain.photos(case_id=case_id, page=page, page_size=500)
            photos.extend(result["items"])
            if len(photos) >= result["total"]:
                return photos
            if len(photos) >= 5000:
                raise DomainError("此案件资料超过5000项，请先按业务拆分案件后准备官网资料")
            page += 1

    def preview(self, case_id, stage, data=None):
        definition = self._stage(stage)
        case = self.domain.case(case_id)
        link = self.link(case_id)
        access = stage_access(case, stage)
        saved = self.draft(case_id, stage)
        draft = data if data is not None else saved
        # A file might have been moved to another case after the draft was saved.
        # Keep the draft visible but report that stale reference instead of dropping it.
        if data is not None and access['editable']:
            values, selections = self._normalize(case_id, stage, draft)
        else:
            values, selections = deepcopy(saved["values"]), deepcopy(saved["files"])
        remote = self._remote(link, stage)
        if not access['editable']:
            # Completed official forms display the read-back record. Keep any
            # older local draft intact, but never overlay it on that record.
            if link['verified_at']:
                values = {f['key']: _text(remote.get(f['key'])) for f in definition['fields'] if f['kind'] not in ('files', 'readonly')}
                for key in ('repairPersonName', 'repairPersonPhone', *(f['name_key'] for f in definition['fields'] if f.get('name_key'))):
                    if key in remote:
                        values[key] = _text(remote[key])
                selections = {}
            else:
                values, selections = deepcopy(saved['values']), deepcopy(saved['files'])
                if not saved['revision']:
                    selections = {}
        for f in definition["fields"]:
            if f["kind"] == "readonly":
                values[f["key"]] = _text(remote.get(f["key"]))
        if stage == "repair":
            values["repairMethod"] = _text(remote.get("repairMethod"))
            if not values["repairMethod"]:
                local_survey = self.draft(case_id, "survey")
                values["repairMethod"] = local_survey["values"].get("repairMethod", "")
        dictionaries = self.dictionaries()["items"]
        issues = []

        def issue(key, message, severity="error", code="invalid"):
            issues.append({"field": key, "message": message, "severity": severity, "code": code})

        if not link["report_receiving_id"]:
            issue("binding", "尚未关联官网案件，报修编号和保司报案编号不能代替官网记录 ID。")
        elif not link["verified_at"] and not link['snapshot'].get('list_verified_at'):
            issue("binding", "官网编号尚未在线核对，请连接官网查询并关联真实案件。")
        if saved["revision"] and saved["target_report_id"] != link["report_receiving_id"]:
            issue("binding", "官网关联已变更，请核对本环节内容后重新保存本地草稿。")
        if link["verified_at"]:
            issue("snapshot", "官网信息读取于 " + link["verified_at"] + "；办理前需刷新节点和已有资料。", "warning")
        elif link['snapshot'].get('list_verified_at'):
            issue('snapshot', '已核对官网列表状态；详情和附件尚未读取，可按需手动刷新。', 'warning')
        row = link["snapshot"].get("row", {})
        if case['workflow_stage'] not in ('historical_unverified', 'insurer_intake') and unsubmitted_report(row, link['snapshot'].get('scope')):
            issue("node", "官网报案仍为保存状态，请先完成官网报案提交后再办理受理。")
        if row.get("interceptFlag") == "1":
            issue("node", "官网案件已拦截，请先在官网核实。")
        if stage in ("repair", "acceptance") and row.get("repairMethod") in ("3", "4"):
            issue("node", "此案件处于零注拒流程，不能按普通维修成果准备。")
        if stage == "repair" and values.get("repairMethod") not in ("1", "2"):
            issue("repairMethod", "维修方式尚未核实，补充材料栏是否适用仍待确认。", code="required" if not values.get('repairMethod') else "invalid")
        if stage == "acceptance" and not row.get("repairAcceptanceId"):
            issue("repairAcceptanceId", "缺少官网修复成果记录 ID，需先核对前一环节记录。")

        for f in definition["fields"]:
            key, kind = f["key"], f["kind"]
            if not access['editable'] or not visible(f, values) or kind in ("files", "readonly"):
                continue
            value = values.get(key, "")
            if _empty(value):
                if f["required"]:
                    issue(key, "缺少" + f["label"], code="required")
                continue
            if kind in ("select", "dictionary"):
                options = f.get("options", dictionaries.get(f.get("dictionary", ""), []))
                option = next((v for v in options if v["code"] == value), None)
                if not option:
                    issue(key, f["label"] + "不是当前核对到的官网选项，请重新选择。")
                elif f.get("name_key"):
                    values[f["name_key"]] = option["name"]
            elif kind in ("date", "datetime"):
                try:
                    parsed = datetime.fromisoformat(value.replace("T", " "))
                    if parsed.tzinfo or (kind == "date" and len(value) != 10) or (kind == "datetime" and len(value) < 16):
                        raise ValueError()
                    values[key] = parsed.strftime("%Y-%m-%d" if kind == "date" else "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    issue(key, f["label"] + "格式无效，请填写实际北京时间。")
            elif kind == "integer":
                if not value.isdigit() or not 1 <= int(value) <= 10000:
                    issue(key, f["label"] + "应为1至10000之间的整数。")
                else:
                    values[key] = int(value)
            elif kind == "personnel":
                ids = list(dict.fromkeys(i.strip() for i in value.split(",") if i.strip()))
                people = link["snapshot"].get("personnel", [])
                matched = [next((p for p in people if p["userId"] == i), None) for i in ids]
                if not ids or any(p is None for p in matched):
                    issue(key, "维修人员需从此官网案件对应的人员列表选择，不能只填姓名。")
                else:
                    values[key] = ",".join(ids)
                    values["repairPersonName"] = ",".join(p["name"] for p in matched)
                    values["repairPersonPhone"] = ",".join(p["phone"] for p in matched)

        for start_key, end_key in (("modifiedSurveyTime", "modifiedSurveyTimeEnd"), ("approachStartWorkDate", "finishWorkDate")):
            if not access['editable']:
                break
            relevant = next((f for f in definition["fields"] if f["key"] == start_key), None)
            if not relevant or not visible(relevant, values):
                continue
            try:
                if values.get(start_key) and values.get(end_key):
                    start, end = [datetime.fromisoformat(values[k]) for k in (start_key, end_key)]
                    if end < start:
                        issue(end_key, "结束时间不能早于开始时间。")
                    elif stage == "repair":
                        values["timeLimit"] = (end.date() - start.date()).days + 1
            except (ValueError, TypeError):
                pass

        photos = self._photos(case_id)
        photo_map = {p["id"]: p for p in photos}
        removed_files = self.removed_files(link, stage)
        field_files, assigned = [], set()
        for f in definition["fields"]:
            if f["kind"] != "files" or not visible(f, values):
                continue
            key = f["key"]
            existing = remote.get(key) or []
            if not isinstance(existing, list):
                existing = []
                issue(key, f["label"] + "的官网已有文件格式异常，请刷新核对。")
            # Blank optional rows on the website are placeholders, not uploaded files.
            existing = [r for r in existing if active_attachment(r)]
            selected = []
            for photo_id in selections.get(key, []):
                photo = photo_map.get(photo_id)
                if not photo:
                    issue(key, "草稿所选文件已移出此案件，请重新选择。")
                    continue
                assigned.add(photo_id)
                blob = self.db.one("SELECT * FROM blobs WHERE hash=?", (photo["blob_hash"],))
                entry = {**photo, "extension": blob["extension"]}
                selected.append(entry)
                if blob["extension"] not in f["extensions"]:
                    issue(key, f["label"] + "不接受“" + photo["original_name"] + "”的格式。")
                limit = f.get("max_bytes_exclusive")
                if limit and blob["size_bytes"] >= limit:
                    issue(key, photo["original_name"] + "超过官网此栏网页控件的文件限制（需小于20MB）。")
                if blob["extension"] == ".pdf":
                    path = self.storage.resolve(blob["stored_path"])
                    header = b""
                    if path.is_file():
                        with path.open("rb") as source:
                            header = source.read(5)
                    if header != b"%PDF-":
                        issue(key, photo["original_name"] + "无法识别为 PDF，请检查原文件。")
            if f["required"] and not existing and not selected:
                issue(key, "缺少" + f["label"], code="required")
            suggest = f.get("suggest")
            suggested_ids = [p["id"] for p in photos if suggest and (p["stage"] == suggest[0] or (suggest[0] == 'work' and p.get('shared_checkin_visit_id') and not p.get('reuse_copy_id'))) and p["role"] == suggest[1]
                             and self.db.one("SELECT extension FROM blobs WHERE hash=?", (p["blob_hash"],))["extension"] in f["extensions"]]
            field_files.append({**f, "readonly": bool(f.get('readonly')) or not access['editable'], "selected": selected, "existing": existing,
                                "removed": [r for r in removed_files.get(key, []) if str(r['id']) not in {str(v['id']) for v in existing}],
                                "suggested_ids": suggested_ids if access['editable'] else [], "count": len(existing) + len(selected)})
        if any(p["stage"] == "work" and p["role"] == "construction" for p in photos) and stage == "repair":
            issue("construction", "施工过程照片已保留在保司资料中；验收阶段可补充相应 PDF。", "warning")
        active_keys = {f["key"] for f in field_files}
        if any(ids and key not in active_keys for key, ids in selections.items()):
            issue("hidden_files", "有已选文件因当前维修方式不适用，不计入本次按栏位资料。", "warning")

        business = {}
        for f in definition["fields"]:
            if f["kind"] != "files" and visible(f, values) and not _empty(values.get(f["key"])):
                business[f["key"]] = values[f["key"]]
                if f.get("name_key") and values.get(f["name_key"]):
                    business[f["name_key"]] = values[f["name_key"]]
        if stage == "survey" and visible(next(f for f in definition["fields"] if f["key"] == "repairUserId"), values):
            for key in ("repairPersonName", "repairPersonPhone"):
                if values.get(key):
                    business[key] = values[key]
        if stage == "acceptance":
            business["repairAcceptanceId"] = row.get("repairAcceptanceId") or ""
        else:
            business["reportReceivingId"] = link["report_receiving_id"]
            record_key = {"survey": "siteSurveyId", "repair": "repairAcceptanceId"}.get(stage)
            if record_key and row.get(record_key):
                business[record_key] = row[record_key]
            if stage == "survey":
                context = {**link["snapshot"].get("claim", {}), **link["snapshot"].get("survey", {})}
                for key in ("communityInfoId", "engineeringModelingParentId", "repairUnitId"):
                    if context.get(key):
                        business[key] = context[key]
        sources = self.field_sources(case_id, stage)
        evidence_signature = input_signature(case, photos, self.prefill_defaults())
        fields_by_key = {f['key']: f for f in definition['fields']}
        for key, source in sources.items():
            source['current'] = key in fields_by_key and canonical_value(fields_by_key[key], values.get(key)) == canonical_value(fields_by_key[key], source['value'])
            source['stale'] = source['origin'] == 'automatic' and source['input_signature'] != evidence_signature
            if access['editable'] and source['current'] and source['stale']:
                issue(key, fields_by_key[key]['label'] + '的自动填写依据已变化，等待重新填写或人工核对后保存。')
        errors = sum(i["severity"] == "error" for i in issues)
        suggestions, suggestion_notes = stage_suggestions(stage, case, link, photos, selections, values, dictionaries, self.prefill_defaults()) if access['editable'] else ({}, {})
        return {"case": case, "stage": stage, "label": definition["label"], "guide_page": definition["guide_page"],
                "contract_version": VERSION, "mode": self.client.status()['mode'], "remote_written": False,
                "link": link, "draft": saved, "values": values, "fields": definition["fields"], "access": access,
                "view_source": 'official' if not access['editable'] and link['verified_at'] else 'local',
                "remote_values": {f["key"]: _text(remote[f["key"]]) for f in definition["fields"] if f["kind"] not in ("readonly", "files") and not _empty(remote.get(f["key"]))},
                "file_fields": field_files, "photos": photos, "photo_total": len(photos), "unassigned_count": len(photos) - len(assigned),
                "issues": issues, "error_count": errors, "warning_count": len(issues) - errors,
                "business_fields": business, "endpoint": definition["endpoint"], "actions": definition["actions"] if access['editable'] else [],
                "personnel": link["snapshot"].get("personnel", []),
                "field_suggestions": suggestions, "field_suggestion_notes": suggestion_notes,
                "field_sources": sources,
                "attachment_removals": self.removals.pending(case_id, stage),
                "prepared_for_review": errors == 0 and access['editable'], "write_enabled": self.client.write_enabled}

    @staticmethod
    def validation_errors(plan, *, allow_incomplete=False):
        return [item for item in plan['issues'] if item['severity'] == 'error'
                and not (allow_incomplete and item.get('code') == 'required')]

    def build_payload(self, plan, action, uploaded_records, *, allow_incomplete=False):
        """Assemble parameters after checking the current stage. Never sends a request.

        `uploaded_records` must contain real file records; local hashes/IDs are never
        substituted for the platform's IDs. Existing remote records are retained.
        """
        self.require_editable(plan['case']['id'], plan['stage'])
        if allow_incomplete and (action != 'save' or plan['stage'] not in ('survey', 'repair')):
            raise DomainError('不完整资料只能保存查勘或修复成果草稿，不能正式提交', 403)
        if plan["contract_version"] != VERSION or self.validation_errors(plan, allow_incomplete=allow_incomplete):
            raise DomainError("资料仍有待补或待核对项，不能组装完整接口参数")
        if action not in plan["actions"]:
            raise DomainError("此环节没有对应的官网保存或提交动作")
        payload = deepcopy(plan["business_fields"])
        if plan["stage"] != "acceptance":
            payload["state"] = 0 if action == "save" else 1
        for f in plan["file_fields"]:
            records = deepcopy(f["existing"])
            for photo in f["selected"]:
                record = uploaded_records.get(photo["id"])
                if not isinstance(record, dict) or not record.get("id") or not record.get("fileUrl"):
                    raise DomainError("本地文件尚未获得官网上传记录，不能用本地编号代替")
                record = deepcopy(record)
                if f.get("custom_type_code"):
                    record.update(customTypeCode=f["custom_type_code"], customType=f["label"], orderId=len(records), noRequire=not f["required"])
                if str(record["id"]) not in {str(r["id"]) for r in records}:
                    records.append(record)
            payload[f["key"]] = records
        return payload

    def package(self, case_id, stage, expected_revision):
        self.require_editable(case_id, stage)
        plan = self.preview(case_id, stage)
        self._normalize(case_id, stage, plan["draft"])
        if expected_revision != plan["draft"]["revision"]:
            raise DomainError("草稿已变化，请刷新后重新生成", 409)
        package_id = uid()
        stamp = now()
        manifest = {"schema": 1, "package_id": package_id, "created_at": stamp, "contract_version": VERSION,
                    "purpose": "官网字段资料准备，未上传、未保存到官网、未提交官网流程",
                    "case": plan["case"], "stage": stage, "stage_label": plan["label"],
                    "link": {k: plan["link"][k] for k in ("report_receiving_id", "platform_report_code", "insurer_report_number", "revision", "verified_at")},
                    "draft_revision": expected_revision, "business_fields": plan["business_fields"],
                    "issues": plan["issues"], "files": [], "remote_written": False}
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for f in plan["file_fields"]:
                for p in f["selected"]:
                    # Recheck membership and bytes at export time, not just at selection.
                    current = self.db.one("SELECT case_id,blob_hash FROM photos WHERE id=?", (p["id"],))
                    if not current or current["case_id"] != case_id or current["blob_hash"] != p["blob_hash"]:
                        raise DomainError("文件归属已变化，请重新核对资料", 409)
                    blob = self.db.one("SELECT * FROM blobs WHERE hash=?", (p["blob_hash"],))
                    source = self.storage.resolve(blob["stored_path"])
                    if not self.storage.verify(blob):
                        raise DomainError("归档原件缺失或内容校验失败，未生成资料包")
                    path = f"{safe_name(f['label'])}/{safe_name(Path(p['original_name']).stem)}__{p['id'][:12]}{blob['extension']}"
                    archive.write(source, path)
                    manifest["files"].append({"field": f["key"], "label": f["label"], "photo_id": p["id"],
                        "sha256": p["blob_hash"], "path": path, "status": "local_only"})
            checklist = ["官网资料准备清单（仅本地）", plan["case"]["project_name"] + " · " + plan["case"]["title"],
                         "环节：" + plan["label"], "本包未上传到官网，也不是平台提交回执。", "",
                         *[f"{f['label']}：本地已选 {len(f['selected'])} 项，官网快照已有 {len(f['existing'])} 项" for f in plan["file_fields"]],
                         "", "待核对：", *[i["message"] for i in plan["issues"]]]
            archive.writestr("字段清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("核对说明.txt", "\n".join(checklist))
        content = out.getvalue()
        relative = f"exports/{package_id}/platform-materials.zip"
        self.storage._atomic(self.storage.resolve(relative), content)
        with self.db.tx() as con:
            # Do not publish an artifact as current if a concurrent save changed it.
            latest = con.execute("SELECT revision FROM platform_drafts WHERE case_id=? AND stage=?", (case_id, stage)).fetchone()
            latest_revision = latest["revision"] if latest else 0
            current_link = con.execute("SELECT revision FROM platform_links WHERE case_id=?", (case_id,)).fetchone()
            if latest_revision != expected_revision or (current_link["revision"] if current_link else 0) != plan["link"]["revision"]:
                self.storage.resolve(relative).unlink(missing_ok=True)
                raise DomainError("资料准备期间草稿或官网关联已变化，请重新生成", 409)
            con.execute("INSERT INTO platform_packages VALUES(?,?,?,?,?,?,?)", (package_id, case_id, stage, dump(manifest),
                        relative, hashlib.sha256(content).hexdigest(), stamp))
            audit(con, "platform_package", package_id, "local_prepare", after={"case_id": case_id, "stage": stage, "file_count": len(manifest["files"]), "remote_written": False})
        return {"id": package_id, "download_url": f"/api/platform/packages/{package_id}/download", "file_count": len(manifest["files"]), "error_count": plan["error_count"]}

    def packages(self, case_id):
        self.domain.case(case_id)
        rows = self.db.all("SELECT id,stage,snapshot,created_at FROM platform_packages WHERE case_id=? ORDER BY created_at DESC", (case_id,))
        for row in rows:
            snapshot = json.loads(row.pop("snapshot"))
            row.update(label=STAGES[row["stage"]]["label"], file_count=len(snapshot["files"]),
                       error_count=sum(i["severity"] == "error" for i in snapshot["issues"]),
                       download_url=f"/api/platform/packages/{row['id']}/download", state="prepared_local")
        return rows
