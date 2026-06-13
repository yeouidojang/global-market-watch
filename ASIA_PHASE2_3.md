# ASIA 시황 텍스트 업그레이드 — Phase 2 / 3 설계서

> 작성일: 2026-06-11  
> 대상: `global_market_watch` ASIA 세션 (KOSPI/KOSDAQ)  
> 목적: 운용 의사결정에 직결되는 "시장 폭 + 자금흐름 + 글로벌 컨텍스트"가 결합된 시황 텍스트로 업그레이드.

---

## Phase 1 완료 (기준선)

| 영역 | 변경 |
|------|------|
| DB | `stocks_daily`에 `foreign_net`, `inst_net` 추가 |
| 수집 | pykrx `get_market_net_purchases_of_equities_by_ticker`로 전 종목 외인/기관 순매수 |
| 분석 | featured 시그널에 `외인매수/매도`, `기관매수/매도` 태그 결합 |
| 출력 | 특징주 표에 외인(억)·기관(억) 컬럼 + LLM 프롬프트에 "수급 동반 vs 단순 급등 구분" 지시 |

→ **현 시점 한계**: 종목 단위 수급은 보이지만, 시장 전체의 **breadth**(상승/하락 폭), **섹터 단위 자금흐름**, **환율-외인 상관** 등 매크로↔미시 연결 컨텍스트가 부족. 글로벌 야간 변화·뉴스도 LLM 자체 추론에만 의존.

---

## Phase 2 — 시장 폭 + 섹터 자금흐름 + 글로벌 연계

### 목표
시황의 "큰 그림"을 데이터로 보강. 종목 나열식 → **시장 구조 + 자금 방향성**이 보이도록.

### 2-1. 시장 폭 (Breadth) 지표

- KOSPI/KOSDAQ 각각 상승/하락/보합 종목 수, 비율
- 신고가/신저가 종목 수 (52주 기준, pykrx `get_market_ohlcv_by_ticker` × 252일 또는 `stock.get_index_ohlcv` 활용)
- 상승종목 거래대금 합계 vs 하락종목 거래대금 합계 (자금 방향성)

**저장**: 새 테이블 `market_breadth_daily`
```sql
CREATE TABLE market_breadth_daily (
    date TEXT, market TEXT,           -- KOSPI | KOSDAQ
    advancers INTEGER, decliners INTEGER, unchanged INTEGER,
    new_high INTEGER, new_low INTEGER,
    advance_trade_val REAL, decline_trade_val REAL,
    PRIMARY KEY (date, market)
);
```

### 2-2. 섹터별 등락률 + 외국인 순매수 상위

- pykrx `get_index_ohlcv` 로 KRX 섹터지수 (KRX 자동차, KRX 반도체, KRX 은행 등) 종가·등락률
- 같은 분류 기준으로 외국인/기관 순매수 합산 → "외인 자금 유입 상위 섹터" 도출
- 종목 메타: pykrx `get_market_sector_classifications` 또는 KRX 산업분류 파일

**저장**: `stocks_daily` 활용 — `session='asia', category='sector_flow'` 행으로 저장
- `ticker`=섹터코드/명, `chg_pct`=섹터지수 등락, `foreign_net`/`inst_net`=섹터 합산 순매수

### 2-3. 전체 시장 수급 + 환율 연계

- 외국인 KOSPI 전체 순매수 (억원) — 5/20일 평균 대비 z-score
- USD/KRW 일별 변동률과의 상관 (최근 20일 rolling)
- 프로그램 매매 (차익/비차익) — pykrx `get_program_trading_volume_by_date`

**저장**: 새 테이블 `market_flow_daily`
```sql
CREATE TABLE market_flow_daily (
    date TEXT, market TEXT,
    foreign_net_total REAL, inst_net_total REAL, retail_net_total REAL,
    program_arb_net REAL, program_nonarb_net REAL,
    PRIMARY KEY (date, market)
);
```

### 2-4. 글로벌 야간 갭 컨텍스트

