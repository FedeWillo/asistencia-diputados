#!/usr/bin/env python3
"""
Scraper de camara.cl (Playwright). Módulo OPCIONAL y desacoplado de la ingesta del API: la app funciona sin él.

Qué captura (lo que el API de datos abiertos no entrega):
  * fichas   → de la ficha de asistencia a Sala de cada diputado: hora de ingreso por sesión (puntualidad),
               cabecera (distrito, región, comunas, bancada) y el resumen oficial.
  * comisiones → asistencia a comisiones del período 2026-2030, que el API no publica: lista de comisiones,
               sesiones por mes y asistencia por sesión (ASISTE / JUSTIFICADO / REEMPLAZADO POR …).

Uso (requiere `pip install playwright` y `playwright install chromium`):
    python scraper.py fichas --limite 3 --paginas 1        # prueba chica: 3 diputados, solo la 1ª página del detalle
    python scraper.py fichas                                # todos los diputados del período actual, todas las páginas
    python scraper.py comisiones --ids 4894 --desde 2026-10-01
    python scraper.py comisiones                            # permanentes desde el 11-mar-2026
    python scraper.py estado

Reglas de convivencia con el sitio: ritmo bajo (pausa entre páginas), un solo navegador, y si el sitio responde
403/429/503 o muestra una página de bloqueo, la corrida se detiene (no se insiste).
Nunca se siguen los enlaces "ver" de las ausencias: abren documentos de justificación con datos personales.
"""

import argparse
import logging
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

import ingesta
from ingesta import abrir_db, ahora, upsert

BASE = "https://www.camara.cl"
URL_FICHA = BASE + "/diputados/detalle/asistencia_sala.aspx?prmId={id}"
URL_LISTAS = {
    "permanentes": BASE + "/legislacion/comisiones/comisiones_permanentes.aspx",
    "unidas": BASE + "/legislacion/comisiones/comisiones_unidas.aspx",
    "otras": BASE + "/legislacion/comisiones/comisiones_otras.aspx",
}
URL_SESIONES = BASE + "/legislacion/comisiones/sesiones.aspx?prmID={id}"
URL_ASISTENCIA = BASE + "/legislacion/comisiones/asistencia.aspx?prmId={comision}&prmIdSesion={sesion}"

PAUSA_SEG = 1.2            # pausa base entre páginas; se le suma un poco de azar
INICIO_PERIODO = date(2026, 3, 11)
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
MESES_NUM = {m.lower(): i + 1 for i, m in enumerate(MESES)}
MESES_ABREV = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12}

log = logging.getLogger("scraper")


class Bloqueado(RuntimeError):
    """El sitio rechazó la visita (403/429/503 o página de bloqueo). Se detiene la corrida."""


# ---------------------------------------------------------------------------
# Extractores en el navegador (JS). Devuelven datos crudos; la interpretación está en Python (parse_*).
# ---------------------------------------------------------------------------

JS_FICHA = """() => {
  const txt = e => (e ? e.innerText.replace(/\\s+/g, ' ').trim() : null);
  const tablas = [...document.querySelectorAll('table')];
  const resumenT = tablas.find(t => /Total Sesiones/i.test(t.innerText));
  const detalleT = tablas.find(t => /Hora Ingreso/i.test(t.innerText));
  const h2 = [...document.querySelectorAll('h2')].map(txt).find(t => /^Diputad[oa]\\s/.test(t)) || null;
  const aux = [...document.querySelectorAll('div.auxi')].find(d => /Comunas:/.test(d.innerText));
  const pct = [...document.querySelectorAll('li.titular')].find(e => /Porcentaje de Asistencia/i.test(e.textContent));
  const fila = tr => [...tr.children].map(td => {
    const c = td.cloneNode(true);
    c.querySelectorAll('a,i').forEach(x => x.remove());      // fuera el enlace "ver" (documento de respaldo)
    return c.textContent.replace(/\\s+/g, ' ').trim();
  });
  const pag = document.querySelector('div.paginacion');
  return {
    titulo: document.title,
    nombre: h2,
    cabecera: aux ? aux.innerText.replace(/\\s+/g, ' ').trim() : null,
    porcentaje: pct ? pct.textContent.replace(/\\s+/g, ' ').trim() : null,
    resumen: resumenT && resumenT.querySelector('tbody tr') ? fila(resumenT.querySelector('tbody tr')) : null,
    detalle: detalleT ? [...detalleT.querySelectorAll('tbody tr')].map(fila) : [],
    pagina_actual: pag && pag.querySelector('span.actual') ? pag.querySelector('span.actual').textContent.trim() : null,
    paginas: pag ? [...pag.querySelectorAll('a')].map(a => ({texto: a.textContent.trim(), id: a.id})) : [],
  };
}"""

