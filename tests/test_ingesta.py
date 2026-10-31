"""Tests de parsers y carga con fixtures que replican la estructura real del API (verificada 31-10-2026).
Correr: python -m unittest discover -s tests -v
"""

import sqlite3
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import ingesta  # noqa: E402

FIX = RAIZ / "tests" / "fixtures"


def fixture(nombre: str) -> ET.Element:
    return ET.parse(FIX / nombre).getroot()


class TestParsers(unittest.TestCase):
    def test_justificaciones(self):
        filas = ingesta.parse_justificaciones(fixture("justificaciones.xml"))
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0], {"valor": 10, "nombre": "Misión oficial con Permiso Constitucional (Art. 37)",
                                    "rebaja_asistencia": 1, "rebaja_quorum": 1})
        self.assertEqual(filas[2]["rebaja_asistencia"], 0)

    def test_sesiones_sala(self):
        filas = ingesta.parse_sesiones_sala(fixture("sesiones_sala.xml"))
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0]["id"], 4736)
        self.assertEqual(filas[0]["estado"], 1)
        self.assertEqual(filas[2]["estado"], 0)  # Citada
        self.assertEqual(filas[0]["anno"], 2026)

    def test_asistencia_sala(self):
        sid, filas = ingesta.parse_asistencia_sala(fixture("asistencia_sala.xml"))
        self.assertEqual(sid, 4837)
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0]["asiste"], 1)
        self.assertIsNone(filas[0]["justificacion_valor"])
        self.assertEqual(filas[1]["asiste"], 0)
        self.assertEqual(filas[1]["justificacion_valor"], 18)
        self.assertEqual(filas[1]["rebaja_asistencia"], 1)
        self.assertEqual(filas[1]["rebaja_quorum"], 0)
        self.assertEqual(filas[2]["asiste"], 0)
        self.assertIsNone(filas[2]["justificacion_valor"])  # injustificada

    def test_diputados_y_militancia_actual(self):
        regs = ingesta.parse_diputados_periodo(fixture("diputados_periodo.xml"))
        self.assertEqual(len(regs), 1)
        d, mil, partidos = regs[0]
        self.assertEqual(d["id"], 1074)
        self.assertEqual(d["sexo"], 2)
        self.assertEqual(len(mil), 3)
        actual = max(mil, key=lambda m: m["fecha_termino"])
        self.assertEqual(actual["partido_id"], "IND")
        self.assertEqual(actual["fecha_inicio"], "2026-03-11T00:00:00")

    def test_votaciones_anno(self):
        filas = ingesta.parse_votaciones_anno(fixture("votaciones_anno.xml"))
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0]["id"], 90328)
        self.assertEqual(filas[0]["total_si"], 111)
        self.assertEqual(filas[0]["quorum"], 1)
        self.assertEqual(filas[0]["resultado_nombre"], "Aprobado")

    def test_votacion_detalle(self):
        cab, votos = ingesta.parse_votacion_detalle(fixture("votacion_detalle.xml"))
        self.assertEqual(cab["id"], 90328)
        self.assertEqual([v["opcion"] for v in votos], [0, 1, 4])
        self.assertEqual(votos[2]["diputado_id"], 1012)

    def test_comision(self):
        com, integrantes, sesiones = ingesta.parse_comision(fixture("comision_sesiones.xml"))
        self.assertEqual(com["id"], 544)
        self.assertEqual(com["presidente_id"], 803)
        self.assertEqual(len(integrantes), 2)
        self.assertEqual(sum(i["historico"] for i in integrantes), 1)
        self.assertEqual(len(sesiones), 1)
        s = sesiones[0]
        self.assertEqual(s["estado"], 3)
        self.assertEqual(s["comision_id"], 544)
        self.assertEqual(len(s["asistencias"]), 3)
        self.assertEqual(s["asistencias"][1]["titular"], 2)
        self.assertEqual(s["asistencias"][2]["justificacion_valor"], 19)


