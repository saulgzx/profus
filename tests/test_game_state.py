"""Tests unitarios para GameState (Fase 3 ciclo 1).

Cubren la API que va a usar bot.py / sadida.py / gui.py en proximos ciclos:
    - set/get con timestamps
    - is_stale / age_s
    - update (set en batch)
    - reset_combat
    - to_dict (con y sin timestamps)
    - validacion de field names (typos detectados al instante)
"""

import os
import sys
import time

import pytest


# Hacer que `import game_state` funcione sin instalar el paquete.
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from game_state import GameState


# ── set / get ────────────────────────────────────────────────────────


def test_default_state_is_empty():
    gs = GameState()
    assert gs.pa is None
    assert gs.pm is None
    assert gs.in_combat is False
    assert gs.fighters == {}
    assert gs.actor_id is None


def test_set_updates_value():
    gs = GameState()
    gs.set("pa", 6)
    gs.set("combat_cell", 215)
    assert gs.pa == 6
    assert gs.combat_cell == 215


def test_get_returns_default_for_missing_attribute():
    gs = GameState()
    # Atributo que no existe → default
    assert gs.get("nonexistent_field", default="X") == "X"
    # Atributo que existe pero con valor None
    assert gs.get("pa") is None


def test_set_invalid_field_raises():
    gs = GameState()
    with pytest.raises(AttributeError, match="GameState no tiene el campo"):
        gs.set("typo_field", 42)


def test_set_private_field_raises():
    gs = GameState()
    with pytest.raises(AttributeError, match="campo privado"):
        gs.set("_timestamps", {"x": 1})


# ── timestamps ──────────────────────────────────────────────────────


def test_set_records_timestamp():
    gs = GameState()
    before = time.time()
    gs.set("pa", 6)
    after = time.time()
    ts = gs.get_timestamp("pa")
    assert before <= ts <= after


def test_set_with_explicit_ts():
    gs = GameState()
    gs.set("pa", 6, ts=12345.6789)
    assert gs.get_timestamp("pa") == 12345.6789


def test_get_timestamp_returns_zero_for_unset_field():
    gs = GameState()
    assert gs.get_timestamp("pa") == 0.0


def test_age_s_for_unset_field_is_infinity():
    gs = GameState()
    assert gs.age_s("pa") == float("inf")


def test_age_s_with_explicit_now():
    gs = GameState()
    gs.set("pa", 6, ts=1000.0)
    # `now` explicito permite tests deterministas
    assert gs.age_s("pa", now=1005.0) == pytest.approx(5.0)


def test_is_stale_unset_field_is_stale():
    gs = GameState()
    assert gs.is_stale("pa", max_age_s=10.0) is True


def test_is_stale_recent_field_is_not_stale():
    gs = GameState()
    gs.set("pa", 6, ts=1000.0)
    assert gs.is_stale("pa", max_age_s=10.0, now=1005.0) is False


def test_is_stale_old_field_is_stale():
    gs = GameState()
    gs.set("pa", 6, ts=1000.0)
    assert gs.is_stale("pa", max_age_s=10.0, now=1020.0) is True


# ── update (batch) ──────────────────────────────────────────────────


def test_update_sets_multiple_fields_with_one_timestamp():
    gs = GameState()
    gs.update({"pa": 6, "pm": 3, "combat_cell": 215}, ts=2000.0)
    assert gs.pa == 6
    assert gs.pm == 3
    assert gs.combat_cell == 215
    assert gs.get_timestamp("pa") == 2000.0
    assert gs.get_timestamp("pm") == 2000.0
    assert gs.get_timestamp("combat_cell") == 2000.0


def test_update_with_invalid_field_raises():
    gs = GameState()
    with pytest.raises(AttributeError):
        gs.update({"pa": 6, "typo": 99})


# ── reset_combat ────────────────────────────────────────────────────


def test_reset_combat_clears_combat_state():
    gs = GameState()
    gs.set("in_combat", True)
    gs.set("pa", 6)
    gs.set("pm", 3)
    gs.set("combat_cell", 215)
    gs.combat_turn_number = 4
    gs.fighters = {"player": {"hp": 100}}

    gs.reset_combat()

    assert gs.in_combat is False
    assert gs.pa is None
    assert gs.pm is None
    assert gs.combat_cell is None
    assert gs.combat_turn_number == 0
    assert gs.fighters == {}


def test_reset_combat_does_not_touch_character_or_map():
    gs = GameState()
    gs.set("hp", 100)
    gs.set("current_map_id", 7414)
    gs.set("in_combat", True)

    gs.reset_combat()

    # Personaje y mapa intactos
    assert gs.hp == 100
    assert gs.current_map_id == 7414
    # Combate limpio
    assert gs.in_combat is False


# ── to_dict ─────────────────────────────────────────────────────────


def test_to_dict_returns_public_fields():
    gs = GameState()
    gs.set("pa", 6)
    gs.set("hp", 100)
    d = gs.to_dict()
    assert d["pa"] == 6
    assert d["hp"] == 100
    assert "_timestamps" not in d  # privado por default


def test_to_dict_with_timestamps_includes_ages():
    gs = GameState()
    gs.set("pa", 6, ts=1000.0)
    d = gs.to_dict(include_timestamps=True)
    assert "_age_s" in d
    assert "pa" in d["_age_s"]
    # Edad en segundos respecto a now (no podemos predecirla, pero debe ser >= 0)
    assert d["_age_s"]["pa"] >= 0


def test_to_dict_does_not_include_private_fields():
    gs = GameState()
    gs.set("pa", 6)
    d = gs.to_dict(include_timestamps=True)
    # `_timestamps` interno no debe aparecer como tal (solo `_age_s`)
    assert "_timestamps" not in d


# ── concurrencia / aliasing ─────────────────────────────────────────


def test_independent_instances_dont_share_state():
    gs1 = GameState()
    gs2 = GameState()
    gs1.set("pa", 6)
    gs2.set("pa", 9)
    assert gs1.pa == 6
    assert gs2.pa == 9
    # Importante: el dict de timestamps tampoco se comparte
    assert gs1.get_timestamp("pa") != gs2.get_timestamp("pa") or gs1.pa != gs2.pa


def test_fighters_dict_is_per_instance():
    gs1 = GameState()
    gs2 = GameState()
    gs1.fighters["a"] = {"hp": 100}
    assert "a" not in gs2.fighters
