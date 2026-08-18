"""Modelos de dominio y reglas de agrupado."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


def ruta_norm(ruta: str, turno: str = "") -> str:
    """Normaliza la ruta con ceros adelante según el turno.
    - Turno 43: 3 dígitos (ej. '920', no '0920').
    - Resto:    4 dígitos (ej. '530' -> '0530').
    Si no es numérica, la devuelve tal cual."""
    r = str(ruta).strip()
    if not r.isdigit():
        return r
    ancho = 3 if str(turno).strip() == "43" else 4
    return r.zfill(ancho)


# Alias retrocompatible (por defecto 4 dígitos, sin turno).
def ruta4(ruta: str) -> str:
    return ruta_norm(ruta, "")


def clave_rl(ruta: str, localidad: str = "", turno: str = "") -> tuple[str, str]:
    """Clave única de una ruta física: (ruta normalizada, localidad en mayúsculas).
    El ancho de la ruta depende del turno (43 -> 3 díg). La localidad solo
    desempata en el turno 43; en el resto va vacía."""
    return (ruta_norm(ruta, turno), (localidad or "").strip().upper())


# --- Estados que el bot escribe en la columna `estado` del sheet -------------
class Estado:
    PENDIENTE = "pendiente"            # valor inicial (o celda vacía) que el bot toma
    PREPARANDO = "preparando"
    LISTO_PARA_CORRECCION = "listo para corrección"  # estado terminal de ESTE bot
    SALTADO_FECHA0 = "saltado: lector no sincronizó (fecha 0)"

    @staticmethod
    def error(motivo: str) -> str:
        return f"no se pudo preparar: {motivo}"

    @staticmethod
    def es_pendiente(valor: str) -> bool:
        return (valor or "").strip().lower() in ("", Estado.PENDIENTE)


# --- Resultado de la liberación de suministro (fase A), por póliza -----------
class ResultadoLiberacion(Enum):
    OK = "ok"                 # suministro liberado correctamente
    SKIP_FECHA0 = "fecha0"    # fecha de lectura en 0 -> NO se toca (fail-safe)
    ERROR = "error"           # falla técnica al liberar


class ResultadoRuta(Enum):
    """Resultado de las fases B y C (a nivel ruta)."""
    OK = "ok"                  # acción realizada
    NO_ENCONTRADA = "no_encontrada"  # la ruta no está en la grilla de esa fase
    ERROR = "error"            # falla técnica


@dataclass
class Caso:
    """Una fila del sheet. `fila` es el índice 1-based para escribir de vuelta."""
    fila: int
    turno: str
    ruta: str
    poliza: str
    colector: str
    estado: str = Estado.PENDIENTE
    localidad: str = ""   # solo se usa para desempatar ruta en el turno 43

    @property
    def clave_ruta(self) -> tuple[str, str, str]:
        """Identidad de la ruta. Incluye localidad para desempatar (turno 43):
        una misma ruta en dos localidades son rutas físicas distintas."""
        return (self.turno.strip(), ruta_norm(self.ruta, self.turno),
                self.localidad.strip().upper())


@dataclass
class GrupoRuta:
    """Casos de una misma (turno, ruta, localidad). Las fases B y C operan acá."""
    turno: str
    ruta: str
    localidad: str = ""                   # desempate de ruta (turno 43); "" = no aplica
    casos: list[Caso] = field(default_factory=list)
    colector: str | None = None          # único colector válido del grupo
    conflicto_colector: bool = False
    liberados: list[Caso] = field(default_factory=list)  # pólizas liberadas en FASE A

    def resolver_colector(self) -> None:
        """Regla #1: un grupo no puede tener más de un colector distinto."""
        colectores = {c.colector.strip() for c in self.casos if c.colector.strip()}
        if len(colectores) > 1:
            self.conflicto_colector = True
        elif colectores:
            self.colector = colectores.pop()


def agrupar_por_ruta(casos: list[Caso]) -> list[GrupoRuta]:
    """Agrupa por (turno, ruta, localidad) y resuelve el colector de cada grupo."""
    grupos: dict[tuple[str, str, str], GrupoRuta] = {}
    for c in casos:
        clave = c.clave_ruta
        if clave not in grupos:
            grupos[clave] = GrupoRuta(turno=clave[0], ruta=clave[1], localidad=c.localidad.strip())
        grupos[clave].casos.append(c)
    for g in grupos.values():
        g.resolver_colector()
    return list(grupos.values())
