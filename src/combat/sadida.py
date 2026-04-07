import time
from .base import CombatProfile, CombatContext, config_delay
import random

# Spell IDs confirmados via sniffer
_SPELL_TEMBLOR           = 181
_SPELL_VIENTO_ENVENENADO = 196
_SPELL_POTENCIA_SILVESTRE = 197
_SPELL_LA_SACRIFICADA    = 189
_SPELL_ZARZA             = 183


class Profile(CombatProfile):
    name = "Sadida"
    needs_panel = True
    mi_turno_template = "MiTurno"

    def __init__(self):
        super().__init__()
        self._last_combo_turn = -999
        # Cross-turn: H3 fue intentado pero no confirmado → reintentar próximo turno
        self._h3_pending = False
        self._zarza_casts_this_turn = 0

    def placement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Maximizar enemigos a distancia <= 9, y secundariamente mantenerse lo más lejos posible (Kite)
        enemies_in_range = sum(1 for d in enemy_distances if d <= 9)
        closest_dist = min(enemy_distances) if enemy_distances else 999
        return (-enemies_in_range, -closest_dist, self_distance, cell_id)

    def movement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Para Temblor y Viento Envenenado (AoE radio 9):
        # 1. Maximizar enemigos a distancia <= 9
        # 2. Mantenerse lo más lejos posible dentro de ese rango (Kiteo)
        enemies_in_range = sum(1 for d in enemy_distances if d <= 9)
        closest_dist = min(enemy_distances) if enemy_distances else 999
        return (-enemies_in_range, -closest_dist, self_distance, cell_id)

    def _cast_spell(
        self,
        ctx: CombatContext,
        key: str,
        target_pos: tuple[int, int],
        expected_spell_id: int | None = None,
        timeout: float = 3.0,
    ) -> bool:
        """
        Lanza un hechizo y espera confirmación del servidor.
        Retorna True si confirmado y la secuencia de animación terminó, False si timeout.

        Confirmación verificada por:
          1. server_confirm_at >= attempt_started_at  (no acepta confirmaciones previas)
          2. spell_id == expected_spell_id            (si se pasa)
          3. PA disminuyó                             (señal de respaldo)
        
        Luego espera el paquete S{actor_id} (last_action_sequence_ready_at) para evitar solapar clicks.
        """
        state = ctx.refresh_combat_state(0.0)
        pa_before = state.get("current_pa")

        attempt_started_at = time.time()
        ctx.actions.quick_press_key(key)
        time.sleep(config_delay(ctx.config, "combat_spell_select_delay", 0.12))
        ctx.actions.quick_click(target_pos)

        deadline = attempt_started_at + timeout
        confirmed = False
        confirm_time = 0.0

        while time.time() < deadline:
            state = ctx.refresh_combat_state(0.05)
            if state.get("fight_ended"):
                return True

            confirm_at = float(state.get("last_spell_server_confirm_at") or 0.0)
            confirm    = state.get("last_spell_server_confirm") or {}

            if not confirmed:
                pa_after = state.get("current_pa")
                if confirm_at >= attempt_started_at:
                    if expected_spell_id is None or confirm.get("spell_id") == expected_spell_id:
                        confirmed = True
                        confirm_time = time.time()
                elif pa_before is not None and pa_after is not None and pa_after < pa_before:
                    confirmed = True
                    confirm_time = time.time()

            if confirmed:
                seq_ready_at = float(state.get("last_action_sequence_ready_at") or 0.0)
                # Check if we received a sequence ready packet AFTER we started casting
                if seq_ready_at >= attempt_started_at:
                    # Give it a tiny bit of extra time to process the actual GameAction, 
                    # as S is sent immediately before or during the GameAction packet
                    if time.time() > seq_ready_at + 0.15:
                        return True
                
                # Esperamos un máximo de 2.0s por la animación si el paquete S se pierde
                if time.time() > confirm_time + 2.0:
                    return True

        return False

    def _already_confirmed(self, ctx: CombatContext, spell_id: int, since: float) -> bool:
        """
        Comprueba si el sniffer ya tiene confirmación del spell_id desde 'since'.
        Útil para detectar confirmaciones tardías antes de reintentar.
        """
        state = ctx.refresh_combat_state(0.3)
        confirm_at = float(state.get("last_spell_server_confirm_at") or 0.0)
        confirm    = state.get("last_spell_server_confirm") or {}
        return confirm_at >= since and confirm.get("spell_id") == spell_id

    def _cast_with_pa_gate(
        self,
        ctx: CombatContext,
        name: str,
        key: str,
        target_pos: tuple,
        spell_id: int,
        cost: int,
        current_pa: int,
    ) -> tuple[bool, int]:
        """
        Lanza un hechizo sólo si hay PA suficientes. Usa el PA del sniffer como
        fuente de verdad: si el PA bajó tras el intento, el hechizo se considera
        confirmado aunque _cast_spell devuelva False (confirmación llegó tarde).

        Retorna (ok, pa_restante_actualizado).
        """
        if current_pa < cost:
            print(f"[SADIDA] {name}: PA insuficiente ({current_pa}/{cost}). Saltando.")
            return False, current_pa

        t_start = time.time()
        ok = self._cast_spell(ctx, key, target_pos, expected_spell_id=spell_id)

        state = ctx.refresh_combat_state(0.0)
        if state.get("fight_ended"):
            return True, 0
        pa_now = int(state.get("current_pa") or current_pa)

        # Si el PA bajó, el hechizo se lanzó aunque el timeout haya expirado
        if not ok and pa_now < current_pa:
            print(f"[SADIDA] {name} confirmado por caída de PA ({current_pa}→{pa_now}).")
            ok = True
        elif not ok:
            # Un último intento de confirmación tardía vía sniffer
            if self._already_confirmed(ctx, spell_id, t_start):
                print(f"[SADIDA] {name} confirmado tardíamente por sniffer.")
                ok = True
                state = ctx.refresh_combat_state(0.0)
                pa_now = int(state.get("current_pa") or pa_now)
            else:
                print(f"[SADIDA] {name} FALLÓ. PA antes={current_pa} ahora={pa_now}")

        # El sniffer puede tardar en reportar el gasto de PA (GA 950 llega tarde).
        # Si el hechizo fue confirmado pero el sniffer aún muestra el PA anterior,
        # aplicamos la deducción manualmente para que el next gate sea correcto.
        if ok and pa_now >= current_pa:
            pa_now = current_pa - cost
            print(f"[SADIDA] {name} PA estimado localmente: {current_pa}→{pa_now} (sniffer pendiente)")

        return ok, pa_now

    def on_turn(self, action_pos: tuple | None, ctx: CombatContext) -> str:
        current_pa = ctx.current_pa if ctx.current_pa is not None else 6
        current_mp = ctx.current_mp if ctx.current_mp is not None else 3

        self._zarza_casts_this_turn = 0

        # Resolver posición absoluta del personaje para self-targets
        self_pos = action_pos
        if not self_pos and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
            self_pos = ctx.project_self_cell(ctx.my_cell)

        # Evaluar si estamos placados (enemigo Cuerpo a Cuerpo)
        is_tackled = False
        if getattr(ctx, "enemy_in_melee_range", None):
            is_tackled = ctx.enemy_in_melee_range()

        if ctx.turn_number == 1:
            self._last_combo_turn = -999
            self._h3_pending = False

        potencia_activa = (current_pa == 0 and current_mp == 0)
        if potencia_activa:
            print(f"[SADIDA] Potencia Silvestre activa (PA=0 MP=0). Pasando turno.")
            ctx.actions.quick_press_key("space")
            return "done"

        cooldown_197 = ctx.spell_cooldowns.get(_SPELL_POTENCIA_SILVESTRE, 0)
        combo_1_ready = (cooldown_197 == 0)

        # ── COMBO 1 (Prioridad Máxima): Temblor + Viento + Potencia ─────────
        if combo_1_ready or self._h3_pending:
            print(f"[SADIDA] Iniciando fase Combo 1 (PA={current_pa} MP={current_mp})")
            
            if not self_pos:
                print("[SADIDA] Sin posicion de PJ para combo 1. Pasando turno.")
                ctx.actions.quick_press_key("space")
                return "done"

            # H3 Pendiente del turno anterior
            if self._h3_pending and current_pa >= 3:
                h3_ok = False
                while current_pa >= 3 and not h3_ok:
                    h3_ok, current_pa = self._cast_with_pa_gate(
                        ctx, "H3 Potencia Silvestre (Pendiente)", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 3, current_pa
                    )
                    if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
                
                if h3_ok:
                    self._h3_pending = False
                
                print("[SADIDA] Fin resolución de turno pendiente. Pasando turno.")
                ctx.actions.quick_press_key("space")
                return "done"

            # Flujo Normal Combo 1
            if combo_1_ready:
                # Moverse para maximizar el área de efecto de Temblor/Viento Envenenado (Radio 9)
                if not is_tackled and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
                    print("[SADIDA] Optimizando posición para maximizar AoE (Radio 9)...")
                    move_res = ctx.move_towards_enemy(current_mp, desired_range=0)
                    if move_res and move_res.get("moved"):
                        time.sleep(0.3)
                        if move_res.get("self_screen_pos"):
                            self_pos = move_res["self_screen_pos"]
                        if move_res.get("combat_cell") is not None:
                            ctx.my_cell = move_res["combat_cell"]
                        
                        if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
                        current_pa = int(ctx.refresh_combat_state(0.0).get("current_pa") or current_pa)
                elif is_tackled and current_mp > 0:
                    print("[SADIDA] Enemigo adyacente (CaC). Omitiendo movimiento para evitar pérdida de PA/PM por placaje.")

                # H1 Temblor (2 PA) - Bucle infinito hasta que se confirme o falte PA
                h1_ok = False
                while current_pa >= 2 and not h1_ok:
                    h1_ok, current_pa = self._cast_with_pa_gate(ctx, "H1 Temblor", "1", self_pos, _SPELL_TEMBLOR, 2, current_pa)
                    if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"

                # H2 Viento Envenenado (3 PA)
                h2_ok = False
                while current_pa >= 3 and not h2_ok:
                    h2_ok, current_pa = self._cast_with_pa_gate(ctx, "H2 Viento Envenenado", "2", self_pos, _SPELL_VIENTO_ENVENENADO, 3, current_pa)
                    if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"

                # H3 Potencia Silvestre (3 PA)
                h3_ok = False
                while current_pa >= 3 and not h3_ok:
                    h3_ok, current_pa = self._cast_with_pa_gate(ctx, "H3 Potencia Silvestre", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 3, current_pa)
                    if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"

                # Fallback cross-turn si los PA no alcanzaron para H3
                if not h3_ok and current_pa < 3:
                    print("[SADIDA] PA insuficiente para H3. Queda pendiente para el próximo turno.")
                    self._h3_pending = True

            print("[SADIDA] Fin Secuencia Combo 1. Pasando turno.")
            ctx.actions.quick_press_key("space")
            return "done"

        # ── COMBO 2: La Sacrificada + Zarza x(N) ────────────────────────────
        print(f"[SADIDA] Combo 1 en CD. Ejecutando Combo 2 (PA={current_pa})")

        if ctx.my_cell is None or not ctx.enemies:
            ctx.actions.quick_press_key("space")
            return "done"

        # Lógica de movimiento: Estático. Solo mover si NO hay línea de visión.
        has_targets_in_los = any(
            ctx.has_line_of_sight(ctx.my_cell, e["cell_id"]) for e in ctx.enemies 
            if ctx.cell_distance(ctx.my_cell, e["cell_id"]) <= 8
        ) if getattr(ctx, "has_line_of_sight", None) and getattr(ctx, "cell_distance", None) else True

        if not has_targets_in_los and not is_tackled and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
            print("[SADIDA] Sin visión para atacar. Intentando obtener LoS.")
            move_res = ctx.move_towards_enemy(current_mp, desired_range=8)
            if move_res and move_res.get("moved"):
                time.sleep(0.3)
                if move_res.get("combat_cell") is not None:
                    ctx.my_cell = move_res["combat_cell"]
        
        if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
        current_pa = int(ctx.refresh_combat_state(0.0).get("current_pa") or current_pa)

        # H4 La Sacrificada (3 PA, Rango 1)
        cooldown_189 = ctx.spell_cooldowns.get(_SPELL_LA_SACRIFICADA, 0)
        if current_pa >= 3 and cooldown_189 == 0:
            r = ctx.my_cell // 14
            adj_offsets = [-14, 14, -15 if r % 2 == 0 else -13, 13 if r % 2 == 0 else 15]
            adj_cells = [ctx.my_cell + off for off in adj_offsets if 0 <= ctx.my_cell + off <= 400]
            
            # Evitar invocar en la misma celda de un enemigo (si está CaC)
            occupied_cells = {e["cell_id"] for e in ctx.enemies}
            
            ctx.enemies.sort(key=lambda e: ctx.cell_distance(ctx.my_cell, e["cell_id"]) if getattr(ctx, "cell_distance", None) else 999)
            closest_enemy = ctx.enemies[0]
            
            best_adj, best_dist = None, float('inf')
            for cell in adj_cells:
                if cell in occupied_cells:
                    continue
                dist = ctx.cell_distance(cell, closest_enemy["cell_id"]) if getattr(ctx, "cell_distance", None) else None
                if dist is not None and dist < best_dist:
                    best_dist = dist
                    best_adj = cell
                    
            if best_adj and getattr(ctx, "project_self_cell", None):
                sacri_pos = ctx.project_self_cell(best_adj)
                if sacri_pos:
                    ok_sacri = False
                    while current_pa >= 3 and not ok_sacri:
                        ok_sacri, current_pa = self._cast_with_pa_gate(ctx, "H4 La Sacrificada", "4", sacri_pos, _SPELL_LA_SACRIFICADA, 3, current_pa)
                        if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"

        # H5 Zarza (4 PA, limit 2/turn) - Prioriza menor HP
        while current_pa >= 4 and self._zarza_casts_this_turn < 2:
            valid_targets = [
                e for e in ctx.enemies 
                if (not getattr(ctx, "has_line_of_sight", None) or ctx.has_line_of_sight(ctx.my_cell, e["cell_id"]))
                and (not getattr(ctx, "cell_distance", None) or ctx.cell_distance(ctx.my_cell, e["cell_id"]) <= 8)
            ]
            if not valid_targets:
                break
            
            valid_targets.sort(key=lambda e: e.get("hp", 9999))
            target = valid_targets[0]
            
            # Proyectar el suelo exacto de la celda del enemigo (más preciso que screen_pos)
            enemy_pos = target["screen_pos"]
            if getattr(ctx, "project_self_cell", None):
                enemy_pos = ctx.project_self_cell(target["cell_id"]) or enemy_pos

            ok_zarza = False
            attempts = 0
            while current_pa >= 4 and not ok_zarza and attempts < 2:
                ok_zarza, current_pa = self._cast_with_pa_gate(ctx, "H5 Zarza", "5", enemy_pos, _SPELL_ZARZA, 4, current_pa)
                attempts += 1
                if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
            
            if ok_zarza:
                self._zarza_casts_this_turn += 1
            else:
                break  # Evitar un loop infinito si la IA no logra hacer el hit

        ctx.actions.quick_press_key("space")
        return "done"
