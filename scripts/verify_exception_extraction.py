#!/usr/bin/env python3
"""
抽检科创板 IPO 科创属性例外条款提取结果。

用法：
    python3 scripts/verify_exception_extraction.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IPO_ACCEPTED_FILE = ROOT / "data" / "ipo_accepted.json"


def main() -> None:
    with open(IPO_ACCEPTED_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    kcb = [r for r in records if r.get("exchange") == "科创板"]

    total = len(kcb)
    with_sci_tech = sum(isinstance(r.get("sci_tech"), dict) for r in kcb)
    exceptional = sum(
        isinstance(r.get("sci_tech"), dict) and r["sci_tech"].get("exceptional")
        for r in kcb
    )
    listing_standard_5 = sum(
        isinstance(r.get("sci_tech"), dict)
        and r["sci_tech"].get("listing_standard") == "第五套上市标准"
        for r in kcb
    )

    clause_counter: Counter[str] = Counter()
    samples: list[tuple[str, str, str]] = []

    for r in kcb:
        st = r.get("sci_tech")
        if not isinstance(st, dict):
            continue
        clauses = st.get("exceptional_clauses", [])
        for c in clauses:
            clause_counter[c["label"]] += 1
            if len(samples) < 10:
                samples.append((r["name"], c["label"], c["text"]))

    print(f"科创板记录总数: {total}")
    print(f"提取到 sci_tech: {with_sci_tech}")
    print(f"标记 exceptional: {exceptional}")
    print(f"第五套上市标准: {listing_standard_5}")
    print(f"\n例外条款分布:")
    for label, count in clause_counter.most_common():
        print(f"  {label}: {count}")

    print(f"\n抽样原文摘录（前 {len(samples)} 条）:")
    for name, label, text in samples:
        print(f"\n【{name}】{label}")
        print(f"  {text[:200]}...")


if __name__ == "__main__":
    main()
