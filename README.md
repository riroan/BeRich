# BeRich

한국투자증권(KIS) API 기반 자동매매 봇

## 주요 기능

- **전략 4종** - RSI Mean Reversion(운영 중), Heikin-Ashi Flip, RSI+HA, Momentum
- **백테스트** - 웹에서 종목·기간·파라미터를 넣고 전략별 시뮬레이션, 캔들/HA 차트와 매매 시점 오버레이
- **미국 주식 전 세션 대응** - 주간거래, 프리마켓, 정규장, 애프터마켓 세션별 스케줄링
- **세션별 KIS 라우팅** - 주간거래 주문 endpoint와 PRE/REGULAR/AFTER 정규 endpoint를 구분
- **웹 대시보드** - 실시간 포지션, RSI 모니터, 차트 (포트 9095)
- **포트폴리오 관리** - 종목별 최대 비중 설정, 파이차트 시각화
- **종목 관리** - 웹에서 실시간 종목 추가/삭제/활성화 (KIS API 검증)
- **전략 설정** - 웹에서 RSI 기간, 매수/매도 레벨, 손절 등 실시간 변경
- **페이퍼 트레이딩** - 실제 시세 + 가상 주문으로 전략 검증
- **DB 기반 상태 복구** - 현재 포지션, 매수/매도 stage, 체결, 성과 이력을 재시작 후 복원
- **Discord 알림** - 매수/매도 체결, 손절, 시스템 오류 알림
- **분석** - 일간/주간/월간 리포트, settlement-adjusted equity curve, 드로우다운, 승률 통계
- **양도소득세 개산** - 연도별 실현손익을 체결일 환율로 원화 환산해 예상 세액과 공제 잔여 표시
- **모바일/PWA 대시보드** - 모바일 카드형 테이블, 햄버거 메뉴, 명시적 테마 토글

## 전략 설명

전략 4종을 내장한다. 시장(market)마다 전략 인스턴스를 하나씩 붙이고, 파라미터는
웹 설정 화면에서 재시작 없이 바꾼다.

| 전략 | 클래스 | 백테스트 키 | 한 줄 요약 |
|------|--------|------------|-----------|
| RSI Mean Reversion | `rsi_mean_reversion.py` | `rsi` | 과매도에 분할 매수, 과매수에 분할 매도 |
| Heikin-Ashi Flip | `heikin_ashi_flip.py` | `ha` | HA 캔들 색이 바뀌면 전량 진입/청산 |
| RSI + Heikin-Ashi | `rsi_heikin_ashi.py` | `rsi_ha` | 위 둘이 동시에 동의할 때만 매수 |
| Momentum | `momentum.py` | (미등록) | 골든크로스 진입, 데드크로스 청산 |

> **아래 숫자는 코드 기본값이다.** 실제 운영값은 DB `strategy_configs.params_json`에
> 있고 웹에서 바뀐다. 예를 들어 현재 3개 설정 모두 매수 사다리가 `35/30/25`이고
> NASDAQ·AMEX는 `stop_loss: -100`으로 손절을 사실상 꺼둔 상태다.

### 1. RSI Mean Reversion — 현재 운영 중인 전략

"많이 빠지면 나눠 사고, 많이 오르면 나눠 판다." 한 번에 사고파는 대신 **사다리**로
쪼개서, 바닥·천장을 맞히지 않아도 평단이 개선되도록 한다.

```
[매수] RSI가 단계별 임계값 아래로 내려갈 때마다 1단계씩
  1차: RSI <= 30 → 남은 비중의 50%
  2차: RSI <= 25 → 30%
  3차: RSI <= 20 → 20%
  매수 금액 = 총자산 x 종목 최대비중 x 단계비율

[매도] RSI가 단계별 임계값 위로 올라갈 때마다 1단계씩 (보유량 기준)
  1차: RSI >= 70 → 보유량의 30%
  2차: RSI >= 75 → 40%
  3차: RSI >= 80 → 50%
  전량이 아니라 비율이라, 계속 오르면 일부를 남긴 채 익절이 이어진다

[손익 사다리] 평단 대비 손익률로도 단계 청산
  stop_loss / take_profit 스칼라 하나만 주면 1단계,
  stop_loss_levels / take_profit_levels로 여러 단계도 가능

[쿨다운] cooldown_days 경과 후 같은 단계를 다시 밟을 수 있다
         (RSI가 회복될 것을 요구하지 않음)
[재진입] reentry_cooldown_days — 전량 청산 후 재매수까지의 대기
```

