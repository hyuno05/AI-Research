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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import pandas as pd
import requests
import yfinance as yf


CHARTS = {
    "SOXX": ["1m", "5m", "30m", "1h", "1d"],
    "NVDA": ["5m"],
    "QQQ": ["5m"],
}
VISIBLE_BARS = {"1m": 390, "5m": 240, "30m": 160, "1h": 120, "1d": 120}
NY_TZ = ZoneInfo("America/New_York")
PREMARKET_START = "04:00"
PREMARKET_END = "09:30"
FEDWATCH_FALLBACK_URL = "https://amazingmazy.github.io/fedwatch-monitor/_static/amazingmazy--fedwatch_monitor/fedwatch_latest_forecast.html"
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
    data = yf.download(symbol, period=period, interval=interval, auto_adjust=False, prepost=True, progress=False)
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    if data.index.tz is None:
        data.index = data.index.tz_localize(timezone.utc)
    return data


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
    index = data.index.tz_convert(NY_TZ) if getattr(data.index, "tz", None) else data.index.tz_localize(NY_TZ)
    session = pd.Series(index.date, index=data.index)
    data["VWAP"] = (typical * data["Volume"]).groupby(session).cumsum() / data["Volume"].groupby(session).cumsum()
    return data


def volume_profile(data: pd.DataFrame, bins: int = 30) -> dict[str, float | None]:
    """Return POC and 70% value area from the visible OHLCV range."""
    prices = (pd.to_numeric(data["High"], errors="coerce") + pd.to_numeric(data["Low"], errors="coerce") + pd.to_numeric(data["Close"], errors="coerce")) / 3
    volumes = pd.to_numeric(data["Volume"], errors="coerce").fillna(0)
    valid = pd.DataFrame({"price": prices, "volume": volumes}).dropna()
    if valid.empty or valid["price"].nunique() < 2:
        price = clean_number(valid["price"].iloc[-1]) if not valid.empty else None
        return {"poc": price, "vah": price, "val": price}
    price_bins = pd.cut(valid["price"], bins=bins)
    profile = valid.groupby(price_bins, observed=True)["volume"].sum()
    poc_bin = profile.idxmax()
    centers = pd.Series({(bucket.left + bucket.right) / 2: volume for bucket, volume in profile.items()})
    poc = float(centers.idxmax())
    target = profile.sum() * 0.70
    poc_position = centers.index.get_loc(poc)
    selected = {poc_position}
    accumulated = float(profile.max())
    while accumulated < target and (len(selected) < len(centers)):
        candidates = [position for position in (min(selected) - 1, max(selected) + 1) if 0 <= position < len(centers) and position not in selected]
        if not candidates:
            break
        position = max(candidates, key=lambda candidate: profile.iloc[candidate])
        selected.add(position)
        accumulated += float(profile.iloc[position])
    return {"poc": poc, "vah": clean_number(max(centers.index[position] for position in selected)), "val": clean_number(min(centers.index[position] for position in selected))}


def volume_profile_poc(data: pd.DataFrame, bins: int = 30) -> float | None:
    return volume_profile(data, bins)["poc"]


def session_profiles(data: pd.DataFrame) -> dict[str, Any]:
    index = data.index.tz_convert(NY_TZ) if getattr(data.index, "tz", None) else data.index.tz_localize(NY_TZ)
    sessions = pd.Series(index.date, index=data.index)
    daily = {str(day): volume_profile(data.loc[sessions == day]) for day in sessions.unique()}
    recent = data.loc[sessions.isin(sorted(sessions.unique())[-5:])]
    return {"sessions": daily, "five_day": volume_profile(recent)}


def prepare_chart_data(data: pd.DataFrame, interval: str) -> pd.DataFrame:
    return add_indicators(data).tail(VISIBLE_BARS.get(interval, 400))


