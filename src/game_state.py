"""GameState — fuente unica de verdad del estado del juego.

Reemplaza progresivamente los ~90 atributos `self._sniffer_*`, `self._combat_*`,
`self._fighters`, `self._route_*` esparcidos por `Bot`. La idea es que toda la
informacion del estado del juego viva en un solo lugar, con timestamp por
campo, y los consumidores (combate, route, GUI) lean siempre desde aca.

Diseño:
    - dataclass plano con campos tipados (Optional para los que pueden faltar).
    - dict interno `_timestamps` actualizado por cada `set(field, value)`.
    - `is_stale(field, max_age_s)` para detectar datos viejos del sniffer.
    - `to_dict()` para serializar a JSON (Fase 7: endpoint /status, GUI debug panel).

NO hace I/O. NO conoce el sniffer ni la GUI. Solo es un contenedor.

Uso:
    from game_state import GameState

    gs = GameState()
    gs.set("pa", 6)
    gs.set("combat_cell", 215)

    if gs.is_stale("pa", max_age_s=2.0):
        # PA del sniffer puede estar desactualizado, hay que refrescar
        ...

    print(gs.to_dict())  # serializable para /status

Migracion (proximos ciclos):
    - C2: bot.py adopta `self.game_state = GameState()` en __init__.
    - C3: `_handle_sniff_event` empieza a llamar `self.game_state.set(...)`
          en vez de mutar `self._sniffer_*` directo.
    - C4: combate (Sadida) lee de `game_state` en vez de `Bot._sniffer_*`.
    - C5: GUI lee de `game_state` para el panel de debug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Optional


@dataclass
class GameState:
    """Estado del juego en un solo lugar. Mutar via `set()` para que se
    actualice el timestamp del campo.

    Convencion: campos sin valor se inician en `None` (no en `0` o `""`)
    para distinguir "no sabemos" de "sabemos que es cero/vacio".
    """

    # ── Personaje ──────────────────────────────────────────────────────
    actor_id: Optional[str] = None
    character_name: Optional[str] = None
    hp: Optional[int] = None
    max_hp: Optional[int] = None
    kamas: Optional[int] = None
    pods_current: Optional[int] = None
    pods_max: Optional[int] = None

    # ── Mapa ───────────────────────────────────────────────────────────
    current_map_id: Optional[int] = None
    current_cell: Optional[int] = None
    map_data_raw: Optional[str] = None  # string del paquete map_data, decodear aparte

    # ── Combate ────────────────────────────────────────────────────────
    in_combat: bool = False
    in_placement: bool = False
    is_my_turn: bool = False
    pa: Optional[int] = None
    pm: Optional[int] = None
    combat_cell: Optional[int] = None
    combat_turn_number: int = 0
    fight_started_at: float = 0.0
    fighters: dict[str, dict] = field(default_factory=dict)  # actor_id → {hp, cell, alive, ...}

    # ── Ruta / navegacion ──────────────────────────────────────────────
    active_route_name: Optional[str] = None
    route_step_idx: int = 0

    # ── Salud del sniffer ──────────────────────────────────────────────
    last_sniff_event_at: float = 0.0
    sniffer_active: bool = False

    # ── Diagnostico ────────────────────────────────────────────────────
    last_error: Optional[str] = None
    last_error_at: float = 0.0

    # ── Internal: timestamps por campo (no se serializa publico) ───────
    _timestamps: dict[str, float] = field(default_factory=dict, repr=False)

    # ─── API ───────────────────────────────────────────────────────────

    def set(self, field_name: str, value: Any, *, ts: Optional[float] = None) -> None:
        """Actualiza un campo y registra el timestamp.

        Args:
            field_name: nombre del campo (debe existir en la dataclass).
            value: nuevo valor.
            ts: timestamp explicito (e.g. del sniffer). Si None, usa time.time().

        Raises:
            AttributeError: si `field_name` no es un campo del GameState
                (ayuda a detectar typos en migraciones).
        """
        if field_name.startswith("_"):
            raise AttributeError(
                f"GameState.set: campo privado {field_name!r} no permitido"
            )
        if field_name not in self._public_field_names():
            raise AttributeError(
                f"GameState no tiene el campo {field_name!r}. "
                f"Campos validos: {sorted(self._public_field_names())}"
            )
        setattr(self, field_name, value)
        self._timestamps[field_name] = ts if ts is not None else time.time()

    def update(self, mapping: dict[str, Any], *, ts: Optional[float] = None) -> None:
        """Atajo: aplica `set` a varios campos con un solo timestamp."""
        actual_ts = ts if ts is not None else time.time()
        for k, v in mapping.items():
            self.set(k, v, ts=actual_ts)

    def get(self, field_name: str, default: Any = None) -> Any:
        """Lee un campo. Si no existe, devuelve default (no raise)."""
        return getattr(self, field_name, default)

    def get_timestamp(self, field_name: str) -> float:
        """Devuelve el timestamp del ultimo set() o 0.0 si nunca se seteo."""
        return self._timestamps.get(field_name, 0.0)

    def age_s(self, field_name: str, *, now: Optional[float] = None) -> float:
        """Segundos desde el ultimo set(). Devuelve `inf` si nunca se seteo."""
        ts = self._timestamps.get(field_name, 0.0)
        if ts <= 0.0:
            return float("inf")
        cur = now if now is not None else time.time()
        return cur - ts

    def is_stale(self, field_name: str, max_age_s: float, *, now: Optional[float] = None) -> bool:
        """True si el campo nunca se seteo o se seteo hace mas de `max_age_s`."""
        return self.age_s(field_name, now=now) > max_age_s

    def reset_combat(self) -> None:
        """Limpia el estado de combate (al on_fight_end). NO toca personaje ni mapa."""
        ts = time.time()
        for fname in ("in_combat", "in_placement", "is_my_turn",
                      "pa", "pm", "combat_cell"):
            setattr(self, fname, _DEFAULT_FOR.get(fname))
            self._timestamps[fname] = ts
        self.combat_turn_number = 0
        self.fight_started_at = 0.0
        self.fighters = {}
        self._timestamps["combat_turn_number"] = ts
        self._timestamps["fight_started_at"] = ts
        self._timestamps["fighters"] = ts

    def to_dict(self, *, include_timestamps: bool = False) -> dict[str, Any]:
        """Serializa a dict (JSON-friendly).

        Args:
            include_timestamps: si True, agrega `_timestamps` con la edad
                en segundos de cada campo (utiles para debug / /status).
        """
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            out[f.name] = getattr(self, f.name)
        if include_timestamps:
            now = time.time()
            out["_age_s"] = {
                k: round(now - v, 3) if v > 0 else None
                for k, v in self._timestamps.items()
            }
        return out

    # ─── helpers internos ──────────────────────────────────────────────

    @classmethod
    def _public_field_names(cls) -> set[str]:
        return {f.name for f in fields(cls) if not f.name.startswith("_")}


# Defaults para reset_combat (sacados de la dataclass via inspeccion).
_DEFAULT_FOR: dict[str, Any] = {
    "in_combat": False,
    "in_placement": False,
    "is_my_turn": False,
    "pa": None,
    "pm": None,
    "combat_cell": None,
}
