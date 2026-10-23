from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from .db import Database, audit, dump, now, rowdict, uid
from .case_codes import issue_code, normalize_code, sync_project_codes, unique_prefix
from .identity import current, project_filter, require_admin, require_project

STAGES = {"unknown": "待分类", "survey": "查勘", "work": "施工", "acceptance": "验收照片"}
ROLES = {"unknown": "待分类", "building_checkin": "楼栋打卡", "door_checkin": "入户门打卡", "problem": "问题照片", "construction": "施工过程", "finished": "完工效果", "other": "其他资料"}
STATUSES = {"pending": "待处理", "survey": "查勘中", "working": "施工中", "completed": "已完工", "accepted": "已验收", "closed": "已结案"}
PHOTO_GROUPS = {"survey_checkin": "查勘打卡", "survey_problem": "查勘问题", "work_checkin": "维修打卡", "construction": "施工", "completion": "完工验收"}
WORKFLOW_STAGES = {"pending_intake": "待维保受理", "insurer_intake": "待保司处理", "insurer_review": "待保司复核", "historical_unverified": "历史待核实", "survey": "查勘中", "repair": "施工中", "acceptance": "待验收", "settlement": "待结算", "closed": "已结束", "direct_payment": "直接赔付", "noncoverage": "非维修流程"}
GROUP_FIELDS = {"survey_checkin": ("survey", "other"), "survey_problem": ("survey", "problem"), "work_checkin": ("work", "other"), "construction": ("work", "construction"), "completion": ("acceptance", "finished")}


def photo_group(stage, role):
    if role == "finished":
        return "completion"
    if role in ("building_checkin", "door_checkin"):
        return {"survey": "survey_checkin", "work": "work_checkin"}.get(stage, "unknown")
    return {("survey", "problem"): "survey_problem", ("work", "construction"): "construction"}.get((stage, role), "unknown")
DEFAULT_REQUIREMENTS = [
    {"stage": "survey", "role": "building_checkin", "minimum": 1, "per_visit": True},
    {"stage": "survey", "role": "door_checkin", "minimum": 1, "per_visit": True},
    {"stage": "survey", "role": "problem", "minimum": 1, "per_visit": False},
    {"stage": "work", "role": "building_checkin", "minimum": 1, "per_visit": True},
    {"stage": "work", "role": "door_checkin", "minimum": 1, "per_visit": True},
    {"stage": "work", "role": "construction", "minimum": 1, "per_visit": False},
    {"stage": "acceptance", "role": "finished", "minimum": 1, "per_visit": False},
]
CASE_FIELDS = {"project_id", "report_number", "platform_report_code", "policy_number", "short_code", "responsible", "insurer_template_id", "title", "building", "unit", "floor", "room", "location_type", "location_detail", "description", "insurer", "contact_name", "contact_phone", "repair_parts", "report_at", "submitted_at", "quoted_at", "accepted_at", "status"}
PHOTO_FIELDS = {"case_id", "project_id", "visit_id", "stage", "role", "photo_group", "part", "process", "watermark_time", "notes"}
CASE_PLATFORM_COLUMNS = """,(SELECT json_group_array(scope) FROM platform_list_entries e WHERE e.case_id=c.id) AS platform_lists,
    (SELECT json_group_array(code) FROM case_code_aliases a WHERE a.case_id=c.id AND a.code<>c.short_code) AS short_code_aliases,
    EXISTS(SELECT 1 FROM platform_links l WHERE l.case_id=c.id AND l.verified_at IS NOT NULL) AS platform_linked"""
