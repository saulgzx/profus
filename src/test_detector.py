"""
Script de prueba del detector.
Captura la pantalla, busca el recurso y muestra el resultado con un rectangulo.
Ejecutar: python src/test_detector.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import cv2
import mss
import numpy as np
from detector import Detector


RESOURCE = "Trigo"  # Nombre del archivo sin .png


def select_monitor(choice=None):
    with mss.mss() as sct:
        monitors = sct.monitors[1:]
        print("\n[TEST] Monitores disponibles:")
        for i, m in enumerate(monitors):
            print(f"  [{i + 1}] {m['width']}x{m['height']} en ({m['left']}, {m['top']})")
        if choice is None:
            print("\nUsa: python src/test_detector.py <numero_monitor>")
            print("Ejemplo: python src/test_detector.py 1")
            sys.exit(0)
        return sct.monitors[int(choice)]


def capture_screen(monitor):
    with mss.mss() as sct:
        screenshot = sct.grab(monitor)
        return np.array(screenshot)[:, :, :3]


def main():
    monitor = select_monitor(sys.argv[1] if len(sys.argv) > 1 else None)
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.75
    detector = Detector(threshold=threshold)
    print(f"[TEST] Threshold: {threshold}")
    print(f"\n[TEST] Buscando '{RESOURCE}' en pantalla...")
    print("[TEST] Tienes 3 segundos para cambiar a la ventana de Dofus...")

    import time
    for i in range(3, 0, -1):
        print(f"  {i}...")
        time.sleep(1)

    frame = np.ascontiguousarray(capture_screen(monitor))
    matches = detector.find_all_resources(frame, RESOURCE)

    template_path = os.path.join(
        os.path.dirname(__file__), "..", "assets", "templates", "resources", f"{RESOURCE}.png"
    )
    template = cv2.imread(template_path)
    h, w = template.shape[:2]

    if matches:
        print(f"[TEST] ENCONTRADOS: {len(matches)} '{RESOURCE}'")
        for x, y in matches:
            cv2.rectangle(frame, (x - w // 2, y - h // 2), (x + w // 2, y + h // 2), (0, 255, 0), 2)
            cv2.putText(frame, RESOURCE, (x - w // 2, y - h // 2 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    else:
        print(f"[TEST] NO encontrado. Intenta con un threshold mas bajo.")

    # Mostrar resultado
    display = cv2.resize(frame, (1280, 720))
    cv2.imshow(f"Detector - {RESOURCE}", display)
    print("[TEST] Presiona cualquier tecla en la ventana de imagen para cerrar.")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
