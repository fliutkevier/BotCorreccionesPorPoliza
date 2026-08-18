"""Configuración central del bot. Nada de valores hardcodeados en el resto del código."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Rutas base (script en desarrollo o .exe de PyInstaller) ------------------
# Congelado (PyInstaller): todo lo editable/portable vive AL LADO del .exe:
# credentials.json, logs\ y ms-playwright\ (el Firefox embebido del paquete).
# En desarrollo: junto a este archivo, como siempre.
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
if FROZEN:
    # Los compañeros NO tienen %LOCALAPPDATA%\ms-playwright: el paquete lleva su
    # propio Firefox. Debe setearse ANTES de arrancar el driver de Playwright
    # (config se importa primero en main, así que acá es seguro).
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE_DIR / "ms-playwright"))

# --- Portales (PRECONDICIÓN: VPN ya conectada) -------------------------------
# Stack real detectado: GeneXus + K2BTools sobre ASP.NET.
BASE_URL = "https://lecturasbanprod.gasnor.com:7091"
URL_GESTION = f"{BASE_URL}/wpadminlecturas.aspx"        # FASE A: liberar suministro
URL_LIBERACION = f"{BASE_URL}/wpliberaruta.aspx"        # FASE B: liberar ruta
URL_ASIGNACION = f"{BASE_URL}/wpasignacionrutas.aspx"   # FASE C: asignar a colector

# --- Navegador ----------------------------------------------------------------
HEADLESS = False          # v1 SIEMPRE visible para validar contra el proceso manual
NAVEGADOR = "firefox"     # el portal corre en Firefox; usamos el Firefox de Playwright
TIMEOUT_NAVEGACION_MS = 60_000
TIMEOUT_ACCION_MS = 45_000
TIMEOUT_CARGA_RUTAS_MS = 60_000  # tras elegir turno: carga AJAX de la grilla (~20s)

# Formato literal de la fecha de lectura cuando el lector NO sincronizó ("en 0").
# Visto en los datos reales: día/mes/año en blanco + hora en cero.
FECHA_LECTURA_CERO_EJEMPLO = "  /  /     00:00:00"

# --- Google Sheets ------------------------------------------------------------
SHEET_ID = "18p_Yjv6DlXaUuhyIaTZ-dbngv5src2WSof_t-qirD5s"   # <-- PONER LOS VALORES DE TU config.py REAL
WORKSHEET = "Hoja 1"                # <-- (SHEET_ID y WORKSHEET)
GOOGLE_CREDENTIALS = BASE_DIR / "credentials.json"

COL_TURNO = "TURNO"
COL_RUTA = "RUTA"
COL_POLIZA = "POLIZA"
COL_COLECTOR = "COLECTOR"
COL_ESTADO = "ESTADO"

# --- Logging ------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
