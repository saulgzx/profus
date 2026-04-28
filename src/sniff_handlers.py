"""Dispatcher de eventos del sniffer — Fase 2.

Reemplaza progresivamente el switch monolitico de `Bot._handle_sniff_event`
(954 LOC, 30+ branches en bot.py) por un registry de funciones modulares.

Diseno:
    - Cada handler es una funcion `handler(bot, data) -> None` registrada
      con el decorador `@register("event_name")`.
    - El dispatcher se construye una vez al startup (`build_dispatcher()`)
      y se consulta por evento. Si no hay handler registrado, Bot cae al
      switch viejo (modo bypass durante la migracion).
    - Cuando todos los handlers esten migrados (F2.C4), el switch viejo se
      elimina y el dispatcher pasa a fail-fast.

Convencion para cada handler:
    1. Mutar `bot._sniffer_*` / `bot._char_*` / `bot._current_*` / etc.
       para preservar backwards-compat con consumidores que aun leen los
       atributos viejos.
    2. Mutar `bot.game_state` (dual write iniciado en F3.C2/C3).
    3. Loguear via los loggers locales declarados aqui.

Tests: ver `tests/test_sniff_handlers.py`.
"""

from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

from app_logger import get_logger
from map_logic import cell_id_to_grid
from telemetry import get_telemetry

# Espejado de bot.py:38 — para evitar import circular (sniff_handlers no puede 
# importar bot porque bot importa sniff_handlers). Si se cambia en bot.py debe 
# cambiarse aqui tambien. Considerar mover a un modulo de constantes en futuro ciclo.
COMBAT_TIMEOUT = 90.0

if TYPE_CHECKING:
    from bot import Bot

# Loggers locales: usan los mismos namespaces que el monolito viejo.
_log_sniff = get_logger("bot.sniff")
_log_combat = get_logger("bot.combat")
_log = get_logger("bot")
_log_farm = get_logger("bot.farm")

# Type alias.
SniffHandler = Callable[["Bot", dict], None]

_REGISTRY: dict[str, SniffHandler] = {}


# ─── Registry API ───────────────────────────────────────────────────────


def register(event_name: str) -> Callable[[SniffHandler], SniffHandler]:
    """Decorador para registrar un handler de un evento del sniffer."""
    if not isinstance(event_name, str) or not event_name:
        raise ValueError(f"event_name debe ser str no vacio, no {event_name!r}")

    def deco(fn: SniffHandler) -> SniffHandler:
        if event_name in _REGISTRY:
            raise ValueError(
                f"sniff_handlers: ya hay handler para {event_name!r}: "
                f"{_REGISTRY[event_name].__name__}, intento registrar {fn.__name__}"
            )
        _REGISTRY[event_name] = fn
        return fn

    return deco


def build_dispatcher() -> dict[str, SniffHandler]:
    """Snapshot del registry para uso por Bot."""
    return dict(_REGISTRY)


def registered_events() -> list[str]:
    """Lista ordenada de eventos con handler registrado."""
    return sorted(_REGISTRY.keys())


def reset_registry() -> None:
    """Limpia el registry. SOLO PARA TESTS."""
    _REGISTRY.clear()


# ─── Handlers migrados (F2.C2) ──────────────────────────────────────────
# Cada handler replica EXACTAMENTE la branch del switch viejo en bot.py.
# Tests de regresion en tests/test_sniff_handlers.py + el switch viejo
# queda como fallback durante la migracion (unreachable porque el
# dispatcher hace early return en _handle_sniff_event).


@register("pa_update")
def handle_pa_update(bot, data):
    """GA action 129: actualizacion PA/PM del PJ."""
    if bot._actor_ids_match(data["actor_id"], bot._sniffer_my_actor):
        old_pa = bot._sniffer_pa
        bot._sniffer_pa = data["pa"]
        bot._sniffer_pm = data.get("pm")
        bot.game_state.update({"pa": bot._sniffer_pa, "pm": bot._sniffer_pm})
        _log_combat.info(f"[COMBAT] GA129 PJ PA={old_pa}->{data['pa']} PM={data.get('pm')}")


@register("pods_update")
def handle_pods_update(bot, data):
    """Ow: actualizacion de peso/pods."""
    bot.current_pods = data.get("current")
    bot.max_pods = data.get("max")
    bot.game_state.update({"pods_current": bot.current_pods, "pods_max": bot.max_pods})
    pct = (bot.current_pods / max(1, bot.max_pods)) * 100
    _log_sniff.info(f"[SNIFFER] PODS actualizados: {bot.current_pods} / {bot.max_pods} ({pct:.1f}%)")
    if bot.max_pods and bot.max_pods > 0:
        pct = (bot.current_pods / bot.max_pods) * 100
        _log_sniff.info(f"[SNIFFER] PODS actualizados: {bot.current_pods} / {bot.max_pods} ({pct:.1f}%)")
    else:
        _log_sniff.info(f"[SNIFFER] PODS actualizados: {bot.current_pods} / {bot.max_pods}")


@register("player_profile")
def handle_player_profile(bot, data):
    """PM~: hint fuerte del actor propio."""
    actor = str(data.get("actor_id", "")).strip()
    name = str(data.get("name", "")).strip()
    if actor and bot._actor_ids_match(actor, bot._sniffer_my_actor):
        _log_sniff.info(f"[SNIFFER] Actor propio confirmado por PM: {actor} ({name or '?'})")
        bot.game_state.update({"actor_id": actor, "character_name": name or None})
        fighter = bot._fighters.get(actor)
        if bot._combat_cell is None and fighter is not None:
            try:
                bot._combat_cell = int(fighter.get("cell_id"))
                _log_sniff.info(f"[SNIFFER] Mi celda recuperada desde luchador PM: {bot._combat_cell}")
            except (TypeError, ValueError):
                pass


