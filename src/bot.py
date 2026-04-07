import os
import time
import queue
import random
from pathlib import Path
from collections import deque
import pyautogui
import cv2
import numpy as np
from screen import Screen
from detector import Detector
from actions import Actions
from combat import load_profile, CombatContext
from sniffer import DofusSniffer
from grid_detector import IsoGridDetector
from map_logic import cell_id_to_grid, cell_id_to_col_row
from dofus_map_data import decode_map_data, load_map_data_from_xml

MENU_TIMEOUT    = 4.0   # segundos esperando que aparezca Segar tras click
SPAM_INTERVAL   = 0.3   # segundos entre chequeos de Segar durante spam
DONE_CONFIRMS   = 4     # checks consecutivos sin Segar para declarar cosechado
DONE_TIMEOUT    = 25.0  # timeout maximo por recurso (failsafe)
COMBAT_POLL     = 1.0   # segundos entre chequeos de combate
COMBAT_TIMEOUT  = 90.0  # timeout maximo en combate antes de forzar re-scan
FUIR_CONFIRMS   = 3     # checks consecutivos sin Fuir para declarar combate terminado
COMBAT_COOLDOWN = 0.35  # segundos minimos entre acciones (evita doble-trigger tras Space)
MAP_CHANGE_TIMEOUT      = 10.0  # segundos max esperando que aparezca/desaparezca CambioMap
EMPTY_SCANS_BEFORE_MOVE = 3     # scans vacios consecutivos antes de cambiar de mapa
_REFINE_GAME_LEFT = 0.14
_REFINE_GAME_RIGHT = 0.90
_REFINE_GAME_TOP = 0.09
_REFINE_GAME_BOTTOM = 0.70
_REFINE_MIN_RED_DOMINANCE = 1.28
_REFINE_MIN_RED_BLUE_DOMINANCE = 1.45


def _config_delay(config: dict, key: str, default: float) -> float:
    bot_cfg = config.get("bot", {})
    min_key = f"{key}_min"
    max_key = f"{key}_max"
    if min_key in bot_cfg or max_key in bot_cfg:
        try:
            low = float(bot_cfg.get(min_key, bot_cfg.get(key, default)))
            high = float(bot_cfg.get(max_key, bot_cfg.get(key, default)))
        except (TypeError, ValueError):
            return float(bot_cfg.get(key, default) or default)
        if high < low:
            low, high = high, low
        return random.uniform(low, high)
    try:
        return float(bot_cfg.get(key, default) or default)
    except (TypeError, ValueError):
        return default


