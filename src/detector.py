import cv2
import numpy as np
import os


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "templates")


class Detector:
    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold
        self._cache: dict[str, np.ndarray] = {}

    def _load_template(self, name: str, category: str = "resources") -> np.ndarray | None:
        key = f"{category}/{name}"
        if key not in self._cache:
            path = os.path.join(TEMPLATES_DIR, category, f"{name}.png")
            if not os.path.exists(path):
                print(f"[DETECTOR] Template no encontrada: {path}")
                return None
            self._cache[key] = cv2.imread(path)
        return self._cache[key]

    def find(self, frame: np.ndarray, name: str, category: str = "resources"):
        """Busca una imagen template en el frame. Devuelve (x, y) o None."""
        template = self._load_template(name, category)
        if template is None:
            return None

        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val >= self.threshold:
            h, w = template.shape[:2]
            center = (max_loc[0] + w // 2, max_loc[1] + h // 2)
            return center
        return None

    def find_resource(self, frame: np.ndarray, resource_name: str):
        return self.find(frame, resource_name, "resources")

    def find_mob(self, frame: np.ndarray, mob_name: str):
        return self.find(frame, mob_name, "mobs")

    def find_ui(self, frame: np.ndarray, element_name: str):
        return self.find(frame, element_name, "ui")
