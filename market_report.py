#!/usr/bin/env python3
"""Generate a daily semiconductor and macro market brief.

The report is deliberately file-first: it writes Markdown, JSON, CSV and PNG
files so it can be attached to ChatGPT or delivered by a separate messenger job.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import matplotlib.pyplot as plt
import pandas as pd
import requests
import yfinance as yf


CHARTS = {
    "SOXX": ["1m", "5m", "30m", "1h", "1d"],
    "NVDA": ["5m"],
    "QQQ": ["5m"],
}
MARKET = {
    "NQ 선물": "NQ=F",
    "VIX": "^VIX",
    "미국채 10년 금리": "^TNX",
    "DXY": "DX-Y.NYB",
}
SEMIS = {"NVDA": "NVDA", "AMD": "AMD", "AVGO": "AVGO", "MU": "MU", "TSM": "TSM"}


@dataclass
class Quote:
    symbol: str
    price: float | None
    change_pct: float | None
    as_of: str | None


def clean_number(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def download(symbol: str, interval: str) -> pd.DataFrame:
    period = "7d" if interval == "1m" else "60d" if interval in {"5m", "15m", "30m", "1h"} else "2y"
    data = yf.download(symbol, period=period, interval=interval, auto_adjust=False, progress=False)
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data.dropna(subset=["Close"])


def add_indicators(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    close = data["Close"]
    for length in (20, 50, 200):
        data[f"EMA{length}"] = close.ewm(span=length, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False).mean()
    data["RSI14"] = 100 - (100 / (1 + gain / loss.replace(0, pd.NA)))
    typical = (data["High"] + data["Low"] + close) / 3
    session = pd.Series(data.index.date, index=data.index)
    data["VWAP"] = (typical * data["Volume"]).groupby(session).cumsum() / data["Volume"].groupby(session).cumsum()
    return data


def volume_profile_poc(data: pd.DataFrame, bins: int = 30) -> float | None:
    """Return the price level with the highest volume in fixed price bins."""
    prices = pd.to_numeric(data["Close"], errors="coerce")
    volumes = pd.to_numeric(data["Volume"], errors="coerce").fillna(0)
    valid = pd.DataFrame({"price": prices, "volume": volumes}).dropna()
    if valid.empty or valid["price"].nunique() < 2:
        return clean_number(valid["price"].iloc[-1]) if not valid.empty else None
    price_bins = pd.cut(valid["price"], bins=bins)
    profile = valid.groupby(price_bins, observed=True)["volume"].sum()
    poc_bin = profile.idxmax()
    return clean_number((poc_bin.left + poc_bin.right) / 2)


def prepare_chart_data(data: pd.DataFrame) -> pd.DataFrame:
    return add_indicators(data).tail(400)


def save_chart(symbol: str, interval: str, data: pd.DataFrame, output: Path) -> str:
    data = prepare_chart_data(data)
    poc = volume_profile_poc(data)
    figure, (price_axis, volume_axis) = plt.subplots(2, 1, figsize=(13, 7), sharex=True, height_ratios=[3, 1])
    price_axis.plot(data.index, data["Close"], label="Close", color="#152238", linewidth=1.4)
    for length, color in ((20, "#e76f51"), (50, "#2a9d8f"), (200, "#e9c46a")):
        price_axis.plot(data.index, data[f"EMA{length}"], label=f"EMA {length}", linewidth=1)
    price_axis.plot(data.index, data["VWAP"], label="VWAP", color="#7b2cbf", linewidth=1)
    if poc is not None:
        price_axis.axhline(poc, label=f"Volume POC {poc:.2f}", color="#d62828", linewidth=1, linestyle="--")
    price_axis.set_title(f"{symbol} · {interval}")
    price_axis.legend(loc="upper left", ncol=5, fontsize=8)
    price_axis.grid(alpha=0.2)
    volume_axis.bar(data.index, data["Volume"], width=0.003, color="#8ecae6")
    volume_axis.set_ylabel("Volume")
    volume_axis.grid(alpha=0.2)
    figure.tight_layout()
    filename = f"{symbol.lower()}-{interval.replace('m', 'min').replace('h', 'hour')}.png"
    figure.savefig(output / filename, dpi=140)
    plt.close(figure)
    return filename


def quote(symbol: str) -> Quote:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="5d", interval="1d", auto_adjust=False)
    if history.empty:
        return Quote(symbol, None, None, None)
    current = clean_number(history["Close"].iloc[-1])
    previous = clean_number(history["Close"].iloc[-2]) if len(history) > 1 else None
    change = (current / previous - 1) * 100 if current is not None and previous else None
    return Quote(symbol, current, change, str(history.index[-1].date()))


def options(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    try:
        expiries = ticker.options
        if not expiries:
            return {"symbol": symbol, "available": False}
        expiry = expiries[0]
        chain = ticker.option_chain(expiry)
        rows = pd.concat([chain.calls.assign(type="call"), chain.puts.assign(type="put")])
        spot = clean_number(getattr(ticker, "fast_info", {}).get("last_price"))
        if spot is None:
            history = ticker.history(period="5d", interval="1d", auto_adjust=False)
            spot = clean_number(history["Close"].iloc[-1]) if not history.empty else None
        rows["openInterest"] = pd.to_numeric(rows["openInterest"], errors="coerce").fillna(0)
        rows["volume"] = pd.to_numeric(rows["volume"], errors="coerce").fillna(0)
        rows["strike"] = pd.to_numeric(rows["strike"], errors="coerce")
        rows["impliedVolatility"] = pd.to_numeric(rows["impliedVolatility"], errors="coerce")
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        days_to_expiry = max((expiry_date - datetime.now(timezone.utc).date()).days, 1)
        if spot is not None:
            rows["gamma"] = rows.apply(lambda row: option_gamma(spot, row["strike"], row["impliedVolatility"], days_to_expiry), axis=1)
            rows["gammaExposure"] = rows["gamma"] * rows["openInterest"] * 100 * spot * spot * 0.01
            rows.loc[rows["type"] == "put", "gammaExposure"] *= -1
        else:
            rows["gamma"] = None
            rows["gammaExposure"] = None
        walls = gamma_walls(rows)
        top = rows.sort_values(["openInterest", "volume"], ascending=False).head(12)
        return {"symbol": symbol, "expiry": expiry, "available": True, "spot": spot, "walls": walls, "rows": top.to_dict("records")}
    except Exception as error:  # Yahoo options can be unavailable outside market hours.
        return {"symbol": symbol, "available": False, "error": str(error)}


def option_gamma(spot: float | None, strike: float | None, implied_volatility: float | None, days_to_expiry: int) -> float | None:
    if not spot or not strike or not implied_volatility or implied_volatility <= 0:
        return None
    time_to_expiry = max(days_to_expiry, 1) / 365
    d1 = (math.log(spot / strike) + 0.5 * implied_volatility**2 * time_to_expiry) / (implied_volatility * math.sqrt(time_to_expiry))
    return NormalDist().pdf(d1) / (spot * implied_volatility * math.sqrt(time_to_expiry))


def gamma_walls(rows: pd.DataFrame) -> dict[str, Any]:
    valid = rows.dropna(subset=["strike", "gammaExposure"])
    if valid.empty:
        return {"totalGammaExposure": None, "callWall": None, "putWall": None}
    calls = valid[valid["type"] == "call"].groupby("strike")["gammaExposure"].sum()
    puts = valid[valid["type"] == "put"].groupby("strike")["gammaExposure"].sum()
    return {
        "totalGammaExposure": clean_number(valid["gammaExposure"].sum()),
        "callWall": clean_number(calls.idxmax()) if not calls.empty else None,
        "putWall": clean_number(puts.idxmin()) if not puts.empty else None,
    }


def economic_calendar() -> dict[str, Any]:
    url = os.getenv("ECONOMIC_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    last_error: Exception | None = None
    try:
        for _ in range(2):
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()
                events = response.json()
                rows = []
                for event in events if isinstance(events, list) else []:
                    if event.get("country") not in {"USD", "US"} or str(event.get("date", ""))[:10] != today:
                        continue
                    rows.append({key: event.get(key, "") for key in ("title", "date", "impact", "actual", "forecast", "previous")})
                return {"date": today, "available": True, "source": url, "events": rows}
            except (requests.RequestException, ValueError, TypeError) as error:
                last_error = error
        return {"date": today, "available": False, "source": url, "events": [], "error": str(last_error)}
    except Exception as error:
        return {"date": today, "available": False, "source": url, "events": [], "error": str(error)}


def fed_probabilities() -> dict[str, Any]:
    url = os.getenv("FEDWATCH_URL", "https://www.cmegroup.com/services/fedwatch-tool-data.json")
    last_error: Exception | None = None
    for _ in range(2):
        try:
            response = requests.get(url, timeout=20, headers={"User-Agent": "market-report/1.0"})
            response.raise_for_status()
            data = response.json()
            meetings = data.get("meetings", data.get("data", [])) if isinstance(data, dict) else data
            return {"available": True, "source": url, "meetings": meetings if isinstance(meetings, list) else []}
        except (requests.RequestException, ValueError, TypeError) as error:
            last_error = error
    return {"available": False, "source": url, "meetings": [], "error": str(last_error)}


def news() -> list[dict[str, str]]:
    feed = feedparser.parse("https://news.google.com/rss/search?q=(semiconductor+OR+AI+OR+tariff+OR+geopolitics)+when:1d&hl=en-US&gl=US&ceid=US:en")
    return [{"title": item.get("title", ""), "link": item.get("link", "")} for item in feed.entries[:15]]


def fred_series(series_id: str) -> float | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        data = pd.read_csv(url).replace(".", pd.NA).dropna()
        return clean_number(data.iloc[-1, 1])
    except (requests.RequestException, ValueError, IndexError):
        return None


def format_value(value: float | None, suffix: str = "") -> str:
    return f"{value:,.2f}{suffix}" if value is not None else "N/A"


def build_report(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    chart_files: list[dict[str, str]] = []
    chart_stats: list[dict[str, Any]] = []
    for symbol, intervals in CHARTS.items():
        for interval in intervals:
            data = download(symbol, interval)
            if data.empty:
                continue
            chart_data = prepare_chart_data(data)
            chart_files.append({"symbol": symbol, "interval": interval, "file": save_chart(symbol, interval, chart_data, output)})
            latest = chart_data.iloc[-1]
            chart_stats.append({"symbol": symbol, "interval": interval, "close": clean_number(latest["Close"]), "rsi14": clean_number(latest["RSI14"]), "vwap": clean_number(latest["VWAP"]), "poc": volume_profile_poc(chart_data), "ema20": clean_number(latest["EMA20"]), "ema50": clean_number(latest["EMA50"]), "ema200": clean_number(latest["EMA200"])})
    market_quotes = {name: asdict(quote(symbol)) for name, symbol in MARKET.items()}
    market_quotes["미국채 2년 금리"] = {"symbol": "DGS2", "price": fred_series("DGS2"), "change_pct": None, "as_of": None}
    semiconductor_quotes = {name: asdict(quote(symbol)) for name, symbol in SEMIS.items()}
    options_data = [options(symbol) for symbol in ("SOXX", "NVDA", "AMD", "AVGO", "MU", "TSM")]
    payload = {"generated_at": generated_at.isoformat(), "charts": chart_stats, "chart_files": chart_files, "market": market_quotes, "semiconductors": semiconductor_quotes, "options": options_data, "economic_calendar": economic_calendar(), "fed_probabilities": fed_probabilities(), "news": news(), "macro": {"DGS2": fred_series("DGS2"), "DGS10": fred_series("DGS10")}, "data_notes": ["1분봉은 Yahoo Finance 제공 제한 때문에 최근 7일만 수집합니다.", "옵션 감마는 Yahoo의 내재변동성/미결제약정으로 계산한 추정치이며, 경제 캘린더와 Fed 금리확률은 외부 데이터 제공자 응답에 따라 N/A가 될 수 있습니다."]}
    (output / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(chart_stats).to_csv(output / "indicators.csv", index=False)
    report_path = output / "market-brief.md"
    report_path.write_text(render_markdown(payload), encoding="utf-8")
    return report_path


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# 미국장 반도체·매크로 브리핑", f"생성 시각(UTC): {payload['generated_at']}", "", "## 시장", "| 항목 | 값 | 등락률 |", "|---|---:|---:|"]
    for name, item in payload["market"].items():
        lines.append(f"| {name} | {format_value(item['price'])} | {format_value(item['change_pct'], '%')} |")
    lines += ["", "## 반도체 프리마켓/최근 거래일", "| 종목 | 값 | 등락률 |", "|---|---:|---:|"]
    for name, item in payload["semiconductors"].items():
        lines.append(f"| {name} | {format_value(item['price'])} | {format_value(item['change_pct'], '%')} |")
    lines += ["", "## 지표 요약", "| 종목 | 주기 | 종가 | RSI(14) | EMA20 | EMA50 | EMA200 | VWAP | Volume POC |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for item in payload["charts"]:
        lines.append("| {symbol} | {interval} | {close:.2f} | {rsi14:.2f} | {ema20:.2f} | {ema50:.2f} | {ema200:.2f} | {vwap:.2f} | {poc:.2f} |".format(**{key: (value if value is not None else float("nan")) for key, value in item.items()}))
    lines += ["", "## 옵션 주요 행사가", "| 종목 | 만기 | 구분 | 행사가 | 미결제약정 | 거래량 | Gamma | Gamma Exposure |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for item in payload["options"]:
        walls = item.get("walls", {})
        lines.append(f"- {item['symbol']} Gamma 합계: {format_value(walls.get('totalGammaExposure'))} · Call Wall: {format_value(walls.get('callWall'))} · Put Wall: {format_value(walls.get('putWall'))}")
        for row in item.get("rows", []):
            lines.append(f"| {item['symbol']} | {item.get('expiry', 'N/A')} | {row.get('type', '')} | {row.get('strike', '')} | {row.get('openInterest', 0)} | {row.get('volume', 0)} | {format_value(row.get('gamma'))} | {format_value(row.get('gammaExposure'))} |")
    calendar = payload["economic_calendar"]
    lines += ["", "## 당일 경제지표", "| 시각 | 지표 | 중요도 | 실제 | 예상 | 이전 |", "|---|---|---|---:|---:|---:|"]
    for event in calendar.get("events", []):
        lines.append(f"| {event.get('date', '')} | {event.get('title', '')} | {event.get('impact', '')} | {event.get('actual', '') or 'N/A'} | {event.get('forecast', '') or 'N/A'} | {event.get('previous', '') or 'N/A'} |")
    if not calendar.get("events"):
        lines.append("| - | 당일 미국 경제지표 없음 또는 데이터 미수신 | - | N/A | N/A | N/A |")
    fed = payload["fed_probabilities"]
    lines += ["", "## Fed 금리확률"]
    if fed.get("meetings"):
        for meeting in fed["meetings"]:
            lines.append(f"- {meeting}")
    else:
        lines.append("- N/A: FedWatch 데이터가 제공되지 않았습니다.")
    lines += ["", "## 매크로 참고", f"- FRED 2년물: {format_value(payload['macro']['DGS2'], '%')}", f"- FRED 10년물: {format_value(payload['macro']['DGS10'], '%')}", "- 경제지표 실제값/예상값은 공개 캘린더, Fed 금리확률은 FedWatch 데이터 응답이 제공하는 범위만 표시합니다.", "", "## 주요 뉴스"]
    lines += [f"- [{item['title']}]({item['link']})" for item in payload["news"]]
    lines += ["", "## 첨부 차트", *[f"- `{item['file']}`" for item in payload["chart_files"]], "", "> 이 파일은 정보 제공용 자동 수집물이며 투자 조언이 아닙니다."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a daily market brief")
    parser.add_argument("--output", default=os.getenv("REPORT_DIR", "reports"), help="output directory")
    args = parser.parse_args()
    print(build_report(Path(args.output)))


if __name__ == "__main__":
    main()
