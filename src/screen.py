import mss
import numpy as np
from PIL import Image


class Screen:
    def __init__(self, window_title: str):
        self.window_title = window_title
        self.sct = mss.mss()

    def capture(self, region=None) -> np.ndarray:
        """Captura la pantalla y devuelve un array BGR para OpenCV."""
        monitor = region if region else self.sct.monitors[1]
        screenshot = self.sct.grab(monitor)
        img = np.array(screenshot)
        # mss devuelve BGRA, convertir a BGR
        return img[:, :, :3]

    def capture_pil(self, region=None) -> Image.Image:
        monitor = region if region else self.sct.monitors[1]
        screenshot = self.sct.grab(monitor)
        return Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
