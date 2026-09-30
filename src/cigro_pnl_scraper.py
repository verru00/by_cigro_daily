"""cigro.io 분석 > 핵심이익지표 엑셀 다운로드 (Playwright).

광고 스크래퍼와 공유: 로그인, 화면 준비 대기, ERR_ABORTED 흡수.

── 화면 진입 ────────────────────────────────────────────────────
확인된 URL 구조:

  https://app.cigro.io/?menu=analysis&tab=key_metrics
      &brand_name=코즈코즈&start_date=2026-08-01&end_date=2026-09-30
      &group=gross_profit

── 광고와 다른 점 ───────────────────────────────────────────────
광고 화면은 기간을 합산해 캠페인당 1행으로 보여주기 때문에 하루씩 받아야 했다.
핵심이익지표는 날짜별 행이 그대로 나오므로 **기간 전체를 한 번에** 받는다.
엑셀 한 파일 안에 공헌이익 / 매출이익 / 순이익 세 시트가 들어 있다.
"""
import asyncio
import hashlib
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from . import config as C
from .cigro_ads_scraper import probe
from .cigro_scraper import _safe_goto, current_brand, ensure_brand, log, login

SCRAPER_VERSION = "pnl v14 (2026-09-28 브랜드 선택 3회 재시도)"

BASE = "https://app.cigro.io/"

# ── 검증된 셀렉터 (기존 cigro_total_auto.py 에서 실제로 동작하던 값) ──────
# 이 화면에는 광고처럼 'EXCEL' 글자가 박힌 버튼이 없다.
# 「기간별 분석」 우측의 아이콘이고, 안에 SVG 스프라이트 참조가 들어 있다.
#   <use href="...#file_download">
# 그래서 텍스트로는 절대 못 찾는다. use[href] 로 찾아야 한다.
DOWNLOAD_SELECTORS = [
    'button:has(use[href*="file_download"])',
    'div[class*="Button"]:has(use[href*="file_download"])',
    'use[href*="file_download"]',          # 마지막 수단: 아이콘 자체를 클릭
]

# 실제 핵심이익지표 화면의 필수 텍스트. 아이콘만 먼저 붙고 데이터가 늦게
# 바뀌는 경우가 있으므로 표 본문 안정화 판정에도 사용한다.
PNL_READY_TEXTS = ["매출총이익", "공헌이익", "순이익", "기간별 분석"]

DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[-~]\s*(\d{4}-\d{2}-\d{2})")


def render_budget(start: str, end: str) -> tuple[int, int]:
    """수집 구간 길이에 맞춰 (렌더 대기 ms, 다운로드 타임아웃 ms) 를 정한다.

    구간이 길수록 cigro 서버가 엑셀을 만드는 데 오래 걸린다.
    기존 cigro_total_auto.py 가 쓰던 기준을 그대로 옮겼다.
    """
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if span > 180:
        return 90_000, 900_000      # 6개월 초과
    if span > 45:
        return 45_000, 420_000      # 45일 초과
    return 15_000, 120_000          # 평소


def build_url(start: str, end: str, brand: str = "") -> str:
    """핵심이익지표 화면 직행 URL."""
    if C.PNL_URL:
        return C.PNL_URL.format(s=start, e=end, b=brand)

    params = {
        "menu": "analysis",
        "tab": "key_metrics",
        "start_date": start,
        "end_date": end,
        "group": C.PNL_GROUP,
    }
    if brand:
        params["brand_name"] = brand
    return BASE + "?" + urlencode(params)


async def download_button(page):
    """다운로드 아이콘 로케이터. 화면 준비 완료 판정에도 쓴다."""
    sels = ([C.PNL_DOWNLOAD_SELECTOR] if C.PNL_DOWNLOAD_SELECTOR else []) + DOWNLOAD_SELECTORS
    for sel in sels:
        loc = page.locator(sel).filter(visible=True).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
    return None


