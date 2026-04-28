"""farming_smart.py — Auto-farming basado en sniffer (sin templates de sprites).

Diseño limpio y separado del pipeline legacy de Campesino. Usa solo señales
del sniffer + map_data XML para:
  1. Detectar recursos disponibles en el mapa actual (vía interactive_object_id
     del XML cruzado con GDF state del sniffer).
  2. Clickear el recurso → menú contextual → opción de cosecha.
  3. Confirmar inicio de harvest vía GA;501 del server.
  4. Detectar fin del harvest vía OAK (item) / JX (xp) / GDF state=3.
  5. Aprender automáticamente interactive_id ↔ template_id en cada cosecha.

API:
    machine = SmartFarmingMachine(bot)
    machine.tick()  # llamado desde Bot.tick() cuando smart_farming activo
    machine.is_active()  # True si farming.smart_farming_enabled

Estados:
    idle            → escaneando recursos
    clicking        → click izq sobre el recurso, esperando menú
    menu_wait       → buscando opción de cosecha en pantalla
    harvest_wait    → server confirmó harvest, esperando OAK/JX/timeout

Activación:
    config.farming.smart_farming_enabled = True

El bot atiende este state machine en lugar de _scan() / click_resource etc
mientras smart_farming está activo. Al desactivar, vuelve al modo previo.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import Bot

from app_logger import get_logger

_log = get_logger("bot.farm")


# Estados internos
_ST_IDLE = "idle"
_ST_CLICKING = "clicking"
_ST_MENU_WAIT = "menu_wait"
_ST_HARVEST_WAIT = "harvest_wait"
_ST_COOLDOWN = "cooldown"  # tras fail/skip, breve pausa antes de re-scan


class SmartFarmingMachine:
    """State machine para farming con sniffer-only."""

    # Timings (configurables vía config.farming.*)
    DEFAULT_MENU_TIMEOUT = 4.0     # max wait para que aparezca el menú
    DEFAULT_HARVEST_TIMEOUT = 18.0  # max wait para OAK/JX (server cap ~13s + margen)
    DEFAULT_COOLDOWN = 1.5          # pausa tras fail antes de re-scan
    DEFAULT_IDLE_RESCAN = 2.0       # cuando no hay recursos, esperar antes de rescan

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self.state = _ST_IDLE
        self._target: dict | None = None     # nodo activo (cell_id, screen_pos, etc.)
        self._state_entered_at: float = 0.0
        self._failed_cells: dict[int, float] = {}  # cell_id → timestamp del fail
        self._collected_count: int = 0       # contador de cosechas exitosas (sesión)

    # ── Activación ─────────────────────────────────────────────────────

    def is_active(self) -> bool:
        farming = (self.bot.config.get("farming") or {})
        return bool(farming.get("smart_farming_enabled", False))

    def reset(self, reason: str = "") -> None:
        if self.state != _ST_IDLE:
            _log.info(f"[SFARM] reset desde state={self.state} ({reason})")
        self.state = _ST_IDLE
        self._target = None
        self._state_entered_at = 0.0

    # ── Tick principal ─────────────────────────────────────────────────

    def tick(self) -> None:
        """Avanza el state machine. Llamar desde Bot.tick() cuando active."""
        # Drenar siempre el sniffer queue para mantener estados frescos
        if self.bot.sniffer_active:
            self.bot._drain_sniff_queue()

        # Skip si entramos en combate (combat tiene precedencia)
        if self.bot.state == "in_combat":
            return

        # Skip si estamos cambiando de mapa o en estado de viaje
        if self.bot.state in {"change_map", "teleport_start", "teleport_click_use",
                              "teleport_select_dest"}:
            return

        now = time.time()

        if self.state == _ST_IDLE:
            self._tick_idle(now)
        elif self.state == _ST_CLICKING:
            self._tick_clicking(now)
        elif self.state == _ST_MENU_WAIT:
            self._tick_menu_wait(now)
        elif self.state == _ST_HARVEST_WAIT:
            self._tick_harvest_wait(now)
        elif self.state == _ST_COOLDOWN:
            self._tick_cooldown(now)

    # ── Estados ────────────────────────────────────────────────────────

    def _tick_idle(self, now: float) -> None:
        """Escanea recursos disponibles y elige uno."""
        # Limpiar entradas viejas de _failed_cells (>60s)
        for cid in list(self._failed_cells.keys()):
            if now - self._failed_cells[cid] > 60.0:
                del self._failed_cells[cid]

        nodes = self.bot._scan_resource_nodes_from_map_data(
            only_known=False, require_state_4=True,
        ) or []

        # Filtrar cells que fallaron recientemente
        nodes = [n for n in nodes if n["cell_id"] not in self._failed_cells]

        if not nodes:
            # Sin recursos disponibles. Esperar antes de re-scan.
            time.sleep(self.DEFAULT_IDLE_RESCAN)
            return

        # Elegir el más cercano al PJ. Si no sabemos pos del PJ, primero.
        target = self._pick_closest(nodes)
        if target is None:
            time.sleep(self.DEFAULT_IDLE_RESCAN)
            return

        _log.info(
            f"[SFARM] Target → cell={target['cell_id']} "
            f"name={target['name']} iid={target['interactive_id']} "
            f"tmpl={target.get('template_id')} pos={target['screen_pos']}"
        )
        self._target = target
        self._enter_state(_ST_CLICKING, now)

    def _pick_closest(self, nodes: list[dict]) -> dict | None:
        """Elige el nodo más cercano al PJ en pixels. Si no se puede, primero."""
        if not nodes:
            return None
        # Posición del PJ on-screen (vía cell del PJ → screen)
        pj_cell = (
            getattr(self.bot, "_combat_cell", None)
            or self.bot._sniffer_my_actor_cell()
            if hasattr(self.bot, "_sniffer_my_actor_cell")
            else None
        )
        pj_pos = None
        if pj_cell is not None:
            try:
                pj_pos = self.bot._cell_to_screen(int(pj_cell))
            except Exception:
                pj_pos = None
        if pj_pos is None:
            return nodes[0]

        def dist2(n):
            sp = n["screen_pos"]
            return (sp[0] - pj_pos[0]) ** 2 + (sp[1] - pj_pos[1]) ** 2

        return min(nodes, key=dist2)

    def _tick_clicking(self, now: float) -> None:
        """Click izquierdo sobre el recurso para abrir el menú contextual."""
        if self._target is None:
            self._enter_state(_ST_IDLE, now)
            return
        pos = self._target["screen_pos"]
        cell_id = self._target["cell_id"]

        # Verificación visual EN TIEMPO REAL antes de clickear.
        # Aunque el GDF diga state=4, hacemos una check final con OpenCV:
        # ¿hay realmente un sprite renderizado en esa posición? Si no
        # (cell vacía visualmente), salteamos y marcamos como agotado local.
        # Esto cubre los casos donde GDF está desactualizado o desincronizado.
        if self._should_visually_verify():
            visible, density = self.bot._is_resource_sprite_present(pos)
            if not visible:
                _log.info(f"[SFARM] cell={cell_id} GDF dice disponible pero "
                      f"VISUALMENTE no hay sprite (edge_density={density:.3f}). "
                      f"Skipeando target.")
                self._mark_failed(cell_id)
                self._enter_state(_ST_COOLDOWN, now)
                return
            _log.info(f"[SFARM] cell={cell_id} verificación visual OK "
                  f"(edge_density={density:.3f})")

        try:
            self.bot.screen.focus_window()
        except Exception:
            pass
        _log.info(f"[SFARM] Click recurso en {pos}")
        self.bot._last_harvest_cell = None
        self.bot._last_harvest_at = 0.0
        try:
            self.bot.actions.click(pos)
        except Exception as exc:
            _log.info(f"[SFARM] click falló: {exc!r} — saltando target.")
            self._mark_failed(cell_id)
            self._enter_state(_ST_COOLDOWN, now)
            return
        self._enter_state(_ST_MENU_WAIT, now)

    def _should_visually_verify(self) -> bool:
        """Lee el flag de config — verificación visual ON/OFF."""
        return bool(
            (self.bot.config.get("farming") or {})
            .get("visual_check_enabled", True)
        )

    def _tick_menu_wait(self, now: float) -> None:
        """Buscar el menú contextual y clickear opción de cosecha."""
        elapsed = now - self._state_entered_at
        if self._target is None:
            self._enter_state(_ST_IDLE, now)
            return

        # Ya empezó el harvest? Atajo: si el server confirmó (GA;501) ya,
        # saltamos directo a esperar el resultado.
        if self.bot._last_harvest_cell == self._target["cell_id"]:
            _log.info(f"[SFARM] Harvest ya iniciado en server (cell={self._target['cell_id']}). Skipeando menú.")
            self._enter_state(_ST_HARVEST_WAIT, now)
            return

        if elapsed > self.DEFAULT_MENU_TIMEOUT:
            _log.info(f"[SFARM] Menú no apareció en {elapsed:.1f}s — fail. cell={self._target['cell_id']}")
            self._mark_failed(self._target["cell_id"])
            self._enter_state(_ST_COOLDOWN, now)
            return

        # Buscar menú vía detección de color HSV (no templates)
        try:
            frame = self.bot.screen.capture()
            menu_pos = self.bot._find_harvest_menu_option(frame, self._target["screen_pos"])
        except Exception as exc:
            _log.info(f"[SFARM] menu detection failed: {exc!r}")
            menu_pos = None

        if menu_pos:
            _log.info(f"[SFARM] Menú detectado en {menu_pos} — clickeando opción.")
            try:
                self.bot.actions.quick_click(menu_pos)
            except Exception as exc:
                _log.info(f"[SFARM] click menú falló: {exc!r}")
                self._mark_failed(self._target["cell_id"])
                self._enter_state(_ST_COOLDOWN, now)
                return
            self._enter_state(_ST_HARVEST_WAIT, now)
            return

        # No menú aún, dejar que llegue (poll cada 200ms)
        time.sleep(0.2)

    def _tick_harvest_wait(self, now: float) -> None:
        """Esperar fin del harvest: OAK / JX / GDF state=3 / timeout."""
        elapsed = now - self._state_entered_at
        if self._target is None:
            self._enter_state(_ST_IDLE, now)
            return
        cell_id = self._target["cell_id"]
        map_id = self.bot._current_map_id

        # Señal 1: GDF actualizó el cell a state=3 (agotado)
        cell_state = (self.bot._interactive_state.get(int(map_id), {}) if map_id else {}).get(cell_id)
        if cell_state == 3:
            self._on_harvest_success(cell_id, reason="GDF state=3")
            return

        # Señal 2: el bot.py learner ya limpió _last_harvest_cell tras OAK
        # (significa que llegó el item). Lo detectamos vía un flag post-OAK
        # que bot.py setea: contador de _last_harvest_at + clear de cell.
        # Como bot._last_harvest_cell se setea en GA;501 y se limpia en OAK,
        # si era nuestro cell y ahora es None, fue procesado.
        if (
            self.bot._last_harvest_cell is None
            and self.bot._last_harvest_at > self._state_entered_at
        ):
            # OAK llegó (limpieza por _learn_resource_mapping)
            self._on_harvest_success(cell_id, reason="OAK procesado")
            return

        # Timeout
        if elapsed > self.DEFAULT_HARVEST_TIMEOUT:
            _log.info(
                f"[SFARM] Harvest timeout ({elapsed:.1f}s) en cell={cell_id}. "
                f"Marcando fail y avanzando."
            )
            self._mark_failed(cell_id)
            self._enter_state(_ST_COOLDOWN, now)
            return

        # Sigue esperando
        time.sleep(0.2)

    def _tick_cooldown(self, now: float) -> None:
        if now - self._state_entered_at >= self.DEFAULT_COOLDOWN:
            self._enter_state(_ST_IDLE, now)
            return
        time.sleep(0.1)

    # ── Helpers ────────────────────────────────────────────────────────

    def _enter_state(self, new_state: str, now: float) -> None:
        if new_state != self.state:
            _log.info(f"[SFARM] state {self.state} → {new_state}")
        self.state = new_state
        self._state_entered_at = now

    def _on_harvest_success(self, cell_id: int, reason: str) -> None:
        self._collected_count += 1
        _log.info(f"[SFARM] ✓ Cosecha exitosa cell={cell_id} ({reason}). "
              f"Total sesión: {self._collected_count}")
        self._target = None
        # Pequeña pausa para drenar packets residuales y dar respiro al server
        self._enter_state(_ST_COOLDOWN, time.time())

    def _mark_failed(self, cell_id: int) -> None:
        """Marca cell como fallida: blacklist temporal en el SM + state local
        en el bot. Esto previene que la GUI siga mostrándola como disponible
        y que el SM la re-elija en su próximo scan."""
        self._failed_cells[cell_id] = time.time()
        try:
            self.bot._mark_resource_depleted_locally(
                cell_id, reason=f"smart_farm fail desde state={self.state}"
            )
        except Exception:
            pass
        self._target = None

    # ── Stats / status (para GUI) ──────────────────────────────────────

    def status_summary(self) -> dict:
        return {
            "active": self.is_active(),
            "state": self.state,
            "target_cell": self._target["cell_id"] if self._target else None,
            "target_name": self._target["name"] if self._target else None,
            "collected": self._collected_count,
            "failed_cells_count": len(self._failed_cells),
        }
