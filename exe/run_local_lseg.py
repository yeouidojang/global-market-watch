"""Local-PC runner for global-market-watch collectors that need LSEG.

Opens the LSEG platform session in-process from the repo's .env (the same
credentials the Droplet uses) and sets it as the library default, so the
collectors' own `if open_state != "Opened": ld.open_session(config)` branch is
skipped. Nothing is written to lseg-data.config.json.

usage:
  exe/run_local_lseg.py spx  [YYYY-MM-DD]
  exe/run_local_lseg.py eps  YYYY-MM-DD [YYYY-MM-DD ...]   # oldest first
"""
import os
import sys
import time
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
GMW = Path(__file__).resolve().parent.parent   # repo root
os.chdir(GMW)
sys.path.insert(0, str(GMW))
from dotenv import load_dotenv
load_dotenv(GMW / ".env")

import lseg.data as ld


def open_platform_from_env():
    s = ld.session.platform.Definition(
        app_key=os.environ["LSEG_APP_KEY"],
        grant=ld.session.platform.GrantPassword(
            username=os.environ["LSEG_USERNAME"], password=os.environ["LSEG_PASSWORD"]),
        signon_control=True,
    ).get_session()
    s.open()
    if s.open_state.name != "Opened":
        raise RuntimeError(f"LSEG session state = {s.open_state.name}")
    ld.session.set_default(s)
    print(f"[lseg] platform session opened from .env")
    return s


def cmd_spx(date_str):
    from exe import collect_spx_constituents as c
    c.run(date_str)


def cmd_eps(dates):
    from db.db_manager import DBManager
    from exe.collect_stocks import _lseg_screener, calc_eps_changes
    tickers = DBManager().get_latest_spx_constituents()
    if not tickers:
        raise RuntimeError("spx_constituents empty - run `spx` first")
    print(f"[eps] tickers={len(tickers)} dates={dates}")
    for d in sorted(dates):
        t0 = time.time()
        meta = _lseg_screener(tickers, fetch_eps=True, target_date=d, eps_universe=tickers)
        estimates = {t: m.get("eps_mean_est") for t, m in meta.items() if m.get("eps_mean_est") is not None}
        changes = calc_eps_changes(d, estimates)
        records = [{
            "ticker": t, "eps_mean_est": est,
            "eps_chg_1w": changes.get(t, {}).get("eps_chg_1w"),
            "eps_chg_1m": changes.get(t, {}).get("eps_chg_1m"),
            "eps_chg_3m": changes.get(t, {}).get("eps_chg_3m"),
            "fetched_date": d,
        } for t, est in estimates.items()]
        saved = DBManager().upsert_eps_cache(records)
        has_3m = sum(1 for r in records if r["eps_chg_3m"] is not None)
        print(f"[eps] {d}: {len(records)} estimates (3M computed {has_3m}) -> upsert {saved} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    cmd = sys.argv[1]
    s = open_platform_from_env()
    try:
        if cmd == "spx":
            cmd_spx(sys.argv[2] if len(sys.argv) > 2 else time.strftime("%Y-%m-%d"))
        elif cmd == "eps":
            cmd_eps(sys.argv[2:])
        else:
            raise SystemExit(f"unknown command {cmd}")
    finally:
        try:
            s.close()
        except Exception:
            pass