def save_chart(symbol: str, interval: str, data: pd.DataFrame, output: Path) -> str:
    profile = volume_profile(data)
    figure, (price_axis, volume_axis) = plt.subplots(2, 1, figsize=(13, 7), sharex=True, height_ratios=[3, 1])
    x_values = mdates.date2num(data.index.to_pydatetime())
    candle_width = max((x_values[-1] - x_values[0]) / max(len(data), 1) * 0.7, 0.0003)
    for x_value, (_, row) in zip(x_values, data.iterrows()):
        color = "#2a9d8f" if row["Close"] >= row["Open"] else "#e76f51"
        price_axis.vlines(x_value, row["Low"], row["High"], color=color, linewidth=0.7)
        price_axis.add_patch(Rectangle((x_value - candle_width / 2, min(row["Open"], row["Close"])), candle_width, max(abs(row["Close"] - row["Open"]), 0.0001), facecolor=color, edgecolor=color, linewidth=0.5))
    for length, color in ((20, "#e76f51"), (50, "#2a9d8f"), (200, "#e9c46a")):
        price_axis.plot(data.index, data[f"EMA{length}"], label=f"EMA {length}", linewidth=1)
    price_axis.plot(data.index, data["VWAP"], label="VWAP", color="#7b2cbf", linewidth=1)
    if profile["poc"] is not None:
        price_axis.axhline(profile["poc"], label=f"POC {profile['poc']:.2f}", color="#d62828", linewidth=1, linestyle="--")
        price_axis.axhline(profile["vah"], label=f"VAH {profile['vah']:.2f}", color="#457b9d", linewidth=0.8, linestyle=":")
        price_axis.axhline(profile["val"], label=f"VAL {profile['val']:.2f}", color="#457b9d", linewidth=0.8, linestyle=":")
    price_axis.set_title(f"{symbol} · {interval} · extended hours · latest {data.index[-1].isoformat()}")
    price_axis.legend(loc="upper left", ncol=5, fontsize=8)
    price_axis.grid(alpha=0.2)
    volume_axis.bar(x_values, data["Volume"], width=candle_width, color="#8ecae6")
    volume_axis.set_ylabel("Volume")
    volume_axis.grid(alpha=0.2)
    figure.tight_layout()
    price_axis.xaxis_date()
    price_axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=NY_TZ))
    filename = f"{symbol.lower()}-{interval.replace('m', 'min').replace('h', 'hour')}.png"
    figure.savefig(output / filename, dpi=140)
    plt.close(figure)
    return filename


def quote(symbol: str) -> Quote:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="5d", interval="1m", auto_adjust=False, prepost=True)
    if history.empty:
        return Quote(symbol, None, None, None)
    current = clean_number(history["Close"].iloc[-1])
    previous = clean_number(history["Close"].iloc[-2]) if len(history) > 1 else None
    change = (current / previous - 1) * 100 if current is not None and previous else None
    return Quote(symbol, current, change, history.index[-1].isoformat())


def atr14(data: pd.DataFrame) -> float | None:
    if data.empty:
        return None
    previous_close = data["Close"].shift(1)
    true_range = pd.concat([data["High"] - data["Low"], (data["High"] - previous_close).abs(), (data["Low"] - previous_close).abs()], axis=1).max(axis=1)
    return clean_number(true_range.rolling(14).mean().iloc[-1])


