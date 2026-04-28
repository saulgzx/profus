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

---

## Sesion 2026-04-26 — Limpieza Sadida-only

Objetivo: dejar Sadida como unico perfil de combate vivo.

### Cambios

- **borrado** `src/combat/anutrof.py` (perfil obsoleto).
- **borrado** `src/combat/sacrogito.py` (perfil obsoleto).
- **`src/bot.py:117`** — default `combat_profile` cambiado de `"Anutrof"` a `"Sadida"`.
- **`src/bot.py`** — eliminada funcion `_resolve_sacrogito_action_position` (~22 LOC).
- **`src/bot.py`** — eliminada rama `if self.combat_profile.name == "Sacrogito"` en el handler de turn_ready (combat loop). Ahora todos los perfiles pasan por `_resolve_action_position` generico. Eliminado el write `config["bot"]["sacrogito_self_pos"]`.
- **`src/bot.py:3445`** — eliminado bloque muerto `saved_pos = config["bot"].get("sacrogito_self_pos")` (~14 LOC). Reemplazado por comentario breadcrumb.
- **`src/gui.py:4790`, `:8606`** — eliminados writes a `sacrogito_self_pos` y `save_config()` correspondientes. Reemplazados por comentario breadcrumb. El resto del flujo de calibracion del label `_sacro_pos_lbl` se conserva.
- **`config.yaml:527`** — eliminada key `sacrogito_self_pos: [2152, 1248]`.

### Validacion

```
python -c "from combat import list_profiles; print(list_profiles())"  # ['Sadida']
python -c "import ast; ast.parse(open('src/bot.py').read())"  # OK
python -c "import ast; ast.parse(open('src/gui.py').read())"  # OK
pytest tests/  # 34 passed
```

---

## Sesion 2026-04-27 — Fase 1 ciclo 1: logger centralizado + matar 3 except:pass

Objetivo: arrancar Fase 1 (estabilizacion) del roadmap del audit `AUDIT_2026-04-27_post_sadida_cleanup.md`.

### Cambios

- **nuevo** `src/app_logger.py` (~115 LOC) — wrapper sobre `logging` stdlib. Funciones expuestas:
  - `configure_logging(log_dir, console_level, file_level, max_bytes, backup_count)` — idempotente, monta StreamHandler consola (INFO+) + RotatingFileHandler en `logs/bot_YYYY-MM-DD.log` (DEBUG+, 5MB x 5 backups).
  - `get_logger(name)` — devuelve logger bajo namespace `bot.*`. `get_logger("combat")` se reescribe a `bot.combat`.
  - Formato: `[YYYY-MM-DD HH:MM:SS] [LEVEL] [name] message`.
- **`src/bot.py:20-22`** — agregado `from app_logger import get_logger` + `_log = get_logger("bot")`.
- **`src/bot.py:2197-2237`** (`_click_offset_breakdown`) — reemplazadas las 3 `except (TypeError, ValueError): pass` por `_log.warning(...)` con contexto (map_id, key, val, error). Antes: calibracion fallaba silenciosa con dx/dy invalidos en config. Ahora: se loguea warning con la entrada exacta que se ignoro.

### Validacion

```
python -c "import ast; ast.parse(open('src/bot.py').read())"      # OK
python -c "import ast; ast.parse(open('src/app_logger.py').read())"  # OK
grep -c "except.*pass" src/bot.py                                  # 0
pytest tests/                                                      # 34 passed
```

Smoke test funcional:
```
python -c "from app_logger import get_logger; log=get_logger('test'); log.warning('val=%r', 'abc')"
# [2026-04-27 ...] [WARNING] [bot.test] val='abc'
```

Smoke test de la modificacion en `_click_offset_breakdown` con datos invalidos
(`dx="abc"`, `gl_offsets[0]="no-numero"`, `cell_entry={"dx":"xx","dy":"yy"}`) → tres warnings disparadas, valores invalidos ignorados, cero crash.

### Notas

- **Bug detectado del entorno**: la tool `Edit` corrompe archivos > ~450KB en el mount Windows. Aplicar la primera modificacion de bot.py corto la cola desde linea 9293. Recuperado haciendo splice con la cola intacta de v1 (`dofus-autofarm`). De aqui en adelante, edits sobre bot.py / gui.py se hacen via Python script en `mcp__workspace__bash`, no via `Edit`.
- 396 prints en bot.py NO migrados todavia. Eso es el ciclo 2 (por capas: combat / route / sniff / action).


---

## Sesion 2026-04-27 — Fase 1 ciclo 2: Migracion de prints de combate a logger

Objetivo: dejar de hacer "logging" via `print()` en la parte de combate de bot.py. Permite filtrar por nivel y revisar post-mortem en `logs/bot_*.log`.

### Survey previo

396 prints totales en `bot.py`, agrupados por tag:

```
[BOT]: 106            (state machine generico — proximo ciclo)
[COMBAT]: 52          ← migrado en C2
[SNIFFER]: 32         (proximo ciclo)
[UNLOAD]: 31          (banco — proximo ciclo)
[TELEPORT]: 22        (proximo ciclo)
[ROUTE]: 14           (proximo ciclo)
[DIAG]: 9             ← migrado en C2 (mayoria son diagnostico de combate)
[HARVEST]/[CALIB]/...  resto
```

Survey con tokenize confirmó que los 94 prints objetivo son **todos single-arg**, cero multi-arg (35 son multi-line con concatenacion implicita de f-strings adyacentes — el logger los acepta sin cambios).

### Cambios

- **`src/bot.py:23`** — agregada declaracion `_log_combat = get_logger("bot.combat")`.
- **`src/bot.py`** — 94 prints reemplazados por llamadas al logger:
  - `[COMBAT]` (61) → `_log_combat.info(...)`
  - `[DIAG]` (25) → `_log_combat.debug(...)`
  - `[COMBAT_CAPTURE]` (2) → `_log_combat.info(...)`
  - `[PLACEMENT]` (2) → `_log_combat.info(...)`
  - `[COMBAT/LVL_RATAS]` (4) → `_log_combat.info(...)`

