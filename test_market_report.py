import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from market_report import (
    CORE_CHARTS,
    latest_completed_trading_day,
    market_session,
    opening_ranges,
    session_vwap,
    session_profiles,
    validate_report,
)

NY = ZoneInfo("America/New_York")


class MarketReportValidationTests(unittest.TestCase):
    def test_closed_market_and_latest_completed_day(self):
        saturday = datetime(2026, 9, 19, 12, 0, tzinfo=NY)
        self.assertEqual(market_session(saturday), "CLOSED")
        self.assertEqual(latest_completed_trading_day(saturday), date(2026, 9, 18))

    def test_time_based_opening_ranges(self):
        index = pd.date_range("2026-09-18 09:30", periods=20, freq="min", tz=NY)
        data = pd.DataFrame({"Open": range(20), "High": range(1, 21), "Low": range(20), "Close": range(20), "Volume": [1] * 20}, index=index)
        or5, or15 = opening_ranges(data, date(2026, 9, 18))
        self.assertTrue(or5["as_of"].endswith("09:34:00-04:00"))
        self.assertTrue(or15["as_of"].endswith("09:44:00-04:00"))

    def test_profile_is_sliced_by_trading_date(self):
        first = pd.date_range("2026-09-17 09:30", periods=3, freq="min", tz=NY)
        second = pd.date_range("2026-09-18 09:30", periods=3, freq="min", tz=NY)
        index = first.append(second)
        data = pd.DataFrame({"Open": [500, 501, 502, 100, 101, 102], "High": [501, 502, 503, 101, 102, 103], "Low": [499, 500, 501, 99, 100, 101], "Close": [500, 501, 502, 100, 101, 102], "Volume": [1] * 6}, index=index)
        profile = session_profiles(data, date(2026, 9, 18))["RTH"]
        self.assertEqual(profile["status"], "OK")
        self.assertLessEqual(profile["vah"], 102)
        self.assertGreaterEqual(profile["val"], 100)

    def test_global_gate_accepts_consistent_dates(self):
        charts = [{"symbol": key.split("/")[0], "interval": key.split("/")[1], "latest_trading_day": "2026-09-18", "context": {"gap_status": "N/A", "or5": {"status": "OK"}, "or15": {"status": "OK"}, "session_profiles": {"PM": {"status": "MISSING"}, "RTH": {"status": "OK"}, "5RTH": {"status": "OK"}, "20RTH": {"status": "OK"}}}} for key in CORE_CHARTS]
        payload = {"latest_completed_trading_day": "2026-09-18", "market_session": "CLOSED", "previous_rth_date": "2026-09-17", "canonical_previous_rth": {"status": "OK", "date": "2026-09-17"}, "charts": charts, "breadth": {"as_of": "2026-09-19T00:00:00+00:00"}, "options": [{"data_as_of": "2026-09-19T00:00:00+00:00"}], "macro": {"DGS10": {"as_of": "2026-09-18T00:00:00+00:00"}}}
        result = validate_report(payload)
        self.assertEqual(result["status"], "PASS")

    def test_global_gate_rejects_date_mismatch(self):
        charts = [{"symbol": key.split("/")[0], "interval": key.split("/")[1], "latest_trading_day": "2026-09-17", "context": {"gap_status": "N/A", "or5": {"status": "OK"}, "or15": {"status": "OK"}, "session_profiles": {}}} for key in CORE_CHARTS]
        payload = {"latest_completed_trading_day": "2026-09-18", "market_session": "CLOSED", "previous_rth_date": "2026-09-17", "canonical_previous_rth": {"status": "OK", "date": "2026-09-17"}, "charts": charts, "breadth": {}, "options": [], "macro": {}}
        result = validate_report(payload)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["checks"]["date_sync"], "DATA_MISMATCH")

    def test_profile_invalid_status_is_not_silent(self):
        index = pd.date_range("2026-09-18 04:00", periods=2, freq="min", tz=NY)
        data = pd.DataFrame({"Open": [100, 101], "High": [101, 102], "Low": [99, 100], "Close": [100, 101], "Volume": [1, 1]}, index=index)
        profiles = session_profiles(data, date(2026, 9, 18))
        self.assertIn(profiles["PM"]["status"], {"OK", "MISSING"})

    def test_session_vwap_stays_inside_session_range(self):
        index = pd.date_range("2026-09-18 04:00", periods=3, freq="min", tz=NY)
        data = pd.DataFrame({"Open": [100, 101, 102], "High": [101, 102, 103], "Low": [99, 100, 101], "Close": [100, 101, 102], "Volume": [10, 20, 30]}, index=index)
        result = session_vwap(data, date(2026, 9, 18), "PREMARKET")
        self.assertEqual(result["status"], "OK")
        self.assertGreaterEqual(result["value"], result["session_low"])
        self.assertLessEqual(result["value"], result["session_high"])


if __name__ == "__main__":
    unittest.main()
