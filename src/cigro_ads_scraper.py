"""cigro.io 분석 > 광고 > 캠페인 엑셀 다운로드 (Playwright).

주문 스크래퍼와 공유: 로그인, air-datepicker 기간 설정, ERR_ABORTED 흡수.

── 화면 진입 ────────────────────────────────────────────────────
확인된 URL 구조:

  https://app.cigro.io/?menu=analysis&tab=ad&group_by=campaign
      &brand_name=코즈코즈&start_date=2026-09-10&end_date=2026-09-10

  menu=analysis       분석
  tab=ad              광고
  group_by=campaign   캠페인 하위탭 (ad_channel 이면 광고채널)
  brand_name          브랜드. 드롭다운 클릭 없이 URL 로 지정된다.

브랜드도 기간도 URL 파라미터라 클릭 탐색이 필요 없다.
다만 주문 화면은 URL 의 start_date 를 '오늘-30일'로 덮어썼던 전력이 있어,
화면에 실제 표시된 기간을 읽어 검증하고 다르면 달력을 직접 클릭한다.

── 하루씩 받는 이유 ─────────────────────────────────────────────
이 화면은 선택한 기간을 '합산'해서 캠페인당 1행으로 보여준다. 날짜 열이 없다.
따라서 이틀치를 한 번에 받으면 두 날이 하나로 합쳐져 일별 데이터가 사라진다.
월요일(전전일+전일)에도 하루씩 두 번 받는다.
"""
import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from . import config as C
from .cigro_scraper import (_safe_goto, apply_date_range, ensure_brand,
                            log, login)

# 광고 스크래퍼 자체의 버전. 광고 코드가 바뀔 때만 올린다.
# (프로젝트 전체 버전은 VERSION.md 를 본다. 예전엔 이 값을 프로젝트 버전으로
#  써서 손익만 고쳐도 이 파일이 매번 바뀌었다.)
SCRAPER_VERSION = "ads v3 (2026-09-16 브랜드 명시 선택)"

BASE = "https://app.cigro.io/"

# 광고 캠페인 화면 판정 신호.
ADS_READY_TEXTS = ["캠페인", "광고비", "ROAS"]

# ── 확인된 셀렉터 (사용자가 개발자도구로 확인) ────────────────────
# export-btn 은 Bubble 자동생성 클래스가 아니라 고정 클래스명이라
# 재배포에도 잘 살아남는다. 1순위로 쓴다.
EXCEL_SELECTORS = ["button.export-btn", "button.coeaXaE0"]

# 사이드바 분석 -> 광고 -> 캠페인. URL 직행이 실패할 때의 대비책.
SEL_NAV_ANALYSIS = "div.cnaNaPc2"    # 사이드바 '분석' 아이콘 그룹
SEL_TAB_AD       = "div.cnaNaQg2"    # 분석 내부 '광고'
SEL_TAB_CAMPAIGN = "div.cnoraD3"     # 광고 화면의 '캠페인' 탭

DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*[-~]\s*(\d{4}-\d{2}-\d{2})")

PROBE_JS = """() => {
    const out = [];
    const sel = 'div.clickable-element, button, a, [role=button]';
    for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        const cls = (el.className || '').toString().trim().split(/\\s+/).join('.');
        const txt = (el.innerText || '').trim().slice(0, 60).replace(/\\s+/g, ' ');
        out.push(el.tagName.toLowerCase() + '.' + cls + '  |  ' + txt);
    }
    return out.slice(0, 400);
}"""

BODY_TEXT_JS = """() => {
    const t = document.body ? document.body.innerText : '';
    return t.trim().replace(/\\n{3,}/g, '\\n\\n').slice(0, 2000);
}"""


def build_url(day: str, brand: str = "") -> str:
    """캠페인 화면 직행 URL. 하루짜리라 start=end 다."""
    if C.ADS_URL:
        return C.ADS_URL.format(s=day, e=day, b=brand)

    params = {
        "menu": "analysis",
        "tab": "ad",
        "group_by": C.ADS_GROUP_BY,
        "start_date": day,
        "end_date": day,
    }
    if brand:
        params["brand_name"] = brand
    return BASE + "?" + urlencode(params)


