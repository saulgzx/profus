# Worklog — Auditoria de combate + GUI Pro Max

Sesion: 2026-04-16
Branch: main

Este archivo se actualiza despues de cada hito para que, si se agota el contexto,
una sesion nueva pueda retomar leyendo solo este archivo.

---

## Hecho — todo verificado con `python -c "import ast; ast.parse(...)"` + smoke test instanciando `App()`

### Quick Wins de combate

- **Q1** — `src/bot.py:3455-3478` — `on_turn` envuelto en `try/except`. Si el perfil lanza excepcion, se loguea el traceback completo, se presiona Space (pasar turno), se refresca `combat_deadline` y el bot continua. Antes: bot quedaba congelado hasta timeout.
- **Q3** — Hook `on_fight_end()` agregado al perfil base.
  - `src/combat/base.py:96-98` — declaracion del hook (no-op por defecto).
  - `src/combat/sadida.py:27-37` — implementacion: resetea `_h1/_h2/_h3_pending`, `_last_combo_turn`, `_zarza_casts_this_turn`, `_ps_cast_at_turn`. Loguea aviso si habia combo pendiente.
  - `src/bot.py:710-716` — handler `fight_end` invoca `combat_profile.on_fight_end()` con try/except.
  - **Resuelve**: viola la regla "Combo 1 siempre prioritario" cuando un combate terminaba a mitad de combo y el siguiente arrancaba en H2.
- **Q4** — `src/bot.py:621-632` — Heartbeat de `combat_deadline` en `_handle_sniff_event`. Cualquier evento de combate vivo (turn_start/end, fighter_stats, combatant_cell, pa_update, action_sequence_ready, game_action, spell_cooldown, arena_state, placement, placement_cells) refresca el deadline a `time.time() + COMBAT_TIMEOUT`. **Resuelve**: si el GTS se perdia, el deadline corria desde el ultimo turn_start y el bot escapaba de una pelea viva.
- **Q7** — `src/sniffer.py:639-651` — Parser GTS robusto. Corta el body por `.|;,` (todos los delimitadores observados), valida que el actor_id sea entero con signo. Si no parsea, loguea `[SNIFFER][DIAG] gts_unparsable raw=...` y descarta. Antes: en formatos raros podia capturar el body completo y nunca matchear el actor.

### GUI Pro Max — tokens y helpers

- **Tokens de diseno** (`src/gui.py:59-130`):
  - Surface levels: `BG_BASE`, `BG_SUBTLE`, `BG_ELEVATED`, `BG_OVERLAY`, `BG_HIGHLIGHT`
  - Brand: `BRAND`, `BRAND_HOVER`, `BRAND_ACTIVE`, `BRAND_SOFT`
  - Texto jerarquico: `TEXT_PRIMARY`, `TEXT_SECONDARY`, `TEXT_TERTIARY`, `TEXT_DISABLED`
  - Bordes: `BORDER_SUBTLE`, `BORDER_DEFAULT`, `BORDER_STRONG`
  - Semantica: `SUCCESS`, `WARNING`, `DANGER`, `INFO`
  - Tipografia: `FONT_DISPLAY/HEADING/TITLE/BODY/BODY_BOLD/CAPTION/LABEL/BUTTON/MONO/MONO_SMALL`
  - Espaciado: `SP_1..SP_8` (multiplos de 4)
  - **Aliases legacy** mantenidos: `BG/PANEL/CARD/HOVER/ACCENT/BLUE/GREEN/RED/YELLOW/TEXT/SUBTEXT/DIM/BORDER` apuntan a los nuevos. Los 6000+ usos en el resto del archivo no se rompen.
- **Helpers nuevos** (todos en `src/gui.py`):
  - `_make_pill_button(parent, text, command, variant, icon, size, state)` — boton tipo pildora con hover binding. Variants: primary/secondary/ghost/danger/success/warning. Sizes: sm/md/lg.
  - `_set_pill_state(btn, enabled, variant)` — habilita/deshabilita preservando hover.
  - `_make_status_pill(parent)` — pildora dot+texto+meta para indicador de estado.
  - `_make_card(parent, padding)` — frame elevado con borde sutil.
  - `_section_title(parent, text, subtitle)` — titulo de seccion uppercase + subtitulo opcional.
  - `_init_ttk_theme()` — configura Pro.TNotebook, Pro.Treeview, Pro.Vertical.TScrollbar, Pro.TEntry/TCombobox/TSpinbox, Pro.TButton + aliases legacy.
  - `_collapsible_section()` reescrito: chevron `▾`/`▸`, hover label, hairline separator subtil.
  - `_sep()` reescrito: linea de 1px BORDER_SUBTLE en lugar de ttk.Separator.

