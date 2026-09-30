import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import cigro_pnl_scraper as pnl
from src import cigro_scraper as scraper


class _FakeLocator:
    def __init__(self, page, kind, index=None):
        self.page = page
        self.kind = kind
        self.index = index

    def filter(self, **_kwargs):
        return self

    @property
    def first(self):
        if self.kind == "options":
            return _FakeLocator(self.page, "row", 0)
        return self

    def nth(self, index):
        return _FakeLocator(self.page, "row", index)

    def locator(self, selector):
        if self.kind == "opener" and selector == scraper.BRAND_LABEL:
            return _FakeLocator(self.page, "label")
        raise AssertionError(f"unexpected nested selector: {selector}")

    async def count(self):
        if self.kind in {"opener", "label", "row"}:
            return 1
        if self.kind == "options":
            return len(self.page.options) if self.page.opened else 0
        return 0

    async def inner_text(self, **_kwargs):
        if self.kind == "label":
            return self.page.selected
        if self.kind == "row":
            return self.page.options[self.index]
        return ""

    async def wait_for(self, **_kwargs):
        return None

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        if self.kind == "opener":
            self.page.opened = True
        elif self.kind == "row":
            self.page.selected = self.page.options[self.index]
            self.page.opened = False


class _FakeBrandPage:
    def __init__(self, selected, options):
        self.selected = selected
        self.options = options
        self.opened = False
        self.keyboard = SimpleNamespace(press=AsyncMock())

    def locator(self, selector):
        if selector == scraper.BRAND_OPENER:
            return _FakeLocator(self, "opener")
        if selector == scraper.BRAND_OPTION:
            return _FakeLocator(self, "options")
        raise AssertionError(f"unexpected selector: {selector}")

    async def wait_for_timeout(self, _milliseconds):
        return None


class BrandGuardTests(unittest.IsolatedAsyncioTestCase):

    async def test_current_brand_ignores_brand_text_outside_selected_label(self):
        page = _FakeBrandPage(
            "전체 (브랜드 매칭된 데이터)",
            ["전체 (브랜드 매칭된 데이터)", "코즈코즈"],
        )
        page.unrelated_body_text = "코즈코즈 상품 분석"
        self.assertEqual(await scraper.current_brand(page), "전체")

    async def test_select_brand_clicks_exact_dropdown_row(self):
        page = _FakeBrandPage(
            "전체 (브랜드 매칭된 데이터)",
            ["전체 (브랜드 매칭된 데이터)", "수면공감", "코즈코즈"],
        )
        self.assertTrue(await scraper.select_brand(page, "코즈코즈", force=True))
        self.assertEqual(await scraper.current_brand(page), "코즈코즈")

    async def test_ensure_brand_rejects_click_failure(self):
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        with (
            patch.object(scraper, "current_brand", new=AsyncMock(return_value="코즈코즈")),
            patch.object(scraper, "select_brand", new=AsyncMock(return_value=False)),
        ):
            self.assertFalse(await scraper.ensure_brand(page, "코즈코즈"))

    async def test_download_stops_before_button_when_brand_changed(self):
        with (
            patch.object(pnl, "current_brand", new=AsyncMock(return_value="전체")),
            patch.object(pnl, "download_button", new=AsyncMock()) as button,
        ):
            result = await pnl.download_excel(
                object(),
                Path("손익.xlsx"),
                expected_brand="코즈코즈",
            )
        self.assertIsNone(result)
        button.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