@register("character_stats")
def handle_character_stats(bot, data):
    """As: stats del PJ propio (HP / max HP). Detecta muerte (HP > 0 -> 0)."""
    try:
        hp = int(data.get("hp")) if data.get("hp") is not None else None
        max_hp = int(data.get("max_hp")) if data.get("max_hp") is not None else None
    except (TypeError, ValueError):
        hp, max_hp = None, None
    if hp is not None:
        old_hp = bot._char_hp
        bot._char_hp = hp
        if max_hp is not None:
            bot._char_max_hp = max_hp
        if old_hp != hp:
            _log_sniff.info(f"[SNIFFER] HP propio: {old_hp} -> {hp}/{bot._char_max_hp}")
        bot.game_state.update({"hp": bot._char_hp, "max_hp": bot._char_max_hp})
        # Deteccion de muerte del PJ
        if (
            hp == 0
            and old_hp is not None
            and old_hp > 0
            and bot.state == "in_combat"
        ):
            fight_id = getattr(bot, "_sniffer_fight_id", None)
            last_alert_fight = getattr(bot, "_death_alert_fight_id", None)
            if fight_id is None or fight_id != last_alert_fight:
                bot._death_alert_fight_id = fight_id
                try:
                    get_telemetry().emit(
                        "pj_died",
                        hp_before=old_hp, hp_after=hp,
                        max_hp=bot._char_max_hp,
                        map_id=bot._current_map_id,
                        combat_cell=bot._combat_cell,
                        fight_id=fight_id,
                    )
                    _log_combat.info(
                        f"[COMBAT] 💀 PJ MURIÓ (HP {old_hp}->0 en map "
                        f"{bot._current_map_id} fight {fight_id})"
                    )
                except Exception as _exc:
                    _log_sniff.debug("[bot] except Exception swallowed: %r", _exc)


@register("turn_end")
def handle_turn_end(bot, data):
    """GTF: termino mi turno."""
    if bot._actor_ids_match(data.get("actor_id"), bot._sniffer_my_actor):
        bot._sniffer_turn_ready = False
        bot.game_state.set("is_my_turn", False)
        # Resetear cooldown para reaccionar mas rapido al proximo GTS
        bot.combat_action_until = min(bot.combat_action_until, time.time() + 0.5)


@register("game_action_finish")
def handle_game_action_finish(bot, data):
    """GAF: fin de animacion (move/spell/push) del actor."""
    if bot._actor_ids_match(data.get("actor_id"), bot._sniffer_my_actor):
        bot._last_gaf_at = time.time()

# ─── Batch A — F2.C3 ──────────────────────────────────────────────────

@register("action_sequence_ready")
def handle_action_sequence_ready(bot, data):
    """Migrado de bot.py:1010-1012 (F2.C3)."""
    if bot._actor_ids_match(data.get("actor_id"), bot._sniffer_my_actor):
        bot._last_action_sequence_ready_at = time.time()
        _log_sniff.info(f"[SNIFFER] Servidor listo para mi siguiente accion (secuencia)")

@register("player_ready")
def handle_player_ready(bot, data):
    """Migrado de bot.py:1097-1115 (F2.C3)."""
    raw = str(data.get("raw", "") or "").strip()
    if (
        raw
        and len(raw) > 1
        and raw[0] in {"0", "1"}
        and time.time() <= bot._awaiting_ready_ack_until
    ):
        actor = raw[1:].strip()
        selected_actor = bot._selected_follow_actor_id()
        if (
            bot._is_probable_player_actor(actor)
            and not bot._actor_ids_match(actor, selected_actor)
        ):
            rebound = bot._set_my_actor_id(actor, "player_ready")
            if rebound:
                _log_sniff.info(f"[SNIFFER] Actor propio confirmado por GR: {actor}")
        if bot._actor_ids_match(actor, bot._sniffer_my_actor):
            bot._sniffer_in_placement = False
        bot._awaiting_ready_ack_until = 0.0

@register("placement")
def handle_placement(bot, data):
    """Migrado de bot.py:1118-1125 (F2.C3)."""
    bot._sniffer_in_placement = True
    bot.game_state.set("in_placement", True)
    if bot._placement_probe_until <= time.time():
        bot._arm_placement_probe()
    _log_sniff.info(f"[SNIFFER] Placement raw={str(data.get('raw', ''))[:220]}")
    if bot.state != "in_combat":
        _log_sniff.info("[SNIFFER] Colocacion detectada — entrando a combate")
        bot._enter_combat(time.time())

@register("placement_cells")
def handle_placement_cells(bot, data):
    """Migrado de bot.py:1133-1162 (F2.C3)."""
    teams_parsed: list[list[int]] = []
    for raw_team in data.get("teams", []) or []:
        if not isinstance(raw_team, (list, tuple)):
            continue
        team_ints: list[int] = []
        for rc in raw_team:
            try:
                team_ints.append(int(rc))
            except (TypeError, ValueError):
                continue
        teams_parsed.append(team_ints)
    # Mantener solo los 2 primeros teams (el tercero suele ser []/0 terminador)
    bot._placement_teams = [t for t in teams_parsed[:2] if t]
    resolved = bot._resolve_placement_team_cells()
    if resolved is not None:
        bot._placement_cells = resolved
        _log_sniff.info(f"[SNIFFER] Celdas de placement del equipo (resuelto por combat_cell={bot._combat_cell}): "
              f"{bot._placement_cells}")
    else:
        # Fallback: usar my_team_cells (teams[0]) hasta que llegue GJK/GIC
        cells = []
        for raw_cell in data.get("my_team_cells", []) or []:
            try:
                cells.append(int(raw_cell))
            except (TypeError, ValueError):
                continue
        bot._placement_cells = cells
        _log_sniff.info(f"[SNIFFER] Celdas de placement (provisional, sin combat_cell aún): "
              f"{bot._placement_cells} — teams guardados para resolver tras GJK")
    bot._sniffer_in_placement = True

@register("map_loaded")
def handle_map_loaded(bot, data):
    """Migrado de bot.py:1310-1311 (F2.C3)."""
    if bot.state == "change_map":
        bot._sniffer_map_loaded = True

