import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from idi.api import create_app
from idi.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings((tmp_path / "archive").resolve(), worker_enabled=False))


@pytest.fixture
def client(app):
    with TestClient(app, headers={"X-IDI-Request": "1"}) as value:
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
