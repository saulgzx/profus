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

    def click_title_bar_for_focus(self) -> bool:
        """Click físico en la barra de título de Dofus para garantizar foco.

        Diseñado como "belt-and-suspenders" sobre `ensure_focus()`. La barra
        de título es manejada por el SO (no por Dofus), así que clickearla:
        - Activa la ventana (foreground garantizado).
        - NO dispara ninguna acción del juego (no hay celda ni HUD ahí).
        - Mueve el cursor a una zona segura ANTES de los siguientes inputs.

        Estrategia:
        - Compara `get_window_region()` (window completa con título+bordes)
          contra `get_window_client_region()` (solo client area). La diferencia
          en `top` es la altura del título.
        - Si no se puede inferir título (window sin decoración / fullscreen),
          devuelve False y el caller debe caer en `ensure_focus()` solo.

        Costo: ~50 ms (move + click + breve pausa). Llamarlo una vez por turno,
        no por cast.
        """
        try:
            outer = self.get_window_region()
            client = self.get_window_client_region()
            if not outer or not client:
                return False
            outer_top = int(outer.get("top", 0))
            outer_left = int(outer.get("left", 0))
            outer_w = int(outer.get("width", 0))
            client_top = int(client.get("top", 0))
            title_h = client_top - outer_top
            if title_h < 8:
                # No hay título visible (borderless / fullscreen). Abortar
                # para no clickear dentro del game area por accidente.
                return False
            # Click 5 px abajo del borde superior, centrado horizontalmente.
            click_x = outer_left + outer_w // 2
            click_y = outer_top + min(max(5, title_h // 2), title_h - 2)
            try:
                pyautogui.moveTo(click_x, click_y, duration=0.05)
                time.sleep(0.02)
                pyautogui.click(click_x, click_y)
                time.sleep(0.05)
            except Exception:
                return False
            return True
        except Exception:
            return False

    def is_foreground(self) -> bool:
        """Devuelve True si la ventana de Dofus es la foreground window.

        Chequeo barato (~0.1ms) usando win32gui.GetForegroundWindow() +
        GetWindowText(). Permite saltar focus_window() (que cuesta ~100ms)
        cuando Dofus ya tiene el foco.

        Si win32gui no está disponible o la consulta falla, devuelve False
        (conservador → ensure_focus disparará focus_window por las dudas).

        Causa raíz histórica (2026-04-25, fight cell 107 map 2881, HP
        1484→1 en 11 turnos): el foco se perdió silenciosamente entre
        placement y combat (probablemente notificación de Windows o GUI
        del bot tomó foco). pyautogui.press("1") fue a la ventana sin foco,
        spell nunca armado, click sobre PJ no produjo GA300, 60+ retries
        muertos sin detectar el problema. La solución es chequear foco
        ANTES de cada keystroke en combate (ver sadida._cast_spell).
        """
        if win32gui is None:
            return False
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            title = (win32gui.GetWindowText(hwnd) or "").strip().lower()
            if not title:
                return False
            # Excluir explícitamente ventanas del propio bot.
            if "autofarm" in title or "sniffer" in title:
                return False
            wanted = str(self.window_title or "").strip().lower()
            if wanted and wanted in title:
                return True
            # Sin window_title configurado: aceptar cualquier ventana cuyo
            # título contenga "dofus" como heurística suelta.
            if not wanted and "dofus" in title:
                return True
            return False
        except Exception:
            return False

    def ensure_focus(self) -> bool:
        """Si Dofus NO es foreground, llama focus_window(). No-op (~0.1ms) si ya lo es.

        Devuelve True si Dofus está (o quedó) en foreground.
        """
        if self.is_foreground():
            return True
        return self.focus_window()

    def focus_window(self) -> bool:
        """Activa la ventana de Dofus y VERIFICA que quedó foreground.

        Antes usaba `pygetwindow.activate()` que falla silenciosamente en
        Windows 10+ por restricciones de SetForegroundWindow. Devolvía True
        pero Dofus NO quedaba al frente → keystrokes y clicks iban a la
        ventana equivocada (combat focus_loss bug 2026-04-25, cell 107
        map 2881 perdió 11 turnos sin enviar UN GA300).

        Estrategia robusta:
        1. Encontrar hwnd de Dofus.
        2. Si está minimizado, restaurar.
        3. Truco Alt-key: presionar Alt para "desbloquear" SetForegroundWindow
           (Windows lo restringe a procesos que tienen foreground o input
           reciente del usuario; el Alt simulado cuenta como input).
        4. SetForegroundWindow(hwnd).
        5. **Verificar** GetForegroundWindow == hwnd. Si NO, fallback a
           click físico en la title-bar para forzar activación al estilo
           usuario.
        """
        hwnd = self._find_dofus_hwnd()
        if not hwnd:
            return self._click_center_fallback()

        try:
            if win32gui is not None:
                # Restaurar si está minimizado
                if win32gui.IsIconic(hwnd):
                    try:
                        import win32con as _wc  # type: ignore
                        win32gui.ShowWindow(hwnd, _wc.SW_RESTORE)
                        time.sleep(0.05)
                    except Exception:
                        pass
                # Truco Alt: presionar y soltar Alt con SendInput evita el
                # foreground-lock de Windows. Sin esto, SetForegroundWindow
                # falla silently y la app queda titilando en taskbar.
                try:
                    pyautogui.keyDown('alt')
                    pyautogui.keyUp('alt')
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                time.sleep(0.04)
                # VERIFICAR que quedó foreground. Si no, fallback brusco.
                try:
                    fg = win32gui.GetForegroundWindow()
                    if int(fg) == int(hwnd):
                        self.refresh_monitor_from_window()
                        return True
                except Exception:
                    pass
                # Fallback: click físico en la title-bar (lo que haría el usuario)
                if self._click_title_bar(hwnd):
                    time.sleep(0.04)
                    try:
                        fg = win32gui.GetForegroundWindow()
                        if int(fg) == int(hwnd):
                            self.refresh_monitor_from_window()
                            return True
                    except Exception:
                        pass
            # gw fallback (sin win32gui)
            if gw is not None:
                for win in gw.getAllWindows():
                    title = str(getattr(win, "title", "") or "").strip().lower()
                    if "autofarm" in title or "sniffer" in title:
                        continue
                    wanted = str(self.window_title or "").strip().lower()
                    if wanted and wanted not in title:
                        continue
                    width = int(getattr(win, "width", 0) or 0)
                    height = int(getattr(win, "height", 0) or 0)
                    if width <= 0 or height <= 0:
                        continue
                    if getattr(win, "isMinimized", False):
                        win.restore()
                        time.sleep(0.05)
                    win.activate()
                    time.sleep(0.05)
                    self.refresh_monitor_from_window()
                    return True
        except Exception:
            pass
        return self._click_center_fallback()

    def _find_dofus_hwnd(self) -> int | None:
        """Devuelve el hwnd de la ventana de Dofus más grande, o None."""
        if win32gui is None:
            return None
        wanted = str(self.window_title or "").strip().lower()
        results: list[tuple[int, int]] = []
        def _enum(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = (win32gui.GetWindowText(hwnd) or "").strip()
                if not title:
                    return
                lowered = title.lower()
                if "autofarm" in lowered or "sniffer" in lowered:
                    return
                if wanted and wanted not in lowered:
                    return
                if not wanted and "dofus" not in lowered:
                    return
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w <= 0 or h <= 0:
                    return
                results.append((w * h, int(hwnd)))
            except Exception:
                return
        try:
            win32gui.EnumWindows(_enum, None)
        except Exception:
            return None
        if not results:
            return None
        results.sort(reverse=True)
        return results[0][1]

    def _click_title_bar(self, hwnd: int) -> bool:
        """Click físico en la barra de título — fuerza activación a estilo usuario."""
        if win32gui is None:
            return False
        try:
            rect = win32gui.GetWindowRect(hwnd)
            cx = (rect[0] + rect[2]) // 2
            cy = rect[1] + 8  # 8 px desde el borde superior
            pyautogui.moveTo(cx, cy, duration=0.04)
            time.sleep(0.02)
            pyautogui.click(cx, cy)
            return True
        except Exception:
            return False

    def _click_center_fallback(self) -> bool:
        """Último recurso: click al centro del monitor para forzar foco."""
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
