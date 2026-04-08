# Dofus AutoFarm Bot

Solo para gente via, no funciona en peruanos

---

## Indice

- [Requisitos previos](#requisitos-previos)
- [Tecnologias utilizadas](#tecnologias-utilizadas)
- [Instalacion](#instalacion)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Configuracion](#configuracion)
- [Uso](#uso)
- [Subir a GitHub](#subir-a-github)
- [Roadmap](#roadmap)

---

## Requisitos previos

### Software a descargar

| Herramienta | Version minima | Link |
|---|---|---|
| [Python](https://www.python.org/downloads/) | 3.10+ | https://www.python.org/downloads/ |
| [Git](https://git-scm.com/download/win) | 2.x | https://git-scm.com/download/win |
| [VS Code](https://code.visualstudio.com/) | Cualquiera | https://code.visualstudio.com/ (recomendado) |
| [GitHub CLI](https://cli.github.com/) | Cualquiera | https://cli.github.com/ (opcional pero util) |

### Cuenta necesaria

- Cuenta en [GitHub](https://github.com) para alojar el repositorio

---

## Tecnologias utilizadas

```
Python 3.10+
├── opencv-python       # Reconocimiento de imagen (detectar recursos, mobs, UI)
├── pyautogui           # Control de mouse y teclado
├── mss                 # Captura de pantalla rapida
├── Pillow              # Procesamiento de imagenes
├── pynput              # Listener de inputs (hotkeys para pausar/detener)
├── numpy               # Manejo de arrays de imagen
└── pyyaml              # Lectura de archivos de configuracion
```

---

## Instalacion

### 1. Clonar el repositorio

```bash
git clone https://github.com/TU_USUARIO/dofus-autofarm.git
cd dofus-autofarm
```

### 2. Crear entorno virtual (recomendado)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

---

## Estructura del proyecto

```
dofus-autofarm/
├── README.md
├── requirements.txt
├── config.yaml                  # Configuracion principal (rutas, personaje, etc.)
├── .gitignore
│
├── src/
│   ├── main.py                  # Punto de entrada
│   ├── bot.py                   # Logica principal del bot
│   ├── screen.py                # Captura y analisis de pantalla
│   ├── actions.py               # Clicks, movimientos, teclado
│   ├── detector.py              # Deteccion de recursos/mobs con OpenCV
│   └── state_machine.py        # Maquina de estados (idle, farming, fighting, etc.)
│
├── assets/
│   └── templates/               # Imagenes de referencia para deteccion
│       ├── resources/           # Iconos de recursos (trigo, madera, etc.)
│       ├── mobs/                # Sprites de mobs objetivo
│       └── ui/                  # Elementos de interfaz (botones, HP bar, etc.)
│
├── logs/
│   └── .gitkeep
│
└── tests/
    ├── test_detector.py
    └── test_actions.py
```

---

## Configuracion

Edita `config.yaml` antes de ejecutar:

```yaml
# config.yaml
game:
  window_title: "Dofus"        # Titulo exacto de la ventana del juego
  resolution: [1920, 1080]
  version: "retro"             # "retro" o "unity"

farming:
  mode: "resource"             # "resource" o "combat"
  resources:
    - "trigo"
    - "madera"
  map_path: []                 # Lista de coordenadas de mapas a recorrer

combat:
  auto_fight: true
  spells: [1, 2, 3]           # Teclas de hechizos a usar

bot:
  delay_min: 0.8               # Delay minimo entre acciones (segundos)
  delay_max: 2.0               # Delay maximo (anti-deteccion)
  pause_key: "F10"             # Tecla para pausar
  stop_key: "F12"              # Tecla para detener
```

---

## Uso

```bash
# Activar entorno virtual primero
venv\Scripts\activate

# Ejecutar el bot
python src/main.py

# Pausar: F10
# Detener: F12
```

---

## Subir a GitHub

### Primera vez (crear repo nuevo)

```bash
# 1. Inicializar git en la carpeta del proyecto
git init

# 2. Agregar todos los archivos
git add .

# 3. Primer commit
git commit -m "feat: initial project structure"

# 4. Crear repo en GitHub (con GitHub CLI)
gh repo create dofus-autofarm --public --source=. --remote=origin --push

# --- O manualmente en github.com ---
# Crear el repo en https://github.com/new
# Luego ejecutar:
git remote add origin https://github.com/TU_USUARIO/dofus-autofarm.git
git branch -M main
git push -u origin main
```

### Flujo de trabajo normal

```bash
git add .
git commit -m "descripcion del cambio"
git push
```

---

## .gitignore recomendado

El archivo `.gitignore` debe incluir:

```
venv/
__pycache__/
*.pyc
*.pyo
logs/*.log
config.local.yaml     # Config con datos sensibles (no subir)
.env
*.egg-info/
dist/
build/
```

---

## requirements.txt

```
opencv-python>=4.8.0
pyautogui>=0.9.54
mss>=9.0.1
Pillow>=10.0.0
pynput>=1.7.6
numpy>=1.24.0
pyyaml>=6.0
```

---

## Roadmap

- [ ] Deteccion de recursos por reconocimiento de imagen
- [ ] Navegacion automatica entre mapas
- [ ] Sistema de combate automatico
- [ ] Anti-deteccion con delays aleatorios
- [ ] Interfaz grafica (GUI) para configuracion
- [ ] Soporte multi-cuenta
- [ ] Dashboard de estadisticas (recursos farmeados, tiempo activo)

---

## Contribuir

1. Hacer fork del repositorio
2. Crear rama: `git checkout -b feature/nueva-funcionalidad`
3. Commit: `git commit -m "feat: descripcion"`
4. Push: `git push origin feature/nueva-funcionalidad`
5. Abrir Pull Request

---

## Licencia

MIT License - ver [LICENSE](LICENSE)
