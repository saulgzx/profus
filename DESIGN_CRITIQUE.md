# Crítica de Diseño — Dofus AutoFarm

**Versión analizada:** v1.0 — capturas `gui_promax_v2.png`, `gui_final.png`, `gui_wide_redesign.png`, `gui_after_redesign.png`
**Código revisado:** `src/gui.py`, tokens de diseño, `assets/brand/logo.svg`
**Stage:** Refinamiento (no es exploración temprana; hay tokens definidos y varias iteraciones)

---

## Impresión general (en 2 segundos)

La app tiene una **base sólida** — paleta dark bien pensada, sistema de tokens con 5 niveles de superficie, tipografía jerárquica, y una arquitectura de pestañas lógica. Se nota el trabajo iterativo (hay al menos 4 versiones visibles del header).

El **problema central** es una **crisis de identidad cromática**: los tokens del código declaran marca **ámbar** (`BRAND = #F5A524`), pero la UI renderizada usa **azul** en el CTA principal y el logo box. El resultado es un aire genérico de "app de bot" en lugar de la identidad cálida y diferenciada que el sistema de design tokens promete. Esa desconexión, sumada a un exceso de colores semánticos en la fila de acciones (verde + amarillo + rojo + azul + gris oscuro simultáneos), es el mayor obstáculo para que la app se vea profesional.

---

## Usabilidad

| Hallazgo | Severidad | Recomendación |
|----------|-----------|---------------|
| A 540px de ancho (`gui_final.png`) las pestañas se **truncan a siluetas ilegibles**: "Farmir", "Rut:", "Per", "Base de d:", "Log:". El usuario no puede saber a qué pestaña va a navegar. | Critica | Definir ancho mínimo de ventana (≥900px) **o** colapsar a menú hamburguesa / sidebar vertical debajo de 800px. Alternativa: pestañas con scroll horizontal y flechitas `‹ ›`. |
| El header duplica el branding: la **barra de título del SO** ya dice "Dofus AutoFarm" y debajo aparece otra vez el logo + título. En ventanas angostas ocupa ~25% del alto vertical. | Critica | Eliminar el duplicado. Mantén sólo el header interno (que sí aporta: logo + estado + subtítulo técnico). Oculta el título nativo con `overrideredirect` si quieres un look premium (cuidado: pierdes resize nativo). |
| **3 botones primarios juntos** en el header: `TEST`, `INICIAR`, `PAUSAR`. TEST no se distingue visualmente de un CTA importante y es un botón de debug. | Moderada | `TEST` debe ser un botón de icono pequeño en un panel de debug, o moverse a Ajustes. En producción probablemente ni debería verse. Deja sólo **una** acción primaria: `INICIAR / PAUSAR / DETENER` con el mismo botón cambiando de estado. |
| Los atajos `F12 toggle · F8 pausa · F10 detener` aparecen como **texto plano** en el header, compitiendo con el status chip. | Moderada | Mover a un tooltip sobre cada botón (`INICIAR` tooltip → `F12`) o a una línea discreta en la status bar inferior. Mostrar atajos junto al botón que los ejecuta, no flotando. |
| En `Farming → NODOS POR MAPA` hay **6 botones de acción en dos filas** con 4 colores distintos (negro, amarillo, verde, amarillo, amarillo, rojo). El usuario no puede deducir cuál es la acción primaria del panel. | Moderada | Sólo una acción primaria (probablemente `Capturar visibles`). El resto en estilo "ghost" (outline o texto) con icono. Reserva rojo SÓLO para `Eliminar nodo` y ponlo separado, con confirmación. |
| El dropdown `Ruta asignada a recursos: Boo's` queda **al lado** de un botón `Guardar` verde. Parece que el "guardar" aplica a la ruta, pero visualmente podría aplicar a toda la sección. | Moderada | Agrupar el par `[dropdown][Guardar]` dentro de un contenedor con label "Asignación rápida" o reemplazar por auto-save al cambiar el dropdown (patrón más moderno). |
| El texto italic amarillo `"Esta herramienta usa map_id del sniffer..."` es **explicación permanente**. Ocupa espacio cada vez que el usuario entra a la pestaña. | Minor | Convertir en tooltip disparado por un ícono `(?)` al lado del título de sección. El onboarding es una sola vez, no tiene que vivir en la UI. |
| No hay **estados vacíos** claros. `"Sin map_id actual"` aparece como texto plano gris; `"Chequeo visual: pendiente"` igual. | Minor | Estado vacío con ícono + mensaje accionable: ej. `⊘ Sin map_id · [Usar actual]`. |

