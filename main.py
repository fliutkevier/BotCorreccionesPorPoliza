"""Entry point del bot.

Ejemplos:
  python main.py                          # lote: todos los casos pendientes
  python main.py --poliza 12345           # uno suelto
  python main.py --mock                   # usa MockPortal (sin VPN ni selectores)
"""
from __future__ import annotations

import argparse
import logging

import config
from models import Caso, Estado
from orchestrator import procesar
from portal import MockPortal, Portal, PortalInterface
from sheets_client import SheetsClient


def _setup_logging() -> None:
    config.LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_DIR / "bot.log", encoding="utf-8"),
        ],
    )


def _filtrar(casos: list[Caso], poliza: str | None) -> list[Caso]:
    """Modo uno suelto: filtra por póliza. Modo lote: solo pendientes."""
    if poliza:
        return [c for c in casos if c.poliza == poliza]
    return [c for c in casos if Estado.es_pendiente(c.estado)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de corrección de rutas Naturgy")
    parser.add_argument("--poliza", help="procesar solo esta póliza (modo uno suelto)")
    parser.add_argument("--mock", action="store_true", help="usar MockPortal (sin navegador)")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("main")

    sheets = SheetsClient()
    casos = _filtrar(sheets.leer_casos(), args.poliza)
    if not casos:
        log.info("No hay casos para procesar.")
        return
    log.info("%s caso(s) a procesar.", len(casos))

    portal: PortalInterface = MockPortal() if args.mock else Portal()
    try:
        procesar(casos, portal, sheets)
    finally:
        portal.cerrar()
    log.info("Proceso finalizado.")


if __name__ == "__main__":
    main()