Tag y mensaje preservados intactos. Solo cambia el destino (`print` → `_log_combat`).

### Validacion

```
python -c "import ast; ast.parse(open('src/bot.py').read())"          # OK
grep -c "print(.*\[COMBAT\]\|print(.*\[DIAG\]" src/bot.py             # 0
grep -c "_log_combat\." src/bot.py                                     # 94
pytest tests/                                                          # 34 passed
```

Smoke test (import bot + dispatch de log):
```
import bot  → OK
bot._log.name = 'bot'
bot._log_combat.name = 'bot.combat'
bot._log_combat.info("...")     → consola + logs/bot_2026-04-27.log
bot._log_combat.debug("...")    → solo archivo (consola filtra a INFO+)
bot._log_combat.warning("...")  → consola + archivo
```

### Convenciones establecidas para el resto de la migracion

| Tag actual | Logger | Nivel default |
|---|---|---|
| `[COMBAT]` | `bot.combat` | info |
| `[DIAG]` | `bot.combat` | debug |
| `[PLACEMENT]` | `bot.combat` | info |
| `[SNIFFER]` (futuro) | `bot.sniff` | info |
| `[ROUTE]` (futuro) | `bot.route` | info |
| `[UNLOAD]` (futuro) | `bot.bank` | info |
| `[TELEPORT]` (futuro) | `bot.bank` | info |
| `[CALIB]` (futuro) | `bot.calibration` | debug |
| `[BOT]` (futuro) | `bot` | info |

### Notas

- Cero modificaciones de mensajes / args. Si un `[COMBAT]` actual contiene "WARN" o "ERROR" en el body, sigue logueado a `info` por ahora. Tunear nivel por mensaje queda para un ciclo posterior si la senal/ruido lo amerita.
- Restantes en bot.py (302 prints): proximos ciclos por capa. Migrar `[SNIFFER]`+`[ROUTE]` juntos en C3 si el usuario aprueba.


---

## Sesion 2026-04-27 — Fase 1 ciclo 3: Migracion sniffer + route + bank

Objetivo: continuar migracion de prints a logger. Mismo patron mecanico que C2.

### Cambios

- **`src/bot.py:24-26`** — agregadas declaraciones:
  - `_log_sniff = get_logger("bot.sniff")`
  - `_log_route = get_logger("bot.route")`
  - `_log_bank = get_logger("bot.bank")`
- **`src/bot.py`** — 125 prints migrados:
  - `[SNIFFER]` × 39 → `_log_sniff.info`
  - `[ROUTE]` × 33 → `_log_route.info`
  - `[UNLOAD]` × 31 → `_log_bank.info`
  - `[TELEPORT]` × 22 → `_log_bank.info`

Survey con tokenize confirmó cero multi-arg prints. Migracion mecanica sin tocar mensajes.

### Estado total de migracion logger en bot.py

- Total prints originales: 396
- Migrados a logger: **219** (55%)
  - C1: 0 (solo agregó infraestructura + 3 warnings nuevas)
  - C2: 94 (combat)
  - C3: 125 (sniff/route/bank)
- Restantes: 177 (44%) — `[BOT]` (~106) y otros menores (`[HARVEST]`, `[CALIB]`, `[FARM]`, `[ANTI-AFK]`, sin tag) → siguientes ciclos.

### Validacion

```
python -c "import ast; ast.parse(open('src/bot.py').read())"  # OK
grep "_log_\(sniff\|route\|bank\) = get_logger" src/bot.py    # 3 declaraciones OK
grep -c "print(.*\[SNIFFER\]\|...\[ROUTE\]\|...\[UNLOAD\]\|...\[TELEPORT\]" src/bot.py  # 0
pytest tests/                                                  # 34 passed
```

Smoke test (import bot + dispatch):
```
loggers: ['bot', 'bot.combat', 'bot.sniff', 'bot.route', 'bot.bank']
bot._log_sniff.info("...")  → consola + archivo
bot._log_route.info("...")  → consola + archivo
bot._log_bank.info("...")   → consola + archivo
```


---

## Sesion 2026-04-27 — Fase 1 ciclo 4: Migracion final de prints en bot.py

Objetivo: completar la migracion de prints a logger en bot.py.

### Cambios

- **`src/bot.py:27-29`** — agregadas declaraciones:
  - `_log_farm = get_logger("bot.farm")`
  - `_log_calibration = get_logger("bot.calibration")`
  - `_log_eat = get_logger("bot.eat")`
- **`src/bot.py`** — 177 prints adicionales migrados (174 + 3 fallback):
  - `[BOT]` × 118 → `_log.info`
  - `[HARVEST]` × 7, `[FARM]` × 6, `[FARMING]` × 1, `[SFARM]` × 2 → `_log_farm.info`
  - `[CALIB]` × 11, `[GRID]` × 8, `[PROBE]` × 8 → `_log_calibration.debug`
  - `[EAT_BREAD]` × 7 → `_log_eat.info`
  - `[ANTI-AFK]` × 2 → `_log.info`
  - `[NAV]` × 3 → `_log_route.info`
  - `[MOBS_ACTIVATED]` × 1 → `_log.info`
  - `[DEFORMATION]` × 1 → `_log.warning` (alerta)
  - `[TEST]` × 2 → `_log.debug` (solo en test_mode)

### Estado final de migracion logger en bot.py

- **396 / 396 prints migrados (100%)** ✓
- 8 loggers en el namespace `bot.*`:
  - `bot` (124 calls — incluye 3 warnings de C1)
  - `bot.combat` (94)
  - `bot.sniff` (39)
  - `bot.route` (36)
  - `bot.bank` (53)
  - `bot.farm` (16)
  - `bot.calibration` (27)
  - `bot.eat` (7)
