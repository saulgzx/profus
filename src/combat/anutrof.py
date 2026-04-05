import time
from .base import CombatProfile, CombatContext, config_delay


class Profile(CombatProfile):
    name = "Anutrof"

    def on_placement(self, listo_pos: tuple, ctx: CombatContext) -> None:
        print("[COMBAT] Anutrof — Click Listo (colocacion)")
        ctx.actions.quick_click(listo_pos)

    def on_turn(self, mi_turno_pos: tuple, ctx: CombatContext) -> str:
        # PA disponibles: usar el valor del sniffer si existe, sino config
        pa_available = ctx.current_pa if ctx.current_pa is not None \
            else ctx.config["bot"].get("combat_pa", 6)
        # Coste por ataque (tecla 1) — configurable
        spell_pa_cost = ctx.config["bot"].get("combat_spell_pa_cost", 3)

        print(f"[COMBAT] Anutrof — turno: PA={pa_available} (coste/hechizo={spell_pa_cost})")

        # ── Determinar objetivos ──────────────────────────────────────────────
        # Prioridad 1: posiciones reales de enemigos obtenidas del sniffer + calibración
        if ctx.enemy_positions:
            targets = list(ctx.enemy_positions)
            print(f"[COMBAT] Anutrof — apuntando a {len(targets)} enemigo(s) real(es): {targets}")
        else:
            # Fallback: desplazamiento desde el botón MiTurno (comportamiento anterior)
            offset     = ctx.config["bot"].get("combat_card_offset", 90)
            card_width = ctx.config["bot"].get("combat_card_width", 90)
            max_targets = ctx.config["bot"].get("combat_max_targets", 3)
            targets = [
                (mi_turno_pos[0] + offset + card_width * i, mi_turno_pos[1])
                for i in range(max_targets)
            ]
            print(f"[COMBAT] Anutrof — sin posiciones reales, usando offsets: {targets[:2]}…")

        target_idx   = 0
        attacks_done = 0

        while pa_available >= spell_pa_cost and target_idx < len(targets):
            pre_check = ctx.screen.capture()

            if ctx.ui_detector.find_ui(pre_check, "CerrarCombate"):
                print("[COMBAT] Combate terminado — cerrando ventana")
                ctx.actions.quick_click(ctx.ui_detector.find_ui(pre_check, "CerrarCombate"))
                time.sleep(config_delay(ctx.config, "combat_close_delay", 0.15))
                return "combat_ended"

            # Click previo para dar foco al juego antes de presionar la tecla
            ctx.actions.quick_click(targets[target_idx])
            time.sleep(config_delay(ctx.config, "combat_pre_spell_pause", 0.06))
            ctx.actions.quick_press_key("1")
            time.sleep(config_delay(ctx.config, "combat_spell_select_delay", 0.12))
            ctx.actions.quick_click(targets[target_idx])
            time.sleep(config_delay(ctx.config, "combat_post_click_delay", 0.12))
            ctx.actions.park_mouse(ctx.screen.parking_regions())

            check = ctx.screen.capture()

            if ctx.ui_detector.find_ui(check, "CerrarCombate"):
                print("[COMBAT] Combate terminado durante ataque — cerrando ventana")
                ctx.actions.quick_click(ctx.ui_detector.find_ui(check, "CerrarCombate"))
                time.sleep(config_delay(ctx.config, "combat_close_delay", 0.15))
                return "combat_ended"

            if ctx.ui_detector.find_ui(check, "FueraAlcance"):
                print(f"[COMBAT] Fuera de alcance en objetivo {target_idx + 1} — probando siguiente")
                close_btn = ctx.ui_detector.find_ui(check, "CerrarPopup")
                if close_btn:
                    ctx.actions.quick_click(close_btn)
                time.sleep(config_delay(ctx.config, "combat_popup_close_delay", 0.12))
                target_idx += 1
            else:
                attacks_done += 1
                pa_available -= spell_pa_cost
                print(f"[COMBAT] Ataque {attacks_done} lanzado — PA restantes estimados: {pa_available}")

        # Verificar antes de pasar turno
        final_check = ctx.screen.capture()
        if ctx.ui_detector.find_ui(final_check, "CerrarCombate"):
            print("[COMBAT] Combate terminado post-ataques — cerrando ventana")
            ctx.actions.quick_click(ctx.ui_detector.find_ui(final_check, "CerrarCombate"))
            time.sleep(config_delay(ctx.config, "combat_close_delay", 0.15))
            return "combat_ended"
        if ctx.ui_detector.find_ui(final_check, "FueraAlcance"):
            close_btn = ctx.ui_detector.find_ui(final_check, "CerrarPopup")
            if close_btn:
                ctx.actions.quick_click(close_btn)
            time.sleep(config_delay(ctx.config, "combat_popup_close_delay", 0.12))

        print(f"[COMBAT] Anutrof — fin de turno ({attacks_done} ataques) (Space)")
        ctx.actions.quick_press_key("space")
        ctx.actions.park_mouse(ctx.screen.parking_regions())
        return "done"
