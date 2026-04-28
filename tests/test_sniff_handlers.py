"""Tests para el dispatcher de sniff_handlers (Fase 2 ciclo 1).

Cubre:
    - register decorator agrega entrada al registry
    - register con nombre duplicado raise
    - register con nombre invalido raise
    - build_dispatcher devuelve copia (mutar el resultado no afecta el registry)
    - registered_events list
    - reset_registry limpia (helper de tests)
"""

import os, sys
import pytest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import sniff_handlers


@pytest.fixture(autouse=True)
def clean_registry():
    """Cada test arranca con registry vacio. Restaura el snapshot original al final
    para no romper otros tests que dependen de los handlers reales."""
    snapshot = dict(sniff_handlers._REGISTRY)
    sniff_handlers.reset_registry()
    yield
    sniff_handlers.reset_registry()
    sniff_handlers._REGISTRY.update(snapshot)


def test_register_adds_handler():
    @sniff_handlers.register("foo_event")
    def h(bot, data): pass

    assert "foo_event" in sniff_handlers.registered_events()
    d = sniff_handlers.build_dispatcher()
    assert "foo_event" in d
    assert d["foo_event"] is h


def test_register_returns_function_unchanged():
    def original(bot, data): return "original"
    decorated = sniff_handlers.register("event_x")(original)
    assert decorated is original


def test_register_duplicate_raises():
    @sniff_handlers.register("dup")
    def h1(bot, data): pass

    with pytest.raises(ValueError, match="ya hay handler"):
        @sniff_handlers.register("dup")
        def h2(bot, data): pass


def test_register_empty_name_raises():
    with pytest.raises(ValueError, match="event_name debe ser str no vacio"):
        sniff_handlers.register("")


def test_register_non_str_name_raises():
    with pytest.raises(ValueError):
        sniff_handlers.register(None)


def test_build_dispatcher_returns_copy():
    @sniff_handlers.register("e1")
    def h(bot, data): pass

    d = sniff_handlers.build_dispatcher()
    d["new_event"] = lambda b, x: None  # no debe afectar registry global

    assert "new_event" not in sniff_handlers.registered_events()


def test_build_dispatcher_empty_registry():
    assert sniff_handlers.build_dispatcher() == {}


def test_registered_events_sorted():
    @sniff_handlers.register("zebra")
    def hz(bot, data): pass
    @sniff_handlers.register("alpha")
    def ha(bot, data): pass
    @sniff_handlers.register("mango")
    def hm(bot, data): pass

    assert sniff_handlers.registered_events() == ["alpha", "mango", "zebra"]


def test_handler_invocation():
    """Verifica que un handler registrado se llama con (bot, data)."""
    captured = {}

    @sniff_handlers.register("capture")
    def h(bot, data):
        captured["bot"] = bot
        captured["data"] = data

    fake_bot = object()
    fake_data = {"key": "value"}
    sniff_handlers.build_dispatcher()["capture"](fake_bot, fake_data)
    assert captured["bot"] is fake_bot
    assert captured["data"] == {"key": "value"}
