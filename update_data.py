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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PyPDF2 import PdfReader
from io import BytesIO

# 项目根目录
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_FILE = DATA_DIR / "data.json"
IPO_ACCEPTED_FILE = DATA_DIR / "ipo_accepted.json"

# 帶重試的 PDF 下載會話（用於招股書下載）
def _create_pdf_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    })
    return session


PDF_SESSION = _create_pdf_session()

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
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
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


def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 120) -> str:
    """從 PDF 字節中提取文本，失敗時返回空字符串。"""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            try:
                txt = page.extract_text()
                if txt:
                    texts.append(txt)
            except Exception:
                pass
        return "\n".join(texts)
    except Exception as e:
        print(f"[WARN] PDF 解析失敗: {e}", file=sys.stderr)
        return ""


def _extract_metric_after_checkbox(section_one_line: str, label_pattern: str, next_labels: list[str]) -> tuple[str | None, bool | None]:
    """在合併後的單行文本中，根據指標標籤正則定位，跳過閾值和勾選，提取"指標情況"。"""
    match = re.search(label_pattern, section_one_line)
    if not match:
        return None, None

    search_start = match.end()
    window = section_one_line[search_start:search_start + 60]

    met = None
    value_start = search_start

    # 常見勾選格式："是 □否"、"√是 □否"、"是 □否"、"■是 □否"
    for marker in ["", "√", "■", "☑"]:
        yes_match = re.search(re.escape(marker) + r"\s*是", window)
        no_match = re.search(re.escape(marker) + r"\s*否", window)
        if yes_match:
            met = True
            value_start = search_start + yes_match.end()
            break
        if no_match:
            met = False
            value_start = search_start + no_match.end()
            break
    if met is None:
        yes_pos = window.find("是")
        no_pos = window.find("否")
        if yes_pos >= 0 and (no_pos < 0 or yes_pos < no_pos):
            met = True
            value_start = search_start + yes_pos + 1
        elif no_pos >= 0:
            met = False
            value_start = search_start + no_pos + 1

    end = len(section_one_line)
    for next_label in next_labels:
        nidx = section_one_line.find(next_label, value_start + 3)
        if nidx >= 0:
            end = min(end, nidx)

    value = section_one_line[value_start:end].strip()
    value = re.sub(r"^[□√■☑\s≥\d\.,亿元万元%\-]*", "", value)
    value = re.sub(r"^否\s*", "", value)
    value = value.strip()
    if not value:
        value = None
    return value, met


