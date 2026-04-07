import time

import pyautogui
import random

from .base import CombatContext, CombatProfile, config_delay

_UI_CATEGORY = "ui/Sacro"


class Profile(CombatProfile):
    name = "Sacrogito"
    needs_panel = False
    mi_turno_template = None
    uses_listo_template = False

    def __init__(self):
        self._castigo_active = False
        self._last_turn_number = 0

    def _self_target_candidates(self, self_pos: tuple[int, int]) -> list[tuple[int, int]]:
        x, y = int(self_pos[0]), int(self_pos[1])
        candidates = [
            (x, y),
            (x, y - 10),
            (x - 12, y + 6),
            (x + 12, y + 6),
            (x, y + 12),
        ]
        deduped: list[tuple[int, int]] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _cast_self_spell_with_retries(
        self,
        ctx: CombatContext,
        self_pos: tuple[int, int],
        spell_key: str,
        spell_name: str,
        initial_pa: int | None,
        expected_spell_id: int | None = None,
        confirm_castigo_state: bool = False,
    ) -> tuple[bool, dict | None]:
        attempt_started_at = 0.0
        refreshed_state = None
        cast_confirmed = False

        def _attempt_cast(target_pos: tuple[int, int], attempt_label: str):
            nonlocal attempt_started_at
            attempt_started_at = time.time()
            print(f"[COMBAT] Sacrogito - {spell_name} (tecla {spell_key}) -> objetivo={target_pos} [{attempt_label}]")
            ctx.screen.focus_window()
            ctx.actions.quick_press_key(spell_key)
            time.sleep(config_delay(ctx.config, "combat_spell_select_delay", 0.12))
            ctx.actions.quick_click(target_pos)
            time.sleep(config_delay(ctx.config, "combat_post_click_delay", 0.12))
            ctx.actions.park_mouse(ctx.screen.parking_regions())
            time.sleep(config_delay(ctx.config, "combat_pre_capture_delay", 0.08))

        for idx, target_pos in enumerate(self._self_target_candidates(self_pos), start=1):
            _attempt_cast(target_pos, f"try{idx}")
            if not callable(getattr(ctx, "refresh_combat_state", None)):
                break
            max_wait_s = 2.0 if idx == 1 else 0.45
            poll_deadline = time.time() + max_wait_s

            while time.time() < poll_deadline:
                refreshed_state = ctx.refresh_combat_state(0.05)
                if refreshed_state.get("fight_ended"):
                    break

                refreshed_pa = refreshed_state.get("current_pa")
                refreshed_cooldown = int(refreshed_state.get("castigo_osado_cooldown") or 0)
                if initial_pa is not None and refreshed_pa is not None and refreshed_pa < initial_pa:
                    cast_confirmed = True

                server_confirm_at = float(refreshed_state.get("last_spell_server_confirm_at") or 0.0)
                server_confirm = refreshed_state.get("last_spell_server_confirm") or {}
                if server_confirm_at >= attempt_started_at:
                    if expected_spell_id is None or server_confirm.get("spell_id") == expected_spell_id:
                        cast_confirmed = True

                if confirm_castigo_state and (
                    refreshed_state.get("castigo_osado_active") or refreshed_cooldown > 0
                ):
                    cast_confirmed = True

                if cast_confirmed:
                    break

            if cast_confirmed or (refreshed_state and refreshed_state.get("fight_ended")):
                break
            if idx < len(self._self_target_candidates(self_pos)):
                print(f"[COMBAT] Sacrogito - sin confirmacion en {target_pos}, probando variante")

        return cast_confirmed, refreshed_state

    def _get_pullable_target(self, ctx: CombatContext) -> dict | None:
        """Encuentra el mejor enemigo para atraer con Atracción."""
        if not callable(getattr(ctx, "has_line_of_sight", None)):
            return None

        candidates = []
        for enemy in ctx.enemies:
            distance = ctx.cell_distance(ctx.my_cell, enemy.get("cell_id"))
            if distance is None or not (2 <= distance <= 7):
                continue
            if not ctx.has_line_of_sight(ctx.my_cell, enemy.get("cell_id")):
                continue
            candidates.append(enemy)

        if not candidates:
            return None
        return random.choice(candidates)

    def on_placement(self, listo_pos: tuple, ctx: CombatContext) -> None:
        print("[COMBAT] Sacrogito - Click Listo (colocacion)")
        ctx.actions.quick_click(listo_pos)

    def on_turn(self, mi_turno_pos: tuple, ctx: CombatContext) -> str:
        if ctx.turn_number <= 1 and ctx.turn_number < self._last_turn_number:
            self._castigo_active = False
        if ctx.turn_number <= 1:
            self._castigo_active = False
        self._last_turn_number = ctx.turn_number

        mon = ctx.screen.monitor
        cx = mon["left"] + mon["width"] // 2
        cy = mon["top"] + mon["height"] // 2

        ctx.screen.focus_window()
        pyautogui.moveTo(cx, cy, duration=ctx.config["bot"].get("combat_center_move_duration", 0.04))
        time.sleep(config_delay(ctx.config, "combat_pre_capture_delay", 0.08))

        frame = ctx.screen.capture()

        cerrar = ctx.ui_detector.find_ui(frame, "Cerrar")
        if cerrar:
            print("[COMBAT] Combate ya terminado")
            ctx.actions.quick_click((mon["left"] + cerrar[0], mon["top"] + cerrar[1]))
            time.sleep(config_delay(ctx.config, "combat_close_delay", 0.15))
            return "combat_ended"

        self_pos = mi_turno_pos
        current_self_cell = ctx.my_cell
        if ctx.my_cell is not None and callable(getattr(ctx, "project_self_cell", None)):
            projected_self = ctx.project_self_cell(ctx.my_cell)
            if projected_self is not None:
                self_pos = projected_self

        move_points = int(ctx.current_mp or 0)
        if move_points > 0 and callable(getattr(ctx, "move_towards_enemy", None)):
            move_result = ctx.move_towards_enemy(move_points, desired_range=1)
            if move_result.get("fight_ended"):
                return "combat_ended"
            moved_self_pos = move_result.get("self_screen_pos")
            if move_result.get("moved") and moved_self_pos is not None:
                self_pos = moved_self_pos
                current_self_cell = move_result.get("combat_cell")
                if move_result.get("combat_cell") is not None and callable(getattr(ctx, "project_self_cell", None)):
                    projected_self = ctx.project_self_cell(move_result.get("combat_cell"))
                    if projected_self is not None:
                        self_pos = projected_self
            elif move_result.get("attempted_move"):
                print("[COMBAT] Sacrogito - movimiento pendiente de confirmacion, reintentando antes de pasar turno")
                return "retry"
        if self_pos is None:
            print("[COMBAT] Sacrogito - sin posicion propia resuelta, no se lanza habilidad")
            print("[COMBAT] Sacrogito - pasando turno (Space)")
            ctx.actions.quick_press_key("space")
            ctx.actions.park_mouse(ctx.screen.parking_regions())
            return "done"

        castigo_activo = bool(ctx.buff_flags.get("castigo_osado_active"))
        castigo_cooldown = int(ctx.buff_flags.get("castigo_osado_cooldown") or 0)
        if not castigo_activo:
            castigo_activo = ctx.ui_detector.find(frame, "CastigoOsadoActivo", _UI_CATEGORY)
        if castigo_activo:
            self._castigo_active = True
        elif castigo_cooldown <= 0:
            self._castigo_active = False

        should_cast_castigo = castigo_cooldown <= 0
        if should_cast_castigo:
            spell_key, spell_name = "2", "CastigoOsado"
            expected_spell_id = 433
        else:
            if not callable(getattr(ctx, "enemy_in_melee_range", None)) or not ctx.enemy_in_melee_range(current_self_cell):
                # No hay enemigos en CaC, intentar atraer uno
                if ctx.current_pa >= 3:
                    pull_target = self._get_pullable_target(ctx)
                    if pull_target:
                        print(f"[COMBAT] Sacrogito - sin enemigo CaC, usando Atracción en {pull_target.get('id')}")
                        self._cast_self_spell_with_retries(
                            ctx,
                            pull_target["screen_pos"],
                            spell_key="4",  # Asumiendo que Atracción está en la tecla 4
                            spell_name="Atraccion",
                            initial_pa=ctx.current_pa,
                        )
                        # Tras atraer, es probable que el turno termine o se necesite re-evaluar
                        print("[COMBAT] Sacrogito - pasando turno (Space)")
                        ctx.actions.quick_press_key("space")
                        return "done"
                print("[COMBAT] Sacrogito - sin enemigo cuerpo a cuerpo y sin objetivo para atraer, no lanza Disolucion")
                print("[COMBAT] Sacrogito - pasando turno (Space)")
                ctx.actions.quick_press_key("space")
                ctx.actions.park_mouse(ctx.screen.parking_regions())
                return "done"
            spell_key, spell_name = "1", "Disolucion"
            expected_spell_id = None

        initial_pa = ctx.current_pa
        if should_cast_castigo and callable(getattr(ctx, "combat_probe", None)):
            ctx.combat_probe("CastigoOsado", self_pos)
        cast_confirmed, refreshed_state = self._cast_self_spell_with_retries(
            ctx,
            self_pos,
            spell_key=spell_key,
            spell_name=spell_name,
            initial_pa=initial_pa,
            expected_spell_id=expected_spell_id,
            confirm_castigo_state=should_cast_castigo,
        )

        if should_cast_castigo and refreshed_state and (
            refreshed_state.get("castigo_osado_active")
            or int(refreshed_state.get("castigo_osado_cooldown") or 0) > 0
        ):
            self._castigo_active = True
            remaining_pa = refreshed_state.get("current_pa")
            try:
                remaining_pa = int(remaining_pa) if remaining_pa is not None else None
            except (TypeError, ValueError):
                remaining_pa = None
            if remaining_pa is not None and remaining_pa >= 3:
                print(f"[COMBAT] Sacrogito - PA restantes tras Castigo Osado: {remaining_pa}; probando Castigo Agilesco")
                _, refreshed_state = self._cast_self_spell_with_retries(
                    ctx,
                    self_pos,
                    spell_key="3",
                    spell_name="CastigoAgilesco",
                    initial_pa=remaining_pa,
                    expected_spell_id=None,
                    confirm_castigo_state=False,
                )

        post = ctx.screen.capture()
        cerrar_post = ctx.ui_detector.find_ui(post, "Cerrar")
        if cerrar_post:
            ctx.actions.quick_click((mon["left"] + cerrar_post[0], mon["top"] + cerrar_post[1]))
            time.sleep(config_delay(ctx.config, "combat_close_delay", 0.15))
            return "combat_ended"

        print("[COMBAT] Sacrogito - pasando turno (Space)")
        ctx.actions.quick_press_key("space")
        ctx.actions.park_mouse(ctx.screen.parking_regions())
        return "done"
