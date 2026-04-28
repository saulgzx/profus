"""Tests de integración para handlers de sniff_handlers (Fase 6 ciclo 1).

Cada test:
  1. Construye un FakeBot mínimo con los atributos necesarios + GameState real.
  2. Invoca el handler como lo haría el dispatcher.
  3. Verifica:
     - el atributo viejo (`bot._sniffer_*`, `bot.current_*`, etc.) se actualizó
     - `bot.game_state` también se actualizó (dual write)

Estos tests cubren regresiones críticas: si alguien modifica un handler y
rompe el dual write, falla el test.
"""

import os, sys, time
import pytest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import sniff_handlers
from game_state import GameState


@pytest.fixture(autouse=True)
def import_handlers():
    """sniff_handlers se importa con todos los @register evaluados al cargar.
    Esta fixture es paranoia para asegurar el registry está poblado en cada
    test. Pero dado que reset_registry() limpia, es importante NO usarlo aquí."""
    # No reset — necesitamos los 30 handlers vivos
    yield


def make_fake_bot():
    """Construye un FakeBot con todos los atributos que los handlers tocan."""
    class FakeBot:
        def __init__(self):
            self.game_state = GameState()
            self._sniffer_my_actor = "12345"
            self._sniffer_pa = None
            self._sniffer_pm = None
            self._sniffer_in_placement = False
            self._sniffer_turn_ready = False
            self._sniffer_map_loaded = False
            self.current_pods = None
            self.max_pods = None
            self._fighters = {}
            self._combat_cell = None
            self._char_hp = None
            self._char_max_hp = None
            self.state = "in_combat"
            self.combat_action_until = time.time() + 100
            self._last_gaf_at = 0.0
            self._current_map_id = 7414
            self._last_action_sequence_ready_at = 0.0
            self.combat_deadline = time.time() + 100
            self._death_alert_fight_id = None
            self._sniffer_fight_id = None
        def _actor_ids_match(self, a, b):
            try:
                return int(a) == int(b)
            except (TypeError, ValueError):
                return str(a or "") == str(b or "")

    return FakeBot()


def test_pa_update_dual_write():
    bot = make_fake_bot()
    h = sniff_handlers.build_dispatcher()["pa_update"]
    h(bot, {"actor_id": "12345", "pa": 6, "pm": 3})
    assert bot._sniffer_pa == 6
    assert bot._sniffer_pm == 3
    assert bot.game_state.get("pa") == 6
    assert bot.game_state.get("pm") == 3
    # Timestamps frescos
    assert bot.game_state.age_s("pa") < 1.0
    assert bot.game_state.age_s("pm") < 1.0


def test_pa_update_other_actor_ignored():
    """Si el evento pa_update viene de OTRO actor, no debe mutar nada."""
    bot = make_fake_bot()
    bot._sniffer_pa = 5
    bot.game_state.set("pa", 5, ts=100.0)
    h = sniff_handlers.build_dispatcher()["pa_update"]
    h(bot, {"actor_id": "99999", "pa": 12, "pm": 8})
    assert bot._sniffer_pa == 5  # sin cambios
    assert bot.game_state.get("pa") == 5
    assert bot.game_state.get_timestamp("pa") == 100.0


def test_pods_update_dual_write():
    bot = make_fake_bot()
    h = sniff_handlers.build_dispatcher()["pods_update"]
    h(bot, {"current": 750, "max": 1500})
    assert bot.current_pods == 750
    assert bot.max_pods == 1500
    assert bot.game_state.get("pods_current") == 750
    assert bot.game_state.get("pods_max") == 1500


def test_character_stats_basic():
    bot = make_fake_bot()
    h = sniff_handlers.build_dispatcher()["character_stats"]
    h(bot, {"hp": 800, "max_hp": 1000})
    assert bot._char_hp == 800
    assert bot._char_max_hp == 1000
    assert bot.game_state.get("hp") == 800
    assert bot.game_state.get("max_hp") == 1000


def test_character_stats_handles_invalid_input():
    """Datos inválidos no deben crashear."""
    bot = make_fake_bot()
    h = sniff_handlers.build_dispatcher()["character_stats"]
    # hp/max_hp no parseables
    h(bot, {"hp": "abc", "max_hp": None})
    assert bot._char_hp is None  # sin cambios


def test_turn_end_dual_write():
    bot = make_fake_bot()
    bot._sniffer_turn_ready = True
    bot.game_state.set("is_my_turn", True)
    h = sniff_handlers.build_dispatcher()["turn_end"]
    h(bot, {"actor_id": "12345"})
    assert bot._sniffer_turn_ready is False
    assert bot.game_state.get("is_my_turn") is False


def test_turn_end_other_actor_ignored():
    bot = make_fake_bot()
    bot._sniffer_turn_ready = True
    bot.game_state.set("is_my_turn", True)
    h = sniff_handlers.build_dispatcher()["turn_end"]
    h(bot, {"actor_id": "99999"})  # otro actor
    # Estado mio sin cambios
    assert bot._sniffer_turn_ready is True
    assert bot.game_state.get("is_my_turn") is True


def test_player_profile_self_confirmed():
    bot = make_fake_bot()
    h = sniff_handlers.build_dispatcher()["player_profile"]
    h(bot, {"actor_id": "12345", "name": "Pepito"})
    assert bot.game_state.get("actor_id") == "12345"
    assert bot.game_state.get("character_name") == "Pepito"


def test_game_action_finish_marks_last_gaf():
    bot = make_fake_bot()
    before = time.time()
    h = sniff_handlers.build_dispatcher()["game_action_finish"]
    h(bot, {"actor_id": "12345"})
    assert bot._last_gaf_at >= before