---

## Jerarquía visual

**Qué capta el ojo primero.** En la vista ancha (`gui_promax_v2.png`) el ojo va primero al **cluster derecho** (TEST + INICIAR azul + PAUSAR). Está bien: el CTA principal debe capturar atención. Pero el chip `● Detenido` con el punto rojo compite fuerte con él, y está a la izquierda. Resultado: el ojo oscila entre los dos polos del header.

**Flujo de lectura.** El layout horizontal del header (logo → status → shortcuts → CTAs) es razonable en pantalla ancha, pero **rompe completamente** en pantalla angosta, donde todo se apila y la jerarquía se pierde.

**Énfasis.** El peso tipográfico está bien usado (display bold para "DOFUS AUTOFARM", body para metadata). El problema es que las **etiquetas de sección** (`NODOS POR MAPA (SNIFFER)`, `CONTROLES`, `MODULO PRINCIPAL`) están en uppercase con tracking pequeño — parecen chips de etiqueta en lugar de títulos. Se leen como `metadata`, no como `heading`.

**Whitespace.** Generalmente bueno en la vista ancha. En la narrow queda **atestado**: los controles se amontonan, la barra de botones del formulario se encabalga.

**Recomendación puntual:** Subir el peso visual de los títulos de sección — `FONT_HEADING` en lugar de uppercase mini; un divisor sutil arriba; o una barra lateral de 3px en el color de marca. Así el usuario reconoce "aquí empieza una sección".

---

## Consistencia

| Elemento | Problema | Recomendación |
|----------|----------|---------------|
| **Paleta de marca** | Los tokens dicen `BRAND = #F5A524` (ámbar) pero toda la UI visible usa azul. Los comentarios en código mencionan "rebrand Apple-inspired". Es un rebrand a medias. | Decidir de una vez: o ámbar real (cambiar `btn_start` a ámbar, logo box a ámbar) o sincerar los tokens y renombrar a `BRAND = #3478F6` azul. Si el objetivo es el look Apple cálido, commitearse al ámbar completamente. |
| **Logo placeholder** | El header muestra un cuadrado azul con una `D` blanca. Existe un `logo.svg` elaborado (huevo Dofus con engranaje) que no se usa en el header. | Renderizar el SVG real a 28-32px con PIL/cairosvg y usarlo en el header. La "D" en caja se ve como placeholder de Gmail, no como app gaming. |
| **Colores de botones** | En una misma fila coexisten negro (`Capturar sprite`), amarillo (`Chequear sprite`, `Chequear en mapa`), verde (`Capturar visibles`), rojo (`Eliminar nodo`), disabled gris (`Recapturar sprite`). No hay regla clara de cuándo usar qué color. | Sistema de 3 jerarquías: **primary** (brand amber), **secondary** (ghost outline), **danger** (rojo, sólo destructivas). Estado disabled claro. Amarillo/naranja NUNCA como color de botón — reservado a warnings/toasts. |
| **Campos de formulario** | En `gui_wide_redesign.png` los dropdowns tienen fondo gris claro (casi blanco) que **rompe el tema dark**. En `gui_promax_v2.png` están oscuros (correcto). | Forzar `fieldbackground` y `foreground` consistentes vía `ttk.Style` para `TCombobox`. Tk/ttk es traicionero aquí: hay que mapear también `readonly`, `focus`, `hover`. |
| **Radios** | Los tokens declaran `RADIUS_SM=4, MD=6, LG=10` pero tk no soporta `border-radius`. Se emulan con padding+color, resultado inconsistente entre botones y cards. | Asumir el constraint de tk: **no fingir radios**. Todo rectángulo con padding generoso + 1px border `BORDER_SUBTLE`. O migrar a CustomTkinter / PyQt si el look moderno es crítico. |
| **Pestañas activas** | En `gui_promax_v2.png` "Farming" activa tiene underline azul + fondo sutil; en `gui_final.png` (angosta) la indicación se pierde parcialmente. | Reforzar el indicador de pestaña activa: underline más grueso (3px) + bold en label + fondo `BG_ELEVATED`. |
| **Iconografía** | Los botones usan glyphs Unicode (`▶`, `⏸`, `●`) sin ser parte de un sistema. No hay íconos en tabs. | Adoptar un set coherente: Tabler Icons vía PIL (PNG 16px). O commitearse 100% a Unicode emoji-less (todos los botones con el mismo estilo). |

