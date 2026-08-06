#!/usr/bin/env python3
"""
抓取 2024 年以來香港新上市企業數據，生成 data/hk_data.json。

數據源：
1. 東方財富港股新股上市頁面（HTML 表格）
   https://hk.eastmoney.com/ipolist_1.html
   字段：股票代码、股票名称、招股价、招股数、募集资金、招股日期、上市日期
2. 東方財富港股 F10 公司資料接口（JSON API）
   https://datacenter.eastmoney.com/securities/api/data/v1/get
   字段：英文名称、所属行业、公司介绍（主营业务）
3. 騰訊財經行情接口補充市值
   https://qt.gtimg.cn/q=hk{code}
   字段：總市值（港元）

板塊劃分：
- 代碼以 08 開頭歸為創業板（GEM）
- 其餘為主板
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import warnings
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import sleep
from typing import Any, Callable

warnings.filterwarnings("ignore")

import pandas as pd
import requests
from bs4 import BeautifulSoup

# 統一 session：不讀取環境/系統代理
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "hk_data.json"

IPO_LIST_URL = "https://hk.eastmoney.com/ipolist_{page}.html"
EASTMONEY_PROFILE_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
MAX_IPO_PAGES = 60
MAX_WORKERS = 5
RETRIES = 3
BACKOFF_BASE = 2.0


def retry_on_failure(max_retries: int = RETRIES, backoff_base: float = BACKOFF_BASE):
    """指數退避重試裝飾器。"""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait = backoff_base * (2 ** attempt)
                        print(
                            f"[WARN] {func.__name__} 第 {attempt + 1} 次失敗: {e}；{wait:.1f}s 后重試",
                            file=sys.stderr,
                        )
                        sleep(wait)
            raise last_exception
        return wrapper
    return decorator


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
            if "%H" not in fmt:
                try:
                    return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            continue
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass
    return None


def classify_hk_board(code: str) -> str:
    """根据港股代码判断板块：08xxx 归为创业板（GEM），其余为主板。"""
    if not code:
        return "主板"
    c = str(code).strip().zfill(5)
    if c.startswith("08"):
        return "创业板"
    return "主板"


def clean_text(value: Any) -> str | None:
    """清理文本字段，空值返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    return s


