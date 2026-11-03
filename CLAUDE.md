# Asistencia Diputados — guía para Claude Code

Webapp pública de asistencia de los diputados de Chile. Este repo contiene la Fase 0: ingesta del API
de datos abiertos de la Cámara a SQLite. La referencia de producto, métricas y datos vive en el proyecto
de Claude (`plan-webapp.md`, `investigacion-benchmark-y-datos.md`).

## Estructura
- `ingesta.py` — CLI de ingesta, solo biblioteca estándar. Parsers puros (`parse_*`) separados de la
  carga (`cargar_*`) para poder testear sin red.
- `schema.sql` — esquema SQLite; se aplica con `CREATE TABLE IF NOT EXISTS` en cada corrida.
- `data/camara.sqlite` — base generada (versionada mientras sea chica).
- `tests/` — tests con fixtures XML que replican la estructura real del API.
- `.github/workflows/ingesta.yml` — cron diario que corre `ingesta.py diario` y commitea la base.

## Reglas
- Namespace obligatorio en todo `findall`: `{http://opendata.camara.cl/camaradiputados/v1}` (usar `NS`).
- Militancia actual = mayor `FechaTermino` (no vienen ordenadas). `DiputadoPeriodo/FechaInicio` tiene bug.
- El Id de partido es un código de texto (`PC`, `IND`, `UDI`), no un número.
- Sala: `Estado` 1 = Celebrada. Comisiones: `Estado` 3 = Celebrada. `OpcionVoto` 4 = No Vota.
- Votaciones no traen sesión: se unen por `Fecha` dentro de `FechaInicio–FechaTermino` (`unir_votaciones_con_sesiones`). La última votación se registra 1–12 s después del término de la sesión (y alguna 50 s antes del inicio): una segunda pasada las asigna a la sesión celebrada más cercana (tolerancia 120 s antes / 60 s después) y marca `votacion.sesion_aprox = 1`. Al 3-nov-2026: 1.165 votaciones, 0 sin sesión, 10 aproximadas.
- Diputados de períodos anteriores aparecen solo como Id en asistencias/votos/integrantes: `completar_diputados` los completa con `retornarDiputado` (parámetro `prmDiputadoId`) y corre dentro de `diario`. No toca `periodo_actual` (queda en 0).
- `cargar_comisiones` descarta sesiones cuyo año no coincide con `prmAnno` (el API devolvió fechas basura de 2006).
- Si el SQL funciona local pero falla en Actions, sospechar de la versión de SQLite del runner: preferir lógica en Python para consultas con subconsultas correlacionadas complejas.
- Toda cambio de asistencia/justificación entre corridas queda en `asistencia_sala_cambio`.
- Probar contra el API con `--limite 5` antes de cargas grandes. El API falla intermitentemente: ya hay backoff.
- `retornarComisionesXPeriodo` usa el parámetro **`prmPeriodoLegislativoId`** (no `prmPeriodoId`; con el nombre equivocado da error de servidor). Período 10 = 2022-2026 (91 comisiones); período 11 = 2026-2030, donde el API solo lista 2 (411, 544, con Id de 2022) y devuelve vacío para los Id nuevos de camara.cl (4894 Salud…). Comisiones 2026-2030 requieren scraping; el cron diario avisa en el log si el API empieza a publicarlas.
- Estados de sesión de comisión: 1 Citada, 2 En Comité, 3 Celebrada, 4 Suspendida, 5 Fracasada.
- `OpcionVoto` 3 = Dispensado.
- Español en código, comentarios y mensajes. Sin dependencias externas salvo justificación explícita.

## Comandos
    python -m unittest discover -s tests -v
    python ingesta.py catalogos && python ingesta.py diputados   # incluye completar nombres históricos
    python ingesta.py diputados --completar
    python ingesta.py sala --anno 2026 --limite 5
    python ingesta.py votaciones --anno 2026 --limite 5
    python ingesta.py comisiones --periodo 10 --annos 2022 2023 2024 2025 2026
    python ingesta.py estado
