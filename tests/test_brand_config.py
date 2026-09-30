import importlib
import os
import unittest
from unittest import mock

from src import brand_config


def load_config(env=None, brand="새브랜드"):
    """환경변수·brand_config 를 바꿔 config 를 다시 읽는다."""
    keys = ("CIGRO_BRANDS", "CIGRO_AD_BRAND", "CIGRO_PNL_BRAND")
    clean = {k: v for k, v in os.environ.items() if k not in keys}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True), \
         mock.patch.object(brand_config, "BRAND", brand):
        import src.config as C
        return importlib.reload(C)


class BrandConfigTest(unittest.TestCase):
    def tearDown(self):
        load_config()

    def test_default_comes_from_brand_config(self):
        C = load_config()
        self.assertEqual((C.BRANDS, C.ADS_BRAND, C.PNL_BRAND), (["새브랜드"], "새브랜드", "새브랜드"))

    def test_empty_workflow_vars_fall_back(self):
        # 워크플로는 Variables 가 없으면 빈 문자열을 넘긴다
        C = load_config({"CIGRO_BRANDS": "", "CIGRO_AD_BRAND": " ", "CIGRO_PNL_BRAND": ""})
        self.assertEqual((C.BRANDS, C.ADS_BRAND, C.PNL_BRAND), (["새브랜드"], "새브랜드", "새브랜드"))

    def test_env_overrides(self):
        C = load_config({"CIGRO_BRANDS": "A, B", "CIGRO_AD_BRAND": "A"})
        self.assertEqual((C.BRANDS, C.ADS_BRAND, C.PNL_BRAND), (["A", "B"], "A", "A"))

    def test_placeholder_stops(self):
        C = load_config(brand="여기에_브랜드명")
        with self.assertRaisesRegex(SystemExit, "brand_config"):
            C.check_brand(C.ADS_BRAND, "광고")
        with self.assertRaisesRegex(SystemExit, "brand_config"):
            C.check_brand(C.BRANDS, "주문")

    def test_empty_brand_stops(self):
        C = load_config(brand="")
        with self.assertRaises(SystemExit):
            C.check_brand(C.ADS_BRAND, "광고")
        with self.assertRaises(SystemExit):
            C.check_brand(C.BRANDS, "주문")


if __name__ == "__main__":
    unittest.main()