- 전일 미국 마감 (SPX/NDX 종가·등락률) → 한국 시초가 갭 (KOSPI 시가 vs 전일종가)
- 야간 선물 변동 (KOSPI200 야간선물) — 가능 시 LSEG `KSc1` 활용
- USD/KRW 야간 변동 (LSEG 기준)

**저장**: `build_snapshot.py`에서 DB의 `market_daily` 조회 → 텍스트 컨텍스트로만 사용 (별도 저장 불필요)

### Phase 2 산출물

브리핑 추가 섹션:
```
**시장 폭 (Breadth)**
KOSPI: 상승 432 / 하락 458 / 보합 32 (상승 거래대금 8.2조 vs 하락 7.8조)
신고가 12종목 · 신저가 5종목 → 모멘텀 약화 시그널

**섹터별 자금흐름 (외국인 순매수 상위)**
| 섹터 | 등락률 | 외인순매수(억) | 기관순매수(억) |
|반도체   | +0.8% | +1,234 | -567 |
|2차전지  | -2.1% |   -987 | +321 |
...

**글로벌 야간 컨텍스트**
SPX 전일 -1.62% / USD/KRW 야간 +0.4% / 한국 시초가 -1.2% 갭다운
```

---

## Phase 3 — 컨텍스트 + 이상신호 + 의사결정 강화

### 목표
"수치 나열" → "운용 액션이 보이는 시황"으로. 뉴스·이벤트·이상거래 패턴을 결합.

### 3-1. 이상거래 패턴 (Anomaly)

- 52주 신고가/신저가 + 거래대금 ≥ 100억 종목
- 5일 평균 거래대금 대비 **3배 이상 급증**한 종목
- VI(변동성완화장치) 발동 종목 — KRX 데이터 가능 시
- 외인 5일 누적 순매수 상위 (단발성 vs 추세 구분)

**구현**: `collect_stocks.py`에 `_detect_anomaly()` 추가
- `market_daily`(asia/stock) 5일 평균과 당일 비교로 surge 계산
- `stocks_daily` 새 category: `anomaly`

### 3-2. 테마 클러스터링

기존 자료 활용:
- `kr/etf_stock/` 또는 `kr/etf_theme/` 의 ETF 보유종목 매핑
- 당일 급등 종목 → 어떤 ETF/테마에 속하는지 역추적
- 예: "급등 5종목 중 4개가 '2차전지 테마'에 속함"

**구현**: 별도 모듈 `summarize/theme_mapper.py`
- ETF 구성종목 메타데이터 한 번만 로드 (캐시)
- featured/anomaly 종목 → 테마 라벨 부여

### 3-3. 뉴스 헤드라인 통합 (선택)

옵션 A: **DART 공시** (수요일 EPS 시즌 등)
- `kr/dart_reader/` 기존 모듈 활용
- 특징주 종목 대상 당일 공시 1~2건 헤드라인 결합

옵션 B: **네이버 금융 종목별 뉴스 크롤링**
- 급등/급락 종목 상위 5개만 헤드라인 1줄씩
- 단순 fetch + BeautifulSoup, 1시간 캐시

→ Phase 3에서는 **옵션 A (DART)**를 우선 권장 (이미 인증·코드 자산 보유)

### 3-4. 의사결정 코멘트 자동화

LLM 프롬프트 강화:
- "외인 매수 + 모멘텀 + EPS 추정 상향" 3박자 충족 종목 → **포커스 종목**으로 마킹
- "급등했으나 외인/기관 동반 매도" → **차익실현 의심 종목** 경고
- 섹터 자금흐름과 종목 단위 자금흐름이 **상충**할 때 → 별도 코멘트

### 3-5. 일일 추적 누적 통계 (선택)

- `briefings`에 metadata JSON 컬럼 추가 → 매일 외인 순매수 상위 종목 누적
- 5일 / 20일 외인 매수 누적 랭킹 (트렌드 종목 추출)

### Phase 3 산출물

