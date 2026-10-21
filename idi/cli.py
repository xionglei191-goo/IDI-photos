from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .config import ROOT, Settings
from .db import uid


def restore(source: Path, target: Path, resume=False):
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("恢复目标必须为新的空目录，不会覆盖现有档案")
    if not source.is_file():
        raise ValueError("备份文件不存在")
    with zipfile.ZipFile(source) as archive:
        metadata_info = archive.getinfo("backup-metadata.json")
        if metadata_info.file_size > 30 * 1024 * 1024:
            raise ValueError("备份元数据异常")
        metadata = json.loads(archive.read("backup-metadata.json"))
        files = metadata.get("files", {})
        if metadata.get("schema") != 1 or "archive.sqlite3" not in files:
            raise ValueError("不是支持的IDI备份")
        entries = []
        for name, digest in files.items():
            path = PurePosixPath(name.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in name or not (name == "archive.sqlite3" or path.parts[0] in {"originals", "thumbnails", "exports"}):
                raise ValueError("备份中包含无效路径")
            destination = (target / name).resolve()
            if not destination.is_relative_to(target):
                raise ValueError("备份路径越界")
            info = archive.getinfo(name)
            if info.is_dir() or info.file_size > 2 * 1024 ** 3:
                raise ValueError("备份文件大小或类型异常")
            entries.append((info, destination, digest))
        target.mkdir(parents=True, exist_ok=True)
        for info, destination, expected in entries:
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(info) as inp, destination.open("xb") as out:
                while block := inp.read(1024 * 1024):
                    digest.update(block)
                    out.write(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"备份校验失败：{info.filename}；恢复目录保持停止状态")
    with closing(sqlite3.connect(target / "archive.sqlite3")) as con, con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复数据库完整性检查失败")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("恢复数据库关联检查失败")
        if not resume:
            con.execute("INSERT INTO settings VALUES('automatic_upload_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({'enabled': False, 'activated_at': ''}),))
            policy = con.execute("SELECT value FROM settings WHERE key='workflow_policy'").fetchone()
            value = json.loads(policy[0]) if policy else {}
            value.update(rules_enabled=False, auto_drafts=False)
            con.execute("INSERT INTO settings VALUES('workflow_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_ai_tasks'").fetchone():
                con.execute("UPDATE photo_ai_tasks SET state='cancelled',error='从备份恢复，请核对资料后重新分析' WHERE state IN ('queued','waiting','running')")
                con.execute("UPDATE rule_draft_intents SET state='needs_check',error='从备份恢复，需核对原审核批次' WHERE state='running'")
                con.execute("UPDATE rule_draft_intents SET state='cancelled',error='从备份恢复，未继续官网保存' WHERE state IN ('queued','awaiting_login','read_only')")
            schedule = con.execute("SELECT value FROM settings WHERE key='platform_sync_policy'").fetchone()
            value = json.loads(schedule[0]) if schedule else {}
            value['enabled'] = False
            con.execute("INSERT INTO settings VALUES('platform_sync_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
            if con.execute("SELECT 1 FROM pragma_table_info('platform_sync_runs') WHERE name='retry_at'").fetchone():
                con.execute("UPDATE platform_sync_runs SET state='cancelled',retry_at=NULL,error='从备份恢复，自动同步暂停' WHERE state IN ('running','queued','partial')")
            con.execute("UPDATE connectors SET enabled=0,status='disabled',error='从备份恢复，等待重新启用'")
            con.execute("UPDATE jobs SET state='failed',error='从备份恢复，等待手动重试' WHERE state IN ('queued','running')")
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='case_agent_runs'").fetchone():
                con.execute("UPDATE case_agent_runs SET state='cancelled',error='从备份恢复，请重新发起审阅' WHERE state IN ('queued','running')")
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_chat_turns'").fetchone():
                con.execute("UPDATE workspace_chat_turns SET state='cancelled',progress='已停止',error='从备份恢复，请重新发送问题' WHERE state IN ('queued','running')")
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feishu_agent_bindings'").fetchone():
                con.execute('UPDATE feishu_agent_bindings SET enabled=0,revision=revision+1')
                con.execute("UPDATE feishu_agent_tasks SET state='cancelled',error='从备份恢复，请核对原任务后重新启用群助手' WHERE state IN ('queued','running')")
                con.execute("UPDATE feishu_agent_outbox SET state='cancelled',error='从备份恢复，未自动发送' WHERE state IN ('queued','failed')")
                con.execute("UPDATE feishu_agent_outbox SET state='unknown',error='恢复前发送结果需人工核对' WHERE state='sending'")
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='followup_contacts'").fetchone():
                con.execute('UPDATE followup_contacts SET auto_feedback=0')
                con.execute("UPDATE followup_feedback SET state='cancelled',error='从备份恢复，需重新确认反馈' WHERE state IN ('queued','failed')")
                con.execute("UPDATE followup_feedback SET state='unknown',error='恢复前发送结果需人工核对' WHERE state='sending'")
                con.execute("UPDATE followup_runs SET state='failed',error='从备份恢复，等待重新检查' WHERE state IN ('queued','running')")
                config = con.execute("SELECT value FROM settings WHERE key='followup'").fetchone()
                if config:
                    value = json.loads(config[0])
                    value.update(auto_review=False, ai_review=False)
                    con.execute("UPDATE settings SET value=? WHERE key='followup'", (json.dumps(value),))
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_notifications'").fetchone():
                con.execute("UPDATE review_notifications SET state='cancelled',error='从备份恢复，核对提醒已暂停' WHERE state IN ('queued','failed')")
                con.execute("UPDATE review_notifications SET state='unknown',error='恢复前发送结果需核对群内记录' WHERE state='sending'")
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_attachment_removals'").fetchone():
                con.execute("UPDATE platform_attachment_removals SET state='stale',error='从备份恢复，请重新核对删除对象' WHERE state='prepared'")
                con.execute("UPDATE platform_attachment_removals SET state='uncertain',error='恢复前撤回结果需回读官网核实' WHERE state='removing'")
    return {"target": str(target), "verified_files": len(files), "connectors_enabled": resume}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--data-dir", type=Path, default=ROOT / "data")
    recovery = sub.add_parser("restore")
    recovery.add_argument("archive", type=Path)
    recovery.add_argument("--target", required=True, type=Path)
    recovery.add_argument("--resume-tasks", action="store_true")
    args = parser.parse_args()
    if args.command == "restore":
        result = restore(args.archive.resolve(), args.target.resolve(), args.resume_tasks)
    else:
        from .api import create_app
        app = create_app(Settings(args.data_dir.resolve(), worker_enabled=False))
        result = app.state.services["reporter"].backup({"backup_id": uid()}, lambda done, total: None)
        result["path"] = str(args.data_dir.resolve() / "backups" / result["filename"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
