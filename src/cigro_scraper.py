"""cigro.io 주문내역 엑셀 다운로드 (Playwright).

cigro_gui.py 에서 검증된 셀렉터와 실행 순서를 그대로 이식했다.
GUI / 키워드 검색 / 광고 수집은 제외.

중요한 순서 (기존 코드 주석 근거):
  데이터 아이콘 -> 주문 화면 -> 주문 카테고리 -> 브랜드 선택 -> 기간 설정
  * 카테고리 전환이 브랜드를 초기화하므로 브랜드는 카테고리 '다음'에 설정
  * 주문 화면은 URL 의 start_date 를 '오늘-30일'로 덮어쓰므로 달력을 직접 클릭
"""
import asyncio
import re
import time
from pathlib import Path

from playwright.async_api import async_playwright

from . import config as C

LOGIN_URL = "https://app.cigro.io/login"
ORDER_URL = "https://app.cigro.io/?menu=data&tab=sales&start_date={s}&end_date={e}"


def log(msg: str) -> None:
    print(msg, flush=True)


async def _safe_goto(page, url: str, settle: int = 0) -> None:
    """SPA 라우팅에 가로채여 발생하는 ERR_ABORTED 를 흡수."""
    try:
        await page.goto(url, wait_until="commit", timeout=20000)
    except Exception as e:
        if "ERR_ABORTED" not in str(e) and "aborted" not in str(e).lower():
            raise
        log("  (ERR_ABORTED 무시 - SPA 라우팅)")
    if settle:
        await page.wait_for_timeout(settle)


