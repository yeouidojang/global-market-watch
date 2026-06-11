# Global Market Watch — 파이프라인 정리 문서

> 최종 수정: 2026-06-11
> 환경: `C:/mquant/global_market_watch` · Python `meritzquant` conda env · SQLite DB

---

## 1. 전체 구조

```
run_session.py
  │
  ├── [1/4] exe/collect_macro.py       지수 · 매크로 수집 → market_daily
  ├── [2/4] exe/collect_econ_cal.py    경제지표 캘린더 수집 → econ_calendar  [asia·us만]
  ├── [3/4-pre] exe/collect_stocks.py  종목 스크리닝 수집 → market_daily · stocks_daily · eps_cache
  ├── [3/4] summarize/llm_briefing.py  Claude API 브리핑 생성 → briefings
  │         └── summarize/build_snapshot.py  DB → 스냅샷 텍스트 조합
  └── [4/4] summarize/notify_slack.py  Slack Webhook 발송
```

---

## 2. 세션별 트리거 및 대상 (KST)

| 세션 | 트리거 | 주요 대상 |
|------|--------|-----------|
| **asia** | 15:40 | KOSPI · KOSDAQ · Nikkei225 · TOPIX · CSI300 · HangSeng · HSCEI |
| **europe** | 01:40 | DAX · FTSE100 · CAC40 · EuroStoxx50 + STOXX600 섹터 10개 |
| **us** | 06:10 | SP500 · Nasdaq100 · DJIA · Russell2000 + 섹터ETF 11개 · SPX 구성종목 503개 |

경제지표 캘린더는 **asia · us** 세션에서만 수집 (FRED API, 5일 ahead).

---

## 3. 스텝별 상세

### [1/4] 지수 · 매크로 수집 — `exe/collect_macro.py`

| 항목 | 소스 | 설정 파일 |
|------|------|-----------|
| KOSPI · KOSDAQ | pykrx | `config/indices.yaml` |
| Nikkei225 · CSI300 · HSI · HSCEI | yfinance | `config/indices.yaml` |
| TOPIX | LSEG SDK | `config/indices.yaml` |
| DAX · FTSE100 · CAC40 · EuroStoxx50 | yfinance | `config/indices.yaml` |
| SP500 · Nasdaq100 · DJIA · Russell2000 | yfinance | `config/indices.yaml` |
| FX (USD/KRW · JPY · EUR · CNY) | LSEG | `config/macro.yaml` |
| 금리 (US10Y · US2Y · KR3Y · JP10Y) | LSEG | `config/macro.yaml` |
| 원자재 (Brent · WTI · Gold · Copper) | LSEG | `config/macro.yaml` |
| 변동성 (VIX · MOVE) | yfinance | `config/macro.yaml` |

- 7일치 시계열 fetch → `market_daily` upsert
- 등락률은 DB에서 전일 종가와 비교 계산 (`build_snapshot.py`)

---

### [2/4] 경제지표 캘린더 — `exe/collect_econ_cal.py`

FRED API 기반 (asia · us 세션만). CPI · Core CPI · PCE · Core PCE · NFP · 실업률 · GDP · JOLTS · 소매판매 · 산업생산 · 주택착공 · Fed Funds Rate (12개).

---

### [3/4-pre] 종목 스크리닝 — `exe/collect_stocks.py`

#### ASIA — `fetch_top_stocks()`

| 항목 | 내용 |
|------|------|
| 소스 | pykrx |
| 대상 | KOSPI + KOSDAQ 전 종목 (~2,769개) |
| DB 저장 | 전 종목 OHLCV → `market_daily` |
| 시장 폭 | 상승/하락/보합 수, ADR, 거래대금 가중 평균 등락률 |
| 전체 수급 | 외국인·기관 순매수 합계 (`inv_map` 집계) |
| 종목별 수급 | pykrx `get_market_net_purchases_of_equities_by_ticker` → `inv_map` |
| 스크리닝 | 시총+거래대금 합산 TOP 10 (major) / \|등락률\|+회전율 TOP 10 (featured) |
| 반환 키 | `major`, `featured`, `investor_flow`, `breadth`, `market_flow` |

#### EUROPE — `fetch_europe_stocks()`