### GUI Pro Max — header rediseñado

- `_build_ui` linea ~1065. Estructura nueva:
  - **Logo box**: cuadrado 28x28 BRAND + glifo "D" blanco
  - **Wordmark**: "Dofus Autofarm" (FONT_DISPLAY) + subtitulo "Retro 1.29.1 · sniffer + visual" (FONT_CAPTION TEXT_TERTIARY)
  - **Status pill**: `● Detenido` en BG_ELEVATED con borde, dot color-coded por estado
  - **Info label**: junto al pill, muestra "Sadida · map:..."
  - **Hint hotkeys**: "F12 toggle · F8 pausa · F10 detener" en TEXT_TERTIARY
  - **Botones pildora**: TEST (ghost) | INICIAR (primary, BRAND) | PAUSAR (secondary, deshabilitado al inicio)
  - Hairline `BORDER_DEFAULT` 1px en el bottom del header
- **Notebook**: aplica `Pro.TNotebook` — tabs grandes (font 10pt bold, padding generoso), background BG_BASE, indicador inferior implicito por color del foreground en estado selected.
- **Responsive layout** (`_apply_responsive_layout`) corregido: ahora re-empaqueta `_header_left` + `_header_center` + `_header_right` en orden estricto (antes solo left+right, lo que hacia que el logo apareciera a la derecha del status pill al re-empaquetar).
- **Botones start/stop/pause** rewireados a `_set_pill_state` con variant dinamico:
  - Detenido: TEST=ghost, INICIAR=primary, PAUSAR=secondary disabled
  - Corriendo: TEST=disabled, INICIAR=success (texto "■ CORRIENDO"), PAUSAR=warning enabled
  - Pausado: PAUSAR=primary "▶ REANUDAR"

### Monitor secundario

- `src/gui.py:_place_window_on_monitor` reescrito: usa indexing nativo de mss (`monitors[1]` = primario Windows, `monitors[2]` = secundario, ...). **Antes** ordenaba por (left, top), lo que en setups con primario a la derecha hacia que `monitor_index=2` cayera en el primario. Ahora respeta lo que Windows considera secundario.
- Agregado fallback con log si el monitor solicitado no existe → cae a primario.
- `App.__init__` lee `gui.monitor_index` desde `config.yaml` (default 2 = secundario). Se puede sobrescribir agregando `gui: { monitor_index: 1 }` al yaml.
- `self.after(50, lift+focus)` para que la ventana aparezca al frente tras la colocacion.

---

### Telemetria de combate (Path A) — IMPLEMENTADO

- **`src/telemetry.py`** (NUEVO, 281 LOC) — Singleton `CombatTelemetry` thread-safe.
  - `RotatingFileHandler` 10MB x5 backups → `logs/combat-{YYYYMMDD}.log`
  - JSONL emitter line-buffered → `logs/combat-{YYYYMMDD}.jsonl` (un evento por linea con `ts`/`kind`/`fight_id`/`turn`)
  - Rotacion automatica al cambiar el dia (`_maybe_rotate_for_day`)
  - API: `set_enabled` / `start_fight` / `set_turn` / `emit(kind, **payload)` / `end_fight` / `info|warn|debug|error(category, msg)` / `configure_from_dict(cfg)`
  - Filtro por categorias opcional via `combat_telemetry_categories: [SADIDA, SNIFFER, ...]`
  - **Sin overhead si `enabled=False`**: todos los emisores son no-op.
- **`src/bot.py`** — Cableado:
  - Import `from telemetry import get_telemetry, configure_from_dict as configure_telemetry` (linea 18)
  - `Bot.__init__`: `configure_telemetry(config['bot'])` + `self._sniffer_fight_id` tracker
  - `_enter_combat` → `tel.start_fight(fight_id)` + `emit("enter_combat", origin, my_cell, profile)` (usa `_sniffer_fight_id` capturado del GJK; fallback `t{int(now)}`)
  - `_handle_sniff_event` `turn_start` propio → `tel.set_turn(N)` + `emit("turn_start", actor, my_cell, pa_pre_gts)`
  - `_handle_sniff_event` `fight_end` → `emit("fight_end_packet", raw)` + `tel.end_fight(reason="GE_packet")`
  - `_handle_sniff_event` `fight_join` → captura `data["fight_id"]` (extraido por sniffer) en `self._sniffer_fight_id`
  - `_handle_combat` antes/despues de `on_turn`: `emit("on_turn_begin", pa, mp, my_cell, action_pos, action_source, enemies, sniffer_turn, template_turn)` + `emit("on_turn_end", result)` + `emit("on_turn_error", error)` en el except
