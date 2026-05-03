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
