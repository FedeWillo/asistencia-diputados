# Asistencia Diputados

Datos abiertos de asistencia de la Cámara de Diputados de Chile, ingeridos a SQLite y (próximamente)
publicados como sitio estático con metodología pública: asistencia a Sala por causal, comisiones,
participación en votaciones y puntualidad.

## Fase 0: ingesta

Requiere Python 3.11+. Sin dependencias externas.

```bash
python -m unittest discover -s tests          # tests con fixtures (sin red)
python ingesta.py catalogos                   # justificaciones, partidos, distritos
python ingesta.py diputados                   # 155 diputados del período actual + militancias
python ingesta.py sala --anno 2026 --limite 5 # sesiones + asistencia (prueba chica)
python ingesta.py sala --anno 2026            # todo el año
python ingesta.py votaciones --anno 2026      # cabeceras + voto por diputado, unidas a su sesión
python ingesta.py comisiones --ids 544 411 --anno 2026
python ingesta.py diario                      # lo que corre el cron
python ingesta.py estado
```

La base queda en `data/camara.sqlite`. La ingesta es idempotente: recarga la asistencia de los últimos
45 días (las justificaciones se cargan con rezago) y registra cada cambio en `asistencia_sala_cambio`.

## Fuente

`https://opendata.camara.cl/camaradiputados/WServices/` (XML, sin autenticación). Espejo:
`opendata.congreso.cl`.

## Scraper de camara.cl (opcional)

Captura lo que el API no entrega: hora de ingreso a Sala (puntualidad), cabecera de las fichas (distrito, región, bancada)
y la asistencia a comisiones del período 2026-2030. Requiere Playwright:

```bash
pip install playwright && playwright install chromium
python scraper.py diagnostico --visible                          # ¿te deja pasar el sitio desde tu conexión?
python scraper.py fichas --limite 3 --paginas 2 --visible        # prueba chica
python scraper.py comisiones --ids 4894 --desde 2026-10-01       # Salud, desde octubre
python scraper.py estado
```

camara.cl usa Cloudflare y bloquea las visitas desde GitHub Actions (403); el scraper se detiene si lo detecta y no intenta
esquivarlo. Ritmo bajo (≈1-2 s entre páginas). No sigue los enlaces "ver" de las ausencias (documentos de respaldo con datos personales).
