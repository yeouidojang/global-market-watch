"""
KOSPI/KOSDAQ Market Breadth 분석 실행기
  · asia 파이프라인(16:10 KST) 이후 실행 권장
  · 출력: /home/quant/kr-market-analysis/output/kr_breadth_YYYYMMDD.xlsx
  · 로그: /home/quant/kr-market-analysis/logs/breadth_YYYYMMDD.log
  · ops 알림: SLACK_WEBHOOK_URL_MARKET_WATCH_OPS 채널

사용법:
    python run_kr_breadth.py              # 오늘 날짜로 실행
    python run_kr_breadth.py --date 2026-06-27
    python run_kr_breadth.py --force      # 기존 파일 덮어쓰기
"""

import argparse
import logging
import sys
import time
import traceback
from datetime import datetime
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GMW_DIR    = SCRIPT_DIR.parent

# .env 에서 LSEG / Slack 자격증명 로드 (collect_macro.py 와 동일 패턴)
from dotenv import load_dotenv
load_dotenv(GMW_DIR / ".env")
# output/logs 경로: 기본은 서버 경로, 로컬 PC에서는 .env의 KR_ANALYSIS_DIR로 재지정
BASE_DIR   = (GMW_DIR / (os.getenv("KR_ANALYSIS_DIR") or "../kr-market-analysis")).resolve()
LOG_DIR    = BASE_DIR / "logs"
OUT_DIR    = BASE_DIR / "output"
LOG_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY = datetime.today().strftime("%Y-%m-%d")


def setup_logging(date_str: str) -> logging.Logger:
    log_file = LOG_DIR / f"breadth_{date_str.replace('-', '')}.log"
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
    return logging.getLogger("kr_breadth")


def _send_ops(text: str):
    try:
        sys.path.insert(0, str(GMW_DIR))
        from summarize.notify_slack import send_ops
        send_ops(text)
    except Exception as e:
        print(f"[ops 알림 실패] {e}")


def already_done(date_str: str) -> bool:
    out = OUT_DIR / f"kr_breadth_{date_str.replace('-', '')}.xlsx"
    return out.exists()


def run(date_str: str, force: bool = False):
    log = setup_logging(date_str)

    if not force and already_done(date_str):
        log.info(f"[SKIP] 이미 생성됨: kr_breadth_{date_str.replace('-', '')}.xlsx (--force로 덮어쓰기)")
        return

    log.info("=" * 55)
    log.info(f"  KR Market Breadth 분석 시작  |  기준일: {date_str}")
    log.info("=" * 55)

    t0 = time.time()
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import importlib
        import kr_market_breadth as m

        m.TODAY = date_str
        importlib.reload(m)
        m.TODAY = date_str

        kp_result, kq_result, kp_sector, kq_sector = m.run()

        fname   = f"kr_breadth_{date_str.replace('-', '')}.xlsx"
        fpath   = OUT_DIR / fname
        elapsed = round(time.time() - t0)

        log.info(f"[완료] KOSPI200={len(kp_result)}개 | KOSDAQ150={len(kq_result)}개 ({elapsed}s)")
        log.info(f"[완료] 파일: output/{fname}")

        # Slack 파일 업로드
        try:
            from notify_slack_file import upload_file
            upload_file(
                file_path=fpath,
                title=f"KR Market Breadth {date_str}",
                comment=(
                    f"*KR Market Breadth 분석* `{date_str}`\n"
                    f"KOSPI200 {len(kp_result)}개 · KOSDAQ150 {len(kq_result)}개 | "
                    f"GICS 섹터 분류 · 250거래일 고가/저가 대비 현재가 & MA 이격도"
                ),
            )
            log.info("[Slack] 파일 발송 완료")
        except Exception as se:
            log.warning(f"[Slack] 발송 실패 (분석 결과는 저장됨): {se}")

        _send_ops(
            f"✅ *KR Breadth 분석 완료* `{date_str}`\n"
            f"KOSPI200 {len(kp_result)}개 | KOSDAQ150 {len(kq_result)}개 | {elapsed}s\n"
            f"파일: `{fname}`"
        )

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"[오류] {type(e).__name__}: {e}")
        log.error(tb)
        _send_ops(
            f"🚨 *KR Breadth 분석 실패* `{date_str}`\n"
            f"`{type(e).__name__}: {str(e)[:200]}`"
        )
        sys.exit(1)


def _parse():
    p = argparse.ArgumentParser(description="KR Market Breadth 분석 실행")
    p.add_argument("--date",  default=TODAY, help="기준일 YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse()
    run(args.date, args.force)
