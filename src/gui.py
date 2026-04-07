import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
elif hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
elif hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import threading
import queue
import time
import math
import traceback
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from PIL import Image, ImageTk
import mss
import numpy as np
import cv2
import yaml
import pyautogui
from pynput import keyboard as kb

from bot import Bot
from detector import Detector
from map_logic import cell_id_to_grid
from combat import load_profile

RESOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "resources")
UI_DIR        = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui")
MOBS_DIR      = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "mobs")

def _list_professions():
    """Devuelve lista de subcarpetas (profesiones) en RESOURCES_DIR."""
    if not os.path.exists(RESOURCES_DIR):
        return []
    return sorted(
        d for d in os.listdir(RESOURCES_DIR)
        if os.path.isdir(os.path.join(RESOURCES_DIR, d))
    )

def _list_mobs():
    """Devuelve lista de subcarpetas (mobs) en MOBS_DIR."""
    if not os.path.exists(MOBS_DIR):
        return []
    return sorted(
        d for d in os.listdir(MOBS_DIR)
        if os.path.isdir(os.path.join(MOBS_DIR, d))
    )
CONFIG_PATH   = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
GUI_ERROR_LOG = os.path.join(os.path.dirname(__file__), "..", "gui_error.log")

BG      = "#1a1a2e"
PANEL   = "#16213e"
ACCENT  = "#0f3460"
GREEN   = "#4ecca3"
RED     = "#e94560"
YELLOW  = "#f5a623"
BLUE    = "#4a9eff"
TEXT    = "#eaeaea"
SUBTEXT = "#8892a4"


def _should_emit_runtime_log(msg: str) -> bool:
    text = str(msg or "").strip()
    if not text:
        return False
    important_markers = ("error", "failed", "traceback", "exception", "fall", "timeout")
    lowered = text.lower()
    if any(marker in lowered for marker in important_markers):
        return True

    noisy_prefixes = (
        "[DIAG]",
        "[HARVEST]",
        "[GRID]",
        "[DETECTOR]",
        "[SNIFFER] GM mapa:",
        "[SNIFFER] Turno de actor ",
        "[SNIFFER] Nuestro turno terminó",
        "[SNIFFER] PA=",
        "[BOT] Sniffer detecta ",
        "[BOT] Sniffer sin mobs probables ",
        "[BOT] Sin mobs visibles ",
    )
    return not text.startswith(noisy_prefixes)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True)