- `print()` restantes en `src/bot.py`: **0**

### Validacion

```
python -c "import ast; ast.parse(open('src/bot.py').read())"  # OK
grep -c "^[[:space:]]*print(" src/bot.py                      # 0
pytest tests/                                                 # 34 passed
```

Smoke test:
```
import bot OK
loggers = ['bot', 'bot.combat', 'bot.sniff', 'bot.route', 'bot.bank',
           'bot.farm', 'bot.calibration', 'bot.eat']
```

### Que viene despues

- Hay que migrar los prints en otros archivos: `gui.py` (~7), `sniffer.py`, `combat/sadida.py`, etc. Pero es ciclo aparte.
- En consola los logs salen via stderr (StreamHandler default). Si la GUI capturaba stdout esperando `[BOT] ...`, cambiar a redirigir stderr o leer del archivo `logs/bot_YYYY-MM-DD.log`.
- Hay 4 niveles efectivos en el archivo de log: DEBUG (calibration, diag), INFO (operacional), WARNING (alertas), ERROR (no usados aun en estos prints — los _log.error se agregaran en proximos ciclos).


---

## Sesion 2026-04-27 — Fase 1 ciclo 5: Migracion logger en archivos restantes

Objetivo: completar la centralizacion de logger en TODOS los archivos `.py` del proyecto.

### Cambios

**C5a** — `src/combat/sadida.py`:
- Insertado `from app_logger import get_logger` + `_log = get_logger("bot.combat.sadida")` despues del bloque try/except de import telemetry (cuidado: la primera insercion cayo dentro del try, se reverti y reinserto antes de los `# Spell IDs`).
- 70 prints `[SADIDA]` → `_log.info`.

**C5b** — `src/sniffer.py`:
- Insertado `_log = get_logger("bot.sniff")` despues de los imports.
- 20 prints `[SNIFFER]` → `_log.info`.
- 6 prints `[DIAG]` → `_log.debug`.
- 4 prints sin tag (en `if __name__ == "__main__"` standalone) NO migrados (CLI debug intencional).

**C5c** — archivos chicos restantes (loop generico):

| Archivo | Logger | Prints migrados |
|---|---|---|
| `src/gui.py` | `bot.gui` | 7 → info |
| `src/gui_web.py` | `bot.gui.web` | 5 → info |
| `src/farming_smart.py` | `bot.farm` | 14 → info |
| `src/grid_detector.py` | `bot.calibration` | 7 → debug |
| `src/detector.py` | `bot.detector` | 2 → info |
| `src/perf.py` | `bot.perf` | 2 → info |
| `src/telemetry.py` | `bot.telemetry` | 2 → info |
| `src/notifications.py` | `bot.notify` | 3 → info |
| `src/combat/__init__.py` | `bot.combat` | 2 → warning (loader errors) |

**Skip**:
- `src/app_logger.py` — sus 2 prints van a stderr como fallback intencional cuando logging mismo falla.
- `src/main.py` — 4 prints del CLI standalone, no son logging operacional.
- `src/test_detector.py` — 11 prints de un script standalone de debug.

### Estado total Fase 1 logging

- **bot.py**: 396 prints → 0 (100% migrado, 8 loggers).
- **resto del proyecto**: ~146 prints → 0 con tag operacional (solo quedan los CLI/debug intencionales).
- **Loggers totales**: 14 nombres registrados bajo `bot.*`:
  - `bot`, `bot.combat`, `bot.combat.sadida`, `bot.sniff`, `bot.route`, `bot.bank`, `bot.farm`, `bot.calibration`, `bot.eat`, `bot.gui`, `bot.gui.web`, `bot.detector`, `bot.perf`, `bot.telemetry`, `bot.notify`.

### Validacion

```
python -c "import ast; ast.parse(open(f).read())"  # todos OK (10 archivos)
cd src && python -c "from combat import list_profiles; print(list_profiles())"  # ['Sadida']
pytest tests/  # 34 passed
```

Verificado: `gui.py` no fue truncado por el bug de Edit (la insercion fue chica, sin disparar el clamp). Tail intacto, line count v2 = v1 + 3 (los 3 imports/decl).

### Notas

- El `print()` que queda en `app_logger.py` redirige a `sys.stderr` — es el fallback intencional cuando el setup de logging falla.
- `combat/__init__.py` usa `_log.warning` para errores de carga de perfil (antes `print(f"[COMBAT] Error cargando modulo...")`).
- El usuario va a ver en consola TODO lo que era INFO+. Si quiere silenciar `bot.calibration.debug` en consola, basta con `configure_logging(console_level=logging.INFO)` (es el default ya).
- Ahora se puede hacer post-mortem leyendo `logs/bot_YYYY-MM-DD.log`.


---

## Sesion 2026-04-27 — Fase 1 ciclos 6, 7, 8: cierre de Fase 1 (estabilizacion)

### C6 — Validacion de config al load (fail-fast)

- **nuevo `src/config_loader.py`** (~150 LOC) — loader unificado:
  - `load_config(path=None, strict=False)` — yaml.safe_load + setdefault de las 5 secciones top-level + validacion de tipos de 11 keys conocidas.
  - Modo `strict=True` raise `ConfigValidationError`. Modo `strict=False` (default) loguea warning via `bot.config` y continua.
  - `save_config(config, path=None)` — yaml.dump + allow_unicode.
- **`src/gui.py:204-217`** — `load_config()` y `save_config()` reemplazadas por proxies que delegan al loader unificado, manteniendo la firma exterior intacta para no romper imports.
- **`src/main.py:108-118`** — idem.
- **`config.yaml`** — limpieza de 43 NUL bytes (residuo del bug de Edit del primer ciclo Sacrogito). Ahora YAML parsea limpio.

