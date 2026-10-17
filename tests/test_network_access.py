import json

import pytest
from fastapi.testclient import TestClient

from idi.api import create_app
from idi.config import Settings


@pytest.fixture
def lan_app(tmp_path):
    return create_app(Settings((tmp_path / 'archive').resolve(), host='0.0.0.0', worker_enabled=False,
                               lan_hosts=('192.168.31.252',), lan_subnets=('192.168.31.0/24',)))


def test_lan_client_reads_page_and_can_make_same_origin_write(lan_app):
    with TestClient(lan_app, base_url='http://192.168.31.252:8766', client=('192.168.31.80', 50000)) as client:
        assert client.get('/api/health').status_code == 200
        system = client.get('/api/system').json()
        assert system['local_only'] is False
        assert system['lan_urls'] == ['http://192.168.31.252:8766']
        response = client.post('/api/projects', json={'name': '局域网测试项目'},
                               headers={'X-IDI-Request': '1', 'Origin': 'http://192.168.31.252:8766'})
        assert response.status_code == 200
        assert response.json()['name'] == '局域网测试项目'


@pytest.mark.parametrize('remote', ['192.168.32.10', '10.1.2.3', '203.0.113.10', '::ffff:192.168.31.80'])
def test_other_networks_cannot_read_even_with_forwarded_headers(lan_app, remote):
    with TestClient(lan_app, base_url='http://192.168.31.252:8766', client=(remote, 50000)) as client:
        response = client.get('/api/health', headers={'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1'})
        assert response.status_code == 403


def test_lan_mode_retains_host_and_write_protections(lan_app):
    with TestClient(lan_app, base_url='http://192.168.31.252:8766', client=('192.168.31.80', 50000)) as client:
        assert client.get('/api/health', headers={'Host': 'attacker.example'}).status_code == 400
        assert client.get('/api/health', headers={'Host': '192.168.31.99'}).status_code == 400
        assert client.post('/api/projects', json={'name': 'bad'}).status_code == 403
        for origin in ('https://attacker.example', 'http://192.168.31.252:8000', 'https://192.168.31.252:8766', 'null'):
            response = client.post('/api/projects', json={'name': 'bad'}, headers={'X-IDI-Request': '1', 'Origin': origin})
            assert response.status_code == 403
        assert client.get('/api/projects').json() == []


def test_default_still_rejects_lan_clients(app):
    with TestClient(app, client=('192.168.31.80', 50000)) as client:
        assert client.get('/api/health').status_code == 403


def test_loopback_remains_available_in_lan_mode(lan_app):
    with TestClient(lan_app, base_url='http://127.0.0.1:8766', client=('127.0.0.1', 50000)) as client:
        assert client.get('/api/health').status_code == 200


