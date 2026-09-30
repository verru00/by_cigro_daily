"""cigro 광고(캠페인) -> '광고 RAW' 시트 데일리 리프레시.

주문 리프레시(main.py)와 독립적으로 돈다.
전일 하루, 월요일이면 전전일+전일 이틀. 화면이 기간을 합산하므로 하루씩 받는다.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from . import ads_sheets
from . import config as C
from . import drive
from .cigro_ads_scraper import fetch_ads
from .cigro_scraper import log
from .main import notify, read_table

# 기존 시트에서 A열 날짜를 이 비율 이상 못 읽으면 중단한다.
# (표시 형식이 예상과 달라 교체가 안 되고 중복이 쌓이는 사고 방지)
BAD_DATE_LIMIT = 0.5


def main() -> int:
    C.validate_ads()
    days = C.ads_dates()
    run_day = datetime.now(C.KST).strftime("%Y%m%d")
    weekday = "월화수목금토일"[datetime.now(C.KST).weekday()]
    out_dir = Path(C.ADS_OUT_DIR)

    log(f"실행: {run_day} ({weekday}) / 수집 날짜: {', '.join(days)}"
        + ("  [월요일 보정 - 이틀]" if len(days) > 1 else ""))
    brands = C.ADS_BRANDS or [C.ADS_BRAND]
    multi = len(brands) > 1          # 여러 브랜드면 '브랜드' 열로 구분
    log(f"브랜드: {', '.join(b or '(전체)' for b in brands)} / 탭: {C.ADS_SHEET_TAB}")
    log(f"DRY_RUN={C.DRY_RUN} / PROBE={C.ADS_PROBE} / "
        f"FILL_FORMULAS={C.ADS_FILL_FORMULAS}")

    # ── 다운로드 (브랜드 × 하루씩) ───────────────────────────────
    files = asyncio.run(fetch_ads(days, out_dir, brands))

    if C.ADS_PROBE:
        log("[PROBE] 화면 덤프만 수행하고 종료합니다. artifact 를 확인하세요.")
        return 0

    jobs = [(b, d) for b in brands for d in days]
    missing = [f"{b} {d}" if multi else d for b, d in jobs if (b, d) not in files]
    if missing:
        # 일부만 반영하면 그 날짜 행이 통째로 비게 된다. 전부 성공해야 쓴다.
        notify(f"❌ cigro 광고 리프레시 실패\n"
               f"다운로드 실패: {', '.join(missing)}\n"
               f"시트 미반영. artifact 의 스크린샷/probe 파일 확인")
        log(f"다운로드 실패 -> 중단: {missing}")
        return 1

    # ── 수집 데이터 취합 ─────────────────────────────────────────
    frames = {}
    for b, day in jobs:
        df = read_table(files[(b, day)])
        frames[(b, day)] = df
        name = f"광고_{b}_{day}.csv" if multi else f"광고_{day}.csv"
        df.to_csv(out_dir / name, index=False, encoding="utf-8-sig")
        log(f"[{b} {day}] {len(df):,}행 × {len(df.columns)}열")

    total = sum(len(d) for d in frames.values())
    if total == 0:
        notify(f"⚠️ cigro 광고 리프레시 중단\n"
               f"날짜: {', '.join(days)}\n수집 0행 - 시트 미반영")
        return 1

    first = frames[jobs[0]]
    if C.DRY_RUN:
        cols = list(first.columns)
        log(f"[DRY_RUN] 시트 미반영. 엑셀 열({len(cols)}개): {cols}")
        notify(f"🧪 cigro 광고 dry-run\n날짜: {', '.join(days)}\n"
               + "\n".join(f"• {b} {d}: {len(frames[(b, d)]):,}행" for b, d in jobs)
               + f"\n엑셀 열: {', '.join(str(c) for c in cols)}")
        return 0

    # ── 시트 병합 ────────────────────────────────────────────────
    gc = ads_sheets.client()
    ws = ads_sheets.worksheet(gc, C.ADS_SHEET_ID, C.ADS_SHEET_TAB)
    brand_col = ads_sheets.ensure_brand_col(ws, C.ADS_BRAND_HEADER) if multi else None
    if multi:
        header, existing, existing_brands = ads_sheets.read_existing(ws, brand_col)
        log(f"브랜드 열: {ads_sheets.col_letter(brand_col)}열")
    else:
        header, existing = ads_sheets.read_existing(ws)
        existing_brands = None
    prev = len(existing)
    log(f"기존 시트: {prev:,}행")

    if not header or not str(header[0]).strip():
        notify(f"❌ cigro 광고 리프레시 중단\n"
               f"'{C.ADS_SHEET_TAB}' 탭 1행에 헤더가 없습니다.")
        return 1

    # A열 날짜를 못 읽으면 교체가 안 되고 같은 날 데이터가 계속 쌓인다.
    bad = ads_sheets.unparseable_ratio(existing)
    if bad > BAD_DATE_LIMIT:
        sample = [r[0] for r in existing[:3]]
        notify(f"❌ cigro 광고 리프레시 중단\n"
               f"기존 행의 {bad:.0%} 가 A열('{header[0]}') 날짜를 읽지 못합니다.\n"
               f"예시: {sample}\n"
               f"교체가 안 되고 중복이 쌓이므로 중단합니다.")
        log(f"A열 날짜 파싱 실패율 {bad:.0%} -> 중단. 예시={sample}")
        return 1

    missing_cols = ads_sheets.check_columns(first, header)
    if missing_cols:
        notify(f"❌ cigro 광고 리프레시 중단\n"
               f"엑셀에 없는 시트 열: {', '.join(missing_cols)}\n"
               f"cigro 화면 구성이 바뀌었을 수 있습니다.")
        log(f"열 불일치 -> 중단: {missing_cols}")
        return 1

    new_rows, new_brands = [], []
    for b, day in jobs:
        rows = ads_sheets.to_rows(frames[(b, day)], header, day)
        new_rows.extend(rows)
        new_brands.extend([b] * len(rows))

    if multi:
        merged, merged_brands, kept = ads_sheets.merge_branded(
            existing, existing_brands, new_rows, new_brands, days, brands,
            legacy=C.ADS_LEGACY_BRAND)
    else:
        merged, kept = ads_sheets.merge(existing, new_rows, days)
        merged_brands = None
    replaced = prev - kept

    # 급감 가드: 병합 결과가 기존보다 크게 줄면 쓰지 않는다
    if prev > 0 and len(merged) < prev * C.SHRINK_GUARD and not C.FORCE_WRITE:
        notify(f"⚠️ cigro 광고 리프레시 중단 (급감 가드)\n"
               f"기존 {prev:,}행 -> 병합 후 {len(merged):,}행\n"
               f"정상이면 force_write=true 로 재실행")
        log(f"급감 가드 발동: {prev:,} -> {len(merged):,}")
        return 1

    written = ads_sheets.write(ws, header, merged, prev,
                               C.ADS_FILL_FORMULAS, existing=existing,
                               brand_col=brand_col, brands=merged_brands,
                               existing_brands=existing_brands)
    log(f"기록 완료: 보존 {kept:,} + 신규 {len(new_rows):,} = {len(merged):,}행 "
        f"(실제 기록 {written:,}행)")

    # ── 드라이브 원본 보관 ───────────────────────────────────────
    uploaded = 0
    if C.DRIVE_FOLDER_ID:
        try:
            svc = drive.service()
            for b, day in jobs:
                stem = f"{C.ADS_PREFIX}캠페인" + (f"_{b}" if multi else "")
                stem += f"_{day}" if C.DRIVE_KEEP_HISTORY else ""
                drive.upload(svc, C.DRIVE_FOLDER_ID, files[(b, day)],
                             stem + files[(b, day)].suffix)
                uploaded += 1
        except Exception as e:
            log(f"드라이브 업로드 실패: {e}")

    lines = ["✅ cigro 광고 캠페인 리프레시",
             f"날짜: {', '.join(days)}" + (" (월요일 보정)" if len(days) > 1 else "")]
    lines += [f"• {b} {d}: {len(frames[(b, d)]):,}행" if multi else f"• {d}: {len(frames[(b, d)]):,}행"
              for b, d in jobs]
    lines.append(f"시트[{C.ADS_SHEET_TAB}]: 보존 {kept:,} + 교체 {len(new_rows):,} "
                 f"= {len(merged):,}행 (직전 {prev:,}행, 제거 {replaced:,})")
    if uploaded:
        lines.append(f"📁 드라이브 {uploaded}건 보관")
    notify("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
