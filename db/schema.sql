-- global_market_watch DB 스키마
-- SQLite

-- 지수·FX·금리·원자재 일별 시계열
CREATE TABLE IF NOT EXISTS market_daily (
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    session     TEXT NOT NULL,          -- asia | europe | us | macro
    category    TEXT NOT NULL,          -- index | fx | rate | commodity | volatility | stock
    name        TEXT NOT NULL,          -- 식별자 (KOSPI, USD_KRW 등)
    market      TEXT,                   -- KOSPI | KOSDAQ (asia stock 전용)
    close       REAL,
    open        REAL,
    high        REAL,
    low         REAL,
    volume      REAL,
    change_pct  REAL,                   -- 전일 대비 등락률 (%)
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
    session     TEXT NOT NULL,          -- asia | global | europe (context only)
    model       TEXT,
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    content     TEXT NOT NULL,
    notified    INTEGER DEFAULT 0,      -- 0: 미발송, 1: Slack 발송
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (date, session)
);

-- 세션별 종목 스크리닝 결과 (Asia 주요종목/특징주, Europe 섹터/종목, US 스크리닝)
CREATE TABLE IF NOT EXISTS stocks_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,          -- YYYY-MM-DD
    session      TEXT NOT NULL,          -- asia | europe | us
    category     TEXT NOT NULL,          -- major | featured | sectors | top_stocks | mktcap_top | tradeval_top | turnover_surge | eps_revision
    ticker       TEXT NOT NULL,          -- yfinance ticker or RIC
    name         TEXT,
    market       TEXT,                   -- KOSPI | KOSDAQ (korea) | DAX/FTSE/CAC (europe) | sector (asia overseas)
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
    ret_1w       REAL,                   -- 5거래일 수익률 (asia/us/europe 종목)
    ret_1m       REAL,                   -- ~22거래일 수익률 (asia/us/europe 종목)
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

-- 어닝 캘린더 (Finnhub earnings_calendar)
CREATE TABLE IF NOT EXISTS earnings_calendar (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date       TEXT NOT NULL,          -- YYYY-MM-DD (발표일)
    hour             TEXT,                   -- bmo (장전) | amc (장후) | dmh (장중) | ''
    symbol           TEXT NOT NULL,          -- yfinance ticker (AAPL, NVDA, BRK-B 등)
    year             INTEGER,                -- 회계연도
    quarter          INTEGER,                -- 분기 (1~4)
    eps_estimate     REAL,
    eps_actual       REAL,
    revenue_estimate REAL,                   -- USD
    revenue_actual   REAL,                   -- USD
    surprise_pct     REAL,                   -- (actual - estimate) / |estimate| * 100
    source           TEXT DEFAULT 'finnhub',
    created_at       TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (event_date, symbol)
);

-- SPX 전 종목 일별 OHLCV (yfinance, ~504종목)
CREATE TABLE IF NOT EXISTS us_stocks_daily (
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    ticker      TEXT NOT NULL,          -- yfinance ticker (AAPL, NVDA, BRK-B 등)
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (date, ticker)
);

-- 시장 휴장일 캘린더 (exchange_calendars 기반)
CREATE TABLE IF NOT EXISTS market_holidays (
    date        TEXT NOT NULL,   -- YYYY-MM-DD
    market_key  TEXT NOT NULL,   -- jp | cn | hk | kr
    is_holiday  INTEGER NOT NULL, -- 1=휴장, 0=개장
    reason      TEXT,            -- 공휴일명 (exchange_calendars 제공 시)
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (date, market_key)
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_market_holidays_date ON market_holidays(date);
CREATE INDEX IF NOT EXISTS idx_us_stocks_daily_date   ON us_stocks_daily(date);
CREATE INDEX IF NOT EXISTS idx_us_stocks_daily_ticker ON us_stocks_daily(ticker);
CREATE INDEX IF NOT EXISTS idx_market_daily_date   ON market_daily(date);
CREATE INDEX IF NOT EXISTS idx_market_daily_name   ON market_daily(name);
CREATE INDEX IF NOT EXISTS idx_market_daily_market ON market_daily(session, category, market, date);
CREATE INDEX IF NOT EXISTS idx_econ_calendar_date  ON econ_calendar(event_date);
CREATE INDEX IF NOT EXISTS idx_briefings_date      ON briefings(date, session);
CREATE UNIQUE INDEX IF NOT EXISTS idx_econ_unique
    ON econ_calendar(event_date, country, indicator);
CREATE INDEX IF NOT EXISTS idx_econ_importance ON econ_calendar(importance);
CREATE INDEX IF NOT EXISTS idx_eps_cache_date    ON eps_cache(fetched_date);
CREATE INDEX IF NOT EXISTS idx_stocks_daily_date ON stocks_daily(date, session);
CREATE INDEX IF NOT EXISTS idx_earnings_cal_date   ON earnings_calendar(event_date);
CREATE INDEX IF NOT EXISTS idx_earnings_cal_symbol ON earnings_calendar(symbol);
