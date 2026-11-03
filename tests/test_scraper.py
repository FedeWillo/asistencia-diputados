"""Tests del scraper con HTML local (fixtures en tests/fixtures/web). No tocan la red.
Los extractores corren en Chromium vía Playwright; si no está instalado, esas pruebas se saltan.
Correr: python -m unittest discover -s tests -v
"""

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import scraper  # noqa: E402

FIX = RAIZ / "tests" / "fixtures" / "web"

try:
    from playwright.sync_api import sync_playwright
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch()
    _browser.close()
    _pw.stop()
    HAY_NAVEGADOR = True
except Exception:  # noqa: BLE001 — sin playwright o sin chromium
    HAY_NAVEGADOR = False


def conexion() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((RAIZ / "schema.sql").read_text(encoding="utf-8"))
    return c


@unittest.skipUnless(HAY_NAVEGADOR, "Playwright/Chromium no disponible")
class TestExtractores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def cargar(self, nombre):
        self.page.set_content((FIX / nombre).read_text(encoding="utf-8"))

    def test_ficha_pagina_1(self):
        self.cargar("ficha_p1.html")
        raw = scraper.extraer_ficha(self.page)
        self.assertEqual(raw["nombre"], "Diputado Sergio Bobadilla Muñoz")
        self.assertEqual(raw["pagina_actual"], "1")
        self.assertEqual([p["texto"] for p in raw["paginas"]], ["2", "3"])
        ficha, filas = scraper.parse_ficha(raw, 815)
        self.assertEqual(ficha["distrito"], "Nº 20")
        self.assertEqual(ficha["region"], "Región del Bío Bío")
        self.assertEqual(ficha["periodo"], "2026-2030")
        self.assertEqual(ficha["partido"], "Unión Demócrata Independiente")
        self.assertEqual(ficha["bancada"], "Unión Demócrata Independiente")
        self.assertTrue(ficha["comunas"].startswith("Hualpén, Hualqui"))
        self.assertEqual((ficha["total_sesiones"], ficha["computables"], ficha["asiste"]), (87, 87, 83))
        self.assertEqual((ficha["justificadas_no_afectan"], ficha["justificadas_rebajan"], ficha["sin_justificacion"]), (5, 0, 0))
        self.assertEqual(ficha["pct_ficha"], 101.22)
        self.assertEqual(len(filas), 3)
        self.assertEqual(filas[0]["sesion_numero"], 86)
        self.assertEqual(filas[0]["fecha"], "2026-11-02")
        self.assertEqual((filas[0]["hora_inicio"], filas[0]["hora_termino"], filas[0]["hora_ingreso"]), ("17:00", "19:16", "17:07"))
        self.assertEqual(filas[1]["asistencia"], "Ausente")
        self.assertIsNone(filas[1]["hora_ingreso"])
        self.assertEqual(filas[1]["observacion"], "Misión oficial con aviso de salida del país (Art. 37)")   # sin el enlace "ver"
        self.assertNotIn("ver", filas[1]["observacion"].split()[-1:])

    def test_ficha_ultima_pagina(self):
        self.cargar("ficha_p9.html")
        raw = scraper.extraer_ficha(self.page)
        self.assertEqual(raw["pagina_actual"], "9")
        _, filas = scraper.parse_ficha(raw, 815)
        self.assertEqual([(f["sesion_numero"], f["fecha"]) for f in filas], [(1, "2026-03-17"), (0, "2026-03-11")])
        self.assertIsNone(filas[1]["hora_ingreso"])    # la sesión 0ª no tiene marca

    def test_lista_comisiones(self):
        self.cargar("lista_comisiones.html")
        lista = scraper.extraer_lista_comisiones(self.page)
        self.assertEqual([c["id"] for c in lista], [4897, 4889, 4894])
        self.assertEqual(lista[2]["nombre"], "Salud")      # nombre vacío del enlace con imagen se ignora; gana el que tiene texto
        self.assertTrue(lista[0]["nombre"].startswith("Gobierno Interior"))

    def test_sesiones_comision(self):
        self.cargar("sesiones_comision.html")
        raw = scraper.extraer_sesiones_comision(self.page)
        self.assertEqual((raw["mes"], raw["anno"]), ("Octubre", "2026"))
        filas = scraper.parse_sesiones_comision(raw, 4894)
        self.assertEqual([(f["numero"], f["fecha"], f["estado"], f["tiene_asistencia"]) for f in filas],
                         [(25, "2026-10-27", "Suspendida", False), (24, "2026-10-20", "Celebrada", True), (23, "2026-10-06", "Celebrada", True)])
        self.assertEqual([f["id"] for f in filas], [82300, 82296, 82190])
        self.assertEqual((filas[1]["hora_inicio"], filas[1]["hora_termino"]), ("17:35", "18:55"))

    def test_asistencia_comision(self):
        self.cargar("asistencia_comision.html")
        raw = scraper.extraer_asistencia_comision(self.page)
        self.assertEqual(len(raw["integrantes"]), 6)
        self.assertIn("17:35 a 18:55", raw["fecha"])
        self.assertTrue(any("Asistencia Sesión N°24" in t for t in raw["titulos"]))
        filas = scraper.parse_asistencia_comision(raw, 82296)
        por_id = {f["diputado_id"]: f for f in filas}
        self.assertEqual((por_id[1102]["estado"], por_id[1102]["asiste"]), ("ASISTE", 1))
        self.assertEqual((por_id[1088]["estado"], por_id[1088]["asiste"], por_id[1088]["justificado"]), ("JUSTIFICADO", 0, 1))
        self.assertEqual((por_id[1107]["estado"], por_id[1107]["asiste"]), ("REEMPLAZADO", 0))
        self.assertEqual(por_id[1107]["reemplazado_por"], "Sra. CONSUELO VELOSO ÁVILA")
        self.assertEqual((por_id[1200]["estado"], por_id[1200]["asiste"]), ("AUSENTE", 0))      # estado desconocido: se guarda crudo


