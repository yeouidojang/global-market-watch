"""
global_market_watch DB 관리 유틸리티

사용법:
    from db.db_manager import DBManager
    db = DBManager()
    db.upsert_market_daily(records)
"""

import os
import sqlite3
import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "market_watch.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"


class DBManager:
    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """스키마 파일로 DB 초기화 (테이블이 없을 때만 생성)."""
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = f.read()
        conn = self._connect()

        # 마이그레이션: importance 컬럼 추가 (테이블이 이미 존재하는 경우)
        existing = [row[1] for row in conn.execute("PRAGMA table_info(econ_calendar)").fetchall()]
        if existing:
            if "importance" not in existing:
                conn.execute("ALTER TABLE econ_calendar ADD COLUMN importance TEXT DEFAULT 'medium'")
                conn.commit()
            if "source_id" not in existing:
                conn.execute("ALTER TABLE econ_calendar ADD COLUMN source_id TEXT")
                conn.commit()

            # UNIQUE INDEX 생성 전 중복 레코드 제거
            conn.execute("""
                DELETE FROM econ_calendar
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM econ_calendar
                    GROUP BY event_date, country, indicator
                )
            """)
            conn.commit()

        # market_daily.market 컬럼 추가 마이그레이션 (executescript 전에 실행)
        md_cols = [r[1] for r in conn.execute("PRAGMA table_info(market_daily)").fetchall()]
        if md_cols and "market" not in md_cols:
            conn.execute("ALTER TABLE market_daily ADD COLUMN market TEXT")
            conn.commit()

        conn.executescript(schema)
        conn.commit()

        # briefings UNIQUE(date, session) 마이그레이션
        # — 기존 DB에 UNIQUE 제약이 없는 경우, 중복 제거 후 unique index 생성
        idx_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_briefings_date_session_unique'"
        ).fetchone()
        if not idx_exists:
            conn.execute("""
                DELETE FROM briefings
                WHERE id NOT IN (
                    SELECT MAX(id) FROM briefings GROUP BY date, session
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_briefings_date_session_unique "
                "ON briefings(date, session)"
            )
            conn.commit()

        # eps_cache JSON → DB 마이그레이션 (최초 1회)
        self._migrate_eps_cache_json(conn)

        # eps_chg_1m, foreign_net, inst_net 컬럼 추가 마이그레이션
        for table in ("eps_cache", "stocks_daily"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if cols and "eps_chg_1m" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN eps_chg_1m REAL")
                conn.commit()
        for col in ("foreign_net", "inst_net"):
            cols = [r[1] for r in conn.execute("PRAGMA table_info(stocks_daily)").fetchall()]
            if cols and col not in cols:
                conn.execute(f"ALTER TABLE stocks_daily ADD COLUMN {col} REAL")
                conn.commit()

        conn.close()

    def _migrate_eps_cache_json(self, conn):
        """db/eps_cache.json이 존재하면 DB로 이전 후 파일 삭제."""
        import json
        json_path = self.db_path.parent / "eps_cache.json"
        if not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text("utf-8"))
            if data:
                conn.executemany(
                    """INSERT INTO eps_cache (ticker, eps_chg_1w, fetched_date)
                       VALUES (:ticker, :eps_chg_1w, :fetched_date)
                       ON CONFLICT(ticker) DO UPDATE SET
                           eps_chg_1w   = excluded.eps_chg_1w,
                           fetched_date = excluded.fetched_date""",
                    [{"ticker": t, "eps_chg_1w": v["eps_chg_1w"], "fetched_date": v["fetched_date"]}
                     for t, v in data.items()],
                )
                conn.commit()
                print(f"[db_manager] eps_cache.json → DB 이전 완료: {len(data)}개 종목")
            json_path.unlink()
        except Exception as e:
            print(f"[db_manager] eps_cache.json 마이그레이션 실패: {e}")

    # ------------------------------------------------------------------ #
    #  market_daily
    # ------------------------------------------------------------------ #
    def upsert_market_daily(self, records: list[dict]) -> int:
        """
        market_daily 테이블 upsert.

        Parameters
        ----------
        records : list of dict  {date, session, category, name, close, open, high, low, volume}

        Returns
        -------
        int  upsert된 행 수
        """
        if not records:
            return 0
        # pandas NA/NaN → None (sqlite3 미지원 타입 방지), market 기본값 보정
        records = [
            {k: (None if (not isinstance(v, str) and pd.isna(v)) else v)
             for k, v in {**{"market": None}, **r}.items()}
            for r in records
        ]
        sql = """
            INSERT INTO market_daily (date, session, category, name, market, close, open, high, low, volume)
            VALUES (:date, :session, :category, :name, :market, :close, :open, :high, :low, :volume)
            ON CONFLICT(date, name) DO UPDATE SET
                market = COALESCE(excluded.market, market),
                close  = excluded.close,
                open   = excluded.open,
                high   = excluded.high,
                low    = excluded.low,
                volume = excluded.volume,
                created_at = datetime('now','localtime')
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_recent(self, names: list[str], n_days: int = 5) -> pd.DataFrame:
        """지정 name 목록의 최근 n_days 데이터 반환."""
        placeholders = ",".join("?" * len(names))
        sql = f"""
            SELECT date, session, category, name, close, open, high, low
            FROM market_daily
            WHERE name IN ({placeholders})
            ORDER BY date DESC, name
            LIMIT {n_days * len(names) * 2}
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn, params=names)
        conn.close()
        return df

    def get_by_date(self, target_date: str) -> pd.DataFrame:
        """특정 날짜의 모든 market_daily 레코드 반환."""
        sql = """
            SELECT date, session, category, name, close, open, high, low
            FROM market_daily
            WHERE date = ?
            ORDER BY session, category, name
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn, params=[target_date])
        conn.close()
        return df

    def get_stocks_universe(self, session: str, start_date: str,
                            end_date: str = None) -> pd.DataFrame:
        """
        특정 세션의 전 종목 주가 시계열 반환.

        Parameters
        ----------
        session    : asia | europe | us
        start_date : YYYY-MM-DD
        end_date   : YYYY-MM-DD (기본: 오늘)

        Returns
        -------
        DataFrame  [date, name, close, open, high, low, volume]
        """
        end = end_date or start_date
        sql = """
            SELECT date, name, close, open, high, low, volume
            FROM market_daily
            WHERE session = :session
              AND category = 'stock'
              AND date BETWEEN :start AND :end
            ORDER BY date, name
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn, params={"session": session, "start": start_date, "end": end})
        conn.close()
        return df

    def get_latest_by_name(self) -> pd.DataFrame:
        """각 name의 가장 최근 레코드 1건씩 반환."""
        sql = """
            SELECT m.*
            FROM market_daily m
            INNER JOIN (
                SELECT name, MAX(date) AS max_date
                FROM market_daily
                GROUP BY name
            ) latest ON m.name = latest.name AND m.date = latest.max_date
            ORDER BY session, category, name
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn)
        conn.close()
        return df

    # ------------------------------------------------------------------ #
    #  econ_calendar
    # ------------------------------------------------------------------ #
    def upsert_econ_event(self, records: list[dict]) -> int:
        if not records:
            return 0
        # 필수 필드 기본값 보정
        for r in records:
            r.setdefault("event_time", None)
            r.setdefault("period", None)
            r.setdefault("actual", None)
            r.setdefault("forecast", None)
            r.setdefault("previous", None)
            r.setdefault("surprise", None)
            r.setdefault("importance", "medium")
            r.setdefault("source_id", None)

        sql = """
            INSERT INTO econ_calendar
                (event_date, event_time, country, indicator, period,
                 actual, forecast, previous, surprise, importance, source_id)
            VALUES
                (:event_date, :event_time, :country, :indicator, :period,
                 :actual, :forecast, :previous, :surprise, :importance, :source_id)
            ON CONFLICT(event_date, country, indicator) DO UPDATE SET
                event_time  = COALESCE(excluded.event_time,  event_time),
                period      = COALESCE(excluded.period,      period),
                actual      = COALESCE(excluded.actual,      actual),
                forecast    = COALESCE(excluded.forecast,    forecast),
                previous    = COALESCE(excluded.previous,    previous),
                surprise    = COALESCE(excluded.surprise,    surprise),
                importance  = excluded.importance,
                source_id   = COALESCE(excluded.source_id,  source_id)
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_upcoming_events(self, from_date: str, days: int = 5) -> pd.DataFrame:
        """from_date 이후 days일 이내 경제 이벤트 반환."""
        sql = """
            SELECT event_date, event_time, country, indicator, period,
                   forecast, previous, actual, importance
            FROM econ_calendar
            WHERE event_date >= :from_date
              AND event_date <= date(:from_date, '+' || :days || ' days')
            ORDER BY
                CASE importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                event_date, event_time
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn, params={"from_date": from_date, "days": days})
        conn.close()
        return df

    # ------------------------------------------------------------------ #
    #  briefings
    # ------------------------------------------------------------------ #
    def save_briefing(self, date: str, session: str, content: str,
                      model: str = None, prompt_tokens: int = None,
                      output_tokens: int = None) -> int:
        sql = """
            INSERT INTO briefings (date, session, model, prompt_tokens, output_tokens, content)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, session) DO UPDATE SET
                content       = excluded.content,
                model         = excluded.model,
                prompt_tokens = excluded.prompt_tokens,
                output_tokens = excluded.output_tokens,
                notified      = 0,
                created_at    = datetime('now','localtime')
        """
        conn = self._connect()
        cursor = conn.execute(sql, (date, session, model, prompt_tokens, output_tokens, content))
        conn.commit()
        row_id = cursor.lastrowid or conn.execute(
            "SELECT id FROM briefings WHERE date=? AND session=?", (date, session)
        ).fetchone()[0]
        conn.close()
        return row_id

    def mark_notified(self, briefing_id: int):
        conn = self._connect()
        conn.execute("UPDATE briefings SET notified=1 WHERE id=?", (briefing_id,))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    #  earnings_calendar
    # ------------------------------------------------------------------ #
    def upsert_earnings_event(self, records: list[dict]) -> int:
        if not records:
            return 0
        for r in records:
            r.setdefault("hour", None)
            r.setdefault("year", None)
            r.setdefault("quarter", None)
            r.setdefault("eps_estimate", None)
            r.setdefault("eps_actual", None)
            r.setdefault("revenue_estimate", None)
            r.setdefault("revenue_actual", None)
            r.setdefault("surprise_pct", None)
            r.setdefault("source", "finnhub")
            for k, v in list(r.items()):
                if isinstance(v, float) and pd.isna(v):
                    r[k] = None

        sql = """
            INSERT INTO earnings_calendar
                (event_date, hour, symbol, year, quarter,
                 eps_estimate, eps_actual, revenue_estimate, revenue_actual,
                 surprise_pct, source)
            VALUES
                (:event_date, :hour, :symbol, :year, :quarter,
                 :eps_estimate, :eps_actual, :revenue_estimate, :revenue_actual,
                 :surprise_pct, :source)
            ON CONFLICT(event_date, symbol) DO UPDATE SET
                hour             = COALESCE(excluded.hour,             hour),
                year             = COALESCE(excluded.year,             year),
                quarter          = COALESCE(excluded.quarter,          quarter),
                eps_estimate     = COALESCE(excluded.eps_estimate,     eps_estimate),
                eps_actual       = COALESCE(excluded.eps_actual,       eps_actual),
                revenue_estimate = COALESCE(excluded.revenue_estimate, revenue_estimate),
                revenue_actual   = COALESCE(excluded.revenue_actual,   revenue_actual),
                surprise_pct     = COALESCE(excluded.surprise_pct,     surprise_pct),
                source           = excluded.source
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_upcoming_earnings(self, from_date: str, days: int = 7,
                              symbols: list[str] | None = None,
                              limit: int | None = None) -> pd.DataFrame:
        """
        from_date 이후 days일 이내 어닝 이벤트 반환.
        symbols 지정 시 해당 종목만, limit 지정 시 결과 행 수 제한.
        """
        base_sql = """
            SELECT event_date, hour, symbol, year, quarter,
                   eps_estimate, eps_actual, revenue_estimate, revenue_actual,
                   surprise_pct
            FROM earnings_calendar
            WHERE event_date >= :from_date
              AND event_date <= date(:from_date, '+' || :days || ' days')
        """
        params = {"from_date": from_date, "days": days}
        if symbols:
            placeholders = ",".join(f":s{i}" for i in range(len(symbols)))
            base_sql += f" AND symbol IN ({placeholders})"
            for i, sym in enumerate(symbols):
                params[f"s{i}"] = sym
        base_sql += " ORDER BY event_date, hour, symbol"
        if limit:
            base_sql += f" LIMIT {int(limit)}"

        conn = self._connect()
        df = pd.read_sql_query(base_sql, conn, params=params)
        conn.close()
        return df

    # ------------------------------------------------------------------ #
    #  stocks_daily
    # ------------------------------------------------------------------ #
    @staticmethod
    def _stocks_data_to_records(date: str, session: str, stocks_data: dict) -> list[dict]:
        """stocks_data dict → stocks_daily 삽입용 레코드 리스트."""
        records = []

        # ── Asia Korea — major / featured ───────────────────────────────
        for cat in ("major", "featured",
                    "major_kospi", "major_kosdaq",
                    "featured_kospi", "featured_kosdaq"):
            for s in stocks_data.get(cat, []):
                records.append({
                    "date": date, "session": session, "category": cat,
                    "ticker": s.get("ticker", ""), "name": s.get("name"),
                    "market": s.get("market"), "close": s.get("close"),
                    "chg_pct": s.get("chg_pct"),
                    "trade_val": s.get("trade_val"),
                    "mktcap": s.get("mktcap"),
                    "ret_1w": s.get("ret_1w"), "ret_1m": s.get("ret_1m"),
                    "turnover": s.get("turnover"),
                    "foreign_net": s.get("foreign_net"),
                    "inst_net": s.get("inst_net"),
                    "signal": s.get("signal"),
                })

        # ── Asia investor_flow ──────────────────────────────────────────
        for s in stocks_data.get("investor_flow", []):
            records.append({
                "date": date, "session": session, "category": "investor_flow",
                "ticker": s.get("ticker", ""), "name": s.get("name"),
                "foreign_net": s.get("foreign_net"), "inst_net": s.get("inst_net"),
                "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                "market": s.get("market"),
            })

        # ── Europe ──────────────────────────────────────────────────────
        if session == "europe":
            # 섹터 지수
            for s in stocks_data.get("sectors", []):
                records.append({
                    "date": date, "session": session, "category": "sectors",
                    "ticker": s.get("ric", s.get("ticker", "")), "name": s.get("name"),
                    "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                })
            # 종목 스크리닝 (market = index 이름: DAX/FTSE/CAC)
            for cat in ("mktcap_top", "tradeval_top", "turnover_surge"):
                for s in stocks_data.get(cat, []):
                    records.append({
                        "date": date, "session": session, "category": cat,
                        "ticker": s.get("ticker", ""), "name": s.get("name"),
                        "market": s.get("index"),           # DAX/FTSE/CAC → market 컬럼
                        "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                        "ret_1w": s.get("ret_1w"), "ret_1m": s.get("ret_1m"),
                        "dollar_vol_b": s.get("dollar_vol_b"),
                        "mktcap_b": s.get("mktcap_b"),
                        "surge_ratio": s.get("surge_ratio"),
                        "signal": s.get("signal"),
                    })

        # ── US ──────────────────────────────────────────────────────────
        if session == "us":
            for s in stocks_data.get("sectors", []):
                records.append({
                    "date": date, "session": session, "category": "sectors",
                    "ticker": s.get("ticker", ""), "name": s.get("name"),
                    "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                })
            for cat in ("mktcap_top", "tradeval_top", "turnover_surge", "eps_revision"):
                for s in stocks_data.get(cat, []):
                    records.append({
                        "date": date, "session": session, "category": cat,
                        "ticker": s.get("ticker", ""), "name": s.get("name"),
                        "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                        "ret_1w": s.get("ret_1w"), "ret_1m": s.get("ret_1m"),
                        "dollar_vol_b": s.get("dollar_vol_b"),
                        "mktcap_b": s.get("mktcap_b"),
                        "surge_ratio": s.get("surge_ratio"),
                        "eps_chg_1m": s.get("eps_chg_1m"),
                        "eps_chg_1w": s.get("eps_chg_1w"),
                        "return_7d": s.get("return_7d"),
                        "signal": s.get("signal"),
                    })

        # ── Asia Overseas (JP/CN/HK) ─────────────────────────────────
        for mk, mk_data in stocks_data.get("overseas_asia", {}).items():
            if not isinstance(mk_data, dict):
                continue
            for sub_cat in ("major", "mktcap_top", "tradeval_top"):
                for s in mk_data.get(sub_cat, []):
                    records.append({
                        "date": date, "session": session, "category": f"{mk}_{sub_cat}",
                        "ticker": s.get("ticker", ""), "name": s.get("name"),
                        "market": s.get("sector"),          # 섹터명 보존
                        "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                        "ret_1w": s.get("ret_1w"), "ret_1m": s.get("ret_1m"),
                        "mktcap_b": s.get("mktcap_b"),
                        "dollar_vol_b": s.get("trade_val_b"),
                    })
            for s in mk_data.get("featured", []):
                records.append({
                    "date": date, "session": session, "category": f"{mk}_featured",
                    "ticker": s.get("ticker", ""), "name": s.get("name"),
                    "market": s.get("sector"),
                    "close": s.get("close"), "chg_pct": s.get("chg_pct"),
                    "ret_1w": s.get("ret_1w"), "ret_1m": s.get("ret_1m"),
                    "surge_ratio": s.get("surge_ratio"),
                    "dollar_vol_b": s.get("trade_val_b"),
                    "signal": s.get("signal"),
                })
            for s in mk_data.get("sectors", []):
                records.append({
                    "date": date, "session": session, "category": f"{mk}_sectors",
                    "ticker": s.get("sector", ""),
                    "name":   s.get("sector"),
                    "chg_pct":  s.get("chg_wmean") or s.get("chg_avg"),
                    "mktcap_b": s.get("mktcap_b"),
                    "volume":   s.get("n"),
                    "signal":   f"avg {s.get('chg_avg')}%",
                })

        return records

    @staticmethod
    def _records_to_stocks_data(rows: list) -> dict:
        """DB rows → stocks_data dict 재조립.

        - jp_* / cn_* / hk_* 카테고리 → overseas_asia 네스티드 구조로 복원
        - Europe mktcap_top 등: market 컬럼 → index 키로 복원
        """
        from collections import defaultdict
        buckets = defaultdict(list)
        session_val = rows[0]["session"] if rows else ""
        for r in rows:
            buckets[r["category"]].append(dict(r))

        STRIP = ("id", "date", "session", "category", "created_at")

        def _clean(rec: dict) -> dict:
            return {k: v for k, v in rec.items()
                    if k not in STRIP and v is not None}

        def _clean_eu(rec: dict) -> dict:
            """Europe 종목: market 컬럼을 index 키로도 노출."""
            d = _clean(rec)
            if "market" in d:
                d.setdefault("index", d["market"])
            return d

        def _clean_ov(rec: dict) -> dict:
            """Asia overseas 종목: market 컬럼을 sector 키로도 노출."""
            d = _clean(rec)
            if "market" in d:
                d.setdefault("sector", d["market"])
            return d

        result: dict = {}
        overseas_asia: dict = {}

        for cat, items in buckets.items():
            # Asia overseas 복원 (jp_*/cn_*/hk_*)
            matched = False
            for mk in ("jp", "cn", "hk"):
                if cat.startswith(f"{mk}_"):
                    sub = cat[len(f"{mk}_"):]   # major | featured | sectors | mktcap_top | tradeval_top
                    if mk not in overseas_asia:
                        overseas_asia[mk] = {}
                    overseas_asia[mk][sub] = [_clean_ov(r) for r in items]
                    matched = True
                    break
            if matched:
                continue

            # Europe 종목 카테고리 (market → index 복원)
            if session_val == "europe" and cat in (
                    "mktcap_top", "tradeval_top", "turnover_surge"):
                result[cat] = [_clean_eu(r) for r in items]
            else:
                result[cat] = [_clean(r) for r in items]

        if overseas_asia:
            result["overseas_asia"] = overseas_asia
        return result

    # stocks_daily 바인딩 키 (Asia 전용 필드 포함). 누락된 키는 None으로 자동 채움.
    _STOCKS_DAILY_KEYS = (
        "date", "session", "category", "ticker", "name", "market",
        "close", "chg_pct", "volume", "trade_val", "dollar_vol_b",
        "mktcap", "mktcap_b", "turnover", "surge_ratio",
        "eps_chg_1m", "eps_chg_1w", "return_7d",
        "ret_1w", "ret_1m",
        "foreign_net", "inst_net", "signal",
    )

    def upsert_stocks_daily(self, date: str, session: str, stocks_data: dict) -> int:
        """stocks_data dict를 stocks_daily 테이블에 upsert."""
        records = self._stocks_data_to_records(date, session, stocks_data)
        if not records:
            return 0

        # 모든 레코드에 누락 키를 None으로 채워 SQL 바인딩 누락 방지
        # (foreign_net/inst_net은 Asia 전용 — US/Europe에서는 자동 None)
        normalized = []
        for r in records:
            row = {k: r.get(k) for k in self._STOCKS_DAILY_KEYS}
            # pandas NA/NaN → None
            for k, v in row.items():
                if isinstance(v, float) and pd.isna(v):
                    row[k] = None
            normalized.append(row)
        records = normalized

        sql = """
            INSERT INTO stocks_daily
                (date, session, category, ticker, name, market,
                 close, chg_pct, volume, trade_val, dollar_vol_b,
                 mktcap, mktcap_b, turnover, surge_ratio,
                 eps_chg_1m, eps_chg_1w, return_7d,
                 ret_1w, ret_1m,
                 foreign_net, inst_net, signal)
            VALUES
                (:date, :session, :category, :ticker, :name, :market,
                 :close, :chg_pct, :volume, :trade_val, :dollar_vol_b,
                 :mktcap, :mktcap_b, :turnover, :surge_ratio,
                 :eps_chg_1m, :eps_chg_1w, :return_7d,
                 :ret_1w, :ret_1m,
                 :foreign_net, :inst_net, :signal)
            ON CONFLICT(date, session, category, ticker) DO UPDATE SET
                name         = excluded.name,
                close        = excluded.close,
                chg_pct      = excluded.chg_pct,
                volume       = COALESCE(excluded.volume,       volume),
                trade_val    = COALESCE(excluded.trade_val,    trade_val),
                dollar_vol_b = COALESCE(excluded.dollar_vol_b, dollar_vol_b),
                mktcap       = COALESCE(excluded.mktcap,       mktcap),
                mktcap_b     = COALESCE(excluded.mktcap_b,     mktcap_b),
                turnover     = COALESCE(excluded.turnover,     turnover),
                surge_ratio  = COALESCE(excluded.surge_ratio,  surge_ratio),
                eps_chg_1m   = COALESCE(excluded.eps_chg_1m,   eps_chg_1m),
                eps_chg_1w   = COALESCE(excluded.eps_chg_1w,   eps_chg_1w),
                return_7d    = COALESCE(excluded.return_7d,    return_7d),
                ret_1w       = COALESCE(excluded.ret_1w,       ret_1w),
                ret_1m       = COALESCE(excluded.ret_1m,       ret_1m),
                foreign_net  = COALESCE(excluded.foreign_net,  foreign_net),
                inst_net     = COALESCE(excluded.inst_net,     inst_net),
                signal       = COALESCE(excluded.signal,       signal),
                created_at   = datetime('now','localtime')
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_stocks_daily(self, date: str, session: str) -> dict:
        """
        stocks_daily에서 date+session 데이터를 stocks_data 형식으로 반환.

        Returns
        -------
        dict  {"major": [...], "featured": [...]} or {"sectors": [...], ...}
        """
        sql = """
            SELECT * FROM stocks_daily
            WHERE date = ? AND session = ?
            ORDER BY category, id
        """
        conn = self._connect()
        rows = conn.execute(sql, (date, session)).fetchall()
        conn.close()
        if not rows:
            return {}
        return self._records_to_stocks_data(rows)

    # ------------------------------------------------------------------ #
    #  eps_cache
    # ------------------------------------------------------------------ #
    def upsert_eps_cache(self, records: list[dict]) -> int:
        """
        EPS 캐시 upsert.

        Parameters
        ----------
        records : list of dict  {ticker, eps_chg_1m, eps_chg_1w, fetched_date}
        """
        if not records:
            return 0
        for r in records:
            r.setdefault("eps_chg_1m", None)
            r.setdefault("eps_chg_1w", None)
        sql = """
            INSERT INTO eps_cache (ticker, eps_chg_1m, eps_chg_1w, fetched_date)
            VALUES (:ticker, :eps_chg_1m, :eps_chg_1w, :fetched_date)
            ON CONFLICT(ticker) DO UPDATE SET
                eps_chg_1m   = excluded.eps_chg_1m,
                eps_chg_1w   = excluded.eps_chg_1w,
                fetched_date = excluded.fetched_date,
                created_at   = datetime('now','localtime')
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_eps_cache(self) -> dict:
        """
        전체 EPS 캐시 반환.

        Returns
        -------
        dict  {ticker: {eps_chg_1m, eps_chg_1w, fetched_date}}
        """
        sql = "SELECT ticker, eps_chg_1m, eps_chg_1w, fetched_date FROM eps_cache"
        conn = self._connect()
        rows = conn.execute(sql).fetchall()
        conn.close()
        return {r["ticker"]: {
                    "eps_chg_1m":  r["eps_chg_1m"],
                    "eps_chg_1w":  r["eps_chg_1w"],
                    "fetched_date": r["fetched_date"],
                } for r in rows}

    def get_market_breadth(self, date: str, session: str) -> dict:
        """
        market_daily에서 시장 폭 지표 재계산 (브리핑 재생성 시 사용).
        당일 + 직전 거래일 종가 비교로 등락률 산출.
        """
        sql = """
            SELECT t.name,
                   t.close  AS close_today,
                   p.close  AS close_prev,
                   (t.close * COALESCE(t.volume, 0)) AS trade_val
            FROM market_daily t
            JOIN market_daily p
              ON p.name = t.name
             AND p.session  = :session
             AND p.category = 'stock'
             AND p.date = (
                 SELECT MAX(m2.date) FROM market_daily m2
                  WHERE m2.name = t.name
                    AND m2.session  = :session
                    AND m2.category = 'stock'
                    AND m2.date < :date
             )
            WHERE t.date     = :date
              AND t.session  = :session
              AND t.category = 'stock'
              AND t.close > 0
              AND p.close > 0
        """
        conn = self._connect()
        rows = conn.execute(sql, {"date": date, "session": session}).fetchall()
        conn.close()
        if not rows:
            return {}

        chg_list = [(r["close_today"] - r["close_prev"]) / abs(r["close_prev"]) * 100
                    for r in rows]
        tv_list  = [r["trade_val"] or 0 for r in rows]
        up    = sum(1 for c in chg_list if c > 0)
        down  = sum(1 for c in chg_list if c < 0)
        flat  = sum(1 for c in chg_list if c == 0)
        total = up + down + flat
        tv_sum = sum(tv_list)
        return {
            "up": up, "down": down, "flat": flat, "total": total,
            "up_pct":       round(up / total * 100, 1) if total > 0 else None,
            "adr":          round(up / (up + down) * 100, 1) if (up + down) > 0 else None,
            "weighted_chg": round(
                sum(c * tv for c, tv in zip(chg_list, tv_list)) / tv_sum, 2
            ) if tv_sum > 0 else None,
        }

    # ------------------------------------------------------------------ #
    #  us_stocks_daily
    # ------------------------------------------------------------------ #
    def upsert_us_stocks_daily(self, records: list[dict]) -> int:
        """
        us_stocks_daily 테이블 upsert.

        Parameters
        ----------
        records : list of dict  {date, ticker, open, high, low, close, volume}

        Returns
        -------
        int  upsert된 행 수
        """
        if not records:
            return 0
        records = [
            {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()}
            for r in records
        ]
        sql = """
            INSERT INTO us_stocks_daily (date, ticker, open, high, low, close, volume)
            VALUES (:date, :ticker, :open, :high, :low, :close, :volume)
            ON CONFLICT(date, ticker) DO UPDATE SET
                open       = excluded.open,
                high       = excluded.high,
                low        = excluded.low,
                close      = excluded.close,
                volume     = excluded.volume,
                created_at = datetime('now','localtime')
        """
        conn = self._connect()
        cursor = conn.executemany(sql, records)
        conn.commit()
        count = cursor.rowcount
        conn.close()
        return count

    def get_us_stocks_daily(self, start_date: str, end_date: str = None,
                            tickers: list[str] | None = None) -> pd.DataFrame:
        """
        us_stocks_daily 조회.

        Parameters
        ----------
        start_date : YYYY-MM-DD
        end_date   : YYYY-MM-DD (기본: start_date와 동일)
        tickers    : 조회할 ticker 목록 (None이면 전체)

        Returns
        -------
        DataFrame  [date, ticker, open, high, low, close, volume]
        """
        end = end_date or start_date
        params: dict = {"start": start_date, "end": end}
        ticker_clause = ""
        if tickers:
            placeholders = ",".join(f":t{i}" for i in range(len(tickers)))
            ticker_clause = f"AND ticker IN ({placeholders})"
            for i, t in enumerate(tickers):
                params[f"t{i}"] = t
        sql = f"""
            SELECT date, ticker, open, high, low, close, volume
            FROM us_stocks_daily
            WHERE date BETWEEN :start AND :end
            {ticker_clause}
            ORDER BY date, ticker
        """
        conn = self._connect()
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df

    # ─────────────────────────────────────────
    #  market_holidays
    # ─────────────────────────────────────────
    def upsert_market_holidays(self, records: list[dict]) -> int:
        """market_holidays upsert. records: [{date, market_key, is_holiday, reason}]"""
        if not records:
            return 0
        conn = self._connect()
        n = 0
        try:
            for r in records:
                conn.execute("""
                    INSERT INTO market_holidays (date, market_key, is_holiday, reason)
                    VALUES (:date, :market_key, :is_holiday, :reason)
                    ON CONFLICT(date, market_key) DO UPDATE SET
                        is_holiday = excluded.is_holiday,
                        reason     = excluded.reason
                """, {
                    "date":       r["date"],
                    "market_key": r["market_key"],
                    "is_holiday": 1 if r.get("is_holiday") else 0,
                    "reason":     r.get("reason"),
                })
                n += 1
            conn.commit()
        finally:
            conn.close()
        return n

    def is_market_holiday(self, date: str, market_key: str) -> bool | None:
        """DB에 저장된 휴장 여부 반환. 데이터 없으면 None."""
        conn = self._connect()
        row = conn.execute(
            "SELECT is_holiday FROM market_holidays WHERE date=? AND market_key=?",
            (date, market_key),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return bool(row[0])

    def get_market_flow(self, date: str) -> dict:
        """
        stocks_daily의 investor_flow 레코드에서 시장 전체 외인/기관 합계 반환.
        """
        sql = """
            SELECT SUM(foreign_net) AS foreign_net,
                   SUM(inst_net)    AS inst_net,
                   COUNT(*)         AS n_stocks
            FROM stocks_daily
            WHERE date = ? AND session = 'asia' AND category = 'investor_flow'
        """
        conn = self._connect()
        row = conn.execute(sql, (date,)).fetchone()
        conn.close()
        if not row or not row["n_stocks"]:
            return {}
        return {
            "foreign_net": int(row["foreign_net"]) if row["foreign_net"] is not None else None,
            "inst_net":    int(row["inst_net"])    if row["inst_net"]    is not None else None,
            "n_stocks":    row["n_stocks"],
        }

    def get_latest_briefing(self, session: str = None) -> dict | None:
        cond = "WHERE session=?" if session else ""
        params = (session,) if session else ()
        sql = f"""
            SELECT * FROM briefings {cond}
            ORDER BY created_at DESC LIMIT 1
        """
        conn = self._connect()
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None