async def probe(page, out_dir: Path, tag: str) -> None:
    """클릭 가능한 요소의 클래스명과 텍스트를 덤프. 셀렉터 찾기용."""
    try:
        await page.screenshot(path=str(out_dir / f"probe_{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        items = await page.evaluate(PROBE_JS)
    except Exception as e:
        items = [f"(덤프 실패: {e})"]

    # 어떤 화면에 있는지 알아야 원인을 짚을 수 있다. 본문 텍스트도 함께 남긴다.
    try:
        title = await page.title()
    except Exception:
        title = "(제목 못 읽음)"
    try:
        body = await page.evaluate(BODY_TEXT_JS)
    except Exception as e:
        body = f"(본문 못 읽음: {e})"

    bar = "=" * 60
    path = out_dir / f"probe_{tag}.txt"
    path.write_text(
        f"URL   : {page.url}\n"
        f"TITLE : {title}\n\n"
        f"{bar}\n본문 텍스트 (앞 2000자)\n{bar}\n{body}\n\n"
        f"{bar}\n클릭 가능 요소 {len(items)}개\n{bar}\n" + "\n".join(items),
        encoding="utf-8")
    log(f"  [probe] {path.name} (요소 {len(items)}개 / 본문 {len(body)}자)")


async def _looks_like_ads_screen(page) -> bool:
    hits = 0
    for t in ADS_READY_TEXTS:
        try:
            if await page.get_by_text(t, exact=False).filter(visible=True).count() > 0:
                hits += 1
        except Exception:
            pass
    return hits >= 2


async def excel_button(page):
    """EXCEL 버튼 로케이터. 화면 준비 완료의 가장 확실한 신호이기도 하다."""
    selectors = ([C.ADS_EXCEL_SELECTOR] if C.ADS_EXCEL_SELECTOR else []) + EXCEL_SELECTORS

    for sel in selectors:
        loc = page.locator(sel).filter(visible=True).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass

    for pat in ("EXCEL", "엑셀", "다운로드"):
        loc = (page.locator("div.clickable-element, button, a")
               .filter(has_text=re.compile(pat, re.I))
               .filter(visible=True).first)
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
    return None


async def body_len(page) -> int:
    """본문 글자 수. 표가 그려졌는지 가늠하는 보조 지표."""
    try:
        return await page.evaluate(
            "() => (document.body ? document.body.innerText.trim().length : 0)")
    except Exception:
        return 0


async def wait_ready(page, timeout_ms: int = 90000) -> bool:
    """EXCEL 버튼이 나타날 때까지 기다린다.

    준비 신호는 **EXCEL 버튼 하나뿐**이다. 우리가 실제로 눌러야 하는 것이고,
    표 데이터가 다 그려진 뒤에야 붙기 때문이다.

    기간 표시(YYYY-MM-DD - YYYY-MM-DD)와 '캠페인/광고비/ROAS' 텍스트는
    준비 신호로 쓰지 않는다. 헤더·탭 라벨이라 표보다 훨씬 먼저 그려져서,
    이것들로 판정하면 0초 만에 통과해버리고 정작 버튼이 없다.
    (실제로 그 버그로 '화면 준비 완료 (기간 표시 확인, 0초)' 직후
     '엑셀 다운로드 버튼을 찾지 못함' 이 났다. 본문은 143자뿐이었다.)
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # 경과는 벽시계로 잰다. 각 검사에 자체 타임아웃이 있어서
    # sleep 시간만 누적하면 실제 경과가 지정값을 크게 넘어간다.
    deadline = time.monotonic() + timeout_ms / 1000
    next_report = 15.0
    while True:
        elapsed = timeout_ms / 1000 - (deadline - time.monotonic())

        if await excel_button(page) is not None:
            log(f"  화면 준비 완료 (EXCEL 버튼 확인, {elapsed:.0f}초)")
            return True

        # 진행 상황을 15초마다 남긴다. 본문 길이가 늘고 있으면 로딩 중,
        # 계속 짧으면 화면 자체가 안 뜨는 것 -> 원인 구분에 쓴다.
        if elapsed >= next_report:
            log(f"  대기 중... {elapsed:.0f}초 (본문 {await body_len(page)}자)")
            next_report += 15.0

        if time.monotonic() >= deadline:
            log(f"  EXCEL 버튼 대기 시간 초과 ({timeout_ms/1000:.0f}초, "
                f"본문 {await body_len(page)}자)")
            return False
        await page.wait_for_timeout(1500)


async def _click(page, selector: str, text: str, label: str) -> bool:
    """셀렉터로 먼저, 안 되면 텍스트로 클릭."""
    for how, loc in (
        (selector, page.locator(selector).filter(visible=True).first),
        (f"text:{text}", page.get_by_text(text, exact=True).filter(visible=True).first),
    ):
        try:
            if await loc.count() == 0:
                continue
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.click(timeout=4000)
            await page.wait_for_timeout(2500)
            log(f"  {label} 클릭 ({how})")
            return True
        except Exception:
            continue
    log(f"  {label} 클릭 실패")
    return False


async def nav_by_ui(page) -> bool:
    """URL 직행이 안 될 때 사이드바를 눌러서 찾아간다.

    셀렉터는 개발자도구로 확인된 값. Bubble 자동생성이라 재배포 시 바뀔 수 있어
    텍스트 클릭을 함께 시도한다.
    """
    log("  URL 직행 실패 -> 사이드바 클릭으로 시도")
    await _safe_goto(page, BASE, settle=5000)
    await _click(page, SEL_NAV_ANALYSIS, "분석", "분석 메뉴")
    await _click(page, SEL_TAB_AD, "광고", "광고 탭")
    await _click(page, SEL_TAB_CAMPAIGN, "캠페인", "캠페인 탭")
    return await wait_ready(page, timeout_ms=45000)


async def shown_range(page, timeout: int = 4000) -> tuple[str, str] | None:
    """화면 우상단에 표시된 기간을 읽는다. URL 이 먹혔는지 확인용."""
    try:
        loc = page.get_by_text(DATE_RANGE_RE).filter(visible=True).first
        txt = (await loc.text_content(timeout=timeout)) or ""
        m = DATE_RANGE_RE.search(txt)
        return (m.group(1), m.group(2)) if m else None
    except Exception:
        return None


async def ensure_day(page, day: str) -> bool:
    """URL 로 지정한 하루가 실제 적용됐는지 확인하고, 아니면 달력으로 설정."""
    cur = await shown_range(page)
    if cur == (day, day):
        log(f"  기간 URL 로 적용됨: {day}")
        return True

    log(f"  화면 기간 {cur} != 목표 ({day}) -> 달력으로 설정")
    if not await apply_date_range(page, day, day):
        return False

    after = await shown_range(page)
    if after != (day, day):
        log(f"  ! 달력 설정 후에도 기간 불일치: {after}")
        return False
    return True


async def _save(download, out_path: Path) -> Path:
    suffix = Path(download.suggested_filename).suffix or ".xlsx"
    target = out_path.with_suffix(suffix)
    await download.save_as(target)
    log(f"  다운로드 완료: {target.name}")
    return target


async def download_excel(page, out_path: Path) -> Path | None:
    """캠페인 화면의 EXCEL 버튼 클릭."""
    loc = await excel_button(page)
    if loc is None:
        log("  엑셀 다운로드 버튼을 찾지 못함")
        return None
    try:
        async with page.expect_download(timeout=180000) as dl:
            await loc.click(timeout=10000)
        return await _save(await dl.value, out_path)
    except Exception as e:
        log(f"  엑셀 다운로드 실패: {e}")
        return None


async def fetch_ads(days: list[str], out_dir: Path,
                    brands: list[str] | None = None) -> dict[tuple[str, str], Path]:
    """브랜드 × 날짜별로 캠페인 엑셀을 받아 {(브랜드, 날짜): 경로} 반환."""
    out_dir.mkdir(parents=True, exist_ok=True)
    brands = brands if brands is not None else (C.ADS_BRANDS or [C.ADS_BRAND])
    results: dict[tuple[str, str], Path] = {}

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

            jobs = [(b, d) for b in brands for d in days]
            for i, (brand, day) in enumerate(jobs, 1):
                tag = f"{brand}_{day}" if len(brands) > 1 else day
                log(f"[{i}/{len(jobs)}] {day}  (브랜드: {brand or '전체'})")
                url = build_url(day, brand)
                log(f"  이동: {url}")
                await _safe_goto(page, url, settle=3000)

                ready = await wait_ready(page)
                if not ready:
                    # Bubble 이 첫 로드에 실패하는 경우가 있다. 한 번 새로고침.
                    log("  새로고침 후 재시도")
                    try:
                        await page.reload(wait_until="commit", timeout=30000)
                    except Exception as e:
                        log(f"  새로고침 실패(무시): {e}")
                    await page.wait_for_timeout(3000)
                    ready = await wait_ready(page)

                if not ready:
                    # 마지막 수단: 사이드바 클릭 경로. 성공하면 기간은
                    # 아래 ensure_day 가 달력으로 맞춘다.
                    ready = await nav_by_ui(page)

                if not ready:
                    log(f"  광고 화면 판정 실패: {page.url}")
                    await page.screenshot(path=str(out_dir / f"err_screen_{tag}.png"))
                    await probe(page, out_dir, f"99_notads_{tag}")
                    continue

                # 브랜드는 계정 전역 설정이라 URL 파라미터만 믿으면 안 된다.
                # 전에는 아무것도 안 걸고 돌렸는데, 전역 설정이 마침
                # 코즈코즈였던 덕에 맞았을 뿐이다. 누가 시그로에서 바꾸면
                # 광고도 조용히 전사를 받는다. 매출·손익과 같은 함수로 건다.
                if brand and not await ensure_brand(page, brand):
                    await page.screenshot(path=str(out_dir / f"err_brand_{tag}.png"))
                    log("  -> 브랜드 확인 실패, 중단 (전사 데이터 유입 방지)")
                    return {}

                if not await ensure_day(page, day):
                    await page.screenshot(path=str(out_dir / f"err_date_{tag}.png"))
                    log("  -> 기간 설정 실패, 건너뜀 (잘못된 기간 수집 방지)")
                    continue

                if C.ADS_PROBE:
                    await probe(page, out_dir, f"ready_{tag}")
                    log("  [PROBE] 다운로드는 건너뜁니다.")
                    continue

                path = await download_excel(page, out_dir / f"광고_캠페인_{tag}")
                if path is None:
                    await page.screenshot(path=str(out_dir / f"err_excel_{tag}.png"))
                    await probe(page, out_dir, f"98_noexcel_{tag}")
                    continue
                results[(brand, day)] = path
        finally:
            await context.close()
            await browser.close()

    return results