def test_network_file_survives_restart_and_local_override(tmp_path, monkeypatch):
    monkeypatch.setenv('IDI_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('IDI_HOST', raising=False)
    assert Settings.from_env().local_only
    (tmp_path / 'network.json').write_text(json.dumps({
        'host': '0.0.0.0', 'lan_hosts': ['192.168.31.252'], 'lan_subnets': ['192.168.31.0/24'],
    }), encoding='utf-8')
    for _ in range(2):
        settings = Settings.from_env()
        assert not settings.local_only
        assert settings.allows_client('192.168.31.80')
    monkeypatch.setenv('IDI_HOST', '127.0.0.1')
    assert Settings.from_env().local_only
    assert not Settings.from_env().allows_client('192.168.31.80')


@pytest.mark.parametrize('network', [
    [], {'host': '0.0.0.0'}, {'host': '0.0.0.0', 'lan_hosts': '*', 'lan_subnets': []},
    {'host': '0.0.0.0', 'lan_hosts': ['192.168.31.252'], 'lan_subnets': ['0.0.0.0/0']},
    {'host': '0.0.0.0', 'lan_hosts': ['192.168.32.252'], 'lan_subnets': ['192.168.31.0/24']},
    {'host': '192.168.31.99', 'lan_hosts': ['192.168.31.252'], 'lan_subnets': ['192.168.31.0/24']},
])
def test_incomplete_or_public_network_configuration_is_rejected(tmp_path, monkeypatch, network):
    monkeypatch.setenv('IDI_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('IDI_HOST', raising=False)
    (tmp_path / 'network.json').write_text(json.dumps(network), encoding='utf-8')
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.fixture
def tunnel_app(tmp_path):
    return create_app(Settings((tmp_path / 'archive').resolve(), host='0.0.0.0', worker_enabled=False,
                               lan_hosts=('192.168.31.252',), lan_subnets=('192.168.31.0/24',),
                               public_origins=('https://idi.example.test',), trusted_proxies=('192.168.31.246',)))


def test_tunnel_get_and_same_origin_write_preserve_real_proxy_peer(tunnel_app):
    with TestClient(tunnel_app, base_url='http://idi.example.test', client=('192.168.31.246', 50000),
                    headers={'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.15'}) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/system').json()['public_urls'] == ['https://idi.example.test']
        response = client.post('/api/projects', json={'name': '公网入口测试'},
                               headers={'Origin': 'https://idi.example.test', 'X-IDI-Request': '1'})
        assert response.status_code == 200
        assert response.json()['name'] == '公网入口测试'


@pytest.mark.parametrize('peer,proto', [
    ('192.168.31.80', 'https'), ('127.0.0.1', 'https'), ('203.0.113.15', 'https'),
    ('192.168.31.246', ''), ('192.168.31.246', 'http'), ('192.168.31.246', 'https,http'),
])
def test_public_host_requires_the_exact_proxy_and_https(tunnel_app, peer, proto):
    with TestClient(tunnel_app, base_url='http://idi.example.test', client=(peer, 50000)) as client:
        response = client.get('/api/health', headers={'X-Forwarded-Proto': proto, 'X-Forwarded-For': '192.168.31.246'})
        assert response.status_code == 403


def test_tunnel_keeps_domain_origin_and_header_checks(tunnel_app):
    with TestClient(tunnel_app, base_url='http://idi.example.test', client=('192.168.31.246', 50000),
                    headers={'X-Forwarded-Proto': 'https'}) as client:
        assert client.get('/api/health', headers={'Host': 'other.example.test'}).status_code == 400
        assert client.get('/api/health', headers={'Host': 'idi.example.test:8766'}).status_code == 403
        assert client.post('/api/projects', json={'name': 'bad'}, headers={'Origin': 'https://idi.example.test'}).status_code == 403
        for origin in ('http://idi.example.test', 'https://other.example.test', 'http://192.168.31.252:8766'):
            response = client.post('/api/projects', json={'name': 'bad'},
                                   headers={'Origin': origin, 'X-IDI-Request': '1'})
            assert response.status_code == 403


def test_public_configuration_does_not_change_direct_lan_access(tunnel_app):
    with TestClient(tunnel_app, base_url='http://192.168.31.252:8766', client=('192.168.31.80', 50000)) as client:
        assert client.get('/api/health').status_code == 200
        response = client.post('/api/projects', json={'name': '直接局域网访问'},
                               headers={'Origin': 'http://192.168.31.252:8766', 'X-IDI-Request': '1', 'X-Forwarded-Proto': 'https'})
        assert response.status_code == 200


@pytest.mark.parametrize('origins,proxies', [
    (['http://idi.example.test'], ['192.168.31.246']),
    (['https://*.example.test'], ['192.168.31.246']),
    (['https://idi.example.test/path'], ['192.168.31.246']),
    (['https://user@idi.example.test'], ['192.168.31.246']),
    (['https://idi.example.test'], []),
    (['https://idi.example.test'], ['192.168.31.0/24']),
    (['https://idi.example.test'], ['203.0.113.15']),
])
def test_unsafe_tunnel_configuration_is_rejected(tmp_path, origins, proxies):
    with pytest.raises(ValueError):
        Settings(tmp_path, host='0.0.0.0', lan_hosts=('192.168.31.252',), lan_subnets=('192.168.31.0/24',),
                 public_origins=tuple(origins), trusted_proxies=tuple(proxies))