- **`src/combat/sadida.py`** — Cableado:
  - Import perezoso de `get_telemetry` con fallback no-op (evita falla si telemetry no estuviera disponible)
  - `_cast_with_pa_gate`: `emit("spell_skip"|"spell_attempt"|"spell_result", name, spell_id, cost, pa_before, pa_after, ok, dt_s)` por intento
  - `on_turn` arranque: `emit("sadida_state", pa, mp, my_cell, sniffer_pa, h{1,2,3}_pending, ps_cd_local, enemies)`
  - `emit("combo_branch", branch="combo1"|"combo2", pa, mp, tackled, cd_*)` al elegir rama
- **`src/sniffer.py`** — `GJK` parser ahora extrae `fight_id` (parts[0]) y lo emite en el evento `fight_join`.
- **`config.yaml`** — Toggle `bot.combat_telemetry: false` (default OFF) + `bot.combat_telemetry_categories: []`.
- **GUI** — Checkbox "Telemetria de combate (logs/combat-*.jsonl)" en pestaña **Ajustes → Runtime**. Handler `_save_combat_telemetry_setting` reconfigura el singleton sin reiniciar el bot.

**Smoke test ejecutado**: emitidos 7 eventos (fight_start, on_turn_begin, log SADIDA, spell_attempt, spell_result, on_turn_end, fight_end) → todos escritos correctamente en `logs/combat-20260416.jsonl` con shape esperado.

---

## Pendiente (no iniciado, para futuras sesiones)

### Combate — medio alcance

- Migrar `print()` → `logging` con categorias filtrable (SADIDA / SNIFFER / DIAG / COMBAT). Telemetria ya cubre el JSONL; falta migrar los `print()` rotativos.
- Refactor `_handle_combat` (`src/bot.py:3268`, ~270 LOC) en sub-funciones: `_combat_handle_popups`, `_combat_handle_placement`, `_combat_handle_turn`, `_combat_handle_idle`.
- Test de simulacion: sniffer mock que reproduce un fight grabado con `combat_debug_capture: true`.

### Combate — reimplementacion (cuando exista telemetria)

- `src/combat/state_machine.py` — extraer maquina de estados explicita (IDLE → ENTERING → PLACEMENT → WAITING_TURN → MY_TURN → ENEMY_TURN → ENDED) con guards declarativos.
- `CombatContext` v2 — propiedades computadas en lugar de cachear `enemy_positions`.
- Layer `SpellCast` abstracto — patron "press key → click → wait for confirm OR PA drop" duplicado entre Sadida/Sacrogito.

### GUI Pro Max — pendientes (aplicar a tab por tab)

- Estilizar Combobox/Spinbox/Entry creados en sub-tabs — los nuevos `Pro.TCombobox` etc. existen pero los widgets actuales no los usan (no especifican `style="Pro.TCombobox"`).
- Reemplazar los botones colorinches del editor de nodos (`Capturar visibles` verde, `Eliminar nodo` pink, etc.) por `_make_pill_button` con variants apropiados.
- Aplicar `_make_card` al panel de "Sprite: ..." en lugar del frame plano.
- Tab Sniffer: usar `Pro.Treeview` + headers rediseñados.
- Tab Mobs: cards consistentes, espaciado uniforme.
- Modal `ResourceCaptureWindow` (linea 115): aplicar mismo theme + tokens.

---

## Notas para sesiones futuras

- **Regla irrevocable**: Sadida combo 1 siempre prioritario sobre combo 2. Cualquier feature que la viole esta mal.
- **Tokens viejos vivos**: `BG/PANEL/CARD/...` siguen funcionando como aliases — no romper esa retrocompatibilidad sin migrar todos los usos primero.
- Archivos clave:
  - `src/bot.py` (5824 LOC) — `_handle_combat` linea 3268, `_handle_sniff_event` linea 621, GTS handler linea 662, GE handler linea 716
  - `src/combat/sadida.py` (528 LOC tras Q3) — perfil activo
  - `src/sniffer.py` (915 LOC tras Q7) — parser TCP
  - `src/gui.py` (~6400 LOC tras refactor) — tokens en linea 59, helpers visuales linea 634, header en linea 1065
  - `config.yaml` — `combat_profile: Sadida`. Puede llevar `gui.monitor_index: 2` (default).
  - `WORKLOG.md` — este archivo. Actualizar despues de cada hito.

## Validacion ejecutada

```
python -c "import ast; ast.parse(open(f).read())"  # bot/sniffer/sadida/base/gui — todos OK
python -c "from gui import App; app=App(); app.update_idletasks(); ..."  # GUI instancia limpia, 8 tabs, posicion en monitor secundario verificada (738,391 dentro de 2560x1440)
```

Capturas: `gui_promax.png`, `gui_promax_v2.png` en raiz.