def _safe_float(text: str) -> float | None:
    """從文本中提取第一個數字（支持萬/億單位）。"""
    if not text:
        return None
    # 去掉千分位逗號
    text = text.replace(",", "")
    # 嘗試匹配 142,797.55、3億、8000萬等
    m = re.search(r"([\d\.]+)\s*(?:萬|万|億|亿)?", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _clean_extracted_text(text: str | None) -> str | None:
    """
    清理從 PDF 中提取的文本：去除勾選框符號、PUA 字符、多餘空白、換行殘留等。
    """
    if not text:
        return None
    # 去除常見勾選框與對勾符號
    text = re.sub(r"[□√■☑▪◆◇]", "", text)
    # 去除 Unicode Private Use Area（PDF 表格複選框等佔位符）
    text = re.sub(r"[-]", "", text)
    # 去除 "是 / 否" 殘留
    text = re.sub(r"\b是\b|\b否\b", "", text)
    # 將多個空白、製表符、換行統一為單個空格
    text = re.sub(r"[\s\t\n\r]+", " ", text)
    # 修復數字與單位/標點之間被插入的空格，如 "5,623 .67"、"3 億"、"2023 - 2025"
    text = re.sub(r"(\d)\s+([\.億亿萬万元%\-])", r"\1\2", text)
    text = re.sub(r"(\d{1,3}(?:,\d{3})*)\s+([\.億亿萬万元%\-])", r"\1\2", text)
    # 修復中文詞內部空格，如 "產業 化"、"發明專 利"
    text = re.sub(r"([一-龥])\s+([一-龥])", r"\1\2", text)
    # 修復中英文標點周圍多餘空格
    text = re.sub(r"\s*([，。、；：？！])\s*", r"\1", text)
    # 去除末尾無意義標點
    text = text.rstrip("，。、；： ")
    text = text.strip()
    return text if text else None


def _extract_sentence_excerpt(one_line: str, match_start: int, match_end: int, context: int = 80, max_len: int = 160) -> str | None:
    """
    從 one_line 文本中提取包含匹配區域的完整句子/語句片段，並做清理。
    優先保留包含匹配位置的整句；若句子過長則在合理標點處截斷，確保摘錄簡潔。
    """
    if not one_line or match_start < 0 or match_end > len(one_line):
        return None

    # 擴展上下文窗口
    window_start = max(0, match_start - context)
    window_end = min(len(one_line), match_end + context)
    window = one_line[window_start:window_end]

    # 把窗口按中文句子結尾拆分，定位包含匹配區域的句子
    sentence_end_re = re.compile(r"[。；！？]")
    ends = [m.end() for m in sentence_end_re.finditer(window)]
    starts = [0] + ends

    rel_match_start = match_start - window_start
    rel_match_end = match_end - window_start
    chosen_start = 0
    chosen_end = len(window)
    for i, s in enumerate(starts[:-1]):
        e = starts[i + 1]
        if s <= rel_match_start < e or s <= rel_match_end <= e:
            chosen_start = s
            chosen_end = e
            break

    excerpt = window[chosen_start:chosen_end]

    # 若句子過長，圍繞匹配位置截斷到 max_len 以內
    if len(excerpt) > max_len:
        # 計算匹配在 excerpt 中的相對位置
        match_rel_start = max(0, rel_match_start - chosen_start)
        match_rel_end = min(len(excerpt), rel_match_end - chosen_start)
        half = max_len // 2
        left = max(0, match_rel_start - half)
        right = min(len(excerpt), match_rel_end + half)
        # 盡量向左右擴展到標點或空格，避免截斷詞語
        if left > 0:
            next_punct = re.search(r"[，。、；：？！\s]", excerpt[left:right])
            if next_punct:
                left += next_punct.start()
        if right < len(excerpt):
            prev_punct = re.search(r"[，。、；：？！\s]", excerpt[left:right])
            if prev_punct:
                right = left + prev_punct.start()
        excerpt = excerpt[left:right]

    return _clean_extracted_text(excerpt)


def _check_met_by_context(section_one_line: str, keyword_pos: int, window_size: int = 120) -> bool | None:
    """根據關鍵詞後面的文本判斷是否符合。"""
    window = section_one_line[keyword_pos:keyword_pos + window_size]
    # 直接關鍵詞
    if re.search(r"[√■☑]\s*是", window):
        return True
    if re.search(r"[√■☑]\s*否", window):
        return False
    if "满足" in window or "符合" in window:
        return True
    if "不满足" in window or "不符合" in window:
        return False
    if "不适用" in window:
        return None  # 特殊標記
    return None


# 科創屬性例外條款（《科創屬性評價指引（試行）》第二條）
EXCEPTION_CLAUSE_PATTERNS: list[tuple[str, str, list[str]]] = [
    (
        "national_strategy",
        "国家战略/国际领先技术",
        [
            r"核心技术.*?经国家主管部门认定",
            r"国家主管部门.*?认定.*?国际领先",
            r"国际领先|国际先进水平",
            r"国家战略.*?重大意义",
            r"对国家重大战略.*?重大意义",
        ],
    ),
    (
        "national_award",
        "国家科学技术奖",
        [
            r"国家科技进步奖",
            r"国家自然科学奖",
            r"国家技术发明奖",
            r"国家科学技术进步奖",
            r"作为主要参与单位.*?获得.*?国家.*?奖",
            r"核心技术人员.*?作为主要参与人员.*?国家.*?奖",
        ],
    ),
    (
        "national_project",
        "国家重大科技专项",
        [
            r"国家重大科技专项",
            r"国家科技重大专项",
            r"国家重点研发计划",
            r"独立或者牵头承担",
            r"牵头承担.*?国家.*?专项",
            r"独立承担.*?国家.*?专项",
            r"承担.*?国家.*?重大科技项目",
        ],
    ),
    (
        "import_substitution",
        "进口替代/关键领域突破",
        [
            r"进口替代",
            r"关键设备|关键产品|关键零部件|关键材料",
            r"国家鼓励、支持和推动",
            r"打破.*?国外.*?垄断",
            r"打破.*?垄断",
            r"填补国内空白",
            r"国产化替代",
        ],
    ),
    (
        "patents_50",
        "50项以上发明专利",
        [
            r"形成核心技术和应用于主营业务.*?发明专利.*?50项",
            r"形成核心技术和主营业务收入.*?发明专利.*?50项",
            r"发明专利.*?50项以上",
            r"发明专利.*?五十项",
            r"能够产业化的发明专利.*?50项",
            r"形成核心技术的发明专利.*?50项",
        ],
    ),
]


# 各板块上市标准定义
LISTING_STANDARDS: dict[str, dict[int, dict[str, str]]] = {
    # 科创板 5 套上市标准（《上海证券交易所科创板股票上市规则》第 2.1.2 条）
    "科创板": {
        1: {
            "name": "第一套上市标准",
            "summary": "预计市值 ≥10 亿元；最近两年净利润均为正且累计 ≥5000 万元；或最近一年净利润为正且营收 ≥1 亿元。",
        },
        2: {
            "name": "第二套上市标准",
            "summary": "预计市值 ≥15 亿元；最近一年营收 ≥2 亿元；最近三年累计研发投入占营收比例 ≥15%。",
        },
        3: {
            "name": "第三套上市标准",
            "summary": "预计市值 ≥20 亿元；最近一年营收 ≥3 亿元；最近三年经营活动现金流净额累计 ≥1 亿元。",
        },
        4: {
            "name": "第四套上市标准",
            "summary": "预计市值 ≥30 亿元；最近一年营收 ≥3 亿元。",
        },
        5: {
            "name": "第五套上市标准",
            "summary": "预计市值 ≥40 亿元；主要业务或产品需经国家有关部门批准，市场空间大，已取得阶段性成果（医药企业需至少一项核心产品获准开展二期临床试验）。",
        },
    },
    # 创业板 4 套上市标准（《深圳证券交易所创业板股票上市规则》第 2.1.2 条）
    "创业板": {
        1: {
            "name": "第一套上市标准",
            "summary": "最近两年净利润均为正，累计净利润 ≥1 亿元，且最近一年净利润 ≥6000 万元。",
        },
        2: {
            "name": "第二套上市标准",
            "summary": "预计市值 ≥15 亿元；最近一年净利润为正且营收 ≥4 亿元。",
        },
        3: {
            "name": "第三套上市标准",
            "summary": "预计市值 ≥50 亿元；最近一年营收 ≥3 亿元。",
        },
        4: {
            "name": "第四套上市标准",
            "summary": "面向新兴产业、未来产业中尚未盈利但高成长或高研发的优质创新创业企业：高成长型要求预计市值≥30亿元、营收≥2亿元、三年复合增长率≥30%；高研发型要求预计市值≥40亿元、营收≥2亿元、三年累计研发投入≥1亿元且占比≥15%。",
        },
    },
    # 北交所 4 套上市标准（《北京证券交易所股票上市规则（试行）》第 2.1.3 条）
    "北交所": {
        1: {
            "name": "第一套上市标准",
            "summary": "预计市值 ≥2 亿元；最近两年净利润均 ≥1500 万元，或最近一年净利润 ≥2500 万元；且最近两年加权平均净资产收益率平均 ≥8%。",
        },
        2: {
            "name": "第二套上市标准",
            "summary": "预计市值 ≥4 亿元；最近两年营收平均 ≥1 亿元，且最近一年营收增长率 ≥30%，最近一年经营活动现金流净额为正。",
        },
        3: {
            "name": "第三套上市标准",
            "summary": "预计市值 ≥8 亿元；最近一年营收 ≥2 亿元，且最近两年研发投入合计占营收比例 ≥8%。",
        },
        4: {
            "name": "第四套上市标准",
            "summary": "预计市值 ≥15 亿元；最近两年研发投入合计 ≥5000 万元。",
        },
    },
    # 沪深主板 3 套上市标准（《上海证券交易所/深圳证券交易所股票上市规则》第 3.1.1 条）
    "沪市主板": {
        1: {
            "name": "第一套上市标准",
            "summary": "最近三年净利润均为正，且累计 ≥2 亿元；最近一年净利润 ≥1 亿元；最近三年经营活动现金流净额累计 ≥2 亿元或营收累计 ≥15 亿元。",
        },
        2: {
            "name": "第二套上市标准",
            "summary": "预计市值 ≥50 亿元；最近一年净利润为正；最近三年经营活动现金流净额累计 ≥2.5 亿元；最近一年营收 ≥6 亿元。",
        },
        3: {
            "name": "第三套上市标准",
            "summary": "预计市值 ≥100 亿元；最近一年净利润为正；最近一年营收 ≥10 亿元。",
        },
    },
    "深市主板": {
        1: {
            "name": "第一套上市标准",
            "summary": "最近三年净利润均为正，且累计 ≥2 亿元；最近一年净利润 ≥1 亿元；最近三年经营活动现金流净额累计 ≥2 亿元或营收累计 ≥15 亿元。",
        },
        2: {
            "name": "第二套上市标准",
            "summary": "预计市值 ≥50 亿元；最近一年净利润为正；最近三年经营活动现金流净额累计 ≥2.5 亿元；最近一年营收 ≥6 亿元。",
        },
        3: {
            "name": "第三套上市标准",
            "summary": "预计市值 ≥100 亿元；最近一年净利润为正；最近一年营收 ≥10 亿元。",
        },
    },
}

# 板块到上市标准规则条文的映射
BOARD_RULE_PATTERNS: dict[str, dict[str, Any]] = {
    "科创板": {
        "article": r"2\.1\.2",
        "max_standard": 5,
    },
    "创业板": {
        "article": r"2\.1\.2",
        "max_standard": 4,
    },
    "北交所": {
        "article": r"2\.1\.3",
        "max_standard": 4,
    },
    "沪市主板": {
        "article": r"3\.1\.1|3\.1\.2|5\.1\.1|5\.1\.2",
        "max_standard": 3,
    },
    "深市主板": {
        "article": r"3\.1\.1|3\.1\.2|5\.1\.1|5\.1\.2",
        "max_standard": 3,
    },
}


def _extract_listing_standard(text: str, board: str = "科创板") -> dict[str, Any] | None:
    """
    从招股书全文中提取企业申报的上市标准。
    支持板块：科创板、创业板、北交所、沪市主板、深市主板。
    返回 {standard: int, name: str, summary: str, excerpt: str} 或 None。
    """
    if not text:
        return None

    board = board or "科创板"
    board_standards = LISTING_STANDARDS.get(board)
    if not board_standards:
        return None

    rule = BOARD_RULE_PATTERNS.get(board, {"article": r"2\.1\.2", "max_standard": 5})
    article_re = rule["article"]
    # 对包含 | 的条文正则（如 3.1.1|5.1.1）加非捕获分组，避免优先级问题
    if "|" in article_re and not article_re.startswith("(?:"):
        article_re = f"(?:{article_re})"
    max_standard = rule["max_standard"]

    one_line = re.sub(r"\s+", " ", text.replace("\n", " "))

    cn_to_arabic = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}

    # 最优先：从“发行人选择的具体上市标准”专属章节提取（最准确）
    # 排除目录中的标题行（目录行后面跟着“···· 页码”），要求标题后紧跟正文内容
    section_match = re.search(r"发行人选择的具体上市标准(?![^一-龥]{0,60}\d+)([^。；！？]{0,40}?[\s\S]*?)(?:第[一二三四五]节|下一章|募集资金|公司治理)", one_line)
    if not section_match:
        section_match = re.search(r"选择的具体上市标准(?![^一-龥]{0,60}\d+)([^。；！？]{0,40}?[\s\S]*?)(?:第[一二三四五]节|下一章|募集资金|公司治理)", one_line)
    if section_match:
        section_text = section_match.group(1)
        # 兼容 “第 2.1.2 条...第（X）项” 以及 “第 2.1.2 款中第（X）项” 等变体
        m = re.search(rf"第\s*{article_re}\s*(?:条|款).*?第\s*（\s*([一二三四五])\s*）\s*项.*?上市标准", section_text)
        if not m:
            m = re.search(r"第\s*([一二三四五])\s*套上市标准", section_text)
        if m:
            raw = m.group(1).strip("() \t")
            num = cn_to_arabic.get(raw)
            if num and 1 <= num <= max_standard:
                std = board_standards[num]
                sec_start = section_match.start(1)
                return {
                    "standard": num,
                    "name": std["name"],
                    "summary": std["summary"],
                    "excerpt": _extract_sentence_excerpt(one_line, sec_start + m.start(), sec_start + m.end()),
                }

    # 优先匹配带规则条文的明确表述，如 "第 2.1.2 条第（一）项上市标准"
    # 公司/发行人明确声明采用/选择某套标准的完整表述（限制跨度，避免从目录跨到正文）
    explicit_adopt_patterns = [
        rf"(?:公司|发行人)(?:选择|拟选择|采用|拟采用)[^。；！？]{{0,60}}第\s*{article_re}\s*(?:条|款)第[一]?款第\s*（\s*([一二三四五])\s*）\s*项(?:规定)?的?上市标准",
        rf"(?:公司|发行人)(?:选择|拟选择|采用|拟采用)[^。；！？]{{0,60}}第\s*{article_re}\s*(?:条|款).*?第\s*（\s*([一二三四五])\s*）\s*项(?:规定)?的?上市标准",
        rf"(?:公司|发行人)(?:选择|拟选择|采用|拟采用)[^。；！？]{{0,40}}第\s*([一二三四五])\s*套上市标准",
    ]

    for pattern in explicit_adopt_patterns:
        for m in re.finditer(pattern, one_line):
            raw = m.group(1).strip("() \t")
            num = cn_to_arabic.get(raw)
            if num and 1 <= num <= max_standard:
                std = board_standards[num]
                excerpt = _extract_sentence_excerpt(one_line, m.start(), m.end())
                # 排除科创属性章节里的例外表述
                if excerpt and re.search(r"可不适用本规定|不适用该指标|科创属性评价", excerpt):
                    continue
                return {
                    "standard": num,
                    "name": std["name"],
                    "summary": std["summary"],
                    "excerpt": excerpt,
                }

    article_patterns = [
        # 第 x.x.x 条第一款第（一）项
        rf"第\s*{article_re}\s*条\s*第\s*一\s*款\s*第\s*（\s*([一二三四五])\s*）\s*项",
        # 第 x.x.x 条第一款上市标准（创业板/北交所常见）
        rf"第\s*{article_re}\s*条\s*第\s*([一二三四五])\s*款\s*上市\s*标\s*准",
        # 第 x.x.x 条/款第（一）项 / 第 x.x.x 条/款第一项
        rf"第\s*{article_re}\s*(?:条|款)\s*第\s*（\s*([一二三四五])\s*）\s*项",
        rf"第\s*{article_re}\s*(?:条|款)\s*第\s*([一二三四五])\s*项",
        # 第 x.x.x 条/款第（一）款（沪市主板常见）
        rf"第\s*{article_re}\s*(?:条|款)\s*第\s*（\s*([一二三四五])\s*）\s*款",
        # 上市规则第 x.x.x 条第（一）项
        rf"上市规则\s*第\s*{article_re}\s*条\s*第\s*（\s*([一二三四五])\s*）\s*项",
    ]

    for pattern in article_patterns:
        for m in re.finditer(pattern, one_line):
            raw = m.group(1).strip("() \t")
            num = cn_to_arabic.get(raw)
            if num and 1 <= num <= max_standard:
                std = board_standards[num]
                excerpt = _extract_sentence_excerpt(one_line, m.start(), m.end())
                # 排除科创属性章节里的例外表述
                if excerpt and re.search(r"可不适用本规定|不适用该指标|科创属性评价", excerpt):
                    continue
                return {
                    "standard": num,
                    "name": std["name"],
                    "summary": std["summary"],
                    "excerpt": excerpt,
                }

    # 通用表述：第一套/第二套/.../上市标准；选择第 x 套/第（x）项上市标准
    # 限制上下文，避免匹配到战略配售选择、目录等无关内容
    generic_patterns = [
        r"第\s*([一二三四五])\s*套\s*上\s*市\s*标\s*准",
        r"选择\s*的\s*具体\s*上市\s*标\s*准\s*[为是].*?第\s*([一二三四五])\s*套",
        r"选择\s*第\s*（\s*([一二三四五])\s*）\s*项\s*上市\s*标\s*准",
        r"选择\s*第\s*([一二三四五])\s*套\s*上市\s*标\s*准",
    ]

    for pattern in generic_patterns:
        for m in re.finditer(pattern, one_line):
            raw = m.group(1).strip("() \t")
            num = cn_to_arabic.get(raw)
            if num and 1 <= num <= max_standard:
                std = board_standards[num]
                return {
                    "standard": num,
                    "name": std["name"],
                    "summary": std["summary"],
                    "excerpt": _extract_sentence_excerpt(one_line, m.start(), m.end()),
                }

    # 次级匹配：关键数值组合（按板块分别配置）
    value_patterns_by_board: dict[str, list[tuple[int, str]]] = {
        "科创板": [
            (1, r"预计市值.*?10\s*亿.*?净利润.*?5[,\s]*0{3}\s*万|预计市值.*?10\s*亿.*?净利润为正.*?营业收入.*?1\s*亿"),
            (2, r"预计市值.*?15\s*亿.*?营业收入.*?2\s*亿.*?研发投入.*?15%"),
            (3, r"预计市值.*?20\s*亿.*?营业收入.*?3\s*亿.*?现金流.*?1\s*亿"),
            (4, r"预计市值.*?30\s*亿.*?营业收入.*?3\s*亿"),
            (5, r"预计市值.*?40\s*亿|第五套上市标准|国家有关部门批准|临床试验"),
        ],
        "创业板": [
            (1, r"最近两年净利润均为正.*?累计.*?1\s*亿.*?最近一年净利润.*?6[,\s]*0{3}\s*万"),
            (2, r"预计市值.*?15\s*亿.*?净利润为正.*?营业收入.*?4\s*亿"),
            (3, r"预计市值.*?50\s*亿.*?营业收入.*?3\s*亿"),
            (4, r"预计市值.*?30\s*亿.*?营业收入.*?2\s*亿.*?复合增长率.*?30%|预计市值.*?40\s*亿.*?营业收入.*?2\s*亿.*?研发投入.*?1\s*亿"),
        ],
        "北交所": [
            (1, r"预计市值.*?2\s*亿.*?净利润.*?1[,\s]*5\s*0{2}\s*万|预计市值.*?2\s*亿.*?净利润.*?2[,\s]*5\s*0{2}\s*万|加权平均净资产收益率.*?8%"),
            (2, r"预计市值.*?4\s*亿.*?营业收入.*?1\s*亿.*?营收增长率.*?30%"),
            (3, r"预计市值.*?8\s*亿.*?营业收入.*?2\s*亿.*?研发投入.*?8%"),
            (4, r"预计市值.*?15\s*亿.*?研发投入.*?5[,\s]*0{3}\s*万"),
        ],
        "沪市主板": [
            (1, r"最近三年净利润均为正.*?累计.*?2\s*亿.*?最近一年净利润.*?1\s*亿|经营活动现金流净额累计.*?2\s*亿|营业收入累计.*?15\s*亿"),
            (2, r"预计市值.*?50\s*亿.*?经营活动现金流净额累计.*?2\.5\s*亿.*?营业收入.*?6\s*亿"),
            (3, r"预计市值.*?100\s*亿.*?营业收入.*?10\s*亿"),
        ],
        "深市主板": [
            (1, r"最近三年净利润均为正.*?累计.*?2\s*亿.*?最近一年净利润.*?1\s*亿|经营活动现金流净额累计.*?2\s*亿|营业收入累计.*?15\s*亿"),
            (2, r"预计市值.*?50\s*亿.*?经营活动现金流净额累计.*?2\.5\s*亿.*?营业收入.*?6\s*亿"),
            (3, r"预计市值.*?100\s*亿.*?营业收入.*?10\s*亿"),
        ],
    }

    value_patterns = value_patterns_by_board.get(board, [])
    for num, pattern in value_patterns:
        if num > max_standard:
            continue
        m = re.search(pattern, one_line)
        if m:
            std = board_standards[num]
            return {
                "standard": num,
                "name": std["name"],
                "summary": std["summary"],
                "excerpt": _extract_sentence_excerpt(one_line, m.start(), m.end()),
            }

    return None