| 항목 | 내용 |
|------|------|
| 소스 (섹터) | LSEG SDK |
| 소스 (종목) | yfinance |
| 섹터 지수 | STOXX600 섹터 10개 → `market_daily` (LSEG) |
| 종목 유니버스 | DAX40 + FTSE100 주요 30 + CAC40 = 110개 config, ~104개 실수신 |
| DB 저장 | 전 종목 × 7일 OHLCV → `market_daily` |
| 시장 폭 | 상승/하락/보합 수, ADR (통화 혼재로 가중 등락률 생략) |
| top_stocks | `\|등락률\|` 상위 15개 + 지수 레이블(DAX/FTSE/CAC) |
| 반환 키 | `sectors`, `top_stocks`, `breadth` |

#### US — `fetch_us_stocks()`

| 항목 | 내용 |
|------|------|
| 소스 (가격) | yfinance |
| 소스 (시총·EPS) | LSEG SDK |
| 종목 유니버스 | SPX 구성종목 503개 (`spx_constituents_prices_*.csv`) |
| DB 저장 | 전 종목 × 7일 OHLCV → `market_daily` |
| 시장 폭 | 상승/하락/보합 수, ADR, 거래대금 가중 평균 등락률 |
| 스크리닝 4종 | mktcap_top / tradeval_top / turnover_surge / eps_revision |
| 반환 키 | `sectors`, `mktcap_top`, `tradeval_top`, `turnover_surge`, `eps_revision`, `breadth` |

**US 스크리닝 4종**

| 카테고리 | 기준 | 건수 |
|----------|------|------|
| `mktcap_top` | LSEG 시가총액 상위 | 15개 |
| `tradeval_top` | 거래대금(close×volume) 상위 | 15개 |
| `turnover_surge` | 거래대금 급증(5일평균 ≥1.5x) + \|등락률\| ≥2% | 최대 15개 |
| `eps_revision` | EPS 추정치 1M 변화 상향 Top5 + 하향 Top5 | 최대 10개 |

**EPS 캐시 (`eps_cache` 테이블)**

- LSEG `TR.MeanPctChg` (`EstimateMeasure=EPS, Period=NTM`)
  - `eps_chg_1m`: WP=30d (핵심)
  - `eps_chg_1w`: WP=7d (서브)
- 대상: SPX 전체 유니버스, 배치 20개씩
- 주기: **7일 캐시** — 만료 시에만 LSEG 재조회

---

### [3/4] LLM 브리핑 생성 — `summarize/llm_briefing.py`

```
generate_briefing()
  ├── stocks_data 없으면 DB(stocks_daily) 로드 (재생성 시 재수집 불필요)
  ├── breadth/market_flow 없으면 DB 재계산 (get_market_breadth / get_market_flow)
  ├── build_snapshot()   DB → 지수·매크로·경제지표 스냅샷
  ├── build_prompt()     세션별 포맷 + 시장구조 데이터 삽입
  └── Claude API 호출   → 브리핑 텍스트 → briefings 저장
```

- 모델: `claude-opus-4-5`, max_tokens: 3,000
- US 세션: 당일 asia·europe 브리핑 앞 400자를 컨텍스트로 추가 전달

**시장 구조 데이터 주입 (`_fmt_market_structure`)**

| 세션 | breadth 레이블 | market_flow |
|------|---------------|-------------|
| asia | KOSPI+KOSDAQ | 외국인·기관 합계 (원화) |
| europe | DAX/FTSE/CAC | 없음 |
| us | SPX | 없음 |

LLM 프롬프트에 자동 삽입 예시:
```
**KOSPI+KOSDAQ 시장 폭**  상승 1243/2769(44.9%)  하락 1401/2769  ADR 0.89  거래대금가중등락 -0.31%
**시장 전체 수급**  외국인 +2,340억  기관 -1,890억
```

**브리핑 출력 섹션 (ASIA 기준)**

1. 핵심 요약
2. 주요 지수 동향 (한국·일본·중국·홍콩)
3. 매크로
4. KOSPI/KOSDAQ 주요 종목 및 특징주 (시장 폭·수급 포함)
5. 향후 주목 이벤트
6. 운용 포인트

**브리핑 출력 섹션 (US 기준)**

