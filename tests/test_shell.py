"""The application shell: server, routes, paths and the repository rules.

The window itself, the splash timing and the navigation are checked by hand;
everything a machine can check is here.
"""

import sys

import pytest

from app import APP_VERSION, create_app
from utils import branding
from utils.net import protocol
from utils.store import paths

# tools/ is a development directory and is meant to be deletable before a
# public release, so these two checks skip rather than fail when it is gone.
check_ascii = pytest.importorskip(
    "tools.check_ascii", reason="tools/ has been removed"
)


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def test_health_reports_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["app_version"] == APP_VERSION
    # Against the protocol module rather than a literal. A version written
    # down twice is a version that eventually disagrees with itself, and the
    # test asserting the old number is not the failure anybody wants.
    assert payload["protocol_version"] == protocol.PROTOCOL_VERSION


def test_branding_endpoint_serves_the_name(client):
    payload = client.get("/api/branding").get_json()
    assert payload["app_name"] == branding.APP_NAME
    assert payload["app_slug"] == branding.APP_SLUG


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"data-splash" in response.data
    assert b"data-nav" in response.data


def test_static_traversal_is_refused(client):
    response = client.get("/static/../app.py")
    assert response.status_code in (301, 308, 400, 403, 404)


def test_unknown_route_returns_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not_found"


def test_data_directory_uses_the_branding_constant(monkeypatch, tmp_path):
    if sys.platform.startswith("win"):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        expected = tmp_path / branding.DATA_DIR_NAME
    elif sys.platform == "darwin":
        monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
        expected = tmp_path / "Library" / "Application Support" / branding.DATA_DIR_NAME
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        expected = tmp_path / branding.DATA_DIR_NAME_POSIX

    assert paths._resolve() == expected


def test_atomic_write_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "profile.json"
    paths.atomic_write_text(target, '{"username": "test"}')

    assert target.read_text(encoding="utf-8") == '{"username": "test"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_repository_is_pure_ascii():
    assert check_ascii.check_ascii(check_ascii.repository_root()) == []


def test_game_name_appears_only_in_branding_module():
    assert check_ascii.check_name_leak(check_ascii.repository_root()) == []


# -- your snake's colour -----------------------------------------------------


def test_a_solo_snake_is_the_colour_in_the_profile(client):
    """One colour, one place it is set.

    The solo snake used to be a hardcoded green while the profile held a colour
    that only multiplayer ever read, so the same player was two different
    snakes depending on which mode they were in.
    """
    client.post("/api/profile", json={"colour": "#58a6ff"})

    started = client.post("/api/solo/start", json={"rules": {}}).get_json()
    assert started["state"]["snakes"][0]["c"] == "#58a6ff"

    client.post("/api/profile", json={"colour": "#ff9d5c"})

    restarted = client.post("/api/solo/start", json={"rules": {}}).get_json()
    assert restarted["state"]["snakes"][0]["c"] == "#ff9d5c"

    client.post("/api/solo/stop")


def test_a_colour_that_is_not_one_of_the_twelve_is_refused(client):
    before = client.get("/api/profile").get_json()["colour"]

    client.post("/api/profile", json={"colour": "javascript:alert(1)"})

    assert client.get("/api/profile").get_json()["colour"] == before

    started = client.post("/api/solo/start", json={"rules": {}}).get_json()
    assert started["state"]["snakes"][0]["c"] == before

    client.post("/api/solo/stop")


def test_the_engine_still_has_a_colour_when_nobody_passes_one():
    from utils.game.engine import SoloEngine
    from utils.game.rules import RuleSet

    engine = SoloEngine(RuleSet())
    engine.start()

    assert engine.keyframe()["state"]["snakes"][0]["c"] == SoloEngine.DEFAULT_COLOUR
