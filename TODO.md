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
