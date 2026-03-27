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


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    config = load_config()
    bot = Bot(config)

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    print(f"[BOT] Iniciado. Pausa: F10 | Detener: F12")
    time.sleep(2)  # Tiempo para cambiar a la ventana del juego

    while running:
        if not paused:
            bot.tick()
        time.sleep(config["bot"]["delay_min"])

    listener.stop()
    print("[BOT] Finalizado.")


if __name__ == "__main__":
    main()
