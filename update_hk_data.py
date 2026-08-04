#!/usr/bin/env python3
"""
抓取 2024 年以來香港新上市企業數據，生成 data/hk_data.json。

數據源：東方財富香港全部股票批量接口（主板 + 創業板）
- f12: 代碼
- f14: 名稱
- f20: 總市值（港元）
- f26: 上市日期（YYYYMMDD）
- f100: 所屬行業
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any

warnings.filterwarnings("ignore")

import pandas as pd
import requests

# 統一 session：不讀取環境/系統代理，避免本地代理工具未運行時連接失敗
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
})

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "hk_data.json"

BASE_URL = "https://33.push2.eastmoney.com/api/qt/clist/get"
BACKUP_HOSTS = [
    "https://72.push2.eastmoney.com/api/qt/clist/get",
    "https://73.push2.eastmoney.com/api/qt/clist/get",
]
# 同时抓取香港主板与创业板（GEM）：m:128 为港股大市场，t:1/t:2 为主板相关，t:3/t:4 为创业板相关
# 注意 Eastmoney 接口要求子市场之间用空格分隔（与 akshare 保持一致）
FS = "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2"
FIELDS = "f12,f14,f20,f26,f100"
PAGE_SIZE = 100


def classify_hk_board(code: str) -> str:
    """根据港股代码判断板块：08xxx 归为创业板（GEM），其余为主板。"""
    if not code:
        return "主板"
    c = str(code).strip().zfill(5)
    # 港股创业板代码传统区间为 08000-08999
    if c.startswith("08"):
        return "创业板"
    return "主板"


def parse_date(value: Any) -> str | None:
    """將多種日期格式統一為 YYYY-MM-DD。"""
    if pd.isna(value) or value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            # 對於純日期格式，嘗試只取前 10 個字元（去掉時間部分）
            if "%H" not in fmt:
                try:
                    return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            continue
    # 嘗試只取日期部分
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass
    return None


def retry(max_attempts: int = 3, base_delay: float = 1.0):
    """簡單的指數退避重試裝飾器。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts:
                        raise
                    sleep_time = base_delay * (2 ** (attempt - 1))
                    print(
                        f"[WARN] {func.__name__} 第 {attempt} 次失敗: {e}；{sleep_time:.1f}s 后重試",
                        file=sys.stderr,
                    )
                    sleep(sleep_time)

        return wrapper

    return decorator


def _fetch_base_list_one_host(url: str) -> list[dict[str, Any]]:
    """從指定 Eastmoney 主機抓取全部港股列表（分頁）。"""
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "pn": page,
            "pz": PAGE_SIZE,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "fid": "f12",
            "fs": FS,
            "fields": FIELDS,
            "_": int(datetime.now().timestamp() * 1000),
        }
        res = SESSION.get(url, params=params, timeout=30)
        res.raise_for_status()
        payload = res.json()
        diff = payload.get("data", {}).get("diff", [])
        if not diff:
            break

        for item in diff:
            records.append({
                "code": str(item.get("f12", "")).strip().zfill(5),
                "name": str(item.get("f14", "")).strip(),
                "market_cap": item.get("f20"),
                "list_date_raw": item.get("f26"),
                "industry": str(item.get("f100", "")).strip() or None,
            })

        if len(diff) < PAGE_SIZE:
            break
        page += 1
        sleep(0.3)
    return records


@retry(max_attempts=2, base_delay=2.0)
def fetch_base_list() -> pd.DataFrame:
    """通過東方財富批量接口獲取全部香港上市公司基本信息（主板 + 創業板）。

    依次嘗試多個 Eastmoney push2 主機，任一主機成功即返回。
    """
    last_error: Exception | None = None
    for url in [BASE_URL] + BACKUP_HOSTS:
        try:
            print(f"[*] 嘗試從 {url} 獲取港股列表...")
            records = _fetch_base_list_one_host(url)
            print(f"[*] 從 {url} 獲取 {len(records)} 條記錄")
            if records:
                df = pd.DataFrame(records)
                df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
                df["list_date"] = df["list_date_raw"].apply(parse_date)
                df["industry"] = df["industry"].fillna("-").replace("", "-")
                return df[["code", "name", "market_cap", "list_date", "industry"]]
        except Exception as e:
            last_error = e
            print(f"[WARN] {url} 獲取失敗: {e}", file=sys.stderr)
            continue
    raise last_error or ConnectionError("所有 Eastmoney 主機均無法獲取港股列表")


