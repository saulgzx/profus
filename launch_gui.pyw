import os
import sys

ROOT = os.path.dirname(__file__)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from gui import App


if __name__ == "__main__":
    app = App()
    app.mainloop()
