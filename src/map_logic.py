from __future__ import annotations


def cell_id_to_grid(cell_id: int, map_width: int = 15) -> tuple[int, int]:
    """Convierte cell_id a coordenadas lógicas x,y usando la lógica de Boffus/Celda.cs."""
    row_block = cell_id // ((map_width * 2) - 1)
    row_offset = cell_id - (row_block * ((map_width * 2) - 1))
    y = row_block - (row_offset % map_width)
    x = (cell_id - ((map_width - 1) * y)) // map_width
    return int(x), int(y)


def cell_id_to_col_row(cell_id: int, map_width: int = 15) -> tuple[int, int]:
    """Compatibilidad con la proyección lineal actual."""
    return int(cell_id % map_width), int(cell_id // map_width)
