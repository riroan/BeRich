# BeRich

한국투자증권(KIS) API를 사용한 실시간 자동매매 봇입니다.

## 설치 및 실행

### 1. 환경 변수 설정

`.env` 파일 생성:

```env
# KIS API
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=your_account_number
KIS_CANO=your_cano
KIS_ACNT_PRDT_CD=01

# MySQL
MYSQL_ROOT_PASSWORD=your_root_password
MYSQL_DATABASE=quant
MYSQL_USER=quant_user
MYSQL_PASSWORD=your_password
```

### 2. Docker로 실행

```bash
docker-compose up -d --build
```

### 3. 로그 확인

```bash
docker logs quant-bot -f
```

## 전략 설정

`config/strategies.yaml`에서 종목 및 파라미터 설정

## 백테스트

```bash
python scripts/backtest_rsi.py
python scripts/backtest_trailing.py
```
