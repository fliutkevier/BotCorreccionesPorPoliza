"""Capa de interacción con los portales (Playwright). Stack: GeneXus + K2BTools.

Bot de CORRECCIONES (3 fases):
  A: liberar suministro por póliza (reusando página por turno y popup por ruta)
  B: liberar rutas del turno en una pasada
  C: asignar rutas a colectores SIN recargar entre colectores

Turno 43: la ruta es de 3 dígitos y se desempata por localidad.
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Protocol

from playwright.sync_api import FrameLocator, Page, sync_playwright

import config
from models import ResultadoLiberacion, clave_rl as _clave_rl, ruta_norm as _ruta_norm

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


class ResultadoAsignacion(Enum):
    OK = "ok"
    COLECTOR_NO_ENCONTRADO = "colector_no_encontrado"
    ERROR = "error"


class PortalInterface(Protocol):
    def liberar_suministros_turno(self, turno: str, casos: list) -> list: ...
    def liberar_rutas(self, turno: str, pares: list) -> set | None: ...
    def ir_a_asignacion(self) -> None: ...
    def seleccionar_turno_c(self, turno: str) -> None: ...
    def tildar_rutas_c(self, pares: list) -> set: ...
    def asignar_colector_c(self, colector: str, pares: list) -> tuple["ResultadoAsignacion", set]: ...
    def cerrar(self) -> None: ...


class Portal:
    def __init__(self) -> None:
        self._pw = sync_playwright().start()
        browser_type = getattr(self._pw, config.NAVEGADOR)
        self._browser = browser_type.launch(headless=config.HEADLESS)
        self._ctx = self._browser.new_context(ignore_https_errors=True)
        self._ctx.set_default_timeout(config.TIMEOUT_ACCION_MS)
        self._ctx.set_default_navigation_timeout(config.TIMEOUT_NAVEGACION_MS)
        self._page: Page = self._ctx.new_page()
        self._turno_actual: str = ""

    # ---------------------------------------------------------------- helpers
    # Overlay de carga de GeneXus: <div class="gx-mask"> se INYECTA mientras algo
    # carga y se ELIMINA al terminar. MIENTRAS el mask está visible, la página
    # BLOQUEA la interacción (un click/tilde durante el mask se pierde).
    # OJO: selector por token exacto `div.gx-mask`; NUNCA por subcadena, porque
    # `gx-masked-relative` es una clase PERMANENTE del contenedor.
    MASK = "div.gx-mask"

    def _sin_mask(self, root=None, timeout_ms: int | None = None) -> None:
        """Espera a que NO haya ningún mask visible en `root` (página o iframe).
        Se llama antes de cada interacción para no clickear/tildar bloqueado.
        Caso común (sin mask): resuelve en la PRIMERA lectura (~1 roundtrip).
        Poll corto (100ms): los masks suelen durar <1s y cada 150ms extra de
        poll se multiplica por cada interacción del bot."""
        root = root or self._page
        deadline = timeout_ms or config.TIMEOUT_CARGA_RUTAS_MS
        transcurrido = 0
        while transcurrido < deadline:
            try:
                # `:visible` de Playwright: una sola consulta (existe Y visible).
                if root.locator(f"{self.MASK}:visible").count() == 0:
                    return
            except Exception:  # noqa: BLE001
                return
            self._page.wait_for_timeout(100)
            transcurrido += 100
        log.warning("El mask no desapareció en %dms; se continúa", deadline)

    def _ciclo_mask(self, root=None, aparicion_ms: int = 800) -> bool:
        """Para acciones que disparan carga (elegir turno, filtrar, asignar):
        espera a que el mask APAREZCA y luego a que DESAPAREZCA.
        Devuelve True si vio el ciclo completo; False si nunca apareció.

        La aparición se espera con `wait_for(state='visible')`: Playwright lo
        resuelve por mutaciones/rAF (~16ms), así que detecta masks de vida muy
        corta que un poll de 150ms se perdía. Por lo mismo, la tolerancia de
        aparición puede ser CORTA: GX inyecta el mask al iniciar el request,
        de forma inmediata tras la acción. Si en ~800ms no apareció, no va a
        aparecer, y esperar 4-8s como antes solo quemaba tiempo en cada carga
        rápida (esta era la causa principal de la lentitud vs. la versión
        vieja de esperas fijas)."""
        root = root or self._page
        mask = root.locator(self.MASK).first
        try:
            mask.wait_for(state="visible", timeout=aparicion_ms)
        except Exception:  # noqa: BLE001 - nunca apareció (carga sin mask o ya terminó)
            return False
        try:
            mask.wait_for(state="hidden", timeout=config.TIMEOUT_CARGA_RUTAS_MS)
        except Exception:  # noqa: BLE001
            log.warning("El mask no desapareció a tiempo; se continúa")
        self._sin_mask(root, timeout_ms=2_000)  # por si hay otro mask simultáneo
        self._page.wait_for_timeout(150)  # margen a que pinte el resultado
        return True

    def _visible(self, selector: str) -> bool:
        """True si el elemento existe y está visible (no display:none)."""
        try:
            loc = self._page.locator(selector)
            return loc.count() > 0 and loc.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    def _seleccionar_turno(self, turno: str, actualizar: bool = False) -> None:
        """Elige el turno y dispara la carga de rutas. SE SELECCIONA UNA SOLA VEZ.

        - actualizar=True (FASE A): elegir el turno NO carga la grilla (eso lo
          hace el botón 'Actualizar' / #REFRESH). El onchange de GX del combo
          dispara un postback que RECARGA la página y RESETEA el combo: esa era
          la causa real de la doble carga (poner turno -> recarga -> combo vacío
          -> re-seleccionar -> recién ahí Actualizar). Por eso acá el valor se
          setea POR JS SIN disparar el change: el REFRESH postea el formulario
          completo (combo incluido), igual que el flujo manual turno->Actualizar.
        - actualizar=False (FASE B y C): la grilla carga sola al elegir el
          turno, así que el onchange SÍ es necesario (select_option normal).
          La espera de la carga la hace _esperar_grilla_rutas directamente
          (aparición + fin del mask de la grilla, con respaldo por conteo):
          NO se consume antes el ciclo con _ciclo_mask, porque esa doble
          espera hacía que la segunda nunca viera aparecer el mask y quemara
          su tolerancia completa + estabilización."""
        self._turno_actual = str(turno).strip()
        self._sin_mask()
        if actualizar:
            # OJO: setear solo el value NO alcanza. GX tiene CHANGE DIFERIDO:
            # guarda el valor viejo del control (data-gxoldvalue) y al clickear
            # cualquier botón compara contra el DOM; si difieren dispara el
            # EVTURNO pendiente EN EL CLICK DE ACTUALIZAR -> recarga y resetea
            # el combo (el "postback innecesario"). Sincronizando el oldvalue,
            # el click en REFRESH no ve cambios pendientes y postea el form
            # con el turno incluido, igual que el flujo manual.
            self._page.eval_on_selector(
                "#vTURNO",
                "(el, v) => { el.value = v; "
                "el.setAttribute('data-gxoldvalue', v); "
                "el.removeAttribute('gxctrlchanged'); }",
                self._turno_actual)
            if self._page.input_value("#vTURNO").strip() != self._turno_actual:
                # el value no matchea ninguna <option>: caer al select clásico
                log.warning("Turno %s no seteable por JS; usando select_option",
                            self._turno_actual)
                self._page.select_option("#vTURNO", value=self._turno_actual)
                self._ciclo_mask(aparicion_ms=1_500)  # postback del onchange
                self._turno_estable()
            self._sin_mask()
            self._click_actualizar()
            self._esperar_grilla_rutas()  # mask del REFRESH: aparición + fin
            # Anomalía (no debería pasar): el REFRESH ignoró el valor del combo.
            # Un solo reintento por el camino clásico con onchange.
            if self._page.input_value("#vTURNO").strip() != self._turno_actual:
                log.warning("El REFRESH no tomó el turno %s; reintento con onchange",
                            self._turno_actual)
                self._page.select_option("#vTURNO", value=self._turno_actual)
                self._ciclo_mask(aparicion_ms=1_500)
                self._turno_estable()
                self._sin_mask()
                self._click_actualizar()
                self._esperar_grilla_rutas()
            return
        # FASE B y C
        self._page.select_option("#vTURNO", value=self._turno_actual)
        self._esperar_grilla_rutas()
        # Anti doble-carga: verificación SONDEADA (una lectura puntual durante
        # el re-render se lee vacía y provocaba re-selección + recarga extra).
        # Solo se re-selecciona si quedó ESTABLEMENTE distinto (caso raro real).
        if not self._turno_estable():
            log.warning("El turno quedó reseteado tras el postback; re-seleccionando (una vez)")
            self._page.select_option("#vTURNO", value=self._turno_actual)
            self._esperar_grilla_rutas()

    def _turno_estable(self, sondeo_ms: int = 5_000) -> bool:
        """True si #vTURNO termina mostrando el turno esperado. Se sondea porque
        durante el re-render del postback el valor puede leerse vacío/transitorio,
        y un falso negativo dispara una re-selección con OTRA carga completa.
        Caso común (valor correcto): resuelve en la primera lectura."""
        transcurrido = 0
        while transcurrido < sondeo_ms:
            try:
                if self._page.input_value("#vTURNO").strip() == self._turno_actual:
                    return True
            except Exception:  # noqa: BLE001 - select re-renderizando
                pass
            self._page.wait_for_timeout(200)
            transcurrido += 200
        return False

    def _click_actualizar(self) -> None:
        """Clickea 'Actualizar' por id (#REFRESH) o, si no, por su value."""
        for sel in ["#REFRESH",
                    "input[type=button][value='Actualizar']",
                    "input[value='Actualizar']"]:
            loc = self._page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=4_000)
            except Exception:  # noqa: BLE001
                pass
            try:
                loc.click(timeout=6_000)
                return
            except Exception:  # noqa: BLE001
                try:
                    loc.click(force=True, timeout=4_000)
                    return
                except Exception:  # noqa: BLE001
                    continue
        raise RuntimeError("No se encontró el botón 'Actualizar' en la página")

    # Overlay de carga de GeneXus: se INYECTA un <div class="gx-mask"> dentro del
    # contenedor de la grilla mientras carga, y se ELIMINA al terminar.
    # OJO: se usa el selector por token exacto `div.gx-mask`. NO usar subcadena
    # ([class*='gx-mask']) porque matchearía `gx-masked-relative`, que es una clase
    # PERMANENTE del contenedor y haría creer que siempre está cargando.
    MASK_LIBRES = "#GridrutasContainerDiv div.gx-mask"
    MASK_ASIGNADA = "#GridrutasasignadaContainerDiv div.gx-mask"

    def _esperar_mask(self, selector: str) -> bool:
        """Espera el ciclo del overlay de carga: que aparezca y luego desaparezca.
        Es la señal REAL de fin de carga (el cartel 'No hay resultados' no sirve:
        se ve también mientras carga).

        Devuelve True si detectó el ciclo, False si nunca lo vio (ahí el llamador
        cae al método por estabilización de conteo).

        Aparición con tolerancia CORTA: se llama inmediatamente después de la
        acción que dispara la carga, y GX inyecta el mask al iniciar el request.
        Si en 1.5s no apareció, no va a aparecer (esperar 6s acá quemaba tiempo
        en toda carga rápida o ya consumida)."""
        try:
            self._page.wait_for_selector(selector, state="visible", timeout=1_500)
        except Exception:  # noqa: BLE001 - no apareció: quizá ya terminó
            return False
        try:
            self._page.wait_for_selector(selector, state="hidden",
                                         timeout=config.TIMEOUT_CARGA_RUTAS_MS)
            self._page.wait_for_timeout(200)  # margen a que pinte el resultado
            return True
        except Exception:  # noqa: BLE001
            log.warning("El overlay de carga no desapareció a tiempo")
            return False

    def _esperar_grilla_rutas(self) -> None:
        """Espera el fin de la carga. Preferentemente por el overlay gx-mask; si no
        se detecta, cae a estabilizar el conteo de filas (método de respaldo).
        NO se mira el cartel 'No hay resultados': sigue visible MIENTRAS carga."""
        filas = "span[id^='span_vRUTASRUTA_']"
        if self._esperar_mask(self.MASK_LIBRES):
            n = self._page.locator(filas).count()
            log.info("Grilla de rutas cargada (mask): %d filas", n)
            return
        previo, estable, transcurrido = -1, 0, 0
        while transcurrido < config.TIMEOUT_CARGA_RUTAS_MS:
            n = self._page.locator(filas).count()
            if n == previo:
                estable += 1
                # Con filas: 2 lecturas iguales (800ms) alcanzan. Grilla vacía:
                # se exige MÁS sostén (2.4s) porque 0 también se lee mientras
                # carga; antes n==0 nunca cortaba y quemaba el timeout completo.
                if (n > 0 and estable >= 2) or (n == 0 and estable >= 6):
                    log.info("Grilla de rutas cargada: %d filas", n)
                    return
            else:
                estable = 0
            previo = n
            self._page.wait_for_timeout(400)
            transcurrido += 400
        log.warning("La grilla no se estabilizó en %dms (filas=%d)",
                    config.TIMEOUT_CARGA_RUTAS_MS, previo)

    def _esperar_rutas_del_colector(self) -> None:
        """Tras elegir un colector, la página carga SUS rutas ya asignadas.
        Se espera a que el conteo se estabilice; acá 0 SÍ es un resultado válido
        (colector sin rutas), por eso no se exige >0. No se mira el cartel
        'No hay resultados' porque también aparece mientras carga."""
        filas = "tr[id^='GridrutasasignadaContainerRow_']"
        if self._esperar_mask(self.MASK_ASIGNADA):
            n = self._page.locator(filas).count()
            log.info("Rutas del colector cargadas (mask): %d", n)
            return
        self._page.wait_for_timeout(800)   # margen: que dispare y avance la carga
        previo, estable, transcurrido = -1, 0, 0
        while transcurrido < 20_000:       # tope corto: es una grilla chica
            n = self._page.locator(filas).count()
            if n == previo:
                estable += 1
                if estable >= 3:           # 3 lecturas iguales (1.2s): asentó
                    log.info("Rutas del colector cargadas: %d", n)
                    return
            else:
                estable = 0
            previo = n
            self._page.wait_for_timeout(400)
            transcurrido += 400

    def _fetch_rutas(self, con_localidad: bool, scope: str = "") -> tuple[list, dict]:
        """Lee (id, texto) de las rutas de la grilla y, si aplica, un mapa
        NNNN -> localidad. Una sola lectura del DOM por llamada."""
        pref = f"{scope} " if scope else ""
        datos_r = self._page.locator(f"{pref}span[id^='span_vRUTASRUTA_']").evaluate_all(
            "els => els.map(e => [e.id, (e.textContent || '').trim()])")
        loc_map: dict[str, str] = {}
        if con_localidad:
            pares = self._page.locator(
                f"{pref}span[id^='span_vRUTASLOCALIDAD_RUTA_']").evaluate_all(
                "els => els.map(e => [e.id, (e.textContent || '').trim()])")
            for el_id, txt in pares:
                m = re.search(r"_(\d{4})$", el_id or "")
                if m:
                    loc_map[m.group(1)] = _norm(txt)
        return datos_r, loc_map

    def _idxs_ruta(self, datos_r: list, loc_map: dict, ruta: str, localidad: str) -> list[str]:
        """Sufijos NNNN de TODAS las filas que matchean la ruta (contiene) y,
        si hay localidad, también la localidad (igual)."""
        objetivo = _norm(_ruta_norm(ruta, self._turno_actual))
        loc = _norm(localidad)
        idxs = []
        for el_id, txt in datos_r:
            m = re.search(r"_(\d{4})$", el_id or "")
            if not m or not objetivo or objetivo not in _norm(txt):
                continue
            idx = m.group(1)
            if loc and loc_map.get(idx, "") != loc:
                continue  # desempate por localidad (turno 43)
            idxs.append(idx)
        return idxs

    def _match_idx(self, datos_r: list, loc_map: dict, ruta: str, localidad: str) -> str | None:
        """Sufijo NNNN de la fila que matchea. None si 0 o >1 coincidencias."""
        idxs = self._idxs_ruta(datos_r, loc_map, ruta, localidad)
        if len(idxs) != 1:
            log.error("Ruta %s (localidad %r): %d coincidencias",
                      _ruta_norm(ruta, self._turno_actual), localidad, len(idxs))
            return None
        return idxs[0]

    def _indice_ruta(self, ruta: str, localidad: str, scope: str = "") -> str | None:
        datos_r, loc_map = self._fetch_rutas(con_localidad=bool(localidad.strip()), scope=scope)
        return self._match_idx(datos_r, loc_map, ruta, localidad)

    def _tildar_rutas(self, pares: list[tuple[str, str]], scope: str = "") -> set[tuple[str, str]]:
        """Tilda las rutas presentes (best-effort). Reglas duras del portal:
        - MIENTRAS hay mask, la página DESCARTA los clicks: hay que esperar
          _sin_mask ANTES de cada tilde (causa real de rutas 'salteadas' en B).
        - Tildar puede disparar un mask corto / re-render que corre índices o
          destilda filas: re-leer la grilla antes de cada intento, VERIFICAR el
          tilde después, y hacer una pasada final de verificación."""
        con_loc = any((loc or "").strip() for _, loc in pares)
        tildadas: set[tuple[str, str]] = set()
        for ruta, localidad in pares:
            for intento in range(3):
                self._sin_mask()  # nunca tildar con la página bloqueada
                datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc, scope=scope)
                idx = self._match_idx(datos_r, loc_map, ruta, localidad)
                if idx is None:
                    break  # no está en la grilla (best-effort)
                sel = f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}"
                try:
                    self._check_gx(sel)
                    # El tilde puede disparar un mask corto entre selecciones.
                    # Tolerancia mínima: si el mask viene, se inyecta al instante;
                    # 1.2s acá se pagaba POR RUTA aunque no hubiera mask.
                    self._ciclo_mask(aparicion_ms=300)
                    if not self._page.locator(sel).is_checked():
                        raise RuntimeError("el tilde no quedó aplicado tras el re-render")
                    tildadas.add(_clave_rl(ruta, localidad, self._turno_actual))
                    break
                except Exception:  # noqa: BLE001 - re-render: reasentar y reintentar
                    log.warning("Reintentando tildado de ruta %s (intento %d)", ruta, intento + 1)
                    self._page.wait_for_timeout(600)
        if tildadas:
            self._verificar_tildes(pares, tildadas, con_loc, scope)
        return tildadas

    def _verificar_tildes(self, pares: list[tuple[str, str]], tildadas: set,
                          con_loc: bool, scope: str) -> None:
        """Pasada final ANTES de accionar el botón: un re-render tardío puede
        haber destildado filas ya tildadas. Se re-lee la grilla y se re-tilda lo
        que falte (máximo 2 pasadas)."""
        for _ in range(2):
            self._sin_mask()
            datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc, scope=scope)
            pendientes: list[tuple[str, str]] = []
            for ruta, localidad in pares:
                if _clave_rl(ruta, localidad, self._turno_actual) not in tildadas:
                    continue
                idx = self._match_idx(datos_r, loc_map, ruta, localidad)
                if idx is None:
                    continue
                try:
                    if not self._page.locator(
                            f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}").is_checked():
                        pendientes.append((ruta, idx))
                except Exception:  # noqa: BLE001
                    pass
            if not pendientes:
                return
            log.warning("Verificación de tildes: %d perdidos por re-render; re-tildando",
                        len(pendientes))
            for ruta, idx in pendientes:
                try:
                    self._sin_mask()
                    self._check_gx(f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}")
                    self._ciclo_mask(aparicion_ms=300)
                except Exception:  # noqa: BLE001
                    log.warning("No se pudo re-tildar la ruta %s en la verificación", ruta)

    def _seleccionar_colector(self, colector: str) -> bool:
        """Selecciona el colector en #vCOLECTOR por NOMBRE (exacto, luego contiene)."""
        objetivo = _norm(colector)
        opciones = self._page.locator("#vCOLECTOR option")
        exactos, contiene = [], []
        for i in range(opciones.count()):
            op = opciones.nth(i)
            txt = _norm(op.inner_text())
            val = op.get_attribute("value") or ""
            if not txt:
                continue
            if txt == objetivo:
                exactos.append(val)
            elif objetivo and objetivo in txt:
                contiene.append(val)
        candidatos = exactos or contiene
        if len(candidatos) != 1:
            log.error("Colector %r: %d coincidencias en el desplegable", colector, len(candidatos))
            return False
        self._page.select_option("#vCOLECTOR", value=candidatos[0])
        return True

    def _check_gx(self, selector: str) -> None:
        loc = self._page.locator(selector)
        loc.wait_for(state="attached", timeout=config.TIMEOUT_ACCION_MS)
        try:
            loc.scroll_into_view_if_needed(timeout=5_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.check(timeout=8_000)
        except Exception:  # noqa: BLE001
            loc.check(force=True, timeout=8_000)
        if not loc.is_checked():  # con force el tilde puede no aplicarse
            raise RuntimeError(f"checkbox {selector} no quedó tildado")

    def _click_gx(self, selector: str) -> None:
        loc = self._page.locator(selector)
        loc.wait_for(state="attached", timeout=config.TIMEOUT_ACCION_MS)
        try:
            loc.scroll_into_view_if_needed(timeout=3_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.click(timeout=6_000)
        except Exception:  # noqa: BLE001
            loc.click(force=True, timeout=6_000)

    def _aceptar_confirm(self, root) -> None:
        """Acepta el modal de confirmación de K2BTools ('¿Está seguro?')."""
        try:
            root.locator("input.K2BT_ConfirmDialogOk").click(timeout=10_000)
        except Exception:  # noqa: BLE001
            log.debug("No apareció el confirm K2BTools (puede no aplicar).")

    @staticmethod
    def _fecha_es_cero(fecha: str | None) -> bool:
        """True si el lector no sincronizó. Cubre None/vacío, '  /  /     00:00:00'
        y cualquier cadena sin dígitos distintos de cero."""
        if not fecha or not fecha.strip():
            return True
        solo_digitos = re.sub(r"\D", "", fecha)
        return solo_digitos == "" or solo_digitos.strip("0") == ""

    # --------------------------------------------------------------- FASE A
    def liberar_suministros_turno(self, turno: str, casos: list) -> list:
        """Libera los suministros de TODAS las pólizas de un turno reusando la página.
        Reuso a tres niveles: turno (recarga), ruta (popup + Escape), póliza (filtro).
        Devuelve [(caso, resultado, anterior_colector), ...]."""
        resultados: list[tuple[object, ResultadoLiberacion, str | None]] = []
        try:
            self._page.goto(config.URL_GESTION, wait_until="domcontentloaded")
            self._seleccionar_turno(turno, actualizar=True)  # carga rutas UNA vez
        except Exception:  # noqa: BLE001
            log.exception("FASE A: fallo al cargar el turno %s", turno)
            return [(c, ResultadoLiberacion.ERROR, None) for c in casos]

        # Agrupar las pólizas por ruta+localidad (localidad solo desempata en turno 43).
        por_ruta: dict[tuple[str, str], dict] = {}
        for c in casos:
            k = _clave_rl(c.ruta, getattr(c, "localidad", ""), turno)
            if k not in por_ruta:
                por_ruta[k] = {"ruta": c.ruta, "localidad": getattr(c, "localidad", ""), "casos": []}
            por_ruta[k]["casos"].append(c)

        for info in por_ruta.values():
            ruta, localidad, casos_ruta = info["ruta"], info["localidad"], info["casos"]
            idx_ruta = self._indice_ruta(ruta, localidad)
            if idx_ruta is None:
                resultados += [(c, ResultadoLiberacion.ERROR, None) for c in casos_ruta]
                continue

            # Anterior colector (mismo para toda la ruta).
            try:
                anterior = self._page.locator(
                    f"#span_vRUTASCOD_COLECTOR_{idx_ruta}").inner_text().strip()
            except Exception:  # noqa: BLE001
                anterior = None

            # Abrir el popup de la ruta. Force-click directo (el ícono de grilla
            # GeneXus se reporta 'not visible' y el tanteo previo agregaba 5-10s).
            # NO esperar filas: el popup puede abrir filtrando la póliza heredada
            # y mostrar "No hay resultados" (0 filas); se espera solo el filtro.
            try:
                accion = self._page.locator(f"#vSUMINISTROS_ACTION_{idx_ruta}")
                try:
                    accion.scroll_into_view_if_needed(timeout=3_000)
                except Exception:  # noqa: BLE001
                    pass
                accion.click(force=True)
                frame = self._page.frame_locator("#gxp0_ifrm")
                frame.locator("#vGENERICFILTER_GRIDSUMINISTROS").wait_for(
                    state="visible", timeout=config.TIMEOUT_CARGA_RUTAS_MS)
                # El popup tiene SU PROPIO mask (se oscurece mientras carga los
                # suministros de la ruta): esperar a que termine ANTES de tocar
                # el filtro, o el primer click/borrado se pierde.
                self._sin_mask(frame)
            except Exception:  # noqa: BLE001
                log.exception("FASE A: no se pudo abrir la ruta %s", ruta)
                resultados += [(c, ResultadoLiberacion.ERROR, anterior) for c in casos_ruta]
                continue

            # Liberar cada póliza de la ruta (limpiando el filtro entre una y otra).
            for c in casos_ruta:
                res = self._liberar_una_poliza(frame, c.poliza)
                resultados.append((c, res, anterior))

            self._cerrar_popup()  # Escape: cierra el cuadro; la grilla queda intacta

        return resultados

    def _liberar_una_poliza(self, frame, poliza: str) -> ResultadoLiberacion:
        """Filtra una póliza dentro del popup ya abierto y libera su suministro.
        Proceso calcado del manual: 1) vaciar el filtro -> esperar el mask de la
        recarga con TODAS las pólizas; 2) escribir la póliza entera de una ->
        esperar el mask de la buscada ('pegar y esperar que desaparezca el mask,
        eso da exacto'). Todas las esperas van contra el MASK DEL IFRAME, no
        contra tiempos fijos ni contra el mask de la página de fondo."""
        try:
            filtro = frame.locator("#vGENERICFILTER_GRIDSUMINISTROS")
            filtro.wait_for(state="visible", timeout=config.TIMEOUT_ACCION_MS)
            self._sin_mask(frame)

            # El filtro es EN VIVO (sin Enter) y hereda la póliza de la ruta anterior.
            # Vaciarlo POR COMPLETO antes de escribir: select-all + borrar, y reforzar
            # por DOM disparando el evento de input (por si el teclado no alcanza).
            filtro.click()
            filtro.press("Control+a")
            filtro.press("Delete")
            try:
                filtro.evaluate(
                    "el => { el.value=''; "
                    "el.dispatchEvent(new Event('input', {bubbles:true})); "
                    "el.dispatchEvent(new Event('keyup', {bubbles:true})); }")
            except Exception:  # noqa: BLE001
                pass
            # Recarga con todas las pólizas: esperar el CICLO del mask del popup.
            # Aparición corta (800ms): el mask del iframe se inyecta al disparar
            # la buscada; si no apareció es que no hubo recarga o ya terminó.
            # Con 4s de tolerancia el costo por póliza superaba a las esperas
            # fijas de la versión vieja (1.2s+1.5s vs hasta 5.2s+5.5s).
            if not self._ciclo_mask(frame, aparicion_ms=800):
                self._page.wait_for_timeout(1200)  # respaldo: no se vio el mask
            # Escribir la póliza entera en una sola ráfaga (equivale a pegarla:
            # el textchanged dispara UNA buscada al terminar) y esperar su mask.
            filtro.type(str(poliza).strip(), delay=30)
            if not self._ciclo_mask(frame, aparicion_ms=800):
                self._page.wait_for_timeout(1500)  # respaldo: no se vio el mask

            idx_med = self._buscar_indice_frame(frame, "span_vNRO_SERVICIO_", poliza)
            if idx_med is None:
                return ResultadoLiberacion.ERROR

            # GUARDA CRÍTICA: fecha de lectura (FH_LECTURA). Si está en 0, NO liberar.
            fecha = frame.locator(f"#span_vFH_LECTURA_{idx_med}").inner_text()
            if self._fecha_es_cero(fecha):
                log.warning("Póliza %s con fecha=%r -> SKIP (no sincronizó)", poliza, fecha)
                return ResultadoLiberacion.SKIP_FECHA0

            # Botón amarillo: necesita la grilla scrolleada al tope derecho.
            boton = frame.locator(f"#vUPDATE_ACTION_{idx_med}")
            boton.wait_for(state="attached", timeout=config.TIMEOUT_ACCION_MS)
            try:
                boton.evaluate(
                    "el => { const c = el.closest('[style*=overflow], .gx-grid, table')?.parentElement "
                    "|| document.scrollingElement; if (c) c.scrollLeft = c.scrollWidth; "
                    "el.scrollIntoView({block:'nearest', inline:'end'}); }")
            except Exception:  # noqa: BLE001
                pass
            boton.click(force=True)
            ok = frame.locator("input.K2BT_ConfirmDialogOk")
            try:
                ok.wait_for(state="visible", timeout=10_000)
                ok.click(force=True)
            except Exception:  # noqa: BLE001
                log.warning("No apareció el confirm al liberar póliza %s", poliza)
            # La baja recarga la grilla del popup: esperar su mask (respaldo fijo).
            if not self._ciclo_mask(frame, aparicion_ms=800):
                self._page.wait_for_timeout(800)
            return ResultadoLiberacion.OK
        except Exception:  # noqa: BLE001
            log.exception("Error liberando suministro de póliza %s", poliza)
            return ResultadoLiberacion.ERROR

    def _buscar_indice_frame(self, frame: FrameLocator, span_prefix: str,
                             valor: str) -> str | None:
        """Sufijo NNNN dentro de un iframe cuyo span matchea `valor` (exacto)."""
        datos = frame.locator(f"span[id^='{span_prefix}']").evaluate_all(
            "els => els.map(e => [e.id, (e.textContent || '').trim()])")
        objetivo = _norm(valor)
        matches = [m.group(1) for el_id, txt in datos
                   if objetivo and _norm(txt) == objetivo
                   and (m := re.search(r"_(\d{4})$", el_id or ""))]
        if len(matches) != 1:
            log.error("%s=%r: %d coincidencias entre %d filas",
                      span_prefix, valor, len(matches), len(datos))
            return None
        return matches[0]

    def _cerrar_popup(self) -> None:
        """Cierra el popup de la ruta (Escape). La grilla de rutas queda intacta."""
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- FASE B
    def liberar_rutas(self, turno: str, pares: list[tuple[str, str]]) -> set | None:
        """Libera en una sola pasada todas las rutas presentes del turno.
        `pares` = [(ruta, localidad), ...]. None = falla técnica de la página."""
        try:
            self._page.goto(config.URL_LIBERACION, wait_until="domcontentloaded")
            self._seleccionar_turno(turno, actualizar=False)
            tildadas = self._tildar_rutas(pares)
            if tildadas:
                self._sin_mask()  # no accionar con la página bloqueada
                self._click_gx("#ACTION")  # value: "Liberar Rutas"
                self._aceptar_confirm(self._page)
                # El mask del procesamiento arranca junto con el request: si en
                # 1.5s no apareció, no viene; respaldo fijo corto y seguir.
                if not self._ciclo_mask(aparicion_ms=1_500):
                    self._page.wait_for_timeout(800)
            log.info("FASE B turno %s: liberadas %s", turno, sorted(tildadas))
            return tildadas
        except Exception:  # noqa: BLE001
            log.exception("Error liberando rutas (turno %s)", turno)
            return None

    # --------------------------------------------------------------- FASE C
    # Se entra a la página UNA vez; entre colectores NO se recarga (la grilla de
    # libres se actualiza sola tras asignar).
    def ir_a_asignacion(self) -> None:
        self._page.goto(config.URL_ASIGNACION, wait_until="domcontentloaded")

    def seleccionar_turno_c(self, turno: str) -> None:
        self._seleccionar_turno(turno, actualizar=False)

    def tildar_rutas_c(self, pares: list) -> set:
        """Tilda rutas en la grilla de LIBRES (scoped, para no tocar la de asignadas)."""
        return self._tildar_rutas(pares, scope="#GridrutasContainerTbl")

    # Cartel flotante de K2BTools con el resultado de la acción, p.ej.
    # "Se Asignarón Rutas al colector 600". Es la señal REAL de fin del ASIGNAR.
    CARTEL_RESULTADO = "div.K2BT_MessageText"

    def _esperar_fin_asignacion(self) -> None:
        """Espera el fin del ASIGNAR por el cartel flotante y luego asienta la
        grilla. PROHIBIDO networkidle acá: los websockets de GeneXus no drenan
        nunca y ese _settle era el 'queda quieto' a veces sí / a veces no.
        Si el cartel no se llega a ver (se desvanece solo), el respaldo es el
        mask + estabilización de la grilla; la verificación por ruta posterior
        es la que decide, así que un cartel perdido no marca nada mal."""
        visto = False
        transcurrido = 0
        while transcurrido < 30_000:
            try:
                if self._page.locator(f"{self.CARTEL_RESULTADO}:visible").count() > 0:
                    visto = True
                    try:
                        txt = self._page.locator(
                            self.CARTEL_RESULTADO).first.inner_text().strip()
                        log.info("Cartel de resultado: %r", txt)
                    except Exception:  # noqa: BLE001
                        pass
                    break
            except Exception:  # noqa: BLE001
                pass
            self._page.wait_for_timeout(200)
            transcurrido += 200
        if not visto:
            log.warning("No se vio el cartel de resultado del ASIGNAR; "
                        "se sigue por mask + grilla")
        self._sin_mask()
        self._esperar_grilla_rutas()  # grilla ya actualizada (asignadas fuera)

    def _rutas_aun_libres(self, pares: list) -> set:
        """Claves de las rutas que SIGUEN en la grilla de libres tras asignar,
        o sea que NO se asignaron (si hay >1 fila ambigua, cuenta como no
        asignada). Además las DESTILDA (best-effort) para que un tilde residual
        no se arrastre a la asignación del siguiente colector."""
        con_loc = any((loc or "").strip() for _, loc in pares)
        self._sin_mask()
        datos_r, loc_map = self._fetch_rutas(con_localidad=con_loc,
                                             scope="#GridrutasContainerTbl")
        quedan: set = set()
        idxs_residuales: list[str] = []
        for ruta, localidad in pares:
            idxs = self._idxs_ruta(datos_r, loc_map, ruta, localidad)
            if not idxs:
                continue  # ya no está entre las libres: se asignó
            quedan.add(_clave_rl(ruta, localidad, self._turno_actual))
            idxs_residuales.extend(idxs)
        for idx in idxs_residuales:
            try:
                loc = self._page.locator(f"#vMULTIROWITEMSELECTED_GRIDRUTAS_{idx}")
                if loc.is_checked():
                    loc.uncheck(force=True, timeout=4_000)
            except Exception:  # noqa: BLE001
                pass
        return quedan

    def asignar_colector_c(self, colector: str, pares: list) -> tuple[ResultadoAsignacion, set]:
        """Con las rutas ya tildadas: elige colector y asigna, sin recargar.
        Devuelve (resultado, claves_no_asignadas): el fin se espera por el
        cartel de K2BTools y después se VERIFICA POR RUTA contra la grilla de
        libres. Solo las rutas que desaparecieron de la grilla se asignaron de
        verdad; el llamador NO debe marcar 'listo' a las que quedaron."""
        claves_todas = {_clave_rl(r, loc, self._turno_actual) for r, loc in pares}
        try:
            self._sin_mask()  # elegir colector dispara su propia carga: no pisarla
            if not self._seleccionar_colector(colector):
                return ResultadoAsignacion.COLECTOR_NO_ENCONTRADO, claves_todas
            self._esperar_rutas_del_colector()
            self._sin_mask()
            self._click_gx("#ACTION")  # value: "Asignar"
            self._aceptar_confirm(self._page)
            self._esperar_fin_asignacion()
            no_asignadas = self._rutas_aun_libres(pares)
            if no_asignadas:
                log.warning("ASIGNAR a %s: %d ruta(s) NO se asignaron: %s",
                            colector, len(no_asignadas), sorted(no_asignadas))
            return ResultadoAsignacion.OK, no_asignadas
        except Exception:  # noqa: BLE001
            log.exception("Error asignando al colector %s", colector)
            return ResultadoAsignacion.ERROR, claves_todas

    def cerrar(self) -> None:
        """Cierre robusto. El portal GeneXus deja websockets abiertos que hacen
        colgar el cierre ordenado; se fuerza cada paso y se ignoran errores."""
        for accion in (lambda: self._ctx.close(),
                       lambda: self._browser.close(),
                       lambda: self._pw.stop()):
            try:
                accion()
            except Exception:  # noqa: BLE001
                pass


class MockPortal:
    """Simula resultados para validar orquestación y flujo con Sheets.
    Convención de prueba: pólizas que terminan en '0' simulan fecha=0 (SKIP)."""

    def __init__(self) -> None:
        self._turno_c: str = ""

    def liberar_suministros_turno(self, turno: str, casos: list) -> list:
        out = []
        for c in casos:
            if str(c.poliza).endswith("0"):
                out.append((c, ResultadoLiberacion.SKIP_FECHA0, "(mock) RUIZ. JONATHAN"))
            else:
                log.info("[MOCK] suministro liberado: %s", c.poliza)
                out.append((c, ResultadoLiberacion.OK, "(mock) RUIZ. JONATHAN"))
        return out

    def liberar_rutas(self, turno: str, pares: list) -> set | None:
        claves = {_clave_rl(r, loc, turno) for r, loc in pares}
        log.info("[MOCK] rutas liberadas (turno %s): %s", turno, sorted(claves))
        return claves

    def ir_a_asignacion(self) -> None:
        log.info("[MOCK] abrir página de asignación")

    def seleccionar_turno_c(self, turno: str) -> None:
        self._turno_c = str(turno).strip()
        log.info("[MOCK] turno %s seleccionado en asignación", turno)

    def tildar_rutas_c(self, pares: list) -> set:
        claves = {_clave_rl(r, loc, self._turno_c) for r, loc in pares}
        log.info("[MOCK] tildadas en C: %s", sorted(claves))
        return claves

    def asignar_colector_c(self, colector: str, pares: list) -> tuple[ResultadoAsignacion, set]:
        log.info("[MOCK] asignadas al colector %s (sin recargar): %s", colector, pares)
        return ResultadoAsignacion.OK, set()

    def cerrar(self) -> None:
        pass