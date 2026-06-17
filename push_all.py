#!/usr/bin/env python3
"""一次性脚本: 拉取当前全量商品，全部作为"上新"推送到飞书"""
import json, os, sys, logging, time
from pathlib import Path

# Windows GBK 编码兼容
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import requests

# Add project dir to path so we can import common
sys.path.insert(0, str(Path(__file__).parent))

from common import build_feishu_cards, send_feishu_card, setup_logging
from monitor_loop import fetch_all_products, normalize_product, load_config

SCRIPT_DIR = Path(__file__).parent

def main():
    cfg = load_config(str(SCRIPT_DIR / "config.json"))
    setup_logging(str(SCRIPT_DIR / "data" / "push_all.log"))

    feishu = cfg["notifications"]["feishu"]
    webhook = feishu.get("webhook_url", "")
    if not webhook or "YOUR_" in webhook:
        print("ERROR: Feishu webhook_url NOT configured!")
        print("   Set FEISHU_WEBHOOK_URL env or update config.json")
        sys.exit(1)

    print("[*] Fetching Jump Shop all products...")
    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    if not products_raw:
        print("ERROR: Failed to fetch products")
        sys.exit(1)

    products = [normalize_product(p) for p in products_raw]
    print(f"[OK] Total {len(products)} products")

    # 全部标记为"上新"
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    changes = []
    for p in products:
        changes.append({
            "product_id": p["id"],
            "change_type": "new",
            "old_value": None,
            "new_value": f"{p['title']} | Y{p['price']} | {'in stock' if p['available'] else 'out of stock'}",
            "product": p,
        })

    # 上传图片 (可选, 慢)
    upload_images = "--no-images" not in sys.argv
    if upload_images and feishu.get("app_id") and "YOUR_" not in feishu.get("app_id", ""):
        print("[*] Uploading images to Feishu (slow)...")
        from common import ensure_image_keys
        import sqlite3
        # 用临时内存数据库
        tmp_conn = sqlite3.connect(":memory:")
        tmp_conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, feishu_img_key TEXT DEFAULT '')")
        for p in products:
            tmp_conn.execute("INSERT OR IGNORE INTO products (id) VALUES (?)", (p["id"],))
        tmp_conn.commit()
        ensure_image_keys(tmp_conn, changes, feishu)
        tmp_conn.close()

    # 生成分页卡片
    shop_config = {
        "name": "JUMP SHOP",
        "template_color": "red",
        "footer": "Jump Shop Monitor",
        "subtitle_field": "vendor",
    }
    cards = build_feishu_cards(changes, now_str, shop_config)
    print(f"[OK] Generated {len(cards)} cards")

    # 发送
    print(f"[*] Sending to Feishu...")
    send_feishu_card(webhook, cards)
    print(f"[OK] All sent!")


if __name__ == "__main__":
    main()