@register("player_action")
def handle_player_action(bot, data):
    """Migrado de bot.py:1481-1503 (F2.C3)."""
    action_id = str(data.get("action_id", "")).strip()
    seq_id = str(data.get("seq_id", "")).strip()
    should_log_harvest = (
        bot.config["farming"].get("mode", "resource") == "resource"
        and (
            time.time() <= bot._harvest_sniff_debug_until
            or action_id.startswith("500")
            or seq_id == "45"
        )
    )
    if should_log_harvest:
        _log_farm.info(f"[HARVEST] player_action raw={data.get('raw')!r} action_id={data.get('action_id')!r} params={data.get('params')}")
    if (
        bot.config["farming"].get("mode", "resource") == "resource"
        and bot.state in {"wait_first_segar", "spam_segar", "wait_harvest_confirm"}
        and action_id.startswith("500")
        and seq_id == "45"
    ):
        bot._harvest_requested = True
        bot._harvest_request_deadline = time.time() + 2.5
        if bot.state == "wait_first_segar":
            bot.state = "wait_harvest_confirm"
        _log_farm.info(f"[HARVEST] solicitud de cosecha detectada action_id={action_id}")

@register("item_added")
def handle_item_added(bot, data):
    """Migrado de bot.py:1593-1611 (F2.C3)."""
    bot._last_productive_at = time.time()
    bot._stuck_alert_sent = False
    if bot.state == "harvesting_wait" and bot._harvest_confirmed:
        if time.time() < bot._harvest_finish_at + 1.0:
            _log_farm.info(f"[HARVEST] ítem recibido (template={data.get('template_id')} "
                  f"qty={data.get('qty')}) — cortando wait y avanzando.")
            bot._harvest_finish_at = time.time()
    # Auto-learning: solo si el OAK llegó dentro de 30s del harvest
    # confirmado por server (más allá ya es probable que sea otro
    # ítem, p.ej. drop de combate).
    if (
        bot._last_harvest_cell is not None
        and time.time() - bot._last_harvest_at < 30.0
    ):
        template_id = data.get("template_id")
        if template_id is not None:
            bot._learn_resource_mapping(bot._last_harvest_cell, template_id, data.get("qty"))
        # Limpiar el tracker: un OAK se atribuye a un único harvest.
        bot._last_harvest_cell = None

@register("job_xp")
def handle_job_xp(bot, data):
    """Migrado de bot.py:1614-1618 (F2.C3)."""
    if bot.state == "harvesting_wait" and bot._harvest_confirmed:
        if time.time() < bot._harvest_finish_at + 1.0:
            _log_farm.info(f"[HARVEST] XP de job_id={data.get('job_id')} recibida "
                  f"({(data.get('raw') or '')[:60]}) — cortando wait y avanzando.")
            bot._harvest_finish_at = time.time()

@register("interactive_state")
def handle_interactive_state(bot, data):
    """Migrado de bot.py:1627-1646 (F2.C3)."""
    map_id = bot._current_map_id
    if map_id is not None:
        map_id_int = int(map_id)
        bot._gdf_received_for_map[map_id_int] = time.time()
        state_dict = bot._interactive_state.setdefault(map_id_int, {})
        changes = []  # para log compacto
        for entry in (data.get("entries") or []):
            cid = entry.get("cell_id")
            st = entry.get("state")
            if cid is None or st is None:
                continue
            cid_i = int(cid); st_i = int(st)
            prev = state_dict.get(cid_i)
            state_dict[cid_i] = st_i
            if prev != st_i:
                changes.append(f"cell {cid_i}: {prev}→{st_i}")
        if changes and bot.config.get("farming", {}).get("mode", "resource") == "resource":
            # Log compacto: muestra solo cambios reales para no spamear
            _log_farm.info(f"[FARM] GDF map={map_id_int} ({len(changes)} cambios): {', '.join(changes[:8])}"
                  + (f" ...+{len(changes)-8} más" if len(changes) > 8 else ""))

@register("actor_snapshot")
def handle_actor_snapshot(bot, data):
    """Migrado de bot.py:1731-1734 (F2.C3)."""
    if time.time() <= bot._castigo_osado_pending_until:
        bot._castigo_osado_active = True
        bot._castigo_osado_pending_until = 0.0
        _log_sniff.info(f"[SNIFFER] Castigo Osado confirmado por protocolo (As) raw={str(data.get('raw', ''))[:140]}")

@register("spell_cooldown")
def handle_spell_cooldown(bot, data):
    """Migrado de bot.py:1737-1752 (F2.C3)."""
    spell_id = data.get("spell_id")
    cooldown = data.get("cooldown")
    if spell_id is None or cooldown is None:
        return
    try:
        spell_id = int(spell_id)
        cooldown = max(0, int(cooldown))
    except (TypeError, ValueError):
        return
    bot._spell_cooldowns[spell_id] = cooldown
    if spell_id == 433:
        if cooldown > 0:
            _log_sniff.info(f"[SNIFFER] Castigo Osado en cooldown: {cooldown}")
        else:
            bot._castigo_osado_active = False
            _log_sniff.info("[SNIFFER] Castigo Osado disponible")

@register("zaap_list")
def handle_zaap_list(bot, data):
    """Migrado de bot.py:1755-1756 (F2.C3)."""
    raw = str(data.get("raw", "")).strip()
    _log_sniff.info(f"[SNIFFER] Menú de Zaap/Zaapi detectado. Destinos: {raw}")

@register("info_msg")
def handle_info_msg(bot, data):
    """Migrado de bot.py:1759-1780 (F2.C3)."""
    msg_id = data.get("msg_id")
    args = data.get("args", "")

    if msg_id == "021" and args:
        _log_farm.info(f"[FARMING] ¡Objeto recolectado/recibido! -> {args}")
    elif msg_id == "112":
        _log.info("[BOT] ⚠️ Inventario LLENO (100% PODS detectado por el juego).")
        if bot.state not in {"in_combat", "full_pods", "change_map", "wait_harvest_confirm", "harvesting_wait"} and not bot.state.startswith("unloading_"):
            pods_threshold = int(bot.config.get("bot", {}).get("bank_unload_pods_threshold", 1800) or 1800)
            if bot.config.get("bot", {}).get("enable_bank_unload", False) and (bot.current_pods is None or bot.current_pods >= pods_threshold):
                _log.info("[BOT] Iniciando descarga en banco por inventario lleno.")
                bot.state = "unloading_start"
            else:
                bot.state = "full_pods"
            bot.pending = []
            bot.mob_pending = []

    if (
        bot.config["farming"].get("mode", "resource") == "resource"
        and time.time() <= bot._harvest_sniff_debug_until
    ):
        _log_farm.info(f"[HARVEST] info_msg id={data.get('msg_id')!r} args={data.get('args')!r} raw={data.get('raw')!r}")