def _extract_expected_market_cap(text: str | None) -> dict[str, Any] | None:
    """
    从招股书全文中提取预计市值/估值信息。
    返回 {value: str, amount: float|None, unit: str} 或 None。
    """
    if not text:
        return None

    one_line = re.sub(r"\s+", " ", text.replace("\n", " "))

    # 限制通配跨度，避免从封面跨到正文
    patterns = [
        r"(?:本次发行后?|预计|对应|测算)[^。；！？]{0,40}市值(?:\s*不\s*低\s*于|\s*不\s*少\s*于|\s*约\s*为|\s*达\s*到|\s*超\s*过|\s*≈|\s*≥)?\s*人\s*民\s*币\s*([\d,\.]+)\s*(亿|万)\s*元",
        r"预计市值(?:\s*不\s*低\s*于|\s*不\s*少\s*于|\s*约\s*为|\s*达\s*到|\s*超\s*过|\s*≈|\s*≥)?\s*([\d,\.]+)\s*(亿|万)\s*元",
    ]

    for pattern in patterns:
        m = re.search(pattern, one_line)
        if m:
            raw_num = m.group(1)
            unit_cn = m.group(2)
            amount = _safe_float(raw_num)
            unit = "亿元" if "亿" in unit_cn else "万元"
            amount_yi = amount if unit == "亿元" else (amount / 10000 if amount else None)
            has_threshold = bool(re.search(r"不低于|不少于|超过|≥", m.group(0)))
            value = f"预计市值不低于人民币 {raw_num}{unit}" if has_threshold else f"预计市值约 {raw_num}{unit}"
            return {
                "value": _clean_extracted_text(value),
                "amount": amount_yi,
                "unit": "亿元",
                "raw": _extract_sentence_excerpt(one_line, m.start(), m.end()),
            }
    return None


