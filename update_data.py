#!/usr/bin/env python3
"""
A 股上市企业信息数据更新脚本（增强版）

功能：
1. 抓取 A 股上市企业基础信息（代码、名称、上市日期等）
2. 根据代码细分板块（沪市主板、科创板、深市主板、创业板、北交所等）
3. 抓取最近 3 年（2022-2024）年度营业总收入和净利润
4. 生成前端可用的 data.json
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import akshare as ak
import pandas as pd

# 项目根目录
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "data.json"

# 默认只保留 2022 年及以后上市的企业
DEFAULT_SINCE = "2022-01-01"

# 财务报表年份
FINANCE_YEARS = [2022, 2023, 2024]

# 字段映射：把 akshare 的不同列名统一为前端使用的名称
COLUMN_MAP = {
    # 代码
    "代码": "code",
    "股票代码": "code",
    "证券代码": "code",
    "A股代码": "code",
    # 名称
    "名称": "name",
    "股票名称": "name",
    "证券简称": "name",
    "A股简称": "name",
    "股票简称": "name",
    # 上市日期
    "上市日期": "list_date",
    "上市时间": "list_date",
    "A股上市日期": "list_date",
    # 交易所/板块
    "交易所": "exchange",
    "板块": "exchange",
    # 行业
    "所属行业": "industry",
    "行业": "industry",
    "所处行业": "industry",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名，便于后续处理。"""
    rename_map = {}
    for col in df.columns:
        key = COLUMN_MAP.get(col)
        if key and key not in rename_map.values():
            rename_map[col] = key
    return df.rename(columns=rename_map).copy()