# ─── Batch B — F2.C3 (medianos: turn_start, fight, map) ─────────────

@register("turn_start")
def handle_turn_start(bot, data):
    """Migrado de bot.py:903-960 (F2.C3)."""
    actor = data["actor_id"]
    bot.combat_deadline = time.time() + COMBAT_TIMEOUT
    bot._maybe_rebind_actor_id(actor, "turn_start")
    # Si aún no conocemos nuestro actor_id, lo aprendemos en el primer
    # turno que coincida con lo que el template matching ya confirmó.
    if bot._sniffer_my_actor is None:
        # Guardar candidato; se confirma cuando el template matching diga "mi turno"
        if bot._sniffer and hasattr(bot._sniffer, "_candidate_actor_id"):
            bot._sniffer_my_actor = bot._sniffer._candidate_actor_id
        else:
            bot._sniffer_my_actor = actor
        _log_sniff.info(f"[SNIFFER] Actor ID aprendido: {bot._sniffer_my_actor}")

    if bot._actor_ids_match(actor, bot._sniffer_my_actor):
        bot._seen_explicit_turn_start = True
        bot._sniffer_turn_ready = True
        bot._manual_turn_notified = False
        bot._sniffer_in_placement = False
        bot._placement_ready_sent = False
        bot._combat_auto_ready_pending = False
        bot._combat_turn_number += 1
        bot.game_state.update({"is_my_turn": True, "in_placement": False, "combat_turn_number": bot._combat_turn_number})
        # Si es el primer turno de la pelea, limpiar cooldowns residuales.
        # Paquetes SC de la pelea anterior pueden llegar a la cola DESPUÉS de
        # que _enter_combat limpió _spell_cooldowns, contaminando la nueva pelea.
        if bot._combat_turn_number == 1:
            bot._spell_cooldowns = {}
        # Resetear PA/PM: el GTM de inicio de turno llegará en ~100ms con los valores restaurados.
        # Sin esto, on_turn puede leer el PA=0 del final del turno anterior.
        # Guardar el valor ANTES del reset: si GTM llegó antes que GTS (p.ej. forma árbol),
        # este valor contiene el PA real del nuevo turno y no debe perderse.
        bot._sniffer_pa_pre_gts = bot._sniffer_pa
        bot._sniffer_pa = None
        bot._sniffer_pm = None
        for spell_id, cooldown in list(bot._spell_cooldowns.items()):
            next_cooldown = max(0, int(cooldown) - 1)
            bot._spell_cooldowns[spell_id] = next_cooldown
            if spell_id == 433 and next_cooldown == 0 and bot._castigo_osado_active:
                bot._castigo_osado_active = False
                _log_sniff.info("[SNIFFER] Castigo Osado vuelve a estar disponible")
        _log_sniff.info(f"[SNIFFER] Es nuestro turno (actor {actor}) turno={bot._combat_turn_number}")
        try:
            tel = get_telemetry()
            tel.set_turn(bot._combat_turn_number)
            tel.emit(
                "turn_start",
                actor=actor,
                my_cell=bot._combat_cell,
                pa_pre_gts=bot._sniffer_pa_pre_gts,
            )
        except Exception as _exc:
            _log.debug("[bot] except Exception swallowed: %r", _exc)
        if bot._combat_cell is None:
            _log_sniff.info("[SNIFFER] Aviso: nuestro turno llego pero aun no hay combat_cell")
        # Si no estábamos en combate, entrar ahora
        if bot.state != "in_combat":
            _log_sniff.info("[SNIFFER] GTS propio fuera de combate — entrando")
            bot._enter_combat(time.time(), preserve_turn_ready=True)

@register("fight_end")
def handle_fight_end(bot, data):
    """Migrado de bot.py:963-1007 (F2.C3)."""
    bot.game_state.reset_combat()
    _log_sniff.info("[SNIFFER] GE recibido — combate terminado")
    try:
        hook = getattr(bot.combat_profile, "on_fight_end", None)
        if callable(hook):
            hook()
    except Exception as exc:
        _log_sniff.info(f"[SNIFFER][ERROR] on_fight_end del perfil fallo: {exc!r}")
    try:
        tel = get_telemetry()
        tel.emit("fight_end_packet", raw=str(data.get("raw", ""))[:200])
        tel.end_fight(reason="GE_packet")
    except Exception as _exc:
        _log.debug("[bot] except Exception swallowed: %r", _exc)
    bot._sniffer_fight_id = None
    bot._sniffer_fight_ended = True
    bot._sniffer_turn_ready  = False
    bot._sniffer_pa          = None
    bot._sniffer_pm          = None
    bot._combat_cell         = None
    bot._last_refined_self_pos = None
    bot._last_refined_cell = None
    bot._sniffer_in_placement = False
    bot._placement_ready_sent = False
    bot._awaiting_ready_ack_until = 0.0
    bot._combat_auto_ready_pending = False
    bot._combat_auto_ready_at = 0.0
    bot._combat_entered_at = 0.0
    bot._placement_auto_attempted = False
    bot._placement_cells = []
    bot._placement_teams = []
    # Modo "route": tras combate, reiniciar cronómetro de pausa para
    # que la pausa de pause_s vuelva a contar desde 0 en este mapa.
    bot._route_seq_arrived_at = 0.0
    bot._castigo_osado_active = False
    bot._castigo_osado_pending_until = 0.0
    bot._spell_cooldowns = {}
    bot._last_spell_server_confirm_at = 0.0
    bot._last_spell_server_confirm = {"spell_id": None, "cell_id": None}
    bot._last_action_sequence_ready_at = 0.0
    bot._last_gaf_at = 0.0
    bot._fighters = {}
    bot._my_team_id = None
    bot._map_entities = {}
    bot.mob_pending = []

