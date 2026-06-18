# TODO: Bot/Web 완전 분리 (DB-first, K8s)

설계 문서: `~/.gstack/projects/riroan-BeRich/riroan-main-design-20260410-184338.md`

## 목표

웹 서버와 트레이딩 봇을 K8s에서 독립 배포. 어느 쪽이 재시작되어도 상대방에 영향 없음.

---

## Step 1: DB 스키마 추가

- [ ] `src/data/models.py` — 신규 테이블 5개 추가
  - `BotState` (id=1 단일행, bot 실행상태)
  - `PositionsSnapshot` (symbol PK, 30s 전체 교체)
  - `AccountSnapshot` (id=1 단일행, 잔고/PnL)
  - `RsiSnapshot` (symbol PK, 변화량 기반 upsert)
  - `BotCommand` (명령 큐: reload_strategies, pause, resume)
- [ ] `src/data/storage.py` — upsert/query 메서드 추가
  - `upsert_bot_state()`
  - `upsert_positions_snapshot()` (트랜잭션: DELETE + bulk INSERT)
  - `upsert_account_snapshot()`
  - `upsert_rsi_snapshot(symbol, rsi, price, market)`
  - `get_pending_commands()`, `mark_command_done(id)`
  - `create_command(command)` (중복 pending 방지 로직 포함)
  - `get_bot_state()`, `get_positions_snapshot()`, `get_account_snapshot()`, `get_rsi_snapshot_all()`
- [ ] Alembic 마이그레이션 실행 (서비스 영향 없음)
- [ ] SQLAlchemy engine pool 설정: `pool_size=5, max_overflow=10, pool_recycle=3600, pool_pre_ping=True`

---

## Step 2: 봇 코드 — 웹 의존성 제거

- [ ] `src/bot/db_writer.py` 신규 작성 (dashboard_sync.py 교체)
  - `DBWriterMixin` 구현
  - `write_positions_to_db()` — 포지션 DB upsert
  - `write_account_to_db()` — 잔고 DB upsert
  - `write_bot_status_to_db()` — bot_state DB upsert
  - `write_rsi_to_db_if_changed()` — RSI 변화량 ≥0.5 또는 10s 경과 시만 upsert
- [ ] `src/bot/dashboard_sync.py` 삭제
- [ ] `src/bot/core.py`
  - `from src.web.app import get_dashboard_state` 제거
  - `DashboardSyncMixin` → `DBWriterMixin` 교체
  - `self._trading_paused = False` 초기화 추가
  - `_poll_commands()` 추가 (10s마다 bot_commands 폴링)
  - `reload_callback` 패턴 제거
- [ ] `src/execution/order_manager.py`
  - `from src.web.app import get_dashboard_state` 제거
  - `trading_paused` 체크: `self.bot._trading_paused` 사용
  - `balance_usd` 읽기: `broker.get_account_balance()` 직접 호출
  - `dashboard.add_trade_log()`, `add_signal()`, `add_order()` 호출 제거
- [ ] `src/broker/paper.py`
  - `from src.web.app import get_dashboard_state` 제거
  - RSI 읽기: `await asyncio.wait_for(storage.get_rsi_snapshot(symbol), timeout=0.1)`
  - RSI None 시 mid-price 체결가 fallback

---

## Step 3: 웹 코드 — DB에서 상태 읽기

- [ ] `src/web/app.py`
  - `refresh_dashboard_from_db()` 함수 추가 (3s 백그라운드 태스크)
    - bot_state → DashboardState (봇 오프라인 감지: updated_at > 120s → 배너 표시)
    - positions_snapshot → dashboard.positions
    - account_snapshot → dashboard.balance_*/cash_*/pnl_*
    - rsi_snapshot → dashboard.rsi_values
  - Strategy CRUD API: `reload_callback()` → `storage.create_command("reload_strategies")`
  - pause API: `storage.create_command("pause")`
  - `broadcast_update()` 제거 (또는 no-op)
  - WebSocket 엔드포인트 제거
  - KIS 심볼 검증: env에서 직접 KIS 클라이언트 초기화 (`dashboard_state.kis_auth_token` 제거)
  - `DashboardState`에서 봇 전용 필드 제거 (`reload_callback`, `kis_auth_token`, `strategy_instances` 등)

