"""
DB에서 최신 데이터를 읽어 스냅샷(변동률·플래그) 생성

반환 구조:
  {
    "date": "2026-06-10",
    "session": "us",
    "indices": { "SP500": {"close": 5800, "chg_pct": 0.52, "flag": ""}, ... },
    "macro":   { "USD_KRW": {"close": 1380, "chg_pct": -0.1, "flag": ""}, ... },
    "econ_upcoming": [{"event_date":..., "indicator":..., "forecast":...}, ...]
  }
"""

import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pandas as pd
from db.db_manager import DBManager


# 이상 변동 임계값
THRESHOLDS = {
    "index":      3.0,   # ±3% 이상이면 🚨
    "fx":         1.5,
    "rate":       0.15,  # 절대값 bp 단위가 아닌 % 기준
    "commodity":  3.0,
    "volatility": 10.0,
}


def compute_chg_pct(current: float, prev: float) -> float | None:
    if prev and prev != 0 and current is not None:
        return round((current - prev) / abs(prev) * 100, 2)
    return None


def flag(chg_pct: float | None, category: str) -> str:
    if chg_pct is None:
        return ""
    thresh = THRESHOLDS.get(category, 3.0)
    if chg_pct >= thresh:
        return "🚨+"
    if chg_pct <= -thresh:
        return "🚨-"
    return ""


def build_snapshot(session: str, target_date: str = None, stocks_data: dict = None) -> dict:
    """
    session  : asia | europe | us
    target_date : YYYY-MM-DD (기본: 오늘)
    """
    today = target_date or date.today().strftime("%Y-%m-%d")
    db    = DBManager()

    # 최근 5거래일 데이터 로드
    all_names_df = db.get_latest_by_name()
    if all_names_df.empty:
        return {"date": today, "session": session, "indices": {}, "macro": {}, "econ_upcoming": []}

    all_names = all_names_df["name"].tolist()

    # 직전 2일치 wide pivot
    df = db.get_recent(all_names, n_days=5)
    if df.empty:
        return {"date": today, "session": session, "indices": {}, "macro": {}, "econ_upcoming": []}

    df = df.sort_values("date")

    # 날짜별 pivot
    pivot = df.pivot_table(index="date", columns="name", values="close", aggfunc="last")
    dates_sorted = sorted(pivot.index.tolist())

    def get_close(name, d):
        try:
            v = pivot.loc[d, name]
            return None if pd.isna(v) else float(v)
        except (KeyError, TypeError, ValueError):
            return None

    def latest_two(name):
        vals = [(d, get_close(name, d)) for d in dates_sorted if get_close(name, d) is not None]
        padded = [(None, None), (None, None)] + vals
        return padded[-2:]

    result_indices = {}
    result_macro   = {}

    for _, row in all_names_df.iterrows():
        name = row["name"]
        cat  = row["category"]
        sess = row["session"]

        two = latest_two(name)
        prev_date, prev_val = two[0]
        cur_date,  cur_val  = two[1]

        chg = compute_chg_pct(cur_val, prev_val)
        fl  = flag(chg, cat)

        entry = {
            "close":    round(cur_val, 4) if cur_val is not None else None,
            "prev":     round(prev_val, 4) if prev_val is not None else None,
            "date":     cur_date,
            "chg_pct":  chg,
            "flag":     fl,
            "category": cat,
        }

        if cat == "index":
            result_indices[name] = entry
        else:
            result_macro[name] = entry

    # 경제지표 향후 3일
    econ_df = db.get_upcoming_events(today, days=3)
    econ_upcoming = econ_df.to_dict("records") if not econ_df.empty else []

    return {
        "date":          today,
        "session":       session,
        "indices":       result_indices,
        "macro":         result_macro,
        "econ_upcoming": econ_upcoming,
        "stocks":        stocks_data or {},
    }


def format_snapshot_text(snapshot: dict) -> str:
    """스냅샷 딕셔너리를 사람이 읽기 좋은 텍스트로 변환 (LLM 프롬프트 입력용)."""
    lines = []
    lines.append(f"=== {snapshot['date']} {snapshot['session'].upper()} 세션 시장 데이터 ===\n")

    # 지수
    today = snapshot["date"]
    lines.append("[주요 지수]")
    for name, v in snapshot["indices"].items():
        chg_str   = f"{v['chg_pct']:+.2f}%" if v["chg_pct"] is not None else "N/A"
        close_str = f"{v['close']:>10.2f}" if v["close"] is not None else "       N/A"
        date_tag  = "" if v["date"] == today else f"  ※전일종가({v['date']})"
        lines.append(f"  {name:15s}  {close_str}  ({chg_str})  {v['flag']}{date_tag}")

    # 매크로 — 카테고리별 그룹
    lines.append("\n[매크로]")
    for cat in ("fx", "rate", "commodity", "volatility"):
        items = {k: v for k, v in snapshot["macro"].items() if v["category"] == cat}
        if not items:
            continue
        lines.append(f"  [{cat.upper()}]")
        for name, v in items.items():
            chg_str   = f"{v['chg_pct']:+.2f}%" if v["chg_pct"] is not None else "N/A"
            close_str = f"{v['close']:>10.4f}" if v["close"] is not None else "       N/A"
            lines.append(f"    {name:15s}  {close_str}  ({chg_str})  {v['flag']}")

    # 주요 종목 / 특징주
    stocks = snapshot.get("stocks", {})
    if stocks.get("major"):
        lines.append("\n[KOSPI/KOSDAQ 주요 종목 (시총+거래대금 기준)]")
        for s in stocks["major"]:
            chg = f"{s['chg_pct']:+.2f}%"
            val = f"{s['trade_val']/1e8:.0f}억"
            cap = f"{s['mktcap']/1e12:.1f}조"
            lines.append(f"  {s['name']:12s}  {s['market']:6s}  {s['close']:>8,}  {chg:>7s}  거래대금 {val}  시총 {cap}")

    if stocks.get("featured"):
        lines.append("\n[KOSPI/KOSDAQ 특징주 (등락률+거래량급증)]")
        for s in stocks["featured"]:
            chg = f"{s['chg_pct']:+.2f}%"
            val = f"{s['trade_val']/1e8:.0f}억"
            lines.append(f"  {s['name']:12s}  {s['market']:6s}  {s['close']:>8,}  {chg:>7s}  거래대금 {val}  [{s['signal']}]")

    # 경제지표
    if snapshot["econ_upcoming"]:
        lines.append("\n[향후 3일 경제지표 발표]")
        for ev in snapshot["econ_upcoming"]:
            fc = f"예상 {ev['forecast']}" if ev.get("forecast") else ""
            lines.append(f"  {ev['event_date']}  {ev['country']:3s}  {ev['indicator']:25s}  {fc}")

    return "\n".join(lines)


if __name__ == "__main__":
    import json
    snap = build_snapshot("us")
    print(format_snapshot_text(snap))
