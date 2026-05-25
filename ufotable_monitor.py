#!/usr/bin/env python3
"""
ufotable WEBSHOP (webshop.ufotable.co.jp) 商品监控
API: MODD platform (client-api.modd.com/UFWE)
检测: 新商品 / 補貨 / 售罄 / 価格変更
"""

import json
import os
import sqlite3
import time
import signal
import sys
import random
import logging
from datetime import datetime
from pathlib import Path

import requests

from common import (
    JST, CHANGE_LABELS,
    setup_logging, log_changes,
    ensure_image_keys,
    detect_soldout_delta, build_feishu_card, send_feishu_card,
)

running = True

API_BASE = "https://client-api.modd.com/UFWE"
SHOP_URL = "https://webshop.ufotable.co.jp"

UFOTABLE_CARD = {
    "name": "ufotable WEBSHOP",
    "template_color": "blue",
    "footer": "ufotable WEBSHOP Monitor",
    "subtitle_field": "works",
}


def load_config(path="ufotable_config.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = json.load(open("ufotable_config.example.json", "r", encoding="utf-8"))

    env_map = {
        "FEISHU_WEBHOOK_URL": "webhook_url",
        "FEISHU_APP_ID": "app_id",
        "FEISHU_APP_SECRET": "app_secret",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            nc = cfg.setdefault("notifications", {}).setdefault("feishu", {})
            nc[cfg_key] = os.environ[env_key]
    return cfg


def init_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            product_code TEXT,
            title TEXT,
            works TEXT,
            category TEXT,
            price INTEGER,
            available INTEGER,
            image_url TEXT,
            url TEXT,
            valid_after TEXT,
            first_seen TEXT,
            last_checked TEXT,
            feishu_img_key TEXT DEFAULT ''
        )
    """)
    try:
        conn.execute("SELECT feishu_img_key FROM products LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE products ADD COLUMN feishu_img_key TEXT DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER, change_type TEXT,
            old_value TEXT, new_value TEXT,
            detected_at TEXT, notified INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS soldout_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            soldout_ids TEXT DEFAULT '[]',
            updated_at TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO soldout_snapshot (id, soldout_ids, updated_at)
        VALUES (1, '[]', '')
    """)
    conn.commit()
    return conn