def parse_date(value):
    """把各种日期格式解析为 YYYY-MM-DD 字符串，解析失败返回 None。"""
    if pd.isna(value):
        return None
    value = str(value).strip()
    if not value or value in {"-", "NaT", "None", "nan"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def classify_board(code: str) -> tuple[str, str]:
    """
    根据 6 位股票代码判断所属市场和细分板块。
    返回 (market, board)。
    """
    code = str(code).strip().zfill(6)
    prefix3 = code[:3]
    prefix2 = code[:2]

    if prefix3 in ("600", "601", "603", "605"):
        return "上交所", "沪市主板"
    if prefix3 == "688" or prefix3 == "689":
        return "上交所", "科创板"
    if prefix3 in ("000", "001", "002", "003", "004"):
        return "深交所", "深市主板"
    if prefix3 in ("300", "301"):
        return "深交所", "创业板"
    if prefix3 == "920":
        return "北交所", "北交所"
    if prefix2 in ("43", "83", "87", "88"):
        return "新三板", "新三板"

    return "其他", "其他"


def fetch_stock_info() -> pd.DataFrame:
    """
    尝试多种 akshare 接口获取 A 股上市企业信息。
    优先使用包含上市日期的全市场接口，否则拼接各交易所数据。
    """
    errors = []

    # 方案 1：ak.stock_info() 通常包含全市场A股及上市日期
    try:
        df = ak.stock_info()
        df = normalize_columns(df)
        if "code" in df.columns and "name" in df.columns:
            print(f"[OK] 通过 ak.stock_info() 获取到 {len(df)} 条记录")
            return df
    except Exception as e:
        errors.append(f"ak.stock_info(): {e}")

    # 方案 2：分别获取沪、深、北交所数据后合并
    parts = []
    fetchers = {
        "上交所主板": lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
        "科创板": lambda: ak.stock_info_sh_name_code(symbol="科创板"),
        "深交所": lambda: ak.stock_info_sz_name_code(),
        "北交所": lambda: ak.stock_info_bj_name_code(),
    }
    for exchange, fetcher in fetchers.items():
        try:
            part = fetcher()
            part = normalize_columns(part)
            if "exchange" not in part.columns:
                part["exchange"] = exchange
            parts.append(part)
            print(f"[OK] 通过 {exchange} 接口获取到 {len(part)} 条记录")
        except Exception as e:
            errors.append(f"{exchange} 接口: {e}")

    if parts:
        df = pd.concat(parts, ignore_index=True, sort=False)
        df = normalize_columns(df)
        return df

    # 方案 3：最后兜底，只拿代码和名称
    try:
        df = ak.stock_info_a_code_name()
        df = normalize_columns(df)
        df["exchange"] = "未知"
        print(f"[WARN] 仅获取到代码和名称，共 {len(df)} 条记录")
        return df
    except Exception as e:
        errors.append(f"ak.stock_info_a_code_name(): {e}")

    raise RuntimeError("所有数据源均失败:\n" + "\n".join(errors))


def fetch_finance_yearly(year: int) -> dict[str, dict]:
    """
    使用 ak.stock_yjbb_em 批量获取某一年度全部 A 股的业绩摘要。
    返回 {code: {revenue, profit}}，单位为原始元。
    """
    date_str = f"{year}1231"
    print(f"[*] 正在抓取 {year} 年度业绩摘要...")
    try:
        df = ak.stock_yjbb_em(date=date_str)
    except Exception as e:
        print(f"[WARN] {year} 年度业绩摘要抓取失败: {e}")
        return {}

    result = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代码", "")).strip().zfill(6)
        if not code:
            continue

        revenue = row.get("营业总收入-营业总收入")
        profit = row.get("净利润-净利润")

        # 处理 NaN / None
        if pd.isna(revenue):
            revenue = None
        if pd.isna(profit):
            profit = None

        result[code] = {
            "revenue": float(revenue) if revenue is not None else None,
            "profit": float(profit) if profit is not None else None,
        }

    print(f"[OK] {year} 年度业绩摘要共 {len(result)} 条")
    return result


def fetch_finance_single(stock_prefix: str, code: str) -> dict[str, dict]:
    """
    单个企业兜底抓取：使用新浪财经财务报告接口。
    stock_prefix 为 sh/sz/bj。
    返回 {year: {revenue, profit}}。
    """
    result = {}
    try:
        df = ak.stock_financial_report_sina(stock=f"{stock_prefix}{code}", symbol="利润表")
        df = normalize_columns(df)
        if "报告日期" not in df.columns:
            return result

        # 筛选年度数据
        df["year"] = df["报告日期"].astype(str).str[:4]
        df = df[df["报告日期"].astype(str).str.endswith("-12-31")]

        for _, row in df.iterrows():
            year = row["year"]
            if int(year) not in FINANCE_YEARS:
                continue

            # 优先使用营业总收入，不存在则用营业收入
            revenue = None
            for col in ["营业总收入", "营业收入", "TOTAL_OPERATE_INCOME", "OPERATE_INCOME"]:
                if col in row and not pd.isna(row[col]):
                    revenue = float(row[col])
                    break

            profit = None
            for col in ["净利润", "NETPROFIT", "PARENT_NETPROFIT"]:
                if col in row and not pd.isna(row[col]):
                    profit = float(row[col])
                    break

            result[year] = {"revenue": revenue, "profit": profit}

    except Exception as e:
        # 单企业失败静默处理，避免日志刷屏
        pass

    return result


def build_finance_data(codes: list[str]) -> dict[str, dict]:
    """
    为所有企业构建最近 3 年财务数据。
    优先使用批量接口，缺失的再用单企业接口补全。
    """
    finance_by_year = {}
    for year in FINANCE_YEARS:
        finance_by_year[str(year)] = fetch_finance_yearly(year)
        time.sleep(0.5)  #  polite delay

    result = {}
    for code in codes:
        code = code.zfill(6)
        record = {}
        missing_years = []

        for year in FINANCE_YEARS:
            year_str = str(year)
            data = finance_by_year[year_str].get(code)
            if data and (data["revenue"] is not None or data["profit"] is not None):
                record[year_str] = data
            else:
                record[year_str] = {"revenue": None, "profit": None}
                missing_years.append(year)

        # 对缺失年份使用单企业接口补全
        if missing_years:
            prefix3 = code[:3]
            if prefix3 in ("600", "601", "603", "605", "688", "689"):
                prefix = "sh"
            elif prefix3 in ("000", "001", "002", "003", "004", "300", "301"):
                prefix = "sz"
            else:
                prefix = "bj"

            single = fetch_finance_single(prefix, code)
            for year in missing_years:
                year_str = str(year)
                if year_str in single:
                    record[year_str] = single[year_str]

        result[code] = record

    return result


def build_records(df: pd.DataFrame, since: str, finance_data: dict) -> list[dict]:
    """清洗数据、过滤上市日期、返回前端可用的字典列表。"""
    # 确保必要字段存在
    for col in ["code", "name"]:
        if col not in df.columns:
            raise ValueError(f"缺少必要字段: {col}")

    # 解析上市日期
    if "list_date" in df.columns:
        df["list_date"] = df["list_date"].apply(parse_date)
    else:
        df["list_date"] = None

    # 处理行业字段
    if "industry" not in df.columns:
        df["industry"] = "-"
    df["industry"] = df["industry"].fillna("-").astype(str)

    # 过滤上市日期
    since_date = datetime.strptime(since, "%Y-%m-%d").date()
    mask = df["list_date"].apply(
        lambda d: d is not None and datetime.strptime(d, "%Y-%m-%d").date() >= since_date
    )
    filtered = df[mask].copy()

    # 去重（按代码），保留第一次出现的记录
    filtered = filtered.drop_duplicates(subset=["code"], keep="first")

    # 构建字典列表
    records = []
    for _, row in filtered.iterrows():
        code = str(row["code"]).strip().zfill(6)
        market, board = classify_board(code)
        fin = finance_data.get(code, {str(y): {"revenue": None, "profit": None} for y in FINANCE_YEARS})

        records.append({
            "code": code,
            "name": str(row["name"]).strip(),
            "list_date": row["list_date"],
            "market": market,
            "board": board,
            "industry": str(row["industry"]).strip() or "-",
            "finance": fin,
            "prospectus_url": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        })

    # 默认按上市日期降序（最新的在前面）
    records.sort(key=lambda x: (x["list_date"] or "0000-00-00"), reverse=True)
    return records


def save_data(records: list[dict]) -> None:
    """保存数据到 data/data.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(records),
        "years": [str(y) for y in FINANCE_YEARS],
        "data": records,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[OK] 已保存 {len(records)} 条记录到 {DATA_FILE}")


def main():
    parser = argparse.ArgumentParser(description="更新 A 股上市企业信息数据")
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"只保留该日期及之后上市的企业，默认 {DEFAULT_SINCE}",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="不过滤上市日期，抓取全部 A 股企业",
    )
    parser.add_argument(
        "--no-finance",
        action="store_true",
        help="跳过财务数据抓取，仅更新企业基础信息",
    )
    args = parser.parse_args()

    since = "1900-01-01" if args.all else args.since

    print(f"[*] 开始抓取 A 股上市企业信息（上市日期 >= {since}）...")
    df = fetch_stock_info()

    codes = df["code"].astype(str).str.strip().str.zfill(6).unique().tolist()

    finance_data = {}
    if not args.no_finance:
        print(f"[*] 开始抓取 {FINANCE_YEARS} 年度财务数据...")
        finance_data = build_finance_data(codes)
    else:
        print("[*] 跳过财务数据抓取")

    records = build_records(df, since, finance_data)
    save_data(records)
    print("[*] 完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
