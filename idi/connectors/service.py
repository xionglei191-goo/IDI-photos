from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import Database, audit, dump, now, uid
from ..domain import DomainError
from ..storage import ALLOWED_EXTS, IMAGE_EXTS, VIDEO_EXTS

log = logging.getLogger(__name__)


class ConnectorService:
    def __init__(self, settings, db, importer, jobs):
        self.settings, self.db, self.importer, self.jobs = settings, db, importer, jobs
        self._stop = threading.Event()
        self._thread = None
        self._collectors = {}
        self._observed_files = {}
        self._restart_after = {}
        self._process_lock = threading.RLock()

    def get(self, connector_id):
        row = self.db.one("SELECT * FROM connectors WHERE id=?", (connector_id,))
        if not row:
            raise DomainError("采集入口不存在", 404)
        row["config"] = json.loads(row["config"])
        row["enabled"] = bool(row["enabled"])
        if row['kind'] == 'feed' and row['config'].get('adapter') == 'feishu':
            from .secret_store import SecretStore
            row['kind'] = 'feishu'
            row['secret_configured'] = SecretStore(self.settings.data_dir).exists(connector_id)
            row['capture'] = self.db.one('SELECT * FROM feishu_state WHERE connector_id=?', (connector_id,))
            row['queue'] = self.db.one("SELECT COUNT(*) AS total,SUM(state='completed') AS completed,SUM(state IN ('pending','processing')) AS pending,SUM(state='failed') AS failed FROM feishu_inbox WHERE connector_id=?", (connector_id,))
            failed = self.db.one("SELECT error FROM feishu_inbox WHERE connector_id=? AND state='failed' ORDER BY updated_at DESC LIMIT 1", (connector_id,))
            row['queue']['last_error'] = failed['error'] if failed else None
            if row['enabled'] and row['capture']:
                row['status'] = row['capture']['phase']
                row['error'] = row['capture']['error'] or row['capture']['history_error']
                heartbeat = row['capture']['heartbeat_at']
                if heartbeat and (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds() > 90:
                    row['status'] = 'error'
                    row['error'] = '接收进程心跳已过期，请检查服务是否运行'
        row["counts"] = self.db.one("SELECT COUNT(*) AS messages,SUM(media_state='saved') AS saved,SUM(media_state='failed') AS failed,SUM(media_state='pending') AS pending FROM messages WHERE connector_id=?", (connector_id,))
        row["wxauto_available"] = importlib.util.find_spec("wxautox4") is not None
        return row

    def list(self):
        return [self.get(row["id"]) for row in self.db.all("SELECT id FROM connectors ORDER BY created_at")]

    def save(self, data, connector_id=None):
        kind = data.get("kind")
        if kind not in ("folder", "wxauto", "feed", "feishu"):
            raise DomainError("采集类型无效")
        name = str(data.get("name", "")).strip()
        if not name:
            raise DomainError("请填写入口名称")
        config = data.get("config", {})
        project_id, case_id = config.get("project_id") or None, config.get("case_id") or None
        if case_id:
            case = self.importer.domain.case(case_id)
            project_id = case["project_id"]
        if project_id and not self.db.one("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise DomainError("项目不存在")
        cleaned = {"project_id": project_id, "case_id": case_id, "path": str(config.get("path", "")).strip(),
                   "groups": [str(x).strip() for x in config.get("groups", []) if str(x).strip()], "auto_ocr": bool(config.get("auto_ocr", True))}
        if len(cleaned["groups"]) > 50:
            raise DomainError("请将每个采集入口限制在50个已确认群以内")
        enabled = bool(data.get("enabled", False))
        connector_id = connector_id or uid()
        previous = self.get(connector_id) if self.db.one('SELECT id FROM connectors WHERE id=?', (connector_id,)) else None
        stored_kind = 'feed' if kind == 'feishu' else kind
        if previous and previous['kind'] != kind:
            raise DomainError('入口创建后不能更换类型，请新增入口')
        if kind == 'feishu':
            from .secret_store import SecretStore
            app_id, chat_id = str(config.get('app_id', '')).strip(), str(config.get('chat_id', '')).strip()
            if not re.fullmatch(r'cli_[a-zA-Z0-9]+', app_id) or not re.fullmatch(r'oc_[a-zA-Z0-9]+', chat_id):
                raise DomainError('请填写有效的飞书 App ID 并选择接收群')
            if previous and any(previous['config'].get(key) != config.get(key) for key in ('app_id', 'chat_id')):
                raise DomainError('更换应用或群时请新增入口，以保留原有补收位置和来源')
            chat_name = str(config.get('chat_name', '')).strip()[:200] or chat_id
            cleaned.update(adapter='feishu', app_id=app_id, chat_id=chat_id, chat_name=chat_name, groups=[chat_name], path='')
            secret = str(data.get('app_secret') or '').strip()
            vault = SecretStore(self.settings.data_dir)
            if enabled and not secret and not vault.exists(connector_id):
                raise DomainError('启用飞书入口前请填写 App Secret')
            if secret:
                vault.save(connector_id, secret)
        if enabled and kind == "folder":
            self.importer.folder_entries(cleaned["path"])
        if enabled and kind == "wxauto" and not cleaned["groups"]:
            raise DomainError("请填写需要接收资料的项目微信群名称")
        with self.db.tx() as con:
            old = con.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
            con.execute("INSERT INTO connectors VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,enabled=excluded.enabled,config=excluded.config,status=excluded.status,error=NULL,updated_at=excluded.updated_at", (connector_id, name[:120], stored_kind, int(enabled), dump(cleaned), "waiting" if enabled else "disabled", None, None, now(), now()))
            audit(con, "connector", connector_id, "update" if old else "create", dict(old) if old else None, {"name": name, "kind": kind, "enabled": enabled, "config": cleaned})
            if kind == 'feishu' and enabled:
                started = int(time.time())
                con.execute('INSERT OR IGNORE INTO feishu_state(connector_id,started_at,cursor) VALUES(?,?,?)', (connector_id, started, started))
        if not enabled or (old and (old["config"] != dump(cleaned) or old["kind"] != stored_kind)) or (kind == 'feishu' and data.get('app_secret')):
            self.stop_collector(connector_id)
        if kind == 'feishu' and enabled and self.settings.worker_enabled:
            self.start_collector(connector_id)
        return self.get(connector_id)

    def probe_feishu(self, data):
        from .feishu import FeishuClient
        from .secret_store import SecretStore
        app_id = str(data.get('app_id') or '').strip()
        secret = str(data.get('app_secret') or '').strip()
        if not secret and data.get('connector_id'):
            connector = self.get(data['connector_id'])
            if connector['kind'] != 'feishu' or connector['config']['app_id'] != app_id:
                raise DomainError('已保存凭据与此应用不匹配')
            secret = SecretStore(self.settings.data_dir).load(connector['id'])
        if not re.fullmatch(r'cli_[a-zA-Z0-9]+', app_id) or not secret:
            raise DomainError('请填写 App ID 和 App Secret')
        client = FeishuClient(app_id, secret)
        try:
            return client.probe()
        finally:
            client.close()

    def ingest(self, connector_id, event):
        connector = self.get(connector_id)
        if not connector["enabled"]:
            raise DomainError("该入口未启用")
        source_key = str(event.get("source_key", "")).strip()
        if not source_key or len(source_key) > 500:
            raise DomainError("采集记录需要稳定的来源标识")
        group = str(event.get("group_name", ""))[:200]
        if connector["kind"] == "wxauto" and group not in connector["config"]["groups"]:
            raise DomainError("消息不属于配置的项目群")
        if connector['kind'] == 'feishu' and event.get('chat_id') != connector['config']['chat_id']:
            raise DomainError('消息不属于配置的飞书群')
        existing = self.db.one("SELECT * FROM messages WHERE connector_id=? AND source_key=?", (connector_id, source_key))
        if existing and existing["media_state"] not in ("pending", "failed"):
            return {"message_id": existing["id"], "duplicate": True, "media_state": existing["media_state"], "photo_id": existing["photo_id"]}
        media = event.get("message_type", "text") in ("image", "file", "video")
        message_id = existing["id"] if existing else uid()
        raw = {k: event[k] for k in ("identity_scope", "ui_id", "ui_hash", "session_id", "file_path", "original_name", "context_text", "chat_id", "feishu_message_id", "remote_message_type", "resource_key", "resource_type", "captured_at") if k in event}
        if not existing:
            with self.db.tx() as con:
                captured_at = event.get('captured_at') if connector['kind'] == 'feishu' else None
                con.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, connector_id, source_key, group, str(event.get("sender", ""))[:200], event.get("sent_at") or None, captured_at or now(), str(event.get("content", ""))[:20000], str(event.get("message_type", "text")), "pending" if media else "none", None, dump(raw), None))
        elif media:
            with self.db.tx() as con:
                con.execute('UPDATE messages SET raw=? WHERE id=?', (dump({**json.loads(existing['raw']), **raw}), message_id))
        result = {"message_id": message_id, "duplicate": bool(existing), "media_state": "pending" if media else "none", "photo_id": None}
        if media:
            try:
                if event.get("error"):
                    raise DomainError(str(event["error"])[:1000])
                if not event.get("file_path"):
                    raise DomainError("已收到消息，但附件路径尚未提供")
                path = Path(event["file_path"]).resolve()
                allowed = self.settings.data_dir / "inbox" / connector_id
                if connector["config"].get("path"):
                    allowed = Path(connector["config"]["path"]).resolve()
                if not path.is_relative_to(allowed.resolve()):
                    raise DomainError("附件不在此采集入口的收件目录")
                if not path.is_file() or path.stat().st_size > self.settings.max_file_bytes:
                    raise DomainError("附件不存在或超过大小限制")
                message = self.db.one("SELECT * FROM messages WHERE id=?", (message_id,))
                since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
                contexts = self.db.all("SELECT content FROM messages WHERE connector_id=? AND group_name=? AND sender=? AND message_type='text' AND received_at>=? ORDER BY received_at DESC LIMIT 5", (connector_id, group, message["sender"], since))
                if connector['kind'] == 'feishu':
                    sent_at = message['sent_at']
                    since = (datetime.fromisoformat(sent_at) - timedelta(minutes=30)).isoformat() if sent_at else ''
                    contexts = self.db.all("SELECT content FROM messages WHERE connector_id=? AND group_name=? AND sender=? AND message_type='text' AND sent_at>=? AND sent_at<=? ORDER BY sent_at DESC LIMIT 5", (connector_id, group, message['sender'], since, sent_at)) if sent_at else []
                context_text = "\n".join([event.get("context_text", ""), *[r["content"] for r in reversed(contexts)]])
                before_read = path.stat()
                content = path.read_bytes()
                after_read = path.stat()
                if (before_read.st_size, before_read.st_mtime_ns) != (after_read.st_size, after_read.st_mtime_ns):
                    raise DomainError('文件仍在写入，等待保存完成后重试')
                imported = self.importer.add_file(content, event.get("original_name") or path.name,
                         case_id=connector["config"].get("case_id"), project_id=connector["config"].get("project_id"), source_type=connector["kind"],
                         source_path=str(path), message=message, metadata={"context_text": context_text, "identity_scope": event.get("identity_scope", "provided_event"), **{key: raw[key] for key in ('chat_id', 'feishu_message_id', 'remote_message_type', 'resource_key', 'resource_type', 'captured_at') if key in raw}})
                result.update(photo_id=imported["photo_id"], media_state="saved", duplicate=bool(existing) or imported["duplicate"])
                with self.db.tx() as con:
                    con.execute("UPDATE messages SET media_state='saved',photo_id=?,error=NULL WHERE id=?", (imported["photo_id"], message_id))
                if imported["media_type"] == "image" and not imported["duplicate"] and connector["config"].get("auto_ocr"):
                    self.jobs.enqueue("recognize", {"photo_ids": [imported["photo_id"]]})
            except Exception as error:
                result.update(media_state="failed", error=str(error))
                with self.db.tx() as con:
                    con.execute("UPDATE messages SET media_state='failed',error=? WHERE id=?", (str(error)[:2000], message_id))
        with self.db.tx() as con:
            con.execute("UPDATE connectors SET last_seen_at=?,status='running',error=NULL WHERE id=?", (now(), connector_id))
        return result

    def retry_message(self, message_id):
        row = self.db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not row:
            raise DomainError("消息记录不存在", 404)
        if row["media_state"] != "failed":
            raise DomainError("仅失败的附件可重试")
        if self.get(row['connector_id'])['kind'] == 'feishu':
            raw = json.loads(row['raw'])
            return self.retry_feishu(row['connector_id'], raw.get('feishu_message_id', ''))
        event = {**json.loads(row["raw"]), "source_key": row["source_key"], "group_name": row["group_name"], "sender": row["sender"], "sent_at": row["sent_at"], "content": row["content"], "message_type": row["message_type"]}
        return self.ingest(row["connector_id"], event)

    def retry_feishu(self, connector_id, message_id=None):
        connector = self.get(connector_id)
        if connector['kind'] != 'feishu' or not connector['enabled']:
            raise DomainError('请先启用飞书入口')
        with self.db.tx() as con:
            where = ' AND message_id=?' if message_id else ''
            values = (connector_id, message_id) if message_id else (connector_id,)
            count = con.execute("UPDATE feishu_inbox SET state='pending',attempts=0,retry_at=0,error=NULL WHERE connector_id=? AND state='failed'" + where, values).rowcount
        return {'media_state': 'pending', 'queued': count}

    def scan_once(self):
        for connector in self.list():
            if not connector["enabled"] or connector["kind"] != "folder":
                continue
            try:
                entries = self.importer.folder_entries(connector["config"]["path"])
                for entry in entries:
                    path = Path(entry["path"])
                    stat = path.stat()
                    if time.time() - stat.st_mtime < 3:
                        continue
                    key = hashlib.sha256(dump([str(path), stat.st_size, stat.st_mtime_ns]).encode()).hexdigest()
                    if self.db.one("SELECT id FROM messages WHERE connector_id=? AND source_key=?", (connector["id"], key)):
                        continue
                    observed_key = (connector['id'], str(path))
                    previous_key, previous_time = self._observed_files.get(observed_key, (None, 0))
                    if previous_key != key:
                        self._observed_files[observed_key] = (key, time.monotonic())
                        continue
                    if time.monotonic() - previous_time < 3:
                        continue
                    suffix = path.suffix.lower()
                    self.ingest(connector["id"], {"source_key": key, "identity_scope": "path_size_mtime", "message_type": "image" if suffix in IMAGE_EXTS else "video" if suffix in VIDEO_EXTS else "file", "file_path": str(path), "original_name": path.name, 'content': path.name})
                    self._observed_files.pop(observed_key, None)
                with self.db.tx() as con:
                    con.execute("UPDATE connectors SET status='running',error=NULL WHERE id=?", (connector["id"],))
            except Exception as error:
                with self.db.tx() as con:
                    con.execute("UPDATE connectors SET status='error',error=? WHERE id=?", (str(error)[:2000], connector["id"]))

    def start_collector(self, connector_id):
        with self._process_lock:
            return self._start_collector(connector_id)

    def _start_collector(self, connector_id):
        connector = self.get(connector_id)
        if connector['kind'] == 'feishu':
            if not connector['enabled']:
                raise DomainError('请先启用飞书入口')
            if not self.settings.worker_enabled:
                raise DomainError('当前实例未启用后台接收')
            existing = self._collectors.get(connector_id)
            if existing and existing.poll() is None:
                return {'status': 'starting', 'pid': existing.pid}
            log_path = self.settings.data_dir / 'logs' / ('feishu-' + connector_id + '.log')
            with self.db.tx() as con:
                con.execute("UPDATE feishu_state SET phase='starting',error=NULL,heartbeat_at=? WHERE connector_id=?", (now(), connector_id))
            with log_path.open('ab') as logfile:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'idi.connectors.feishu_worker', '--connector-id', connector_id, '--data-dir', str(self.settings.data_dir), '--parent-pid', str(os.getpid())], stdout=logfile, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._collectors[connector_id] = process
            return {'status': 'starting', 'pid': process.pid}
        if connector["kind"] != "wxauto" or not connector["enabled"]:
            raise DomainError("请先配置并启用微信入口")
        if not connector["config"]["groups"]:
            raise DomainError("尚未配置项目微信群")
        if importlib.util.find_spec("wxautox4") is None:
            raise DomainError("尚未安装匹配当前微信版本的wxauto Plus组件，请先完成版本和授权配置")
        existing = self._collectors.get(connector_id)
        if existing and existing.poll() is None:
            return {"status": "running", "pid": existing.pid}
        log_path = self.settings.data_dir / "logs" / ("collector-" + connector_id + ".log")
        with log_path.open("ab") as logfile:
            process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "idi.connectors.wxauto", "--connector-id", connector_id, "--server", f"http://127.0.0.1:{self.settings.port}", "--data-dir", str(self.settings.data_dir)], stdout=logfile, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._collectors[connector_id] = process
        return {"status": "starting", "pid": process.pid}

    def stop_collector(self, connector_id):
        with self._process_lock:
            self._stop_collector(connector_id)

    def _stop_collector(self, connector_id):
        process = self._collectors.pop(connector_id, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)

    def start(self):
        with self.db.tx() as con:
            con.execute("UPDATE connectors SET status='waiting',error='服务已启动，请在入口列表启动微信采集' WHERE kind='wxauto' AND enabled=1")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="idi-inbox", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.scan_once()
                for connector_id, process in list(self._collectors.items()):
                    if process.poll() is not None:
                        with self.db.tx() as con:
                            con.execute("UPDATE connectors SET status='error',error=? WHERE id=?", (f"采集进程已退出：{process.returncode}，请查看日志", connector_id))
                            con.execute("UPDATE feishu_state SET phase='error',error='接收进程已退出，30 秒后自动重启' WHERE connector_id=?", (connector_id,))
                        self._collectors.pop(connector_id, None)
                        self._restart_after[connector_id] = time.monotonic() + 30
                for connector in self.list():
                    if connector['kind'] == 'feishu' and connector['enabled'] and connector['id'] not in self._collectors and time.monotonic() >= self._restart_after.get(connector['id'], 0):
                        self.start_collector(connector['id'])
            except Exception:
                log.exception("Collection scan failed")
            self._stop.wait(5)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        for connector_id in list(self._collectors):
            self.stop_collector(connector_id)
