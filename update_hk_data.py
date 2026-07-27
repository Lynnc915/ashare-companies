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

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "hk_data.json"

BASE_URL = "https://33.push2.eastmoney.com/api/qt/clist/get"
FS = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"
FIELDS = "f12,f14,f20,f26,f100"
PAGE_SIZE = 100


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


@retry(max_attempts=3, base_delay=2.0)
def fetch_base_list() -> pd.DataFrame:
    """通過東方財富批量接口獲取港股通成分股基本信息。"""
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
        res = requests.get(BASE_URL, params=params, timeout=30)
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

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["list_date"] = df["list_date_raw"].apply(parse_date)
    df["industry"] = df["industry"].fillna("-").replace("", "-")
    return df[["code", "name", "market_cap", "list_date", "industry"]]


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
            "board": "创业板" if str(row["code"]).startswith("08") else "主板",
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

    records.sort(key=lambda r: r["code"])

    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "count": len(records),
        "source_name": "东方财富 / akshare / HKEX",
        "source_url": "https://www.hkex.com.hk",
        "years": [],
        "data": records,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已保存 {len(records)} 條記錄到 {DATA_FILE}")


if __name__ == "__main__":
    main()
