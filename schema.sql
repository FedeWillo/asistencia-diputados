-- Esquema SQLite de Asistencia Diputados (Fase 0)
-- Convención: Ids del API de la Cámara se guardan tal cual. Fechas en ISO 8601 (texto).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Catálogos (WSComun)
CREATE TABLE IF NOT EXISTS justificacion_tipo (
    valor               INTEGER PRIMARY KEY,
    nombre              TEXT NOT NULL,
    rebaja_asistencia   INTEGER NOT NULL,   -- 1 = la inasistencia no baja el % oficial
    rebaja_quorum       INTEGER NOT NULL    -- 1 = el diputado no se cuenta en ejercicio
);

CREATE TABLE IF NOT EXISTS partido (
    id      INTEGER PRIMARY KEY,
    nombre  TEXT NOT NULL,
    alias   TEXT
);

CREATE TABLE IF NOT EXISTS distrito (
    numero  INTEGER PRIMARY KEY,
    comunas TEXT        -- nombres separados por ';' tal como vienen del API (60 distritos: incluye sistema antiguo)
);

-- Diputados y militancias (WSDiputado)
CREATE TABLE IF NOT EXISTS diputado (
    id                  INTEGER PRIMARY KEY,
    nombre              TEXT NOT NULL,
    nombre2             TEXT,
    apellido_paterno    TEXT NOT NULL,
    apellido_materno    TEXT,
    fecha_nacimiento    TEXT,
    sexo                INTEGER,            -- Valor del API
    periodo_actual      INTEGER NOT NULL DEFAULT 0   -- 1 si figura en retornarDiputadosPeriodoActual
);

CREATE TABLE IF NOT EXISTS militancia (
    diputado_id     INTEGER NOT NULL REFERENCES diputado(id),
    partido_id      INTEGER NOT NULL REFERENCES partido(id),
    fecha_inicio    TEXT NOT NULL,
    fecha_termino   TEXT,
    PRIMARY KEY (diputado_id, partido_id, fecha_inicio)
);

-- Sala (WSSala)
CREATE TABLE IF NOT EXISTS sesion_sala (
    id              INTEGER PRIMARY KEY,
    numero          INTEGER,
    fecha_inicio    TEXT NOT NULL,
    fecha_termino   TEXT,
    tipo            INTEGER,    -- 1 Ordinaria, 2 Especial, 3 Congreso Pleno
    tipo_nombre     TEXT,
    estado          INTEGER,    -- 0 Citada, 1 Celebrada
    estado_nombre   TEXT,
    anno            INTEGER NOT NULL,
    asistencia_cargada_en TEXT  -- última vez que se descargó la asistencia
);

CREATE TABLE IF NOT EXISTS asistencia_sala (
    sesion_id           INTEGER NOT NULL REFERENCES sesion_sala(id),
    diputado_id         INTEGER NOT NULL,
    asiste              INTEGER NOT NULL,   -- TipoAsistencia: 1 Asiste, 0 No Asiste
    justificacion_valor INTEGER,            -- NULL = sin justificación
    rebaja_asistencia   INTEGER,
    rebaja_quorum       INTEGER,
    PRIMARY KEY (sesion_id, diputado_id)
);

-- Historial de cambios de asistencia/justificación (las secretarías corrigen con rezago)
CREATE TABLE IF NOT EXISTS asistencia_sala_cambio (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detectado_en        TEXT NOT NULL,
    sesion_id           INTEGER NOT NULL,
    diputado_id         INTEGER NOT NULL,
    asiste_antes        INTEGER,
    justificacion_antes INTEGER,
    asiste_despues      INTEGER,
    justificacion_despues INTEGER
);

-- Votaciones (WSLegislativo)
CREATE TABLE IF NOT EXISTS votacion (
    id                  INTEGER PRIMARY KEY,
    descripcion         TEXT,
    fecha               TEXT NOT NULL,      -- al segundo
    total_si            INTEGER,
    total_no            INTEGER,
    total_abstencion    INTEGER,
    total_dispensado    INTEGER,
    quorum              INTEGER,
    quorum_nombre       TEXT,
    resultado           INTEGER,
    resultado_nombre    TEXT,
    tipo                INTEGER,
    tipo_nombre         TEXT,
    anno                INTEGER NOT NULL,
    sesion_id           INTEGER REFERENCES sesion_sala(id),   -- unida por timestamp; NULL si no calza
    detalle_cargado_en  TEXT
);

CREATE TABLE IF NOT EXISTS voto (
    votacion_id     INTEGER NOT NULL REFERENCES votacion(id),
    diputado_id     INTEGER NOT NULL,
    opcion          INTEGER NOT NULL,   -- 0 En Contra, 1 Afirmativo, 2 Abstención, 4 No Vota
    PRIMARY KEY (votacion_id, diputado_id)
);

-- Comisiones (WSComision)
CREATE TABLE IF NOT EXISTS comision (
    id                  INTEGER PRIMARY KEY,
    nombre              TEXT NOT NULL,
    alias               TEXT,
    tipo                INTEGER,
    tipo_nombre         TEXT,
    fecha_inicio        TEXT,
    fecha_termino       TEXT,
    presidente_id       INTEGER
);

CREATE TABLE IF NOT EXISTS integrante_comision (
    comision_id     INTEGER NOT NULL REFERENCES comision(id),
    diputado_id     INTEGER NOT NULL,
    fecha_inicio    TEXT NOT NULL,
    fecha_termino   TEXT,
    historico       INTEGER NOT NULL DEFAULT 0,   -- 1 si viene de IntegrantesHistorico
    PRIMARY KEY (comision_id, diputado_id, fecha_inicio)
);

CREATE TABLE IF NOT EXISTS sesion_comision (
    id              INTEGER PRIMARY KEY,
    comision_id     INTEGER NOT NULL REFERENCES comision(id),
    numero          INTEGER,
    fecha_inicio    TEXT NOT NULL,
    fecha_termino   TEXT,
    tipo            INTEGER,
    tipo_nombre     TEXT,
    estado          INTEGER,    -- 3 = Celebrada (distinto de Sala)
    estado_nombre   TEXT,
    anno            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS asistencia_comision (
    sesion_id           INTEGER NOT NULL REFERENCES sesion_comision(id),
    diputado_id         INTEGER NOT NULL,
    asiste              INTEGER NOT NULL,
    justificacion_valor INTEGER,
    titular             INTEGER,            -- TipoTitularAsistencia: 1 Titular, 2 Reemplazante
    PRIMARY KEY (sesion_id, diputado_id)
);

-- Registro de corridas de ingesta
CREATE TABLE IF NOT EXISTS corrida (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inicio      TEXT NOT NULL,
    fin         TEXT,
    comando     TEXT,
    ok          INTEGER,
    detalle     TEXT
);

CREATE INDEX IF NOT EXISTS idx_asistencia_sala_dip ON asistencia_sala(diputado_id);
CREATE INDEX IF NOT EXISTS idx_voto_dip ON voto(diputado_id);
CREATE INDEX IF NOT EXISTS idx_votacion_sesion ON votacion(sesion_id);
CREATE INDEX IF NOT EXISTS idx_sesion_sala_fecha ON sesion_sala(fecha_inicio);