@register("fight_join")
def handle_fight_join(bot, data):
    """Migrado de bot.py:1166-1202 (F2.C3)."""
    bot._last_productive_at = time.time()
    bot._stuck_alert_sent = False
    actor   = data.get("actor_id", "")
    team_id = data.get("team_id", "")
    cell_id = data.get("cell_id")
    fight_id_pkt = data.get("fight_id")
    if fight_id_pkt:
        bot._sniffer_fight_id = str(fight_id_pkt)
    if bot._placement_probe_until <= time.time() and bot._combat_turn_number == 0:
        bot._arm_placement_probe()
    _log_sniff.info(f"[SNIFFER] FightJoin raw={str(data.get('raw', ''))[:220]}")
    # Almacenar todos los luchadores (ID negativo = monstruo en Dofus Retro)
    if actor:
        bot._fighters[actor] = {
            "cell_id": cell_id,
            "team_id": team_id,
            "alive":   True,
            "hp":      None,
        }
        bot._maybe_rebind_actor_id(actor, "fight_join")

    if bot._actor_ids_match(actor, bot._sniffer_my_actor):
        if cell_id is not None:
            bot._combat_cell = cell_id
            screen_pos = bot._cell_to_screen(cell_id)
            _log_sniff.info(f"[SNIFFER] Mi celda inicial: {cell_id} → pantalla {screen_pos}")
            bot._reconcile_placement_cells_after_combat_cell_change("fight_join")
        if team_id:
            bot._my_team_id = team_id
            _log_sniff.info(f"[SNIFFER] Mi equipo: {team_id}")
    elif cell_id is not None:
        _log_sniff.info(
            f"[SNIFFER] GJK luchador: actor={actor} team={team_id} cell={cell_id}"
        )
    if bot.state != "in_combat":
        _log_sniff.info("[SNIFFER] GJK — entrando a combate")
        bot._enter_combat(time.time())

@register("combatant_cell")
def handle_combatant_cell(bot, data):
    """Migrado de bot.py:1213-1257 (F2.C3)."""
    actor = data.get("actor_id")
    cell_id = data.get("cell_id")
    updated_entry = None
    bot._remember_recent_actor_cell(actor, cell_id, source=str(data.get("source", "") or "combatant_cell"))
    if actor and cell_id is not None:
        actor_key = str(actor).strip()
        map_entry = bot._map_entities.get(actor_key)
        if map_entry is not None:
            map_entry["cell_id"] = cell_id
            try:
                map_entry["grid_xy"] = cell_id_to_grid(int(cell_id))
            except (TypeError, ValueError):
                pass
            map_entry["last_seen_at"] = time.time()
            bot._map_entities[actor_key] = map_entry
            updated_entry = map_entry
    if bot.state != "in_combat":
        if actor and updated_entry is not None:
            selected_actor = bot._selected_follow_actor_id()
            if selected_actor and str(actor).strip() == selected_actor:
                _log.info(
                    f"[BOT] Movimiento trackeado actor={selected_actor} "
                    f"cell={updated_entry.get('cell_id')} grid={updated_entry.get('grid_xy')}"
                )
            bot._maybe_follow_selected_player_event(str(actor).strip(), updated_entry, "combatant_cell")
            bot._maybe_follow_tracked_players_on_event()
        return
    # Actualizar posición de cualquier luchador
    if actor and cell_id is not None:
        if actor in bot._fighters:
            old_cell = bot._fighters[actor].get("cell_id")
            bot._fighters[actor]["cell_id"] = cell_id
            if old_cell != cell_id and not bot._actor_ids_match(actor, bot._sniffer_my_actor):
                is_enemy = bot._fighters[actor].get("team_id") != bot._my_team_id or bot._my_team_id is None
                hp = bot._fighters[actor].get("hp")
                label = "ENEMIGO" if is_enemy else "aliado"
                _log_combat.info(f"[COMBAT] {label} actor={actor} movió cell={old_cell}->{cell_id} HP={hp}")
        else:
            bot._fighters[actor] = {"cell_id": cell_id, "team_id": None, "alive": True, "hp": None}
    if bot._actor_ids_match(actor, bot._sniffer_my_actor) and cell_id is not None:
        old_cell = bot._combat_cell
        bot._combat_cell = cell_id
        screen_pos = bot._cell_to_screen(cell_id)
        if old_cell != cell_id:
            _log_combat.info(f"[COMBAT] PJ movió cell={old_cell}->{cell_id} pos={screen_pos}")

@register("map_data")
def handle_map_data(bot, data):
    """Migrado de bot.py:1260-1307 (F2.C3)."""
    bot._last_map_id = bot._current_map_id
    bot._current_map_id = data.get("map_id")
    bot.game_state.update({"current_map_id": bot._current_map_id, "map_data_raw": data.get("map_data")})
    bot._ensure_visual_grid_base_for_map(bot._current_map_id)
    bot._current_map_data, bot._current_map_cells = bot._load_map_cells_for_map_id(
        bot._current_map_id,
        map_data=data.get("map_data"),
    )
    if bot._current_map_id != bot._last_map_id:
        # Watchdog: cambiar de mapa = actividad productiva
        bot._last_productive_at = time.time()
        bot._stuck_alert_sent = False
        bot._map_entities = {}
        bot._mob_click_last = {}
        bot._nav_click_last = {}
        bot._follow_player_last_seen_sig = {}
        bot._follow_player_pending = None
        bot._follow_player_wait_until = 0.0
        # Modo "route": cambio de mapa resetea cronómetro de pausa.
        # _route_step() volverá a marcar arrival_at en su próxima tick
        # si el mapa nuevo coincide con el esperado por la secuencia.
        bot._route_seq_arrived_at = 0.0
        # Smart farming: invalidar el cache de interactive_state para
        # el mapa nuevo. El próximo GDF lo va a popular con info fresca.
        # Sin esto, arrastraríamos cells con state=3 (agotado) del mapa
        # anterior a éste y el bot no scanearía bien al re-entrar.
        if bot._current_map_id is not None:
            new_id = int(bot._current_map_id)
            bot._interactive_state.pop(new_id, None)
            if hasattr(bot, "_gdf_received_for_map"):
                bot._gdf_received_for_map.pop(new_id, None)

    interactives = [c["cell_id"] for c in bot._current_map_cells if c.get("is_interactive_cell")]
    teleports = [c["cell_id"] for c in bot._current_map_cells if c.get("has_teleport_texture")]
    _log_combat.debug(
        f"[DIAG] map id={bot._current_map_id} name={data.get('map_name')!r} "
        f"| interactivas={interactives} | teleports={teleports}"
    )
    if (
        bot.state == "change_map"
        and bot._current_map_id is not None
        and bot._current_map_id != bot._last_map_id
    ):
        bot._sniffer_map_loaded = True

    # Análisis de deformación: alerta GUI + auto-pausa cuando hay mapa nuevo deformado sin override
    if bot._current_map_id is not None and bot._current_map_id != bot._last_map_id:
        bot._maybe_alert_map_deformation(bot._current_map_id, bot._current_map_cells)

