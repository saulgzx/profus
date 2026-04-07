"""
DofusSniffer — captura y parsea el protocolo texto de Dofus Retro.

Requiere Npcap instalado: https://npcap.com/#download
Requiere scapy:  pip install scapy

Protocolo Dofus Retro 1.x:
  - TCP puerto 443, texto plano SIN TLS (Ankama usa 443 para bypass de firewalls)
  - Delimitador: null byte \\x00 (algunos paquetes también \\n)
  - A partir de v1.39.5 los paquetes C→S llevan telemetría entre marcadores 0xC3 0xB9
  - Server → Client: GTS, GTE, GE, Gp, GA, Im, ...
  - Client → Server: GKK (pasar turno), GA (hechizo), GPA (listo colocacion), ...

Eventos emitidos al event_queue (tipo, datos):
  ("turn_start",   {"actor_id": str})          GTS — empieza turno del actor
  ("turn_end",     {"actor_id": str})          GTE — termina turno del actor
  ("fight_end",    {"raw": str})               GE  — combate terminado
  ("placement",    {"raw": str})               Gp  — fase de colocacion
  ("game_action",  {"raw": str, "actor_id": str, "action_id": str, "params": list})
  ("info_msg",     {"raw": str, "msg_id": str, "args": str})  Im — mensajes del sistema
  ("pa_update",    {"actor_id": str, "pa": int, "pm": int})   GA action 129
  ("pods_update",  {"current": int, "max": int})              Ow — actualización de pods (peso)
  ("raw_packet",   {"direction": str, "data": str})           solo en debug_mode=True
  ("player_profile", {"actor_id": str, "name": str, "raw": str}) PM~ — hint fuerte del actor propio
"""

import threading
import queue
import re

_HASH_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

DOFUS_PORT = 443          # Ankama usa 443 sin TLS para bypass de firewalls
TELEMETRY_MARKER = b'\xc3\xb9'  # marcador de telemetría v1.39.5+


# ─────────────────────────────────────────── parsers ──
def _parse_packets(raw: str) -> list[str]:
    """Divide un buffer en paquetes completos (delimitados por \\n o \\x00)."""
    return [p for p in re.split(r"[\n\x00]", raw) if p.strip()]


def _parse_game_action(data: str) -> dict:
    """Parsea un paquete GA.
    Formato observado: ;{actionId};{sequenceId};{params}
    Ejemplo: ;300;-1;626,150,0,1,0,0,1
    """
    parts = data.split(";")
    normalized_parts = parts[1:] if parts and parts[0] == "" else parts
    # Eliminar primer elemento vacío si el paquete empieza con ";"
    if parts and parts[0] == "":
        parts = parts[1:]
    parsed = {
        "raw":       data,
        "action_id": normalized_parts[0] if len(normalized_parts) > 0 else "",
        "seq_id":    normalized_parts[1] if len(normalized_parts) > 1 else "",
        "params":    normalized_parts[2:] if len(normalized_parts) > 2 else [],
        "ga_action_id": "",
        "actor_id": "",
        "action_params": [],
    }
    if len(parts) >= 4 and parts[1].strip():
        parsed["ga_action_id"] = parts[1].strip()
        parsed["actor_id"] = parts[2].strip()
        parsed["action_params"] = parts[3:]
    elif len(normalized_parts) >= 3 and normalized_parts[0].strip():
        parsed["ga_action_id"] = normalized_parts[0].strip()
        parsed["actor_id"] = normalized_parts[1].strip()
        parsed["action_params"] = normalized_parts[2:]
    return parsed


def _decode_cell_id_from_hash(token: str) -> int | None:
    token = str(token or "").strip()
    if len(token) != 2:
        return None
    try:
        left = _HASH_CHARS.index(token[0])
        right = _HASH_CHARS.index(token[1])
    except ValueError:
        return None
    return left * 64 + right


