"""
S&P 500 거래대금 급증 + 모멘텀 + EPS 상향 스크리닝 실행기
  · global 파이프라인(06:10 KST) + US Breadth(07:00 KST) 이후 실행 (07:30 KST)
  · 출력: /home/quant/us-market-analysis/output/sp500_screen_YYYYMMDD.xlsx
  · Slack: SLACK_BREADTH_CHANNEL (파일 업로드)
  · Ops : SLACK_WEBHOOK_URL_MARKET_WATCH_OPS

사용법:
    python run_sp500_screen.py              # 오늘 날짜
    python run_sp500_screen.py --date 2026-06-27
    python run_sp500_screen.py --force      # 기존 파일 덮어쓰기
"""

import argparse
import logging
import sys
import time
import traceback
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent   # global-market-watch/
load_dotenv(BASE_DIR / ".env")
_ANALYSIS_DIR = (BASE_DIR / (os.getenv("US_ANALYSIS_DIR") or "../us-market-analysis")).resolve()
LOG_DIR  = _ANALYSIS_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR  = _ANALYSIS_DIR / "output"

TODAY = datetime.today().strftime("%Y-%m-%d")


def setup_logging(date_str: str) -> logging.Logger:
    log_file = LOG_DIR / f"sp500_screen_{date_str.replace('-', '')}.log"
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
    return logging.getLogger("sp500_screen")


def _send_ops(text: str):
    try:
        sys.path.insert(0, str(BASE_DIR))
        from summarize.notify_slack import send_ops
        send_ops(text)
    except Exception as e:
        print(f"[ops 알림 실패] {e}")


def _upload_slack(file_path: Path, screened_count: int, date_str: str):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify_slack_file import upload_file
        comment = (
            f"*S&P 500 스크리닝 결과* — {date_str}\n"
            f"거래대금 급증 + 가격 모멘텀 + EPS 상향 조건 통과: *{screened_count}개 종목*\n"
            f"• 최근 5일 거래대금 > 직전 4주 평균\n"
            f"• 5일 수익률 > 0 & 현재가 > MA20\n"
            f"• 최근 1달 EPS 추정치 상향"
        )
        upload_file(
            file_path=file_path,
            title=f"S&P500 스크리닝_{date_str}",
            comment=comment,
        )
    except Exception as e:
        print(f"[Slack 파일 업로드 실패] {e}")
        raise


def already_done(date_str: str) -> bool:
    return (OUT_DIR / f"sp500_screen_{date_str.replace('-', '')}.xlsx").exists()


def run(date_str: str, force: bool = False):
    log = setup_logging(date_str)

    if not force and already_done(date_str):
        log.info(f"[SKIP] 이미 생성된 파일 존재: sp500_screen_{date_str.replace('-', '')}.xlsx")
        return

    log.info("=" * 60)
    log.info(f"  S&P 500 스크리닝 시작  |  기준일: {date_str}")
    log.info("=" * 60)

    t0 = time.time()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import importlib
        import sp500_volume_screen as m
        m.TODAY = date_str
        importlib.reload(m)
        m.TODAY = date_str

        screened, file_path = m.run(date_str)
        elapsed = round(time.time() - t0, 1)

        log.info(f"\n✅ 완료 ({elapsed}s) — 통과 종목: {len(screened)}개")

        # Slack 파일 업로드
        _upload_slack(file_path, len(screened), date_str)

        # Ops 성공 알림
        _send_ops(
            f"✅ S&P500 스크리닝 완료 ({date_str})\n"
            f"통과 종목: {len(screened)}개 | 소요: {elapsed}s\n"
            f"파일: {file_path.name}"
        )

    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        tb = traceback.format_exc()
        log.error(f"[오류] {e}\n{tb}")
        _send_ops(
            f"❌ S&P500 스크리닝 실패 ({date_str})\n"
            f"{type(e).__name__}: {str(e)[:200]}\n"
            f"소요: {elapsed}s"
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default=TODAY)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    run(args.date, args.force)
