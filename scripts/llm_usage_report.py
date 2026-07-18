#!/usr/bin/env python3
"""汇总 agent_events 里的 LLM 调用用量，输出每日/每模型/每操作的 token 报表。

数据来源是平台已经在持久化的 llm_call_finished span 事件（含输入/输出 token、
缓存命中 token、耗时、操作类型、模型名），本脚本只做聚合展示，不新增采集。

用法：
    python scripts/llm_usage_report.py                # 最近 7 天
    python scripts/llm_usage_report.py --days 30
    python scripts/llm_usage_report.py --by operation # 按操作维度细分
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "backend" / "skill_agent_loop.db"


def _local_date(created_at: str) -> str:
    # created_at 落库时是 UTC naive 时间；转换成本机时区后按天分组。
    naive = datetime.fromisoformat(created_at)
    return naive.replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d")


def load_rows(db_path: Path, days: int) -> list[tuple[str, dict]]:
    since = (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None).isoformat(sep=" ")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT created_at, payload_json FROM agent_events "
            "WHERE event_type = 'llm_call_finished' AND created_at >= ? ORDER BY created_at",
            (since,),
        )
        rows = []
        for created_at, payload_json in cur.fetchall():
            try:
                rows.append((created_at, json.loads(payload_json)))
            except (TypeError, ValueError):
                continue
        return rows
    finally:
        conn.close()


def aggregate(rows: list[tuple[str, dict]], by: str) -> dict[tuple, dict]:
    buckets: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "input": 0, "output": 0, "cached": 0, "duration_ms": 0.0}
    )
    for created_at, payload in rows:
        day = _local_date(created_at)
        model = str(payload.get("model") or "?")
        key: tuple
        if by == "operation":
            key = (day, model, str(payload.get("operation") or "?"))
        else:
            key = (day, model)
        bucket = buckets[key]
        bucket["calls"] += 1
        bucket["input"] += int(payload.get("input_tokens") or 0)
        bucket["output"] += int(payload.get("output_tokens") or 0)
        bucket["cached"] += int(payload.get("cached_input_tokens") or 0)
        bucket["duration_ms"] += float(payload.get("duration_ms") or 0.0)
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--by", choices=["model", "operation"], default="model")
    args = parser.parse_args()

    rows = load_rows(args.db, args.days)
    if not rows:
        print(f"最近 {args.days} 天没有 llm_call_finished 记录。")
        return 0

    buckets = aggregate(rows, args.by)
    header_extra = " | 操作" if args.by == "operation" else ""
    print(f"日期       | 模型{header_extra} | 调用数 | 输入tok | 输出tok | 缓存命中tok | 命中率 | 均耗时")
    print("-" * 100)
    totals = {"calls": 0, "input": 0, "output": 0, "cached": 0, "duration_ms": 0.0}
    for key in sorted(buckets):
        b = buckets[key]
        for name in totals:
            totals[name] += b[name]
        hit_rate = f"{b['cached'] / b['input'] * 100:.1f}%" if b["input"] else "-"
        avg_ms = f"{b['duration_ms'] / b['calls'] / 1000:.1f}s" if b["calls"] else "-"
        label = " | ".join(key)
        print(
            f"{label} | {b['calls']:>4} | {b['input']:>8} | {b['output']:>7} "
            f"| {b['cached']:>9} | {hit_rate:>6} | {avg_ms:>6}"
        )
    print("-" * 100)
    total_hit = f"{totals['cached'] / totals['input'] * 100:.1f}%" if totals["input"] else "-"
    print(
        f"合计: {totals['calls']} 次调用, 输入 {totals['input']} tok, 输出 {totals['output']} tok, "
        f"缓存命中 {totals['cached']} tok (命中率 {total_hit})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
