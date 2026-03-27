import random
import time
from screen import Screen
from detector import Detector
from actions import Actions


class Bot:
    def __init__(self, config: dict):
        self.config = config
        self.screen = Screen(config["game"]["window_title"])
        self.detector = Detector()
        self.actions = Actions(config["bot"])
        self.state = "idle"

    def tick(self):
        frame = self.screen.capture()

        if self.state == "idle":
            self._look_for_resources(frame)
        elif self.state == "moving":
            self._wait_movement()
        elif self.state == "collecting":
            self._collect()

    def _look_for_resources(self, frame):
        for resource in self.config["farming"]["resources"]:
            pos = self.detector.find_resource(frame, resource)
            if pos:
                print(f"[BOT] Recurso '{resource}' encontrado en {pos}")
                self.actions.click(pos)
                self.state = "collecting"
                return
        # Sin recursos: cambiar de mapa (placeholder)
        print("[BOT] Sin recursos en este mapa, esperando...")

    def _collect(self):
        delay = random.uniform(
            self.config["bot"]["delay_min"],
            self.config["bot"]["delay_max"]
        )
        time.sleep(delay)
        self.state = "idle"

    def _wait_movement(self):
        time.sleep(1.5)
        self.state = "idle"