@retry(max_attempts=3, base_delay=2.0)
def fetch_financial_data(code: str) -> dict[str, dict[str, float | None]]:
    """通過東方財富數據中心獲取港股年度營收/淨利潤。"""
    url = "https://datacenter.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_HKF10_FN_GMAININDICATOR",
        "columns": "ALL",
        "filter": f'(SECUCODE="{code}.HK")',
        "pageNumber": 1,
        "pageSize": 12,
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
    }
    res = SESSION.get(url, params=params, timeout=20)
    res.raise_for_status()
    payload = res.json()
    items = payload.get("result", {}).get("data", [])

    finance: dict[str, dict[str, float | None]] = {}
    for item in items:
        report_date = str(item.get("REPORT_DATE", ""))[:10]
        date_type = str(item.get("DATE_TYPE", ""))
        if not report_date:
            continue
        # 只取年報數據（DATE_TYPE 為年報，或報告期為 12-31）
        if date_type != "年报" and not report_date.endswith("12-31"):
            continue
        year = report_date[:4]
        revenue = item.get("OPERATE_INCOME")
        profit = item.get("HOLDER_PROFIT")
        if revenue is None and profit is None:
            continue
        finance[year] = {
            "revenue": float(revenue) if revenue is not None else None,
            "profit": float(profit) if profit is not None else None,
        }
    return finance


def enrich_finance(record: dict[str, Any]) -> dict[str, Any]:
    """為單條記錄補充財務數據。"""
    code = record["code"]
    try:
        finance = fetch_financial_data(code)
        record["finance"] = finance
    except Exception as e:
        print(f"[WARN] {code} 財務數據獲取失敗: {e}", file=sys.stderr)
        record["finance"] = {}
    sleep(0.15)
    return record


def main():
    parser = argparse.ArgumentParser(description="抓取香港新上市企業數據")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="僅保留前 N 條新上市記錄（用於測試）",
    )
    parser.add_argument(
        "--since",
        type=str,
        default="2024-01-01",
        help="僅保留上市日期在此日期之后的新上市公司，默認 2024-01-01",
    )
    args = parser.parse_args()

    since = parse_date(args.since)
    if since is None:
        print(f"[ERROR] --since 日期格式無法識別: {args.since}", file=sys.stderr)
        sys.exit(1)

    print("[*] 正在獲取香港上市公司列表...")
    base_df = fetch_base_list()
    if base_df.empty:
        print("[ERROR] 未獲取到任何數據", file=sys.stderr)
        sys.exit(1)

    print(f"[*] 共 {len(base_df)} 只股票待處理")

    records: list[dict[str, Any]] = []
    for _, row in base_df.iterrows():
        records.append({
            "code": row["code"],
            "name": row["name"],
            "name_en": None,
            "list_date": row["list_date"],
            "board": classify_hk_board(row["code"]),
            "industry": row["industry"] if row["industry"] and row["industry"] != "-" else "-",
            "main_business": "-",
            "market_cap": float(row["market_cap"]) if pd.notna(row["market_cap"]) else None,
            "market_cap_currency": "HKD",
        })

    # 按上市日期過濾，僅保留 2024 年以來的新上市公司
    filtered = [r for r in records if r.get("list_date") and r["list_date"] >= since]
    if len(filtered) != len(records):
        print(f"[*] 按上市日期 {since} 過濾后保留 {len(filtered)} 條記錄")
    records = filtered

    if args.limit:
        records = records[:args.limit]

    # 補充年度財務數據
    print("[*] 正在補充財務數據...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_to_code = {executor.submit(enrich_finance, r): r["code"] for r in records}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code = future_to_code[future]
            try:
                future.result()
            except Exception as e:
                print(f"[WARN] {code} 財務數據處理失敗: {e}", file=sys.stderr)
            if i % 20 == 0 or i == len(records):
                print(f"  財務進度 {i}/{len(records)}")

    records.sort(key=lambda r: r["code"])

    # 收集所有財務年份
    years = sorted({
        year
        for r in records
        for year in (r.get("finance") or {}).keys()
    })

    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "count": len(records),
        "source_name": "东方财富 / akshare / HKEX",
        "source_url": "https://www.hkex.com.hk",
        "years": years,
        "data": records,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已保存 {len(records)} 條記錄到 {DATA_FILE}")


if __name__ == "__main__":
    main()
