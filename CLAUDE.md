> **워크스페이스 공통**: [`../CLAUDE.md`](../CLAUDE.md) · 실행환경 [`../docs/ENVIRONMENTS.md`](../docs/ENVIRONMENTS.md) · 데이터소스 [`../docs/DATA-SOURCES.md`](../docs/DATA-SOURCES.md) · 규약 [`../docs/CONVENTIONS.md`](../docs/CONVENTIONS.md)
> **실행 문서**: [`../exec-docs/INDEX.md`](../exec-docs/INDEX.md) — 작업은 exec-doc 단위로 돈다 (`/capture` 정리, `/exec` 실행, `/wrap` 마감)

# Global Market Watch

아시아·유럽·미국 증시와 매크로 지표(FX·금리·원자재)를 수집해 DB에 쌓고, 그 위에서
(1) Claude API 시황 브리핑, (2) US/KR 마켓 브레드스, (3) S&P 500 스크리닝을 만든다.

## 실행 환경 — 로컬 Windows `.venv`가 유일하다

- **Linux 운영 서버(`/home/quant/...`)는 2026-09-29 폐지됐다.** 서버 스케줄·경로는 더 이상 고려 대상이 아니다.
- 인터프리터: `.venv\Scripts\python.exe` (py3.12 — anthropic·lseg·yfinance·apscheduler·weasyprint·slack_sdk·exchange_calendars·pykrx 설치됨)
- 항상 `$env:PYTHONIOENCODING="utf-8"`을 붙이고 이 폴더에서 실행한다.
- DB: `db/market_watch.db` (SQLite, 자동 생성, git 제외)
- 산출물: `reports/us-market-analysis/`, `reports/kr-market-analysis/` (git 제외). 기본 경로가 repo 기준이라 환경변수는 필요 없다.
  다른 곳에 쓰려면 `US_ANALYSIS_DIR` / `KR_ANALYSIS_DIR`로 덮어쓴다.
- LSEG: `~/lseg-data.config.json` 세션 필요 (회사 프록시 우회 패치가 각 스크립트 상단에 있다).

### 알려진 제약

- **Slack 발송 불가** — 사내 웹필터가 slack.com·hooks.slack.com을 403 차단한다. 업로드 실패는 경고만 내고 산출물 저장에는 영향 없다.
- **프로젝트 `.env` 없음** — 코드는 `global-market-watch/.env`를 읽는데 파일이 없다. 키는 루트 `../.env`에만 있다.
  LLM 브리핑·`run_investment_points`처럼 키가 필요한 경로는 돌리기 전에 확인할 것. (Slack 채널 변수명도 `SLACK_BREADTH_CHANNEL` vs 루트의 `SLACK_CHANNEL_ID`로 어긋나 있다.)
- **yfinance 미확정 봉** — 미국 장 마감 후 수 시간은 최신 봉이 비거나 일부 종목만 온다. US 수집은 KST 오전 10시 이후에 돌리고,
  "미확정" 예외가 나면 억지로 넘기지 말고 나중에 다시 돌린다 (커버리지 95% 미만 날짜는 코드가 저장을 거부한다).
- `scheduler.py`(APScheduler 상시 실행)는 서버용으로 만든 것이다. 경로는 로컬에서도 풀리게 고쳐 뒀지만 지금은 쓰지 않는다 — 수동 실행이 기본.

---

## US 분석 (가장 자주 돌리는 것)

상세 프로세스·검증 기록·체크리스트는 **[`us_breadth/SP500_SCREEN.md`](us_breadth/SP500_SCREEN.md)**. 요약:

```powershell
$env:PYTHONIOENCODING="utf-8"
.venv\Scripts\python.exe exe\sync_us_data.py --dry-run        # 무엇이 비었는지
.venv\Scripts\python.exe exe\sync_us_data.py                  # 가격(빈 개장일+최근 3일) + EPS(빈 금요일, LSEG)
.venv\Scripts\python.exe us_breadth\run_breadth.py --date YYYY-MM-DD       # → us_breadth_YYYYMMDD.xlsx
.venv\Scripts\python.exe us_breadth\run_sp500_screen.py --date YYYY-MM-DD  # → sp500_screen_YYYYMMDD.xlsx
.venv\Scripts\python.exe us_breadth\run_sp500_screen_history.py           # 최근 3개월 일괄 + 연속편입 요약
```

- `--date`로 과거 날짜를 다시 돌려도 그날 시점 데이터만 읽는다 (2026-09-30 수정).
- `run_investment_points.py`는 Claude API + web_search를 쓴다 — 비용이 드니 요청받을 때만.
- SPX 구성종목: `exe/collect_spx_constituents.py` (월 1회, 3주차 금요일 다음 월요일 — `exe/run_spx_constituents_monthly.sh`).

## KR 브레드스