@register("map_actor_batch")
def handle_map_actor_batch(bot, data):
    """Migrado de bot.py:1314-1355 (F2.C3)."""
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if entries:
        now = time.time()
        selected_actor = bot._selected_follow_actor_id()
        selected_entry = None
        selected_removed = False
        for raw_entry in entries:
            actor_id = str(raw_entry.get("actor_id", "")).strip()
            operation = str(raw_entry.get("operation", "")).strip()
            if not actor_id:
                continue
            if operation in {"-", "~"}:
                removed_entry = bot._map_entities.get(actor_id)
                bot._handle_probable_external_fight(removed_entry)
                bot._refine_recent_external_fight_with_removed_actor(actor_id)
                bot._remember_follow_player_entry(actor_id, removed_entry, visible=False)
                bot._map_entities.pop(actor_id, None)
                # Fix 2026-04-23: replicar el mark-dead del handler
                # map_actor (single) acá en el batch. Sin esto, los
                # enemigos eliminados en batch quedaban con alive=True
                # indefinidamente — el bot los seguía targeteando y
                # el dashboard los mostraba como vivos.
                if bot.state == "in_combat" and actor_id in bot._fighters:
                    fighter = bot._fighters[actor_id]
                    if fighter.get("alive", True):
                        fighter["alive"] = False
                        _log_combat.info(f"[COMBAT] actor={actor_id} ELIMINADO (map_actor_batch removed, cell={fighter.get('cell_id')})")
                if actor_id == selected_actor:
                    selected_removed = True
                continue
            entry = dict(raw_entry)
            entry["last_seen_at"] = now
            bot._map_entities[actor_id] = entry
            bot._remember_follow_player_entry(actor_id, entry, visible=True)
            if actor_id == selected_actor:
                selected_entry = entry
        if selected_entry is not None:
            bot._maybe_follow_selected_player_event(selected_actor, selected_entry, "map_actor_batch")
        elif selected_removed and selected_actor:
            bot._maybe_follow_selected_player_event(selected_actor, None, "map_actor_batch_removed")
        else:
            bot._maybe_follow_tracked_players_on_event()

@register("map_actor")
def handle_map_actor(bot, data):
    """Migrado de bot.py:1358-1410 (F2.C3)."""
    actor_id = str(data.get("actor_id", "")).strip()
    if not actor_id:
        return
    now = time.time()
    operation = str(data.get("operation", "")).strip()
    if operation in {"-", "~"}:
        removed_entry = bot._map_entities.get(actor_id)
        bot._handle_probable_external_fight(removed_entry)
        bot._refine_recent_external_fight_with_removed_actor(actor_id)
        bot._remember_follow_player_entry(actor_id, bot._map_entities.get(actor_id), visible=False)
        bot._map_entities.pop(actor_id, None)
        bot._maybe_follow_selected_player_event(actor_id, None, "map_actor_removed")
        # En combate, si el actor removido es un luchador, marcarlo
        # como muerto. El server en Dofus Retro rara vez manda HP<0
        # explícito: los mobs desaparecen del GTM y su actor se
        # retira del mapa. Sin este update, `_get_enemy_targets()`
        # los seguía devolviendo como vivos con el HP cacheado, y
        # el perfil elegía targets ya eliminados (ej: Sadida
        # lanzando Zarza sobre -8 muerto).
        if bot.state == "in_combat" and actor_id in bot._fighters:
            fighter = bot._fighters[actor_id]
            if fighter.get("alive", True):
                fighter["alive"] = False
                _log_combat.info(f"[COMBAT] actor={actor_id} ELIMINADO (map_actor removed, cell={fighter.get('cell_id')})")
    else:
        entry = dict(data)
        entry["last_seen_at"] = now
        bot._map_entities[actor_id] = entry
        bot._remember_recent_actor_cell(actor_id, entry.get("cell_id"), source="map_actor")
        bot._remember_follow_player_entry(actor_id, entry, visible=True)
        bot._maybe_follow_selected_player_event(actor_id, entry, "map_actor")
        if entry.get("entity_kind") == "fight_marker":
            pending = bot._external_fight_pending or {}
            if entry.get("cell_id") is not None:
                try:
                    pending["fight_cell"] = int(entry.get("cell_id"))
                except (TypeError, ValueError):
                    pass
            if "at" not in pending:
                pending["at"] = now
            bot._external_fight_pending = dict(pending)
            owner_actor = str(entry.get("fight_owner_actor_id", "") or "").strip()
            owner_name = str(entry.get("fight_owner_name", "") or "").strip()
            _log_sniff.info(
                f"[SNIFFER] Pelea visible en mapa: actor={actor_id} "
                f"starter_actor={owner_actor or '?'} starter_name={owner_name or '?'} "
                f"cell={entry.get('cell_id')}"
            )
            if bot.state != "in_combat":
                bot._attempt_join_external_fight()
        if entry.get("entity_kind") in {"mob", "mob_group"}:
            bot._schedule_sniffer_mob_attack(f"map_actor:{actor_id}")
    bot._maybe_follow_tracked_players_on_event()

# ─── Batch C — F2.C3 (grandes: raw_packet, game_action, etc) ────────

