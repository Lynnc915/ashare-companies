#!/usr/bin/env python3
"""
抓取港股通成分股數據，生成 data/hk_data.json。

數據源：
- 基礎列表：akshare.stock_hk_ggt_components_em（港股通成分股）
- 上市信息：akshare.stock_hk_security_profile_em（上市日期、板塊）
- 公司資料：akshare.stock_hk_company_profile_em（行業、主營業務）
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "hk_data.json"


def parse_date(value: Any) -> str | None:
    """將多種日期格式統一為 YYYY-MM-DD。"""
    if pd.isna(value) or value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
    # 嘗試只取日期部分
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass
    return None


def fetch_base_list() -> pd.DataFrame:
    """獲取港股通成分股基礎列表。"""
    import akshare as ak

    df = ak.stock_hk_ggt_components_em()
    df = df.rename(columns={"代码": "code", "名称": "name"})
    df["code"] = df["code"].astype(str).str.strip().str.zfill(5)
    return df[["code", "name"]]


def fetch_security_profile(code: str) -> dict[str, Any]:
    """獲取港股上市信息（上市日期、板塊等）。"""
    import akshare as ak

    result: dict[str, Any] = {
        "list_date": None,
        "board": "主板",
        "market_cap": None,
        "market_cap_currency": "HKD",
    }
    try:
        df = ak.stock_hk_security_profile_em(symbol=code)
        if df.empty:
            return result
        # df 通常是兩列：項目、值
        if df.shape[1] >= 2:
            mapping = {}
            for _, row in df.iterrows():
                key = str(row.iloc[0]).strip()
                val = row.iloc[1]
                mapping[key] = val

            result["list_date"] = parse_date(mapping.get("上市日期"))
            board = str(mapping.get("板块", "主板")).strip()
            result["board"] = "创业板" if "GEM" in board.upper() or "創業" in board or "创业板" in board else "主板"

            # 市值字段可能有多種名稱
            for cap_key in ("总市值", "港股市值", "市值"):
                if cap_key in mapping:
                    try:
                        result["market_cap"] = float(mapping[cap_key])
                        break
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print(f"[WARN] {code} security_profile 失敗: {e}", file=sys.stderr)
    return result


def fetch_company_profile(code: str) -> dict[str, Any]:
    """獲取港股公司資料（行業、主營業務等）。"""
    import akshare as ak

    result: dict[str, Any] = {
        "industry": None,
        "main_business": None,
        "name_en": None,
    }
    try:
        df = ak.stock_hk_company_profile_em(symbol=code)
        if df.empty or df.shape[1] < 2:
            return result
        mapping = {}
        for _, row in df.iterrows():
            key = str(row.iloc[0]).strip()
            val = row.iloc[1]
            mapping[key] = val

        result["industry"] = str(mapping.get("所属行业", "")).strip() or None
        if not result["industry"]:
            result["industry"] = str(mapping.get("行業", "")).strip() or None

        result["main_business"] = str(mapping.get("公司介绍", "")).strip() or None
        if not result["main_business"]:
            result["main_business"] = str(mapping.get("公司簡介", "")).strip() or None

        result["name_en"] = str(mapping.get("英文名称", "")).strip() or None
        if not result["name_en"]:
            result["name_en"] = str(mapping.get("公司名稱", "")).strip() or None
    except Exception as e:
        print(f"[WARN] {code} company_profile 失敗: {e}", file=sys.stderr)
    return result


def enrich_stock(row: pd.Series) -> dict[str, Any]:
    """合併基礎資料、上市信息、公司資料。"""
    code = row["code"]
    name = str(row["name"]).strip()

    security = fetch_security_profile(code)
    sleep(0.3)  # 禮貌延遲，避免請求過快
    company = fetch_company_profile(code)

    record = {
        "code": code,
        "name": name,
        "name_en": company.get("name_en") or None,
        "list_date": security.get("list_date"),
        "board": security.get("board") or "主板",
        "industry": company.get("industry") or "-",
        "main_business": company.get("main_business") or "-",
        "market_cap": security.get("market_cap"),
        "market_cap_currency": "HKD",
    }
    return record


def main():
    parser = argparse.ArgumentParser(description="抓取港股通數據")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="僅抓取前 N 條記錄（用於測試）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="並發線程數（默認 4）",
    )
    args = parser.parse_args()

    print("[*] 正在獲取港股通成分股列表...")
    base_df = fetch_base_list()
    if args.limit:
        base_df = base_df.head(args.limit)
    print(f"[*] 共 {len(base_df)} 只股票待處理")

    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_code = {executor.submit(enrich_stock, row): row["code"] for _, row in base_df.iterrows()}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code = future_to_code[future]
            try:
                record = future.result()
                records.append(record)
            except Exception as e:
                print(f"[WARN] {code} 處理失敗: {e}", file=sys.stderr)
                records.append({
                    "code": code,
                    "name": str(base_df.loc[base_df["code"] == code, "name"].values[0]),
                    "list_date": None,
                    "board": "主板",
                    "industry": "-",
                    "main_business": "-",
                })
            if i % 50 == 0 or i == len(base_df):
                print(f"  進度 {i}/{len(base_df)}")

    # 按代碼排序
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
