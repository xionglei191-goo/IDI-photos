"""Isolated, synthetic IDI backend for Android device tests (never production data)."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from idi.api import create_app
from idi.config import Settings


def prepare(root: Path, mock_xhey=False, live_xhey_config=None, local_only=False, live_verify=False):
    app = create_app(Settings((root / 'server').resolve(), worker_enabled=False))
    services = app.state.services
    if live_xhey_config:
        from idi.xhey_camera import XheySettings
        if mock_xhey:
            raise ValueError('Live and mock providers cannot be combined')
        if root.resolve() == Path(live_xhey_config).resolve() or (root / 'server').resolve() == Path(live_xhey_config).resolve():
            raise ValueError('Live smoke must use a separate synthetic data directory')
        value = XheySettings(Path(live_xhey_config)).load()
        services['official_camera'].settings.save({**value, 'enabled': True, 'auto_verify': live_verify})
        if not live_verify:
            def forbidden_live_verification(config):
                raise AssertionError('Live SDK acceptance has local verification disabled')
            services['official_camera'].client_factory = forbidden_live_verification
        services['official_camera'].start()
    if mock_xhey:
        import httpx
        from idi.xhey_camera import XheyClient
        official = services['official_camera']
        official.settings.save({'enabled': True, 'auto_verify': True, 'sdk_app_id': 'SYNTHETIC-TEST', 'sdk_secret_key': 'SYNTHETIC-TEST',
                                'group_key': 'SYNTHETIC-TEST', 'group_secret': 'SYNTHETIC-TEST', 'group_watermark_id': 'SYNTHETIC-TEST'})
        def transport(request):
            action = request.url.path.rsplit('/', 1)[-1]
            if action == 'create':
                data = {'status': 0, 'data': [{'taskID': 'SYNTHETIC-MOCK-TASK'}]}
            elif action == 'query':
                data = {'status': 0, 'taskStatus': 5, 'list': [{'photoBase64Idx': 0, 'status': 0,
                        'antiFakeCode': 'SYNTHETIC-NOT-A-REAL-CODE', 'photoTime': '2026年10月20日 08:00'}]}
            else:
                raise AssertionError('Unexpected mock API action')
            return httpx.Response(200, json={'code': 200, 'data': data})
        official.client_factory = lambda config: XheyClient(config, transport=httpx.MockTransport(transport))
        official.start()
    if local_only:
        official = services['official_camera']
        official.settings.save({'enabled': True, 'auto_verify': False,
                                'sdk_app_id': 'SYNTHETIC-TEST', 'sdk_secret_key': 'SYNTHETIC-TEST',
                                'group_watermark_id': 'SYNTHETIC-TEST'})
        def forbidden(config):
            raise AssertionError('Local-only acceptance must never create a verification client')
        official.client_factory = forbidden
        official.start()
    domain, db = services['domain'], services['db']
    project = next((item for item in domain.projects() if item['name'] == '铂悦府（测试）'), None)
    if project is None:
        project = domain.save_project({'name': '铂悦府（测试）'})
        for building, room, title, number, stage, part in (
            ('2', '302', '厨房渗水', 'ZLBH202610190000000002', 'survey', '厨房'),
            ('1', '201', '卫生间瓷砖修补', 'ZLBH202610190000000001', 'repair', '卫生间'),
        ):
            case = domain.save_case({'project_id': project['id'], 'title': title, 'building': building,
                                     'room': room, 'platform_report_code': number, 'repair_parts': [part]})
            with db.tx() as con:
                con.execute('UPDATE cases SET workflow_stage=? WHERE id=?', (stage, case['id']))
    owner = db.one("SELECT id FROM idi_users WHERE username='android-acceptance' AND enabled=1")
    if owner is None:
        owner = services['access'].save_user({
            'username': 'android-acceptance', 'display_name': '安卓隔离验收',
            'password': secrets.token_urlsafe(24), 'system_role': 'admin'}, bootstrap=True)
    pairing = services['mobile_camera'].create_pairing({
        'name': '安卓验收模拟器', 'project_ids': [project['id']], 'user_id': owner['id']})
    root.mkdir(parents=True, exist_ok=True)
    (root / 'pairing.json').write_text(json.dumps(pairing), encoding='utf-8')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT / 'artifacts' / 'camera-acceptance')
    parser.add_argument('--port', type=int, default=8876)
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument('--mock-xhey', action='store_true', help='Synthetic verification transport; never calls Xhey')
    provider.add_argument('--local-only', action='store_true', help='SDK-only config with verification forbidden; synthetic local archive test')
    provider.add_argument('--live-xhey-config', type=Path, help='Explicit live smoke: read encrypted credentials from this data directory; test photos use trial quota')
    parser.add_argument('--live-verify', action='store_true', help='Explicitly enable billable local verification for live SDK tests; default off')
    args = parser.parse_args()
    if args.live_verify and not args.live_xhey_config:
        parser.error('--live-verify requires --live-xhey-config')
    app = prepare(args.root, args.mock_xhey, args.live_xhey_config, args.local_only, args.live_verify)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')
