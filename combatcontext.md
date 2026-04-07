# Contexto y Arquitectura de la IA de Combate Avanzada

## 1. Arquitectura Propuesta

### A. Memoria y Sincronización de Estado (State Tracker)
La IA no debe depender de leer la pantalla para saber si tiene PA o si un hechizo está en cooldown. 
- **Sniffer como Fuente de Verdad:** Escucharemos los paquetes `GTM` (PA, PM, HP, Celda), `SC` (Cooldowns dados por el servidor) y `GA 300` (Confirmación de hechizo lanzado).
- **Predicción Local (Shadow State):** Como el servidor a veces tarda milisegundos en enviar el paquete `GTM` o `SC`, la IA descontará los PA y PM localmente de forma inmediata tras lanzar un hechizo. Si el servidor dice otra cosa después, el sniffer corrige el estado.

### B. Motor de Pathfinding Táctico
Actualmente el bot solo se "acerca" al enemigo (`move_towards_enemy`). La nueva IA usará el mapa de celdas (`dofus_map_data.py`) para:
- Encontrar la celda más cercana que otorgue **Línea de Visión (LoS)** al objetivo.
- Calcular si los **PM** actuales alcanzan para llegar a esa celda.
- Considerar obstáculos y entidades que bloqueen el paso.

### C. Sistema de Combos (Árbol de Prioridades)
Definiremos un sistema estricto donde la IA evalúa de arriba hacia abajo:
1. ¿Puedo lanzar **Combo 1** desde mi celda actual? -> Lo lanzo.
2. ¿No puedo? ¿Puedo moverme a una celda con mis PM para lanzar **Combo 1**? -> Me muevo y lo lanzo.
3. ¿Es imposible el Combo 1 (por Cooldown, PA o PM)? -> Evalúo **Combo 2** bajo la misma lógica.
4. Si sobran PA/PM, ¿me acerco o me alejo (kiteo)?

---

## 2. Requisitos: ¿Qué necesito que me entregues?

Para codificar esta IA a la perfección (empezando por una clase específica, ej. Sadida o Sacrógito), necesito que me detalles lo siguiente:

### 1. Diccionario de Hechizos del Personaje
Por cada hechizo que use el bot, necesito sus estadísticas exactas a nivel en el que lo tienes:
- **Nombre / Tecla:** (Ej. Zarza / Tecla 1)
- **Costo de PA:** (Ej. 4 PA)
- **Alcance Mínimo y Máximo:** (Ej. Rango 1 a 8)
- **¿Alcance Modificable?:** (Sí / No) *(Importante para saber si un buff de alcance lo afecta)*
- **¿Requiere Línea de Visión (LoS)?:** (Sí / No) *(Ej. Temblor no requiere, Zarza sí)*
- **¿Lanzamiento en línea recta?:** (Sí / No)
- **Cooldown (Tiempo de relanzamiento):** (Ej. 0 turnos, o 4 turnos para Potencia Silvestre)
- **Límites por turno:** (Ej. Máximo 2 veces por objetivo, o 3 veces por turno en total).

### 2. Definición Exacta de los Combos
Dime exactamente cómo se componen el Combo 1 y el Combo 2, y sus condiciones.

**Ejemplo (Sadida):**
- **Combo 1 (Prioridad Máxima):** Temblor + Viento Envenenado + Potencia Silvestre. 
  - *Condición:* Requiere 8 PA. Solo se lanza si Potencia Silvestre no está en cooldown.
  - *Target:* A sí mismo.
- **Combo 2 (Secundario):** La Sacrificada + Zarza x(N).
  - *Condición:* Si Combo 1 está en CD. Sacrificada a 1 celda libre cerca del enemigo. Zarza al enemigo con menos HP.

### 3. Comportamiento de Movimiento (Comportamiento Base)
¿Qué debe hacer el bot si le sobran PM o si no puede atacar?
- **Rushear:** Gastar todos los PM para acercarse al enemigo más cercano.
- **Kitear (Huir):** Gastar todos los PM para alejarse lo más posible (útil para el Sadida cuando lanza venenos).
- **Estático:** No moverse si no es necesario para atacar.

### 4. (Opcional pero Recomendado) Log del Sniffer
Si tienes un log de consola reciente de un combate donde uses estos combos, pégalo. Me sirve para confirmar los `spell_id` exactos que el servidor envía en los paquetes `SC` (cooldowns) y `GA 300` (lanzamientos), lo que nos permitirá atar la IA al protocolo 100% libre de errores visuales.

---

## Siguientes Pasos
Una vez me des las estadísticas de los hechizos y la lógica de tus combos, modificaré `base.py` para inyectar el sistema de memoria/cooldowns precisos, mejoraré el pathfinding en `bot.py` para usar A* y LoS, y te entregaré el perfil del personaje (ej. `sacrogito.py` o `sadida.py`) reescrito con esta nueva súper IA.