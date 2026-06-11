"""
경제지표 캘린더 수집 v2 (Finnhub primary + FRED secondary)

Finnhub /api/v1/economic-calendar:
  - 미국(US), 유럽(EU), 일본(JP), 한국(KR), 중국(CN)
  - estimate(예상치) + actual(실제치) + prev(이전치) 모두 지원
  - 향후 14일 기본 수집 (이번주·다음주)

FRED (선택, 기본 활성화): US 지표 actual/period 보완
  - --no-fred 플래그로 비활성화

사용법:
    python collect_econ_cal.py              # 향후 14일 (Finnhub + FRED)
    python collect_econ_cal.py --days 7
    python collect_econ_cal.py --no-fred    # Finnhub만 사용
"""

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))

import requests
import pandas as pd

from db.db_manager import DBManager

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FRED_API_KEY    = os.getenv("FRED_API_KEY", "")
FINNHUB_BASE    = "https://finnhub.io/api/v1"
FRED_BASE       = "https://api.stlouisfed.org/fred"

TARGET_COUNTRIES  = {"US", "EU", "JP", "KR", "CN"}
IMPORTANCE_FILTER = {"high", "medium"}   # low 제외


# ── Finnhub ─────────────────────────────────────────────────────────

def _fetch_finnhub_econ(from_date: str, to_date: str) -> list:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY가 .env에 없습니다.")
    resp = requests.get(
        f"{FINNHUB_BASE}/economic-calendar",
        params={"from": from_date, "to": to_date, "token": FINNHUB_API_KEY},
        timeout=20, verify=False,
    )
    resp.raise_for_status()
    return resp.json().get("economicCalendar", [])


def collect_finnhub_econ(from_date: str, to_date: str) -> list[dict]:
    """Finnhub 경제지표 → econ_calendar 레코드 리스트."""
    raw = _fetch_finnhub_econ(from_date, to_date)
    records = []
    for ev in raw:
        country = (ev.get("country") or "").upper()
        impact  = (ev.get("impact")  or "").lower()
        if country not in TARGET_COUNTRIES or impact not in IMPORTANCE_FILTER:
            continue

        time_str = ev.get("time", "")
        if len(time_str) > 10 and " " in time_str:
            event_date = time_str[:10]
            event_time = time_str[11:16]   # "HH:MM"
        else:
            event_date = time_str[:10]
            event_time = None

        if not event_date:
            continue

        actual   = ev.get("actual")
        forecast = ev.get("estimate")
        previous = ev.get("prev")

        surprise = None
        if actual is not None and forecast is not None:
            try:
                surprise = round(float(actual) - float(forecast), 4)
            except (TypeError, ValueError):
                pass

        records.append({
            "event_date":  event_date,
            "event_time":  event_time,
            "country":     country,
            "indicator":   ev.get("event", ""),
            "period":      None,
            "actual":      actual,
            "forecast":    forecast,
            "previous":    previous,
            "surprise":    surprise,
            "importance":  impact,
            "source_id":   "finnhub",
        })
    return records


# ── FRED (US actuals 보완) ────────────────────────────────────────────

