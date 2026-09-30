"""cigro 핵심이익지표 -> 공헌이익 / 매출이익 / 순이익 탭 데일리 리프레시.

주문(main.py) · 광고(ads_main.py) 와 독립적으로 돈다.
수집 기간은 주문과 동일한 전전월 1일 ~ 어제.
매일 지난 두 달을 다시 받으므로 월말·월초에 입력되는 변동 판관비가
언제 반영되든 다음 실행에서 따라온다.

엑셀 한 파일 안의 시트 3개를 각각 다른 탭에 기록한다.
한 탭이라도 실패하면 나머지도 쓰지 않는다 (지표 간 시점이 어긋나면 분석이 틀어짐).
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config as C
from . import drive
from . import pnl_sheets
from .cigro_pnl_scraper import fetch_pnl
from .cigro_scraper import log
from .main import notify


def read_sheet(path: Path, name: str, index: int):
    """엑셀에서 시트를 읽는다. 이름으로 못 찾으면 순서로 시도."""
    try:
        return pd.read_excel(path, sheet_name=name)
    except Exception:
        try:
            df = pd.read_excel(path, sheet_name=index)
            log(f"  [{name}] 시트명으로 못 찾아 {index}번째 시트를 사용")
            return df
        except Exception as e:
            log(f"  [{name}] 시트 읽기 실패: {e}")
            return None


def check_sga(plans) -> str:
    """변동판관비가 들어왔는지 확인해 알림 한 줄을 만든다.

    월말·월초에만 동작한다. 기록 '전'의 시트 값과 새로 받은 값을 비교하므로
    이 함수는 반드시 write() 호출 전에 불러야 한다.
    """
    today = datetime.now(C.KST).date()
    ym = pnl_sheets.sga_target_month(today)
    if not ym:
        return ""                      # 감시 구간이 아니다

    for tab, ws, header, merged, prev, existing, kept, n_new, xls_name, df, date_col in plans:
        if xls_name != "공헌이익":
            continue
        col = pnl_sheets.find_sga_column(header)
        if not col:
            log(f"[판관비] '{tab}' 헤더에서 변동판관비 열을 찾지 못함 — 확인 생략")
            return f"⚠️ 변동판관비 열을 찾지 못했습니다 ({ym})"

        before = pnl_sheets.month_sum_rows(existing, header, col, ym)
        after = pnl_sheets.month_sum_df(df, date_col, col, ym)
        delta = after - before
        log(f"[판관비] {ym} '{col}' 기존 {before:,.0f} -> 수집 {after:,.0f} "
            f"(변동 {delta:+,.0f})")

        if abs(delta) < 1:
            return (f"🔎 변동판관비 확인 필요 — {ym} 변동 없음 "
                    f"(현재 {after:,.0f}원)")
        return f"💰 변동판관비 {delta:+,.0f}원 확인 — {ym} (현재 {after:,.0f}원)"

    return ""


def main() -> int:
    C.validate_pnl()
    start, end = C.pnl_period()
    out_dir = Path(C.PNL_OUT_DIR)
    run_day = datetime.now(C.KST).strftime("%Y%m%d")

    log(f"실행: {run_day} / 수집 기간: {start} ~ {end}")
    log(f"브랜드: {C.PNL_BRAND or '(전체)'} / 탭: {', '.join(C.PNL_TABS.values())}")
    log(f"DRY_RUN={C.DRY_RUN} / PROBE={C.PNL_PROBE} / FILL_DATEPARTS={C.PNL_FILL_DATEPARTS}")

    # ── 다운로드 ─────────────────────────────────────────────────
    path = asyncio.run(fetch_pnl(start, end, out_dir))

    if C.PNL_PROBE:
        log("[PROBE] 화면 덤프만 수행하고 종료합니다. artifact 를 확인하세요.")
        return 0

    if path is None:
        notify(f"❌ cigro 손익 리프레시 실패\n"
               f"기간: {start} ~ {end}\n"
               f"엑셀 다운로드 실패. artifact 의 스크린샷/probe 파일 확인")
        return 1

    # ── 시트별 읽기 ──────────────────────────────────────────────
    frames, missing_date = {}, []
    for idx, (xls_name, tab) in enumerate(C.PNL_TABS.items()):
        df = read_sheet(path, xls_name, idx)
        if df is None:
            notify(f"❌ cigro 손익 리프레시 중단\n"
                   f"엑셀에서 '{xls_name}' 시트를 읽지 못했습니다.")
            return 1
        date_col = pnl_sheets.find_date_column(df)
        if date_col is None:
            missing_date.append(f"{xls_name}({', '.join(str(c) for c in df.columns)})")
            continue
        frames[xls_name] = (df, date_col)
        df.to_csv(out_dir / f"{xls_name}.csv", index=False, encoding="utf-8-sig")
        log(f"[{xls_name}] {len(df):,}행 × {len(df.columns)}열 / 날짜열='{date_col}'")

    if missing_date:
        notify(f"❌ cigro 손익 리프레시 중단\n"
               f"날짜 열을 찾지 못한 시트가 있습니다.\n"
               + "\n".join(f"• {m}" for m in missing_date)
               + f"\n찾는 이름: {', '.join(C.PNL_DATE_HEADERS)}\n"
                 f"PNL_DATE_HEADERS 환경변수로 지정할 수 있습니다.")
        return 1

    total = sum(len(df) for df, _ in frames.values())
    if total == 0:
        notify(f"⚠️ cigro 손익 리프레시 중단\n"
               f"기간: {start} ~ {end}\n수집 0행 - 시트 미반영")
        return 1

    if C.DRY_RUN:
        log("[DRY_RUN] 시트 미반영")
        lines = [f"🧪 cigro 손익 dry-run", f"기간: {start} ~ {end}"]
        for name, (df, dcol) in frames.items():
            lines.append(f"• {name}: {len(df):,}행 / 열: "
                         + ", ".join(str(c) for c in df.columns))
        notify("\n".join(lines))
        return 0

    # ── 시트 병합 (3개 탭 전부 검증한 뒤에 쓴다) ──────────────────
    gc = pnl_sheets.client()
    plans = []
    for xls_name, (df, date_col) in frames.items():
        tab = C.PNL_TABS[xls_name]
        ws = pnl_sheets.worksheet(gc, C.PNL_SHEET_ID, tab)
        header, existing = pnl_sheets.read_existing(ws)

        if not header or not str(header[0]).strip():
            notify(f"❌ cigro 손익 리프레시 중단\n"
                   f"'{tab}' 탭 1행에 헤더가 없습니다.")
            return 1

        miss = pnl_sheets.check_columns(df, header)
        if miss:
            notify(f"❌ cigro 손익 리프레시 중단\n"
                   f"[{tab}] 엑셀에 없는 시트 열: {', '.join(miss)}\n"
                   f"cigro 화면 구성이 바뀌었을 수 있습니다.")
            log(f"[{tab}] 열 불일치 -> 중단: {miss}")
            return 1

        new_rows = pnl_sheets.to_rows(df, header, date_col)
        merged, kept = pnl_sheets.merge(existing, new_rows, start, end)
        prev = len(existing)

        if prev > 0 and len(merged) < prev * C.SHRINK_GUARD and not C.FORCE_WRITE:
            notify(f"⚠️ cigro 손익 리프레시 중단 (급감 가드)\n"
                   f"[{tab}] 기존 {prev:,}행 -> 병합 후 {len(merged):,}행\n"
                   f"정상이면 force_write=true 로 재실행")
            log(f"[{tab}] 급감 가드 발동: {prev:,} -> {len(merged):,}")
            return 1

        plans.append((tab, ws, header, merged, prev, existing, kept, len(new_rows),
                      xls_name, df, date_col))

    # 여기까지 왔으면 세 탭 모두 안전하다. 이제 기록한다.
    sga_line = check_sga(plans)

    lines = ["✅ cigro 손익 리프레시", f"기간: {start} ~ {end}"]
    for tab, ws, header, merged, prev, existing, kept, n_new, *_ in plans:
        log(f"[{tab}] 기존 {prev:,}행")
        pnl_sheets.write(ws, header, merged, prev, existing=existing)
        lines.append(f"• {tab}: 보존 {kept:,} + 교체 {n_new:,} = {len(merged):,}행 "
                     f"(직전 {prev:,})")
        log(f"[{tab}] 기록 완료: {len(merged):,}행")

    if sga_line:
        lines.append(sga_line)

    # ── 드라이브 원본 보관 ───────────────────────────────────────
    if C.DRIVE_FOLDER_ID:
        try:
            svc = drive.service()
            stem = C.PNL_PREFIX + (f"{start}_{end}" if C.DRIVE_KEEP_HISTORY else "핵심이익지표")
            drive.upload(svc, C.DRIVE_FOLDER_ID, path, stem + path.suffix)
            lines.append("📁 드라이브 원본 보관")
        except Exception as e:
            log(f"드라이브 업로드 실패: {e}")

    notify("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
