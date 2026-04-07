# Dofus AutoFarm Bot - Contexto actual

## Archivos principales
- [src/gui.py](src/gui.py): app principal, GUI Tkinter con pestañas.
- [src/bot.py](src/bot.py): core del bot, máquina de estados.
- [src/sniffer.py](src/sniffer.py): sniffer TCP con Scapy sobre puerto 443.
- [src/detector.py](src/detector.py): detección visual con OpenCV, template matching y aros.
- [src/grid_detector.py](src/grid_detector.py): `IsoGridDetector`, RANSAC y auto-calibración de arena.
- [src/combat/anutrof.py](src/combat/anutrof.py): perfil de combate Anutrof.
- [src/combat/sacrogito.py](src/combat/sacrogito.py): perfil de combate Sacrogito.
- [config.yaml](config.yaml): configuración principal.

---

## Modos de operación
- `resource`: farming de recursos. Estado base: `scan`.
- `leveling`: auto-nivel atacando mobs. Estado base: `scan_mobs`.

Se configura en `farming.mode` de `config.yaml` o desde el toggle de la GUI.

---

## Máquina de estados

En `bot.py`, el `tick()` se comporta así:

```text
scan  -> (resource) -> _scan()      -> click_resource -> wait_first_segar / harvesting_wait
      -> (leveling) -> scan_mobs    -> _scan_mobs()   -> click_mob -> _click_mob()
any   -> in_combat  (GJK/Gp sniffer) -> _handle_combat() -> _combat_origin
```

### Re-escaneo de recursos
- Después de cada cosecha o skip, se hace `pending = []` y `state = "scan"`.
- Eso fuerza un `_scan()` fresco con detección por sprite.
- `harvested_positions` excluye nodos recientes:
  - 5 s si fue cosechado.
  - 8 s si fue saltado.
- `max_distance` en `_resource_visible_at_node` está en `45px` para reducir falsos positivos post-cosecha.

---

## Sistema de combate

### Entrada a combate
1. El sniffer detecta `GJK` o `Gp` y llama `_enter_combat()`.
2. `_SCAN_STATES = {"scan", "scan_mobs", "click_mob"}`.
3. Se guarda `_combat_origin` para saber a qué estado volver al terminar.

### Turno
1. `turn_start` del sniffer marca `_sniffer_turn_ready = True`.
2. `_cell_to_screen()` proyecta `_combat_cell` a coordenadas de pantalla.
3. `_resolve_action_position()` intenta resolver la posición del PJ en este orden:
   - `cell_refined`
   - `cell_cached_refined`
   - `cell`
   - `fallback_saved_refined`
   - `fallback_cached_refined`
   - `global_refined`
   - `fallback_saved`
   - `fallback_center`
4. Se construye un `CombatContext` con `enemy_positions`, `current_pa` y `my_cell`.
5. `combat_profile.on_turn(action_pos, ctx)` ejecuta la lógica del perfil.

### Fighter tracking
- `_fighters: dict[str, dict]` guarda `cell_id`, `team_id`, `alive` y `hp` por actor.
- En Dofus Retro, un ID negativo representa un monstruo.
- Se alimenta desde:
  - `GJK`: alta de actores.
  - `GIC`: estado de arena.
  - `GM`: movimientos.
  - `GTM`: HP, AP, MP y celda post-acción.
- `_get_enemy_screen_positions()` convierte `cell_id` de enemigos a coordenadas de pantalla.

### Ajustes relevantes ya aplicados en `sniffer.py`
- `GJK` debe parsearse con `.split("|")`, no con `.split(";")`.
- Formato observado de `GTM`:
  - `actor;delta_or_seq;max_hp;ap;mp;cell_id;;current_hp`
- Interpretación correcta de `GTM`:
  - `parts[1]` es delta o secuencia, no HP actual.
  - HP actual = `parts[7]` si existe; si no, `parts[2]`.
  - AP = `parts[3]`.
  - MP = `parts[4]`.
  - `cell_id` = `parts[5]`.
- La condición de muerte correcta es `hp < 0`, no `hp <= 0`.
- `GTM` actualiza también `_fighters[actor]["cell_id"]`.

---

## IsoGridDetector

- Archivo: [src/grid_detector.py](src/grid_detector.py)
- Al recibir `GIC`, guarda `_pending_gic_entries` con actores y celdas.
- En el primer frame de combate, `_try_detect_grid(frame)` llama `IsoGridDetector.detect()`.
- Detecta aros rojos de aliados y azules de enemigos.
- RANSAC genera hipótesis de origen a partir de pares `(aro_visual, cell_id)`.
- Si RANSAC tiene éxito, `_detected_origin` tiene prioridad máxima sobre fingerprint, `map_id` y fallback global.
- Si la confianza es alta (`score >= 2` y `spread <= 300px`), persiste el origen en `config.yaml` bajo fingerprint.

