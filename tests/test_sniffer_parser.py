"""
Tests de caracterización del parser del sniffer.

Protegen el comportamiento actual antes de refactorizar el dispatcher
de eventos. Si estos tests rompen después de un cambio, evaluar si es
intencional.

Los payloads están tomados de trazas reales del bot (ver `WORKLOG.md`
del 2026-04-22).
"""
import sniffer as sn


# ────────────────────────────────────────── helpers básicos ──

def test_decode_cell_id_from_hash_basic():
    # _HASH_CHARS = "abcdefghijk...z ABCD...Z 012...9 -_" (64 chars total)
    # b=1, p=15 → 1*64 + 15 = 79
    assert sn._decode_cell_id_from_hash("bp") == 79
    # c=2, h=7 → 2*64 + 7 = 135
    assert sn._decode_cell_id_from_hash("ch") == 135
    # f=5, z=25 → 5*64 + 25 = 345
    assert sn._decode_cell_id_from_hash("fz") == 345


def test_decode_cell_id_returns_none_on_invalid():
    assert sn._decode_cell_id_from_hash("") is None
    assert sn._decode_cell_id_from_hash("x") is None
    assert sn._decode_cell_id_from_hash("##") is None
    assert sn._decode_cell_id_from_hash("a!") is None


def test_parse_packets_splits_null_and_newline():
    raw = "GIC|1;2;3\x00GE|fight=5\nGTF123|"
    packets = sn._parse_packets(raw)
    assert packets == ["GIC|1;2;3", "GE|fight=5", "GTF123|"]


def test_parse_packets_ignores_empty():
    raw = "\x00\x00\nGIC|a\n\x00"
    packets = sn._parse_packets(raw)
    assert packets == ["GIC|a"]


# ────────────────────────────────────────── placement ──

def test_parse_placement_cells_two_teams():
    # Extracto real del log de hoy (map 2966, pelea vs Blops).
    # CARACTERIZACIÓN: el parser actual SIEMPRE toma teams[0] como equipo del PJ,
    # sin importar el contenido. En el log del 2026-04-22, las dos peleas
    # del map 2966 tuvieron raws con orden de equipos invertido — el sniffer
    # imprimió diferentes celdas como "del equipo" en cada una. Si refactorizamos
    # para detectar el lado real del PJ, este test debe actualizarse.
    raw = "bpbRcjcJcNc_dbdF|chcLdddHd_eDe7fz|0"
    result = sn._parse_placement_cells(raw)
    assert result is not None
    assert len(result["teams"]) == 3  # incluye el "0" final vacío
    team1 = result["teams"][0]
    # bp=79, bR=107, cj=137, cJ=163, cN=167, c_=191, db=193, dF=223
    assert team1 == [79, 107, 137, 163, 167, 191, 193, 223]
    team2 = result["teams"][1]
    # ch=135, cL=165, dd=195, dH=225, d_=255, eD=285, e7=315, fz=345
    assert team2 == [135, 165, 195, 225, 255, 285, 315, 345]
    # Bug latente conocido: my_team siempre es teams[0] aunque el PJ
    # esté en teams[1]. Documentado en project_visual_grid_vs_deformation.md.
    assert result["my_team_cells"] == team1


def test_parse_placement_cells_empty_data_returns_empty_lists():
    result = sn._parse_placement_cells("|")
    assert result is not None
    assert result["teams"] == [[], []]


# ────────────────────────────────────────── info msg ──

def test_parse_info_msg_with_args():
    # Del log: Im01;37 (regenerás 37 HP con Duna Yar)
    result = sn._parse_info_msg("01;37")
    assert result["msg_id"] == "01"
    assert result["args"] == "37"
    assert result["raw"] == "01;37"


def test_parse_info_msg_no_args():
    # Del log: Im095 (celdas de colocación listas)
    result = sn._parse_info_msg("095")
    assert result["msg_id"] == "095"
    assert result["args"] == ""


def test_parse_info_msg_multiple_semicolons():
    # Caso edge: múltiples ; en args
    result = sn._parse_info_msg("01;37;extra")
    assert result["msg_id"] == "01"
    assert result["args"] == "37;extra"


# ────────────────────────────────────────── game action ──

def test_parse_game_action_with_actor():
    # Del log real: ;950;22240;22240,3,0
    result = sn._parse_game_action(";950;22240;22240,3,0")
    assert result["ga_action_id"] == "950"
    assert result["actor_id"] == "22240"
    assert result["action_params"] == ["22240,3,0"]


def test_parse_game_action_short():
    result = sn._parse_game_action(";300;-1")
    assert result["action_id"] == "300"
    assert result["seq_id"] == "-1"


# ────────────────────────────────────────── As (character stats) ──

def test_parse_as_hp_parses():
    # Extracto real del log del Duna Yar (HP full tras regen)
    raw = "449351137,444564000,458551000|730111|25|8|0~0,0,0,0,0,0|1304,1304|1100,10000|1022|166"
    result = sn._parse_as(raw)
    assert result is not None
    assert result["hp"] == 1304
    assert result["max_hp"] == 1304
    assert result["kamas"] == 730111


def test_parse_as_hp_mid_regen():
    # Del log anterior al Duna Yar (HP=1264/1304)
    raw = "449144090,444564000,458551000|730088|25|8|0~0,0,0,0,0,0|1264,1304|1100,10000"
    result = sn._parse_as(raw)
    assert result["hp"] == 1264
    assert result["max_hp"] == 1304


def test_parse_as_malformed_returns_none():
    assert sn._parse_as("") is None
    assert sn._parse_as("xyz") is None
    assert sn._parse_as("1|2|3") is None  # muy corto


# ────────────────────────────────────────── GTM ──

def test_parse_gtm_single_fighter():
    # Fighter con HP=483, PA=0, PM=0, cell=239
    # Formato: actor;delta;max_hp;ap;mp;cell;;current_hp
    raw = "-1;0;495;0;0;239;;483"
    entries = sn._parse_gtm(raw)
    assert len(entries) == 1
    e = entries[0]
    assert e["actor_id"] == "-1"
    assert e["cell_id"] == 239
    # hp actual viene de parts[7] cuando está disponible
    assert e.get("hp") == 483 or e.get("current_hp") == 483


def test_parse_gtm_empty_returns_empty():
    assert sn._parse_gtm("") == []
    assert sn._parse_gtm("|") == []


# ────────────────────────────────────────── spell cooldown ──

def test_parse_spell_cooldown_valid():
    result = sn._parse_spell_cooldown("181;3")
    assert result == {"spell_id": 181, "cooldown": 3, "raw": "181;3"}


def test_parse_spell_cooldown_invalid_returns_none():
    assert sn._parse_spell_cooldown("181") is None
    assert sn._parse_spell_cooldown("abc;def") is None