def summarize_main_business(text: str | None, max_length: int = 180) -> str | None:
    """從東方財富 F10 公司介紹中提取最貼近主營業務的簡短描述。

    策略：
    1. 若原文已足夠短，直接保留。
    2. 否則按句拆分，根據業務關鍵詞打分，選出最能說明「做什麼」的 1–2 句。
       打分會懲罰歷史沿革、榮譽資質、設備羅列、願景口號等無關句子，並適度獎勵靠前的句子。
    3. 最終長度控制在 max_length 左右，超出時以「…」截斷。
    """
    if not text:
        return None
    s = re.sub(r"\s+", "", str(text).strip())
    if len(s) <= max_length:
        return s

    # 中文句子拆分（優先按句號，其次按分號/驚嘆號/問號）
    sentences = [seg.strip() for seg in re.split(r"[。！？；]", s) if len(seg.strip()) >= 8]
    if not sentences:
        return s[:max_length] + "…"

    positive_keywords = [
        "主營", "主营", "業務", "业务", "產品", "产品", "服務", "服务",
        "提供", "研發", "研发", "生產", "生产", "銷售", "销售", "製造", "制造",
        "解決方案", "解决方案", "專注於", "专注于", "致力於", "致力于",
        "主要從事", "主要从事", "從事", "从事", "經營", "经营", "供應", "供应",
        "產業鏈", "产业链",
    ]
    negative_keywords = [
        "成立於", "成立于", "始建於", "始建于", "創辦", "创办",
        "榮獲", "荣获", "獲得", "获得", "稱號", "称号", "獎項", "奖项", "榮譽", "荣誉", "資質", "资质",
        "願景", "愿景", "使命", "戰略", "战略", "未來", "未来", "規劃", "规划",
        "A+H", "掛牌上市", "挂牌上市", "交易所", "股份代號", "股份代号",
        "進口了", "进口了", "設備", "设备", "磨床", "檢測儀", "检测仪",
        "測量儀", "测量仪", "生產線", "生产线", "基地", "廠房", "厂房",
        "500強", "500强", "排名", "入選", "入选", "認定", "认定", "鏈主", "链主",
    ]

    def score(idx: int, sentence: str) -> int:
        pos = sum(1 for kw in positive_keywords if kw in sentence)
        neg = sum(1 for kw in negative_keywords if kw in sentence)
        # 業務詞加分，歷史/設備/願景詞減分；靠前的句子給予適度位置獎勵
        position_bonus = max(0, 3 - idx) if idx <= 2 else 0
        return pos * 3 - neg * 2 + position_bonus

    ranked = sorted(
        enumerate(sentences),
        key=lambda x: score(x[0], x[1]),
        reverse=True,
    )
    best = ranked[0][1]
    # 若首句過短，嘗試補充第二句（業務相關性最高的另一句）
    if len(best) < 50 and len(ranked) > 1:
        second = ranked[1][1]
        if best != second:
            combined = f"{best}。{second}"
            if len(combined) <= max_length:
                best = combined

    if len(best) > max_length:
        # 嘗試在 max_length 前找一個句號或逗號截斷
        truncate_pos = max_length
        for punct in ["，", "、", "；"]:
            idx = best.rfind(punct, max_length // 2, max_length)
            if idx > 0:
                truncate_pos = idx
                break
        best = best[:truncate_pos] + "…"

    return best


@retry_on_failure()
def fetch_ipo_list_page(page: int) -> list[dict[str, Any]]:
    """抓取東方財富港股新股列表的某一頁。"""
    url = IPO_LIST_URL.format(page=page)
    res = SESSION.get(url, timeout=30)
    res.raise_for_status()
    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    records = []
    rows = table.find_all("tr")[1:]  # 跳過表頭
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 8:
            continue
        # cells: 序号, 股票代码, 股票名称, 招股价, 招股数, 募集资金, 招股日期, 上市日期
        code_raw = cells[1]
        name = cells[2]
        issue_price = cells[3]
        issue_shares = cells[4]
        raise_amount = cells[5]
        subscribe_date_raw = cells[6]
        list_date_raw = cells[7]
        code = re.sub(r"[^0-9]", "", code_raw)
        if not code:
            continue
        records.append({
            "code": code.zfill(5),
            "name": name,
            "list_date": parse_date(list_date_raw),
            "subscribe_date": parse_date(subscribe_date_raw),
            "issue_price": clean_text(issue_price),
            "issue_shares": clean_text(issue_shares),
            "raise_amount": clean_text(raise_amount),
        })
    return records


def fetch_all_ipo_listings() -> list[dict[str, Any]]:
    """抓取東方財富港股新股全部頁面。"""
    all_records: list[dict[str, Any]] = []
    for page in range(1, MAX_IPO_PAGES + 1):
        try:
            records = fetch_ipo_list_page(page)
        except Exception as e:
            print(f"[WARN] 第 {page} 页抓取失败: {e}", file=sys.stderr)
            records = []
        if not records:
            break
        all_records.extend(records)
        print(f"[*] 第 {page} 页: {len(records)} 条")
        sleep(0.3)
    return all_records


@retry_on_failure()
def fetch_company_profile(code: str) -> dict[str, Any] | None:
    """通過東方財富 F10 接口獲取港股公司資料。

    返回：{name_en, industry, main_business}
    """
    params = {
        "reportName": "RPT_HKF10_INFO_ORGPROFILE",
        "columns": "ORG_NAME,ORG_EN_ABBR,BELONG_INDUSTRY,ORG_PROFILE",
        "filter": f'(SECUCODE="{code}.HK")',
        "pageNumber": "1",
        "pageSize": "200",
        "source": "F10",
        "client": "PC",
    }
    res = SESSION.get(EASTMONEY_PROFILE_URL, params=params, timeout=30)
    res.raise_for_status()
    data = res.json()
    if not data.get("result") or not data["result"].get("data"):
        return None
    item = data["result"]["data"][0]
    return {
        "name_en": clean_text(item.get("ORG_EN_ABBR")),
        "industry": clean_text(item.get("BELONG_INDUSTRY")),
        "main_business": summarize_main_business(item.get("ORG_PROFILE")),
    }


def fetch_all_company_profiles(codes: list[str]) -> dict[str, dict[str, Any]]:
    """並發獲取所有港股公司資料。"""
    profiles: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_code = {executor.submit(fetch_company_profile, code): code for code in codes}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code = future_to_code[future]
            try:
                profile = future.result()
                if profile:
                    profiles[code] = profile
            except Exception as e:
                print(f"[WARN] 获取 {code} 公司资料失败: {e}", file=sys.stderr)
            if i % 20 == 0 or i == len(codes):
                print(f"  公司资料进度 {i}/{len(codes)}")
    return profiles


def fetch_tencent_market_caps(codes: list[str]) -> dict[str, float | None]:
    """通過騰訊接口批量獲取港股總市值（港元）。

    返回：{code: market_cap_hkd}
    """
    if not codes:
        return {}

    # 騰訊接口需要保留港股代碼前導零（如 03308），去掉前導零會導致無匹配
    symbols = ",".join(f"hk{code}" for code in codes)
    url = f"{TENCENT_QUOTE_URL}{symbols}"
    res = SESSION.get(url, timeout=30)
    res.encoding = "gbk"
    text = res.text

    caps: dict[str, float | None] = {}
    pattern = re.compile(r'v_hk(\d+)="([^"]*)"')
    for m in pattern.finditer(text):
        raw_code = m.group(1)
        fields = m.group(2).split("~")
        # 字段 69 为总市值（港元）
        cap = None
        if len(fields) > 69:
            try:
                cap = float(fields[69])
            except (ValueError, TypeError):
                cap = None
        # raw_code 已經包含完整前導零
        code5 = raw_code.zfill(5)
        caps[code5] = cap
    return caps


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

    print("[*] 正在抓取東方財富港股新股列表...")
    records = fetch_all_ipo_listings()
    print(f"[*] 共抓取 {len(records)} 條港股新股記錄")

    # 過濾上市日期
    records = [r for r in records if r.get("list_date") and r["list_date"] >= since]
    print(f"[*] 上市日期在 {since} 之后：{len(records)} 條")

    if args.limit:
        records = records[: args.limit]

    codes = [r["code"] for r in records]

    # 補充公司資料
    print("[*] 正在通過東方財富 F10 接口補充公司資料...")
    profiles = fetch_all_company_profiles(codes)
    print(f"[*] 成功獲取 {len(profiles)} 條公司資料")

    # 補充市值
    print("[*] 正在通過騰訊行情接口補充市值...")
    caps: dict[str, float | None] = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        batch_caps = fetch_tencent_market_caps(batch)
        caps.update(batch_caps)
        print(f"  市值進度 {min(i + batch_size, len(codes))}/{len(codes)}")
        sleep(0.3)

    # 組合最終記錄
    final_records: list[dict[str, Any]] = []
    for r in records:
        code = r["code"]
        profile = profiles.get(code, {})
        cap = caps.get(code)
        final_records.append({
            "code": code,
            "name": r["name"],
            "name_en": profile.get("name_en"),
            "list_date": r["list_date"],
            "subscribe_date": r.get("subscribe_date"),
            "issue_price": r.get("issue_price"),
            "issue_shares": r.get("issue_shares"),
            "raise_amount": r.get("raise_amount"),
            "board": classify_hk_board(code),
            "industry": profile.get("industry") or "-",
            "main_business": profile.get("main_business"),
            "market_cap": cap,
            "market_cap_currency": "HKD" if cap is not None else None,
        })

    final_records.sort(key=lambda r: r["list_date"] or "", reverse=True)

    # 統計板塊
    main_count = sum(1 for r in final_records if r["board"] == "主板")
    gem_count = sum(1 for r in final_records if r["board"] == "创业板")
    print(f"[*] 主板: {main_count}，创业板: {gem_count}")

    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "count": len(final_records),
        "source_name": "东方财富港股新股 / 东方财富 F10 / 腾讯财经",
        "source_url": "https://hk.eastmoney.com/ipolist_1.html",
        "years": [],
        "data": final_records,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已保存 {len(final_records)} 條記錄到 {DATA_FILE}")


if __name__ == "__main__":
    main()