### Filtro anti-falsos positivos
- Usa `RETR_TREE` sin `MORPH_CLOSE`.
- Solo acepta formas anulares con `first_child != -1`.
- `_GAME_BOTTOM = 0.70` excluye retratos del PJ en la barra inferior.

---

## Perfiles de combate

### Anutrof
Archivo: [src/combat/anutrof.py](src/combat/anutrof.py)

- Usa `ctx.current_pa` y `combat_spell_pa_cost` para casteo basado en PA.
- Usa `ctx.enemy_positions` como targeting principal.
- Si no hay posiciones válidas, cae a offsets del botón de turno.
- Se eliminó la dependencia de `MiTurno.png`, porque esa template no existe y cortaba el loop de ataques prematuramente.

### Sacrogito
Archivo: [src/combat/sacrogito.py](src/combat/sacrogito.py)

- Hace self-target detectando `PJ.png` solo dentro del área de juego.
- Área usada para búsqueda:
  - X: 14% a 90%
  - Y: 9% a 70%
- `pj_threshold` por defecto: `0.60`.
- Si encuentra el sprite, convierte la posición del crop a coordenadas absolutas.
- Si no lo encuentra, usa fallback al centro del monitor.
- Prioriza `CastigoOsadoActivo` con hechizo `2`.
- Si no está disponible, usa `Disolucion` con hechizo `1`.
- Luego pasa turno con `Space`.

### Flujo de `on_turn()` en Sacrogito
1. Mueve el mouse al centro y captura un frame.
2. Chequea `Cerrar` por si el combate ya terminó.
3. Busca `PJ.png` solo en el crop del área de juego y resuelve `self_pos`.
4. Si `CastigoOsadoActivo` está disponible:
   - tecla `2`
   - click en `self_pos`
   - `Space`
5. Si no:
   - tecla `1`
   - click en `self_pos`
   - `Space`
6. Después de cada hechizo, vuelve a chequear `Cerrar`.

### Templates de combate
- `assets/templates/ui/Sacro/CastigoOsadoActivo.png`
- `assets/templates/ui/Sacro/DisolucionActivo.png`
- `assets/templates/ui/pj/PJ.png`

---

## Templates UI

### Existentes
- `assets/templates/ui/Cerrar.png`: botón X para cerrar resultados de combate.
- `assets/templates/ui/OK.png`: popup de subir de nivel.
- `assets/templates/ui/Atacar.png`: opción del menú contextual para iniciar combate.
- `assets/templates/ui/Sacro/CastigoOsadoActivo.png`
- `assets/templates/ui/Sacro/DisolucionActivo.png`
- `assets/templates/ui/pj/PJ.png`

### Ausentes
- `FueraAlcance.png`: útil para detectar cuando un ataque falla por rango.

### Manejo de `OK.png`
- Tiene prioridad 0 al inicio de cada `tick()`.
- Si `OK.png` existe y se detecta en pantalla, el bot hace click y retorna.
- También se chequea en el loop post-combate tras `GE fight_end`, con 6 iteraciones.

---

## Mobs / Auto-Nivel

### Detección de mobs
- `find_all_mob_sprites(frame, mob_name)` en `detector.py` prueba todos los sprites de `assets/templates/mobs/{mob_name}/`.
- Usa NMS a `40px`.
- Antes de detectar, se recorta el frame al área de juego para evitar falsos positivos en la UI inferior derecha:

```python
gx1 = int(fw * _REFINE_GAME_LEFT)    # 0.14
gx2 = int(fw * _REFINE_GAME_RIGHT)   # 0.90
gy1 = int(fh * _REFINE_GAME_TOP)     # 0.09
gy2 = int(fh * _REFINE_GAME_BOTTOM)  # 0.70
```

### Flujo
1. `_scan_mobs()` captura frame, aplica crop, llama `find_all_mob_sprites()` por cada mob habilitado y ordena hits por cercanía al centro del monitor.
2. Luego pasa a `state = "click_mob"`.
3. `_click_mob()`:
   - hace click en el mob más cercano,
   - espera `0.6s`,
   - busca `Atacar.png`,
   - hace click en la template si aparece,
   - si no aparece, usa `attack_menu_offset`.
4. Fija `_combat_origin = "scan_mobs"` y espera `GJK` o `Gp`.
5. Si no entra en combate en `2s`, vuelve a `scan_mobs`.

