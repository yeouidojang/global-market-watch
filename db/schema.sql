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
    importance  TEXT DEFAULT 'medium', -- high | medium | low
    source_id   TEXT,                  -- FRED series ID 등
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

-- 세션별 종목 스크리닝 결과 (Asia 주요종목/특징주, Europe 섹터/종목, US 스크리닝)
CREATE TABLE IF NOT EXISTS stocks_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,          -- YYYY-MM-DD
    session      TEXT NOT NULL,          -- asia | europe | us
    category     TEXT NOT NULL,          -- major | featured | sectors | top_stocks | mktcap_top | tradeval_top | turnover_surge | eps_revision
    ticker       TEXT NOT NULL,          -- yfinance ticker or RIC
    name         TEXT,
    market       TEXT,                   -- KOSPI | KOSDAQ (asia only)
    close        REAL,
    chg_pct      REAL,
    volume       REAL,                   -- 거래량 (europe top_stocks)
    trade_val    REAL,                   -- 거래대금 원화 (asia)
    dollar_vol_b REAL,                   -- 거래대금 USD B (us)
    mktcap       REAL,                   -- 시가총액 원화 (asia)
    mktcap_b     REAL,                   -- 시가총액 USD B (us/europe)
    turnover     REAL,                   -- 거래량회전율 (asia featured)
    surge_ratio  REAL,                   -- 거래대금 급증 배수 (us)
    eps_chg_1m   REAL,                   -- 30일 EPS 추정치 변화율 (%) ← 핵심
    eps_chg_1w   REAL,                   -- 7일 EPS 추정치 변화율 (%)  ← 서브
    return_7d    REAL,                   -- 7일 누적 수익률 (us eps_revision)
    foreign_net  REAL,                   -- 외국인 순매수 (원, asia investor_flow/featured)
    inst_net     REAL,                   -- 기관 순매수 (원, asia investor_flow/featured)
    signal       TEXT,
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (date, session, category, ticker)
);

-- EPS 추정치 주간 캐시 (LSEG TR.MeanPctChg, 7일 단위 수집)
CREATE TABLE IF NOT EXISTS eps_cache (
    ticker       TEXT NOT NULL,          -- yfinance ticker (AAPL, NVDA 등)
    eps_chg_1m   REAL,                   -- 30일 EPS 추정치 변화율 (%) ← 핵심
    eps_chg_1w   REAL,                   -- 7일 EPS 추정치 변화율 (%)  ← 서브
    fetched_date TEXT NOT NULL,          -- 수집 기준일 YYYY-MM-DD
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (ticker)
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_market_daily_date   ON market_daily(date);
CREATE INDEX IF NOT EXISTS idx_market_daily_name   ON market_daily(name);
CREATE INDEX IF NOT EXISTS idx_econ_calendar_date  ON econ_calendar(event_date);
CREATE INDEX IF NOT EXISTS idx_briefings_date      ON briefings(date, session);
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_unique
    ON econ_calendar(event_date, country, indicator);
CREATE INDEX IF NOT EXISTS idx_econ_importance ON econ_calendar(importance);
CREATE INDEX IF NOT EXISTS idx_eps_cache_date    ON eps_cache(fetched_date);
CREATE INDEX IF NOT EXISTS idx_stocks_daily_date ON stocks_daily(date, session);
