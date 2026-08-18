"""Orquestación en 3 etapas GLOBALES (lote optimizado).

A diferencia del flujo grupo-por-grupo, se procesa:
  FASE A: liberar el suministro de cada póliza (por grupo de ruta).
  FASE B: por turno, liberar TODAS las rutas avanzadas en una sola pasada.
  FASE C: por (turno, colector), asignar TODAS sus rutas en una sola pasada.

Con N rutas distintas, se entra a las páginas de B y C una vez por turno/colector,
no N veces.

Regla de estado final por caso:
  - 'listo para corrección' si su suministro se liberó (A) y su ruta quedó asignada (C).
  - error si se liberó en A pero su ruta NO apareció en C, o falla técnica.
  - 'saltado'/error de A no entran a B/C.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from models import Caso, Estado, GrupoRuta, ResultadoLiberacion, agrupar_por_ruta, clave_rl, ruta4
from portal import PortalInterface, ResultadoAsignacion
from sheets_client import SheetsClient

log = logging.getLogger(__name__)


def _marcar(casos: list[Caso], estado: str, sheets: SheetsClient) -> None:
    for c in casos:
        sheets.actualizar_estado(c, estado)


def procesar(casos: list[Caso], portal: PortalInterface, sheets: SheetsClient) -> None:
    grupos = agrupar_por_ruta(casos)

    # Validaciones de datos (conflicto de colector / falta colector).
    validos: list[GrupoRuta] = []
    for g in grupos:
        if g.conflicto_colector:
            _marcar(g.casos, Estado.error("conflicto de colector en la ruta"), sheets)
        elif not g.colector:
            _marcar(g.casos, Estado.error("falta colector"), sheets)
        else:
            validos.append(g)

    # ---- FASE A: liberar suministros, reusando la página por turno ----------
    # Mapear cada caso a su grupo de ruta (para poblar g.liberados) y agrupar
    # los casos por turno (FASE A navega una sola vez por turno).
    caso_a_grupo: dict[int, GrupoRuta] = {}
    casos_por_turno: dict[str, list[Caso]] = defaultdict(list)
    for g in validos:
        for caso in g.casos:
            caso_a_grupo[id(caso)] = g
            casos_por_turno[caso.turno].append(caso)

    for turno, casos_turno in casos_por_turno.items():
        for caso in casos_turno:
            sheets.actualizar_estado(caso, Estado.PREPARANDO)
        resultados = portal.liberar_suministros_turno(turno, casos_turno)
        for caso, res, anterior in resultados:
            if anterior:
                sheets.actualizar_anterior_colector(caso, anterior)
            g = caso_a_grupo[id(caso)]
            if res is ResultadoLiberacion.OK:
                g.liberados.append(caso)
            elif res is ResultadoLiberacion.SKIP_FECHA0:
                sheets.actualizar_estado(caso, Estado.SALTADO_FECHA0)
            else:
                sheets.actualizar_estado(caso, Estado.error("falla al liberar suministro"))

    # Solo avanzan los grupos cuya ruta tuvo >=1 póliza liberada.
    avance = [g for g in validos if g.liberados]
    if not avance:
        log.info("Ninguna ruta avanzó a FASE B/C.")
        return

    # ---- FASE B: por turno, liberar todas las rutas juntas ------------------
    turnos_b_fallidos: set[str] = set()
    pares_por_turno: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for g in avance:
        pares_por_turno[g.turno].append((g.ruta, g.localidad))
    for turno, pares in pares_por_turno.items():
        if portal.liberar_rutas(turno, pares) is None:  # falla técnica de la página
            turnos_b_fallidos.add(turno)
            for g in avance:
                if g.turno == turno:
                    _marcar(g.liberados, Estado.error("falla técnica al liberar ruta"), sheets)

    # ---- FASE C: se entra UNA vez; por turno se selecciona el desplegable y,
    # entre colectores, NO se recarga (la grilla de libres se actualiza sola). ---
    grupos_c = [g for g in avance if g.turno not in turnos_b_fallidos]
    if not grupos_c:
        return

    # Agrupar por turno y, dentro, por colector (preservando orden).
    por_turno: dict[str, dict[str, list[GrupoRuta]]] = defaultdict(lambda: defaultdict(list))
    for g in grupos_c:
        por_turno[g.turno][g.colector].append(g)

    portal.ir_a_asignacion()
    for turno, por_colector in por_turno.items():
        portal.seleccionar_turno_c(turno)
        for colector, grupos_col in por_colector.items():
            pares = [(g.ruta, g.localidad) for g in grupos_col]
            tildadas = portal.tildar_rutas_c(pares)  # claves (ruta4, localidad)

            presentes = [g for g in grupos_col if clave_rl(g.ruta, g.localidad, g.turno) in tildadas]
            ausentes = [g for g in grupos_col if clave_rl(g.ruta, g.localidad, g.turno) not in tildadas]
            for g in ausentes:
                _marcar(g.liberados, Estado.error("la ruta no apareció para asignar (FASE C)"), sheets)
            if not presentes:
                continue

            res, no_asignadas = portal.asignar_colector_c(
                colector, [(g.ruta, g.localidad) for g in presentes])
            if res is ResultadoAsignacion.OK:
                # Verificación POR RUTA: 'listo para corrección' SOLO si la ruta
                # desapareció de la grilla de libres (se asignó de verdad). Una
                # ruta que quedó sin asignar antes salía como lista igual.
                for g in presentes:
                    if clave_rl(g.ruta, g.localidad, g.turno) in no_asignadas:
                        _marcar(g.liberados,
                                Estado.error("la ruta no se asignó al colector (verificación)"),
                                sheets)
                    else:
                        _marcar(g.liberados, Estado.LISTO_PARA_CORRECCION, sheets)
            else:
                motivo = ("colector no encontrado"
                          if res is ResultadoAsignacion.COLECTOR_NO_ENCONTRADO
                          else "falla al asignar ruta")
                for g in presentes:
                    _marcar(g.liberados, Estado.error(motivo), sheets)
                # Reset: destildar recargando el turno para no arrastrar al siguiente colector.
                portal.seleccionar_turno_c(turno)