JS_LISTA_COMISIONES = """() => {
  const vistos = new Map();       // Map conserva el orden de la página (un objeto reordenaría las claves numéricas)
  for (const a of document.querySelectorAll('a')) {
    const m = (a.getAttribute('href') || '').match(/integrantes\\.aspx\\?prmID=(\\d+)/i);
    if (!m) continue;
    const nombre = a.textContent.replace(/\\s+/g, ' ').trim();
    if (nombre && !vistos.has(m[1])) vistos.set(m[1], nombre);
  }
  return [...vistos].map(([id, nombre]) => ({id: Number(id), nombre}));
}"""

JS_SESIONES_COMISION = """() => {
  const sel = suf => document.querySelector('select[id$=' + suf + ']');
  const mes = sel('ddlMes'), anno = sel('ddlAnnos');
  const tabla = document.querySelector('table.tabla');
  const idSesion = (a) => { const m = (a.getAttribute('href') || '').match(/prmIdSesion=(\\d+)/i); return m ? Number(m[1]) : null; };
  const filas = tabla ? [...tabla.querySelectorAll('tbody tr')].map(tr => {
    const celdas = [...tr.children].map(td => td.innerText.replace(/\\s+/g, ' ').trim());
    const links = [...tr.querySelectorAll('a')];
    const asis = links.find(a => /asistencia\\.aspx/i.test(a.getAttribute('href') || ''));
    const cualquiera = links.map(idSesion).find(x => x);
    return {celdas, sesion_id: asis ? idSesion(asis) : (cualquiera || null), tiene_asistencia: !!asis};
  }) : [];
  return {mes: mes ? mes.selectedOptions[0].text.trim() : null, anno: anno ? anno.selectedOptions[0].text.trim() : null, filas};
}"""

JS_ASISTENCIA_COMISION = """() => {
  const txt = e => (e ? e.innerText.replace(/\\s+/g, ' ').trim() : null);
  const titulos = [...document.querySelectorAll('h2,h3,h4')].map(txt);
  const idDip = a => { const m = ((a && a.getAttribute('href')) || '').match(/prmId=(\\d+)/i); return m ? Number(m[1]) : null; };
  const integrantes = [...document.querySelectorAll('article.integrante')].map(art => {
    const a = [...art.querySelectorAll('a')].find(x => /\\S/.test(x.textContent));
    const lab = art.querySelector('label');
    return {id: idDip(a), nombre: txt(a), estado: txt(lab)};
  });
  const fechaEl = [...document.querySelectorAll('p,span,div,h4')].map(txt).find(t => t && /de \\d{1,2}:\\d{2} a \\d{1,2}:\\d{2}/.test(t) && t.length < 120);
  return {titulos, fecha: fechaEl || null, integrantes};
}"""


def extraer_ficha(page) -> dict:
    return page.evaluate(JS_FICHA)


def extraer_lista_comisiones(page) -> list[dict]:
    return page.evaluate(JS_LISTA_COMISIONES)


def extraer_sesiones_comision(page) -> dict:
    return page.evaluate(JS_SESIONES_COMISION)


def extraer_asistencia_comision(page) -> dict:
    return page.evaluate(JS_ASISTENCIA_COMISION)


# ---------------------------------------------------------------------------
# Interpretación (Python puro, sin navegador)
# ---------------------------------------------------------------------------

def a_entero(t: str | None) -> int | None:
    if t is None:
        return None
    m = re.search(r"-?\d+", t.replace(".", ""))
    return int(m.group()) if m else None


