#!/usr/bin/env python3
"""
秒杀分析报告 — 离线分析 Jump Shop / ufotable WEBSHOP 售罄速度

用法:
  python analysis.py                          # 默认: Jump Shop, 最近24小时
  python analysis.py --type ufotable          # ufotable WEBSHOP
  python analysis.py --days 7                 # 最近7天
  python analysis.py --top 20                 # Top 20 最快售罄
  python analysis.py --vendor 集英社           # 按厂商筛选
  python analysis.py --threshold 60           # 只看60秒内售罄的 (闪电)
  python analysis.py --hourly                 # 按小时段分布

依赖 change_log + products 两张表。
"""

import sqlite3
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

JST = timezone(timedelta(hours=9))


def parse_jst(ts_str):
    """解析 JST 格式时间戳 "2026-06-11 14:35:00 JST" """
    if not ts_str:
        return None
    try:
        if "JST" in ts_str:
            return datetime.strptime(ts_str.replace(" JST", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def format_seconds(s):
    """格式化秒数"""
    s = int(s)
    if s < 60:
        return f"{s}秒"
    elif s < 3600:
        return f"{s // 60}分{s % 60}秒"
    else:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}小时{m}分"


def load_sellout_data(db_path, days=None, vendor=None, threshold=None, shop_type="jumpshop"):
    """从 DB 中提取售罄速度数据"""
    if not Path(db_path).exists():
        print(f"  DB 不存在: {db_path}")
        return []

    conn = sqlite3.connect(db_path)

    # 检查表是否存在
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {t[0] for t in tables}
    if "change_log" not in table_names or "products" not in table_names:
        print("  DB 缺少 change_log 或 products 表")
        conn.close()
        return []

    # 基础查询: 关联 change_log 和 products, 只取售罄事件
    vendor_field = "vendor" if shop_type == "jumpshop" else "works"

    query = f"""
        SELECT cl.product_id, p.title, p.{vendor_field} AS vendor,
               p.price, p.first_seen, p.published_at, p.valid_after,
               p.last_available_at, cl.detected_at
        FROM change_log cl
        JOIN products p ON p.id = cl.product_id
        WHERE cl.change_type = 'sold_out'
    """

    params = []
    conditions = []

    if days:
        cutoff = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conditions.append("cl.detected_at >= ? || ' JST'")
        params.append(cutoff)

    if vendor:
        conditions.append(f"p.{vendor_field} LIKE ?")
        params.append(f"%{vendor}%")

    if conditions:
        query += " AND " + " AND ".join(conditions)

    query += " ORDER BY cl.detected_at DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for row in rows:
        pid, title, vendor, price, first_seen, pub_at, valid_after, last_avail, detected_at = row

        detected_dt = parse_jst(detected_at)
        if not detected_dt:
            continue

        sellout_seconds = None
        source = None

        # 按优先级尝试各时间源
        for src_label, ts_val in [
            ("last_available", last_avail),
            ("published", pub_at),
            ("valid_after", valid_after),
            ("first_seen", first_seen),
        ]:
            if ts_val:
                ref_dt = parse_jst(ts_val) or (
                    datetime.fromisoformat(ts_val) if "T" in str(ts_val) else None
                )
                if ref_dt:
                    delta = (detected_dt - ref_dt.replace(tzinfo=detected_dt.tzinfo) if ref_dt.tzinfo is None
                             else detected_dt - ref_dt).total_seconds()
                    if 0 <= delta:
                        sellout_seconds = delta
                        source = src_label
                        break

        if threshold and (sellout_seconds is None or sellout_seconds > threshold):
            continue

        results.append({
            "product_id": pid,
            "title": title,
            "vendor": vendor or "",
            "price": price or 0,
            "sellout_seconds": sellout_seconds,
            "source": source,
            "detected_at": detected_dt,
            "first_seen": parse_jst(first_seen),
            "published_at": pub_at,
        })

    return results


def print_header(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_top_fastest(results, top_n=20):
    """打印最快售罄排行"""
    # 筛选有时长数据的记录
    timed = [r for r in results if r["sellout_seconds"] is not None]
    timed.sort(key=lambda r: r["sellout_seconds"])

    if not timed:
        print("\n  暂无有时长数据的售罄记录")
        return

    print_header(f"  最快售罄排行 Top {min(top_n, len(timed))}")
    print(f"  {'商品名称':<40} {'价格':>8} {'耗时':>12} {'来源':>14}")
    print("  " + "-" * 74)

    for r in timed[:top_n]:
        title = r["title"][:38] + (".." if len(r["title"]) > 40 else "")
        price_str = f"  {r['price']:,}" if r["price"] else "N/A"
        duration = format_seconds(r["sellout_seconds"])
        source_map = {
            "last_available": "上次有货",
            "published": "上架时间",
            "valid_after": "生效时间",
            "first_seen": "首次发现",
        }
        src = source_map.get(r["source"], r["source"] or "?")
        print(f"  {title:<40} {price_str:>8} {duration:>12} {src:>14}")


def print_vendor_stats(results):
    """按厂商/作品统计"""
    vendor_data = defaultdict(list)
    for r in results:
        vendor = r["vendor"] or "(未知)"
        if r["sellout_seconds"] is not None:
            vendor_data[vendor].append(r["sellout_seconds"])

    if not vendor_data:
        return

    print_header("按厂商/作品 售罄速度分布")
    print(f"  {'厂商':<25} {'次数':>6} {'最快':>10} {'最慢':>10} {'平均':>10}")
    print("  " + "-" * 61)

    sorted_vendors = sorted(vendor_data.items(), key=lambda x: min(x[1]))
    for vendor, times in sorted_vendors:
        print(f"  {vendor:<25} {len(times):>6} {format_seconds(min(times)):>10} "
              f"{format_seconds(max(times)):>10} {format_seconds(sum(times)//len(times)):>10}")


def print_hourly_heatmap(results):
    """按小时段分布 (识别 bot 活跃时间)"""
    hourly = defaultdict(lambda: {"count": 0, "times": [], "lightning": 0})

    for r in results:
        if r["detected_at"]:
            hour = r["detected_at"].hour
            hourly[hour]["count"] += 1
            if r["sellout_seconds"] is not None:
                hourly[hour]["times"].append(r["sellout_seconds"])
                if r["sellout_seconds"] <= 300:  # 5分钟以内 = 闪电
                    hourly[hour]["lightning"] += 1

    if not hourly:
        return

    print_header("按小时段 售罄活动分布 (JST)")
    print(f"  {'时段':<10} {'售罄总数':>8} {'闪电(≤5分)':>12} {'最快':>10} {'平均':>10}")
    print("  " + "-" * 50)

    for hour in range(24):
        h = hourly.get(hour)
        if not h or h["count"] == 0:
            continue
        avg_time = format_seconds(sum(h["times"]) // len(h["times"])) if h["times"] else "-"
        min_time = format_seconds(min(h["times"])) if h["times"] else "-"
        bar = "  " + "█" * min(h["lightning"], 20)
        print(f"  {hour:02d}:00-{hour:02d}:59 {h['count']:>8} {h['lightning']:>12} "
              f"{min_time:>10} {avg_time:>10} {bar}")


def print_summary(results):
    """总体摘要"""
    total = len(results)
    timed = [r for r in results if r["sellout_seconds"] is not None]
    lightning = [r for r in timed if r["sellout_seconds"] <= 300]
    instant = [r for r in timed if r["sellout_seconds"] <= 60]

    print_header("总览")
    print(f"  售罄事件总数: {total}")
    print(f"  有时长数据: {len(timed)}")
    print(f"  闪电售罄 (≤5分): {len(lightning)} ({len(lightning)*100//len(timed) if timed else 0}%)")
    print(f"  瞬间售罄 (≤1分): {len(instant)}")
    if timed:
        print(f"  最快售罄: {format_seconds(min(timed))}")
        print(f"  最慢售罄: {format_seconds(max(timed))}")
        print(f"  平均售罄: {format_seconds(sum(timed) // len(timed))}")
        # 中位数
        sorted_times = sorted(timed, key=lambda r: r["sellout_seconds"])
        median = sorted_times[len(sorted_times) // 2]["sellout_seconds"]
        print(f"  中位数: {format_seconds(median)}")


def main():
    parser = argparse.ArgumentParser(
        description="秒杀分析报告 — Jump Shop / ufotable WEBSHOP 售罄速度分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python analysis.py                          # Jump Shop, 最近24小时
  python analysis.py --type ufotable          # ufotable WEBSHOP
  python analysis.py --days 7 --top 30        # 最近7天, Top 30
  python analysis.py --vendor 集英社 --threshold 120  # 集英社出品, 2分钟内售罄
  python analysis.py --hourly                 # 仅显示小时分布
        """
    )
    parser.add_argument("--type", choices=["jumpshop", "ufotable"], default="jumpshop",
                        help="商店类型 (默认: jumpshop)")
    parser.add_argument("--db", help="数据库路径 (默认自动选择)")
    parser.add_argument("--days", type=int, default=1,
                        help="分析最近 N 天的数据 (默认: 1)")
    parser.add_argument("--top", type=int, default=20,
                        help="显示 Top N 最快售罄 (默认: 20)")
    parser.add_argument("--vendor", help="按厂商/作品筛选 (模糊匹配)")
    parser.add_argument("--threshold", type=int,
                        help="只显示 ≤N 秒的闪电售罄 (如 --threshold 60)")
    parser.add_argument("--hourly", action="store_true",
                        help="仅显示按小时段分布")
    parser.add_argument("--no-summary", action="store_true",
                        help="不显示总览")
    parser.add_argument("--no-vendor", action="store_true",
                        help="不显示厂商统计")

    args = parser.parse_args()

    # 确定 DB 路径
    if args.db:
        db_path = args.db
    elif args.type == "ufotable":
        db_path = "data/ufotable.db"
    else:
        db_path = "data/products.db"

    shop_name = "ufotable WEBSHOP" if args.type == "ufotable" else "JUMP SHOP"

    print(f"\n  {shop_name} 秒杀分析报告")
    print(f"  DB: {db_path}")
    print(f"  时间范围: 最近 {args.days} 天")
    if args.vendor:
        print(f"  厂商筛选: {args.vendor}")
    if args.threshold:
        print(f"  速度筛选: ≤{args.threshold}秒 ({format_seconds(args.threshold)})")

    # 加载数据
    results = load_sellout_data(
        db_path, days=args.days, vendor=args.vendor,
        threshold=args.threshold, shop_type=args.type
    )

    if not results:
        print("\n  没有找到匹配的售罄记录。")
        print("  可能原因: DB 尚无数据 / 筛选条件过严 / 表结构不完整")
        return

    if not args.no_summary:
        print_summary(results)

    if args.hourly:
        print_hourly_heatmap(results)
    else:
        print_top_fastest(results, args.top)
        if not args.no_vendor:
            print_vendor_stats(results)
        print_hourly_heatmap(results)

    print()


if __name__ == "__main__":
    main()
