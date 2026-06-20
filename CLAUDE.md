# Global Market Watch — Claude Code Agent 지시서

## 프로젝트 목적
아시아·유럽·미국 증시와 매크로 지표(FX·금리·원자재)를 시간대별로 수집하고,
Claude API로 시황 브리핑을 생성해 Slack으로 발송하는 자동화 파이프라인.

---

## 환경 정보
- Python 실행: `/usr/bin/python3` (Linux 서버)
- 작업 디렉토리: `/home/quant/global-market-watch`
- 환경 변수: `/home/quant/.env` → 프로젝트 루트에 심볼릭링크(`/home/quant/global-market-watch/.env`)
- DB 경로: `db/market_watch.db` (SQLite, 자동 생성)

---

## 핵심 실행 명령어

### 수동 원샷 실행 (가장 자주 사용)
```bash
# 특정 세션 전체 파이프라인 (수집 → 브리핑 → Slack 발송)
python run_session.py --session asia
python run_session.py --session global

# 특정 날짜 지정
python run_session.py --session global --date 2026-06-19

# 데이터 수집만 (LLM·Slack 생략)
python run_session.py --session global --no-llm --no-notify

# 수집 + 브리핑만 (Slack 생략)
python run_session.py --session global --no-notify
```

### 개별 모듈 실행
```bash
# 데이터 수집 (지수 + 매크로)
python exe/collect_macro.py --session asia
python exe/collect_macro.py --session all --date 2026-06-09

# 경제지표 캘린더 수집 (FRED)
python exe/collect_econ_cal.py --days 7

# 스냅샷 확인 (DB → 변동률 계산)
python summarize/build_snapshot.py

# 브리핑만 생성 (저장 없이 터미널 출력)
python summarize/llm_briefing.py --session us --no-save

# Slack 테스트 메시지
python summarize/notify_slack.py

# 상시 스케줄러 시작
python scheduler.py

# 스케줄러 즉시 테스트 (전체 세션 1회 실행)
python scheduler.py --test
python scheduler.py --test --session us
```

---

## 세션별 [3/4-pre] 종목 수집 모듈

| 세션 | 함수 | 데이터 소스 | 내용 |
|------|------|-------------|------|
| asia | `fetch_top_stocks()` | pykrx | KOSPI/KOSDAQ 주요종목(시총+거래대금) + 특징주(등락+거래량) |
| europe | `fetch_europe_stocks()` | LSEG | STOXX600 섹터 지수 10개 + DAX·FTSE·CAC 대형주 15개 |
| us | `fetch_us_stocks()` | yfinance | SPX 섹터ETF 11개 + 대형주 15개 + 특징주(급등락) |

US 세션은 추가로 당일 asia/europe 브리핑 요약을 DB에서 읽어 LLM 컨텍스트에 포함 (글로벌 통합 브리핑).


```
run_session.py --session asia
  ├── [1] exe/collect_macro.py      LSEG SDK + yfinance → DB(market_daily)
  ├── [2] (econ_cal 생략)
  ├── [3] exe/collect_stocks.py     pykrx + LSEG → KOSPI/KOSDAQ + 해외 아시아
  ├── [4] summarize/llm_briefing.py → DB(briefings) 저장
  └── [5] summarize/notify_slack.py → Slack 발송

run_session.py --session global   ← Europe(sub) + US(main) 통합
  ├── [1] exe/collect_macro.py      LSEG SDK + yfinance → DB(market_daily)
  ├── [2] exe/collect_econ_cal.py   FRED API → DB(econ_calendar)
  ├── [3] exe/collect_stocks.py     LSEG(Europe) + yfinance(US) → DB
  ├── [4] summarize/llm_briefing.py → Europe 컨텍스트 브리핑 + US 통합 브리핑
  └── [5] summarize/notify_slack.py → Slack 발송 (1건)
```

---

## 세션별 트리거 시각 (KST)

| 세션 | 트리거 | 대상 |
|------|--------|------|
| asia | 16:10 | KOSPI·KOSDAQ (main) + Nikkei·TOPIX·CSI300·HSI (sub) |
| global | 06:10 | SPX·NDX·DJIA·Russell2000 (main) + DAX·FTSE·CAC40·EuroStoxx50 (sub) |
| macro | 세션과 함께 | FX·금리·원자재·VIX (항상 전체) |

---

## config 파일 위치

| 파일 | 역할 |
|------|------|
| `config/indices.yaml` | 지역별 지수 RIC 목록 |
| `config/macro.yaml` | FX·금리·원자재·변동성 RIC/ticker |
| `config/schedule.yaml` | 스케줄 트리거 시각 |

---

## DB 테이블 구조

| 테이블 | 내용 |
|--------|------|
| `market_daily` | 지수·FX·금리·원자재 일별 OHLCV |
| `econ_calendar` | 경제지표 발표 실적·예정 |
| `briefings` | LLM 브리핑 텍스트 및 발송 상태 |

---

## 자주 요청되는 작업 예시

### 특정 날짜 데이터 재수집
```bash
python exe/collect_macro.py --session all --date 2026-06-09
```

### DB 내용 조회
```python
from db.db_manager import DBManager
db = DBManager()
df = db.get_latest_by_name()
print(df)
```

### 브리핑 텍스트 재생성 (기존 데이터 기반)
```bash
python summarize/llm_briefing.py --session us --date 2026-06-09
```

### Slack에 임의 메시지 발송
```python
from summarize.notify_slack import send_text
send_text("테스트 메시지")
```

### 새 지수 추가
`config/indices.yaml` 에서 해당 지역 아래 `ric` / `name` 항목 추가.

### 새 매크로 지표 추가
`config/macro.yaml` 에서 해당 카테고리 아래 항목 추가.
`source: lseg` 또는 `source: yfinance` 지정 필수.

---

## 오류 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| LSEG 인증 오류 | 세션 만료 | `lseg-data.config.json` 확인, VPN 연결 여부 점검 |
| `FRED_API_KEY` 없음 | .env 미설정 | `FRED_API_KEY` 발급 후 .env 추가 |
| Slack 404 no_service | Webhook URL 만료 | api.slack.com 에서 URL 재발급 후 .env 업데이트 |
| DB 스키마 오류 | db 파일 손상 | `db/market_watch.db` 삭제 후 재실행 (자동 재생성) |

---

## 주의 사항
- `.env` 파일은 절대 git commit 하지 않는다 (`.gitignore` 적용됨).
- LSEG SDK는 회사 프록시 우회 패치(`httpx`, `urllib3`)가 각 스크립트 상단에 포함됨.
- `run_session.py` 는 항상 `/home/quant/global-market-watch` 를 기준 디렉토리로 실행.