---

## Step 4: Dockerfile 분리

- [ ] `Dockerfile.bot` 작성 (`python -m src.bot` entrypoint)
- [ ] `Dockerfile.web` 작성 (`uvicorn src.web.app:app` entrypoint)
- [ ] `docker-compose.yml` 업데이트 (bot + web 분리 서비스)

---

## Step 5: K8s 배포 파일

- [ ] `k8s/bot-deployment.yaml`
- [ ] `k8s/web-deployment.yaml`
- [ ] `k8s/mysql-statefulset.yaml` (또는 외부 관리형 DB)
- [ ] `k8s/secret.yaml` (KIS_APP_KEY, KIS_APP_SECRET, DATABASE_URL)

---

## 참고: 마이그레이션 순서 (무중단)

```
1. Step 1 완료 후 Alembic 실행         ← 기존 서비스 영향 없음
2. Step 2 봇 배포                       ← DB 쓰기 시작, 웹은 아직 DashboardState 읽음
3. Step 3 웹 배포                       ← DB에서 읽기 시작
4. DashboardState 잔여 코드 정리        ← cleanup
5. Step 4, 5 배포 분리                  ← K8s 완전 독립
```

---

# Backtest UI v2 후보 (from /plan-eng-review 2026-05-01)

설계 문서: `~/.gstack/projects/riroan-BeRich/riroan-main-design-20260501-231151.md`

## TODO: 백테스트 데이터 세션 캐시
**What:** `backtest_symbol_async()` 호출 시 (symbol, market, start_date, end_date) 기준 df를 세션/메모리 캐시. 동일 키로 재요청 시 yfinance/DB 재로드 생략.

**Why:** yfinance fallback 종목으로 슬라이더 조정 반복 시 (RSI period만 14→15 변경) 매 요청 ~1s yfinance 다운로드 낭비. KIS DB hit 종목은 50ms라 영향 적음.

**Pros:** "조정→재실행" UX 매끄러움, yfinance API 호출 횟수 감소 (rate limit 안전 마진).

**Cons:** 캐시 무효화 정책 필요(일봉 업데이트는 장 마감 후 1회), 캐시 권역 선택(in-process dict vs lru_cache vs Redis).

**Context:** 첫 버전은 `functools.lru_cache(maxsize=32)`로 in-process 캐시. df는 작아서 메모리 무시. 일봉 업데이트 시점(KST 장 마감 후 ~16:00) 기준 TTL 만료. 처음 메인 백테스트 디자인과 독립적이어서 별도 PR로 추가 가능.

**Depends on / blocked by:** 없음 — 메인 백테스트 UI 구현 완료 후 별도로.

## TODO: 라이브 strategy와 backtest 엔진 통합
`scripts/backtest_rsi.py`의 `_run_simulation()`과 `src/strategy/builtin/rsi_mean_reversion.py`가 동일한 RSI Mean Reversion 로직을 두 곳에서 구현. 한쪽만 바뀌면 백테스트 결과와 라이브 동작이 어긋남. v2에서 strategy 모듈을 backtest-friendly하게 리팩터링하여 양쪽 한 코드.

**Depends on:** 메인 백테스트 UI 구현 완료 + dogfood 후 결과 신뢰도가 라이브 의사결정 영향 줄 단계 도달 시.

## TODO: 파라미터 sweep / walk-forward 모드
2026-04-12 디자인 Approach C에서 deferred. 두 파라미터 세트 동시 실행 후 수익률 곡선 오버레이, 또는 walk-forward 검증.

**Depends on:** 메인 백테스트 UI 구현 + 캐시 추가 후 (sweep은 동일 데이터 다회 시뮬레이션).

---

# US 24시간 트레이딩 — C' 설계 (2026-06-18 설계 확정)

