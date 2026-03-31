# BeRich

한국투자증권(KIS) API 기반 RSI Mean Reversion 자동매매 봇

## 주요 기능

- **RSI Mean Reversion 전략** - 일봉 RSI 기반 3단계 분할 매수/매도
- **웹 대시보드** - 실시간 포지션, RSI 모니터, 차트 (포트 9095)
- **포트폴리오 관리** - 종목별 최대 비중 설정, 파이차트 시각화
- **종목 관리** - 웹에서 실시간 종목 추가/삭제/활성화 (KIS API 검증)
- **전략 설정** - 웹에서 RSI 기간, 매수/매도 레벨, 손절 등 실시간 변경
- **페이퍼 트레이딩** - 실제 시세 + 가상 주문으로 전략 검증
- **Discord 알림** - 매수/매도 체결, 손절, 시스템 오류 알림
- **분석** - 일간/주간/월간 리포트, 드로우다운 분석, 승률 통계

## 매매 로직

```
[매수] RSI 하락 시 3단계 분할 매수 (총자산 x 종목비중 x 단계비율)
  1차: RSI <= 35 → 30%
  2차: RSI <= 30 → 35%
  3차: RSI <= 25 → 35%

[매도] RSI 상승 시 3단계 분할 매도 (보유량 기준)
  1차: RSI >= 70 → 25%
  2차: RSI >= 75 → 35%
  3차: RSI >= 80 → 40%

[손절] 평단 대비 -10% → 전량 매도
[쿨다운] 3일 경과 + RSI 50 이상 회복 시 매수 단계 리셋
```

## 설치 및 실행

### 1. 환경 변수 설정

`.env` 파일 생성:

```env
# KIS API
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=your_account_number

# 모드: true=페이퍼트레이딩, false=실거래
KIS_PAPER_TRADING=true

# MySQL
MYSQL_ROOT_PASSWORD=your_root_password
MYSQL_DATABASE=quant
MYSQL_USER=quant
MYSQL_PASSWORD=your_password

# Discord (선택)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# 로깅
LOG_LEVEL=INFO
```

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
| `/trades` | 거래 내역 |
| `/performance` | 성과 분석 |
| `/analytics` | 리포트, 드로우다운, 승률 통계 |
| `/symbol/{symbol}` | 종목 상세 차트 (가격 + RSI) |

## 프로젝트 구조

```
BeRich/
├── config/
│   ├── settings.yaml        # 봇 설정 (워밍업, 리스크)
│   └── strategies.yaml      # 전략 및 종목 설정 (초기 시딩용)
├── src/
│   ├── bot/                  # 봇 코어 (틱 처리, 대시보드 동기화)
│   ├── broker/
│   │   ├── kis/              # 한국투자증권 API 클라이언트
│   │   └── paper.py          # 페이퍼 트레이딩 브로커
│   ├── strategy/
│   │   ├── base.py           # 전략 베이스 클래스
│   │   ├── engine.py         # 전략 실행 엔진
│   │   └── builtin/          # RSI Mean Reversion 등
│   ├── execution/            # 주문 관리자
│   ├── data/                 # DB 모델 및 스토리지
│   ├── analytics/            # 리포트, 드로우다운, 통계
│   ├── web/                  # FastAPI 대시보드
│   ├── risk/                 # 리스크 관리
│   └── utils/                # 설정, 로거, 스케줄러, 알림
├── tests/                    # 테스트
├── docker-compose.yml
├── Dockerfile
└── .env                      # 환경 변수 (gitignore)
```

## 설정

### 종목 관리

처음 실행 시 `config/strategies.yaml`에서 DB로 자동 시딩. 이후 웹 `/symbols` 페이지에서 관리.
- 종목 추가 시 KIS API로 유효성 검증
- 종목별 최대 포트폴리오 비중(%) 설정 가능
- 활성/비활성 전환 시 재시작 없이 즉시 반영

### 전략 파라미터

웹 `/settings` 페이지에서 실시간 변경:
- RSI 기간, 손절 %, 쿨다운 일수
- 매수/매도 레벨 및 비율

### 페이퍼 트레이딩

`.env`에서 `KIS_PAPER_TRADING=true`로 설정하면:
- 실제 시세 조회 (KIS API)
- 주문은 가상 체결 (실제 돈 사용 안 함)
- 워밍업 없이 즉시 시작
- 대시보드에 `PAPER` 배지 표시

## 테스트

```bash
python -m pytest tests/ -v
```

## 기술 스택

- **Python 3.12** / FastAPI / SQLAlchemy (async)
- **MySQL 8.0** / Docker Compose
- **KIS Open API** (한국투자증권)
- **LightweightCharts 4.2** (차트)
- **Discord Webhooks** (알림)