---

## Accesibilidad

**Contraste de color** (estimado WCAG AA — necesita texto ≥4.5:1 para body, ≥3:1 para large text):

| Par de color | Ratio estimado | Veredicto |
|--------------|----------------|-----------|
| `TEXT_PRIMARY #E6E8EC` sobre `BG_BASE #0A0C10` | ~16:1 | Excelente |
| `TEXT_SECONDARY #A0A6B0` sobre `BG_SUBTLE #12151B` | ~7.8:1 | Bien |
| `TEXT_TERTIARY #6B7280` sobre `BG_ELEVATED #181C24` | ~3.6:1 | Fails AA para body — sólo usar para texto NO crítico |
| `TEXT_DISABLED #4A5260` sobre cualquier fondo dark | <2:1 | OK para disabled (no aplica AA), pero confirma que el usuario no pueda confundirlo con texto activo |
| Botón amarillo `#FBBF24` con texto negro | ~10:1 | Bien |
| Botón rojo `Eliminar nodo` `#F87171` con texto blanco | ~3.2:1 | Fails AA para body. Oscurece a `#DC2626` o usa texto negro. |
| Link/active en pestañas (azul) sobre dark | depende del azul exacto | Verificar que el underline activo sea `≥3:1` |

**Tamaños táctiles / clicáreas.** En desktop no aplica estricto (44x44px es móvil), pero algunos botones del form (`Guardar` verde al lado del dropdown) parecen ≤24px de alto. Mínimo deseable: 32px de alto para cualquier clickable.

**Legibilidad.** `FONT_CAPTION = (Segoe UI, 9)` y `FONT_MONO_SMALL = (Consolas, 8)` son **muy pequeñas** para densidad de información (logs, listas de nodos). Subir a 10/9 respectivamente, o permitir zoom.

**Estados de foco.** No vi indicador visible de foco de teclado en las capturas. En tk hay que configurar `focuscolor` en el style. Crítico para navegación con Tab.

**Color-only signals.** El status usa sólo un punto de color (rojo=detenido, verde=activo). Añadir **texto siempre visible** (ya está: `Detenido`), pero también icono (`●` vs `▶` vs `⏸`) para usuarios con daltonismo.

---

## Lo que funciona bien

- **Sistema de tokens en el código** — 5 niveles de superficie, tipografía escalonada, espaciado en múltiplos de 4. Esto es raro de ver en apps hobby; es nivel producto real.
- **Separación entre `BG_BASE` / `BG_SUBTLE` / `BG_ELEVATED` / `BG_OVERLAY`** con valores que realmente difieren — da sensación de profundidad sin exagerar.
- **Status bar inferior** con `● Detenido · Cosechados: 0 · v1.0` — patrón clásico de app técnica, útil de un vistazo.
- **Breadcrumb contextual** (`MODULO PRINCIPAL · actor=22240 · perfil=Sadida · modo=leveling`) — excelente para un bot; el usuario siempre sabe en qué contexto está operando.
- **Logo SVG** existente (huevo Dofus + engranaje) es conceptualmente fuerte — fusiona el universo del juego con "bot/automatización". Sólo falta usarlo.
- **Dark theme bien calibrado** — no es el típico `#000` plano; los grises fríos con `#0A0C10` base se sienten modernos.

---

## Recomendaciones priorizadas

### 1. Decidir la marca y aplicarla de punta a punta (impacto alto, esfuerzo medio)
El rebrand ámbar existe en tokens pero no en píxeles. Dos caminos:

- **Camino A (ámbar committed):** `btn_start` cambia a `BRAND #F5A524` con texto negro, `nav-active-underline` en ámbar, logo box en ámbar. El azul desaparece. Se alinea con el comentario "Apple-inspired" del código.
- **Camino B (azul committed):** renombrar tokens a `BRAND = #3478F6`, eliminar el amarillo de botones de acción, usar el azul sólo en `INICIAR` y en pestaña activa.

**El pecado es quedarse a mitad.** Elige uno y haz un `find-replace` consistente en el código.

### 2. Arreglar la experiencia en ventana angosta (impacto alto, esfuerzo medio)
Es donde la app se ve peor (`gui_final.png`). Opciones en orden de preferencia:

1. Establecer `minsize(960, 600)` en el root y asumir desktop.
2. Si hay usuarios con laptops pequeñas: migrar pestañas a **sidebar vertical colapsable** (patrón VSCode / Notion).
3. Al menos: scroll horizontal en el notebook con flechas, en lugar de truncar labels a 4 letras.

### 3. Reducir el ruido cromático de los botones (impacto alto, esfuerzo bajo)
Implementa **tres estilos de botón y basta**:

- `PrimaryButton` — fondo brand, texto contrastante, para UNA acción por pantalla
- `SecondaryButton` — ghost/outline, transparente con border `BORDER_DEFAULT`
- `DangerButton` — sólo para destructivas, con confirmación

Elimina los amarillos/naranjas como color de botón. El amarillo es warning, no acción.

### 4. Renderizar el logo SVG real (impacto medio, esfuerzo bajo)
Reemplaza la caja azul `D` con el huevo-engranaje. Usa `cairosvg` o `Pillow` + `svglib`:

```python
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPM
drawing = svg2rlg("assets/brand/logo.svg")
renderPM.drawToFile(drawing, "logo_32.png", fmt="PNG", dpi=144)
```

Cachea a 24/32/48px para distintos usos.

### 5. Jerarquizar el header (impacto medio, esfuerzo bajo)
- Una sola acción primaria (el botón combinado `INICIAR/PAUSAR/DETENER`)
- `TEST` fuera del header — a un panel dev-only con `config.yaml: debug_mode: true`
- Shortcuts como tooltip sobre el botón, no texto suelto
- Status chip con más aire a su alrededor para no competir con el CTA

### 6. Tratar los estados vacíos y tooltips (impacto medio, esfuerzo bajo)
- Texto explicativo amarillo italic → icono `(?)` con tooltip
- `Sin map_id actual` → empty state con CTA `[Usar actual]`
- `Chequeo visual: pendiente` → badge con icono reloj, no texto suelto

### 7. Accesibilidad mínima (impacto alto para usuarios afectados, esfuerzo bajo)
- Oscurecer el rojo de `Eliminar` a `#DC2626`
- Subir `FONT_CAPTION` de 9 a 10
- Añadir anillo de foco visible (`focuscolor=BRAND`) para Tab navigation
- No usar `TEXT_TERTIARY` (#6B7280) para texto que el usuario necesita leer

---

## Nota sobre la plataforma

tkinter/ttk es honesto pero tiene **techo visual bajo**: no hay border-radius real, no hay sombras, los widgets nativos se ven de los 2010s. El código ya está empujando al límite (emula radios con padding+colores). Si la ambición es un look 2026, considera:

- **CustomTkinter** — drop-in con radios, sombras, widgets modernos. Migración parcial posible.
- **PyQt6 / PySide6** — más esfuerzo, pero QSS (CSS-like) te da todo lo que los tokens prometen.
- **Dejar de pelearse con tk** — abrazar el look "técnico funcional" (tipo herramienta Linux) y ahorrar esfuerzo en efectos visuales. El branding + la coherencia cromática llevan a la app al 80% del camino igual.

Mi recomendación: **quedarse en tkinter**, aplicar las 7 prioridades de arriba, y aceptar el estilo "tool técnica pulida" en lugar de perseguir el look SaaS moderno (que tk no va a alcanzar nunca por mucho token que declares).