def _extract_text_excerpt(section_one_line: str, match_start: int, match_end: int, context: int = 80) -> str:
    """根據匹配位置截取帶前後文的中文文本片段，並清理 PDF 雜訊。"""
    start = max(0, match_start - context)
    end = min(len(section_one_line), match_end + context)
    excerpt = section_one_line[start:end]
    cleaned = _clean_extracted_text(excerpt)
    return cleaned or excerpt


def _extract_exception_clauses(section_one_line: str) -> tuple[list[dict[str, str]], str | None, bool]:
    """
    從科創屬性章節文本中提取例外條款、第五套上市標準及未盈利標記。
    返回：(exceptional_clauses, listing_standard, unprofitable)
    """
    clauses: list[dict[str, str]] = []
    listing_standard: str | None = None
    unprofitable = False

    # 先判斷是否存在例外語境（若無則降低誤報）
    has_exception_context = bool(
        re.search(r"例外|第二条|标准二|未同时满足上述指标|虽未同时满足", section_one_line)
    )

    # 匹配第五套上市標準（用於營收指標不適用邏輯）
    if re.search(r"第五套上市标准|预计市值.*?(不低于|超过).*?人民币.*?(40亿|四十亿)|尚未盈利|未盈利企业", section_one_line):
        listing_standard = "第五套上市标准"
        unprofitable = True

    if not has_exception_context and not listing_standard:
        return clauses, listing_standard, unprofitable

    # 依次匹配 5 類例外條款
    for category, label, patterns in EXCEPTION_CLAUSE_PATTERNS:
        best_excerpt = ""
        best_score = 0
        for pattern in patterns:
            for m in re.finditer(pattern, section_one_line):
                # 簡單置信度：匹配越長、位置靠後（正文區域）分數略高
                score = len(m.group(0))
                if score > best_score:
                    best_score = score
                    best_excerpt = _extract_text_excerpt(section_one_line, m.start(), m.end())
        if best_excerpt:
            clauses.append({
                "category": category,
                "label": label,
                "text": best_excerpt,
            })

    return clauses, listing_standard, unprofitable


