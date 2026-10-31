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
- Votaciones no traen sesión: se unen por `Fecha` dentro de `FechaInicio–FechaTermino` (`unir_votaciones_con_sesiones`).
- Toda cambio de asistencia/justificación entre corridas queda en `asistencia_sala_cambio`.
- Probar contra el API con `--limite 5` antes de cargas grandes. El API falla intermitentemente: ya hay backoff.
- `retornarComisionesXPeriodo?prmPeriodoId=11` da error de servidor (31-10-2026): lista de comisiones en `COMISIONES_FALLBACK`.
- Español en código, comentarios y mensajes. Sin dependencias externas salvo justificación explícita.

## Comandos
    python -m unittest discover -s tests -v
    python ingesta.py catalogos && python ingesta.py diputados
    python ingesta.py sala --anno 2026 --limite 5
    python ingesta.py votaciones --anno 2026 --limite 5
    python ingesta.py estado
