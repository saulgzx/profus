# Audit técnico v2 — 2026-04-27 (post Sadida-only cleanup)

> Diagnóstico inicial del proyecto **dofus-autofarm-v2** después de la limpieza
> Sadida-only del 2026-04-26 y antes de arrancar el roadmap por fases.
>
> v1 (`C:\Users\Alexis\dofus-autofarm`) está en producción y NO se toca.
> Todo el trabajo descripto acá va en v2.

## 1. Mapa actual del proyecto

```
dofus-autofarm-v2/
├── src/                  ~25.3k LOC Python
├── mapas/                9,223 XML (DB local de celdas)
├── assets/templates/     52 imágenes (mobs, recursos, UI, PJ)
├── tests/                3 archivos pytest (34 tests)
├── config.yaml           21 KB, ~89 claves bot.*
└── logs/                 JSONL diarios
```

| Archivo | LOC | Rol | Estado |
|---|---|---|---|
| `src/bot.py` | 9,289 | Máquina de estados (27+), driver del sniffer, proyección celda→pixel (5 estrategias), pathfinding BFS, combate, banco, anti-AFK | 🔴 Monolito |
| `src/gui.py` | 9,282 | Tkinter app, BotThread, ROI, dashboard, calibración visual, editores | 🔴 Monolito |
| `src/sniffer.py` | 1,098 | Parser TCP del protocolo Retro 1.39.5 (~30 tipos eventos) | 🟢 Aislado |
| `src/combat/sadida.py` | 1,273 | Combos, heurísticas, movimiento táctico | 🟡 Monolito local |
| `src/grid_detector.py` | 436 | RANSAC iso-grid, detección aros rojo/azul | 🟢 |
| `src/farming_smart.py` | 343 | Detección sprites de recursos | 🟢 |
| `src/screen.py` | 487 | mss capture, foreground HWND, ROI | 🟢 |
| `src/perf.py` | 334 | Medidor overhead-zero (JSONL rotante) | 🟢 |
| `src/telemetry.py` | 321 | JSONL + RotatingFileHandler | 🟢 |
| `src/gui_web.py` | 1,021 | HTTP read-only (Tailscale, móvil) | 🟢 Independiente |
| `src/gui_dashboard.py` | 535 | Métricas live | 🟡 Acoplado a gui.py |
| `src/detector.py` | 134 | Template matching OpenCV | 🟢 |
| `src/actions.py` | 133 | Wrappers pyautogui | 🟢 |
| `src/dofus_map_data.py` | 148 | Decode `map_data`, load XML | 🟢 |

**bot.py + gui.py = 72% del código vivo.** Esos dos archivos son donde concentrar el refactor.

## 2. Flujo actual de datos

Cliente Dofus → TCP plaintext → **Scapy sniffer** (`sniffer.py`) → `event_queue` (~30 tipos)
→ `Bot._drain_sniff_queue()` → `Bot._handle_sniff_event()` (954 LOC, switch que muta
~30 atributos `self._sniffer_*` y `self._combat_*`) → `Bot.tick()` (1,172 LOC, decide acción
según `self.state`) → `mss` frame capture → `detector.find_ui_screen` / sprites → si combate:
`CombatContext` → `Sadida.on_turn()` → `actions.click()` (pyautogui) → telemetría JSONL.

Flujo lineal pero entrelazado. **No existe `GameState` único** — el estado vive como ~90
atributos sueltos en `Bot` + claves en `config["bot"][...]`.

## 3. Problemas detectados

### Críticos
- `Bot.tick()` 1,172 LOC mezclando combat popups, drain sniffer, unload pods, anti-AFK,
  popups, map change, route, scan, combate.
- `Bot._handle_sniff_event()` 954 LOC con switch sobre 30+ tipos mutando estado global.
- GameState distribuido en ~90 atributos `self._sniffer_*`, `self._combat_*`, `self._route_*`,
  `self._fighters`, `self._map_entities` + más estado en `config["bot"][...]` persistido a YAML.
- Logging = `print()`. **396 prints en bot.py**. Imposible filtrar por severidad o módulo.

### Importantes
- 5 estrategias paralelas de proyección celda→pixel (`_with_visual_grid_exact`,
  `_with_visual_grid`, `_with_affine`, `_with_origin`, `_manual_pixel_for_cell`).
- Excepciones silenciadas en `bot.py:2199-2218` (3× `except (TypeError, ValueError): pass`
  en `_record_learned_movement_offset`).
- Sin timestamps en eventos del sniffer.
- Sin validación post-acción.
- Hardcodes de resolución (`_REFINE_GAME_LEFT=0.14`, `TOP=0.09`, `BOTTOM=0.70`).
- Detección de grid = color matching exacto, sin fallback.