단계 번호는 DB에 저장되므로 봇을 재시작해도 "3단계까지 샀다"는 사실이 유지된다.
이게 없으면 재시작 때마다 1단계부터 다시 사서 비중이 초과된다.

### 2. Heikin-Ashi Flip

Heikin-Ashi는 캔들을 평균내어 잔물결을 지운 차트다. 색이 바뀌는 지점을 추세 전환으로
보고 **전량** 진입·청산한다.

```
[매수] HA 캔들 음봉 → 양봉 전환
[매도] HA 캔들 양봉 → 음봉 전환
```

**확정된 일봉만 읽는다.** 형성 중인 캔들은 장중에 색이 바뀌므로, 그걸 보고 신호를 내면
하루에도 사고팔기를 반복하게 된다. 그래서 전환은 그 캔들이 닫힌 **다음 세션 첫 틱**에
집행된다 — 백테스트가 가정한 타이밍과 같다.

### 3. RSI + Heikin-Ashi

RSI 전략을 그대로 상속하고 **매수에만** 조건을 하나 더 건다: 마지막 확정 HA 캔들이
양봉이어야 한다. 사다리·손절·쿨다운은 전부 부모와 동일하다.

매도는 게이트하지 않는다. "추세가 아직 괜찮아 보인다"는 지표 때문에 손절이 막히면
손실 사다리의 존재 이유가 사라지기 때문이다.

두 조건은 구조적으로 서로 반대를 본다 — RSI 30은 계속 빠졌다는 뜻이고 HA 양봉은
오르고 있다는 뜻이다. 그래서 거래가 훨씬 드물다. 2018-2026년 14개 종목 측정에서
RSI 진입 신호 1,862건 중 **6.6%(122건)**만 HA 양봉과 겹쳤다.

### 4. Momentum

역추세인 위 셋과 달리 추세 추종이다. 이동평균 교차로 진입/청산한다.

```
[매수] 단기 MA(10)가 장기 MA(20)를 상향 돌파 + RSI > 30
[매도] 데드크로스 또는 RSI > 70
```

백테스트 레지스트리(`scripts/backtest_registry.py`)에는 등록돼 있지 않다.

### 공통 사항

- RSI는 **일봉** 기준이다. 장중 현재가는 오늘 일봉의 종가 자리를 갱신해 실시간 RSI를
  추정하는 데 쓴다.
- RSI 계산은 Wilder 평활법. `rsi_method` 파라미터로 바꿀 수 있다.
- 종목별 `max_weight`(최대 비중)가 매수 금액의 상한이다. 사다리 단계비율은 그 안에서
  나뉜다.

## 미국장 세션

US 전용 스케줄러는 KST 기준으로 KIS가 지원하는 미국 주식 세션을 구분한다. 휴장일과 단축장은 XNYS 캘린더로 게이트하며, 캘린더 캐시는 재시작마다 5년 앞까지 생성한다.

| 세션 | KST 서머타임 기준 | KIS 처리 |
|------|------------------|----------|
| 주간거래 | 09:00-17:00 | `/daytime-order`, `TTTS6036U/6037U`, 지정가 |
| 프리마켓 | 17:00-22:30 | 정규 해외주식 주문 endpoint, 지정가 |
| 정규장 | 22:30-05:00 | 정규 해외주식 주문 endpoint, 지정가 |
| 애프터마켓 | 05:00-07:00 | 정규 해외주식 주문 endpoint, 지정가 |
| CLOSED | 07:00-09:00 | 주문 불가 |

시세 조회는 세션별 venue 코드를 다르게 사용한다.
- 주간거래: `BAQ`/`BAY`/`BAA`
- 프리마켓, 정규장, 애프터마켓: `NAS`/`NYS`/`AMS`

주간거래 활동은 로그와 Discord 알림에 `[DAYTIME]` 태그가 붙어 정규/프리/애프터와 구분된다.

## 설치 및 실행

### 1. 환경 변수 설정

`.env` 파일 생성:

```env
# Broker selection: kis or yfinance
BROKER=yfinance

# Trading mode: paper or live. yfinance supports paper only.
TRADING_MODE=paper

# KIS API (BROKER=kis일 때 필요)
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=your_account_number
KIS_HTS_ID=your_hts_id

# MySQL
MYSQL_ROOT_PASSWORD=your_root_password
MYSQL_DATABASE=quant
MYSQL_USER=quant
MYSQL_PASSWORD=your_password

# 대시보드 로그인 (필수)
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change_me

# Discord (선택)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# 로깅
LOG_LEVEL=INFO
```

전체 항목은 `.env.example` 참고.

### 2. Docker로 실행

```bash
docker-compose up -d --build
```

### 3. 대시보드 접속

```
http://localhost:9095
```

### 4. 로그 확인

```bash
docker logs quant-bot -f
```

## 웹 페이지

| 경로 | 설명 |
|------|------|
| `/` | 메인 대시보드 (포지션, RSI, 시그널) |
| `/symbols` | 종목 관리 (추가/삭제/활성화/비중 설정) |
| `/settings` | 전략 파라미터 실시간 변경 |
| `/portfolio` | 포트폴리오 비중 차트 |
| `/portfolio/correlation` | 보유 종목 간 상관관계 히트맵 |
| `/portfolio/rsi-trend` | 종목별 RSI 추이 |
| `/trades` | 거래 내역 |
| `/backtest` | 전략 백테스트 (종목·기간·파라미터 지정, 캔들/HA 차트) |
| `/performance` | DB 기반 equity curve, 성과 분석, 연도별 양도소득세 |
| `/analytics` | 리포트, 드로우다운, 연속 손익, 종목별 통계 |
| `/symbol/{symbol}` | 종목 상세 차트 (가격 + RSI) |
| `/menu` | 모바일 메뉴 |
| `/login` | 로그인 |

## 프로젝트 구조

```
BeRich/
├── config/
│   └── settings.yaml        # 봇 설정 (워밍업, 리스크)
├── src/
│   ├── bot/                  # 봇 코어 (틱 처리, 대시보드 동기화)
│   ├── broker/
│   │   ├── kis/              # 한국투자증권 API 클라이언트
│   │   └── paper.py          # 페이퍼 트레이딩 브로커
│   ├── strategy/
│   │   ├── base.py           # 전략 베이스 클래스
│   │   ├── engine.py         # 전략 실행 엔진
│   │   └── builtin/          # 전략 4종
│   ├── execution/            # 주문 관리자
│   ├── data/                 # DB 모델 및 스토리지
│   ├── analytics/            # 리포트, 드로우다운, 통계, 양도소득세
│   ├── web/                  # FastAPI 대시보드
│   ├── risk/                 # 리스크 관리
│   └── utils/                # 설정, 로거, 스케줄러, 알림
├── scripts/
│   ├── run_bot.py            # 봇 실행 (--web로 대시보드 동시 기동)
│   ├── run_dashboard.py      # 대시보드 단독 실행
│   ├── backtest_engine.py    # 전략 비의존 백테스트 엔진 (자금·체결 담당)
│   └── backtest_registry.py  # API 키 → 백테스트 전략 매핑
├── tests/                    # 테스트
├── docker-compose.yml
├── Dockerfile
└── .env                      # 환경 변수 (gitignore)
```

## 현재 아키텍처

현재 봇과 웹 대시보드는 같은 코드베이스에서 동작하며, 일부 실시간 화면 상태는 `src.web.app`의 in-memory `DashboardState`를 공유한다.

DB가 source of truth인 데이터:
- `strategy_configs` / `strategy_params`: 전략·종목·파라미터 설정
- `orders` / `fills`: 주문·체결 이력
- `current_positions`: 현재 보유 포지션과 매수/매도 stage
- `price_rsi`: tick 경로에서 기록한 가격·RSI 이력
- `equity_snapshots`: 잔고/equity curve 히스토리, settlement adjustment 포함

재시작 시 복원되는 데이터:
- 현재 보유 포지션
- RSI 전략의 buy/sell stage 상태
- 체결 기반 trade log와 performance 지표
- DB 기반 90일 equity curve

