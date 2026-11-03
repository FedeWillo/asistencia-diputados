#!/usr/bin/env python3
"""
Ingesta de datos abiertos de la Cámara de Diputados de Chile → SQLite.

Idempotente: se puede correr todos los días; vuelve a descargar lo que puede haber
cambiado (sesiones recientes, justificaciones) y registra los cambios detectados.

Uso:
    python ingesta.py catalogos
    python ingesta.py diputados [--completar]
    python ingesta.py sala --anno 2026 [--limite 5] [--solo-nuevas]
    python ingesta.py votaciones --anno 2026 [--limite 5] [--solo-nuevas]
    python ingesta.py comisiones --periodo 10 --annos 2022 2023 2024 2025 2026
    python ingesta.py comisiones --ids 544 411 --annos 2026
    python ingesta.py diario            # catalogos + diputados + sala y votaciones del año en curso
    python ingesta.py estado            # resumen de lo cargado

Solo usa la biblioteca estándar (urllib, sqlite3, xml.etree).
"""

import argparse
import logging
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

BASE_URL = "https://opendata.camara.cl/camaradiputados/WServices/"
BASE_URL_ESPEJO = "https://opendata.congreso.cl/camaradiputados/WServices/"
NS_URI = "http://opendata.camara.cl/camaradiputados/v1"
NS = {"c": NS_URI}

RAIZ = Path(__file__).resolve().parent
DB_PATH = RAIZ / "data" / "camara.sqlite"
SCHEMA_PATH = RAIZ / "schema.sql"

# Respaldo si retornarComisionesXPeriodo falla. (El parámetro correcto es prmPeriodoLegislativoId, no prmPeriodoId.)
COMISIONES_FALLBACK = [544, 411]

log = logging.getLogger("ingesta")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def ahora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def texto(el: ET.Element | None, ruta: str | None = None) -> str | None:
    """Texto de un subelemento (con namespace) o None si no existe / está vacío."""
    if el is None:
        return None
    nodo = el.find(f"c:{ruta}", NS) if ruta else el
    if nodo is None or nodo.text is None:
        return None
    t = nodo.text.strip()
    return t or None


def entero(el: ET.Element | None, ruta: str | None = None) -> int | None:
    t = texto(el, ruta)
    return int(t) if t is not None else None


def valor(el: ET.Element | None, ruta: str) -> int | None:
    """Atributo Valor de un subelemento (p. ej. <Estado Valor="1">Celebrada</Estado>)."""
    if el is None:
        return None
    nodo = el.find(f"c:{ruta}", NS)
    if nodo is None or nodo.get("Valor") is None:
        return None
    return int(nodo.get("Valor"))


def booleano(el: ET.Element | None, ruta: str) -> int | None:
    t = texto(el, ruta)
    if t is None:
        return None
    return 1 if t.lower() == "true" else 0


def anno_de(fecha_iso: str) -> int:
    return int(fecha_iso[:4])


# ---------------------------------------------------------------------------
# HTTP con reintentos
# ---------------------------------------------------------------------------