**상태:** 9개 항목 전부 구현 완료 + KIS API 문서로 주문 경로 검증 + 페이퍼 구동 검증 완료 (2026-06-18). 테스트 271 passed. 브랜치 `feat/us-24h-trading` (커밋 b913483, c455e98, 736b612).
**검증됨 (KIS 해외주식 주문 문서):** 데이마켓 TR `TTTS6036U/6037U` + `/daytime-order`, 정규/프리/애프터 ORD_DVSN `00`, 세션 시각 경계(애프터 07:00 마감 + CLOSED 갭). 무효였던 `01` 시장가 fallback 제거.
**검증됨 (페이퍼 구동, 정규장 23:4x KST):** 정상 기동 + 새 스케줄러 활성 + 정규장 가격/RSI 수집 + 심볼 추가(~2초)/제거(~15초) 재시작 없이 반영.
**라이브 전 남은 확인:** ① **데이마켓/프리/애프터 시세** — 아직 정규 `HHDFS00000300` 사용. 정규장 외 세션에서 가격이 실제로 들어오는지 **미검증** (그 시간대에 페이퍼로 띄워 로그 확인 필요). ② 주간거래 **지원 종목** 범위 (일부만 매매 가능). ③ 모의투자는 일부 종목 + ORD_DVSN `00`만. ④ 라이브 주문 경로(데이마켓/시간외)는 페이퍼가 PaperBroker로 처리하므로 **라이브 첫 전환 시 첫 검증 대상**.

## 목표

미국 정규장만 거래하던 봇을 **US 전 세션(데이마켓·프리·정규·애프터)**으로 확장. 한국장(KRX)은 작업 범위 외.

## C' 설계 핵심

### RSI 계산
- **윈도우 = 직전 14개 정규장 종가 (확정·고정) + 현재가 (라이브, 매 틱 갱신)**
- **슬라이드 트리거**: 시각 기반 X → **KIS 일봉 API에 새 정규장 일봉이 컨펌**될 때만 한 칸 슬라이드
- **컨펌 폴링**: 정규장 마감 추정 시각 이후 5분 간격, 최대 30분 재시도
- **컨펌 전엔**: 어제 베이스 유지 + 라이브 슬롯만 갱신
- **휴장일·주말**: 새 일봉이 안 들어오므로 자연스럽게 슬라이드 안 함 (시각 기반 아니라서 자동 처리됨)

### 거래 세션 (US-only, EDT 기준 KST 시각) — KIS 문서 검증 완료 (2026-06-18)
| 세션 | 시간 (KST, 서머) | 주문 경로 |
|------|------|------|
| 데이마켓 | 09:00-17:00 | KIS 주간거래 `TTTS6036U/6037U` + `/daytime-order`, ORD_DVSN `00` (지정가 only) |
| 프리마켓 | 17:00-22:30 | 정규 `TTTT1002U/1006U` + `/order`, ORD_DVSN `00` |
| 정규장 | 22:30-05:00 (다음날) | 정규 `order`, ORD_DVSN `00` |
| 애프터 | **05:00-07:00** (다음날) | 정규 `order`, ORD_DVSN `00` |
| (CLOSED) | **07:00-09:00** | KIS 정규 endpoint 운영시간 밖 + 주간거래 미개장 → 거래 불가 |

**검증 결과 (KIS 해외주식 주문 API 문서):**
- 프리/정규/애프터는 **전부 정규 `order` endpoint + ORD_DVSN `00`** (KIS가 시각으로 라우팅). 별도 시간외 코드 없음.
- 해외 ORD_DVSN: `00`지정가 / `31`MOO / `32`LOO / `33`MOC / `34`LOC / `35`TWAP / `36`VWAP. **`01`(시장가) 없음**. **모의투자는 `00`만.**
- 애프터 마감 = **07:00 (양 계절 고정)**, 그 후 주간거래 개장(09:00 서머/10:00 겨울)까지 CLOSED. 즉 24h 아님 (~22h, 갭 있음).
- 운영시간 밖 정규 endpoint 호출 시 KIS 에러.

EST(겨울)는 1시간 시프트 (단, **애프터 마감 07:00은 고정**). DST는 `is_us_dst()` 활용.

