"""notifications.py — Push notifications al celular vía ntfy.sh.

ntfy.sh es gratis, sin signup para topics públicos. Flujo:
  1. Elegís un topic largo + único (tipo `dofus-alexis-a3k9m2x7`). No lo
     compartas — cualquiera con ese topic puede leer tus alerts.
  2. En el celular instalás la app "ntfy" (Play Store / App Store).
  3. En la app, suscribite al mismo topic.
  4. El bot hace POST a https://ntfy.sh/{topic} → te llega como push.

Config en config.yaml:
  notifications:
    enabled: true
    ntfy_topic: dofus-alexis-a3k9m2x7   # ← cambialo por uno único
    ntfy_server: https://ntfy.sh        # opcional, por si querés self-host

Usa solo stdlib (urllib) — zero deps extra.
"""
from __future__ import annotations

import threading
import urllib.request
import urllib.error
from typing import Iterable

from app_logger import get_logger

_log = get_logger("bot.notify")



class NotificationClient:
    """Cliente simple para ntfy.sh. Non-blocking (cada send corre en thread)."""

    def __init__(self, topic: str | None, server: str = "https://ntfy.sh",
                 timeout: float = 5.0) -> None:
        self.topic = (topic or "").strip()
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.timeout = timeout
        self._enabled = bool(self.topic)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(
        self,
        title: str,
        message: str,
        priority: int = 3,
        tags: Iterable[str] | None = None,
    ) -> None:
        """Enviá una notificación. No bloqueante: corre en thread daemon.

        priority: 1 (min) a 5 (max/urgent). 3 = default.
        tags: lista de emoji-shortcodes (ej ["skull", "warning"]) que ntfy
              renderiza como emojis en la notificación.
        """
        if not self._enabled:
            return
        t = threading.Thread(
            target=self._send_sync,
            args=(title, message, priority, list(tags or [])),
            daemon=True,
            name="NotifyPush",
        )
        t.start()

    def _send_sync(self, title: str, message: str, priority: int, tags: list[str]) -> None:
        try:
            url = f"{self.server}/{self.topic}"
            # ntfy usa headers ASCII — si title tiene caracteres no-ASCII
            # (ej ñ, tildes, emojis) hay que encodear a UTF-8 + b64.
            # Versión simple: normalizar a ASCII best-effort.
            headers = {
                "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
                "Priority": str(max(1, min(5, int(priority)))),
            }
            if tags:
                headers["Tags"] = ",".join(tags)
            req = urllib.request.Request(
                url,
                data=message.encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status >= 400:
                    _log.info(f"[NOTIFY] ntfy respondió {r.status}")
        except urllib.error.URLError as exc:
            _log.info(f"[NOTIFY] error de red: {exc}")
        except Exception as exc:
            _log.info(f"[NOTIFY] error inesperado: {exc!r}")


_noop_client: NotificationClient | None = None


def get_noop() -> NotificationClient:
    """Cliente deshabilitado (no-op) — útil cuando config no está lista."""
    global _noop_client
    if _noop_client is None:
        _noop_client = NotificationClient(topic=None)
    return _noop_client
