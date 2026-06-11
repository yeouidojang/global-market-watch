-- global_market_watch DB 스키마
-- SQLite

-- 지수·FX·금리·원자재 일별 시계열
CREATE TABLE IF NOT EXISTS market_daily (
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    session     TEXT NOT NULL,          -- asia | europe | us | macro
    category    TEXT NOT NULL,          -- index | fx | rate | commodity | volatility
    name        TEXT NOT NULL,          -- 식별자 (KOSPI, USD_KRW 등)
    close       REAL,
    open        REAL,
    high        REAL,
    low         REAL,
    volume      REAL,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (date, name)
);

-- 경제지표 캘린더
CREATE TABLE IF NOT EXISTS econ_calendar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TEXT NOT NULL,          -- YYYY-MM-DD
    event_time  TEXT,                   -- HH:MM KST
    country     TEXT,                   -- US | KR | EU | JP
    indicator   TEXT NOT NULL,          -- CPI, NFP, FOMC 등
    period      TEXT,                   -- 2026-05 등
    actual      REAL,
    forecast    REAL,
    previous    REAL,
    surprise    REAL,                   -- actual - forecast
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- LLM 브리핑 저장
CREATE TABLE IF NOT EXISTS briefings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    session     TEXT NOT NULL,          -- asia | europe | us
    model       TEXT,
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    content     TEXT NOT NULL,
    notified    INTEGER DEFAULT 0,      -- 0: 미발송, 1: Slack 발송
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_market_daily_date   ON market_daily(date);
CREATE INDEX IF NOT EXISTS idx_market_daily_name   ON market_daily(name);
CREATE INDEX IF NOT EXISTS idx_econ_calendar_date  ON econ_calendar(event_date);
CREATE INDEX IF NOT EXISTS idx_briefings_date      ON briefings(date, session);