def descargar_xml(servicio: str, metodo: str, intentos: int = 5, pausa: float = 0.3, **params) -> ET.Element:
    """GET al API con backoff exponencial y espejo de respaldo. Devuelve la raíz del XML."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    ruta = f"{servicio}.asmx/{metodo}{query}"
    ultimo_error: Exception | None = None
    for intento in range(intentos):
        base = BASE_URL if intento < intentos - 1 else BASE_URL_ESPEJO
        url = base + ruta
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "asistencia-diputados/0.1 (ingesta)"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                datos = resp.read()
            raiz = ET.fromstring(datos)
            time.sleep(pausa)  # ritmo bajo: el API tiene errores intermitentes bajo carga
            return raiz
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError) as e:
            ultimo_error = e
            espera = (2 ** intento) + random.uniform(0, 1)
            log.warning("Error en %s (intento %d/%d): %s — reintento en %.1fs", ruta, intento + 1, intentos, e, espera)
            time.sleep(espera)
    raise RuntimeError(f"No se pudo descargar {ruta}: {ultimo_error}")


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def abrir_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Migración: bases creadas antes de agregar periodo_id a sesion_comision
    if "periodo_id" not in {r[1] for r in conn.execute("PRAGMA table_info(sesion_comision)")}:
        conn.execute("ALTER TABLE sesion_comision ADD COLUMN periodo_id INTEGER")
        conn.commit()
    if "sesion_aprox" not in {r[1] for r in conn.execute("PRAGMA table_info(votacion)")}:
        conn.execute("ALTER TABLE votacion ADD COLUMN sesion_aprox INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Parsers (XML → filas). Puros: no tocan red ni base de datos. Testeables con fixtures.
# ---------------------------------------------------------------------------

def parse_justificaciones(raiz: ET.Element) -> list[dict]:
    filas = []
    for j in raiz.findall("c:JustificacionInasistencia", NS):
        filas.append({
            "valor": int(j.get("Valor")),
            "nombre": texto(j, "Nombre"),
            "rebaja_asistencia": booleano(j, "RebajaAsistencia"),
            "rebaja_quorum": booleano(j, "RebajaQuorum"),
        })
    return filas


def parse_partidos(raiz: ET.Element) -> list[dict]:
    return [
        {"id": texto(p, "Id"), "nombre": texto(p, "Nombre"), "alias": texto(p, "Alias")}  # Id es texto ("PC")
        for p in raiz.findall("c:PartidoPolitico", NS)
    ]


def parse_distritos(raiz: ET.Element) -> list[dict]:
    """retornarDistritos trae 60 distritos (incluye el sistema antiguo) con solo Numero y Comunas."""
    filas = []
    for d in raiz.findall("c:Distrito", NS):
        comunas = [texto(c, "Nombre") for c in d.findall("c:Comunas/c:Comuna", NS)]
        filas.append({
            "numero": entero(d, "Numero"),
            "comunas": ";".join(c for c in comunas if c) or None,
        })
    return filas


def parse_diputado(dip: ET.Element) -> tuple[dict, list[dict], list[dict]]:
    """Devuelve (diputado, militancias, partidos)."""
    d = {
        "id": entero(dip, "Id"),
        "nombre": texto(dip, "Nombre"),
        "nombre2": texto(dip, "Nombre2"),
        "apellido_paterno": texto(dip, "ApellidoPaterno"),
        "apellido_materno": texto(dip, "ApellidoMaterno"),
        "fecha_nacimiento": texto(dip, "FechaNacimiento"),
        "sexo": valor(dip, "Sexo"),
    }
    militancias, partidos = [], []
    for m in dip.findall("c:Militancias/c:Militancia", NS):
        partido = m.find("c:Partido", NS)
        pid = texto(partido, "Id")  # código de texto, no número
        partidos.append({"id": pid, "nombre": texto(partido, "Nombre"), "alias": texto(partido, "Alias")})
        militancias.append({
            "diputado_id": d["id"],
            "partido_id": pid,
            "fecha_inicio": texto(m, "FechaInicio"),
            "fecha_termino": texto(m, "FechaTermino"),
        })
    # Nota: las militancias NO vienen ordenadas; la actual es la de mayor FechaTermino.
    return d, militancias, partidos


def parse_diputados_periodo(raiz: ET.Element) -> list[tuple[dict, list[dict], list[dict]]]:
    # DiputadoPeriodo/FechaInicio trae 2030-03-10 por error: no se usa.
    return [parse_diputado(dp.find("c:Diputado", NS)) for dp in raiz.findall("c:DiputadoPeriodo", NS)]


def parse_sesiones_sala(raiz: ET.Element) -> list[dict]:
    filas = []
    for s in raiz.findall("c:Sesion", NS):
        fi = texto(s, "FechaInicio")
        filas.append({
            "id": entero(s, "Id"),
            "numero": entero(s, "Numero"),
            "fecha_inicio": fi,
            "fecha_termino": texto(s, "FechaTermino"),
            "tipo": valor(s, "Tipo"),
            "tipo_nombre": texto(s, "Tipo"),
            "estado": valor(s, "Estado"),
            "estado_nombre": texto(s, "Estado"),
            "anno": anno_de(fi),
        })
    return filas


def _parse_asistencia(a: ET.Element) -> dict:
    dip = a.find("c:Diputado", NS)
    just = a.find("c:Justificacion", NS)
    return {
        "diputado_id": entero(dip, "Id"),
        "asiste": valor(a, "TipoAsistencia"),
        "justificacion_valor": int(just.get("Valor")) if just is not None and just.get("Valor") else None,
        "rebaja_asistencia": booleano(just, "RebajaAsistencia") if just is not None else None,
        "rebaja_quorum": booleano(just, "RebajaQuorum") if just is not None else None,
        "titular": valor(a, "TipoTitularAsistencia"),  # solo en comisiones
    }


def parse_asistencia_sala(raiz: ET.Element) -> tuple[int, list[dict]]:
    """raíz = SesionSala. Devuelve (sesion_id, asistencias)."""
    sesion_id = entero(raiz, "Id")
    filas = [_parse_asistencia(a) for a in raiz.findall("c:ListadoAsistencia/c:Asistencia", NS)]
    return sesion_id, filas


def _parse_votacion_cabecera(v: ET.Element) -> dict:
    fecha = texto(v, "Fecha")
    return {
        "id": entero(v, "Id"),
        "descripcion": texto(v, "Descripcion"),
        "fecha": fecha,
        "total_si": entero(v, "TotalSi"),
        "total_no": entero(v, "TotalNo"),
        "total_abstencion": entero(v, "TotalAbstencion"),
        "total_dispensado": entero(v, "TotalDispensado"),
        "quorum": valor(v, "Quorum"),
        "quorum_nombre": texto(v, "Quorum"),
        "resultado": valor(v, "Resultado"),
        "resultado_nombre": texto(v, "Resultado"),
        "tipo": valor(v, "Tipo"),
        "tipo_nombre": texto(v, "Tipo"),
        "anno": anno_de(fecha),
    }


def parse_votaciones_anno(raiz: ET.Element) -> list[dict]:
    return [_parse_votacion_cabecera(v) for v in raiz.findall("c:Votacion", NS)]


def parse_votacion_detalle(raiz: ET.Element) -> tuple[dict, list[dict]]:
    """raíz = Votacion. Devuelve (cabecera, votos)."""
    cab = _parse_votacion_cabecera(raiz)
    votos = []
    for v in raiz.findall("c:Votos/c:Voto", NS):
        votos.append({
            "votacion_id": cab["id"],
            "diputado_id": entero(v.find("c:Diputado", NS), "Id"),
            "opcion": valor(v, "OpcionVoto"),
        })
    return cab, votos


def _cabecera_comision(c: ET.Element) -> dict:
    pres = c.find("c:Presidente", NS)
    return {
        "id": entero(c, "Id"),
        "nombre": texto(c, "Nombre"),
        "alias": texto(c, "Alias"),
        "tipo": valor(c, "Tipo"),
        "tipo_nombre": texto(c, "Tipo"),
        "fecha_inicio": texto(c, "FechaInicio"),
        "fecha_termino": texto(c, "FechaTermino"),
        "presidente_id": entero(pres, "Id") if pres is not None else None,
    }


def parse_lista_comisiones(raiz: ET.Element) -> list[dict]:
    """retornarComisionesXPeriodo / retornarComisionesVigentes: lista de Comision (sin integrantes ni sesiones)."""
    return [_cabecera_comision(c) for c in raiz.findall("c:Comision", NS) if entero(c, "Id") is not None]


def parse_comision(raiz: ET.Element) -> tuple[dict, list[dict], list[dict]]:
    """raíz = Comision (de retornarComision o de retornarSesionesXComisionYAnno).
    Devuelve (comision, integrantes, sesiones_con_asistencia)."""
    com = _cabecera_comision(raiz)
    integrantes = []
    for ruta, historico in (("Integrantes", 0), ("IntegrantesHistorico", 1)):
        for di in raiz.findall(f"c:{ruta}/c:DiputadoIntegrante", NS):
            integrantes.append({
                "comision_id": com["id"],
                "diputado_id": entero(di.find("c:Diputado", NS), "Id"),
                "fecha_inicio": texto(di, "FechaInicio"),
                "fecha_termino": texto(di, "FechaTermino"),
                "historico": historico,
            })
    sesiones = []
    for s in raiz.findall("c:Sesiones/c:SesionComision", NS):
        fi = texto(s, "FechaInicio")
        sesion = {
            "id": entero(s, "Id"),
            "comision_id": com["id"],
            "numero": entero(s, "Numero"),
            "fecha_inicio": fi,
            "fecha_termino": texto(s, "FechaTermino"),
            "tipo": valor(s, "Tipo"),
            "tipo_nombre": texto(s, "Tipo"),
            "estado": valor(s, "Estado"),     # 3 = Celebrada
            "estado_nombre": texto(s, "Estado"),   # 1 Citada, 2 En Comité, 3 Celebrada, 4 Suspendida, 5 Fracasada
            "anno": anno_de(fi),
            "periodo_id": entero(s.find("c:PeriodoLegislativo", NS), "Id"),
            "asistencias": [_parse_asistencia(a) for a in s.findall("c:Asistencia/c:Asistencia", NS)],
        }
        sesiones.append(sesion)
    return com, integrantes, sesiones


# ---------------------------------------------------------------------------
# Carga (filas → SQLite). Upserts idempotentes.
# ---------------------------------------------------------------------------

def upsert(conn: sqlite3.Connection, tabla: str, filas: list[dict], clave: tuple[str, ...]) -> int:
    if not filas:
        return 0
    cols = list(filas[0].keys())
    actualizables = [c for c in cols if c not in clave]
    sql = (
        f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(clave)}) DO UPDATE SET "
        + ", ".join(f"{c}=excluded.{c}" for c in actualizables)
    ) if actualizables else (
        f"INSERT OR IGNORE INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    )
    conn.executemany(sql, [tuple(f[c] for c in cols) for f in filas])
    return len(filas)


def cargar_catalogos(conn: sqlite3.Connection) -> None:
    """Catálogos de WSComun. Un catálogo caído no detiene la corrida."""
    catalogos = (
        ("retornarTiposJustificacionesInasistencia", parse_justificaciones, "justificacion_tipo", ("valor",)),
        ("retornarPartidosPoliticos", parse_partidos, "partido", ("id",)),
        ("retornarDistritos", parse_distritos, "distrito", ("numero",)),
    )
    for metodo, parser, tabla, clave in catalogos:
        try:
            n = upsert(conn, tabla, parser(descargar_xml("WSComun", metodo)), clave)
            conn.commit()
            log.info("Catálogo %s: %d filas", tabla, n)
        except (RuntimeError, ValueError, AttributeError) as e:
            log.warning("Catálogo %s no cargado: %s", tabla, e)


def cargar_diputados(conn: sqlite3.Connection) -> None:
    raiz = descargar_xml("WSDiputado", "retornarDiputadosPeriodoActual")
    registros = parse_diputados_periodo(raiz)
    conn.execute("UPDATE diputado SET periodo_actual = 0")
    for d, militancias, partidos in registros:
        upsert(conn, "partido", partidos, ("id",))
        d["periodo_actual"] = 1
        upsert(conn, "diputado", [d], ("id",))
        upsert(conn, "militancia", militancias, ("diputado_id", "partido_id", "fecha_inicio"))
    conn.commit()
    log.info("Diputados del período actual: %d", len(registros))


def completar_diputados(conn: sqlite3.Connection) -> int:
    """Completa con retornarDiputado los Id que solo existen como fila mínima (nombre vacío): diputados de
    períodos anteriores que aparecen en asistencias, votos o comisiones pero no en retornarDiputadosPeriodoActual."""
    ids = [r[0] for r in conn.execute("SELECT id FROM diputado WHERE nombre IS NULL OR nombre = '' ORDER BY id")]
    log.info("Diputados sin nombre por completar: %d", len(ids))
    hechos = 0
    for i, did in enumerate(ids, 1):
        try:
            d, militancias, partidos = parse_diputado(descargar_xml("WSDiputado", "retornarDiputado", prmDiputadoId=did))
        except RuntimeError as e:
            log.error("Diputado %d: %s", did, e)
            continue
        if d["id"] is None:
            log.warning("Diputado %d: el API devuelve vacío", did)
            continue
        upsert(conn, "partido", partidos, ("id",))
        upsert(conn, "diputado", [d], ("id",))   # no toca periodo_actual
        upsert(conn, "militancia", militancias, ("diputado_id", "partido_id", "fecha_inicio"))
        hechos += 1
        if i % 50 == 0:
            conn.commit()
            log.info("  %d/%d", i, len(ids))
    conn.commit()
    log.info("Diputados completados: %d de %d", hechos, len(ids))
    return hechos


def asegurar_diputados(conn: sqlite3.Connection, ids: set[int]) -> None:
    """Crea filas mínimas para diputados que aparecen en asistencias/votos pero no en el período actual
    (histórico). Se completan después con retornarDiputado si hace falta."""
    existentes = {r[0] for r in conn.execute("SELECT id FROM diputado")}
    faltantes = ids - existentes
    if faltantes:
        conn.executemany(
            "INSERT OR IGNORE INTO diputado (id, nombre, apellido_paterno) VALUES (?, '', '')",
            [(i,) for i in faltantes],
        )


def registrar_cambios_asistencia(conn: sqlite3.Connection, sesion_id: int, nuevas: list[dict]) -> int:
    """Compara la asistencia nueva con la guardada y anota diferencias (cambios de justificación, etc.)."""
    previas = {
        r["diputado_id"]: (r["asiste"], r["justificacion_valor"])
        for r in conn.execute("SELECT diputado_id, asiste, justificacion_valor FROM asistencia_sala WHERE sesion_id = ?", (sesion_id,))
    }
    if not previas:
        return 0
    cambios = []
    ts = ahora()
    for a in nuevas:
        antes = previas.get(a["diputado_id"])
        despues = (a["asiste"], a["justificacion_valor"])
        if antes is not None and antes != despues:
            cambios.append((ts, sesion_id, a["diputado_id"], antes[0], antes[1], despues[0], despues[1]))
    if cambios:
        conn.executemany(
            "INSERT INTO asistencia_sala_cambio (detectado_en, sesion_id, diputado_id, asiste_antes, "
            "justificacion_antes, asiste_despues, justificacion_despues) VALUES (?,?,?,?,?,?,?)",
            cambios,
        )
    return len(cambios)


def cargar_sala(conn: sqlite3.Connection, anno: int, limite: int | None = None, solo_nuevas: bool = False,
                dias_recarga: int = 45) -> None:
    """Sesiones del año + asistencia por sesión.
    Por defecto recarga la asistencia de las sesiones de los últimos `dias_recarga` días (las
    justificaciones se cargan a mano con rezago). Con --solo-nuevas, solo sesiones sin asistencia."""
    sesiones = parse_sesiones_sala(descargar_xml("WSSala", "retornarSesionesXAnno", prmAnno=anno))
    upsert(conn, "sesion_sala", sesiones, ("id",))  # no toca asistencia_cargada_en
    conn.commit()
    log.info("Sala %d: %d sesiones", anno, len(sesiones))

    hoy = date.today().isoformat()
    pendientes = []
    for r in conn.execute("SELECT id, fecha_inicio, estado, asistencia_cargada_en FROM sesion_sala WHERE anno = ? ORDER BY fecha_inicio", (anno,)):
        if r["estado"] != 1:
            continue  # solo celebradas
        if r["asistencia_cargada_en"] is None:
            pendientes.append(r["id"])
        elif not solo_nuevas and (date.fromisoformat(hoy) - date.fromisoformat(r["fecha_inicio"][:10])).days <= dias_recarga:
            pendientes.append(r["id"])
    if limite:
        pendientes = pendientes[:limite]
    log.info("Sala %d: %d sesiones por descargar asistencia", anno, len(pendientes))

    total_cambios = 0
    for sid in pendientes:
        sesion_id, asistencias = parse_asistencia_sala(descargar_xml("WSSala", "retornarSesionAsistencia", prmSesionID=sid))
        if not asistencias:
            log.warning("Sesión %d sin registros de asistencia", sid)
            continue
        asegurar_diputados(conn, {a["diputado_id"] for a in asistencias})
        total_cambios += registrar_cambios_asistencia(conn, sid, asistencias)
        filas = [{"sesion_id": sid, **{k: a[k] for k in ("diputado_id", "asiste", "justificacion_valor", "rebaja_asistencia", "rebaja_quorum")}} for a in asistencias]
        upsert(conn, "asistencia_sala", filas, ("sesion_id", "diputado_id"))
        conn.execute("UPDATE sesion_sala SET asistencia_cargada_en = ? WHERE id = ?", (ahora(), sid))
        conn.commit()
        log.debug("Sesión %d: %d asistencias", sid, len(filas))
    log.info("Sala %d: asistencia cargada para %d sesiones; %d cambios detectados", anno, len(pendientes), total_cambios)


# Las votaciones finales se registran unos segundos después del FechaTermino de la sesión (1–12 s observados el
# 2-nov-2026) y alguna justo antes del FechaInicio (50 s). Se asignan a la sesión celebrada más cercana dentro de esta tolerancia.
TOLERANCIA_ANTES_SEG = 120
TOLERANCIA_DESPUES_SEG = 60


def unir_votaciones_con_sesiones(conn: sqlite3.Connection, anno: int) -> int:
    """Asigna sesion_id a cada votación. 1) Fecha dentro de FechaInicio–FechaTermino (exacto);
    2) las que quedan, a la sesión celebrada más cercana dentro de la tolerancia (sesion_aprox = 1)."""
    antes = conn.execute("SELECT COUNT(*) FROM votacion WHERE anno = ? AND sesion_id IS NOT NULL", (anno,)).fetchone()[0]
    conn.execute(
        """
        UPDATE votacion SET sesion_id = (
            SELECT s.id FROM sesion_sala s
            WHERE votacion.fecha >= s.fecha_inicio AND votacion.fecha <= COALESCE(s.fecha_termino, s.fecha_inicio || 'T23:59:59')
            ORDER BY s.fecha_inicio DESC LIMIT 1
        )
        WHERE anno = ? AND sesion_id IS NULL
        """,
        (anno,),
    )
    # Segunda pasada en Python (portable entre versiones de SQLite): sesión celebrada más cercana dentro de la tolerancia.
    sesiones = [(r["id"], datetime.fromisoformat(r["fecha_inicio"]),
                 datetime.fromisoformat(r["fecha_termino"] or r["fecha_inicio"]))
                for r in conn.execute("SELECT id, fecha_inicio, fecha_termino FROM sesion_sala WHERE estado = 1 AND anno = ?", (anno,))]
    for r in conn.execute("SELECT id, fecha FROM votacion WHERE anno = ? AND sesion_id IS NULL", (anno,)).fetchall():
        t = datetime.fromisoformat(r["fecha"])
        mejor, dist_mejor = None, None
        for sid, ini_s, fin_s in sesiones:
            if t < ini_s:
                dist, ok = (ini_s - t).total_seconds(), (ini_s - t).total_seconds() <= TOLERANCIA_ANTES_SEG
            elif t > fin_s:
                dist, ok = (t - fin_s).total_seconds(), (t - fin_s).total_seconds() <= TOLERANCIA_DESPUES_SEG
            else:
                dist, ok = 0.0, True
            if ok and (dist_mejor is None or dist < dist_mejor):
                mejor, dist_mejor = sid, dist
        if mejor is not None:
            conn.execute("UPDATE votacion SET sesion_id = ?, sesion_aprox = 1 WHERE id = ?", (mejor, r["id"]))
    conn.commit()
    despues = conn.execute("SELECT COUNT(*) FROM votacion WHERE anno = ? AND sesion_id IS NOT NULL", (anno,)).fetchone()[0]
    return despues - antes


def cargar_votaciones(conn: sqlite3.Connection, anno: int, limite: int | None = None, solo_nuevas: bool = True) -> None:
    """Cabeceras del año + detalle (voto por diputado) de las que no tienen detalle."""
    cabeceras = parse_votaciones_anno(descargar_xml("WSLegislativo", "retornarVotacionesXAnno", prmAnno=anno))
    upsert(conn, "votacion", cabeceras, ("id",))
    conn.commit()
    log.info("Votaciones %d: %d cabeceras", anno, len(cabeceras))

    sql = "SELECT id FROM votacion WHERE anno = ?" + (" AND detalle_cargado_en IS NULL" if solo_nuevas else "") + " ORDER BY fecha"
    pendientes = [r["id"] for r in conn.execute(sql, (anno,))]
    if limite:
        pendientes = pendientes[:limite]
    log.info("Votaciones %d: %d detalles por descargar", anno, len(pendientes))
    for vid in pendientes:
        cab, votos = parse_votacion_detalle(descargar_xml("WSLegislativo", "retornarVotacionDetalle", prmVotacionId=vid))
        asegurar_diputados(conn, {v["diputado_id"] for v in votos})
        upsert(conn, "voto", votos, ("votacion_id", "diputado_id"))
        conn.execute("UPDATE votacion SET detalle_cargado_en = ? WHERE id = ?", (ahora(), vid))
        conn.commit()
    unidas = unir_votaciones_con_sesiones(conn, anno)
    sin_sesion = conn.execute("SELECT COUNT(*) FROM votacion WHERE anno = ? AND sesion_id IS NULL", (anno,)).fetchone()[0]
    log.info("Votaciones %d: %d unidas a sesión en esta corrida; %d sin sesión", anno, unidas, sin_sesion)


def cargar_comisiones(conn: sqlite3.Connection, ids: list[int], annos: list[int]) -> None:
    """Integrantes (retornarComision) + sesiones con asistencia (retornarSesionesXComisionYAnno) por año."""
    for cid in ids:
        try:
            com, integrantes, _ = parse_comision(descargar_xml("WSComision", "retornarComision", prmComisionId=cid))
            if com["id"] is None:
                log.warning("Comisión %d: el API devuelve vacío (¿Id sin publicar en datos abiertos?)", cid)
                continue
            upsert(conn, "comision", [com], ("id",))
            asegurar_diputados(conn, {i["diputado_id"] for i in integrantes})
            upsert(conn, "integrante_comision", integrantes, ("comision_id", "diputado_id", "fecha_inicio"))
        except RuntimeError as e:
            log.error("Comisión %d (integrantes): %s", cid, e)
        for anno in annos:
            try:
                raiz = descargar_xml("WSComision", "retornarSesionesXComisionYAnno", prmComisionId=cid, prmAnno=anno)
            except RuntimeError as e:
                log.error("Comisión %d año %d: %s", cid, anno, e)
                continue
            com, _, sesiones = parse_comision(raiz)
            if com["id"] is None:
                continue
            upsert(conn, "comision", [com], ("id",))
            fuera = [x for x in sesiones if x["anno"] != anno]
            if fuera:
                log.warning("Comisión %d: %d sesiones con año distinto de %d descartadas (fechas %s)", cid, len(fuera), anno,
                            ", ".join(x["fecha_inicio"][:10] for x in fuera[:3]))
            sesiones = [x for x in sesiones if x["anno"] == anno]
            for s in sesiones:
                asistencias = s.pop("asistencias")
                upsert(conn, "sesion_comision", [s], ("id",))
                asegurar_diputados(conn, {a["diputado_id"] for a in asistencias})
                filas = [{"sesion_id": s["id"], "diputado_id": a["diputado_id"], "asiste": a["asiste"],
                          "justificacion_valor": a["justificacion_valor"], "titular": a["titular"]} for a in asistencias]
                upsert(conn, "asistencia_comision", filas, ("sesion_id", "diputado_id"))
            conn.commit()
            log.info("Comisión %d (%s) %d: %d sesiones", cid, com["nombre"], anno, len(sesiones))


def cargar_comisiones_periodo(conn: sqlite3.Connection, periodo_id: int, annos: list[int]) -> None:
    """Todas las comisiones que el API lista para un período legislativo (11 = 2026-2030, 10 = 2022-2026)."""
    lista = parse_lista_comisiones(descargar_xml("WSComision", "retornarComisionesXPeriodo", prmPeriodoLegislativoId=periodo_id))
    upsert(conn, "comision", lista, ("id",))
    conn.commit()
    log.info("Período %d: el API lista %d comisiones", periodo_id, len(lista))
    if periodo_id == 11 and len(lista) <= 2:
        log.warning("El API aún no publica las comisiones del período 2026-2030 (solo %d, de 2022); "
                    "camara.cl lista 27 permanentes con Id nuevos (p. ej. 4894 Salud) que el API devuelve vacíos.", len(lista))
    cargar_comisiones(conn, [c["id"] for c in lista] or COMISIONES_FALLBACK, annos)


def mostrar_estado(conn: sqlite3.Connection) -> None:
    tablas = ["justificacion_tipo", "partido", "distrito", "diputado", "militancia", "sesion_sala", "asistencia_sala",
              "asistencia_sala_cambio", "votacion", "voto", "comision", "integrante_comision", "sesion_comision",
              "asistencia_comision", "corrida"]
    for t in tablas:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:26s} {n:>8d}")
    r = conn.execute("SELECT MAX(fecha_inicio) FROM sesion_sala WHERE estado = 1").fetchone()[0]
    print(f"\nÚltima sesión de Sala celebrada: {r}")
    r = conn.execute("SELECT MAX(fecha) FROM votacion").fetchone()[0]
    print(f"Última votación:                 {r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="comando", required=True)

    sub.add_parser("catalogos")
    sp = sub.add_parser("diputados")
    sp.add_argument("--completar", action="store_true", help="solo completar nombres de diputados de períodos anteriores")
    sp = sub.add_parser("sala")
    sp.add_argument("--anno", type=int, default=date.today().year)
    sp.add_argument("--limite", type=int)
    sp.add_argument("--solo-nuevas", action="store_true")
    sp = sub.add_parser("votaciones")
    sp.add_argument("--anno", type=int, default=date.today().year)
    sp.add_argument("--limite", type=int)
    sp.add_argument("--todas", action="store_true", help="recargar también las que ya tienen detalle")
    sp = sub.add_parser("comisiones")
    sp.add_argument("--periodo", type=int, help="cargar todas las comisiones del período legislativo (10 = 2022-2026, 11 = 2026-2030)")
    sp.add_argument("--ids", type=int, nargs="+", help="o solo estas comisiones")
    sp.add_argument("--annos", type=int, nargs="+", default=[date.today().year])
    sp = sub.add_parser("diario")
    sp.add_argument("--anno", type=int, default=date.today().year)
    sub.add_parser("estado")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    conn = abrir_db(args.db)
    if args.comando == "estado":
        mostrar_estado(conn)
        return 0

    corrida_id = conn.execute("INSERT INTO corrida (inicio, comando) VALUES (?, ?)", (ahora(), " ".join(sys.argv[1:]))).lastrowid
    conn.commit()
    ok, detalle = 1, None
    try:
        if args.comando == "catalogos":
            cargar_catalogos(conn)
        elif args.comando == "diputados":
            if args.completar:
                completar_diputados(conn)
            else:
                cargar_diputados(conn)
                completar_diputados(conn)
        elif args.comando == "sala":
            cargar_sala(conn, args.anno, args.limite, args.solo_nuevas)
        elif args.comando == "votaciones":
            cargar_votaciones(conn, args.anno, args.limite, solo_nuevas=not args.todas)
        elif args.comando == "comisiones":
            if args.periodo:
                cargar_comisiones_periodo(conn, args.periodo, args.annos)
            else:
                cargar_comisiones(conn, args.ids or COMISIONES_FALLBACK, args.annos)
        elif args.comando == "diario":
            cargar_catalogos(conn)
            cargar_diputados(conn)
            cargar_sala(conn, args.anno)
            cargar_votaciones(conn, args.anno)
            cargar_comisiones_periodo(conn, 11, [args.anno])
            completar_diputados(conn)
    except Exception as e:  # noqa: BLE001 — registrar y propagar
        ok, detalle = 0, repr(e)
        log.exception("Corrida fallida")
        raise
    finally:
        conn.execute("UPDATE corrida SET fin = ?, ok = ?, detalle = ? WHERE id = ?", (ahora(), ok, detalle, corrida_id))
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
