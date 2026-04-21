import time
from .base import CombatProfile, CombatContext, config_delay
import random

try:
    from telemetry import get_telemetry
except Exception:
    def get_telemetry():
        class _Noop:
            def emit(self, *a, **k): pass
            def info(self, *a, **k): pass
            def warn(self, *a, **k): pass
        return _Noop()

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
        self._h1_pending = False
        self._h2_pending = False
        self._h3_pending = False
        self._zarza_casts_this_turn = 0
        self._ps_cast_at_turn = -999   # turno propio en que se lanzó Potencia Silvestre por última vez
        # Contador de fallos consecutivos de H1 (entre turnos). Cuando alcanza
        # el umbral, se fuerza un movimiento táctico para abandonar la celda
        # actual (probablemente con click bloqueado por HUD/sprite/UI).
        self._h1_consecutive_fails = 0
        self._h1_last_fail_cell: int | None = None

    def on_fight_end(self) -> None:
        """Reset de estado entre peleas: si la pelea termino con un combo en curso,
        evitar que la siguiente arranque a mitad de combo (viola prioridad combo 1)."""
        if self._h1_pending or self._h2_pending or self._h3_pending:
            print("[SADIDA] fight_end con combo pendiente — reseteando estado.")
        self._last_combo_turn = -999
        self._h1_pending = False
        self._h2_pending = False
        self._h3_pending = False
        self._zarza_casts_this_turn = 0
        self._ps_cast_at_turn = -999
        self._h1_consecutive_fails = 0
        self._h1_last_fail_cell = None

    def placement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Combo 1 (Temblor/Viento Envenenado) AoE = circulo Manhattan radio 9 (CkCk en hechizos.xml).
        # Prioridades lexicograficas (menor = mejor):
        #   1) MAXIMIZAR enemigos dentro del AoE radio 9 — el OBJETIVO del combo Sadida
        #   2) Maximizar distancia al enemigo mas cercano — desempate, margen marginal
        #   3) Minimizar distancia recorrida (no malgastar PM)
        # NOTA: NO penalizamos placaje. El placaje en Dofus solo aplica cuando intentamos
        # MOVERNOS desde una celda adyacente a un enemigo (perdemos PA/PM al despegarnos).
        # Si nos quedamos en esa celda y casteamos desde ahí, no hay penalty alguna.
        # El combo Sadida (Temblor + Viento Envenenado + Potencia Silvestre) NO requiere
        # movimiento posterior, así que estar adyacente al final del movimiento es FINO.
        closest_dist = min(enemy_distances) if enemy_distances else 999
        enemies_in_range = sum(1 for d in enemy_distances if d <= 9)
        return (-enemies_in_range, -closest_dist, self_distance, cell_id)

    def movement_score(self, cell_id: int, self_distance: int, enemy_distances: list[int]) -> tuple:
        # Mismo criterio que placement_score: maximizar AoE coverage es lo único que importa.
        closest_dist = min(enemy_distances) if enemy_distances else 999
        enemies_in_range = sum(1 for d in enemy_distances if d <= 9)
        return (-enemies_in_range, -closest_dist, self_distance, cell_id)

    def _pass_turn(self, ctx: CombatContext):
        """Pasa el turno: solo Space.

        IMPORTANTE: NO usar ESC aquí. En Dofus 1.29.1 ESC abre el menú
        "Salir del juego", lo cual es destructivo. Pasar turno es siempre
        un Space limpio.

        Si en el futuro reaparece el bug "hechizo armado no deja pasar
        turno tras click fallido", la solución NO es ESC. Opciones reales:
        re-presionar la hotkey del hechizo (toggle off), right-click para
        deseleccionar, o reintentar Space.
        """
        ctx.actions.quick_press_key("space")

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
            get_telemetry().emit(
                "spell_skip", name=name, spell_id=spell_id, cost=cost, pa=current_pa,
                reason="insufficient_pa",
            )
            return False, current_pa

        t_start = time.time()
        get_telemetry().emit(
            "spell_attempt", name=name, spell_id=spell_id, key=key,
            cost=cost, pa_before=current_pa, target_pos=list(target_pos) if target_pos else None,
        )
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

        get_telemetry().emit(
            "spell_result", name=name, spell_id=spell_id, ok=bool(ok),
            pa_before=current_pa, pa_after=pa_now, dt_s=round(time.time() - t_start, 3),
        )
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
        pj_click: bool = True,
        target_cell_id: int | None = None,
        target_actor_id: str | int | None = None,
    ) -> tuple[bool, int]:
        """Reintenta el mismo hechizo hasta confirmarlo o agotar los intentos.

        self_target=True: re-proyecta la posición desde ctx.my_cell del sniffer en cada intento.
        pj_click=True: el click cae sobre el PJ (combo Sadida es AoE alrededor del PJ).
            Aplica offset Y para apuntar al cuerpo del sprite, no a los pies.
            False para hechizos que apuntan a enemigo (Zarza).
        target_cell_id: cell_id de la celda donde se está clickeando (para auto-learning).
            Si un retry con jitter tiene éxito, persistimos el (dx,dy) ganador para esa celda.
        target_actor_id: id del actor enemigo al que apuntamos. Si se pasa y el spell
            NO es pj_click/self_target, en cada retry se refresca el combat state y se
            re-proyecta la posición desde la celda ACTUAL del actor (por si el enemigo
            se movió entre intentos — evita clickear la celda vieja 6 veces).
        """
        max_attempts = int(ctx.config.get("bot", {}).get("sadida_combo_retry_attempts", 6) or 6)
        # Umbral para disparar random-move desbloqueador: si llevamos N fallos
        # consecutivos en un spell apuntado a enemigo (pj_click=False,
        # self_target=False), asumimos que el sprite del PJ está tapando al
        # mob y nos corremos a una celda aleatoria cercana para liberar el
        # clic. One-shot por llamada: el flag `unblock_triggered` evita
        # entrar en loop de mover-fallar-mover-fallar.
        unblock_threshold = int(
            ctx.config.get("bot", {}).get("combat_sprite_unblock_attempts_threshold", 3) or 3
        )
        unblock_triggered = False
        attempts = 0
        ok = False
        retry_delay = config_delay(ctx.config, "combat_retry_delay", 0.35)
        # Capturado al inicio del retry para detectar confirmaciones que llegan
        # ENTRE intentos (más viejas que el attempt_started_at del nuevo intento
        # pero válidas para esta secuencia de retry).
        t_repeat_started = time.time()
        # Detección de cambio de turno durante el retry: si el sniffer reporta
        # PA > pa_at_start_of_retry, significa que el server reseteó PA (nuevo turno).
        # Sin esto, el bot sigue clickeando el hechizo durante el turno enemigo.
        pa_at_retry_start = current_pa
        turn_at_retry_start = int(getattr(ctx, "turn_number", 0) or 0)
        # Offset Y para clicks de hechizo sobre el PJ: el centro del cell rhombus
        # cae en los pies del sprite; el cuerpo está ~29 px arriba (bajado 1 px
        # desde -30 para que el click quede algo más dentro del rombo).
        spell_click_y_offset = int(
            ctx.config.get("bot", {}).get("combat_self_spell_click_y_offset", -29) or 0
        )

        # ── Jitter de retry para spells (calibración progresiva) ──
        # En vez de clickear el MISMO pixel 6 veces (si la calibración está ~5px off,
        # los 6 intentos fallan idénticos), variamos la posición en cada retry.
        # El primer intento (attempt=0) siempre va al target original; los siguientes
        # samplean offsets en patrón cruz/diagonal.
        spell_jitter_cfg = ctx.config.get("bot", {}).get("combat_spell_retry_jitter")
        if spell_jitter_cfg and isinstance(spell_jitter_cfg, list):
            jitter_offsets = [(0, 0)] + [tuple(o) for o in spell_jitter_cfg]
        else:
            # Default: cruz + diagonales prioritando arriba (sprite tiene cuerpo arriba de pies)
            jitter_offsets = [
                (0, 0),
                (0, -10),
                (0, +10),
                (-15, 0),
                (+15, 0),
                (-10, -15),
                (+10, -15),
                (-10, +10),
                (+10, +10),
            ]

        while current_pa >= cost and not ok and attempts < max_attempts:
            # Antes de retry: chequear si una confirmación llegó tarde para nuestro spell_id
            # (caso típico: cast 1 timeout pasado, GA llegó 50ms después; no relancemos).
            if attempts > 0 and self._already_confirmed(ctx, spell_id, t_repeat_started):
                print(f"[SADIDA] {name} confirmado tardamente por sniffer (entre intentos). Continuando combo.")
                ok = True
                # Asumir PA gastada ya que el server confirmó el cast
                current_pa = max(0, current_pa - cost)
                break

            # Re-tracking de target enemigo: si el spell apunta a un actor y tenemos
            # su id, refrescar el sniffer y re-proyectar desde su celda ACTUAL antes
            # de cada intento. Sin esto, si el enemigo se mueve durante los retries
            # los 6 clicks caen en la celda vieja y el spell falla en cascada.
            if (
                not self_target
                and not pj_click
                and target_actor_id is not None
                and getattr(ctx, "refresh_combat_state", None)
                and getattr(ctx, "project_self_cell", None)
            ):
                try:
                    ctx.refresh_combat_state(0.05)
                    fresh_enemy = None
                    for e in (ctx.enemies or []):
                        if str(e.get("id")) == str(target_actor_id):
                            fresh_enemy = e
                            break
                    if fresh_enemy is not None:
                        fresh_cell = fresh_enemy.get("cell_id")
                        if fresh_cell is not None and fresh_cell != target_cell_id:
                            new_pos = ctx.project_self_cell(int(fresh_cell))
                            if new_pos:
                                print(f"[SADIDA] {name}: target actor={target_actor_id} "
                                      f"se movió cell={target_cell_id}->{fresh_cell}. "
                                      f"Re-proyectando pos {target_pos}->{new_pos}.")
                                target_pos = new_pos
                                target_cell_id = int(fresh_cell)
                except Exception as _e:
                    pass

            # Re-proyectar posición desde celda actual del sniffer en cada intento.
            # self_target=True (spell auto-apuntado) — siempre re-proyecta.
            # pj_click=True (AoE centrada en el PJ, ej. Combo 1) — también re-proyecta:
            #   `target_pos` puede venir de `_resolve_action_position` con red-ring
            #   matcheando el retrato del PJ en la barra superior (y≈165) o con un
            #   `_last_refined_self_pos` stale de turno anterior. La celda del sniffer
            #   es la fuente de verdad para cualquier click sobre el caster; el jitter
            #   aprendido + y_offset ya cubren el ajuste al cuerpo del sprite.
            pos = target_pos
            did_reproject = False
            # LEY ABSOLUTA (rule_manual_pixel_positions_law.md): si el usuario
            # calibró un pixel manual para (map, cell) y el spell es self-target
            # o centrado en el PJ, se usa ESE pixel directo SIN y-offset, SIN
            # jitter, SIN re-proyección iso. Garantiza 100% success rate en las
            # celdas de los 3 mapas farmeables.
            manual_self_pixel = None
            if (self_target or pj_click) and ctx.my_cell is not None:
                getter = getattr(ctx, "manual_pixel_for_cell", None)
                if getter is not None:
                    try:
                        manual_self_pixel = getter(ctx.my_cell)
                    except Exception:
                        manual_self_pixel = None
            if manual_self_pixel is not None:
                if pos != manual_self_pixel:
                    print(f"[SADIDA] {name}: MANUAL_PIXEL self-cast cell={ctx.my_cell} "
                          f"({pos} -> {manual_self_pixel}) [pj_click={pj_click} self_target={self_target}]")
                pos = manual_self_pixel
                did_reproject = True
            elif (self_target or pj_click) and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
                fresh_pos = ctx.project_self_cell(ctx.my_cell)
                if fresh_pos:
                    if pos != fresh_pos:
                        print(f"[SADIDA] {name}: re-proyectando desde cell={ctx.my_cell} "
                              f"({pos} -> {fresh_pos}) [pj_click={pj_click} self_target={self_target}]")
                    pos = fresh_pos
                    did_reproject = True

            # Aplicar offset Y para apuntar al cuerpo del sprite (no a los pies).
            # Solo cuando el click cae sobre el PJ (combo AoE); para Zarza apuntando a
            # enemigo no se aplica. NO se aplica si estamos usando el pixel manual —
            # el manual ya está calibrado a mano y es LEY.
            if pj_click and pos and spell_click_y_offset and manual_self_pixel is None:
                pos = (int(pos[0]), int(pos[1]) + spell_click_y_offset)

            # Aplicar jitter de retry:
            # - attempt 0: si hay jitter APRENDIDO para (cell_id) y es pj_click, usarlo
            #   como offset inicial (evita perder el primer intento cuando ya sabemos
            #   que la calibración tiene un sesgo sistemático en ese cell).
            # - attempts > 0: sampleo por patrón cruz/diagonal.
            # EXCEPCION LEY: si estamos usando pixel manual para self-cast, NO se
            # aplica ningún jitter (ni aprendido, ni de retry). Se reintenta siempre
            # sobre el mismo pixel calibrado a mano.
            jitter_dx, jitter_dy = (0, 0)
            if manual_self_pixel is not None:
                if attempts > 0:
                    print(f"[SADIDA] {name}: retry #{attempts} MANUAL_PIXEL cell={ctx.my_cell} -> pos={pos} (sin jitter, LEY)")
            elif attempts == 0 and pj_click and pos and target_cell_id is not None:
                getter = getattr(ctx, "get_spell_jitter_offset", None)
                if getter is not None:
                    try:
                        learned = getter(int(target_cell_id))
                        if learned and (learned[0] or learned[1]):
                            jitter_dx, jitter_dy = int(learned[0]), int(learned[1])
                            pos = (int(pos[0]) + jitter_dx, int(pos[1]) + jitter_dy)
                            print(f"[SADIDA] {name}: jitter APRENDIDO ({jitter_dx:+d},{jitter_dy:+d}) para cell={target_cell_id} -> pos={pos}")
                    except Exception:
                        pass
            elif attempts > 0 and pos and jitter_offsets:
                idx = attempts % len(jitter_offsets)
                jitter_dx, jitter_dy = jitter_offsets[idx]
                if jitter_dx or jitter_dy:
                    pos = (int(pos[0]) + jitter_dx, int(pos[1]) + jitter_dy)
                    print(f"[SADIDA] {name}: retry #{attempts} con jitter ({jitter_dx:+d},{jitter_dy:+d}) -> pos={pos}")

            # Clamp del click a la región del juego.
            # Histórico: se diseñó para evitar pegarle al HUD superior cuando
            # `target_pos` venía contaminado por `_resolve_action_position`
            # detectando el RETRATO del PJ en la barra de turnos (y≈165) — pero
            # ese bug se resolvió en re-proyección desde `ctx.my_cell`. Ahora
            # con `did_reproject=True`, la y viene de la celda autoritativa del
            # sniffer; clamparla rompe los cells legítimos de filas superiores
            # del iso (cell 34 a y=100 → click en y=138 cae al cell del sur,
            # spell falla 6 veces seguidas).
            #
            # Solo aplica el clamp como red de seguridad cuando NO re-proyectamos
            # (target_pos es la única fuente — puede estar contaminado).
            if pj_click and pos and not did_reproject:
                try:
                    game = ctx.screen.game_region()
                    safe_top = int(game["top"]) + 60
                    if pos[1] < safe_top:
                        old_y = pos[1]
                        pos = (int(pos[0]), safe_top)
                        print(f"[SADIDA] {name}: click Y={old_y} fuera de iso (top HUD, sin reproyeccion). Clamp a Y={safe_top}.")
                except Exception:
                    pass
            # Adicional: aún con re-proyección, clampar al borde MUY superior
            # del game_region (margen 5px) para no clickear afuera de la ventana
            # del juego en cells extremos. Inofensivo si pos[1] >= game_top+5.
            if pj_click and pos and did_reproject:
                try:
                    game = ctx.screen.game_region()
                    hard_top = int(game["top"]) + 5
                    if pos[1] < hard_top:
                        old_y = pos[1]
                        pos = (int(pos[0]), hard_top)
                        print(f"[SADIDA] {name}: click Y={old_y} fuera de game_region. Clamp a Y={hard_top}.")
                except Exception:
                    pass

            # Capturar celda ANTES del cast para detectar cell-jump post-click.
            # Si tras el click el PJ se movió a otra celda Y no hay confirmación
            # del hechizo, es señal clara de que la hotkey no armó el spell y
            # el click se interpretó como movimiento. Reintentar es contraproducente:
            # los próximos clicks pegarán en celda incorrecta y seguiremos
            # gastando PM hasta perder el turno.
            cell_before_click = ctx.my_cell

            print(f"[SADIDA] {name}: click final pos={pos} (attempt #{attempts}, self_target={self_target}, pj_click={pj_click})")
            ok, current_pa = self._cast_with_pa_gate(ctx, name, key, pos, spell_id, cost, current_pa)
            attempts += 1

            # Cell-jump check (solo para pj_click — los hechizos AoE-self esperan
            # PJ quieto durante el cast; movimiento = click no llegó como hechizo).
            if not ok and pj_click and cell_before_click is not None:
                cell_after_click = ctx.my_cell
                if cell_after_click is not None and cell_after_click != cell_before_click:
                    print(f"[SADIDA] {name}: CELL-JUMP detectado tras click "
                          f"(cell {cell_before_click}->{cell_after_click}) sin confirmación de hechizo. "
                          f"La hotkey no armó el spell — abortando retries.")
                    get_telemetry().emit(
                        "spell_cell_jump", name=name, spell_id=spell_id,
                        cell_before=cell_before_click, cell_after=cell_after_click,
                        attempt=attempts,
                    )
                    break

            # Auto-learning: si un cast (con cualquier jitter — incluido el aprendido
            # de attempt 0) tuvo éxito y es pj_click, persistimos el offset ganador
            # en `spell_jitter_offsets_by_map_id` (dict SEPARADO del de movimiento
            # para no contaminar _movement_click_pos_for_cell ni self_pos).
            # No aprendemos para spells con pj_click=False (Zarza) porque su jitter
            # se refiere al sprite del ENEMIGO (distinto cell), no al PJ.
            if ok and pj_click and (jitter_dx or jitter_dy) and target_cell_id is not None:
                cb = getattr(ctx, "record_learned_offset", None)
                if cb is not None:
                    try:
                        cb(int(target_cell_id), int(jitter_dx), int(jitter_dy))
                        print(f"[SADIDA] {name}: APRENDIDO spell-jitter ({jitter_dx:+d},{jitter_dy:+d}) para cell={target_cell_id} (intento #{attempts}).")
                    except Exception as e:
                        print(f"[SADIDA] {name}: record_learned_offset falló: {e}")

            # Drenar sniffer y chequear fin de combate
            state = ctx.refresh_combat_state(0.0)
            if state.get("fight_ended"):
                return True, 0

            if not ok:
                # Doble check post-cast: el GA puede haber llegado en este mismo refresh
                if self._already_confirmed(ctx, spell_id, t_repeat_started):
                    print(f"[SADIDA] {name} confirmado por sniffer tras attempt {attempts}. Combo continúa.")
                    ok = True
                    current_pa = max(0, current_pa - cost)
                    break

                print(f"[SADIDA] {name} fallo. Reintentando mismo hechizo ({attempts}/{max_attempts}).")

                # Fallback desbloqueador: si llevamos N fallos consecutivos en un
                # spell apuntado a enemigo (no pj_click, no self_target), el
                # sprite del PJ probablemente está tapando al mob. Movemos el PJ
                # a una celda aleatoria cercana y re-proyectamos la celda del
                # enemigo desde la nueva posición del PJ.
                if (
                    not ok
                    and not unblock_triggered
                    and not self_target
                    and not pj_click
                    and attempts >= unblock_threshold
                    and getattr(ctx, "move_random_reachable", None) is not None
                ):
                    mp_now = ctx.current_mp if ctx.current_mp is not None else 0
                    if mp_now > 0:
                        print(
                            f"[SADIDA] {name}: {attempts} fallos consecutivos — "
                            f"posible sprite tapando al mob. Disparando random-move "
                            f"(MP disponible={mp_now})."
                        )
                        unblock_triggered = True
                        try:
                            move_res = ctx.move_random_reachable(
                                mp_now, reason=f"desbloquear {name}"
                            ) or {}
                        except Exception as _mv_exc:
                            print(f"[SADIDA] {name}: move_random_reachable falló: {_mv_exc!r}")
                            move_res = {}
                        if move_res.get("fight_ended"):
                            return True, 0
                        # Re-sincronizar contexto tras el movimiento.
                        post_state = ctx.refresh_combat_state(0.1)
                        if post_state.get("fight_ended"):
                            return True, 0
                        # Re-proyectar target_pos desde la celda ACTUAL del
                        # enemigo apuntado (el enemigo no se movió, pero el
                        # PJ sí — el target_pos absoluto no cambia, pero el
                        # bloque de re-tracking al inicio del próximo loop
                        # lo recalculará igual). Cuando se desbloqueó sprite
                        # reseteamos el contador de jitter volviendo a
                        # attempts=0 visualmente: no reseteamos `attempts`
                        # para no exceder max_attempts totales, pero sí
                        # forzamos jitter base (0,0) en el próximo intento.
                        if move_res.get("moved"):
                            print(
                                f"[SADIDA] {name}: PJ ahora en cell={post_state.get('combat_cell')}. "
                                f"PA={post_state.get('current_pa')} PM={post_state.get('current_mp')}."
                            )
                        # continuar al resto del bloque normal (retry_delay)
                # Espera con drain del sniffer (en vez de sleep ciego)
                state = ctx.refresh_combat_state(retry_delay)
                if state.get("fight_ended"):
                    return True, 0

                # Re-check confirmación tras la espera (GA pudo llegar en estos 350ms)
                if self._already_confirmed(ctx, spell_id, t_repeat_started):
                    print(f"[SADIDA] {name} confirmado por sniffer durante espera. Combo continúa.")
                    ok = True
                    current_pa = max(0, current_pa - cost)
                    break

                # Verificar PA real del sniffer tras la espera (GTM puede haber llegado)
                sniffer_pa = state.get("current_pa")
                if sniffer_pa is not None and sniffer_pa < cost:
                    print(f"[SADIDA] {name}: Sniffer confirma PA insuficiente tras espera ({sniffer_pa}<{cost}). Deteniendo reintentos.")
                    current_pa = sniffer_pa
                    break

                # ── Detección de cambio de turno (CRÍTICO) ──
                # Si el sniffer reporta PA > pa_at_retry_start, el server reseteó PA = nuevo turno.
                # Seguir clickeando aquí significa hechizar durante el turno enemigo (no pasa nada
                # útil y se pierden 6 retries × 350ms mientras nos atacan).
                if sniffer_pa is not None and sniffer_pa > pa_at_retry_start:
                    print(f"[SADIDA] {name}: PA subió ({pa_at_retry_start}->{sniffer_pa}) — turno nuevo detectado. Abortando retries.")
                    current_pa = sniffer_pa
                    break
                # Doble seguro: si el bot global incrementó turn_number durante el retry, abortar.
                turn_now = int(getattr(ctx, "turn_number", 0) or 0)
                if turn_now > turn_at_retry_start:
                    print(f"[SADIDA] {name}: turn_number {turn_at_retry_start}->{turn_now} durante retry. Abortando.")
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

        # EXTENDED WAIT: si current_pa == 0 y turn_number > 1, el valor es
        # ambiguo. Puede ser:
        #   (a) forma árbol real (PA=0 al inicio del turno, correcto pasar).
        #   (b) GTM de regen en vuelo (el server envía PA=8 DESPUÉS del GTS).
        # Observado en Dofus 1.29: el server a veces envía la PA-regen con
        # 1-3s de retraso post-GTS. Sin este wait extendido, pasamos el turno
        # con PA "0" mientras el PA=8 real llega un instante después.
        #
        # Tolerancia: si estamos realmente en forma árbol, pagamos un timeout
        # de ~2s extra cada ~11 turnos (hasta que el PS-cd expire). Si NO
        # estamos en forma árbol, rescatamos el turno entero (enorme ganancia).
        if ctx.current_pa == 0 and ctx.turn_number > 1:
            extended_start = time.time()
            extended_deadline = extended_start + 2.0
            prev_pa = ctx.current_pa
            rescued = False
            while time.time() < extended_deadline:
                last_state = ctx.refresh_combat_state(0.1)
                if last_state.get("fight_ended"):
                    return "combat_ended"
                # El wrapper auto_update_refresh ya actualizó ctx.current_pa
                # si llegó un GTM con campo `current_pa` > 0. Pero como el
                # wrapper sólo escribe cuando res["current_pa"] is not None,
                # un valor 0 no pisa uno previo. Por eso chequeamos explícito.
                fresh = last_state.get("current_pa")
                if fresh is not None and int(fresh) > 0:
                    ctx.current_pa = int(fresh)
                    print(
                        f"[SADIDA] PA rescatado por GTM tardío: {prev_pa}->{ctx.current_pa} "
                        f"(waited {time.time() - extended_start:.2f}s post-GTS)."
                    )
                    rescued = True
                    break
            if not rescued:
                # Timeout: PA realmente es 0 (forma árbol u otro caso).
                _ps_cd_here = max(0, 11 - (ctx.turn_number - self._ps_cast_at_turn))
                print(
                    f"[SADIDA] Wait extendido agotado (2s) — PA=0 confirmado. "
                    f"Probable forma árbol (PS cd local={_ps_cd_here})."
                )

        # Reset de estado entre peleas: DEBE hacerse ANTES de calcular _local_ps_cd
        # para evitar que valores de la pelea anterior contaminen la lógica actual.
        if ctx.turn_number == 1:
            self._last_combo_turn = -999
            self._h1_pending = False
            self._h2_pending = False
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
        get_telemetry().emit(
            "sadida_state", pa=current_pa, mp=current_mp, my_cell=ctx.my_cell,
            sniffer_pa=ctx.current_pa, sniffer_mp=ctx.current_mp,
            h1_pending=self._h1_pending, h2_pending=self._h2_pending, h3_pending=self._h3_pending,
            ps_cd_local=_local_ps_cd, enemies=len(ctx.enemies or []),
        )

        # PA=0 → forma árbol u otro estado sin acciones. Pasar turno inmediatamente.
        if current_pa == 0:
            print("[SADIDA] PA=0 — pasando turno.")
            self._pass_turn(ctx)
            return "done"
        self._zarza_casts_this_turn = 0

        self_pos = action_pos
        if not self_pos and ctx.my_cell is not None and getattr(ctx, "project_self_cell", None):
            self_pos = ctx.project_self_cell(ctx.my_cell)

        # --- Cap de pendientes: si H1 falló 2 turnos seguidos, no insistas ---
        # 12 retries gastados en una celda fallida ya demuestran que el problema
        # es estructural (calibración off, hechizo no se arma, HUD bloqueando).
        # Abandonar el estado pendiente y dejar que el flujo normal de turno
        # corra (Combo 1 fresh si los CDs lo permiten, o Combo 2).
        _max_pendiente_fails = int(ctx.config.get("bot", {}).get("sadida_max_pendiente_fails", 2) or 2)
        if self._h1_consecutive_fails >= _max_pendiente_fails:
            print(f"[SADIDA] H1 pendiente falló {self._h1_consecutive_fails} turno(s) seguidos "
                  f"(cap={_max_pendiente_fails}). Abandonando pendientes y reseteando estado.")
            get_telemetry().emit(
                "pendiente_cap_reached", spell="H1", fails=self._h1_consecutive_fails,
                cap=_max_pendiente_fails, last_fail_cell=self._h1_last_fail_cell,
            )
            self._h1_pending = False
            self._h2_pending = False
            self._h3_pending = False
            self._h1_consecutive_fails = 0
            self._h1_last_fail_cell = None

        # --- Combo 1: Lógica de continuación de combo pendiente ---
        if self._h1_pending:
            print(f"[SADIDA] H1 (Temblor) pendiente del turno anterior (PA={current_pa}, fails_consec={self._h1_consecutive_fails}).")
            # Si H1 ya falló >=1 vez en la celda actual y tenemos PM, FORZAR
            # reubicación a otra celda antes de reintentar (la celda actual
            # tiene algún problema: HUD/sprite/UI bloqueando el click).
            if (self._h1_consecutive_fails >= 1
                    and current_mp > 0
                    and ctx.my_cell is not None
                    and self._h1_last_fail_cell == ctx.my_cell
                    and getattr(ctx, "move_towards_enemy", None)):
                print(f"[SADIDA] H1 falló en cell={ctx.my_cell}. Forzando reubicación táctica antes de reintentar.")
                move_res = ctx.move_towards_enemy(current_mp, desired_range=0, bypass_rat_mode=True, force_relocate=True)
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
            if self_pos and current_pa >= 2:
                h1_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H1 Temblor (Pendiente)", "1", self_pos, _SPELL_TEMBLOR, 2, current_pa,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
                if h1_ok:
                    self._h1_pending = False
                    self._h2_pending = True
                    self._h1_consecutive_fails = 0
                    self._h1_last_fail_cell = None
                else:
                    self._h1_consecutive_fails += 1
                    self._h1_last_fail_cell = ctx.my_cell
                    print(f"[SADIDA] H1 (Pendiente) no pudo confirmarse (fails_consec={self._h1_consecutive_fails}). Pasando turno.")
                    self._pass_turn(ctx)
                    return "done"
            else:
                self._pass_turn(ctx)
                return "done"

        if self._h2_pending:
            print(f"[SADIDA] H2 (Viento) pendiente (PA={current_pa}).")
            if self_pos and current_pa >= 3:
                h2_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H2 Viento Envenenado (Pendiente)", "2", self_pos, _SPELL_VIENTO_ENVENENADO, 3, current_pa,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"): return "combat_ended"
                if h2_ok:
                    self._h2_pending = False
                    self._h3_pending = True
                else:
                    print("[SADIDA] H2 (Pendiente) no pudo confirmarse. Pasando turno.")
                    self._pass_turn(ctx)
                    return "done"
            else:
                self._pass_turn(ctx)
                return "done"

        if self._h3_pending:
            print(f"[SADIDA] H3 (Potencia) pendiente (PA={current_pa}).")
            if self_pos and current_pa >= 2:
                h3_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H3 Potencia Silvestre (Pendiente)", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 2, current_pa,
                    self_target=False,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if h3_ok:
                    # PS exitoso = forma arbol (0 PA, 0 PM). No hay mas acciones
                    # posibles este turno: pasar turno inmediatamente. Si caiamos
                    # a combo 1, con _local_ps_cd stale el bot reintentaba H1/H2
                    # con PA falso y perdia segundos.
                    self._h3_pending = False
                    self._ps_cast_at_turn = ctx.turn_number
                    print("[SADIDA] H3 (Pendiente) confirmada. Forma arbol - pasando turno.")
                    self._pass_turn(ctx)
                    return "done"
                else:
                    print("[SADIDA] H3 (Pendiente) no pudo confirmarse. Pasando turno.")
                    self._pass_turn(ctx)
                    return "done"
            else:
                self._pass_turn(ctx)
                return "done"

        cooldown_181 = ctx.spell_cooldowns.get(_SPELL_TEMBLOR, 0)
        cooldown_196 = ctx.spell_cooldowns.get(_SPELL_VIENTO_ENVENENADO, 0)
        cooldown_197 = max(ctx.spell_cooldowns.get(_SPELL_POTENCIA_SILVESTRE, 0), _local_ps_cd)

        # Combo 1: prioridad irrevocable. Se lanza siempre que al menos un hechizo
        # del combo no esté en cooldown, sin importar PA disponible ni placaje.
        # Si el PA no alcanza para el combo completo, se lanza lo que se pueda
        # y el resto queda pendiente para el turno siguiente.
        combo_1_available = (
            (cooldown_181 == 0 or cooldown_196 == 0 or cooldown_197 == 0)
            and self_pos is not None
        )

        if combo_1_available:
            is_tackled = bool(getattr(ctx, "enemy_in_melee_range", None) and ctx.enemy_in_melee_range())
            print(f"[SADIDA] Combo 1 — PA={current_pa} PM={current_mp} placado={is_tackled} "
                  f"(cds: T={cooldown_181} VE={cooldown_196} PS={cooldown_197})")
            get_telemetry().emit(
                "combo_branch", branch="combo1", pa=current_pa, mp=current_mp,
                tackled=is_tackled, cd_temblor=cooldown_181, cd_viento=cooldown_196, cd_ps=cooldown_197,
            )

            # Skip movimiento si TODOS los enemigos ya están en rango AoE (radio 9)
            # desde la celda actual. No tiene sentido gastar PM si el combo ya cubre
            # a todos desde donde estoy. El placaje no aplica si no me muevo.
            skip_movement = False
            combo_aoe_radius = int(ctx.config.get("bot", {}).get("sadida_combo_aoe_radius", 9) or 9)
            if (ctx.my_cell is not None
                    and ctx.enemies
                    and getattr(ctx, "cell_distance", None)):
                enemy_dists = []
                for e in ctx.enemies:
                    ec = e.get("cell_id")
                    if ec is None:
                        continue
                    d = ctx.cell_distance(ctx.my_cell, ec)
                    if d is not None:
                        enemy_dists.append(d)
                if enemy_dists:
                    in_range = sum(1 for d in enemy_dists if d <= combo_aoe_radius)
                    if in_range == len(enemy_dists):
                        skip_movement = True
                        print(f"[SADIDA] Combo 1: TODOS los enemigos ({in_range}) ya en rango AoE "
                              f"<={combo_aoe_radius} desde cell={ctx.my_cell}. Skip movimiento.")

            # Reposicionar siempre que haya PM disponible. _choose_combat_approach_cell ya
            # descuenta el costo de placaje internamente (effective_mp = mp - tackle_cost) y
            # movement_score evita destinos placados via is_tackled_dest como llave primaria.
            if not skip_movement and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
                print(f"[SADIDA] Optimizando posicion para Combo 1 (placado={is_tackled} PM={current_mp})...")
                move_res = ctx.move_towards_enemy(current_mp, desired_range=0, bypass_rat_mode=True)
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

            # H1: Temblor (2 PA) — solo si no está en cooldown
            if cooldown_181 == 0 and current_pa >= 2 and self_pos:
                h1_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H1 Temblor", "1", self_pos, _SPELL_TEMBLOR, 2, current_pa,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if not h1_ok:
                    self._h1_pending = True
                    self._h1_consecutive_fails += 1
                    self._h1_last_fail_cell = ctx.my_cell
                    print(f"[SADIDA] H1 no pudo confirmarse (fails_consec={self._h1_consecutive_fails}). Marcando pendiente.")
                    self._pass_turn(ctx)
                    return "done"
                else:
                    # H1 confirmado: resetear contador de fallos
                    self._h1_consecutive_fails = 0
                    self._h1_last_fail_cell = None
            elif cooldown_181 == 0 and current_pa < 2:
                print(f"[SADIDA] H1 disponible pero PA insuficiente ({current_pa}/2). Queda pendiente.")
                self._h1_pending = True
                self._pass_turn(ctx)
                return "done"

            # H2: Viento Envenenado (3 PA) — solo si no está en cooldown
            if cooldown_196 == 0 and current_pa >= 3 and self_pos:
                h2_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H2 Viento Envenenado", "2", self_pos, _SPELL_VIENTO_ENVENENADO, 3, current_pa,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if not h2_ok:
                    self._h2_pending = True
                    print("[SADIDA] H2 no pudo confirmarse. Marcando pendiente.")
                    self._pass_turn(ctx)
                    return "done"
            elif cooldown_196 == 0 and current_pa < 3:
                print(f"[SADIDA] H2 disponible pero PA insuficiente ({current_pa}/3). Queda pendiente.")
                self._h2_pending = True
                self._pass_turn(ctx)
                return "done"

            # H3: Potencia Silvestre (2 PA) — solo si no está en cooldown
            if cooldown_197 == 0 and current_pa >= 2 and self_pos:
                h3_ok, current_pa = self._repeat_same_spell_until_success(
                    ctx, "H3 Potencia Silvestre", "3", self_pos, _SPELL_POTENCIA_SILVESTRE, 2, current_pa,
                    self_target=False,
                    target_cell_id=ctx.my_cell,
                )
                if ctx.refresh_combat_state(0.0).get("fight_ended"):
                    return "combat_ended"
                if h3_ok:
                    self._ps_cast_at_turn = ctx.turn_number
                else:
                    self._h3_pending = True
                    print("[SADIDA] H3 no pudo confirmarse. Marcando pendiente.")
            elif cooldown_197 == 0 and current_pa < 2:
                print(f"[SADIDA] H3 disponible pero PA insuficiente ({current_pa}/2). Queda pendiente.")
                self._h3_pending = True

            # === Chain Combo 1 → Combo 2 ===
            # Spec del usuario: "Se lanzaron todas las habilidades de combo 1
            # satisfactoriamente y se cumple el cooldown de hechizos de combo 1
            # = seguir con combo 2".
            #
            # Refrescamos cooldowns y PA del sniffer. Si los 3 hechizos de Combo
            # 1 están ahora en CD (= los lanzamos OK este turno o ya estaban
            # consumidos en turnos previos) Y queda PA, encadenamos a Combo 2
            # en el mismo turno.
            #
            # Nota: si PS (H3) se confirmó este turno, forma árbol deja PA=0,
            # por lo que current_pa>0 bloquea naturalmente el chain en ese caso.
            # Casos típicos donde dispara: H1 en CD previo + H2/H3 lanzados
            # con PA inicial alto, o cualquier combo parcial que deja sobrante.
            fresh_chain = ctx.refresh_combat_state(0.1)
            if fresh_chain.get("fight_ended"):
                return "combat_ended"
            if not ctx.enemies:
                print("[SADIDA] Sin enemigos vivos tras Combo 1. Pasando turno.")
                self._pass_turn(ctx)
                return "done"
            ctx.spell_cooldowns = dict(fresh_chain.get("spell_cooldowns") or ctx.spell_cooldowns)
            _fresh_pa_chain = fresh_chain.get("current_pa")
            if _fresh_pa_chain is not None:
                current_pa = int(_fresh_pa_chain)
            _fresh_mp_chain = fresh_chain.get("current_mp")
            if _fresh_mp_chain is not None:
                current_mp = int(_fresh_mp_chain)
            cd1_after = ctx.spell_cooldowns.get(_SPELL_TEMBLOR, 0)
            cd2_after = ctx.spell_cooldowns.get(_SPELL_VIENTO_ENVENENADO, 0)
            cd3_after = max(ctx.spell_cooldowns.get(_SPELL_POTENCIA_SILVESTRE, 0), _local_ps_cd)

            chained_into_combo2 = (
                cd1_after > 0 and cd2_after > 0 and cd3_after > 0 and current_pa > 0
            )

            if not chained_into_combo2:
                self._pass_turn(ctx)
                return "done"

            print(f"[SADIDA] Combo 1 completo + todos en CD (T={cd1_after} VE={cd2_after} "
                  f"PS={cd3_after}). Encadenando Combo 2 con PA={current_pa} MP={current_mp}.")
            get_telemetry().emit(
                "combo_branch", branch="combo1->combo2_chain",
                pa=current_pa, mp=current_mp,
                cd_temblor=cd1_after, cd_viento=cd2_after, cd_ps=cd3_after,
            )
            # Fall through al bloque Combo 2 de abajo (NO pass_turn aquí).
        else:
            chained_into_combo2 = False

        # Combo 2: cuando TODOS los hechizos de Combo 1 están en cooldown,
        # ya sea por entrada directa (Combo 1 no disponible este turno) o por
        # chain desde Combo 1 (recién consumido).
        if not chained_into_combo2:
            print(f"[SADIDA] Combo 1 en cooldown (T={cooldown_181} VE={cooldown_196} PS={cooldown_197}). Ejecutando Combo 2 (PA={current_pa})")
            get_telemetry().emit(
                "combo_branch", branch="combo2", pa=current_pa, mp=current_mp,
                cd_temblor=cooldown_181, cd_viento=cooldown_196, cd_ps=cooldown_197,
            )

        if ctx.my_cell is None or not ctx.enemies:
            self._pass_turn(ctx)
            return "done"

        # Comportamiento base: estatico. Solo mover si no hay LoS para atacar.
        has_targets_in_los = any(
            ctx.has_line_of_sight(ctx.my_cell, e["cell_id"]) for e in ctx.enemies
            if ctx.cell_distance(ctx.my_cell, e["cell_id"]) <= 8
        ) if getattr(ctx, "has_line_of_sight", None) and getattr(ctx, "cell_distance", None) else True

        if not has_targets_in_los and current_mp > 0 and getattr(ctx, "move_towards_enemy", None):
            # El costo de placaje se descuenta internamente; mover solo si hay celda alcanzable.
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
            self._pass_turn(ctx)
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

            ok_zarza, current_pa = self._repeat_same_spell_until_success(
                ctx, f"H5 Zarza ({self._zarza_casts_this_turn + 1}/2)", "5", enemy_pos, _SPELL_ZARZA, 4, current_pa,
                pj_click=False,
                target_cell_id=target.get("cell_id"),
                target_actor_id=target.get("id"),
            )
            if ctx.refresh_combat_state(0.0).get("fight_ended"):
                return "combat_ended"
            if ok_zarza:
                self._zarza_casts_this_turn += 1
            else:
                print("[SADIDA] H5 Zarza falló tras reintentos. Deteniendo ataques.")
                break

        self._pass_turn(ctx)
        return "done"
