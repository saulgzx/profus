"""Test end-to-end del endpoint /api/game_state (Fase 6 ciclo 2).

Levanta el WebDashboardServer en un puerto libre, hace GET /api/game_state,
verifica que devuelve el JSON esperado.
"""

import os, sys, json, time, http.client, socket, threading
import pytest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Stubs para módulos no disponibles en sandbox
import types
sys.modules.setdefault('pyautogui', types.ModuleType('pyautogui'))
mss_stub = types.ModuleType('mss')
class _MssStub:
    def mss(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass
mss_stub.mss = _MssStub()
sys.modules.setdefault('mss', mss_stub)

from gui_web import WebDashboardServer
from game_state import GameState


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server_with_provider():
    """Lanza WebServer con GameState mock + game_state_provider."""
    gs = GameState()
    gs.set("pa", 6)
    gs.set("pm", 3)
    gs.set("hp", 100)
    gs.set("max_hp", 200)
    gs.set("current_map_id", 7414)
    gs.set("in_combat", True)

    port = _free_port()
    srv = WebDashboardServer(
        state_provider=lambda: {"smoke": True},
        host="127.0.0.1",
        port=port,
        game_state_provider=lambda: gs.to_dict(include_timestamps=True),
    )
    ok = srv.start()
    assert ok, "server failed to start"
    time.sleep(0.1)
    yield port
    # Cleanup: el server corre en daemon thread, muere con el test


@pytest.fixture
def server_without_provider():
    port = _free_port()
    srv = WebDashboardServer(
        state_provider=lambda: {},
        host="127.0.0.1",
        port=port,
    )
    srv.start()
    time.sleep(0.1)
    yield port


def _http_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read().decode("utf-8")
    conn.close()
    return r.status, body


def test_game_state_endpoint_returns_200(server_with_provider):
    port = server_with_provider
    status, body = _http_get(port, "/api/game_state")
    assert status == 200


def test_game_state_endpoint_payload_has_expected_fields(server_with_provider):
    port = server_with_provider
    _, body = _http_get(port, "/api/game_state")
    data = json.loads(body)
    # Campos del GameState
    assert data["pa"] == 6
    assert data["pm"] == 3
    assert data["hp"] == 100
    assert data["max_hp"] == 200
    assert data["current_map_id"] == 7414
    assert data["in_combat"] is True


def test_game_state_endpoint_includes_age_timestamps(server_with_provider):
    port = server_with_provider
    _, body = _http_get(port, "/api/game_state")
    data = json.loads(body)
    assert "_age_s" in data
    # Debe tener edad de los campos seteados
    assert "pa" in data["_age_s"]
    assert isinstance(data["_age_s"]["pa"], (int, float))
    assert data["_age_s"]["pa"] >= 0


def test_game_state_endpoint_503_without_provider(server_without_provider):
    port = server_without_provider
    status, body = _http_get(port, "/api/game_state")
    assert status == 503
    data = json.loads(body)
    assert "error" in data


def test_game_state_endpoint_504_when_provider_raises():
    """Si el provider raise, el endpoint debe devolver 500 con detail."""
    def bad_provider():
        raise RuntimeError("boom")

    port = _free_port()
    srv = WebDashboardServer(
        state_provider=lambda: {},
        host="127.0.0.1",
        port=port,
        game_state_provider=bad_provider,
    )
    srv.start()
    time.sleep(0.1)
    status, body = _http_get(port, "/api/game_state")
    assert status == 500
    data = json.loads(body)
    assert "error" in data
    assert "boom" in data.get("detail", "")