# ================================================================ Captura ==
class ResourceCaptureWindow(tk.Toplevel):
    """Ventana para capturar y recortar un recurso desde pantalla."""

    def __init__(self, parent, monitor_index: int, on_saved, save_dir: str | None = None):
        super().__init__(parent)
        self.title("Capturar recurso")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.on_saved = on_saved
        self.monitor_index = monitor_index
        self.save_dir = save_dir or RESOURCES_DIR

        self._screenshot = None   # PIL Image original
        self._tk_image = None     # ImageTk para canvas
        self._scale = 1.0
        self._rect = None         # ID del rectangulo en canvas
        self._start = None        # (x, y) inicio del drag

        self._build_ui()
        self._take_screenshot()

    def _build_ui(self):
        top = tk.Frame(self, bg=BG, pady=6, padx=10)
        top.pack(fill="x")

        tk.Label(top, text="Arrastra para seleccionar el recurso", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10)).pack(side="left")

        btn_frame = tk.Frame(top, bg=BG)
        btn_frame.pack(side="right")

        tk.Button(btn_frame, text="Nuevo screenshot", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._take_screenshot).pack(side="left", padx=(0, 6))

        tk.Button(btn_frame, text="Guardar recorte", bg=GREEN, fg=BG,
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._save_crop).pack(side="left")

        # Canvas scrollable
        canvas_frame = tk.Frame(self, bg=BG)
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.canvas = tk.Canvas(canvas_frame, bg="#0a0a1a", cursor="crosshair",
                                highlightthickness=0)
        hbar = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        hbar.pack(side="bottom", fill="x")
        vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)

        # Label coordenadas
        self.lbl_coords = tk.Label(self, text="Seleccion: ninguna", bg=BG,
                                   fg=SUBTEXT, font=("Segoe UI", 8))
        self.lbl_coords.pack(pady=(0, 6))

        self._sel = None  # (x1, y1, x2, y2) en coordenadas de imagen original

    def _take_screenshot(self):
        self.withdraw()
        time.sleep(0.4)
        with mss.mss() as sct:
            monitor = sct.monitors[self.monitor_index]
            shot = sct.grab(monitor)
            self._screenshot = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        self.deiconify()
        self._render_screenshot()

    def _render_screenshot(self):
        if not self._screenshot:
            return
        screen_w = self.winfo_screenwidth() - 60
        screen_h = self.winfo_screenheight() - 180
        img_w, img_h = self._screenshot.size
        self._scale = min(screen_w / img_w, screen_h / img_h, 1.0)
        disp_w = int(img_w * self._scale)
        disp_h = int(img_h * self._scale)

        display = self._screenshot.resize((disp_w, disp_h), Image.LANCZOS)
        self._tk_image = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_image)
        self.canvas.configure(scrollregion=(0, 0, disp_w, disp_h))
        self.geometry(f"{min(disp_w + 30, screen_w + 30)}x{min(disp_h + 110, screen_h + 110)}")
        self._sel = None
        self._rect = None
        self.lbl_coords.config(text="Seleccion: ninguna")

    # ---- Mouse events ----
    def _canvas_coords(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        return x, y

    def _on_press(self, event):
        self._start = self._canvas_coords(event)
        if self._rect:
            self.canvas.delete(self._rect)

    def _on_drag(self, event):
        if not self._start:
            return
        x, y = self._canvas_coords(event)
        if self._rect:
            self.canvas.delete(self._rect)
        self._rect = self.canvas.create_rectangle(
            self._start[0], self._start[1], x, y,
            outline=GREEN, width=2, dash=(4, 2)
        )

    def _on_release(self, event):
        if not self._start:
            return
        x2, y2 = self._canvas_coords(event)
        x1, y1 = self._start
        # Normalizar
        rx1, rx2 = sorted([x1, x2])
        ry1, ry2 = sorted([y1, y2])
        # Convertir a coordenadas de imagen original
        s = self._scale
        self._sel = (int(rx1 / s), int(ry1 / s), int(rx2 / s), int(ry2 / s))
        w = self._sel[2] - self._sel[0]
        h = self._sel[3] - self._sel[1]
        self.lbl_coords.config(text=f"Seleccion: {w}x{h}px  ({self._sel[0]},{self._sel[1]}) - ({self._sel[2]},{self._sel[3]})")

    def _save_crop(self):
        if not self._sel:
            messagebox.showwarning("Sin seleccion", "Arrastra para seleccionar un area primero.", parent=self)
            return
        x1, y1, x2, y2 = self._sel
        if (x2 - x1) < 5 or (y2 - y1) < 5:
            messagebox.showwarning("Seleccion muy pequena", "Selecciona un area mas grande.", parent=self)
            return

        name = simpledialog.askstring("Nombre del recurso",
                                      "Nombre del recurso (sin .png):",
                                      parent=self, initialvalue="Trigo")
        if not name:
            return
        name = name.strip()
        os.makedirs(self.save_dir, exist_ok=True)
        save_path = os.path.join(self.save_dir, f"{name}.png")

        if os.path.exists(save_path):
            overwrite = messagebox.askyesno(
                "Ya existe",
                f"'{name}.png' ya existe.\n¿Sobreescribir con el nuevo recorte?",
                parent=self
            )
            if not overwrite:
                return

        crop = self._screenshot.crop((x1, y1, x2, y2))
        crop.save(save_path)
        messagebox.showinfo("Guardado", f"'{name}.png' guardado ({x2-x1}x{y2-y1}px)", parent=self)
        self.on_saved(name)
        self.destroy()


class _PJCaptureWindow(ResourceCaptureWindow):
    """Variante de captura que siempre guarda como PJ.png sin pedir nombre."""

    def _save_crop(self):
        if not self._sel:
            messagebox.showwarning("Sin seleccion", "Arrastra para seleccionar un area primero.", parent=self)
            return
        x1, y1, x2, y2 = self._sel
        if (x2 - x1) < 5 or (y2 - y1) < 5:
            messagebox.showwarning("Seleccion muy pequena", "Selecciona un area mas grande.", parent=self)
            return
        os.makedirs(self.save_dir, exist_ok=True)
        save_path = os.path.join(self.save_dir, "PJ.png")
        crop = self._screenshot.crop((x1, y1, x2, y2))
        crop.save(save_path)
        messagebox.showinfo("Guardado", f"PJ.png guardado ({x2-x1}x{y2-y1}px)", parent=self)
        self.on_saved("PJ")
        self.destroy()


class _MobIconCaptureWindow(ResourceCaptureWindow):
    """Variante de captura que siempre guarda como _icon.png para preview de GUI."""

    def _save_crop(self):
        if not self._sel:
            messagebox.showwarning("Sin seleccion", "Arrastra para seleccionar un area primero.", parent=self)
            return
        x1, y1, x2, y2 = self._sel
        if (x2 - x1) < 5 or (y2 - y1) < 5:
            messagebox.showwarning("Seleccion muy pequena", "Selecciona un area mas grande.", parent=self)
            return
        os.makedirs(self.save_dir, exist_ok=True)
        save_path = os.path.join(self.save_dir, "_icon.png")
        crop = self._screenshot.crop((x1, y1, x2, y2))
        crop.save(save_path)
        messagebox.showinfo("Guardado", f"_icon.png guardado ({x2-x1}x{y2-y1}px)", parent=self)
        self.on_saved("_icon")
        self.destroy()


# ================================================================= BotThread ==
class BotThread(threading.Thread):
    def __init__(self, config, log_queue, test_mode: bool = False):
        super().__init__(daemon=True)
        self.config = config
        self.config.setdefault("bot", {})
        self.config["bot"]["test_mode"] = bool(test_mode)
        self.log_queue = log_queue
        self._paused = False
        self._running = True
        self.bot = None
        self._original_print = None
        self.test_mode = bool(test_mode)

    def run(self):
        self._patch_print()
        try:
            self.bot = Bot(self.config)
            self.log_queue.put(("log", "[BOT] Iniciado"))
            while self._running:
                if not self._paused:
                    try:
                        self.bot.tick()
                    except Exception as e:
                        import traceback
                        error_msg = traceback.format_exc()
                        self.log_queue.put(("log", f"[ERROR FATAL] Bot se detuvo por un error:\n{error_msg}"))
                        break
                loop_sleep = 0.1
                if self.bot and self.bot.sniffer_active:
                    mode = self.bot.config.get("farming", {}).get("mode", "resource")
                    if self.test_mode or mode == "leveling":
                        loop_sleep = 0.015
                    else:
                        loop_sleep = 0.03
                time.sleep(loop_sleep)
        finally:
            if self.bot:
                self.bot.shutdown()
            self._restore_print()
            self.log_queue.put(("log", "[BOT] Detenido"))
            self.log_queue.put(("stopped", None))

    def _patch_print(self):
        import builtins
        if self._original_print is not None:
            return
        self._original_print = builtins.print
        q = self.log_queue
        def patched(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            if _should_emit_runtime_log(msg):
                q.put(("log", msg))
                self._original_print(*args, **kwargs)
        builtins.print = patched

    def _restore_print(self):
        import builtins
        if self._original_print is not None:
            builtins.print = self._original_print
            self._original_print = None

    def pause(self):
        self._paused = not self._paused
        state = "Pausado" if self._paused else "Reanudado"
        self.log_queue.put(("log", f"[BOT] {state}"))
        self.log_queue.put(("paused", self._paused))

    def stop(self):
        self._running = False


# ===================================================================== App ==
class App(tk.Tk):
    def _place_window_on_monitor(self, width: int, height: int, monitor_index: int = 2) -> None:
        try:
            with mss.mss() as sct:
                monitors = list(sct.monitors[1:]) if len(sct.monitors) > 1 else []
                monitors = sorted(
                    monitors,
                    key=lambda mon: (int(mon.get("left", 0)), int(mon.get("top", 0))),
                )
                target_idx = max(0, int(monitor_index) - 1)
                if not monitors or target_idx >= len(monitors):
                    self.geometry(f"{width}x{height}")
                    return
                monitor = monitors[target_idx]
        except Exception:
            self.geometry(f"{width}x{height}")
            return

        x = int(monitor["left"] + max(0, (monitor["width"] - width) // 2))
        y = int(monitor["top"] + max(0, (monitor["height"] - height) // 2))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def __init__(self):
        super().__init__()
        self.report_callback_exception = self._report_tk_callback_exception
        sys.excepthook = self._report_global_exception
        self.title("Dofus AutoFarm")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._place_window_on_monitor(480, 780, monitor_index=2)
        self.minsize(420, 500)
        self.config_data = load_config()
        self.bot_thread = None
        self.log_queue = queue.Queue()
        self.resource_vars = {}
        self.resource_images = {}
        self.resources_frame = None
        self.resource_nodes_frame = None
        self._resource_node_map_var = tk.StringVar(value="-")
        self._resource_node_prof_var = tk.StringVar(value="")
        self._resource_node_res_var = tk.StringVar(value="")
        self._resource_node_wait_var = tk.StringVar(value="7.0")
        self._wait_saved_lbl = None
        self._resource_node_status_var = tk.StringVar(value="Sin map_id actual")
        self._resource_node_prof_cb = None
        self._resource_node_res_cb = None
        self._resource_node_listbox = None
        self._resource_node_check_lbl = None
        self._resource_node_sprite_label = None
        self._resource_node_sprite_name_var = tk.StringVar(value="Sprite: sin recurso")
        self.mob_vars = {}
        self.mob_ignore_vars = {}
        self.mob_template_vars = {}
        self.mob_images = {}
        self._mob_card_collapsed = {}
        self._mob_search_var = tk.StringVar(value="")
        self._mob_search_var.trace_add("write", self._on_mob_search_changed)
        self._mob_search_entry = None
        self.mobs_frame = None
        self._raw_log_cb = None
        self._notebook = None
        self._sniffer_tab = None
        self._sniffer_test_status_var = None
        self._sniffer_summary_var = None
        self._sniffer_copy_status_var = None
        self._sniffer_tree = None
        self._sniffer_tree_entries = {}
        self._sniffer_selection_entry = None
        self._sniffer_samples_tree = None
        self._sniffer_sample_entries = {}
        self._sniffer_sample_selection = None
        self._sniffer_grid_canvas = None
        self._sniffer_grid_status_var = None
        self._sniffer_grid_image = None
        self._sniffer_grid_photo = None
        self._sniffer_grid_show_logic_var = tk.BooleanVar(value=True)
        self._sniffer_grid_scale = 1.0
        self._sniffer_grid_cell_width_var = tk.DoubleVar(value=64.0)
        self._sniffer_grid_cell_height_var = tk.DoubleVar(value=32.0)
        self._sniffer_grid_offset_x_var = tk.DoubleVar(value=0.0)
        self._sniffer_grid_offset_y_var = tk.DoubleVar(value=0.0)
        self._sniffer_grid_cell_width_entry_var = tk.StringVar(value="64.0")
        self._sniffer_grid_cell_height_entry_var = tk.StringVar(value="32.0")
        self._sniffer_grid_offset_x_entry_var = tk.StringVar(value="0.0")
        self._sniffer_grid_offset_y_entry_var = tk.StringVar(value="0.0")
        self._sniffer_grid_last_map_id = None
        self._sniffer_grid_polygons = []
        self._sniffer_events_text = None
        self._sniffer_payload_text = ""
        self._sniffer_body_left = None
        self._sniffer_body_center = None
        self._sniffer_body_right = None
        self._responsive_after_id = None
        self._header_frame = None
        self._header_left = None
        self._header_right = None
        self._main_module_frame = None
        self._main_module_content = None
        self._main_module_title_label = None
        self._main_module_collapsed = False
        self._main_module_cards = None
        self._main_module_form = None
        self._actor_form_label = None
        self._actor_form_entry = None
        self._actor_form_button = None
        self._bottom_frame = None
        self._bottom_controls_host = None
        self._bottom_info_host = None
        self._status_frame = None
        self._log_frame = None
        self._notebook = None
        self._scrollable_tab_canvases = {}
        self._database_frame = None
        self._mob_db_filter_id_var = tk.StringVar(value="Todos")
        self._mob_db_filter_name_var = tk.StringVar(value="Todos")
        self._mob_db_search_name_var = tk.StringVar(value="")
        self._mob_db_sort_var = tk.StringVar(value="ID asc")
        self._player_db_filter_id_var = tk.StringVar(value="Todos")
        self._player_db_filter_name_var = tk.StringVar(value="Todos")
        self._player_db_search_name_var = tk.StringVar(value="")
        self._player_db_sort_var = tk.StringVar(value="ID asc")
        self._app_scroll_canvas = None
        self._app_scroll_body = None
        self._main_runtime_actor_var = tk.StringVar(value="sin configurar")
        self._main_runtime_profile_var = tk.StringVar(value=str(self.config_data.get("bot", {}).get("combat_profile", "-")))
        self._main_runtime_mode_var = tk.StringVar(value=str(self.config_data.get("farming", {}).get("mode", "-")))
        self._main_runtime_map_var = tk.StringVar(value="-")
        self._main_runtime_sniffer_var = tk.StringVar(value="inactivo")

        self._setup_hotkeys()
        self._build_ui()
        self.bind("<Configure>", self._schedule_responsive_layout)
        self.after_idle(self._apply_responsive_layout)
        self._poll_queue()

    def _write_gui_exception(self, label: str, exc_type, exc_value, exc_tb):
        try:
            rendered = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            with open(GUI_ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}\n")
                fh.write(rendered)
                fh.write("\n")
        except Exception:
            pass
        try:
            self._append_log(f"[GUI] {label}: {exc_value}")
        except Exception:
            pass
        try:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set(f"{label}: {exc_value}")
        except Exception:
            pass

    def _report_tk_callback_exception(self, exc_type, exc_value, exc_tb):
        self._write_gui_exception("Tk callback exception", exc_type, exc_value, exc_tb)

    def _report_global_exception(self, exc_type, exc_value, exc_tb):
        self._write_gui_exception("Global exception", exc_type, exc_value, exc_tb)

    def _setup_hotkeys(self):
        def on_press(key):
            if key == kb.Key.f10 and self.bot_thread:
                self.bot_thread.pause()
            elif key == kb.Key.f12:
                self._toggle_bot()
        listener = kb.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()

    # ------------------------------------------------------------------ UI --
    def _sep(self, parent):
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=8)

    def _collapsible_section(self, parent, title, start_collapsed=False):
        """Crea una seccion colapsable. Devuelve (header_frame, content_frame)."""
        state = {"collapsed": start_collapsed}

        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(6, 0))

        arrow = "▶" if start_collapsed else "▼"
        lbl = tk.Label(header, text=f"{arrow} {title}", bg=BG, fg=TEXT,
                       font=("Segoe UI", 10, "bold"), cursor="hand2")
        lbl.pack(side="left")

        content = tk.Frame(parent, bg=BG)
        if not start_collapsed:
            content.pack(fill="x", pady=(4, 0))

        def toggle(e=None):
            if state["collapsed"]:
                content.pack(fill="x", pady=(4, 0))
                lbl.config(text=f"▼ {title}")
            else:
                content.pack_forget()
                lbl.config(text=f"▶ {title}")
            state["collapsed"] = not state["collapsed"]

        lbl.bind("<Button-1>", toggle)
        return header, content

    def _is_descendant_widget(self, widget, ancestor) -> bool:
        current = widget
        while current is not None:
            if str(current) == str(ancestor):
                return True
            parent_name = current.winfo_parent()
            if not parent_name:
                break
            try:
                current = current.nametowidget(parent_name)
            except Exception:
                break
        return False

    def _has_inner_scroll_owner(self, widget, stop_at=None) -> bool:
        scroll_classes = {"Text", "Treeview", "Listbox"}
        current = widget
        while current is not None:
            if stop_at is not None and str(current) == str(stop_at):
                return False
            try:
                if current.winfo_class() in scroll_classes:
                    return True
            except Exception:
                return False
            parent_name = current.winfo_parent()
            if not parent_name:
                break
            try:
                current = current.nametowidget(parent_name)
            except Exception:
                break
        return False

    def _scroll_canvas_from_event(self, canvas, ev):
        if canvas is None or not canvas.winfo_exists():
            return None
        top, bottom = canvas.yview()
        delta = getattr(ev, "delta", 0)
        if delta:
            step = int(-1 * (delta / 120))
        elif getattr(ev, "num", None) == 4:
            step = -1
        elif getattr(ev, "num", None) == 5:
            step = 1
        else:
            step = 0
        if step == 0:
            return None
        if step < 0 and top <= 0.0:
            return "break"
        if step > 0 and bottom >= 1.0:
            return "break"
        canvas.yview_scroll(step, "units")
        return "break"

    def _handle_global_mousewheel(self, ev):
        notebook = self._notebook
        if notebook is None or not notebook.winfo_exists():
            return None
        selected_tab_id = notebook.select()
        if not selected_tab_id:
            return None
        canvas = self._scrollable_tab_canvases.get(selected_tab_id)
        if canvas is None:
            return None
        try:
            selected_tab = notebook.nametowidget(selected_tab_id)
        except Exception:
            return None
        widget = getattr(ev, "widget", None)
        if widget is None or not self._is_descendant_widget(widget, selected_tab):
            return None
        if self._has_inner_scroll_owner(widget, stop_at=selected_tab):
            return None
        return self._scroll_canvas_from_event(canvas, ev)

    def _make_scrollable_tab(self, notebook, title: str):
        """Crea una pestaña con canvas scrolleable. Devuelve (tab_frame, body_frame)."""
        tab = tk.Frame(notebook, bg=BG)
        notebook.add(tab, text=f"  {title}  ")

        canvas = tk.Canvas(tab, bg=BG, highlightthickness=0)
        vscroll = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG, padx=16, pady=10)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        self._scrollable_tab_canvases[str(tab)] = canvas

        return tab, body

    def _build_ui(self):
        # ── Header fijo ──────────────────────────────────────────────────
        header = tk.Frame(self, bg=ACCENT, pady=8)
        header.pack(fill="x")
        self._header_frame = header

        left = tk.Frame(header, bg=ACCENT)
        left.pack(side="left", padx=12)
        self._header_left = left
        tk.Label(left, text="Dofus AutoFarm", bg=ACCENT, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(left, text="F10 Pausa  |  F12 Iniciar/Detener", bg=ACCENT, fg=SUBTEXT,
                 font=("Segoe UI", 7)).pack(anchor="w")

        right = tk.Frame(header, bg=ACCENT)
        right.pack(side="right", padx=12)
        self._header_right = right
        self.btn_test = tk.Button(right, text="Iniciar TEST", bg=BLUE, fg=BG,
                                  font=("Segoe UI", 10, "bold"),
                                  relief="flat", padx=16, pady=6,
                                  cursor="hand2", command=self._start_test_mode)
        self.btn_test.pack(side="left", padx=(0, 6))
        self.btn_toggle = tk.Button(right, text="▶  Iniciar", bg=GREEN, fg=BG,
                                    font=("Segoe UI", 10, "bold"),
                                    relief="flat", padx=16, pady=6,
                                    cursor="hand2", command=self._toggle_bot)
        self.btn_toggle.pack(side="left", padx=(0, 6))
        self.btn_pause = tk.Button(right, text="⏸  Pausar", bg=YELLOW, fg=BG,
                                   font=("Segoe UI", 10, "bold"),
                                   relief="flat", padx=16, pady=6,
                                   cursor="hand2", command=self._pause_bot,
                                   state="disabled")
        self.btn_pause.pack(side="left")

        main_module = tk.Frame(self, bg=PANEL, padx=16, pady=10)
        main_module.pack(fill="x")
        self._main_module_frame = main_module
        self._build_main_module(main_module)

        # ── Notebook (pestañas) ───────────────────────────────────────────
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TNotebook",
                        background=BG, tabmargins=[2, 4, 2, 0])
        style.configure("Dark.TNotebook.Tab",
                        background=ACCENT, foreground=TEXT,
                        font=("Segoe UI", 9, "bold"),
                        padding=[12, 6])
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", PANEL), ("active", "#1a2a50")],
                  foreground=[("selected", GREEN)])

        notebook = ttk.Notebook(self, style="Dark.TNotebook")
        self._notebook = notebook
        notebook.pack(fill="both", expand=True, padx=0, pady=0)
        self.bind_all("<MouseWheel>", self._handle_global_mousewheel, add="+")
        self.bind_all("<Button-4>", self._handle_global_mousewheel, add="+")
        self.bind_all("<Button-5>", self._handle_global_mousewheel, add="+")

        # ── Pestaña 1: Farming ───────────────────────────────────────────
        _, farm_body = self._make_scrollable_tab(notebook, "Farming")

        use_sprite_fallback = self.config_data.get("farming", {}).get("use_sprite_fallback", False)

        _, cnt = self._collapsible_section(farm_body, "Nodos por Mapa (Sniffer)")
        self.resource_nodes_frame = tk.Frame(cnt, bg=PANEL)
        self.resource_nodes_frame.pack(fill="x")
        self._refresh_resource_nodes_editor()

        self._sep(farm_body)

        # Secciones ocultas (instanciadas pero no visibles en UI)
        self.resources_frame = tk.Frame(self, bg=PANEL)
        self._refresh_resources()
        self.ui_check_frame = tk.Frame(self, bg=PANEL)
        self._refresh_ui_checker()

        nav_cfg = self._navigation_cfg()
        _, route_body = self._make_scrollable_tab(notebook, "Rutas")
        self._build_navigation_content(route_body, nav_cfg)

        _, profile_body = self._make_scrollable_tab(notebook, "Perfil")
        self._build_profile_tab(profile_body)

        # ── Pestaña 2: Mobs / Auto-Nivel ─────────────────────────────────
        _, mob_body = self._make_scrollable_tab(notebook, "Mobs / Auto-Nivel")

        self.mobs_frame = tk.Frame(mob_body, bg=BG)
        self.mobs_frame.pack(fill="x")
        self._refresh_mobs()

        _, db_body = self._make_scrollable_tab(notebook, "Base de datos")
        self._database_frame = tk.Frame(db_body, bg=BG)
        self._database_frame.pack(fill="x")
        self._refresh_database_tab()

        _, sniffer_body = self._make_scrollable_tab(notebook, "Sniffer")
        self._build_sniffer_tab(sniffer_body)

        # ── Zona fija inferior (Control + Status + Log) ───────────────────
        bottom = tk.Frame(self, bg=BG, padx=16, pady=6)
        bottom.pack(fill="x", side="bottom")
        self._bottom_frame = bottom

        ttk.Separator(bottom, orient="horizontal").pack(fill="x", pady=(0, 6))
        bottom_content = tk.Frame(bottom, bg=BG)
        bottom_content.pack(fill="x")
        controls_host = tk.Frame(bottom_content, bg=BG)
        info_host = tk.Frame(bottom_content, bg=BG)
        self._bottom_controls_host = controls_host
        self._bottom_info_host = info_host
        _, cnt = self._collapsible_section(controls_host, "Control")
        self._build_controls(cnt)
        ttk.Separator(info_host, orient="horizontal").pack(fill="x", pady=(0, 6))
        self._build_status(info_host)
        self._build_log(info_host)

    def _build_resources(self, parent):
        self.resources_frame = tk.Frame(parent, bg=PANEL)
        self.resources_frame.pack(fill="x")
        self._refresh_resources()

    def _refresh_resources(self):
        for w in self.resources_frame.winfo_children():
            w.destroy()
        self.resource_vars.clear()
        self.resource_images.clear()
        use_sprite_fallback = self.config_data.get("farming", {}).get("use_sprite_fallback", False)

        professions = _list_professions()
        active_config = self.config_data["farming"].get("professions", {})

        if not use_sprite_fallback:
            tk.Label(
                self.resources_frame,
                text="El farmeo automatico usa map_id + nodos + validacion visual local. Aqui puedes capturar sprites y chequear cuantos recursos visibles hay como herramienta de validacion.",
                bg=PANEL,
                fg=YELLOW,
                font=("Segoe UI", 9, "italic"),
                wraplength=380,
                justify="left",
            ).pack(fill="x", padx=10, pady=(8, 4))

        if not professions:
            tk.Label(self.resources_frame, text="Sin profesiones — crea una para comenzar",
                     bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 9)).pack(padx=10, pady=8)
        else:
            for prof_name in professions:
                prof_dir = os.path.join(RESOURCES_DIR, prof_name)
                active_resources = active_config.get(prof_name, {}).get("resources", [])

                lf = tk.LabelFrame(self.resources_frame, text=f"  {prof_name}  ",
                                   bg=PANEL, fg=GREEN,
                                   font=("Segoe UI", 9, "bold"), bd=1, relief="groove")
                lf.pack(fill="x", padx=10, pady=(6, 2))

                pngs = sorted(f for f in os.listdir(prof_dir) if f.lower().endswith(".png"))
                for png in pngs:
                    res_name = os.path.splitext(png)[0]
                    key = (prof_name, res_name)
                    var = tk.BooleanVar(value=res_name in active_resources)
                    self.resource_vars[key] = var

                    row = tk.Frame(lf, bg=PANEL)
                    row.pack(fill="x", padx=8, pady=3)

                    try:
                        img = Image.open(os.path.join(prof_dir, png))
                        img.thumbnail((36, 36), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(img)
                        self.resource_images[key] = photo
                        tk.Label(row, image=photo, bg=PANEL).pack(side="left", padx=(0, 6))
                    except Exception:
                        pass

                    tk.Label(row, text=res_name, bg=PANEL, fg=TEXT,
                             font=("Segoe UI", 10)).pack(side="left")

                    tk.Checkbutton(row, variable=var, bg=PANEL, activebackground=PANEL,
                                   fg=GREEN, selectcolor=PANEL,
                                   command=self._update_resources).pack(side="right")

                    lbl_count = tk.Label(row, text="", bg=PANEL, fg=GREEN,
                                         font=("Segoe UI", 8, "bold"), width=5)
                    lbl_count.pack(side="right", padx=(0, 2))

                    tk.Button(row, text="Chequear", bg=YELLOW, fg=BG,
                              font=("Segoe UI", 7), relief="flat", padx=6, pady=2,
                              cursor="hand2",
                              command=lambda n=res_name, p=prof_name, lbl=lbl_count:
                                  self._check_resource(n, p, lbl)).pack(side="right", padx=(4, 0))

                    tk.Button(row, text="Recapturar", bg=ACCENT, fg=SUBTEXT,
                              font=("Segoe UI", 7), relief="flat", padx=6, pady=2,
                              cursor="hand2",
                              command=lambda n=res_name, p=prof_name:
                                  self._open_capture(prefill=n, profession=p)).pack(side="right", padx=(4, 0))

                    tk.Button(row, text="Eliminar", bg=RED, fg=TEXT,
                              font=("Segoe UI", 7), relief="flat", padx=6, pady=2,
                              cursor="hand2",
                              command=lambda n=res_name, p=prof_name:
                                  self._delete_resource(p, n)).pack(side="right", padx=(4, 0))

                # Boton agregar recurso a esta profesion
                add_row = tk.Frame(lf, bg=PANEL)
                add_row.pack(fill="x", padx=8, pady=(2, 6))
                tk.Button(add_row, text=f"+ Capturar recurso", bg=BLUE, fg=BG,
                          font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                          cursor="hand2",
                          command=lambda p=prof_name: self._open_capture(profession=p)).pack(side="left")

        # Boton nueva profesion
        new_row = tk.Frame(self.resources_frame, bg=PANEL)
        new_row.pack(fill="x", padx=10, pady=(4, 6))
        tk.Button(new_row, text="+ Nueva profesion", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=3,
                  cursor="hand2", command=self._new_profession).pack(side="left")

        if self.resource_nodes_frame is not None:
            self._refresh_resource_nodes_editor()

    def _build_ui_checker(self, parent):
        self.ui_check_frame = tk.Frame(parent, bg=PANEL)
        self.ui_check_frame.pack(fill="x")
        self._refresh_ui_checker()

    def _resource_profession_names(self):
        return sorted(self.config_data.get("farming", {}).get("professions", {}).keys())

    def _resource_names_for_profession(self, profession: str):
        prof_cfg = self.config_data.get("farming", {}).get("professions", {}).get(profession, {})
        return sorted(prof_cfg.get("resources", []))

    def _current_runtime_map_id(self):
        if self.bot_thread and self.bot_thread.bot:
            return self.bot_thread.bot._current_map_id
        return None

    def _selected_resource_node_map_id(self):
        raw = (self._resource_node_map_var.get() or "").strip()
        if raw in ("", "-"):
            return None
        return raw

    def _refresh_resource_nodes_editor(self):
        for w in self.resource_nodes_frame.winfo_children():
            w.destroy()
        # Resetear referencias a widgets destruidos para evitar TclError
        self._resource_node_sprite_label = None

        frame = self.resource_nodes_frame

        help_row = tk.Frame(frame, bg=PANEL)
        help_row.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(
            help_row,
            text="Esta herramienta usa map_id del sniffer + sprite del recurso seleccionado. Puedes chequear visibles y guardar automaticamente los nodos detectados en el mapa actual.",
            bg=PANEL,
            fg=SUBTEXT,
            font=("Segoe UI", 8, "italic"),
            wraplength=380,
            justify="left",
        ).pack(anchor="w")

        route_row = tk.Frame(frame, bg=PANEL)
        route_row.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(route_row, text="Ruta asignada a recursos:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        route_names = sorted(self._route_profiles_cfg().keys())
        farming_cfg = self.config_data.setdefault("farming", {})
        default_route = farming_cfg.get("route_profile") or (route_names[0] if route_names else "")
        self._resource_route_var = tk.StringVar(value=default_route)
        self._resource_route_cb = ttk.Combobox(route_row, textvariable=self._resource_route_var,
                                               state="readonly", width=20, values=route_names)
        self._resource_route_cb.pack(side="left", padx=(8, 4))
        self._resource_route_cb.bind("<<ComboboxSelected>>", lambda e: self._save_resource_route_profile())
        tk.Button(route_row, text="Guardar", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._save_resource_route_profile).pack(side="left")

        top = tk.Frame(frame, bg=PANEL)
        top.pack(fill="x", padx=10, pady=(8, 4))

        tk.Label(top, text="Map ID:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(top, textvariable=self._resource_node_map_var, bg=PANEL, fg=GREEN,
                 font=("Consolas", 10, "bold")).pack(side="left", padx=(6, 0))
        tk.Button(top, text="Usar actual", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._use_current_map_id_for_nodes).pack(side="right")

        status = tk.Frame(frame, bg=PANEL)
        status.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(status, textvariable=self._resource_node_status_var, bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8, "italic")).pack(anchor="w")

        form = tk.Frame(frame, bg=PANEL)
        form.pack(fill="x", padx=10, pady=(2, 4))

        tk.Label(form, text="Profesion:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self._resource_node_prof_cb = ttk.Combobox(
            form,
            textvariable=self._resource_node_prof_var,
            values=self._resource_profession_names(),
            state="readonly",
            width=18,
            font=("Segoe UI", 9),
        )
        self._resource_node_prof_cb.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._resource_node_prof_cb.bind("<<ComboboxSelected>>", lambda e: (
            self._refresh_resource_node_resource_choices(),
            self._load_profession_wait(),
        ))
        tk.Button(form, text="+", bg=ACCENT, fg=GREEN,
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=6, pady=0,
                  cursor="hand2", command=self._new_profession_from_nodes
                  ).grid(row=0, column=2, padx=(4, 0))

        tk.Label(form, text="Recurso:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._resource_node_res_cb = ttk.Combobox(
            form,
            textvariable=self._resource_node_res_var,
            values=[],
            state="readonly",
            width=18,
            font=("Segoe UI", 9),
        )
        self._resource_node_res_cb.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        self._resource_node_res_cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_resource_node_sprite_preview())
        tk.Button(form, text="+", bg=ACCENT, fg=GREEN,
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=6, pady=0,
                  cursor="hand2", command=self._add_resource_to_profession
                  ).grid(row=1, column=2, padx=(4, 0), pady=(6, 0))

        tk.Label(form, text="Espera cosecha (s):", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=(6, 0))
        wait_row = tk.Frame(form, bg=PANEL)
        wait_row.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))
        wait_spin = tk.Spinbox(
            wait_row,
            textvariable=self._resource_node_wait_var,
            from_=1.0, to=60.0, increment=0.5,
            width=7, font=("Segoe UI", 9),
            bg=ACCENT, fg=TEXT, buttonbackground=ACCENT,
            relief="flat",
            command=self._save_profession_wait,
        )
        wait_spin.pack(side="left")
        wait_spin.bind("<FocusOut>", lambda e: self._save_profession_wait())
        wait_spin.bind("<Return>",   lambda e: self._save_profession_wait())
        tk.Button(wait_row, text="Guardar", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._save_profession_wait
                  ).pack(side="left", padx=(6, 0))
        self._wait_saved_lbl = tk.Label(wait_row, text="", bg=PANEL, fg=GREEN,
                                        font=("Segoe UI", 8))
        self._wait_saved_lbl.pack(side="left", padx=(6, 0))
        form.grid_columnconfigure(1, weight=1)

        if not self._resource_node_prof_var.get():
            profs = self._resource_profession_names()
            if profs:
                self._resource_node_prof_var.set(profs[0])
        self._refresh_resource_node_resource_choices()
        self._load_profession_wait()

        sprite_frame = tk.Frame(frame, bg=PANEL)
        sprite_frame.pack(fill="x", padx=10, pady=(0, 6))
        self._resource_node_sprite_label = tk.Label(
            sprite_frame,
            bg=ACCENT,
            width=72,
            height=72,
            relief="flat",
        )
        self._resource_node_sprite_label.pack(side="left")
        sprite_meta = tk.Frame(sprite_frame, bg=PANEL)
        sprite_meta.pack(side="left", fill="x", expand=True, padx=(10, 0))
        tk.Label(
            sprite_meta,
            textvariable=self._resource_node_sprite_name_var,
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            sprite_meta,
            text="El sprite se usa para validar presencia local y para capturar visibles del mapa.",
            bg=PANEL,
            fg=SUBTEXT,
            font=("Segoe UI", 8),
            justify="left",
            wraplength=260,
        ).pack(anchor="w", pady=(4, 0))

        btns = tk.Frame(frame, bg=PANEL)
        btns.pack(fill="x", padx=10, pady=(4, 6))
        tk.Button(btns, text="Capturar sprite", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._capture_selected_resource_sprite).pack(side="left", padx=(0, 4))
        tk.Button(btns, text="Recapturar sprite", bg=ACCENT, fg=SUBTEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._recapture_selected_resource_sprite).pack(side="left", padx=(0, 4))
        tk.Button(btns, text="Chequear sprite", bg=YELLOW, fg=BG,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._check_selected_resource_on_map).pack(side="left", padx=(0, 4))

        btns2 = tk.Frame(frame, bg=PANEL)
        btns2.pack(fill="x", padx=10, pady=(0, 6))
        tk.Button(btns2, text="Capturar visibles", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._capture_visible_resource_nodes).pack(side="left", padx=(0, 4))
        tk.Button(btns2, text="Chequear en mapa", bg=YELLOW, fg=BG,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._check_selected_resource_on_map).pack(side="left", padx=(0, 4))
        tk.Button(btns2, text="Eliminar nodo", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._remove_resource_node).pack(side="left")

        check_row = tk.Frame(frame, bg=PANEL)
        check_row.pack(fill="x", padx=10, pady=(0, 6))
        self._resource_node_check_lbl = tk.Label(
            check_row,
            text="Chequeo visual: pendiente",
            bg=PANEL,
            fg=SUBTEXT,
            font=("Segoe UI", 8, "italic"),
        )
        self._resource_node_check_lbl.pack(anchor="w")

        list_wrap = tk.Frame(frame, bg=PANEL)
        list_wrap.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(list_wrap, text="Nodos guardados para este mapa:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(anchor="w")

        lb_frame = tk.Frame(list_wrap, bg=PANEL)
        lb_frame.pack(fill="x", pady=(2, 0))
        self._resource_node_listbox = tk.Listbox(
            lb_frame,
            height=5,
            bg=ACCENT,
            fg=TEXT,
            font=("Consolas", 9),
            relief="flat",
            selectbackground=GREEN,
            selectforeground=BG,
            activestyle="none",
        )
        self._resource_node_listbox.pack(side="left", fill="x", expand=True)
        lb_scroll = ttk.Scrollbar(lb_frame, orient="vertical", command=self._resource_node_listbox.yview)
        self._resource_node_listbox.configure(yscrollcommand=lb_scroll.set)
        lb_scroll.pack(side="right", fill="y")

        self._refresh_resource_node_list()
        self._refresh_resource_node_sprite_preview()

    def _refresh_resource_node_resource_choices(self):
        profession = self._resource_node_prof_var.get().strip()
        resources = self._resource_names_for_profession(profession)
        if self._resource_node_res_cb is not None:
            self._resource_node_res_cb["values"] = resources
        if self._resource_node_res_var.get() not in resources:
            self._resource_node_res_var.set(resources[0] if resources else "")
        self._refresh_resource_node_sprite_preview()

    def _resource_sprite_path(self, profession: str, resource: str) -> str:
        return os.path.join(RESOURCES_DIR, profession, f"{resource}.png")

    def _refresh_resource_node_sprite_preview(self):
        if self._resource_node_sprite_label is None:
            return
        if not self._resource_node_sprite_label.winfo_exists():
            self._resource_node_sprite_label = None
            return
        profession = self._resource_node_prof_var.get().strip()
        resource = self._resource_node_res_var.get().strip()
        if not profession or not resource:
            self._resource_node_sprite_label.config(image="", text="")
            self._resource_node_sprite_name_var.set("Sprite: sin recurso")
            return
        sprite_path = self._resource_sprite_path(profession, resource)
        if not os.path.exists(sprite_path):
            self._resource_node_sprite_label.config(image="", text="Sin\nsprite", fg=SUBTEXT)
            self._resource_node_sprite_name_var.set(f"Sprite: {profession}/{resource} no capturado")
            return
        try:
            img = Image.open(sprite_path)
            img.thumbnail((72, 72), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.resource_images[("node_preview", profession, resource)] = photo
            self._resource_node_sprite_label.config(image=photo, text="")
            self._resource_node_sprite_name_var.set(f"Sprite: {profession}/{resource}")
        except Exception:
            self._resource_node_sprite_label.config(image="", text="Error", fg=RED)
            self._resource_node_sprite_name_var.set(f"Sprite: error cargando {profession}/{resource}")

    def _resource_nodes_cfg(self):
        farming = self.config_data.setdefault("farming", {})
        return farming.setdefault("resource_nodes_by_map_id", {})

    def _navigation_cfg(self):
        nav = self.config_data.setdefault("navigation", {})
        if "route_profiles" not in nav:
            nav["route_profiles"] = {
                "Default": {
                    "route": list(nav.get("route", []) or []),
                    "route_by_map_id": dict(nav.get("route_by_map_id", {}) or {}),
                    "route_exit_by_map_id": dict(nav.get("route_exit_by_map_id", {}) or {}),
                }
            }
        return nav

    def _route_profiles_cfg(self):
        nav = self._navigation_cfg()
        profiles = nav.setdefault("route_profiles", {})
        if not profiles:
            profiles["Default"] = {"route": [], "route_by_map_id": {}, "route_exit_by_map_id": {}}
        return profiles

    def _sync_runtime_bot_config(self):
        if not self.bot_thread or not self.bot_thread.bot:
            return
        bot = self.bot_thread.bot
        bot.config = load_config()
        configured_actor = bot.config.get("bot", {}).get("actor_id")
        bot._configured_actor_id = str(configured_actor).strip() if configured_actor not in (None, "") else None
        if bot._configured_actor_id:
            bot._set_my_actor_id(bot._configured_actor_id, "gui_actor_id")
        profile_name = bot.config.get("bot", {}).get("combat_profile", bot.combat_profile.name)
        bot.combat_profile = load_profile(profile_name)

    def _build_main_module(self, parent):
        title = tk.Label(parent, text="▼ Modulo principal", bg=PANEL, fg=GREEN,
                         font=("Segoe UI", 10, "bold"), cursor="hand2")
        title.pack(anchor="w")
        title.bind("<Button-1>", lambda _e: self._toggle_main_module())
        self._main_module_title_label = title

        content = tk.Frame(parent, bg=PANEL)
        content.pack(fill="x")
        self._main_module_content = content

        tk.Label(
            content,
            text="Selecciona el Actor ID del PJ actual. Si cambias de personaje, actualizalo aqui.",
            bg=PANEL,
            fg=SUBTEXT,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        cards = tk.Frame(content, bg=PANEL)
        cards.pack(fill="x", pady=(0, 8))
        self._main_module_cards = cards
        self._build_main_summary_cards(cards)

        actor_row = tk.Frame(content, bg=PANEL)
        actor_row.pack(fill="x")
        self._main_module_form = actor_row
        actor_label = tk.Label(actor_row, text="Actor ID del PJ actual:", bg=PANEL, fg=TEXT,
                               font=("Segoe UI", 9))
        actor_label.pack(side="left")
        self._actor_form_label = actor_label
        current_actor_id = str(self.config_data.get("bot", {}).get("actor_id", "") or "")
        self._primary_actor_id_var = tk.StringVar(value=current_actor_id)
        actor_entry = tk.Entry(
            actor_row,
            textvariable=self._primary_actor_id_var,
            width=16,
            bg=BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
        )
        actor_entry.pack(side="left", padx=(8, 6))
        self._actor_form_entry = actor_entry
        actor_button = tk.Button(
            actor_row,
            text="Guardar",
            bg=GREEN,
            fg=BG,
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=self._save_primary_actor_id,
        )
        actor_button.pack(side="left")
        self._actor_form_button = actor_button

        self._primary_actor_status_var = tk.StringVar(
            value=f"Actual: {current_actor_id or 'sin configurar'}"
        )
        tk.Label(content, textvariable=self._primary_actor_status_var, bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8, "italic")).pack(anchor="w", pady=(6, 0))

    def _set_main_module_collapsed(self, collapsed: bool):
        self._main_module_collapsed = bool(collapsed)
        if self._main_module_title_label is not None:
            arrow = "▶" if self._main_module_collapsed else "▼"
            actor_value = self._main_runtime_actor_var.get() or "sin configurar"
            profile_value = self._main_runtime_profile_var.get() or "-"
            mode_value = self._main_runtime_mode_var.get() or "-"
            summary = f"  |  actor={actor_value}  |  perfil={profile_value}  |  modo={mode_value}"
            self._main_module_title_label.config(
                text=f"{arrow} Modulo principal{summary if self._main_module_collapsed else ''}"
            )
        if self._main_module_content is not None:
            if self._main_module_collapsed:
                self._main_module_content.pack_forget()
            else:
                self._main_module_content.pack(fill="x")

    def _toggle_main_module(self):
        self._set_main_module_collapsed(not self._main_module_collapsed)

    def _build_main_summary_cards(self, parent):
        cards = [
            ("Actor activo", self._main_runtime_actor_var, GREEN),
            ("Perfil", self._main_runtime_profile_var, BLUE),
            ("Modo", self._main_runtime_mode_var, YELLOW),
            ("Map ID", self._main_runtime_map_var, TEXT),
            ("Sniffer", self._main_runtime_sniffer_var, GREEN),
        ]
        for title, value_var, color in cards:
            card = tk.Frame(parent, bg=BG, padx=10, pady=8, bd=1, relief="flat")
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(card, text=title, bg=BG, fg=SUBTEXT, font=("Segoe UI", 8, "bold")).pack(anchor="w")
            tk.Label(card, textvariable=value_var, bg=BG, fg=color, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 0))

    def _schedule_responsive_layout(self, _event=None):
        if self._responsive_after_id is not None:
            try:
                self.after_cancel(self._responsive_after_id)
            except Exception:
                pass
        self._responsive_after_id = self.after(80, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        self._responsive_after_id = None
        width = max(1, int(self.winfo_width() or 0))

        if self._header_left is not None and self._header_right is not None:
            self._header_left.pack_forget()
            self._header_right.pack_forget()
            if width < 760:
                self._header_left.pack(fill="x", padx=12, anchor="w")
                self._header_right.pack(fill="x", padx=12, pady=(8, 0), anchor="w")
                for button in (self.btn_test, self.btn_toggle, self.btn_pause):
                    button.pack_configure(side="top", fill="x", padx=0, pady=(0, 6))
            else:
                self._header_left.pack(side="left", padx=12)
                self._header_right.pack(side="right", padx=12)
                self.btn_test.pack_configure(side="left", fill="none", padx=(0, 6), pady=0)
                self.btn_toggle.pack_configure(side="left", fill="none", padx=(0, 6), pady=0)
                self.btn_pause.pack_configure(side="left", fill="none", padx=0, pady=0)

        if self._main_module_content is not None:
            if width < 700 and not self._main_module_collapsed:
                self._set_main_module_collapsed(True)
            elif width >= 700 and self._main_module_collapsed:
                self._set_main_module_collapsed(False)

        if self._main_module_cards is not None:
            card_widgets = list(self._main_module_cards.winfo_children())
            for idx, card in enumerate(card_widgets):
                card.pack_forget()
                if width < 760:
                    card.pack(fill="x", expand=True, padx=0, pady=(0, 6))
                else:
                    padx = (0, 8) if idx < len(card_widgets) - 1 else 0
                    card.pack(side="left", fill="x", expand=True, padx=padx, pady=0)

        if self._main_module_form is not None and self._actor_form_label is not None and self._actor_form_entry is not None and self._actor_form_button is not None:
            self._actor_form_label.pack_forget()
            self._actor_form_entry.pack_forget()
            self._actor_form_button.pack_forget()
            if width < 720:
                self._actor_form_label.pack(anchor="w")
                self._actor_form_entry.pack(fill="x", pady=(6, 6))
                self._actor_form_button.pack(anchor="w")
            else:
                self._actor_form_label.pack(side="left")
                self._actor_form_entry.pack(side="left", padx=(8, 6))
                self._actor_form_button.pack(side="left")

        if self._bottom_controls_host is not None and self._bottom_info_host is not None:
            self._bottom_controls_host.pack_forget()
            self._bottom_info_host.pack_forget()
            if width < 980:
                self._bottom_controls_host.pack(fill="x")
                self._bottom_info_host.pack(fill="x", pady=(10, 0))
            else:
                self._bottom_controls_host.pack(side="left", fill="x", expand=True, padx=(0, 10))
                self._bottom_info_host.pack(side="left", fill="both", expand=True)

        if self._sniffer_body_left is not None and self._sniffer_body_center is not None and self._sniffer_body_right is not None:
            self._sniffer_body_left.pack_forget()
            self._sniffer_body_center.pack_forget()
            self._sniffer_body_right.pack_forget()
            if width < 1180:
                self._sniffer_body_left.pack(fill="both", expand=True, pady=(0, 10))
                self._sniffer_body_center.pack(fill="both", expand=True, pady=(0, 10))
                self._sniffer_body_right.pack(fill="both", expand=True)
            else:
                self._sniffer_body_left.pack(side="left", fill="both", expand=True)
                self._sniffer_body_center.pack(side="left", fill="both", expand=True, padx=(10, 0))
                self._sniffer_body_right.pack(side="left", fill="both", expand=True, padx=(10, 0))

    def _refresh_main_runtime_summary(self):
        bot_cfg = self.config_data.get("bot", {})
        farming_cfg = self.config_data.get("farming", {})
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        actor_value = str(bot_cfg.get("actor_id", "") or "sin configurar")
        profile_value = str(bot_cfg.get("combat_profile", "-") or "-")
        mode_value = str(farming_cfg.get("mode", "-") or "-")
        map_value = "-"
        sniffer_value = "configurado" if bool(bot_cfg.get("sniffer_enabled", False)) else "inactivo"
        if bot is not None:
            actor_value = str(getattr(bot, "_sniffer_my_actor", None) or actor_value)
            profile_value = str(getattr(getattr(bot, "combat_profile", None), "name", profile_value) or profile_value)
            mode_value = str(bot.config.get("farming", {}).get("mode", mode_value) or mode_value)
            current_map = getattr(bot, "_current_map_id", None)
            map_value = str(current_map) if current_map is not None else "-"
            sniffer_value = "activo" if bot.sniffer_active else sniffer_value
        self._main_runtime_actor_var.set(actor_value)
        self._main_runtime_profile_var.set(profile_value)
        self._main_runtime_mode_var.set(mode_value)
        self._main_runtime_map_var.set(map_value)
        self._main_runtime_sniffer_var.set(sniffer_value)

    def _refresh_resource_node_list(self):
        if self._resource_node_listbox is None:
            return
        self._resource_node_listbox.delete(0, "end")
        map_id = self._selected_resource_node_map_id()
        if not map_id:
            self._resource_node_status_var.set("Sin map_id actual")
            return

        nodes = self._resource_nodes_cfg().get(str(map_id), [])
        self._resource_node_status_var.set(
            f"Map ID {map_id} | {len(nodes)} nodo(s) guardado(s)"
        )
        for idx, node in enumerate(nodes, start=1):
            prof = node.get("profession", "?")
            res = node.get("resource", "?")
            pos = node.get("pos") or ["?", "?"]
            self._resource_node_listbox.insert("end", f"{idx:02d}. {prof} | {res} | {pos[0]}, {pos[1]}")

    def _use_current_map_id_for_nodes(self):
        map_id = self._current_runtime_map_id()
        if map_id is None:
            messagebox.showinfo("Sin map_id", "No hay map_id actual. Inicia el bot con sniffer y espera un GDM/GDK.", parent=self)
            return
        self._resource_node_map_var.set(str(map_id))
        self._refresh_resource_node_list()

    def _capture_resource_node(self):
        map_id = self._selected_resource_node_map_id()
        profession = self._resource_node_prof_var.get().strip()
        resource = self._resource_node_res_var.get().strip()

        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona 'Usar actual' o espera a que el bot detecte el mapa.", parent=self)
            return
        if not profession or not resource:
            messagebox.showwarning("Datos incompletos", "Selecciona profesion y recurso antes de capturar.", parent=self)
            return

        self._resource_node_status_var.set(f"Capturando {resource} en 3 segundos...")

        def _do():
            import pyautogui
            time.sleep(3)
            x, y = pyautogui.position()
            self.after(0, lambda: self._save_resource_node(map_id, profession, resource, x, y))

        threading.Thread(target=_do, daemon=True).start()

    def _capture_selected_resource_sprite(self):
        profession = self._resource_node_prof_var.get().strip()
        if not profession:
            messagebox.showwarning("Sin profesion", "Selecciona una profesion primero.", parent=self)
            return
        self._open_capture(profession=profession)

    def _recapture_selected_resource_sprite(self):
        profession = self._resource_node_prof_var.get().strip()
        resource = self._resource_node_res_var.get().strip()
        if not profession or not resource:
            messagebox.showwarning("Datos incompletos", "Selecciona profesion y recurso primero.", parent=self)
            return
        self._open_capture(prefill=resource, profession=profession)

    def _check_selected_resource_on_map(self):
        profession = self._resource_node_prof_var.get().strip()
        resource = self._resource_node_res_var.get().strip()
        if not profession or not resource:
            messagebox.showwarning("Datos incompletos", "Selecciona profesion y recurso primero.", parent=self)
            return
        if self._resource_node_check_lbl is not None:
            self._resource_node_check_lbl.config(text="Chequeando...", fg=YELLOW)
        dummy = self._resource_node_check_lbl or tk.Label(self)
        self._check_resource(resource, profession, dummy)

    def _capture_visible_resource_nodes(self):
        map_id = self._selected_resource_node_map_id()
        profession = self._resource_node_prof_var.get().strip()
        resource = self._resource_node_res_var.get().strip()
        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona 'Usar actual' o espera a que el bot detecte el mapa.", parent=self)
            return
        if not profession or not resource:
            messagebox.showwarning("Datos incompletos", "Selecciona profesion y recurso primero.", parent=self)
            return

        sprite_path = self._resource_sprite_path(profession, resource)
        if not os.path.exists(sprite_path):
            messagebox.showwarning("Sin sprite", "Captura primero el sprite del recurso seleccionado.", parent=self)
            return

        self._resource_node_status_var.set(f"Detectando {resource} visibles en map_id {map_id}...")

        def run():
            threshold = self.config_data["bot"].get("threshold", 0.55)
            detector = Detector(threshold=threshold)
            monitor_idx = self.config_data["game"].get("monitor", 2)
            with mss.mss() as sct:
                monitor = sct.monitors[monitor_idx]
                shot = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(shot)[:, :, :3])
            matches = detector.find_all_resources(frame, resource, profession=profession)

            def finish():
                added = 0
                ignored = 0
                for x, y in matches:
                    if self._save_resource_node(map_id, profession, resource, int(x), int(y), source="sprite", log=False):
                        added += 1
                    else:
                        ignored += 1
                self.config_data = load_config()
                self._sync_runtime_bot_config()
                self._resource_node_map_var.set(str(map_id))
                self._refresh_resource_node_list()
                if self._resource_node_check_lbl is not None:
                    color = GREEN if matches else RED
                    self._resource_node_check_lbl.config(
                        text=f"Chequeo visual: {len(matches)} visible(s) | {added} agregado(s) | {ignored} duplicado(s)",
                        fg=color,
                    )
                self._resource_node_status_var.set(
                    f"Map ID {map_id} | {added} nodo(s) agregados por sprite para {resource}"
                )
                self.log_queue.put(("log", f"[NODES] Captura visible map_id={map_id} {profession}/{resource} -> {len(matches)} match(es), {added} agregado(s), {ignored} duplicado(s)"))

            self.after(0, finish)

        threading.Thread(target=run, daemon=True).start()

    def _save_resource_node(self, map_id: str, profession: str, resource: str, x: int, y: int, source: str = "manual", log: bool = True):
        nodes_cfg = self._resource_nodes_cfg()
        entries = nodes_cfg.setdefault(str(map_id), [])
        new_entry = {
            "profession": profession,
            "resource": resource,
            "pos": [int(x), int(y)],
        }
        duplicate = False
        duplicate_radius = 50
        for entry in entries:
            if entry.get("profession") != profession or entry.get("resource") != resource:
                continue
            pos = entry.get("pos") or [None, None]
            if len(pos) == 2:
                dx = int(pos[0]) - int(x)
                dy = int(pos[1]) - int(y)
                if (dx * dx + dy * dy) ** 0.5 <= duplicate_radius:
                    duplicate = True
                    break

        saved = False
        if not duplicate and new_entry not in entries:
            entries.append(new_entry)
            save_config(self.config_data)
            saved = True
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._resource_node_map_var.set(str(map_id))
        self._refresh_resource_node_resource_choices()
        self._refresh_resource_node_list()
        if duplicate:
            if log:
                self.log_queue.put(("log", f"[NODES] Nodo repetido ignorado map_id={map_id} {profession}/{resource} -> ({x}, {y})"))
            return False

        if not log:
            return saved

        if source == "sprite":
            origin = "sprite"
        elif source == "click":
            origin = "click"
        else:
            origin = "manual"
        self.log_queue.put(("log", f"[NODES] Nodo guardado ({origin}) map_id={map_id} {profession}/{resource} -> ({x}, {y})"))
        return saved

    def _remove_resource_node(self):
        sel = self._resource_node_listbox.curselection() if self._resource_node_listbox else ()
        map_id = self._selected_resource_node_map_id()
        if not sel or not map_id:
            return
        nodes_cfg = self._resource_nodes_cfg()
        entries = nodes_cfg.get(str(map_id), [])
        idx = sel[0]
        if 0 <= idx < len(entries):
            removed = entries.pop(idx)
            if not entries:
                nodes_cfg.pop(str(map_id), None)
            save_config(self.config_data)
            self.config_data = load_config()
            self._sync_runtime_bot_config()
            self._refresh_resource_node_list()
            self.log_queue.put((
                "log",
                f"[NODES] Nodo eliminado map_id={map_id} {removed.get('profession')}/{removed.get('resource')} -> {tuple(removed.get('pos', []))}"
            ))

    def _refresh_ui_checker(self):
        for w in self.ui_check_frame.winfo_children():
            w.destroy()

        pngs = sorted([f for f in os.listdir(UI_DIR) if f.lower().endswith(".png")])
        if not pngs:
            tk.Label(self.ui_check_frame, text="Sin templates UI",
                     bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 9)).pack(padx=10, pady=8)
            return

        for png in pngs:
            name = os.path.splitext(png)[0]
            row = tk.Frame(self.ui_check_frame, bg=PANEL)
            row.pack(fill="x", padx=10, pady=3)

            tk.Label(row, text=name, bg=PANEL, fg=TEXT,
                     font=("Segoe UI", 9), width=16, anchor="w").pack(side="left")

            lbl_result = tk.Label(row, text="—", bg=PANEL, fg=SUBTEXT,
                                  font=("Segoe UI", 8, "bold"), width=14, anchor="w")
            lbl_result.pack(side="left", padx=(4, 0))

            tk.Button(row, text="Chequear", bg=YELLOW, fg=BG,
                      font=("Segoe UI", 7), relief="flat", padx=6, pady=2,
                      cursor="hand2",
                      command=lambda n=name, lbl=lbl_result: self._check_ui(n, lbl)).pack(side="right")

    def _check_ui(self, template_name: str, lbl: tk.Label):
        lbl.config(text="...", fg=YELLOW)
        self.update_idletasks()

        def run():
            import numpy as np
            from detector import Detector
            ui_threshold = self.config_data["bot"].get("ui_threshold", 0.85)
            detector = Detector(threshold=ui_threshold)
            monitor_idx = self.config_data["game"].get("monitor", 2)
            with mss.mss() as sct:
                monitor = sct.monitors[monitor_idx]
                shot = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(shot)[:, :, :3])
            pos = detector.find_ui(frame, template_name)
            if pos:
                lbl.config(text=f"SI  {pos}", fg=GREEN)
            else:
                lbl.config(text="NO detectado", fg=RED)

        threading.Thread(target=run, daemon=True).start()

    def _build_navigation_content(self, parent, nav_cfg):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="x")

        self._nav_enabled_var = tk.BooleanVar(value=nav_cfg.get("enabled", False))
        self._nav_profile_var = tk.StringVar()

        hdr = tk.Frame(frame, bg=PANEL)
        hdr.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(hdr, text="Rutas guardadas", bg=PANEL, fg=GREEN,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Checkbutton(hdr, text="Activar navegacion", variable=self._nav_enabled_var,
                       bg=PANEL, activebackground=PANEL, fg=GREEN, selectcolor=PANEL,
                       font=("Segoe UI", 9), command=self._save_navigation).pack(side="right")

        tk.Label(frame,
                 text="Cada ruta tiene nombre propio y sus salidas por map_id. Luego puedes asignarla al leveling desde la pestaña de mobs.",
                 bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8, "italic"),
                 wraplength=520, justify="left").pack(fill="x", padx=10, pady=(0, 6))

        cfg_row = tk.Frame(frame, bg=PANEL)
        cfg_row.pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(cfg_row, text="Scans vacios antes de mover:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        self._nav_scans_var = tk.StringVar(value=str(nav_cfg.get("empty_scans_before_move", 3)))
        spin = tk.Spinbox(cfg_row, from_=1, to=20, width=4, textvariable=self._nav_scans_var,
                          bg=ACCENT, fg=TEXT, buttonbackground=ACCENT, relief="flat",
                          command=self._save_navigation)
        spin.pack(side="left", padx=(6, 0))
        spin.bind("<FocusOut>", lambda e: self._save_navigation())

        selector_row = tk.Frame(frame, bg=PANEL)
        selector_row.pack(fill="x", padx=10, pady=(4, 6))
        tk.Label(selector_row, text="Ruta activa en editor:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        self._nav_profile_cb = ttk.Combobox(selector_row, textvariable=self._nav_profile_var,
                                            state="readonly", width=24)
        self._nav_profile_cb.pack(side="left", padx=(6, 4))
        self._nav_profile_cb.bind("<<ComboboxSelected>>", lambda e: self._on_nav_profile_changed())
        tk.Button(selector_row, text="Nueva", bg=BLUE, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_new_profile).pack(side="left", padx=(0, 4))
        tk.Button(selector_row, text="Renombrar", bg=YELLOW, fg=BG,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_rename_profile).pack(side="left", padx=(0, 4))
        tk.Button(selector_row, text="Eliminar", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_delete_profile).pack(side="left")

        map_route_row = tk.Frame(frame, bg=PANEL)
        map_route_row.pack(fill="x", padx=10, pady=(6, 2))
        tk.Label(map_route_row, text="Map ID actual:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        self._nav_map_id_var = tk.StringVar(value="")
        tk.Entry(map_route_row, textvariable=self._nav_map_id_var, width=10,
                 bg=ACCENT, fg=TEXT, relief="flat",
                 insertbackground=TEXT).pack(side="left", padx=(6, 4))
        tk.Button(map_route_row, text="Usar actual", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_use_current_map_id).pack(side="left")

        map_list_row = tk.Frame(frame, bg=PANEL)
        map_list_row.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(map_list_row, text="Salidas por map_id:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(anchor="w")

        map_lb_frame = tk.Frame(map_list_row, bg=PANEL)
        map_lb_frame.pack(fill="x")
        self._nav_map_route_listbox = tk.Listbox(map_lb_frame, height=6, bg=ACCENT, fg=TEXT,
                                                 font=("Consolas", 9), relief="flat",
                                                 selectbackground=GREEN, selectforeground=BG,
                                                 activestyle="none")
        self._nav_map_route_listbox.pack(side="left", fill="x", expand=True)
        map_lb_scroll = ttk.Scrollbar(map_lb_frame, orient="vertical", command=self._nav_map_route_listbox.yview)
        self._nav_map_route_listbox.configure(yscrollcommand=map_lb_scroll.set)
        map_lb_scroll.pack(side="right", fill="y")

        map_btn_row = tk.Frame(frame, bg=PANEL)
        map_btn_row.pack(fill="x", padx=10, pady=(2, 6))
        tk.Button(map_btn_row, text="Guardar punto actual", bg=BLUE, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_add_map_point).pack(side="left", padx=(0, 4))
        tk.Button(map_btn_row, text="Capturar mouse map_id (3s)", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_capture_mouse_for_map).pack(side="left", padx=(0, 4))
        tk.Button(map_btn_row, text="Eliminar map_id", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_remove_map_point).pack(side="left")

        exit_row = tk.Frame(frame, bg=PANEL)
        exit_row.pack(fill="x", padx=10, pady=(2, 6))
        tk.Label(exit_row, text="Salida automática por borde:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        for label in ("Arriba", "Derecha", "Abajo", "Izquierda"):
            tk.Button(
                exit_row,
                text=label,
                bg=ACCENT,
                fg=TEXT,
                font=("Segoe UI", 8),
                relief="flat",
                padx=8,
                pady=2,
                cursor="hand2",
                command=lambda d=label.lower(): self._nav_store_map_exit_direction(d),
            ).pack(side="left", padx=(6, 0))

        tk.Button(
            exit_row,
            text="Celda específica",
            bg=BLUE,
            fg=BG,
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=self._nav_store_map_exit_cell,
        ).pack(side="left", padx=(12, 0))

        tk.Button(
            exit_row,
            text="Capturar Celda (3s)",
            bg=ACCENT,
            fg=TEXT,
            font=("Segoe UI", 8),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=self._nav_capture_cell_for_map,
        ).pack(side="left", padx=(6, 0))

        list_row = tk.Frame(frame, bg=PANEL)
        list_row.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(list_row, text="Ruta fallback (sin map_id):", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(anchor="w")

        lb_frame = tk.Frame(list_row, bg=PANEL)
        lb_frame.pack(fill="x")
        self._route_listbox = tk.Listbox(lb_frame, height=4, bg=ACCENT, fg=TEXT,
                                         font=("Consolas", 9), relief="flat",
                                         selectbackground=GREEN, selectforeground=BG,
                                         activestyle="none")
        self._route_listbox.pack(side="left", fill="x", expand=True)
        lb_scroll = ttk.Scrollbar(lb_frame, orient="vertical", command=self._route_listbox.yview)
        self._route_listbox.configure(yscrollcommand=lb_scroll.set)
        lb_scroll.pack(side="right", fill="y")

        btn_row = tk.Frame(frame, bg=PANEL)
        btn_row.pack(fill="x", padx=10, pady=(2, 6))
        tk.Button(btn_row, text="+ Agregar punto", bg=BLUE, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_add_point).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Capturar mouse (3s)", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_capture_mouse).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Eliminar", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._nav_remove_point).pack(side="left")

        self._refresh_navigation_profiles()

    def _selected_nav_profile_name(self):
        raw = (self._nav_profile_var.get() or "").strip()
        return raw or None

    def _current_nav_profile_cfg(self):
        profiles = self._route_profiles_cfg()
        name = self._selected_nav_profile_name()
        if not name or name not in profiles:
            name = next(iter(profiles.keys()))
            self._nav_profile_var.set(name)
        profile = profiles.setdefault(name, {"route": [], "route_by_map_id": {}})
        profile.setdefault("route", [])
        profile.setdefault("route_by_map_id", {})
        return profile

    def _refresh_navigation_profiles(self):
        profiles = self._route_profiles_cfg()
        names = sorted(profiles.keys())
        self._nav_profile_cb["values"] = names
        selected = self._selected_nav_profile_name()
        if not selected or selected not in profiles:
            selected = names[0]
            self._nav_profile_var.set(selected)
        self._load_navigation_profile_to_editor()
        self._refresh_leveling_route_options()

    def _load_navigation_profile_to_editor(self):
        profile = self._current_nav_profile_cfg()
        current_map_id = self._current_runtime_map_id()
        self._nav_map_id_var.set(str(current_map_id) if current_map_id is not None else "")
        self._route_listbox.delete(0, "end")
        for point in profile.get("route", []):
            try:
                self._route_listbox.insert("end", f"{int(point[0])}, {int(point[1])}")
            except (TypeError, ValueError, IndexError):
                pass
        self._nav_map_route_listbox.delete(0, "end")
        for map_id, point in sorted(profile.get("route_by_map_id", {}).items(), key=lambda item: str(item[0])):
            try:
                self._nav_map_route_listbox.insert("end", f"{map_id} -> {int(point[0])}, {int(point[1])}")
            except (TypeError, ValueError, IndexError):
                pass
        for map_id, direction in sorted(profile.get("route_exit_by_map_id", {}).items(), key=lambda item: str(item[0])):
            try:
                self._nav_map_route_listbox.insert("end", f"{map_id} -> auto:{str(direction).strip().lower()}")
            except Exception:
                pass

    def _on_nav_profile_changed(self):
        self._load_navigation_profile_to_editor()

    def _nav_new_profile(self):
        name = simpledialog.askstring("Nueva ruta", "Nombre de la ruta:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        profiles = self._route_profiles_cfg()
        if name in profiles:
            messagebox.showwarning("Ruta existente", f"Ya existe una ruta llamada '{name}'.", parent=self)
            return
        current = self._current_nav_profile_cfg()
        profiles[name] = {
            "route": [list(point) for point in current.get("route", [])],
            "route_by_map_id": dict(current.get("route_by_map_id", {})),
            "route_exit_by_map_id": dict(current.get("route_exit_by_map_id", {})),
        }
        self._nav_profile_var.set(name)
        self._save_navigation()

    def _nav_rename_profile(self):
        current_name = self._selected_nav_profile_name()
        if not current_name:
            return
        new_name = simpledialog.askstring("Renombrar ruta", "Nuevo nombre:", initialvalue=current_name, parent=self)
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or new_name == current_name:
            return
        profiles = self._route_profiles_cfg()
        if new_name in profiles:
            messagebox.showwarning("Ruta existente", f"Ya existe una ruta llamada '{new_name}'.", parent=self)
            return
        profiles[new_name] = profiles.pop(current_name)
        leveling = self.config_data.setdefault("leveling", {})
        if leveling.get("route_profile") == current_name:
            leveling["route_profile"] = new_name
        self._nav_profile_var.set(new_name)
        self._save_navigation()

    def _nav_delete_profile(self):
        current_name = self._selected_nav_profile_name()
        profiles = self._route_profiles_cfg()
        if not current_name or current_name not in profiles:
            return
        if len(profiles) <= 1:
            messagebox.showwarning("Ultima ruta", "Debe existir al menos una ruta guardada.", parent=self)
            return
        if not messagebox.askyesno("Eliminar ruta", f"Eliminar la ruta '{current_name}'?", parent=self):
            return
        profiles.pop(current_name, None)
        leveling = self.config_data.setdefault("leveling", {})
        if leveling.get("route_profile") == current_name:
            leveling["route_profile"] = sorted(profiles.keys())[0]
        self._nav_profile_var.set(sorted(profiles.keys())[0])
        self._save_navigation()

    def _selected_nav_map_id(self):
        raw = (self._nav_map_id_var.get() or "").strip()
        return raw or None

    def _nav_use_current_map_id(self):
        map_id = self._current_runtime_map_id()
        if map_id is None:
            messagebox.showinfo("Sin map_id", "No hay map_id actual. Inicia el bot con sniffer y espera un GDM/GDK.", parent=self)
            return
        self._nav_map_id_var.set(str(map_id))

    def _nav_add_map_point(self):
        self._nav_capture_mouse_for_map()

    def _nav_capture_mouse_for_map(self):
        import pyautogui
        map_id = self._selected_nav_map_id()
        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona un map_id o usa 'Usar actual'.", parent=self)
            return
        self._log_nav(f"Capturando salida para map_id {map_id} en 3 segundos...")
        def _do():
            time.sleep(3)
            x, y = pyautogui.position()
            self.after(0, lambda: self._nav_store_map_point(map_id, x, y))
        threading.Thread(target=_do, daemon=True).start()

    def _nav_store_map_point(self, map_id: str, x: int, y: int):
        prefix = f"{map_id} ->"
        for i in range(self._nav_map_route_listbox.size()):
            item = self._nav_map_route_listbox.get(i)
            if item.startswith(prefix):
                self._nav_map_route_listbox.delete(i)
                self._nav_map_route_listbox.insert(i, f"{map_id} -> {x}, {y}")
                self._save_navigation()
                self._log_nav(f"Salida actualizada map_id={map_id}: {x}, {y}")
                return
        self._nav_map_route_listbox.insert("end", f"{map_id} -> {x}, {y}")
        self._save_navigation()
        self._log_nav(f"Salida guardada map_id={map_id}: {x}, {y}")

    def _nav_store_map_exit_direction(self, direction: str):
        map_id = self._selected_nav_map_id()
        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona un map_id o usa 'Usar actual'.", parent=self)
            return
        prefix = f"{map_id} ->"
        for i in range(self._nav_map_route_listbox.size()):
            item = self._nav_map_route_listbox.get(i)
            if item.startswith(prefix):
                self._nav_map_route_listbox.delete(i)
                self._nav_map_route_listbox.insert(i, f"{map_id} -> auto:{direction}")
                self._save_navigation()
                self._log_nav(f"Salida automática guardada map_id={map_id}: {direction}")
                return
        self._nav_map_route_listbox.insert("end", f"{map_id} -> auto:{direction}")
        self._save_navigation()
        self._log_nav(f"Salida automática guardada map_id={map_id}: {direction}")

    def _nav_store_map_exit_cell(self):
        map_id = self._selected_nav_map_id()
        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona un map_id o usa 'Usar actual'.", parent=self)
            return
        cell_str = simpledialog.askstring("Salida por Celda", "Ingresa el ID de la celda (ej: puerta, zaap o sol):", parent=self)
        if not cell_str:
            return
        try:
            cell_id = int(cell_str.strip())
        except ValueError:
            messagebox.showwarning("ID Inválido", "El ID de la celda debe ser un número entero.", parent=self)
            return
        self._nav_store_map_exit_cell_direct(cell_id)

    def _nav_capture_cell_for_map(self):
        map_id = self._selected_nav_map_id()
        if not map_id:
            messagebox.showwarning("Sin map_id", "Primero selecciona un map_id o usa 'Usar actual'.", parent=self)
            return
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if not bot or str(bot._current_map_id) != str(map_id):
            messagebox.showwarning("Mapa no coincide", "El bot debe estar corriendo en el mapa actual para proyectar celdas.", parent=self)
            return
        
        self._log_nav(f"Capturando celda para map_id {map_id} en 3 segundos...")
        import pyautogui
        def _do():
            time.sleep(3)
            x, y = pyautogui.position()
            self.after(0, lambda: self._process_captured_cell(map_id, x, y, bot))
        threading.Thread(target=_do, daemon=True).start()

    def _process_captured_cell(self, map_id: str, x: int, y: int, bot):
        cells = bot.get_current_map_cells_snapshot()
        best_cell = None
        best_dist = float('inf')
        for cell in cells:
            cid = cell.get("cell_id")
            if cid is None:
                continue
            pos = bot._cell_to_screen(int(cid))
            if not pos:
                continue
            dist = ((pos[0] - x)**2 + (pos[1] - y)**2)**0.5
            if dist < best_dist:
                best_dist = dist
                best_cell = cid
                
        if best_cell is not None and best_dist < 80:  # Tolerancia de 80 px (tamaño razonable de celda)
            self._log_nav(f"Celda detectada: {best_cell} (distancia: {best_dist:.1f}px)")
            self._nav_store_map_exit_cell_direct(int(best_cell))
        else:
            self._log_nav(f"No se encontró una celda cerca de la posición del mouse ({x}, {y})")
            messagebox.showwarning("Celda no encontrada", "No se pudo proyectar ninguna celda cerca del mouse. Asegúrate de tener la grilla calibrada.", parent=self)

    def _nav_store_map_exit_cell_direct(self, cell_id: int):
        map_id = self._selected_nav_map_id()
        if not map_id:
            return
        direction = f"cell:{cell_id}"
        prefix = f"{map_id} ->"
        for i in range(self._nav_map_route_listbox.size()):
            item = self._nav_map_route_listbox.get(i)
            if item.startswith(prefix):
                self._nav_map_route_listbox.delete(i)
                self._nav_map_route_listbox.insert(i, f"{map_id} -> auto:{direction}")
                self._save_navigation()
                self._log_nav(f"Salida por celda guardada map_id={map_id}: {direction}")
                return
        self._nav_map_route_listbox.insert("end", f"{map_id} -> auto:{direction}")
        self._save_navigation()
        self._log_nav(f"Salida por celda guardada map_id={map_id}: {direction}")

    def _nav_remove_map_point(self):
        sel = self._nav_map_route_listbox.curselection()
        if not sel:
            return
        self._nav_map_route_listbox.delete(sel[0])
        self._save_navigation()

    def _nav_add_point(self):
        raw = simpledialog.askstring("Agregar punto",
                                     "Coordenadas X, Y (ej: 1280, 720):", parent=self)
        if not raw:
            return
        try:
            x_str, y_str = raw.replace(" ", "").split(",")
            x, y = int(x_str), int(y_str)
        except ValueError:
            messagebox.showwarning("Formato invalido", "Usa el formato: X, Y", parent=self)
            return
        self._route_listbox.insert("end", f"{x}, {y}")
        self._save_navigation()

    def _nav_capture_mouse(self):
        import pyautogui
        self._log_nav("Capturando posicion en 3 segundos...")
        def _do():
            time.sleep(3)
            x, y = pyautogui.position()
            self._route_listbox.insert("end", f"{x}, {y}")
            self._save_navigation()
            self._log_nav(f"Punto capturado: {x}, {y}")
        threading.Thread(target=_do, daemon=True).start()

    def _nav_remove_point(self):
        sel = self._route_listbox.curselection()
        if not sel:
            return
        self._route_listbox.delete(sel[0])
        self._save_navigation()

    def _save_navigation(self):
        route = []
        for i in range(self._route_listbox.size()):
            item = self._route_listbox.get(i)
            try:
                x_str, y_str = item.replace(" ", "").split(",")
                route.append([int(x_str), int(y_str)])
            except ValueError:
                pass
        route_by_map_id = {}
        route_exit_by_map_id = {}
        for i in range(self._nav_map_route_listbox.size()):
            item = self._nav_map_route_listbox.get(i)
            try:
                map_id, point = item.split("->", 1)
                point = point.strip()
                if point.lower().startswith("auto:"):
                    route_exit_by_map_id[str(map_id).strip()] = point.split(":", 1)[1].strip().lower()
                    continue
                x_str, y_str = point.replace(" ", "").split(",")
                route_by_map_id[str(map_id).strip()] = [int(x_str), int(y_str)]
            except ValueError:
                pass
        try:
            scans = int(self._nav_scans_var.get())
        except ValueError:
            scans = 3
        nav = self._navigation_cfg()
        profiles = self._route_profiles_cfg()
        selected = self._selected_nav_profile_name() or "Default"
        profiles[selected] = {
            "route": route,
            "route_by_map_id": route_by_map_id,
            "route_exit_by_map_id": route_exit_by_map_id,
        }
        nav["enabled"] = self._nav_enabled_var.get()
        nav["empty_scans_before_move"] = scans
        nav["route_profiles"] = profiles
        nav["route"] = route
        nav["route_by_map_id"] = route_by_map_id
        nav["route_exit_by_map_id"] = route_exit_by_map_id
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._refresh_navigation_profiles()

    def _log_nav(self, msg: str):
        self.log_queue.put(("log", f"[NAV] {msg}"))

    def _build_profile_tab(self, parent):
        self._profile_tab_frame = tk.Frame(parent, bg=BG)
        self._profile_tab_frame.pack(fill="both", expand=True)
        
        top = tk.Frame(self._profile_tab_frame, bg=BG)
        top.pack(fill="x", pady=(8, 6))
        tk.Label(top, text="Perfiles de Teleport / Secuencias", bg=BG, fg=GREEN, font=("Segoe UI", 11, "bold")).pack(side="left")
        
        active_row = tk.Frame(self._profile_tab_frame, bg=PANEL, padx=10, pady=8)
        active_row.pack(fill="x", pady=(0, 10))
        tk.Label(active_row, text="Perfil activo:", bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 9)).pack(side="left")
        
        self._active_teleport_var = tk.StringVar(value=self.config_data.get("active_teleport_profile", ""))
        self._active_teleport_cb = ttk.Combobox(active_row, textvariable=self._active_teleport_var, state="readonly", width=24)
        self._active_teleport_cb.pack(side="left", padx=(8, 4))
        self._active_teleport_cb.bind("<<ComboboxSelected>>", lambda e: self._save_active_teleport())
        
        self._active_teleport_toggle_btn = tk.Button(active_row, font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2, cursor="hand2", command=self._toggle_teleport_enabled)
        self._active_teleport_toggle_btn.pack(side="left")
        
        if "teleport_enabled" not in self.config_data:
            self.config_data["teleport_enabled"] = bool(self.config_data.get("active_teleport_profile"))
        self._update_teleport_toggle_btn()

        editor_frame = tk.Frame(self._profile_tab_frame, bg=PANEL, padx=10, pady=8)
        editor_frame.pack(fill="x")
        tk.Label(editor_frame, text="Editor de Perfiles", bg=PANEL, fg=YELLOW, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        
        sel_row = tk.Frame(editor_frame, bg=PANEL)
        sel_row.pack(fill="x", pady=(0, 6))
        tk.Label(sel_row, text="Editar:", bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 9)).pack(side="left")
        self._edit_teleport_var = tk.StringVar()
        self._edit_teleport_cb = ttk.Combobox(sel_row, textvariable=self._edit_teleport_var, state="readonly", width=24)
        self._edit_teleport_cb.pack(side="left", padx=(8, 4))
        self._edit_teleport_cb.bind("<<ComboboxSelected>>", lambda e: self._load_teleport_profile_to_editor())
        
        tk.Button(sel_row, text="Nuevo", bg=BLUE, fg=BG, font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2, cursor="hand2", command=self._new_teleport_profile).pack(side="left", padx=(0, 4))
        tk.Button(sel_row, text="Eliminar", bg=RED, fg=TEXT, font=("Segoe UI", 8), relief="flat", padx=8, pady=2, cursor="hand2", command=self._delete_teleport_profile).pack(side="left")
        
        form = tk.Frame(editor_frame, bg=PANEL)
        form.pack(fill="x", pady=6)
        
        self._tp_trigger_map_var = tk.StringVar()
        self._tp_cell_id_var = tk.StringVar()
        self._tp_dest_image_var = tk.StringVar()
        self._tp_route_name_var = tk.StringVar()
        self._tp_expected_map_var = tk.StringVar()
        self._tp_farm_map_var = tk.StringVar()
        self._tp_mobs_activate_var = tk.StringVar()
        
        fields = [
            ("Map ID de activación:", self._tp_trigger_map_var, "Ej: 7411"),
            ("Celda del Zaap/Objeto:", self._tp_cell_id_var, "Ej: 268"),
            ("Imagen de Destino (sin .png):", self._tp_dest_image_var, "Ej: LaCuna"),
            ("Ruta a cargar post-teleport:", self._tp_route_name_var, "Ej: FarmJalato"),
            ("Map ID Esperado:", self._tp_expected_map_var, "Ej: 6954"),
            ("Map ID Inicio Farmeo:", self._tp_farm_map_var, "Opc. Ej: 1234 (ignora mobs en camino)"),
            ("Template IDs a Activar:", self._tp_mobs_activate_var, "Opc. Ej: 101,134"),
        ]
        
        for label_text, str_var, hint in fields:
            row = tk.Frame(form, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label_text, bg=PANEL, fg=TEXT, font=("Segoe UI", 9), width=24, anchor="w").pack(side="left")
            if label_text.startswith("Ruta a cargar"):
                cb = ttk.Combobox(row, textvariable=str_var, state="readonly", width=22)
                cb.pack(side="left", padx=(0, 6))
                self._tp_route_cb = cb
            else:
                tk.Entry(row, textvariable=str_var, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 9), width=24).pack(side="left", padx=(0, 6))
            tk.Label(row, text=hint, bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8, "italic")).pack(side="left")
            
        tk.Button(editor_frame, text="Guardar Perfil", bg=GREEN, fg=BG, font=("Segoe UI", 9, "bold"), relief="flat", padx=12, pady=4, cursor="hand2", command=self._save_teleport_profile_from_editor).pack(pady=(6, 0))
        
        self._refresh_teleport_profiles()

    def _teleport_cfg(self):
        return self.config_data.setdefault("teleport_profiles", {})

    def _refresh_teleport_profiles(self):
        profiles = self._teleport_cfg()
        names = sorted(profiles.keys())
        if not names:
            profiles["Farm Jalato"] = {
                "trigger_map": "7411",
                "cell_id": "268",
                "dest_image": "LaCuna",
                "route_name": "FarmJalato",
                "expected_map": "6954",
                "farm_map": "",
                "mobs_activate": ""
            }
            save_config(self.config_data)
            names = ["Farm Jalato"]
            
        self._active_teleport_cb["values"] = names
        self._edit_teleport_cb["values"] = names
        
        if self._edit_teleport_var.get() not in names:
            self._edit_teleport_var.set(names[0] if names else "")
            
        if hasattr(self, "_tp_route_cb"):
            self._tp_route_cb["values"] = sorted(self._route_profiles_cfg().keys())
            
        self._load_teleport_profile_to_editor()
        
    def _update_teleport_toggle_btn(self):
        if self.config_data.get("teleport_enabled", False):
            self._active_teleport_toggle_btn.config(text="Activado", bg=GREEN, fg=BG)
        else:
            self._active_teleport_toggle_btn.config(text="Desactivado", bg=RED, fg=TEXT)

    def _toggle_teleport_enabled(self):
        current = self.config_data.get("teleport_enabled", False)
        self.config_data["teleport_enabled"] = not current
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._update_teleport_toggle_btn()
        state = "Activado" if not current else "Desactivado"
        self.log_queue.put(("log", f"[TELEPORT] Perfiles de teleport: {state}"))

    def _save_active_teleport(self):
        self.config_data["active_teleport_profile"] = self._active_teleport_var.get()
        self.config_data["teleport_enabled"] = True
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._update_teleport_toggle_btn()
        self.log_queue.put(("log", f"[TELEPORT] Perfil activo guardado: {self._active_teleport_var.get()}"))
        
    def _new_teleport_profile(self):
        name = simpledialog.askstring("Nuevo Perfil", "Nombre del perfil:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        profiles = self._teleport_cfg()
        if name in profiles:
            messagebox.showwarning("Perfil existente", "Ya existe un perfil con ese nombre.", parent=self)
            return
        profiles[name] = {
            "trigger_map": "",
            "cell_id": "",
            "dest_image": "",
            "route_name": "",
            "expected_map": "",
            "farm_map": "",
            "mobs_activate": ""
        }
        self._edit_teleport_var.set(name)
        self._refresh_teleport_profiles()

    def _delete_teleport_profile(self):
        name = self._edit_teleport_var.get()
        if not name:
            return
        if messagebox.askyesno("Eliminar", f"¿Eliminar perfil '{name}'?", parent=self):
            profiles = self._teleport_cfg()
            profiles.pop(name, None)
            if self.config_data.get("active_teleport_profile") == name:
                self.config_data["active_teleport_profile"] = ""
                self.config_data["teleport_enabled"] = False
                self._active_teleport_var.set("")
                self._update_teleport_toggle_btn()
            save_config(self.config_data)
            self._refresh_teleport_profiles()

    def _load_teleport_profile_to_editor(self):
        name = self._edit_teleport_var.get()
        profile = self._teleport_cfg().get(name, {})
        self._tp_trigger_map_var.set(str(profile.get("trigger_map", "")))
        self._tp_cell_id_var.set(str(profile.get("cell_id", "")))
        self._tp_dest_image_var.set(str(profile.get("dest_image", "")))
        self._tp_route_name_var.set(str(profile.get("route_name", "")))
        self._tp_expected_map_var.set(str(profile.get("expected_map", "")))
        self._tp_farm_map_var.set(str(profile.get("farm_map", "")))
        self._tp_mobs_activate_var.set(str(profile.get("mobs_activate", "")))
        
    def _save_teleport_profile_from_editor(self):
        name = self._edit_teleport_var.get()
        if not name:
            return
        profiles = self._teleport_cfg()
        profiles[name] = {
            "trigger_map": self._tp_trigger_map_var.get().strip(),
            "cell_id": self._tp_cell_id_var.get().strip(),
            "dest_image": self._tp_dest_image_var.get().strip(),
            "route_name": self._tp_route_name_var.get().strip(),
            "expected_map": self._tp_expected_map_var.get().strip(),
            "farm_map": self._tp_farm_map_var.get().strip(),
            "mobs_activate": self._tp_mobs_activate_var.get().strip(),
        }
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[TELEPORT] Perfil '{name}' guardado correctamente."))

    # ----------------------------------------------------------- Mobs --
    def _refresh_mobs(self):
        for w in self.mobs_frame.winfo_children():
            w.destroy()
        self.mob_vars.clear()
        self.mob_ignore_vars.clear()
        self.mob_template_vars.clear()
        self.mob_images.clear()

        # ── Selector de modo ─────────────────────────────────────────────
        mode_row = tk.Frame(self.mobs_frame, bg=BG)
        mode_row.pack(fill="x", padx=10, pady=(8, 4))
        current_mode = self.config_data["farming"].get("mode", "resource")
        is_leveling  = current_mode == "leveling"
        mode_color   = GREEN if is_leveling else SUBTEXT
        mode_text    = "Modo: Leveling ✓" if is_leveling else "Modo: Recursos (inactivo)"
        self._mob_mode_lbl = tk.Label(mode_row, text=mode_text,
                                      bg=BG, fg=mode_color,
                                      font=("Segoe UI", 10, "bold"))
        self._mob_mode_lbl.pack(side="left")
        tk.Button(mode_row, text="Desactivar todos", bg=RED, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._disable_all_mobs).pack(side="right", padx=(6, 0))
        btn_text = "Desactivar leveling" if is_leveling else "Activar leveling"
        btn_color = RED if is_leveling else GREEN
        tk.Button(mode_row, text=btn_text, bg=btn_color, fg=BG,
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2",
                  command=self._toggle_leveling_mode).pack(side="right")

        route_row = tk.Frame(self.mobs_frame, bg=BG)
        route_row.pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(route_row, text="Ruta asignada al leveling:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left")
        leveling_cfg = self.config_data.setdefault("leveling", {})
        route_names = sorted(self._route_profiles_cfg().keys())
        default_route = leveling_cfg.get("route_profile") or (route_names[0] if route_names else "")
        self._leveling_route_var = tk.StringVar(value=default_route)
        self._leveling_route_cb = ttk.Combobox(route_row, textvariable=self._leveling_route_var,
                                               state="readonly", width=22, values=route_names)
        self._leveling_route_cb.pack(side="left", padx=(6, 4))
        self._leveling_route_cb.bind("<<ComboboxSelected>>", lambda e: self._save_leveling_route_profile())
        tk.Button(route_row, text="Guardar", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._save_leveling_route_profile).pack(side="left")
        self._leveling_route_toggle_btn = tk.Button(
            route_row,
            font=("Segoe UI", 7, "bold"),
            relief="flat",
            padx=8,
            pady=1,
            cursor="hand2",
            command=self._toggle_leveling_route_enabled,
        )
        self._leveling_route_toggle_btn.pack(side="left", padx=(6, 0))
        self._update_leveling_route_toggle_button()

        settings_box = tk.Frame(self.mobs_frame, bg=PANEL, padx=10, pady=8)
        settings_box.pack(fill="x", padx=10, pady=(0, 8))
        settings_header = tk.Frame(settings_box, bg=PANEL)
        settings_header.pack(fill="x")
        tk.Label(settings_header, text="Ajustes de auto-nivel", bg=PANEL, fg=YELLOW,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        search_entry = tk.Entry(settings_header, textvariable=self._mob_search_var, width=22,
                                bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                                font=("Segoe UI", 8))
        search_entry.pack(side="right", padx=(0, 4))
        self._mob_search_entry = search_entry
        tk.Button(settings_header, text="Limpiar", bg=BG, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._clear_mob_search).pack(side="right", padx=(0, 4))
        tk.Label(settings_header, text="Buscar:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="right", padx=(0, 6))

        veto_row = tk.Frame(settings_box, bg=PANEL)
        veto_row.pack(fill="x", pady=(6, 0))
        tk.Label(veto_row, text="Vetar IDs (ignorar grupo si contiene):", bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
        
        veto_raw = leveling_cfg.get("mob_group_veto_template_ids", [])
        if isinstance(veto_raw, list):
            veto_str = ",".join(str(v) for v in veto_raw)
        else:
            veto_str = str(veto_raw)
        self._mob_veto_template_ids_var = tk.StringVar(value=veto_str)
        tk.Entry(veto_row, textvariable=self._mob_veto_template_ids_var, width=18, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 8)).pack(side="left", padx=(6, 4))
        tk.Button(veto_row, text="Guardar veto", bg=ACCENT, fg=TEXT, font=("Segoe UI", 7), relief="flat", padx=6, pady=1, cursor="hand2", command=self._save_mob_group_veto_template_ids).pack(side="left")


        mobs = _list_mobs()
        search_text = (self._mob_search_var.get() or "").strip().lower()
        if search_text:
            mobs = [mob_name for mob_name in mobs if search_text in mob_name.lower()]
        active_cfg = self.config_data.get("leveling", {}).get("mobs", {})

        if not mobs:
            tk.Label(self.mobs_frame, text="Sin mobs — agrega uno para comenzar",
                     bg=BG, fg=SUBTEXT, font=("Segoe UI", 9)).pack(padx=10, pady=8)
        else:
            for mob_name in mobs:
                mob_dir = os.path.join(MOBS_DIR, mob_name)
                mob_data = active_cfg.get(mob_name, {})
                self._mob_card_collapsed.setdefault(mob_name, True)
                lf = tk.Frame(self.mobs_frame, bg=PANEL, bd=1, relief="groove")
                lf.pack(fill="x", padx=10, pady=(4, 2))

                header = tk.Frame(lf, bg=PANEL, padx=8, pady=4)
                header.pack(fill="x")

                icon_photo = self._load_mob_icon_photo(mob_name)
                icon_box = tk.Frame(header, bg=PANEL, width=42, height=42)
                icon_box.pack(side="left")
                icon_box.pack_propagate(False)
                if icon_photo is not None:
                    tk.Label(icon_box, image=icon_photo, bg=PANEL).pack(fill="both", expand=True)
                else:
                    tk.Label(icon_box, text="ICONO", bg=BG, fg=SUBTEXT,
                             font=("Segoe UI", 7, "bold")).pack(fill="both", expand=True)

                summary = tk.Frame(header, bg=PANEL)
                summary.pack(side="left", fill="x", expand=True, padx=(8, 0))
                template_text = ",".join(str(v) for v in mob_data.get("template_ids", []))
                summary_text = (
                    f"Ignorar={'SI' if mob_data.get('ignore', False) else 'NO'}"
                    f" | IDs={template_text or '-'}"
                )
                tk.Label(summary, text=mob_name, bg=PANEL, fg=YELLOW,
                         font=("Segoe UI", 9, "bold")).pack(anchor="w")
                tk.Label(summary, text=summary_text, bg=PANEL, fg=SUBTEXT,
                         font=("Segoe UI", 8)).pack(anchor="w", pady=(1, 0))

                toggle_btn = tk.Button(
                    header,
                    text="▼" if not self._mob_card_collapsed.get(mob_name, True) else "▶",
                    bg=PANEL,
                    fg=TEXT,
                    relief="flat",
                    padx=6,
                    pady=1,
                    cursor="hand2",
                    font=("Segoe UI", 9, "bold"),
                    command=lambda n=mob_name: self._toggle_mob_card(n),
                )
                toggle_btn.pack(side="right")
                
                var = tk.BooleanVar(value=mob_data.get("enabled", True))
                self.mob_vars[mob_name] = var
                tk.Checkbutton(
                    header,
                    text="Activo",
                    variable=var,
                    bg=PANEL,
                    activebackground=PANEL,
                    fg=GREEN,
                    selectcolor=PANEL,
                    font=("Segoe UI", 8, "bold"),
                    command=self._update_mobs
                ).pack(side="right", padx=(0, 8))

                for widget in (header, summary, icon_box):
                    widget.bind("<Button-1>", lambda _e, n=mob_name: self._toggle_mob_card(n))

                detail = tk.Frame(lf, bg=PANEL, padx=8, pady=4)
                if not self._mob_card_collapsed.get(mob_name, True):
                    detail.pack(fill="x")

                ignore_var = tk.BooleanVar(value=bool(mob_data.get("ignore", False)))
                self.mob_ignore_vars[mob_name] = ignore_var

                row1 = tk.Frame(detail, bg=PANEL)
                row1.pack(fill="x", pady=(0, 3))
                tk.Checkbutton(row1, text="Ignorar", variable=ignore_var, bg=PANEL,
                               activebackground=PANEL, fg=RED, selectcolor=PANEL,
                               font=("Segoe UI", 8), command=self._update_mobs).pack(side="left")
                lbl_result = tk.Label(row1, text="—", bg=PANEL, fg=SUBTEXT, font=("Segoe UI", 8, "bold"))
                lbl_result.pack(side="right")
                tk.Button(row1, text="Chequear", bg=YELLOW, fg=BG,
                          font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                          cursor="hand2",
                          command=lambda n=mob_name, lbl=lbl_result: self._check_mob(n, lbl)).pack(side="right", padx=(4, 0))
                tk.Button(row1, text="Escanear", bg="#f07010", fg=BG,
                          font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                          cursor="hand2",
                          command=lambda n=mob_name, lbl=lbl_result: self._scan_mob_on_map(n, lbl)).pack(side="right", padx=(4, 0))

                row2 = tk.Frame(detail, bg=PANEL)
                row2.pack(fill="x", pady=(0, 3))
                tk.Label(row2, text="Template IDs:", bg=PANEL, fg=SUBTEXT,
                         font=("Segoe UI", 8)).pack(side="left")
                template_var = tk.StringVar(value=template_text)
                self.mob_template_vars[mob_name] = template_var
                tk.Entry(row2, textvariable=template_var, width=18,
                         bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                         font=("Segoe UI", 8)).pack(side="left", padx=(6, 4), fill="x", expand=True)
                tk.Button(row2, text="Guardar IDs", bg=ACCENT, fg=TEXT,
                          font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                          cursor="hand2",
                          command=lambda n=mob_name: self._save_mob_template_ids(n)).pack(side="left")
                tk.Label(row2, text="Ej: 112,115", bg=PANEL, fg=SUBTEXT,
                         font=("Segoe UI", 7, "italic")).pack(side="left", padx=(6, 0))

                row3 = tk.Frame(detail, bg=PANEL)
                row3.pack(fill="x")
                tk.Button(row3, text="Capturar ICONO", bg=BLUE, fg=BG,
                          font=("Segoe UI", 7, "bold"), relief="flat", padx=8, pady=1,
                          cursor="hand2",
                          command=lambda n=mob_name: self._open_capture_mob_icon(n)).pack(side="left")
                tk.Button(row3, text="Eliminar mob", bg=RED, fg=BG,
                          font=("Segoe UI", 7, "bold"), relief="flat", padx=8, pady=1,
                          cursor="hand2",
                          command=lambda n=mob_name: self._delete_mob(n)).pack(side="right")

        # ── Botón nuevo mob ───────────────────────────────────────────────
        new_row = tk.Frame(self.mobs_frame, bg=BG)
        new_row.pack(fill="x", padx=10, pady=(4, 6))
        tk.Button(new_row, text="+ Nuevo mob", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=3,
                  cursor="hand2", command=self._new_mob).pack(side="left")

    def _refresh_database_tab(self):
        if self._database_frame is None:
            return
        for w in self._database_frame.winfo_children():
            w.destroy()

        top = tk.Frame(self._database_frame, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 6))
        tk.Label(top, text="Base de datos", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(
            top,
            text="Gestiona aquí las bases manuales de Template ID -> Mob y Actor ID -> Player.",
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        mobs_box = tk.Frame(self._database_frame, bg=PANEL, padx=10, pady=8)
        mobs_box.pack(fill="x", padx=10, pady=(0, 8))
        mobs_header = tk.Frame(mobs_box, bg=PANEL)
        mobs_header.pack(fill="x")
        tk.Label(mobs_header, text="DB mobs por Template ID", bg=PANEL, fg=YELLOW,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(mobs_header, text="Buscar nombre", bg=BLUE, fg=BG,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._prompt_mob_db_name_search).pack(side="right")
        tk.Button(mobs_header, text="Limpiar", bg=BG, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._reset_mob_db_filters).pack(side="right", padx=(0, 4))
        mobs_form = tk.Frame(mobs_box, bg=PANEL)
        mobs_form.pack(fill="x", pady=(6, 6))
        self._template_db_id_var = tk.StringVar()
        self._template_db_name_var = tk.StringVar()
        tk.Label(mobs_form, text="Template ID:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Entry(mobs_form, textvariable=self._template_db_id_var, width=10,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 4))
        tk.Label(mobs_form, text="Nombre:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))
        tk.Entry(mobs_form, textvariable=self._template_db_name_var, width=22,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 4), fill="x", expand=True)
        tk.Button(mobs_form, text="Guardar en base", bg=GREEN, fg=BG,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._save_template_db_entry).pack(side="left")
        tk.Button(mobs_form, text="Refrescar", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._refresh_database_tab).pack(side="left", padx=(4, 0))
        mobs_text_frame = tk.Frame(mobs_box, bg=PANEL)
        mobs_text_frame.pack(fill="both", expand=True)
        mobs_tree = ttk.Treeview(mobs_text_frame, columns=("id", "name"), show="headings", height=8)
        self._mob_db_tree = mobs_tree
        id_heading = self._database_heading_text("Template ID", self._mob_db_filter_id_var.get())
        name_heading = self._database_heading_text("Nombre", self._mob_db_filter_name_var.get(), self._mob_db_search_name_var.get())
        mobs_tree.heading("id", text=id_heading, command=self._open_mob_db_id_menu)
        mobs_tree.heading("name", text=name_heading, command=self._open_mob_db_name_menu)
        mobs_tree.column("id", width=110, anchor="w")
        mobs_tree.column("name", width=320, anchor="w")
        mobs_scroll = ttk.Scrollbar(mobs_text_frame, command=mobs_tree.yview)
        mobs_tree.configure(yscrollcommand=mobs_scroll.set)
        mobs_scroll.pack(side="right", fill="y")
        mobs_tree.pack(side="left", fill="both", expand=True)
        template_db = self._filtered_template_db_items()
        if template_db:
            for template_id, name in template_db:
                mobs_tree.insert("", "end", values=(template_id, name))
        else:
            mobs_tree.insert("", "end", values=("-", "Sin registros manuales aún."))

        players_box = tk.Frame(self._database_frame, bg=PANEL, padx=10, pady=8)
        players_box.pack(fill="x", padx=10, pady=(0, 8))
        players_header = tk.Frame(players_box, bg=PANEL)
        players_header.pack(fill="x")
        tk.Label(players_header, text="DB players por Actor ID", bg=PANEL, fg=GREEN,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(players_header, text="Buscar nombre", bg=BLUE, fg=BG,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._prompt_player_db_name_search).pack(side="right")
        tk.Button(players_header, text="Limpiar", bg=BG, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._reset_player_db_filters).pack(side="right", padx=(0, 4))
        players_form = tk.Frame(players_box, bg=PANEL)
        players_form.pack(fill="x", pady=(6, 6))
        self._player_db_id_var = tk.StringVar()
        self._player_db_name_var = tk.StringVar()
        tk.Label(players_form, text="Actor ID:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Entry(players_form, textvariable=self._player_db_id_var, width=10,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 4))
        tk.Label(players_form, text="Nombre:", bg=PANEL, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))
        tk.Entry(players_form, textvariable=self._player_db_name_var, width=22,
                 bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 4), fill="x", expand=True)
        tk.Button(players_form, text="Guardar en base", bg=GREEN, fg=BG,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._save_player_db_entry).pack(side="left")
        tk.Button(players_form, text="Refrescar", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 7), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._refresh_database_tab).pack(side="left", padx=(4, 0))
        players_text_frame = tk.Frame(players_box, bg=PANEL)
        players_text_frame.pack(fill="both", expand=True)
        players_tree = ttk.Treeview(players_text_frame, columns=("id", "name", "state"), show="headings", height=8)
        self._player_db_tree = players_tree
        actor_heading = self._database_heading_text("Actor ID", self._player_db_filter_id_var.get())
        player_name_heading = self._database_heading_text("Nombre", self._player_db_filter_name_var.get(), self._player_db_search_name_var.get())
        players_tree.heading("id", text=actor_heading, command=self._open_player_db_id_menu)
        players_tree.heading("name", text=player_name_heading, command=self._open_player_db_name_menu)
        players_tree.heading("state", text="Estado")
        players_tree.column("id", width=110, anchor="w")
        players_tree.column("name", width=260, anchor="w")
        players_tree.column("state", width=90, anchor="w")
        players_scroll = ttk.Scrollbar(players_text_frame, command=players_tree.yview)
        players_tree.configure(yscrollcommand=players_scroll.set)
        players_scroll.pack(side="right", fill="y")
        players_tree.pack(side="left", fill="both", expand=True)
        player_db = self._filtered_player_db_items()
        if player_db:
            for actor_id, payload in player_db:
                state = "ON" if payload.get("enabled", True) else "OFF"
                players_tree.insert("", "end", values=(actor_id, payload.get("name", ""), state))
        else:
            players_tree.insert("", "end", values=("-", "Sin players manuales aún.", "-"))

    def _filtered_template_db_items(self):
        items = self._template_db_items_sorted_by_id()
        selected_id = self._mob_db_filter_id_var.get()
        selected_name = self._mob_db_filter_name_var.get()
        if selected_id and selected_id != "Todos":
            items = [item for item in items if item[0] == selected_id]
        if selected_name and selected_name != "Todos":
            items = [item for item in items if item[1] == selected_name]
        sort_mode = self._mob_db_sort_var.get()
        if sort_mode == "ID desc":
            items.sort(key=lambda item: int(item[0]), reverse=True)
        elif sort_mode == "Nombre A-Z":
            items.sort(key=lambda item: (item[1].lower(), int(item[0])))
        elif sort_mode == "Nombre Z-A":
            items.sort(key=lambda item: (item[1].lower(), int(item[0])), reverse=True)
        else:
            items.sort(key=lambda item: int(item[0]))
        search_name = (self._mob_db_search_name_var.get() or "").strip().lower()
        if search_name:
            items = [item for item in items if search_name in item[1].lower()]
        return items

    def _filtered_player_db_items(self):
        items = self._player_db_items_sorted_by_id()
        selected_id = self._player_db_filter_id_var.get()
        selected_name = self._player_db_filter_name_var.get()
        if selected_id and selected_id != "Todos":
            items = [item for item in items if item[0] == selected_id]
        if selected_name and selected_name != "Todos":
            items = [item for item in items if str(item[1].get("name", "")) == selected_name]
        sort_mode = self._player_db_sort_var.get()
        if sort_mode == "ID desc":
            items.sort(key=lambda item: int(item[0]), reverse=True)
        elif sort_mode == "Nombre A-Z":
            items.sort(key=lambda item: (str(item[1].get("name", "")).lower(), int(item[0])))
        elif sort_mode == "Nombre Z-A":
            items.sort(key=lambda item: (str(item[1].get("name", "")).lower(), int(item[0])), reverse=True)
        else:
            items.sort(key=lambda item: int(item[0]))
        search_name = (self._player_db_search_name_var.get() or "").strip().lower()
        if search_name:
            items = [item for item in items if search_name in str(item[1].get("name", "")).lower()]
        return items

    def _template_db_items_sorted_by_id(self):
        template_db = self.config_data.get("leveling", {}).get("template_id_db", {})
        return sorted(
            [(str(template_id), str(name)) for template_id, name in template_db.items()],
            key=lambda item: int(item[0]),
        )

    def _player_db_items_sorted_by_id(self):
        player_db = self._normalized_follow_player_db()
        return sorted(
            [(str(actor_id), payload) for actor_id, payload in player_db.items()],
            key=lambda item: int(item[0]),
        )

    def _reset_mob_db_filters(self):
        self._mob_db_filter_id_var.set("Todos")
        self._mob_db_filter_name_var.set("Todos")
        self._mob_db_search_name_var.set("")
        self._mob_db_sort_var.set("ID asc")
        self._refresh_database_tab()

    def _reset_player_db_filters(self):
        self._player_db_filter_id_var.set("Todos")
        self._player_db_filter_name_var.set("Todos")
        self._player_db_search_name_var.set("")
        self._player_db_sort_var.set("ID asc")
        self._refresh_database_tab()

    def _database_heading_text(self, base: str, selected_filter: str, search_text: str = "") -> str:
        suffix = ""
        if selected_filter and selected_filter != "Todos":
            suffix = f" [{selected_filter}]"
        elif search_text:
            suffix = " [buscar]"
        return f"{base}{suffix} ▼"

    def _post_database_menu(self, commands):
        menu = tk.Menu(self, tearoff=False, bg=BG, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT)
        for item in commands:
            if item is None:
                menu.add_separator()
            else:
                label, callback = item
                menu.add_command(label=label, command=callback)
        menu.post(self.winfo_pointerx(), self.winfo_pointery())

    def _open_mob_db_id_menu(self):
        items = [template_id for template_id, _name in self._template_db_items_sorted_by_id()]
        commands = [("Todos", lambda: self._set_mob_db_filter_id("Todos")), None]
        commands.extend((template_id, lambda value=template_id: self._set_mob_db_filter_id(value)) for template_id in items)
        commands.extend([
            None,
            ("Ordenar ID asc", lambda: self._set_mob_db_sort("ID asc")),
            ("Ordenar ID desc", lambda: self._set_mob_db_sort("ID desc")),
        ])
        self._post_database_menu(commands)

    def _open_mob_db_name_menu(self):
        names = sorted({name for _template_id, name in self._template_db_items_sorted_by_id()}, key=str.lower)
        commands = [("Todos", lambda: self._set_mob_db_filter_name("Todos")), None]
        commands.extend((name, lambda value=name: self._set_mob_db_filter_name(value)) for name in names)
        commands.extend([
            None,
            ("Ordenar Nombre A-Z", lambda: self._set_mob_db_sort("Nombre A-Z")),
            ("Ordenar Nombre Z-A", lambda: self._set_mob_db_sort("Nombre Z-A")),
        ])
        self._post_database_menu(commands)

    def _open_player_db_id_menu(self):
        items = [actor_id for actor_id, _payload in self._player_db_items_sorted_by_id()]
        commands = [("Todos", lambda: self._set_player_db_filter_id("Todos")), None]
        commands.extend((actor_id, lambda value=actor_id: self._set_player_db_filter_id(value)) for actor_id in items)
        commands.extend([
            None,
            ("Ordenar ID asc", lambda: self._set_player_db_sort("ID asc")),
            ("Ordenar ID desc", lambda: self._set_player_db_sort("ID desc")),
        ])
        self._post_database_menu(commands)

    def _open_player_db_name_menu(self):
        names = sorted({str(payload.get("name", "")) for _actor_id, payload in self._player_db_items_sorted_by_id()}, key=str.lower)
        commands = [("Todos", lambda: self._set_player_db_filter_name("Todos")), None]
        commands.extend((name, lambda value=name: self._set_player_db_filter_name(value)) for name in names)
        commands.extend([
            None,
            ("Ordenar Nombre A-Z", lambda: self._set_player_db_sort("Nombre A-Z")),
            ("Ordenar Nombre Z-A", lambda: self._set_player_db_sort("Nombre Z-A")),
        ])
        self._post_database_menu(commands)

    def _set_mob_db_filter_id(self, value: str):
        self._mob_db_filter_id_var.set(value)
        self._refresh_database_tab()

    def _set_mob_db_filter_name(self, value: str):
        self._mob_db_filter_name_var.set(value)
        self._refresh_database_tab()

    def _set_mob_db_sort(self, value: str):
        self._mob_db_sort_var.set(value)
        self._refresh_database_tab()

    def _set_player_db_filter_id(self, value: str):
        self._player_db_filter_id_var.set(value)
        self._refresh_database_tab()

    def _set_player_db_filter_name(self, value: str):
        self._player_db_filter_name_var.set(value)
        self._refresh_database_tab()

    def _set_player_db_sort(self, value: str):
        self._player_db_sort_var.set(value)
        self._refresh_database_tab()

    def _prompt_mob_db_name_search(self):
        value = simpledialog.askstring("Buscar mob", "Buscar nombre de mob:", parent=self, initialvalue=self._mob_db_search_name_var.get())
        if value is None:
            return
        self._mob_db_search_name_var.set(value.strip())
        self._refresh_database_tab()

    def _prompt_player_db_name_search(self):
        value = simpledialog.askstring("Buscar player", "Buscar nombre de player:", parent=self, initialvalue=self._player_db_search_name_var.get())
        if value is None:
            return
        self._player_db_search_name_var.set(value.strip())
        self._refresh_database_tab()

    def _locate_pj_sprite(self):
        """Busca PJ.png en la tarjeta UI y guarda la posición detectada."""
        PJ_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui", "pj", "PJ.png")
        if not os.path.exists(PJ_PATH):
            self._sacro_pos_lbl.config(text="PJ.png no existe", fg=RED)
            self.log_queue.put(("log", "[PJ] PJ.png no encontrado en assets/templates/ui/pj/"))
            return

        self._sacro_pos_lbl.config(text="Buscando...", fg=YELLOW)

        def run():
            info = self._detect_pj_card()
            threshold = info["threshold"]
            best_center = info["best_center"]
            best_score = info["best_score"]
            source = info.get("source")
            rect = info.get("rect")
            match = info["match"]
            click = info["click"]

            if match is not None and click is not None:
                match_x, match_y = match
                abs_x, abs_y = click
                self.config_data["bot"]["sacrogito_self_pos"] = [abs_x, abs_y]
                save_config(self.config_data)
                self._sync_runtime_bot_config()
                self.after(0, lambda: self._sacro_pos_lbl.config(
                    text=f"{abs_x}, {abs_y}", fg=GREEN))
                self.log_queue.put((
                    "log",
                    f"[PJ] Localizado en tarjeta: source={source} match=({match_x}, {match_y}) click=({abs_x}, {abs_y}) rect={rect} | score={best_score:.4f} | threshold={threshold:.4f}"
                ))
            else:
                self.after(0, lambda: self._sacro_pos_lbl.config(
                    text="No encontrado", fg=RED))
                self.log_queue.put((
                    "log",
                    f"[PJ] Tarjeta no detectada | source={source} best={best_center} rect={rect} | score={best_score:.4f} | threshold={threshold:.4f}"
                ))

        threading.Thread(target=run, daemon=True).start()

    def _capture_pj_sprite(self):
        """Abre el recortador para capturar PJ.png — siempre se guarda como PJ.png."""
        PJ_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui", "pj")
        os.makedirs(PJ_DIR, exist_ok=True)
        monitor = self.config_data["game"].get("monitor", 2)

        def on_saved(name):
            self.after(0, lambda: self._sacro_pos_lbl.config(
                text="PJ.png guardado", fg=GREEN))
            self.log_queue.put(("log", "[PJ] PJ.png guardado/actualizado"))

        win = _PJCaptureWindow(self, monitor, on_saved, save_dir=PJ_DIR)
        win.title("Capturar PJ.png")

    def _detect_pj_card(self):
        import numpy as np
        from detector import Detector

        threshold = float(self.config_data["bot"].get("pj_threshold", 0.40) or 0.40)
        detector = Detector(threshold=threshold)
        monitor_idx = self.config_data["game"].get("monitor", 2)
        with mss.mss() as sct:
            monitor = sct.monitors[monitor_idx]
            shot = sct.grab(monitor)
            frame = np.ascontiguousarray(np.array(shot)[:, :, :3])

        fh, fw = frame.shape[:2]
        gx1 = int(fw * 0.70)
        gx2 = int(fw * 0.97)
        gy1 = int(fh * 0.68)
        gy2 = int(fh * 0.97)
        crop = frame[gy1:gy2, gx1:gx2]
        band_h = max(1, int(crop.shape[0] * 0.32))
        top_band = crop[:band_h, :]
        band_center, band_score = detector.best_match(top_band, "PJ", "ui/pj")
        if band_center is not None and band_score >= max(0.75, threshold):
            match_x = monitor["left"] + gx1 + int(band_center[0])
            match_y = monitor["top"] + gy1 + int(band_center[1])
            return {
                "threshold": threshold,
                "roi": (gx1, gy1, gx2, gy1 + band_h),
                "hits": [],
                "best_center": band_center,
                "best_score": band_score,
                "match": (match_x, match_y),
                "click": (match_x, match_y),
                "source": "pj_band",
                "rect": None,
            }
        marker_center, marker_rect, marker_score = self._find_selected_card_by_marker(crop)
        if marker_center is not None:
            click_local = self._marker_click_point(marker_rect, marker_center)
            match_x = monitor["left"] + gx1 + int(marker_center[0])
            match_y = monitor["top"] + gy1 + int(marker_center[1])
            click_x = monitor["left"] + gx1 + int(click_local[0])
            click_y = monitor["top"] + gy1 + int(click_local[1])
            return {
                "threshold": threshold,
                "roi": (gx1, gy1, gx2, gy2),
                "hits": [],
                "best_center": marker_center,
                "best_score": marker_score,
                "match": (match_x, match_y),
                "click": (click_x, click_y),
                "source": "card_marker",
                "rect": marker_rect,
            }
        card_center, card_rect, card_score, pj_score = self._find_selected_card_in_crop(crop, detector)
        if card_center is not None:
            click_local = self._card_click_point(card_rect, card_center)
            match_x = monitor["left"] + gx1 + int(card_center[0])
            match_y = monitor["top"] + gy1 + int(card_center[1])
            click_x = monitor["left"] + gx1 + int(click_local[0])
            click_y = monitor["top"] + gy1 + int(click_local[1])
            return {
                "threshold": threshold,
                "roi": (gx1, gy1, gx2, gy2),
                "hits": [],
                "best_center": card_center,
                "best_score": card_score,
                "match": (match_x, match_y),
                "click": (click_x, click_y),
                "source": "card_rect",
                "rect": card_rect,
                "pj_score": pj_score,
            }

        best_center, best_score = detector.best_match(crop, "PJ", "ui/pj")
        return {
            "threshold": threshold,
            "roi": (gx1, gy1, gx2, gy2),
            "hits": [],
            "best_center": best_center,
            "best_score": best_score,
            "match": None,
            "click": None,
            "source": "not_found",
            "rect": None,
        }

    def _marker_click_point(self, rect, fallback):
        if rect is None:
            return fallback
        x, y, w, h = rect
        return (x + (w // 2), y + h + max(10, min(22, h)))

    def _card_click_point(self, rect, fallback):
        if rect is None:
            return fallback
        x, y, w, h = rect
        return (x + (w // 2), y + max(16, min(34, h // 3)))

    def _find_selected_card_by_marker(self, crop):
        if crop.size == 0:
            return None, None, 0.0
        crop_h, crop_w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        orange_mask = cv2.inRange(hsv, np.array([5, 90, 100]), np.array([30, 255, 255]))
        kernel = np.ones((3, 3), np.uint8)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(orange_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_center = None
        best_rect = None
        best_score = 0.0
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < 60 or area > 1200:
                continue
            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.08 * peri, True)
            x, y, w, h = cv2.boundingRect(contour)
            if y > int(crop_h * 0.35):
                continue
            if w < 12 or w > 70 or h < 10 or h > 55:
                continue
            if len(approx) < 3 or len(approx) > 6:
                continue

            cx = x + w // 2
            wy1 = min(crop_h - 1, y + h + 2)
            wy2 = min(crop_h, wy1 + 150)
            wx1 = max(0, cx - 70)
            wx2 = min(crop_w, cx + 70)
            window = crop[wy1:wy2, wx1:wx2]
            if window.size == 0:
                continue
            win_hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
            white_mask = cv2.inRange(win_hsv, np.array([0, 0, 145]), np.array([180, 85, 255]))
            white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_OPEN, kernel, iterations=1)
            white_contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for wcontour in white_contours:
                wx, wy, ww, wh = cv2.boundingRect(wcontour)
                warea = float(cv2.contourArea(wcontour))
                if ww < 20 or ww > 90 or wh < 45 or wh > 130:
                    continue
                if warea < 800:
                    continue
                aspect = float(wh) / float(max(ww, 1))
                if aspect < 1.1 or aspect > 3.8:
                    continue
                white_ratio = warea / float(max(ww * wh, 1))
                if white_ratio < 0.45:
                    continue
                card_roi = window[wy:wy + wh, wx:wx + ww]
                if card_roi.size == 0:
                    continue
                card_hsv = cv2.cvtColor(card_roi, cv2.COLOR_BGR2HSV)
                red_mask_1 = cv2.inRange(card_hsv, np.array([0, 90, 70]), np.array([12, 255, 255]))
                red_mask_2 = cv2.inRange(card_hsv, np.array([170, 90, 70]), np.array([180, 255, 255]))
                red_mask = cv2.bitwise_or(red_mask_1, red_mask_2)
                stripe_x1 = max(0, int(ww * 0.68))
                stripe_x2 = min(ww, int(ww * 0.95))
                stripe = red_mask[:, stripe_x1:stripe_x2]
                if stripe.size == 0:
                    red_ratio = 0.0
                else:
                    red_ratio = float(cv2.countNonZero(stripe)) / float(max(stripe.shape[0] * stripe.shape[1], 1))
                top_bias = max(0.0, (crop_h - y) * 2.5)
                score = (area * 2.0) + warea + (white_ratio * 900.0) + (red_ratio * 900.0) + top_bias
                if score > best_score:
                    best_score = score
                    best_rect = (wx1 + wx, wy1 + wy, ww, wh)
                    best_center = (wx1 + wx + ww // 2, wy1 + wy + wh // 2)
        return best_center, best_rect, best_score

    def _find_selected_card_in_crop(self, crop, detector):
        if crop.size == 0:
            return None, None, 0.0, 0.0
        crop_h, crop_w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        orange_mask = cv2.inRange(hsv, np.array([5, 90, 100]), np.array([30, 255, 255]))
        kernel = np.ones((3, 3), np.uint8)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_CLOSE, kernel, iterations=3)
        orange_mask = cv2.dilate(orange_mask, kernel, iterations=1)
        orange_mask = cv2.morphologyEx(orange_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(orange_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_center = None
        best_rect = None
        best_score = 0.0
        best_pj_score = 0.0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if y > int(crop_h * 0.20):
                continue
            if w < 70 or w > 150 or h < 70 or h > 150:
                continue
            area = float(cv2.contourArea(contour))
            if area < 2000:
                continue
            aspect = float(h) / float(max(w, 1))
            if aspect < 0.80 or aspect > 1.25:
                continue
            inner_x1 = x + max(6, int(w * 0.16))
            inner_x2 = x + min(w - 6, int(w * 0.84))
            inner_y1 = y + max(6, int(h * 0.10))
            inner_y2 = y + min(h - 6, int(h * 0.86))
            if inner_x2 <= inner_x1 or inner_y2 <= inner_y1:
                continue
            inner = crop[inner_y1:inner_y2, inner_x1:inner_x2]
            pj_center, pj_score = detector.best_match(inner, "PJ", "ui/pj")
            score = area - (y * 8.0) + (pj_score * 6000.0)
            if score > best_score:
                best_score = score
                best_rect = (x, y, w, h)
                best_center = (x + w // 2, y + h // 2)
                best_pj_score = pj_score
        return best_center, best_rect, best_score, best_pj_score

    def _check_pj_visible(self):
        """Escanea la pantalla actual y reporta si PJ.png esta visible en la tarjeta UI."""
        PJ_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui", "pj", "PJ.png")
        if not os.path.exists(PJ_PATH):
            self._pj_visible_lbl.config(text="PJ.png no existe", fg=RED)
            self.log_queue.put(("log", "[PJ] PJ.png no encontrado en assets/templates/ui/pj/"))
            return

        self._pj_visible_lbl.config(text="Buscando...", fg=YELLOW)

        def run():
            info = self._detect_pj_card()
            gx1, gy1, gx2, gy2 = info["roi"]
            threshold = info["threshold"]
            best_center = info["best_center"]
            best_score = info["best_score"]
            source = info.get("source")
            rect = info.get("rect")
            hits = info["hits"]
            match = info["match"]

            if match is not None:
                abs_x, abs_y = match
                self.after(0, lambda: self._pj_visible_lbl.config(text="Visible", fg=GREEN))
                self.log_queue.put((
                    "log",
                    f"[PJ] Visible: source={source} hits={len(hits)} mejor=({abs_x}, {abs_y}) rect={rect} | score={best_score:.4f} | threshold={threshold:.4f} | roi=({gx1},{gy1})-({gx2},{gy2})"
                ))
            else:
                self.after(0, lambda: self._pj_visible_lbl.config(text="No visible", fg=RED))
                self.log_queue.put((
                    "log",
                    f"[PJ] No visible en pantalla | source={source} best={best_center} rect={rect} | score={best_score:.4f} | threshold={threshold:.4f} | roi=({gx1},{gy1})-({gx2},{gy2})"
                ))

        threading.Thread(target=run, daemon=True).start()

    def _test_pj_click(self):
        """Detecta PJ.png en la tarjeta y hace un click de prueba en el punto final."""
        PJ_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "templates", "ui", "pj", "PJ.png")
        if not os.path.exists(PJ_PATH):
            self.log_queue.put(("log", "[PJ] PJ.png no encontrado en assets/templates/ui/pj/"))
            return

        def run():
            import pyautogui
            info = self._detect_pj_card()
            threshold = info["threshold"]
            best_center = info["best_center"]
            best_score = info["best_score"]
            source = info.get("source")
            rect = info.get("rect")
            match = info["match"]
            click = info["click"]
            if match is None or click is None:
                self.log_queue.put((
                    "log",
                    f"[PJ] Test click fallido: no visible | source={source} best={best_center} rect={rect} | score={best_score:.4f} | threshold={threshold:.4f}"
                ))
                return

            match_x, match_y = match
            click_x, click_y = click
            pyautogui.moveTo(click_x, click_y, duration=0.08)
            pyautogui.click(click_x, click_y)
            self.log_queue.put((
                "log",
                f"[PJ] Test click ejecutado: source={source} match=({match_x}, {match_y}) click=({click_x}, {click_y}) rect={rect} | score={best_score:.4f}"
            ))

        threading.Thread(target=run, daemon=True).start()

    def _save_attack_offset(self):
        try:
            dx = int(self._atk_off_x.get())
            dy = int(self._atk_off_y.get())
        except ValueError:
            return
        lev = self.config_data.setdefault("leveling", {})
        lev["attack_menu_offset"] = [dx, dy]
        save_config(self.config_data)
        self._sync_runtime_bot_config()

    def _refresh_leveling_route_options(self):
        if hasattr(self, "_resource_route_cb") and self._resource_route_cb is not None:
            route_names = sorted(self._route_profiles_cfg().keys())
            self._resource_route_cb["values"] = route_names
            current_resource = (self._resource_route_var.get() or "").strip() if hasattr(self, "_resource_route_var") else ""
            if current_resource not in route_names and route_names:
                self._resource_route_var.set(route_names[0])
        if not hasattr(self, "_leveling_route_cb") or self._leveling_route_cb is None:
            return
        route_names = sorted(self._route_profiles_cfg().keys())
        self._leveling_route_cb["values"] = route_names
        current = (self._leveling_route_var.get() or "").strip()
        if current not in route_names and route_names:
            self._leveling_route_var.set(route_names[0])

    def _save_resource_route_profile(self):
        route_name = (self._resource_route_var.get() or "").strip()
        if not route_name:
            return
        farming = self.config_data.setdefault("farming", {})
        farming["route_profile"] = route_name
        save_config(self.config_data)
        self._sync_runtime_bot_config()

    def _save_leveling_route_profile(self):
        route_name = (self._leveling_route_var.get() or "").strip()
        if not route_name:
            return
        lev = self.config_data.setdefault("leveling", {})
        lev["route_profile"] = route_name
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()

    def _update_leveling_route_toggle_button(self):
        if not hasattr(self, "_leveling_route_toggle_btn"):
            return
        enabled = bool(self.config_data.get("navigation", {}).get("enabled", True))
        if enabled:
            self._leveling_route_toggle_btn.config(text="Ruta: ON", bg=GREEN, fg=BG)
        else:
            self._leveling_route_toggle_btn.config(text="Ruta: OFF", bg=RED, fg=TEXT)

    def _toggle_leveling_route_enabled(self):
        nav = self.config_data.setdefault("navigation", {})
        nav["enabled"] = not bool(nav.get("enabled", True))
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._update_leveling_route_toggle_button()
        state = "activada" if self.config_data.get("navigation", {}).get("enabled", True) else "detenida"
        self.log_queue.put(("log", f"[NAV] Ruta de leveling {state}"))

    def _toggle_leveling_mode(self):
        current = self.config_data["farming"].get("mode", "resource")
        new_mode = "resource" if current == "leveling" else "leveling"
        self.config_data["farming"]["mode"] = new_mode
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._refresh_mobs()

    def _scan_mob_on_map(self, mob_name: str, lbl: tk.Label):
        """Escanea el área de juego (sin UI) y muestra cuántos mobs detecta."""
        lbl.config(text="...", fg=YELLOW)
        self.update_idletasks()

        def run():
            import numpy as np
            from detector import Detector
            threshold = self.config_data["bot"].get("threshold", 0.55)
            detector  = Detector(threshold=threshold)
            monitor_idx = self.config_data["game"].get("monitor", 2)
            with mss.mss() as sct:
                monitor = sct.monitors[monitor_idx]
                shot    = sct.grab(monitor)
                frame   = np.ascontiguousarray(np.array(shot)[:, :, :3])
            # Recortar al área de juego para evitar falsos positivos en la UI
            fh, fw = frame.shape[:2]
            gx1 = int(fw * 0.14)
            gx2 = int(fw * 0.90)
            gy1 = int(fh * 0.09)
            gy2 = int(fh * 0.70)
            game_frame = frame[gy1:gy2, gx1:gx2]
            matches = detector.find_all_mob_sprites(game_frame, mob_name)
            count   = len(matches)
            color   = GREEN if count > 0 else RED
            text    = f"{count} mob(s) en mapa"
            lbl.config(text=text, fg=color)

        threading.Thread(target=run, daemon=True).start()

    def _delete_mob(self, mob_name: str):
        import shutil
        ok = messagebox.askyesno(
            "Eliminar mob",
            f"¿Eliminar mob '{mob_name}' y todos sus sprites?",
            parent=self,
        )
        if not ok:
            return
        mob_dir = os.path.join(MOBS_DIR, mob_name)
        if os.path.isdir(mob_dir):
            shutil.rmtree(mob_dir, ignore_errors=True)
        lev = self.config_data.get("leveling", {})
        lev.get("mobs", {}).pop(mob_name, None)
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._refresh_mobs()

    def _new_mob(self):
        name = simpledialog.askstring("Nuevo mob", "Nombre del mob:", parent=self)
        if not name:
            return
        name = name.strip()
        os.makedirs(os.path.join(MOBS_DIR, name), exist_ok=True)
        lev = self.config_data.setdefault("leveling", {})
        mobs = lev.setdefault("mobs", {})
        if name not in mobs:
            mobs[name] = {"enabled": True}
            save_config(self.config_data)
        self._sync_runtime_bot_config()
        self._refresh_mobs()

    def _open_capture_sprite(self, mob_name: str):
        save_dir = os.path.join(MOBS_DIR, mob_name)
        os.makedirs(save_dir, exist_ok=True)
        monitor = self.config_data["game"].get("monitor", 2)

        def on_saved(name):
            # Asegurar que el mob está registrado en config con enabled=True
            lev = self.config_data.setdefault("leveling", {})
            mobs = lev.setdefault("mobs", {})
            if mob_name not in mobs:
                mobs[mob_name] = {"enabled": True}
                save_config(self.config_data)
            self.config_data = load_config()
            self._sync_runtime_bot_config()
            self._refresh_mobs()

        ResourceCaptureWindow(self, monitor, on_saved, save_dir=save_dir)

    def _mob_icon_path(self, mob_name: str) -> str:
        return os.path.join(MOBS_DIR, mob_name, "_icon.png")

    def _load_mob_icon_photo(self, mob_name: str):
        icon_path = self._mob_icon_path(mob_name)
        if not os.path.exists(icon_path):
            return None
        key = (mob_name, "_icon")
        try:
            img = Image.open(icon_path)
            img.thumbnail((40, 40), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.mob_images[key] = photo
            return photo
        except Exception:
            return None

    def _toggle_mob_card(self, mob_name: str):
        current = bool(self._mob_card_collapsed.get(mob_name, True))
        self._mob_card_collapsed[mob_name] = not current
        self._refresh_mobs()

    def _on_mob_search_changed(self, *_args):
        if self.mobs_frame is None or not self.mobs_frame.winfo_exists():
            return
        self.after_idle(self._refresh_mobs_preserving_search_focus)

    def _refresh_mobs_preserving_search_focus(self):
        search_text = self._mob_search_var.get() or ""
        self._refresh_mobs()
        if self._mob_search_entry is not None and self._mob_search_entry.winfo_exists():
            try:
                self._mob_search_entry.focus_set()
                self._mob_search_entry.icursor(len(search_text))
            except Exception:
                pass

    def _clear_mob_search(self):
        self._mob_search_var.set("")

    def _open_capture_mob_icon(self, mob_name: str):
        save_dir = os.path.join(MOBS_DIR, mob_name)
        os.makedirs(save_dir, exist_ok=True)
        monitor = self.config_data["game"].get("monitor", 2)

        def on_saved(_name):
            self.config_data = load_config()
            self._sync_runtime_bot_config()
            self._refresh_mobs()

        _MobIconCaptureWindow(self, monitor, on_saved, save_dir=save_dir)

    def _check_mob(self, mob_name: str, lbl: tk.Label):
        lbl.config(text="...", fg=YELLOW)
        self.update_idletasks()

        def run():
            import numpy as np
            from detector import Detector
            threshold = self.config_data["bot"].get("threshold", 0.55)
            detector = Detector(threshold=threshold)
            monitor_idx = self.config_data["game"].get("monitor", 2)
            with mss.mss() as sct:
                monitor = sct.monitors[monitor_idx]
                shot = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(shot)[:, :, :3])
            matches = detector.find_all_mob_sprites(frame, mob_name)
            count = len(matches)
            color = GREEN if count > 0 else RED
            lbl.config(text=f"{count} det.", fg=color)

        threading.Thread(target=run, daemon=True).start()

    def _update_mobs(self):
        lev = self.config_data.setdefault("leveling", {})
        mobs = lev.setdefault("mobs", {})
        for mob_name, var in self.mob_vars.items():
            if mob_name not in mobs:
                mobs[mob_name] = {}
            mobs[mob_name]["enabled"] = var.get()
        for mob_name, var in self.mob_ignore_vars.items():
            if mob_name not in mobs:
                mobs[mob_name] = {}
            mobs[mob_name]["ignore"] = bool(var.get())
        save_config(self.config_data)
        self._sync_runtime_bot_config()
        if self.bot_thread and self.bot_thread.bot:
            self.bot_thread.bot.mob_pending = []
            if self.bot_thread.bot.state == "click_mob":
                self.bot_thread.bot.state = "scan_mobs"

    def _disable_all_mobs(self):
        lev = self.config_data.setdefault("leveling", {})
        mobs = lev.setdefault("mobs", {})
        for mob_name in _list_mobs():
            mob_cfg = mobs.setdefault(mob_name, {})
            mob_cfg["enabled"] = False
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        if self.bot_thread and self.bot_thread.bot:
            self.bot_thread.bot.mob_pending = []
            if self.bot_thread.bot.state == "click_mob":
                self.bot_thread.bot.state = "scan_mobs"
        self.log_queue.put(("log", "[MOBS] Todos los mobs quedaron desactivados"))
        self._refresh_mobs()

    def _save_mob_template_ids(self, mob_name: str):
        raw = (self.mob_template_vars.get(mob_name).get() if mob_name in self.mob_template_vars else "").strip()
        values: list[int] = []
        if raw:
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    values.append(int(token))
                except ValueError:
                    messagebox.showwarning(
                        "Template ID inválido",
                        f"'{token}' no es un entero válido para {mob_name}. Usa valores separados por coma.",
                        parent=self,
                    )
                    return
        lev = self.config_data.setdefault("leveling", {})
        mobs = lev.setdefault("mobs", {})
        mob_cfg = mobs.setdefault(mob_name, {})
        mob_cfg["template_ids"] = values
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[MOBS] {mob_name} template_ids guardados: {values or '[]'}"))
    def _save_mob_group_veto_template_ids(self):
        raw = (self._mob_veto_template_ids_var.get() if hasattr(self, "_mob_veto_template_ids_var") else "").strip()
        values: list[int] = []
        if raw:
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    values.append(int(token))
                except ValueError:
                    messagebox.showwarning(
                        "Template ID inv?lido",
                        f"'{token}' no es un entero v?lido. Usa Template IDs separados por coma.",
                        parent=self,
                    )
                    return
        leveling_cfg = self.config_data.setdefault("leveling", {})
        leveling_cfg["mob_group_veto_template_ids"] = values
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        rendered = ", ".join(str(v) for v in values) if values else "(vac?o)"
        self.log_queue.put(("log", f"[MOBS] veto de grupos por template_id guardado: {rendered}"))

    def _save_ignore_single_mob_groups(self):
        leveling_cfg = self.config_data.setdefault("leveling", {})
        enabled = bool(self._ignore_single_mob_groups_var.get()) if hasattr(self, "_ignore_single_mob_groups_var") else False
        leveling_cfg["ignore_single_mob_groups"] = enabled
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        state = "activo" if enabled else "inactivo"
        self.log_queue.put(("log", f"[MOBS] ignorar grupos de 1 mob: {state}"))

    def _save_template_db_entry(self):
        raw_id = (self._template_db_id_var.get() or "").strip()
        raw_name = (self._template_db_name_var.get() or "").strip()
        if not raw_id or not raw_name:
            messagebox.showwarning(
                "Dato faltante",
                "Ingresa Template ID y nombre para guardar en la base manual.",
                parent=self,
            )
            return
        try:
            template_id = int(raw_id)
        except ValueError:
            messagebox.showwarning(
                "Template ID inválido",
                f"'{raw_id}' no es un entero válido.",
                parent=self,
            )
            return
        leveling_cfg = self.config_data.setdefault("leveling", {})
        db_cfg = leveling_cfg.setdefault("template_id_db", {})
        db_cfg[str(template_id)] = raw_name
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[MOBS] template_id_db guardado: {template_id} -> {raw_name}"))
        self._refresh_database_tab()

    def _save_player_db_entry(self):
        raw_id = (self._player_db_id_var.get() or "").strip()
        raw_name = (self._player_db_name_var.get() or "").strip()
        if not raw_id or not raw_name:
            messagebox.showwarning(
                "Dato faltante",
                "Ingresa Actor ID y nombre para guardar el player en la base manual.",
                parent=self,
            )
            return
        try:
            actor_id = int(raw_id)
        except ValueError:
            messagebox.showwarning(
                "Actor ID invalido",
                f"'{raw_id}' no es un entero valido.",
                parent=self,
            )
            return
        if actor_id <= 0:
            messagebox.showwarning(
                "Actor ID invalido",
                "El Actor ID del player debe ser positivo.",
                parent=self,
            )
            return
        normalized = self._normalized_follow_player_db()
        normalized[str(actor_id)] = {"name": raw_name, "enabled": True}
        self._save_normalized_follow_player_db(normalized)
        leveling_cfg = self.config_data.setdefault("leveling", {})
        leveling_cfg["follow_players_enabled"] = True
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        if hasattr(self, "_follow_players_var"):
            self._follow_players_var.set(True)
        self._refresh_follow_player_controls(preferred_actor_id=str(actor_id))
        self._refresh_external_fight_join_controls()
        self.log_queue.put(("log", f"[PLAYERS] follow_player_db guardado: {actor_id} -> {raw_name}"))
        self._refresh_database_tab()
        self._refresh_mobs()
        self._refresh_mobs()

    def _open_leveling_db_view(self):
        win = tk.Toplevel(self)
        win.title("Auto-nivel - Bases manuales")
        win.configure(bg=BG)
        win.geometry("760x520")

        tk.Label(
            win,
            text="Bases manuales de mobs y players usadas por auto-nivel.",
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=10, pady=(8, 4))

        body = tk.Frame(win, bg=BG, padx=10, pady=6)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        tk.Label(left, text="DB mobs por Template ID", bg=BG, fg=YELLOW,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        mobs_text = tk.Text(left, bg=PANEL, fg=TEXT, font=("Consolas", 8),
                            relief="flat", wrap="word", state="normal")
        mobs_scroll = ttk.Scrollbar(left, command=mobs_text.yview)
        mobs_text.configure(yscrollcommand=mobs_scroll.set)
        mobs_scroll.pack(side="right", fill="y", pady=(4, 0))
        mobs_text.pack(side="left", fill="both", expand=True, pady=(4, 0))

        template_db = self.config_data.get("leveling", {}).get("template_id_db", {})
        if template_db:
            for template_id, name in sorted(template_db.items(), key=lambda item: int(item[0])):
                mobs_text.insert("end", f"{template_id} -> {name}\n")
        else:
            mobs_text.insert("end", "Sin registros manuales aún.\n")
        mobs_text.config(state="disabled")

        tk.Label(right, text="DB players por Actor ID", bg=BG, fg=GREEN,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        players_text = tk.Text(right, bg=PANEL, fg=TEXT, font=("Consolas", 8),
                               relief="flat", wrap="word", state="normal")
        players_scroll = ttk.Scrollbar(right, command=players_text.yview)
        players_text.configure(yscrollcommand=players_scroll.set)
        players_scroll.pack(side="right", fill="y", pady=(4, 0))
        players_text.pack(side="left", fill="both", expand=True, pady=(4, 0))

        player_db = self._normalized_follow_player_db()
        if player_db:
            for actor_id, payload in sorted(player_db.items(), key=lambda item: int(item[0])):
                state = "ON" if payload.get("enabled", True) else "OFF"
                players_text.insert("end", f"{actor_id} -> {payload.get('name', '')} [{state}]\n")
        else:
            players_text.insert("end", "Sin players manuales aún.\n")
        players_text.config(state="disabled")

    def _save_map_origin_by_map_id(self, map_id: int, origin: dict):
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        by_map_id = cal_cfg.setdefault("map_origins_by_map_id", {})
        by_map_id[str(map_id)] = {
            "x": round(float(origin["x"]), 2),
            "y": round(float(origin["y"]), 2),
        }
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()

    def _save_world_map_calibration_sample(self, calibration: dict):
        map_id = calibration.get("map_id")
        if map_id is None:
            return
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        samples_by_map = cal_cfg.setdefault("world_map_samples_by_map_id", {})
        map_key = str(int(map_id))
        samples = samples_by_map.setdefault(map_key, [])
        sample = {
            "cell_id": int(calibration["cell_id"]),
            "screen_x": int(calibration["click_pos"][0]),
            "screen_y": int(calibration["click_pos"][1]),
            "grid_x": int(calibration["grid_xy"][0]),
            "grid_y": int(calibration["grid_xy"][1]),
            "actor_id": str(calibration.get("actor_id") or ""),
            "entity_kind": str(calibration.get("entity_kind") or ""),
            "mob_name": str(calibration.get("mob_name") or ""),
            "saved_at": round(time.time(), 3),
        }
        replaced = False
        for idx, existing in enumerate(list(samples)):
            try:
                if int(existing.get("cell_id")) == sample["cell_id"]:
                    samples[idx] = sample
                    replaced = True
                    break
            except (TypeError, ValueError, AttributeError):
                continue
        if not replaced:
            samples.append(sample)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()

    def _delete_world_map_sample(self, map_id: int, cell_id: int) -> bool:
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        samples_by_map = cal_cfg.setdefault("world_map_samples_by_map_id", {})
        map_key = str(int(map_id))
        samples = samples_by_map.get(map_key)
        if not isinstance(samples, list):
            return False
        filtered = []
        removed = False
        for sample in samples:
            try:
                sample_cell = int(sample.get("cell_id"))
            except (TypeError, ValueError, AttributeError):
                filtered.append(sample)
                continue
            if sample_cell == int(cell_id) and not removed:
                removed = True
                continue
            filtered.append(sample)
        if not removed:
            return False
        if filtered:
            samples_by_map[map_key] = filtered
        else:
            samples_by_map.pop(map_key, None)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        return True

    def _clear_world_map_samples(self, map_id: int) -> int:
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        samples_by_map = cal_cfg.setdefault("world_map_samples_by_map_id", {})
        map_key = str(int(map_id))
        samples = samples_by_map.pop(map_key, [])
        count = len(samples) if isinstance(samples, list) else 0
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        return count

    def _start_sniffer_map_calibration(self, entry: dict | None, status_var: tk.StringVar | None = None):
        if not entry:
            messagebox.showinfo("Sin selección", "Selecciona una entidad o una celda del overlay primero.", parent=self)
            return
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            messagebox.showinfo("Bot detenido", "Inicia el bot con sniffer antes de calibrar.", parent=self)
            return
        map_id = bot._current_map_id
        if map_id is None:
            messagebox.showwarning("Sin map_id", "No hay map_id actual en runtime.", parent=self)
            return
        try:
            cell_id = int(entry.get("cell_id"))
        except (TypeError, ValueError):
            messagebox.showwarning("Cell inválida", "La entidad seleccionada no tiene un cell_id válido.", parent=self)
            return
        actor_id = str(entry.get("actor_id") or "?").strip() or "?"
        label = "celda" if str(entry.get("entity_kind") or "") == "cell" else "actor"
        if status_var is not None:
            status_var.set(
                f"Acomoda el mouse en el punto exacto de {label}={actor_id} cell={cell_id}. Capturo en 3s..."
            )

        def _do():
            time.sleep(3)
            mouse_x, mouse_y = pyautogui.position()
            self.after(
                0,
                lambda: self._finish_sniffer_map_calibration(
                    int(map_id),
                    int(cell_id),
                    (int(mouse_x), int(mouse_y)),
                    dict(entry),
                    status_var,
                ),
            )

        threading.Thread(target=_do, daemon=True).start()

    def _finish_sniffer_map_calibration(
        self,
        map_id: int,
        cell_id: int,
        mouse_pos: tuple[int, int],
        entry: dict,
        status_var: tk.StringVar | None = None,
    ):
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            if status_var is not None:
                status_var.set("Bot detenido")
            return
        calibration = bot.estimate_map_origin_from_click(cell_id, mouse_pos, map_id=map_id)
        if not calibration:
            messagebox.showerror(
                "Calibración fallida",
                "No pude calcular el origen. Revisa slopes y map_width en cell_calibration.",
                parent=self,
            )
            if status_var is not None:
                status_var.set("Falló cálculo de origen")
            return

        actor_id = entry.get("actor_id", "")
        resolved = ", ".join(entry.get("resolved_mobs", [])) or str(entry.get("mob_signature") or actor_id or "?")
        grid_xy = calibration.get("grid_xy")
        projected = calibration.get("projected")
        calibration["actor_id"] = actor_id
        calibration["entity_kind"] = str(entry.get("entity_kind") or "")
        calibration["mob_name"] = resolved
        self._save_world_map_calibration_sample(calibration)
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        affine = bot._fit_world_map_affine(map_id) if bot else None
        sample_count = len((self.config_data.get("bot", {}).get("cell_calibration", {}).get("world_map_samples_by_map_id", {}) or {}).get(str(map_id), []))
        if status_var is not None:
            status_var.set(
                f"Guardado map_id={map_id} cell={cell_id} | muestras={sample_count}"
                + (" | affine lista" if affine else " | faltan más muestras")
            )
            self.after(3500, lambda: status_var.set(""))
        self.log_queue.put((
            "log",
            "[CAL] map_id={} actor={} mob={} cell={} grid={} mouse={} projected={} samples={} affine={}".format(
                map_id,
                actor_id,
                resolved,
                cell_id,
                grid_xy,
                mouse_pos,
                projected,
                sample_count,
                "ready" if affine else "pending",
            ),
        ))

    def _move_mouse_to_sniffer_selection(self, entry: dict | None, status_var: tk.StringVar | None = None):
        if not entry:
            messagebox.showinfo("Sin selección", "Selecciona una entidad del mapa primero.", parent=self)
            return
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            messagebox.showinfo("Bot detenido", "Inicia el bot con sniffer antes de probar la proyección.", parent=self)
            return
        projection = self._project_sniffer_entry_with_visual_grid(entry)
        if not projection:
            projection = bot.project_map_entity_to_screen(entry)
        if not projection:
            messagebox.showwarning(
                "Sin proyección",
                "No pude proyectar esa entidad a pantalla. Revisa la calibración del mapa actual.",
                parent=self,
            )
            if status_var is not None:
                status_var.set("Sin proyección para la selección")
            return
        screen_pos = projection["screen_pos"]
        try:
            pyautogui.moveTo(int(screen_pos[0]), int(screen_pos[1]), duration=0.12)
        except Exception as exc:
            messagebox.showerror("Movimiento fallido", f"No pude mover el mouse: {exc}", parent=self)
            if status_var is not None:
                status_var.set("Falló moveTo")
            return
        actor_id = projection.get("actor_id") or "?"
        cell_id = projection.get("cell_id")
        grid_xy = projection.get("grid_xy")
        map_id = projection.get("map_id")
        message = (
            f"Mouse movido a map_id={map_id if map_id is not None else '?'} "
            f"| actor={actor_id} | cell={cell_id} | grid={grid_xy} | pos={screen_pos}"
        )
        if status_var is not None:
            status_var.set(message)
            self.after(3500, lambda: status_var.set(""))
        self.log_queue.put(("log", f"[TEST] {message}"))

    def _project_sniffer_entry_with_visual_grid(self, entry: dict | None) -> dict | None:
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None or not entry:
            return None
        map_id = self._current_runtime_map_id()
        if map_id is None:
            return None
        try:
            cell_id = int(entry.get("cell_id"))
        except (TypeError, ValueError):
            return None

        monitor = dict(bot.screen.monitor)
        cell_cal = self.config_data.get("bot", {}).get("cell_calibration", {}) or {}
        by_map = cell_cal.get("visual_grid_by_map_id", {}) or {}
        raw = by_map.get(str(map_id), {}) or {}
        settings = self._get_visual_grid_settings(int(map_id), int(monitor["width"]), int(monitor["height"]))
        saved_width = float(raw.get("canvas_width", monitor["width"]) or monitor["width"])
        saved_height = float(raw.get("canvas_height", monitor["height"]) or monitor["height"])
        scale_x = float(monitor["width"]) / max(saved_width, 1.0)
        scale_y = float(monitor["height"]) / max(saved_height, 1.0)

        cell_width = float(settings.get("cell_width", 0.0) or 0.0) * scale_x
        cell_height = float(settings.get("cell_height", 0.0) or 0.0) * scale_y
        offset_x = float(settings.get("offset_x", 0.0) or 0.0) * scale_x
        offset_y = float(settings.get("offset_y", 0.0) or 0.0) * scale_y
        if cell_width <= 0 or cell_height <= 0:
            return None

        mid_w = cell_width / 2.0
        mid_h = cell_height / 2.0
        map_cells = bot.get_current_map_cells_snapshot()
        map_cell = None
        for item in map_cells:
            try:
                if int(item.get("cell_id")) == cell_id:
                    map_cell = item
                    break
            except (TypeError, ValueError, AttributeError):
                continue

        if map_cell is not None:
            grid_x = float(map_cell.get("x", 0.0))
            grid_y = float(map_cell.get("y", 0.0))
        else:
            grid_x, grid_y = cell_id_to_grid(cell_id, 15)
            grid_x = float(grid_x)
            grid_y = float(grid_y)
        grid_xy = cell_id_to_grid(cell_id, 15)

        iso_x = (grid_x - grid_y) * mid_w
        iso_y = (grid_x + grid_y) * mid_h
        center_x = offset_x + iso_x + mid_w
        center_y = offset_y + iso_y + mid_h
        screen_pos = (
            int(round(monitor["left"] + center_x)),
            int(round(monitor["top"] + center_y)),
        )
        return {
            "map_id": int(map_id),
            "actor_id": str(entry.get("actor_id", "")).strip(),
            "cell_id": cell_id,
            "grid_xy": grid_xy,
            "entity_kind": str(entry.get("entity_kind", "")).strip(),
            "screen_pos": screen_pos,
        }

    def _start_visual_grid_anchor(self, entry: dict | None, status_var: tk.StringVar | None = None):
        if not entry:
            messagebox.showinfo("Sin selección", "Selecciona una entidad o celda primero.", parent=self)
            return
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            messagebox.showinfo("Bot detenido", "Inicia el bot antes de anclar la grilla.", parent=self)
            return
        map_id = self._current_runtime_map_id()
        if map_id is None:
            messagebox.showwarning("Sin map_id", "No hay map_id actual.", parent=self)
            return
        try:
            cell_id = int(entry.get("cell_id"))
        except (TypeError, ValueError):
            messagebox.showwarning("Cell inválida", "La selección no tiene cell_id válido.", parent=self)
            return
        actor_id = str(entry.get("actor_id") or "?").strip() or "?"
        if status_var is not None:
            status_var.set(f"Acomoda el mouse en actor={actor_id} cell={cell_id}. Capturo en 3s...")

        def _do():
            time.sleep(3)
            mouse_x, mouse_y = pyautogui.position()
            self.after(
                0,
                lambda: self._finish_visual_grid_anchor(
                    int(map_id),
                    int(cell_id),
                    (int(mouse_x), int(mouse_y)),
                    dict(entry),
                    status_var,
                ),
            )

        threading.Thread(target=_do, daemon=True).start()

    def _finish_visual_grid_anchor(
        self,
        map_id: int,
        cell_id: int,
        mouse_pos: tuple[int, int],
        entry: dict,
        status_var: tk.StringVar | None = None,
    ):
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            if status_var is not None:
                status_var.set("Bot detenido")
            return
        monitor = dict(bot.screen.monitor)
        settings = self._get_visual_grid_settings(int(map_id), int(monitor["width"]), int(monitor["height"]))
        cell_width = float(settings.get("cell_width", 0.0) or 0.0)
        cell_height = float(settings.get("cell_height", 0.0) or 0.0)
        if cell_width <= 0 or cell_height <= 0:
            if status_var is not None:
                status_var.set("Tamaño de celda inválido")
            return

        map_cells = bot.get_current_map_cells_snapshot()
        map_cell = None
        for item in map_cells:
            try:
                if int(item.get("cell_id")) == int(cell_id):
                    map_cell = item
                    break
            except (TypeError, ValueError, AttributeError):
                continue
        if map_cell is None:
            messagebox.showwarning("Sin celda lógica", f"No encontré la celda {cell_id} en el mapa lógico actual.", parent=self)
            if status_var is not None:
                status_var.set("No encontré la celda en mapa lógico")
            return

        grid_x = float(map_cell.get("x", 0.0))
        grid_y = float(map_cell.get("y", 0.0))
        mid_w = cell_width / 2.0
        mid_h = cell_height / 2.0
        rel_x = float(mouse_pos[0] - monitor["left"])
        rel_y = float(mouse_pos[1] - monitor["top"])
        offset_x = rel_x - ((grid_x - grid_y) * mid_w + mid_w)
        offset_y = rel_y - ((grid_x + grid_y) * mid_h + mid_h)

        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        by_map = cal_cfg.setdefault("visual_grid_by_map_id", {})
        by_map[str(map_id)] = {
            "canvas_width": int(monitor["width"]),
            "canvas_height": int(monitor["height"]),
            "cell_width": round(cell_width, 2),
            "cell_height": round(cell_height, 2),
            "offset_x": round(offset_x, 2),
            "offset_y": round(offset_y, 2),
        }
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._sniffer_grid_offset_x_var.set(round(offset_x, 2))
        self._sniffer_grid_offset_y_var.set(round(offset_y, 2))
        self._sniffer_grid_sync_entry_vars()
        self._redraw_sniffer_grid_overlay()
        actor_id = str(entry.get("actor_id") or "?").strip() or "?"
        if status_var is not None:
            status_var.set(f"Offset anclado: map_id={map_id} actor={actor_id} cell={cell_id}")
            self.after(3500, lambda: status_var.set(""))
        self.log_queue.put((
            "log",
            f"[GRID] offset anclado map_id={map_id} actor={actor_id} cell={cell_id} mouse={mouse_pos} "
            f"offset=({round(offset_x,2)},{round(offset_y,2)})"
        ))

    def _delete_selected_sniffer_sample(self, status_var: tk.StringVar | None = None):
        selection = self._sniffer_sample_selection
        map_id = self._current_runtime_map_id()
        if selection is None or map_id is None:
            if status_var is not None:
                status_var.set("Selecciona una muestra del mapa actual")
            return
        try:
            cell_id = int(selection.get("cell_id"))
        except (TypeError, ValueError, AttributeError):
            if status_var is not None:
                status_var.set("La muestra seleccionada no es válida")
            return
        if not self._delete_world_map_sample(int(map_id), cell_id):
            if status_var is not None:
                status_var.set("No pude borrar la muestra seleccionada")
            return
        if status_var is not None:
            status_var.set(f"Muestra borrada: map_id={map_id} cell={cell_id}")
            self.after(3000, lambda: status_var.set(""))
        self.log_queue.put(("log", f"[CAL] muestra borrada map_id={map_id} cell={cell_id}"))
        self._refresh_sniffer_tab()

    def _clear_current_map_samples(self, status_var: tk.StringVar | None = None):
        map_id = self._current_runtime_map_id()
        if map_id is None:
            if status_var is not None:
                status_var.set("No hay map_id actual")
            return
        removed = self._clear_world_map_samples(int(map_id))
        self._sniffer_sample_selection = None
        if status_var is not None:
            status_var.set(f"Calibración limpiada: map_id={map_id} | muestras borradas={removed}")
            self.after(3500, lambda: status_var.set(""))
        self.log_queue.put(("log", f"[CAL] calibración limpiada map_id={map_id} removed={removed}"))
        self._refresh_sniffer_tab()

    def _get_visual_grid_settings(self, map_id: int | None, width: int, height: int) -> dict:
        cell_cal = self.config_data.get("bot", {}).get("cell_calibration", {}) or {}
        by_map = cell_cal.get("visual_grid_by_map_id", {}) or {}
        global_base = cell_cal.get("visual_grid_global", {}) or {}
        raw = by_map.get(str(map_id), {}) if map_id is not None else {}
        map_w = 15
        map_h = 17
        default_cell_width = min(width / (map_w + 1), (height / (map_h + 1)) * 2)
        default_cell_height = max(8.0, round(default_cell_width / 2.0, 2))
        default_offset_x = (width - ((map_w + 0.5) * default_cell_width)) / 2
        default_offset_y = (height - ((map_h + 0.5) * default_cell_height)) / 2
        inherited_cell_width = float(global_base.get("cell_width", default_cell_width))
        inherited_cell_height = float(global_base.get("cell_height", default_cell_height))
        inherited_offset_x = float(global_base.get("offset_x", default_offset_x))
        inherited_offset_y = float(global_base.get("offset_y", default_offset_y))
        return {
            "cell_width": float(raw.get("cell_width", inherited_cell_width)),
            "cell_height": float(raw.get("cell_height", inherited_cell_height)),
            "offset_x": float(raw.get("offset_x", inherited_offset_x)),
            "offset_y": float(raw.get("offset_y", inherited_offset_y)),
        }

    def _save_visual_grid_settings(self, map_id: int, width: int, height: int):
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        by_map = cal_cfg.setdefault("visual_grid_by_map_id", {})
        by_map[str(map_id)] = {
            "canvas_width": int(width),
            "canvas_height": int(height),
            "cell_width": round(float(self._sniffer_grid_cell_width_var.get()), 2),
            "cell_height": round(float(self._sniffer_grid_cell_height_var.get()), 2),
            "offset_x": round(float(self._sniffer_grid_offset_x_var.get()), 2),
            "offset_y": round(float(self._sniffer_grid_offset_y_var.get()), 2),
        }
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()

    def _ensure_visual_grid_saved_for_map(self, map_id: int, width: int, height: int) -> bool:
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        global_base = cal_cfg.get("visual_grid_global", {}) or {}
        if not isinstance(global_base, dict):
            return False
        required = ("cell_width", "cell_height", "offset_x", "offset_y")
        if not all(global_base.get(key) not in (None, "") for key in required):
            return False
        by_map = cal_cfg.setdefault("visual_grid_by_map_id", {})
        key = str(map_id)
        raw = by_map.get(key)
        if not isinstance(raw, dict):
            raw = {}
        changed = False
        for field in required:
            if raw.get(field) in (None, ""):
                raw[field] = round(float(global_base[field]), 2)
                changed = True
        if raw.get("canvas_width") in (None, "", 0):
            raw["canvas_width"] = int(width)
            changed = True
        if raw.get("canvas_height") in (None, "", 0):
            raw["canvas_height"] = int(height)
            changed = True
        if changed:
            by_map[key] = raw
            save_config(self.config_data)
            self.config_data = load_config()
            self._sync_runtime_bot_config()
        return changed

    def _save_current_visual_grid_as_global(self, status_var: tk.StringVar | None = None):
        bot_cfg = self.config_data.setdefault("bot", {})
        cal_cfg = bot_cfg.setdefault("cell_calibration", {})
        global_grid = {
            "cell_width": round(float(self._sniffer_grid_cell_width_var.get()), 2),
            "cell_height": round(float(self._sniffer_grid_cell_height_var.get()), 2),
            "offset_x": round(float(self._sniffer_grid_offset_x_var.get()), 2),
            "offset_y": round(float(self._sniffer_grid_offset_y_var.get()), 2),
        }
        cal_cfg["visual_grid_global"] = dict(global_grid)
        by_map = cal_cfg.setdefault("visual_grid_by_map_id", {})
        for map_id, payload in list(by_map.items()):
            if not isinstance(payload, dict):
                payload = {}
            payload.update(global_grid)
            by_map[str(map_id)] = payload
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        if status_var is not None:
            status_var.set("Grilla actual guardada como base global para todos los mapas")
            self.after(3500, lambda: status_var.set(""))
        self.log_queue.put(("log", "[GRID] base global guardada y propagada a todos los mapas"))

    def _refresh_sniffer_grid_capture(self):
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None or self._sniffer_grid_canvas is None:
            return
        try:
            monitor = dict(bot.screen.monitor)
            with mss.mss() as sct:
                screenshot = sct.grab(monitor)
            image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        except Exception as exc:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set(f"Captura falló: {exc}")
            return
        canvas_w = max(640, int(self._sniffer_grid_canvas.winfo_width() or 960))
        canvas_h = max(360, int(self._sniffer_grid_canvas.winfo_height() or 540))
        scale = min(canvas_w / image.width, canvas_h / image.height)
        scale = min(scale, 1.0)
        resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
        self._sniffer_grid_image = resized
        self._sniffer_grid_photo = ImageTk.PhotoImage(resized)
        self._sniffer_grid_scale = scale
        map_id = bot._current_map_id
        if map_id != self._sniffer_grid_last_map_id:
            seeded = self._ensure_visual_grid_saved_for_map(int(map_id), int(resized.width), int(resized.height))
            settings = self._get_visual_grid_settings(map_id, resized.width, resized.height)
            self._sniffer_grid_cell_width_var.set(settings["cell_width"])
            self._sniffer_grid_cell_height_var.set(settings["cell_height"])
            self._sniffer_grid_offset_x_var.set(settings["offset_x"])
            self._sniffer_grid_offset_y_var.set(settings["offset_y"])
            self._sniffer_grid_last_map_id = map_id
            if seeded and self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set(f"Base global auto-aplicada y guardada para map_id={map_id}")
        self._sniffer_grid_sync_entry_vars()
        try:
            self._redraw_sniffer_grid_overlay()
        except Exception as exc:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set(f"Overlay falló: {exc}")

    def _redraw_sniffer_grid_overlay(self):
        if self._sniffer_grid_canvas is None:
            return
        self._sniffer_grid_canvas.delete("all")
        self._sniffer_grid_polygons = []
        if self._sniffer_grid_photo is None or self._sniffer_grid_image is None:
            return
        canvas = self._sniffer_grid_canvas
        background_photo = self._sniffer_grid_photo
        background_image = self._sniffer_grid_image
        canvas.create_image(0, 0, image=background_photo, anchor="nw", tags=("bg",))
        map_w = 15
        map_h = 17
        cell_width = float(self._sniffer_grid_cell_width_var.get() or 0.0)
        cell_height = float(self._sniffer_grid_cell_height_var.get() or 0.0)
        if cell_width <= 0 or cell_height <= 0:
            return
        mid_w = cell_width / 2.0
        mid_h = cell_height / 2.0
        offset_x = float(self._sniffer_grid_offset_x_var.get() or 0.0)
        offset_y = float(self._sniffer_grid_offset_y_var.get() or 0.0)
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        map_cells = bot.get_current_map_cells_snapshot() if bot and self._sniffer_grid_show_logic_var.get() else []
        map_cell_by_id = {int(cell.get("cell_id")): cell for cell in map_cells if cell.get("cell_id") is not None}
        selected_cell = None
        if self._sniffer_selection_entry:
            try:
                selected_cell = int(self._sniffer_selection_entry.get("cell_id"))
            except (TypeError, ValueError):
                selected_cell = None
        if map_cells:
            iterable_cells = sorted(map_cells, key=lambda item: int(item.get("cell_id") if item.get("cell_id") is not None else 0))
        else:
            iterable_cells = []
            cell_id = 0
            for y in range((2 * map_h)):
                if y % 2 == 0:
                    x_range = range(map_w)
                    shift_x = 0.0
                else:
                    x_range = range(map_w - 1)
                    shift_x = mid_w
                for x in x_range:
                    iterable_cells.append({
                        "cell_id": cell_id,
                        "_legacy_x": x,
                        "_legacy_y": y,
                        "_legacy_shift_x": shift_x,
                    })
                    cell_id += 1

        for cell_entry in iterable_cells:
            cell_id = int(cell_entry.get("cell_id", 0))
            if "_legacy_x" in cell_entry:
                x = float(cell_entry["_legacy_x"])
                y = float(cell_entry["_legacy_y"])
                shift_x = float(cell_entry.get("_legacy_shift_x", 0.0))
                left = (offset_x + (x * cell_width) + shift_x, offset_y + (y * mid_h) + mid_h)
                top = (offset_x + (x * cell_width) + mid_w + shift_x, offset_y + (y * mid_h))
                right = (offset_x + (x * cell_width) + cell_width + shift_x, offset_y + (y * mid_h) + mid_h)
                down = (offset_x + (x * cell_width) + mid_w + shift_x, offset_y + (y * mid_h) + cell_height)
            else:
                grid_x = float(cell_entry.get("x", 0.0))
                grid_y = float(cell_entry.get("y", 0.0))
                iso_x = (grid_x - grid_y) * mid_w
                iso_y = (grid_x + grid_y) * mid_h
                left = (offset_x + iso_x, offset_y + iso_y + mid_h)
                top = (offset_x + iso_x + mid_w, offset_y + iso_y)
                right = (offset_x + iso_x + cell_width, offset_y + iso_y + mid_h)
                down = (offset_x + iso_x + mid_w, offset_y + iso_y + cell_height)

            points = [left, top, right, down]
            outline = "#4f6b85"
            width = 1
            fill = ""
            cell_meta = map_cell_by_id.get(cell_id)
            if cell_meta:
                raw_type = int(cell_meta.get("raw_cell_type", cell_meta.get("cell_type", -1)) or -1)
                type_label = str(cell_meta.get("type_label", "") or "")
                is_interactive = bool(cell_meta.get("is_interactive_cell")) or int(cell_meta.get("interactive_object_id", -1) or -1) != -1
                if type_label == "teleport_cell" or bool(cell_meta.get("has_teleport_texture")):
                    fill = "#2a2448"
                    outline = "#6d5bd0"
                elif raw_type == 0 and is_interactive:
                    fill = "#4c3a17"
                    outline = "#c78a1b"
                elif raw_type == 0 and not is_interactive:
                    fill = "#3f2029"
                    outline = "#8c2f42"
                elif raw_type == 1 and is_interactive:
                    fill = "#4c3a17"
                    outline = "#c78a1b"
                elif raw_type == 1 and not is_interactive:
                    fill = "#3f2029"
                    outline = "#8c2f42"
                elif raw_type == 4 and is_interactive:
                    fill = "#4c3a17"
                    outline = "#c78a1b"
                elif type_label == "path":
                    fill = "#1f3c35"
                    outline = "#4f6b85"
                else:
                    fill = "#1c2438"
                    outline = "#4f6b85"
            if selected_cell is not None and cell_id == selected_cell:
                outline = GREEN
                width = 3
                fill = "#24453a"
            flat = [coord for point in points for coord in point]
            canvas.create_polygon(*flat, outline=outline, fill=fill, width=width)
            cx = (left[0] + right[0]) / 2.0
            cy = (top[1] + down[1]) / 2.0
            self._sniffer_grid_polygons.append({
                "cell_id": cell_id,
                "points": [(float(px), float(py)) for px, py in points],
                "center": (float(cx), float(cy)),
            })
            if selected_cell is not None and cell_id == selected_cell:
                canvas.create_text(cx, cy, text=str(cell_id), fill=TEXT, font=("Segoe UI", 9, "bold"))
        if self._sniffer_grid_status_var is not None:
            logic_count = len(map_cells)
            self._sniffer_grid_status_var.set(
                f"Grilla map_id={self._current_runtime_map_id()} | logic={logic_count} | cell=({cell_width:.1f},{cell_height:.1f}) "
                f"| offset=({offset_x:.1f},{offset_y:.1f})"
            )

    def _on_sniffer_grid_param_change(self, *_args):
        self._sniffer_grid_sync_entry_vars()
        try:
            self._redraw_sniffer_grid_overlay()
        except Exception as exc:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set(f"Ajuste falló: {exc}")

    def _sniffer_grid_sync_entry_vars(self):
        self._sniffer_grid_cell_width_entry_var.set(f"{float(self._sniffer_grid_cell_width_var.get() or 0.0):.2f}")
        self._sniffer_grid_cell_height_entry_var.set(f"{float(self._sniffer_grid_cell_height_var.get() or 0.0):.2f}")
        self._sniffer_grid_offset_x_entry_var.set(f"{float(self._sniffer_grid_offset_x_var.get() or 0.0):.2f}")
        self._sniffer_grid_offset_y_entry_var.set(f"{float(self._sniffer_grid_offset_y_var.get() or 0.0):.2f}")

    def _apply_sniffer_grid_entry_values(self):
        try:
            self._sniffer_grid_cell_width_var.set(float((self._sniffer_grid_cell_width_entry_var.get() or "0").replace(",", ".")))
            self._sniffer_grid_cell_height_var.set(float((self._sniffer_grid_cell_height_entry_var.get() or "0").replace(",", ".")))
            self._sniffer_grid_offset_x_var.set(float((self._sniffer_grid_offset_x_entry_var.get() or "0").replace(",", ".")))
            self._sniffer_grid_offset_y_var.set(float((self._sniffer_grid_offset_y_entry_var.get() or "0").replace(",", ".")))
        except ValueError:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set("Valores de grilla invalidos")
            return
        self._sniffer_grid_sync_entry_vars()
        self._redraw_sniffer_grid_overlay()

    def _bind_sniffer_grid_entry(self, entry_widget):
        entry_widget.bind("<Return>", lambda _event: self._apply_sniffer_grid_entry_values())
        entry_widget.bind("<FocusOut>", lambda _event: self._apply_sniffer_grid_entry_values())

    def _point_in_polygon(self, x: float, y: float, points: list[tuple[float, float]]) -> bool:
        inside = False
        j = len(points) - 1
        for i in range(len(points)):
            xi, yi = points[i]
            xj, yj = points[j]
            intersects = ((yi > y) != (yj > y)) and (
                x < ((xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi)
            )
            if intersects:
                inside = not inside
            j = i
        return inside

    def _on_sniffer_grid_canvas_click(self, event=None):
        if event is None or not self._sniffer_grid_polygons:
            return
        click_x = float(event.x)
        click_y = float(event.y)
        chosen = None
        for polygon in reversed(self._sniffer_grid_polygons):
            if self._point_in_polygon(click_x, click_y, polygon["points"]):
                chosen = polygon
                break
        if chosen is None:
            chosen = min(
                self._sniffer_grid_polygons,
                key=lambda polygon: ((polygon["center"][0] - click_x) ** 2 + (polygon["center"][1] - click_y) ** 2),
            )
        cell_id = int(chosen["cell_id"])
        selected_entry = None
        if self._sniffer_tree_entries:
            for item_id, entry in self._sniffer_tree_entries.items():
                try:
                    if int(entry.get("cell_id")) == cell_id:
                        selected_entry = dict(entry)
                        if self._sniffer_tree is not None:
                            self._sniffer_tree.selection_set(item_id)
                            self._sniffer_tree.see(item_id)
                        break
                except (TypeError, ValueError):
                    continue
        if selected_entry is None:
            selected_entry = {"cell_id": cell_id, "actor_id": "", "entity_kind": "cell"}
        self._sniffer_selection_entry = selected_entry
        if self._sniffer_test_status_var is not None:
            self._sniffer_test_status_var.set(
                f"Seleccionada celda overlay | map_id={self._current_runtime_map_id()} | cell={cell_id}"
            )
        self._redraw_sniffer_grid_overlay()

    def _save_current_visual_grid(self):
        map_id = self._current_runtime_map_id()
        if map_id is None or self._sniffer_grid_image is None:
            if self._sniffer_grid_status_var is not None:
                self._sniffer_grid_status_var.set("No hay captura/map_id para guardar")
            return
        self._save_visual_grid_settings(int(map_id), self._sniffer_grid_image.width, self._sniffer_grid_image.height)
        if self._sniffer_grid_status_var is not None:
            self._sniffer_grid_status_var.set(f"Grilla visual guardada para map_id={map_id}")

    def _save_farming_mode(self):
        self.config_data["farming"]["mode"] = "resource"
        save_config(self.config_data)

    def _save_sniffer_setting(self):
        self.config_data["bot"]["sniffer_enabled"] = self._sniffer_var.get()
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()

    def _save_bank_unload_setting(self):
        self.config_data["bot"]["enable_bank_unload"] = self._enable_bank_unload_var.get()
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        state = "activada" if self._enable_bank_unload_var.get() else "desactivada"
        self.log_queue.put(("log", f"[BOT] Descarga en banco automática {state}"))

    def _save_combat_manual_mode_setting(self):
        self.config_data.setdefault("bot", {})["combat_manual_mode"] = self._combat_manual_mode_var.get()
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        state = "activado" if self._combat_manual_mode_var.get() else "desactivado"
        self.log_queue.put(("log", f"[BOT] Modo manual en combate {state}"))

    def _toggle_sniffer_debug(self):
        """Abre ventana con log raw de paquetes Dofus para descubrir actor_id."""
        win = tk.Toplevel(self)
        win.title("Sniffer — Paquetes raw")
        win.configure(bg=BG)
        win.geometry("600x400")
        tk.Label(win, text="Paquetes raw Dofus (S→C y C→S). Busca GTS para tu actor ID.",
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=4)
        txt = tk.Text(win, bg=PANEL, fg=GREEN, font=("Consolas", 8),
                      relief="flat", wrap="word")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=6, pady=6)

        def poll():
            if self.bot_thread and self.bot_thread.bot:
                bot = self.bot_thread.bot
                # Activar debug_mode en el sniffer si está corriendo
                if bot._sniffer:
                    bot._sniffer.debug_mode = True
            if win.winfo_exists():
                win.after(200, poll)

        def on_log(msg):
            if "[SNIFFER]" in msg or "S→C" in msg or "C→S" in msg:
                txt.config(state="normal")
                txt.insert("end", msg + "\n")
                txt.see("end")
                txt.config(state="disabled")

        # Hookear el log_queue para filtrar mensajes del sniffer
        self._raw_log_cb = on_log
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "_raw_log_cb", None), win.destroy()))
        poll()

    def _build_sniffer_tab(self, parent):
        self._sniffer_tab = parent
        top = tk.Frame(parent, bg=BG, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(
            top,
            text="Inspector del sniffer. Selecciona una entidad, mueve el mouse al punto exacto y guarda puntos manuales de calibración.",
            bg=BG,
            fg=SUBTEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        self._sniffer_summary_var = tk.StringVar(value="Bot detenido")
        tk.Label(top, textvariable=self._sniffer_summary_var, bg=BG, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 0))
        action_row = tk.Frame(top, bg=BG)
        action_row.pack(fill="x", pady=(6, 0))
        action_row_1 = tk.Frame(action_row, bg=BG)
        action_row_1.pack(fill="x")
        action_row_2 = tk.Frame(action_row, bg=BG)
        action_row_2.pack(fill="x", pady=(6, 0))
        self._sniffer_copy_status_var = tk.StringVar(value="")
        self._sniffer_test_status_var = tk.StringVar(value="")
        tk.Button(action_row_1, text="Copiar todo", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._copy_sniffer_snapshot(self._sniffer_payload_text, self._sniffer_copy_status_var)
                  ).pack(side="left")
        tk.Button(action_row_1, text="Guardar player ID", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._save_selected_follow_player_id).pack(side="left", padx=(6, 0))
        tk.Button(action_row_1, text="Quitar player ID", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._remove_selected_follow_player_id).pack(side="left", padx=(6, 0))
        tk.Button(action_row_1, text="Guardar punto manual (3s)", bg=BLUE, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._start_sniffer_map_calibration(self._sniffer_selection_entry, self._sniffer_test_status_var)
                  ).pack(side="left", padx=(6, 0))
        tk.Button(action_row_2, text="Mover mouse a selección", bg=YELLOW, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._move_mouse_to_sniffer_selection(self._sniffer_selection_entry, self._sniffer_test_status_var)
                  ).pack(side="left", padx=(6, 0))
        tk.Button(action_row_2, text="Anclar offset (3s)", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._start_visual_grid_anchor(self._sniffer_selection_entry, self._sniffer_test_status_var)
                  ).pack(side="left", padx=(6, 0))
        tk.Button(action_row_2, text="Borrar muestra", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._delete_selected_sniffer_sample(self._sniffer_test_status_var)
                  ).pack(side="left", padx=(6, 0))
        tk.Button(action_row_2, text="Limpiar map_id", bg=RED, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._clear_current_map_samples(self._sniffer_test_status_var)
                  ).pack(side="left", padx=(6, 0))
        tk.Label(action_row_2, textvariable=self._sniffer_copy_status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        tk.Label(action_row_2, textvariable=self._sniffer_test_status_var, bg=BG, fg=YELLOW,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(12, 0))

        body = tk.Frame(parent, bg=BG, padx=10, pady=6)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        center = tk.Frame(body, bg=BG)
        center.pack(side="left", fill="both", expand=True, padx=(10, 0))
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self._sniffer_body_left = left
        self._sniffer_body_center = center
        self._sniffer_body_right = right

        tk.Label(left, text="Entidades de mapa (GM)", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tree_frame = tk.Frame(left, bg=PANEL)
        tree_frame.pack(fill="x", expand=False, pady=(4, 0))
        columns = ("op", "actor", "cell", "grid", "kind", "mob", "sig", "lvl", "dir", "extra")
        self._sniffer_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)
        headings = {
            "op": "Op",
            "actor": "Actor ID",
            "cell": "Cell",
            "grid": "Grid XY",
            "kind": "Kind",
            "mob": "Mob",
            "sig": "Template IDs",
            "lvl": "Levels",
            "dir": "Dir",
            "extra": "Extra",
        }
        widths = {"op": 45, "actor": 90, "cell": 55, "grid": 70, "kind": 90, "mob": 110, "sig": 130, "lvl": 90, "dir": 45, "extra": 200}
        for col in columns:
            self._sniffer_tree.heading(col, text=headings[col])
            self._sniffer_tree.column(col, width=widths[col], anchor="w")
        tree_scroll = ttk.Scrollbar(tree_frame, command=self._sniffer_tree.yview)
        self._sniffer_tree.configure(yscrollcommand=tree_scroll.set)
        self._sniffer_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        def on_tree_select(event=None):
            if self._sniffer_tree is None:
                return
            selection = self._sniffer_tree.selection()
            if not selection:
                self._sniffer_selection_entry = None
                return
            entry = self._sniffer_tree_entries.get(selection[0])
            self._sniffer_selection_entry = entry
            if not entry:
                return
            template_ids = entry.get("template_ids", [])
            if template_ids and hasattr(self, "_template_db_id_var"):
                self._template_db_id_var.set(str(template_ids[0]))
            resolved = entry.get("resolved_mobs", [])
            if resolved and hasattr(self, "_template_db_name_var"):
                self._template_db_name_var.set(str(resolved[0]))
            elif entry.get("entity_kind") in {"mob", "mob_group"} and hasattr(self, "_template_db_name_var"):
                self._template_db_name_var.set("")
            actor_id = str(entry.get("actor_id", "")).strip()
            if actor_id and actor_id.lstrip("+-").isdigit() and int(actor_id) > 0 and hasattr(self, "_player_db_id_var"):
                self._player_db_id_var.set(actor_id)
                follow_db = self._normalized_follow_player_db()
                if hasattr(self, "_player_db_name_var"):
                    self._player_db_name_var.set(str(follow_db.get(actor_id, {}).get("name", "")))
            map_id = self._current_runtime_map_id()
            if self._sniffer_test_status_var is not None:
                self._sniffer_test_status_var.set(
                    f"Seleccionado map_id={map_id if map_id is not None else '?'} "
                    f"| actor={entry.get('actor_id', '?')} | cell={entry.get('cell_id', '?')}"
                )

        self._sniffer_tree.bind("<<TreeviewSelect>>", on_tree_select)

        tk.Label(center, text="Muestras de calibraciÃ³n del map_id", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        samples_frame = tk.Frame(center, bg=PANEL)
        samples_frame.pack(fill="x", expand=False, pady=(4, 0))
        sample_columns = ("cell", "grid", "screen", "error", "state", "actor", "kind", "saved")
        self._sniffer_samples_tree = ttk.Treeview(samples_frame, columns=sample_columns, show="headings", height=8)
        sample_headings = {
            "cell": "Cell",
            "grid": "Grid XY",
            "screen": "Screen XY",
            "error": "Error px",
            "state": "Estado",
            "actor": "Actor",
            "kind": "Kind",
            "saved": "Saved",
        }
        sample_widths = {"cell": 55, "grid": 80, "screen": 105, "error": 70, "state": 75, "actor": 70, "kind": 90, "saved": 110}
        for col in sample_columns:
            self._sniffer_samples_tree.heading(col, text=sample_headings[col])
            self._sniffer_samples_tree.column(col, width=sample_widths[col], anchor="w")
        self._sniffer_samples_tree.tag_configure("ok", foreground=GREEN)
        self._sniffer_samples_tree.tag_configure("warn", foreground=YELLOW)
        self._sniffer_samples_tree.tag_configure("outlier", foreground=RED)
        sample_scroll = ttk.Scrollbar(samples_frame, command=self._sniffer_samples_tree.yview)
        self._sniffer_samples_tree.configure(yscrollcommand=sample_scroll.set)
        self._sniffer_samples_tree.pack(side="left", fill="both", expand=True)
        sample_scroll.pack(side="right", fill="y")

        def on_sample_select(event=None):
            if self._sniffer_samples_tree is None:
                return
            selection = self._sniffer_samples_tree.selection()
            if not selection:
                self._sniffer_sample_selection = None
                return
            self._sniffer_sample_selection = self._sniffer_sample_entries.get(selection[0])

        self._sniffer_samples_tree.bind("<<TreeviewSelect>>", on_sample_select)

        tk.Label(right, text="Eventos recientes del sniffer", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self._sniffer_events_text = tk.Text(right, bg=PANEL, fg=GREEN, font=("Consolas", 8),
                                            relief="flat", wrap="word", state="disabled")
        events_scroll = ttk.Scrollbar(right, command=self._sniffer_events_text.yview)
        self._sniffer_events_text.configure(yscrollcommand=events_scroll.set)
        self._sniffer_events_text.pack(side="left", fill="both", expand=True, pady=(4, 0))
        events_scroll.pack(side="right", fill="y", pady=(4, 0))

        calibrator = tk.Frame(parent, bg=BG, padx=10)
        calibrator.pack(fill="both", expand=True, pady=(6, 10))
        calib_top = tk.Frame(calibrator, bg=BG)
        calib_top.pack(fill="x", pady=(8, 4))
        tk.Label(calib_top, text="Calibrador visual de grilla", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self._sniffer_grid_status_var = tk.StringVar(value="Sin captura aún")
        tk.Label(calib_top, textvariable=self._sniffer_grid_status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0))
        tk.Button(calib_top, text="Refrescar captura", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._refresh_sniffer_grid_capture).pack(side="right")
        tk.Checkbutton(calib_top, text="Ver mapa logico", variable=self._sniffer_grid_show_logic_var,
                       bg=BG, fg=SUBTEXT, activebackground=BG, activeforeground=TEXT,
                       selectcolor=PANEL, command=self._on_sniffer_grid_param_change).pack(side="right", padx=(0, 8))
        tk.Button(calib_top, text="Guardar grilla", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._save_current_visual_grid).pack(side="right", padx=(0, 6))
        tk.Button(calib_top, text="Usar como base global", bg=BLUE, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=lambda: self._save_current_visual_grid_as_global(self._sniffer_test_status_var)
                  ).pack(side="right", padx=(0, 6))

        sliders = tk.Frame(calibrator, bg=BG)
        sliders.pack(fill="x", pady=(0, 6))
        tk.Label(sliders, text="Cell Width", bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
        cell_width_entry = tk.Entry(sliders, textvariable=self._sniffer_grid_cell_width_entry_var, width=7,
                                    bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat")
        cell_width_entry.pack(side="left", padx=(4, 6))
        self._bind_sniffer_grid_entry(cell_width_entry)
        tk.Scale(sliders, from_=20, to=140, resolution=0.1, orient="horizontal",
                 variable=self._sniffer_grid_cell_width_var, bg=BG, fg=TEXT,
                 highlightthickness=0, command=self._on_sniffer_grid_param_change,
                 length=180).pack(side="left", padx=(4, 12))
        tk.Label(sliders, text="Cell Height", bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
        cell_height_entry = tk.Entry(sliders, textvariable=self._sniffer_grid_cell_height_entry_var, width=7,
                                     bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat")
        cell_height_entry.pack(side="left", padx=(4, 6))
        self._bind_sniffer_grid_entry(cell_height_entry)
        tk.Scale(sliders, from_=10, to=90, resolution=0.1, orient="horizontal",
                 variable=self._sniffer_grid_cell_height_var, bg=BG, fg=TEXT,
                 highlightthickness=0, command=self._on_sniffer_grid_param_change,
                 length=180).pack(side="left", padx=(4, 12))
        tk.Label(sliders, text="Offset X", bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
        offset_x_entry = tk.Entry(sliders, textvariable=self._sniffer_grid_offset_x_entry_var, width=7,
                                  bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat")
        offset_x_entry.pack(side="left", padx=(4, 6))
        self._bind_sniffer_grid_entry(offset_x_entry)
        tk.Scale(sliders, from_=-300, to=300, resolution=0.5, orient="horizontal",
                 variable=self._sniffer_grid_offset_x_var, bg=BG, fg=TEXT,
                 highlightthickness=0, command=self._on_sniffer_grid_param_change,
                 length=180).pack(side="left", padx=(4, 12))
        tk.Label(sliders, text="Offset Y", bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
        offset_y_entry = tk.Entry(sliders, textvariable=self._sniffer_grid_offset_y_entry_var, width=7,
                                  bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat")
        offset_y_entry.pack(side="left", padx=(4, 6))
        self._bind_sniffer_grid_entry(offset_y_entry)
        tk.Scale(sliders, from_=-250, to=250, resolution=0.5, orient="horizontal",
                 variable=self._sniffer_grid_offset_y_var, bg=BG, fg=TEXT,
                 highlightthickness=0, command=self._on_sniffer_grid_param_change,
                 length=180).pack(side="left", padx=(4, 0))
        tk.Button(sliders, text="Aplicar", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._apply_sniffer_grid_entry_values).pack(side="left", padx=(8, 0))

        self._sniffer_grid_canvas = tk.Canvas(calibrator, bg="#0c1020", height=420, highlightthickness=1,
                                              highlightbackground=ACCENT)
        self._sniffer_grid_canvas.pack(fill="both", expand=True)
        self._sniffer_grid_canvas.bind("<Button-1>", self._on_sniffer_grid_canvas_click)

    def _refresh_sniffer_tab(self):
        if self._sniffer_summary_var is None or self._sniffer_tree is None or self._sniffer_events_text is None:
            return
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is None:
            self._sniffer_summary_var.set("Bot detenido. Inicia el bot con sniffer para inspeccionar eventos.")
            self._sniffer_tree.delete(*self._sniffer_tree.get_children())
            self._sniffer_tree_entries.clear()
            if self._sniffer_samples_tree is not None:
                self._sniffer_samples_tree.delete(*self._sniffer_samples_tree.get_children())
                self._sniffer_sample_entries.clear()
            self._sniffer_events_text.config(state="normal")
            self._sniffer_events_text.delete("1.0", "end")
            self._sniffer_events_text.insert("1.0", "Sin eventos aún.")
            self._sniffer_events_text.config(state="disabled")
            self._sniffer_payload_text = ""
            return

        map_entities = bot.get_map_entities_snapshot()
        mob_count = sum(1 for entry in map_entities if entry.get("entity_kind") == "mob")
        map_id = bot._current_map_id
        samples_by_map = self.config_data.get("bot", {}).get("cell_calibration", {}).get("world_map_samples_by_map_id", {}) or {}
        current_samples = samples_by_map.get(str(map_id), []) if map_id is not None else []
        affine = bot._fit_world_map_affine(map_id) if map_id is not None else None
        self._sniffer_summary_var.set(
            f"sniffer={'activo' if bot.sniffer_active else 'inactivo'} | "
            f"map_id={map_id} | actor={bot._sniffer_my_actor or '?'} | "
            f"entidades={len(map_entities)} | mobs~={mob_count} | "
            f"muestras={len(current_samples)} | affine={'lista' if affine else 'pendiente'}"
        )

        self._sniffer_tree.delete(*self._sniffer_tree.get_children())
        self._sniffer_tree_entries.clear()
        for entry in map_entities:
            extras_parts = [part for part in entry.get("extra_fields", [])[:4] if part]
            fight_owner = str(entry.get("fight_owner_actor_id", "") or "").strip()
            fight_owner_name = str(entry.get("fight_owner_name", "") or "").strip()
            if fight_owner or fight_owner_name:
                extras_parts.insert(0, f"starter={fight_owner or '?'}:{fight_owner_name or '?'}")
            extras = ", ".join(extras_parts)
            resolved_mobs = ", ".join(entry.get("resolved_mobs", []))
            template_ids = ",".join(str(value) for value in entry.get("template_ids", []))
            levels = ",".join(str(value) for value in entry.get("levels", []))
            grid_xy = entry.get("grid_xy")
            item_id = self._sniffer_tree.insert(
                "",
                "end",
                values=(
                    entry.get("operation", ""),
                    entry.get("actor_id", ""),
                    entry.get("cell_id", ""),
                    f"{grid_xy[0]},{grid_xy[1]}" if isinstance(grid_xy, (list, tuple)) and len(grid_xy) == 2 else "",
                    entry.get("entity_kind", ""),
                    resolved_mobs,
                    template_ids or entry.get("mob_signature", ""),
                    levels,
                    entry.get("direction", ""),
                    extras,
                ),
            )
            self._sniffer_tree_entries[item_id] = dict(entry)

        if self._sniffer_samples_tree is not None:
            self._sniffer_samples_tree.delete(*self._sniffer_samples_tree.get_children())
            self._sniffer_sample_entries.clear()
            for sample in current_samples:
                try:
                    cell_id = int(sample.get("cell_id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                grid_x = sample.get("grid_x")
                grid_y = sample.get("grid_y")
                saved_at = sample.get("saved_at")
                error_info = bot.world_map_sample_error(map_id, sample) if map_id is not None else None
                error_px = ""
                state = "PEND"
                tag = ""
                if error_info is not None:
                    distance = float(error_info.get("distance", 0.0))
                    error_px = f"{distance:.1f}"
                    if distance <= 8.0:
                        state = "OK"
                        tag = "ok"
                    elif distance <= 20.0:
                        state = "WARN"
                        tag = "warn"
                    else:
                        state = "OUTLIER"
                        tag = "outlier"
                item_id = self._sniffer_samples_tree.insert(
                    "",
                    "end",
                    values=(
                        cell_id,
                        f"{grid_x},{grid_y}",
                        f"{sample.get('screen_x')},{sample.get('screen_y')}",
                        error_px,
                        state,
                        sample.get("actor_id", ""),
                        sample.get("entity_kind", ""),
                        f"{saved_at:.3f}" if isinstance(saved_at, (int, float)) else saved_at or "",
                    ),
                    tags=(tag,) if tag else (),
                )
                self._sniffer_sample_entries[item_id] = dict(sample)

        previous_entry = self._sniffer_selection_entry
        if previous_entry:
            for item_id, entry in self._sniffer_tree_entries.items():
                if (
                    entry.get("actor_id") == previous_entry.get("actor_id")
                    and entry.get("cell_id") == previous_entry.get("cell_id")
                ):
                    self._sniffer_tree.selection_set(item_id)
                    self._sniffer_selection_entry = entry
                    break
            else:
                self._sniffer_selection_entry = None

        lines = []
        for item in bot.get_recent_sniffer_events()[-60:]:
            parts = [item.get("event", "?")]
            for key in ("map_id", "actor_id", "cell_id", "entity_kind", "operation", "source", "count"):
                if key in item:
                    parts.append(f"{key}={item[key]}")
            raw = item.get("raw")
            if raw:
                parts.append(f"raw={raw}")
            lines.append(" | ".join(parts))
        self._sniffer_events_text.config(state="normal")
        self._sniffer_events_text.delete("1.0", "end")
        self._sniffer_events_text.insert("1.0", "\n".join(lines) if lines else "Sin eventos aún.")
        self._sniffer_events_text.config(state="disabled")
        if self._sniffer_grid_canvas is not None:
            try:
                if self._sniffer_grid_image is None or map_id != self._sniffer_grid_last_map_id:
                    self.after_idle(self._refresh_sniffer_grid_capture)
                else:
                    self._redraw_sniffer_grid_overlay()
            except Exception as exc:
                if self._sniffer_grid_status_var is not None:
                    self._sniffer_grid_status_var.set(f"Grid refresh falló: {exc}")

        entities_lines = []
        for entry in map_entities:
            entities_lines.append(
                " | ".join([
                    f"operation={entry.get('operation', '')}",
                    f"actor_id={entry.get('actor_id', '')}",
                    f"cell_id={entry.get('cell_id', '')}",
                    f"grid_xy={entry.get('grid_xy', '')}",
                    f"entity_kind={entry.get('entity_kind', '')}",
                    f"resolved_mobs={','.join(entry.get('resolved_mobs', []))}",
                    f"sprite_type={entry.get('sprite_type', '')}",
                    f"leader_template_id={entry.get('leader_template_id', '')}",
                    f"total_monsters={entry.get('total_monsters', '')}",
                    f"total_level={entry.get('total_level', '')}",
                    f"template_ids={','.join(str(v) for v in entry.get('template_ids', []))}",
                    f"levels={','.join(str(v) for v in entry.get('levels', []))}",
                    f"direction={entry.get('direction', '')}",
                    f"extras={','.join(entry.get('extra_fields', []))}",
                    f"raw={entry.get('raw', '')}",
                ])
            )
        self._sniffer_payload_text = "\n".join([
            self._sniffer_summary_var.get(),
            "",
            "[Entidades GM]",
            "\n".join(entities_lines) if entities_lines else "Sin entidades.",
            "",
            "[Eventos recientes]",
            "\n".join(lines) if lines else "Sin eventos aún.",
        ])

    def _open_sniffer_tester(self):
        if self._notebook is not None and self._sniffer_tab is not None:
            self._notebook.select(self._sniffer_tab)
        self._refresh_sniffer_tab()

    def _copy_sniffer_snapshot(self, text: str, status_var: tk.StringVar | None = None):
        if not text.strip():
            if status_var is not None:
                status_var.set("Sin datos para copiar")
                self.after(1800, lambda: status_var.set(""))
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        if status_var is not None:
            status_var.set("Copiado al portapapeles")
            self.after(1800, lambda: status_var.set(""))

    def _build_controls(self, parent):
        runtime_header, runtime_body = self._collapsible_section(parent, "Runtime", start_collapsed=False)
        sniffer_row = tk.Frame(runtime_body, bg=BG)
        sniffer_row.pack(fill="x", pady=(2, 6))
        self._sniffer_var = tk.BooleanVar(value=bool(self.config_data["bot"].get("sniffer_enabled", False)))
        tk.Checkbutton(sniffer_row, text="Sniffer activo", variable=self._sniffer_var, bg=BG,
                       activebackground=BG, fg=GREEN, selectcolor=BG,
                       font=("Segoe UI", 9), command=self._save_sniffer_setting).pack(side="left")
        self._lbl_sniffer_status = tk.Label(sniffer_row, text="inactivo", bg=BG, fg=SUBTEXT,
                                            font=("Segoe UI", 8, "bold"))
        self._lbl_sniffer_status.pack(side="left", padx=(4, 0))
        tk.Button(sniffer_row, text="Simular Descarga Banco", bg=BLUE, fg=BG,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=6, pady=1,
                  cursor="hand2", command=self._simulate_unload).pack(side="right", padx=(0, 10))

        unload_row = tk.Frame(runtime_body, bg=BG)
        unload_row.pack(fill="x", pady=(0, 6))
        self._enable_bank_unload_var = tk.BooleanVar(value=bool(self.config_data["bot"].get("enable_bank_unload", False)))
        tk.Checkbutton(unload_row, text="Habilitar descarga en banco", variable=self._enable_bank_unload_var, bg=BG,
                       activebackground=BG, fg=GREEN, selectcolor=BG,
                       font=("Segoe UI", 9), command=self._save_bank_unload_setting).pack(side="left")

        manual_row = tk.Frame(runtime_body, bg=BG)
        manual_row.pack(fill="x", pady=(0, 6))
        self._combat_manual_mode_var = tk.BooleanVar(value=bool(self.config_data["bot"].get("combat_manual_mode", False)))
        tk.Checkbutton(manual_row, text="Modo manual en combate (pausa auto-combate)", variable=self._combat_manual_mode_var, bg=BG,
                       activebackground=BG, fg=YELLOW, selectcolor=BG,
                       font=("Segoe UI", 9, "bold"), command=self._save_combat_manual_mode_setting).pack(side="left")

        # Selector de perfil de combate
        combat_header, combat_body = self._collapsible_section(parent, "Combate", start_collapsed=False)
        profile_row = tk.Frame(combat_body, bg=BG)
        profile_row.pack(fill="x", pady=(4, 6))
        tk.Label(profile_row, text="Perfil de combate:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")

        from combat import list_profiles
        profiles = list_profiles()
        current_profile = self.config_data["bot"].get("combat_profile", profiles[0] if profiles else "")
        self._combat_profile_var = tk.StringVar(value=current_profile)
        profile_cb = ttk.Combobox(profile_row, textvariable=self._combat_profile_var,
                                   values=profiles, state="readonly", width=16,
                                   font=("Segoe UI", 9))
        profile_cb.pack(side="left", padx=(8, 0))
        profile_cb.bind("<<ComboboxSelected>>", lambda e: self._on_profile_changed())

        ready_row = tk.Frame(combat_body, bg=BG)
        ready_row.pack(fill="x", pady=(0, 6))
        tk.Label(ready_row, text="Tiempo tras posicionarse (s):", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        initial_ready = self.config_data["bot"].get("combat_placement_settle_delay", 0.12)
        self._combat_placement_settle_var = tk.StringVar(value=str(float(initial_ready)))
        tk.Entry(ready_row, textvariable=self._combat_placement_settle_var, width=8,
                 bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 6))
        tk.Button(ready_row, text="Guardar", bg=GREEN, fg=BG,
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._save_combat_timing).pack(side="left")
        tk.Label(combat_body, text="Espera extra antes de marcar listo tras auto-posicionarse al iniciar combate.",
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 8), wraplength=420, justify="left").pack(anchor="w", pady=(0, 6))

        bot_cfg = self.config_data.get("bot", {})
        self._combat_poll_min_var = tk.StringVar(value=str(bot_cfg.get("combat_poll_interval_min", 1.0)))
        self._combat_poll_max_var = tk.StringVar(value=str(bot_cfg.get("combat_poll_interval_max", 1.0)))
        self._combat_spell_select_min_var = tk.StringVar(value=str(bot_cfg.get("combat_spell_select_delay_min", 0.12)))
        self._combat_spell_select_max_var = tk.StringVar(value=str(bot_cfg.get("combat_spell_select_delay_max", 0.12)))
        self._combat_post_click_min_var = tk.StringVar(value=str(bot_cfg.get("combat_post_click_delay_min", 0.12)))
        self._combat_post_click_max_var = tk.StringVar(value=str(bot_cfg.get("combat_post_click_delay_max", 0.12)))
        self._combat_cooldown_min_var = tk.StringVar(value=str(bot_cfg.get("combat_turn_cooldown_min", 0.35)))
        self._combat_cooldown_max_var = tk.StringVar(value=str(bot_cfg.get("combat_turn_cooldown_max", 0.35)))

        def _add_timing_fields():
            timing_container = tk.Frame(combat_body, bg=BG)
            timing_container.pack(fill="x", pady=(6, 6))
            tk.Label(timing_container, text="Velocidad de combate (min - max s):", bg=BG, fg=YELLOW, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))
            
            pairs = [
                ("Poll sniffer:", self._combat_poll_min_var, self._combat_poll_max_var),
                ("Delay select spell:", self._combat_spell_select_min_var, self._combat_spell_select_max_var),
                ("Delay post click:", self._combat_post_click_min_var, self._combat_post_click_max_var),
                ("Cooldown fin turno:", self._combat_cooldown_min_var, self._combat_cooldown_max_var),
            ]
            for label, vmin, vmax in pairs:
                row = tk.Frame(timing_container, bg=BG)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=label, bg=BG, fg=SUBTEXT, font=("Segoe UI", 8), width=18, anchor="w").pack(side="left")
                tk.Entry(row, textvariable=vmin, width=5, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 8)).pack(side="left", padx=(0, 4))
                tk.Label(row, text="-", bg=BG, fg=SUBTEXT, font=("Segoe UI", 8)).pack(side="left")
                tk.Entry(row, textvariable=vmax, width=5, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Consolas", 8)).pack(side="left", padx=(4, 6))

            btn_row = tk.Frame(timing_container, bg=BG)
            btn_row.pack(fill="x", pady=(4, 0))
            tk.Button(btn_row, text="Guardar tiempos", bg=GREEN, fg=BG, font=("Segoe UI", 8, "bold"), relief="flat", padx=8, pady=2, cursor="hand2", command=self._save_combat_speed_timing).pack(side="left")
        
        _add_timing_fields()

        players_header, players_body = self._collapsible_section(parent, "Players", start_collapsed=False)
        follow_row = tk.Frame(players_body, bg=BG)
        follow_row.pack(fill="x", pady=(0, 6))
        leveling_cfg = self.config_data.setdefault("leveling", {})
        self._follow_players_var = tk.BooleanVar(value=bool(leveling_cfg.get("follow_players_enabled", False)))
        self._follow_players_count_var = tk.StringVar(value="IDs guardados: 0")
        tk.Checkbutton(
            follow_row,
            text="Seguir players guardados",
            variable=self._follow_players_var,
            bg=BG,
            activebackground=BG,
            fg=GREEN,
            selectcolor=BG,
            font=("Segoe UI", 9),
            command=self._save_follow_players_setting,
        ).pack(side="left")
        tk.Label(follow_row, textvariable=self._follow_players_count_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(10, 0))

        follow_detail_row = tk.Frame(players_body, bg=BG)
        follow_detail_row.pack(fill="x", pady=(0, 6))
        tk.Label(follow_detail_row, text="Player guardado:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left")
        self._follow_player_selected_var = tk.StringVar(value="")
        self._follow_player_combo = ttk.Combobox(
            follow_detail_row,
            textvariable=self._follow_player_selected_var,
            state="readonly",
            width=28,
            values=[],
        )
        self._follow_player_combo.pack(side="left", padx=(6, 6))
        self._follow_player_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_follow_player_selected())
        self._follow_player_enabled_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            follow_detail_row,
            text="Activo",
            variable=self._follow_player_enabled_var,
            bg=BG,
            activebackground=BG,
            fg=GREEN,
            selectcolor=BG,
            font=("Segoe UI", 8, "bold"),
            command=self._save_selected_follow_player_enabled,
        ).pack(side="left")
        self._follow_player_status_var = tk.StringVar(value="")
        tk.Label(follow_detail_row, textvariable=self._follow_player_status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
        self._refresh_follow_player_controls()

        join_fight_row = tk.Frame(players_body, bg=BG)
        join_fight_row.pack(fill="x", pady=(0, 6))
        tk.Label(join_fight_row, text="Unirse a peleas de:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(anchor="w")
        join_fight_form = tk.Frame(join_fight_row, bg=BG)
        join_fight_form.pack(fill="x", pady=(4, 0))
        self._join_external_fight_actor_var = tk.StringVar(value="")
        self._join_external_fight_actor_combo = ttk.Combobox(
            join_fight_form,
            textvariable=self._join_external_fight_actor_var,
            state="readonly",
            width=28,
            values=[],
        )
        self._join_external_fight_actor_combo.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self._join_external_fight_actor_combo.bind("<<ComboboxSelected>>", lambda _e: self._save_external_fight_join_settings())
        leveling_cfg = self.config_data.setdefault("leveling", {})
        self._join_external_fight_any_var = tk.BooleanVar(
            value=bool(leveling_cfg.get("join_external_fights_any", False))
        )
        tk.Checkbutton(
            join_fight_form,
            text="Cualquier pelea",
            variable=self._join_external_fight_any_var,
            bg=BG,
            activebackground=BG,
            fg=GREEN,
            selectcolor=BG,
            font=("Segoe UI", 8, "bold"),
            command=self._save_external_fight_join_settings,
        ).pack(side="left", padx=(6, 0))
        self._join_external_fight_status_var = tk.StringVar(value="")
        tk.Label(players_body, textvariable=self._join_external_fight_status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))
        self._refresh_external_fight_join_controls()

        conditions_header, conditions_body = self._collapsible_section(parent, "Condiciones del combate", start_collapsed=False)
        conditions_row = tk.Frame(conditions_body, bg=BG)
        conditions_row.pack(fill="x", pady=(0, 6))
        self._ignore_single_mob_groups_var = tk.BooleanVar(
            value=bool(leveling_cfg.get("ignore_single_mob_groups", False))
        )
        tk.Checkbutton(
            conditions_row,
            text="Ignorar todo combate con solo 1 mob",
            variable=self._ignore_single_mob_groups_var,
            bg=BG,
            activebackground=BG,
            fg=YELLOW,
            selectcolor=BG,
            font=("Segoe UI", 8, "bold"),
            command=self._save_ignore_single_mob_groups,
        ).pack(side="left")

        # Fila de configuracion especifica por perfil
        self._profile_extra_frame = tk.Frame(combat_body, bg=BG)
        self._profile_extra_frame.pack(fill="x")
        self._build_profile_extra()

    def _build_status(self, parent):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        self._status_frame = row
        tk.Label(row, text="Estado:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")
        self.lbl_status = tk.Label(row, text="Detenido", bg=BG, fg=RED,
                                   font=("Segoe UI", 9, "bold"))
        self.lbl_status.pack(side="left", padx=6)
        self.lbl_pods = tk.Label(row, text="PODS: ? / ?", bg=BG, fg=SUBTEXT,
                                 font=("Segoe UI", 9, "bold"))
        self.lbl_pods.pack(side="left", padx=10)
        self.lbl_count = tk.Label(row, text="Cosechados: 0", bg=BG, fg=SUBTEXT,
                                  font=("Segoe UI", 9))
        self.lbl_count.pack(side="right")

    def _build_log(self, parent):
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(10, 2))
        tk.Label(header, text="Log", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(header, text="Copiar todo", bg=ACCENT, fg=TEXT,
                  font=("Segoe UI", 7, "bold"), relief="flat", padx=8, pady=1,
                  cursor="hand2", command=self._copy_log_text).pack(side="right")
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="both")
        self._log_frame = frame
        self.log_text = tk.Text(frame, height=12, width=52, bg=PANEL, fg=TEXT,
                                font=("Consolas", 8), relief="flat",
                                state="disabled", wrap="word")
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", padx=6, pady=6)
        scroll.pack(side="right", fill="y")

    def _copy_log_text(self):
        if not hasattr(self, "log_text") or self.log_text is None:
            return
        text = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)

    # ------------------------------------------------------------ Actions --
    def _check_resource(self, resource_name: str, profession: str, lbl_count: tk.Label):
        """Captura el monitor y cuenta cuantos recursos detecta."""
        lbl_count.config(text="...", fg=YELLOW)
        self.update_idletasks()

        def run():
            import numpy as np
            from detector import Detector
            threshold = self.config_data["bot"].get("threshold", 0.55)
            detector = Detector(threshold=threshold)
            monitor_idx = self.config_data["game"].get("monitor", 2)
            with mss.mss() as sct:
                monitor = sct.monitors[monitor_idx]
                shot = sct.grab(monitor)
                frame = np.ascontiguousarray(np.array(shot)[:, :, :3])
            matches = detector.find_all_resources(frame, resource_name, profession=profession)
            count = len(matches)
            color = GREEN if count > 0 else RED
            lbl_count.config(text=f"{count} det.", fg=color)

        threading.Thread(target=run, daemon=True).start()

    def _open_capture(self, prefill=None, profession: str | None = None):
        if profession is None:
            professions = _list_professions()
            if not professions:
                messagebox.showinfo("Sin profesiones",
                                    "Crea una profesion primero con '+ Nueva profesion'.",
                                    parent=self)
                return
            profession = professions[0]

        save_dir = os.path.join(RESOURCES_DIR, profession)
        os.makedirs(save_dir, exist_ok=True)
        monitor = self.config_data["game"].get("monitor", 2)

        def on_saved(name):
            self.config_data = load_config()
            profs = self.config_data["farming"].setdefault("professions", {})
            prof_data = profs.setdefault(profession, {"enabled": True, "resources": []})
            if name not in prof_data["resources"]:
                prof_data["resources"].append(name)
            save_config(self.config_data)
            self.config_data = load_config()
            self._refresh_resources()

        win = ResourceCaptureWindow(self, monitor, on_saved, save_dir=save_dir)
        if prefill:
            win._prefill_name = prefill

    def _load_profession_wait(self):
        """Carga el collect_min_wait de la profesion seleccionada en el spinbox."""
        profession = self._resource_node_prof_var.get().strip()
        if not profession:
            return
        prof_cfg = self.config_data.get("farming", {}).get("professions", {}).get(profession, {})
        wait = prof_cfg.get("collect_min_wait",
               self.config_data.get("bot", {}).get("collect_min_wait", 7.0))
        self._resource_node_wait_var.set(str(float(wait)))

    def _save_profession_wait(self):
        """Guarda el collect_min_wait en el config para la profesion seleccionada."""
        profession = self._resource_node_prof_var.get().strip()
        if not profession:
            return
        try:
            wait = float(self._resource_node_wait_var.get())
            if wait < 0.5:
                wait = 0.5
        except ValueError:
            return
        profs = self.config_data["farming"].setdefault("professions", {})
        prof_data = profs.setdefault(profession, {"enabled": True, "resources": []})
        prof_data["collect_min_wait"] = round(wait, 1)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        print(f"[GUI] Tiempo de cosecha {profession}: {wait:.1f}s guardado")
        # Feedback visual
        if self._wait_saved_lbl and self._wait_saved_lbl.winfo_exists():
            self._wait_saved_lbl.config(text=f"✓ {wait:.1f}s guardado")
            self.after(2000, lambda: self._wait_saved_lbl.config(text="")
                       if self._wait_saved_lbl and self._wait_saved_lbl.winfo_exists() else None)

    def _new_profession_from_nodes(self):
        """Crear nueva profesion desde el editor de nodos (refleja en ambas secciones)."""
        name = simpledialog.askstring("Nueva profesion", "Nombre de la profesion:", parent=self)
        if not name:
            return
        name = name.strip()
        prof_dir = os.path.join(RESOURCES_DIR, name)
        os.makedirs(prof_dir, exist_ok=True)
        profs = self.config_data["farming"].setdefault("professions", {})
        if name not in profs:
            profs[name] = {"enabled": True, "resources": []}
            save_config(self.config_data)
        self._resource_node_prof_var.set(name)
        self._refresh_resources()  # actualiza la sección Recursos por Profesion también

    def _add_resource_to_profession(self):
        """Agregar un nuevo recurso a la profesion seleccionada por nombre."""
        profession = self._resource_node_prof_var.get().strip()
        if not profession:
            messagebox.showwarning("Sin profesion", "Selecciona o crea una profesion primero.", parent=self)
            return
        name = simpledialog.askstring(
            "Nuevo recurso",
            f"Nombre del recurso para '{profession}':",
            parent=self,
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        # Agregar al config
        profs = self.config_data["farming"].setdefault("professions", {})
        prof_data = profs.setdefault(profession, {"enabled": True, "resources": []})
        if name not in prof_data["resources"]:
            prof_data["resources"].append(name)
            save_config(self.config_data)
            self.config_data = load_config()
        # Seleccionar el nuevo recurso en el dropdown
        self._resource_node_res_var.set(name)
        self._refresh_resource_node_resource_choices()
        # Notificar que falta el sprite
        sprite_path = self._resource_sprite_path(profession, name)
        if not os.path.exists(sprite_path):
            if messagebox.askyesno(
                "Capturar sprite",
                f"'{name}' agregado. Aun no tiene sprite.\n¿Capturar sprite ahora?",
                parent=self,
            ):
                self._open_capture(prefill=name, profession=profession)

    def _new_profession(self):
        name = simpledialog.askstring("Nueva profesion", "Nombre de la profesion:", parent=self)
        if not name:
            return
        name = name.strip()
        prof_dir = os.path.join(RESOURCES_DIR, name)
        os.makedirs(prof_dir, exist_ok=True)
        profs = self.config_data["farming"].setdefault("professions", {})
        if name not in profs:
            profs[name] = {"enabled": True, "resources": []}
            save_config(self.config_data)
        self._refresh_resources()

    def _update_resources(self):
        existing = self.config_data["farming"].get("professions", {})
        new_professions = {}
        for (prof, res), var in self.resource_vars.items():
            if prof not in new_professions:
                enabled = existing.get(prof, {}).get("enabled", True)
                new_professions[prof] = {"enabled": enabled, "resources": []}
            if var.get():
                new_professions[prof]["resources"].append(res)
        self.config_data["farming"]["professions"] = new_professions
        save_config(self.config_data)

    def _delete_resource(self, profession: str, resource_name: str):
        confirm = messagebox.askyesno(
            "Eliminar material",
            (
                f"Eliminar '{resource_name}' de '{profession}'?\n\n"
                "Esto borrara el template PNG y tambien eliminara los nodos guardados "
                "de ese material en todos los mapas."
            ),
            parent=self,
        )
        if not confirm:
            return

        template_path = os.path.join(RESOURCES_DIR, profession, f"{resource_name}.png")
        removed_nodes = 0

        if os.path.exists(template_path):
            try:
                os.remove(template_path)
            except OSError as exc:
                messagebox.showerror("No se pudo eliminar", f"No pude borrar el template:\n{exc}", parent=self)
                return

        professions = self.config_data.setdefault("farming", {}).setdefault("professions", {})
        prof_cfg = professions.get(profession, {})
        resources = list(prof_cfg.get("resources", []))
        prof_cfg["resources"] = [r for r in resources if r != resource_name]
        professions[profession] = prof_cfg

        nodes_cfg = self.config_data.setdefault("farming", {}).setdefault("resource_nodes_by_map_id", {})
        empty_maps = []
        for map_id, entries in nodes_cfg.items():
            kept = []
            for entry in entries:
                if entry.get("profession") == profession and entry.get("resource") == resource_name:
                    removed_nodes += 1
                    continue
                kept.append(entry)
            nodes_cfg[map_id] = kept
            if not kept:
                empty_maps.append(map_id)
        for map_id in empty_maps:
            nodes_cfg.pop(map_id, None)

        if self._resource_node_prof_var.get() == profession and self._resource_node_res_var.get() == resource_name:
            self._resource_node_res_var.set("")

        save_config(self.config_data)
        self.config_data = load_config()
        self._refresh_resources()
        self._refresh_resource_node_resource_choices()
        self._refresh_resource_node_list()
        self.log_queue.put((
            "log",
            f"[NODES] Material eliminado {profession}/{resource_name} | nodos borrados: {removed_nodes}"
        ))

    def _on_profile_changed(self):
        self._save_combat_profile()
        self._build_profile_extra()

    def _save_combat_profile(self):
        self.config_data["bot"]["combat_profile"] = self._combat_profile_var.get()
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[COMBAT] Perfil de combate guardado: {self._combat_profile_var.get()}"))

    def _save_primary_actor_id(self):
        raw = (self._primary_actor_id_var.get() or "").strip()
        if raw:
            if not raw.lstrip("+-").isdigit() or int(raw) <= 0:
                messagebox.showwarning(
                    "Actor ID invalido",
                    "Ingresa un Actor ID numerico positivo del PJ actual.",
                    parent=self,
                )
                return
            actor_id = str(int(raw))
        else:
            actor_id = None

        self.config_data.setdefault("bot", {})["actor_id"] = actor_id
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        rendered = actor_id or "sin configurar"
        self._primary_actor_status_var.set(f"Actual: {rendered}")
        self.log_queue.put(("log", f"[BOT] Actor ID principal guardado: {rendered}"))

    def _save_combat_timing(self):
        try:
            delay = float((self._combat_placement_settle_var.get() or "").strip())
        except ValueError:
            messagebox.showwarning(
                "Tiempo invalido",
                "Ingresa un numero valido de segundos para la espera tras posicionarse.",
                parent=self,
            )
            return
        delay = max(0.0, round(delay, 2))
        self.config_data["bot"]["combat_placement_settle_delay"] = delay
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[COMBAT] Tiempo tras posicionarse guardado: {delay:.2f}s"))

    def _normalized_follow_player_db(self) -> dict[str, dict]:
        leveling_cfg = self.config_data.setdefault("leveling", {})
        follow_ids = leveling_cfg.get("follow_player_actor_ids", [])
        raw_db = leveling_cfg.get("follow_player_db", {})
        normalized: dict[str, dict] = {}
        if isinstance(raw_db, dict):
            for raw_actor_id, raw_value in raw_db.items():
                actor_id = str(raw_actor_id).strip()
                if not actor_id or not actor_id.lstrip("+-").isdigit() or int(actor_id) <= 0:
                    continue
                if isinstance(raw_value, dict):
                    name = str(raw_value.get("name", "")).strip() or f"Player {actor_id}"
                    enabled = bool(raw_value.get("enabled", True))
                else:
                    name = str(raw_value).strip() or f"Player {actor_id}"
                    enabled = True
                normalized[actor_id] = {"name": name, "enabled": enabled}
        for raw_actor_id in follow_ids:
            actor_id = str(raw_actor_id).strip()
            if not actor_id or not actor_id.lstrip("+-").isdigit() or int(actor_id) <= 0:
                continue
            normalized.setdefault(actor_id, {"name": f"Player {actor_id}", "enabled": True})
        return normalized

    def _save_normalized_follow_player_db(self, normalized: dict[str, dict]):
        leveling_cfg = self.config_data.setdefault("leveling", {})
        cleaned: dict[str, dict] = {}
        enabled_ids: list[str] = []
        for actor_id, payload in sorted(normalized.items(), key=lambda item: int(item[0])):
            actor = str(actor_id).strip()
            if not actor or not actor.lstrip("+-").isdigit() or int(actor) <= 0:
                continue
            name = str(payload.get("name", "")).strip() or f"Player {actor}"
            enabled = bool(payload.get("enabled", True))
            cleaned[actor] = {"name": name, "enabled": enabled}
            if enabled:
                enabled_ids.append(actor)
        leveling_cfg["follow_player_db"] = cleaned
        leveling_cfg["follow_player_actor_ids"] = enabled_ids

    def _follow_player_display_values(self) -> list[str]:
        values = []
        for actor_id, payload in sorted(self._normalized_follow_player_db().items(), key=lambda item: int(item[0])):
            state = "ON" if payload.get("enabled", True) else "OFF"
            values.append(f"{actor_id} | {payload.get('name', '')} | {state}")
        return values

    def _selected_external_fight_actor_from_combo(self) -> str | None:
        if not hasattr(self, "_join_external_fight_actor_var"):
            return None
        raw = (self._join_external_fight_actor_var.get() or "").strip()
        if not raw:
            return None
        actor_id = raw.split("|", 1)[0].strip()
        if actor_id and actor_id.lstrip("+-").isdigit() and int(actor_id) > 0:
            return actor_id
        return None

    def _refresh_external_fight_join_controls(self):
        if not hasattr(self, "_join_external_fight_actor_combo"):
            return
        values = self._follow_player_display_values()
        self._join_external_fight_actor_combo["values"] = values
        leveling_cfg = self.config_data.setdefault("leveling", {})
        saved_actor = str(leveling_cfg.get("join_external_fights_actor_id", "") or "").strip()
        normalized = self._normalized_follow_player_db()
        if saved_actor and saved_actor in normalized:
            payload = normalized[saved_actor]
            state = "ON" if payload.get("enabled", True) else "OFF"
            self._join_external_fight_actor_var.set(f"{saved_actor} | {payload.get('name', '')} | {state}")
        elif values:
            self._join_external_fight_actor_var.set(values[0])
            saved_actor = self._selected_external_fight_actor_from_combo() or ""
        else:
            self._join_external_fight_actor_var.set("")
            saved_actor = ""
        any_enabled = bool(leveling_cfg.get("join_external_fights_any", False))
        if hasattr(self, "_join_external_fight_any_var"):
            self._join_external_fight_any_var.set(any_enabled)
        if hasattr(self, "_join_external_fight_status_var"):
            if any_enabled:
                self._join_external_fight_status_var.set("Modo: unirse a cualquier pelea")
            elif saved_actor:
                self._join_external_fight_status_var.set(f"Modo: unirse solo a peleas de actor {saved_actor}")
            else:
                self._join_external_fight_status_var.set("Modo: unión automática desactivada")

    def _save_external_fight_join_settings(self):
        leveling_cfg = self.config_data.setdefault("leveling", {})
        actor_id = self._selected_external_fight_actor_from_combo()
        any_enabled = bool(self._join_external_fight_any_var.get()) if hasattr(self, "_join_external_fight_any_var") else False
        leveling_cfg["join_external_fights_actor_id"] = actor_id or None
        leveling_cfg["join_external_fights_any"] = any_enabled
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._refresh_external_fight_join_controls()
        if any_enabled:
            self.log_queue.put(("log", "[PLAYERS] unión automática configurada: cualquier pelea"))
        else:
            self.log_queue.put(("log", f"[PLAYERS] unión automática configurada para actor={actor_id or 'ninguno'}"))

    def _selected_follow_player_actor_from_combo(self) -> str | None:
        raw = (self._follow_player_selected_var.get() or "").strip()
        if not raw:
            return None
        actor_id = raw.split("|", 1)[0].strip()
        if actor_id and actor_id.lstrip("+-").isdigit() and int(actor_id) > 0:
            return actor_id
        return None

    def _refresh_follow_player_controls(self, preferred_actor_id: str | None = None):
        if not hasattr(self, "_follow_players_count_var"):
            return
        normalized = self._normalized_follow_player_db()
        total = len(normalized)
        self._follow_players_count_var.set(f"IDs guardados: {total}")
        values = self._follow_player_display_values() if hasattr(self, "_follow_player_combo") else []
        if hasattr(self, "_follow_player_combo"):
            self._follow_player_combo["values"] = values
        saved_selected = str(self.config_data.get("leveling", {}).get("follow_player_selected_actor_id", "")).strip()
        actor_id = preferred_actor_id or self._selected_follow_player_actor_from_combo() or saved_selected
        if actor_id not in normalized:
            actor_id = next(iter(normalized.keys()), None)
        if actor_id and hasattr(self, "_follow_player_selected_var"):
            payload = normalized[actor_id]
            self._follow_player_selected_var.set(
                f"{actor_id} | {payload.get('name', '')} | {'ON' if payload.get('enabled', True) else 'OFF'}"
            )
            if hasattr(self, "_follow_player_enabled_var"):
                self._follow_player_enabled_var.set(bool(payload.get("enabled", True)))
            if hasattr(self, "_follow_player_status_var"):
                self._follow_player_status_var.set(payload.get("name", ""))
        else:
            if hasattr(self, "_follow_player_selected_var"):
                self._follow_player_selected_var.set("")
            if hasattr(self, "_follow_player_enabled_var"):
                self._follow_player_enabled_var.set(False)
            if hasattr(self, "_follow_player_status_var"):
                self._follow_player_status_var.set("Sin players guardados")
        self._refresh_external_fight_join_controls()

    def _on_follow_player_selected(self):
        actor_id = self._selected_follow_player_actor_from_combo()
        leveling_cfg = self.config_data.setdefault("leveling", {})
        leveling_cfg["follow_player_selected_actor_id"] = actor_id or None
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._refresh_follow_player_controls(preferred_actor_id=actor_id)
        self._refresh_external_fight_join_controls()

    def _save_selected_follow_player_enabled(self):
        actor_id = self._selected_follow_player_actor_from_combo()
        if not actor_id:
            return
        normalized = self._normalized_follow_player_db()
        payload = normalized.get(actor_id)
        if payload is None:
            return
        payload["enabled"] = bool(self._follow_player_enabled_var.get())
        normalized[actor_id] = payload
        self._save_normalized_follow_player_db(normalized)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._refresh_follow_player_controls(preferred_actor_id=actor_id)
        self._refresh_external_fight_join_controls()
        state = "activo" if self._follow_player_enabled_var.get() else "inactivo"
        self.log_queue.put(("log", f"[PLAYERS] {actor_id} marcado como {state}"))

    def _save_follow_players_setting(self):
        leveling_cfg = self.config_data.setdefault("leveling", {})
        leveling_cfg["follow_players_enabled"] = bool(self._follow_players_var.get())
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        total = len(self._normalized_follow_player_db())
        self._refresh_follow_player_controls()
        self._refresh_external_fight_join_controls()
        state = "activo" if self.config_data.get("leveling", {}).get("follow_players_enabled") else "inactivo"
        self.log_queue.put(("log", f"[PLAYERS] Seguimiento de players {state} | IDs={total}"))

    def _selected_followable_actor_id(self) -> str | None:
        entry = self._sniffer_selection_entry or {}
        actor_id = str(entry.get("actor_id", "")).strip()
        if not actor_id or not actor_id.lstrip("+-").isdigit():
            return None
        if int(actor_id) <= 0:
            return None
        bot = self.bot_thread.bot if self.bot_thread and self.bot_thread.bot else None
        if bot is not None and bot._actor_ids_match(actor_id, getattr(bot, "_sniffer_my_actor", None)):
            return None
        return actor_id

    def _save_selected_follow_player_id(self):
        actor_id = self._selected_followable_actor_id()
        if not actor_id:
            messagebox.showwarning(
                "Player invalido",
                "Selecciona primero un player real del sniffer para guardar su Actor ID.",
                parent=self,
            )
            return
        normalized = self._normalized_follow_player_db()
        payload = normalized.get(actor_id, {"name": f"Player {actor_id}", "enabled": True})
        payload["enabled"] = True
        normalized[actor_id] = payload
        self._save_normalized_follow_player_db(normalized)
        leveling_cfg = self.config_data.setdefault("leveling", {})
        leveling_cfg["follow_players_enabled"] = True
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        if hasattr(self, "_follow_players_var"):
            self._follow_players_var.set(True)
        self._refresh_follow_player_controls(preferred_actor_id=actor_id)
        self._refresh_external_fight_join_controls()
        if self._sniffer_test_status_var is not None:
            self._sniffer_test_status_var.set(f"Player guardado para seguimiento: actor={actor_id}")
        self.log_queue.put(("log", f"[PLAYERS] Actor ID guardado para seguimiento: {actor_id}"))

    def _remove_selected_follow_player_id(self):
        actor_id = self._selected_followable_actor_id()
        if not actor_id:
            messagebox.showwarning(
                "Player invalido",
                "Selecciona primero un player real del sniffer para quitar su Actor ID.",
                parent=self,
            )
            return
        normalized = self._normalized_follow_player_db()
        if actor_id not in normalized:
            if self._sniffer_test_status_var is not None:
                self._sniffer_test_status_var.set(f"Actor {actor_id} no estaba guardado")
            return
        normalized.pop(actor_id, None)
        self._save_normalized_follow_player_db(normalized)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self._refresh_follow_player_controls()
        self._refresh_external_fight_join_controls()
        if self._sniffer_test_status_var is not None:
            self._sniffer_test_status_var.set(f"Player quitado del seguimiento: actor={actor_id}")
        self.log_queue.put(("log", f"[PLAYERS] Actor ID quitado del seguimiento: {actor_id}"))

    def _save_mob_scan_timing(self):
        try:
            scans = int((self._combat_scan_move_var.get() or "").strip())
        except ValueError:
            messagebox.showwarning(
                "Valor invalido",
                "Ingresa un numero entero de escaneos antes de mover.",
                parent=self,
            )
            return
        scans = max(1, scans)
        nav = self.config_data.setdefault("navigation", {})
        nav["empty_scans_before_move"] = scans
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put(("log", f"[NAV] Scans vacios antes de mover guardados: {scans}"))

    def _save_combat_speed_timing(self):
        try:
            poll_min = max(0.1, round(float((self._combat_poll_min_var.get() or "").strip()), 2))
            poll_max = max(0.1, round(float((self._combat_poll_max_var.get() or "").strip()), 2))
            spell_min = max(0.0, round(float((self._combat_spell_select_min_var.get() or "").strip()), 2))
            spell_max = max(0.0, round(float((self._combat_spell_select_max_var.get() or "").strip()), 2))
            post_min = max(0.0, round(float((self._combat_post_click_min_var.get() or "").strip()), 2))
            post_max = max(0.0, round(float((self._combat_post_click_max_var.get() or "").strip()), 2))
            cooldown_min = max(0.0, round(float((self._combat_cooldown_min_var.get() or "").strip()), 2))
            cooldown_max = max(0.0, round(float((self._combat_cooldown_max_var.get() or "").strip()), 2))
        except ValueError:
            messagebox.showwarning(
                "Valor invalido",
                "Ingresa numeros validos para los tiempos de combate.",
                parent=self,
            )
            return
        bot_cfg = self.config_data.setdefault("bot", {})
        bot_cfg["combat_poll_interval_min"] = min(poll_min, poll_max)
        bot_cfg["combat_poll_interval_max"] = max(poll_min, poll_max)
        bot_cfg["combat_spell_select_delay_min"] = min(spell_min, spell_max)
        bot_cfg["combat_spell_select_delay_max"] = max(spell_min, spell_max)
        bot_cfg["combat_post_click_delay_min"] = min(post_min, post_max)
        bot_cfg["combat_post_click_delay_max"] = max(post_min, post_max)
        bot_cfg["combat_turn_cooldown_min"] = min(cooldown_min, cooldown_max)
        bot_cfg["combat_turn_cooldown_max"] = max(cooldown_min, cooldown_max)
        save_config(self.config_data)
        self.config_data = load_config()
        self._sync_runtime_bot_config()
        self.log_queue.put((
            "log",
            f"[COMBAT] Rangos guardados poll={bot_cfg['combat_poll_interval_min']:.2f}-{bot_cfg['combat_poll_interval_max']:.2f}s "
            f"spell={bot_cfg['combat_spell_select_delay_min']:.2f}-{bot_cfg['combat_spell_select_delay_max']:.2f}s "
            f"post={bot_cfg['combat_post_click_delay_min']:.2f}-{bot_cfg['combat_post_click_delay_max']:.2f}s "
            f"cooldown={bot_cfg['combat_turn_cooldown_min']:.2f}-{bot_cfg['combat_turn_cooldown_max']:.2f}s",
        ))

    def _build_profile_extra(self):
        for w in self._profile_extra_frame.winfo_children():
            w.destroy()
        return

    def _capture_sacro_self_pos(self):
        self._sacro_pos_lbl.config(text="Esperando...", fg=YELLOW)
        import pyautogui
        def _do():
            time.sleep(3)
            mouse_x, mouse_y = pyautogui.position()
            info = self._detect_pj_card()
            threshold = info["threshold"]
            match = info["match"]
            best_score = info["best_score"]

            if match is None:
                self.after(0, lambda: self._sacro_pos_lbl.config(text="PJ no detectado", fg=RED))
                self.log_queue.put(("log", f"[SACRO] No pude calibrar offset: PJ.png no detectado | score={best_score:.4f} | threshold={threshold:.4f}"))
                return

            match_x, match_y = match
            self.config_data["bot"]["sacrogito_self_pos"] = [mouse_x, mouse_y]
            save_config(self.config_data)
            self._sync_runtime_bot_config()
            self.after(0, lambda: self._sacro_pos_lbl.config(text=f"{mouse_x}, {mouse_y}", fg=GREEN))
            self.log_queue.put((
                "log",
                f"[SACRO] Posicion capturada: match=({match_x}, {match_y}) mouse=({mouse_x}, {mouse_y})"
            ))
        threading.Thread(target=_do, daemon=True).start()

    def _toggle_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self):
        self._start_bot_with_mode(test_mode=False)

    def _start_test_mode(self):
        self._start_bot_with_mode(test_mode=True)

    def _start_bot_with_mode(self, test_mode: bool):
        if self.bot_thread and self.bot_thread.is_alive():
            return
        self._update_resources()
        config = load_config()
        self.bot_thread = BotThread(config, self.log_queue, test_mode=test_mode)
        self.bot_thread.start()
        self.btn_toggle.config(text="⏹  Detener", bg=RED, fg=TEXT)
        self.btn_test.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.lbl_status.config(text="TEST" if test_mode else "Corriendo", fg=BLUE if test_mode else GREEN)

    def _pause_bot(self):
        if self.bot_thread:
            self.bot_thread.pause()

    def _stop_bot(self):
        if self.bot_thread:
            self.bot_thread.stop()
        self.btn_toggle.config(text="▶  Iniciar", bg=GREEN, fg=BG)
        self.btn_test.config(state="normal")
        self.btn_pause.config(state="disabled", text="⏸  Pausar")
        self.lbl_status.config(text="Detenido", fg=RED)

    def _simulate_unload(self):
        if self.bot_thread and self.bot_thread.bot:
            self.bot_thread.bot.simulate_unload()
        else:
            messagebox.showinfo("Bot detenido", "Inicia el bot primero para simular la descarga.", parent=self)

    # --------------------------------------------------------- Queue poll --
    def _poll_queue(self):
        try:
            while True:
                event, data = self.log_queue.get_nowait()
                if event == "log":
                    self._append_log(data)
                    if "Cosechados:" in data:
                        total = data.split("Cosechados:")[-1].strip()
                        self.lbl_count.config(text=f"Cosechados: {total}")
                    if "[MOBS_ACTIVATED]" in data or "[TELEPORT] Ruta" in data or "[UNLOAD] Pócima" in data:
                        self.config_data = load_config()
                        self._sync_runtime_bot_config()
                        self._refresh_mobs()
                        self._refresh_navigation_profiles()
                        self._update_leveling_route_toggle_button()
                    # Redirigir a ventana de debug si está abierta
                    cb = getattr(self, "_raw_log_cb", None)
                    if cb:
                        cb(data)
                elif event == "paused":
                    if data:
                        self.btn_pause.config(text="▶  Reanudar")
                        self.lbl_status.config(text="Pausado", fg=YELLOW)
                    else:
                        self.btn_pause.config(text="⏸  Pausar")
                        self.lbl_status.config(text="Corriendo", fg=GREEN)
                elif event == "scan_mobs_empty":
                    self.log_queue.put(("log", "[MOBS] Todos los mobs quedaron desactivados"))
                    self._refresh_mobs()
                elif event == "stopped":
                    self.bot_thread = None
                    self._stop_bot()
        except queue.Empty:
            pass

        # Actualizar label de pods
        if hasattr(self, "lbl_pods") and self.bot_thread and self.bot_thread.bot:
            bot = self.bot_thread.bot
            p_curr = bot.current_pods if bot.current_pods is not None else "?"
            p_max = bot.max_pods if bot.max_pods is not None else "?"
            self.lbl_pods.config(text=f"PODS: {p_curr} / {p_max}")

        # Actualizar label de estado del sniffer
        if hasattr(self, "_lbl_sniffer_status"):
            if self.bot_thread and self.bot_thread.bot:
                bot = self.bot_thread.bot
                if bot.sniffer_active:
                    actor = bot._sniffer_my_actor or "?"
                    self._lbl_sniffer_status.config(text=f"activo | actor {actor}", fg=GREEN)
                elif self._sniffer_var.get():
                    self._lbl_sniffer_status.config(text="esperando (necesita admin + Npcap)", fg=YELLOW)
                else:
                    self._lbl_sniffer_status.config(text="inactivo", fg=SUBTEXT)
            elif self._sniffer_var.get():
                self._lbl_sniffer_status.config(text="configurado", fg=YELLOW)
            else:
                self._lbl_sniffer_status.config(text="inactivo", fg=SUBTEXT)

        current_map_id = self._current_runtime_map_id()
        if current_map_id is not None and self._resource_node_map_var.get() != str(current_map_id):
            self._resource_node_map_var.set(str(current_map_id))
            self._refresh_resource_node_list()
        self._refresh_main_runtime_summary()

        try:
            self._refresh_sniffer_tab()
        except Exception as exc:
            try:
                if self._sniffer_grid_status_var is not None:
                    self._sniffer_grid_status_var.set(f"Sniffer tab falló: {exc}")
            except Exception:
                pass
            try:
                self._append_log(f"[GUI] _refresh_sniffer_tab fallo: {exc}")
            except Exception:
                pass

        self.after(40, self._poll_queue)

    def _append_log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")


if __name__ == "__main__":
    app = App()
    app.mainloop()