def fetch_data():
    """拉取商品列表和库存"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://webshop.ufotable.co.jp",
        "Referer": "https://webshop.ufotable.co.jp/",
    }

    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/product", headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products", [])
            break
        except Exception as e:
            logging.warning(f"Product fetch attempt {attempt+1}/3: {e}")
            if attempt < 2:
                time.sleep(2)
            else:
                return [], {}

    stock_map = {}
    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/productStock", headers=headers, timeout=30)
            resp.raise_for_status()
            stock_list = resp.json()
            for s in stock_list:
                stock_map[s["productCode"]] = s.get("available", False)
            break
        except Exception as e:
            logging.warning(f"Stock fetch attempt {attempt+1}/3: {e}")
            if attempt < 2:
                time.sleep(2)

    return products, stock_map


def normalize_product(p, stock_map):
    var = p.get("variations", [{}])[0] if p.get("variations") else {}
    code = var.get("productCode", "")
    images = p.get("images", [])
    image_url = images[0]["url"] if images else ""

    works = ""
    category = ""
    for cat in p.get("categories", []):
        if cat.get("groupName") == "works":
            works = cat.get("displayName", "")
        if cat.get("groupName") == "category":
            category = cat.get("displayName", "")

    return {
        "id": p["id"],
        "product_code": code,
        "title": p["title"],
        "works": works,
        "category": category,
        "price": var.get("price", 0),
        "available": 1 if stock_map.get(code, False) else 0,
        "image_url": image_url,
        "url": f"{SHOP_URL}/product/{code}" if code else SHOP_URL,
        "valid_after": p.get("validAfter", ""),
    }


def detect_changes(conn, products, cfg):
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    changes = []

    for p in products:
        pid = p["id"]
        cur = conn.execute("SELECT price, available FROM products WHERE id=?", (pid,))
        old = cur.fetchone()

        if old is None:
            if cfg.get("monitor_options", {}).get("detect_new_products", True):
                changes.append({
                    "product_id": pid, "change_type": "new",
                    "old_value": None,
                    "new_value": f"{p['title']} | Y{p['price']}",
                    "product": p,
                })
        else:
            old_price, old_available = old
            if cfg.get("monitor_options", {}).get("detect_restocks", True) and old_available == 0 and p["available"] == 1:
                changes.append({
                    "product_id": pid, "change_type": "restock",
                    "old_value": "out of stock", "new_value": "in stock", "product": p,
                })
            if cfg.get("monitor_options", {}).get("detect_sold_out", True) and old_available == 1 and p["available"] == 0:
                changes.append({
                    "product_id": pid, "change_type": "sold_out",
                    "old_value": "in stock", "new_value": "out of stock", "product": p,
                })
            if cfg.get("monitor_options", {}).get("detect_price_changes", True) and old_price != p["price"] and old_price != 0:
                changes.append({
                    "product_id": pid, "change_type": "price_change",
                    "old_value": f"Y{old_price}", "new_value": f"Y{p['price']}", "product": p,
                })

    return changes, now_str


def update_db(conn, products, now_str):
    for p in products:
        conn.execute("""
            INSERT INTO products (id, product_code, title, works, category, price, available, image_url, url, valid_after, first_seen, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                product_code=excluded.product_code, title=excluded.title,
                works=excluded.works, category=excluded.category,
                price=excluded.price, available=excluded.available,
                image_url=excluded.image_url, url=excluded.url,
                valid_after=excluded.valid_after, last_checked=excluded.last_checked
        """, (p["id"], p["product_code"], p["title"], p["works"], p["category"],
              p["price"], p["available"], p["image_url"], p["url"],
              p["valid_after"], now_str, now_str))
    conn.commit()


def send_feishu(feishu_cfg, changes, now_str):
    """飞书交互式卡片通知(蓝色模板)"""
    webhook_url = feishu_cfg["webhook_url"]
    payload = build_feishu_card(changes, now_str, UFOTABLE_CARD)
    send_feishu_card(webhook_url, payload)


def send_notifications(cfg, conn, changes, now_str):
    if not changes:
        return
    nc = cfg.get("notifications", {})
    if nc.get("feishu", {}).get("enabled"):
        feishu_cfg = nc["feishu"]
        if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
            ensure_image_keys(conn, changes, feishu_cfg)
        send_feishu(feishu_cfg, changes, now_str)


def run_once(cfg, conn, is_first_run=False, silent=False):
    start = time.time()
    logging.info("Checking ufotable WEBSHOP...")

    products_raw, stock_map = fetch_data()
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p, stock_map) for p in products_raw]
    changes, now_str = detect_changes(conn, products, cfg)

    # 快照对比: 补充 per-product 检测可能漏掉的售罄/補貨
    detect_sold_out = cfg.get("monitor_options", {}).get("detect_sold_out", True)
    snapshot_changes = detect_soldout_delta(conn, products, detect_sold_out, now_str)
    existing_ids = {c["product_id"]: c for c in changes}
    for sc in snapshot_changes:
        if sc["product_id"] not in existing_ids:
            changes.append(sc)

    if changes:
        logging.info(f"Detected {len(changes)} changes")
        for c in changes[:10]:
            p = c["product"]
            logging.info(f"  {CHANGE_LABELS[c['change_type']]} {p['title'][:60]} | Y{p['price']}")
        if len(changes) > 10:
            logging.info(f"  ... and {len(changes)-10} more")

        if not silent and (not is_first_run or cfg.get("monitor_options", {}).get("notify_on_first_run")):
            send_notifications(cfg, conn, changes, now_str)
        elif silent:
            logging.info("Silent mode - skipping notifications")
        log_changes(conn, changes, now_str)
    else:
        logging.info("No changes")

    update_db(conn, products, now_str)
    elapsed = time.time() - start
    logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked")
    return len(changes)


def signal_handler(sig, frame):
    global running
    logging.info("Shutting down...")
    running = False


def main():
    global running
    cfg = load_config()
    db_path = cfg.get("database_path", "data/ufotable.db")
    setup_logging(cfg.get("log_file", "data/ufotable_monitor.log"),
                  fmt="%(asctime)s [UFOTABLE] %(message)s")
    conn = init_db(db_path)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cur = conn.execute("SELECT COUNT(*) FROM products")
    is_first_run = cur.fetchone()[0] == 0

    if is_first_run:
        logging.info("First run - building baseline database...")

    silent = "--silent" in sys.argv

    if "--once" in sys.argv:
        run_once(cfg, conn, is_first_run=is_first_run, silent=silent)
        conn.close()
        return

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info(f"Continuous monitoring started (interval={interval}s)")

    while running:
        try:
            run_once(cfg, conn, is_first_run=is_first_run, silent=silent)
            is_first_run = False
        except Exception as e:
            logging.error(f"Run failed: {e}", exc_info=True)
        if not running:
            break
        jitter = random.uniform(-0.2, 0.2) * interval
        wait = interval + jitter
        logging.info(f"Next check in {wait:.0f}s...")
        for _ in range(int(wait)):
            if not running:
                break
            time.sleep(1)

    conn.close()
    logging.info("Monitor stopped")


if __name__ == "__main__":
    main()