PHOTO_SELECT = """SELECT p.*, b.media_type,b.mime_type,b.size_bytes,b.width,b.height,b.extension,b.thumbnail_path,
    c.title AS case_title,c.report_number,c.platform_report_code,c.short_code,c.building,c.unit,c.floor,c.room,c.location_type,c.location_detail,c.insurer,
    pr.name AS project_name,(SELECT display_name FROM idi_users u WHERE u.id=p.uploaded_by) AS uploaded_by_name,
    (SELECT original_name FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.source_type='xhey_camera' DESC,s.received_at,s.rowid LIMIT 1) AS original_name,
    (SELECT sender FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.source_type='xhey_camera' DESC,s.received_at,s.rowid LIMIT 1) AS sender,
    (SELECT group_name FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.source_type='xhey_camera' DESC,s.received_at,s.rowid LIMIT 1) AS group_name,
    (SELECT message_time FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.source_type='xhey_camera' DESC,s.received_at,s.rowid LIMIT 1) AS message_time,
    (SELECT source_type FROM photo_sources s WHERE s.photo_id=p.id ORDER BY s.source_type='xhey_camera' DESC,s.received_at,s.rowid LIMIT 1) AS source_type,
    (SELECT COUNT(*) FROM photo_sources s WHERE s.photo_id=p.id) AS source_count,
    EXISTS(SELECT 1 FROM photos source WHERE source.id=p.reuse_source_id) AS reuse_source_exists,
    (SELECT copy.id FROM photos copy WHERE copy.reuse_source_id=p.id AND copy.case_id=p.case_id LIMIT 1) AS reuse_copy_id,
    v.visit_date,v.visit_key,sv.id AS shared_checkin_visit_id,sv.visit_date AS shared_checkin_date,sv.visit_key AS shared_checkin_key
    FROM photos p JOIN blobs b ON b.hash=p.blob_hash LEFT JOIN cases c ON c.id=p.case_id
    LEFT JOIN projects pr ON pr.id=p.project_id LEFT JOIN visits v ON v.id=p.visit_id
    LEFT JOIN visit_shared_checkins sc ON sc.photo_id=p.id LEFT JOIN visits sv ON sv.id=sc.visit_id"""


class DomainError(Exception):
    def __init__(self, message: str, status=400):
        super().__init__(message)
        self.status = status


def case_filters(project_id=None, query=None, status=None):
    scope, values = project_filter('c.project_id')
    where = [scope]
    for col, val in (("c.project_id", project_id), ("c.status", status)):
        if val:
            where.append(col + "=?")
            values.append(val)
    if query:
        where.append("(c.title LIKE ? OR c.report_number LIKE ? OR c.room LIKE ? OR pr.name LIKE ? OR c.building||'-'||c.room LIKE ? OR c.short_code LIKE ? OR c.platform_report_code LIKE ? OR c.policy_number LIKE ? OR EXISTS(SELECT 1 FROM case_code_aliases a WHERE a.case_id=c.id AND a.code LIKE ?))")
        values.extend([f"%{query}%"] * 9)
    return where, values


def decode_case(row):
    if row:
        row["repair_parts"] = json.loads(row["repair_parts"])
        row["platform_lists"] = json.loads(row.get("platform_lists") or '[]')
        row["short_code_aliases"] = json.loads(row.get("short_code_aliases") or '[]')
        row["platform_linked"] = bool(row.get("platform_linked"))
        # Official cases follow the actual repair list, not an inferred stage.
        row["followup_eligible"] = 'repair' in row["platform_lists"] or (
            not row["platform_linked"] and row.get("workflow_source") != 'platform'
            and row.get("workflow_stage") in ('survey', 'repair', 'acceptance', 'settlement', 'closed'))
    return row


def decode_photo(row):
    if row:
        row["confirmed_fields"] = json.loads(row["confirmed_fields"])
        row["reuse_metadata"] = json.loads(row.get("reuse_metadata") or '{}')
        row["thumbnail_url"] = f"/api/photos/{row['id']}/thumbnail?v={row['blob_hash']}" if row.get("thumbnail_path") else None
        row["original_url"] = f"/api/photos/{row['id']}/original?v={row['blob_hash']}"
        row.pop("thumbnail_path", None)
    return row


def clean_date(value, field):
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    try:
        datetime.fromisoformat(text)
    except ValueError as error:
        raise DomainError(f"{field} 日期格式无效") from error
    return text