브리핑 추가 섹션:
```
**이상거래 패턴**
- LG에너지솔루션: 5일 평균 대비 거래대금 4.2x 급증, 신고가 갱신
- 삼성중공업: VI 2회 발동, 외인 5일 누적 매수 1위 ← 추세 진입 가능성

**테마 분석**
급등 상위 6종목 중 4종목이 'AI반도체 테마' (XLK·SOX 야간 강세 연동)

**포커스 종목 (3박자 충족)**
| 종목 | 등락률 | 외인(억) | 기관(억) | EPS추정(1W) | 시그널 |
| SK하이닉스 | +3.2% | +1,890 | +234 | +2.1% | ⭐ 포커스 |

**차익실현 의심**
- 한미반도체 +8%: 외인 -345억 / 기관 -120억 동반 매도
```

---

## 구현 우선순위

| 우선순위 | Phase | 항목 | 난이도 | 예상 효과 |
|---------|-------|------|--------|----------|
| **★★★** | 2-1 | 시장 폭 (Breadth) | 낮음 | 높음 — "추세 강도" 즉시 가시화 |
| **★★★** | 2-3 | 전체 수급 + 환율 연계 | 중간 | 높음 — "외인 방향성" 정량화 |
| **★★** | 2-2 | 섹터별 자금흐름 | 중간 | 높음 — "어디로 돈이 가는가" 핵심 |
| **★★** | 2-4 | 글로벌 야간 컨텍스트 | 낮음 | 중간 — 갭 원인 설명 |
| **★★** | 3-1 | 이상거래 패턴 | 중간 | 높음 — 액션 시그널 직결 |
| **★** | 3-4 | 의사결정 자동 코멘트 | 낮음 (프롬프트) | 높음 — 종합 인사이트 |
| **★** | 3-2 | 테마 클러스터링 | 중간 | 중간 — 자산 보유 시 빠른 구현 |
| **△** | 3-3 | 뉴스/공시 통합 | 높음 | 중간 — 별도 인프라 부담 |
| **△** | 3-5 | 누적 통계 | 중간 | 중간 — 데이터 축적 필요 |

권장 구현 순서: **2-1 → 2-3 → 2-4 → 2-2 → 3-1 → 3-4 → 3-2 → (3-3, 3-5 보류)**

---

## Phase 2-1 ~ 2-4 + 3-1 + 3-4 통합 구현 요청

> 아래 블록을 Claude Code에 그대로 전달하여 구현 의뢰.

