# Bear Market Radar Bot setup

## 파일 구성

```text
radar.py                         # 메인 실행 파일
advanced_signals.py              # CAPEX, Nvidia guidance, 반도체 수출, Forward EPS, EPS Revision 자동수집
advanced_signals.csv             # 자동수집 실패 시 쓰는 수동 백업 CSV
eps_symbols.csv                  # Forward EPS / EPS Revision 계산용 watchlist
requirements.txt                 # Python 패키지
.github/workflows/radar.yml      # 매일 09:07 KST 자동 실행용 GitHub Actions workflow
```

## GitHub Secrets

Repository → Settings → Secrets and variables → Actions → New repository secret 에 아래 값을 넣으세요.

```text
FRED_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
SEC_USER_AGENT
DATA_GO_KR_SERVICE_KEY
FMP_API_KEY
```

`SEC_USER_AGENT` 예시:

```text
bearmarket-radar your-email@example.com
```

## 매우 중요

Telegram bot token은 코드나 CSV에 절대 직접 넣지 마세요. GitHub Secrets에만 저장하세요.
이미 채팅방에 노출한 token은 BotFather에서 Revoke current token으로 폐기하고 새로 발급받으세요.

## 수동 실행

GitHub Actions 탭 → Bear Market Radar → Run workflow.

## 매일 실행 시간

`.github/workflows/radar.yml`은 UTC 00:07에 실행되도록 설정되어 있습니다. 한국시간으로 09:07입니다.

## Forward EPS watchlist 변경

`eps_symbols.csv`의 `symbol` 열을 수정하면 됩니다. 또는 GitHub Secret/Variable이 아니라 Actions env에 `EPS_SYMBOLS`를 지정해도 됩니다.

예:

```text
EPS_SYMBOLS=NVDA,MSFT,AAPL,AMZN,GOOGL,META,AVGO,AMD
```

## advanced_signals.csv 사용법

자동수집이 실패하는 지표를 임시로 직접 넣고 싶을 때만 사용합니다.

```csv
name,value,risk,trend_z,asof,weight
하이퍼스케일러 CAPEX,"TTM YoY +20%",35,-0.4,2026-05-01,15
```

같은 이름의 지표가 자동수집되면 CSV 값은 무시됩니다.