def _extract_move_destination_cell(path: str) -> int | None:
    path = str(path or "").strip()
    if len(path) < 2:
        return None
    return _decode_cell_id_from_hash(path[-2:])


def _parse_info_msg(data: str) -> dict:
    """Parsea Im{msgId};{args}  o  Im{msgId}"""
    # En Dofus 1.29 el delimitador real entre ID y argumentos es ';'
    if ";" in data:
        msg_id, args = data.split(";", 1)
    else:
        msg_id, args = data, ""
    return {"raw": data, "msg_id": msg_id, "args": args}


def _parse_spell_cooldown(data: str) -> dict | None:
    """Parsea SC{spellId};{cooldown}."""
    parts = data.split(";")
    if len(parts) < 2:
        return None
    try:
        spell_id = int(parts[0].strip())
        cooldown = int(parts[1].strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return {"spell_id": spell_id, "cooldown": cooldown, "raw": data}


def _parse_placement_cells(data: str) -> dict | None:
    """Parsea GP{team1_hashes}|{team2_hashes}|...

    Cada equipo viene como una secuencia compacta de hashes de 2 caracteres.
    En referencias locales de Dofus 1.29 el primer bloque corresponde al equipo del PJ.
    """
    body = str(data or "")
    segments = body.split("|")
    teams: list[list[int]] = []
    for segment in segments:
        token = segment.strip()
        if not token:
            teams.append([])
            continue
        cells: list[int] = []
        for idx in range(0, len(token) - 1, 2):
            cell_id = _decode_cell_id_from_hash(token[idx:idx + 2])
            if cell_id is not None:
                cells.append(cell_id)
        teams.append(cells)
    if not teams:
        return None
    return {
        "raw": body,
        "teams": teams,
        "my_team_cells": teams[0] if teams else [],
    }


def _parse_game_map_data(data: str) -> dict | None:
    """Parsea GDM|{mapId}|{key}|...

    En Retro este paquete no trae el MAPA_DATA completo; solo metadata del mapa.
    """
    parts = data.lstrip("|").split("|")
    if len(parts) < 2:
        return None
    try:
        map_id = int(parts[0].strip())
    except ValueError:
        return None
    return {
        "map_id": map_id,
        "map_name": "",
        "map_key": parts[1] if len(parts) > 1 else "",
        "map_data": None,
        "raw": data,
    }


def _parse_gtm(data: str) -> list[dict]:
    """Parsea GTM|actor;?;max_hp;ap;mp;cell_id;;current_hp|...
    Formato observado: actor_id;delta_or_seq;max_hp;ap;mp;cell_id;;current_hp
    - parts[1] = delta de HP o secuencia (0 si sin daño) — NO es el HP actual
    - parts[2] = max_hp
    - parts[3] = ap actuales
    - parts[4] = mp actuales
    - parts[5] = cell_id
    - parts[7] = hp actuales (si disponible), sino usar parts[2]
    """
    entries: list[dict] = []
    for raw_entry in data.lstrip("|").split("|"):
        parts = raw_entry.strip().split(";")
        if len(parts) < 2:
            continue
        actor_id = parts[0].strip()
        if not actor_id:
            continue
        def _try_int(s: str) -> int | None:
            try:
                return int(s.strip())
            except (ValueError, AttributeError):
                return None
        # HP actual: parts[7] si disponible y no vacío, sino parts[2] (max_hp = hp si sin daño)
        if len(parts) > 7 and parts[7].strip():
            hp = _try_int(parts[7])
        elif len(parts) > 2 and parts[2].strip():
            hp = _try_int(parts[2])
        else:
            hp = None
        entries.append({
            "actor_id": actor_id,
            "hp":      hp,
            "ap":      _try_int(parts[3]) if len(parts) > 3 else None,
            "mp":      _try_int(parts[4]) if len(parts) > 4 else None,
            "cell_id": _try_int(parts[5]) if len(parts) > 5 else None,
            "raw": raw_entry,
        })
    return entries


def _parse_game_players_coordinates(data: str) -> list[dict]:
    """Parsea GIC|actor;cell;dir|actor;cell;dir..."""
    entries: list[dict] = []
    for raw_entry in data.lstrip("|").split("|"):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = entry.split(";")
        if len(parts) < 2:
            continue
        actor_id = parts[0].strip()
        try:
            cell_id = int(parts[1].strip())
        except ValueError:
            continue
        direction = parts[2].strip() if len(parts) > 2 else ""
        entries.append({
            "actor_id": actor_id,
            "cell_id": cell_id,
            "direction": direction,
            "raw": raw_entry,
        })
    return entries


def _parse_int_list(raw: str) -> list[int]:
    values: list[int] = []
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(int(token))
        except ValueError:
            continue
    return values


def _parse_sprite_type(raw: str) -> tuple[int | None, str]:
    token = str(raw or "").strip()
    if not token:
        return None, ""
    try:
        return int(token), token
    except ValueError:
        pass
    head = token.split(",", 1)[0].strip()
    try:
        return int(head), token
    except ValueError:
        return None, token


def _classify_map_actor(actor_id: str, operation: str, sprite_type: int | None, extras: list[str]) -> str:
    """Clasificacion heuristica de actores de mapa fuera de combate.

    Inferencia conservadora:
    - sprite_type -3: grupo de monstruos.
    - sprite_type -1/-2: entidad de pelea visible en mapa (espada / join fight).
    - operation '-' o '~': actor removido/transicion.
    - resto: jugador, NPC u otro actor del mapa.
    """
    if operation in {"-", "~"}:
        return "removed"
    if sprite_type == -3:
        return "mob_group"
    if sprite_type in {-1, -2}:
        return "fight_marker"
    actor = str(actor_id).strip()
    if actor.lstrip("+-").isdigit():
        try:
            if int(actor) < 0:
                return "mob"
        except ValueError:
            pass
    extras_joined = ";".join(part.strip() for part in extras if part.strip())
    if "monster" in extras_joined.lower():
        return "mob"
    return "other"


def _parse_game_map_actors(data: str) -> list[dict]:
    """Parsea actores del mapa desde GM.

    Mantiene todos los campos utiles para inspeccion en GUI y heuristicas.
    Formatos observados por entrada:
    - Alta/actualizacion: {op}{cell};{dir};...;{actor_id};{extra...}
    - Remocion/transicion corta: -{actor_id} / ~{actor_id}
    """
    entries: list[dict] = []
    for raw_entry in data.split("|"):
        entry = raw_entry.strip()
        if len(entry) < 2 or entry[0] not in "+-~":
            continue
        operation = entry[0]
        parts = entry[1:].split(";")
        if operation in {"-", "~"} and len(parts) == 1:
            actor_id = parts[0].strip()
            if not actor_id:
                continue
            entries.append({
                "operation": operation,
                "cell_id": None,
                "direction": "",
                "actor_id": actor_id,
                "sprite_type": None,
                "template_ids": [],
                "levels": [],
                "mob_signature": "",
                "leader_template_id": None,
                "total_monsters": 0,
                "total_level": 0,
                "extra_fields": [],
                "entity_kind": "removed",
                "raw": raw_entry[:420],
            })
            continue
        if len(parts) < 4:
            continue
        try:
            cell_id = int(parts[0].strip())
        except ValueError:
            continue
        direction = parts[1].strip() if len(parts) > 1 else ""
        actor_id = parts[3].strip() if len(parts) > 3 else ""
        extras = [part.strip() for part in parts[4:]]
        if not actor_id:
            continue
        sprite_type: int | None = None
        sprite_raw = ""
        if len(parts) > 5:
            sprite_type, sprite_raw = _parse_sprite_type(parts[5])
        template_ids: list[int] = []
        levels: list[int] = []
        mob_signature = ""
        leader_template_id: int | None = None
        total_monsters = 0
        total_level = 0
        fight_owner_actor_id: str | None = None
        fight_owner_name = ""
        if sprite_type == -3 and len(parts) > 7:
            template_ids = _parse_int_list(parts[4])
            levels = _parse_int_list(parts[7])
            mob_signature = ",".join(str(template_id) for template_id in template_ids)
            leader_template_id = template_ids[0] if template_ids else None
            total_monsters = len(template_ids)
            total_level = sum(levels)
        elif sprite_type in {-1, -2}:
            if actor_id.lstrip("+-").isdigit():
                try:
                    if int(actor_id) > 0:
                        fight_owner_actor_id = actor_id
                except ValueError:
                    pass
            if len(parts) > 4:
                fight_owner_name = parts[4].strip()
        entries.append({
            "operation": operation,
            "cell_id": cell_id,
            "direction": direction,
            "actor_id": actor_id,
            "sprite_type": sprite_type,
            "sprite_raw": sprite_raw,
            "template_ids": template_ids,
            "levels": levels,
            "mob_signature": mob_signature,
            "leader_template_id": leader_template_id,
            "total_monsters": total_monsters,
            "total_level": total_level,
            "fight_owner_actor_id": fight_owner_actor_id,
            "fight_owner_name": fight_owner_name,
            "extra_fields": extras,
            "entity_kind": _classify_map_actor(actor_id, operation, sprite_type, extras),
            "raw": raw_entry[:420],
        })
    return entries


def _parse_game_movement_cells(data: str) -> list[dict]:
    """Extrae actor_id/cell_id desde un paquete GM."""
    return [
        {"actor_id": entry["actor_id"], "cell_id": entry["cell_id"], "raw": entry["raw"]}
        for entry in _parse_game_map_actors(data)
    ]


# ─────────────────────────────────────────── sniffer ──
class DofusSniffer:
    """
    Sniffa el tráfico Dofus Retro y emite eventos al queue proporcionado.

    Uso:
        event_queue = queue.Queue()
        sniffer = DofusSniffer(event_queue, debug_mode=False)
        sniffer.start()
        # en otro hilo:
        while True:
            event, data = event_queue.get()
            ...
        sniffer.stop()
    """

    def __init__(self, event_queue: queue.Queue, debug_mode: bool = False):
        self.event_queue = event_queue
        self.debug_mode  = debug_mode
        self._running    = False
        self._thread: threading.Thread | None = None

        # Buffers por stream TCP (src_ip:src_port -> buffer)
        self._buffers: dict[str, str] = {}

        # ID del personaje propio (se aprende automáticamente o se configura)
        self.my_actor_id: str | None = None
        self._pending_turn_confirm = False  # True mientras esperamos confirmar nuestro ID

    # ──────────────────────────── public api ──
    def start(self):
        """Inicia el sniffer en un hilo daemon."""
        try:
            from scapy.all import sniff, conf
            conf.verb = 0  # silenciar scapy
        except ImportError:
            print("[SNIFFER] scapy no instalado — instala con: pip install scapy")
            print("[SNIFFER] También necesitas Npcap: https://npcap.com/#download")
            return

        # Auto-detectar IP del servidor Dofus Retro buscando el proceso
        self.server_ips = self._detect_dofus_server_ips()
        self.server_ip = self.server_ips[0] if self.server_ips else None
        if self.server_ips:
            print(f"[SNIFFER] Servidores Dofus detectados: {', '.join(self.server_ips)}")
        else:
            print(f"[SNIFFER] Servidor no detectado — capturando todo el puerto {DOFUS_PORT}")

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="DofusSniffer")
        self._thread.start()
        print(f"[SNIFFER] Iniciado — escuchando puerto {DOFUS_PORT}")

    def _detect_dofus_server_ips(self) -> list[str]:
        """Busca las IPs del servidor Dofus Retro en las conexiones TCP activas."""
        import subprocess
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, encoding="cp850", timeout=5
            )
            # Buscar el PID de Dofus Retro.exe
            tasklist = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="cp850", timeout=5
            )
            dofus_pids = []
            for line in tasklist.stdout.splitlines():
                if "dofus" in line.lower():
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        pid = parts[1].strip().strip('"')
                        if pid.isdigit():
                            dofus_pids.append(pid)
            if not dofus_pids:
                return []

            # Buscar la conexión externa entre TODOS los pids de Dofus
            remote_ips: list[str] = []
            for line in result.stdout.splitlines():
                if "ESTABLISHED" not in line or f":{DOFUS_PORT} " not in line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                pid_in_line = parts[-1].strip()
                if pid_in_line not in dofus_pids:
                    continue
                remote = parts[2]
                ip = remote.rsplit(":", 1)[0]
                if ip.startswith("127.") or ip.startswith("::"):
                    continue
                if ip not in remote_ips:
                    remote_ips.append(ip)
            return remote_ips
        except Exception as e:
            print(f"[SNIFFER] Error detectando servidor: {e}")
        return []

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def set_my_actor_id(self, actor_id: str):
        """Configura manualmente el ID del personaje (ver debug logs para encontrarlo)."""
        self.my_actor_id = actor_id
        print(f"[SNIFFER] Actor ID configurado: {actor_id}")

    @property
    def active(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ──────────────────────────── internal ──
    def _run(self):
        from scapy.all import sniff, TCP, Raw
        try:
            # Filtrar por puerto y por todas las IPs detectadas del servidor.
            # Si no hay IPs configuradas, capturamos todo el puerto 443.
            server_ips = list(getattr(self, "server_ips", []) or [])
            bpf = f"tcp port {DOFUS_PORT}"
            if server_ips:
                host_filters = " or ".join(f"host {ip}" for ip in server_ips)
                bpf = f"tcp port {DOFUS_PORT} and ({host_filters})"
            print(f"[SNIFFER] Filtro BPF: {bpf}")
            sniff(
                filter=bpf,
                prn=self._on_packet,
                stop_filter=lambda _: not self._running,
                store=False,
            )
        except Exception as e:
            print(f"[SNIFFER] Error en captura: {e}")
            print("[SNIFFER] Asegúrate de ejecutar como Administrador y tener Npcap instalado.")
        finally:
            self._running = False

    def _strip_telemetry(self, raw: bytes) -> bytes:
        """Elimina bloques de telemetría entre marcadores 0xC3 0xB9 (v1.39.5+)."""
        marker = TELEMETRY_MARKER
        result = bytearray()
        i = 0
        while i < len(raw):
            if raw[i:i+2] == marker:
                # Buscar el marcador de cierre
                end = raw.find(marker, i + 2)
                if end == -1:
                    break   # telemetría incompleta — descartar el resto
                i = end + 2  # saltar el bloque completo
            else:
                result.append(raw[i])
                i += 1
        return bytes(result)

    def _on_packet(self, pkt):
        try:
            from scapy.all import TCP, Raw
            if not (pkt.haslayer(TCP) and pkt.haslayer(Raw)):
                return

            tcp = pkt[TCP]
            raw_bytes = pkt[Raw].load

            # Determinar dirección
            is_server_to_client = (tcp.sport == DOFUS_PORT)

            # Strip de telemetría en paquetes C→S
            if not is_server_to_client:
                raw_bytes = self._strip_telemetry(raw_bytes)

            # Clave única por stream (origen del segmento)
            key = f"{pkt.src}:{tcp.sport}"

            try:
                text = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                return

            # Acumular en buffer y procesar paquetes completos (delimitador: \x00 o \n)
            self._buffers[key] = self._buffers.get(key, "") + text
            while True:
                buf = self._buffers[key]
                # Buscar el delimitador más próximo
                pos_null = buf.find("\x00")
                pos_nl   = buf.find("\n")
                candidates = [p for p in (pos_null, pos_nl) if p >= 0]
                if not candidates:
                    break
                pos = min(candidates)
                packet_str = buf[:pos].strip()
                self._buffers[key] = buf[pos + 1:]
                if packet_str:
                    self._parse_and_emit(packet_str, is_server_to_client)

        except Exception as e:
            if self.debug_mode:
                print(f"[SNIFFER] Error procesando paquete: {e}")

    def _parse_and_emit(self, packet: str, from_server: bool):
        direction = "S→C" if from_server else "C→S"

        if self.debug_mode:
            self.event_queue.put(("raw_packet", {"direction": direction, "data": packet}))

        if from_server:
            self._parse_server_packet(packet)
        else:
            self._parse_client_packet(packet)

    def _parse_server_packet(self, packet: str):
        q = self.event_queue

        # ── GTF — fin de turno (Game Turn Finish) ─────────────────────────
        # Formato real observado: GTF{actorId}.{stats}
        if packet.startswith("GTF"):
            body = packet[3:]
            actor_id = body.split(".")[0].split("|")[0].strip()
            q.put(("turn_end", {"actor_id": actor_id}))
            if actor_id == self.my_actor_id:
                print(f"[SNIFFER] Nuestro turno terminó (actor {actor_id})")
            return

        # ── GTS — inicio de turno ──────────────────────────────────────────
        if packet.startswith("GTS"):
            actor_id = packet[3:].split(".")[0].split("|")[0].strip()
            q.put(("turn_start", {"actor_id": actor_id}))
            print(f"[SNIFFER] Turno de actor {actor_id}")
            return

        # ── GE — combate terminado ─────────────────────────────────────────
        if packet.startswith("GE") and not packet.startswith("GEA"):
            q.put(("fight_end", {"raw": packet[2:]}))
            print(f"[SNIFFER] Combate terminado: {packet[2:30]}")
            return

        # ── GP — celdas de colocación/preparación ──────────────────────────
        if packet.startswith("GP"):
            parsed = _parse_placement_cells(packet[2:])
            if parsed:
                q.put(("placement_cells", parsed))
                q.put(("placement", {"raw": packet[2:]}))
                print("[SNIFFER] Celdas de colocacion detectadas")
            else:
                q.put(("placement", {"raw": packet[2:]}))
                print("[SNIFFER] Fase de colocacion detectada")
            return

        # ── Gp — compatibilidad/fallback de fase de colocación ─────────────
        if packet.startswith("Gp"):
            q.put(("placement", {"raw": packet[2:]}))
            print("[SNIFFER] Fase de colocacion detectada")
            return

        # ── GJK — entró a combate ──────────────────────────────────────────
        # Formato: GJK{fightId}|{actorId}|{teamId}|{cellId}|{dir}|{alive}
        if packet.startswith("GJK"):
            parts = packet[3:].split("|")
            actor_id = parts[1].strip() if len(parts) > 1 else ""
            team_id  = parts[2].strip() if len(parts) > 2 else ""
            cell_id: int | None = None
            if len(parts) > 3:
                try:
                    cell_id = int(parts[3].strip())
                except ValueError:
                    pass
            if actor_id and self.my_actor_id is None:
                self._candidate_actor_id = actor_id
            q.put(("fight_join", {"raw": packet[3:], "actor_id": actor_id, "team_id": team_id, "cell_id": cell_id}))
            print(f"[DIAG] gjk actor={actor_id!r} team={team_id!r} cell={cell_id} raw={packet[:140]!r}")
            return

        # ── S{actor_id} — servidor listo para siguiente acción del actor ─────
        # Formato: S{actor_id}  ej: S22240
        if len(packet) > 1 and packet[0] == "S" and packet[1:].strip().lstrip("-").isdigit():
            actor_id = packet[1:].strip()
            q.put(("action_sequence_ready", {"actor_id": actor_id}))
            return

        # ── GA — acción de juego ───────────────────────────────────────────
        if packet.startswith("GA"):
            data = packet[2:]
            parsed = _parse_game_action(data)
            q.put(("game_action", parsed))

            move_actor = str(parsed.get("actor_id", "")).strip()
            move_action_id = str(parsed.get("ga_action_id", "")).strip()
            move_params = parsed.get("action_params") or []
            if move_action_id == "1" and move_actor and move_params:
                destination_cell = _extract_move_destination_cell(move_params[0])
                if destination_cell is not None:
                    q.put((
                        "combatant_cell",
                        {
                            "actor_id": move_actor,
                            "cell_id": destination_cell,
                            "raw": parsed["raw"],
                            "source": "ga_move",
                        },
                    ))
            # Atrapar desplazamientos: slide (4), push (5), pull (6)
            elif move_action_id in {"4", "5", "6"} and move_params:
                parts = str(move_params[0]).split(",")
                if len(parts) >= 2:
                    target_id = parts[0].strip()
                    try:
                        dest_cell = int(parts[1].strip())
                        q.put((
                            "combatant_cell",
                            {
                                "actor_id": target_id,
                                "cell_id": dest_cell,
                                "raw": parsed["raw"],
                                "source": f"ga_{move_action_id}",
                            },
                        ))
                    except ValueError:
                        pass

            # Action 129 = stats update (PA/PM restantes)
            pa_action_id = str(parsed.get("ga_action_id") or parsed.get("action_id") or "").strip()
            pa_params = parsed.get("action_params") or parsed.get("params") or []
            pa_actor = str(parsed.get("actor_id") or "").strip()
            if pa_action_id == "129" and len(pa_params) >= 2:
                try:
                    pa = int(pa_params[0])
                    pm = int(pa_params[1])
                    q.put(("pa_update", {
                        "actor_id": pa_actor,
                        "pa": pa, "pm": pm
                    }))
                    print(f"[SNIFFER] PA={pa} PM={pm} (actor {pa_actor})")
                except ValueError:
                    pass
            return

        # ── Im — mensaje del sistema ───────────────────────────────────────
        if packet.startswith("GM"):
            map_entries = _parse_game_map_actors(packet[2:])
            if map_entries:
                q.put(("map_actor_batch", {"entries": map_entries, "raw": packet[2:]}))
            for entry in map_entries:
                q.put(("map_actor", entry))
            entries = _parse_game_movement_cells(packet[2:])
            for entry in entries:
                q.put(("combatant_cell", entry))
            if map_entries:
                mob_count = sum(1 for entry in map_entries if entry.get("entity_kind") == "mob")
                print(f"[SNIFFER] GM mapa: actores={len(map_entries)} mobs~={mob_count}")
            return

        if packet.startswith("GDM"):
            parsed = _parse_game_map_data(packet[3:])
            if parsed:
                q.put(("map_data", parsed))
                print(f"[DIAG] gdm id={parsed['map_id']} name={parsed['map_name']!r}")
            else:
                print(f"[DIAG] gdm raw_unparsed={packet[:200]!r}")
            return

        if packet.startswith("GDK"):
            q.put(("map_loaded", {"raw": packet[3:]}))
            print(f"[DIAG] gdk raw={packet[:200]!r}")
            return

        if packet.startswith("GIC"):
            entries = _parse_game_players_coordinates(packet[3:])
            q.put(("arena_state", {"entries": entries, "raw": packet[3:]}))
            print(f"[DIAG] gic raw={packet[:200]!r}")
            return

        # ── GTM — stats de luchadores tras cada acción (HP/PA/PM) ─────────────
        if packet.startswith("GTM"):
            entries = _parse_gtm(packet[3:])
            if entries:
                q.put(("fighter_stats", {"entries": entries}))
                print(f"[DIAG] gtm {len(entries)} luchadores raw={packet[:140]!r}")
            return

        if packet.startswith("As"):
            q.put(("actor_snapshot", {"raw": packet[2:]}))
            return

        if packet.startswith("SC"):
            parsed = _parse_spell_cooldown(packet[2:])
            if parsed:
                q.put(("spell_cooldown", parsed))
            return

        if packet.startswith("Im"):
            parsed = _parse_info_msg(packet[2:])
            q.put(("info_msg", parsed))
            return

        if packet.startswith("PM"):
            raw = packet[2:]
            actor_id = ""
            actor_name = ""
            if raw.startswith("~"):
                fields = raw[1:].split(";")
                actor_id = fields[0].strip() if len(fields) > 0 else ""
                actor_name = fields[1].strip() if len(fields) > 1 else ""
            if actor_id:
                q.put(("player_profile", {"actor_id": actor_id, "name": actor_name, "raw": raw}))
            return

        if packet.startswith("Go"):
            q.put(("game_object", {"raw": packet[2:], "packet": packet}))
            return

        if packet.startswith("Ow"):
            parts = packet[2:].split("|")
            if len(parts) >= 2:
                try:
                    current_pods = int(parts[0])
                    max_pods = int(parts[1])
                    q.put(("pods_update", {"current": current_pods, "max": max_pods}))
                except ValueError:
                    pass
            return

        # ── WC/Wv/Wc — Menú de Zaap / Zaapi ────────────────────────────────
        if packet.startswith("WC") or packet.startswith("Wc") or packet.startswith("Wv"):
            q.put(("zaap_list", {"raw": packet}))
            return

        # ── cMK — mensaje de chat ──────────────────────────────────────────
        # No emitir al queue (ruido), solo en debug
        if packet.startswith("cMK"):
            return

        # ── qpong — keepalive ──────────────────────────────────────────────
        if packet.startswith("qpong"):
            return

    def _parse_client_packet(self, packet: str):
        q = self.event_queue

        # ── GKK — el jugador pasa turno ────────────────────────────────────
        if packet == "GKK":
            q.put(("player_end_turn", {}))
            return

        # ── GPA — el jugador clickó "Listo" en colocación ─────────────────
        if packet.startswith("GPA"):
            q.put(("player_ready", {}))
            return

        # ── Gp — mover a celda de colocación ───────────────────────────────
        if packet.startswith("Gp"):
            try:
                q.put(("player_placement_move", {"cell_id": int(packet[2:]), "raw": packet[2:]}))
            except (TypeError, ValueError):
                pass
            return

        # ── GR1 — botón listo en colocación ────────────────────────────────
        if packet.startswith("GR"):
            q.put(("player_ready", {"raw": packet[2:]}))
            return

        # ── GA — el jugador usó un hechizo ────────────────────────────────
        if packet.startswith("GA"):
            parsed = _parse_game_action(packet[2:])
            q.put(("player_action", parsed))
            return


# ────────────────────────────────────── utilidad standalone ──
if __name__ == "__main__":
    """Modo debug: muestra todos los paquetes Dofus en tiempo real."""
    import sys
    import io
    # Forzar stdout UTF-8 para evitar errores de encoding en Windows
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    eq: queue.Queue = queue.Queue()
    sniffer = DofusSniffer(eq, debug_mode=True)
    sniffer.start()

    print("Capturando paquetes Dofus Retro... (Ctrl+C para salir)")
    try:
        while True:
            event, data = eq.get()
            if event == "raw_packet":
                direction = data["direction"]
                raw = data["data"][:200]
                print(f"  [{direction}] {raw}")
            else:
                print(f"  EVENT: {event:20s} | {data}")
    except KeyboardInterrupt:
        sniffer.stop()
        print("Detenido.")