```
# 작업 요청: ASIA 시황 Phase 2 + Phase 3 일부 구현

## 컨텍스트
- 작업 디렉토리: c:/mquant/global_market_watch
- 환경: C:/Users/LEESH/.conda/envs/meritzquant/python.exe
- DB: c:/mquant/global_market_watch/db/market_watch.db (SQLite)
- pykrx 사용 시 반드시 exe/collect_macro.py의 _get_krx_session() 인증 패턴 따를 것
- LSEG 사용 시 run_session.py 상단의 httpx 프록시 패치 패턴 따를 것

## 작업 범위 (우선순위 순)

### [P2-1] 시장 폭 (Breadth) — 신규
1. db/schema.sql에 market_breadth_daily 테이블 추가
2. exe/collect_stocks.py 의 fetch_top_stocks() 내부에서 KOSPI/KOSDAQ 각각:
   - advancers (chg_pct > 0), decliners (< 0), unchanged (==0) 카운트
   - advance_trade_val / decline_trade_val 합계
   - 52주 신고가/신저가 카운트:
     · 252일 OHLCV 한번에 못 가져오면, market_daily에서 SELECT MAX/MIN(close)로 비교
     · 처음엔 5거래일만 비교하여 "5일 신고가/신저가"로 fallback
3. DBManager에 upsert_market_breadth(records) 추가
4. summarize/build_snapshot.py 의 ASIA 섹션에 breadth 텍스트 추가

### [P2-3] 전체 시장 수급 + 환율 연계 — 신규
1. db/schema.sql에 market_flow_daily 테이블 추가
2. fetch_top_stocks() 내부 inv_map 집계 후:
   - KOSPI/KOSDAQ 각각 외인/기관/개인 순매수 합계
   - (선택) pykrx.get_program_trading_volume_by_date 시도 — 실패하면 NULL
3. build_snapshot.py 의 ASIA 매크로 섹션에 다음 추가:
   - "외인 KOSPI 순매수: -3,250억원 (5일 평균 -1,800억 대비 z=-0.8)"
   - "USD/KRW 1,358원 (+0.4%) — 원화 약세 vs 외인 순매도 동조"

### [P2-4] 글로벌 야간 컨텍스트 — 데이터 조회만
1. build_snapshot.py 의 ASIA 섹션 맨 앞에 추가:
   - 전일 SPX/NDX 종가·등락률 (market_daily에서 SELECT)
   - 전일 USD/KRW (LSEG로 이미 수집됨)
   - KOSPI 시초가 vs 전일종가 = 갭 (%) 계산
2. 저장 불필요 — 텍스트만 조합

### [P2-2] 섹터별 자금흐름 — 신규
1. fetch_top_stocks() 내부에서:
   - pykrx.get_index_portfolio_deposit_file(ticker) 활용해 KRX 주요 섹터지수 구성종목 매핑
     · 또는 KRX 산업분류 사용 (stock.get_market_ticker_list + 별도 메타)
   - 주요 섹터 8~10개만: 반도체/2차전지/자동차/금융/바이오/조선/철강/유틸리티/IT서비스/소비재
2. 섹터별 등락률 = 시총가중 평균
3. 섹터별 외인/기관 순매수 = 구성종목 합산
4. stocks_daily 에 session='asia', category='sector_flow' 로 upsert (ticker=섹터명)
5. llm_briefing._fmt_stocks_asia() 에 섹터 자금흐름 테이블 추가

### [P3-1] 이상거래 패턴 — 신규
1. fetch_top_stocks() 마지막에 _detect_anomaly() 호출:
   - 5일 평균 거래대금 대비 3x 이상 종목 (market_daily 활용)
   - 52주 또는 5일 신고가/신저가
2. stocks_daily 에 category='anomaly' 로 저장 (필드: surge_ratio, signal='52w_high', etc)
3. build_snapshot.py 에 "이상거래 패턴" 섹션 추가

### [P3-4] LLM 프롬프트 강화 — 텍스트만 수정
llm_briefing.py 의 ASIA system prompt에 다음 지시 추가:
- "외인 매수 + 기관 매수 + 거래대금 급증 3박자 충족 종목은 ⭐ 포커스 종목으로 별도 표시"
- "급등했으나 외인+기관 동반 매도 종목은 '⚠️ 차익실현 의심' 코멘트 추가"
- "섹터 외인 매수가 상위 3개 섹터와 종목 단위 외인 매수 상위 종목이 일치하면 '추세 진입' 코멘트"

## 검증
구현 후 아래 명령으로 검증:
  python run_session.py --session asia --date 2026-06-10 --no-notify

기대 출력:
- "시장 폭 KOSPI 상승 X / 하락 Y" 로그
- "섹터 자금흐름: 반도체 +1,234억 (외인)" 텍스트
- "글로벌 야간 갭" 컨텍스트
- "이상거래 N종목" 로그
- LLM 브리핑에 ⭐ 포커스 / ⚠️ 차익실현 의심 코멘트 등장

## 제약
- 모든 변경은 추가형으로. 기존 major/featured/investor_flow 로직 건드리지 말 것.
- pykrx 휴장일 빈 데이터 케이스 항상 가드.
- LSEG/yfinance 타임아웃 가드 (try/except).
- DB 마이그레이션은 db_manager.py 의 _init_db() 패턴 따라 컬럼·테이블 존재 체크.
- 새 테이블은 schema.sql에도 추가하고 ALTER 마이그레이션 코드 동시 작성.
```

---

## 참고: 추후 Phase 4 후보 (현 범위 외)

- 일중 데이터 (분봉) — 장중 자동 알림용
- 옵션 PCR, 코스피200 선물 베이시스
- 글로벌 ETF 자금흐름 (KOSPI ↔ EWY, KOREA ETF)
- 머신러닝 기반 이상신호 (z-score → autoencoder)
- 백테스트 연계 (Phase 1~3 시그널의 historical 성과 추적)
