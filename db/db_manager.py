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
        conn.executescript(schema)
        conn.commit()
        conn.close()

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
        # pandas NA/NaN → None (sqlite3 미지원 타입 방지)
        records = [
            {k: (None if pd.isna(v) else v) for k, v in r.items()}
            for r in records
        ]
        sql = """
            INSERT INTO market_daily (date, session, category, name, close, open, high, low, volume)
            VALUES (:date, :session, :category, :name, :close, :open, :high, :low, :volume)
            ON CONFLICT(date, name) DO UPDATE SET
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
        sql = """
            INSERT INTO econ_calendar
                (event_date, event_time, country, indicator, period, actual, forecast, previous, surprise)
            VALUES
                (:event_date, :event_time, :country, :indicator, :period, :actual, :forecast, :previous, :surprise)
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
            SELECT event_date, event_time, country, indicator, period, forecast, previous
            FROM econ_calendar
            WHERE event_date >= :from_date
              AND event_date <= date(:from_date, '+' || :days || ' days')
            ORDER BY event_date, event_time
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
        """
        conn = self._connect()
        cursor = conn.execute(sql, (date, session, model, prompt_tokens, output_tokens, content))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def mark_notified(self, briefing_id: int):
        conn = self._connect()
        conn.execute("UPDATE briefings SET notified=1 WHERE id=?", (briefing_id,))
        conn.commit()
        conn.close()

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
