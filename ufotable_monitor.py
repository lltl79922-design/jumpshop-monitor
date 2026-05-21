#!/usr/bin/env python3
"""
ufotable WEBSHOP (webshop.ufotable.co.jp) 商品监控
API: MODD platform (client-api.modd.com/UFWE)
检测: 新商品 / 補貨 / 售罄 / 価格変更
"""

import json
import os
import sqlite3
import smtplib
import time
import signal
import sys
import random
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
running = True

API_BASE = "https://client-api.modd.com/UFWE"
SHOP_URL = "https://webshop.ufotable.co.jp"


def load_config(path="ufotable_config.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = json.load(open("config.example.json", "r", encoding="utf-8"))

    for key in ["FEISHU_WEBHOOK_URL", "FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
        env_key = key.lower()
        if os.environ.get(key):
            nc = cfg.setdefault("notifications", {}).setdefault("feishu", {})
            if "webhook" in env_key:
                nc["webhook_url"] = os.environ[key]
            elif "app_id" in env_key:
                nc["app_id"] = os.environ[key]
            elif "app_secret" in env_key:
                nc["app_secret"] = os.environ[key]
    return cfg


def setup_logging(log_file):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [UFOTABLE] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )


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

    # 提取作品名
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


def log_changes(conn, changes, now_str):
    for c in changes:
        conn.execute(
            "INSERT INTO change_log (product_id, change_type, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
            (c["product_id"], c["change_type"], c["old_value"], c["new_value"], now_str))
    conn.commit()


# ---------------------------------------------------------------------------
# 飞书 API
# ---------------------------------------------------------------------------
_feishu_token = None
_feishu_token_expiry = 0


def get_feishu_token(app_id, app_secret):
    global _feishu_token, _feishu_token_expiry
    now = time.time()
    if _feishu_token and now < _feishu_token_expiry - 60:
        return _feishu_token
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                       json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"Feishu auth failed: {data}")
    _feishu_token = data["tenant_access_token"]
    _feishu_token_expiry = now + data.get("expire", 7200)
    return _feishu_token


def upload_image_to_feishu(image_url, app_id, app_secret):
    try:
        token = get_feishu_token(app_id, app_secret)
        img_data = requests.get(image_url, timeout=15).content
        content_type = "image/webp" if image_url.endswith(".webp") else "image/jpeg"
        r = requests.post("https://open.feishu.cn/open-apis/im/v1/images",
                          headers={"Authorization": f"Bearer {token}"},
                          files={"image": ("product.webp", img_data, content_type)},
                          data={"image_type": "message"}, timeout=20)
        result = r.json()
        if result.get("code") == 0:
            return result["data"]["image_key"]
        logging.warning(f"Feishu image upload failed: {result}")
        return ""
    except Exception as e:
        logging.warning(f"Feishu image upload error: {e}")
        return ""