def a_float_cl(t: str | None) -> float | None:
    """'101,22%' → 101.22"""
    if not t:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", t)
    return float(m.group(1).replace(",", ".")) if m else None


def normalizar_nombre(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"^(sr|sra|diputado|diputada)\.?\s+", "", s.strip(), flags=re.I)
    return re.sub(r"\s+", " ", s).upper().strip()


_RE_CABECERA = re.compile(
    r"Comunas:\s*(?P<comunas>.*?)\s*Distrito:\s*(?P<distrito>.*?)\s*Regi[oó]n:\s*(?P<region>.*?)\s*"
    r"Per[ií]odo:\s*(?P<periodo>.*?)(?:\s*Partido:\s*(?P<partido>.*?))?(?:\s*Bancada:\s*(?P<bancada>.*?))?"
    r"(?:\s*Contacto|\s*Tel[eé]fono|$)", re.I)

_RE_SESION_SALA = re.compile(
    r"Sesi[oó]n\s+(?P<num>\d+)ª?,\s*Legislatura\s+\d+ª?,\s*(?P<dia>\d{1,2})\s+(?P<mes>[A-Za-zñÑáéíóú]+)\s+(?P<anno>\d{4})"
    r"\s*-\s*de\s*(?P<ini>\d{1,2}:\d{2})\s*a\s*(?P<fin>\d{1,2}:\d{2})", re.I)


def hhmm(t: str | None) -> str | None:
    if not t:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_ficha(raw: dict, diputado_id: int) -> tuple[dict, list[dict]]:
    """Devuelve (ficha, filas_de_esta_página_del_detalle)."""
    cab = _RE_CABECERA.search(raw.get("cabecera") or "")
    g = (lambda k: (cab.group(k).strip() or None) if cab and cab.group(k) else None)
    res = raw.get("resumen") or []
    n = [a_entero(x) for x in res] + [None] * 6
    ficha = {
        "diputado_id": diputado_id,
        "distrito": g("distrito"), "region": g("region"), "comunas": g("comunas"), "periodo": g("periodo"),
        "partido": g("partido"), "bancada": g("bancada"),
        "total_sesiones": n[0], "computables": n[1], "asiste": n[2],
        "justificadas_no_afectan": n[3], "justificadas_rebajan": n[4], "sin_justificacion": n[5],
        "pct_ficha": a_float_cl(raw.get("porcentaje")),
    }
    filas = []
    for celdas in raw.get("detalle") or []:
        if len(celdas) < 3:
            continue
        m = _RE_SESION_SALA.search(celdas[0])
        if not m:
            log.warning("Fila de detalle no reconocida: %r", celdas[0][:80])
            continue
        mes = MESES_NUM.get(m.group("mes").lower())
        if not mes:
            log.warning("Mes no reconocido: %r", m.group("mes"))
            continue
        ingreso = hhmm(celdas[1]) if celdas[1].strip() not in ("", "-") else None
        filas.append({
            "diputado_id": diputado_id,
            "sesion_numero": int(m.group("num")),
            "fecha": date(int(m.group("anno")), mes, int(m.group("dia"))).isoformat(),
            "hora_inicio": hhmm(m.group("ini")), "hora_termino": hhmm(m.group("fin")),
            "hora_ingreso": ingreso,
            "asistencia": celdas[2].strip() or None,
            "observacion": (celdas[3].strip() or None) if len(celdas) > 3 else None,
        })
    return ficha, filas


def parse_sesiones_comision(raw: dict, comision_id: int) -> list[dict]:
    filas = []
    for f in raw.get("filas") or []:
        texto = " ".join(f["celdas"][:5])
        m = re.match(r"^\s*(\d+)\s+(\d{1,2})\s+([a-záéíóú]+)\.?\s+(\d{4})\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+(.+?)\s*$", texto, re.I)
        if not m or not f.get("sesion_id"):
            if f["celdas"] and any(f["celdas"]):
                log.debug("Fila de sesión sin parsear (comisión %d): %r", comision_id, texto[:80])
            continue
        mes = MESES_ABREV.get(m.group(3).lower()[:4]) or MESES_ABREV.get(m.group(3).lower()[:3])
        if not mes:
            log.warning("Mes abreviado no reconocido: %r", m.group(3))
            continue
        filas.append({
            "id": f["sesion_id"], "comision_id": comision_id, "numero": int(m.group(1)),
            "fecha": date(int(m.group(4)), mes, int(m.group(2))).isoformat(),
            "hora_inicio": hhmm(m.group(5)), "hora_termino": hhmm(m.group(6)),
            "estado": m.group(7).strip(), "tiene_asistencia": bool(f["tiene_asistencia"]),
        })
    return filas


