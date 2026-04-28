import os
import importlib

from .base import CombatProfile, CombatContext

from app_logger import get_logger

_log = get_logger("bot.combat")


_COMBAT_DIR = os.path.dirname(__file__)
_SKIP = {"__init__.py", "base.py"}


def _iter_profile_modules():
    for fname in sorted(os.listdir(_COMBAT_DIR)):
        if not fname.endswith(".py") or fname in _SKIP:
            continue
        mod_name = fname[:-3]
        try:
            mod = importlib.import_module(f".{mod_name}", package=__name__)
            if hasattr(mod, "Profile") and hasattr(mod.Profile, "name"):
                yield mod
        except Exception as e:
            _log.warning(f"[COMBAT] Error cargando modulo '{mod_name}': {e}")


def list_profiles() -> list[str]:
    """Devuelve los nombres de todos los perfiles disponibles."""
    return [mod.Profile.name for mod in _iter_profile_modules()]


def load_profile(name: str) -> CombatProfile:
    """Carga un perfil por nombre. Si no existe, devuelve el perfil base."""
    for mod in _iter_profile_modules():
        if mod.Profile.name == name:
            return mod.Profile()
    _log.warning(f"[COMBAT] Perfil '{name}' no encontrado — usando perfil base")
    return CombatProfile()
