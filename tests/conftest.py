import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from idi.api import create_app
from idi.config import Settings
from idi.connectors.secret_store import initialize_key


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    # Linux integration tests use real encryption with a new key for every test.
    # Windows keeps exercising DPAPI unless a test explicitly selects a key file.
    monkeypatch.delenv("IDI_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("IDI_NETWORK_FILE", raising=False)
    if os.name != "nt":
        key = tmp_path / "key" / "master.key"
        initialize_key(key, tmp_path / "archive")
        monkeypatch.setenv("IDI_SECRET_KEY_FILE", str(key))


@pytest.fixture
def app(tmp_path):
    return create_app(Settings((tmp_path / "archive").resolve(), worker_enabled=False))


@pytest.fixture
def admin_user(app):
    access = app.state.services['access']
    return access.save_user({'username': 'test-admin', 'display_name': '测试管理员',
                             'password': 'isolated-admin-password', 'system_role': 'admin'}, bootstrap=True)


@pytest.fixture
def client(app, admin_user):
    with TestClient(app, headers={"X-IDI-Request": "1"}) as value:
        response = value.post('/api/auth/login', json={'username': 'test-admin', 'password': 'isolated-admin-password'})
        assert response.status_code == 200, response.text
        yield value


@pytest.fixture
def services(app):
    return app.state.services


@pytest.fixture
def project(services):
    return services["domain"].save_project({"name": "从化铂悦府", "aliases": ["碧桂园铂悦府"]})


@pytest.fixture
def case(services, project):
    return services["domain"].save_case({"project_id": project["id"], "title": "主卧、客厅墙面油漆起皮", "building": "1", "room": "201", "repair_parts": ["客厅沙发旁", "主卧靠床头"]})


def image_bytes(color="navy"):
    stream = io.BytesIO()
    Image.new("RGB", (140, 100), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def photo(services, case):
    def add(color="navy", name="照片.png", **metadata):
        result = services["importer"].add_file(image_bytes(color), name, case_id=case["id"], metadata=metadata)
        return services["domain"].photo(result["photo_id"])
    return add
