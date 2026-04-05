import cv2
import numpy as np
import os


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "templates")


class Detector:
    def __init__(self, threshold: float = 0.8):
        self.threshold = threshold
        self._cache: dict[str, np.ndarray] = {}
        self._missing: set[str] = set()

    def _load_template(self, name: str, category: str = "resources") -> np.ndarray | None:
        key = f"{category}/{name}"
        if key not in self._cache:
            path = os.path.join(TEMPLATES_DIR, category, f"{name}.png")
            if not os.path.exists(path):
                if path not in self._missing:
                    print(f"[DETECTOR] Template no encontrada: {path}")
                    self._missing.add(path)
                return None
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            if img.shape[2] == 4:  # RGBA -> BGR
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            self._cache[key] = img
        return self._cache[key]

    def find(self, frame: np.ndarray, name: str, category: str = "resources"):
        """Devuelve la primera coincidencia (x, y) o None."""
        matches = self.find_all(frame, name, category)
        return matches[0] if matches else None

    def find_all(self, frame: np.ndarray, name: str, category: str = "resources") -> list[tuple[int, int]]:
        """Devuelve todas las coincidencias encontradas como lista de (x, y)."""
        template = self._load_template(name, category)
        if template is None:
            return []

        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        h, w = template.shape[:2]

        locations = np.where(result >= self.threshold)
        points = list(zip(locations[1], locations[0]))  # (x, y)

        # Eliminar duplicados cercanos (non-maximum suppression simple)
        filtered = []
        for x, y in points:
            cx, cy = int(x + w // 2), int(y + h // 2)
            too_close = any(abs(cx - fx) < w and abs(cy - fy) < h for fx, fy in filtered)
            if not too_close:
                filtered.append((cx, cy))

        return filtered

    def best_match(self, frame: np.ndarray, name: str, category: str = "resources") -> tuple[tuple[int, int] | None, float]:
        """Devuelve el mejor centro y score aunque no supere el threshold."""
        template = self._load_template(name, category)
        if template is None:
            return None, 0.0
        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        h, w = template.shape[:2]
        center = (int(max_loc[0] + w // 2), int(max_loc[1] + h // 2))
        return center, float(max_val)

    def template_size(self, name: str, category: str = "resources") -> tuple[int, int] | None:
        template = self._load_template(name, category)
        if template is None:
            return None
        h, w = template.shape[:2]
        return int(w), int(h)

    def find_resource(self, frame: np.ndarray, resource_name: str, profession: str | None = None):
        category = f"resources/{profession}" if profession else "resources"
        return self.find(frame, resource_name, category)

    def find_all_resources(self, frame: np.ndarray, resource_name: str, profession: str | None = None):
        category = f"resources/{profession}" if profession else "resources"
        return self.find_all(frame, resource_name, category)

    def find_mob(self, frame: np.ndarray, mob_name: str):
        return self.find(frame, mob_name, "mobs")

    def find_all_mob_sprites(self, frame: np.ndarray, mob_name: str) -> list[tuple[int, int]]:
        """Busca todas las instancias de un mob usando sus multiples sprites."""
        mob_dir = os.path.join(TEMPLATES_DIR, "mobs", mob_name)
        if not os.path.isdir(mob_dir):
            print(f"[DETECTOR] Carpeta de mob no encontrada: {mob_dir}")
            return []

        all_points = []
        for fname in sorted(os.listdir(mob_dir)):
            if not fname.lower().endswith(".png"):
                continue
            sprite_name = os.path.splitext(fname)[0]
            matches = self.find_all(frame, sprite_name, f"mobs/{mob_name}")
            all_points.extend(matches)

        # NMS global entre todos los sprites (radio fijo de 40px)
        filtered: list[tuple[int, int]] = []
        for cx, cy in all_points:
            if not any(abs(cx - fx) < 40 and abs(cy - fy) < 40 for fx, fy in filtered):
                filtered.append((cx, cy))
        return filtered

    def find_pj_sprites(self, frame: np.ndarray) -> list[tuple[int, int]]:
        """Busca el personaje propio priorizando PJ.png en assets/templates/ui/pj/."""
        pj_dir = os.path.join(TEMPLATES_DIR, "ui", "pj")
        if not os.path.isdir(pj_dir):
            return []
        preferred_path = os.path.join(pj_dir, "PJ.png")
        if os.path.exists(preferred_path):
            return self.find_all(frame, "PJ", "ui/pj")
        all_points = []
        for fname in sorted(os.listdir(pj_dir)):
            if not fname.lower().endswith(".png"):
                continue
            sprite_name = os.path.splitext(fname)[0]
            matches = self.find_all(frame, sprite_name, "ui/pj")
            all_points.extend(matches)
        filtered: list[tuple[int, int]] = []
        for cx, cy in all_points:
            if not any(abs(cx - fx) < 40 and abs(cy - fy) < 40 for fx, fy in filtered):
                filtered.append((cx, cy))
        return filtered

    def find_ui(self, frame: np.ndarray, element_name: str):
        return self.find(frame, element_name, "ui")