### 신호·진입
- 매 틱 RSI 평가
- 분할 매수/매도 단계는 **같은 거래일 안에서도 연쇄 진입 허용** (의도된 동작)
- 단계당 1회 진입은 `_buy_stages` 카운터로 보장 — 단, **카운터 증가 시점을 시그널 생성 → 체결로 이동** (Phase 5에서 수정)

### 손절
- 모든 세션 매 틱 현재가 기준
- 슬리피지 버퍼는 모두 1% 유지 (`config/settings.yaml:18`)
- **미체결 시 다음 세션 시작 즉시 재제출**

### 쿨다운 (`recovery_rsi` 조건 제거)
- 현재: `cooldown_days 경과 AND RSI ≥ 50 한 번 회복`
- 변경: `cooldown_days 경과`만
- 연관 코드 제거: `_rsi_recovered: dict`, `recovery_rsi` 파라미터, 관련 reset 로직

## 작업 분해 (9개)

### Phase 1 — RSI 데이터 모델
- [x] **1. `update_daily_close` 재설계** (`src/strategy/builtin/rsi_mean_reversion.py`) ✅ 2026-06-18
  - 확정 base(`_daily_bars`) + 라이브 슬롯(`_live_price`) 분리. `update_daily_close`는 매 틱 라이브만 갱신, 날짜 기반 슬라이드 제거.
  - `get_daily_dataframe`가 base + 라이브 forming row 합쳐 반환 (base 불변).
  - 슬라이드는 `confirm_daily_bar()`로만 (append on newer date / refresh on same date / skip on stale).
  - 테스트: `tests/test_strategy.py` (live-layer, no-clock-slide, append/refresh/skip).
- [x] **2. 정규장 마감 후 일봉 폴링 모듈 신규** (`src/bot/core.py`) ✅ 2026-06-18
  - `_maybe_run_daily_confirm()` (on_tick에서 호출): REGULAR→AFTER 전이 감지 시 하루 1회 폴링 태스크 기동.
  - `_run_daily_confirm_poll()`: 5분 간격 최대 6회(30분), 심볼별 `get_historical_bars(days=5)` 최신 봉을 `confirm_daily_bar`로 fold. "appended" 시 완료, 미컨펌 심볼은 이전 base 유지 + 경고 로그 (조용한 슬라이드 없음).
  - `stop()`에서 태스크 취소. 테스트: `tests/test_bot.py` (전이 트리거/비트리거/슬라이드).

### Phase 2 — 세션 인프라 ✅ 2026-06-18
- [x] **3. `Session` enum + `get_current_session(ts)` 함수** (`src/utils/scheduler.py`)
  - `DAY_MARKET / PRE / REGULAR / AFTER / CLOSED`, DST 분기, 주말 경계 처리.
  - 테스트: `tests/test_scheduler.py` (EDT/EST 경계 + 토/일/월 주말).
- [x] **4. 스케줄러 윈도우 4개로 확장** (`src/utils/scheduler.py`)
  - `get_us_session_windows_kst()` 신규(us_only 기본값). `is_market_open()`는 `get_current_session() != CLOSED`로 단일화. 레거시 `get_us_market_hours_kst()`(정규장 전용)는 KRX+US 경로용으로 유지.
  - ⚠️ 주말 경계는 KIS 24h 세션 캘린더 기준으로 추후 검증 필요 (코드에 주석).

### Phase 3 — 주문 경로 (구현 + KIS 문서 검증 완료, 2026-06-18) ✅
- [x] **5. 데이마켓 주문 경로 신규** (`src/broker/kis/client.py`)
  - `submit_order`가 `get_current_session()`로 라우팅: DAY_MARKET → `_submit_overseas_day_order()`, 그 외 해외 → `_submit_overseas_order()`.
  - `_submit_overseas_day_order()`: TR `TTTS6036U/6037U`, `/daytime-order`, ORD_DVSN `00`(지정가 only), 페이퍼/모의 거부. **✅ KIS 문서로 TR·엔드포인트·지정가-only 검증 완료.**
  - ⚠️ **시세(데이터) 엔드포인트는 미변경** — 기존 `HHDFS00000300` 사용. 데이마켓 중 가격이 stale하면 전용 엔드포인트로 교체 필요 (남은 확인 항목).