1. 핵심 요약
2. 미국 지수 동향
3. 섹터 분석 (시장 폭 참고)
4. SPX 섹터 ETF 성과
5. 시총 상위 종목
6. 매크로
7. 거래대금 상위 / 급증 특징주 / EPS 추정치 변화
8. 향후 주목 이벤트
9. 글로벌 운용 포인트

---

### [4/4] Slack 발송 — `summarize/notify_slack.py`

- `---` 구분자 기준 섹션 분리 → 섹션별 개별 메시지 발송
- Markdown → Slack mrkdwn 변환
- 섹션당 최대 3,000자
- 발송 완료 후 `briefings.notified = 1` 업데이트

---

## 4. DB 스키마 (`db/market_watch.db`)

### `market_daily` — 시계열 가격 데이터

| 컬럼 | 타입 | 설명 |
|------|------|------|
| date | TEXT | YYYY-MM-DD |
| session | TEXT | asia \| europe \| us \| macro |
| category | TEXT | index \| stock \| sector \| fx \| rate \| commodity \| volatility |
| name | TEXT | 식별자 (KOSPI, AAPL, SAP.DE 등) |
| close / open / high / low | REAL | 가격 |
| volume | REAL | 거래량 |
| **PK** | | (date, name) |

**저장 규모**

| session | category | 종목 수 | 보유 일수 |
|---------|----------|---------|----------|
| us | stock | ~503개 | ~7일 |
| europe | stock | ~104개 | ~7일 |
| asia | stock | ~2,769개 | 당일 |
| europe | sector | 10개 | 당일 (LSEG) |
| 지수·매크로 | index/fx/rate 등 | 30여개 | ~7일 |

---

### `stocks_daily` — 세션별 스크리닝 결과

| 컬럼 | 설명 |
|------|------|
| date, session, category | 식별자 |
| ticker, name, market | 종목 정보 |
| close, chg_pct | 가격·등락률 |
| trade_val, dollar_vol_b | 거래대금 (원화 / USD B) |
| mktcap, mktcap_b | 시가총액 |
| turnover | 거래량 회전율 (asia) |
| surge_ratio | 거래대금 급증 배수 (us) |
| eps_chg_1m, eps_chg_1w | EPS 추정치 변화율 1M/1W |
| return_7d | 7일 누적 수익률 (eps_revision) |
| foreign_net, inst_net | 외국인·기관 순매수 원화 (asia) |
| signal | 시그널 태그 |
| **UNIQUE** | (date, session, category, ticker) |

**category 값**

| category | 세션 | 내용 |
|----------|------|------|
| major | asia | 시총+거래대금 TOP 10 |
| featured | asia | 등락률+회전율 TOP 10 |
| investor_flow | asia | 전 종목 외인·기관 순매수 |
| sectors | europe / us | 섹터 지수·ETF |
| top_stocks | europe | DAX/FTSE/CAC 등락상위 15 |
| mktcap_top | us | 시총 상위 15 |
| tradeval_top | us | 거래대금 상위 15 |
| turnover_surge | us | 거래대금 급증 특징주 |
| eps_revision | us | EPS 추정치 변화 상위·하위 |

---

### `eps_cache` — EPS 추정치 주간 캐시

| 컬럼 | 설명 |
|------|------|
| ticker | yfinance ticker (PK) |
| eps_chg_1m | 30일 EPS 변화율 % (핵심) |
| eps_chg_1w | 7일 EPS 변화율 % (서브) |
| fetched_date | 수집 기준일 |

- 7일 이내면 캐시 재사용, 만료 시 전체 SPX 유니버스 재조회

---

### `econ_calendar` — 경제지표 발표 일정

| 컬럼 | 설명 |
|------|------|
| event_date | 실제 발표일 (FRED release date) |
| event_time | 발표 시각 ET |
| country | US / KR / EU / JP |
| indicator | CPI, NFP 등 |
| period | 기준 기간 |
| actual / forecast / previous | 실적·예상·이전 |
| importance | high \| medium \| low |
| **UNIQUE** | (event_date, country, indicator) |

---

### `briefings` — LLM 브리핑 텍스트