---

## GUI

Archivo: [src/gui.py](src/gui.py)

Usa `ttk.Notebook` con tema oscuro `clam`.

### Pestañas visibles
- `Farming`: nodos por mapa y navegación.
- `Mobs / Auto-Nivel`: toggle de leveling y lista de mobs con acciones.
- `Control + Status + Log`: frame fijo fuera del notebook, siempre visible.

### Métodos relevantes
- `_sync_runtime_bot_config()`: sincroniza config al bot en ejecución sin reiniciar.
- `_toggle_leveling_mode()`: activa o desactiva leveling.
- `_scan_mob_on_map()`: escanea mobs con crop al área de juego.
- `_refresh_mobs()`: reconstruye la pestaña de mobs desde config.

---

## Calibración celda -> pantalla

### Prioridad de origen
1. `_detected_origin`: origen calculado por `IsoGridDetector`.
2. `map_origins_by_fingerprint`: origen persistido por fingerprint de `GIC`.
3. `map_origins_by_map_id`: origen asociado al `map_id` de `GDM`.
4. `map_idx = 0`: fallback al primer origen de `map_origins`.

### Fingerprint
- El fingerprint se construye como lista ordenada de `cell_id` del `GIC`.
- Ejemplo: `"185,301,302,326"`.

### Slopes en `config.yaml`

```yaml
cell_calibration:
  slopes:
    col_x: -357.43
    col_y: 18.0
    row_x: -321.0  # revisar
    row_y: 54.0
  map_origins: [...]
  map_origins_by_fingerprint: {...}
  map_origins_by_map_id: {...}
```

### Limitación conocida
- Si ningún origen coincide con la arena actual, las proyecciones de celdas enemigas quedan fuera de pantalla.
- `_is_point_on_monitor()` las rechaza.
- `enemy_positions` queda vacío.
- En ese caso, Anutrof usa su fallback por offsets.

---

## Paquetes del sniffer

| Paquete | Uso |
|---------|-----|
| `GTS` | inicio de combate o turno inicial |
| `GTF` | fin de turno del actor |
| `GE` | evento genérico |
| `Gp` | inicio alternativo de combate |
| `GJK` | join fighter, delimitado por `|` |
| `GM` | movimiento de actor, actualiza `cell_id` |
| `GIC` | estado de arena, construye fingerprint |
| `GDM` | `map_id` al cargar mapa |
| `GDK` | confirmación de carga de mapa |
| `GA(129)` | update de PA del actor |
| `GTM` | stats post-acción: HP, AP, MP, celda |
| `Im` | mensaje del sistema |

---

## Problemas abiertos

### 1. Falta `FueraAlcance.png`
- Sin esa template, Anutrof no detecta si el ataque falla por rango.
- Eso puede hacer que desperdicie PA.

### 2. Sacrogito requiere validación en sesión real
- Tras el último ajuste, falta confirmar que `self_pos` apunta al personaje en el mapa.
- El riesgo es que detecte el retrato de turno en la esquina inferior derecha en vez del sprite del personaje.

### 3. Calibración de arena nueva
- Si `IsoGridDetector` encuentra muy pocos aros, por ejemplo solo uno, RANSAC falla.
- En ese caso cae al fallback `map_idx = 0`, que puede ser incorrecto.
- Resultado: las celdas enemigas se proyectan fuera de pantalla.

Caso observado:
- Fingerprint `185,301,302,326`
- Mapa `673`
- RANSAC falló
- No hay origen manual configurado
- Anutrof usa fallback por offsets

### 4. `_combat_cell` puede quedar en `None` al reingresar por `GTS`
- Si el bot reinicia en medio de un combate y entra por `GTS` en vez de `GJK`, `_combat_cell` queda en `None`.
- Se corrige recién cuando llega un `GTM` con la celda del PJ.

---

## Claves relevantes de `config.yaml`

```yaml
bot:
  actor_id: <id_del_personaje>
  sniffer_enabled: true
  combat_profile: Sacrogito   # o Anutrof
  combat_pa: 6
  combat_spell_pa_cost: 3
  sacrogito_self_pos: [x, y]  # fallback self-target
  cell_calibration:
    slopes: {col_x, col_y, row_x, row_y}
    map_origins: [...]
    map_origins_by_fingerprint: {"fp": [ox, oy]}
    map_origins_by_map_id: {"map_id": [ox, oy]}

farming:
  mode: resource  # o leveling
  resource_nodes_by_map_id:
    "map_id":
      - profession: Campesino
        resource: Trigo
        pos: [x, y]
```
