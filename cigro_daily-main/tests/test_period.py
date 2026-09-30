# -*- coding: utf-8 -*-
"""수집 기간 계산. 특히 start_date 만 넣었을 때.

실제로 워크플로에 start_date=2026-01-01 만 넣었는데 계속 7월(전전월)부터
수집했다. 예전 코드가 `if OVERRIDE_START and OVERRIDE_END:` 라서 한쪽만
넣으면 조용히 자동 기간으로 돌아갔다. 경고도 안 찍혔다.
"""
import importlib
import os
import unittest
from datetime import date, datetime, timedelta


def _config(**env):
    """환경변수를 바꾼 상태로 config 를 새로 읽는다 (모듈 로드 시점에 읽음)."""
    old = {k: os.environ.get(k) for k in ("START_DATE", "END_DATE")}
    os.environ["START_DATE"] = env.get("start", "")
    os.environ["END_DATE"] = env.get("end", "")
    try:
        import src.config as C
        return importlib.reload(C)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PeriodTest(unittest.TestCase):

    def test_start_only_is_honored(self):
        C = _config(start="2026-01-01")
        start, end = C.period()
        self.assertEqual(start, "2026-01-01")          # 7월로 돌아가면 안 된다
        yesterday = datetime.now(C.KST).date() - timedelta(days=1)
        self.assertEqual(end, yesterday.isoformat())   # 종료일은 자동

    def test_end_only_is_honored(self):
        """종료일만 넣으면 시작일은 자동(전전월 1일)."""
        C = _config()
        cut = (datetime.now(C.KST).date() - timedelta(days=5)).isoformat()
        C = _config(end=cut)
        start, end = C.period()
        self.assertEqual(end, cut)
        today = datetime.now(C.KST).date()
        self.assertEqual(start, C._minus_months(today, 2).isoformat())

    def test_both_empty_is_auto(self):
        C = _config()
        start, end = C.period()
        today = datetime.now(C.KST).date()
        self.assertEqual(start, C._minus_months(today, 2).isoformat())
        self.assertEqual(end, (today - timedelta(days=1)).isoformat())

    def test_reversed_period_raises(self):
        C = _config(start="2026-09-10", end="2026-09-01")
        with self.assertRaises(ValueError):
            C.period()

    def test_ads_start_only_runs_until_yesterday(self):
        C = _config(start="2026-09-01")
        days = C.ads_dates()
        yesterday = datetime.now(C.KST).date() - timedelta(days=1)
        self.assertEqual(days[0], "2026-09-01")
        self.assertEqual(days[-1], yesterday.isoformat())
        self.assertEqual(len(days), (yesterday - date(2026, 9, 1)).days + 1)

    def test_ads_both_empty_is_auto(self):
        C = _config()
        today = datetime.now(C.KST).date()
        back = 2 if today.weekday() == 0 else 1
        self.assertEqual(len(C.ads_dates()), back)


if __name__ == "__main__":
    unittest.main()