@register("arena_state")
def handle_arena_state(bot, data):
    """Migrado de bot.py:1413-1478 (F2.C3)."""
    entries = data.get("entries", [])
    cells = sorted(
        entry["cell_id"]
        for entry in entries
        if entry.get("cell_id") is not None
    )
    if cells:
        bot._current_arena_fingerprint = ",".join(str(cell) for cell in cells)
        _log_combat.debug(f"[DIAG] arena fp={bot._current_arena_fingerprint}")
    my_entry = next(
        (
            entry for entry in entries
            if bot._actor_ids_match(entry.get("actor_id"), bot._sniffer_my_actor)
            and entry.get("cell_id") is not None
        ),
        None,
    )
    if my_entry is not None and bot.state == "in_combat":
        gic_cell = int(my_entry["cell_id"])
        if gic_cell != bot._combat_cell:
            old_cell = bot._combat_cell
            bot._combat_cell = gic_cell
            screen_pos = bot._cell_to_screen(gic_cell)
            _log_combat.debug(
                f"[DIAG] gic_cell actor={my_entry.get('actor_id')} "
                f"cell={gic_cell} old={old_cell} pos={screen_pos}"
            )
            bot._reconcile_placement_cells_after_combat_cell_change("gic_cell")
    if entries:
        bot._pending_gic_entries = entries
        _log_combat.debug(f"[DIAG] grid_detect: {len(entries)} gic entries guardadas")
        if bot.state != "in_combat":
            for e in entries:
                aid = str(e.get("actor_id", "")).strip()
                cid = e.get("cell_id")
                if not aid or cid is None or not bot._is_probable_player_actor(aid):
                    continue
                existing = dict(bot._map_entities.get(aid, {}))
                existing.update({
                    "actor_id": aid,
                    "cell_id": int(cid),
                    "direction": str(e.get("direction", "")).strip(),
                    "entity_kind": existing.get("entity_kind", "other") or "other",
                    "operation": "+",
                    "last_seen_at": time.time(),
                    "raw": e.get("raw", ""),
                })
                bot._map_entities[aid] = existing
                bot._remember_follow_player_entry(aid, existing, visible=True)
                bot._maybe_follow_selected_player_event(aid, existing, "gic")
            bot._maybe_follow_tracked_players_on_event()
        # Actualizar posiciones de todos los luchadores desde GIC
        for e in entries:
            aid = e.get("actor_id", "")
            cid = e.get("cell_id")
            if not aid or cid is None:
                continue
            if aid in bot._fighters:
                bot._fighters[aid]["cell_id"] = cid
            else:
                bot._fighters[aid] = {"cell_id": cid, "team_id": None, "alive": True, "hp": None}
            # Extraer nuestra celda si aún no la tenemos
            if bot._actor_ids_match(aid, bot._sniffer_my_actor) and bot._combat_cell is None:
                bot._combat_cell = int(cid)
                _log_sniff.info(f"[SNIFFER] Mi celda extraída de GIC: {bot._combat_cell}")
                bot._reconcile_placement_cells_after_combat_cell_change("gic_loop")

@register("game_action")
def handle_game_action(bot, data):
    """Migrado de bot.py:1506-1582 (F2.C3)."""
    seq_id = str(data.get("seq_id", "")).strip()
    params = data.get("params") or []
    ga_action_id = str(data.get("ga_action_id", "")).strip()
    ga_actor = str(data.get("actor_id", "")).strip()
    ga_params = data.get("action_params") or []
    # Captura completa en combate: loguea todos los GA para análisis de protocolo
    if bot.state == "in_combat" and bot.config["bot"].get("combat_debug_capture", False):
        _log_combat.info(f"[COMBAT_CAPTURE] GA action={ga_action_id} actor={ga_actor} params={ga_params} raw={data.get('raw')!r}")
    if ga_action_id == "300" and bot._actor_ids_match(ga_actor, bot._sniffer_my_actor) and ga_params:

        spell_tokens = [part.strip() for part in str(ga_params[0]).split(",")]
        spell_id = spell_tokens[0] if len(spell_tokens) > 0 else None
        cell_id = spell_tokens[1] if len(spell_tokens) > 1 else None
        try:
            spell_id = int(spell_id) if spell_id not in (None, "") else None
        except (TypeError, ValueError):
            spell_id = None
        try:
            cell_id = int(cell_id) if cell_id not in (None, "") else None
        except (TypeError, ValueError):
            cell_id = None
        bot._last_spell_server_confirm_at = time.time()
        bot._last_spell_server_confirm = {"spell_id": spell_id, "cell_id": cell_id}
        _log_sniff.info(f"[SNIFFER] Lanzamiento confirmado por servidor: spell={spell_id} cell={cell_id}")
    actor = str(params[0]).strip() if len(params) >= 1 else ""
    should_log_harvest = (
        bot.config["farming"].get("mode", "resource") == "resource"
        and (
            time.time() <= bot._harvest_sniff_debug_until
            or seq_id == "501"
            or bot._actor_ids_match(actor, bot._sniffer_my_actor)
        )
    )
    if should_log_harvest:
        _log_farm.info(f"[HARVEST] game_action raw={data.get('raw')!r} action_id={data.get('action_id')!r} params={data.get('params')}")
    # Tracker para auto-learning: cuando el server confirma "harvest
    # iniciado" (GA;501;{actor};{cell},{object_id}), guardamos el cell.
    # Cuando llegue OAK (item_added) podremos vincular interactive_id
    # → template_id del recurso de ese cell automáticamente.
    if (
        ga_action_id == "501"
        and bot._actor_ids_match(ga_actor, bot._sniffer_my_actor)
        and ga_params
    ):
        try:
            _harvest_cell = int(str(ga_params[0]).split(",")[0])
            bot._last_harvest_cell = _harvest_cell
            bot._last_harvest_at = time.time()
            _log_farm.info(f"[FARM] Harvest iniciado por server en cell={_harvest_cell}")
        except (ValueError, IndexError):
            pass
    if (
        bot.config["farming"].get("mode", "resource") == "resource"
        and bot.state in {"wait_first_segar", "spam_segar", "wait_harvest_confirm"}
        and seq_id == "501"
        and bot._actor_ids_match(actor, bot._sniffer_my_actor)
        and (
            bot._harvest_requested
            or time.time() <= bot._harvest_request_deadline
        )
    ):
        # Lookup del tiempo de espera: primero por profesion, luego global
        _profession_for_wait = (
            bot._last_resource_click[0] if bot._last_resource_click else None
        )
        _prof_cfg = (
            bot.config["farming"].get("professions", {}).get(_profession_for_wait, {})
            if _profession_for_wait else {}
        )
        wait_s = float(
            _prof_cfg.get("collect_min_wait",
            bot.config["bot"].get("collect_min_wait", 7.0))
        )
        bot._harvest_confirmed = True
        bot._harvest_finish_at = time.time() + wait_s
        bot.state = "harvesting_wait"
        _log_farm.info(f"[HARVEST] cosecha confirmada ({_profession_for_wait}) — esperando {wait_s:.1f}s")

