# Diagnóstico y Fix: Bug de descarga al banco

## Síntoma reportado
> "al ya llegar al último map_ID de la descarga no hizo click a la entidad e inició secuencia de sprites, sino que presionó 2 y quedó en loop"

El bot llegaba al penúltimo mapa de la ruta Zaapabanco (ej. map 7414), y en lugar de hacer click en la celda de salida y continuar al banco, quedaba en un loop prematuro sin interactuar con el cajero.

---

## Ruta de descarga configurada (Zaapabanco)

```yaml
Zaapabanco:
  route: []
  route_by_map_id: {}
  route_exit_by_map_id:
    '7411': cell:456
    '7412': cell:455
    '7413': cell:456
    '7414': cell:142
```

- El banco está en un mapa posterior a 7414 (no listado en `route_exit_by_map_id`).
- Los mapas 7411–7414 son de tránsito; el banco en sí no está en la lista.

---

## Flujo de estados del unloading

```
unloading_start
  → presiona tecla 2 (pócima de recuerdo), cambia ruta a "Zaapabanco"
  → unloading_wait_map (espera 4s)
  → unloading_navigate  ←── loop de navegación
      si route_point != None: change_map → vuelve a unloading_navigate al cargar
      si route_point == None: "llegamos" → unloading_interact_banker
  → unloading_interact_banker   (click NPC)
  → unloading_click_hablar      (detecta "Hablar.png")
  → unloading_open_bank         (detecta "Consultarcaja.png")
  → unloading_transfer_1/2/3    (detecta templates de transferencia)
  → unloading_finish            (ESC + tecla 2 para volver)
  → unloading_wait_return → scan
```

---

## Root cause

### Función `_route_point_for_current_map()` (`src/bot.py:4711`)

```python
if direction_str.startswith("cell:"):
    target_cell = int(direction_str.split(":", 1)[1])
    click_pos = self._movement_click_pos_for_cell(target_cell)
    if click_pos:
        return click_pos
    # ← BUG: si click_pos es None, cae al _next_route_point() que
    #         devuelve None porque route=[] está vacío
```

Cuando `_movement_click_pos_for_cell(target_cell)` devuelve `None` (datos de celdas del mapa aún no cargados por el sniffer), la función no devuelve la dirección correcta. En cambio cae a `_next_route_point()` → `None` → `unloading_navigate` cree que "llegó" al banco.

### Por qué falla `_movement_click_pos_for_cell`

1. Llama a `_cell_to_screen(cell_id)`
2. `_cell_to_screen` intenta `_project_cell_with_visual_grid` primero
3. Esta función requiere que `_current_map_cells` tenga la celda cargada
4. Si el paquete GDM del sniffer no llegó aún, `_current_map_cells` está vacío → devuelve `None`
5. Los fallbacks (world_affine, detected_origin, map_origins_by_map_id) también pueden fallar si el mapa no tiene datos registrados

### Consecuencia

`unloading_navigate` recibe `None` en un mapa intermedio (7414) y transiciona a `unloading_interact_banker`. El cajero no está en ese mapa → el bot queda ciclando entre `unloading_interact_banker` y `unloading_click_hablar` (espera "Hablar.png", no la encuentra, reintenta), o bien avanza por false positives de templates hasta `unloading_finish` que vuelve a presionar tecla 2 → loop.

---

## Fix aplicado

### 1. Fallback en `_route_point_for_current_map` (`src/bot.py:4719`)

Cuando `_movement_click_pos_for_cell` falla, intenta `_cell_to_screen` directamente (sin ground offset) antes de rendirse:

```python
click_pos = self._movement_click_pos_for_cell(target_cell)
if click_pos:
    return click_pos
# Fallback: raw _cell_to_screen sin offset de suelo
raw_pos = self._cell_to_screen(target_cell)
if raw_pos:
    print(f"[NAV] Fallback a _cell_to_screen para celda {target_cell} en mapa {self._current_map_id}")
    return raw_pos
# Ambas proyecciones no disponibles — unloading_navigate reintentará vía su guard
```

### 2. Guard en `unloading_navigate` (`src/bot.py:3513`)

Antes de considerar `None` como "llegamos al banco", verifica si el mapa actual tiene salida definida en `route_exit_by_map_id`. Si la tiene, es un fallo de proyección transitorio → reintenta hasta 4 segundos:

```python
exit_dir = exit_by_map.get(str(self._current_map_id)) or exit_by_map.get(self._current_map_id)
if exit_dir:
    # Mapa tiene salida configurada → la proyección falló, esperar
    retry_until = getattr(self, "_unloading_nav_retry_deadline", 0.0)
    if retry_until == 0.0:
        self._unloading_nav_retry_deadline = now + 4.0
    if now < self._unloading_nav_retry_deadline:
        time.sleep(0.2)
        return
    # Tras 4s de fallo persistente → forzar avance igualmente
self._unloading_nav_retry_deadline = 0.0
# Ahora sí: llegamos al destino
self.state = "unloading_interact_banker"
```

---

## Estructura de proyección de celdas (resumen)

`_cell_to_screen(cell_id)` intenta en orden:
1. `_project_cell_with_visual_grid` — necesita `_current_map_cells` (datos GDM del sniffer)
2. `_fit_world_affine` — necesita ≥3 world_map_samples para el mapa
3. Si hay 1–2 samples → devuelve `None` explícitamente (línea 1725)
4. `_detected_origin` — origen detectado por IsoGridDetector
5. `map_origins_by_map_id[map_id]` — origen calibrado manualmente
6. `map_origins[_current_map_idx]` — fallback global (siempre disponible si la lista no está vacía)

Para mapas 7411–7414:
- Tienen `visual_grid_settings` ✓
- No tienen `world_map_samples` ✓ (no bloquean en paso 3)
- No tienen entrada en `map_origins_by_map_id`
- Usan fallback global en paso 6

---

## Deuda técnica relacionada

- **Calibración de mapas 7411–7414**: añadir entradas en `map_origins_by_map_id` para estos mapas mejoraría la precisión del click de salida.
- **Visual grid cells**: asegurarse de que el sniffer recibe y parsea el paquete GDM para cada mapa de tránsito antes de intentar la proyección.