def _extract_kcb_sci_tech_impl(pdf_url: str) -> dict | None:
    """
    從科創板招股說明書 PDF 中提取科創屬性指標（內部實現）。
    返回結構化數據；失敗返回 None。
    """
    if not pdf_url:
        return None
    try:
        res = PDF_SESSION.get(pdf_url, timeout=60)
        res.raise_for_status()
    except Exception as e:
        print(f"[WARN] 招股書下載失敗 {pdf_url}: {e}", file=sys.stderr)
        return None

    text = extract_pdf_text(res.content, max_pages=120)
    if not text:
        return None

    # 定位科创属性章节：优先匹配正文中的具体指标区域，避免只匹配到目录
    keywords = [
        "科创属性相关指标要求",
        "科创属性评价标准",
        "科创属性相关指标或情形",
        "科创属性评价指引",
        "科创属性评价",
        "发行人符合科创板定位",
        "科创属性符合科创板定位要求",
    ]
    candidates = []
    for kw in keywords:
        idx = text.find(kw)
        while idx >= 0:
            candidates.append(idx)
            idx = text.find(kw, idx + len(kw))
    if not candidates:
        return None
    # 優先選擇同時包含"研发投入"的位置（避開目錄頁）
    start_idx = None
    for idx in sorted(candidates):
        snippet = text[idx:idx + 300]
        if "研发投入" in snippet or "研发人员" in snippet:
            start_idx = idx
            break
    if start_idx is None:
        start_idx = min(candidates)

    section = text[start_idx:start_idx + 5000]
    section_one_line = re.sub(r"\s+", " ", section.replace("\n", " "))

    result: dict[str, Any] = {
        "metrics": {},
        "exceptional": False,
        "exceptional_clauses": [],
        "listing_standard": None,
        "unprofitable": False,
        "all_met": None,
    }

    # 1. 研发投入：优先找实际金额/比例，而非仅阈值
    rd_value = None
    rd_met = None
    rd_patterns = [
        # 实际三年研发投入金额（分别列示），允许词间空格
        r"研发\s*投\s*入(?:分别)?为\s*([\d,\.]+\s*(?:万元|亿元))、\s*([\d,\.]+\s*(?:万元|亿元))(?:和|、)\s*([\d,\.]+\s*(?:万元|亿元))",
        # 累计研发投入金额
        r"累计研发\s*投\s*入(?:金额)?(?:为|分别)?\s*([\d,\.]+\s*(?:万元|亿元))[^。，]{0,80}?[，。]",
        # 实际研发投入占比（多期表格：10.16% 11.04% 11.25%）
        r"研发\s*投\s*入占(?:最近三年累计)?营业\s*收\s*入的?\s*比\s*例\s*([\d\.]+%)\s+([\d\.]+%)(?:\s+([\d\.]+%))?",
        # 表格行：阈值 ≥5%  是/否  实际值 10.16%
        r"研发\s*投\s*入占(?:最近三年累计)?营业\s*收\s*入的?\s*比\s*例\s*[≥≈]?\s*\d+(?:\.\d+)?%\s*[√■☑]?\s*是\s*([\d\.]+%)",
        # 退而求其次：阈值表述
        r"研发\s*投\s*入占(?:最近三年累计)?营业\s*收\s*入的?\s*比\s*例\s*(?:为|≥|≈|不低于|达到)?\s*([\d\.]+%)",
    ]
    for pattern in rd_patterns:
        m = re.search(pattern, section_one_line)
        if m:
            rd_value = m.group(0).strip()
            # 如果匹配到多期比例，取最后一期作为最新值展示
            if len(m.groups()) >= 2 and all('%' in (g or '') for g in m.groups()):
                percentages = [g for g in m.groups() if g and '%' in g]
                if percentages:
                    rd_value = f"研发投入占营业收入比例 {' / '.join(percentages)}"
                    rd_num = _safe_float(percentages[-1])
                else:
                    rd_num = _safe_float(m.group(1))
            else:
                rd_num = _safe_float(m.group(1))
            # 判断标准：金额 >= 8000万 或 比例 >= 5%
            if "万元" in m.group(1) or "亿元" in m.group(1):
                # 转成万元
                unit = 1
                if "亿" in m.group(1):
                    unit = 10000
                rd_met = (rd_num is not None and rd_num * unit >= 8000) or _check_met_by_context(section_one_line, m.start()) is True
            elif "%" in m.group(1):
                rd_met = (rd_num is not None and rd_num >= 5) or _check_met_by_context(section_one_line, m.start()) is True
            else:
                rd_met = _check_met_by_context(section_one_line, m.start())
            break
    result["metrics"]["rd_investment"] = {"value": _clean_extracted_text(rd_value), "met": rd_met}

    # 2. 研发人员占比：优先提取实际比例，而非阈值“≥10%”
    rd_person_value = None
    rd_person_met = None
    # 允许 PDF 提取后中文词内部出现单个空格
    rd_person_patterns = [
        # 截至某日期，公司研发人员占员工总数的比例为 15.32%
        r"研发\s*人\s*员\s*占\s*(?:当\s*年)?\s*员\s*工\s*总\s*数\s*(?:的)?\s*比\s*例\s*(?:为|是)\s*([\d\.]+%)",
        # 占员工总数比例为 15.32%（前面可能有人数）
        r"占\s*员\s*工\s*总\s*数\s*比\s*例\s*(?:为|是)\s*([\d\.]+%)",
        # 研发人员数量 X人，占员工总数比例为 Y%
        r"研发\s*人\s*员\s*数\s*量\s*\d+\s*人\s*，\s*占\s*员\s*工\s*总\s*数\s*比\s*例\s*(?:为|是)\s*([\d\.]+%)",
        # 研发人员占比 15.32%
        r"研发\s*人\s*员\s*占\s*比\s*(?:为|是)?\s*([\d\.]+%)",
        # 表格行：阈值 ≥10%   是/否   实际值 15.32%
        r"研发\s*人\s*员\s*占\s*(?:当\s*年)?\s*员\s*工\s*总\s*数\s*(?:的)?\s*比\s*例\s*[≥≈]?\s*\d+(?:\.\d+)?%\s*[√■☑]?\s*是\s*([\d\.]+%)",
    ]
    for pattern in rd_person_patterns:
        m = re.search(pattern, section_one_line)
        if m:
            ratio = _safe_float(m.group(1))
            rd_person_value = f"研发人员占当年员工总数的比例为 {m.group(1)}"
            rd_person_met = ratio is not None and ratio >= 10
            break
    if rd_person_value is None:
        # 退而求其次：匹配阈值并依赖上下文勾选
        m = re.search(r"研发\s*人\s*员\s*占\s*(?:当\s*年)?\s*员\s*工\s*总\s*数\s*(?:的)?\s*比\s*例\s*(?:为|是|不低于|达到|≥|≈)?\s*([\d\.]+%)", section_one_line)
        if m:
            ratio = _safe_float(m.group(1))
            context_met = _check_met_by_context(section_one_line, m.start())
            rd_person_value = m.group(0).strip()
            if context_met is not None:
                rd_person_met = context_met
            elif ratio is not None:
                rd_person_met = ratio >= 10
    result["metrics"]["rd_personnel"] = {"value": _clean_extracted_text(rd_person_value), "met": rd_person_met}

    # 3. 发明专利：优先找实际数量，而非阈值“≥7项”
    patent_value = None
    patent_met = None
    patent_patterns = [
        # 能够产业化的发明专利合计 119 项
        r"(?:形成主营业务收入|应用于公司主营业务|能够产业化).*?发明专\s*利(?:合计)?\s*(\d+)\s*项",
        # 公司共有发明专利 119 项
        r"(?:公司共有|拥有).*?发明专\s*利\s*(\d+)\s*项",
        # 应用于主营业务的发明专利 119 项
        r"发明专\s*利(?:合计)?\s*(\d+)\s*项[^。，]{0,60}(?:主营业务|产业化)",
        # 更宽松
        r"(?:授权|发明)专\s*利(?:合计)?\s*(\d+)\s*项",
        r"(\d+)\s*项\s*(?:授权|发明)专\s*利",
    ]
    for pattern in patent_patterns:
        m = re.search(pattern, section_one_line)
        if m:
            patent_value = f"应用于公司主营业务并能够产业化的发明专利 {m.group(1)} 项"
            count = int(m.group(1))
            context_met = _check_met_by_context(section_one_line, m.start())
            if context_met is not None:
                patent_met = context_met
            else:
                patent_met = count >= 7
            break
    result["metrics"]["patents"] = {"value": _clean_extracted_text(patent_value), "met": patent_met}

    # 4. 营业收入增长：优先找实际复合增长率/营收金额
    revenue_value = None
    revenue_met = None
    revenue_not_applicable = False
    # 实际营业收入复合增长率
    m = re.search(r"营业收入复合增\s*长率(?:为|是)\s*([\d\.]+%)", section_one_line)
    if not m:
        m = re.search(r"复合增\s*长率(?:为|是)\s*([\d\.]+%)", section_one_line)
    if not m:
        # 表格行：阈值 ≥25%  是/否  实际值 32.5%
        m = re.search(r"营业收入复合增\s*长率\s*[≥≈]?\s*\d+(?:\.\d+)?%\s*[√■☑]?\s*是\s*([\d\.]+%)", section_one_line)
    if m:
        revenue_value = f"营业收入复合增长率为 {m.group(1)}"
        growth = _safe_float(m.group(1))
        context_met = _check_met_by_context(section_one_line, m.start())
        if context_met is True:
            revenue_met = True
        elif context_met is False:
            revenue_met = False
        elif growth is not None:
            revenue_met = growth >= 25
    # 找"营业收入分别为"，計算複合增長率或取最近一年營收
    if not revenue_value:
        m = re.search(r"营业收入分别(?:为|是)\s*([\d,\.]+\s*(?:万元|亿元))、\s*([\d,\.]+\s*(?:万元|亿元))、\s*([\d,\.]+\s*(?:万元|亿元))", section_one_line)
        if m:
            amounts = [_safe_float(v) for v in m.groups()]
            units = [10000 if "亿" in v else 1 for v in m.groups()]
            amounts_wan = [a * u for a, u in zip(amounts, units) if a is not None]
            if len(amounts_wan) == 3 and amounts_wan[0] > 0:
                # 複合增長率 = (末期/初期)^(1/2) - 1
                growth = (amounts_wan[2] / amounts_wan[0]) ** 0.5 - 1
                revenue_value = f"最近三年营业收入分别为 {m.group(1)}、{m.group(2)}、{m.group(3)}，复合增长率约 {(growth*100):.2f}%"
                revenue_met = growth >= 0.25 or amounts_wan[2] >= 30000
    # 找最近一年营收（作为替代标准）
    if not revenue_value:
        m = re.search(r"最近一年营业收入(?:金额)?(?:为|达到)?\s*[≥≈]?\s*([\d,\.]+\s*(?:万元|亿元))", section_one_line)
        if m:
            revenue_value = m.group(0).strip()
            amount = _safe_float(m.group(1))
            unit = 10000 if "亿" in m.group(1) else 1
            if amount is not None:
                revenue_met = amount * unit >= 30000
    # 识别"不适用"（第五套标准等）
    if re.search(r"不适用.*?关于营业收入的要求|第五套上市标准|尚未盈利|未盈利企业|预计市值.*?(不低于|超过).*?人民币.*?(40亿|四十亿)", section_one_line):
        revenue_not_applicable = True
        revenue_met = True
        result["listing_standard"] = "第五套上市标准"
        result["unprofitable"] = True
        if not revenue_value or "不适用" not in revenue_value:
            revenue_value = (revenue_value or "") + "（注：不适用科创属性营业收入指标，拟采用第五套上市标准）"
    result["metrics"]["revenue"] = {"value": _clean_extracted_text(revenue_value), "met": revenue_met, "not_applicable": revenue_not_applicable}

    # 判断是否全部满足
    mets = [v["met"] for v in result["metrics"].values() if v["met"] is not None]
    if mets:
        result["all_met"] = all(mets)

    # 提取例外條款及第五套上市標準
    clauses, listing_standard, unprofitable = _extract_exception_clauses(section_one_line)
    result["exceptional_clauses"] = clauses
    if listing_standard:
        result["listing_standard"] = listing_standard
    result["unprofitable"] = unprofitable
    if clauses or "标准二" in section_one_line or "例外" in section_one_line or "五项" in section_one_line:
        result["exceptional"] = True

    # 提取企業實際申報的上市標準（第 1-5 套）
    listing_std_detected = _extract_listing_standard(text, board="科创板")
    if listing_std_detected:
        result["listing_standard_detected"] = listing_std_detected

    # 提取预计市值/估值
    expected_market_cap = _extract_expected_market_cap(text)
    if expected_market_cap:
        result["expected_market_cap"] = expected_market_cap

    return result