아직 메모리 의존이 남아 있는 데이터:
- 현재 잔고/현금/PnL
- 봇 상태, pause 상태, 최근 업데이트 시각
- 최근 signal/order 이벤트
- 현재 RSI snapshot과 WebSocket broadcast 상태

## 리팩토링 방향

목표는 봇과 웹을 DB-first 구조로 분리해 별도 프로세스/K8s deployment로 독립 실행하는 것이다. 자세한 작업 목록은 `TODO.md`의 "Bot/Web 완전 분리" 섹션을 따른다.

핵심 원칙:
- 봇은 DB writer, 웹은 DB reader/control-command writer로 역할 분리
- 봇/실행/브로커 코드에서 `src.web.app` import 제거
- 웹은 봇 객체, 전략 인스턴스, callback을 직접 참조하지 않음
- 현재 잔고는 `account_state` 단일 row로 관리
- 잔고·성과 히스토리는 기존 `equity_snapshots` 유지
- pause/resume/reload/settings apply는 `bot_commands` 큐로 전달

로드맵:
1. `account_state`, `bot_status`, `bot_events`, `bot_commands` 추가
2. 봇이 잔고/status/equity/signal/order 이벤트를 DB에 기록
3. 웹 Dashboard/Performance/Portfolio가 메모리 대신 DB에서 읽도록 변경
4. pause/reload/settings apply를 callback 대신 command queue로 변경
5. 봇/실행/브로커에서 `src.web.app` import 완전 제거
6. `src/web/app.py`를 route/service 단위로 분리

## 설정

### 종목 관리

전략·종목은 DB(`strategy_configs` 테이블)에서 관리. 웹 `/symbols` 페이지에서 추가/수정.
- 종목 추가 시 KIS API로 유효성 검증
- 종목별 최대 포트폴리오 비중(%) 설정 가능
- 활성/비활성 전환 시 재시작 없이 즉시 반영

### 전략 파라미터

웹 `/settings` 페이지에서 실시간 변경:
- RSI 기간, 쿨다운 일수, 재진입 쿨다운
- 매수(avg down) / 매도 RSI 레벨 및 단계별 비율
- 손절·익절 사다리 (`stop_loss_levels` / `take_profit_levels`) — 단일 값만 넣으면 1단계

### yfinance paper mode (KIS 없이 실행)

KIS API 키 없이 미국/한국 종목을 paper trading으로 테스트하려면 `.env`에서:

```env
BROKER=yfinance
TRADING_MODE=paper
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change_me
```

이 모드는 yfinance에서 시세/일봉 데이터를 가져오고, 주문은 로컬 paper 계좌에서 즉시 체결 처리합니다. 실제 주문은 발생하지 않습니다. Paper cash/positions/orders/fills 상태는 기본적으로 `data/yfinance_paper_state.json`에 저장되어 재시작 후에도 유지됩니다.

실행:

```bash
uv run python scripts/run_bot.py --web --web-port 9095
```

주의:

- yfinance 데이터는 지연/누락될 수 있어 실거래 판단용으로 쓰면 안 됩니다.
- 한국 종목은 yfinance suffix가 필요할 수 있습니다. 예: `005930.KS`, `091990.KQ`.
- suffix 없는 6자리 KRX 코드는 기본적으로 `.KS`로 조회합니다.
- yfinance broker는 live trading을 지원하지 않습니다.

### 페이퍼 트레이딩

`.env`에서 `BROKER=yfinance`, `TRADING_MODE=paper`로 설정하면:
- yfinance 시세/일봉 데이터 사용
- 주문은 로컬 paper 계좌에서 가상 체결 (실제 돈 사용 안 함)
- paper 상태는 `data/yfinance_paper_state.json`에 유지
- 워밍업 없이 즉시 시작
- 대시보드에 `PAPER` 배지 표시

KIS 기반 paper trading을 쓰려면 `BROKER=kis`, `TRADING_MODE=paper`와 유효한 KIS API 키가 필요합니다.

## 테스트

```bash
uv run --python 3.13 --locked --extra dev pytest
```

## 기술 스택

- **Python 3.13** / FastAPI / SQLAlchemy (async)
- **MySQL 8.0** / Docker Compose
- **KIS Open API** (한국투자증권)
- **LightweightCharts 4.2** (차트)
- **Discord Webhooks** (알림)
