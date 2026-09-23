"""
S&P500 투자포인트 자동 조사 실행기
  · sp500_screen (07:30 KST) 완료 후 실행 (08:00 KST)
  · Claude API + web_search 로 종목별 투자포인트 조사 → Excel → Slack
  · 로그: /home/quant/us-market-analysis/logs/investment_points_YYYYMMDD.log
  · Ops: SLACK_WEBHOOK_URL_MARKET_WATCH_OPS

사용법:
    python run_investment_points.py              # 오늘 날짜
    python run_investment_points.py --date 2026-06-30
    python run_investment_points.py --force      # 기존 파일 덮어쓰기
"""

import argparse
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # global-market-watch/
LOG_DIR  = Path("/home/quant/us-market-analysis/logs")
OUT_DIR  = Path("/home/quant/us-market-analysis/output")
LOG_DIR.mkdir(parents=True, exist_ok=True)

TODAY = datetime.today().strftime("%Y-%m-%d")


def setup_logging(date_str: str) -> logging.Logger:
    log_file = LOG_DIR / f"investment_points_{date_str.replace('-', '')}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("investment_points")


def _send_ops(text: str):
    try:
        sys.path.insert(0, str(BASE_DIR))
        from summarize.notify_slack import send_ops
        send_ops(text)
    except Exception as e:
        print(f"[ops 알림 실패] {e}")


def already_done(date_str: str) -> bool:
    return (OUT_DIR / f"sp500_investment_points_{date_str.replace('-', '')}.xlsx").exists()


def run(date_str: str, force: bool = False):
    log = setup_logging(date_str)

    if not force and already_done(date_str):
        log.info(f"[SKIP] 이미 생성된 파일 존재: sp500_investment_points_{date_str.replace('-', '')}.xlsx")
        return

    log.info("=" * 60)
    log.info(f"  S&P500 투자포인트 조사 시작  |  기준일: {date_str}")
    log.info("=" * 60)

    t0 = time.time()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import importlib
        import investment_points_research as m
        m.TODAY = date_str
        importlib.reload(m)
        m.TODAY = date_str

        fpath = m.run(date_str)
        elapsed = round(time.time() - t0, 1)

        log.info(f"\n✅ 완료 ({elapsed}s) — {fpath.name}")
        _send_ops(
            f"✅ S&P500 투자포인트 완료 ({date_str})\n"
            f"파일: {fpath.name} | 소요: {elapsed}s"
        )

    except FileNotFoundError as e:
        elapsed = round(time.time() - t0, 1)
        log.warning(f"[SKIP] {e}")
        _send_ops(f"⚠️ S&P500 투자포인트 스킵 ({date_str})\n{e}")

    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        tb = traceback.format_exc()
        log.error(f"[오류] {e}\n{tb}")
        _send_ops(
            f"❌ S&P500 투자포인트 실패 ({date_str})\n"
            f"{type(e).__name__}: {str(e)[:200]}\n소요: {elapsed}s"
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default=TODAY)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run(args.date, args.force)