def extract_kcb_sci_tech(pdf_url: str) -> dict | None:
    """帶異常保護的招股書科創屬性提取入口。"""
    try:
        return _extract_kcb_sci_tech_impl(pdf_url)
    except Exception as e:
        print(f"[WARN] 提取科创属性失败 {pdf_url}: {e}", file=sys.stderr)
        return None


def enrich_kcb_sci_tech(records: list[dict]) -> list[dict]:
    """為科創板 IPO 受理企業補充科創屬性指標，並為全部 IPO 企業補充招股書摘要信息。"""
    kcb_records = [r for r in records if r.get("exchange") == "科创板" and r.get("prospectus_url")]
    all_records = [r for r in records if r.get("prospectus_url")]

    # 1. 科創板科創屬性（單線程，避免對交易所/巨潮造成過大壓力）
    if kcb_records:
        print(f"[*] 正在提取 {len(kcb_records)} 家科創板企業的科創屬性指標...")
        success = 0
        failed = 0
        for i, record in enumerate(kcb_records, 1):
            sci_tech = extract_kcb_sci_tech(record.get("prospectus_url"))
            record["sci_tech"] = sci_tech
            if sci_tech and sci_tech.get("metrics"):
                success += 1
            else:
                failed += 1
            if i % 10 == 0 or i == len(kcb_records):
                print(f"  科創屬性提取進度 {i}/{len(kcb_records)}，成功 {success}，失敗 {failed}")
            if i < len(kcb_records):
                time.sleep(0.5)
        print(f"[OK] 科創屬性提取完成，成功 {success}，失敗 {failed}")

    # 2. 全部 IPO 企業：上市標準 + 预计市值（多線程提速）
    if all_records:
        print(f"[*] 正在提取 {len(all_records)} 家 IPO 企業的上市標準與预计市值...")
        success_std = 0
        success_val = 0
        failed = 0
        print_lock = threading.Lock()

        def _ipo_summary_worker(args: tuple[int, dict, int]) -> tuple[int, bool, bool]:
            nonlocal failed
            i, record, total = args
            url = record.get("prospectus_url")
            board = record.get("exchange") or "科创板"
            try:
                res = PDF_SESSION.get(url, timeout=(10, 30))
                res.raise_for_status()
                text = extract_pdf_text(res.content, max_pages=120)
                has_std = False
                has_val = False
                if text:
                    std = _extract_listing_standard(text, board=board)
                    if std:
                        record["listing_standard_detected"] = std
                        has_std = True
                    val = _extract_expected_market_cap(text)
                    if val:
                        record["expected_market_cap"] = val
                        has_val = True
                if not (has_std or has_val):
                    with print_lock:
                        failed += 1
                with print_lock:
                    if i % 10 == 0 or i == total:
                        print(f"  IPO 摘要提取進度 {i}/{total}，上市標準 {success_std}，预计市值 {success_val}，失敗 {failed}")
                return i, has_std, has_val
            except Exception as e:
                with print_lock:
                    failed += 1
                    if i % 10 == 0 or i == total:
                        print(f"  IPO 摘要提取進度 {i}/{total}，上市標準 {success_std}，预计市值 {success_val}，失敗 {failed}")
                    print(f"[WARN] 提取 {record.get('name')} ({board}) 招股書摘要失败: {e}")
                return i, False, False

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_ipo_summary_worker, (i, r, len(all_records))): (i, r)
                for i, r in enumerate(all_records, 1)
            }
            for future in as_completed(futures):
                i, has_std, has_val = future.result()
                if has_std:
                    success_std += 1
                if has_val:
                    success_val += 1
        print(f"[OK] IPO 摘要提取完成，上市標準 {success_std}，预计市值 {success_val}，失敗 {failed}")
    return records


