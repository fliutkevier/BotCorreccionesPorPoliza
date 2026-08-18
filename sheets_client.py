"""Lectura/escritura de casos en Google Sheets vía service account."""
from __future__ import annotations

import logging

import gspread
from google.oauth2.service_account import Credentials

import config
from models import Caso, Estado

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsClient:
    def __init__(self) -> None:
        creds = Credentials.from_service_account_file(
            str(config.GOOGLE_CREDENTIALS), scopes=_SCOPES
        )
        gc = gspread.authorize(creds)
        self._ws = gc.open_by_key(config.SHEET_ID).worksheet(config.WORKSHEET)
        # Mapeo nombre-de-columna -> índice 1-based, leído del header (fila 1).
        header = self._ws.row_values(1)
        self._col = {nombre: i + 1 for i, nombre in enumerate(header)}
        faltantes = [
            c for c in (config.COL_TURNO, config.COL_RUTA, config.COL_POLIZA,
                        config.COL_COLECTOR, config.COL_ESTADO)
            if c not in self._col
        ]
        if faltantes:
            raise ValueError(f"Faltan columnas en el sheet: {faltantes}")

    def leer_casos(self) -> list[Caso]:
        """Devuelve todas las filas como Caso (incluye su índice de fila real)."""
        registros = self._ws.get_all_records()  # lista de dicts, fila 1 = header
        casos: list[Caso] = []
        nombre_loc = getattr(config, "COL_LOCALIDAD", "LOCALIDAD")
        for offset, row in enumerate(registros):
            casos.append(Caso(
                fila=offset + 2,  # +2: header + base 0
                turno=str(row.get(config.COL_TURNO, "")).strip(),
                ruta=str(row.get(config.COL_RUTA, "")).strip(),
                poliza=str(row.get(config.COL_POLIZA, "")).strip(),
                colector=str(row.get(config.COL_COLECTOR, "")).strip(),
                estado=str(row.get(config.COL_ESTADO, "")).strip(),
                localidad=str(row.get(nombre_loc, "")).strip(),  # vacío si no existe la columna
            ))
        return casos

    def actualizar_estado(self, caso: Caso, estado: str) -> None:
        """Escribe el estado de un caso de inmediato (para ver el avance en vivo)."""
        caso.estado = estado
        self._ws.update_cell(caso.fila, self._col[config.COL_ESTADO], estado)
        log.info("Fila %s (póliza %s) -> %s", caso.fila, caso.poliza, estado)

    def actualizar_anterior_colector(self, caso: Caso, colector: str) -> None:
        """Escribe en 'ANTERIOR COLECTOR' quién tenía la ruta. Si la columna no
        existe en el sheet, avisa y no rompe (queda como opcional)."""
        nombre_col = getattr(config, "COL_ANTERIOR_COLECTOR", "ANTERIOR COLECTOR")
        col = self._col.get(nombre_col)
        if col is None:
            log.warning("No existe la columna %r en el sheet; se omite el anterior colector",
                        nombre_col)
            return
        self._ws.update_cell(caso.fila, col, colector)
        log.info("Fila %s: anterior colector = %r", caso.fila, colector)
