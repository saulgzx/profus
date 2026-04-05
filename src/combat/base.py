import random


def config_delay(config: dict, key: str, default: float) -> float:
    """Lee un delay fijo o aleatorio desde config usando key o key_min/key_max."""
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


class CombatContext:
    """Contexto de combate pasado a los perfiles — acceso a screen, detector, actions y config.

    Atributos adicionales provenientes del sniffer:
      enemy_positions  — lista de (x, y) en pantalla de enemigos activos (puede estar vacía
                         si la calibración de celdas aún no está lista)
      current_pa       — PA disponibles según el protocolo (None si no se conoce aún)
      my_cell          — cell_id actual del personaje (None si no se conoce)
    """
    def __init__(self, screen, ui_detector, actions, config,
                 enemy_positions: list | None = None,
                 current_pa: int | None = None,
                 current_mp: int | None = None,
                 my_cell: int | None = None,
                 turn_number: int = 0,
                 combat_probe=None,
                 buff_flags: dict | None = None,
                 refresh_combat_state=None,
                 project_self_cell=None,
                 move_towards_enemy=None,
                 enemy_in_melee_range=None):
        self.screen          = screen
        self.ui_detector     = ui_detector
        self.actions         = actions
        self.config          = config
        self.enemy_positions = enemy_positions or []
        self.current_pa      = current_pa
        self.current_mp      = current_mp
        self.my_cell         = my_cell
        self.turn_number     = turn_number
        self.combat_probe    = combat_probe
        self.buff_flags      = buff_flags or {}
        self.refresh_combat_state = refresh_combat_state
        self.project_self_cell = project_self_cell
        self.move_towards_enemy = move_towards_enemy
        self.enemy_in_melee_range = enemy_in_melee_range


class CombatProfile:
    """Perfil de combate base. Subclasear para implementar un personaje especifico."""
    name        = "Base"
    needs_panel = True        # False para perfiles que no necesitan desplegar el panel de combate
    mi_turno_template = "MiTurno"  # None para perfiles que no usan template de turno
    uses_listo_template = True

    def on_placement(self, listo_pos: tuple, ctx: CombatContext) -> None:
        """Llamado cuando el boton Listo es visible (fase de colocacion)."""
        ctx.actions.quick_click(listo_pos)

    def on_turn(self, mi_turno_pos: tuple, ctx: CombatContext) -> str:
        """
        Llamado cuando es el turno del jugador.
        Debe retornar 'done' o 'combat_ended'.
        """
        return "done"
