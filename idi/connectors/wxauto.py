"""Optional wxauto Plus read-only collector. Never imported during normal startup.

Requires an explicitly configured group list and a compatible licensed wxautox4
installation. UI IDs only deduplicate inside this collector session. Durable
outbox event IDs survive delivery retries; they do not prove history backfill.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8766")
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    from wxautox4 import WeChat

    inbox = Path(args.data_dir).resolve() / "inbox" / args.connector_id
    outbox = inbox / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    seen = set()
    with httpx.Client(base_url=args.server, headers={"X-IDI-Request": "1"}, timeout=30) as client:
        response = client.get("/api/connectors/" + args.connector_id)
        response.raise_for_status()
        connector = response.json()
        groups = connector["config"].get("groups", [])
        if not connector["enabled"] or not groups:
            raise RuntimeError("The connector is disabled or has no configured groups")
        if connector["config"].get("path"):
            inbox = Path(connector["config"]["path"]).resolve()
            inbox.mkdir(parents=True, exist_ok=True)
        def callback_for(group):
            def on_message(message, chat):
                message_type = getattr(message, "type", "other")
                if message_type not in ("text", "image", "file", "video"):
                    return
                ui_id = str(getattr(message, "id", ""))
                key = (group, ui_id)
                if ui_id and key in seen:
                    return
                event_id = uuid.uuid4().hex
                event = {"source_key": event_id, "session_id": session_id, "identity_scope": "session_ui" if ui_id else "callback_event", "ui_id": ui_id,
                         "ui_hash": str(getattr(message, "hash", "")), "group_name": group, "sender": str(getattr(message, "sender", "")),
                         "message_type": message_type, "content": str(getattr(message, "content", "")), "sent_at": None}
                # Timestamp is unknown unless the adapter can independently establish it.
                if message_type in ("image", "video", "file"):
                    try:
                        if message_type in ("image", "video"):
                            path = message.download(dir_path=inbox, original=True)
                        else:
                            path = message.download(dir_path=inbox)
                        if not isinstance(path, (str, Path)) or not Path(path).is_file():
                            raise RuntimeError("Attachment download did not return an existing file")
                        event.update(file_path=str(Path(path).resolve()), original_name=Path(path).name)
                    except Exception as error:
                        event["error"] = type(error).__name__ + ": " + str(error)[:500]
                target = outbox / (event_id + ".json")
                temp = target.with_suffix(".tmp")
                temp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
                temp.replace(target)
                if ui_id:
                    seen.add(key)
            return on_message
        wx = WeChat()
        for group in groups:
            wx.AddListenChat(nickname=group, callback=callback_for(group))
        wx.StartListening()
        try:
            while True:
                for path in sorted(outbox.glob("*.json")):
                    try:
                        event = json.loads(path.read_text(encoding="utf-8"))
                        response = client.post("/api/connectors/" + args.connector_id + "/events", json=event)
                        response.raise_for_status()
                        path.replace(path.with_suffix(".delivered"))
                    except (httpx.HTTPError, ValueError):
                        break
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            wx.StopListening()


if __name__ == "__main__":
    main()
