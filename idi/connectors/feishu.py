"""Read-only Feishu HTTP access and a durable, idempotent receive pipeline."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

from ..db import dump, now
from ..domain import DomainError
from ..storage import ALLOWED_EXTS, IMAGE_EXTS, VIDEO_EXTS, safe_name

TOKEN_ERRORS = {99991661, 99991663, 99991664, 99991668}


def api_error(code):
    tips = {99991672: "应用权限尚未生效，请检查权限和应用发布状态", 99991679: "缺少接口权限，请检查权限和应用发布状态",
            234009: "飞书不支持从外部群下载此资源", 230001: "请求参数不被飞书接受", 230002: "机器人尚未加入该群"}
    return f"飞书接口返回 {code}：{tips.get(code, '请检查应用权限、群成员身份及资源是否仍可访问')}"


class FeishuClient:
    def __init__(self, app_id, secret, *, transport=None):
        self.app_id, self.secret = app_id, secret
        self.http = httpx.Client(base_url="https://open.feishu.cn/open-apis/", timeout=httpx.Timeout(45, connect=15),
                                 follow_redirects=False, transport=transport)
        self.token, self.expires = "", 0

    def close(self):
        self.http.close()

    def authenticate(self):
        if self.token and time.monotonic() < self.expires:
            return self.token
        try:
            response = self.http.post("auth/v3/tenant_access_token/internal", json={"app_id": self.app_id, "app_secret": self.secret})
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError("无法连接飞书认证服务，请检查网络后重试") from None
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise DomainError("飞书应用认证失败，请检查 App ID、App Secret 和应用状态")
        self.token = data["tenant_access_token"]
        self.expires = time.monotonic() + max(1, int(data.get("expire", 7200)) - 120)
        return self.token

    def get(self, path, params=None):
        for attempt in range(2):
            try:
                response = self.http.get(path, params=params, headers={"Authorization": "Bearer " + self.authenticate()})
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise DomainError("无法读取飞书数据，请检查网络后重试") from None
            if data.get("code") in TOKEN_ERRORS and attempt == 0:
                self.expires = 0
                continue
            if data.get("code") != 0:
                raise DomainError(api_error(data.get("code", "unknown")))
            return data.get("data", data)
        raise DomainError("飞书登录令牌刷新失败")

    def probe(self):
        bot = self.get("bot/v3/info").get("bot", {})
        groups, token, seen = [], "", set()
        while True:
            params = {"page_size": 100}
            if token:
                params["page_token"] = token
            page = self.get("im/v1/chats", params)
            groups.extend({"chat_id": item["chat_id"], "name": item.get("name", "")} for item in page.get("items", []))
            if not page.get("has_more"):
                break
            token = page.get("page_token")
            if not token or token in seen:
                raise DomainError("飞书群列表分页异常，请重试")
            seen.add(token)
        return {"bot_name": bot.get("app_name", ""), "groups": groups}

    def history(self, chat_id, start, end):
        token, seen = "", set()
        while True:
            params = {"container_id_type": "chat", "container_id": chat_id, "start_time": str(start), "end_time": str(end),
                      "sort_type": "ByCreateTimeAsc", "page_size": 50}
            if token:
                params["page_token"] = token
            page = self.get("im/v1/messages", params)
            for item in page.get("items", []):
                yield normalize(item, chat_id=chat_id)
            if not page.get("has_more"):
                return
            token = page.get("page_token")
            if not token or token in seen:
                raise DomainError("飞书消息分页异常，补收位置未推进")
            seen.add(token)

    def download(self, message_id, resource, directory, max_bytes):
        key = resource["key"]
        if not key or len(key) > 300 or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise DomainError("飞书附件标识无效")
        name = safe_name(resource.get("name") or (key + ".jpg"))
        if resource["type"] == "file" and Path(name).suffix.lower() not in ALLOWED_EXTS:
            raise DomainError("暂不支持该附件格式：" + (Path(name).suffix.lower() or "无扩展名"))
        directory.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha256((message_id + ":" + key).encode()).hexdigest()
        temp = directory / (stem + ".partial")
        path = f"im/v1/messages/{quote(message_id, safe='')}/resources/{quote(key, safe='')}"
        try:
            for attempt in range(2):
                with self.http.stream("GET", path, params={"type": resource["type"]}, headers={"Authorization": "Bearer " + self.authenticate()}) as response:
                    if "application/json" in response.headers.get("content-type", ""):
                        data = json.loads(response.read())
                        if data.get("code") in TOKEN_ERRORS and attempt == 0:
                            self.expires = 0
                            continue
                        raise DomainError(api_error(data.get("code", "unknown")))
                    response.raise_for_status()
                    if int(response.headers.get("content-length", "0")) > max_bytes:
                        raise DomainError("附件超过本机单文件大小限制")
                    size = 0
                    with temp.open("wb") as output:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise DomainError("附件超过本机单文件大小限制")
                            output.write(chunk)
                    if not size:
                        raise DomainError("飞书返回了空附件")
                    break
            if resource["type"] == "image":
                try:
                    with Image.open(temp) as image:
                        suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif"}.get(image.format)
                    if not suffix:
                        raise ValueError()
                    name = key + suffix
                except (OSError, ValueError):
                    raise DomainError("飞书附件无法解码为受支持的图片") from None
            destination = directory / (stem + Path(name).suffix.lower())
            temp.replace(destination)
            return destination, name
        except (httpx.HTTPError, ValueError):
            raise DomainError("飞书附件下载失败，请检查网络或稍后重试") from None
        finally:
            temp.unlink(missing_ok=True)


def normalize(value, *, chat_id=None):
    message = value.get("message", value)
    return {"message_id": message.get("message_id", ""), "chat_id": message.get("chat_id") or chat_id or "",
            "message_type": message.get("message_type") or message.get("msg_type") or "unknown",
            "create_time": message.get("create_time"), "content": message.get("content", message.get("body", {}).get("content", "{}")),
            "sender": value.get("sender", message.get("sender", {})), "deleted": bool(message.get("deleted", False))}


def content_parts(payload):
    content = payload.get("content", {})
    if isinstance(content, str):
        content = json.loads(content)
    if not isinstance(content, dict):
        raise DomainError("飞书消息内容格式无效")
    kind = payload["message_type"]
    resources, texts = [], []
    if kind == "image":
        resources.append({"type": "image", "key": content.get("image_key", ""), "name": ""})
    elif kind in ("file", "media"):
        resources.append({"type": "file", "key": content.get("file_key", ""), "name": content.get("file_name") or ("video.mp4" if kind == "media" else "file")})
    elif kind == "post":
        def walk(value):
            if isinstance(value, dict):
                if value.get("tag") == "img":
                    resources.append({"type": "image", "key": value.get("image_key", ""), "name": ""})
                if isinstance(value.get("text"), str):
                    texts.append(value["text"])
                if isinstance(value.get("title"), str):
                    texts.append(value["title"])
                for child in value.values():
                    if isinstance(child, (list, dict)):
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(content)
    elif kind == "text":
        texts.append(str(content.get("text", "")))
    else:
        texts.append(f"[暂未自动归档的飞书消息类型：{kind}]")
    unique = {item["type"] + ":" + item["key"]: item for item in resources}
    return "\n".join(texts)[:20000], list(unique.values())


class FeishuCapture:
    def __init__(self, service, connector_id, client):
        self.service, self.db, self.client = service, service.db, client
        self.connector_id = connector_id
        self.connector = service.get(connector_id)

    def state(self, **values):
        allowed = {"phase", "connected_at", "last_event_at", "last_history_at", "heartbeat_at", "error", "history_error", "cursor"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid capture state")
        with self.db.tx() as con:
            con.execute("UPDATE feishu_state SET " + ",".join(key + "=?" for key in values) + " WHERE connector_id=?", (*values.values(), self.connector_id))

    def receive(self, payload, *, live=False):
        if payload.get("chat_id") != self.connector["config"]["chat_id"] or payload.get("deleted"):
            return False
        sender = payload.get("sender", {})
        if sender.get("sender_type") != "user":
            return False
        message_id = payload.get("message_id", "")
        if not re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
            raise DomainError("飞书消息标识无效")
        current = self.db.one("SELECT * FROM feishu_state WHERE connector_id=?", (self.connector_id,))
        sent = int(payload.get("create_time") or 0) / 1000
        if sent < current["started_at"]:
            return False
        with self.db.tx() as con:
            result = con.execute("INSERT OR IGNORE INTO feishu_inbox(connector_id,message_id,payload,received_at,updated_at) VALUES(?,?,?,?,?)", (self.connector_id, message_id, dump(payload), now(), now()))
            if live:
                con.execute("UPDATE feishu_state SET last_event_at=? WHERE connector_id=?", (now(), self.connector_id))
        return bool(result.rowcount)

    def catch_up(self, end=None):
        state = self.db.one("SELECT * FROM feishu_state WHERE connector_id=?", (self.connector_id,))
        # Freeze each window; page through all results before advancing the cursor.
        end = int(time.time()) if end is None else int(end)
        start = max(state["started_at"], state["cursor"] - 2)
        try:
            if end > start:
                for payload in self.client.history(self.connector["config"]["chat_id"], start, end):
                    self.receive(payload)
                    self.state(heartbeat_at=now())
                self.state(cursor=end)
            self.state(last_history_at=now(), history_error=None)
            return True
        except Exception as error:
            self.state(history_error=str(error)[:500] if isinstance(error, DomainError) else "补收暂时失败，位置已保留并将重试")
            return False

    def recover(self):
        with self.db.tx() as con:
            con.execute("UPDATE feishu_inbox SET state='pending',retry_at=0 WHERE connector_id=? AND state='processing'", (self.connector_id,))

    def process_one(self):
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM feishu_inbox WHERE connector_id=? AND (state='pending' OR (state='failed' AND attempts<8 AND retry_at<=?)) ORDER BY CAST(json_extract(payload,'$.create_time') AS INTEGER),received_at,message_id LIMIT 1", (self.connector_id, time.time())).fetchone()
            if not row:
                return False
            row = dict(row)
            con.execute("UPDATE feishu_inbox SET state='processing',attempts=attempts+1,updated_at=? WHERE connector_id=? AND message_id=?", (now(), self.connector_id, row["message_id"]))
        try:
            self.archive(json.loads(row["payload"]), captured_at=row['received_at'])
            result, error, retry = "completed", None, 0
        except Exception as exc:
            result = "failed"
            error = str(exc)[:500] if isinstance(exc, DomainError) else "消息归档暂时失败，请重试或检查服务日志"
            retry = time.time() + min(3600, 30 * 2 ** min(row["attempts"], 7))
        with self.db.tx() as con:
            con.execute("UPDATE feishu_inbox SET state=?,error=?,retry_at=?,updated_at=? WHERE connector_id=? AND message_id=?", (result, error, retry, now(), self.connector_id, row["message_id"]))
        return True

    def archive(self, payload, *, captured_at=None):
        text, resources = content_parts(payload)
        sender = payload["sender"]
        sender_id = sender.get("sender_id", {}).get("open_id") or sender.get("id") or "未知发送人"
        sent_at = datetime.fromtimestamp(int(payload["create_time"]) / 1000, timezone.utc).isoformat()
        base = {"group_name": self.connector["config"]["chat_name"], "sender": sender_id, "sent_at": sent_at,
                "content": text, "context_text": text, "identity_scope": "feishu_message_resource",
                "chat_id": payload["chat_id"], "feishu_message_id": payload["message_id"], "remote_message_type": payload["message_type"], 'captured_at': captured_at or now()}
        if text or not resources:
            self.service.ingest(self.connector_id, {**base, "source_key": payload["message_id"] + ":text", "message_type": "text"})
        errors = []
        for resource in resources:
            source_key = payload["message_id"] + ":" + resource["type"] + ":" + resource["key"]
            existing = self.db.one("SELECT media_state FROM messages WHERE connector_id=? AND source_key=?", (self.connector_id, source_key))
            if existing and existing["media_state"] == "saved":
                continue
            event = {**base, "source_key": source_key, "resource_key": resource["key"], "resource_type": resource["type"], "message_type": "image" if resource["type"] == "image" else "file"}
            try:
                directory = self.service.settings.data_dir / "inbox" / self.connector_id
                path, name = self.client.download(payload["message_id"], resource, directory, self.service.settings.max_file_bytes)
                event.update(file_path=str(path), original_name=name)
                if path.suffix.lower() in IMAGE_EXTS:
                    event["message_type"] = "image"
                elif path.suffix.lower() in VIDEO_EXTS:
                    event["message_type"] = "video"
            except Exception as error:
                event["error"] = str(error)[:500] if isinstance(error, DomainError) else "附件下载暂时失败，将自动重试"
            result = self.service.ingest(self.connector_id, event)
            if result["media_state"] == "failed":
                errors.append(result.get("error", "附件未保存"))
        if errors:
            raise DomainError("；".join(errors)[:500])