def _get_ipo_source_url(exchange: str) -> str:
    """根據擬上市地點返回交易所公開審核入口 URL。"""
    mapping = {
        "科创板": "http://kcb.sse.com.cn/renewal/",
        "沪主板": "https://www.sse.com.cn/listing/renewal/ipo/",
        "深主板": "http://www.szse.cn/market/listing/ipo/",
        "创业板": "http://www.szse.cn/market/listing/ipo/",
        "北交所": "http://www.bse.cn/",
    }
    return mapping.get(exchange, "https://data.eastmoney.com/xg/ipo/")


def fetch_ipo_accepted(df: pd.DataFrame | None, since: str = None, until: str = None) -> list[dict]:
    """
    抓取 IPO 受理企业数据（含历史受理记录及当前最新状态）。
    使用 akshare 的 stock_register_all_em（来源：东方财富）。
    返回字段：name, status, accept_date, exchange, industry, reg_address, sponsor,
              prospectus_url, prospectus_url_em, prospectus_source_name,
              source_name, source_url, sci_tech(科創板)
    """
    print("[*] 正在抓取 IPO 受理企业数据...")
    if df is None or df.empty:
        return []

    # 按受理日期过滤（保留所有当前状态：已受理/已问询/通过/终止等）
    try:
        df["受理日期_dt"] = pd.to_datetime(df["受理日期"], errors="coerce").dt.date
        df = df[df["受理日期_dt"].apply(lambda d: d is not None)]

        if since:
            since_dt = datetime.strptime(since, "%Y-%m-%d").date()
            df = df[df["受理日期_dt"].apply(lambda d: d >= since_dt)]

        if until:
            until_dt = datetime.strptime(until, "%Y-%m-%d").date()
            df = df[df["受理日期_dt"].apply(lambda d: d <= until_dt)]
    except Exception as e:
        print(f"[WARN] 受理日期过滤失败: {e}")

    # 字段映射
    records = []
    for _, row in df.iterrows():
        exchange = str(row.get("拟上市地点", "")).strip()
        prospectus_url = str(row.get("招股说明书", "")).strip() or ""
        records.append({
            "name": str(row.get("企业名称", "")).strip(),
            "status": str(row.get("最新状态", "")).strip(),
            "accept_date": str(row.get("受理日期", "")).strip(),
            "exchange": exchange,
            "industry": str(row.get("行业", "")).strip() or "-",
            "reg_address": str(row.get("注册地", "")).strip() or "-",
            "sponsor": str(row.get("保荐机构", "")).strip() or "-",
            "prospectus_url": prospectus_url,
            "prospectus_url_em": prospectus_url,
            "prospectus_source_name": "东方财富" if prospectus_url else "-",
            "source_name": "东方财富 IPO 数据中心",
            "source_url": "https://data.eastmoney.com/xg/ipo/",
        })

    # 為每條記錄補充交易所公開審核入口
    for record in records:
        record["source_url"] = _get_ipo_source_url(record.get("exchange"))

    # 按受理日期降序
    records.sort(key=lambda x: x["accept_date"] or "0000-00-00", reverse=True)

    # 為科創板企業補充科創屬性指標
    records = enrich_kcb_sci_tech(records)

    print(f"[OK] IPO 受理企业共 {len(records)} 条")
    return records


def save_ipo_accepted(records: list[dict]) -> None:
    """保存 IPO 受理企业数据到 data/ipo_accepted.json。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(records),
        "source_name": "东方财富 IPO 数据中心 / 巨潮资讯网 / 交易所公开审核信息",
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