class Bot:
    def __init__(self, config: dict):
        self.config = config
        monitor_index = config["game"].get("monitor", 2)
        game_roi = config.get("game", {}).get("game_roi")
        self.screen = Screen(config["game"]["window_title"], monitor_index, game_roi=game_roi)
        threshold = config["bot"].get("threshold", 0.55)
        ui_threshold = config["bot"].get("ui_threshold", 0.85)
        self.detector = Detector(threshold=threshold)
        self.ui_detector = Detector(threshold=ui_threshold)
        self.actions = Actions(config["bot"])

        config.setdefault("farming", {})
        self.state = "scan"
        self.pending: list[tuple[str, str, tuple[int, int]]] = []  # (profession, resource, pos)
        self.collected = 0
        self.last_pos: tuple[int, int] | None = None
        self.harvested_positions: list[tuple[int, int]] = []
        self.harvested_until = 0.0
        self._resource_recording_mode = False
        self._harvest_sniff_debug_until = 0.0
        self._last_resource_click: tuple[str, str, tuple[int, int]] | None = None  # (profession, resource, pos)
        self._harvest_requested = False
        self._harvest_confirmed = False
        self._harvest_finish_at = 0.0
        self._harvest_request_deadline = 0.0
        self._harvest_menu_fallback_used = False

        # Estado spam_segar
        self.menu_deadline = 0.0
        self.spam_deadline = 0.0
        self.no_segar_count = 0   # checks consecutivos sin Segar

        # Estado combate
        self.last_combat_check = 0.0
        self.combat_deadline = 0.0
        self.combat_action_until = 0.0  # cooldown tras cada accion de turno
        self.no_fuir_count = 0    # checks consecutivos sin Fuir para confirmar fin de combate
        profile_name = config["bot"].get("combat_profile", "Anutrof")
        self.combat_profile = load_profile(profile_name)
        print(f"[BOT] Perfil de combate: {self.combat_profile.name}")

        # Estado navegacion
        self.empty_scan_count = 0
        self.route_index = 0
        self.map_change_phase = "click"   # "click" | "wait_appear" | "wait_gone"
        self.map_change_deadline = 0.0

        # Estado leveling
        self.mob_pending: list[tuple[str, tuple[int, int]]] = []
        self._combat_origin = self.state  # estado al que volver tras combate
        self.empty_mob_scan_count = 0

        # Sniffer de protocolo
        self._sniff_queue: queue.Queue = queue.Queue()
        self._sniffer: DofusSniffer | None = None
        configured_actor = config["bot"].get("actor_id")
        self._configured_actor_id: str | None = (
            str(configured_actor).strip() if configured_actor not in (None, "") else None
        )
        self._sniffer_my_actor: str | None = self._configured_actor_id
        self._sniffer_turn_ready   = False   # True cuando el sniffer confirmó nuestro turno
        self._sniffer_fight_ended  = False   # True cuando sniffer detectó GE (fin combate)
        self._sniffer_pa: int | None = None  # PA actuales según el sniffer
        self._sniffer_pm: int | None = None  # PM actuales según el sniffer
        self._combat_turn_number   = 0       # nuestros turnos completados en este combate
        self._combat_cell: int | None = None # cell_id actual del PJ en combate
        self._last_refined_self_pos: tuple[int, int] | None = None
        self._last_refined_cell: int | None = None
        self._sniffer_in_placement = False   # True durante fase de colocacion con boton Listo
        self._combat_auto_ready_pending = False  # enviar Space una vez al entrar a combate
        self._combat_auto_ready_at = 0.0
        self._combat_entered_at = 0.0
        self._placement_auto_attempted = False
        self._placement_ready_sent = False
        self._awaiting_ready_ack_until = 0.0
        self._seen_explicit_turn_start = False
        self._current_map_id: int | None = None
        self._last_map_id: int | None = None
        self._current_map_data: str | None = None
        self._current_map_cells: list[dict] | list = []
        self._sniffer_map_loaded = False
        self._map_entities: dict[str, dict] = {}
        self._follow_player_memory: dict[str, dict] = {}
        self._follow_player_pending: dict | None = None
        self._follow_player_wait_until = 0.0
        self._follow_player_last_seen_sig: dict[str, tuple[int | None, int | None]] = {}
        self._follow_player_last_action_sig: tuple | None = None
        self._follow_player_last_action_at = 0.0
        self._recent_actor_cells: dict[str, dict] = {}
        self._recent_removed_mob_groups: deque[dict] = deque(maxlen=12)
        self._external_fight_pending: dict | None = None
        self._recent_sniffer_events: deque[dict] = deque(maxlen=160)
        self._current_arena_fingerprint: str | None = None
        self._last_missing_projection_warn_map_id: int | None = None
        self._combat_probe_until = 0.0
        self._combat_probe_name: str | None = None
        self._placement_probe_until = 0.0
        self._placement_cells: list[int] = []
        self._castigo_osado_active = False
        self._castigo_osado_pending_until = 0.0
        self._spell_cooldowns: dict[int, int] = {}
        self._last_spell_server_confirm_at = 0.0
        self._last_spell_server_confirm = {"spell_id": None, "cell_id": None}
        self._last_action_sequence_ready_at = 0.0
        self._last_enemy_positions_log: tuple | None = None
        self.current_pods: int | None = None
        self.max_pods: int | None = None
        origins = config["bot"].get("cell_calibration", {}).get("map_origins", [])
        start_map_idx = int(config["bot"].get("start_map_idx", 0) or 0)
        max_map_idx = max(0, len(origins) - 1)
        self._current_map_idx: int = max(0, min(start_map_idx, max_map_idx))
        print(f"[BOT] Mapa inicial para calibracion: indice {self._current_map_idx}")

        # Tracking de luchadores en combate (actor_id → {cell_id, team_id, alive, hp})
        # ID negativo = monstruo/enemigo (protocolo Dofus Retro)
        self._fighters: dict[str, dict] = {}
        self._my_team_id: str | None = None

        # Grid detector automático por visión
        self._detected_origin: tuple[float, float] | None = None
        self._pending_gic_entries: list = []
        self._grid_detect_attempts: int = 0
        _slopes = config["bot"].get("cell_calibration", {}).get("slopes", {})
        _map_w  = config["bot"].get("cell_calibration", {}).get("map_width", 15)
        self._grid_detector: IsoGridDetector | None = (
            IsoGridDetector(_slopes, _map_w) if _slopes else None
        )
        if self._grid_detector:
            print("[BOT] IsoGridDetector listo")
        self.test_mode = bool(config.get("bot", {}).get("test_mode", False))
        if self.test_mode:
            print("[BOT] Modo TEST activo - sin clicks ni ataques")
        self._traveling_to_farm_map: str | None = None
        self._mobs_to_activate_on_arrival: str = ""

        if config["bot"].get("sniffer_enabled", False):
            self._start_sniffer()

    def _local_map_xml_dir(self) -> Path:
        configured = (
            self.config.get("bot", {})
            .get("cell_calibration", {})
            .get("local_map_xml_dir")
        )
        if configured:
            return Path(str(configured))
        return Path(r"C:\Users\Alexis\Downloads\Bot-Dofus-1.29.1-master\mapas\mapas")

    def _decoded_cells_to_dicts(self, decoded_cells: list) -> list[dict]:
        return [
            {
                "cell_id": cell.cell_id,
                "x": cell.x,
                "y": cell.y,
                "cell_type": cell.cell_type,
                "raw_cell_type": cell.raw_cell_type,
                "raw_type_label": cell.raw_type_label,
                "type_label": cell.type_label,
                "line_of_sight": cell.line_of_sight,
                "ground_level": cell.ground_level,
                "ground_slope": cell.ground_slope,
                "interactive_object_id": cell.interactive_object_id,
                "layer_object_1_num": cell.layer_object_1_num,
                "layer_object_2_num": cell.layer_object_2_num,
                "has_teleport_texture": cell.has_teleport_texture,
                "is_interactive_cell": cell.is_interactive_cell,
                "is_walkable": cell.is_walkable,
            }
            for cell in decoded_cells
        ]

    def _load_map_cells_for_map_id(self, map_id: int | None, map_data: str | None = None) -> tuple[str | None, list[dict]]:
        if map_id is None:
            return None, []
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15)

        if map_data:
            try:
                decoded_cells = decode_map_data(map_data, map_width=map_width)
                return map_data, self._decoded_cells_to_dicts(decoded_cells)
            except Exception as exc:
                print(f"[BOT] No pude decodificar MAPA_DATA del sniffer para map_id={map_id}: {exc}")

        xml_dir = self._local_map_xml_dir()
        try:
            loaded = load_map_data_from_xml(int(map_id), xml_dir)
        except Exception as exc:
            print(f"[BOT] No pude leer XML local para map_id={map_id}: {exc}")
            return None, []
        if not loaded:
            print(f"[BOT] Sin XML local para map_id={map_id} en {xml_dir}")
            return None, []
        xml_map_data, xml_width, _xml_height = loaded
        try:
            decoded_cells = decode_map_data(xml_map_data, map_width=xml_width or map_width)
            print(f"[BOT] MAPA_DATA cargado desde XML local para map_id={map_id} ({xml_dir})")
            return xml_map_data, self._decoded_cells_to_dicts(decoded_cells)
        except Exception as exc:
            print(f"[BOT] No pude decodificar XML local para map_id={map_id}: {exc}")
            return xml_map_data, []

    def _has_line_of_sight(self, start_cell: int, end_cell: int) -> bool:
        """Chequea si hay línea de visión entre dos celdas usando el algoritmo de Bresenham."""
        if not self._current_map_cells:
            return True  # Fallback optimista si no hay datos del mapa

        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15)
        try:
            start_coords = cell_id_to_grid(start_cell, map_width)
            end_coords = cell_id_to_grid(end_cell, map_width)
        except (TypeError, ValueError):
            return False

        if not start_coords or not end_coords:
            return False

        map_cells_by_grid = {(c['x'], c['y']): c for c in self._current_map_cells if 'x' in c and 'y' in c}
        x1, y1 = start_coords
        x2, y2 = end_coords
        dx = abs(x2 - x1)
        dy = -abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx + dy

        path_cells = []
        while True:
            path_cells.append((x1, y1))
            if x1 == x2 and y1 == y2:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x1 += sx
            if e2 <= dx:
                err += dx
                y1 += sy
        
        # Revisar celdas intermedias en el camino
        for i in range(1, len(path_cells) - 1):
            cell_coords = path_cells[i]
            cell_data = map_cells_by_grid.get(cell_coords)
            if cell_data and not cell_data.get('line_of_sight', True):
                return False  # Obstáculo encontrado
                
        return True

    # ─────────────────────────────────────── sniffer helpers ──
    def _enter_combat(self, now: float, preserve_turn_ready: bool = False):
        """Transición al estado in_combat desde cualquier estado base."""
        _SCAN_STATES = {"scan", "scan_mobs", "click_mob"}
        self._combat_origin = self.state if self.state in _SCAN_STATES else "scan"
        self.pending = []
        self.mob_pending = []
        self.empty_scan_count = 0
        self.empty_mob_scan_count = 0
        self.map_change_phase = "click"
        self._sniffer_map_loaded = False
        self._map_entities = {}
        self.no_fuir_count = 0
        if not preserve_turn_ready:
            self._sniffer_turn_ready = False
        self._sniffer_fight_ended = False
        self._sniffer_pa = None
        self._sniffer_pm = None
        self._sniffer_in_placement = False
        self._combat_auto_ready_pending = True
        self._combat_entered_at = now
        self._placement_auto_attempted = False
        self._placement_ready_sent = False
        self._awaiting_ready_ack_until = 0.0
        self._seen_explicit_turn_start = False
        self._placement_cells = []
        self._follow_player_last_seen_sig = {}
        self._manual_placement_notified = False
        self._manual_turn_notified = False
        if self._combat_auto_ready_at <= now:
            auto_ready_delay = float(self.config["bot"].get("combat_auto_ready_delay", 0.0) or 0.0)
            self._combat_auto_ready_at = now + max(0.0, auto_ready_delay)
        self._current_arena_fingerprint = None
        self._last_refined_self_pos = None
        self._last_refined_cell = None
        self._detected_origin = None
        self._pending_gic_entries = []
        self._grid_detect_attempts = 0
        self._fighters = {}
        self._my_team_id = None
        self._combat_turn_number = 0
        self._castigo_osado_active = False
        self._castigo_osado_pending_until = 0.0
        self._spell_cooldowns = {}
        self.combat_deadline = now + COMBAT_TIMEOUT
        self.state = "in_combat"

    def _finish_map_change(self, reason: str):
        print(f"[BOT] Mapa cargado ({reason}) — escaneando recursos")
        self.harvested_positions = []
        self.empty_scan_count = 0
        self.empty_mob_scan_count = 0
        self.map_change_phase = "click"
        self._sniffer_map_loaded = False
        n_origins = len(self.config["bot"].get("cell_calibration", {}).get("map_origins", []))
        if n_origins > 1:
            self._current_map_idx = (self._current_map_idx + 1) % n_origins
            print(f"[BOT] Origen de mapa actualizado → indice {self._current_map_idx}")
        self._follow_player_pending = None
        self._follow_player_wait_until = 0.0
        self._follow_player_last_seen_sig = {}
        self.state = self._combat_origin

    def _start_sniffer(self):
        self._sniffer = DofusSniffer(self._sniff_queue, debug_mode=False)
        if self._sniffer_my_actor:
            self._sniffer.set_my_actor_id(self._sniffer_my_actor)
        self._sniffer.start()

    def _set_my_actor_id(self, actor_id: str | None, reason: str) -> bool:
        actor = str(actor_id).strip() if actor_id is not None else ""
        if not actor or actor == "0":
            return False
        if self._actor_ids_match(actor, self._sniffer_my_actor):
            return False
        previous = self._sniffer_my_actor
        self._sniffer_my_actor = actor
        if self._sniffer:
            self._sniffer.set_my_actor_id(actor)
        if previous:
            print(f"[SNIFFER] Actor ID actualizado: {previous} -> {actor} ({reason})")
        else:
            print(f"[SNIFFER] Actor ID aprendido: {actor} ({reason})")
        return True

    def _is_probable_player_actor(self, actor_id: str | None) -> bool:
        if actor_id is None:
            return False
        actor = str(actor_id).strip()
        if not actor or not actor.lstrip("+-").isdigit():
            return False
        return int(actor) > 0

    def _configured_follow_player_actor_ids(self) -> set[str]:
        leveling_cfg = self.config.get("leveling", {})
        values = leveling_cfg.get("follow_player_actor_ids", [])
        resolved: set[str] = set()
        for value in values:
            actor_id = str(value).strip()
            if actor_id and actor_id.lstrip("+-").isdigit() and int(actor_id) > 0:
                resolved.add(actor_id)
        follow_db = leveling_cfg.get("follow_player_db", {})
        if isinstance(follow_db, dict):
            for actor_id, payload in follow_db.items():
                actor = str(actor_id).strip()
                if actor and actor.lstrip("+-").isdigit() and int(actor) > 0:
                    enabled = True
                    if isinstance(payload, dict):
                        enabled = bool(payload.get("enabled", True))
                    if enabled:
                        resolved.add(actor)
        selected_actor = str(leveling_cfg.get("follow_player_selected_actor_id", "") or "").strip()
        if selected_actor:
            if selected_actor in resolved:
                return {selected_actor}
            return set()
        return resolved

    def _follow_players_enabled(self) -> bool:
        leveling_cfg = self.config.get("leveling", {})
        return bool(leveling_cfg.get("follow_players_enabled", False)) and bool(self._configured_follow_player_actor_ids())

    def _selected_follow_actor_id(self) -> str:
        return str(self.config.get("leveling", {}).get("follow_player_selected_actor_id", "") or "").strip()

    def _remember_follow_player_entry(self, actor_id: str, entry: dict | None, visible: bool):
        if actor_id not in self._configured_follow_player_actor_ids():
            return
        memory = self._follow_player_memory.setdefault(actor_id, {"actor_id": actor_id})
        previous_cell = memory.get("cell_id")
        previous_visible = bool(memory.get("visible"))
        memory["actor_id"] = actor_id
        memory["map_id"] = self._current_map_id
        memory["visible"] = visible
        memory["updated_at"] = time.time()
        if entry is not None:
            cell_id = entry.get("cell_id")
            if cell_id is not None:
                try:
                    if previous_cell is not None:
                        memory["prev_cell_id"] = int(previous_cell)
                    memory["cell_id"] = int(cell_id)
                except (TypeError, ValueError):
                    pass
            memory["entry"] = dict(entry)
            memory["entity_kind"] = str(entry.get("entity_kind", "")).strip()
            if visible:
                current_cell = memory.get("cell_id")
                if current_cell is not None and (previous_cell != current_cell or not previous_visible):
                    memory["follow_pending"] = True
                memory["last_seen_at"] = time.time()
                memory.pop("removed_at", None)
        else:
            if previous_visible:
                memory["follow_pending"] = True
            memory["removed_at"] = time.time()

    def _known_positive_actor_ids(self) -> list[str]:
        positives: list[str] = []
        for actor_id in self._fighters.keys():
            if self._is_probable_player_actor(actor_id) and actor_id not in positives:
                positives.append(actor_id)
        for entry in self._pending_gic_entries:
            actor_id = str(entry.get("actor_id", "")).strip()
            if self._is_probable_player_actor(actor_id) and actor_id not in positives:
                positives.append(actor_id)
        return positives

    def _maybe_rebind_actor_id(self, candidate: str | None, source: str) -> bool:
        if not self._is_probable_player_actor(candidate):
            return False
        candidate = str(candidate).strip()
        selected_follow_actor = self._selected_follow_actor_id()
        if selected_follow_actor and self._actor_ids_match(candidate, selected_follow_actor):
            return False
        if self._actor_ids_match(candidate, self._sniffer_my_actor):
            return False
        positive_ids = self._known_positive_actor_ids()
        current_known = any(
            self._actor_ids_match(self._sniffer_my_actor, aid) for aid in positive_ids
        )
        candidate_unique = len(positive_ids) == 1 and self._actor_ids_match(candidate, positive_ids[0])
        if self._sniffer_my_actor is None or candidate_unique or not current_known:
            return self._set_my_actor_id(candidate, source)
        return False

    def _arm_ready_actor_ack(self, window_s: float = 2.0):
        self._awaiting_ready_ack_until = time.time() + max(0.5, float(window_s))

    def _stop_sniffer(self):
        if self._sniffer:
            self._sniffer.stop()
            self._sniffer = None

    def shutdown(self):
        self._stop_sniffer()

    def simulate_unload(self):
        print("[BOT] Simulando secuencia de descarga de inventario...")
        self.state = "unloading_start"
        self.pending = []
        self.mob_pending = []

    def set_resource_recording_mode(self, active: bool):
        self._resource_recording_mode = bool(active)
        if not active:
            return
        if self.config["farming"].get("mode", "resource") != "resource":
            return
        if self.state != "in_combat":
            self.pending = []
            self.state = "scan"
            self.menu_deadline = 0.0
            self.spam_deadline = 0.0
            self.no_segar_count = 0
            self._harvest_requested = False
            self._harvest_confirmed = False
            self._harvest_finish_at = 0.0
            self._harvest_request_deadline = 0.0

    @property
    def sniffer_active(self) -> bool:
        return self._sniffer is not None and self._sniffer.active

    def _drain_sniff_queue(self):
        """Procesa todos los eventos pendientes del sniffer. Llamar al inicio de tick()."""
        while True:
            try:
                event, data = self._sniff_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_sniff_event(event, data)

    def _arm_combat_probe(self, name: str, target_pos: tuple[int, int] | None = None):
        self._combat_probe_name = str(name or "probe")
        self._combat_probe_until = time.time() + 4.0
        if self._combat_probe_name == "CastigoOsado":
            self._castigo_osado_pending_until = self._combat_probe_until
        if self._sniffer:
            self._sniffer.debug_mode = True
        print(
            f"[PROBE] activada {self._combat_probe_name} "
            f"turno={self._combat_turn_number} cell={self._combat_cell} target={target_pos}"
        )

    def _arm_placement_probe(self):
        self._placement_probe_until = time.time() + 4.0
        if self._sniffer:
            self._sniffer.debug_mode = True
        print(
            f"[PROBE] activada Placement "
            f"cell={self._combat_cell} team={self._my_team_id} map_id={self._current_map_id}"
        )

    def _maybe_finish_combat_probe(self):
        now = time.time()
        if self._combat_probe_until > 0.0 and now >= self._combat_probe_until:
            self._combat_probe_until = 0.0
            print(f"[PROBE] cerrada {self._combat_probe_name or 'probe'}")
            self._combat_probe_name = None
        if self._placement_probe_until > 0.0 and now >= self._placement_probe_until:
            self._placement_probe_until = 0.0
            print("[PROBE] cerrada Placement")
        if self._sniffer and self._combat_probe_until <= 0.0 and self._placement_probe_until <= 0.0:
            self._sniffer.debug_mode = False

    def _refresh_combat_state_for_profile(self, wait_seconds: float = 0.0) -> dict:
        deadline = time.time() + max(0.0, float(wait_seconds or 0.0))
        while time.time() < deadline:
            if self.sniffer_active:
                self._drain_sniff_queue()
            time.sleep(0.05)
        if self.sniffer_active:
            self._drain_sniff_queue()
        return {
            "current_pa": self._sniffer_pa,
            "current_mp": self._sniffer_pm,
            "turn_ready": self._sniffer_turn_ready,
            "fight_ended": self._sniffer_fight_ended,
            "combat_cell": self._combat_cell,
            "enemies": self._get_enemy_targets(),
            "castigo_osado_active": self._castigo_osado_active,
            "castigo_osado_cooldown": self._spell_cooldowns.get(433, 0),
            "spell_cooldowns": dict(self._spell_cooldowns),
            "last_spell_server_confirm_at": self._last_spell_server_confirm_at,
            "last_spell_server_confirm": dict(self._last_spell_server_confirm),
            "last_action_sequence_ready_at": self._last_action_sequence_ready_at,
        }

    def _actor_ids_match(self, left: str | None, right: str | None) -> bool:
        """Compara actor_id tolerando espacios y diferencias de formato triviales."""
        if left is None or right is None:
            return False
        a = str(left).strip()
        b = str(right).strip()
        if not a or not b:
            return False
        if a == b:
            return True
        if a.lstrip("+-").isdigit() and b.lstrip("+-").isdigit():
            return int(a) == int(b)
        return False

    def _handle_sniff_event(self, event: str, data: dict):
        self._remember_sniffer_event(event, data)
        probe_active = (time.time() <= self._combat_probe_until) or (time.time() <= self._placement_probe_until)
        if probe_active:
            if event == "raw_packet":
                direction = str(data.get("direction", "")).strip()
                raw = str(data.get("data", "")).strip()
                if raw and raw[:3] not in {"cMK", "Gf~", "BN "} and raw != "qpong":
                    print(f"[PROBE] raw {direction} {raw[:220]}")
            elif event in {
                "player_action",
                "game_action",
                "game_object",
                "fighter_stats",
                "info_msg",
                "arena_state",
                "turn_start",
                "turn_end",
                "spell_cooldown",
                "map_actor",
                "map_actor_batch",
                "combatant_cell",
            }:
                print(f"[PROBE] event={event} data={data}")
                if event == "game_object":
                    print(f"[PROBE] game_object packet={data.get('packet')!r} raw={data.get('raw')!r}")
                if event == "map_actor":
                    sprite_type = data.get("sprite_type")
                    sprite_raw = str(data.get("sprite_raw", "") or "").strip()
                    kind = str(data.get("entity_kind", "") or "").strip()
                    if (
                        kind == "fight_marker"
                        or sprite_type in {-1, -2}
                        or sprite_raw.startswith("-1")
                        or sprite_raw.startswith("-2")
                    ):
                        print(
                            f"[PROBE] fight_marker_candidate actor={data.get('actor_id')} "
                            f"cell={data.get('cell_id')} kind={kind} sprite_type={sprite_type} "
                            f"sprite_raw={sprite_raw!r} raw={str(data.get('raw', ''))[:260]}"
                        )
        if event == "turn_start":
            actor = data["actor_id"]
            self.combat_deadline = time.time() + COMBAT_TIMEOUT
            self._maybe_rebind_actor_id(actor, "turn_start")
            # Si aún no conocemos nuestro actor_id, lo aprendemos en el primer
            # turno que coincida con lo que el template matching ya confirmó.
            if self._sniffer_my_actor is None:
                # Guardar candidato; se confirma cuando el template matching diga "mi turno"
                if self._sniffer and hasattr(self._sniffer, "_candidate_actor_id"):
                    self._sniffer_my_actor = self._sniffer._candidate_actor_id
                else:
                    self._sniffer_my_actor = actor
                print(f"[SNIFFER] Actor ID aprendido: {self._sniffer_my_actor}")

            if self._actor_ids_match(actor, self._sniffer_my_actor):
                self._seen_explicit_turn_start = True
                self._sniffer_turn_ready = True
                self._manual_turn_notified = False
                self._sniffer_in_placement = False
                self._placement_ready_sent = False
                self._combat_auto_ready_pending = False
                self._combat_turn_number += 1
                for spell_id, cooldown in list(self._spell_cooldowns.items()):
                    next_cooldown = max(0, int(cooldown) - 1)
                    self._spell_cooldowns[spell_id] = next_cooldown
                    if spell_id == 433 and next_cooldown == 0 and self._castigo_osado_active:
                        self._castigo_osado_active = False
                        print("[SNIFFER] Castigo Osado vuelve a estar disponible")
                print(f"[SNIFFER] Es nuestro turno (actor {actor}) turno={self._combat_turn_number}")
                if self._combat_cell is None:
                    print("[SNIFFER] Aviso: nuestro turno llego pero aun no hay combat_cell")
                # Si no estábamos en combate, entrar ahora
                if self.state != "in_combat":
                    print("[SNIFFER] GTS propio fuera de combate — entrando")
                    self._enter_combat(time.time(), preserve_turn_ready=True)

        elif event == "fight_end":
            print("[SNIFFER] GE recibido — combate terminado")
            self._sniffer_fight_ended = True
            self._sniffer_turn_ready  = False
            self._sniffer_pa          = None
            self._sniffer_pm          = None
            self._combat_cell         = None
            self._last_refined_self_pos = None
            self._last_refined_cell = None
            self._sniffer_in_placement = False
            self._placement_ready_sent = False
            self._awaiting_ready_ack_until = 0.0
            self._combat_auto_ready_pending = False
            self._combat_auto_ready_at = 0.0
            self._combat_entered_at = 0.0
            self._placement_auto_attempted = False
            self._placement_cells = []
            self._castigo_osado_active = False
            self._castigo_osado_pending_until = 0.0
            self._spell_cooldowns = {}
            self._last_spell_server_confirm_at = 0.0
            self._last_spell_server_confirm = {"spell_id": None, "cell_id": None}
            self._last_action_sequence_ready_at = 0.0
            self._fighters = {}
            self._my_team_id = None
            self._map_entities = {}
            self.mob_pending = []

        elif event == "action_sequence_ready":
            if self._actor_ids_match(data.get("actor_id"), self._sniffer_my_actor):
                self._last_action_sequence_ready_at = time.time()
                print(f"[SNIFFER] Servidor listo para mi siguiente accion (secuencia)")

        elif event == "pa_update":
            if self._actor_ids_match(data["actor_id"], self._sniffer_my_actor):
                self._sniffer_pa = data["pa"]
                self._sniffer_pm = data.get("pm")
                print(f"[SNIFFER] PA actualizados: {data['pa']}")

        elif event == "pods_update":
            self.current_pods = data.get("current")
            self.max_pods = data.get("max")
            pct = (self.current_pods / max(1, self.max_pods)) * 100
            print(f"[SNIFFER] PODS actualizados: {self.current_pods} / {self.max_pods} ({pct:.1f}%)")
            if self.max_pods and self.max_pods > 0:
                pct = (self.current_pods / self.max_pods) * 100
                print(f"[SNIFFER] PODS actualizados: {self.current_pods} / {self.max_pods} ({pct:.1f}%)")
            else:
                print(f"[SNIFFER] PODS actualizados: {self.current_pods} / {self.max_pods}")

        elif event == "player_profile":
            actor = str(data.get("actor_id", "")).strip()
            name = str(data.get("name", "")).strip()
            if actor and self._actor_ids_match(actor, self._sniffer_my_actor):
                print(f"[SNIFFER] Actor propio confirmado por PM: {actor} ({name or '?'})")
                fighter = self._fighters.get(actor)
                if self._combat_cell is None and fighter is not None:
                    try:
                        self._combat_cell = int(fighter.get("cell_id"))
                        print(f"[SNIFFER] Mi celda recuperada desde luchador PM: {self._combat_cell}")
                    except (TypeError, ValueError):
                        pass

        elif event == "player_ready":
            raw = str(data.get("raw", "") or "").strip()
            if (
                raw
                and len(raw) > 1
                and raw[0] in {"0", "1"}
                and time.time() <= self._awaiting_ready_ack_until
            ):
                actor = raw[1:].strip()
                selected_actor = self._selected_follow_actor_id()
                if (
                    self._is_probable_player_actor(actor)
                    and not self._actor_ids_match(actor, selected_actor)
                ):
                    rebound = self._set_my_actor_id(actor, "player_ready")
                    if rebound:
                        print(f"[SNIFFER] Actor propio confirmado por GR: {actor}")
                if self._actor_ids_match(actor, self._sniffer_my_actor):
                    self._sniffer_in_placement = False
                self._awaiting_ready_ack_until = 0.0

        elif event == "placement":
            self._sniffer_in_placement = True
            if self._placement_probe_until <= time.time():
                self._arm_placement_probe()
            print(f"[SNIFFER] Placement raw={str(data.get('raw', ''))[:220]}")
            if self.state != "in_combat":
                print("[SNIFFER] Colocacion detectada — entrando a combate")
                self._enter_combat(time.time())

        elif event == "placement_cells":
            cells = []
            for raw_cell in data.get("my_team_cells", []) or []:
                try:
                    cells.append(int(raw_cell))
                except (TypeError, ValueError):
                    continue
            self._placement_cells = cells
            self._sniffer_in_placement = True
            print(f"[SNIFFER] Celdas de placement del equipo: {self._placement_cells}")

        elif event == "fight_join":
            actor   = data.get("actor_id", "")
            team_id = data.get("team_id", "")
            cell_id = data.get("cell_id")
            if self._placement_probe_until <= time.time() and self._combat_turn_number == 0:
                self._arm_placement_probe()
            print(f"[SNIFFER] FightJoin raw={str(data.get('raw', ''))[:220]}")

            # Almacenar todos los luchadores (ID negativo = monstruo en Dofus Retro)
            if actor:
                self._fighters[actor] = {
                    "cell_id": cell_id,
                    "team_id": team_id,
                    "alive":   True,
                    "hp":      None,
                }
                self._maybe_rebind_actor_id(actor, "fight_join")

            if self._actor_ids_match(actor, self._sniffer_my_actor):
                if cell_id is not None:
                    self._combat_cell = cell_id
                    screen_pos = self._cell_to_screen(cell_id)
                    print(f"[SNIFFER] Mi celda inicial: {cell_id} → pantalla {screen_pos}")
                if team_id:
                    self._my_team_id = team_id
                    print(f"[SNIFFER] Mi equipo: {team_id}")
            elif cell_id is not None:
                print(
                    f"[SNIFFER] GJK luchador: actor={actor} team={team_id} cell={cell_id}"
                )
            if self.state != "in_combat":
                print("[SNIFFER] GJK — entrando a combate")
                self._enter_combat(time.time())

        elif event == "turn_end":
            # GTF propio: el servidor confirmó que nuestro turno terminó
            if self._actor_ids_match(data.get("actor_id"), self._sniffer_my_actor):
                self._sniffer_turn_ready = False
                # Resetear cooldown para reaccionar más rápido al próximo GTS
                self.combat_action_until = min(self.combat_action_until, time.time() + 0.5)

        elif event == "combatant_cell":
            actor = data.get("actor_id")
            cell_id = data.get("cell_id")
            updated_entry = None
            self._remember_recent_actor_cell(actor, cell_id, source=str(data.get("source", "") or "combatant_cell"))
            if actor and cell_id is not None:
                actor_key = str(actor).strip()
                map_entry = self._map_entities.get(actor_key)
                if map_entry is not None:
                    map_entry["cell_id"] = cell_id
                    try:
                        map_entry["grid_xy"] = cell_id_to_grid(int(cell_id))
                    except (TypeError, ValueError):
                        pass
                    map_entry["last_seen_at"] = time.time()
                    self._map_entities[actor_key] = map_entry
                    updated_entry = map_entry
            if self.state != "in_combat":
                if actor and updated_entry is not None:
                    selected_actor = self._selected_follow_actor_id()
                    if selected_actor and str(actor).strip() == selected_actor:
                        print(
                            f"[BOT] Movimiento trackeado actor={selected_actor} "
                            f"cell={updated_entry.get('cell_id')} grid={updated_entry.get('grid_xy')}"
                        )
                    self._maybe_follow_selected_player_event(str(actor).strip(), updated_entry, "combatant_cell")
                    self._maybe_follow_tracked_players_on_event()
                return
            # Actualizar posición de cualquier luchador
            if actor and cell_id is not None:
                if actor in self._fighters:
                    self._fighters[actor]["cell_id"] = cell_id
                else:
                    self._fighters[actor] = {"cell_id": cell_id, "team_id": None, "alive": True, "hp": None}
            if self._actor_ids_match(actor, self._sniffer_my_actor) and cell_id is not None:
                old_cell = self._combat_cell
                self._combat_cell = cell_id
                screen_pos = self._cell_to_screen(cell_id)
                print(f"[DIAG] gm_cell actor={actor} cell={cell_id} old={old_cell} pos={screen_pos}")

        elif event == "map_data":
            self._last_map_id = self._current_map_id
            self._current_map_id = data.get("map_id")
            self._ensure_visual_grid_base_for_map(self._current_map_id)
            self._current_map_data, self._current_map_cells = self._load_map_cells_for_map_id(
                self._current_map_id,
                map_data=data.get("map_data"),
            )
            if self._current_map_id != self._last_map_id:
                self._map_entities = {}
                self._follow_player_last_seen_sig = {}
                self._follow_player_pending = None
                self._follow_player_wait_until = 0.0
            
            interactives = [c["cell_id"] for c in self._current_map_cells if c.get("is_interactive_cell")]
            teleports = [c["cell_id"] for c in self._current_map_cells if c.get("has_teleport_texture")]
            print(
                f"[DIAG] map id={self._current_map_id} name={data.get('map_name')!r} "
                f"| interactivas={interactives} | teleports={teleports}"
            )
            if (
                self.state == "change_map"
                and self._current_map_id is not None
                and self._current_map_id != self._last_map_id
            ):
                self._sniffer_map_loaded = True

        elif event == "map_loaded":
            if self.state == "change_map":
                self._sniffer_map_loaded = True

        elif event == "map_actor_batch":
            entries = data.get("entries", []) if isinstance(data, dict) else []
            if entries:
                now = time.time()
                selected_actor = self._selected_follow_actor_id()
                selected_entry = None
                selected_removed = False
                for raw_entry in entries:
                    actor_id = str(raw_entry.get("actor_id", "")).strip()
                    operation = str(raw_entry.get("operation", "")).strip()
                    if not actor_id:
                        continue
                    if operation in {"-", "~"}:
                        removed_entry = self._map_entities.get(actor_id)
                        self._handle_probable_external_fight(removed_entry)
                        self._refine_recent_external_fight_with_removed_actor(actor_id)
                        self._remember_follow_player_entry(actor_id, removed_entry, visible=False)
                        self._map_entities.pop(actor_id, None)
                        if actor_id == selected_actor:
                            selected_removed = True
                        continue
                    entry = dict(raw_entry)
                    entry["last_seen_at"] = now
                    self._map_entities[actor_id] = entry
                    self._remember_follow_player_entry(actor_id, entry, visible=True)
                    if actor_id == selected_actor:
                        selected_entry = entry
                if selected_entry is not None:
                    self._maybe_follow_selected_player_event(selected_actor, selected_entry, "map_actor_batch")
                elif selected_removed and selected_actor:
                    self._maybe_follow_selected_player_event(selected_actor, None, "map_actor_batch_removed")
                else:
                    self._maybe_follow_tracked_players_on_event()

        elif event == "map_actor":
            actor_id = str(data.get("actor_id", "")).strip()
            if not actor_id:
                return
            now = time.time()
            operation = str(data.get("operation", "")).strip()
            if operation in {"-", "~"}:
                removed_entry = self._map_entities.get(actor_id)
                self._handle_probable_external_fight(removed_entry)
                self._refine_recent_external_fight_with_removed_actor(actor_id)
                self._remember_follow_player_entry(actor_id, self._map_entities.get(actor_id), visible=False)
                self._map_entities.pop(actor_id, None)
                self._maybe_follow_selected_player_event(actor_id, None, "map_actor_removed")
            else:
                entry = dict(data)
                entry["last_seen_at"] = now
                self._map_entities[actor_id] = entry
                self._remember_recent_actor_cell(actor_id, entry.get("cell_id"), source="map_actor")
                self._remember_follow_player_entry(actor_id, entry, visible=True)
                self._maybe_follow_selected_player_event(actor_id, entry, "map_actor")
                if entry.get("entity_kind") == "fight_marker":
                    pending = self._external_fight_pending or {}
                    if entry.get("cell_id") is not None:
                        try:
                            pending["fight_cell"] = int(entry.get("cell_id"))
                        except (TypeError, ValueError):
                            pass
                    if "at" not in pending:
                        pending["at"] = now
                    self._external_fight_pending = dict(pending)
                    owner_actor = str(entry.get("fight_owner_actor_id", "") or "").strip()
                    owner_name = str(entry.get("fight_owner_name", "") or "").strip()
                    print(
                        f"[SNIFFER] Pelea visible en mapa: actor={actor_id} "
                        f"starter_actor={owner_actor or '?'} starter_name={owner_name or '?'} "
                        f"cell={entry.get('cell_id')}"
                    )
                    if self.state != "in_combat":
                        self._attempt_join_external_fight()
                if entry.get("entity_kind") in {"mob", "mob_group"}:
                    self._schedule_sniffer_mob_attack(f"map_actor:{actor_id}")
            self._maybe_follow_tracked_players_on_event()

        elif event == "arena_state":
            entries = data.get("entries", [])
            cells = sorted(
                entry["cell_id"]
                for entry in entries
                if entry.get("cell_id") is not None
            )
            if cells:
                self._current_arena_fingerprint = ",".join(str(cell) for cell in cells)
                print(f"[DIAG] arena fp={self._current_arena_fingerprint}")
            my_entry = next(
                (
                    entry for entry in entries
                    if self._actor_ids_match(entry.get("actor_id"), self._sniffer_my_actor)
                    and entry.get("cell_id") is not None
                ),
                None,
            )
            if my_entry is not None and self.state == "in_combat":
                gic_cell = int(my_entry["cell_id"])
                if gic_cell != self._combat_cell:
                    old_cell = self._combat_cell
                    self._combat_cell = gic_cell
                    screen_pos = self._cell_to_screen(gic_cell)
                    print(
                        f"[DIAG] gic_cell actor={my_entry.get('actor_id')} "
                        f"cell={gic_cell} old={old_cell} pos={screen_pos}"
                    )
            if entries:
                self._pending_gic_entries = entries
                print(f"[DIAG] grid_detect: {len(entries)} gic entries guardadas")
                if self.state != "in_combat":
                    for e in entries:
                        aid = str(e.get("actor_id", "")).strip()
                        cid = e.get("cell_id")
                        if not aid or cid is None or not self._is_probable_player_actor(aid):
                            continue
                        existing = dict(self._map_entities.get(aid, {}))
                        existing.update({
                            "actor_id": aid,
                            "cell_id": int(cid),
                            "direction": str(e.get("direction", "")).strip(),
                            "entity_kind": existing.get("entity_kind", "other") or "other",
                            "operation": "+",
                            "last_seen_at": time.time(),
                            "raw": e.get("raw", ""),
                        })
                        self._map_entities[aid] = existing
                        self._remember_follow_player_entry(aid, existing, visible=True)
                        self._maybe_follow_selected_player_event(aid, existing, "gic")
                    self._maybe_follow_tracked_players_on_event()
                # Actualizar posiciones de todos los luchadores desde GIC
                for e in entries:
                    aid = e.get("actor_id", "")
                    cid = e.get("cell_id")
                    if not aid or cid is None:
                        continue
                    if aid in self._fighters:
                        self._fighters[aid]["cell_id"] = cid
                    else:
                        self._fighters[aid] = {"cell_id": cid, "team_id": None, "alive": True, "hp": None}
                    # Extraer nuestra celda si aún no la tenemos
                    if self._actor_ids_match(aid, self._sniffer_my_actor) and self._combat_cell is None:
                        self._combat_cell = int(cid)
                        print(f"[SNIFFER] Mi celda extraída de GIC: {self._combat_cell}")

        elif event == "player_action":
            action_id = str(data.get("action_id", "")).strip()
            seq_id = str(data.get("seq_id", "")).strip()
            should_log_harvest = (
                self.config["farming"].get("mode", "resource") == "resource"
                and (
                    time.time() <= self._harvest_sniff_debug_until
                    or action_id.startswith("500")
                    or seq_id == "45"
                )
            )
            if should_log_harvest:
                print(f"[HARVEST] player_action raw={data.get('raw')!r} action_id={data.get('action_id')!r} params={data.get('params')}")
            if (
                self.config["farming"].get("mode", "resource") == "resource"
                and self.state in {"wait_first_segar", "spam_segar", "wait_harvest_confirm"}
                and action_id.startswith("500")
                and seq_id == "45"
            ):
                self._harvest_requested = True
                self._harvest_request_deadline = time.time() + 2.5
                if self.state == "wait_first_segar":
                    self.state = "wait_harvest_confirm"
                print(f"[HARVEST] solicitud de cosecha detectada action_id={action_id}")

        elif event == "game_action":
            seq_id = str(data.get("seq_id", "")).strip()
            params = data.get("params") or []
            ga_action_id = str(data.get("ga_action_id", "")).strip()
            ga_actor = str(data.get("actor_id", "")).strip()
            ga_params = data.get("action_params") or []
            # Captura completa en combate: loguea todos los GA para análisis de protocolo
            if self.state == "in_combat" and self.config["bot"].get("combat_debug_capture", False):
                print(f"[COMBAT_CAPTURE] GA action={ga_action_id} actor={ga_actor} params={ga_params} raw={data.get('raw')!r}")
            if ga_action_id == "300" and self._actor_ids_match(ga_actor, self._sniffer_my_actor) and ga_params:

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
                self._last_spell_server_confirm_at = time.time()
                self._last_spell_server_confirm = {"spell_id": spell_id, "cell_id": cell_id}
                print(f"[SNIFFER] Lanzamiento confirmado por servidor: spell={spell_id} cell={cell_id}")
            actor = str(params[0]).strip() if len(params) >= 1 else ""
            should_log_harvest = (
                self.config["farming"].get("mode", "resource") == "resource"
                and (
                    time.time() <= self._harvest_sniff_debug_until
                    or seq_id == "501"
                    or self._actor_ids_match(actor, self._sniffer_my_actor)
                )
            )
            if should_log_harvest:
                print(f"[HARVEST] game_action raw={data.get('raw')!r} action_id={data.get('action_id')!r} params={data.get('params')}")
            if (
                self.config["farming"].get("mode", "resource") == "resource"
                and self.state in {"wait_first_segar", "spam_segar", "wait_harvest_confirm"}
                and seq_id == "501"
                and self._actor_ids_match(actor, self._sniffer_my_actor)
                and (
                    self._harvest_requested
                    or time.time() <= self._harvest_request_deadline
                )
            ):
                # Lookup del tiempo de espera: primero por profesion, luego global
                _profession_for_wait = (
                    self._last_resource_click[0] if self._last_resource_click else None
                )
                _prof_cfg = (
                    self.config["farming"].get("professions", {}).get(_profession_for_wait, {})
                    if _profession_for_wait else {}
                )
                wait_s = float(
                    _prof_cfg.get("collect_min_wait",
                    self.config["bot"].get("collect_min_wait", 7.0))
                )
                self._harvest_confirmed = True
                self._harvest_finish_at = time.time() + wait_s
                self.state = "harvesting_wait"
                print(f"[HARVEST] cosecha confirmada ({_profession_for_wait}) — esperando {wait_s:.1f}s")

        elif event == "fighter_stats":
            # GTM — actualizar HP/PA/celda de luchadores tras cada acción
            if self.state == "in_combat" and self.config["bot"].get("combat_debug_capture", False):
                print(f"[COMBAT_CAPTURE] fighter_stats entries={data.get('entries')} raw={data.get('raw')!r}")
            for entry in data.get("entries", []):
                actor_id = entry.get("actor_id", "")
                if not actor_id:
                    continue
                self._maybe_rebind_actor_id(actor_id, "fighter_stats")
                hp      = entry.get("hp")
                ap      = entry.get("ap")
                cell_id = entry.get("cell_id")
                if actor_id not in self._fighters:
                    self._fighters[actor_id] = {"cell_id": cell_id, "team_id": None, "alive": True, "hp": hp}
                else:
                    if hp is not None:
                        self._fighters[actor_id]["hp"] = hp
                        # HP < 0 = eliminado (0 puede ser "sin cambio" en el protocolo Retro)
                        if hp < 0:
                            self._fighters[actor_id]["alive"] = False
                            print(f"[COMBAT] Luchador {actor_id} eliminado (HP={hp})")
                    if cell_id is not None:
                        self._fighters[actor_id]["cell_id"] = cell_id
                # Si es nuestro actor, actualizar PA también
                if self._actor_ids_match(actor_id, self._sniffer_my_actor) and ap is not None:
                    self._sniffer_pa = ap
                mp = entry.get("mp")
                if self._actor_ids_match(actor_id, self._sniffer_my_actor) and mp is not None:
                    self._sniffer_pm = mp
                if self._actor_ids_match(actor_id, self._sniffer_my_actor) and cell_id is not None:
                    old_cell = self._combat_cell
                    self._combat_cell = int(cell_id)
                    if old_cell != self._combat_cell:
                        print(f"[SNIFFER] Mi celda actualizada desde GTM: {old_cell} -> {self._combat_cell}")

        elif event == "actor_snapshot":
            if time.time() <= self._castigo_osado_pending_until:
                self._castigo_osado_active = True
                self._castigo_osado_pending_until = 0.0
                print(f"[SNIFFER] Castigo Osado confirmado por protocolo (As) raw={str(data.get('raw', ''))[:140]}")

        elif event == "spell_cooldown":
            spell_id = data.get("spell_id")
            cooldown = data.get("cooldown")
            if spell_id is None or cooldown is None:
                return
            try:
                spell_id = int(spell_id)
                cooldown = max(0, int(cooldown))
            except (TypeError, ValueError):
                return
            self._spell_cooldowns[spell_id] = cooldown
            if spell_id == 433:
                if cooldown > 0:
                    print(f"[SNIFFER] Castigo Osado en cooldown: {cooldown}")
                else:
                    self._castigo_osado_active = False
                    print("[SNIFFER] Castigo Osado disponible")

        elif event == "zaap_list":
            raw = str(data.get("raw", "")).strip()
            print(f"[SNIFFER] Menú de Zaap/Zaapi detectado. Destinos: {raw}")

        elif event == "info_msg":
            msg_id = data.get("msg_id")
            args = data.get("args", "")

            if msg_id == "021" and args:
                print(f"[FARMING] ¡Objeto recolectado/recibido! -> {args}")
            elif msg_id == "112":
                print("[BOT] ⚠️ Inventario LLENO (100% PODS detectado por el juego).")
                if self.state not in {"in_combat", "full_pods", "change_map", "wait_harvest_confirm", "harvesting_wait"} and not self.state.startswith("unloading_"):
                    if self.config.get("bot", {}).get("enable_bank_unload", False) and (self.current_pods is None or self.current_pods >= 950):
                        print("[BOT] Iniciando descarga en banco por inventario lleno.")
                        self.state = "unloading_start"
                    else:
                        self.state = "full_pods"
                    self.pending = []
                    self.mob_pending = []

            if (
                self.config["farming"].get("mode", "resource") == "resource"
                and time.time() <= self._harvest_sniff_debug_until
            ):
                print(f"[HARVEST] info_msg id={data.get('msg_id')!r} args={data.get('args')!r} raw={data.get('raw')!r}")

        elif event == "game_object":
            packet = str(data.get("packet", "") or "").strip()
            raw = str(data.get("raw", "") or "").strip()
            pending = self._external_fight_pending
            if packet.startswith("Go+P") and not pending:
                now = time.time()
                for item in reversed(self._recent_removed_mob_groups):
                    age = now - float(item.get("at", 0.0) or 0.0)
                    if age > 3.0:
                        break
                    pending = dict(item)
                    self._external_fight_pending = dict(pending)
                    break
            if packet.startswith("Go+P") and pending:
                if not str(pending.get("starter_actor_id") or "").strip():
                    selected_actor = self._selected_follow_actor_id()
                    if selected_actor:
                        self._promote_selected_follow_actor_fight(selected_actor)
                        pending = self._external_fight_pending or pending
                pending["go_packet"] = packet
                pending["go_raw"] = raw
                pending["go_seen_at"] = time.time()
                pending["fight_marker_kind"] = "go_packet"
                if pending.get("fight_cell") is None and pending.get("mob_cell") is not None:
                    pending["fight_cell"] = pending.get("mob_cell")
                self._external_fight_pending = dict(pending)
                print(
                    f"[SNIFFER] Marcador de pelea visible por protocolo: "
                    f"packet={packet} mob_cell={pending.get('mob_cell')} fight_cell={pending.get('fight_cell')} "
                    f"starter≈{pending.get('starter_actor_id')}"
                )
                if self.state != "in_combat":
                    self._attempt_join_external_fight()

    def _is_point_on_monitor(self, pos: tuple[int, int] | None) -> bool:
        if pos is None:
            return False
        mon = self.screen.game_region()
        x, y = pos
        return (
            mon["left"] <= x < mon["left"] + mon["width"]
            and mon["top"] <= y < mon["top"] + mon["height"]
        )

    def _frame_pos_to_screen(
        self,
        pos: tuple[int, int] | None,
        region: dict | None = None,
    ) -> tuple[int, int] | None:
        """Convierte coordenadas relativas al frame capturado en pantalla absoluta."""
        if pos is None:
            return None
        ref = region or self.screen.game_region()
        return (int(ref["left"] + pos[0]), int(ref["top"] + pos[1]))

    def _find_ui_screen(
        self,
        frame,
        element_name: str,
        region: dict | None = None,
    ) -> tuple[int, int] | None:
        return self._frame_pos_to_screen(
            self.ui_detector.find_ui(frame, element_name),
            region=region,
        )

    def _find_ui_screen_rightmost(
        self,
        frame,
        element_name: str,
        region: dict | None = None,
    ) -> tuple[int, int] | None:
        """Busca todas las coincidencias y devuelve la que este mas a la derecha (eje X mayor)."""
        matches = self.ui_detector.find_all(frame, element_name, "ui")
        if not matches:
            return None
        rightmost = max(matches, key=lambda p: p[0])
        return self._frame_pos_to_screen(rightmost, region=region)

    def _has_specific_projection_calibration(self) -> bool:
        cal = self.config["bot"].get("cell_calibration", {})
        origins_by_map_id = cal.get("map_origins_by_map_id", {})
        origins_by_fingerprint = cal.get("map_origins_by_fingerprint", {})
        if self._current_arena_fingerprint and origins_by_fingerprint.get(self._current_arena_fingerprint):
            return True
        if self._current_map_id is not None:
            by_id = origins_by_map_id.get(str(self._current_map_id))
            if by_id is None:
                by_id = origins_by_map_id.get(self._current_map_id)
            if by_id:
                return True
        return False

    def _project_cell_with_origin(
        self,
        cell_id: int,
        origin: dict,
        slopes: dict,
        map_width: int,
    ) -> tuple[int, int]:
        col, row = cell_id_to_col_row(cell_id, map_width)
        x = int(round(origin["x"] + slopes["col_x"] * col + slopes["row_x"] * row))
        y = int(round(origin["y"] + slopes["col_y"] * col + slopes["row_y"] * row))
        return (x, y)

    def _world_map_samples_for_map(self, map_id: int | None) -> list[dict]:
        if map_id is None:
            return []
        cal = self.config.get("bot", {}).get("cell_calibration", {})
        samples_by_map = cal.get("world_map_samples_by_map_id", {})
        samples = samples_by_map.get(str(map_id))
        if samples is None:
            samples = samples_by_map.get(map_id, [])
        if not isinstance(samples, list):
            return []
        return samples

    def _visual_grid_settings_for_map(self, map_id: int | None) -> dict | None:
        if map_id is None:
            return None
        cal = self.config.get("bot", {}).get("cell_calibration", {})
        by_map = cal.get("visual_grid_by_map_id", {}) or {}
        global_base = cal.get("visual_grid_global") or {}
        raw = by_map.get(str(map_id))
        if raw is None:
            raw = by_map.get(map_id)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or not isinstance(global_base, dict):
            return None
        merged = dict(global_base)
        merged.update(raw)
        try:
            return {
                "canvas_width": float(merged.get("canvas_width", 0) or 0),
                "canvas_height": float(merged.get("canvas_height", 0) or 0),
                "cell_width": float(merged.get("cell_width", 0) or 0),
                "cell_height": float(merged.get("cell_height", 0) or 0),
                "offset_x": float(merged.get("offset_x", 0) or 0),
                "offset_y": float(merged.get("offset_y", 0) or 0),
            }
        except (TypeError, ValueError):
            return None

    def _ensure_visual_grid_base_for_map(self, map_id: int | None):
        if map_id is None:
            return
        cal = self.config.setdefault("bot", {}).setdefault("cell_calibration", {})
        global_base = cal.get("visual_grid_global") or {}
        if not isinstance(global_base, dict):
            return
        required = ("cell_width", "cell_height", "offset_x", "offset_y")
        if not all(key in global_base for key in required):
            return
        by_map = cal.setdefault("visual_grid_by_map_id", {})
        key = str(map_id)
        raw = by_map.get(key)
        if raw is None:
            raw = by_map.get(map_id)
        if not isinstance(raw, dict):
            raw = {}
        needs_seed = False
        for field in required:
            value = raw.get(field)
            if value in (None, ""):
                needs_seed = True
                raw[field] = global_base.get(field)
        if raw.get("canvas_width") in (None, "", 0):
            raw["canvas_width"] = int(self.screen.monitor["width"])
            needs_seed = True
        if raw.get("canvas_height") in (None, "", 0):
            raw["canvas_height"] = int(self.screen.monitor["height"])
            needs_seed = True
        if needs_seed:
            by_map[key] = raw

    def _project_cell_with_visual_grid_exact(self, cell_id: int, map_id: int | None) -> tuple[int, int] | None:
        if map_id is None:
            return None
        settings = self._visual_grid_settings_for_map(map_id)
        if not settings:
            return None
        monitor = dict(self.screen.monitor)
        saved_width = float(settings.get("canvas_width", monitor["width"]) or monitor["width"])
        saved_height = float(settings.get("canvas_height", monitor["height"]) or monitor["height"])
        scale_x = float(monitor["width"]) / max(saved_width, 1.0)
        scale_y = float(monitor["height"]) / max(saved_height, 1.0)

        cell_width = float(settings.get("cell_width", 0.0) or 0.0) * scale_x
        cell_height = float(settings.get("cell_height", 0.0) or 0.0) * scale_y
        offset_x = float(settings.get("offset_x", 0.0) or 0.0) * scale_x
        offset_y = float(settings.get("offset_y", 0.0) or 0.0) * scale_y
        if cell_width <= 0 or cell_height <= 0:
            return None

        mid_w = cell_width / 2.0
        mid_h = cell_height / 2.0
        map_cells = self.get_current_map_cells_snapshot()
        map_cell = None
        for item in map_cells:
            try:
                if int(item.get("cell_id")) == int(cell_id):
                    map_cell = item
                    break
            except (TypeError, ValueError, AttributeError):
                continue

        if map_cell is not None:
            grid_x = float(map_cell.get("x", 0.0))
            grid_y = float(map_cell.get("y", 0.0))
        else:
            grid_x, grid_y = cell_id_to_grid(int(cell_id), 15)
            grid_x = float(grid_x)
            grid_y = float(grid_y)

        iso_x = (grid_x - grid_y) * mid_w
        iso_y = (grid_x + grid_y) * mid_h
        center_x = offset_x + iso_x + mid_w
        center_y = offset_y + iso_y + mid_h
        return (
            int(round(monitor["left"] + center_x)),
            int(round(monitor["top"] + center_y)),
        )

    def _project_cell_with_visual_grid(self, cell_id: int, map_id: int | None) -> tuple[int, int] | None:
        settings = self._visual_grid_settings_for_map(map_id)
        if not settings:
            return None
        monitor = dict(self.screen.monitor)
        saved_width = max(settings["canvas_width"], 1.0)
        saved_height = max(settings["canvas_height"], 1.0)
        scale_x = float(monitor["width"]) / saved_width
        scale_y = float(monitor["height"]) / saved_height
        cell_width = settings["cell_width"] * scale_x
        cell_height = settings["cell_height"] * scale_y
        offset_x = settings["offset_x"] * scale_x
        offset_y = settings["offset_y"] * scale_y
        if cell_width <= 0 or cell_height <= 0:
            return None

        cell_meta = None
        for item in self._current_map_cells:
            try:
                if int(item.get("cell_id")) == int(cell_id):
                    cell_meta = item
                    break
            except (TypeError, ValueError, AttributeError):
                continue
        if cell_meta is None:
            return None

        grid_x = float(cell_meta.get("x", 0.0))
        grid_y = float(cell_meta.get("y", 0.0))
        mid_w = cell_width / 2.0
        mid_h = cell_height / 2.0
        iso_x = (grid_x - grid_y) * mid_w
        iso_y = (grid_x + grid_y) * mid_h
        center_x = offset_x + iso_x + mid_w
        center_y = offset_y + iso_y + mid_h
        return (
            int(round(monitor["left"] + center_x)),
            int(round(monitor["top"] + center_y)),
        )

    def _movement_click_pos_for_cell(self, cell_id: int) -> tuple[int, int] | None:
        center = self._cell_to_screen(int(cell_id))
        if center is None:
            return None
        map_id = self._current_map_id
        settings = self._visual_grid_settings_for_map(map_id) if map_id is not None else None
        if settings:
            monitor = dict(self.screen.monitor)
            saved_width = max(float(settings.get("canvas_width", monitor["width"]) or monitor["width"]), 1.0)
            saved_height = max(float(settings.get("canvas_height", monitor["height"]) or monitor["height"]), 1.0)
            scale_y = float(monitor["height"]) / saved_height
            cell_height = float(settings.get("cell_height", 0.0) or 0.0) * scale_y
            if cell_height > 0:
                ground_offset_y = int(round(min(cell_height * 0.62, max(12.0, cell_height * 0.40))))
                return (int(center[0]), int(center[1] + ground_offset_y))
        return (int(center[0]), int(center[1] + 16))

    def _fit_world_map_affine(self, map_id: int | None) -> dict | None:
        samples = self._world_map_samples_for_map(map_id)
        if len(samples) < 3:
            return None
        samples = sorted(
            samples,
            key=lambda sample: float(sample.get("saved_at", 0.0) or 0.0),
            reverse=True,
        )
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 14) or 14)
        rows = []
        xs = []
        ys = []
        seen: set[tuple[int, int]] = set()
        for sample in samples:
            try:
                cell_id = int(sample.get("cell_id"))
                gx, gy = cell_id_to_grid(cell_id, map_width)
                screen_x = float(sample.get("screen_x"))
                screen_y = float(sample.get("screen_y"))
            except (TypeError, ValueError):
                continue
            key = (gx, gy)
            if key in seen:
                continue
            seen.add(key)
            rows.append([1.0, float(gx), float(gy)])
            xs.append(screen_x)
            ys.append(screen_y)
            if len(rows) >= 6:
                break
        if len(rows) < 3:
            return None
        matrix = np.array(rows, dtype=float)
        target_x = np.array(xs, dtype=float)
        target_y = np.array(ys, dtype=float)
        if np.linalg.matrix_rank(matrix) < 3:
            return None
        coef_x, _, _, _ = np.linalg.lstsq(matrix, target_x, rcond=None)
        coef_y, _, _, _ = np.linalg.lstsq(matrix, target_y, rcond=None)
        return {
            "bias_x": float(coef_x[0]),
            "grid_x_x": float(coef_x[1]),
            "grid_y_x": float(coef_x[2]),
            "bias_y": float(coef_y[0]),
            "grid_x_y": float(coef_y[1]),
            "grid_y_y": float(coef_y[2]),
            "sample_count": len(rows),
        }

    def _project_cell_with_affine(
        self,
        cell_id: int,
        affine: dict,
        map_width: int,
    ) -> tuple[int, int]:
        grid_x, grid_y = cell_id_to_grid(cell_id, map_width)
        x = int(round(
            affine["bias_x"]
            + affine["grid_x_x"] * grid_x
            + affine["grid_y_x"] * grid_y
        ))
        y = int(round(
            affine["bias_y"]
            + affine["grid_x_y"] * grid_x
            + affine["grid_y_y"] * grid_y
        ))
        return (x, y)

    def world_map_sample_error(self, map_id: int | None, sample: dict) -> dict | None:
        if map_id is None:
            return None
        affine = self._fit_world_map_affine(map_id)
        if not affine:
            return None
        try:
            cell_id = int(sample.get("cell_id"))
            actual_x = float(sample.get("screen_x"))
            actual_y = float(sample.get("screen_y"))
        except (TypeError, ValueError, AttributeError):
            return None
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 14) or 14)
        projected_x, projected_y = self._project_cell_with_affine(cell_id, affine, map_width)
        dx = float(projected_x) - actual_x
        dy = float(projected_y) - actual_y
        distance = float((dx * dx + dy * dy) ** 0.5)
        return {
            "projected_x": int(projected_x),
            "projected_y": int(projected_y),
            "dx": round(dx, 2),
            "dy": round(dy, 2),
            "distance": round(distance, 2),
        }

    def estimate_map_origin_from_click(
        self,
        cell_id: int,
        click_pos: tuple[int, int],
        map_id: int | None = None,
    ) -> dict | None:
        """Calcula un origen lineal usando una cell conocida y un click real."""
        cal = self.config.get("bot", {}).get("cell_calibration", {})
        slopes = cal.get("slopes")
        if not slopes:
            return None
        try:
            target_cell = int(cell_id)
            click_x = int(click_pos[0])
            click_y = int(click_pos[1])
        except (TypeError, ValueError, IndexError):
            return None

        map_width = int(cal.get("map_width", 14) or 14)
        col, row = cell_id_to_col_row(target_cell, map_width)
        grid_x, grid_y = cell_id_to_grid(target_cell, map_width)
        origin_x = round(click_x - (slopes["col_x"] * col) - (slopes["row_x"] * row), 2)
        origin_y = round(click_y - (slopes["col_y"] * col) - (slopes["row_y"] * row), 2)
        origin = {"x": origin_x, "y": origin_y}
        projected = self._project_cell_with_origin(target_cell, origin, slopes, map_width)
        return {
            "map_id": int(map_id) if map_id is not None else self._current_map_id,
            "cell_id": target_cell,
            "click_pos": (click_x, click_y),
            "origin": origin,
            "projected": projected,
            "col_row": (col, row),
            "grid_xy": (grid_x, grid_y),
        }

    def project_map_entity_to_screen(self, entry: dict | None) -> dict | None:
        if not entry:
            return None
        try:
            cell_id = int(entry.get("cell_id"))
        except (TypeError, ValueError):
            return None
        projected = self._cell_to_screen(cell_id)
        if projected is None:
            return None
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 14) or 14)
        try:
            grid_xy = cell_id_to_grid(cell_id, map_width)
        except (TypeError, ValueError):
            grid_xy = None
        return {
            "map_id": self._current_map_id,
            "actor_id": str(entry.get("actor_id", "")).strip(),
            "cell_id": cell_id,
            "grid_xy": grid_xy,
            "entity_kind": str(entry.get("entity_kind", "")).strip(),
            "screen_pos": (int(projected[0]), int(projected[1])),
        }

    def _cell_to_screen(self, cell_id: int) -> tuple | None:
        """Convierte un cell ID de Dofus Retro a coordenadas de pantalla.

        Usa la fórmula lineal calibrada con 4 puntos reales:
          x = origin_x + slopes.col_x * col + slopes.row_x * row
          y = origin_y + slopes.col_y * col + slopes.row_y * row
        El origen cambia por mapa; _current_map_idx indica cuál usar.
        """
        cal = self.config["bot"].get("cell_calibration", {})
        slopes = cal.get("slopes")
        origins = cal.get("map_origins", [])
        origins_by_map_id = cal.get("map_origins_by_map_id", {})
        origins_by_fingerprint = cal.get("map_origins_by_fingerprint", {})
        if not slopes:
            return None

        if self._current_map_id is not None:
            visual_pos = self._project_cell_with_visual_grid(int(cell_id), self._current_map_id)
            if visual_pos is not None:
                grid_x, grid_y = cell_id_to_grid(cell_id, int(cal.get("map_width", 15) or 15))
                print(
                    f"[DIAG] project source=visual_grid_map_id={self._current_map_id} "
                    f"cell={cell_id} grid=({grid_x},{grid_y}) pos={visual_pos}"
                )
                return visual_pos

        if self.state != "in_combat" and self._current_map_id is not None:
            world_affine = self._fit_world_map_affine(self._current_map_id)
            if world_affine:
                pos = self._project_cell_with_affine(int(cell_id), world_affine, int(cal.get("map_width", 14) or 14))
                grid_x, grid_y = cell_id_to_grid(cell_id, int(cal.get("map_width", 14) or 14))
                print(
                    f"[DIAG] project source=world_affine_map_id={self._current_map_id} "
                    f"cell={cell_id} grid=({grid_x},{grid_y}) "
                    f"samples={world_affine['sample_count']} pos={pos}"
                )
                return pos

        # Prioridad 0: origen detectado automáticamente por IsoGridDetector
        if self.state != "in_combat" and self._current_map_id is not None:
            current_samples = self._world_map_samples_for_map(self._current_map_id)
            if current_samples:
                print(
                    f"[DIAG] project source=world_affine_map_id={self._current_map_id} "
                    f"cell={cell_id} pendiente samples={len(current_samples)}"
                )
                return None
        if self._detected_origin is not None:
            MAP_W_d = cal.get("map_width", 14)
            col_d, row_d = cell_id_to_col_row(cell_id, MAP_W_d)
            ox, oy  = self._detected_origin
            x = int(round(ox + slopes["col_x"] * col_d + slopes["row_x"] * row_d))
            y = int(round(oy + slopes["col_y"] * col_d + slopes["row_y"] * row_d))
            pos = (x, y)
            print(f"[DIAG] project source=detected_origin cell={cell_id} pos={pos}")
            return pos

        MAP_W = cal.get("map_width", 14)
        col, row = cell_id_to_col_row(cell_id, MAP_W)
        grid_x, grid_y = cell_id_to_grid(cell_id, MAP_W)
        origin = None
        origin_label = "none"

        if self._current_arena_fingerprint:
            by_fp = origins_by_fingerprint.get(self._current_arena_fingerprint)
            if by_fp:
                origin = by_fp
                origin_label = f"arena_fp={self._current_arena_fingerprint}"

        if self._current_map_id is not None:
            by_id = origins_by_map_id.get(str(self._current_map_id))
            if by_id is None:
                by_id = origins_by_map_id.get(self._current_map_id)
            if by_id:
                origin = by_id
                origin_label = f"map_id={self._current_map_id}"

        if origin is None:
            if not origins:
                return None
            idx = min(self._current_map_idx, len(origins) - 1)
            origin = origins[idx]
            origin_label = f"map_idx={idx}"

        pos = self._project_cell_with_origin(int(cell_id), origin, slopes, MAP_W)
        print(
            f"[DIAG] project source={origin_label} cell={cell_id} "
            f"col={col} row={row} grid=({grid_x},{grid_y}) "
            f"origin=({origin['x']},{origin['y']}) pos={pos}"
        )
        return pos

    def _find_red_ring_anchor(
        self,
        frame: np.ndarray,
        anchor_pos: tuple[int, int],
        search_radius_x: int = 520,
        search_radius_y: int = 340,
        max_distance: int | None = None,
    ) -> tuple[int, int] | None:
        """Refina una posicion estimada buscando el aro rojo del personaje."""
        mon = self.screen.game_region()
        frame_h, frame_w = frame.shape[:2]
        anchor_x = int(anchor_pos[0] - mon["left"])
        anchor_y = int(anchor_pos[1] - mon["top"])

        x1 = max(0, anchor_x - search_radius_x)
        y1 = max(0, anchor_y - search_radius_y)
        x2 = min(frame_w, anchor_x + search_radius_x)
        y2 = min(frame_h, anchor_y + search_radius_y)
        if x2 - x1 < 20 or y2 - y1 < 20:
            return None

        crop = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

        mask_low = cv2.inRange(hsv, (0, 140, 95), (8, 255, 255))
        mask_high = cv2.inRange(hsv, (172, 140, 95), (180, 255, 255))
        mask = cv2.bitwise_or(mask_low, mask_high)

        # Excluir zonas de UI. Los límites se calculan en coordenadas del frame
        # completo y luego se convierten al crop para que sean consistentes
        # independientemente de dónde esté el anchor.
        frame_game_x1 = int(frame_w * _REFINE_GAME_LEFT)
        frame_game_x2 = int(frame_w * _REFINE_GAME_RIGHT)
        frame_game_y1 = int(frame_h * _REFINE_GAME_TOP)
        frame_game_y2 = int(frame_h * _REFINE_GAME_BOTTOM)
        cg_x1 = max(0, frame_game_x1 - x1)
        cg_x2 = min(x2 - x1, frame_game_x2 - x1)
        cg_y1 = max(0, frame_game_y1 - y1)
        cg_y2 = min(y2 - y1, frame_game_y2 - y1)
        ui_mask = np.zeros_like(mask)
        if cg_x2 > cg_x1 and cg_y2 > cg_y1:
            cv2.rectangle(ui_mask, (cg_x1, cg_y1), (cg_x2, cg_y2), 255, thickness=-1)
        mask = cv2.bitwise_and(mask, ui_mask)

        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            print(f"[DIAG] refine anchor={anchor_pos} best=None")
            return None
        hier = hierarchy[0]

        best_pos = None
        best_score = float("inf")
        best_dist = float("inf")
        for i, contour in enumerate(contours):
            if hier[i][3] != -1:
                continue
            if hier[i][2] == -1:
                continue
            area = cv2.contourArea(contour)
            if area < 120 or area > 18000:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            if w < 16 or h < 8:
                continue

            aspect = w / max(h, 1)
            if not (1.1 <= aspect <= 4.8):
                continue

            hull_area = cv2.contourArea(cv2.convexHull(contour))
            if hull_area <= 0:
                continue
            fill_ratio = area / hull_area
            if not (0.18 <= fill_ratio <= 0.95):
                continue

            (cx, cy), radius = cv2.minEnclosingCircle(contour)
            if radius < 8 or radius > 120:
                continue

            obj_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.drawContours(obj_mask, [contour], -1, 255, thickness=-1)
            mean_b, mean_g, mean_r, _ = cv2.mean(crop, mask=obj_mask)
            if mean_r < mean_g * _REFINE_MIN_RED_DOMINANCE:
                continue
            if mean_r < mean_b * _REFINE_MIN_RED_BLUE_DOMINANCE:
                continue

            abs_cx = int(round(mon["left"] + x1 + cx))
            abs_cy = int(round(mon["top"] + y1 + cy))
            dist = ((abs_cx - anchor_pos[0]) ** 2 + (abs_cy - anchor_pos[1]) ** 2) ** 0.5

            score = dist + abs(aspect - 1.9) * 55 + abs(fill_ratio - 0.55) * 140
            if score < best_score:
                best_score = score
                best_dist = dist
                best_pos = (abs_cx, abs_cy)

        if best_pos is not None and max_distance is not None and best_dist > max_distance:
            print(
                f"[DIAG] refine anchor={anchor_pos} rejected={best_pos} "
                f"dist={best_dist:.1f} max={max_distance}"
            )
            return None
        if best_pos is not None:
            print(
                f"[DIAG] refine anchor={anchor_pos} best={best_pos} "
                f"score={best_score:.1f} dist={best_dist:.1f}"
            )
        else:
            print(f"[DIAG] refine anchor={anchor_pos} best=None")
        return best_pos

    def _find_red_ring_global(self, frame: np.ndarray) -> tuple[int, int] | None:
        """Busca el mejor candidato rojo en todo el monitor cuando no hay ancla confiable."""
        mon = self.screen.game_region()
        center = (mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
        # Reusar la heuristica local, pero abarcar casi todo el monitor.
        best = self._find_red_ring_anchor(
            frame,
            center,
            search_radius_x=max(200, mon["width"] // 2 - 20),
            search_radius_y=max(160, mon["height"] // 2 - 20),
            max_distance=max(320, min(mon["width"], mon["height"]) // 3),
        )
        print(f"[DIAG] refine_global best={best}")
        return best

    def _find_harvest_menu_option(
        self,
        frame: np.ndarray,
        anchor_pos: tuple[int, int],
        search_radius_x: int = 220,
        search_radius_y: int = 170,
    ) -> tuple[int, int] | None:
        """Busca el menu contextual de cosecha sin PNG y devuelve el centro de la segunda opcion."""
        mon = self.screen.game_region()
        frame_h, frame_w = frame.shape[:2]
        anchor_x = int(anchor_pos[0] - mon["left"])
        anchor_y = int(anchor_pos[1] - mon["top"])

        x1 = max(0, anchor_x - search_radius_x)
        y1 = max(0, anchor_y - search_radius_y)
        x2 = min(frame_w, anchor_x + search_radius_x)
        y2 = min(frame_h, anchor_y + search_radius_y)
        if x2 - x1 < 40 or y2 - y1 < 40:
            return None

        crop = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

        # Detectar el header "Avena" (fila superior del menu, marron oscuro).
        # La fila "Segar" es beis claro — no necesitamos detectarla: clickeamos
        # justo DEBAJO del header para caer en ella.
        mask_header = cv2.inRange(hsv, (8, 30, 30), (30, 200, 180))
        # También detectar el menú completo (beis claro de "Segar") como alternativa.
        mask_full = cv2.inRange(hsv, (15, 20, 140), (35, 120, 220))
        mask = cv2.bitwise_or(mask_header, mask_full)

        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_click = None
        best_score = float("inf")

        # Altura aproximada de cada fila del menú contextual de Dofus Retro
        ROW_H = int(self.config["farming"].get("menu_row_height", 28))

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 500 or area > 45000:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            # Aceptar tanto el header solo (~20-40px) como el menú completo (~50-180px)
            if not (60 <= w <= 280 and 15 <= h <= 180):
                continue

            roi = crop[y:y + h, x:x + w]
            mean_b, mean_g, mean_r = cv2.mean(roi)[:3]
            if not (40 <= mean_r <= 220 and 35 <= mean_g <= 180 and 20 <= mean_b <= 160):
                continue

            cx = x + (w / 2.0)

            if h < 45:
                # Solo se detectó el header: click debajo del borde inferior = fila Segar
                cy = y + h + ROW_H // 2
            else:
                # Se detectó el menú completo: click en la mitad de la segunda fila
                cy = y + h / 2.0 + ROW_H // 2

            abs_x = int(round(mon["left"] + x1 + cx))
            abs_y = int(round(mon["top"] + y1 + cy))

            if not self._is_point_on_monitor((abs_x, abs_y)):
                continue

            dist = ((abs_x - anchor_pos[0]) ** 2 + (abs_y - anchor_pos[1]) ** 2) ** 0.5
            score = dist
            if score < best_score:
                best_score = score
                best_click = (abs_x, abs_y)

        print(f"[DIAG] harvest_menu anchor={anchor_pos} click={best_click} row_h={ROW_H}")
        return best_click

    def _fallback_harvest_menu_click(self, anchor_pos: tuple[int, int]) -> tuple[int, int] | None:
        dx, dy = self.config["farming"].get("harvest_menu_offset", [46, 70])
        try:
            pos = (int(anchor_pos[0] + int(dx)), int(anchor_pos[1] + int(dy)))
        except (TypeError, ValueError):
            pos = (int(anchor_pos[0] + 46), int(anchor_pos[1] + 70))
        if not self._is_point_on_monitor(pos):
            return None
        print(f"[DIAG] harvest_menu_fallback anchor={anchor_pos} click={pos}")
        return pos

    def _find_pj_on_screen(self, frame: np.ndarray) -> tuple[int, int] | None:
        """Detecta la tarjeta del PJ en la banda inferior derecha."""
        mon = self.screen.game_region()
        fh, fw = frame.shape[:2]
        gx1 = int(fw * 0.70)
        gx2 = int(fw * 0.97)
        gy1 = int(fh * 0.68)
        gy2 = int(fh * 0.97)
        crop = frame[gy1:gy2, gx1:gx2]
        band_h = max(1, int(crop.shape[0] * 0.32))
        top_band = crop[:band_h, :]
        pj_threshold = float(self.config["bot"].get("pj_threshold", 0.40) or 0.40)
        band_center, band_score = self.detector.best_match(top_band, "PJ", "ui/pj")
        self._save_pj_debug(top_band, band_center, band_score)
        if band_center is not None and band_score >= max(0.75, pj_threshold):
            abs_pos = (mon["left"] + gx1 + int(band_center[0]), mon["top"] + gy1 + int(band_center[1]))
            print(
                f"[DIAG] pj_card_band detectado center={band_center} abs={abs_pos} "
                f"roi=({gx1},{gy1})-({gx2},{gy1 + band_h}) score={band_score:.4f}"
            )
            return abs_pos
        marker_center, marker_rect, marker_score = self._find_selected_card_by_marker(crop)
        if marker_center is not None:
            click_local = self._marker_click_point(marker_rect, marker_center)
            abs_pos = (mon["left"] + gx1 + int(click_local[0]), mon["top"] + gy1 + int(click_local[1]))
            print(
                f"[DIAG] pj_card_marker detectado center={marker_center} click={click_local} abs={abs_pos} "
                f"rect={marker_rect} roi=({gx1},{gy1})-({gx2},{gy2}) score={marker_score:.1f}"
            )
            return abs_pos
        card_center, card_rect, card_score, pj_score = self._find_selected_card_in_crop(crop)
        if card_center is not None:
            click_local = self._card_click_point(card_rect, card_center)
            abs_pos = (mon["left"] + gx1 + int(click_local[0]), mon["top"] + gy1 + int(click_local[1]))
            print(
                f"[DIAG] pj_card_rect detectado center={card_center} click={click_local} abs={abs_pos} "
                f"rect={card_rect} roi=({gx1},{gy1})-({gx2},{gy2}) score={card_score:.1f} pj_score={pj_score:.4f}"
            )
            return abs_pos

        best_center, best_score = self.detector.best_match(crop, "PJ", "ui/pj")
        self._save_pj_debug(crop, best_center, best_score)
        print(
            f"[DIAG] pj_card no detectado roi=({gx1},{gy1})-({gx2},{gy2}) "
            f"best={best_center} score={best_score:.4f} threshold={pj_threshold:.4f}"
        )
        return None

    def _marker_click_point(
        self,
        rect: tuple[int, int, int, int] | None,
        fallback: tuple[int, int],
    ) -> tuple[int, int]:
        if rect is None:
            return fallback
        x, y, w, h = rect
        # El triangulo apunta a la tarjeta: click justo debajo de la punta.
        return (x + (w // 2), y + h + max(10, min(22, h)))

    def _card_click_point(
        self,
        rect: tuple[int, int, int, int] | None,
        fallback: tuple[int, int],
    ) -> tuple[int, int]:
        if rect is None:
            return fallback
        x, y, w, h = rect
        return (x + (w // 2), y + max(16, min(34, h // 3)))

    def _find_selected_card_by_marker(
        self,
        crop: np.ndarray,
    ) -> tuple[tuple[int, int] | None, tuple[int, int, int, int] | None, float]:
        """Busca el triangulo naranja encima de la tarjeta y la tarjeta blanca justo debajo."""
        if crop.size == 0:
            return None, None, 0.0
        crop_h, crop_w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        orange_mask = cv2.inRange(hsv, np.array([5, 90, 100]), np.array([30, 255, 255]))
        kernel = np.ones((3, 3), np.uint8)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(orange_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_center = None
        best_rect = None
        best_score = 0.0
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < 60 or area > 1200:
                continue
            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.08 * peri, True)
            x, y, w, h = cv2.boundingRect(contour)
            if y > int(crop_h * 0.35):
                continue
            if w < 12 or w > 70 or h < 10 or h > 55:
                continue
            if len(approx) < 3 or len(approx) > 6:
                continue

            cx = x + w // 2
            wy1 = min(crop_h - 1, y + h + 2)
            wy2 = min(crop_h, wy1 + 150)
            wx1 = max(0, cx - 70)
            wx2 = min(crop_w, cx + 70)
            window = crop[wy1:wy2, wx1:wx2]
            if window.size == 0:
                continue
            win_hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
            white_mask = cv2.inRange(win_hsv, np.array([0, 0, 145]), np.array([180, 85, 255]))
            white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_OPEN, kernel, iterations=1)
            white_contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for wcontour in white_contours:
                wx, wy, ww, wh = cv2.boundingRect(wcontour)
                warea = float(cv2.contourArea(wcontour))
                if ww < 20 or ww > 90 or wh < 45 or wh > 130:
                    continue
                if warea < 800:
                    continue
                aspect = float(wh) / float(max(ww, 1))
                if aspect < 1.1 or aspect > 3.8:
                    continue
                white_ratio = warea / float(max(ww * wh, 1))
                if white_ratio < 0.45:
                    continue
                card_roi = window[wy:wy + wh, wx:wx + ww]
                if card_roi.size == 0:
                    continue
                card_hsv = cv2.cvtColor(card_roi, cv2.COLOR_BGR2HSV)
                red_mask_1 = cv2.inRange(card_hsv, np.array([0, 90, 70]), np.array([12, 255, 255]))
                red_mask_2 = cv2.inRange(card_hsv, np.array([170, 90, 70]), np.array([180, 255, 255]))
                red_mask = cv2.bitwise_or(red_mask_1, red_mask_2)
                stripe_x1 = max(0, int(ww * 0.68))
                stripe_x2 = min(ww, int(ww * 0.95))
                stripe = red_mask[:, stripe_x1:stripe_x2]
                if stripe.size == 0:
                    red_ratio = 0.0
                else:
                    red_ratio = float(cv2.countNonZero(stripe)) / float(max(stripe.shape[0] * stripe.shape[1], 1))
                top_bias = max(0.0, (crop_h - y) * 2.5)
                score = (area * 2.0) + warea + (white_ratio * 900.0) + (red_ratio * 900.0) + top_bias
                if score > best_score:
                    best_score = score
                    best_rect = (wx1 + wx, wy1 + wy, ww, wh)
                    best_center = (wx1 + wx + ww // 2, wy1 + wy + wh // 2)
        return best_center, best_rect, best_score

    def _find_selected_card_in_crop(
        self,
        crop: np.ndarray,
    ) -> tuple[tuple[int, int] | None, tuple[int, int, int, int] | None, float, float]:
        """Busca tarjetas candidatas por marco naranja y las desempata con PJ.png dentro del retrato."""
        if crop.size == 0:
            return None, None, 0.0, 0.0
        crop_h, crop_w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        orange_mask = cv2.inRange(hsv, np.array([5, 90, 100]), np.array([30, 255, 255]))
        kernel = np.ones((3, 3), np.uint8)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_CLOSE, kernel, iterations=3)
        orange_mask = cv2.dilate(orange_mask, kernel, iterations=1)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(orange_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_center = None
        best_rect = None
        best_score = 0.0
        best_pj_score = 0.0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if y > int(crop_h * 0.20):
                continue
            if w < 70 or w > 150 or h < 70 or h > 150:
                continue
            area = float(cv2.contourArea(contour))
            if area < 2000:
                continue
            aspect = float(h) / float(max(w, 1))
            if aspect < 0.80 or aspect > 1.25:
                continue
            # El retrato util vive en la zona interior alta-centrada de la tarjeta.
            inner_x1 = x + max(6, int(w * 0.16))
            inner_x2 = x + min(w - 6, int(w * 0.84))
            inner_y1 = y + max(6, int(h * 0.10))
            inner_y2 = y + min(h - 6, int(h * 0.86))
            if inner_x2 <= inner_x1 or inner_y2 <= inner_y1:
                continue
            inner = crop[inner_y1:inner_y2, inner_x1:inner_x2]
            pj_center, pj_score = self.detector.best_match(inner, "PJ", "ui/pj")
            score = area - (y * 8.0) + (pj_score * 6000.0)
            if score > best_score:
                best_score = score
                best_rect = (x, y, w, h)
                best_center = (x + w // 2, y + h // 2)
                best_pj_score = pj_score
        self._save_pj_card_mask_debug(crop, orange_mask, best_rect)
        return best_center, best_rect, best_score, best_pj_score

    def _save_pj_card_mask_debug(
        self,
        crop: np.ndarray,
        orange_mask: np.ndarray,
        best_rect: tuple[int, int, int, int] | None,
    ) -> None:
        try:
            debug = crop.copy()
            if best_rect is not None:
                x, y, w, h = best_rect
                cv2.rectangle(debug, (x, y), (x + w, y + h), (0, 255, 255), 2)
            mask_bgr = cv2.cvtColor(orange_mask, cv2.COLOR_GRAY2BGR)
            panel = np.hstack([debug, mask_bgr])
            debug_path = os.path.join(os.path.dirname(__file__), "..", "pj_card_mask_debug.png")
            cv2.imwrite(debug_path, panel)
        except Exception:
            pass

    def _save_pj_debug(
        self,
        crop: np.ndarray,
        best_center: tuple[int, int] | None,
        best_score: float,
    ) -> None:
        """Guarda el ROI de la tarjeta PJ con el mejor candidato y score."""
        try:
            debug = crop.copy()
            if best_center is not None:
                bx, by = int(best_center[0]), int(best_center[1])
                cv2.drawMarker(
                    debug,
                    (bx, by),
                    (0, 0, 255),
                    markerType=cv2.MARKER_CROSS,
                    markerSize=28,
                    thickness=2,
                )
            cv2.putText(
                debug,
                f"score={best_score:.4f}",
                (10, 26),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )
            debug_path = os.path.join(os.path.dirname(__file__), "..", "pj_card_debug.png")
            cv2.imwrite(debug_path, debug)
        except Exception:
            pass

    def _resolve_sacrogito_action_position(
        self,
        frame: np.ndarray,
    ) -> tuple[tuple[int, int] | None, str]:
        """Sacrogito: usar la celda propia del sniffer/mapa, no fallback por script."""
        projected_pos = None
        if self._combat_cell is not None:
            projected_pos = self._cell_to_screen(self._combat_cell)
        if projected_pos and self._is_point_on_monitor(projected_pos):
            max_distance = 180 if self._has_specific_projection_calibration() else 320
            refined = self._find_red_ring_anchor(frame, projected_pos, max_distance=max_distance)
            if refined:
                self._last_refined_self_pos = refined
                self._last_refined_cell = self._combat_cell
                return refined, "cell_refined"
            return projected_pos, "cell"
        pj_sprite_pos = self._find_pj_on_screen(frame)
        if pj_sprite_pos:
            self._last_refined_self_pos = pj_sprite_pos
            self._last_refined_cell = None
            return pj_sprite_pos, "pj_sprite"
        return (None, "not_found")

    def _resolve_action_position(
        self,
        frame: np.ndarray,
        projected_pos: tuple[int, int] | None,
        cell_id: int | None = None,
    ) -> tuple[tuple[int, int], str]:
        """Combina la proyeccion por celda con un refinamiento visual local."""
        specific_calibration = self._has_specific_projection_calibration()
        if projected_pos and self._is_point_on_monitor(projected_pos):
            if (
                cell_id is not None
                and self._last_refined_cell == cell_id
                and self._last_refined_self_pos
                and self._is_point_on_monitor(self._last_refined_self_pos)
            ):
                return self._last_refined_self_pos, "cell_locked_refined"
            max_distance = 180 if specific_calibration else 320
            refined = self._find_red_ring_anchor(frame, projected_pos, max_distance=max_distance)
            if refined:
                return refined, "cell_refined"
            if self._last_refined_self_pos and self._is_point_on_monitor(self._last_refined_self_pos):
                return self._last_refined_self_pos, "cell_cached_refined"
            return projected_pos, "cell"

        saved_pos = self.config["bot"].get("sacrogito_self_pos")
        if saved_pos:
            fallback = (int(saved_pos[0]), int(saved_pos[1]))
            refined = self._find_red_ring_anchor(frame, fallback, max_distance=260)
            if refined:
                return refined, "fallback_saved_refined"
            if self._last_refined_self_pos and self._is_point_on_monitor(self._last_refined_self_pos):
                return self._last_refined_self_pos, "fallback_cached_refined"
            global_refined = self._find_red_ring_global(frame)
            if global_refined:
                return global_refined, "global_refined"
            return fallback, "fallback_saved"

        mon = self.screen.game_region()
        center = (mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
        global_refined = self._find_red_ring_global(frame)
        if global_refined:
            return global_refined, "global_refined"
        # Último recurso: sprite del PJ en los retratos de turno (inferior izquierda)
        pj_sprite_pos = self._find_pj_on_screen(frame)
        if pj_sprite_pos:
            self._last_refined_self_pos = pj_sprite_pos
            return pj_sprite_pos, "pj_sprite_retrato"
        return center, "fallback_center"

    # ─────────────────────────────────── grid auto-detect ──

    _MAX_DETECT_ATTEMPTS = 4

    def _try_detect_grid(self, frame) -> None:
        """Intenta detectar el origen del grid usando IsoGridDetector + frame actual."""
        if self._grid_detector is None:
            return
        if self._detected_origin is not None:
            return
        if self._grid_detect_attempts >= self._MAX_DETECT_ATTEMPTS:
            return
        if not self._pending_gic_entries:
            print("[GRID] Sin entradas GIC disponibles — detección diferida")
            return

        self._grid_detect_attempts += 1
        print(
            f"[GRID] Intento {self._grid_detect_attempts}/{self._MAX_DETECT_ATTEMPTS} "
            f"({len(self._pending_gic_entries)} celdas GIC)"
        )

        import os
        debug_path = os.path.join(
            os.path.dirname(__file__), "..",
            f"grid_debug_{self._grid_detect_attempts}.png"
        )

        grid_result = self._grid_detector.detect(
            frame,
            self.screen.game_region(),
            self._pending_gic_entries,
            my_cell_id=self._combat_cell,
            debug_path=debug_path,
        )

        if grid_result is None:
            print(f"[GRID] Intento {self._grid_detect_attempts} fallido — sin origen")
            return

        origin = grid_result.origin
        self._detected_origin = origin
        print(
            f"[GRID] Origin detectado ({grid_result.confidence}) "
            f"score={grid_result.score}: x={origin[0]:.1f} y={origin[1]:.1f}"
        )

        # Solo persistir en config.yaml si la confianza es alta (RANSAC score ≥ 2)
        if grid_result.confidence == "high":
            self._save_detected_origin(origin)
        else:
            print(
                "[GRID] Confianza baja — origin usado solo en sesión actual "
                "(revisar grid_debug_*.png para validar)"
            )

    def _save_detected_origin(self, origin: tuple[float, float]) -> None:
        """Persiste el origen detectado en config.yaml bajo el fingerprint actual."""
        fp = self._current_arena_fingerprint
        if not fp:
            return
        cal = self.config["bot"].get("cell_calibration", {})
        by_fp = cal.setdefault("map_origins_by_fingerprint", {})
        if fp in by_fp:
            return  # ya existe, no sobreescribir el manual
        ox, oy = origin
        by_fp[fp] = {"x": round(ox, 2), "y": round(oy, 2)}
        print(f"[GRID] Guardando origin para fp={fp}: ({ox:.1f}, {oy:.1f})")
        try:
            import yaml
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "config.yaml"
            )
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            raw["bot"]["cell_calibration"]["map_origins_by_fingerprint"][fp] = {
                "x": round(ox, 2), "y": round(oy, 2)
            }
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
            print(f"[GRID] config.yaml actualizado con fingerprint {fp}")
        except Exception as e:
            print(f"[GRID] No se pudo guardar en config.yaml: {e}")

    def _get_enemy_targets(self) -> list[dict]:
        """Retorna las posiciones en pantalla de los enemigos activos.

        En Dofus Retro los monstruos tienen actor_id negativo.
        Si conocemos el team_id propio, también filtramos por equipo.
        """
        targets: list[dict] = []
        for actor_id, fighter in self._fighters.items():
            if self._actor_ids_match(actor_id, self._sniffer_my_actor):
                continue  # saltar al propio personaje
            if not fighter.get("alive", True):
                continue  # saltar enemigos ya eliminados

            # Determinar si es enemigo: ID negativo = monstruo (protocolo Dofus Retro)
            # Fallback: si conocemos team_ids, comparar equipos
            is_enemy = False
            try:
                is_enemy = int(actor_id) < 0
            except (ValueError, TypeError):
                pass
            if not is_enemy and self._my_team_id is not None:
                fighter_team = fighter.get("team_id")
                if fighter_team is not None and fighter_team != self._my_team_id:
                    is_enemy = True

            if not is_enemy:
                continue

            cell_id = fighter.get("cell_id")
            if cell_id is None:
                continue
            screen_pos = self._cell_to_screen(cell_id)
            if not (screen_pos and self._is_point_on_monitor(screen_pos)):
                continue
            
            targets.append({
                "id": actor_id,
                "cell_id": cell_id,
                "hp": fighter.get("hp"),
                "screen_pos": screen_pos
            })

        current_signature = tuple(sorted(t["id"] for t in targets))
        if current_signature != self._last_enemy_positions_log:
            if targets:
                print(f"[COMBAT] {len(targets)} enemigo(s) detectados: {[t['id'] for t in targets]}")
            else:
                print(f"[COMBAT] Sin posiciones de enemigos (fighters={len(self._fighters)})")
            self._last_enemy_positions_log = current_signature
        return targets

    def _get_enemy_fighter_cells(self) -> list[int]:
        enemy_cells: list[int] = []
        for actor_id, fighter in self._fighters.items():
            if self._actor_ids_match(actor_id, self._sniffer_my_actor):
                continue
            if not fighter.get("alive", True):
                continue
            is_enemy = False
            try:
                is_enemy = int(actor_id) < 0
            except (ValueError, TypeError):
                pass
            if not is_enemy and self._my_team_id is not None:
                fighter_team = fighter.get("team_id")
                if fighter_team is not None and fighter_team != self._my_team_id:
                    is_enemy = True
            if not is_enemy:
                continue
            try:
                cell_id = int(fighter.get("cell_id"))
            except (TypeError, ValueError):
                continue
            enemy_cells.append(cell_id)
        return enemy_cells

    def _map_cell_by_id(self, cell_id: int | None) -> dict | None:
        if cell_id is None:
            return None
        for cell in self._current_map_cells:
            try:
                if int(cell.get("cell_id")) == int(cell_id):
                    return cell
            except (TypeError, ValueError, AttributeError):
                continue
        return None

    def _combat_cell_distance(self, left_cell: int | None, right_cell: int | None) -> int | None:
        left_meta = self._map_cell_by_id(left_cell)
        right_meta = self._map_cell_by_id(right_cell)
        if left_meta and right_meta:
            try:
                dx = abs(int(left_meta.get("x")) - int(right_meta.get("x")))
                dy = abs(int(left_meta.get("y")) - int(right_meta.get("y")))
                return dx + dy
            except (TypeError, ValueError, AttributeError):
                pass
        if left_cell is None or right_cell is None:
            return None
        try:
            left_x, left_y = cell_id_to_grid(int(left_cell), 15)
            right_x, right_y = cell_id_to_grid(int(right_cell), 15)
        except (TypeError, ValueError):
            return None
        return abs(left_x - right_x) + abs(left_y - right_y)

    def _choose_combat_approach_cell(self, move_points: int, desired_range: int = 1) -> dict | None:
        if self._combat_cell is None or move_points <= 0:
            return None
        enemy_cells = self._get_enemy_fighter_cells()
        if not enemy_cells:
            return None
        current_distances = [
            distance
            for distance in (
                self._combat_cell_distance(self._combat_cell, enemy_cell)
                for enemy_cell in enemy_cells
            )
            if distance is not None
        ]
        if not current_distances:
            return None
        current_min_distance = min(current_distances)
        if current_min_distance <= desired_range:
            return None

        if hasattr(self.combat_profile, "movement_score"):
            current_rank = self.combat_profile.movement_score(self._combat_cell, 0, current_distances)
        else:
            current_rank = None

        occupied_cells: set[int] = set()
        for actor_id, fighter in self._fighters.items():
            if not fighter.get("alive", True):
                continue
            try:
                occupied_cell = int(fighter.get("cell_id"))
            except (TypeError, ValueError):
                continue
            if self._actor_ids_match(actor_id, self._sniffer_my_actor):
                continue
            occupied_cells.add(occupied_cell)

        best: dict | None = None
        for cell in self._current_map_cells:
            try:
                candidate_cell = int(cell.get("cell_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if candidate_cell == self._combat_cell:
                continue
            if candidate_cell in occupied_cells:
                continue
            if not bool(cell.get("is_walkable")):
                continue
            self_distance = self._combat_cell_distance(self._combat_cell, candidate_cell)
            if self_distance is None or self_distance > move_points:
                continue
            enemy_distances = [
                distance
                for distance in (
                    self._combat_cell_distance(candidate_cell, enemy_cell)
                    for enemy_cell in enemy_cells
                )
                if distance is not None
            ]
            if not enemy_distances:
                continue
            enemy_distance = min(enemy_distances)
            projected = self._cell_to_screen(candidate_cell)
            if projected is None or not self._is_point_on_monitor(projected):
                continue
                
            if hasattr(self.combat_profile, "movement_score"):
                rank = self.combat_profile.movement_score(candidate_cell, self_distance, enemy_distances)
                if rank >= current_rank:
                    continue
            else:
                if enemy_distance >= current_min_distance:
                    continue
                rank = (enemy_distance, -self_distance, candidate_cell)
                
            if best is None or rank < best["rank"]:
                best = {
                    "cell_id": candidate_cell,
                    "screen_pos": projected,
                    "enemy_distance": enemy_distance,
                    "self_distance": self_distance,
                    "rank": rank,
                }
        return best

    def _enemy_in_melee_range(self, source_cell: int | None = None, max_distance: int = 1) -> bool:
        if source_cell is None:
            source_cell = self._combat_cell
        if source_cell is None:
            return False
        for enemy_cell in self._get_enemy_fighter_cells():
            distance = self._combat_cell_distance(source_cell, enemy_cell)
            if distance is not None and distance <= max_distance:
                return True
        return False

    def _choose_placement_cell(self, frame: np.ndarray) -> dict | None:
        if self._combat_cell is None:
            return None
        enemy_cells = self._get_enemy_fighter_cells()
        if not enemy_cells:
            return None

        occupied_cells: set[int] = set()
        for actor_id, fighter in self._fighters.items():
            if not fighter.get("alive", True):
                continue
            try:
                occupied_cell = int(fighter.get("cell_id"))
            except (TypeError, ValueError):
                continue
            if self._actor_ids_match(actor_id, self._sniffer_my_actor):
                continue
            occupied_cells.add(occupied_cell)

        current_distances = [
            distance
            for distance in (
                self._combat_cell_distance(self._combat_cell, enemy_cell)
                for enemy_cell in enemy_cells
            )
            if distance is not None
        ]
        if not current_distances:
            return None
        current_min_distance = min(current_distances)
        current_rank: tuple[int, int, int] | None = None
        if self._combat_cell is not None:
            current_self_distance = 0
            current_enemy_distances = [
                distance
                for distance in (
                    self._combat_cell_distance(self._combat_cell, enemy_cell)
                    for enemy_cell in enemy_cells
                )
                if distance is not None
            ]
            if current_enemy_distances:
                if hasattr(self.combat_profile, "placement_score"):
                    current_rank = self.combat_profile.placement_score(int(self._combat_cell), current_self_distance, current_enemy_distances)
                else:
                    current_rank = (min(current_enemy_distances), current_self_distance, int(self._combat_cell))
        best: dict | None = None
        placement_candidates = list(self._placement_cells) if self._placement_cells else []
        if placement_candidates:
            iterable = [self._map_cell_by_id(cell_id) for cell_id in placement_candidates]
        else:
            iterable = list(self._current_map_cells)
        for cell in iterable:
            if not cell:
                continue
            try:
                candidate_cell = int(cell.get("cell_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if candidate_cell == self._combat_cell:
                continue
            if candidate_cell in occupied_cells:
                continue
            if not bool(cell.get("is_walkable")):
                continue
            projected = self._cell_to_screen(candidate_cell)
            if projected is None or not self._is_point_on_monitor(projected):
                continue
            refined = projected
            if not placement_candidates:
                ring_refined = self._find_red_ring_anchor(frame, projected, max_distance=48)
                if ring_refined is None:
                    continue
                refined = ring_refined
            self_distance = self._combat_cell_distance(self._combat_cell, candidate_cell)
            if self_distance is None:
                continue
            enemy_distances = [
                distance
                for distance in (
                    self._combat_cell_distance(candidate_cell, enemy_cell)
                    for enemy_cell in enemy_cells
                )
                if distance is not None
            ]
            if not enemy_distances:
                continue
            enemy_distance = min(enemy_distances)
            
            if hasattr(self.combat_profile, "placement_score"):
                rank = self.combat_profile.placement_score(candidate_cell, self_distance, enemy_distances)
            else:
                rank = (enemy_distance, self_distance, candidate_cell)
                if enemy_distance >= current_min_distance and best is not None:
                    continue
                    
            if best is None or rank < best["rank"]:
                best = {
                    "cell_id": candidate_cell,
                    "screen_pos": refined,
                    "enemy_distance": enemy_distance,
                    "self_distance": self_distance,
                    "rank": rank,
                }
        if self._combat_cell is not None and current_rank is not None:
            current_pos = self._cell_to_screen(self._combat_cell)
            if current_pos is not None and self._is_point_on_monitor(current_pos):
                if best is None or current_rank <= best["rank"]:
                    return {
                        "cell_id": int(self._combat_cell),
                        "screen_pos": current_pos,
                        "enemy_distance": current_rank[0],
                        "self_distance": current_rank[1],
                        "rank": current_rank,
                        "already_optimal": True,
                    }
        return best

    def _auto_place_before_ready(self, frame: np.ndarray) -> np.ndarray:
        if self._placement_auto_attempted:
            return frame
        self._placement_auto_attempted = True
        selected = self._choose_placement_cell(frame)
        if not selected:
            print("[COMBAT] Placement automático: sin celda roja válida, usando posición actual")
            return frame
        target_cell = int(selected["cell_id"])
        target_pos = tuple(selected["screen_pos"])
        old_cell = self._combat_cell
        if selected.get("already_optimal"):
            print(
                f"[COMBAT] Placement automático: celda actual óptima "
                f"cell={target_cell} dist_enemy={selected['enemy_distance']}"
            )
            return frame
        print(
            f"[COMBAT] Placement automático -> cell={target_cell} pos={target_pos} "
            f"dist_self={selected['self_distance']} dist_enemy={selected['enemy_distance']}"
        )
        self.screen.focus_window()
        self.actions.quick_click(target_pos)
        wait_deadline = time.time() + float(self.config["bot"].get("combat_placement_move_wait", 1.0) or 1.0)
        while time.time() < wait_deadline:
            if self.sniffer_active:
                self._drain_sniff_queue()
            if self._sniffer_fight_ended or not self._sniffer_in_placement:
                break
            if self._combat_cell is not None and self._combat_cell != old_cell:
                break
            time.sleep(0.05)
        if self._combat_cell is not None and self._combat_cell != old_cell:
            print(f"[COMBAT] Placement confirmado: {old_cell} -> {self._combat_cell}")
        else:
            print(f"[COMBAT] Placement no confirmado hacia cell={target_cell}")
        time.sleep(float(self.config["bot"].get("combat_placement_settle_delay", 0.12) or 0.12))
        return self.screen.capture()

    def _move_towards_enemy_for_profile(self, move_points: int, desired_range: int = 1) -> dict:
        move_points = max(0, int(move_points or 0))
        selected = self._choose_combat_approach_cell(move_points, desired_range=desired_range)
        if not selected:
            return {
                "moved": False,
                "combat_cell": self._combat_cell,
                "self_screen_pos": self._cell_to_screen(self._combat_cell) if self._combat_cell is not None else None,
                "fight_ended": self._sniffer_fight_ended,
                "turn_ready": self._sniffer_turn_ready,
                "current_pa": self._sniffer_pa,
                "current_mp": self._sniffer_pm,
            }

        target_cell = int(selected["cell_id"])
        target_pos = tuple(selected["screen_pos"])
        old_cell = self._combat_cell
        print(
            f"[COMBAT] Movimiento táctico -> cell={target_cell} pos={target_pos} "
            f"dist_self={selected['self_distance']} dist_enemy={selected['enemy_distance']}"
        )
        self.screen.focus_window()
        self.actions.quick_click(target_pos)
        wait_deadline = time.time() + float(self.config["bot"].get("combat_move_wait", 1.25) or 1.25)
        while time.time() < wait_deadline:
            if self.sniffer_active:
                self._drain_sniff_queue()
            if self._sniffer_fight_ended:
                break
            if self._combat_cell is not None and self._combat_cell != old_cell:
                break
            time.sleep(0.05)
        current_cell = self._combat_cell
        moved = current_cell is not None and current_cell != old_cell
        if moved:
            print(f"[COMBAT] Movimiento confirmado: {old_cell} -> {current_cell}")
        else:
            print(f"[COMBAT] Movimiento no confirmado hacia cell={target_cell}")
        return {
            "attempted_move": True,
            "moved": moved,
            "target_cell": target_cell,
            "target_pos": target_pos,
            "combat_cell": current_cell,
            "self_screen_pos": self._cell_to_screen(current_cell) if current_cell is not None else None,
            "fight_ended": self._sniffer_fight_ended,
            "turn_ready": self._sniffer_turn_ready,
            "current_pa": self._sniffer_pa,
            "current_mp": self._sniffer_pm,
        }

    def _dismiss_popup(self, frame, name: str) -> bool:
        """Cierra un popup si esta visible. Devuelve True si fue cerrado."""
        pos = self._find_ui_screen(frame, name)
        if pos:
            print(f"[BOT] Popup '{name}' detectado — cerrando")
            self.actions.quick_click(pos)
            time.sleep(self.config["bot"].get("combat_popup_close_delay", 0.12))
            return True
        return False

    def _find_listo_screen(self, frame: np.ndarray) -> tuple[int, int] | None:
        pos = self._find_ui_screen(frame, "Listo")
        if pos:
            return pos

        fh, fw = frame.shape[:2]
        y1 = max(0, int(fh * 0.50))
        x1 = max(0, int(fw * 0.50))
        crop = frame[y1:fh, x1:fw]
        best_pos, best_score = self.ui_detector.best_match(crop, "Listo", "ui")
        if best_pos is not None and best_score >= 0.65:
            abs_pos = self._frame_pos_to_screen((x1 + int(best_pos[0]), y1 + int(best_pos[1])))
            print(f"[BOT] Listo detectado por best_match score={best_score:.3f} pos={abs_pos}")
            return abs_pos

        return None

    def _is_listo_visible(self, frame: np.ndarray) -> bool:
        return self._find_listo_screen(frame) is not None

    def _find_combat_result_close(self, frame: np.ndarray, *, allow_orange_fallback: bool = False) -> tuple[int, int] | None:
        close_pos = self._find_ui_screen(frame, "Cerrar")
        if close_pos:
            return close_pos

        best_pos, best_score = self.ui_detector.best_match(frame, "Cerrar", "ui")
        min_score = float(self.config["bot"].get("combat_result_close_min_score", 0.68) or 0.68)
        if best_pos is not None and best_score >= min_score:
            print(f"[BOT] Cerrar detectado por best_match score={best_score:.3f} pos={best_pos}")
            return best_pos

        if not allow_orange_fallback:
            return None
        
        return None

    def _cast_dunayar_in_placement_if_visible(self, frame: np.ndarray) -> bool:
        dunayar_x = int(self.config["bot"].get("dunayar_x", 1679) or 1679)
        dunayar_y = int(self.config["bot"].get("dunayar_y", 1261) or 1261)
        print(f"[COMBAT] Duna Yar - doble click en inventario ({dunayar_x}, {dunayar_y})")
        self.screen.focus_window()
        self.actions.double_click((dunayar_x, dunayar_y))
        time.sleep(float(self.config["bot"].get("dunayar_refresh_delay", 0.3) or 0.3))
        return True

    def tick(self):
        now = time.time()
        self._maybe_finish_combat_probe()
        farming_mode = self.config["farming"].get("mode", "resource")
        sniffer_resource_mode = self.sniffer_active and farming_mode == "resource"

        # ── 0. Popup OK (subida de nivel/oficio) — siempre, sin importar estado ──
        _ok_path = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui", "OK.png")
        if os.path.exists(_ok_path):
            frame_ok = self.screen.capture()
            ok_btn = self._find_ui_screen(frame_ok, "OK")
            if ok_btn:
                print("[BOT] Popup OK detectado — clickeando")
                self.actions.quick_click(ok_btn)
                time.sleep(self.config["bot"].get("combat_ok_delay", 0.15))
                return

        # ── 1. Eventos del sniffer — prioridad máxima, sin captura de pantalla ──
        if self.sniffer_active:
            self._drain_sniff_queue()

            # Fin de combate por protocolo (GE) — reintenta popup hasta 3 veces
            if self._sniffer_fight_ended and self.state == "in_combat":
                print("[BOT] GE recibido — cerrando resultado de combate")
                self._sniffer_fight_ended = False
                self.harvested_positions = []
                for _ in range(6):
                    frame_fc = self.screen.capture()
                    ok_btn = self._find_ui_screen(frame_fc, "OK")
                    if ok_btn:
                        print("[BOT] Popup OK (subida de nivel) — clickeando")
                        self.actions.quick_click(ok_btn)
                        time.sleep(self.config["bot"].get("combat_ok_delay", 0.15))
                        continue
                    cerrar = self._find_combat_result_close(frame_fc, allow_orange_fallback=True)
                    if cerrar:
                        self.actions.quick_click(cerrar)
                        time.sleep(self.config["bot"].get("combat_close_delay", 0.15))
                        break
                    time.sleep(self.config["bot"].get("combat_popup_retry_delay", 0.15))
                self.state = "scan"
                return

        # ── 1.5 Descarga automática al alcanzar 950 PODS ──
        if self.state not in {"in_combat", "full_pods", "change_map", "wait_harvest_confirm", "harvesting_wait"} and not self.state.startswith("unloading_") and not self.state.startswith("teleport_"):
            if self.current_pods is not None:
                if self.current_pods >= 950:
                    if self.config.get("bot", {}).get("enable_bank_unload", False):
                        print(f"[BOT] ⚠️ Límite de PODS alcanzado ({self.current_pods}). Iniciando descarga en banco.")
                        self.state = "unloading_start"
                        self.pending = []
                        self.mob_pending = []
                    else:
                        print(f"[BOT] ⚠️ Límite de PODS alcanzado ({self.current_pods}). Descarga en banco deshabilitada, deteniendo farmeo.")
                        self.state = "full_pods"
                        self.pending = []
                        self.mob_pending = []

        if self.state == "full_pods":
            if self.current_pods is not None and self.current_pods < 950:
                print("[BOT] Espacio en inventario liberado. Reanudando...")
                self.state = "scan"
            time.sleep(1.0)
            return

        # ── 2. Popups globales — solo capturar si no estamos esperando turno ──
        # Con sniffer en combate: evitar capturas de pantalla innecesarias
        if (
            self.sniffer_active
            and self.state != "in_combat"
            and self._external_fight_pending
            and self.state in {"scan", "scan_mobs", "click_mob", "follow_player_wait"}
        ):
            if self._attempt_join_external_fight():
                return
        waiting_for_turn = (self.sniffer_active and self.state == "in_combat"
                            and not self._sniffer_turn_ready
                            and not self._sniffer_in_placement)
        if not waiting_for_turn and not (sniffer_resource_mode and self.state != "in_combat"):
            frame_global = self.screen.capture()
            for popup in ("OK", "SubioNivel", "Cerrar", "FueraAlcance"):
                if popup == "Cerrar" and self.state.startswith("teleport_"):
                    continue
                if popup == "Cerrar":
                    close_pos = self._find_combat_result_close(frame_global, allow_orange_fallback=False)
                    if close_pos:
                        print("[BOT] Popup 'Cerrar' detectado — cerrando")
                        self.actions.quick_click(close_pos)
                        time.sleep(self.config["bot"].get("combat_popup_close_delay", 0.12))
                        self.harvested_positions = []
                        self.state = self._combat_origin
                        return
                elif self._dismiss_popup(frame_global, popup):
                    if popup == "Cerrar":
                        self.harvested_positions = []
                        self.state = self._combat_origin
                    return

        # ── 3. Detección de entrada a combate ──
        # Con sniffer: GJK/Gp lo manejan → omitir poll de templates
        # Sin sniffer: poll COMBAT_POLL con template matching
        if self.state != "in_combat" and not sniffer_resource_mode:
            combat_poll = _config_delay(self.config, "combat_poll_interval", COMBAT_POLL)
            if not self.sniffer_active and now - self.last_combat_check >= combat_poll:
                self.last_combat_check = now
                frame = self.screen.capture()
                mi_turno_tpl = getattr(self.combat_profile, "mi_turno_template", None)
                listo_detected = (
                    self._is_listo_visible(frame)
                    if getattr(self.combat_profile, "uses_listo_template", True)
                    else False
                )
                mi_turno_detected = (
                    self.ui_detector.find_ui(frame, mi_turno_tpl)
                    if mi_turno_tpl
                    else None
                )
                if listo_detected or mi_turno_detected:
                    print("[BOT] Combate detectado (template)")
                    self._enter_combat(now)
                    return
            elif self.sniffer_active and now - self.last_combat_check >= combat_poll * 2:
                # Fallback: poll reducido con sniffer por si se perdió el GJK
                self.last_combat_check = now
                frame = self.screen.capture()
                mi_turno_tpl = getattr(self.combat_profile, "mi_turno_template", None)
                listo_detected = (
                    self._is_listo_visible(frame)
                    if getattr(self.combat_profile, "uses_listo_template", True)
                    else False
                )
                mi_turno_detected = (
                    self.ui_detector.find_ui(frame, mi_turno_tpl)
                    if mi_turno_tpl
                    else None
                )
                if listo_detected or mi_turno_detected:
                    print("[BOT] Combate detectado (template fallback)")
                    self._enter_combat(now)
                    return

        # ── 4. Estado in_combat ──────────────────────────────────────────────
        if self.state == "in_combat":
            cooldown = _config_delay(self.config, "combat_turn_cooldown", COMBAT_COOLDOWN)
            idle_wait = float(self.config["bot"].get("combat_idle_wait", 0.02) or 0.02)
            frame_delay = float(self.config["bot"].get("combat_frame_delay", 0.02 if self.sniffer_active else 0.08) or 0.02)

            if now < self.combat_action_until:
                time.sleep(idle_wait if self.sniffer_active else max(idle_wait, 0.05))
                return

            if self._combat_auto_ready_pending:
                if self.config.get("bot", {}).get("combat_manual_mode", False):
                    self._combat_auto_ready_pending = False
                    
                placement_active = bool(self._sniffer_in_placement or self._placement_cells)
                if not placement_active:
                    placement_grace = float(self.config["bot"].get("combat_placement_grace_delay", 0.75) or 0.75)
                    if (
                        self.sniffer_active
                        and self._combat_turn_number == 0
                        and (now - self._combat_entered_at) < placement_grace
                    ):
                        time.sleep(idle_wait)
                        return
                    if now < self._combat_auto_ready_at:
                        time.sleep(idle_wait)
                        return

                    if self.sniffer_active:
                        self._drain_sniff_queue()
                    if self.sniffer_active and self._sniffer_turn_ready:
                        print("[BOT] Turno iniciado antes de enviar Space de auto-ready. Omitiendo.")
                        self._combat_auto_ready_pending = False
                        self.combat_action_until = min(self.combat_action_until, time.time())
                        return

                    print("[BOT] Inicio de combate detectado - enviando Space para marcar listo")
                    self._arm_ready_actor_ack()
                    self.actions.quick_press_key("space")
                    self.actions.park_mouse(self.screen.parking_regions())
                    self._combat_auto_ready_pending = False
                    self._combat_auto_ready_at = 0.0
                    # NO forzar _sniffer_in_placement=True aqui: si no hay fase de colocacion
                    # el bot quedaria loopeando en vez de esperar el GTS del sniffer.
                    # El evento Gp del sniffer lo activara si corresponde.
                    self.combat_action_until = now + self.config["bot"].get("combat_ready_delay", 0.3)
                    return
            # Con sniffer: priorizar GTS, pero permitir fallback visual de MiTurno.
            if self.sniffer_active and not self._sniffer_turn_ready and not self._sniffer_in_placement:
                if now > self.combat_deadline:
                    print("[BOT] Timeout combate — re-escaneando")
                    self.harvested_positions = []
                    self.state = self._combat_origin
                    return

            # Capturar frame (necesario para posición y popups)
            time.sleep(frame_delay)
            frame = self.screen.capture()

            # Prioridad 1: resultado de combate
            cerrar_combate = self._find_ui_screen(frame, "Cerrar")
            if cerrar_combate:
                print("[BOT] Resultado de combate — cerrando ventana")
                self.actions.quick_click(cerrar_combate)
                time.sleep(self.config["bot"].get("combat_close_delay", 0.15))
                self.harvested_positions = []
                self._sniffer_fight_ended = False
                self.state = "scan"
                return

            # Prioridad 2: popup FueraAlcance
            if self.ui_detector.find_ui(frame, "FueraAlcance"):
                close_btn = self._find_ui_screen(frame, "CerrarPopup")
                if close_btn:
                    print("[BOT] Cerrando popup FueraAlcance")
                    self.actions.quick_click(close_btn)
                    time.sleep(self.config["bot"].get("combat_popup_close_delay", 0.12))
                    frame = self.screen.capture()

            listo_pos = (
                self._find_listo_screen(frame)
                if getattr(self.combat_profile, "uses_listo_template", True)
                else None
            )
            mi_turno_tpl = getattr(self.combat_profile, "mi_turno_template", None)
            mi_turno_detected = self.ui_detector.find_ui(frame, mi_turno_tpl) if mi_turno_tpl else None
            sniffer_turn = self.sniffer_active and self._sniffer_turn_ready            
            enemy_targets = self._get_enemy_targets()
            enemy_positions = [t["screen_pos"] for t in enemy_targets]
            ctx = CombatContext(
                self.screen, self.ui_detector, self.actions, self.config,
                enemies=enemy_targets,
                enemy_positions=enemy_positions,
                current_pa=self._sniffer_pa,
                current_mp=self._sniffer_pm,
                my_cell=self._combat_cell,
                turn_number=self._combat_turn_number,
                combat_probe=self._arm_combat_probe,
                buff_flags={
                    "castigo_osado_active": self._castigo_osado_active,
                    "castigo_osado_cooldown": self._spell_cooldowns.get(433, 0),
                    "spell_cooldowns": dict(self._spell_cooldowns),
                },
                refresh_combat_state=self._refresh_combat_state_for_profile,
                project_self_cell=lambda cell_id: self._project_cell_with_visual_grid_exact(cell_id, self._current_map_id),
                move_towards_enemy=self._move_towards_enemy_for_profile,
                enemy_in_melee_range=self._enemy_in_melee_range,                
                has_line_of_sight=self._has_line_of_sight,
                cell_distance=self._combat_cell_distance,
            )

            # Auto-actualizar context properties cuando el perfil refresca el estado
            original_refresh = ctx.refresh_combat_state
            def _auto_update_refresh(wait_s=0.0):
                res = original_refresh(wait_s)
                if "enemies" in res:
                    ctx.enemies = res["enemies"]
                    ctx.enemy_positions = [e["screen_pos"] for e in res["enemies"]]
                if "combat_cell" in res and res["combat_cell"] is not None:
                    ctx.my_cell = res["combat_cell"]
                return res
            ctx.refresh_combat_state = _auto_update_refresh

            placement_detected = bool(listo_pos) or (self.sniffer_active and self._sniffer_in_placement)
            turn_detected = bool(mi_turno_detected or sniffer_turn)
            if turn_detected:
                self._sniffer_in_placement = False
                
                if self.config.get("bot", {}).get("combat_manual_mode", False):
                    self._sniffer_turn_ready = False
                    if not getattr(self, "_manual_turn_notified", False):
                        print("[COMBAT] Modo manual activo: turno del jugador (esperando accion manual)")
                        self._manual_turn_notified = True
                    self.combat_action_until = now + 1.0
                    return

                if self._detected_origin is None:
                    self._try_detect_grid(frame)
                action_source = "unknown"
                action_pos = None
                projected_pos = None

                if self.combat_profile.name == "Sacrogito":
                    action_pos, action_source = self._resolve_sacrogito_action_position(frame)
                    print(
                        f"[DIAG] turn_ready sniffer={sniffer_turn} "
                        f"template={bool(mi_turno_detected)} sacro_action_source={action_source} "
                        f"action={action_pos}"
                    )
                else:
                    if self._combat_cell is not None:
                        projected_pos = self._cell_to_screen(self._combat_cell)
                        if projected_pos and not self._is_point_on_monitor(projected_pos):
                            print(
                                f"[DIAG] action source=cell_out_of_bounds "
                                f"cell={self._combat_cell} pos={projected_pos}"
                            )

                    action_pos, action_source = self._resolve_action_position(
                        frame,
                        projected_pos,
                        self._combat_cell,
                    )
                    if "refined" in action_source:
                        self._last_refined_self_pos = (int(action_pos[0]), int(action_pos[1]))
                        self._last_refined_cell = self._combat_cell
                        self.config["bot"]["sacrogito_self_pos"] = [int(action_pos[0]), int(action_pos[1])]
                    print(
                        f"[DIAG] action source={action_source} cell={self._combat_cell} "
                        f"projected={projected_pos} pos={action_pos}"
                    )
                    print(
                        f"[DIAG] turn_ready sniffer={sniffer_turn} "
                        f"template={bool(mi_turno_detected)} cell={self._combat_cell} action={action_pos}"
                    )
                self._sniffer_turn_ready = False
                result = self.combat_profile.on_turn(action_pos, ctx)
                if result == "combat_ended":
                    self.harvested_positions = []
                    self.state = self._combat_origin
                    return
                if result == "retry":
                    self._sniffer_turn_ready = True
                    self.combat_action_until = now + float(
                        self.config["bot"].get("combat_retry_delay", 0.35) or 0.35
                    )
                    return
                self.combat_action_until = now + cooldown
                self.no_fuir_count = 0

            elif placement_detected:
                self._sniffer_in_placement = True
                
                if self.config.get("bot", {}).get("combat_manual_mode", False):
                    if not getattr(self, "_manual_placement_notified", False):
                        print("[COMBAT] Modo manual activo: fase de colocacion manual (presiona Listo para empezar)")
                        self._manual_placement_notified = True
                    self.combat_action_until = now + 1.0
                    return

                if not self._placement_ready_sent:
                    self._cast_dunayar_in_placement_if_visible(frame)
                if not self._placement_ready_sent or (self.sniffer_active and now > self._awaiting_ready_ack_until):
                    if not self._placement_ready_sent:
                        frame = self._auto_place_before_ready(frame)

                    if self.sniffer_active:
                        self._drain_sniff_queue()
                    if self.sniffer_active and (not self._sniffer_in_placement or self._sniffer_turn_ready):
                        if not self._placement_ready_sent:
                            print("[BOT] Combate iniciado (GTS) durante auto-colocacion. Se omite presionar Listo.")
                        self._placement_ready_sent = True
                        self._combat_auto_ready_pending = False
                        self.combat_action_until = min(self.combat_action_until, time.time())
                        return

                    listo_pos = (
                        self._find_listo_screen(frame)
                        if getattr(self.combat_profile, "uses_listo_template", True)
                        else None
                    )
                    if listo_pos:
                        print(f"[BOT] Fase de colocacion detectada - clickeando Listo (retry={self._placement_ready_sent})")
                        self._arm_ready_actor_ack(window_s=3.0)
                        self.combat_profile.on_placement(listo_pos, ctx)
                    else:
                        print(f"[BOT] Fase de colocacion detectada - marcando listo con Space (retry={self._placement_ready_sent})")
                        self._arm_ready_actor_ack(window_s=3.0)
                        self.actions.quick_press_key("space")
                        self.actions.park_mouse(self.screen.parking_regions())
                    self._placement_ready_sent = True
                self._combat_auto_ready_pending = False
                self._combat_auto_ready_at = 0.0
                self.combat_action_until = now + cooldown
                self.no_fuir_count = 0
                self._try_detect_grid(frame)

            elif now > self.combat_deadline:
                print("[BOT] Timeout combate — re-escaneando")
                self.harvested_positions = []
                self.state = self._combat_origin

            else:
                # Sin sniffer: heurística no_fuir_count para detectar fin de combate
                if not self.sniffer_active and self.combat_profile.needs_panel:
                    desplegar = self._find_ui_screen(frame, "DesplegarPanel")
                    if desplegar:
                        print("[BOT] Panel colapsado — desplegando")
                        self.actions.quick_click(desplegar)
                        self.no_fuir_count = 0
                        return
                    self.no_fuir_count += 1
                    if self.no_fuir_count >= FUIR_CONFIRMS:
                        print("[BOT] Combate terminado — reanudando")
                        self.harvested_positions = []
                        self.state = self._combat_origin
            return

        if self._resource_recording_mode and self.config["farming"].get("mode", "resource") == "resource":
            time.sleep(0.1)
            return

        if self.state != "in_combat" and self._maybe_follow_selected_player_from_map_entities("realtime"):
            return

        if self.state in {"scan", "scan_mobs"} and self._maybe_follow_tracked_players():
            return

        if self.state in {"scan", "scan_mobs"}:
            active_teleport_name = self.config.get("active_teleport_profile")
            teleport_enabled = self.config.get("teleport_enabled", bool(active_teleport_name))
            if teleport_enabled and active_teleport_name:
                active_teleport = self.config.get("teleport_profiles", {}).get(active_teleport_name)
                if active_teleport and str(self._current_map_id) == str(active_teleport.get("trigger_map")):
                    print(f"[TELEPORT] Iniciando secuencia de perfil: {active_teleport_name}")
                    self.state = "teleport_start"
                    self._teleport_deadline = now + 1.5
                    self.pending = []
                    self.mob_pending = []
                    return

        if self.state == "scan":
            if farming_mode == "leveling":
                self.state = "scan_mobs"
            else:
                self._scan()

        elif self.state == "scan_mobs":
            self._scan_mobs()

        elif self.state == "click_mob":
            self._click_mob()

        elif self.state == "follow_player_wait":
            self._follow_player_wait()

        elif self.state == "change_map":
            self._change_map()

        elif self.state == "teleport_start":
            active_teleport = self.config.get("teleport_profiles", {}).get(self.config.get("active_teleport_profile"))
            if not active_teleport or str(self._current_map_id) != str(active_teleport.get("trigger_map")):
                self.state = "scan"
                return
            if now < self._teleport_deadline:
                time.sleep(0.1)
                return
            cell_id = active_teleport.get("cell_id")
            pos = self._movement_click_pos_for_cell(int(cell_id)) or self._cell_to_screen(int(cell_id))
            if pos:
                print(f"[TELEPORT] Click en celda: {cell_id}")
                self.screen.focus_window()
                self.actions.quick_click(pos)
                self.state = "teleport_click_use"
                self._teleport_deadline = now + 3.0
            else:
                print(f"[TELEPORT] No se pudo proyectar la celda {cell_id}. Reintentando en breve.")
                self._teleport_deadline = now + 2.0

        elif self.state == "teleport_click_use":
            if now > self._teleport_deadline:
                print("[TELEPORT] Timeout esperando menu contextual, reintentando...")
                self.state = "teleport_start"
                self._teleport_deadline = now + 1.0
                return
            frame = self.screen.capture()
            pos = self._find_ui_screen(frame, "Utilizar")
            if pos:
                print("[TELEPORT] Click en 'Utilizar.png'")
                self.actions.quick_click(pos)
                self.state = "teleport_select_dest"
                self._teleport_deadline = now + 4.0
            time.sleep(0.1)

        elif self.state == "teleport_select_dest":
            if now > self._teleport_deadline:
                print("[TELEPORT] Timeout esperando destino, reintentando...")
                self.state = "teleport_start"
                self._teleport_deadline = now + 1.0
                return
            active_teleport = self.config.get("teleport_profiles", {}).get(self.config.get("active_teleport_profile"))
            dest_image = active_teleport.get("dest_image")
            frame = self.screen.capture()
            pos = self._find_ui_screen(frame, dest_image)
            if pos:
                print(f"[TELEPORT] Click en '{dest_image}.png'")
                self.actions.quick_click(pos)
                self.state = "teleport_confirm"
                self._teleport_deadline = now + 2.0
            time.sleep(0.1)

        elif self.state == "teleport_confirm":
            if now > self._teleport_deadline:
                print("[TELEPORT] Timeout confirmacion, reintentando...")
                self.state = "teleport_start"
                self._teleport_deadline = now + 1.0
                return
            frame = self.screen.capture()
            pos = self._find_ui_screen(frame, "Teletransportarse")
            if pos:
                print("[TELEPORT] Click en 'Teletransportarse.png'")
                self._last_map_id = self._current_map_id
                self._sniffer_map_loaded = False
                self.actions.quick_click(pos)
                self.state = "teleport_wait_map"
                self._teleport_deadline = now + 20.0
            time.sleep(0.1)

        elif self.state == "teleport_wait_map":
            active_name = self.config.get("active_teleport_profile")
            active_teleport = self.config.get("teleport_profiles", {}).get(active_name)
            if not active_teleport:
                print(f"[TELEPORT] Error: Perfil '{active_name}' no encontrado. Abortando.")
                self.state = "scan"
                return
                
            expected_map = str(active_teleport.get("expected_map") or "").strip()
            current_map = str(self._current_map_id).strip()
            trigger_map = str(active_teleport.get("trigger_map") or "").strip()
            
            map_changed = (self._current_map_id is not None and self._last_map_id is not None and self._current_map_id != self._last_map_id) or self._sniffer_map_loaded
            
            if map_changed or (expected_map and current_map == expected_map) or (current_map != trigger_map and current_map):
                print(f"[TELEPORT] Llegamos al destino: {current_map}")
                route_name = active_teleport.get("route_name")
                mode = self.config.get("farming", {}).get("mode", "resource")
                if mode == "resource":
                    self.config.setdefault("farming", {})["route_profile"] = route_name
                else:
                    self.config.setdefault("leveling", {})["route_profile"] = route_name
                
                self.config.setdefault("navigation", {})["enabled"] = True
                
                try:
                    import yaml
                    config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
                    with open(config_path, "r", encoding="utf-8") as f:
                        raw = yaml.safe_load(f)
                    
                    if mode == "resource":
                        raw.setdefault("farming", {})["route_profile"] = route_name
                    else:
                        raw.setdefault("leveling", {})["route_profile"] = route_name
                    
                    raw.setdefault("navigation", {})["enabled"] = True
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
                    print(f"[TELEPORT] Ruta '{route_name}' y navegación activada guardadas en config.")
                except Exception as e:
                    print(f"[BOT] Error guardando config.yaml (route): {e}")

                self.route_index = 0
                
                farm_map = active_teleport.get("farm_map")
                mobs_activate = active_teleport.get("mobs_activate", "")
                if farm_map and str(farm_map).strip():
                    if str(farm_map).strip() == str(self._current_map_id):
                        print(f"[TELEPORT] Ya estamos en el mapa de farmeo ({farm_map}).")
                        self._traveling_to_farm_map = None
                        self._activate_pending_mobs(mobs_activate)
                    else:
                        self._traveling_to_farm_map = str(farm_map).strip()
                        self._mobs_to_activate_on_arrival = mobs_activate
                        print(f"[TELEPORT] Modo viaje activo. Ignorando mobs/recursos hasta map_id={self._traveling_to_farm_map}")
                else:
                    self._traveling_to_farm_map = None
                    self._activate_pending_mobs(mobs_activate)
                    
                self.state = "scan"
                return
            if now > self._teleport_deadline:
                print(f"[TELEPORT] Timeout esperando mapa '{expected_map}' (actual: '{current_map}'), abortando.")
                self.state = "scan"
                return
            time.sleep(0.1)

        elif self.state == "unloading_start":
            # Deshabilitar todos los mobs activos para evitar combate durante la descarga
            mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
            self._unloading_disabled_mobs = [
                name for name, cfg in mobs_cfg.items() if cfg.get("enabled", True)
            ]
            for name in self._unloading_disabled_mobs:
                mobs_cfg[name]["enabled"] = False
            if self._unloading_disabled_mobs:
                print(f"[UNLOAD] Mobs deshabilitados temporalmente: {self._unloading_disabled_mobs}")
            print("[UNLOAD] Usando pócima de recuerdo (tecla 2)...")
            self.screen.focus_window()
            self.actions.quick_press_key("2")
            self._unloading_original_route = self.config.get("farming", {}).get("route_profile")
            mode = self.config.get("farming", {}).get("mode")
            if mode == "leveling":
                self._unloading_original_route = self.config.get("leveling", {}).get("route_profile")
            
            if mode == "resource":
                self.config.setdefault("farming", {})["route_profile"] = "Zaapabanco"
            else:
                self.config.setdefault("leveling", {})["route_profile"] = "Zaapabanco"
                
            try:
                import yaml
                config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
                with open(config_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                if mode == "resource":
                    raw.setdefault("farming", {})["route_profile"] = "Zaapabanco"
                else:
                    raw.setdefault("leveling", {})["route_profile"] = "Zaapabanco"
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
            except Exception:
                pass
            
            self._unloading_wait_until = now + 4.0
            self.state = "unloading_wait_map"

        elif self.state == "unloading_wait_map":
            if now < self._unloading_wait_until:
                time.sleep(0.1)
                return
            self.state = "unloading_navigate"

        elif self.state == "unloading_navigate":
            route_point = self._route_point_for_current_map()
            if route_point is None:
                print("[UNLOAD] Llegamos al destino. Interactuando con cajero en celda 520.")
                self.state = "unloading_interact_banker"
                self._unloading_step_deadline = now + 2.0
            else:
                self.map_change_phase = "click"
                self.state = "change_map"
                self._combat_origin = "unloading_navigate"

        elif self.state == "unloading_interact_banker":
            if now < self._unloading_step_deadline:
                time.sleep(0.1)
                return
            
            target_cell = 520
            banker_entry = self._map_entities.get("-1")
            if banker_entry and banker_entry.get("cell_id") is not None:
                try:
                    target_cell = int(banker_entry["cell_id"])
                    print(f"[UNLOAD] Banquero (actor -1) detectado en celda {target_cell}.")
                except (ValueError, TypeError):
                    pass
            else:
                print("[UNLOAD] Banquero no detectado, usando celda 520 como fallback.")

            projected = self._cell_to_screen(target_cell)
            self.screen.focus_window()
            if projected and self._is_point_on_monitor(projected):
                self.actions.quick_click(projected)
            else:
                print(f"[UNLOAD] No se pudo proyectar celda {target_cell}. Fallback al centro.")
                mon = self.screen.game_region()
                self.actions.quick_click((mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2))
            
            self._unloading_step_deadline = now + 2.0
            self.state = "unloading_click_hablar"

        elif self.state == "unloading_click_hablar":
            if now < self._unloading_step_deadline:
                frame = self.screen.capture()
                pos = self._find_ui_screen(frame, "Hablar")
                if pos:
                    print("[UNLOAD] Click en 'Hablar.png'")
                    self.actions.quick_click(pos)
                    self._unloading_step_deadline = now + 3.0
                    self.state = "unloading_open_bank"
                time.sleep(0.1)
                return
            print("[UNLOAD] No se vio 'Hablar.png', reintentando interactuar con el cajero...")
            self.state = "unloading_interact_banker"

        elif self.state == "unloading_open_bank":
            if now < self._unloading_step_deadline:
                frame = self.screen.capture()
                pos = self._find_ui_screen(frame, "Consultarcaja")
                if pos:
                    print("[UNLOAD] Click en 'Consultarcaja.png'")
                    self.actions.quick_click(pos)
                    self._unloading_step_deadline = now + 2.0
                    self.state = "unloading_transfer_1"
                time.sleep(0.1)
                return
            print("[UNLOAD] Reintentando interactuar con el cajero...")
            self.state = "unloading_interact_banker"

        elif self.state == "unloading_transfer_1":
            if now < self._unloading_step_deadline:
                time.sleep(0.1)
                return
            frame = self.screen.capture()
            pos = self._find_ui_screen_rightmost(frame, "Recursosbank")
            if pos:
                print("[UNLOAD] Click en 'Recursosbank.png'")
                self.actions.quick_click(pos)
                self._unloading_step_deadline = now + 1.0
                self.state = "unloading_transfer_2"
            else:
                print("[UNLOAD] Esperando 'Recursosbank.png'...")
                time.sleep(0.2)

        elif self.state == "unloading_transfer_2":
            if now < self._unloading_step_deadline:
                time.sleep(0.1)
                return
            frame = self.screen.capture()
            pos = self._find_ui_screen_rightmost(frame, "ordenarytransferir")
            if pos:
                print("[UNLOAD] Click en 'ordenarytransferir.png'")
                self.actions.quick_click(pos)
                self._unloading_step_deadline = now + 1.0
                self.state = "unloading_transfer_3"
            else:
                print("[UNLOAD] Esperando 'ordenarytransferir.png'...")
                time.sleep(0.2)

        elif self.state == "unloading_transfer_3":
            if now < self._unloading_step_deadline:
                time.sleep(0.1)
                return
            frame = self.screen.capture()
            pos = self._find_ui_screen_rightmost(frame, "transferirobjetos")
            if pos:
                print("[UNLOAD] Click en 'transferirobjetos.png'")
                self.actions.quick_click(pos)
                self._unloading_step_deadline = now + 1.5
                self.state = "unloading_finish"
            else:
                print("[UNLOAD] Esperando 'transferirobjetos.png'...")
                time.sleep(0.2)

        elif self.state == "unloading_finish":
            if now < self._unloading_step_deadline:
                time.sleep(0.1)
                return
            print("[UNLOAD] Descarga terminada. Presionando ESC y volviendo (tecla 2).")
            print("[UNLOAD] Descarga terminada. Presionando ESC y usando pócima (tecla 2).")
            self.actions.quick_press_key("esc")
            time.sleep(0.5)
            self._last_map_id = self._current_map_id
            self._sniffer_map_loaded = False
            self.actions.quick_press_key("2")
            
            if hasattr(self, "_unloading_original_route"):
                mode = self.config.get("farming", {}).get("mode")
                if mode == "resource":
                    self.config.setdefault("farming", {})["route_profile"] = self._unloading_original_route
                else:
                    self.config.setdefault("leveling", {})["route_profile"] = self._unloading_original_route
                    
                try:
                    import yaml
                    config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
                    with open(config_path, "r", encoding="utf-8") as f:
                        raw = yaml.safe_load(f)
                    if mode == "resource":
                        raw.setdefault("farming", {})["route_profile"] = self._unloading_original_route
                    else:
                        raw.setdefault("leveling", {})["route_profile"] = self._unloading_original_route
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
                except Exception as e:
                    print(f"[BOT] Error restaurando ruta en config.yaml: {e}")
            
            self._unloading_wait_until = now + 5.0
            self._unloading_wait_until = now + 15.0
            self.state = "unloading_wait_return"

        elif self.state == "unloading_wait_return":
            if now < self._unloading_wait_until:
                time.sleep(0.1)
            if self._sniffer_map_loaded or (self._current_map_id is not None and self._last_map_id is not None and self._current_map_id != self._last_map_id):
                print("[UNLOAD] Pócima utilizada y mapa cargado. Retomando actividad normal.")
                self._restore_unloading_mobs()
                self.state = "scan"
                self.route_index = 0
                return
            print("[UNLOAD] Retomando actividad normal.")
            self._restore_unloading_mobs()
            self.state = "scan"
            if now > self._unloading_wait_until:
                print("[UNLOAD] Timeout esperando carga de mapa de pócima. Retomando de todos modos.")
                self.state = "scan"
                self.route_index = 0
                return
            time.sleep(0.1)

        elif self.state == "click_resource":
            if not self.pending:
                self.state = "scan"
                return
            profession, resource_name, pos = self.pending[0]
            print(f"[BOT] Click recurso en {pos} [{profession}/{resource_name}] ({len(self.pending)} restantes)")
            self._last_resource_click = (profession, resource_name, pos)
            self._harvest_sniff_debug_until = now + 6.0
            self._harvest_requested = False
            self._harvest_confirmed = False
            self._harvest_finish_at = 0.0
            self._harvest_request_deadline = 0.0
            self._harvest_menu_fallback_used = False
            self.actions.click(pos)
            self.menu_deadline = now + MENU_TIMEOUT
            self.state = "wait_first_segar"

        elif self.state == "wait_harvest_confirm":
            if self._harvest_confirmed:
                self.state = "harvesting_wait"
                return
            if now < self._harvest_request_deadline:
                time.sleep(0.1)
                return
            print(f"[BOT] Sin confirmacion de cosecha por sniffer en {self.pending[0][2]} — saltando")
            skipped = self.pending.pop(0)
            self._harvest_sniff_debug_until = 0.0
            self._harvest_requested = False
            self._harvest_confirmed = False
            self._harvest_request_deadline = 0.0
            self._harvest_menu_fallback_used = False
            self.harvested_positions.append(skipped[2])
            self.harvested_until = now + 8.0
            self.pending = []
            self.state = "scan"

        elif self.state == "harvesting_wait":
            if now < self._harvest_finish_at:
                time.sleep(0.1)
                return
            if not self.pending:
                self.state = "scan"
                self._harvest_requested = False
                self._harvest_confirmed = False
                self._harvest_sniff_debug_until = 0.0
                self._harvest_request_deadline = 0.0
                self._harvest_menu_fallback_used = False
                return
            profession, resource_name, pos = self.pending.pop(0)
            self._harvest_requested = False
            self._harvest_confirmed = False
            self._harvest_sniff_debug_until = 0.0
            self._harvest_request_deadline = 0.0
            self._harvest_menu_fallback_used = False
            self.collected += 1
            self.last_pos = pos
            self.harvested_positions.append(pos)
            self.harvested_until = now + 5.0
            print(f"[BOT] Cosechado! Total: {self.collected} — re-escaneando mapa")
            self.pending = []
            self.state = "scan"

        elif self.state == "wait_first_segar":
            if self._harvest_confirmed:
                self.state = "harvesting_wait"
                return
            if self._harvest_requested:
                self.state = "wait_harvest_confirm"
                return
            if self._resource_sniffer_only_mode():
                frame = self.screen.capture()
                _, _, resource_pos = self.pending[0]
                menu_pos = self._find_harvest_menu_option(frame, resource_pos)
                if menu_pos:
                    print(f"[BOT] Menu de cosecha detectado — clickeando opcion en {menu_pos}")
                    self.actions.quick_click(menu_pos)
                    self._harvest_menu_fallback_used = True
                    self._harvest_request_deadline = now + 3.0
                    self.state = "wait_harvest_confirm"
                    return
                if not self._harvest_menu_fallback_used and now > self.menu_deadline - 3.3:
                    fallback_pos = self._fallback_harvest_menu_click(resource_pos)
                    if fallback_pos:
                        print(f"[BOT] Fallback menu de cosecha — clickeando opcion en {fallback_pos}")
                        self.actions.quick_click(fallback_pos)
                        self._harvest_menu_fallback_used = True
                        self._harvest_request_deadline = now + 3.0
                        self.state = "wait_harvest_confirm"
                        return
                if now > self.menu_deadline:
                    print(f"[BOT] Sin menu de cosecha en {self.pending[0][2]} — saltando")
                    skipped = self.pending.pop(0)
                    self._harvest_sniff_debug_until = 0.0
                    self._harvest_requested = False
                    self._harvest_confirmed = False
                    self._harvest_request_deadline = 0.0
                    self._harvest_menu_fallback_used = False
                    self.harvested_positions.append(skipped[2])
                    self.harvested_until = now + 8.0
                    self.pending = []
                    self.state = "scan"
                return
            if now > self.menu_deadline:
                skipped = self.pending.pop(0)
                self._harvest_sniff_debug_until = 0.0
                self._harvest_requested = False
                self._harvest_confirmed = False
                self._harvest_request_deadline = 0.0
                self.harvested_positions.append(skipped[2])
                self.harvested_until = now + 8.0
                self.pending = []
                self.state = "scan"

        elif self.state == "spam_segar":
            # Legacy desactivado: el flujo de recursos usa solo sniffer + menu geometrico.
            self.state = "wait_harvest_confirm" if self._harvest_requested else "click_resource"

    def _already_harvested(self, pos: tuple[int, int], radius: int = 50) -> bool:
        return any(abs(pos[0] - hp[0]) < radius and abs(pos[1] - hp[1]) < radius
                   for hp in self.harvested_positions)

    def _enabled_resource_names(self) -> set[tuple[str, str]]:
        enabled: set[tuple[str, str]] = set()
        professions = self.config["farming"].get("professions", {})
        for prof_name, prof_data in professions.items():
            if not prof_data.get("enabled", True):
                continue
            for resource in prof_data.get("resources", []):
                enabled.add((prof_name, resource))
        return enabled

    def _remember_sniffer_event(self, event: str, data: dict):
        if event == "raw_packet":
            return
        summary = {"event": event, "at": time.time()}
        if isinstance(data, dict):
            for key in ("actor_id", "cell_id", "map_id", "team_id", "entity_kind", "operation", "source"):
                if key in data:
                    summary[key] = data.get(key)
            if "entries" in data and isinstance(data["entries"], list):
                summary["count"] = len(data["entries"])
            raw = data.get("raw")
            if raw:
                summary["raw"] = str(raw)[:180]
        self._recent_sniffer_events.append(summary)

    def _remember_recent_actor_cell(self, actor_id: str | None, cell_id: int | None, *, source: str = ""):
        actor = str(actor_id or "").strip()
        if not actor or cell_id is None:
            return
        try:
            cell = int(cell_id)
        except (TypeError, ValueError):
            return
        entry = self._recent_actor_cells.setdefault(actor, {})
        entry["cell_id"] = cell
        entry["at"] = time.time()
        entry["source"] = source

    def _handle_probable_external_fight(self, removed_entry: dict | None):
        if not removed_entry:
            return
        if str(removed_entry.get("entity_kind", "")).strip() != "mob_group":
            return
        removed_at = time.time()
        removed_cell = removed_entry.get("cell_id")
        if removed_cell is None:
            return
        try:
            mob_cell = int(removed_cell)
        except (TypeError, ValueError):
            return
        candidates: list[tuple[int, float, float, str, int]] = []
        for actor_id, recent in list(self._recent_actor_cells.items()):
            if not str(actor_id).lstrip("+-").isdigit() or int(actor_id) <= 0:
                continue
            seen_at = float(recent.get("at", 0.0) or 0.0)
            if seen_at <= 0 or (removed_at - seen_at) > 2.5:
                continue
            try:
                actor_cell = int(recent.get("cell_id"))
            except (TypeError, ValueError):
                continue
            gx1, gy1 = cell_id_to_grid(mob_cell, int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15))
            gx2, gy2 = cell_id_to_grid(actor_cell, int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15))
            distance = abs(gx1 - gx2) + abs(gy1 - gy2)
            if distance > 8:
                continue
            age = removed_at - seen_at
            exact_cell = 0 if actor_cell == mob_cell else 1
            candidates.append((exact_cell, distance, age, actor_id, actor_cell))
        if not candidates:
            pending = {
                "mob_actor_id": str(removed_entry.get("actor_id", "")).strip(),
                "mob_cell": mob_cell,
                "fight_cell": mob_cell,
                "at": removed_at,
                "starter_actor_id": None,
                "starter_cell": None,
                "joiners": [],
            }
            self._recent_removed_mob_groups.append(pending)
            self._external_fight_pending = dict(pending)
            return
        candidates.sort(key=lambda item: (item[0], item[1], item[2], int(item[3])))
        starter_actor = candidates[0][3]
        starter_cell = candidates[0][4]
        joiners = [actor_id for _, _, _, actor_id, _ in candidates[1:4]]
        pending = {
            "mob_actor_id": str(removed_entry.get("actor_id", "")).strip(),
            "mob_cell": mob_cell,
            "fight_cell": mob_cell,
            "at": removed_at,
            "starter_actor_id": starter_actor,
            "starter_cell": starter_cell,
            "joiners": list(joiners),
        }
        self._recent_removed_mob_groups.append(pending)
        self._external_fight_pending = dict(pending)
        print(
            f"[SNIFFER] Pelea ajena detectada: mob_actor={removed_entry.get('actor_id')} "
            f"mob_cell={mob_cell} starter≈{starter_actor}@{starter_cell} joiners={joiners}"
        )

    def _refine_recent_external_fight_with_removed_actor(self, actor_id: str):
        actor = str(actor_id or "").strip()
        if not actor or not actor.lstrip("+-").isdigit() or int(actor) <= 0:
            return
        recent = self._recent_actor_cells.get(actor)
        if not recent:
            return
        try:
            actor_cell = int(recent.get("cell_id"))
        except (TypeError, ValueError):
            return
        now = time.time()
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15)
        for item in reversed(self._recent_removed_mob_groups):
            age = now - float(item.get("at", 0.0) or 0.0)
            if age > 1.2:
                break
            try:
                mob_cell = int(item.get("mob_cell"))
            except (TypeError, ValueError):
                continue
            gx1, gy1 = cell_id_to_grid(mob_cell, map_width)
            gx2, gy2 = cell_id_to_grid(actor_cell, map_width)
            distance = abs(gx1 - gx2) + abs(gy1 - gy2)
            if distance > 8:
                continue
            exact_same_cell = actor_cell == mob_cell
            starter = str(item.get("starter_actor_id") or "").strip()
            joiners = list(item.get("joiners") or [])
            if exact_same_cell or starter != actor:
                if starter and starter not in joiners:
                    joiners.insert(0, starter)
                item["starter_actor_id"] = actor
                item["starter_cell"] = actor_cell
            if exact_same_cell:
                item["fight_cell"] = actor_cell
            if actor in joiners:
                joiners = [value for value in joiners if value != actor]
            item["joiners"] = joiners[:4]
            self._external_fight_pending = dict(item)
            print(
                f"[SNIFFER] Pelea ajena refinada: mob_actor={item.get('mob_actor_id')} "
                f"mob_cell={mob_cell} starter≈{item.get('starter_actor_id')}@{item.get('starter_cell')} "
                f"joiners={item.get('joiners')}"
            )
            break

    def _promote_selected_follow_actor_fight(self, actor_id: str) -> bool:
        actor = str(actor_id or "").strip()
        if not actor or actor != self._selected_follow_actor_id():
            return False
        memory = self._follow_player_memory.get(actor) or {}
        try:
            actor_cell = int(memory.get("cell_id"))
        except (TypeError, ValueError):
            return False
        try:
            prev_cell = int(memory.get("prev_cell_id"))
        except (TypeError, ValueError):
            prev_cell = None
        now = time.time()
        map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15)
        for item in reversed(self._recent_removed_mob_groups):
            age = now - float(item.get("at", 0.0) or 0.0)
            if age > 2.0:
                break
            try:
                mob_cell = int(item.get("mob_cell"))
            except (TypeError, ValueError):
                continue
            gx1, gy1 = cell_id_to_grid(mob_cell, map_width)
            gx2, gy2 = cell_id_to_grid(actor_cell, map_width)
            distance = abs(gx1 - gx2) + abs(gy1 - gy2)
            if distance > 8:
                continue
            starter = str(item.get("starter_actor_id") or "").strip()
            joiners = [value for value in list(item.get("joiners") or []) if value != actor]
            if starter and starter != actor and starter not in joiners:
                joiners.insert(0, starter)
            item["starter_actor_id"] = actor
            item["starter_cell"] = actor_cell
            if prev_cell is not None:
                item["rally_cell"] = prev_cell
            item["joiners"] = joiners[:4]
            self._external_fight_pending = dict(item)
            print(
                f"[SNIFFER] Pelea ajena promovida por player seguido: "
                f"mob_actor={item.get('mob_actor_id')} mob_cell={mob_cell} "
                f"starter≈{actor}@{actor_cell} rally={item.get('rally_cell')} joiners={item.get('joiners')}"
            )
            return True
        return False

    def _seed_selected_follow_actor_fight(self, actor_id: str) -> bool:
        actor = str(actor_id or "").strip()
        if not actor or actor != self._selected_follow_actor_id():
            return False
        memory = self._follow_player_memory.get(actor) or {}
        try:
            actor_cell = int(memory.get("cell_id"))
        except (TypeError, ValueError):
            return False
        try:
            prev_cell = int(memory.get("prev_cell_id"))
        except (TypeError, ValueError):
            prev_cell = None
        pending = {
            "mob_actor_id": None,
            "mob_cell": None,
            "fight_cell": None,
            "at": time.time(),
            "starter_actor_id": actor,
            "starter_cell": actor_cell,
            "rally_cell": prev_cell,
            "joiners": [],
        }
        self._external_fight_pending = dict(pending)
        print(
            f"[SNIFFER] Pelea ajena sembrada por player seguido: "
            f"starter≈{actor}@{actor_cell} rally={prev_cell}"
        )
        return True

    def _attempt_join_external_fight(self) -> bool:
        if getattr(self, "_traveling_to_farm_map", None):
            return False
        pending = self._external_fight_pending
        if not pending or self.state == "in_combat":
            return False
        now = time.time()
        starter_actor = str(pending.get("starter_actor_id") or "").strip()
        leveling_cfg = self.config.get("leveling", {})
        join_any = bool(leveling_cfg.get("join_external_fights_any", False))
        selected_actor = str(leveling_cfg.get("join_external_fights_actor_id", "") or "").strip()
        effective_actor = starter_actor
        go_visible = str(pending.get("go_packet") or "").startswith("Go+P")
        if not effective_actor and go_visible and selected_actor:
            effective_actor = selected_actor
        if not join_any:
            if not selected_actor:
                return False
            if effective_actor != selected_actor:
                return False
        if not go_visible:
            return False
        if not effective_actor:
            return False

        try:
            rally_cell = int(pending.get("rally_cell"))
        except (TypeError, ValueError):
            rally_cell = None
        join_ready_at = float(pending.get("join_ready_at", 0.0) or 0.0)
        if rally_cell is not None and not bool(pending.get("rally_started")):
            rally_pos = self._cell_to_screen(rally_cell)
            if rally_pos is not None and self._is_point_on_monitor(rally_pos):
                print(f"[BOT] Reposicionando para unirse: rally_cell={rally_cell} pos={rally_pos}")
                self.screen.focus_window()
                self.actions.click(self._movement_click_pos_for_cell(rally_cell) or rally_pos)
                pending["rally_started"] = True
                pending["join_ready_at"] = now + 3.0
                self._external_fight_pending = dict(pending)
                return False
        if join_ready_at > now:
            return False

        candidate_cells: list[int] = []
        for raw_cell in (pending.get("starter_cell"), pending.get("fight_cell"), pending.get("mob_cell")):
            try:
                cell = int(raw_cell)
            except (TypeError, ValueError):
                continue
            if cell not in candidate_cells:
                candidate_cells.append(cell)
        entry = self._map_entities.get(effective_actor)
        if entry and entry.get("cell_id") is not None:
            try:
                current_cell = int(entry.get("cell_id"))
            except (TypeError, ValueError):
                current_cell = None
            if current_cell is not None and current_cell not in candidate_cells:
                candidate_cells.insert(0, current_cell)
        if not candidate_cells:
            print(f"[BOT] No pude unirme: starter={effective_actor or '-'} sin celdas candidatas")
            return False

        self.screen.focus_window()
        for target_cell in candidate_cells:
            projected = self._cell_to_screen(target_cell)
            if projected is None or not self._is_point_on_monitor(projected):
                print(f"[BOT] No pude unirme: starter={effective_actor or '-'} cell={target_cell} sin proyeccion")
                continue
            print(
                f"[BOT] Intentando unirme a pelea ajena: starter={effective_actor or '-'} "
                f"cell={target_cell} pos={projected} "
                f"go={str(pending.get('go_packet', '') or '-')}"
            )
            self.actions.quick_click(projected)
            time.sleep(self.config["bot"].get("combat_menu_open_delay", 0.2))
            frame = self.screen.capture()
            join_pos = self._find_ui_screen(frame, "Unirse")
            if join_pos:
                print(f"[BOT] Menu de pelea detectado en {join_pos} - clickeando")
                self.actions.quick_click(join_pos)
                joined = self._wait_for_combat_entry(attack_pos=join_pos)
                if joined:
                    print(f"[BOT] Union a pelea ajena confirmada starter={effective_actor or '-'} cell={target_cell}")
                    self._external_fight_pending = None
                    return True
                continue

            offset = self.config.get("leveling", {}).get("attack_menu_offset", [-60, 30])
            candidate_offsets = [
                (int(offset[0]), int(offset[1])),
                (int(offset[0]) - 20, int(offset[1])),
                (int(offset[0]) + 20, int(offset[1])),
                (int(offset[0]), int(offset[1]) - 15),
                (int(offset[0]), int(offset[1]) + 15),
            ]
            tried_offsets: set[tuple[int, int]] = set()
            for dx, dy in candidate_offsets:
                if (dx, dy) in tried_offsets:
                    continue
                tried_offsets.add((dx, dy))
                join_pos = (projected[0] + dx, projected[1] + dy)
                print(f"[BOT] Unirse no detectado - fallback offset {join_pos}")
                self.actions.quick_click(join_pos)
                joined = self._wait_for_combat_entry(attack_pos=join_pos)
                if joined:
                    print(f"[BOT] Union a pelea ajena confirmada starter={effective_actor or '-'} cell={target_cell}")
                    self._external_fight_pending = None
                    return True
        print(f"[BOT] No pude unirme a pelea ajena starter={effective_actor or '-'} cells={candidate_cells}")
        return False

    def get_map_entities_snapshot(self) -> list[dict]:
        rows = []
        for entry in self._map_entities.values():
            item = dict(entry)
            item["resolved_mobs"] = self._resolve_sniffed_mob_names(entry)
            cell_id = item.get("cell_id")
            if cell_id is not None:
                try:
                    map_width = int(self.config.get("bot", {}).get("cell_calibration", {}).get("map_width", 15) or 15)
                    item["grid_xy"] = cell_id_to_grid(int(cell_id), map_width)
                except (TypeError, ValueError):
                    item["grid_xy"] = None
            rows.append(item)
        return sorted(
            rows,
            key=lambda entry: (
                0 if entry.get("entity_kind") in {"mob", "mob_group"} else 1,
                int(entry.get("cell_id") if entry.get("cell_id") is not None else 0),
                str(entry.get("actor_id", "")),
            ),
        )

    def get_current_map_cells_snapshot(self) -> list[dict]:
        return [dict(cell) for cell in self._current_map_cells]

    def get_recent_sniffer_events(self) -> list[dict]:
        return list(self._recent_sniffer_events)

    def _template_name_database(self) -> dict[int, str]:
        db_cfg = self.config.get("leveling", {}).get("template_id_db", {})
        resolved: dict[int, str] = {}
        for raw_id, raw_name in db_cfg.items():
            try:
                template_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = str(raw_name).strip()
            if name:
                resolved[template_id] = name
        return resolved

    def _configured_mob_template_ids(self) -> dict[str, set[int]]:
        mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
        mapping: dict[str, set[int]] = {}
        for mob_name, mob_cfg in mobs_cfg.items():
            if not mob_cfg.get("enabled", True):
                continue
            values = mob_cfg.get("template_ids", [])
            ids: set[int] = set()
            for value in values:
                try:
                    ids.add(int(value))
                except (TypeError, ValueError):
                    continue
            if ids:
                mapping[mob_name] = ids
        return mapping

    def _configured_ignored_mobs(self) -> dict[str, set[int]]:
        mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
        mapping: dict[str, set[int]] = {}
        for mob_name, mob_cfg in mobs_cfg.items():
            if not mob_cfg.get("ignore", False):
                continue
            ids: set[int] = set()
            for value in mob_cfg.get("template_ids", []):
                try:
                    ids.add(int(value))
                except (TypeError, ValueError):
                    continue
            mapping[mob_name] = ids
        return mapping

    def _configured_veto_mob_template_ids(self) -> set[int]:
        leveling_cfg = self.config.get("leveling", {})
        raw = leveling_cfg.get("mob_group_veto_template_ids", [])
        values: list[int] = []
        if isinstance(raw, str):
            tokens = [token.strip() for token in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            tokens = [str(token).strip() for token in raw]
        else:
            tokens = []
        for token in tokens:
            if not token:
                continue
            try:
                values.append(int(token))
            except ValueError:
                continue
        return set(values)

    def _resolve_sniffed_mob_names(self, entry: dict) -> list[str]:
        template_ids = {
            int(value) for value in entry.get("template_ids", [])
            if isinstance(value, int) or str(value).lstrip("+-").isdigit()
        }
        if not template_ids:
            return []
        matches: list[str] = []
        template_db = self._template_name_database()
        for template_id in sorted(template_ids):
            db_name = template_db.get(template_id)
            if db_name and db_name not in matches:
                matches.append(db_name)
        for mob_name, configured_ids in self._configured_mob_template_ids().items():
            if template_ids & configured_ids:
                matches.append(mob_name)
        deduped: list[str] = []
        for name in matches:
            if name not in deduped:
                deduped.append(name)
        return deduped

    def _sniffed_entry_template_ids(self, entry: dict) -> set[int]:
        values: set[int] = set()
        for value in entry.get("template_ids", []):
            try:
                values.add(int(value))
            except (TypeError, ValueError):
                continue
        return values

    def _sniffed_map_mob_candidates(self) -> list[dict]:
        candidates: list[dict] = []
        configured = self._configured_mob_template_ids()
        ignored_mobs = self._configured_ignored_mobs()
        veto_template_ids = self._configured_veto_mob_template_ids()
        ignore_single_mob_groups = bool(self.config.get("leveling", {}).get("ignore_single_mob_groups", False))
        if not configured:
            return candidates
        for entry in self._map_entities.values():
            if entry.get("entity_kind") not in {"mob", "mob_group"}:
                continue
            template_ids = self._sniffed_entry_template_ids(entry)
            total_monsters = 0
            try:
                total_monsters = int(entry.get("total_monsters", 0) or 0)
            except (TypeError, ValueError):
                total_monsters = 0
            if ignore_single_mob_groups and total_monsters == 1:
                actor_id = str(entry.get("actor_id", "")).strip() or "?"
                print(f"[BOT] Grupo ignorado por 1 mob: actor={actor_id} template_ids={sorted(template_ids)}")
                continue
            matched_configured = [
                mob_name
                for mob_name, configured_ids in configured.items()
                if template_ids & configured_ids
            ]
            if configured and not matched_configured:
                continue
            resolved = self._resolve_sniffed_mob_names(entry)
            ignored_hits = sorted({
                mob_name
                for mob_name in resolved + matched_configured
                if mob_name in ignored_mobs
            })
            ignored_template_hits = sorted({
                template_id
                for mob_name, configured_ids in ignored_mobs.items()
                if template_ids & configured_ids
                for template_id in (template_ids & configured_ids)
            })
            if ignored_hits or ignored_template_hits:
                actor_id = str(entry.get("actor_id", "")).strip() or "?"
                print(
                    f"[BOT] Grupo ignorado por mob marcado: actor={actor_id} "
                    f"mobs={resolved or matched_configured} "
                    f"ignored={ignored_hits or '[template_id]'} "
                    f"template_ids={ignored_template_hits}"
                )
                continue
            veto_hits = sorted(template_ids & veto_template_ids)
            if veto_hits:
                actor_id = str(entry.get("actor_id", "")).strip() or "?"
                print(f"[BOT] Mob vetado omitido: actor={actor_id} mobs={resolved} veto_template_ids={veto_hits}")
                continue
            enriched = dict(entry)
            enriched["resolved_mobs"] = matched_configured or resolved
            candidates.append(enriched)
        return candidates

    def _sniffed_projected_mob_targets(self) -> list[tuple[str, tuple[int, int]]]:
        targets: list[tuple[str, tuple[int, int]]] = []
        for entry in self._sniffed_map_mob_candidates():
            cell_id = entry.get("cell_id")
            if cell_id is None:
                continue
            try:
                projected = self._cell_to_screen(int(cell_id))
            except (TypeError, ValueError):
                continue
            if not self._is_point_on_monitor(projected):
                continue
            resolved = entry.get("resolved_mobs") or []
            target_name = resolved[0] if resolved else str(entry.get("mob_signature") or entry.get("actor_id"))
            targets.append((target_name, projected))
        return targets

    def _sniffer_attack_target_still_valid(self, attack_pos: tuple[int, int] | None) -> bool:
        if not self.sniffer_active or attack_pos is None:
            return True
        ax, ay = int(attack_pos[0]), int(attack_pos[1])
        nearest_mob = None
        nearest_marker = None
        for entry in self._map_entities.values():
            kind = str(entry.get("entity_kind", "")).strip()
            if kind not in {"mob", "mob_group", "fight_marker"}:
                continue
            projected = self.project_map_entity_to_screen(entry)
            if not projected:
                continue
            px, py = projected["screen_pos"]
            dist2 = (px - ax) ** 2 + (py - ay) ** 2
            if kind in {"mob", "mob_group"}:
                if nearest_mob is None or dist2 < nearest_mob[0]:
                    nearest_mob = (dist2, projected, entry)
            elif kind == "fight_marker":
                if nearest_marker is None or dist2 < nearest_marker[0]:
                    nearest_marker = (dist2, projected, entry)
        mob_dist = (nearest_mob[0] ** 0.5) if nearest_mob is not None else None
        marker_dist = (nearest_marker[0] ** 0.5) if nearest_marker is not None else None
        if marker_dist is not None and marker_dist <= 95 and (mob_dist is None or mob_dist > 95):
            marker_entry = nearest_marker[2]
            if self._combat_probe_until <= time.time():
                self._arm_combat_probe("MobTakenByOther", attack_pos)
            print(
                f"[BOT] Target invalidado por pelea ajena: "
                f"fight_marker actor={marker_entry.get('actor_id')} cell={marker_entry.get('cell_id')} "
                f"dist={marker_dist:.1f}"
            )
            return False
        if mob_dist is None:
            if self._combat_probe_until <= time.time():
                self._arm_combat_probe("MobDisappeared", attack_pos)
            print("[BOT] Target invalidado: el mob ya no sigue visible por sniffer")
            return False
        return True

    def _sniffed_follow_player_candidates(self) -> list[dict]:
        if not self._follow_players_enabled():
            return []
        candidates: list[dict] = []
        for actor_id in self._configured_follow_player_actor_ids():
            entry = self._map_entities.get(actor_id)
            if not entry:
                continue
            cell_id = entry.get("cell_id")
            if cell_id is None or self._actor_ids_match(actor_id, self._sniffer_my_actor):
                continue
            projected = self._cell_to_screen(int(cell_id))
            click_pos = self._movement_click_pos_for_cell(int(cell_id))
            if not projected or not click_pos or not self._is_point_on_monitor(click_pos):
                continue
            enriched = dict(entry)
            enriched["screen_pos"] = projected
            enriched["click_pos"] = click_pos
            candidates.append(enriched)
        return candidates

    def _selected_follow_player_entry(self) -> tuple[str, dict] | None:
        if not self.sniffer_active or not self._follow_players_enabled():
            return None
        actor_id = self._selected_follow_actor_id()
        if not actor_id:
            return None
        entry = self._map_entities.get(actor_id)
        if not entry:
            return None
        cell_id = entry.get("cell_id")
        if cell_id is None or self._actor_ids_match(actor_id, self._sniffer_my_actor):
            return None
        return actor_id, entry

    def _selected_follow_player_sig(self, actor_id: str, entry: dict) -> tuple[int | None, int | None] | None:
        cell_id = entry.get("cell_id")
        if cell_id is None:
            return None
        try:
            return (self._current_map_id, int(cell_id))
        except (TypeError, ValueError):
            return None

    def _maybe_follow_selected_player_from_map_entities(self, reason: str) -> bool:
        if self.state == "in_combat":
            return False
        selected = self._selected_follow_player_entry()
        if not selected:
            return False
        actor_id, entry = selected
        sig = self._selected_follow_player_sig(actor_id, entry)
        if sig is None:
            return False
        last_sig = self._follow_player_last_seen_sig.get(actor_id)
        if last_sig == sig:
            return False
        cell_id = sig[1]
        projected = self._cell_to_screen(cell_id)
        click_pos = self._movement_click_pos_for_cell(cell_id)
        if not projected or not click_pos or not self._is_point_on_monitor(click_pos):
            print(
                f"[BOT] Follow descartado actor={actor_id} cell={cell_id} "
                f"reason={reason} projected={projected} click_pos={click_pos}"
            )
            return False
        started = self._start_follow_player_click(actor_id, cell_id, projected, reason, click_pos=click_pos)
        if started:
            self._follow_player_last_seen_sig[actor_id] = sig
        else:
            print(
                f"[BOT] Follow omitido actor={actor_id} cell={cell_id} "
                f"reason={reason} cooldown_o_estado"
            )
        return started

    def _start_follow_player_click(
        self,
        actor_id: str,
        cell_id: int,
        pos: tuple[int, int],
        reason: str,
        *,
        click_pos: tuple[int, int] | None = None,
    ) -> bool:
        now = time.time()
        signature = (self._current_map_id, actor_id, cell_id, reason)
        cooldown = float(self.config["bot"].get("follow_player_click_cooldown", 1.2) or 1.2)
        if signature == self._follow_player_last_action_sig and (now - self._follow_player_last_action_at) < cooldown:
            return False
        grid = cell_id_to_grid(cell_id)
        target_click = tuple(click_pos) if click_pos is not None else pos
        print(
            f"[BOT] Siguiendo player actor={actor_id} cell={cell_id} grid={grid} "
            f"cell_pos={pos} click_pos={target_click} reason={reason}"
        )
        self.screen.focus_window()
        self.actions.click(target_click)
        self._follow_player_last_action_sig = signature
        self._follow_player_last_action_at = now
        memory = self._follow_player_memory.get(actor_id)
        if memory is not None:
            memory["follow_pending"] = False
        self._follow_player_pending = {
            "actor_id": actor_id,
            "cell_id": cell_id,
            "map_id": self._current_map_id,
            "reason": reason,
            "pos": pos,
            "click_pos": target_click,
        }
        wait_delay = float(self.config["bot"].get("follow_player_wait_delay", 0.9) or 0.9)
        self._follow_player_wait_until = now + max(0.2, wait_delay)
        self.map_change_deadline = now + MAP_CHANGE_TIMEOUT
        self._last_map_id = self._current_map_id
        self._sniffer_map_loaded = False
        self.state = "follow_player_wait"
        return True

    def _maybe_follow_tracked_players(self) -> bool:
        return self._maybe_follow_selected_player_from_map_entities("visible")

    def _maybe_follow_tracked_players_on_event(self) -> bool:
        if self.state == "in_combat":
            return False
        return self._maybe_follow_tracked_players()

    def _maybe_follow_selected_player_event(self, actor_id: str, entry: dict | None, reason: str) -> bool:
        selected_actor = self._selected_follow_actor_id()
        if not selected_actor or str(actor_id).strip() != selected_actor:
            return False
        if entry is None and reason in {"map_actor_removed", "map_actor_batch_removed"}:
            if self._combat_probe_until <= time.time():
                memory = self._follow_player_memory.get(selected_actor) or {}
                target_cell = memory.get("cell_id")
                target_pos = None
                try:
                    target_pos = self._cell_to_screen(int(target_cell))
                except (TypeError, ValueError):
                    target_pos = None
                self._arm_combat_probe("FollowedPlayerFight", target_pos)
            promoted = self._promote_selected_follow_actor_fight(selected_actor)
            if not promoted and not self._external_fight_pending:
                self._seed_selected_follow_actor_fight(selected_actor)
            if self._attempt_join_external_fight():
                return True
        return self._maybe_follow_selected_player_from_map_entities(f"event:{reason}")

    def _follow_player_wait(self):
        if self.test_mode:
            self.state = "scan_mobs"
            return
        now = time.time()
        if self.sniffer_active:
            self._drain_sniff_queue()
        if self._maybe_follow_selected_player_from_map_entities("realtime_wait"):
            return
        pending = self._follow_player_pending
        if not pending:
            self.state = "scan_mobs"
            return
        if (
            self._current_map_id is not None
            and pending.get("map_id") is not None
            and self._current_map_id != pending.get("map_id")
        ):
            print(f"[BOT] Seguimiento confirmado por cambio de mapa actor={pending.get('actor_id')}")
            self.empty_scan_count = 0
            self.empty_mob_scan_count = 0
            self._follow_player_pending = None
            self.state = "scan_mobs"
            return
        actor_id = str(pending.get("actor_id", "")).strip()
        entry = self._map_entities.get(actor_id)
        if entry and entry.get("cell_id") is not None and int(entry.get("cell_id")) != int(pending.get("cell_id")):
            new_cell = int(entry.get("cell_id"))
            projected = self._cell_to_screen(new_cell)
            click_pos = self._movement_click_pos_for_cell(new_cell)
            if projected and click_pos and self._is_point_on_monitor(click_pos):
                self._follow_player_last_seen_sig[actor_id] = (self._current_map_id, new_cell)
                print(f"[BOT] Seguimiento actualizado actor={actor_id} nueva_cell={new_cell} - reintentando")
                self._follow_player_pending = None
                self._start_follow_player_click(actor_id, new_cell, projected, "updated", click_pos=click_pos)
                return
            print(f"[BOT] Seguimiento actualizado actor={actor_id} nueva_cell={new_cell} sin proyeccion valida")
            self._follow_player_pending = None
            self.state = "scan_mobs"
            return
        if now >= self._follow_player_wait_until or now >= self.map_change_deadline:
            self._follow_player_pending = None
            self.state = "scan_mobs"
            return
        time.sleep(0.02)

    def _schedule_sniffer_mob_attack(self, reason: str) -> bool:
        if self.config.get("farming", {}).get("mode", "resource") != "leveling":
            return False
        if self.state not in {"scan", "scan_mobs"}:
            return False
        projected_targets = self._sniffed_projected_mob_targets()
        if not projected_targets:
            map_id = self._current_map_id
            if map_id != self._last_missing_projection_warn_map_id:
                print(f"[BOT] map_id={map_id} sin calibración específica - no atacaré por sniffer aún")
                self._last_missing_projection_warn_map_id = map_id
            return False
        if self.test_mode:
            target_name, projected = projected_targets[0]
            print(
                f"[TEST] map_id={self._current_map_id} target={target_name} "
                f"pos={projected} por {reason}"
            )
            return False
        game_region = self.screen.game_region()
        cx = game_region["left"] + game_region["width"] // 2
        cy = game_region["top"] + game_region["height"] // 2
        projected_targets.sort(key=lambda m: (m[1][0] - cx) ** 2 + (m[1][1] - cy) ** 2)
        self.mob_pending = projected_targets
        self.empty_mob_scan_count = 0
        self.state = "click_mob"
        print(
            f"[BOT] Sniffer realtime programó {len(projected_targets)} target(s) "
            f"por {reason} en map_id={self._current_map_id}"
        )
        return True

    def _resource_nodes_for_current_map(self) -> list[tuple[str, tuple[int, int]]]:
        """Devuelve nodos configurados para el map_id actual.

        Formato esperado en config.yaml:
          farming:
            resource_nodes_by_map_id:
              "7423":
                - profession: Campesino
                  resource: Trigo
                  pos: [1234, 567]
        """
        map_id = self._current_map_id
        if map_id is None:
            return []
        nodes_cfg = self.config["farming"].get("resource_nodes_by_map_id", {})
        map_nodes = nodes_cfg.get(str(map_id), nodes_cfg.get(map_id, []))
        if not map_nodes:
            return []

        enabled = self._enabled_resource_names()
        nodes: list[tuple[str, tuple[int, int]]] = []
        for node in map_nodes:
            profession = str(node.get("profession", "")).strip()
            resource = str(node.get("resource", "")).strip()
            pos = node.get("pos")
            if not profession or not resource or not isinstance(pos, (list, tuple)) or len(pos) != 2:
                continue
            if (profession, resource) not in enabled:
                continue
            try:
                x = int(pos[0])
                y = int(pos[1])
            except (TypeError, ValueError):
                continue
            nodes.append((profession, resource, (x, y)))
        return nodes

    def _resource_visible_at_node(
        self,
        frame: np.ndarray,
        resource_name: str,
        pos: tuple[int, int],
        profession: str | None = "Campesino",
        search_radius_x: int = 90,
        search_radius_y: int = 90,
        max_distance: int = 45,
    ) -> bool:
        """Valida visualmente si el recurso esperado sigue visible cerca del nodo."""
        mon = self.screen.game_region()
        frame_h, frame_w = frame.shape[:2]
        px = int(pos[0] - mon["left"])
        py = int(pos[1] - mon["top"])

        x1 = max(0, px - search_radius_x)
        y1 = max(0, py - search_radius_y)
        x2 = min(frame_w, px + search_radius_x)
        y2 = min(frame_h, py + search_radius_y)
        if x2 - x1 < 20 or y2 - y1 < 20:
            return False

        crop = frame[y1:y2, x1:x2]
        matches = self.detector.find_all_resources(crop, resource_name, profession=profession)
        if not matches:
            print(f"[DIAG] node_check resource={resource_name} pos={pos} visible=False matches=0")
            return False

        abs_matches = [
            (int(mon["left"] + x1 + mx), int(mon["top"] + y1 + my))
            for mx, my in matches
        ]
        best_dist = min(
            ((mx - pos[0]) ** 2 + (my - pos[1]) ** 2) ** 0.5
            for mx, my in abs_matches
        )
        visible = best_dist <= max_distance
        print(
            f"[DIAG] node_check resource={resource_name} pos={pos} "
            f"visible={visible} matches={len(abs_matches)} best_dist={best_dist:.1f}"
        )
        return visible

    def _sort_by_proximity(self, items: list[tuple[str, str, tuple[int, int]]]) -> list[tuple[str, str, tuple[int, int]]]:
        if not self.last_pos:
            return items
        ref = self.last_pos
        return sorted(items, key=lambda item: (item[2][0] - ref[0]) ** 2 + (item[2][1] - ref[1]) ** 2)

    def _resource_sniffer_only_mode(self) -> bool:
        return (
            self.config["farming"].get("mode", "resource") == "resource"
            and self.sniffer_active
        )

    def _next_route_point(self) -> tuple[int, int] | None:
        nav = self._active_navigation_config()
        route = nav.get("route", [])
        if not route:
            return None
        idx = self.route_index % len(route)
        point = route[idx]
        self.route_index = (self.route_index + 1) % len(route)
        try:
            return int(point[0]), int(point[1])
        except (TypeError, ValueError, IndexError):
            return None

    def _route_point_for_current_map(self) -> tuple[int, int] | None:
        nav = self._active_navigation_config()
        routes_by_map = nav.get("route_by_map_id", {})
        if self._current_map_id is not None:
            exit_by_map = nav.get("route_exit_by_map_id", {})
            direction = exit_by_map.get(str(self._current_map_id), exit_by_map.get(self._current_map_id))
            if direction:
                direction_str = str(direction).strip().lower()
                if direction_str.startswith("cell:"):
                    try:
                        target_cell = int(direction_str.split(":", 1)[1])
                        click_pos = self._movement_click_pos_for_cell(target_cell)
                        if click_pos:
                            return click_pos
                    except (ValueError, IndexError):
                        pass
                else:
                    auto_point = self._auto_exit_point_for_direction(direction_str)
                    if auto_point is not None:
                        return auto_point

            point = routes_by_map.get(str(self._current_map_id), routes_by_map.get(self._current_map_id))
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    return int(point[0]), int(point[1])
                except (TypeError, ValueError):
                    pass

        return self._next_route_point()

    def _active_navigation_config(self) -> dict:
        nav = self.config.get("navigation", {})
        profiles = nav.get("route_profiles", {})
        farming_mode = self.config.get("farming", {}).get("mode", "resource")
        if farming_mode == "resource":
            profile_name = self.config.get("farming", {}).get("route_profile")
        else:
            profile_name = self.config.get("leveling", {}).get("route_profile")
        profile = profiles.get(profile_name) if isinstance(profiles, dict) and profile_name else None
        if isinstance(profile, dict):
            merged = dict(nav)
            merged.update(profile)
            return merged
        return nav

    def _scan(self):
        if getattr(self, "_traveling_to_farm_map", None):
            if str(self._current_map_id) == self._traveling_to_farm_map:
                print(f"[BOT] Llegamos al mapa de farmeo ({self._traveling_to_farm_map}). Desactivando modo viaje.")
                self._traveling_to_farm_map = None
                self._activate_pending_mobs()
                return
            else:
                print(f"[BOT] Viajando hacia {self._traveling_to_farm_map} - Ignorando recursos en map_id={self._current_map_id}")
                self.map_change_phase = "click"
                self.state = "change_map"
                time.sleep(0.2)
                return

        # Modo recursos: solo nodos por map_id + confirmacion del sniffer.
        map_nodes = self._resource_nodes_for_current_map()
        if map_nodes:
            frame = self.screen.capture()
            filtered_nodes = []
            for profession, resource_name, pos in map_nodes:
                if self._already_harvested(pos):
                    continue
                if self._resource_visible_at_node(frame, resource_name, pos, profession=profession):
                    filtered_nodes.append((profession, resource_name, pos))
            print(
                f"[BOT] Mapa {self._current_map_id}: "
                f"{len(filtered_nodes)}/{len(map_nodes)} nodos disponibles por sniffer"
            )
            if filtered_nodes:
                self.empty_scan_count = 0
                self.pending = self._sort_by_proximity(filtered_nodes)
                self.state = "click_resource"
                return

            # 0 nodos disponibles — buscar sprites libremente en el mapa para detectar
            # recursos que regeneraron (sin filtrar por harvested_positions).
            enabled = self._enabled_resource_names()
            free_hits: list[tuple[str, str, tuple[int, int]]] = []
            for profession, resource_name, _ in map_nodes:
                if (profession, resource_name) not in enabled:
                    continue
                matches = self.detector.find_all_resources(frame, resource_name, profession=profession)
                mon = self.screen.game_region()
                for mx, my in matches:
                    abs_pos = (mon["left"] + mx, mon["top"] + my)
                    free_hits.append((profession, resource_name, abs_pos))

            if free_hits:
                print(f"[BOT] Scan libre detectó {len(free_hits)} sprite(s) — usando posiciones actuales")
                self.harvested_positions = []
                self.harvested_until = 0.0
                self.empty_scan_count = 0
                self.pending = self._sort_by_proximity(free_hits)
                self.state = "click_resource"
                return
        else:
            if self._current_map_id is not None:
                print(f"[BOT] Mapa {self._current_map_id}: sin nodos guardados por sniffer")
            else:
                print("[BOT] Esperando map_id por sniffer")

        # Sin nodos disponibles en el mapa actual: permanecer en el mapa.
        self.empty_scan_count += 1
        if time.time() > self.harvested_until:
            self.harvested_positions = []
            
        nav_cfg = self.config.get("navigation", {})
        move_after = int(nav_cfg.get("empty_scans_before_move", EMPTY_SCANS_BEFORE_MOVE) or EMPTY_SCANS_BEFORE_MOVE)
        if nav_cfg.get("enabled") and self.empty_scan_count >= move_after:
            print(f"[BOT] Sin recursos en map_id={self._current_map_id} - iniciando cambio de mapa")
            self.map_change_phase = "click"
            self.state = "change_map"
            return
            
        time.sleep(self.config["bot"].get("scan_idle_delay", 0.6))

    def _change_map(self):
        if self.test_mode:
            time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
            return
        now = time.time()
        if self.sniffer_active:
            self._drain_sniff_queue()

        if getattr(self, "_traveling_to_farm_map", None) == str(self._current_map_id):
            print(f"[BOT] Llegamos al mapa de farmeo ({self._traveling_to_farm_map}). Desactivando modo viaje.")
            self._traveling_to_farm_map = None
            self._activate_pending_mobs()
            self.empty_scan_count = 0
            self.empty_mob_scan_count = 0
            self.state = "scan"
            return

        route_point = self._route_point_for_current_map() if self.map_change_phase == "click" else None
        if self.map_change_phase == "click":
            if route_point is None:
                print(f"[BOT] Sin ruta configurada para map_id={self._current_map_id} - re-escaneando")
                self.empty_scan_count = 0
                self.empty_mob_scan_count = 0
                if getattr(self, "_traveling_to_farm_map", None):
                    print("[BOT] Modo viaje abortado por falta de ruta.")
                    self._traveling_to_farm_map = None
                    self._activate_pending_mobs()
                self.state = "scan"
                return
            print(f"[BOT] Cambio de mapa map_id={self._current_map_id} - clickeando {route_point}")
            self._sniffer_map_loaded = False
            self._last_map_id = self._current_map_id
            self.actions.quick_click(route_point)
            self.map_change_deadline = now + MAP_CHANGE_TIMEOUT
            self.map_change_phase = "wait_gone"
            return

        if self.map_change_phase == "wait_gone":
            if self._sniffer_map_loaded:
                self._finish_map_change("sniffer")
                return
            if (
                self._current_map_id is not None
                and self._last_map_id is not None
                and self._current_map_id != self._last_map_id
            ):
                self._finish_map_change("map_id")
                return
            if now > self.map_change_deadline:
                print("[BOT] Timeout carga de mapa â€” continuando")
                self._finish_map_change("timeout")
                return
        frame = self.screen.capture()
        cambio_pos = self.ui_detector.find_ui(frame, "CambioMap")

        if self.map_change_phase == "click":
            if cambio_pos:
                print(f"[BOT] CambioMap detectado en {cambio_pos} — clickeando")
                self._sniffer_map_loaded = False
                self.actions.quick_click(cambio_pos)
                self.map_change_deadline = now + MAP_CHANGE_TIMEOUT
                self.map_change_phase = "wait_gone"
            else:
                print("[BOT] CambioMap no visible — re-escaneando")
                self.empty_scan_count = 0
                self.empty_mob_scan_count = 0
                self.state = "scan"

        elif self.map_change_phase == "wait_gone":
            if self._sniffer_map_loaded:
                self._finish_map_change("sniffer")
            elif not cambio_pos or now > self.map_change_deadline:
                if not cambio_pos:
                    self._finish_map_change("template")
                else:
                    print("[BOT] Timeout carga de mapa — continuando")
                    self._finish_map_change("timeout")

    def _cell_matches_exit_direction(self, cell: dict, direction: str | None) -> bool:
        if not direction:
            return False
        try:
            x = int(cell.get("x"))
            y = int(cell.get("y"))
        except (TypeError, ValueError, AttributeError):
            return False
        direction = str(direction).strip().lower()
        if direction in {"left", "izquierda"}:
            return (x - 1) == y
        if direction in {"right", "derecha"}:
            return (x - 27) == y
        if direction in {"down", "abajo"}:
            return (x + y) == 31
        if direction in {"up", "arriba"}:
            return (x - abs(y)) == 1
        return False

    def _auto_exit_point_for_direction(self, direction: str | None) -> tuple[int, int] | None:
        if self._current_map_id is None or not direction:
            return None
        candidates = []
        for cell in self._current_map_cells:
            if not self._cell_matches_exit_direction(cell, direction):
                continue
            if not bool(cell.get("is_walkable")):
                continue
            projected = self._cell_to_screen(int(cell.get("cell_id")))
            if projected is None or not self._is_point_on_monitor(projected):
                continue
            candidates.append((int(cell.get("cell_id")), projected))
        if not candidates:
            print(f"[NAV] Sin salida automática válida para map_id={self._current_map_id} direction={direction}")
            return None
        game_region = self.screen.game_region()
        cx = game_region["left"] + game_region["width"] // 2
        cy = game_region["top"] + game_region["height"] // 2
        candidates.sort(key=lambda item: (item[1][0] - cx) ** 2 + (item[1][1] - cy) ** 2)
        chosen_cell, chosen_pos = candidates[0]
        print(
            f"[NAV] Salida automática map_id={self._current_map_id} direction={direction} "
            f"cell={chosen_cell} pos={chosen_pos}"
        )
        return chosen_pos

    def _scan_mobs(self):
        """Escanea el mapa actual buscando sprites de mobs habilitados.

        Limita la búsqueda al área de juego (excluye barras de UI) para evitar
        falsos positivos en íconos de la barra de acción / menú lateral.
        """
        if getattr(self, "_traveling_to_farm_map", None):
            if str(self._current_map_id) == self._traveling_to_farm_map:
                print(f"[BOT] Llegamos al mapa de farmeo ({self._traveling_to_farm_map}). Desactivando modo viaje.")
                self._traveling_to_farm_map = None
                self._activate_pending_mobs()
                return
            else:
                print(f"[BOT] Viajando hacia {self._traveling_to_farm_map} - Ignorando mobs en map_id={self._current_map_id}")
                self.map_change_phase = "click"
                self.state = "change_map"
                time.sleep(0.2)
                return

        mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
        enabled = [
            n for n, c in mobs_cfg.items()
            if c.get("enabled", True) and not c.get("ignore", False)
        ]

        if self._maybe_follow_tracked_players():
            return

        if not enabled:
            self.empty_mob_scan_count += 1
            nav_cfg = self.config.get("navigation", {})
            move_after = int(nav_cfg.get("empty_scans_before_move", EMPTY_SCANS_BEFORE_MOVE) or EMPTY_SCANS_BEFORE_MOVE)
            if nav_cfg.get("enabled") and self.empty_mob_scan_count >= move_after:
                print(f"[BOT] Sin mobs habilitados en map_id={self._current_map_id} - iniciando cambio de mapa")
                self.map_change_phase = "click"
                self.state = "change_map"
                return
            time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
            return

        if self.sniffer_active:
            sniffed_candidates = self._sniffed_map_mob_candidates()
            if sniffed_candidates and not self._sniffed_projected_mob_targets():
                map_id = self._current_map_id
                if map_id != self._last_missing_projection_warn_map_id:
                    print(f"[BOT] map_id={map_id} tiene mobs por sniffer pero falta calibración específica")
                    self._last_missing_projection_warn_map_id = map_id
                self.empty_mob_scan_count += 1
                nav_cfg = self.config.get("navigation", {})
                move_after = int(nav_cfg.get("empty_scans_before_move", EMPTY_SCANS_BEFORE_MOVE) or EMPTY_SCANS_BEFORE_MOVE)
                if nav_cfg.get("enabled") and self.empty_mob_scan_count >= move_after:
                    print(f"[BOT] Saltando map_id={self._current_map_id} por falta de calibración para atacar")
                    self.map_change_phase = "click"
                    self.state = "change_map"
                    return
                time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
                return
            projected_targets = self._sniffed_projected_mob_targets()
            if projected_targets:
                if self.test_mode:
                    names = ", ".join(sorted({name for name, _ in projected_targets}))
                    print(
                        f"[TEST] map_id={self._current_map_id} proyeccion OK "
                        f"targets={len(projected_targets)} [{names}]"
                    )
                    time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
                    return
                self.mob_pending = projected_targets
                self.empty_mob_scan_count = 0
                game_region = self.screen.game_region()
                cx = game_region["left"] + game_region["width"] // 2
                cy = game_region["top"] + game_region["height"] // 2
                self.mob_pending.sort(key=lambda m: (m[1][0] - cx) ** 2 + (m[1][1] - cy) ** 2)
                print(
                    f"[BOT] Sniffer proyectó {len(projected_targets)} target(s) en pantalla "
                    f"para map_id={self._current_map_id}"
                )
                self.state = "click_mob"
                return
            if sniffed_candidates:
                labels = sorted({
                    mob_name
                    for entry in sniffed_candidates
                    for mob_name in entry.get("resolved_mobs", [])
                })
                print(
                    f"[BOT] Sniffer detecta {len(sniffed_candidates)} actor(es) "
                    f"de mapa tipo mob en map_id={self._current_map_id}"
                    + (f" -> {', '.join(labels)}" if labels else "")
                )
            elif self._map_entities:
                self.empty_mob_scan_count += 1
                print(
                    f"[BOT] Sniffer sin mobs probables en map_id={self._current_map_id} "
                    f"(escaneo #{self.empty_mob_scan_count})"
                )
                nav_cfg = self.config.get("navigation", {})
                move_after = int(
                    nav_cfg.get("empty_scans_before_move", EMPTY_SCANS_BEFORE_MOVE)
                    or EMPTY_SCANS_BEFORE_MOVE
                )
                if nav_cfg.get("enabled") and self.empty_mob_scan_count >= move_after:
                    print(f"[BOT] Sin mobs por sniffer en map_id={self._current_map_id} - iniciando cambio de mapa")
                    self.map_change_phase = "click"
                    self.state = "change_map"
                    return
                time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
                return

        frame = self.screen.capture()
        mon   = self.screen.game_region()
        fh, fw = frame.shape[:2]

        # Recortar al área de juego (mismos límites que el red-ring detector)
        gx1 = int(fw * _REFINE_GAME_LEFT)
        gx2 = int(fw * _REFINE_GAME_RIGHT)
        gy1 = int(fh * _REFINE_GAME_TOP)
        gy2 = int(fh * _REFINE_GAME_BOTTOM)
        game_frame = frame[gy1:gy2, gx1:gx2]

        self.mob_pending = []

        for mob_name in enabled:
            # find_all_mob_sprites prueba TODOS los sprites del mob (orientaciones)
            hits = self.detector.find_all_mob_sprites(game_frame, mob_name)
            for fx, fy in hits:
                # Convertir coordenadas del crop a coordenadas absolutas de pantalla
                abs_pos = (mon["left"] + gx1 + fx, mon["top"] + gy1 + fy)
                self.mob_pending.append((mob_name, abs_pos))

        if self.mob_pending:
            self.empty_mob_scan_count = 0
            # Ordenar por proximidad al centro del área de juego
            cx = mon["left"] + (gx1 + gx2) // 2
            cy = mon["top"]  + (gy1 + gy2) // 2
            self.mob_pending.sort(key=lambda m: (m[1][0]-cx)**2 + (m[1][1]-cy)**2)
            print(f"[BOT] {len(self.mob_pending)} mob(s) detectados — atacando")
            self.state = "click_mob"
        else:
            self.empty_mob_scan_count += 1
            print(f"[BOT] Sin mobs visibles (escaneo #{self.empty_mob_scan_count})")
            nav_cfg = self.config.get("navigation", {})
            move_after = int(nav_cfg.get("empty_scans_before_move", EMPTY_SCANS_BEFORE_MOVE) or EMPTY_SCANS_BEFORE_MOVE)
            if nav_cfg.get("enabled") and self.empty_mob_scan_count >= move_after:
                print(f"[BOT] Sin mobs en map_id={self._current_map_id} - iniciando cambio de mapa")
                self.map_change_phase = "click"
                self.state = "change_map"
                return
            time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))

    def _click_mob(self):
        """Hace click sobre el mob más cercano para iniciar combate."""
        if self.test_mode:
            self.state = "scan_mobs"
            time.sleep(self.config["bot"].get("mob_scan_idle_delay", 0.5))
            return
        mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
        enabled = [
            n for n, c in mobs_cfg.items()
            if c.get("enabled", True) and not c.get("ignore", False)
        ]
        if not enabled:
            self.mob_pending = []
            self.state = "scan_mobs"
            return
        if not self.mob_pending:
            self.state = "scan_mobs"
            return

        mob_name, pos = self.mob_pending.pop(0)
        if not self._is_point_on_monitor(pos):
            print(f"[BOT] Mob '{mob_name}' fuera de pantalla — re-escaneando")
            self.mob_pending = []
            self.state = "scan_mobs"
            return

        print(f"[BOT] Click mob '{mob_name}' en {pos}")
        self._combat_origin = "scan_mobs"
        self.screen.focus_window()

        # Click izquierdo abre el menú contextual con la opción "Atacar"
        self.actions.quick_click(pos)
        time.sleep(self.config["bot"].get("combat_menu_open_delay", 0.2))

        # Buscar el botón "Atacar" por template
        frame = self.screen.capture()
        atacar_pos = self._find_ui_screen(frame, "Atacar")
        if atacar_pos:
            print(f"[BOT] Botón Atacar detectado en {atacar_pos} — clickeando")
            self.actions.quick_click(atacar_pos)
        else:
            # Fallback: offset configurable
            offset = self.config.get("leveling", {}).get("attack_menu_offset", [-60, 30])
            atacar_pos = (pos[0] + offset[0], pos[1] + offset[1])
            print(f"[BOT] Atacar no detectado — fallback offset {atacar_pos}")
            self.actions.quick_click(atacar_pos)

        auto_ready_delay = float(self.config["bot"].get("combat_auto_ready_delay", 0.0) or 0.0)
        self._combat_auto_ready_at = time.time() + max(0.0, auto_ready_delay)

        # Espera activa: durante la entrada pueden llegar eventos del sniffer
        # o aparecer templates de combate antes del siguiente tick().
        if not self._wait_for_combat_entry(attack_pos=atacar_pos):
            if self._attempt_join_external_fight():
                return
            print(f"[BOT] No entró en combate tras click — re-escaneando")
            self._combat_auto_ready_at = 0.0
            self.state = "scan_mobs"

    def _restore_unloading_mobs(self):
        """Reactiva los mobs que fueron deshabilitados al inicio del unloading."""
        disabled = getattr(self, "_unloading_disabled_mobs", [])
        if not disabled:
            return
        mobs_cfg = self.config.get("leveling", {}).get("mobs", {})
        for name in disabled:
            if name in mobs_cfg:
                mobs_cfg[name]["enabled"] = True
        print(f"[UNLOAD] Mobs reactivados: {disabled}")
        self._unloading_disabled_mobs = []

    def _activate_pending_mobs(self, mobs_str: str | None = None):
        mobs_to_activate = mobs_str if mobs_str is not None else getattr(self, "_mobs_to_activate_on_arrival", "")
        self._mobs_to_activate_on_arrival = ""
        if not mobs_to_activate:
            return
            
        target_ids = []
        for m in mobs_to_activate.split(","):
            m = m.strip()
            if m.isdigit():
                target_ids.append(int(m))
                
        if not target_ids:
            return
        
        changed = False
        lev = self.config.setdefault("leveling", {})
        mobs = lev.setdefault("mobs", {})
        template_db = lev.get("template_id_db", {})
        
        activated_names = set()

        for mob_name, mob_cfg in mobs.items():
            mob_tids = []
            for tid in mob_cfg.get("template_ids", []):
                try:
                    mob_tids.append(int(tid))
                except ValueError:
                    pass
            if any(tid in target_ids for tid in mob_tids):
                if not mob_cfg.get("enabled", False):
                    mob_cfg["enabled"] = True
                    changed = True
                activated_names.add(mob_name)
        
        for tid in target_ids:
            already_active = False
            for mob_name in activated_names:
                mob_tids = [int(x) for x in mobs.get(mob_name, {}).get("template_ids", []) if str(x).isdigit()]
                if tid in mob_tids:
                    already_active = True
                    break
            if already_active:
                continue
                
            db_name = template_db.get(str(tid)) or template_db.get(tid)
            if db_name:
                db_name = str(db_name).strip()
                mob_cfg = mobs.setdefault(db_name, {})
                if not mob_cfg.get("enabled", False):
                    mob_cfg["enabled"] = True
                    changed = True
                
                current_tids = mob_cfg.get("template_ids", [])
                if tid not in current_tids:
                    current_tids.append(tid)
                    mob_cfg["template_ids"] = current_tids
                    changed = True
                
                activated_names.add(db_name)
        
        if changed:
            try:
                import yaml
                config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
                with open(config_path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                
                raw_mobs = raw.setdefault("leveling", {}).setdefault("mobs", {})
                for mob_name in activated_names:
                    raw_mob_cfg = raw_mobs.setdefault(mob_name, {})
                    raw_mob_cfg["enabled"] = True
                    if mob_name in mobs:
                        raw_mob_cfg["template_ids"] = mobs[mob_name].get("template_ids", [])
                        
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
                print(f"[MOBS_ACTIVATED] Mobs activados por ID automáticamente: {', '.join(activated_names)}")
            except Exception as e:
                print(f"[BOT] Error guardando config.yaml: {e}")

    def _wait_for_combat_entry(self, attack_pos: tuple[int, int] | None = None) -> bool:
        """Espera la transición a combate drenando la cola del sniffer y usando fallback visual."""
        entry_wait = float(self.config["bot"].get("combat_entry_wait", 1.1) or 1.1)
        min_wait = 1.6 if self.sniffer_active else 1.2
        deadline = time.time() + max(min_wait, entry_wait)
        self.combat_deadline = time.time() + 8.0
        poll_delay = min(0.14, max(0.05, entry_wait / 8.0))
        retry_delay = float(self.config["bot"].get("combat_entry_attack_retry_delay", 0.35) or 0.35)
        max_retries = int(self.config["bot"].get("combat_entry_attack_retries", 2) or 2)
        attack_retries = 0
        next_retry_at = time.time() + max(0.15, retry_delay)

        while time.time() < deadline:
            if self.sniffer_active:
                self._drain_sniff_queue()
                if self.state == "in_combat":
                    return True
                if not self._sniffer_attack_target_still_valid(attack_pos):
                    return False

            frame = self.screen.capture()

            ok_btn = self._find_ui_screen(frame, "OK")
            if ok_btn:
                print("[BOT] Popup OK detectado durante entrada a combate — cerrando")
                self.actions.quick_click(ok_btn)
    