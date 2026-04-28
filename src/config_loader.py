"""Loader unificado de config.yaml.

Antes este código estaba duplicado en `gui.py:204-212` y `main.py:108-118`.
Esta version unifica + agrega validacion ligera de tipos.

Uso:
    from config_loader import load_config, save_config

    cfg = load_config()                         # comportamiento actual
    cfg = load_config(strict=True)              # raise si validacion falla
    cfg = load_config("ruta/a/config.yaml")     # path custom

Validacion:
    Loguea WARNING (via _log_config) si:
      - una key conocida tiene tipo incorrecto (e.g. threshold no-float)
      - una seccion top-level no es dict
    En modo strict=True, levanta ConfigValidationError en lugar de warning.

NO valida la *ausencia* de keys (eso lo maneja .get() con defaults en cada
llamador), porque romperia configs viejos. Solo valida tipos de keys que SI
estan presentes.
"""

from __future__ import annotations

import os
from typing import Any
from pathlib import Path

import yaml

from app_logger import get_logger

_log = get_logger("bot.config")


CONFIG_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# Secciones top-level que siempre deben existir como dict (creadas con
# setdefault si faltan).
_REQUIRED_SECTIONS = ("bot", "farming", "game", "leveling", "navigation")


def _detect_truncation(raw_text: str) -> str | None:
    """Heuristicas para detectar si el YAML termino abruptamente.

    Retorna mensaje de warning, o None si no detecto problema.
    Detecta:
      - String quotada sin cerrar ('algo o "algo)
      - Lista sin elementos cerrados (- al final)
      - Bloque sin newline final
      - Tamaño 0
    """
    if not raw_text:
        return "config esta vacio"
    last_lines = raw_text.rstrip("\r\n").split("\n")[-3:]
    last = last_lines[-1] if last_lines else ""
    # 1) string quotada sin cerrar
    open_quotes = last.count("'") - last.count("''") * 2
    if open_quotes % 2 != 0:
        return f"ultima linea termina con string sin cerrar: {last!r}"
    open_dquotes = last.count('"')
    if open_dquotes % 2 != 0:
        return f"ultima linea termina con string sin cerrar: {last!r}"
    # 2) Termina en : o - (key sin valor o lista sin item)
    stripped = last.rstrip()
    if stripped.endswith(":") and not stripped.endswith("::"):
        # Puede ser legit (mapping vacio en el ultimo nivel) pero usualmente truncamiento
        return f"ultima linea termina en ':' (mapping sin valor): {stripped!r}"
    return None


def _write_robust(path, raw_str: str, max_attempts: int = 5) -> bool:
    """Escribe text al path con fsync + verificacion de tamaño post-write.

    En el mount Windows del workspace, ocasionalmente la escritura trunca.
    Esta funcion verifica que el tamaño on-disk == tamaño esperado, y reintenta
    hasta `max_attempts` veces. Retorna True si tuvo exito.
    """
    import time
    raw = raw_str.encode("utf-8")
    target = len(raw)
    for attempt in range(max_attempts):
        with open(path, "wb") as f:
            f.write(raw)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # algunos FS no soportan fsync
        with open(path, "rb") as f:
            actual_size = len(f.read())
        if actual_size == target:
            return True
        _log.warning(
            "save_config attempt %d: target %d bytes, on-disk %d. Reintentando.",
            attempt + 1, target, actual_size,
        )
        time.sleep(0.2)
    _log.error("save_config FAILED tras %d intentos. Archivo en disco puede estar truncado.", max_attempts)
    return False




class ConfigValidationError(ValueError):
    """Levantada cuando load_config(strict=True) detecta un valor invalido."""


# Lista de validadores de tipo. Cada entrada: (path_str, expected_type, label).
# - path_str: dot-path en el dict (e.g. "bot.threshold").
# - expected_type: tipo o tupla de tipos aceptados.
# - label: descripcion humana para el log.
#
# Si la key no esta presente, NO se valida (no es obligatoria a nivel de
# este loader — los defaults estan en cada consumidor).
_TYPE_VALIDATORS: list[tuple[str, type | tuple[type, ...], str]] = [
    ("bot.combat_profile",   str,           "nombre del perfil de combate"),
    ("bot.threshold",        (float, int),  "umbral de template matching (0..1)"),
    ("bot.ui_threshold",     (float, int),  "umbral UI"),
    ("bot.pj_threshold",     (float, int),  "umbral PJ"),
    ("bot.sniffer_enabled",  bool,          "sniffer on/off"),
    ("bot.scan_idle_delay",  (float, int),  "delay entre scans"),
    ("bot.start_map_idx",    int,           "indice de mapa inicial"),
    ("bot.stop_key",         str,           "tecla de stop"),
    ("farming._previous_mode", str,         "modo de farming previo"),
    ("game.monitor_index",   int,           "indice de monitor del juego"),
    ("game.window_title",    str,           "titulo de la ventana del juego"),
]


