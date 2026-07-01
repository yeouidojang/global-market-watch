#!/usr/bin/env bash
# 매월 세번째 금요일(지수 리밸런싱 기준일) 다음 월요일에만 SPX 구성종목 수집을 실행.
# cron은 "n번째 요일" 스케줄을 지원하지 않으므로 매주 월요일에 호출되고,
# 이번 주 금요일(오늘-3일)이 그 달의 3주차(15~21일)가 아니면 조용히 종료한다.
set -euo pipefail

FRI_DOM=$(date -d "-3 days" +%-d)

if [ "$FRI_DOM" -lt 15 ] || [ "$FRI_DOM" -gt 21 ]; then
    exit 0
fi

cd /home/quant/global-market-watch
exec /home/quant/global-market-watch/.venv/bin/python exe/collect_spx_constituents.py