FRED_SERIES = {
    "CPIAUCSL":    {"name": "CPI YoY",         "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "CPILFESL":    {"name": "Core CPI YoY",    "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PCEPI":       {"name": "PCE",             "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PCEPILFE":    {"name": "Core PCE",        "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "PAYEMS":      {"name": "NFP",             "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "UNRATE":      {"name": "Unemployment",    "freq": "M", "importance": "high",   "release_time_et": "08:30"},
    "JTSJOL":      {"name": "JOLTS",           "freq": "M", "importance": "medium", "release_time_et": "10:00"},
    "RETAILSMNSA": {"name": "Retail Sales",    "freq": "M", "importance": "medium", "release_time_et": "08:30"},
    "INDPRO":      {"name": "Industrial Prod", "freq": "M", "importance": "medium", "release_time_et": "09:15"},
    "HOUST":       {"name": "Housing Starts",  "freq": "M", "importance": "medium", "release_time_et": "08:30"},
    "GDP":         {"name": "GDP QoQ",         "freq": "Q", "importance": "high",   "release_time_et": "08:30"},
    "FEDFUNDS":    {"name": "Fed Funds Rate",  "freq": "M", "importance": "high",   "release_time_et": None},
}

MONTHS_KR = ["1월","2월","3월","4월","5월","6월","7월","8월","9월","10월","11월","12월"]


def _format_period(obs_date: str, freq: str) -> str:
    dt = pd.Timestamp(obs_date)
    if freq == "M":
        return f"{dt.strftime('%Y-%m')} ({MONTHS_KR[dt.month - 1]})"
    elif freq == "Q":
        q = (dt.month - 1) // 3 + 1
        return f"{dt.year}Q{q}"
    return obs_date[:7]


def _advance_date(obs_date: str, freq: str) -> str:
    dt = pd.Timestamp(obs_date)
    if freq == "M":
        return (dt + pd.DateOffset(months=1)).strftime("%Y-%m-%d")
    elif freq == "Q":
        return (dt + pd.DateOffset(months=3)).strftime("%Y-%m-%d")
    return (dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _fred_get(endpoint: str, params: dict) -> dict:
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY가 .env에 없습니다.")
    params["api_key"]   = FRED_API_KEY
    params["file_type"] = "json"
    resp = requests.get(f"{FRED_BASE}/{endpoint}", params=params, timeout=20, verify=False)
    resp.raise_for_status()
    return resp.json()


def _fred_observations(series_id: str, n: int = 2) -> list:
    try:
        data = _fred_get("series/observations", {"series_id": series_id, "sort_order": "desc", "limit": n})
        return [o for o in data.get("observations", []) if o.get("value", ".") != "."]
    except Exception as e:
        print(f"  [FRED obs] {series_id}: {e}")
        return []


def _fred_release_dates(series_id: str, from_date: str, to_date: str) -> list:
    try:
        rel = _fred_get("series/release", {"series_id": series_id})
        rid = rel["releases"][0]["id"]
        data = _fred_get("release/dates", {
            "release_id": rid,
            "realtime_start": from_date,
            "realtime_end":   to_date,
            "include_release_dates_with_no_data": "false",
        })
        return sorted(d["date"] for d in data.get("release_dates", []))
    except Exception as e:
        print(f"  [FRED dates] {series_id}: {e}")
        return []


def _collect_fred_series(series_id: str, meta: dict, today_str: str, to_date: str) -> list[dict]:
    freq       = meta.get("freq", "M")
    importance = meta.get("importance", "medium")
    rel_time   = meta.get("release_time_et")

    back_date  = (pd.Timestamp(today_str) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    all_rds    = _fred_release_dates(series_id, back_date, to_date)
    obs_list   = _fred_observations(series_id, n=2)
    latest     = obs_list[0] if obs_list else None
    prev_obs   = obs_list[1] if len(obs_list) > 1 else None
    records    = []

    if latest:
        obs_date     = latest["date"]
        period       = _format_period(obs_date, freq)
        actual       = float(latest["value"])
        previous     = float(prev_obs["value"]) if prev_obs else None
        past_rds     = [rd for rd in all_rds if rd > obs_date]
        release_date = past_rds[0] if past_rds else today_str
        records.append({
            "event_date": release_date, "event_time": rel_time,
            "country": "US",           "indicator": meta["name"],
            "period": period,          "actual": actual,
            "forecast": None,          "previous": previous,
            "surprise": None,          "importance": importance,
            "source_id": series_id,
        })

    future_rds = [rd for rd in all_rds if rd > today_str]
    for i, rd in enumerate(future_rds[:2]):
        if latest:
            nxt = latest["date"]
            for _ in range(i + 1):
                nxt = _advance_date(nxt, freq)
            next_period = _format_period(nxt, freq)
        else:
            next_period = None
        prev_val = float(latest["value"]) if latest else None
        records.append({
            "event_date": rd,           "event_time": rel_time,
            "country": "US",            "indicator": meta["name"],
            "period": next_period,      "actual": None,
            "forecast": None,           "previous": prev_val,
            "surprise": None,           "importance": importance,
            "source_id": series_id,
        })
    return records


def collect_fred_supplement(today_str: str, to_date: str) -> list[dict]:
    """FRED: US 지표 actual + period 정보 보완."""
    if not FRED_API_KEY:
        print("  [FRED] FRED_API_KEY 없음 — 건너뜀")
        return []
    all_records = []
    for series_id, meta in FRED_SERIES.items():
        print(f"  [FRED] {series_id:15s} {meta['name']}")
        recs = _collect_fred_series(series_id, meta, today_str, to_date)
        all_records.extend(recs)
    return all_records


# ── 통합 수집 ─────────────────────────────────────────────────────────

def _merge_records(finnhub_recs: list[dict], fred_recs: list[dict]) -> list[dict]:
    """Finnhub + FRED 병합.

    동일 (event_date, indicator 대소문자 무시) 조합은 Finnhub 우선.
    FRED는 Finnhub에 없는 항목 또는 period 정보 보완용으로만 추가.
    """
    finnhub_keys = {
        (r["event_date"], r["indicator"].upper())
        for r in finnhub_recs
    }
    merged = list(finnhub_recs)
    for r in fred_recs:
        key = (r["event_date"], r["indicator"].upper())
        if key not in finnhub_keys:
            merged.append(r)
    return merged


def collect_econ_calendar(days_ahead: int = 14, use_fred: bool = True) -> list[dict]:
    """경제지표 캘린더 수집 (Finnhub primary + FRED secondary).

    from_date: 과거 3일 포함 (발표 직후 actual 수집 목적)
    to_date:   오늘 + days_ahead
    """
    today_str = date.today().strftime("%Y-%m-%d")
    from_date = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")
    to_date   = (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    print(f"  [Finnhub] {from_date} ~ {to_date} 경제지표 수집 중...")
    finnhub_recs = []
    try:
        finnhub_recs = collect_finnhub_econ(from_date, to_date)
        by_country = {}
        for r in finnhub_recs:
            by_country[r["country"]] = by_country.get(r["country"], 0) + 1
        print(f"  [Finnhub] 수집: {len(finnhub_recs)}건  "
              + "  ".join(f"{c}:{n}" for c, n in sorted(by_country.items())))
    except Exception as e:
        print(f"  [Finnhub ERROR] {e}")

    fred_recs = []
    if use_fred and FRED_API_KEY:
        print(f"  [FRED] US 지표 actual/period 보완 중...")
        try:
            fred_recs = collect_fred_supplement(today_str, to_date)
            print(f"  [FRED] 수집: {len(fred_recs)}건")
        except Exception as e:
            print(f"  [FRED ERROR] {e}")

    return _merge_records(finnhub_recs, fred_recs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",    type=int,  default=14,
                        help="오늘부터 수집할 향후 일수 (기본 14)")
    parser.add_argument("--no-fred", action="store_true",
                        help="FRED 보완 건너뜀 (Finnhub만 사용)")
    args = parser.parse_args()

    print(f"[collect_econ_cal] 향후 {args.days}일 경제지표 수집 중...")
    records = collect_econ_calendar(days_ahead=args.days, use_fred=not args.no_fred)
    if not records:
        print("  수집된 데이터 없음")
        return

    db = DBManager()
    n  = db.upsert_econ_event(records)
    print(f"[collect_econ_cal] 저장 완료: {n}건")

    df = pd.DataFrame(records).sort_values(
        ["event_date", "country", "importance"],
        ascending=[True, True, True],
    )
    cols = ["event_date", "event_time", "country", "indicator",
            "actual", "forecast", "previous", "importance"]
    print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
