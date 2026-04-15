import time
from .base import CombatProfile, CombatContext, config_delay
import random

# Spell IDs confirmados via sniffer
_SPELL_TEMBLOR = 181
_SPELL_VIENTO_ENVENENADO = 196
_SPELL_POTENCIA_SILVESTRE = 197
_SPELL_LA_SACRIFICADA = 189
_SPELL_ZARZA = 183


class Profile(CombatProfile):
    name = "Sadida"
    needs_panel = True
    mi_turno_template = "MiTurno"

    def __init__(self):
        super().__init__()
        self._last_combo_turn = -999
        self._h3_pending = False
        self._zarza_casts_this_turn = 0
        self._ps_cast_at_turn = -999   # turno propio en que se lanzó Potencia Silvestre por última vez

    def placement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Maximizar enemigos a distancia <= 9, y secundariamente mantenerse lo mas lejos posible.
        enemies_in_range = sum(1 for d in enemy_distances if d <= 9)
        closest_dist = min(enemy_distances) if enemy_distances else 999
        return (-enemies_in_range, -closest_dist, self_distance, cell_id)

    def movement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Para Temblor/Viento: priorizar muchas unidades en rango y kitear dentro de ese rango.
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
        local_pa: int | None = None,
    ) -> bool:
        state = ctx.refresh_combat_state(0.0)
        # Usar PA local (estimado tras hechizos anteriores) si está disponible,
        # porque el sniffer puede tener GTM pendiente con PA desactualizado.
        pa_before = local_pa if local_pa is not None else state.get("current_pa")

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
            confirm = state.get("last_spell_server_confirm") or {}

            if not confirmed:
                pa_after = state.get("current_pa")
                if confirm_at >= attempt_started_at:
                    if expected_spell_id is None or confirm.get("spell_id") == expected_spell_id:
                        confirmed = True
                        confirm_time = time.time()
                elif pa_before is not None and pa_after is not None and pa_after < pa_before:
                    # El sniffer reportó caída de PA respecto al estimado local → hechizo lanzado
                    confirmed = True
                    confirm_time = time.time()

            if confirmed:
                seq_ready_at = float(state.get("last_action_sequence_ready_at") or 0.0)
                if seq_ready_at >= attempt_started_at:
                    if time.time() > seq_ready_at + 0.15:
                        return True
                if time.time() > confirm_time + 2.0:
                    return True

        return False

    def _already_confirmed(self, ctx: CombatContext, spell_id: int, since: float) -> bool:
        state = ctx.refresh_combat_state(0.3)
        confirm_at = float(state.get("last_spell_server_confirm_at") or 0.0)
        confirm = state.get("last_spell_server_confirm") or {}
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
        if current_pa < cost:
            print(f"[SADIDA] {name}: PA insuficiente ({current_pa}/{cost}). Saltando.")
            return False, current_pa

        t_start = time.time()
        ok = self._cast_spell(ctx, key, target_pos, expected_spell_id=spell_id, local_pa=current_pa)

        state = ctx.refresh_combat_state(0.0)
        if state.get("fight_ended"):
            return True, 0
        _raw_pa = state.get("current_pa")
        pa_now = int(_raw_pa) if _raw_pa is not None else current_pa

        if not ok and pa_now < current_pa:
            print(f"[SADIDA] {name} confirmado por caida de PA ({current_pa}->{pa_now}).")
            ok = True
        elif not ok:
            if self._already_confirmed(ctx, spell_id, t_start):
                print(f"[SADIDA] {name} confirmado tardamente por sniffer.")
                ok = True
                state = ctx.refresh_combat_state(0.0)
                _late_pa = state.get("current_pa")
                pa_now = int(_late_pa) if _late_pa is not None else pa_now
            else:
                # Sniffer puede tener PA desactualizado (GTM pendiente). Preservar estimado local.
                print(f"[SADIDA] {name} FALLO. PA local={current_pa} sniffer={pa_now}")
                return False, current_pa

        if ok and pa_now >= current_pa:
            pa_now = current_pa - cost
            print(f"[SADIDA] {name} PA estimado localmente: {current_pa}->{pa_now} (sniffer pendiente)")

        return ok, pa_now

    def _repeat_same_spell_until_success(
        self,
        ctx: CombatContext,
        name: str,
        key: str,
        target_pos: tuple,
        spell_id: int,
        cost: int,
        current_pa: int,
        self_target: bool = False,
    ) -> tuple[bool, int]:
        """Reintenta el mismo hechizo hasta confirmarlo o agotar los intentos.

        self_target=True: re-proyecta la posición desde ctx.my_cell del sniffer en cada intento.
        """
        max_attempts = int(ctx.config.get("bot", {}).get("sadida_combo_retry_attempts", 6) or 6)
        attempts = 0
        ok = False
        retry_delay = config_delay(ctx.config, "combat_retry_delay", 0.35)

        while current_pa >= cost and not ok and attempts < max_attempts:
            # Re-proyectar posición desde celda actual del sniffer en cada intento
            pos = target_pos
            if self_target and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
                fresh_pos = ctx.project_self_cell(ctx.my_cell)
                if fresh_pos:
                    pos = fresh_pos

            ok, current_pa = self._cast_with_pa_gate(ctx, name, key, pos, spell_id, cost, current_pa)
            attempts += 1

            # Drenar sniffer y chequear fin de combate
            state = ctx.refresh_combat_state(0.0)
            if state.get("fight_ended"):
                return True, 0

            if not ok:
                print(f"[SADIDA] {name} fallo. Reintentando mismo hechizo ({attempts}/{max_attempts}).")
                # Espera con drain del sniffer (en vez de sleep ciego)
                state = ctx.refresh_combat_state(retry_delay)
                if state.get("fight_ended"):
                    return True, 0

                # Verificar PA real del sniffer tras la espera (GTM puede haber llegado)
                sniffer_pa = state.get("current_pa")
                if sniffer_pa is not None and sniffer_pa < cost:
                    print(f"[SADIDA] {name}: Sniffer confirma PA insuficiente tras espera ({sniffer_pa}<{cost}). Deteniendo reintentos.")
                    current_pa = sniffer_pa
                    break

        return ok, current_pa

    def on_turn(self, action_pos: tuple | None, ctx: CombatContext) -> str:
        # ── Esperar GTM de inicio de turno ──────────────────────────────────
        # GTS resetea _sniffer_pa a None. Esperamos hasta 1.0s para el GTM.
        # Caso especial (forma árbol): el GTM puede llegar ANTES del GTS o tener
        # el campo AP ausente. En ese caso pa_pre_gts contiene el valor correcto.
        wait_gtm_deadline = time.time() + 1.0
        last_state = None
        while ctx.current_pa is None and time.time() < wait_gtm_deadline:
            last_state = ctx.refresh_combat_state(0.05)
            if last_state.get("fight_ended"):
                return "combat_ended"
            if ctx.current_pa is not None:
                break

        # Si el sniffer nunca entregó PA tras el GTS, usar el valor capturado
        # justo antes del GTS (pa_pre_gts): cubre el caso "GTM antes de GTS".
        if ctx.current_pa is None and last_state is not None:
            _pre = last_state.get("pa_pre_gts")
            if _pre is not None:
                ctx.current_pa = int(_pre)
                print(f"[SADIDA] GTM llegó antes del GTS — PA rescatado: {_pre}")

        # Reset de estado entre peleas: DEBE hacerse ANTES de calcular _local_ps_cd
        # para evitar que valores de la pelea anterior contaminen la lógica actual.
        if ctx.turn_number == 1:
            self._last_combo_turn = -999
            self._h3_pending = False
            self._ps_cast_at_turn = -999

        # Cooldown local de PS: respaldo en caso de que el paquete SC no llegue
        # o se pierda (p.ej. en la transición forma árbol → turno de árbol).
        _turns_since_ps = ctx.turn_number - self._ps_cast_at_turn
        _local_ps_cd = max(0, 11 - _turns_since_ps)

        # Si sabemos que PS fue lanzado recientemente y el sniffer no entregó PA,
        # asumimos forma árbol → PA=0.
        if ctx.current_pa is None and _local_ps_cd > 0:
            ctx.current_pa = 0
            print(f"[SADIDA] PA desconocido + PS reciente (cd local={_local_ps_cd}) → asumiendo forma árbol PA=0.")

        current_pa = ctx.current_pa if ctx.current_pa is not None else 8
        current_mp = ctx.current_mp if ctx.current_mp is not None else 3
        print(f"[SADIDA] Inicio turno #{ctx.turn_number}: PA={current_pa} PM={current_mp} cell={ctx.my_cell} "
              f"(sniffer_raw: PA={ctx.current_pa} PM={ctx.current_mp} ps_cd_local={_local_ps_cd})")

        # PA=0 → forma árbol u otro estado sin acciones. Pasar turno inmediatamente.
        if current_pa == 0:
            print("[SADIDA] PA=0 — pasando turno.")
            ctx.actions.quick_press_key("space")
            return "done"
        self._zarza_casts_this_turn = 0

        self_pos = action_pos
        if not self_pos and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
            self_pos = ctx.project_self_cell(ctx.my_cell)

        is_tackled = False
        if getattr(ctx, "enemy_in_melee_range", None):
            is_tackled = ctx.enemy_in_melee_range()

        cooldown_181 = ctx.spell_cooldowns.get(_SPELL_TEMBLOR, 0)
        cooldown_196 = ctx.spell_cooldowns.get(_SPELL_VIENTO_ENVENENADO, 0)
        # Usar el máximo entre el CD reportado por el servidor y el CD local calculado
        # por turno. Esto protege contra el caso donde el paquete SC de Potencia Silvestre
        # no llegó antes del GTS del siguiente turno (forma árbol sin cooldown detectado).
        cooldown_197 = max(ctx.spell_cooldowns.get(_SPELL_POTENCIA_SILVESTRE, 0), _local_ps_cd)
        combo_1_ready = (
            cooldown_181 == 0 and cooldown_196 == 0 and cooldown_197 == 0
            and current_pa >= 8 and self_pos is not None
        )

        if self._h3_pending:
            print(f"[SADIDA] H3 pendiente del turno anterior (PA={current_pa}).")
            if self_pos and current_pa >= 3:
                h3_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H3 Potencia Silvestre (Pendiente)", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 3, current_pa,
                    self_target=True,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if h3_ok:
                    self._h3_pending = False
                    self._ps_cast_at_turn = ctx.turn_number  # Registrar turno del lanzamiento exitoso
            print("[SADIDA] Fin resolucion de H3 pendiente. Pasando turno.")
            ctx.actions.quick_press_key("space")
            return "done"

        # Combo 1 estricto segun combatcontext.md:
        # solo se ejecuta si la secuencia completa es viable.
        if combo_1_ready:
            print(f"[SADIDA] Ejecutando Combo 1 estricto (PA={current_pa} MP={current_mp})")

            if not is_tackled and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
                print("[SADIDA] Optimizando posicion para Combo 1...")
                move_res = ctx.move_towards_enemy(current_mp, desired_range=0)
                if move_res and move_res.get("moved"):
                    time.sleep(0.3)
                    if move_res.get("self_screen_pos"):
                        self_pos = move_res["self_screen_pos"]
                    if move_res.get("combat_cell") is not None:
                        ctx.my_cell = move_res["combat_cell"]
                    _st = ctx.refresh_combat_state(0.0)
                    if _st.get("fight_ended"):
                        return "combat_ended"
                    _raw_pa = _st.get("current_pa")
                    current_pa = int(_raw_pa) if _raw_pa is not None else current_pa
                    if not self_pos and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
                        self_pos = ctx.project_self_cell(ctx.my_cell)
            elif is_tackled and current_mp > 0:
                print("[SADIDA] Enemigo adyacente (CaC). Omitiendo movimiento para evitar placaje.")

            if current_pa >= 8 and self_pos:
                h1_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H1 Temblor", "1", self_pos, _SPELL_TEMBLOR, 2, current_pa
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if not h1_ok:
                    print("[SADIDA] H1 no pudo confirmarse tras reintentos. Pasando turno.")
                    ctx.actions.quick_press_key("space")
                    return "done"

                h2_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H2 Viento Envenenado", "2", self_pos, _SPELL_VIENTO_ENVENENADO, 3, current_pa
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if not h2_ok:
                    print("[SADIDA] H2 no pudo confirmarse tras reintentos. Pasando turno.")
                    ctx.actions.quick_press_key("space")
                    return "done"

                if current_pa >= 3:
                    h3_ok, current_pa = self._repeat_same_spell_until_success(
                        ctx, "H3 Potencia Silvestre", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 3, current_pa,
                        self_target=True,
                    )
                    if ctx.refresh_combat_state(0.0).get("fight_ended"):
                        return "combat_ended"
                    if h3_ok:
                        self._ps_cast_at_turn = ctx.turn_number  # Registrar turno del lanzamiento exitoso
                    else:
                        print("[SADIDA] H3 no pudo confirmarse tras reintentos. Marcando pendiente para el proximo turno.")
                        self._h3_pending = True
                        ctx.actions.quick_press_key("space")
                        return "done"
                else:
                    print("[SADIDA] PA insuficiente para H3. Queda pendiente para el proximo turno.")
                    self._h3_pending = True

                print("[SADIDA] Fin Secuencia Combo 1. Pasando turno.")
                ctx.actions.quick_press_key("space")
                return "done"

            print(f"[SADIDA] Combo 1 deja de ser viable tras refresco (PA={current_pa} self_pos={bool(self_pos)}). Paso a Combo 2.")

        combo_1_reasons = []
        if cooldown_181 != 0:
            combo_1_reasons.append(f"cooldown Temblor={cooldown_181}")
        if cooldown_196 != 0:
            combo_1_reasons.append(f"cooldown VientoEnvenenado={cooldown_196}")
        if cooldown_197 != 0:
            combo_1_reasons.append(f"cooldown PotenciaSilvestre={cooldown_197}")
        if current_pa < 8:
            combo_1_reasons.append(f"PA insuficiente={current_pa}")
        if not self_pos:
            combo_1_reasons.append("sin self_pos")
        print(f"[SADIDA] Combo 1 no viable ({', '.join(combo_1_reasons) or 'sin motivo'}). Ejecutando Combo 2 (PA={current_pa})")

        if ctx.my_cell is None or not ctx.enemies:
            ctx.actions.quick_press_key("space")
            return "done"

        # Comportamiento base: estatico. Solo mover si no hay LoS para atacar.
        has_targets_in_los = any(
            ctx.has_line_of_sight(ctx.my_cell, e["cell_id"]) for e in ctx.enemies
            if ctx.cell_distance(ctx.my_cell, e["cell_id"]) <= 8
        ) if getattr(ctx, "has_line_of_sight", None) and getattr(ctx, "cell_distance", None) else True

        if not has_targets_in_los and not is_tackled and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
            print("[SADIDA] Sin vision para atacar. Intentando obtener LoS.")
            move_res = ctx.move_towards_enemy(current_mp, desired_range=8)
            if move_res and move_res.get("moved"):
                time.sleep(0.3)
                if move_res.get("combat_cell") is not None:
                    ctx.my_cell = move_res["combat_cell"]

        # Refrescar estado completo antes de Combo 2: PA y posiciones enemigas actuales del sniffer
        fresh = ctx.refresh_combat_state(0.15)
        if fresh.get("fight_ended"):
            return "combat_ended"
        # Si todos los enemigos murieron (veneno/efecto entre turnos) antes de que llegue el GE:
        if not ctx.enemies:
            print("[SADIDA] Sin enemigos tras refresco — GE pendiente. Pasando turno.")
            ctx.actions.quick_press_key("space")
            return "done"
        fresh_pa = fresh.get("current_pa")
        current_pa = int(fresh_pa) if fresh_pa is not None else current_pa
        ctx.spell_cooldowns = dict(fresh.get("spell_cooldowns") or ctx.spell_cooldowns)

        cooldown_189 = ctx.spell_cooldowns.get(_SPELL_LA_SACRIFICADA, 0)
        if current_pa >= 3 and cooldown_189 == 0:
            r = ctx.my_cell // 14
            adj_offsets = [-14, 14, -15 if r % 2 == 0 else -13, 13 if r % 2 == 0 else 15]
            adj_cells = [ctx.my_cell + off for off in adj_offsets if 0 <= ctx.my_cell + off <= 400]
            occupied_cells = {e["cell_id"] for e in ctx.enemies}

            ctx.enemies.sort(
                key=lambda e: ctx.cell_distance(ctx.my_cell, e["cell_id"])
                if getattr(ctx, "cell_distance", None) else 999
            )
            closest_enemy = ctx.enemies[0]

            # Ordenar celdas adyacentes válidas por distancia al enemigo más cercano
            candidate_cells: list[tuple[float, int]] = []
            for cell in adj_cells:
                if cell in occupied_cells:
                    continue
                if getattr(ctx, "has_line_of_sight", None) and not ctx.has_line_of_sight(ctx.my_cell, cell):
                    continue
                dist = ctx.cell_distance(cell, closest_enemy["cell_id"]) if getattr(ctx, "cell_distance", None) else 999
                if dist is not None:
                    candidate_cells.append((dist, cell))
            candidate_cells.sort()  # menor distancia primero

            ok_sacri = False
            for _, target_cell in candidate_cells:
                if current_pa < 3:
                    break
                sacri_pos = ctx.project_self_cell(target_cell) if getattr(ctx, "project_self_cell", None) else None
                if not sacri_pos:
                    continue
                print(f"[SADIDA] H4 La Sacrificada intentando celda={target_cell}")
                ok_sacri, current_pa = self._cast_with_pa_gate(
                    ctx, "H4 La Sacrificada", "4", sacri_pos, _SPELL_LA_SACRIFICADA, 3, current_pa
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if ok_sacri:
                    break
                print(f"[SADIDA] H4 La Sacrificada falló en celda={target_cell}, probando siguiente.")

        while current_pa >= 4 and self._zarza_casts_this_turn < 2:
            # Refrescar posiciones enemigas del sniffer antes de cada Zarza
            ctx.refresh_combat_state(0.1)
            valid_targets = [
                e for e in ctx.enemies
                if e.get("cell_id") is not None
                and (not getattr(ctx, "has_line_of_sight", None) or ctx.has_line_of_sight(ctx.my_cell, e["cell_id"]))
                and (not getattr(ctx, "cell_distance", None) or ctx.cell_distance(ctx.my_cell, e["cell_id"]) <= 8)
            ]
            if not valid_targets:
                print("[SADIDA] H5 Zarza: sin objetivos válidos en rango/LoS.")
                break

            valid_targets.sort(key=lambda e: e.get("hp", 9999))
            target = valid_targets[0]
            # Usar siempre la proyección de cell_id actual del sniffer (no screen_pos estático)
            enemy_pos = (
                ctx.project_self_cell(target["cell_id"])
                if getattr(ctx, "project_self_cell", None)
                else target.get("screen_pos")
            )
            if not enemy_pos:
                print(f"[SADIDA] H5 Zarza: no se pudo proyectar celda={target['cell_id']}. Saltando.")
                break
            print(f"[SADIDA] H5 Zarza -> actor={target.get('id')} cell={target['cell_id']} HP={target.get('hp')} pos={enemy_pos}")

            ok_zarza = False
            attempts = 0
            while current_pa >= 4 and not ok_zarza and attempts < 2:
                ok_zarza, current_pa = self._cast_with_pa_gate(
                    ctx, "H5 Zarza", "5", enemy_pos, _SPELL_ZARZA, 4, current_pa
                )
                attempts += 1
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"

            if ok_zarza:
                self._zarza_casts_this_turn += 1
            else:
                break

        ctx.actions.quick_press_key("space")
        return "done"