def ohlc_summary(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        return {key: None for key in ("open", "high", "low", "close", "volume", "as_of")}
    return {"open": clean_number(data["Open"].iloc[0]), "high": clean_number(data["High"].max()), "low": clean_number(data["Low"].min()), "close": clean_number(data["Close"].iloc[-1]), "volume": clean_number(data["Volume"].sum()), "as_of": data.index[-1].isoformat()}


def trading_context(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        return {}
    local_index = data.index.tz_convert(NY_TZ) if getattr(data.index, "tz", None) else data.index.tz_localize(NY_TZ)
    local = data.copy()
    local.index = local_index
    today = local.index[-1].date()
    regular = local.between_time("09:30", "16:00")
    days = sorted(set(regular.index.date))
    if not days:
        return {"previous_day": ohlc_summary(pd.DataFrame()), "premarket": ohlc_summary(pd.DataFrame()), "gap_pct": None, "atr14": atr14(data)}
    previous_day = days[-2] if len(days) > 1 and days[-1] == today else days[-1]
    previous = regular[regular.index.date == previous_day]
    today_regular = regular[regular.index.date == today]
    prem = local[(local.index.date == today) & (local.index.strftime("%H:%M") >= PREMARKET_START) & (local.index.strftime("%H:%M") < PREMARKET_END)]
    current_open = clean_number(today_regular["Open"].iloc[0]) if not today_regular.empty else (clean_number(prem["Open"].iloc[0]) if not prem.empty else None)
    prior_close = clean_number(previous["Close"].iloc[-1]) if not previous.empty else None
    return {"previous_day": ohlc_summary(previous), "premarket": ohlc_summary(prem), "gap_pct": (current_open / prior_close - 1) * 100 if current_open is not None and prior_close else None, "atr14": atr14(data)}


def options(symbol: str) -> dict[str, Any]:
    ticker = yf.Ticker(symbol)
    try:
        expiries = ticker.options
        if not expiries:
            return {"symbol": symbol, "available": False}
        spot = clean_number(getattr(ticker, "fast_info", {}).get("last_price"))
        if spot is None:
            history = ticker.history(period="5d", interval="1d", auto_adjust=False)
            spot = clean_number(history["Close"].iloc[-1]) if not history.empty else None
        today = datetime.now(NY_TZ).date()
        expiry_dates = [datetime.strptime(value, "%Y-%m-%d").date() for value in expiries]
        categories: dict[str, date] = {}
        for expiry_date in expiry_dates:
            if expiry_date == today:
                categories["0DTE"] = expiry_date
            elif "weekly" not in categories and expiry_date >= today:
                categories["weekly"] = expiry_date
            elif "monthly" not in categories and expiry_date >= today and expiry_date.weekday() == 4 and 15 <= expiry_date.day <= 21:
                categories["monthly"] = expiry_date
        if "monthly" not in categories:
            monthly = [value for value in expiry_dates if value >= today and value.weekday() == 4 and 15 <= value.day <= 21]
            if monthly:
                categories["monthly"] = min(monthly)
        result_categories = {}
        captured_at = datetime.now(timezone.utc).isoformat()
        for category, expiry_date in categories.items():
            expiry = expiry_date.isoformat()
            chain = ticker.option_chain(expiry)
            rows = pd.concat([chain.calls.assign(type="call"), chain.puts.assign(type="put")])
            rows = valid_option_rows(rows, spot)
            if rows.empty:
                result_categories[category] = {"expiry": expiry, "data_as_of": captured_at, "rows": [], "walls": gamma_walls(rows)}
                continue
            days_to_expiry = max((expiry_date - today).days, 0)
            rows["gamma"] = rows.apply(lambda row: option_gamma(spot, row["strike"], row["impliedVolatility"], days_to_expiry), axis=1)
            rows["gammaExposure"] = rows["gamma"] * rows["openInterest"] * 100 * spot * spot * 0.01
            rows.loc[rows["type"] == "put", "gammaExposure"] *= -1
            walls = gamma_walls(rows)
            top = rows.sort_values(["openInterest", "volume"], ascending=False).head(12)
            result_categories[category] = {"expiry": expiry, "data_as_of": captured_at, "rows": top.to_dict("records"), "walls": walls}
        return {"symbol": symbol, "available": bool(result_categories), "spot": spot, "categories": result_categories, "data_as_of": captured_at}
    except Exception as error:  # Yahoo options can be unavailable outside market hours.
        return {"symbol": symbol, "available": False, "error": str(error)}


def valid_option_rows(rows: pd.DataFrame, spot: float | None) -> pd.DataFrame:
    columns = ["strike", "bid", "ask", "impliedVolatility", "openInterest", "volume"]
    for column in columns:
        rows[column] = pd.to_numeric(rows.get(column), errors="coerce")
    rows = rows.dropna(subset=["strike", "bid", "ask", "impliedVolatility", "openInterest"])
    rows = rows[(rows["bid"] > 0) & (rows["ask"] >= rows["bid"]) & (rows["impliedVolatility"] > 0) & (rows["openInterest"] > 0)]
    if spot is not None:
        rows = rows[rows["strike"].between(spot * 0.8, spot * 1.2)]
    return rows


def option_gamma(spot: float | None, strike: float | None, implied_volatility: float | None, days_to_expiry: int) -> float | None:
    if not spot or not strike or not implied_volatility or implied_volatility <= 0:
        return None
    time_to_expiry = max(days_to_expiry, 1) / 365
    d1 = (math.log(spot / strike) + 0.5 * implied_volatility**2 * time_to_expiry) / (implied_volatility * math.sqrt(time_to_expiry))
    return NormalDist().pdf(d1) / (spot * implied_volatility * math.sqrt(time_to_expiry))


def gamma_walls(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty or "gammaExposure" not in rows.columns:
        return {"totalGammaExposure": None, "callWall": None, "putWall": None, "gammaFlip": None}
    valid = rows.dropna(subset=["strike", "gammaExposure"])
    if valid.empty:
        return {"totalGammaExposure": None, "callWall": None, "putWall": None, "gammaFlip": None}
    calls = valid[valid["type"] == "call"].groupby("strike")["gammaExposure"].sum()
    puts = valid[valid["type"] == "put"].groupby("strike")["gammaExposure"].sum()
    by_strike = valid.groupby("strike")["gammaExposure"].sum().sort_index()
    cumulative = by_strike.cumsum()
    flip = None
    for position, (before, after) in enumerate(zip(cumulative.iloc[:-1], cumulative.iloc[1:]), start=1):
        if before == 0 or before * after < 0:
            flip = clean_number(cumulative.index[position])
            break
    return {
        "totalGammaExposure": clean_number(valid["gammaExposure"].sum()),
        "callWall": clean_number(calls.idxmax()) if not calls.empty else None,
        "putWall": clean_number(puts.idxmin()) if not puts.empty else None,
        "gammaFlip": flip,
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
            return {"available": True, "source": url, "as_of": datetime.now(timezone.utc).isoformat(), "next_fomc": next_fomc_date(), "meetings": meetings if isinstance(meetings, list) else []}
        except (requests.RequestException, ValueError, TypeError) as error:
            last_error = error
    fallback_url = os.getenv("FEDWATCH_FALLBACK_URL", FEDWATCH_FALLBACK_URL)
    try:
        response = requests.get(fallback_url, timeout=20, headers={"User-Agent": "market-report/1.0"})
        response.raise_for_status()
        fallback = parse_fedwatch_html(response.text, fallback_url)
        if fallback["meetings"]:
            fallback["next_fomc"] = next_fomc_date()
            return fallback
    except (requests.RequestException, ValueError, TypeError) as error:
        last_error = error
    return {"available": False, "source": url, "meetings": [], "error": str(last_error)}


def parse_fedwatch_html(document: str, source: str) -> dict[str, Any]:
    probability_match = re.search(r'"text":(\[[^\]]+\]),"textposition"', document)
    label_match = re.search(r'"x":(\[[^\]]+\]),"y":', document)
    y_match = re.search(r'"y":\{"dtype":"f8","bdata":"([^"]+)"', document)
    as_of_match = re.search(r"202\d-\d{2}-\d{2}", document)
    if not probability_match or not label_match or not y_match:
        return {"available": False, "source": source, "meetings": []}
    import base64
    import struct
    probabilities = list(struct.unpack(f"{len(base64.b64decode(y_match.group(1))) // 8}d", base64.b64decode(y_match.group(1))))
    labels = json.loads(label_match.group(1))
    return {
        "available": True,
        "source": source,
        "as_of": as_of_match.group(0) if as_of_match else None,
        "meetings": [{"outcome": label.replace("<br>", " "), "probability_pct": probability} for label, probability in zip(labels, probabilities)],
    }


def next_fomc_date() -> str | None:
    try:
        html = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=20).text
        dates = sorted({datetime.strptime(match, "%Y%m%d").date() for match in re.findall(r"(20\d{2})(0[1-9]|1[0-2])([0-3]\d)", html) if len(match) == 3 for match in ["".join(match)]})
        future = [value for value in dates if value >= datetime.now(NY_TZ).date()]
        if future:
            return future[0].isoformat()
        published_schedule = [date(2026, 10, 28), date(2026, 12, 9), date(2027, 1, 27), date(2027, 3, 17)]
        future = [value for value in published_schedule if value >= datetime.now(NY_TZ).date()]
        return future[0].isoformat() if future else None
    except (requests.RequestException, ValueError):
        return None


def news() -> list[dict[str, str]]:
    feed = feedparser.parse("https://news.google.com/rss/search?q=(semiconductor+OR+AI+OR+tariff+OR+geopolitics)+when:1d&hl=en-US&gl=US&ceid=US:en")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    impact_terms = ("fed", "fomc", "rate", "yield", "inflation", "tariff", "sanction", "chip", "semiconductor", "ai", "nvidia", "earnings", "guidance", "nasdaq", "s&p", "market")
    results = []
    for item in feed.entries:
        published = item.get("published_parsed") or item.get("updated_parsed")
        published_at = datetime(*published[:6], tzinfo=timezone.utc) if published else None
        title = item.get("title", "")
        if published_at and published_at < cutoff:
            continue
        if not any(term in title.lower() for term in impact_terms):
            continue
        results.append({"title": title, "link": item.get("link", ""), "published_at": published_at.isoformat() if published_at else None})
        if len(results) == 15:
            break
    return results


def fred_series(series_id: str) -> float | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        data = pd.read_csv(url).replace(".", pd.NA).dropna()
        return clean_number(data.iloc[-1, 1])
    except (requests.RequestException, ValueError, IndexError):
        return None


def fred_observation(series_id: str) -> dict[str, Any]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        data = pd.read_csv(url).replace(".", pd.NA).dropna()
        return {"value": clean_number(data.iloc[-1, 1]), "as_of": f"{data.iloc[-1, 0]}T00:00:00+00:00"}
    except (requests.RequestException, ValueError, IndexError):
        return {"value": None, "as_of": None}


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
            chart_data = prepare_chart_data(data, interval)
            csv_name = f"{symbol.lower()}-{interval.replace('m', 'min').replace('h', 'hour')}-ohlcv.csv"
            chart_data[["Open", "High", "Low", "Close", "Volume"]].to_csv(output / csv_name, index_label="timestamp")
            chart_files.append({"symbol": symbol, "interval": interval, "file": save_chart(symbol, interval, chart_data, output)})
            latest = chart_data.iloc[-1]
            profile = volume_profile(chart_data)
            chart_stats.append({"symbol": symbol, "interval": interval, "visible_bars": len(chart_data), "latest_timestamp": chart_data.index[-1].isoformat(), "close": clean_number(latest["Close"]), "rsi14": clean_number(latest["RSI14"]), "vwap": clean_number(latest["VWAP"]), "poc": profile["poc"], "vah": profile["vah"], "val": profile["val"], "session_profiles": session_profiles(chart_data), "context": trading_context(data), "ohlcv_csv": csv_name, "ema20": clean_number(latest["EMA20"]), "ema50": clean_number(latest["EMA50"]), "ema200": clean_number(latest["EMA200"])})
    market_quotes = {name: asdict(quote(symbol)) for name, symbol in MARKET.items()}
    dgs2 = fred_observation("DGS2")
    market_quotes["미국채 2년 금리"] = {"symbol": "DGS2", "price": dgs2["value"], "change_pct": None, "as_of": dgs2["as_of"]}
    semiconductor_quotes = {name: asdict(quote(symbol)) for name, symbol in SEMIS.items()}
    options_data = [options(symbol) for symbol in ("SOXX", "NVDA", "AMD", "AVGO", "MU", "TSM")]
    dgs10 = fred_observation("DGS10")
    payload = {"generated_at": generated_at.isoformat(), "charts": chart_stats, "chart_files": chart_files, "market": market_quotes, "semiconductors": semiconductor_quotes, "options": options_data, "economic_calendar": economic_calendar(), "fed_probabilities": fed_probabilities(), "news": news(), "macro": {"DGS2": dgs2, "DGS10": dgs10}, "data_notes": ["1분봉은 Yahoo Finance 제공 제한 때문에 최근 7일만 수집합니다.", "옵션 감마는 유효한 bid/ask·IV·미결제약정과 현물가 ±20% 범위에서 계산한 추정치입니다."]}
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
    lines += ["", "## 지표 요약", "| 종목 | 주기 | 최신 시각 | 종가 | RSI(14) | EMA20 | EMA50 | EMA200 | VWAP | POC | VAH | VAL | Gap | ATR14 |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for item in payload["charts"]:
        context = item.get("context", {})
        lines.append("| {symbol} | {interval} | {latest_timestamp} | {close:.2f} | {rsi14:.2f} | {ema20:.2f} | {ema50:.2f} | {ema200:.2f} | {vwap:.2f} | {poc:.2f} | {vah:.2f} | {val:.2f} | {gap:.2f}% | {atr:.2f} |".format(gap=context.get("gap_pct") if context.get("gap_pct") is not None else float("nan"), atr=context.get("atr14") if context.get("atr14") is not None else float("nan"), **{key: (value if value is not None else float("nan")) for key, value in item.items()}))
    lines += ["", "## 옵션 Gamma", "| 종목 | 기준 | 만기 | 수집 시각 | GEX | Call Wall | Put Wall | Gamma Flip |", "|---|---|---|---|---:|---:|---:|---:|"]
    for item in payload["options"]:
        for category, details in item.get("categories", {}).items():
            walls = details.get("walls", {})
            lines.append(f"| {item['symbol']} | {category} | {details.get('expiry', 'N/A')} | {details.get('data_as_of', 'N/A')} | {format_value(walls.get('totalGammaExposure'))} | {format_value(walls.get('callWall'))} | {format_value(walls.get('putWall'))} | {format_value(walls.get('gammaFlip'))} |")
            for row in details.get("rows", []):
                lines.append(f"| {item['symbol']} {category} | {details.get('expiry', 'N/A')} | {row.get('type', '')} | {row.get('strike', '')} | {row.get('openInterest', 0)} | {row.get('volume', 0)} | {format_value(row.get('gamma'))} | {format_value(row.get('gammaExposure'))} |")
    calendar = payload["economic_calendar"]
    lines += ["", "## 당일 경제지표", "| 시각 | 지표 | 중요도 | 실제 | 예상 | 이전 |", "|---|---|---|---:|---:|---:|"]
    for event in calendar.get("events", []):
        lines.append(f"| {event.get('date', '')} | {event.get('title', '')} | {event.get('impact', '')} | {event.get('actual', '') or 'N/A'} | {event.get('forecast', '') or 'N/A'} | {event.get('previous', '') or 'N/A'} |")
    if not calendar.get("events"):
        lines.append("| - | 당일 미국 경제지표 없음 또는 데이터 미수신 | - | N/A | N/A | N/A |")
    fed = payload["fed_probabilities"]
    lines += ["", "## Fed 금리확률"]
    if fed.get("meetings"):
        lines.append(f"- 다음 미래 FOMC: {fed.get('next_fomc', 'N/A')} · 데이터 시각: {fed.get('as_of', 'N/A')} · 출처: {fed.get('source', 'N/A')}")
        for meeting in fed["meetings"]:
            if "probability_pct" in meeting:
                lines.append(f"- {meeting.get('outcome', 'N/A')}: {meeting['probability_pct']:.1f}%")
            else:
                lines.append(f"- {meeting}")
    else:
        lines.append("- N/A: FedWatch 데이터가 제공되지 않았습니다.")
    lines += ["", "## 매크로 참고", f"- FRED 2년물: {format_value(payload['macro']['DGS2']['value'], '%')} (시각 {payload['macro']['DGS2']['as_of']})", f"- FRED 10년물: {format_value(payload['macro']['DGS10']['value'], '%')} (시각 {payload['macro']['DGS10']['as_of']})", "- 경제지표 실제값/예상값은 공개 캘린더, Fed 금리확률은 FedWatch 데이터 응답이 제공하는 범위만 표시합니다.", "", "## 주요 뉴스"]
    lines += [f"- [{item['title']}]({item['link']}) ({item.get('published_at', 'timestamp N/A')})" for item in payload["news"]]
    lines += ["", "## 첨부 차트", *[f"- `{item['file']}`" for item in payload["chart_files"]], "", "> 이 파일은 정보 제공용 자동 수집물이며 투자 조언이 아닙니다."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a daily market brief")
    parser.add_argument("--output", default=os.getenv("REPORT_DIR", "reports"), help="output directory")
    args = parser.parse_args()
    print(build_report(Path(args.output)))


if __name__ == "__main__":
    main()
