import pyautogui
import random
import time


pyautogui.FAILSAFE = True  # Mover mouse a esquina superior izquierda para detener


class Actions:
    def __init__(self, bot_config: dict):
        self.delay_min = bot_config["delay_min"]
        self.delay_max = bot_config["delay_max"]

    def _random_delay(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _jitter(self, x: int, y: int, radius: int = 3) -> tuple[int, int]:
        """Agrega un pequeño offset aleatorio al click para parecer mas humano."""
        return (
            x + random.randint(-radius, radius),
            y + random.randint(-radius, radius),
        )

    def click(self, pos: tuple[int, int], button: str = "left"):
        x, y = self._jitter(*pos)
        pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.5))
        self._random_delay()
        pyautogui.click(x, y, button=button)

    def double_click(self, pos: tuple[int, int]):
        x, y = self._jitter(*pos)
        pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.4))
        pyautogui.doubleClick(x, y)

    def press_key(self, key: str):
        self._random_delay()
        pyautogui.press(key)

    def type_text(self, text: str):
        self._random_delay()
        pyautogui.typewrite(text, interval=random.uniform(0.05, 0.15))