def parse_asistencia_comision(raw: dict, sesion_id: int) -> list[dict]:
    """Un registro por titular. Los reemplazos quedan en `reemplazado_por` (el reemplazante se agrega al guardar)."""
    filas = []
    for it in raw.get("integrantes") or []:
        if not it.get("id") or not it.get("estado"):
            continue
        e = it["estado"].strip()
        mrep = re.match(r"^REEMPLAZAD[OA]\s+POR\s+(.+)$", e, re.I)
        if mrep:
            estado, asiste, just, repl = "REEMPLAZADO", 0, 0, mrep.group(1).strip()
        elif re.match(r"^ASISTE$", e, re.I):
            estado, asiste, just, repl = "ASISTE", 1, 0, None
        elif re.match(r"^JUSTIFICAD[OA]$", e, re.I):
            estado, asiste, just, repl = "JUSTIFICADO", 0, 1, None
        else:
            estado, asiste, just, repl = e.upper(), 0, 0, None     # AUSENTE u otro texto: se guarda crudo
        filas.append({"sesion_id": sesion_id, "diputado_id": it["id"], "estado": estado, "asiste": asiste,
                      "justificado": just, "reemplazado_por": repl, "reemplazante_id": None})
    return filas


def indice_nombres(conn: sqlite3.Connection) -> dict[str, int]:
    """Nombre normalizado (con y sin segundo nombre/apellido materno) → Id de diputado."""
    idx: dict[str, int] = {}
    ambiguos: set[str] = set()
    for r in conn.execute("SELECT id, nombre, nombre2, apellido_paterno, apellido_materno FROM diputado WHERE nombre != ''"):
        n1, n2, ap, am = (r["nombre"] or ""), (r["nombre2"] or ""), (r["apellido_paterno"] or ""), (r["apellido_materno"] or "")
        for clave in {f"{n1} {n2} {ap} {am}", f"{n1} {ap} {am}", f"{n1} {n2} {ap}", f"{n1} {ap}"}:
            k = normalizar_nombre(clave)
            if k in idx and idx[k] != r["id"]:
                ambiguos.add(k)
            idx[k] = r["id"]
    for k in ambiguos:
        idx.pop(k, None)
    return idx


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def guardar_ficha(conn: sqlite3.Connection, ficha: dict, filas: list[dict]) -> None:
    ts = ahora()
    upsert(conn, "ficha_diputado", [{**ficha, "capturado_en": ts}], ("diputado_id",))
    upsert(conn, "ingreso_sala", [{**f, "capturado_en": ts} for f in filas], ("diputado_id", "sesion_numero", "fecha"))


def guardar_asistencia_comision(conn: sqlite3.Connection, filas: list[dict], idx: dict[str, int] | None = None) -> int:
    """Guarda titulares y agrega un registro por cada reemplazante (resuelto por nombre si es posible).
    Devuelve cuántos reemplazantes no se pudieron resolver a un Id."""
    idx = idx if idx is not None else indice_nombres(conn)
    sin_resolver = 0
    extra = []
    for f in filas:
        if f["reemplazado_por"]:
            rid = idx.get(normalizar_nombre(f["reemplazado_por"]))
            f["reemplazante_id"] = rid
            if rid:
                extra.append({"sesion_id": f["sesion_id"], "diputado_id": rid, "estado": "REEMPLAZANTE", "asiste": 1,
                              "justificado": 0, "reemplazado_por": None, "reemplazante_id": None})
            else:
                sin_resolver += 1
                log.warning("Reemplazante sin Id: %r (sesión %d)", f["reemplazado_por"], f["sesion_id"])
    # un titular de otra comisión podría figurar como reemplazante y titular a la vez; el titular manda
    titulares = {(f["sesion_id"], f["diputado_id"]) for f in filas}
    extra = [e for e in extra if (e["sesion_id"], e["diputado_id"]) not in titulares]
    upsert(conn, "web_asistencia_comision", filas + extra, ("sesion_id", "diputado_id"))
    return sin_resolver


