#!/usr/bin/env python3
"""只推送本地DB中不存在的商品 (= 6/11以来真正的新品)"""
import json, os, sys, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))

from common import build_feishu_cards, send_feishu_card, setup_logging
from monitor_loop import fetch_all_products, normalize_product, load_config

SCRIPT_DIR = Path(__file__).parent
JST = timezone(timedelta(hours=9))

def main():
    cfg = load_config(str(SCRIPT_DIR / "config.json"))
    setup_logging(str(SCRIPT_DIR / "data" / "push_new_only.log"))

    webhook = cfg["notifications"]["feishu"]["webhook_url"]
    if not webhook or "YOUR_" in webhook:
        print("ERROR: webhook not configured")
        sys.exit(1)

    # Load existing product IDs from local DB
    db_path = cfg["database_path"]
    old_ids = set()
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        old_ids = {row[0] for row in conn.execute("SELECT id FROM products")}
        conn.close()
    print(f"[*] Local DB has {len(old_ids)} products (as of June 11)")

    # Fetch current products
    print("[*] Fetching current products from API...")
    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    if not products_raw:
        print("ERROR: fetch failed")
        sys.exit(1)

    products = [normalize_product(p) for p in products_raw]
    print(f"[*] API returned {len(products)} products")

    # Find truly new ones (not in old DB)
    new_products = [p for p in products if p["id"] not in old_ids]
    print(f"[OK] {len(new_products)} truly new products (NOT in local DB)")

    if not new_products:
        print("No new products to push.")
        return

    # Build changes
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    changes = []
    for p in new_products:
        changes.append({
            "product_id": p["id"],
            "change_type": "new",
            "old_value": None,
            "new_value": f"{p['title']} | Y{p['price']} | {'in stock' if p['available'] else 'out of stock'}",
            "product": p,
        })

    # Optional image upload
    upload_images = "--no-images" not in sys.argv
    feishu = cfg["notifications"]["feishu"]
    if upload_images and feishu.get("app_id") and "YOUR_" not in feishu.get("app_id", ""):
        print("[*] Uploading images (slow)...")
        from common import ensure_image_keys
        tmp_conn = sqlite3.connect(":memory:")
        tmp_conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, feishu_img_key TEXT DEFAULT '')")
        for p in new_products:
            tmp_conn.execute("INSERT OR IGNORE INTO products (id) VALUES (?)", (p["id"],))
        tmp_conn.commit()
        ensure_image_keys(tmp_conn, changes, feishu)
        tmp_conn.close()

    shop_config = {
        "name": "JUMP SHOP",
        "template_color": "red",
        "footer": "Jump Shop Monitor",
        "subtitle_field": "vendor",
    }
    cards = build_feishu_cards(changes, now_str, shop_config)
    print(f"[*] {len(cards)} cards, sending...")
    send_feishu_card(webhook, cards)
    print(f"[OK] Done! {len(new_products)} new products pushed.")


if __name__ == "__main__":
    main()