async def wait_rendered(page, timeout_ms: int = 90000, min_chars: int = 800) -> bool:
    """본문이 실제로 그려지고 떠 있는 오버레이가 사라질 때까지 기다린다.

    prep_clicks 를 너무 일찍 부르면 본문이 200자뿐인 빈 화면을 클릭하게 된다.

    coaBaYaJ2 floating-group 이 사라지길 기다리게 했다가 90초를 통째로
    날린 적이 있다. 그건 로딩 오버레이가 아니라 **상시 존재하는 요소**였다
    (본문이 3230자로 다 그려진 뒤에도 계속 1개). 이제 본문 길이만 본다.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last = 0
    while True:
        try:
            n = await page.evaluate(
                "() => (document.body ? document.body.innerText.trim().length : 0)")
        except Exception:
            n = 0
        if n >= min_chars:
            log(f"  화면 렌더 완료 (본문 {n}자)")
            return True
        if time.monotonic() >= deadline:
            log(f"  렌더 대기 시간 초과 (본문 {n}자)")
            return False
        if n != last:
            log(f"  렌더 중... 본문 {n}자")
            last = n
        await page.wait_for_timeout(3000)


async def prep_clicks(page, brand: str = "", attempts: int = 3) -> bool:
    """핵심이익지표 진입 전에 공통 상단 선택기로 브랜드를 확정한다.

    Bubble 콜드 로드 중에는 선택기가 보여도 클릭이 먹지 않아 드롭다운이
    안 열린다 (2026-09-28: 본문 551자 시점에 열기 5초 타임아웃으로 전체 중단).
    한 번 실패하면 바로 죽이지 않고 간격을 두고 다시 시도한다.
    """
    for attempt in range(1, attempts + 1):
        if await ensure_brand(page, brand):
            return True
        if attempt == attempts:
            break
        log(f"  브랜드 선택 재시도 대기 ({attempt}/{attempts}, 8초)")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(8000)
    return False


async def wait_ready(page, timeout_ms: int) -> bool:
    """다운로드 아이콘이 나타날 때까지 기다린다.

    광고 스크래퍼와 같은 원칙이다. 화면 텍스트(탭 라벨)는 표보다 훨씬 먼저
    그려져서 준비 신호로 쓸 수 없다. 실제로 눌러야 하는 것만 본다.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    next_report = 30.0
    while True:
        elapsed = timeout_ms / 1000 - (deadline - time.monotonic())
        if await download_button(page) is not None:
            log(f"  화면 준비 완료 (다운로드 아이콘 확인, {elapsed:.0f}초)")
            return True
        if elapsed >= next_report:
            try:
                n = await page.evaluate(
                    "() => (document.body ? document.body.innerText.trim().length : 0)")
            except Exception:
                n = 0
            log(f"  대기 중... {elapsed:.0f}초 (본문 {n}자)")
            next_report += 30.0
        if time.monotonic() >= deadline:
            log(f"  다운로드 아이콘 대기 시간 초과 ({timeout_ms/1000:.0f}초)")
            return False
        await page.wait_for_timeout(3000)


async def shown_range(page) -> tuple[str, str] | None:
    """화면에 표시된 기간을 읽는다. URL 이 덮어써졌는지 확인용."""
    try:
        body = await page.evaluate(
            "() => (document.body ? document.body.innerText : '')")
    except Exception:
        return None
    m = DATE_RANGE_RE.search(body or "")
    return (m.group(1), m.group(2)) if m else None


async def pnl_fingerprint(page) -> tuple[str, int] | None:
    """기간별 분석 표의 현재 텍스트 지문과 길이를 반환한다."""
    try:
        body = await page.evaluate(
            "() => (document.body ? document.body.innerText : '')")
    except Exception:
        return None

    lines = [" ".join(line.split()) for line in (body or "").splitlines()]
    text = "\n".join(line for line in lines if line)
    if not all(marker in text for marker in PNL_READY_TEXTS):
        return None
    if "기간별 분석" not in text:
        return None

    section = text.split("기간별 분석", 1)[1]
    # 표 뒤의 제품 영역/채팅 위젯은 지문에서 제외한다.
    if "\n제품" in section:
        section = section.split("\n제품", 1)[0]
    if not re.search(r"\d{4}-\d{2}-\d{2}", section):
        return None
    if not re.search(r"\d[\d,]{2,}", section):
        return None
    return hashlib.sha256(section.encode("utf-8")).hexdigest(), len(section)