async def login(page) -> None:
    log("로그인 시도 중...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    await page.wait_for_selector('input[type="email"][placeholder="E-mail"]', timeout=15000)
    await page.fill('input[type="email"][placeholder="E-mail"]', C.CIGRO_EMAIL)

    await page.wait_for_selector('input[type="password"][placeholder="Password"]', timeout=8000)
    await page.fill('input[type="password"][placeholder="Password"]', C.CIGRO_PASSWORD)

    try:
        await page.wait_for_selector(".cnaNaCaF0", timeout=5000)
        await page.click(".cnaNaCaF0")
    except Exception:
        await page.locator('div.clickable-element:has-text("로그인")').first.click(timeout=5000)

    try:
        await page.wait_for_url(lambda u: "/login" not in u, timeout=25000)
    except Exception:
        await page.wait_for_selector('div[class*="sidebar"], nav', timeout=10000)
    log("로그인 완료")


async def enter_order_screen(page, start: str, end: str) -> None:
    """데이터 아이콘 -> 주문 화면 -> 주문 카테고리. (브랜드 루프 전 1회만)"""
    try:
        await page.wait_for_selector(".cneaWaH1", timeout=10000)
        await page.click(".cneaWaH1")
        await page.wait_for_timeout(1500)
    except Exception as e:
        log(f"  데이터 아이콘 클릭 실패(무시): {e}")

    await _safe_goto(page, ORDER_URL.format(s=start, e=end), settle=3000)

    await page.wait_for_selector("div.cneaWk1", timeout=15000)
    await page.click("div.cneaWk1")
    await page.wait_for_timeout(2000)
    log("주문 화면 진입 완료")


# 실제 Chrome 에서 대시보드/광고/핵심이익지표 화면 모두 같은 구조임을 확인.
# 본문 전체에서 브랜드 이름을 찾으면 상품명·드롭다운 선택지에도 같은 문자열이
# 있어 거짓 양성이 난다. 반드시 상단 선택기의 현재 라벨만 읽는다.
BRAND_OPENER = "div.cnbaEt1.clickable-element"
BRAND_LABEL = "div.coldw1"
BRAND_OPTION = "div.cnbaFt1.clickable-element"
BRAND_ALL_TEXT = "전체 (브랜드 매칭된 데이터)"


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").split())


async def current_brand(page, brand: str = "") -> str:
    """상단 브랜드 선택기의 현재 라벨을 정확히 읽는다.

    ``brand`` 는 기존 호출부 호환용이다. 부분 문자열 판정에는 사용하지 않는다.
    선택기가 없거나 둘 이상이면 안전하게 빈 문자열을 반환한다.
    """
    del brand
    try:
        openers = page.locator(BRAND_OPENER).filter(visible=True)
        if await openers.count() != 1:
            return ""
        labels = openers.first.locator(BRAND_LABEL).filter(visible=True)
        if await labels.count() != 1:
            return ""
        label = _norm_text(await labels.first.inner_text(timeout=2000))
    except Exception:
        return ""
    if label == BRAND_ALL_TEXT:
        return "전체"
    return label


async def select_brand(page, brand: str, force: bool = False) -> bool:
    """상단 드롭다운의 정확히 일치하는 브랜드 행을 하나만 골라 선택한다."""
    brand = _norm_text(brand)
    if not brand:
        log("  브랜드명이 비어 있어 선택할 수 없음")
        return False

    if not force and await current_brand(page) == brand:
        log(f"  브랜드 이미 설정됨: {brand}")
        return True

    try:
        opener = page.locator(BRAND_OPENER).filter(visible=True)
        await opener.first.wait_for(state="visible", timeout=15000)
        if await opener.count() != 1:
            log(f"  브랜드 선택기 개수 오류: {await opener.count()}개")
            return False
        await opener.first.scroll_into_view_if_needed(timeout=3000)
        await opener.first.click(timeout=5000)
        await page.locator(BRAND_OPTION).filter(visible=True).first.wait_for(
            state="visible", timeout=5000)
    except Exception as e:
        log(f"  브랜드 드롭다운 열기 실패: {e}")
        return False

    rows = page.locator(BRAND_OPTION).filter(visible=True)
    choices: list[str] = []
    matches: list[int] = []
    try:
        for index in range(await rows.count()):
            text = _norm_text(await rows.nth(index).inner_text(timeout=2000))
            choices.append(text)
            if text == brand:
                matches.append(index)
    except Exception as e:
        log(f"  브랜드 선택지 읽기 실패: {e}")
        return False

    if len(matches) != 1:
        log(f"  브랜드 '{brand}' 정확일치 행 {len(matches)}개 / 선택지: {choices}")
        return False

    try:
        row = rows.nth(matches[0])
        await row.scroll_into_view_if_needed(timeout=3000)
        await row.click(timeout=5000)
    except Exception as e:
        log(f"  브랜드 '{brand}' 클릭 실패: {e}")
        return False

    # 라벨이 실제로 바뀔 때까지 기다린다. 클릭 성공만으로는 선택 성공이 아니다.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if await current_brand(page) == brand:
            try:
                if await page.locator(BRAND_OPTION).filter(visible=True).count() > 0:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(250)
            except Exception:
                pass
            if await current_brand(page) == brand:
                log(f"  브랜드 선택 완료: {brand}")
                return True
        await page.wait_for_timeout(250)

    log(f"  브랜드 선택 후 라벨이 '{brand}' 로 바뀌지 않음")
    return False


async def ensure_brand(page, brand: str) -> bool:
    """브랜드를 강제로 다시 선택하고 현재 라벨을 연속 확인한다."""
    brand = _norm_text(brand)
    if not brand:
        log("  브랜드 미지정 - 드롭다운 건드리지 않음")
        return True

    before = await current_brand(page)
    log(f"  브랜드 선택 전: '{before}'")
    ok = await select_brand(page, brand, force=True)
    if not ok:
        log("  ! 브랜드 행 클릭/확인 실패")
        return False

    # SPA 라벨이 잠깐 맞았다가 되돌아가는 경우를 막기 위해 연속 3회 확인한다.
    stable = 0
    deadline = time.monotonic() + 10
    last = ""
    while time.monotonic() < deadline:
        last = await current_brand(page)
        stable = stable + 1 if last == brand else 0
        if stable >= 3:
            log(f"  브랜드 선택 후: '{last}' (연속 확인 완료)")
            return True
        await page.wait_for_timeout(400)

    log(f"  ! 화면이 '{brand}' 를 안정적으로 가리키지 않음 (현재: '{last}')")
    return False


async def _pick_air_date(page, y: int, m: int, d: int, tag: str = "") -> bool:
    """air-datepicker 에서 (연, 월[1-12], 일) 셀 클릭. 달 이동 overshoot 방지."""
    target = (y, m - 1)  # data-month 는 0부터
    cell_sel = (f".air-datepicker-cell.-day-[data-year='{y}']"
                f"[data-month='{m-1}'][data-date='{d}']"
                f":not(.-other-month-):not(.-disabled-)")

    async def cur_ym():
        try:
            c = page.locator(".air-datepicker-cell.-day-:not(.-other-month-)").first
            return (int(await c.get_attribute("data-year")),
                    int(await c.get_attribute("data-month")))
        except Exception:
            return None

    async def nav(action: str):
        loc = page.locator(f".air-datepicker-nav--action[data-action='{action}']").first
        if await loc.count() == 0:
            loc = (page.locator(".air-datepicker-nav--action").first if action == "prev"
                   else page.locator(".air-datepicker-nav--action").last)
        await loc.click(timeout=2000)

    for _ in range(48):
        cur = await cur_ym()
        if cur is None or cur == target:
            break
        try:
            await nav("prev" if cur > target else "next")
        except Exception:
            log(f"  달력 이동 화살표 없음 ({tag})")
            break
        changed = False
        for _w in range(25):
            await page.wait_for_timeout(120)
            if await cur_ym() != cur:
                changed = True
                break
        if not changed:
            log(f"  달력이 안 바뀜 - 중단 ({tag})")
            break

    try:
        loc = page.locator(cell_sel).filter(visible=True).first
        await loc.scroll_into_view_if_needed(timeout=2000)
        await loc.click(timeout=3000)
        await page.wait_for_timeout(500)
        return True
    except Exception:
        log(f"  날짜 셀 못 찾음 ({tag})")
        return False


async def apply_date_range(page, start: str, end: str) -> bool:
    """화면 달력으로 기간 직접 지정. URL 파라미터는 리셋되므로 필수."""
    sy, sm, sd = (int(x) for x in start.split("-"))
    ey, em, ed = (int(x) for x in end.split("-"))

    try:
        trig = page.get_by_text(
            re.compile(r"\d{4}-\d{2}-\d{2}\s*[-~]\s*\d{4}-\d{2}-\d{2}")
        ).filter(visible=True).first
        await trig.click(timeout=5000)
        await page.wait_for_selector(".air-datepicker-cell.-day-", timeout=6000)
    except Exception as e:
        log(f"  날짜 선택기 열기 실패: {e}")
        return False

    ok1 = await _pick_air_date(page, sy, sm, sd, f"시작 {start}")
    ok2 = await _pick_air_date(page, ey, em, ed, f"종료 {end}")

    try:
        try:
            await page.locator("button.coaOys", has_text="OK").first.click(timeout=4000)
        except Exception:
            await page.locator("button", has_text=re.compile(r"^\s*OK\s*$")).first.click(timeout=4000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(4000)
    except Exception as e:
        log(f"  날짜 OK 클릭 실패: {e}")
        return False

    if not (ok1 and ok2):
        log("  ! 기간 일부만 적용됐을 수 있음")
        return False
    log(f"  기간 적용 완료: {start} ~ {end}")
    return True


async def apply_date_range_retry(page, start: str, end: str, attempts: int = 3) -> bool:
    """기간 설정을 최대 attempts 회 시도. 매번 맨 위로 스크롤하고 열린 달력을 닫는다."""
    for n in range(1, attempts + 1):
        try:
            await page.keyboard.press("Escape")
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(800)
        except Exception:
            pass
        if await apply_date_range(page, start, end):
            return True
        if n < attempts:
            log(f"  기간 설정 재시도 ({n + 1}/{attempts})")
            await page.wait_for_timeout(2000)
    return False


async def download_excel(page, out_path: Path) -> bool:
    """검색어 없이 전체 결과 엑셀 다운로드."""
    # 검색창이 이전 값을 물고 있을 수 있으므로 비우고 재조회 (실패해도 진행)
    try:
        await page.fill("input.cnhaXp1", "")
        await page.click("button.cnhaXr1", timeout=4000)
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("button.coeaXaE0", timeout=15000)
        async with page.expect_download(timeout=180000) as dl_info:
            await page.click("button.coeaXaE0")
        download = await dl_info.value
        suffix = Path(download.suggested_filename).suffix or ".xlsx"
        target = out_path.with_suffix(suffix)
        await download.save_as(target)
        log(f"  다운로드 완료: {target.name}")
        return True
    except Exception as e:
        log(f"  엑셀 다운로드 실패: {e}")
        return False


async def fetch_all(brands: list[str], start: str, end: str, out_dir: Path) -> dict[str, Path]:
    """브랜드별 주문내역 엑셀을 받아 {브랜드: 파일경로} 반환."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=C.HEADLESS)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1600, "height": 1000},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        context.on("page", lambda np: asyncio.ensure_future(np.close()))

        try:
            await login(page)
            await enter_order_screen(page, start, end)

            for i, brand in enumerate(brands, 1):
                log(f"[{i}/{len(brands)}] {brand}")
                # 두 번째 브랜드부터는 앞 브랜드가 남긴 화면(스크롤·달력 상태)에서
                # 기간 설정이 실패했다. 첫 브랜드와 같은 깨끗한 화면에서 시작한다.
                if i > 1:
                    await enter_order_screen(page, start, end)

                if not await select_brand(page, brand):
                    await page.screenshot(path=str(out_dir / f"err_brand_{brand}.png"))
                    log(f"  -> 브랜드 선택 실패, 건너뜀")
                    continue

                if not await apply_date_range_retry(page, start, end):
                    await page.screenshot(path=str(out_dir / f"err_date_{brand}.png"))
                    log(f"  -> 기간 설정 실패, 건너뜀 (잘못된 기간 수집 방지)")
                    continue

                stem = out_dir / f"주문_{brand}_{start}_{end}"
                if await download_excel(page, stem):
                    for ext in (".xlsx", ".xls", ".csv"):
                        if stem.with_suffix(ext).exists():
                            results[brand] = stem.with_suffix(ext)
                            break
                else:
                    await page.screenshot(path=str(out_dir / f"err_excel_{brand}.png"))
        finally:
            await context.close()
            await browser.close()

    return results