Validators de tipo registrados (no obligatorios — solo si la key esta presente):
- `bot.combat_profile` str, `bot.threshold/ui_threshold/pj_threshold` float|int,
  `bot.sniffer_enabled` bool, `bot.scan_idle_delay` float|int,
  `bot.start_map_idx` int, `bot.stop_key` str, `farming._previous_mode` str,
  `game.monitor_index` int, `game.window_title` str.

Smoke test:
```
load_config()                                            → real config OK
load_config(bad_threshold_yaml, strict=True)             → ConfigValidationError
load_config(bad_threshold_yaml, strict=False)            → warning + continua
```

### C7 — Heartbeat anti-AFK con verificacion de cliente vivo

- **`src/bot.py:124`** — `self._last_sniff_event_at = 0.0` inicializado en `__init__`.
- **`src/bot.py:744-758`** (`_drain_sniff_queue`) — trackea `_last_sniff_event_at = time.time()` cuando proceso al menos un evento.
- **`src/bot.py:3819-3835`** (`_maybe_anti_afk_heartbeat`) — antes de jigglear:
  - Si `sniffer_active` y > 60s sin paquetes (`bot.anti_afk_sniff_silence_s`, default 60),
    skip el jiggle, log warning ("cliente probablemente desconectado o crasheado") y throttle.
  - Si sniffer no activo, comportamiento original.

Resuelve riesgo R8 del audit: anti-AFK heartbeat ya no jigglea contra una ventana muerta.

### C8 — Reemplazar `except Exception: pass` puros en bot.py

- 43 ocurrencias de `except Exception:\n    pass` reemplazadas por:
  ```
  except Exception as _exc:
      _log.debug("[bot] except Exception swallowed: %r", _exc)
  ```
- Conservados intencionalmente: 134 `except (TypeError, ValueError): pass` y similares
  (parsing defensivo valido), `except queue.Empty`, `except (BrokenPipeError, ...)`.

Resuelve parcialmente riesgo R5: errores reales que antes se tragaban silenciosamente
ahora dejan rastro en el log (al nivel DEBUG, sin spamear consola).

### Validacion final Fase 1

```
python -c "import ast; ast.parse(open('src/bot.py').read())"           # OK
python -c "import ast; ast.parse(open('src/config_loader.py').read())" # OK
python -c "from main import load_config; print(load_config()['bot']['combat_profile'])"
                                                                       # → 'Sadida'
python -m pytest tests/                                                # 34 passed
```

---

## **Cierre Fase 1 — estabilizacion COMPLETA** 

| Quick win | Estado |
|---|---|
| Logger centralizado (`app_logger.py`) | ✅ C1 |
| Logger en `bot.py` (396 prints → 0) | ✅ C2-C4 |
| Logger en resto del proyecto (sadida, sniffer, gui, etc.) | ✅ C5 |
| Validacion de config al load | ✅ C6 |
| Heartbeat anti-AFK con cliente-vivo | ✅ C7 |
| `except Exception: pass` peligrosos eliminados en bot.py | ✅ C8 |

**Loggers en namespace `bot.*`:** 15 nombres
- `bot`, `bot.combat`, `bot.combat.sadida`, `bot.sniff`, `bot.route`, `bot.bank`,
  `bot.farm`, `bot.calibration`, `bot.eat`, `bot.gui`, `bot.gui.web`, `bot.detector`,
  `bot.perf`, `bot.telemetry`, `bot.notify`, `bot.config`.

**Cosas pendientes para futuras fases (no F1):**
- 134 `except (Type, Value): pass` defensivos — mantener pero auditar caso por caso si emergen sorpresas.
- 43 `except Exception: pass` en `gui.py` y otros archivos — escapan al alcance de C8 (solo tocamos bot.py). Migrar en F5 (GUI).
- Levels (`info` vs `warning` vs `error`) — C2-C5 default a `info`/`debug` mecanico. Tunear cuando aparezcan logs ruidosos en el primer run real.


---

## Sesion 2026-04-27 — Fase 3 ciclos 1-3: GameState + dual write desde sniffer handlers

### F3.C1 — GameState creado + tests

- **nuevo `src/game_state.py`** (~155 LOC) — dataclass con campos tipados y timestamps por campo:
  - Personaje: `actor_id`, `character_name`, `hp`, `max_hp`, `kamas`, `pods_current`, `pods_max`.
  - Mapa: `current_map_id`, `current_cell`, `map_data_raw`.
  - Combate: `in_combat`, `in_placement`, `is_my_turn`, `pa`, `pm`, `combat_cell`, `combat_turn_number`, `fight_started_at`, `fighters`.
  - Ruta: `active_route_name`, `route_step_idx`.
  - Sniffer health: `last_sniff_event_at`, `sniffer_active`.
  - Diagnostico: `last_error`, `last_error_at`.
- API: `set(field, value, ts=)`, `update(mapping, ts=)`, `get(field)`, `get_timestamp(field)`, `age_s(field)`, `is_stale(field, max_age_s)`, `reset_combat()`, `to_dict(include_timestamps=)`.
- **nuevo `tests/test_game_state.py`** — 22 tests cubriendo set/get, timestamps, is_stale, age_s, update, reset_combat, to_dict, validacion de field names (typos detectados al instante).

### F3.C2 — Instanciar GameState en Bot + primer campo end-to-end

- **`src/bot.py`** — `from game_state import GameState`.
- **`src/bot.py:128`** — `self.game_state = GameState()` en `__init__`.
- **`src/bot.py:132`** — `self.game_state.set("sniffer_active", False)` (sync inicial).
- **`src/bot.py:768`** — en `_drain_sniff_queue`, dual write: `self._last_sniff_event_at = now` + `self.game_state.set("last_sniff_event_at", now, ts=now)`.
- **`src/bot.py:3844`** — `_maybe_anti_afk_heartbeat` ahora lee de `self.game_state.get("last_sniff_event_at")` en vez del atributo suelto. Primer LECTOR migrado.