class TestPuros(unittest.TestCase):
    def test_numeros(self):
        self.assertEqual(scraper.a_float_cl("Porcentaje de Asistencia 101,22%"), 101.22)
        self.assertEqual(scraper.a_float_cl("Porcentaje de Asistencia 200%"), 200.0)
        self.assertIsNone(scraper.a_float_cl(None))
        self.assertEqual(scraper.a_entero("87"), 87)

    def test_nombres(self):
        self.assertEqual(scraper.normalizar_nombre("Sra. CONSUELO VELOSO ÁVILA"), "CONSUELO VELOSO AVILA")
        self.assertEqual(scraper.normalizar_nombre("Sr.  María   Pérez"), "MARIA PEREZ")

    def test_meses_en_rango(self):
        self.assertEqual(scraper.meses_en_rango(date(2026, 11, 20), date(2027, 2, 1)), [(2026, 11), (2026, 12), (2027, 1), (2027, 2)])
        self.assertEqual(len(scraper.meses_en_rango(date(2026, 3, 11), date(2026, 11, 3))), 9)

    def test_opciones_comunes_en_cualquier_posicion(self):
        p = scraper.crear_parser()
        for argv in (["fichas", "--limite", "3", "--visible"], ["--visible", "fichas", "--limite", "3"]):
            a = p.parse_args(argv)
            self.assertTrue(a.visible)
            self.assertEqual(a.limite, 3)
        self.assertFalse(p.parse_args(["estado"]).visible)
        self.assertTrue(p.parse_args(["diagnostico", "--visible"]).visible)
        self.assertEqual(p.parse_args(["comisiones", "--ids", "4894"]).ids, [4894])

    def test_parse_ficha_sin_datos_no_revienta(self):
        ficha, filas = scraper.parse_ficha({"cabecera": None, "resumen": None, "detalle": [["fila rara", "-", "Asiste"]]}, 1)
        self.assertIsNone(ficha["distrito"])
        self.assertEqual(filas, [])