- [x] **6. 프리/애프터 주문 경로** (`_submit_overseas_order`)
  - ✅ KIS 문서 확인: 프리/정규/애프터 **전부 정규 `order` endpoint + ORD_DVSN `00`** (KIS가 시각 라우팅). 별도 시간외 코드 불필요 → `_KIS_EXTENDED_ORD_DVSN` 제거, `session` 인자 제거로 단순화.
  - **무효였던 `01` 시장가 fallback 제거** (해외엔 `01` 없음). 해외 주문은 가격 없으면 거부.
  - 테스트: `tests/test_broker_kis.py` (세션 라우팅, 무가격 거부, 데이마켓 페이퍼 거부).

### Phase 4 — 손절 강화 (2026-06-18) ✅
- [x] **7. 손절 미체결 → 다음 세션 시작 즉시 재제출**
  - `order_manager.cancel_unfilled_stop_losses()`: 미체결(0 fill) 손절 주문만 취소 (부분체결은 보존). `metadata["reason"]=="stop_loss"`로 식별.
  - `bot._handle_session_transition()`: 세션 전환 시 호출 → 다음 틱에 전략이 새 세션 가격으로 손절 재발행 (#8 in-flight 가드 + 체결 기반 리셋 덕분에 포지션 안 날아감).
  - 슬리피지 버퍼 1% 그대로 (재발행도 marketable-limit).
  - 테스트: `tests/test_execution.py`(취소 필터), `tests/test_bot.py`(전환 트리거).

### Phase 5 — 카운터 정합성 (스코프 확장)
- [x] **8. stage 카운터 증가를 시그널 시점 → 체결 시점으로 이동** (전체 체결 기반, 2026-06-18) ✅
  - `Order`/`Fill`에 `metadata` 필드 추가 → 시그널 metadata가 같은 Order 객체를 타고 broker(실/페이퍼) 거쳐 `on_fill`까지 전달 (`engine._on_fill`가 `Fill.metadata` 채움).
  - `calculate_signal`에서 `_buy_stages`/`_sell_stages`/`_last_buy_time`/`_reset_position` 모두 제거 → `on_fill`로 이동. `metadata["stage"]`(target stage)로 set(증가X) → 부분체결 idempotent.
  - 손절: 포지션 완전 청산(`get_position<=0`) 시에만 reset (부분체결로 상태 안 날아감). 최종 분할매도: 체결 시 reset(잔량 사이클 재시작).
  - **in-flight 가드**: `order_manager._has_active_order()` — 같은 `(symbol, side)` 활성 주문 있으면 시그널 억제. 체결/취소/거부 시 해제 → 미체결 stage 재시도 가능.
  - 테스트: `tests/test_strategy.py`(체결 기반 카운터/리셋), `tests/test_execution.py`(in-flight 가드, metadata 전파).
  - ⚠️ 잔여 엣지(Phase 4로 이월): 시간외 손절 **부분**체결 → 잔량 보유한 채 주문 in-flight. 다음 세션 재제출은 Phase 4(#7)에서.
- [x] **9. 쿨다운 리셋 조건에서 RSI 50 제거** ✅ 2026-06-18
  - 쿨다운 조건 `and rsi_recovered` 제거 → `cooldown_days` 경과만.
  - `_rsi_recovered` 필드 + recovery 추적 블록 + `recovery_rsi` 파라미터 사용처 + `_reset_position`의 `_rsi_recovered` 제거 완료.

> ⚠️ **#8 시작 전 결정 필요**: 카운터를 체결 시점으로 옮기면, 미체결 주문이 매 틱 동일 stage 시그널을 재발행 → 중복 주문. signal-time 증가가 현재 이걸 막고 있음. 60초 dedup 윈도우로는 부족 (틱 간격과 동일). **in-flight 가드 필요** — 권장: 실행 계층(`_active_orders`)에서 같은 `(symbol, side)`에 활성 주문 있으면 새 시그널 억제. 추가로 최종매도/손절의 `_reset_position`도 체결 시점으로 옮길지 결정 필요.

### Phase 6 — 검증 ✅ 2026-06-18
- 각 Phase 인라인/회귀 테스트 완료 (271 passed): `test_scheduler.py`(세션), `test_strategy.py`(RSI 슬라이드·체결 카운터), `test_execution.py`(in-flight 가드·metadata·손절 취소), `test_broker_kis.py`(세션 라우팅), `test_bot.py`(일봉 폴링·세션 전환·심볼 동기화).
- 페이퍼 모드 구동 확인 (정규장 23:4x KST): 정상 기동, 새 스케줄러 활성, 정규장 가격/RSI 수집, 심볼 추가/제거 재시작 없이 반영.
- ⚠️ 정규장 외(데이마켓/프리/애프터) 시세 수집은 그 시간대 미구동으로 **미검증**.

## 추가 수정 — 심볼 동기화 핫리로드 버그 (2026-06-18, 커밋 736b612)

**증상:** 웹 UI/DB에서 심볼 추가·제거해도 봇 재시작 전까지 반영 안 됨.
**근본 원인:** 웹서버는 별도 스레드/이벤트 루프(`run_bot.py`)에서 돌고 봇과는 공유 DB로만 통신. 봇의 심볼 동기화 `_sync_enabled_symbols`가 `on_tick` 안에 있는데, `on_tick`은 `scheduler._run_loop`가 `is_market_open()`으로 게이팅 → 장 마감 중 편집은 다음 세션까지 reconcile 안 됨. 재시작은 startup `_load_strategies`가 DB를 무조건 다시 읽어 반영되는 것처럼 보였음.
**수정:** `TradingBot._config_sync_loop` 추가 — 시장 시간과 무관하게 15초마다 `_sync_enabled_symbols()` 호출 (`start`/`stop`에서 기동·취소). `on_tick`의 게이팅된 호출 제거 → 단일·상시 동기화 경로. 증분 동기화라 기존 심볼 상태(stage/진입가) 보존.
**검증:** 페이퍼 구동에서 MSFT 추가 ~2초, 제거 ~15초 내 반영 (재시작 없음).
- ⚠️ **남은 한계:** 새 *전략* 생성/활성화는 `_sync_enabled_symbols`가 아니라 `reload_callback`에 의존하는데, 그건 `asyncio.create_task`로 웹 루프에 스케줄 → 봇 루프 객체 교차 접근(불안정). "새 전략"은 아직 재시작 필요할 수 있음 (후속 작업).
- 후속(미착수): 즉시 반영(<1s)은 웹 엔드포인트 → `run_coroutine_threadsafe`로 봇 루프 직접 트리거하면 가능.

## 결정된 트레이드오프

- **백테스트와의 갭 (시간외 노이즈)**: 감수. 라이브 RSI 시뮬레이션하는 틱 백테스트는 만들지 않음.
- **장중 false trigger (정규장 마감 시 회복되어도 매수 들어감)**: 감수. 빠른 진입이 우선.
- **같은 거래일 연쇄 진입 (1·2·3차 분할 매수가 같은 날 다 들어감)**: 의도된 동작.
- **세션별 슬리피지 차등 없음 (모두 1%)**: 미체결 빈도 보고 추후 튜닝.

## ✅ 확정 (2026-06-18 사용자 확인)

**"손절 직후 쿨다운만 지나면 RSI 회복 없이 즉시 재진입 가능" — 의도된 동작 맞음.**

회복 조건(RSI ≥ 50) 제거 확정. cooldown_days 경과만으로 재진입 허용. 평균회귀 "바닥 잡기"로 일관성 유지. Phase 5 item #9 그대로 진행.

## 참고: 기존 코드 진입점

- 스케줄러: `src/utils/scheduler.py` (`is_market_open()`, `get_us_market_hours_kst()`)
- 봇 코어 와이어링: `src/bot/core.py:163` (`us_only=True`)
- 해외 주문: `src/broker/kis/client.py:458` (`_submit_overseas_order`)
- RSI 전략: `src/strategy/builtin/rsi_mean_reversion.py`
- 설정: `config/settings.yaml`
