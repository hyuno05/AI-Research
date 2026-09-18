# 미국장 반도체·매크로 일일 브리핑

매일 한국시간 12시에 SOXX/NVDA/QQQ 차트와 반도체·매크로·옵션·뉴스를 수집해 파일로 만드는 작은 자동화입니다.

## 생성 파일

- `market-brief.md`: ChatGPT에 업로드하기 좋은 요약 문서
- `report.json`: 원본 구조화 데이터
- `indicators.csv`: 차트별 EMA 20/50/200, RSI(14), VWAP, 종가
- `*.png`: SOXX 1분/5분/30분/1시간/일봉, NVDA 5분, QQQ 5분 차트

## 실행

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python market_report.py --output reports/manual
```

로컬에서 Python을 실행하고 싶지 않다면 저장소를 GitHub에 올린 뒤 `Actions` 탭에서 `Daily market report`를 한 번 수동 실행하세요. 이후 평일 12:00 KST에 GitHub 서버가 자동 실행하고, 실행 결과는 해당 workflow의 `Artifacts`에서 다운로드할 수 있습니다. 이 방식은 `.github/workflows/daily-market-report.yml`에 설정되어 있습니다.

매일 12:00 KST에 실행하려면 서버의 `crontab -e`에 다음을 등록합니다.

```cron
0 12 * * 1-5 cd /workspaces/AI-Research && mkdir -p reports && bash run_daily.sh >> reports/cron.log 2>&1
```

실행 환경의 `TZ`가 UTC이면 KST 기준으로 맞지 않을 수 있으므로, 서버가 한국시간으로 설정되어 있는지 확인하거나 cron 앞에 `CRON_TZ=Asia/Seoul`을 추가하세요.

## 데이터 범위와 한계

Yahoo Finance는 1분봉을 최근 7일만 제공하므로 SOXX 1분 차트는 그 범위만 생성됩니다. 옵션은 가장 가까운 만기의 주요 미결제약정/거래량 행사가를 표시하며, Yahoo 데이터만으로 신뢰할 수 있는 감마 계산은 하지 않습니다.

경제지표 일정/실제 결과, Fed 발언, CME 금리확률, 옵션 감마는 별도 공급자 API가 필요합니다. 현재 보고서에는 해당 항목의 연결 필요 상태와 FRED 2년/10년 금리 최신값을 명시합니다. KakaoTalk 자동 발송도 카카오 비즈메시지/채널 서버 인증이 필요하므로, 우선 생성된 `market-brief.md`를 파일로 전달하는 방식이 안정적입니다.

## 주의

이 프로젝트는 정보 수집용이며 투자 조언이 아닙니다. Yahoo Finance, FRED, Google News RSS의 서비스 제한이나 장외 시간에 따라 일부 값이 `N/A`가 될 수 있습니다.