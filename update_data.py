#!/usr/bin/env python3
"""
A 股上市企业信息数据更新脚本（增强版）

功能：
1. 抓取 A 股上市企业基础信息（代码、名称、上市日期等）
2. 根据代码细分板块（沪市主板、科创板、深市主板、创业板、北交所等）
3. 抓取 2019-2025 年年度营业总收入和净利润，用于展示报告期及最新年度财务数据
4. 生成前端可用的 data.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

# 项目根目录
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "data.json"
IPO_ACCEPTED_FILE = DATA_DIR / "ipo_accepted.json"

# 默认只保留 2022 年及以后上市的企业
DEFAULT_SINCE = "2022-01-01"

# 财务报表年份：覆盖 2022 年前及以后上市企业的报告期，以及最新年度 2025
FINANCE_YEARS = [2019, 2020, 2021, 2022, 2023, 2024, 2025]

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


def normalize_company_name(name: str) -> str:
    """对企业名称做规范化，用于模糊匹配保荐机构。"""
    name = str(name).strip()
    name = re.sub(r"^[NC]\s*", "", name)
    name = re.sub(r"[（(].*?[）)]", "", name)
    for suffix in ("股份有限公司", "有限公司", "集团公司", "集团", "股份"):
        name = name.replace(suffix, "")
    return name.strip()


def is_valid_sponsor(value: str) -> bool:
    if not value:
        return False
    v = str(value).strip().lower()
    return v not in {"-", "none", "nan", "null", ""}


def find_sponsor(
    name: str,
    exact_map: dict[str, str],
    normalized_entries: list[tuple[str, str]],
    threshold: float = 0.8,
) -> str | None:
    """先精确匹配，再按规范化名称做模糊匹配。"""
    if not name:
        return None

    if name in exact_map:
        return exact_map[name]

    norm_name = normalize_company_name(name)
    if not norm_name or not normalized_entries:
        return None

    best_orig = None
    best_ratio = 0.0
    for norm_entry, orig_entry in normalized_entries:
        ratio = SequenceMatcher(None, norm_name, norm_entry).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_orig = orig_entry

    if best_orig and best_ratio >= threshold:
        return exact_map.get(best_orig)

    return None


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
    为所有企业构建 2019-2025 年财务数据。
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


def fetch_main_business_single(code: str) -> tuple[str, str]:
    """单个企业抓取主营业务，返回 (code, business)。"""
    code = code.zfill(6)
    try:
        df = ak.stock_profile_cninfo(symbol=code)
        if df.empty or "主营业务" not in df.columns:
            return code, None
        business = str(df.iloc[0]["主营业务"]).strip()
        if business and business.lower() not in {"-", "none", "nan", "null"}:
            return code, business
    except Exception:
        pass
    return code, None


def fetch_main_business(codes: list[str], max_workers: int = 8) -> dict[str, str]:
    """
    并发抓取每个企业的主营业务。
    优先满足沪市主板和科创板，其他板块有数据也会补充。
    返回 {code: business}。
    """
    codes = sorted(set(c.zfill(6) for c in codes))
    result = {}
    print(f"[*] 开始抓取 {len(codes)} 家企业的主营业务...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_code = {executor.submit(fetch_main_business_single, c): c for c in codes}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code, business = future.result()
            if business:
                result[code] = business
            if i % 50 == 0 or i == len(codes):
                print(f"  进度 {i}/{len(codes)}，已获取 {len(result)} 条主营业务")
    print(f"[OK] 主营业务抓取完成，共 {len(result)} 条")
    return result


def build_records(
    df: pd.DataFrame,
    since: str,
    finance_data: dict,
    main_business: dict | None = None,
    sponsor_index: tuple[dict[str, str], list[tuple[str, str]]] | None = None,
    org_id_map: dict[str, str] | None = None,
) -> list[dict]:
    """清洗数据、过滤上市日期、返回前端可用的字典列表。"""
    main_business = main_business or {}
    sponsor_exact_map, sponsor_entries = sponsor_index or ({}, [])
    org_id_map = org_id_map or {}
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
        name = str(row["name"]).strip()

        records.append({
            "code": code,
            "name": name,
            "list_date": row["list_date"],
            "market": market,
            "board": board,
            "industry": str(row["industry"]).strip() or "-",
            "main_business": main_business.get(code, "-"),
            "sponsor": find_sponsor(name, sponsor_exact_map, sponsor_entries) or "-",
            "org_id": org_id_map.get(code, ""),
            "finance": fin,
        })

    # 默认按上市日期降序（最新的在前面）
    records.sort(key=lambda x: (x["list_date"] or "0000-00-00"), reverse=True)
    return records


def fetch_register_all_em() -> pd.DataFrame | None:
    """抓取东方财富 IPO 审核全量数据，供保荐机构匹配和 IPO 受理企业筛选复用。"""
    try:
        df = ak.stock_register_all_em()
        return df
    except Exception as e:
        print(f"[WARN] IPO 审核数据抓取失败: {e}")
        return None


def build_sponsor_index(df: pd.DataFrame | None) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """从 IPO 审核数据中构建保荐机构索引（精确 + 规范化模糊匹配）。"""
    if df is None or df.empty:
        return {}, []
    exact_map = {}
    normalized_entries = []
    for _, row in df.iterrows():
        name = str(row.get("企业名称", "")).strip()
        sponsor = str(row.get("保荐机构", "")).strip()
        if not name:
            continue
        if is_valid_sponsor(sponsor):
            exact_map[name] = sponsor
        normalized_entries.append((normalize_company_name(name), name))
    print(f"[OK] 保荐机构索引共 {len(normalized_entries)} 条，精确映射 {len(exact_map)} 条")
    return exact_map, normalized_entries


def fetch_org_ids(codes: list[str]) -> dict[str, str]:
    """通过 CNInfo topSearch 接口查询每个股票代码对应的 orgId。"""
    url = "http://www.cninfo.com.cn/new/information/topSearch/query"
    result = {}

    def query_one(code: str) -> tuple[str, str | None]:
        code = code.zfill(6)
        try:
            res = requests.post(
                url,
                data={"keyWord": code},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()
            if isinstance(data, list) and data:
                for item in data:
                    if str(item.get("code", "")).zfill(6) == code:
                        return code, item.get("orgId")
                return code, data[0].get("orgId")
        except Exception:
            pass
        return code, None

    print(f"[*] 正在查询 {len(codes)} 家企业的 orgId...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_code = {executor.submit(query_one, c): c for c in codes}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code, org_id = future.result()
            if org_id:
                result[code] = org_id
            if i % 100 == 0 or i == len(codes):
                print(f"  进度 {i}/{len(codes)}，已获取 {len(result)} 条 orgId")
    print(f"[OK] orgId 查询完成，共 {len(result)} 条")
    return result


def fetch_prospectus_urls(records: list[dict]) -> dict[str, str]:
    """通过 CNInfo hisAnnouncement/query 查询每家上市企业的招股说明书 PDF 直链。"""
    url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    pdf_base = "https://static.cninfo.com.cn"

    def get_params(board: str) -> tuple[str, str, str]:
        if "科创板" in board or "沪市" in board:
            return "sse", "sh", "category_szsh_all"
        if "创业板" in board or "深市" in board:
            return "szse", "sz", "category_szsh_all"
        if "北交所" in board:
            return "bjse", "bj", ""
        return "szse", "sz", "category_szsh_all"

    def pick(announcements: list[dict]) -> dict | None:
        if not announcements:
            return None
        excludes = ("提示性公告", "摘要", "更正", "修订")
        for a in announcements:
            title = str(a.get("announcementTitle", ""))
            if "招股说明书" in title and not any(e in title for e in excludes):
                return a
        for a in announcements:
            if "招股说明书" in str(a.get("announcementTitle", "")):
                return a
        return announcements[0]

    def query_one(record: dict) -> tuple[str, str | None]:
        code = str(record.get("code", "")).zfill(6)
        org_id = record.get("org_id", "")
        board = record.get("board", "")
        if not org_id:
            return code, None
        column, plate, category = get_params(board)
        try:
            res = requests.post(
                url,
                data={
                    "pageNum": "1",
                    "pageSize": "30",
                    "column": column,
                    "tabName": "fulltext",
                    "plate": plate,
                    "stock": f"{code},{org_id}",
                    "searchkey": "招股说明书",
                    "seDate": "",
                    "category": category,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            res.raise_for_status()
            data = res.json()
            announcements = data.get("announcements") or []
            picked = pick(announcements)
            adjunct = picked.get("adjunctUrl") if picked else None
            if adjunct:
                return code, f"{pdf_base}/{adjunct}"
        except Exception:
            pass
        return code, None

    result = {}
    print(f"[*] 正在查询 {len(records)} 家企业的招股说明书 PDF...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_code = {executor.submit(query_one, r): r["code"] for r in records}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code, pdf_url = future.result()
            if pdf_url:
                result[code] = pdf_url
            if i % 100 == 0 or i == len(records):
                print(f"  进度 {i}/{len(records)}，已匹配 {len(result)} 条")
    print(f"[OK] 招股说明书 PDF 查询完成，共 {len(result)} 条")
    return result


def fetch_ipo_accepted(df: pd.DataFrame | None, since: str = None) -> list[dict]:
    """
    抓取 IPO 获得受理的企业数据。
    使用 akshare 的 stock_register_all_em（来源：东方财富）。
    返回字段：name, status, accept_date, exchange, industry, reg_address, sponsor, prospectus_url
    """
    print("[*] 正在抓取 IPO 获得受理企业数据...")
    if df is None or df.empty:
        return []

    # 只保留已受理状态
    df = df[df["最新状态"] == "已受理"].copy()

    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").date()
            df["受理日期_dt"] = pd.to_datetime(df["受理日期"], errors="coerce").dt.date
            df = df[df["受理日期_dt"].apply(lambda d: d is not None and d >= since_dt)]
        except Exception as e:
            print(f"[WARN] 受理日期过滤失败: {e}")

    # 字段映射
    records = []
    for _, row in df.iterrows():
        records.append({
            "name": str(row.get("企业名称", "")).strip(),
            "status": str(row.get("最新状态", "")).strip(),
            "accept_date": str(row.get("受理日期", "")).strip(),
            "exchange": str(row.get("拟上市地点", "")).strip(),
            "industry": str(row.get("行业", "")).strip() or "-",
            "reg_address": str(row.get("注册地", "")).strip() or "-",
            "sponsor": str(row.get("保荐机构", "")).strip() or "-",
            "prospectus_url": str(row.get("招股说明书", "")).strip() or "",
        })

    # 按受理日期降序
    records.sort(key=lambda x: x["accept_date"] or "0000-00-00", reverse=True)
    print(f"[OK] IPO 获得受理企业共 {len(records)} 条")
    return records


def save_ipo_accepted(records: list[dict]) -> None:
    """保存 IPO 受理企业数据到 data/ipo_accepted.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(records),
        "source_name": "东方财富 IPO 数据中心",
        "source_url": "https://data.eastmoney.com/xg/ipo/",
        "data": records,
    }
    with open(IPO_ACCEPTED_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[OK] 已保存 {len(records)} 条 IPO 受理企业记录到 {IPO_ACCEPTED_FILE}")


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
        "--no-business",
        action="store_true",
        help="跳过主营业务抓取",
    )
    parser.add_argument(
        "--no-finance",
        action="store_true",
        help="跳过财务数据抓取",
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

    main_business = {}
    if not args.no_business:
        main_business = fetch_main_business(codes)
    else:
        print("[*] 跳过主营业务抓取")

    # 抓取 IPO 审核数据，复用于保荐机构匹配和 IPO 受理企业
    register_df = fetch_register_all_em()
    sponsor_index = build_sponsor_index(register_df)
    org_id_map = fetch_org_ids(codes)

    records = build_records(df, since, finance_data, main_business, sponsor_index, org_id_map)

    # 补充招股说明书 PDF 直链
    prospectus_map = fetch_prospectus_urls(records)
    for record in records:
        record["prospectus_url"] = prospectus_map.get(record["code"], "")

    save_data(records)

    # 抓取 IPO 获得受理企业数据（使用相同 since 过滤受理日期）
    ipo_accepted = fetch_ipo_accepted(register_df, since)
    save_ipo_accepted(ipo_accepted)

    print("[*] 完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