@register("fighter_stats")
def handle_fighter_stats(bot, data):
    """Migrado de bot.py:1650-1728 (F2.C3)."""
    if bot.state == "in_combat" and bot.config["bot"].get("combat_debug_capture", False):
        _log_combat.info(f"[COMBAT_CAPTURE] fighter_stats entries={data.get('entries')} raw={data.get('raw')!r}")
    for entry in data.get("entries", []):
        actor_id = entry.get("actor_id", "")
        if not actor_id:
            continue
        bot._maybe_rebind_actor_id(actor_id, "fighter_stats")
        hp      = entry.get("hp")
        ap      = entry.get("ap")
        cell_id = entry.get("cell_id")
        is_self = bot._actor_ids_match(actor_id, bot._sniffer_my_actor)
        # Fix 2026-04-23: formato corto `actor_id;1` en GTM = fighter
        # muerto. Confirmado con 7200 ocurrencias en log real. Si el
        # parser detectó `dead=True`, marcar alive=False y saltar el
        # resto de la lógica (no hay HP/cell válidos en el entry).
        # Esta es la vía PRIMARIA por la que el server informa muertes
        # en Dofus Retro — antes la ignorábamos silenciosamente, por
        # eso los enemigos quedaban visibles en el dashboard después
        # de morir.
        if entry.get("dead") and not is_self:
            if actor_id in bot._fighters:
                fighter = bot._fighters[actor_id]
                if fighter.get("alive", True):
                    fighter["alive"] = False
                    fighter["hp"] = 0
                    _log_combat.info(f"[COMBAT] actor={actor_id} ELIMINADO (GTM short-form dead, cell={fighter.get('cell_id')})")
            else:
                # Si no lo teníamos aún (raro, pero robustez): crear ya muerto.
                bot._fighters[actor_id] = {
                    "cell_id": None, "team_id": None, "alive": False, "hp": 0,
                }
            continue
        if actor_id not in bot._fighters:
            bot._fighters[actor_id] = {"cell_id": cell_id, "team_id": None, "alive": True, "hp": hp}
        else:
            if hp is not None:
                old_hp = bot._fighters[actor_id].get("hp")
                bot._fighters[actor_id]["hp"] = hp
                # HP<=0 (no solo <0): algunos servers mandan 0 en vez
                # de negativo al morir. Sin esto, si el GTM trae HP=0
                # justo antes de que el actor desaparezca del mapa,
                # el bot lo seguiría tomando como target vivo.
                # Excluir al PJ: su HP puede quedar 0 un instante sin
                # que "muera" estrictamente (energía, eclip, etc).
                if hp <= 0 and not is_self:
                    bot._fighters[actor_id]["alive"] = False
                    _log_combat.info(f"[COMBAT] actor={actor_id} ELIMINADO (HP={hp})")
                elif not is_self and bot.state == "in_combat":
                    label = "ENEMIGO" if bot._fighters[actor_id].get("team_id") != bot._my_team_id else "aliado"
                    _log_combat.info(f"[COMBAT] GTM {label} actor={actor_id} HP={old_hp}->{hp} cell={cell_id}")
            if cell_id is not None:
                bot._fighters[actor_id]["cell_id"] = cell_id
        # Si es nuestro actor, actualizar PA/PM también
        if is_self and ap is not None:
            old_pa = bot._sniffer_pa
            bot._sniffer_pa = ap
            if bot.state == "in_combat":
                _log_combat.info(f"[COMBAT] GTM PJ PA={old_pa}->{ap}")
        mp = entry.get("mp")
        if is_self and mp is not None:
            bot._sniffer_pm = mp
        if is_self and cell_id is not None:
            old_cell = bot._combat_cell
            bot._combat_cell = int(cell_id)
            if old_cell != bot._combat_cell:
                _log_combat.info(f"[COMBAT] GTM PJ celda={old_cell}->{bot._combat_cell}")
                bot._reconcile_placement_cells_after_combat_cell_change("gtm_pj_cell")
        # Mirror del HP del PJ en _char_hp: As (character_stats) no
        # fire suficientemente seguido durante combate; GTM trae el
        # HP real después de cada daño/cura. Sin este mirror, el
        # dashboard mostraba HP stale (solo actualizaba en turn_start).
        if is_self and hp is not None:
            old_char_hp = bot._char_hp
            if old_char_hp != hp:
                bot._char_hp = hp
                # Print solo si es un cambio material (evita spam si
                # GTM re-emite el mismo valor cada pocos ms)
                if old_char_hp is None or abs((old_char_hp or 0) - hp) > 0:
                    _log_combat.info(f"[COMBAT] GTM PJ HP={old_char_hp}->{hp}")

@register("game_object")
def handle_game_object(bot, data):
    """Migrado de bot.py:1783-1814 (F2.C3)."""
    packet = str(data.get("packet", "") or "").strip()
    raw = str(data.get("raw", "") or "").strip()
    pending = bot._external_fight_pending
    if packet.startswith("Go+P") and not pending:
        now = time.time()
        for item in reversed(bot._recent_removed_mob_groups):
            age = now - float(item.get("at", 0.0) or 0.0)
            if age > 3.0:
                break
            pending = dict(item)
            bot._external_fight_pending = dict(pending)
            break
    if packet.startswith("Go+P") and pending:
        if not str(pending.get("starter_actor_id") or "").strip():
            selected_actor = bot._selected_follow_actor_id()
            if selected_actor:
                bot._promote_selected_follow_actor_fight(selected_actor)
                pending = bot._external_fight_pending or pending
        pending["go_packet"] = packet
        pending["go_raw"] = raw
        pending["go_seen_at"] = time.time()
        pending["fight_marker_kind"] = "go_packet"
        if pending.get("fight_cell") is None and pending.get("mob_cell") is not None:
            pending["fight_cell"] = pending.get("mob_cell")
        bot._external_fight_pending = dict(pending)
        _log_sniff.info(
            f"[SNIFFER] Marcador de pelea visible por protocolo: "
            f"packet={packet} mob_cell={pending.get('mob_cell')} fight_cell={pending.get('fight_cell')} "
            f"starter≈{pending.get('starter_actor_id')}"
        )
        if bot.state != "in_combat":
            bot._attempt_join_external_fight()