### F3.C3 — Migracion masiva de campos sniffer al GameState (dual write)

Handlers en `_handle_sniff_event` que ahora escriben en GameState ademas del atributo viejo:

| Handler | Campos GameState seteados |
|---|---|
| `pa_update` | `pa`, `pm` |
| `pods_update` | `pods_current`, `pods_max` |
| `character_stats` | `hp`, `max_hp` |
| `player_profile` | `actor_id`, `character_name` |
| `map_data` | `current_map_id`, `map_data_raw` |
| `placement` | `in_placement = True` |
| `turn_start` (mi turno) | `is_my_turn = True`, `in_placement = False`, `combat_turn_number` |
| `turn_end` (mi turno) | `is_my_turn = False` |
| `fight_end` | `reset_combat()` (limpia in_combat, pa, pm, combat_cell, fighters, etc) |

Total: 13 referencias a `self.game_state` en bot.py.

**Estrategia dual write**: los atributos viejos (`self._sniffer_pa`, `self._char_hp`, `self.current_pods`, `self._current_map_id`, `self._sniffer_in_placement`, `self._sniffer_turn_ready`, etc.) se siguen escribiendo intactos. Esto preserva el comportamiento actual de TODO el codigo que los lee. La migracion de los lectores pasa a F4 (combate) y F5 (GUI), donde se hace por consumidor.

**Bug detectado y corregido**: el primer script de C3 modificaba la variable global `text` pero olvidaba escribir el archivo al final. Resultado: 7 handlers que crei migrados estaban solo en memoria. Detectado por `grep -c "self.game_state\." bot.py` que dio 5 cuando esperaba 11+. Re-aplicado en una segunda pasada.

### Validacion

```
python -c "import ast; ast.parse(open(f).read())"  # game_state.py + bot.py OK
pytest tests/                                       # 56 passed (34 previos + 22 nuevos)
grep -c "self.game_state" src/bot.py                # 13
```

### Que sigue en F3 (opcional) o saltamos a F4

Pendientes tipicos de F3 que NO hice todavia:
- `combatant_cell` event → poblar `game_state.fighters` y `game_state.combat_cell` (es el evento que mas rota el dict de fighters).
- Inyectar `event["ts"]` desde `sniffer.py` para que el GameState use el timestamp del paquete, no del drain.

Pero ya hay valor concreto: el dict `to_dict()` se puede serializar para /status (F7) y la GUI puede observarlo (F5). Las nuevas escrituras del sniffer alimentan el GameState con todos los campos que hoy importan al combate (pa/pm/hp/cell/turn).


---

## Sesion 2026-04-27 — Fase 4 ciclo 1: Observabilidad de GameState en Sadida

Objetivo: que el perfil de combate vea staleness de los datos del sniffer SIN cambiar logica de combate. Permite detectar desincronizacion en produccion.

### Cambios

- **`src/combat/base.py:53,99-101`** — `CombatContext.__init__` acepta `game_state=None` opcional. Por backwards-compat: perfiles que no lo usan no se rompen.
- **`src/bot.py:6076`** — al construir `CombatContext`, pasar `game_state=self.game_state`.
- **`src/combat/sadida.py:653-666`** — `Sadida.on_turn` arranca con un bloque defensivo:
  ```
  gs = getattr(ctx, "game_state", None)
  if gs is not None:
      for fname, max_age in (("pa", 5.0), ("pm", 5.0), ("combat_cell", 10.0), ("is_my_turn", 15.0)):
          age = gs.age_s(fname)
          if age > max_age:
              _log.warning("[SADIDA] on_turn entry: game_state.%s tiene %.1fs (>%.1fs); valor=%r", fname, age, max_age, gs.get(fname))
  ```
- **`combat/base.py` se truncó por Edit**, recuperado desde v1 con splice. Memoria del bug actualizada: ahora aplica a TODO archivo del workspace, no solo grandes. Regla: solo Python via bash para edits en el mount.

### Validacion

```
python -c "import ast; ast.parse(open(f).read())"  # base.py + bot.py + sadida.py OK
python -c "from combat import list_profiles; print(list_profiles())"  # ['Sadida']
pytest tests/                                       # 56 passed
```

Smoke test funcional:
```
gs = GameState()
gs.set("pa", 6, ts=time.time()-10)   # stale 10s
gs.set("pm", 3, ts=time.time()-10)
gs.set("combat_cell", 215, ts=time.time()-10)
gs.set("is_my_turn", True, ts=time.time()-10)
# Ejecutar bloque de observabilidad → 3 warnings (pa>5, pm>5, combat_cell>10)
# is_my_turn no porque max=15 y age=10
# Caso fresh → silencio
```

### Notas

- Cambio puramente observacional. Si el sniffer va con lag, en lugar de pasar combate con datos viejos sin aviso, ahora aparece un WARNING en `logs/bot_*.log` con la edad exacta del campo.
- Los thresholds (5s/5s/10s/15s) se eligieron a ojo. Tunear cuando aparezca senal en produccion.
- No migré combatant_cell handler todavia (poblar `gs.fighters`). Sin eso, `gs.get("combat_cell")` se llena solo cuando hay player_profile. Mejorar en proximo ciclo de F3 si veo en logs que `combat_cell` siempre esta stale.


---

## Sesion 2026-04-27 — Fase 2 ciclos 1-2: dispatcher de eventos del sniffer

Objetivo: romper el switch monolitico de `Bot._handle_sniff_event` (954 LOC, 30+ branches) en handlers modulares testables. Estrategia gradual con bypass para no romper nada.