```powershell
.venv\Scripts\python.exe kr_breadth\run_kr_breadth.py --date YYYY-MM-DD   # → reports/kr-market-analysis/output/kr_breadth_YYYYMMDD.xlsx
```

## 시황 브리핑 세션 (`run_session.py`)

```powershell
.venv\Scripts\python.exe run_session.py --session asia                         # 수집 → 브리핑 → Slack
.venv\Scripts\python.exe run_session.py --session global --date 2026-06-19
.venv\Scripts\python.exe run_session.py --session global --no-llm --no-notify  # 수집만
.venv\Scripts\python.exe run_session.py --session global --no-notify           # 수집 + 브리핑
```

```
run_session.py --session asia
  ├── [1] exe/collect_macro.py      LSEG SDK + yfinance → DB(market_daily)
  ├── [2] (econ_cal 생략)
  ├── [3] exe/collect_stocks.py     pykrx + LSEG → KOSPI/KOSDAQ + 해외 아시아
  ├── [4] summarize/llm_briefing.py → DB(briefings) 저장
  └── [5] summarize/notify_slack.py → Slack 발송 (현재 차단됨)

run_session.py --session global   ← Europe(sub) + US(main) 통합
  ├── [1] exe/collect_macro.py      LSEG SDK + yfinance → DB(market_daily)
  ├── [2] exe/collect_econ_cal.py   FRED API → DB(econ_calendar)
  ├── [3] exe/collect_stocks.py     LSEG(Europe) + yfinance(US) → DB
  ├── [4] summarize/llm_briefing.py → Europe 컨텍스트 브리핑 + US 통합 브리핑
  └── [5] summarize/notify_slack.py → Slack 발송 (현재 차단됨)
```

| 세션 | 함수 | 데이터 소스 | 내용 |
|------|------|-------------|------|
| asia | `fetch_top_stocks()` | pykrx | KOSPI/KOSDAQ 주요종목(시총+거래대금) + 특징주(등락+거래량) |
| europe | `fetch_europe_stocks()` | LSEG | STOXX600 섹터 지수 10개 + DAX·FTSE·CAC 대형주 15개 |
| us | `fetch_us_stocks()` | yfinance | SPX 섹터ETF 11개 + 대형주 15개 + 특징주(급등락) |

US 세션은 당일 asia/europe 브리핑 요약을 DB에서 읽어 LLM 컨텍스트에 넣는다 (글로벌 통합 브리핑).

### 개별 모듈

```powershell
.venv\Scripts\python.exe exe\collect_macro.py --session all --date 2026-06-09   # 지수·매크로 재수집
.venv\Scripts\python.exe exe\collect_econ_cal.py --days 7                       # FRED 경제지표 캘린더
.venv\Scripts\python.exe summarize\build_snapshot.py                            # DB → 변동률 스냅샷
.venv\Scripts\python.exe summarize\llm_briefing.py --session us --no-save       # 브리핑 터미널 출력
```

---

## 참고

| 파일 | 내용 |
|------|------|
| `PIPELINE.md` | 세션 파이프라인·DB 스키마·DB 메서드 상세 |
| `ASIA_PHASE2_3.md` | 아시아 세션 확장 설계 |
| `us_breadth/SP500_SCREEN.md` | S&P 500 스크리닝 프로세스·검증 기록 |
| `config/indices.yaml` | 지역별 지수 RIC (`ric`/`name` 추가) |
| `config/macro.yaml` | FX·금리·원자재·변동성 (`source: lseg` 또는 `yfinance` 필수) |
| `config/schedule.yaml` | 스케줄러 트리거 시각 (서버 시절 값) |

| DB 테이블 | 내용 |
|--------|------|
| `market_daily` | 지수·FX·금리·원자재·US 종목 일별 OHLCV |
| `stocks_daily` | 세션별 종목 스크리닝 결과 |
| `eps_cache` | LSEG NTM EPS 주간(금요일) 스냅샷 + 1W/1M/3M 변화율 |
| `spx_constituents` | S&P 500 구성종목 월간 스냅샷 |
| `econ_calendar` | 경제지표 발표 실적·예정 |
| `briefings` | LLM 브리핑 텍스트 및 발송 상태 |

## 오류 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| LSEG 인증 오류 | 세션 만료 | `~/lseg-data.config.json` 확인, VPN 점검 |
| "US 종목 데이터 미확정" | yfinance 최신 봉 미확정 | 몇 시간 뒤 재실행 (강제로 넘기지 말 것) |
| `FRED_API_KEY` 없음 | 프로젝트 `.env` 없음 | 루트 `.env`에 있는지 확인 후 로드 경로 점검 |
| Slack 403 / 필터 HTML | 사내 웹필터 | 해결 불가 — 산출물 파일로 전달 |
| DB 스키마 오류 | db 파일 손상 | `db/market_watch.db` 백업 후 재생성 (자동) |

`.env`는 절대 커밋하지 않는다.