def ensure_image_keys(conn, changes, feishu_cfg):
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    if not app_id or not app_secret:
        return
    max_images = feishu_cfg.get("max_image_items", 10)
    count = 0
    for c in changes:
        if count >= max_images:
            break
        p = c["product"]
        if not p.get("image_url"):
            continue
        row = conn.execute("SELECT feishu_img_key FROM products WHERE id=?", (p["id"],)).fetchone()
        if row and row[0]:
            p["feishu_img_key"] = row[0]
            count += 1
            continue
        logging.info(f"Uploading image: {p['title'][:40]}...")
        img_key = upload_image_to_feishu(p["image_url"], app_id, app_secret)
        if img_key:
            p["feishu_img_key"] = img_key
            conn.execute("UPDATE products SET feishu_img_key=? WHERE id=?", (img_key, p["id"]))
            conn.commit()
            count += 1
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def build_feishu_card(changes, now_str):
    total = len(changes)
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out")
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"  {new_count}  **|**    {restock_count}  **|**    {soldout_count}  **|**    {price_count}"}},
        {"tag": "hr"}
    ]

    type_order = [
        ("new", "  新商品上架"),
        ("restock", "  補貨"),
        ("sold_out", "  售罄"),
        ("price_change", "  価格変更"),
    ]

    max_items = 15
    shown = 0

    for ctype, header in type_order:
        items = [c for c in changes if c["change_type"] == ctype]
        if not items:
            continue

        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{header} ({len(items)}件)**"}})

        for c in items:
            if shown >= max_items:
                break

            p = c["product"]
            status_icon = "  " if p["available"] else "  "
            status_text = "在庫あり" if p["available"] else "在庫なし"
            price_yen = f"  {p['price']:,}"
            works = p.get("works", "")
            img_key = p.get("feishu_img_key", "")

            product_md = f"**{p['title']}**\n{price_yen} | {works} | {status_icon} {status_text}"
            if ctype == "price_change":
                product_md += f"\n{c['old_value']}    {c['new_value']}"

            if img_key:
                elements.append({
                    "tag": "column_set", "flex_mode": "bisect", "background_style": "default",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 2,
                         "elements": [{"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": ""}, "preview": True, "mode": "fit_horizontal"}]},
                        {"tag": "column", "width": "weighted", "weight": 3,
                         "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": product_md}}]}
                    ]
                })
            else:
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": product_md}})

            elements.append({
                "tag": "action",
                "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "  商品ページ"}, "type": "default", "url": p["url"]}]
            })
            shown += 1

        if shown >= max_items:
            break

    if total > max_items:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"...    他 {total - max_items} 件  変更"}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": f"  {now_str}  |  ufotable WEBSHOP Monitor"}]})

    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "  ufotable WEBSHOP 商品監視"}, "template": "blue"},
        "elements": elements
    }}


def send_feishu(feishu_cfg, changes, now_str):
    webhook_url = feishu_cfg["webhook_url"]
    payload = build_feishu_card(changes, now_str)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            logging.info("Feishu notification sent")
        else:
            logging.error(f"Feishu card error: {result}")
    except Exception as e:
        logging.error(f"Feishu send failed: {e}")


def send_notifications(cfg, conn, changes, now_str):
    if not changes:
        return
    nc = cfg.get("notifications", {})
    if nc.get("feishu", {}).get("enabled"):
        feishu_cfg = nc["feishu"]
        if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
            ensure_image_keys(conn, changes, feishu_cfg)
        send_feishu(feishu_cfg, changes, now_str)


CHANGE_LABELS = {"new": "[NEW]", "restock": "[RESTOCK]", "sold_out": "[SOLD OUT]", "price_change": "[PRICE]"}


def run_once(cfg, conn, is_first_run=False):
    start = time.time()
    logging.info("Checking ufotable WEBSHOP...")

    products_raw, stock_map = fetch_data()
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p, stock_map) for p in products_raw]
    changes, now_str = detect_changes(conn, products, cfg)

    if changes:
        logging.info(f"Detected {len(changes)} changes")
        for c in changes[:10]:
            p = c["product"]
            logging.info(f"  {CHANGE_LABELS[c['change_type']]} {p['title'][:60]} | Y{p['price']}")
        if len(changes) > 10:
            logging.info(f"  ... and {len(changes)-10} more")

        if not is_first_run or cfg.get("monitor_options", {}).get("notify_on_first_run"):
            send_notifications(cfg, conn, changes, now_str)
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
    setup_logging(cfg.get("log_file", "data/ufotable_monitor.log"))
    conn = init_db(db_path)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cur = conn.execute("SELECT COUNT(*) FROM products")
    is_first_run = cur.fetchone()[0] == 0

    if is_first_run:
        logging.info("First run - building baseline database...")

    if "--once" in sys.argv:
        run_once(cfg, conn, is_first_run=is_first_run)
        conn.close()
        return

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info(f"Continuous monitoring started (interval={interval}s)")

    while running:
        try:
            run_once(cfg, conn, is_first_run=is_first_run)
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
