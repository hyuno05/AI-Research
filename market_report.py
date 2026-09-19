#!/usr/bin/env python3
"""Generate a daily semiconductor and macro market brief.

The report is deliberately file-first: it writes Markdown, JSON, CSV and PNG
files so it can be attached to ChatGPT or delivered by a separate messenger job.
"""

from __future__ import annotations

import argparse
import html
import io
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
    "SMH": ["5m"],
}
VISIBLE_BARS = {"1m": 390, "5m": 240, "30m": 160, "1h": 120, "1d": 120}
NY_TZ = ZoneInfo("America/New_York")
PREMARKET_START = "04:00"
PREMARKET_END = "09:30"
RTH_START = "09:30"
RTH_END = "16:00"
FEDWATCH_FALLBACK_URL = "https://amazingmazy.github.io/fedwatch-monitor/_static/amazingmazy--fedwatch_monitor/fedwatch_latest_forecast.html"
MARKET = {
    "NQ 선물": "NQ=F",
    "ES 선물": "ES=F",
    "VIX": "^VIX",
    "WTI": "CL=F",
    "미국채 10년 금리": "^TNX",
    "DXY": "DX-Y.NYB",
}
SEMIS = {"SOXX": "SOXX", "NVDA": "NVDA", "QQQ": "QQQ", "AMD": "AMD", "AVGO": "AVGO", "MU": "MU", "TSM": "TSM", "SMH": "SMH"}
SOXX_HOLDINGS_URL = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/latest-holdings.csv"


@dataclass
class Quote:
    symbol: str
    price: float | None
    change_pct: float | None
    as_of: str | None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    quality: str = "MISSING"


def clean_number(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def download(symbol: str, interval: str) -> pd.DataFrame:
    polygon_data = download_polygon(symbol, interval)
    if polygon_data is not None and not polygon_data.empty:
        polygon_data.attrs["source"] = "Polygon.io aggregates"
        return polygon_data
    period = "7d" if interval == "1m" else "60d" if interval in {"5m", "15m", "30m", "1h"} else "3y"
    data = yf.download(symbol, period=period, interval=interval, auto_adjust=False, prepost=True, progress=False)
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    if data.index.tz is None:
        data.index = data.index.tz_localize(timezone.utc)
    data.attrs["source"] = "Yahoo Finance/yfinance (fallback; extended-hours volume may be incomplete)"
    return clean_ohlcv(data)


def download_polygon(symbol: str, interval: str) -> pd.DataFrame | None:
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key or interval == "1d":
        return None
    multiplier, timespan = {"1m": (1, "minute"), "5m": (5, "minute"), "30m": (30, "minute"), "1h": (1, "hour")}.get(interval, (None, None))
    if multiplier is None:
        return None
    end = datetime.now(NY_TZ).date()
    start = end - timedelta(days=8 if interval == "1m" else 70)
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{start}/{end}"
    try:
        response = requests.get(url, params={"adjusted": "false", "sort": "asc", "limit": 50000, "apiKey": api_key}, timeout=30)
        response.raise_for_status()
        rows = response.json().get("results", [])
        if not rows:
            return None
        data = pd.DataFrame(rows).rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        data.index = pd.to_datetime(data["t"], unit="ms", utc=True)
        return clean_ohlcv(data[["Open", "High", "Low", "Close", "Volume"]])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def clean_ohlcv(data: pd.DataFrame) -> pd.DataFrame:
    data = data.sort_index().copy()
    numeric = ["Open", "High", "Low", "Close", "Volume"]
    for column in numeric:
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["Open", "High", "Low", "Close"])
    valid = (data["High"] >= data[["Open", "Close"]].max(axis=1)) & (data["Low"] <= data[["Open", "Close"]].min(axis=1))
    returns = data["Close"].pct_change().abs()
    median_return = returns.rolling(50, min_periods=10).median()
    outlier = returns > (median_return * 12).clip(lower=0.08)
    if not outlier.empty:
        outlier.iloc[-1] = False
    data.loc[~valid | outlier, ["Open", "High", "Low", "Close", "Volume"]] = pd.NA
    return data.dropna(subset=["Open", "High", "Low", "Close"])


