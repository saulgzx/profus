import time

import mss
import numpy as np
import pyautogui
from PIL import Image

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    from pywinauto import Application
except Exception:
    Application = None

try:
    import win32gui
except Exception:
    win32gui = None


class Screen:
    def __init__(self, window_title: str, monitor_index: int = 2, game_roi: dict | None = None):
        self.window_title = window_title
        self.sct = mss.mss()
        self.monitors = list(self.sct.monitors[1:])
        self.monitor_index = monitor_index if monitor_index < len(self.sct.monitors) else 1
        self.monitor = self.sct.monitors[monitor_index]
        self.window_region = None
        self.game_roi = game_roi or {
            "left": 0.02,
            "top": 0.04,
            "right": 0.87,
            "bottom": 0.82,
        }
        self.refresh_monitor_from_window()

    def _window_handle_from_candidate(self, win) -> int | None:
        for attr in ("_hWnd", "hWnd", "handle"):
            try:
                value = getattr(win, attr, None)
            except Exception:
                value = None
            if value:
                try:
                    return int(value)
                except Exception:
                    continue
        return None

    def _normalize_region(self, region: dict | None) -> dict | None:
        if not region:
            return None
        try:
            left = int(region["left"])
            top = int(region["top"])
            width = int(region["width"])
            height = int(region["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return {"left": left, "top": top, "width": width, "height": height}

    def get_window_region(self) -> dict | None:
        if gw is None:
            return None
        try:
            windows = gw.getAllWindows()
        except Exception:
            return None
        wanted = str(self.window_title or "").strip().lower()
        candidates = []
        for win in windows:
            try:
                title = str(getattr(win, "title", "") or "").strip()
                width = int(getattr(win, "width", 0) or 0)
                height = int(getattr(win, "height", 0) or 0)
                left = int(getattr(win, "left", 0) or 0)
                top = int(getattr(win, "top", 0) or 0)
            except Exception:
                continue
            lowered = title.lower()
            if width <= 0 or height <= 0:
                continue
            if "autofarm" in lowered or "sniffer" in lowered:
                continue
            if wanted and wanted not in lowered:
                continue
            candidates.append({
                "window": win,
                "title": title,
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "area": width * height,
                "active": bool(getattr(win, "isActive", False)),
            })
        if not candidates:
            return None
        candidates.sort(key=lambda item: (1 if item["active"] else 0, item["area"]), reverse=True)
        best = candidates[0]
        return {
            "left": best["left"],
            "top": best["top"],
            "width": best["width"],
            "height": best["height"],
        }

    def get_window_client_region(self) -> dict | None:
        if gw is None:
            return None
        try:
            windows = gw.getAllWindows()
        except Exception:
            return None
        wanted = str(self.window_title or "").strip().lower()
        candidates = []
        for win in windows:
            try:
                title = str(getattr(win, "title", "") or "").strip()
                width = int(getattr(win, "width", 0) or 0)
                height = int(getattr(win, "height", 0) or 0)
            except Exception:
                continue
            lowered = title.lower()
            if width <= 0 or height <= 0:
                continue
            if "autofarm" in lowered or "sniffer" in lowered:
                continue
            if wanted and wanted not in lowered:
                continue
            hwnd = self._window_handle_from_candidate(win)
            if not hwnd:
                continue
            candidates.append((1 if getattr(win, "isActive", False) else 0, width * height, hwnd))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        hwnd = candidates[0][2]

        if win32gui is not None:
            try:
                left_top = win32gui.ClientToScreen(hwnd, (0, 0))
                right_bottom_client = win32gui.GetClientRect(hwnd)
                client_w = int(right_bottom_client[2] - right_bottom_client[0])
                client_h = int(right_bottom_client[3] - right_bottom_client[1])
                if client_w > 0 and client_h > 0:
                    return {
                        "left": int(left_top[0]),
                        "top": int(left_top[1]),
                        "width": client_w,
                        "height": client_h,
                    }
            except Exception:
                pass

        if Application is not None:
            try:
                app = Application(backend="win32").connect(handle=hwnd)
                wrapper = app.window(handle=hwnd)
                rect = wrapper.client_rect()
                origin = wrapper.client_to_screen((0, 0))
                width = int(rect.width())
                height = int(rect.height())
                if width > 0 and height > 0:
                    return {
                        "left": int(origin[0]),
                        "top": int(origin[1]),
                        "width": width,
                        "height": height,
                    }
            except Exception:
                pass
        return None

    def refresh_monitor_from_window(self) -> dict:
        region = self._normalize_region(self.get_window_client_region()) or self._normalize_region(self.get_window_region())
        if region:
            self.window_region = region
            self.monitor = region
            return region
        self.window_region = None
        self.monitor = self.sct.monitors[self.monitor_index]
        return self.monitor

    def capture_region(self) -> dict:
        return self._normalize_region(self.window_region) or self._normalize_region(self.get_window_region()) or self.monitor

    def game_region(self) -> dict:
        base = self.capture_region()
        left_ratio = float(self.game_roi.get("left", 0.0) or 0.0)
        top_ratio = float(self.game_roi.get("top", 0.0) or 0.0)
        right_ratio = float(self.game_roi.get("right", 1.0) or 1.0)
        bottom_ratio = float(self.game_roi.get("bottom", 1.0) or 1.0)
        left_ratio = min(max(left_ratio, 0.0), 1.0)
        top_ratio = min(max(top_ratio, 0.0), 1.0)
        right_ratio = min(max(right_ratio, left_ratio + 0.01), 1.0)
        bottom_ratio = min(max(bottom_ratio, top_ratio + 0.01), 1.0)
        width = int(base["width"])
        height = int(base["height"])
        left = int(round(base["left"] + width * left_ratio))
        top = int(round(base["top"] + height * top_ratio))
        right = int(round(base["left"] + width * right_ratio))
        bottom = int(round(base["top"] + height * bottom_ratio))
        return {
            "left": left,
            "top": top,
            "width": max(1, right - left),
            "height": max(1, bottom - top),
        }

    def capture(self, region=None) -> np.ndarray:
        """Captura la pantalla y devuelve un array BGR para OpenCV."""
        monitor = self._normalize_region(region) or self.game_region()
        screenshot = self.sct.grab(monitor)
        img = np.ascontiguousarray(np.array(screenshot)[:, :, :3])
        return img

    def capture_pil(self, region=None) -> Image.Image:
        monitor = self._normalize_region(region) or self.game_region()
        screenshot = self.sct.grab(monitor)
        return Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

    def focus_window(self) -> bool:
        """Intenta enfocar la ventana del juego; si falla, hace click al centro del monitor."""
        try:
            if gw is not None:
                windows = gw.getAllWindows()
                wanted = str(self.window_title or "").strip().lower()
                candidates = []
                for win in windows:
                    title = str(getattr(win, "title", "") or "").strip()
                    lowered = title.lower()
                    if "autofarm" in lowered or "sniffer" in lowered:
                        continue
                    if wanted and wanted not in lowered:
                        continue
                    width = int(getattr(win, "width", 0) or 0)
                    height = int(getattr(win, "height", 0) or 0)
                    if width <= 0 or height <= 0:
                        continue
                    candidates.append((width * height, win))
                candidates.sort(key=lambda item: item[0], reverse=True)
                win = candidates[0][1] if candidates else None
            else:
                windows = pyautogui.getWindowsWithTitle(self.window_title)
                win = windows[0] if windows else None
            if win is not None:
                if getattr(win, "isMinimized", False):
                    win.restore()
                    time.sleep(0.05)
                win.activate()
                time.sleep(0.05)
                self.refresh_monitor_from_window()
                return True
        except Exception:
            pass

        try:
            monitor = self.capture_region()
            cx = monitor["left"] + monitor["width"] // 2
            cy = monitor["top"] + monitor["height"] // 2
            pyautogui.click(cx, cy)
            time.sleep(0.05)
            self.refresh_monitor_from_window()
            return True
        except Exception:
            return False

    def parking_regions(self) -> list[dict]:
        regions = []
        for idx, monitor in enumerate(self.monitors, start=1):
            if idx == self.monitor_index:
                continue
            regions.append(monitor)
        if regions:
            return regions
        return [self.monitor]