class TestPersistencia(unittest.TestCase):
    def test_ficha_e_ingreso_y_puntualidad(self):
        c = conexion()
        raw_ficha = {"cabecera": "Comunas: A, B Distrito: Nº 3 Región: Región X Período: 2026-2030 Partido: P Bancada: B Contacto Teléfono",
                     "resumen": ["87", "87", "83", "5", "0", "0"], "porcentaje": "Porcentaje de Asistencia 101,22%",
                     "detalle": [["Sesión 86ª, Legislatura 374ª, 2 Noviembre 2026 - de 17:00 a 19:16", "17:07", "Asiste", ""],
                                 ["Sesión 85ª, Legislatura 374ª, 28 Octubre 2026 - de 15:03 a 17:14", "-", "Ausente", "Impedimento grave (Art. 42)"],
                                 ["Sesión 84ª, Legislatura 374ª, 28 Octubre 2026 - de 10:04 a 14:05", "10:04", "Asiste", ""]]}
        ficha, filas = scraper.parse_ficha(raw_ficha, 815)
        scraper.guardar_ficha(c, ficha, filas)
        scraper.guardar_ficha(c, ficha, filas)      # idempotente
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ingreso_sala").fetchone()[0], 3)
        f = c.execute("SELECT * FROM ficha_diputado WHERE diputado_id = 815").fetchone()
        self.assertEqual((f["distrito"], f["bancada"], f["pct_ficha"]), ("Nº 3", "B", 101.22))
        atrasos = {r["sesion_numero"]: r["atraso_min"] for r in c.execute("SELECT sesion_numero, atraso_min FROM v_puntualidad")}
        self.assertEqual(atrasos, {86: 7, 84: 0})     # la ausente no figura en la vista

    def test_asistencia_comision_con_reemplazantes(self):
        c = conexion()
        c.executemany("INSERT INTO diputado (id, nombre, nombre2, apellido_paterno, apellido_materno) VALUES (?,?,?,?,?)",
                      [(2001, "Consuelo", "", "Veloso", "Ávila"), (2002, "Marcela", "", "Hernando", "Pérez"), (1107, "María Francisca", "", "Bello", "Campos")])
        raw = {"integrantes": [{"id": 1107, "nombre": "Sra. María Francisca Bello Campos", "estado": "REEMPLAZADA POR Sra. CONSUELO VELOSO ÁVILA"},
                               {"id": 1102, "nombre": "Sr. Roberto Arroyo", "estado": "ASISTE"},
                               {"id": 1142, "nombre": "Sr. Daniel M.", "estado": "REEMPLAZADO POR Sra. MARCELA HERNANDO PÉREZ"},
                               {"id": 1300, "nombre": "Sr. X", "estado": "REEMPLAZADO POR Sr. NOMBRE DESCONOCIDO"}]}
        c.execute("INSERT INTO diputado (id, nombre, apellido_paterno) VALUES (1102, 'Roberto', 'Arroyo'), (1142, 'Daniel', 'Manouchehri'), (1300, 'X', 'Y')")
        filas = scraper.parse_asistencia_comision(raw, 82296)
        sin = scraper.guardar_asistencia_comision(c, filas)
        self.assertEqual(sin, 1)
        filas_db = {r["diputado_id"]: r for r in c.execute("SELECT * FROM web_asistencia_comision")}
        self.assertEqual(filas_db[2001]["estado"], "REEMPLAZANTE")
        self.assertEqual(filas_db[2001]["asiste"], 1)
        self.assertEqual(filas_db[1107]["reemplazante_id"], 2001)
        self.assertEqual(filas_db[2002]["asiste"], 1)
        self.assertIsNone(filas_db[1300]["reemplazante_id"])
        self.assertEqual(len(filas_db), 6)    # 4 titulares + 2 reemplazantes resueltos


if __name__ == "__main__":
    unittest.main()