### F2.C1 — Infra del dispatcher (sin mover handlers)

- **nuevo `src/sniff_handlers.py`** (~80 LOC iniciales) — registry + decorator:
  - `@register("event_name")` valida nombre + duplicados.
  - `build_dispatcher()` devuelve copia inmutable del registry.
  - `registered_events()` para diagnostico.
  - `reset_registry()` solo para tests.
- **`src/bot.py:22`** — `import sniff_handlers`.
- **`src/bot.py:137`** — en `__init__`, `self._sniff_dispatcher = sniff_handlers.build_dispatcher()`.
- **`src/bot.py:850-854`** — al inicio de `_handle_sniff_event`, si hay handler en el dispatcher para `event`, lo invoca y retorna. Si no, cae al switch viejo.
- **nuevo `tests/test_sniff_handlers.py`** — 9 tests cubriendo registry API, duplicados, snapshot, etc.

### F2.C2 — Migrar 6 handlers chicos al dispatcher

Movidos al modulo nuevo (replicando branches identicas del switch viejo, que queda como fallback unreachable):

| Evento | LOC migradas | Helper usado |
|---|---|---|
| `pa_update` | ~7 | `bot._actor_ids_match` |
| `pods_update` | ~10 | (solo escrituras + log) |
| `player_profile` | ~13 | `bot._actor_ids_match`, `bot._fighters` |
| `character_stats` | ~38 | `bot._actor_ids_match`, telemetry death detection |
| `turn_end` | ~6 | `bot._actor_ids_match` |
| `game_action_finish` | ~3 | `bot._actor_ids_match` |

Convencion del modulo:
- Cada handler `(bot, data) -> None`.
- Muta `bot._sniffer_*` / `bot._char_*` / `bot.current_*` (backwards-compat).
- Muta `bot.game_state` (dual write F3).
- Loguea via loggers locales del modulo (`_log_sniff`, `_log_combat`).

### Validacion

```
python -c "import ast; ast.parse(open(f).read())"        # sniff_handlers.py + bot.py OK
cd src && python -c "import sniff_handlers; print(sniff_handlers.registered_events())"
# → ['character_stats', 'game_action_finish', 'pa_update', 'player_profile', 'pods_update', 'turn_end']
pytest tests/                                             # 65 passed (56 + 9 nuevos)
```

Smoke test funcional con FakeBot:
- `pa_update`: muta `_sniffer_pa`, `_sniffer_pm` + `gs.pa`, `gs.pm`.
- `pods_update`: muta `current_pods`, `max_pods` + `gs.pods_current`, `gs.pods_max`.
- `turn_end`: muta `_sniffer_turn_ready=False` + `gs.is_my_turn=False`.
- `character_stats`: muta `_char_hp` + `gs.hp`.
- `player_profile`: muta `gs.actor_id`, `gs.character_name`.

### Estado

- 6 / ~30 eventos migrados al dispatcher.
- Switch viejo INTACTO para los otros 24+. Comportamiento idéntico al run anterior.
- Cuando el bypass captura un evento migrado, la branch vieja del switch queda unreachable (igual sigue ahi, como spec leíble durante la migración).

### Quedan en F2

- C3: handlers complejos (`turn_start`, `map_data`, `combatant_cell`, `fight_end`, `placement`, `game_action`, ...)
- C4: eliminar el switch viejo + fail-fast cuando llegue evento sin handler.


---

## Sesion 2026-04-27 — Fase 2 ciclos 3-4: Cierre del refactor del dispatcher

### F2.C3 — 24 handlers restantes migrados al dispatcher (3 batches)

**Batch A** (chicos, 13 handlers, ~155 LOC): action_sequence_ready, player_ready,
placement, placement_cells, map_loaded, player_action, item_added, job_xp,
actor_snapshot, spell_cooldown, zaap_list, info_msg, interactive_state.

**Batch B** (medianos, 7 handlers, ~280 LOC): turn_start, fight_end, fight_join,
combatant_cell, map_data, map_actor, map_actor_batch.

**Batch C** (grandes, 5 handlers, ~325 LOC): raw_packet (NO migrado — vive en
otro bloque defensivo), game_object, arena_state, game_action, fighter_stats.

Generador automatico: script Python que parsea bot.py via AST, extrae el
cuerpo de cada branch, transforma `self.X` → `bot.X` con regex, genera el
codigo del handler. Append a sniff_handlers.py.

Bug intermedio: `map_actor` quedo en batch B y batch C por error mio. El
`@register` lo detecto al import (`ya hay handler para 'map_actor'`).
Truncado batch C duplicado y re-aplicado sin map_actor.

**Total handlers en dispatcher: 30 / 30** (todos los del switch original).

### F2.C4 — Eliminar el switch viejo + activar fail-warn

- **`src/bot.py:902-1814`** — el switch viejo (913 LOC, 30 branches) fue
  reemplazado por una linea de comentario:
  ```python
  # F2.C3: switch viejo eliminado. 30 handlers migrados a sniff_handlers.py.
  ```
- **`src/bot.py:849-857`** — el bypass del dispatcher pasa a ser el flujo
  unico:
  ```python
  _h = self._sniff_dispatcher.get(event)
  if _h is None:
      if event not in self._unhandled_sniff_events_logged:
          self._unhandled_sniff_events_logged.add(event)
          _log_sniff.warning("evento sin handler en dispatcher: %r", event)
      return
  _h(self, data)
  return
  ```
- **`src/bot.py:138`** — `self._unhandled_sniff_events_logged: set[str] = set()`
  inicializado en `__init__`. Throttle: cada evento desconocido se loguea
  una sola vez (no spam).

### Resultado

| Metrica | Antes | Despues | Delta |
|---|---:|---:|---:|
| `bot.py` LOC | 9378 | 8466 | **-912 (-10%)** |
| `_handle_sniff_event` LOC | 974 | ~30 | **-944 (-97%)** |
| `sniff_handlers.py` LOC | 0 | 1048 | +1048 (modulo nuevo) |
| Tests | 65 | 65 | (sin regresion) |