# ---------------------------------------------------------------------------
# Navegación (Playwright)
# ---------------------------------------------------------------------------

def pausa() -> None:
    time.sleep(PAUSA_SEG + random.uniform(0, 0.8))


def revisar_bloqueo(page, respuesta=None) -> None:
    if respuesta is not None and respuesta.status in (403, 429, 503):
        raise Bloqueado(f"El sitio respondió {respuesta.status} en {page.url}")
    titulo = (page.title() or "").lower()
    if any(s in titulo for s in ("access denied", "forbidden", "just a moment", "attention required", "blocked")):
        raise Bloqueado(f"Página de bloqueo ({page.title()!r}) en {page.url}")


def ir(page, url: str, intentos: int = 3):
    ultimo = None
    for i in range(intentos):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            revisar_bloqueo(page, resp)
            pausa()
            return resp
        except Bloqueado:
            raise
        except Exception as e:  # noqa: BLE001 — tiempo de espera, red, etc.
            ultimo = e
            espera = 2 ** (i + 1)
            log.warning("Error al abrir %s (intento %d/%d): %s — reintento en %ds", url, i + 1, intentos, e, espera)
            time.sleep(espera)
    raise RuntimeError(f"No se pudo abrir {url}: {ultimo}")


def hacer_postback(page, accion) -> None:
    """Ejecuta una acción que dispara un postback de ASP.NET (clic en paginador, cambio de selector) y espera la recarga."""
    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000) as nav:
        accion()
    revisar_bloqueo(page, nav.value)
    pausa()


