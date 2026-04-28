import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8")

import yaml
import time
from pynput import keyboard
from bot import Bot
from mouse_tracer import enable_mouse_trace
enable_mouse_trace()

PAUSE_KEY = keyboard.Key.f10
STOP_KEY = keyboard.Key.f12

paused = False
running = True


def on_press(key):
    global paused, running
    if key == PAUSE_KEY:
        paused = not paused
        print("[BOT] Pausado" if paused else "[BOT] Reanudado")
    elif key == STOP_KEY:
        running = False
        print("[BOT] Detenido")


def load_config(path=None):
    """Wrapper compatible: delega a config_loader.load_config."""
    from config_loader import load_config as _shared_load
    return _shared_load(path)


def save_config(config, path=None):
    """Wrapper compatible: delega a config_loader.save_config.

    Usado por bot.py:3705 (`from main import save_config`).
    """
    from config_loader import save_config as _shared_save
    _shared_save(config, path)


def main():
    config = load_config()
    bot = Bot(config)

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    print(f"[BOT] Iniciado. Pausa: F10 | Detener: F12")
    time.sleep(2)  # Tiempo para cambiar a la ventana del juego

    # Loop event-driven: el sniffer marca `wake_event` al parsear cualquier
    # paquete del server (GTS, GA, GTM, GE, ...). El bot reacciona en <5ms
    # en vez de hasta 100ms. Si el sniffer aún no está activo (pre-combate,
    # arranque) caemos a `time.sleep(0.1)` como antes. Clear+tick+wait
    # asegura que no perdemos despertares: nuevos eventos llegados entre
    # drain y wait ya dejaron el flag en True y retornan al instante.
    while running:
        wake = getattr(bot, "sniffer_wake_event", None)
        if wake is not None:
            wake.clear()
        if not paused:
            bot.tick()
        if wake is not None:
            wake.wait(timeout=0.1)
        else:
            time.sleep(0.1)

    listener.stop()
    print("[BOT] Finalizado.")


if __name__ == "__main__":
    main()