| 컬럼 | 설명 |
|------|------|
| date | 브리핑 기준일 |
| session | asia \| europe \| us |
| model | 사용 모델명 |
| prompt_tokens / output_tokens | 토큰 사용량 |
| content | 브리핑 전문 (Markdown) |
| notified | Slack 발송 여부 (0/1) |

---

## 5. 주요 DB 메서드 (`db/db_manager.py`)

| 메서드 | 설명 |
|--------|------|
| `upsert_market_daily(records)` | 지수·종목 OHLCV upsert |
| `get_recent(names, n_days)` | 최근 n일 시계열 반환 |
| `get_stocks_universe(session, start, end)` | 전 종목 시계열 반환 |
| `get_market_breadth(date, session)` | market_daily 기반 시장 폭 재계산 |
| `get_market_flow(date)` | investor_flow 집계 (asia) |
| `upsert_stocks_daily(date, session, stocks_data)` | 스크리닝 결과 upsert |
| `get_stocks_daily(date, session)` | 스크리닝 결과 stocks_data 형식으로 반환 |
| `upsert_eps_cache(records)` | EPS 캐시 upsert |
| `get_eps_cache()` | 전체 EPS 캐시 반환 |
| `get_latest_briefing(session)` | 최신 브리핑 반환 |

---

## 6. 주요 설정 파일

| 파일 | 역할 |
|------|------|
| `config/indices.yaml` | 지역별 지수 ticker/RIC, 소스 지정. `europe.stock_universe`에 DAX40·FTSE30·CAC40 110개 포함 |
| `config/macro.yaml` | FX·금리·원자재·변동성 |
| `config/schedule.yaml` | 세션별 KST 트리거 시각 |
| `C:/mquant/.env` | API 키 (LSEG · Anthropic · Slack · FRED) |
| `C:/mquant/lseg-data.config.json` | LSEG SDK 인증 |
| `C:/mquant/spx_constituents_prices_*.csv` | SPX 구성종목 리스트 (헤더 기준) |

---

## 7. 데이터 소스별 특이사항

### pykrx (한국)
- KRX 공식 로그인 세션 필요 (`_get_krx_session()`)
- 거래일 기준 — 장마감 후(15:40 이후)에만 투자자 수급 정상 수신
- 장중 실행 시 `investor_flow` 0건 (에러 방어 처리됨, breadth는 정상 수집)

### LSEG SDK
- 회사 프록시 우회: `httpx` 패치 (`run_session.py` 상단)
- **타임아웃**: EPS 배치 20개 단위, 재시도 1회 자동 처리
- **세션 상태**: 미열림 시 자동 `open_session()` 호출
- EPS 7일 캐시로 일일 LSEG 호출 최소화

### yfinance (US · Europe · 일본·중국 지수)
- 7~9일치 범위 fetch → 등락률·급증비율 계산
- Europe: `.DE`(독일) · `.L`(영국) · `.PA`(프랑스) suffix 지원
- 상장폐지·delisted 종목 자동 스킵 (yfinance 경고 무시)

### FRED API
- `FRED_API_KEY` 환경변수 필요
- `release/dates` API로 실제 발표일 특정

### Claude API
- `ANTHROPIC_API_KEY` 환경변수 필요
- 모델: `claude-opus-4-5`, max_tokens: 3,000
- 재생성 시 DB stocks_daily → stocks_data 자동 복원, breadth/market_flow DB 재계산

---

## 8. 운영 명령어

```bash
# 전체 파이프라인 (수집 → 브리핑 → Slack)
python run_session.py --session asia
python run_session.py --session europe
python run_session.py --session us

# 특정 날짜 재실행
python run_session.py --session us --date 2026-06-10

# 데이터 수집만 (브리핑·Slack 생략)
python run_session.py --session us --no-llm --no-notify

# 수집 + 브리핑 (Slack 생략)
python run_session.py --session us --no-notify

# 브리핑만 재생성 (DB 데이터 활용, 재수집 없음)
python summarize/llm_briefing.py --session us --date 2026-06-10

# 상시 스케줄러 실행
python scheduler.py
```

---