class Domain:
    def __init__(self, db: Database):
        self.db = db

    def projects(self):
        scope, args = project_filter('pr.id')
        rows = self.db.all("SELECT pr.*,(SELECT COUNT(*) FROM cases c WHERE c.project_id=pr.id) AS case_count,(SELECT COUNT(*) FROM photos p WHERE p.project_id=pr.id) AS photo_count FROM projects pr WHERE " + scope + " ORDER BY pr.name", args)
        for row in rows:
            row["aliases"] = json.loads(row["aliases"])
        return rows

    def save_project(self, data: dict, project_id=None):
        require_admin()
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 120:
            raise DomainError("项目名称不能为空，且不能超过120字")
        aliases = data.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            raise DomainError("项目别名应为文字列表")
        project_id = project_id or uid()
        try:
            with self.db.tx() as con:
                before = rowdict(con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
                template_id = data.get('insurer_template_id', (before or {}).get('insurer_template_id', 'insurer-v2'))
                if not isinstance(template_id, str) or not con.execute('SELECT id FROM templates WHERE id=?', (template_id,)).fetchone():
                    raise DomainError('保司资料要求不存在')
                prefix = normalize_code(data.get("short_prefix") or (before or {}).get("short_prefix") or unique_prefix(con, name, project_id))
                if not re.fullmatch(r"[A-Z0-9]{1,16}", prefix):
                    raise DomainError("项目简称请使用1至16位英文字母或数字")
                con.execute("INSERT INTO projects(id,name,aliases,insurer,created_at,updated_at,short_prefix,insurer_template_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,insurer=excluded.insurer,short_prefix=excluded.short_prefix,insurer_template_id=excluded.insurer_template_id,updated_at=excluded.updated_at", (project_id, name, dump(list(dict.fromkeys(a.strip() for a in aliases if a.strip()))), str(data.get("insurer", "")).strip(), now(), now(), prefix, template_id))
                updated_codes = sync_project_codes(con, project_id, prefix)
                after = rowdict(con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
                audit(con, "project", project_id, "update" if before else "create", before, after)
        except sqlite3.IntegrityError as error:
            raise DomainError("项目名称或简称已经存在", 409) from error
        return {**next(p for p in self.projects() if p["id"] == project_id), 'updated_case_codes': updated_codes}

    def cases(self, project_id=None, query=None, status=None):
        where, values = case_filters(project_id, query, status)
        rows = self.db.all("SELECT c.*,pr.name AS project_name,(SELECT COUNT(*) FROM photos p WHERE p.case_id=c.id) AS photo_count" + CASE_PLATFORM_COLUMNS + " FROM cases c JOIN projects pr ON pr.id=c.project_id" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.updated_at DESC", values)
        return [decode_case(row) for row in rows]

    def case(self, case_id: str):
        row = self.db.one("SELECT c.*,pr.name AS project_name" + CASE_PLATFORM_COLUMNS + " FROM cases c JOIN projects pr ON pr.id=c.project_id WHERE c.id=?", (case_id,))
        if not row:
            raise DomainError("案件不存在", 404)
        require_project(row['project_id'])
        return decode_case(row)

    def _case_values(self, data, existing=None):
        values = {key: data[key] for key in CASE_FIELDS if key in data}
        for key in set(values) - {"repair_parts"}:
            if values[key] is not None:
                values[key] = str(values[key]).strip()
        if existing is None:
            if not values.get("project_id") or not values.get("title"):
                raise DomainError("请填写项目和案件内容")
        if "title" in values and not values["title"]:
            raise DomainError("案件内容不能为空")
        for key in ("report_number", "platform_report_code", "short_code"):
            if key in values:
                values[key] = values[key] or None
        if values.get("short_code"):
            values["short_code"] = normalize_code(values["short_code"])
            if not re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+){3,5}", values["short_code"]):
                raise DomainError("短编号格式应为项目简称-楼栋-房号-序号，例如 BYF-1-201-02")
        if existing and existing.get("short_code") and "short_code" in values and values["short_code"] != existing["short_code"]:
            raise DomainError("短编号由项目简称统一更新，请在系统设置中修改项目简称", 409)
        if "project_id" in values and not self.db.one("SELECT id FROM projects WHERE id=?", (values["project_id"],)):
            raise DomainError("项目不存在")
        if "insurer_template_id" in values and not self.db.one("SELECT id FROM templates WHERE id=?", (values["insurer_template_id"],)):
            raise DomainError("保司资料要求不存在")
        if "status" in values and values["status"] not in STATUSES:
            raise DomainError("案件状态无效")
        if "location_type" in values and values["location_type"] not in {"household", "public"}:
            raise DomainError("位置类型无效")
        for key in ("report_at", "submitted_at", "quoted_at", "accepted_at"):
            if key in values:
                values[key] = clean_date(values[key], key)
        if "repair_parts" in values:
            if not isinstance(values["repair_parts"], list) or any(not isinstance(x, str) for x in values["repair_parts"]):
                raise DomainError("维修部位应为文字列表")
            values["repair_parts"] = dump(list(dict.fromkeys(x.strip() for x in values["repair_parts"] if x.strip())))
        for key, value in values.items():
            if isinstance(value, str) and len(value) > (10000 if key == "description" else 2000):
                raise DomainError(f"字段 {key} 过长")
        return values

    def save_case(self, data: dict, case_id=None):
        existing = self.case(case_id) if case_id else None
        if existing:
            require_project(existing['project_id'], manage=True)
        require_project(data.get('project_id') or (existing or {}).get('project_id'), manage=True)
        values = self._case_values(data, existing)
        case_id = case_id or uid()
        try:
            with self.db.tx() as con:
                before = rowdict(con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
                if before and before.get('short_code') and 'short_code' in values and values['short_code'] != before['short_code']:
                    raise DomainError("短编号已更新，请刷新案件后重试", 409)
                if not (before or {}).get("short_code") and not values.get("short_code"):
                    values["short_code"] = issue_code(con, {**(before or {}), **values})
                if before:
                    if data.get("revision") is not None and data["revision"] != before["revision"]:
                        raise DomainError("案件已被其他操作修改，请刷新后重试", 409)
                    values.update(updated_at=now(), revision=before["revision"] + 1)
                    con.execute("UPDATE cases SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), case_id))
                    if values.get("project_id"):
                        con.execute("UPDATE photos SET project_id=?,updated_at=?,revision=revision+1 WHERE case_id=?", (values["project_id"], now(), case_id))
                else:
                    if 'insurer_template_id' not in values:
                        values['insurer_template_id'] = con.execute('SELECT insurer_template_id FROM projects WHERE id=?', (values['project_id'],)).fetchone()[0]
                    values.update(id=case_id, created_at=now(), updated_at=now())
                    con.execute("INSERT INTO cases(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
                after = rowdict(con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
                audit(con, "case", case_id, "update" if before else "create", before, after)
        except sqlite3.IntegrityError as error:
            raise DomainError("报修编号或短编号（含历史编号）已关联其他案件，请使用已有案件", 409) from error
        return self.case(case_id)

    def photos(self, project_id=None, case_id=None, stage=None, role=None, part=None, query=None,
               review=None, sort="watermark_time", direction="asc", page=1, page_size=30, ids=None, photo_group=None, visit_id=None):
        scope, values = project_filter('p.project_id')
        where = [scope]
        for col, value in (("p.project_id", project_id), ("p.case_id", case_id), ("p.stage", stage), ("p.role", role), ("p.part", part), ("p.photo_group", photo_group)):
            if value:
                if (col, value) in (("p.stage", "work"), ("p.photo_group", "work_checkin")):
                    where.append('(' + col + '=? OR EXISTS(SELECT 1 FROM visit_shared_checkins s WHERE s.photo_id=p.id '
                                 'AND NOT EXISTS(SELECT 1 FROM photos copy WHERE copy.reuse_source_id=p.id)))')
                else:
                    where.append(col + "=?")
                values.append(value)
        if visit_id:
            where.append('(p.visit_id=? OR EXISTS(SELECT 1 FROM visit_shared_checkins s WHERE s.photo_id=p.id AND s.visit_id=? '
                         'AND NOT EXISTS(SELECT 1 FROM photos copy WHERE copy.reuse_source_id=p.id)))')
            values.extend((visit_id, visit_id))
        if review == "unassigned":
            where.append("p.case_id IS NULL")
        elif review == "unknown":
            where.append("p.photo_group='unknown'")
        elif review == "exceptions":
            where.append("(p.case_id IS NULL OR p.photo_group='unknown' OR p.recognition_state IN ('failed','conflict')) AND p.blob_hash IN (SELECT hash FROM blobs WHERE media_type='image')")
        elif review == "pending":
            where.append("p.recognition_state <> 'confirmed'")
        elif review == "failed":
            where.append("p.recognition_state='failed'")
        if query:
            where.append("(c.title LIKE ? OR c.report_number LIKE ? OR pr.name LIKE ? OR p.part LIKE ? OR p.process LIKE ? OR c.room LIKE ? OR c.short_code LIKE ? OR c.platform_report_code LIKE ? OR EXISTS(SELECT 1 FROM photo_sources s WHERE s.photo_id=p.id AND s.original_name LIKE ?) OR EXISTS(SELECT 1 FROM case_code_aliases a WHERE a.case_id=c.id AND a.code LIKE ?))")
            values.extend([f"%{query}%"] * 10)
        if ids is not None:
            if not ids:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
            where.append("p.id IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        condition = " WHERE " + " AND ".join(where) if where else ""
        count_sql = "SELECT COUNT(*) AS count FROM photos p LEFT JOIN cases c ON c.id=p.case_id LEFT JOIN projects pr ON pr.id=p.project_id" + condition
        total = self.db.one(count_sql, values)["count"]
        sort_col = {"watermark_time": "COALESCE(p.watermark_time,p.exif_time,'9999')", "created_at": "p.created_at", "stage": "p.stage", "part": "p.part", "case": "c.title"}.get(sort, "p.created_at")
        direction = "DESC" if direction.lower() == "desc" else "ASC"
        page, page_size = max(1, int(page)), min(500, max(1, int(page_size)))
        unknown_last = "CASE WHEN p.watermark_time IS NULL AND p.exif_time IS NULL THEN 1 ELSE 0 END," if sort == "watermark_time" else ""
        rows = self.db.all(PHOTO_SELECT + condition + f" ORDER BY {unknown_last}{sort_col} {direction},p.id LIMIT ? OFFSET ?", (*values, page_size, (page - 1) * page_size))
        return {"items": [decode_photo(r) for r in rows], "total": total, "page": page, "page_size": page_size}

    def photo(self, photo_id):
        row = self.db.one(PHOTO_SELECT + " WHERE p.id=?", (photo_id,))
        if not row:
            raise DomainError("照片不存在", 404)
        require_project(row['project_id'])
        result = decode_photo(row)
        result["sources"] = self.db.all("SELECT * FROM photo_sources WHERE photo_id=? ORDER BY received_at", (photo_id,))
        for source in result["sources"]:
            source["metadata"] = json.loads(source["metadata"])
        result["recognitions"] = self.db.all("SELECT * FROM recognitions WHERE photo_id=? ORDER BY created_at DESC", (photo_id,))
        for recognition in result["recognitions"]:
            recognition["suggestions"] = json.loads(recognition["suggestions"])
            recognition["candidates"] = json.loads(recognition["candidates"])
        return result

    def update_photo(self, photo_id: str, data: dict, actor="local"):
        values = {key: data[key] for key in PHOTO_FIELDS if key in data}
        if "stage" in values and values["stage"] not in STAGES:
            raise DomainError("照片阶段无效")
        if "role" in values and values["role"] not in ROLES:
            raise DomainError("照片用途无效")
        if "photo_group" in values and values["photo_group"] not in {*PHOTO_GROUPS, "unknown"}:
            raise DomainError("照片分类无效")
        for key in ("part", "process", "notes"):
            if key in values:
                values[key] = str(values[key] or "").strip()[:10000]
        for key in ("case_id", "project_id", "visit_id"):
            if key in values:
                values[key] = values[key] or None
        if values.get("case_id"):
            case = self.case(values["case_id"])
            values["project_id"] = case["project_id"]
        if "watermark_time" in values:
            values["watermark_time"] = clean_date(values["watermark_time"], "水印时间")
        try:
            with self.db.tx() as con:
                before = rowdict(con.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone())
                if not before:
                    raise DomainError("照片不存在", 404)
                require_project(before['project_id'], manage=actor == 'local')
                if 'project_id' in values:
                    require_project(values['project_id'], manage=True)
                if data.get("revision") is not None and before["revision"] != data["revision"]:
                    raise DomainError("照片已被其他操作修改，请刷新后重试", 409)
                if values.get("photo_group") in GROUP_FIELDS:
                    stage, role = GROUP_FIELDS[values["photo_group"]]
                    if values.get("stage", stage) != stage:
                        raise DomainError("照片分类与阶段不一致")
                    values["stage"] = stage
                    if role == "other" and values.get("role", before["role"]) in ("building_checkin", "door_checkin"):
                        role = values.get("role", before["role"])
                    if "role" in values and values["role"] != role:
                        raise DomainError("照片分类与用途不一致")
                    values["role"] = role
                elif values.get('photo_group') == 'unknown':
                    values.update(stage='unknown', role='unknown')
                elif "stage" in values or "role" in values:
                    values["photo_group"] = photo_group(values.get("stage", before["stage"]), values.get("role", before["role"]))
                next_case = values.get("case_id", before["case_id"])
                if "project_id" in values and next_case:
                    case_project = con.execute("SELECT project_id FROM cases WHERE id=?", (next_case,)).fetchone()[0]
                    if values["project_id"] != case_project:
                        raise DomainError("照片项目必须与所属案件一致")
                if any(key in values for key in ("case_id", "stage", "watermark_time")) and "visit_id" not in values:
                    values["visit_id"] = None
                if values.get("visit_id"):
                    from .visits import allows_photo, photo_date
                    visit = con.execute("SELECT * FROM visits WHERE id=?", (values["visit_id"],)).fetchone()
                    changed_photo = {**before, **values}
                    if not visit or visit["case_id"] != next_case or not allows_photo(visit['stage'], changed_photo):
                        raise DomainError("到访记录与照片所属案件或阶段不一致")
                    if photo_date(changed_photo) and not visit['visit_date'] <= photo_date(changed_photo) <= (visit['finished_at'] or visit['visit_date'])[:10]:
                        raise DomainError('照片日期不在所选进场时间范围内')
                confirmed = set(json.loads(before["confirmed_fields"]))
                if actor == "local":
                    confirmed.update(values)
                    values["confirmed_fields"] = dump(sorted(confirmed))
                    if data.get("confirm"):
                        values["recognition_state"] = "confirmed"
                        values["watermark_dedup_reason"] = ""
                        values["confirmed_fields"] = dump(sorted(PHOTO_FIELDS - {"notes", "visit_id"}))
                values.update(updated_at=now(), revision=before["revision"] + 1)
                con.execute("UPDATE photos SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), photo_id))
                if values.get('visit_id', before['visit_id']) != before['visit_id']:
                    if before['reuse_source_id']:
                        con.execute('DELETE FROM visit_shared_checkins WHERE photo_id=? AND visit_id=?',
                                    (before['reuse_source_id'], before['visit_id']))
                    for affected in {before['visit_id'], values.get('visit_id')} - {None}:
                        con.execute('UPDATE visits SET revision=revision+1 WHERE id=?', (affected,))
                after = rowdict(con.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone())
                audit(con, "photo", photo_id, "confirm" if data.get("confirm") else "update", before, after, actor)
        except sqlite3.IntegrityError as error:
            raise DomainError("该案件已经存在相同原文件，或指定的关联记录不存在", 409) from error
        return self.photo(photo_id)

    def reuse_checkin(self, photo_id, data):
        from .checkin_copies import copy_for_work
        if type(data.get('revision')) is not int or not data.get('case_id'):
            raise DomainError('请核对案件和照片版本后复用')
        with self.db.tx() as con:
            copy_id, created = copy_for_work(con, photo_id, data['case_id'], revision=data['revision'],
                                             repair_date=data.get('repair_date'))
        return {'photo': self.photo(copy_id), 'created': created, 'source_photo_id': photo_id}

    def summary(self):
        scope, args = project_filter('p.project_id')
        counts = self.db.one("SELECT COUNT(*) AS total,SUM(case_id IS NULL) AS unassigned,SUM(photo_group='unknown') AS unknown,SUM(recognition_state='failed') AS failed,SUM(b.media_type='image' AND (case_id IS NULL OR photo_group='unknown' OR recognition_state IN ('failed','conflict'))) AS exceptions FROM photos p JOIN blobs b ON b.hash=p.blob_hash WHERE " + scope, args)
        counts = {k: v or 0 for k, v in counts.items()}
        counts["stages"] = {r["stage"]: r["count"] for r in self.db.all("SELECT stage,COUNT(*) AS count FROM photos p WHERE " + scope + " GROUP BY stage", args)}
        scope, args = project_filter('project_id')
        counts["cases"] = self.db.one("SELECT COUNT(*) AS count FROM cases WHERE " + scope, args)["count"]
        person = current()
        own = person is not None and not person.admin
        counts["active_jobs"] = self.db.one("SELECT COUNT(*) AS count FROM jobs WHERE state IN ('queued','running')" + (" AND user_id=?" if own else ""), (person.id,) if own else ())["count"]
        counts["attachment_failures"] = 0 if own else self.db.one("SELECT COUNT(*) AS count FROM messages WHERE media_state IN ('failed','pending')")["count"]
        return counts

    def templates(self):
        rows = self.db.all("SELECT * FROM templates ORDER BY created_at")
        for row in rows:
            row["requirements"] = json.loads(row["requirements"])
        return rows

    def save_template(self, data, template_id=None):
        name = str(data.get("name", "")).strip()
        if not name:
            raise DomainError("请填写资料要求名称")
        rules = data.get("requirements", [])
        if not isinstance(rules, list) or len(rules) > 100:
            raise DomainError("资料要求格式无效")
        cleaned = []
        for rule in rules:
            group = rule.get("photo_group")
            if group:
                if group not in GROUP_FIELDS:
                    raise DomainError("资料要求的照片分类无效")
                rule = {**rule, "stage": GROUP_FIELDS[group][0], "role": GROUP_FIELDS[group][1]}
            if rule.get("stage") not in STAGES or rule.get("role") not in ROLES or not isinstance(rule.get("minimum"), int) or isinstance(rule.get("minimum"), bool) or not 1 <= rule["minimum"] <= 100:
                raise DomainError("每项要求需指定有效阶段、用途和1至100张最低数量")
            cleaned.append({"stage": rule["stage"], "role": rule["role"], "minimum": rule["minimum"], "per_visit": bool(rule.get("per_visit")), "part": str(rule.get("part", "")).strip(), "process": str(rule.get("process", "")).strip(), **({"photo_group": group} if group else {})})
        keys = [(r["stage"], r["role"], r.get("photo_group"), r["part"], r["process"], r["per_visit"]) for r in cleaned]
        if len(keys) != len(set(keys)):
            raise DomainError("资料要求中存在重复条目，请合并后保存")
        template_id = template_id or uid()
        with self.db.tx() as con:
            before = rowdict(con.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone())
            con.execute("INSERT INTO templates VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,insurer=excluded.insurer,requirements=excluded.requirements,updated_at=excluded.updated_at", (template_id, name[:120], str(data.get("insurer", ""))[:120], dump(cleaned), now(), now()))
            audit(con, "template", template_id, "update" if before else "create", before, data)
        return next(t for t in self.templates() if t["id"] == template_id)

    def completeness(self, case_id, template_id="general", photo_ids=None):
        case = self.case(case_id)
        template = next((t for t in self.templates() if t["id"] == template_id), None)
        if not template:
            raise DomainError("资料要求模板不存在", 404)
        all_photos = self.db.all("SELECT p.*,b.media_type,v.visit_date,v.visit_key,v.basis FROM photos p JOIN blobs b ON b.hash=p.blob_hash LEFT JOIN visits v ON v.id=p.visit_id WHERE p.case_id=?", (case_id,))
        images = [p for p in all_photos if p["media_type"] == "image" and p['recognition_state'] != 'conflict' and (photo_ids is None or p["id"] in photo_ids)]
        full_case = photo_ids is None or {p['id'] for p in all_photos if p['media_type'] == 'image'}.issubset(set(photo_ids))
        shared = {r['photo_id']: r for r in self.db.all('SELECT s.photo_id,s.visit_id,v.visit_date,v.visit_key,v.basis FROM visit_shared_checkins s JOIN visits v ON v.id=s.visit_id WHERE v.case_id=? '
                  'AND NOT EXISTS(SELECT 1 FROM photos copy WHERE copy.reuse_source_id=s.photo_id)', (case_id,))}
        # Count a shared original once in each intended use, never as two files.
        effective = images + [{**p, **shared[p['id']], 'stage': 'work', 'photo_group': 'work_checkin', 'shared_checkin': True}
                              for p in images if p['id'] in shared]
        results = []
        for rule in template["requirements"]:
            stage_photos = [p for p in effective if p["stage"] == rule["stage"]]
            scopes = {}
            if rule.get("per_visit"):
                if full_case:
                    for visit in self.db.all("SELECT * FROM visits WHERE case_id=? AND stage=?", (case_id, rule["stage"])):
                        label = visit["visit_date"] + (" · " + visit["visit_key"] if visit["visit_key"] else "")
                        scopes["visit:" + visit["id"]] = {"label": label, "photos": []}
                for p in stage_photos:
                    if p["visit_id"]:
                        key = "visit:" + p["visit_id"]
                        label = (p["visit_date"] or "日期未知") + (" · " + p["visit_key"] if p["visit_key"] else "")
                    else:
                        date = (p["watermark_time"] or p["exif_time"] or "")[:10]
                        key, label = "date:" + date, (date + " · 未分进场（按照片日期）" if date else "日期及进场待核对")
                    scopes.setdefault(key, {"label": label, "photos": []})["photos"].append(p)
                if not scopes:
                    scopes["none"] = {"label": "未见该阶段照片", "photos": []}
            else:
                scopes["case"] = {"label": "全案", "photos": stage_photos}
            for key, scope in scopes.items():
                matched = [p for p in scope["photos"] if (p["photo_group"] == rule["photo_group"] if rule.get("photo_group") else p["role"] == rule["role"]) and (not rule.get("part") or p["part"] == rule["part"]) and (not rule.get("process") or rule["process"] in p["process"])]
                count = len(matched)
                results.append({"stage": rule["stage"], "role": rule["role"], "photo_group": rule.get("photo_group", ""), "part": rule.get("part", ""), "process": rule.get("process", ""), "scope": scope["label"], "scope_key": key, "minimum": rule["minimum"], "count": count, "missing": max(0, rule["minimum"] - count), "photo_ids": [p["id"] for p in matched]})
        return {"case_id": case_id, "case_title": case["title"], "template_id": template_id, "scope": "selected" if photo_ids is not None else "case", "total_images": len(images), "missing_count": sum(r["missing"] > 0 for r in results), "items": results}

    def visits(self, case_id):
        from .visits import list_visits
        return list_visits(self, case_id)

    def save_visit(self, case_id, data, visit_id=None):
        from .visits import save_visit
        return save_visit(self, case_id, data, visit_id)
