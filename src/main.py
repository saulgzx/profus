import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8")

import yaml
import time
from pynput import keyboard
from bot import Bot

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
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    for key in ("bot", "farming", "game", "leveling", "navigation"):
        data.setdefault(key, {})
    return data


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