### Menores
- `gui_dashboard.py` acoplado a `gui.py`.
- `test_detector.py` está en `src/` (debería estar en `tests/`).
- Duplicidad de detección de recursos entre `farming_smart.py` y `bot.py::tick()`.

### Deuda técnica
- Comentarios fechados (2026-04-26) breadcrumbs a limpiar después.
- README desactualizado vs. estado real.
- 7 docs `.md` en raíz mezclados (audit, contexto, worklog, design critique).

## 4. Riesgos técnicos

| # | Riesgo | Severidad |
|---|---|---|
| R1 | Proyección celda→pixel frágil (5 estrategias, fallback hardcodeado) | 🔴 Máxima |
| R2 | Hardcodes de resolución (asume 16:9 o 16:10) | 🔴 Máxima |
| R3 | Grid detection = color exacto, sin fallback | 🟡 Alta |
| R4 | Race conditions Bot ↔ GUI (queues compartidas sin lock explícito) | 🟡 Alta |
| R5 | Excepciones tragadas en calibración (aprende `None` sin warning) | 🟡 Alta |
| R6 | Config como base de datos (estado runtime persistido a YAML) | 🟡 Alta |
| R7 | Sniffer sin timestamp servidor (datos obsoletos tratados como frescos) | 🟡 Media |
| R8 | Anti-AFK heartbeat ciego (jiggle sin verificar cliente vivo) | 🟡 Media |
| R9 | GIC corrupto persistido (origen falso → próxima sesión arranca mal) | 🟡 Media |

## 5. Quick wins (bajo riesgo, alto impacto)

| # | Archivo:línea | Cambio |
|---|---|---|
| QW1 | `src/bot.py:2199-2218` | `except (TypeError, ValueError): pass` → log warning explícito |
| QW2 | `src/bot.py` (396 prints) | Crear `src/logger.py` con `logging.getLogger("bot")`. Migrar prints por capas. |
| QW3 | `src/sniffer.py` (cada `event_queue.put`) | Inyectar `event["ts"] = time.time()` |
| QW4 | `src/combat/base.py::CombatContext` | Añadir `game_state: Optional[GameState] = None` (preparar Fase 3) |
| QW5 | `src/grid_detector.py::detect()` | Devolver `confidence_score` y `fallback_used` |
| QW6 | `config.yaml` | Crear `config_schema.py` con dataclasses + validación al load |
| QW7 | `src/bot.py::_anti_afk_heartbeat` | Validar foreground + último paquete antes de jigglear |

## 6. Roadmap por fases

### Fase 1 — Estabilización
1. Logger centralizado (`src/logger.py`) + reemplazo gradual de `print` por capas.
2. Eliminar `except: pass` silenciosos.
3. Validación de config al load (fail-fast).
4. Heartbeat anti-AFK con verificación de vivo.

### Fase 2 — Arquitectura
1. `_handle_sniff_event` → `src/sniff_handlers.py` con dispatcher dict.
2. 5 estrategias de proyección → `src/projection.py` con interfaz unificada.
3. `gui_dashboard.py` standalone que reciba `GameState` por parámetro.

### Fase 3 — Sniffer + GameState
1. Crear `src/game_state.py` (dataclass con timestamps por campo).
2. `_handle_sniff_event` muta solo `self.game_state.*`.
3. `event["ts"]` propagado desde sniffer.
4. Validación de obsolescencia (`game_state.is_stale(field, max_age_s)`).

### Fase 4 — Combate
1. `Sadida.on_turn` lee de `game_state` (no de `Bot._sniffer_*`).
2. Validación post-acción (esperar `pa_update` o `game_action`).
3. Combos incompletos: persistir `pending_combo` en GameState.

### Fase 5 — GUI
1. Separar `BotThread` y queues en `src/bot_runtime.py`. GUI solo observa.
2. Panel debug: `game_state` en vivo.
3. Resolver crisis cromática del `DESIGN_CRITIQUE.md`.

### Fase 6 — Pruebas
1. Tests para `projection.py` (cell ↔ screen) por resolución.
2. Tests para `Sadida.on_turn` con `game_state` mockeado.
3. Tests para el dispatcher de sniff_handlers.

### Fase 7 — Monitoreo remoto
1. Endpoint `GET /status` (read-only) sirviendo `game_state.to_dict()` + últimos N logs.
2. JSON expuesto vía `gui_web.py`.
3. Sin sacar la ejecución a la nube — solo telemetría.

---

## Resumen ejecutivo

El proyecto **funciona y tiene buenos cimientos**: sniffer aislado, parser robusto,
telemetría JSONL, tests de parser, GUI completa. Los problemas son arquitectónicos:
dos monolitos con todo el estado distribuido en atributos sueltos y "logging" hecho
con prints.

El refactor más rentable es **Fase 1 (logger + matar except:pass) → Fase 3 (GameState
único)**. El resto son consecuencias.

Cambios sucesivos quedan documentados en `WORKLOG.md`.
