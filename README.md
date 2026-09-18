# 미국장 반도체·매크로 일일 브리핑

매일 한국시간 12시에 SOXX/NVDA/QQQ 차트와 반도체·매크로·옵션·뉴스를 수집해 파일로 만드는 작은 자동화입니다.

## 생성 파일

- `market-brief.md`: ChatGPT에 업로드하기 좋은 요약 문서
- `report.json`: 원본 구조화 데이터
- `indicators.csv`: 차트별 EMA 20/50/200, RSI(14), 세션별 VWAP, POC/VAH/VAL, gap, OR5/OR15, ATR14, RVOL
- `*.png`: extended-hours OHLC 캔들 차트
- `*-ohlcv.csv`: 각 심볼/타임프레임의 timestamp, OHLCV 원본
- `report.json`: 원본 데이터와 source/as_of/quality, 시장 breadth, relative strength, 이벤트, 뉴스, 옵션 결과

## 실행

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python market_report.py --output reports/manual
```

로컬에서 Python을 실행하고 싶지 않다면 저장소를 GitHub에 올린 뒤 `Actions` 탭에서 `Daily market report`를 한 번 수동 실행하세요. 이후 평일 12:00 KST에 GitHub 서버가 자동 실행하고, 실행 결과는 해당 workflow의 `Artifacts`에서 다운로드할 수 있습니다. 이 방식은 `.github/workflows/daily-market-report.yml`에 설정되어 있습니다.

## Discord 자동 전송

1. Discord 서버의 채널 설정에서 `연동` → `웹후크` → `새 웹후크`를 만듭니다.
2. Webhook URL을 복사합니다. URL은 다른 사람에게 공개하지 마세요.
3. GitHub 저장소의 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`으로 이동합니다.
4. 이름을 `DISCORD_WEBHOOK_URL`, 값에 복사한 URL을 입력합니다.

다음 workflow 실행부터 생성된 보고서 ZIP이 Discord 채널에 첨부됩니다. Secret을 등록하지 않아도 보고서 생성과 GitHub Artifact 업로드는 계속 동작합니다.

매일 12:00 KST에 실행하려면 서버의 `crontab -e`에 다음을 등록합니다.

```cron
0 12 * * 1-5 cd /workspaces/AI-Research && mkdir -p reports && bash run_daily.sh >> reports/cron.log 2>&1
```

실행 환경의 `TZ`가 UTC이면 KST 기준으로 맞지 않을 수 있으므로, 서버가 한국시간으로 설정되어 있는지 확인하거나 cron 앞에 `CRON_TZ=Asia/Seoul`을 추가하세요.

## 데이터 범위와 한계

Yahoo Finance는 1분봉을 최근 7일만 제공하므로 SOXX 1분 차트는 그 범위만 생성됩니다. 일봉은 3년, intraday는 60일을 받아 EMA200 warm-up을 확보한 뒤 표시 구간을 잘라냅니다. 모든 intraday 수집은 extended hours를 포함하고 최신 캔들의 timestamp를 기록합니다. 다만 Yahoo가 extended-hours 실제 volume을 0으로 반환하면 PM VWAP/Profile/RVOL을 계산하지 않고 `LOW QUALITY`로 표시합니다. 차트는 OHLC 캔들로 표시하며 Premarket/RTH/overnight VWAP은 세션마다 reset됩니다.

시세·차트·옵션·FRED·뉴스 데이터에는 가능한 경우 원천 데이터의 최신 timestamp를 붙입니다. 뉴스는 최근 48시간 내 제목에서 금리, Fed/FOMC, 수익, 반도체/AI, 관세, 지수·시장 등 영향 키워드가 확인되는 항목만 표시합니다.

현재 보고서에는 FRED 2년/10년 금리와 공개 경제 캘린더의 당일 미국 이벤트(실제값/예상값/이전값)를 포함합니다. FedWatch 데이터는 기본 CME 엔드포인트를 사용하며, 실행 환경에서 접근할 수 있는 JSON 엔드포인트가 따로 있으면 `FEDWATCH_URL` 환경변수로 지정할 수 있습니다. 응답이 없을 때는 보고서 생성을 중단하지 않고 `N/A`로 표시합니다.

옵션은 SOXX/QQQ/NVDA/SMH의 0DTE/weekly/monthly 대표 만기별로 spot ±20%, 양의 bid/ask, 양의 IV와 미결제약정, strike coverage를 필터링합니다. 충분한 coverage가 없으면 계산값 대신 `N/A`와 `LOW QUALITY`를 표시합니다. 유효한 체인에 대해 Gamma Exposure, Call Wall, Put Wall, Gamma Flip, 기준 만기일/수집 시각을 표시합니다. Volume Profile은 실제 volume-at-price가 있는 PM/당일 RTH/5RTH/20RTH의 POC/VAH/VAL을 제공합니다. 전일·전주·overnight·premarket OHLC, gap, OR5/OR15, ATR14, RVOL도 기록합니다.

모든 intraday timeframe의 previous close와 premarket gap은 canonical 직전 RTH 종가를 사용합니다. OR5/OR15는 09:30 ET 이후에만 채워지고, 개장 전에는 `N/A`입니다. 5RTH/20RTH Profile은 visible chart window가 아니라 raw 최근 5/20개 정규장 전체 데이터로 계산합니다.

Fed 금리확률은 CME FedWatch를 먼저 시도하고, CME가 차단되면 ZQ 선물 기반 확률을 공개하는 FedWatch monitor 결과를 fallback으로 읽습니다. fallback 결과의 기준일과 출처도 리포트에 표시되며, 두 소스 모두 실패한 경우에만 `N/A`가 됩니다.

SOXX 구성비는 iShares holdings CSV를 기본 소스로 사용해 weighted breadth와 top constituent contribution을 계산합니다. 구성비 원본이 차단되거나 오래되면 임의의 비중으로 대체하지 않고 `LOW QUALITY`로 표시합니다. SOXX/QQQ와 SMH/QQQ relative strength, SMH 5분봉도 함께 제공합니다.

시장 요약에는 NQ, ES, VIX, DXY, WTI, 2Y, 10Y와 가능한 경우 10Y real yield를 현재값, 변화, timestamp, 품질 상태와 함께 표시합니다. 경제지표는 actual/forecast/previous/surprise/importance, 향후 7일 반도체 기업 이벤트를 포함합니다. 뉴스는 최근 48시간의 시장 영향 키워드와 Reuters/Bloomberg/CNBC/WSJ/SEC/기업 IR 검색 결과를 event-deduplicate해 제목 기반 factual summary와 관련 ticker를 표시합니다.

경제 캘린더 공급자를 바꾸려면 `ECONOMIC_CALENDAR_URL`을 JSON URL로 지정하세요. KakaoTalk 자동 발송도 카카오 비즈메시지/채널 서버 인증이 필요하므로, 우선 생성된 `market-brief.md`를 파일로 전달하는 방식이 안정적입니다.

## 주의

이 프로젝트는 정보 수집용이며 투자 조언이 아닙니다. Yahoo Finance, FRED, Google News RSS의 서비스 제한이나 장외 시간에 따라 일부 값이 `N/A`가 될 수 있습니다.