def abrir_navegador(headless: bool = True):
    from playwright.sync_api import sync_playwright   # import tardío: el resto del proyecto no lo necesita
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    ctx = browser.new_context(locale="es-CL", timezone_id="America/Santiago", viewport={"width": 1366, "height": 900},
                              user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/130.0 Safari/537.36 asistencia-diputados-scraper")
    page = ctx.new_page()
    page.set_default_timeout(45000)
    return pw, browser, page


# --- Fichas -----------------------------------------------------------------

def scrapear_ficha(page, diputado_id: int, max_paginas: int | None = None) -> tuple[dict, list[dict]]:
    ir(page, URL_FICHA.format(id=diputado_id))
    raw = extraer_ficha(page)
    if not raw.get("resumen") and not raw.get("detalle"):
        raise RuntimeError(f"La ficha del diputado {diputado_id} no trae resumen ni detalle (¿cambió el HTML?)")
    ficha, filas = parse_ficha(raw, diputado_id)
    actual = a_entero(raw.get("pagina_actual")) or 1
    paginas = 1
    while max_paginas is None or paginas < max_paginas:
        siguiente = next((p for p in raw.get("paginas", []) if p["texto"] == str(actual + 1)), None)
        if not siguiente:
            break
        hacer_postback(page, lambda sid=siguiente["id"]: page.locator(f'[id="{sid}"]').click())
        raw = extraer_ficha(page)
        nuevo = a_entero(raw.get("pagina_actual")) or actual + 1
        if nuevo == actual:
            log.warning("El paginador no avanzó (diputado %d, página %d)", diputado_id, actual)
            break
        actual = nuevo
        paginas += 1
        _, mas = parse_ficha(raw, diputado_id)
        filas.extend(mas)
    return ficha, filas


def cmd_fichas(conn, page, ids: list[int] | None, limite: int | None, max_paginas: int | None) -> None:
    if not ids:
        ids = [r[0] for r in conn.execute("SELECT id FROM diputado WHERE periodo_actual = 1 ORDER BY id")]
    if limite:
        ids = ids[:limite]
    log.info("Fichas por scrapear: %d (páginas por ficha: %s)", len(ids), max_paginas or "todas")
    ok = 0
    for i, did in enumerate(ids, 1):
        try:
            ficha, filas = scrapear_ficha(page, did, max_paginas)
        except Bloqueado:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("Ficha %d: %s", did, e)
            continue
        guardar_ficha(conn, ficha, filas)
        conn.commit()
        ok += 1
        if i % 10 == 0 or i == len(ids):
            log.info("  %d/%d fichas (última: %d, %d sesiones, %s%%)", i, len(ids), did, len(filas), ficha["pct_ficha"])
    log.info("Fichas guardadas: %d de %d", ok, len(ids))


# --- Comisiones -------------------------------------------------------------

def seleccionar(page, sufijo_id: str, etiqueta: str) -> None:
    sel = page.locator(f"select[id$={sufijo_id}]")
    actual = sel.evaluate("s => s.selectedOptions[0].text.trim()")
    if actual.lower() == etiqueta.lower():
        return
    hacer_postback(page, lambda: sel.select_option(label=etiqueta))


def meses_en_rango(desde: date, hasta: date) -> list[tuple[int, int]]:
    out, (a, m) = [], (desde.year, desde.month)
    while (a, m) <= (hasta.year, hasta.month):
        out.append((a, m))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return out


def cmd_comisiones(conn, page, tipo: str, ids: list[int] | None, desde: date, hasta: date, dias_recarga: int) -> None:
    ir(page, URL_LISTAS[tipo])
    lista = extraer_lista_comisiones(page)
    log.info("Comisiones %s en la web: %d", tipo, len(lista))
    if not lista:
        raise RuntimeError("La lista de comisiones vino vacía (¿cambió el HTML?)")
    upsert(conn, "web_comision", [{"id": c["id"], "nombre": c["nombre"], "tipo": tipo, "capturado_en": ahora()} for c in lista], ("id",))
    conn.commit()
    idx = indice_nombres(conn)
    hoy = date.today()
    for c in lista:
        if ids and c["id"] not in ids:
            continue
        sesiones: list[dict] = []
        ir(page, URL_SESIONES.format(id=c["id"]))
        for anno, mes in meses_en_rango(desde, hasta):
            try:
                seleccionar(page, "ddlAnnos", str(anno))
                seleccionar(page, "ddlMes", MESES[mes - 1])
                sesiones.extend(parse_sesiones_comision(extraer_sesiones_comision(page), c["id"]))
            except Bloqueado:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("Comisión %d, %d-%02d: %s", c["id"], anno, mes, e)
        sesiones = [s for s in sesiones if desde.isoformat() <= s["fecha"] <= hasta.isoformat()]
        previas = {r["id"]: r["asistencia_cargada_en"] for r in conn.execute("SELECT id, asistencia_cargada_en FROM web_sesion_comision WHERE comision_id = ?", (c["id"],))}
        upsert(conn, "web_sesion_comision", [{k: s[k] for k in ("id", "comision_id", "numero", "fecha", "hora_inicio", "hora_termino", "estado")} for s in sesiones], ("id",))
        conn.commit()
        pendientes = []
        for s in sesiones:
            if not (s["tiene_asistencia"] and s["estado"].lower().startswith("celebrada")):
                continue
            if previas.get(s["id"]) and (hoy - date.fromisoformat(s["fecha"])).days > dias_recarga:
                continue     # ya cargada y vieja: las justificaciones ya no deberían cambiar
            pendientes.append(s)
        log.info("Comisión %d (%s): %d sesiones, %d con asistencia por descargar", c["id"], c["nombre"][:40], len(sesiones), len(pendientes))
        for s in pendientes:
            try:
                ir(page, URL_ASISTENCIA.format(comision=c["id"], sesion=s["id"]))
                filas = parse_asistencia_comision(extraer_asistencia_comision(page), s["id"])
                if not filas:
                    log.warning("Sesión %d sin integrantes (¿cambió el HTML?)", s["id"])
                    continue
                asegurar = {f["diputado_id"] for f in filas}
                ingesta.asegurar_diputados(conn, asegurar)
                guardar_asistencia_comision(conn, filas, idx)
                conn.execute("UPDATE web_sesion_comision SET asistencia_cargada_en = ? WHERE id = ?", (ahora(), s["id"]))
                conn.commit()
            except Bloqueado:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("Asistencia sesión %d: %s", s["id"], e)


def cmd_diagnostico() -> None:
    """Pide una ficha con distintas configuraciones y muestra qué responde el sitio (para entender un bloqueo).
    No intenta evadir nada: solo compara el User-Agent propio con el de Chromium por defecto."""
    from playwright.sync_api import sync_playwright
    url = URL_FICHA.format(id=815)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for nombre, ua in (("ua_propio", None), ("ua_chromium_por_defecto", "__defecto__")):
            kw = {"locale": "es-CL"}
            if ua is None:
                kw["user_agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/130.0 Safari/537.36 asistencia-diputados-scraper")
            ctx = browser.new_context(**kw)
            page = ctx.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                cab = {k: v for k, v in resp.headers.items() if k.lower() in ("server", "content-type", "x-cache", "via", "x-served-by", "cf-ray", "x-akamai-transformed")}
                cuerpo = " ".join((page.inner_text("body") or "").split())[:140]
                print(f"[{nombre}] status={resp.status} titulo={page.title()!r} cabeceras={cab} cuerpo={cuerpo!r}")
            except Exception as e:  # noqa: BLE001
                print(f"[{nombre}] error: {e}")
            ctx.close()
        browser.close()


def mostrar_estado(conn) -> None:
    for t in ("ficha_diputado", "ingreso_sala", "web_comision", "web_sesion_comision", "web_asistencia_comision"):
        print(f"{t:26s} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>8d}")
    r = conn.execute("SELECT MAX(fecha) FROM ingreso_sala").fetchone()[0]
    print(f"\nÚltima sesión con hora de ingreso: {r}")
    r = conn.execute("SELECT MAX(fecha) FROM web_sesion_comision WHERE asistencia_cargada_en IS NOT NULL").fetchone()[0]
    print(f"Última sesión de comisión con asistencia: {r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=ingesta.DB_PATH)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--visible", action="store_true", help="mostrar el navegador (para depurar en tu computador)")
    sub = p.add_subparsers(dest="comando", required=True)
    sp = sub.add_parser("fichas")
    sp.add_argument("--ids", type=int, nargs="+")
    sp.add_argument("--limite", type=int)
    sp.add_argument("--paginas", type=int, help="cuántas páginas del detalle leer por diputado (10 sesiones por página; por defecto todas)")
    sp = sub.add_parser("comisiones")
    sp.add_argument("--tipo", choices=sorted(URL_LISTAS), default="permanentes")
    sp.add_argument("--ids", type=int, nargs="+", help="Id de comisión de la web (p. ej. 4894 = Salud)")
    sp.add_argument("--desde", type=date.fromisoformat, default=INICIO_PERIODO)
    sp.add_argument("--hasta", type=date.fromisoformat, default=date.today())
    sp.add_argument("--dias-recarga", type=int, default=21, help="vuelve a bajar la asistencia de sesiones más recientes que esto")
    sub.add_parser("estado")
    sub.add_parser("diagnostico", help="compara configuraciones de navegador contra el sitio (entender bloqueos)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.comando == "diagnostico":
        cmd_diagnostico()
        return 0
    conn = abrir_db(args.db)
    if args.comando == "estado":
        mostrar_estado(conn)
        return 0
    corrida = conn.execute("INSERT INTO corrida (inicio, comando) VALUES (?, ?)", (ahora(), "scraper " + " ".join(sys.argv[1:]))).lastrowid
    conn.commit()
    ok, detalle = 1, None
    pw = browser = None
    try:
        pw, browser, page = abrir_navegador(headless=not args.visible)
        if args.comando == "fichas":
            cmd_fichas(conn, page, args.ids, args.limite, args.paginas)
        elif args.comando == "comisiones":
            cmd_comisiones(conn, page, args.tipo, args.ids, args.desde, args.hasta, args.dias_recarga)
    except Bloqueado as e:
        ok, detalle = 0, f"bloqueado: {e}"
        log.error("Sitio bloqueó el acceso: %s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        ok, detalle = 0, repr(e)
        log.exception("Corrida fallida")
        raise
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
        conn.execute("UPDATE corrida SET fin = ?, ok = ?, detalle = ? WHERE id = ?", (ahora(), ok, detalle, corrida))
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