### Validacion

```
python -c "import ast; ast.parse(open('src/bot.py').read())"           # OK
python -c "import ast; ast.parse(open('src/sniff_handlers.py').read())" # OK
cd src && python -c "import sniff_handlers; print(len(sniff_handlers.registered_events()))"
# → 30
pytest tests/                                                          # 65 passed
```

Smoke test:
- 30 / 30 eventos esperados estan registrados.
- Eventos desconocidos dispararian `_log_sniff.warning("evento sin handler...")` una vez.

### Notas

- `raw_packet` queda en bot.py dentro del bloque `if probe_active:` (es un
  flujo defensivo aparte, no parte del switch principal).
- Los handlers en sniff_handlers.py mantienen escritura dual (atributos
  viejos + game_state) por backwards-compat. La eliminacion de los
  atributos viejos es un refactor aparte (Fase 3 o posterior).
- Cleanup de WORKLOG: F2 + F3 + F4 ahora avanzaron en paralelo. Estado
  actualizado en el resumen final del documento.


---

## Sesion 2026-04-27 — Cierre del audit (F4 / F7 / F6 / F5 finales)

### F4.C2 — Sadida lee del GameState

- **`src/combat/sadida.py:687-700`** — antes del wait loop por GTM, si
  `ctx.current_pa is None` y el GameState tiene PA reciente (<5s), usar
  ese valor en vez de esperar el GTM. Mismo para PM. Reduce latencia al
  inicio del turno cuando el sniffer mantiene el estado al dia.

### F7.C1 — Endpoint /api/game_state

- **`src/gui_web.py`** — `WebDashboardServer.__init__` acepta `game_state_provider` opcional.
- **`src/gui_web.py:do_GET`** — ruta `/api/game_state` devuelve `gs.to_dict(include_timestamps=True)`.
  - 503 si no hay provider configurado.
  - 500 si el provider raise.
- **`src/gui.py:_collect_game_state_dict`** — provider que pega al `bot.game_state.to_dict(...)`.
- **`src/gui.py:843`** — `WebDashboardServer(...)` recibe `game_state_provider=self._collect_game_state_dict`.

JSON respuesta incluye:
- Todos los campos publicos del GameState (pa, pm, hp, max_hp, current_map_id, in_combat, ...).
- Diccionario `_age_s` con la edad (en segundos) de cada campo, util para detectar staleness.

### F6 — Tests

**F6.C1 — `tests/test_sniff_handlers_integration.py`** (9 tests):
- Cada test construye un FakeBot con los atributos minimos.
- Invoca cada handler como lo haria el dispatcher.
- Verifica dual write (atributo viejo + GameState) + filtros por actor_id.

**F6.C2 — `tests/test_status_endpoint.py`** (5 tests):
- Levanta `WebDashboardServer` real en puerto libre.
- HTTP GET /api/game_state, parsea JSON, valida fields + age_s + status codes (200, 503, 500).

Bug intermedio en F6.C1: la fixture `clean_registry` de `test_sniff_handlers.py`
(que hacia `reset_registry()` al final) dejaba el registry vacio y rompia los
tests de integracion que necesitaban los 30 handlers reales. Fix: snapshot+restore
en vez de reset destructivo.

### F5.C1 — Cleanup de except Exception: pass en gui.py

- 49 ocurrencias de `except Exception:\n    pass` reemplazadas por:
  ```
  except Exception as _exc:
      _log.debug("[gui] except Exception swallowed: %r", _exc)
  ```
- Conservados: 50+ excepciones especificas (`except (TypeError, ValueError): pass`,
  `except queue.Empty`, `except (BrokenPipeError, ConnectionResetError)`).

### Validacion final

```
python -c "import ast; ast.parse(open(f).read())"   # bot, gui, sniff_handlers, sadida, base — todos OK
cd src && python -c "from combat import list_profiles; print(list_profiles())"  # ['Sadida']
cd src && python -c "import sniff_handlers; print(len(sniff_handlers.registered_events()))"  # 30
pytest tests/                                        # 79 passed
```

---

## **CIERRE DEL AUDIT — TODAS LAS FASES 1-7 COMPLETAS**

### Resumen ejecutivo

| Fase | Foco | Estado |
|---|---|---|
| F1 | Estabilizacion (logger, config, anti-AFK, except handling) | ✅ |
| F2 | Arquitectura (dispatcher de eventos del sniffer) | ✅ |
| F3 | Sniffer + GameState (dataclass + dual write + 22 tests) | ✅ |
| F4 | Combate (observabilidad + lectura del GameState) | ✅ |
| F5 | GUI (cleanup de except: pass + endpoint via gui.py) | ✅ |
| F6 | Pruebas (tests para sniff_handlers + endpoint /api/game_state) | ✅ |
| F7 | Monitoreo remoto (endpoint /api/game_state via gui_web.py) | ✅ |

### Numeros

| Metrica | Antes (pre-audit) | Despues | Delta |
|---|---:|---:|---:|
| `bot.py` LOC | 9378 | 8466 | **-912 (-10%)** |
| `_handle_sniff_event` LOC | 974 | ~30 | **-944 (-97%)** |
| Modulos nuevos | — | 4 | `app_logger.py`, `config_loader.py`, `game_state.py`, `sniff_handlers.py` |
| Tests | 34 | **79** | **+45 (+132%)** |
| Loggers nombrados | 0 (prints) | 15 | namespace `bot.*` |
| Prints en `bot.py` | 396 | 0 | -396 |
| `except Exception: pass` peligrosos | 92 | 0 | -92 (-100%) en bot.py + gui.py |
| Perfiles de combate | 3 (Sadida, Anutrof, Sacrogito) | 1 (Sadida) | limpieza inicial |