def market_session(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.astimezone(timezone.utc).weekday() >= 5:
        return "CLOSED"
    local = instant.astimezone(NY_TZ)
    if not is_market_day(local.date()):
        return "CLOSED"
    clock = local.strftime("%H:%M")
    if clock < PREMARKET_START:
        return "CLOSED"
    if clock < PREMARKET_END:
        return "PREMARKET"
    if clock < RTH_END:
        return "RTH"
    return "AFTER_HOURS"


def previous_market_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not is_market_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def is_market_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    fixed = {(1, 1), (6, 19), (7, 4), (12, 25)}
    if (day.month, day.day) in fixed:
        return False
    if day.month == 1 and day.weekday() == 0 and 15 <= day.day <= 21:
        return False
    if day.month == 2 and day.weekday() == 0 and 15 <= day.day <= 21:
        return False
    if day.month == 5 and day.weekday() == 0 and day.day >= 25:
        return False
    if day.month == 9 and day.weekday() == 0 and day.day <= 7:
        return False
    if day.month == 11 and day.weekday() == 3 and 22 <= day.day <= 28:
        return False
    if day.month == 12 and day.weekday() == 0 and day.day >= 25:
        return False
    # Anonymous Gregorian Easter calculation, used only for Good Friday.
    year = day.year
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    month = (a + 11 * h + 22 * l) // 451
    easter = date(year, (h + l - 7 * month + 114) // 31, (h + l - 7 * month + 114) % 31 + 1)
    return day != easter - timedelta(days=2)


def latest_completed_trading_day(now: datetime | None = None) -> date:
    local = (now or datetime.now(timezone.utc)).astimezone(NY_TZ)
    if local.weekday() < 5 and local.strftime("%H:%M") >= RTH_END:
        return local.date()
    return previous_market_day(local.date())


def localize_index(data: pd.DataFrame) -> pd.DatetimeIndex:
    return data.index.tz_convert(NY_TZ) if getattr(data.index, "tz", None) else data.index.tz_localize(NY_TZ)


def data_trading_day(data: pd.DataFrame) -> date:
    latest = data.index[-1]
    if latest.tzinfo is not None and latest.hour == 0 and latest.minute == 0:
        return latest.date()
    return localize_index(data)[-1].date()


def data_quality(data: pd.DataFrame, required_bars: int, expected_day: date | None = None) -> dict[str, Any]:
    if data.empty:
        return {"status": "MISSING", "source": "Yahoo Finance/yfinance", "as_of": None, "bars": 0}
    as_of = data.index[-1].isoformat()
    age_minutes = (datetime.now(timezone.utc) - data.index[-1].to_pydatetime().astimezone(timezone.utc)).total_seconds() / 60
    latest_day = data_trading_day(data)
    date_mismatch = expected_day is not None and latest_day < expected_day
    live_stale = market_session() not in {"CLOSED", "AFTER_HOURS"} and age_minutes > 30
    status = "STALE" if date_mismatch or live_stale else "OK" if len(data) >= required_bars else "LOW QUALITY"
    result = {"status": status, "source": data.attrs.get("source", "Yahoo Finance/yfinance"), "as_of": as_of, "bars": len(data), "required_bars": required_bars, "age_minutes": round(age_minutes, 1), "latest_trading_day": latest_day.isoformat()}
    if date_mismatch:
        result["reason"] = "DATE_MISMATCH"
    return result


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
    session = pd.Series(index.map(session_name), index=data.index)
    trading_dates = pd.Series(index.date, index=data.index)
    session_key = pd.Series(list(zip(trading_dates, session)), index=data.index)
    volume = pd.to_numeric(data["Volume"], errors="coerce").fillna(0)
    cumulative_volume = volume.groupby(session_key).cumsum().replace(0, pd.NA)
    data["VWAP"] = (typical * volume).groupby(session_key).cumsum() / cumulative_volume
    for name in ("PREMARKET", "RTH", "OVERNIGHT"):
        mask = session == name
        grouped_volume = volume.where(mask, 0).groupby(trading_dates).cumsum().replace(0, pd.NA)
        grouped_value = ((typical * volume).where(mask, 0).groupby(trading_dates).cumsum())
        data[f"VWAP_{name}"] = (grouped_value / grouped_volume).where(mask)
    return data


def session_name(timestamp: Any) -> str:
    local_time = timestamp.timetz().replace(tzinfo=None)
    if PREMARKET_START <= local_time.strftime("%H:%M") < PREMARKET_END:
        return "PREMARKET"
    if "09:30" <= local_time.strftime("%H:%M") < "16:00":
        return "RTH"
    return "OVERNIGHT"


def session_vwap(data: pd.DataFrame, trading_date: date, session: str) -> dict[str, Any]:
    local = data.copy()
    local.index = localize_index(data)
    names = pd.Series(local.index.map(session_name), index=local.index)
    sliced = local[(local.index.date == trading_date) & (names == session)]
    if sliced.empty:
        return {"value": None, "as_of": None, "session_low": None, "session_high": None, "status": "MISSING"}
    typical = (sliced["High"] + sliced["Low"] + sliced["Close"]) / 3
    volume = pd.to_numeric(sliced["Volume"], errors="coerce").fillna(0)
    total_volume = volume.sum()
    if total_volume <= 0:
        return {"value": None, "as_of": sliced.index[-1].isoformat(), "session_low": clean_number(sliced["Low"].min()), "session_high": clean_number(sliced["High"].max()), "status": "LOW QUALITY"}
    value = clean_number((typical * volume).sum() / total_volume)
    low = clean_number(sliced["Low"].min())
    high = clean_number(sliced["High"].max())
    valid = value is not None and low is not None and high is not None and low <= value <= high
    return {"value": value if valid else None, "as_of": sliced.index[-1].isoformat(), "session_low": low, "session_high": high, "status": "OK" if valid else "INVALID"}


def volume_profile(data: pd.DataFrame, bins: int = 30) -> dict[str, float | None]:
    """Return POC and 70% value area from the visible OHLCV range."""
    prices = (pd.to_numeric(data["High"], errors="coerce") + pd.to_numeric(data["Low"], errors="coerce") + pd.to_numeric(data["Close"], errors="coerce")) / 3
    volumes = pd.to_numeric(data["Volume"], errors="coerce").fillna(0)
    valid = pd.DataFrame({"price": prices, "volume": volumes}).dropna()
    valid = valid[valid["volume"] > 0]
    if valid.empty:
        return {"poc": None, "vah": None, "val": None, "status": "MISSING"}
    if valid.empty or valid["price"].nunique() < 2:
        price = clean_number(valid["price"].iloc[-1]) if not valid.empty else None
        return {"poc": price, "vah": price, "val": price, "status": "OK"}
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
    vah = clean_number(max(centers.index[position] for position in selected))
    val = clean_number(min(centers.index[position] for position in selected))
    low = clean_number(valid["price"].min())
    high = clean_number(valid["price"].max())
    valid_range = low is not None and high is not None and low <= val <= poc <= vah <= high
    return {"poc": poc, "vah": vah, "val": val, "session_low": low, "session_high": high, "status": "OK" if valid_range else "PROFILE_INVALID"}


def volume_profile_poc(data: pd.DataFrame, bins: int = 30) -> float | None:
    return volume_profile(data, bins)["poc"]


def session_profiles(data: pd.DataFrame, trading_date: date | None = None) -> dict[str, Any]:
    index = localize_index(data)
    sessions = pd.Series(index.date, index=data.index)
    names = pd.Series(index.map(session_name), index=data.index)
    target = trading_date or latest_completed_trading_day()
    pm = data.loc[(sessions == target) & (names == "PREMARKET")]
    rth = data.loc[(sessions == target) & (names == "RTH")]
    days = sorted(set(sessions[names == "RTH"]))
    five = data.loc[sessions.isin(days[-5:]) & (names == "RTH")]
    twenty = data.loc[sessions.isin(days[-20:]) & (names == "RTH")]
    profiles = {"PM": volume_profile(pm), "RTH": volume_profile(rth), "5RTH": volume_profile(five), "20RTH": volume_profile(twenty)}
    profiles["PM"]["vwap"] = session_vwap(data, target, "PREMARKET")
    profiles["RTH"]["vwap"] = session_vwap(data, target, "RTH")
    for profile in profiles.values():
        if profile.get("status") == "PROFILE_INVALID":
            profile["value"] = None
    return {**profiles, "trading_date": target.isoformat(), "volume_bars": {"PM": int((pm["Volume"] > 0).sum()), "RTH": int((rth["Volume"] > 0).sum()), "5RTH": int((five["Volume"] > 0).sum()), "20RTH": int((twenty["Volume"] > 0).sum())}}


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
    if interval != "1d":
        price_axis.plot(data.index, pd.to_numeric(data["VWAP"], errors="coerce"), label="VWAP", color="#7b2cbf", linewidth=1)
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


def quote(symbol: str, canonical_previous_close: float | None = None) -> Quote:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="5d", interval="1m", auto_adjust=False, prepost=True)
    if history.empty:
        return Quote(symbol, None, None, None)
    current = clean_number(history["Close"].iloc[-1])
    previous = canonical_previous_close if canonical_previous_close is not None else clean_number(history["Close"].iloc[-2]) if len(history) > 1 else None
    change = (current / previous - 1) * 100 if current is not None and previous else None
    info = getattr(ticker, "fast_info", {})
    bid = clean_number(info.get("bid"))
    ask = clean_number(info.get("ask"))
    spread = ask - bid if bid is not None and ask is not None and ask >= bid else None
    spread_pct = spread / ((ask + bid) / 2) * 100 if spread is not None and bid and ask else None
    age_minutes = (datetime.now(timezone.utc) - history.index[-1].to_pydatetime().astimezone(timezone.utc)).total_seconds() / 60
    quality = "OK" if age_minutes <= 30 else "STALE"
    return Quote(symbol, current, change, history.index[-1].isoformat(), bid, ask, spread, spread_pct, quality)


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


def opening_ranges(data: pd.DataFrame, trading_date: date) -> tuple[dict[str, Any], dict[str, Any]]:
    local = data.copy()
    local.index = localize_index(data)
    day = local[local.index.date == trading_date]
    def range_for(end_time: str) -> dict[str, Any]:
        sliced = day[(day.index.strftime("%H:%M:%S") >= "09:30:00") & (day.index.strftime("%H:%M:%S") < end_time)]
        result = ohlc_summary(sliced)
        result["trading_date"] = trading_date.isoformat()
        result["status"] = "MISSING" if sliced.empty else "OK"
        if not sliced.empty and result["as_of"] > f"{trading_date.isoformat()}T{end_time}-04:00":
            result["status"] = "INVALID"
        return result
    return range_for("09:35:00"), range_for("09:45:00")


def trading_context(data: pd.DataFrame, canonical_previous_close: float | dict[str, Any] | None = None, trading_date: date | None = None, opening_data: pd.DataFrame | None = None) -> dict[str, Any]:
    if data.empty:
        return {}
    local_index = data.index.tz_convert(NY_TZ) if getattr(data.index, "tz", None) else data.index.tz_localize(NY_TZ)
    local = data.copy()
    local.index = local_index
    today = trading_date or latest_completed_trading_day()
    regular = local.between_time("09:30", "16:00")
    days = sorted(set(regular.index.date))
    if not days:
        empty = ohlc_summary(pd.DataFrame())
        canonical = canonical_previous_close if isinstance(canonical_previous_close, dict) else {"date": previous_market_day(today), "close": canonical_previous_close}
        return {"market_session": market_session(), "latest_completed_session": today.isoformat(), "previous_day": empty, "previous_week": empty, "premarket": empty, "overnight": empty, "previous_rth": {"date": canonical.get("date"), "close": canonical.get("close"), "as_of": None, "source": "canonical daily RTH close", "status": "OK" if canonical.get("close") is not None else "INVALID_PREVIOUS_CLOSE"}, "current_rth": empty, "after_hours": empty, "current_session": market_session(), "reference_session_date": None, "reference_price": None, "reference_price_timestamp": None, "previous_rth_date": canonical.get("date"), "previous_rth_close": canonical.get("close"), "gap_pct": None, "gap_status": "N/A", "gap_basis": "N/A", "or5": {**empty, "status": "MISSING"}, "or15": {**empty, "status": "MISSING"}, "atr14": atr14(data), "rvol": None}
    previous_day = previous_market_day(today)
    previous = regular[regular.index.date == previous_day]
    today_regular = regular[regular.index.date == today]
    prem = local[(local.index.date == today) & (local.index.strftime("%H:%M") >= PREMARKET_START) & (local.index.strftime("%H:%M") < PREMARKET_END)]
    current_open = clean_number(today_regular["Open"].iloc[0]) if not today_regular.empty else (clean_number(prem["Open"].iloc[0]) if not prem.empty else None)
    canonical = canonical_previous_close if isinstance(canonical_previous_close, dict) else {"date": previous_day, "close": canonical_previous_close}
    prior_close = canonical.get("close") if canonical.get("date") in {previous_day, previous_day.isoformat()} else None
    previous_days = sorted(set(regular.index.date))
    regular_days = pd.Series(regular.index.date, index=regular.index)
    previous_week = regular.loc[regular_days.isin(previous_days[-6:-1])]
    overnight = local[(local.index.date == today) & (local.index.strftime("%H:%M") >= "16:00")]
    today_rth = regular[regular.index.date == today]
    opening5, opening = opening_ranges(opening_data if opening_data is not None else data, today)
    prior_daily = [regular[regular.index.date == day]["Volume"].sum() for day in previous_days[-21:-1]]
    today_volume = today_rth["Volume"].sum() if not today_rth.empty else prem["Volume"].sum()
    current_premarket_price = clean_number(prem["Close"].iloc[-1]) if not prem.empty else None
    gap_value = current_premarket_price if current_premarket_price is not None else current_open
    current_session = market_session()
    gap_basis = "latest premarket price" if current_premarket_price is not None else "premarket open" if not prem.empty and current_open is not None else "current RTH open" if not today_regular.empty else "N/A"
    reference_timestamp = prem.index[-1].isoformat() if not prem.empty else today_regular.index[0].isoformat() if not today_regular.empty else None
    gap_valid = gap_value is not None and prior_close is not None and canonical.get("date") in {previous_day, previous_day.isoformat()}
    return {"market_session": current_session, "latest_completed_session": today.isoformat(), "previous_day": ohlc_summary(previous), "previous_week": ohlc_summary(previous_week), "premarket": ohlc_summary(prem), "overnight": ohlc_summary(overnight), "previous_rth": {"date": canonical.get("date"), "close": prior_close, "as_of": previous.index[-1].isoformat() if not previous.empty else None, "source": "canonical daily RTH close", "status": "OK" if prior_close is not None else "INVALID_PREVIOUS_CLOSE"}, "current_rth": ohlc_summary(today_regular), "after_hours": ohlc_summary(overnight), "current_session": current_session, "reference_session_date": today.isoformat() if gap_value is not None else None, "reference_price": gap_value, "reference_price_timestamp": reference_timestamp, "previous_rth_date": canonical.get("date"), "previous_rth_close": prior_close, "gap_pct": (gap_value / prior_close - 1) * 100 if gap_valid else None, "gap_status": "OK" if gap_valid else "N/A", "gap_basis": gap_basis if gap_valid else "N/A", "or5": opening5, "or15": opening, "atr14": atr14(data), "rvol": clean_number(today_volume / (sum(prior_daily) / len(prior_daily))) if prior_daily and sum(prior_daily) else None}


def canonical_rth_close(symbol: str) -> dict[str, Any]:
    try:
        data = yf.download(symbol, period="10d", interval="1d", auto_adjust=False, progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        if data.empty:
            return {"symbol": symbol, "date": None, "close": None, "status": "MISSING", "source": "Yahoo Finance/yfinance daily RTH"}
        local_dates = pd.DatetimeIndex(data.index).date
        today = datetime.now(NY_TZ).date()
        target = previous_market_day(latest_completed_trading_day())
        eligible = data.loc[[value <= target for value in local_dates]]
        if eligible.empty:
            return {"symbol": symbol, "date": None, "close": None, "status": "MISSING", "source": "Yahoo Finance/yfinance daily RTH"}
        row_date = local_dates[list(data.index).index(eligible.index[-1])]
        return {"symbol": symbol, "date": row_date.isoformat(), "close": clean_number(eligible["Close"].iloc[-1]), "status": "OK", "source": "Yahoo Finance/yfinance daily RTH"}
    except Exception:
        return {"symbol": symbol, "date": None, "close": None, "status": "MISSING", "source": "Yahoo Finance/yfinance daily RTH"}


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
            coverage = int(rows["strike"].nunique()) if not rows.empty else 0
            side_coverage = rows.groupby("type")["strike"].nunique().to_dict() if not rows.empty else {}
            category_quality = "OK" if coverage >= 8 and side_coverage.get("call", 0) >= 4 and side_coverage.get("put", 0) >= 4 else "LOW QUALITY"
            if rows.empty:
                result_categories[category] = {"expiry": expiry, "data_as_of": captured_at, "quality": "LOW QUALITY", "strike_coverage": 0, "rows": [], "walls": gamma_walls(rows)}
                continue
            days_to_expiry = max((expiry_date - today).days, 0)
            rows["gamma"] = rows.apply(lambda row: option_gamma(spot, row["strike"], row["impliedVolatility"], days_to_expiry), axis=1)
            rows["gammaExposure"] = rows["gamma"] * rows["openInterest"] * 100 * spot * spot * 0.01
            rows.loc[rows["type"] == "put", "gammaExposure"] *= -1
            walls = gamma_walls(rows)
            top = rows.sort_values(["openInterest", "volume"], ascending=False).head(12)
            result_categories[category] = {"expiry": expiry, "data_as_of": captured_at, "quality": category_quality, "strike_coverage": coverage, "rows": top.to_dict("records"), "walls": walls if category_quality == "OK" else gamma_walls(pd.DataFrame())}
        return {"symbol": symbol, "available": bool(result_categories), "quality": "OK" if any(item.get("quality") == "OK" for item in result_categories.values()) else "LOW QUALITY", "spot": spot, "categories": result_categories, "data_as_of": captured_at}
    except Exception as error:  # Yahoo options can be unavailable outside market hours.
        return {"symbol": symbol, "available": False, "error": str(error)}


def valid_option_rows(rows: pd.DataFrame, spot: float | None) -> pd.DataFrame:
    columns = ["strike", "bid", "ask", "impliedVolatility", "openInterest", "volume"]
    for column in columns:
        rows[column] = pd.to_numeric(rows.get(column), errors="coerce")
    rows = rows.dropna(subset=["strike", "bid", "ask", "impliedVolatility", "openInterest"])
    rows = rows[(rows["bid"] > 0) & (rows["ask"] > rows["bid"]) & (rows["impliedVolatility"].between(0.01, 5.0)) & (rows["openInterest"] >= 5) & (rows["volume"].fillna(0) >= 1)]
    if "lastTradeDate" in rows:
        traded_at = pd.to_datetime(rows["lastTradeDate"], utc=True, errors="coerce")
        age_hours = (pd.Timestamp.now(tz="UTC") - traded_at).dt.total_seconds() / 3600
        rows = rows[age_hours <= 48]
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
                    row = {key: event.get(key, "") for key in ("title", "date", "impact", "actual", "forecast", "previous")}
                    row["importance"] = row.pop("impact")
                    row["surprise"] = numeric_surprise(row.get("actual"), row.get("forecast"))
                    rows.append(row)
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
            futures = quote("ZQ=F")
            return {"available": True, "quality": "LOW CONFIDENCE" if futures.price is None else "LOW CONFIDENCE", "cross_validation": {"source": "CME Fed Funds Futures proxy (ZQ=F)", "price": futures.price, "implied_rate_pct": 100 - futures.price if futures.price is not None else None, "as_of": futures.as_of, "status": "AVAILABLE" if futures.price is not None else "MISSING"}, "source": url, "as_of": datetime.now(timezone.utc).isoformat(), "next_fomc": next_fomc_date(), "meetings": meetings if isinstance(meetings, list) else []}
        except (requests.RequestException, ValueError, TypeError) as error:
            last_error = error
    fallback_url = os.getenv("FEDWATCH_FALLBACK_URL", FEDWATCH_FALLBACK_URL)
    try:
        response = requests.get(fallback_url, timeout=20, headers={"User-Agent": "market-report/1.0"})
        response.raise_for_status()
        fallback = parse_fedwatch_html(response.text, fallback_url)
        if fallback["meetings"]:
            fallback["next_fomc"] = next_fomc_date()
            fallback["quality"] = "LOW CONFIDENCE"
            fallback["cross_validation"] = "CME UNAVAILABLE; FED FUNDS FUTURES SOURCE ONLY"
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
    encoded_values = json.loads(f'"{y_match.group(1)}"')
    decoded_values = base64.b64decode(encoded_values)
    item_size = 8 if len(decoded_values) % 8 == 0 else 4
    value_format = "d" if item_size == 8 else "f"
    probabilities = list(struct.unpack(f"{len(decoded_values) // item_size}{value_format}", decoded_values[: len(decoded_values) // item_size * item_size]))
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


def numeric_surprise(actual: Any, forecast: Any) -> float | None:
    actual_number = clean_number(str(actual).replace("%", "").replace(",", ""))
    forecast_number = clean_number(str(forecast).replace("%", "").replace(",", ""))
    return actual_number - forecast_number if actual_number is not None and forecast_number is not None else None


def news() -> list[dict[str, str]]:
    feeds = ["Reuters semiconductor stocks", "Bloomberg markets semiconductor", "CNBC Fed market", "WSJ technology stocks", "site:sec.gov semiconductor 8-k", "semiconductor investor relations"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    impact_terms = ("fed", "fomc", "rate", "yield", "inflation", "tariff", "sanction", "chip", "semiconductor", "ai", "nvidia", "earnings", "guidance", "nasdaq", "s&p", "market")
    results = []
    seen = set()
    for query in feeds:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&when:2d&hl=en-US&gl=US&ceid=US:en")
        for item in feed.entries:
            published = item.get("published_parsed") or item.get("updated_parsed")
            published_at = datetime(*published[:6], tzinfo=timezone.utc) if published else None
            title = item.get("title", "")
            key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            if key in seen or (published_at and published_at < cutoff) or not any(term in title.lower() for term in impact_terms):
                continue
            seen.add(key)
            raw_summary = re.sub(r"<[^>]+>", " ", item.get("summary", ""))
            factual = re.sub(r"\s+", " ", html.unescape(raw_summary)).strip()[:420]
            summary = f"{factual}." if factual and not factual.endswith(".") else factual
            if summary:
                summary += " 관련 수급·정책·실적 경로를 통해 반도체 및 지수 변동성에 영향을 줄 수 있습니다."
            else:
                summary = "원문 요약이 제공되지 않아 제목 외 사실관계는 확인하지 못했습니다. 관련 종목의 가격·거래량 반응을 함께 확인해야 합니다."
            paths = []
            for term, path in (("fed", "금리·할인율"), ("rate", "금리·할인율"), ("yield", "채권금리"), ("inflation", "인플레이션·정책"), ("tariff", "관세·공급망"), ("chip", "반도체 공급망"), ("semiconductor", "반도체 공급망"), ("earnings", "실적·가이던스"), ("ai", "AI 수요")):
                if term in title.lower() and path not in paths:
                    paths.append(path)
            impact_path = " → ".join(paths)
            results.append({"title": title, "summary": summary, "impact_path": impact_path or "시장 심리", "link": item.get("link", ""), "source": item.get("source", {}).get("title", query) if isinstance(item.get("source"), dict) else query, "published_at": published_at.isoformat() if published_at else None, "related_tickers": sorted(set(re.findall(r"\b(?:SOXX|SMH|QQQ|NVDA|AMD|AVGO|MU|TSM|SPY|ES|NQ)\b", title.upper())))})
            if len(results) == 15:
                return results
    return results


def semiconductor_events() -> list[dict[str, Any]]:
    events = []
    cutoff = datetime.now(timezone.utc)
    horizon = cutoff + timedelta(days=7)
    symbols = ("NVDA", "AMD", "AVGO", "MU", "TSM")
    try:
        holdings = soxx_constituents().get("rows", [])
        symbols += tuple(row["symbol"] for row in holdings[:30] if row["symbol"] not in symbols)
    except Exception:
        pass
    for symbol in symbols:
        try:
            dates = yf.Ticker(symbol).get_earnings_dates(limit=8)
            for timestamp in dates.index:
                stamp = timestamp.to_pydatetime()
                if cutoff <= stamp.astimezone(timezone.utc) <= horizon:
                    events.append({"symbol": symbol, "event": "earnings", "event_type": "earnings", "as_of": stamp.isoformat(), "source": "Yahoo Finance/yfinance"})
        except Exception:
            continue
    return events


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
        as_of = f"{data.iloc[-1, 0]}T00:00:00+00:00"
        age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(as_of)).total_seconds() / 3600
        return {"value": clean_number(data.iloc[-1, 1]), "as_of": as_of, "source": "FRED", "quality": "OK" if age_hours <= 30 else "STALE"}
    except (requests.RequestException, ValueError, IndexError):
        return {"value": None, "as_of": None, "source": "FRED", "quality": "MISSING"}


def soxx_constituents() -> dict[str, Any]:
    url = os.getenv("SOXX_HOLDINGS_URL", SOXX_HOLDINGS_URL)
    try:
        response = requests.get(url, headers={"User-Agent": "market-report/1.0", "Accept": "text/csv"}, timeout=30)
        response.raise_for_status()
        if response.content.lstrip().startswith(b"<"):
            raise ValueError("iShares returned HTML instead of holdings CSV")
        lines = response.text.splitlines()
        header_line = next((position for position, line in enumerate(lines) if "Ticker" in line and "Weight" in line), None)
        if header_line is None:
            raise ValueError("iShares holdings CSV header not found")
        data = pd.read_csv(io.StringIO("\n".join(lines[header_line:])))
        symbol_column = next((column for column in data.columns if str(column).lower() in {"ticker", "symbol"}), None)
        weight_column = next((column for column in data.columns if "weight" in str(column).lower()), None)
        if symbol_column is None or weight_column is None:
            raise ValueError("SOXX holdings columns not found")
        rows = data[[symbol_column, weight_column]].copy()
        rows.columns = ["symbol", "weight"]
        rows["weight"] = rows["weight"].astype(str).str.replace("%", "", regex=False).pipe(pd.to_numeric, errors="coerce") / 100
        rows = rows.dropna().query("weight > 0")
        rows["symbol"] = rows["symbol"].astype(str).str.strip()
        return {"available": True, "source": url, "as_of": datetime.now(timezone.utc).isoformat(), "rows": rows.head(30).to_dict("records")}
    except (requests.RequestException, ValueError, OSError, pd.errors.ParserError) as error:
        return {"available": False, "source": url, "as_of": None, "rows": [], "error": str(error)}


def breadth_and_relative_strength(holdings: dict[str, Any], quotes: dict[str, Any]) -> dict[str, Any]:
    rows = holdings.get("rows", [])
    covered = [row for row in rows if clean_number(quotes.get(row["symbol"], {}).get("change_pct")) is not None]
    up = sum(1 for row in covered if quotes[row["symbol"]]["change_pct"] > 0)
    down = sum(1 for row in covered if quotes[row["symbol"]]["change_pct"] < 0)
    unchanged = len(covered) - up - down
    weighted_up = sum(row["weight"] for row in covered if quotes[row["symbol"]]["change_pct"] > 0)
    weighted_down = sum(row["weight"] for row in covered if quotes[row["symbol"]]["change_pct"] < 0)
    contribution = sorted(({"symbol": row["symbol"], "weight": row["weight"], "change_pct": quotes[row["symbol"]]["change_pct"], "contribution_pct": row["weight"] * quotes[row["symbol"]]["change_pct"]} for row in covered), key=lambda row: abs(row["contribution_pct"]), reverse=True)[:10]
    return {"up_count": up, "down_count": down, "unchanged_count": unchanged, "covered_count": len(covered), "total_count": len(rows), "coverage_pct": round(len(covered) / len(rows) * 100, 2) if rows else None, "weighted_up_pct": round(weighted_up * 100, 4) if rows else None, "weighted_down_pct": round(weighted_down * 100, 4) if rows else None, "weighted_breadth": round((weighted_up - weighted_down) * 100, 4) if rows else None, "top_constituent_contribution": contribution, "quality": "OK" if covered and len(covered) / len(rows) >= 0.8 else "LOW QUALITY", "as_of": max((quotes[row["symbol"]].get("as_of") for row in covered), default=None), "source": "iShares holdings + quote provider"}


def relative_strength(data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    result = {}
    for left, right in (("SOXX", "QQQ"), ("SMH", "QQQ")):
        if left not in data or right not in data or data[left].empty or data[right].empty:
            result[f"{left}/{right}"] = None
            continue
        left_return = data[left]["Close"].iloc[-1] / data[left]["Close"].iloc[max(0, len(data[left]) - 20)] - 1
        right_return = data[right]["Close"].iloc[-1] / data[right]["Close"].iloc[max(0, len(data[right]) - 20)] - 1
        result[f"{left}/{right}"] = {"relative_return_pct": (left_return - right_return) * 100, "as_of": data[left].index[-1].isoformat()}
    return result


def format_value(value: float | None, suffix: str = "") -> str:
    return f"{value:,.2f}{suffix}" if value is not None else "N/A"


CORE_CHARTS = ("SOXX/1m", "SOXX/5m", "SOXX/30m", "SOXX/1h", "SOXX/1d", "NVDA/5m", "QQQ/5m", "SMH/5m")


def validate_report(payload: dict[str, Any]) -> dict[str, Any]:
    expected = payload["latest_completed_trading_day"]
    chart_map = {f"{item['symbol']}/{item['interval']}": item for item in payload.get("charts", [])}
    dates = {}
    for key in CORE_CHARTS:
        item = chart_map.get(key)
        if item is None:
            dates[key] = {"status": "MISSING", "latest": None}
            continue
        latest = item.get("latest_trading_day")
        status = "PASS" if latest == expected else "DATE_MISMATCH"
        dates[key] = {"status": status, "latest": latest}
    date_status = "PASS" if all(item["status"] == "PASS" for item in dates.values()) else "DATA_MISMATCH"
    canonical = payload.get("canonical_previous_rth", {})
    canonical_status = "PASS" if canonical.get("status") == "OK" and canonical.get("date") == payload.get("previous_rth_date") else "INVALID_PREVIOUS_CLOSE"
    gaps = [item.get("context", {}) for item in chart_map.values() if "gap_status" in item.get("context", {})]
    gap_status = "PASS" if all(context.get("gap_status") in {"OK", "N/A"} for context in gaps) else "INVALID"
    or_status = "PASS" if all(context.get("or5", {}).get("status") != "INVALID" and context.get("or15", {}).get("status") != "INVALID" for context in gaps) else "INVALID"
    profiles = [item.get("session_profiles", {}) for item in chart_map.values()]
    profile_status = "PASS" if not any(profile.get(session, {}).get("status") == "PROFILE_INVALID" for profile in profiles for session in ("PM", "RTH", "5RTH", "20RTH")) else "PROFILE_INVALID"
    vwap_status = "PASS" if not any(item.get("vwap_premarket_status") == "INVALID" or item.get("vwap_rth_status") == "INVALID" for item in chart_map.values()) else "INVALID"
    as_of_status = "PASS" if payload.get("breadth", {}).get("as_of") and payload.get("macro", {}).get("DGS10", {}).get("as_of") and any(item.get("data_as_of") for item in payload.get("options", [])) else "MISSING"
    statuses = {"date_sync": date_status, "market_session": "PASS" if payload.get("market_session") else "MISSING", "canonical_previous_rth": canonical_status, "gap": gap_status, "opening_range": or_status, "volume_profile": profile_status, "session_vwap": vwap_status, "breadth_options_macro_as_of": as_of_status}
    return {"status": "PASS" if all(value == "PASS" for value in statuses.values()) else "FAIL", "expected_latest_completed_trading_day": expected, "core_dates": dates, "checks": statuses}


def build_report(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    chart_files: list[dict[str, str]] = []
    chart_stats: list[dict[str, Any]] = []
    raw_data: dict[str, pd.DataFrame] = {}
    quality_table: list[dict[str, Any]] = []
    analysis_day = latest_completed_trading_day()
    canonical_closes = {symbol: canonical_rth_close(symbol) for symbol in CHARTS}
    opening_source = download("SOXX", "1m")
    for symbol, intervals in CHARTS.items():
        for interval in intervals:
            data = download(symbol, interval)
            if data.empty:
                quality_table.append({"item": f"{symbol}/{interval}", "status": "MISSING", "source": "Yahoo Finance/yfinance", "as_of": None})
                continue
            raw_data[f"{symbol}/{interval}"] = data
            chart_data = prepare_chart_data(data, interval)
            csv_name = f"{symbol.lower()}-{interval.replace('m', 'min').replace('h', 'hour')}-ohlcv.csv"
            chart_data[["Open", "High", "Low", "Close", "Volume"]].to_csv(output / csv_name, index_label="timestamp")
            chart_files.append({"symbol": symbol, "interval": interval, "file": save_chart(symbol, interval, chart_data, output)})
            latest = chart_data.iloc[-1]
            quality = data_quality(data, 400 if interval != "1d" else 250, analysis_day)
            local_index = data.index.tz_convert(NY_TZ)
            pm_volume = sum(float(row.Volume or 0) for row in data.itertuples() if session_name(row.Index) == "PREMARKET")
            if interval != "1d" and pm_volume <= 0:
                quality["status"] = "LOW QUALITY"
                quality["volume_quality"] = "MISSING"
            else:
                quality["volume_quality"] = "OK"
            quality_table.append({"item": f"{symbol}/{interval}", **quality})
            profiles = session_profiles(data, analysis_day)
            profile = profiles["RTH"]
            canonical = canonical_closes.get(symbol, {})
            chart_stats.append({"symbol": symbol, "interval": interval, "visible_bars": len(chart_data), "latest_timestamp": chart_data.index[-1].isoformat(), "latest_trading_day": data_trading_day(data).isoformat(), "close": clean_number(latest["Close"]), "rsi14": clean_number(latest["RSI14"]), "vwap": None if interval == "1d" else clean_number(latest["VWAP"]), "vwap_premarket": profiles["PM"]["vwap"].get("value"), "vwap_rth": profiles["RTH"]["vwap"].get("value"), "vwap_premarket_status": profiles["PM"]["vwap"].get("status"), "vwap_rth_status": profiles["RTH"]["vwap"].get("status"), "vwap_previous_rth": canonical.get("close"), "poc": profile["poc"], "vah": profile["vah"], "val": profile["val"], "session_profiles": profiles, "context": trading_context(data, canonical, analysis_day, opening_source), "ohlcv_csv": csv_name, "quality": quality, "ema20": clean_number(latest["EMA20"]), "ema50": clean_number(latest["EMA50"]), "ema200": clean_number(latest["EMA200"])})
    market_quotes = {name: asdict(quote(symbol)) for name, symbol in MARKET.items()}
    quality_table.extend({"item": name, "status": item.get("quality", "MISSING"), "source": item.get("source", "Yahoo Finance/yfinance"), "as_of": item.get("as_of")} for name, item in market_quotes.items())
    dgs2 = fred_observation("DGS2")
    market_quotes["미국채 2년 금리"] = {"symbol": "DGS2", "price": dgs2["value"], "change_pct": None, "as_of": dgs2["as_of"]}
    semiconductor_quotes = {name: asdict(quote(symbol, canonical_closes.get(symbol, {}).get("close"))) for name, symbol in SEMIS.items()}
    holdings = soxx_constituents()
    holding_symbols = {row["symbol"] for row in holdings.get("rows", [])}
    holding_quotes = {symbol: asdict(quote(symbol, canonical_rth_close(symbol).get("close"))) for symbol in holding_symbols if symbol not in semiconductor_quotes}
    breadth = breadth_and_relative_strength(holdings, {**semiconductor_quotes, **holding_quotes})
    options_data = [options(symbol) for symbol in ("SOXX", "QQQ", "NVDA", "SMH")]
    dgs10 = fred_observation("DGS10")
    real10 = fred_observation("DFII10")
    quality_table.extend([{"item": "FRED/DGS2", "status": dgs2.get("quality", "MISSING"), "source": "FRED", "as_of": dgs2["as_of"]}, {"item": "FRED/DGS10", "status": dgs10.get("quality", "MISSING"), "source": "FRED", "as_of": dgs10["as_of"]}, {"item": "FRED/DFII10 real yield", "status": real10.get("quality", "MISSING"), "source": "FRED", "as_of": real10["as_of"]}, {"item": "SOXX holdings", "status": "OK" if holdings["available"] else "LOW QUALITY", "source": holdings["source"], "as_of": holdings["as_of"]}, {"item": "SOXX breadth", "status": breadth.get("quality", "MISSING"), "source": breadth.get("source"), "as_of": breadth.get("as_of")}])
    calendar = economic_calendar()
    fed = fed_probabilities()
    news_items = news()
    quality_table.extend([{"item": "options", "status": "OK" if any(item.get("quality") == "OK" for item in options_data) else "LOW QUALITY", "source": "Yahoo Finance/yfinance", "as_of": datetime.now(timezone.utc).isoformat()}, {"item": "FedWatch", "status": fed.get("quality", "MISSING") if fed.get("meetings") else "MISSING", "source": fed.get("source"), "as_of": fed.get("as_of")}, {"item": "economic calendar", "status": "OK" if calendar.get("available") else "MISSING", "source": calendar.get("source"), "as_of": calendar.get("date")}, {"item": "news", "status": "OK" if news_items else "LOW QUALITY", "source": "filtered Google News RSS", "as_of": generated_at.isoformat()}])
    canonical_previous = canonical_closes.get("SOXX", {})
    payload = {"generated_at": generated_at.isoformat(), "market_session": market_session(), "latest_completed_trading_day": analysis_day.isoformat(), "previous_rth_date": canonical_previous.get("date"), "canonical_previous_rth": canonical_previous, "charts": chart_stats, "chart_files": chart_files, "market": market_quotes, "semiconductors": semiconductor_quotes, "options": options_data, "economic_calendar": calendar, "fed_probabilities": fed, "news": news_items, "semiconductor_events": semiconductor_events(), "macro": {"DGS2": dgs2, "DGS10": dgs10, "DFII10": real10}, "breadth": breadth, "relative_strength": relative_strength({symbol: raw_data[key] for symbol, key in (("SOXX", "SOXX/5m"), ("QQQ", "QQQ/5m"), ("SMH", "SMH/5m")) if key in raw_data}), "soxx_holdings": holdings, "quality": quality_table, "data_notes": ["1분봉은 Yahoo Finance 제공 제한 때문에 최근 7일만 수집합니다.", "EMA200은 intraday 400 bars, 일봉 250 거래일 이상 warm-up 후 계산합니다.", "옵션 감마는 유효한 bid/ask·IV·미결제약정과 현물가 ±20% 범위에서 계산한 추정치입니다."]}
    payload["validation"] = validate_report(payload)
    if payload["validation"]["checks"]["date_sync"] != "PASS":
        payload["analysis_status"] = "DATA_MISMATCH"
        payload["relative_strength"] = None
    else:
        payload["analysis_status"] = "READY"
    (output / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(chart_stats).to_csv(output / "indicators.csv", index=False)
    report_path = output / "market-brief.md"
    report_path.write_text(render_markdown(payload), encoding="utf-8")
    return report_path


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# 미국장 반도체·매크로 브리핑", f"생성 시각(UTC): {payload['generated_at']}", f"시장 상태: {payload.get('market_session', 'N/A')} · 최신 완료 거래일: {payload.get('latest_completed_trading_day', 'N/A')}", "", "## 시장", "| 항목 | 값 | 등락률 |", "|---|---:|---:|"]
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
            lines.append(f"| {item['symbol']} | {category} ({details.get('quality', 'N/A')}) | {details.get('expiry', 'N/A')} | {details.get('data_as_of', 'N/A')} | {format_value(walls.get('totalGammaExposure'))} | {format_value(walls.get('callWall'))} | {format_value(walls.get('putWall'))} | {format_value(walls.get('gammaFlip'))} |")
            for row in details.get("rows", []):
                lines.append(f"| {item['symbol']} {category} | {details.get('expiry', 'N/A')} | {row.get('type', '')} | {row.get('strike', '')} | {row.get('openInterest', 0)} | {row.get('volume', 0)} | {format_value(row.get('gamma'))} | {format_value(row.get('gammaExposure'))} |")
    calendar = payload["economic_calendar"]
    lines += ["", "## 당일 경제지표", "| 시각 | 지표 | 중요도 | 실제 | 예상 | 이전 | Surprise |", "|---|---|---|---:|---:|---:|---:|"]
    for event in calendar.get("events", []):
        lines.append(f"| {event.get('date', '')} | {event.get('title', '')} | {event.get('importance', '')} | {event.get('actual', '') or 'N/A'} | {event.get('forecast', '') or 'N/A'} | {event.get('previous', '') or 'N/A'} | {event.get('surprise', 'N/A')} |")
    if not calendar.get("events"):
        lines.append("| - | 당일 미국 경제지표 없음 또는 데이터 미수신 | - | N/A | N/A | N/A | N/A |")
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
    lines += ["", "## Breadth / Relative Strength", f"- SOXX breadth: 상승 {payload['breadth'].get('up_count', 'N/A')} / 하락 {payload['breadth'].get('down_count', 'N/A')} / 보합 {payload['breadth'].get('unchanged_count', 'N/A')} (coverage {format_value(payload['breadth'].get('coverage_pct'), '%')})", f"- SOXX weighted breadth: {format_value(payload['breadth'].get('weighted_breadth'), '%')} · 상태 {payload['breadth'].get('quality', 'MISSING')}", f"- Relative strength: {payload.get('relative_strength', {})}", "", "## 향후 7일 반도체 이벤트"]
    lines += [f"- {item['symbol']} {item['event']} · {item['as_of']}" for item in payload.get("semiconductor_events", [])] or ["- N/A"]
    lines += ["", "## Global Validation", f"전체 상태: **{payload.get('validation', {}).get('status', 'N/A')}**", "| 검증 항목 | 결과 |", "|---|---|"]
    lines += [f"| {name} | {status} |" for name, status in payload.get("validation", {}).get("checks", {}).items()]
    lines += ["", "## 데이터 품질", "| 항목 | 상태 | 출처 | 기준 시각 |", "|---|---|---|---|"]
    lines += [f"| {item.get('item')} | {item.get('status')} | {item.get('source')} | {item.get('as_of')} |" for item in payload.get("quality", [])]
    lines += ["", "## 매크로 참고", f"- FRED 2년물: {format_value(payload['macro']['DGS2']['value'], '%')} (시각 {payload['macro']['DGS2']['as_of']})", f"- FRED 10년물: {format_value(payload['macro']['DGS10']['value'], '%')} (시각 {payload['macro']['DGS10']['as_of']})", f"- FRED 10년 실질금리: {format_value(payload['macro']['DFII10']['value'], '%')} (시각 {payload['macro']['DFII10']['as_of']})", "- 경제지표 실제값/예상값은 공개 캘린더, Fed 금리확률은 FedWatch 데이터 응답이 제공하는 범위만 표시합니다.", "", "## 주요 뉴스"]
    lines += [f"- [{item['title']}]({item['link']}) · {item.get('source', 'N/A')} · {item.get('published_at', 'timestamp N/A')} · {item.get('summary', item['title'])} · 영향: {item.get('impact_path', 'N/A')} · 종목: {','.join(item.get('related_tickers', [])) or 'N/A'}" for item in payload["news"]]
    lines += ["", "## 첨부 차트", *[f"- `{item['file']}`" for item in payload["chart_files"]], "", "> 이 파일은 정보 제공용 자동 수집물이며 투자 조언이 아닙니다."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a daily market brief")
    parser.add_argument("--output", default=os.getenv("REPORT_DIR", "reports"), help="output directory")
    args = parser.parse_args()
    print(build_report(Path(args.output)))


if __name__ == "__main__":
    main()