class TestCarga(unittest.TestCase):
    """Carga de punta a punta con el API simulado por fixtures."""

    RUTAS = {
        ("WSComun", "retornarTiposJustificacionesInasistencia"): "justificaciones.xml",
        ("WSComun", "retornarPartidosPoliticos"): "partidos.xml",
        ("WSComun", "retornarDistritos"): "distritos.xml",
        ("WSSala", "retornarSesionesXAnno"): "sesiones_sala.xml",
        ("WSSala", "retornarSesionAsistencia"): "asistencia_sala.xml",
        ("WSDiputado", "retornarDiputadosPeriodoActual"): "diputados_periodo.xml",
        ("WSLegislativo", "retornarVotacionesXAnno"): "votaciones_anno.xml",
        ("WSLegislativo", "retornarVotacionDetalle"): "votacion_detalle.xml",
        ("WSComision", "retornarSesionesXComisionYAnno"): "comision_sesiones.xml",
    }

    def falso_descargar(self, servicio, metodo, **params):
        if (servicio, metodo) in self.RUTAS:
            return fixture(self.RUTAS[(servicio, metodo)])
        raise RuntimeError(f"sin fixture para {servicio}/{metodo}")

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((RAIZ / "schema.sql").read_text(encoding="utf-8"))
        self.parche = mock.patch.object(ingesta, "descargar_xml", side_effect=self.falso_descargar)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        self.conn.close()

    def n(self, tabla):
        return self.conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]

    def test_catalogos(self):
        ingesta.cargar_catalogos(self.conn)
        self.assertEqual(self.n("justificacion_tipo"), 3)
        self.assertEqual(self.n("partido"), 2)
        r = self.conn.execute("SELECT comunas FROM distrito WHERE numero = 10").fetchone()
        self.assertEqual(r["comunas"], "Providencia;Ñuñoa")

    def test_diputados(self):
        ingesta.cargar_diputados(self.conn)
        self.assertEqual(self.n("diputado"), 1)
        self.assertEqual(self.n("militancia"), 3)
        self.assertEqual(self.n("partido"), 2)
        ingesta.cargar_diputados(self.conn)  # idempotente
        self.assertEqual(self.n("militancia"), 3)

    def test_sala_idempotente_y_detecta_cambios(self):
        ingesta.cargar_sala(self.conn, 2026)
        self.assertEqual(self.n("sesion_sala"), 3)
        # 2 celebradas (fixture devuelve la misma asistencia para ambas) → 6 filas
        self.assertEqual(self.n("asistencia_sala"), 6)
        self.assertEqual(self.n("asistencia_sala_cambio"), 0)
        # Simular que la secretaría cargó una justificación después
        self.conn.execute("UPDATE asistencia_sala SET justificacion_valor = NULL WHERE diputado_id = 1012")
        self.conn.commit()
        ingesta.cargar_sala(self.conn, 2026, dias_recarga=100000)
        self.assertEqual(self.n("asistencia_sala"), 6)
        self.assertEqual(self.n("asistencia_sala_cambio"), 2)
        cambio = self.conn.execute("SELECT * FROM asistencia_sala_cambio LIMIT 1").fetchone()
        self.assertIsNone(cambio["justificacion_antes"])
        self.assertEqual(cambio["justificacion_despues"], 18)

    def test_solo_nuevas_no_recarga(self):
        ingesta.cargar_sala(self.conn, 2026)
        with mock.patch.object(ingesta, "parse_asistencia_sala", wraps=ingesta.parse_asistencia_sala) as p:
            ingesta.cargar_sala(self.conn, 2026, solo_nuevas=True)
            p.assert_not_called()

    def test_votaciones_y_union_con_sesion(self):
        ingesta.cargar_sala(self.conn, 2026)
        ingesta.cargar_votaciones(self.conn, 2026)
        self.assertEqual(self.n("votacion"), 2)
        self.assertEqual(self.n("voto"), 3)  # el fixture de detalle siempre es la 90328 → 3 votos únicos
        filas = {r["id"]: r["sesion_id"] for r in self.conn.execute("SELECT id, sesion_id FROM votacion")}
        self.assertEqual(filas[90328], 4837)   # 13:18:43 cae dentro de 10:00–13:58:10
        self.assertIsNone(filas[90001])        # huérfana
        # Segunda corrida no vuelve a bajar detalle
        with mock.patch.object(ingesta, "parse_votacion_detalle", wraps=ingesta.parse_votacion_detalle) as p:
            ingesta.cargar_votaciones(self.conn, 2026)
            p.assert_not_called()

    def test_comisiones(self):
        ingesta.cargar_comisiones(self.conn, [544], 2026)
        self.assertEqual(self.n("comision"), 1)
        self.assertEqual(self.n("integrante_comision"), 2)
        self.assertEqual(self.n("sesion_comision"), 1)
        self.assertEqual(self.n("asistencia_comision"), 3)
        r = self.conn.execute("SELECT titular FROM asistencia_comision WHERE diputado_id = 815").fetchone()
        self.assertEqual(r["titular"], 2)


if __name__ == "__main__":
    unittest.main()