## 9. 오류 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| `LSEG mktcap/eps BATCH ERROR ReadTimeout` | LSEG 서버 지연 | 배치 20개·재시도 자동 처리; 캐시 있으면 영향 없음 |
| `pykrx investor ERROR Length mismatch` | 장중 실행 (빈 데이터) | 장마감(15:30) 이후 재실행; breadth는 정상 수집됨 |
| `LSEG Session is not opened` | LSEG 세션 만료 | `lseg-data.config.json` 확인; europe 섹터 0건, 종목은 yfinance로 정상 수집 |
| Europe `섹터 0개` | LSEG 세션 닫힘 | LSEG 재인증 후 재실행 (종목 데이터는 yfinance로 정상) |
| Slack 발송 실패 404 | Webhook URL 만료 | api.slack.com 에서 URL 재발급 후 `.env` 업데이트 |
| DB 스키마 오류 | `market_watch.db` 손상 | 파일 삭제 후 재실행 (자동 재생성) |
| `FRED_API_KEY` 오류 | `.env` 미설정 | FRED API 키 발급 후 `.env`에 추가 |

---

## 10. 구현 로드맵

### Phase 1 — 기준선 (완료)

| 세션 | 수집 내용 |
|------|-----------|
| ASIA | 주요종목 TOP10 · 특징주 TOP10 · 종목별 외인/기관 수급 |
| EUROPE | STOXX600 섹터 10개 · DAX/FTSE/CAC 대형주 15개 (LSEG) |
| US | 섹터ETF 11개 · 시총/거래대금/급증/EPS수정 스크리닝 |
| 공통 | FX·금리·원자재·VIX · 경제지표 캘린더 · 전 종목 OHLCV → DB |

### Phase 2 — 시장 구조 (완료)

| 항목 | 세션 | 구현 방법 | 상태 |
|------|------|-----------|------|
| P2-1 시장 폭 | ASIA | ohlcv 2,769종목 집계 | ✅ |
| P2-1 시장 폭 | EUROPE | yfinance 104종목 집계 | ✅ |
| P2-1 시장 폭 | US | stats 503종목 집계 | ✅ |
| P2-3 전체 수급 | ASIA | inv_map 외인·기관 합산 | ✅ |
| P2-3 전체 수급 | EUROPE / US | 무료 소스 없음 | N/A |
| Europe 유니버스 확장 | EUROPE | LSEG 15개 → yfinance 110개 | ✅ |

### Phase 3 — 컨텍스트·의사결정 (일부 완료)

| 항목 | 상태 | 비고 |
|------|------|------|
| P3-4 LLM 프롬프트 강화 | ✅ | 시장 폭·수급 데이터 자동 주입, 수급 동반 급등 vs 단순 급등 구분 지시 |
| P3-1 이상거래 탐지 | 보류 | 현재 `turnover_surge` (US) 가 유사 역할; ASIA 확장 가능 |
| P3-3 뉴스·공시 | 보류 | DART API + 뉴스 크롤링 인프라 필요 |
| P3-5 누적 통계 | 보류 | 장기 히스토리 축적 후 의미 있음 |
| P3-2 테마 클러스터링 | 보류 | 업종 DB + ML 파이프라인 필요 |

---

## 11. 파일 구조

```
global_market_watch/
├── run_session.py          수동 원샷 실행 진입점
├── scheduler.py            APScheduler 상시 스케줄러
├── CLAUDE.md               Claude Code 에이전트 지시서
├── PIPELINE.md             ← 본 문서
│
├── config/
│   ├── indices.yaml        지역별 지수 설정 (europe.stock_universe: DAX40·FTSE30·CAC40 110개)
│   ├── macro.yaml          매크로 지표 설정
│   └── schedule.yaml       KST 트리거 시각
│
├── exe/
│   ├── collect_macro.py    지수·매크로 수집
│   ├── collect_stocks.py   종목 스크리닝 수집 (시장 폭·수급 포함)
│   └── collect_econ_cal.py 경제지표 캘린더 수집 (FRED)
│
├── summarize/
│   ├── build_snapshot.py   DB → 스냅샷 텍스트 조합
│   ├── llm_briefing.py     Claude API 브리핑 생성 (_fmt_market_structure 포함)
│   └── notify_slack.py     Slack Webhook 발송
│
└── db/
    ├── schema.sql          SQLite 스키마 정의
    ├── db_manager.py       DB CRUD (get_market_breadth · get_market_flow 포함)
    └── market_watch.db     SQLite DB (자동 생성)
```