### Capacidades nuevas habilitadas

1. **Logging filtrable**: `logs/bot_YYYY-MM-DD.log` con niveles, namespaces, timestamps.
   `tail -f logs/bot_*.log | grep "bot.combat"` para ver solo combate.
2. **Diagnostico de calibración**: warnings con valor + map_id + cell_id cuando
   un offset de combate viene corrupto. Antes: silencio.
3. **Anti-AFK inteligente**: NO jiggle si el sniffer lleva > 60s sin paquetes
   (el cliente probablemente murió). Antes: jiggle ciego.
4. **GameState con timestamps**: `gs.is_stale("pa", 5.0)` detecta datos viejos
   del sniffer. Sadida loggea warnings cuando los entries de combate llegan stale.
5. **Endpoint HTTP `/api/game_state`**: dashboard externo o app movil pueden
   consultar el estado real-time sin tocar la GUI.
6. **Dispatcher modular**: agregar un evento nuevo del sniffer requiere solo
   un decorator `@register("evento_nuevo")` en `sniff_handlers.py`. El switch
   gigante ya no existe.

### Reglas operativas para mantenimiento futuro

- v1 (`C:\Users\Alexis\dofus-autofarm`) es PRODUCCION. NO tocar. Usar como
  baseline para comparar cambios de v2.
- Edits sobre cualquier archivo del workspace se hacen via Python via bash,
  NO con Edit/Write (bug del mount Windows trunca o deja NUL bytes).
- Cambios en ciclos pequenos. WORKLOG.md actualizado por hito.

### Pendientes opcionales (no bloqueantes)

- Eliminar atributos viejos `bot._sniffer_*`, `bot._char_*` cuando todos los
  consumidores hayan migrado a `bot.game_state.get(...)`. Hoy hay dual write,
  el siguiente ciclo es eliminar el lado viejo.
- Migrar las 5 estrategias de proyeccion celda→pixel a `src/projection.py`
  con una interfaz unificada (era F2.B planeado).
- Resolver crisis cromatica del DESIGN_CRITIQUE.md en gui.py.
- Tests para `Sadida.on_turn` con GameState mockeado (combos completos vs
  incompletos, PA insuficiente, enemigo fuera de LoS).


---

## Sesion 2026-04-27 — Fix1 post-deploy: imports faltantes en sniff_handlers.py

### Bug reportado por el usuario en runtime

```
File "src/sniff_handlers.py", line 587, in handle_combatant_cell
    map_entry["grid_xy"] = cell_id_to_grid(int(cell_id))
                           ^^^^^^^^^^^^^^^
NameError: name 'cell_id_to_grid' is not defined
```

### Causa raiz

Cuando migré los 30 handlers de bot.py a sniff_handlers.py (F2.C2/C3), el script
de migración solo trasladó código pero NO resolvió dependencias del namespace
de bot.py. Las funciones que estaban importadas a nivel módulo de bot.py
(`cell_id_to_grid`, `get_telemetry`, etc.) y constantes (`COMBAT_TIMEOUT`)
quedaron sin importar en sniff_handlers.py.

El AST + pytest no detectaron esto porque:
- AST.parse no valida nombres no resueltos.
- Los tests existentes de sniff_handlers (test_sniff_handlers.py) usan registry
  vacio (solo testean infra) y los de integración (test_sniff_handlers_integration.py)
  cubrieron handlers chicos (pa_update, pods_update, character_stats, turn_end,
  player_profile, game_action_finish) que SI tenían sus deps en orden — los
  handlers complejos (combatant_cell, map_data, fight_end, turn_start, etc.)
  no tienen test directo.

### Cambios

- **`src/sniff_handlers.py:31-33`** — agregados imports:
  ```
  from map_logic import cell_id_to_grid
  from telemetry import get_telemetry
  ```
- **`src/sniff_handlers.py:35-38`** — agregada constante espejada:
  ```
  COMBAT_TIMEOUT = 90.0  # debe coincidir con bot.py:38
  ```
- **`src/sniff_handlers.py:160-161`** — eliminado import inline duplicado de
  `get_telemetry` dentro de `handle_character_stats`.

### Verificación adicional cruzada

Hice un análisis estático cruzado:
- Recolectar todos los nombres a nivel módulo usados en sniff_handlers.py.
- Recolectar todos los `bot.X` (atributos/métodos accedidos via `bot.`).
- Verificar que cada uno está definido (importado, declarado en sniff_handlers
  o accesible en class Bot).

Resultado:
- Nombres globales no definidos: 0 (despues del fix)
- bot.X usados: 97
- bot.X NO encontrados en class Bot: 1 (`_death_alert_fight_id`, falso positivo —
  se asigna dentro del mismo handler con `bot._death_alert_fight_id = fight_id`
  y se lee con `getattr(bot, "_death_alert_fight_id", None)`).

### Validacion

```
python -c "import ast; ast.parse(open('src/sniff_handlers.py').read())"  # OK
cd src && python -c "import sniff_handlers; print(len(sniff_handlers.registered_events()))"  # 30
pytest tests/  # 79 passed
```

Smoke test del handler que crasheó:
```
import sniff_handlers
h = sniff_handlers.build_dispatcher()["combatant_cell"]
h(fake_bot, {"actor_id": "12345", "cell_id": 215})  # OK, sin NameError
```

### Lección para próximos refactors de gran movimiento

Antes de migrar código de un módulo a otro:
1. Hacer survey de los nombres LIBRES (no-self, no-builtin) usados.
2. Para cada nombre: confirmar si es import, constante o función global del
   módulo origen, y agregarlo al módulo destino.
3. NO confiar solo en AST + pytest — los nombres no definidos no se detectan
   hasta runtime si los tests no cubren el code path.