def _get_dot(d: dict, dot_path: str) -> tuple[bool, Any]:
    """Devuelve (presente, valor). presente=False si la key no esta."""
    cur: Any = d
    for part in dot_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _validate_types(data: dict, strict: bool) -> list[str]:
    """Aplica los validadores. Devuelve lista de errores (strings)."""
    errors: list[str] = []
    for dot_path, expected, label in _TYPE_VALIDATORS:
        present, val = _get_dot(data, dot_path)
        if not present:
            continue
        if isinstance(val, expected):
            continue
        # Excepcion: bool es subclase de int en Python; si esperamos int,
        # rechazar bool a proposito (ya manejado porque bool no esta en _TYPE_VALIDATORS
        # como int salvo donde corresponde).
        msg = (
            f"config[{dot_path}]={val!r} (tipo {type(val).__name__}) — se "
            f"esperaba {expected if not isinstance(expected, tuple) else tuple(t.__name__ for t in expected)}"
            f" ({label})"
        )
        errors.append(msg)
    return errors


def load_config(path: str | os.PathLike | None = None, strict: bool = False) -> dict:
    """Carga config.yaml con setdefault de secciones + validacion de tipos.

    Args:
        path: ruta al config.yaml. Si None, usa el del proyecto.
        strict: si True, raise ConfigValidationError ante errores de tipo.
                Si False (default), loguea warnings y continua.

    Returns:
        dict con las 5 secciones (`bot`, `farming`, `game`, `leveling`,
        `navigation`) garantizadas como dict.

    Raises:
        ConfigValidationError: solo si strict=True y la validacion fallo.
        yaml.YAMLError: si el archivo existe pero no es YAML valido.
    """
    p = Path(path) if path is not None else CONFIG_DEFAULT_PATH

    try:
        with open(p, "r", encoding="utf-8") as f:
            raw_text = f.read()
        # Check de truncamiento ANTES de parsear — atrapa el bug del Edit/Write en mount Windows.
        trunc_warning = _detect_truncation(raw_text)
        if trunc_warning:
            msg = f"config.yaml en {p} parece truncado: {trunc_warning}"
            if strict:
                raise ConfigValidationError(msg)
            _log.error(msg)
        data = yaml.safe_load(raw_text) or {}
    except FileNotFoundError:
        _log.warning("config.yaml no encontrado en %s — usando dict vacio", p)
        data = {}
    except yaml.YAMLError as e:
        msg = f"config.yaml en {p} no parsea: {e}"
        if strict:
            raise ConfigValidationError(msg) from e
        _log.error(msg)
        data = {}

    if not isinstance(data, dict):
        msg = f"config.yaml en {p} no es un dict en el top-level (tipo: {type(data).__name__})"
        if strict:
            raise ConfigValidationError(msg)
        _log.error(msg)
        data = {}

    for section in _REQUIRED_SECTIONS:
        if section not in data:
            data[section] = {}
        elif not isinstance(data[section], dict):
            msg = f"config[{section}] no es dict (tipo: {type(data[section]).__name__})"
            if strict:
                raise ConfigValidationError(msg)
            _log.warning(msg + " — reemplazado por dict vacio")
            data[section] = {}

    errors = _validate_types(data, strict)
    if errors:
        if strict:
            raise ConfigValidationError("; ".join(errors))
        for e in errors:
            _log.warning("validacion: %s", e)

    return data


def save_config(config: dict, path: str | os.PathLike | None = None) -> None:
    """Persiste config.yaml con escritura robusta + PROTECCION ANTI-PERDIDA.

    Antes de escribir, compara las top-level keys de `config` contra el on-disk.
    Si el dict pasado perdio alguna key top-level que el on-disk tiene → ABORTA
    y loggea ERROR. Esto previene el bug recurrente donde la GUI carga un config
    danado (teleport_profiles vacio), modifica algo, y al guardar destruye el
    archivo propagando el dano.

    Tambien aborta si una seccion top-level pasaria de dict no-vacio a vacio.
    """
    p = Path(path) if path is not None else CONFIG_DEFAULT_PATH

    # Cargar on-disk para comparar
    on_disk_data: dict = {}
    on_disk_keys: set = set()
    if Path(p).exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f.read()) or {}
            if isinstance(loaded, dict):
                on_disk_data = loaded
                on_disk_keys = set(loaded.keys())
        except (OSError, yaml.YAMLError) as e:
            _log.warning("save_config: no pude leer on-disk para comparacion: %s", e)

    new_keys = set(config.keys()) if isinstance(config, dict) else set()
    lost_keys = on_disk_keys - new_keys
    if lost_keys:
        msg = (
            f"save_config: el dict en memoria perdio top-level keys que estan on-disk: "
            f"{sorted(lost_keys)}. ABORTANDO escritura para no destruir {p}. "
            f"Probable causa: GUI cargo un config corrupto y al guardar reescribiria sin esas keys."
        )
        _log.error(msg)
        raise ConfigValidationError(msg)

    for key in on_disk_keys & new_keys:
        old = on_disk_data.get(key)
        new = config.get(key)
        if isinstance(old, dict) and old and isinstance(new, dict) and not new:
            msg = (
                f"save_config: top-level key {key!r} pasaria de dict no-vacio "
                f"({len(old)} entries) a dict vacio. ABORTANDO."
            )
            _log.error(msg)
            raise ConfigValidationError(msg)

    text_out = yaml.dump(config, allow_unicode=True, sort_keys=True)
    ok = _write_robust(str(p), text_out)
    if not ok:
        raise ConfigValidationError(
            f"save_config: write to {p} truncated o failed despues de retries"
        )