async def wait_pnl_stable(page, start: str, end: str, brand: str,
                          timeout_ms: int, stable_reads: int = 3) -> str | None:
    """브랜드·기간·표 지문이 연속으로 안정될 때만 다운로드를 허용한다."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_signature = ""
    consecutive = 0
    next_report = 30.0
    started = time.monotonic()

    while time.monotonic() < deadline:
        selected = await current_brand(page)
        date_range = await shown_range(page)
        fingerprint = await pnl_fingerprint(page)
        button = await download_button(page)

        ready = (
            selected == brand
            and date_range == (start, end)
            and fingerprint is not None
            and button is not None
        )
        if ready:
            signature, chars = fingerprint
            consecutive = consecutive + 1 if signature == last_signature else 1
            last_signature = signature
            if consecutive >= stable_reads:
                elapsed = time.monotonic() - started
                log(f"  핵심이익 표 안정 확인 ({chars}자, {elapsed:.0f}초)")
                return signature
        else:
            consecutive = 0
            last_signature = ""

        elapsed = time.monotonic() - started
        if elapsed >= next_report:
            log("  표 안정화 대기 중... "
                f"브랜드='{selected}', 기간={date_range}, "
                f"표={'확인' if fingerprint else '미확인'}, "
                f"다운로드={'확인' if button else '미확인'}")
            next_report += 30.0
        await page.wait_for_timeout(2000)

    log("  핵심이익 표 안정화 시간 초과")
    return None


async def download_excel(page, out_path: Path, dl_timeout: int = 300000,
                         expected_brand: str = "",
                         expected_range: tuple[str, str] | None = None,
                         expected_signature: str = "") -> Path | None:
    # 클릭과 최대한 가까운 곳에서 다시 검사한다. 상단 라벨만 맞고 이미 전체
    # 브랜드로 생성된 export context 를 받았던 실제 사고를 fail-closed 한다.
    if expected_brand:
        selected = await current_brand(page)
        if selected != expected_brand:
            log(f"  다운로드 중단: 브랜드 '{selected}' != '{expected_brand}'")
            return None
    if expected_range:
        date_range = await shown_range(page)
        if date_range != expected_range:
            log(f"  다운로드 중단: 기간 {date_range} != {expected_range}")
            return None
    if expected_signature:
        fingerprint = await pnl_fingerprint(page)
        if fingerprint is None or fingerprint[0] != expected_signature:
            log("  다운로드 중단: 안정화 뒤 핵심이익 표가 다시 변경됨")
            return None

    loc = await download_button(page)
    if loc is None:
        log("  다운로드 아이콘을 찾지 못함")
        return None
    try:
        async with page.expect_download(timeout=dl_timeout) as dl:
            await loc.click(timeout=10000)
        download = await dl.value
        suffix = Path(download.suggested_filename).suffix or ".xlsx"
        target = out_path.with_suffix(suffix)
        await download.save_as(target)
        log(f"  다운로드 완료: {target.name}")
        return target
    except Exception as e:
        log(f"  엑셀 다운로드 실패: {e}")
        return None


async def fetch_pnl(start: str, end: str, out_dir: Path) -> Path | None:
    """핵심이익지표 엑셀 1개를 받아 경로를 반환. 실패하면 None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    brand = C.PNL_BRAND

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
            log(f"스크래퍼 버전: {SCRAPER_VERSION}")
            await login(page)

            render_wait, dl_timeout = render_budget(start, end)
            log(f"수집 구간 대기: 렌더 {render_wait // 1000}초 / "
                f"다운로드 제한 {dl_timeout // 60000}분")

            # 핵심 원인: 이전 코드는 전체 상태로 핵심이익지표 화면을 먼저
            # mount 한 뒤 상단 라벨만 코즈코즈로 바꿨다. 그 경우 화면 라벨은
            # 맞아도 엑셀 exporter 는 최초 전체-brand context 를 계속 썼다.
            # 로그인 landing(대시보드)에서 브랜드를 먼저 확정한 뒤 들어간다.
            await wait_rendered(page, timeout_ms=90000, min_chars=300)
            if not await prep_clicks(page, brand):
                await page.screenshot(path=str(out_dir / "err_brand_before_pnl.png"))
                await probe(page, out_dir, "96_brand_before_pnl")
                log("  -> 핵심이익지표 진입 전 브랜드 선택 실패, 중단")
                return None
            # 계정 전역 브랜드 상태가 백엔드까지 반영된 뒤 화면을 mount 한다.
            await page.wait_for_timeout(5000)

            url = build_url(start, end, brand)
            stable_signature: str | None = None
            for attempt in range(1, 3):
                log(f"이동 ({attempt}/2): {url}")
                await _safe_goto(page, url, settle=6000)
                await wait_rendered(page)

                selected = await current_brand(page)
                log(f"  핵심이익지표 진입 후 브랜드: '{selected}'")
                if selected == brand:
                    await page.wait_for_timeout(render_wait)
                    stable_signature = await wait_pnl_stable(
                        page, start, end, brand, C.PNL_WAIT_MS)
                    if stable_signature:
                        break
                else:
                    log(f"  브랜드가 '{brand}' 에서 바뀜")

                if attempt == 1:
                    log("  대시보드에서 브랜드를 다시 확정한 뒤 재진입")
                    await _safe_goto(
                        page, BASE + "?menu=dashboard&tab=main", settle=6000)
                    await wait_rendered(page, timeout_ms=90000, min_chars=300)
                    if not await prep_clicks(page, brand):
                        break
                    await page.wait_for_timeout(5000)

            if not stable_signature:
                log(f"  핵심이익지표 브랜드/기간/표 안정화 실패: {page.url}")
                await page.screenshot(path=str(out_dir / "err_screen.png"))
                await probe(page, out_dir, "99_notpnl")
                return None

            # 기간 불일치는 경고가 아니라 중단 사유다.
            rng = await shown_range(page)
            if rng != (start, end):
                log(f"  화면 기간 {rng} / 요청 {(start, end)} -> 중단")
                return None

            if C.PNL_PROBE:
                await probe(page, out_dir, "ready")
                log("  [PROBE] 다운로드는 건너뜁니다.")
                return None

            # 무엇이 선택된 상태로 받았는지 눈으로 확인할 수 있게 남긴다.
            try:
                await page.screenshot(path=str(out_dir / "before_download.png"))
            except Exception:
                pass
            log(f"  다운로드 직전 브랜드 표시: '{await current_brand(page)}'")

            path = await download_excel(
                page,
                out_dir / f"손익_{start}_{end}",
                dl_timeout,
                expected_brand=brand,
                expected_range=(start, end),
                expected_signature=stable_signature,
            )
            if path is None:
                await page.screenshot(path=str(out_dir / "err_excel.png"))
                await probe(page, out_dir, "98_noexcel")
            return path
        finally:
            await context.close()
            await browser.close()
