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
                 enemies: list | None = None,
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
                 enemy_in_melee_range=None,
                 has_line_of_sight=None,
                 cell_distance=None,
                 record_learned_offset=None,
                 get_spell_jitter_offset=None,
                 move_random_reachable=None,
                 manual_pixel_for_cell=None):
        self.screen          = screen
        self.ui_detector     = ui_detector
        self.actions         = actions
        self.config          = config
        self.enemy_positions = enemy_positions or []
        self.current_pa      = current_pa
        self.current_mp      = current_mp
        self.enemies         = enemies or []
        self.my_cell         = my_cell
        self.turn_number     = turn_number
        self.combat_probe    = combat_probe
        self.buff_flags      = buff_flags or {}
        self.spell_cooldowns = dict(self.buff_flags.get("spell_cooldowns") or {})
        self.refresh_combat_state = refresh_combat_state
        self.project_self_cell = project_self_cell
        self.move_towards_enemy = move_towards_enemy
        self.enemy_in_melee_range = enemy_in_melee_range
        self.has_line_of_sight = has_line_of_sight
        self.cell_distance = cell_distance
        self.record_learned_offset = record_learned_offset
        self.get_spell_jitter_offset = get_spell_jitter_offset
        # Fallback para desbloquear sprite del PJ tapando un mob: los perfiles
        # pueden llamarlo tras N fallos consecutivos de un spell dirigido a
        # enemigo. Mueve el PJ a una celda aleatoria alcanzable con el PM
        # pasado. Retorna dict con 'moved', 'combat_cell', etc.
        self.move_random_reachable = move_random_reachable
        # Lookup directo al pixel manual calibrado por el usuario para una
        # (map_id, cell_id). Retorna (x, y) si hay override manual en
        # `bot.manual_pixel_by_map_cell[<map>][<cell>]`, None si no hay.
        # LEY ABSOLUTA — cuando hay pixel manual para la celda del PJ se usa
        # SIN offsets (y-offset del cuerpo, jitter, learned). Rule:
        # rule_manual_pixel_positions_law.md.
        self.manual_pixel_for_cell = manual_pixel_for_cell


class CombatProfile:
    """Perfil de combate base. Subclasear para implementar un personaje especifico."""
    name        = "Base"
    needs_panel = True        # False para perfiles que no necesitan desplegar el panel de combate
    mi_turno_template = "MiTurno"  # None para perfiles que no usan template de turno
    uses_listo_template = True

    def placement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        """Puntuación de una celda para posicionamiento inicial. Menor es mejor."""
        return (min(enemy_distances) if enemy_distances else 999, self_distance, cell_id)

    def movement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        """Puntuación de una celda para el movimiento del turno. Menor es mejor."""
        return (min(enemy_distances) if enemy_distances else 999, -self_distance, cell_id)

    def on_placement(self, listo_pos: tuple, ctx: CombatContext) -> None:
        """Llamado cuando el boton Listo es visible (fase de colocacion)."""
        ctx.actions.quick_click(listo_pos)

    def on_turn(self, mi_turno_pos: tuple, ctx: CombatContext) -> str:
        """
        Llamado cuando es el turno del jugador.
        Debe retornar 'done' o 'combat_ended'.
        """
        return "done"

    def on_fight_end(self) -> None:
        """Hook invocado al recibir GE (fin de pelea). Subperfiles pueden resetear estado interno."""
        return None
