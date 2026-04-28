import pyautogui
import random
import time


pyautogui.FAILSAFE = True  # Mover mouse a esquina superior izquierda para detener
# pyautogui aplica por default un sleep de 100ms DESPUÉS de cada moveTo/click/press.
# Medido 2026-04-23: sumaba ~200ms a cada quick_click (p50=234ms) — ~10min/sesión
# de puro wait muerto. Nosotros ya tenemos nuestros propios pauses configurables
# (quick_move_*, quick_click_pause_*, quick_key_pause_*) que cubren el tiempo
# que el cliente de Dofus necesita entre inputs. Desactivar PAUSE es seguro.
pyautogui.PAUSE = 0


class Actions:
    def __init__(self, bot_config: dict):
        self.delay_min = bot_config.get("delay_min", 0.1)
        self.delay_max = bot_config.get("delay_max", 0.3)
        self.quick_move_min = bot_config.get("quick_move_min", 0.02)
        self.quick_move_max = bot_config.get("quick_move_max", 0.06)
        self.quick_click_pause_min = bot_config.get("quick_click_pause_min", 0.02)
        self.quick_click_pause_max = bot_config.get("quick_click_pause_max", 0.05)
        self.quick_key_pause_min = bot_config.get("quick_key_pause_min", 0.02)
        self.quick_key_pause_max = bot_config.get("quick_key_pause_max", 0.05)
        self.dismiss_pause_min = bot_config.get("dismiss_pause_min", 0.02)
        self.dismiss_pause_max = bot_config.get("dismiss_pause_max", 0.05)
        self.dismiss_after_click_min = bot_config.get("dismiss_after_click_min", 0.05)
        self.dismiss_after_click_max = bot_config.get("dismiss_after_click_max", 0.1)
        self.park_move_min = bot_config.get("park_move_min", 0.08)
        self.park_move_max = bot_config.get("park_move_max", 0.18)
        self.park_margin = int(bot_config.get("park_margin", 24) or 24)

    def _random_delay(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _jitter(self, x: int, y: int, radius: int = 3) -> tuple[int, int]:
        """Agrega un pequeno offset aleatorio al click para parecer mas humano."""
        return (
            x + random.randint(-radius, radius),
            y + random.randint(-radius, radius),
        )

    def click(self, pos: tuple[int, int], button: str = "left"):
        x, y = self._jitter(*pos)
        pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.5))
        self._random_delay()
        pyautogui.click(x, y, button=button)

    def quick_click(self, pos: tuple[int, int], button: str = "left"):
        """Click rapido para menus contextuales sin delay largo."""
        # Instrumentación perf: opt-in vía config, no-op si deshabilitado.
        _perf_ctx = None
        try:
            from perf import get_perf
            _perf_ctx = get_perf().measure("actions.quick_click",
                                           pos_x=int(pos[0]), pos_y=int(pos[1]),
                                           button=button)
            _perf_ctx.__enter__()
        except Exception:
            _perf_ctx = None
        try:
            x, y = self._jitter(*pos, radius=2)
            pyautogui.moveTo(x, y, duration=random.uniform(self.quick_move_min, self.quick_move_max))
            time.sleep(random.uniform(self.quick_click_pause_min, self.quick_click_pause_max))
            pyautogui.click(x, y, button=button)
        finally:
            if _perf_ctx is not None:
                try:
                    _perf_ctx.__exit__(None, None, None)
                except Exception:
                    pass

    def double_click(self, pos: tuple[int, int]):
        x, y = self._jitter(*pos)
        pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.4))
        pyautogui.doubleClick(x, y)

    def press_key(self, key: str):
        self._random_delay()
        pyautogui.press(key)

    def quick_press_key(self, key: str):
        """Tecla rapida sin delay largo para combate."""
        time.sleep(random.uniform(self.quick_key_pause_min, self.quick_key_pause_max))
        pyautogui.press(key)

    def dismiss_click(self, menu_pos: tuple[int, int]):
        """Click fuera de un menu contextual para cerrarlo."""
        x = menu_pos[0] - 220
        y = menu_pos[1] - 80
        pyautogui.moveTo(x, y, duration=random.uniform(self.quick_move_min, self.quick_move_max))
        time.sleep(random.uniform(self.dismiss_pause_min, self.dismiss_pause_max))
        pyautogui.click(x, y)
        time.sleep(random.uniform(self.dismiss_after_click_min, self.dismiss_after_click_max))

    def type_text(self, text: str):
        self._random_delay()
        pyautogui.typewrite(text, interval=random.uniform(0.05, 0.15))

    def scroll_at(self, pos: tuple[int, int], clicks: int = -3):
        """Rueda del ratón en una posición. clicks negativo = scroll down."""
        x, y = self._jitter(*pos, radius=4)
        pyautogui.moveTo(x, y, duration=random.uniform(self.quick_move_min, self.quick_move_max))
        time.sleep(random.uniform(0.05, 0.12))
        pyautogui.scroll(clicks, x=x, y=y)

    def park_mouse(self, regions: list[dict] | None):
        """Mueve el cursor a una region aleatoria fuera del juego para no tapar la UI."""
        if not regions:
            return
        valid_regions = [region for region in regions if region.get("width", 0) > 10 and region.get("height", 0) > 10]
        if not valid_regions:
            return
        region = random.choice(valid_regions)
        left = int(region.get("left", 0))
        top = int(region.get("top", 0))
        width = int(region.get("width", 0))
        height = int(region.get("height", 0))
        margin_x = max(6, min(self.park_margin, max(6, width // 5)))
        margin_y = max(6, min(self.park_margin, max(6, height // 5)))
        min_x = left + margin_x
        max_x = left + max(margin_x, width - margin_x - 1)
        min_y = top + margin_y
        max_y = top + max(margin_y, height - margin_y - 1)
        if max_x < min_x:
            min_x = left + 6
            max_x = left + max(6, width - 6)
        if max_y < min_y:
            min_y = top + 6
            max_y = top + max(6, height - 6)
        x = random.randint(min_x, max_x)
        y = random.randint(min_y, max_y)
        pyautogui.moveTo(x, y, duration=random.uniform(self.park_move_min, self.park_move_max))
