from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET


HASH_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
HASH_LOOKUP = {char: idx for idx, char in enumerate(HASH_CHARS)}


TYPE_LABELS = {
    0: "no_walkable",
    1: "interactive_object",
    2: "teleport_cell",
    3: "unknown_1",
    4: "walkable_cell",
    5: "unknown_2",
    6: "path",
}

TELEPORT_TEXTURES = {1030, 1029, 1764, 2298, 745}


def hash_value(char: str) -> int:
    if char not in HASH_LOOKUP:
        raise ValueError(f"Caracter hash invalido: {char!r}")
    return HASH_LOOKUP[char]


@dataclass(slots=True)
class MapCell:
    cell_id: int
    x: int
    y: int
    raw_cell_type: int
    effective_cell_type: int
    line_of_sight: bool
    ground_level: int
    ground_slope: int
    interactive_object_id: int
    layer_object_1_num: int
    layer_object_2_num: int

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.effective_cell_type, f"unknown_{self.effective_cell_type}")

    @property
    def raw_type_label(self) -> str:
        return TYPE_LABELS.get(self.raw_cell_type, f"unknown_{self.raw_cell_type}")

    @property
    def cell_type(self) -> int:
        return self.effective_cell_type

    @property
    def is_walkable(self) -> bool:
        return self.effective_cell_type not in {0, 1}

    @property
    def is_interactive_cell(self) -> bool:
        return self.effective_cell_type == 1 or self.interactive_object_id != -1

    @property
    def has_teleport_texture(self) -> bool:
        return self.layer_object_1_num in TELEPORT_TEXTURES or self.layer_object_2_num in TELEPORT_TEXTURES


def cell_id_to_xy(cell_id: int, map_width: int = 15) -> tuple[int, int]:
    row_block = cell_id // ((map_width * 2) - 1)
    row_offset = cell_id - (row_block * ((map_width * 2) - 1))
    y = row_block - (row_offset % map_width)
    x = (cell_id - ((map_width - 1) * y)) // map_width
    return int(x), int(y)


def decode_map_cell(cell_data: str, cell_id: int, map_width: int = 15) -> MapCell:
    info = [hash_value(char) for char in cell_data]
    raw_cell_type = int((info[2] & 56) >> 3)
    line_of_sight = (info[0] & 1) == 1
    has_interactive = ((info[7] & 2) >> 1) != 0
    layer_object_2_num = int(((info[0] & 2) << 12) + ((info[7] & 1) << 12) + (info[8] << 6) + info[9])
    layer_object_1_num = int(((info[0] & 4) << 11) + ((info[4] & 1) << 12) + (info[5] << 6) + info[6])
    ground_level = int(info[1] & 15)
    ground_slope = int((info[4] & 60) >> 2)
    x, y = cell_id_to_xy(cell_id, map_width)
    if layer_object_1_num in TELEPORT_TEXTURES or layer_object_2_num in TELEPORT_TEXTURES:
        effective_cell_type = 2
    elif raw_cell_type == 2:
        effective_cell_type = 4
    else:
        effective_cell_type = raw_cell_type
    return MapCell(
        cell_id=int(cell_id),
        x=x,
        y=y,
        raw_cell_type=raw_cell_type,
        effective_cell_type=effective_cell_type,
        line_of_sight=bool(line_of_sight),
        ground_level=ground_level,
        ground_slope=ground_slope,
        interactive_object_id=layer_object_2_num if has_interactive else -1,
        layer_object_1_num=layer_object_1_num,
        layer_object_2_num=layer_object_2_num,
    )


def decode_map_data(map_data: str, map_width: int = 15) -> list[MapCell]:
    if not map_data:
        return []
    if len(map_data) % 10 != 0:
        raise ValueError("MAPA_DATA invalido: longitud no divisible por 10")
    cells: list[MapCell] = []
    for offset in range(0, len(map_data), 10):
        chunk = map_data[offset : offset + 10]
        cells.append(decode_map_cell(chunk, offset // 10, map_width=map_width))
    return cells


def load_map_data_from_xml(map_id: int, xml_dir: str | Path) -> tuple[str, int, int] | None:
    try:
        target = Path(xml_dir) / f"{int(map_id)}.xml"
    except (TypeError, ValueError):
        return None
    if not target.exists():
        return None
    root = ET.parse(target).getroot()
    map_data = (root.findtext("MAPA_DATA") or "").strip()
    if not map_data:
        return None
    try:
        width = int((root.findtext("ANCHURA") or "15").strip() or 15)
    except (TypeError, ValueError, AttributeError):
        width = 15
    try:
        height = int((root.findtext("ALTURA") or "17").strip() or 17)
    except (TypeError, ValueError, AttributeError):
        height = 17
    return map_data, width, height


def cell_distance(cell_a: MapCell, cell_b: MapCell) -> int:
    return abs(int(cell_a.x) - int(cell_b.x)) + abs(int(cell_a.y) - int(cell_b.y))


def cell_same_line(cell_a: MapCell, cell_b: MapCell) -> bool:
    return int(cell_a.x) == int(cell_b.x) or int(cell_a.y) == int(cell_b.y)
