#!/usr/bin/env python3
"""
Jump Shop (jumpshop-benelic.com) 商品上新+补货监控系统
检测: 新商品上架 / 补货 / 售罄 / 价格变动
通知: 飞书 / 企业微信 / 邮箱
"""

import json
import os
import sqlite3
import smtplib
import time
import random
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(log_file):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            title TEXT,
            handle TEXT,
            vendor TEXT,
            tags TEXT,
            price INTEGER,
            available INTEGER,
            sku TEXT,
            image_url TEXT,
            url TEXT,
            published_at TEXT,
            updated_at TEXT,
            first_seen TEXT,
            last_checked TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            change_type TEXT,
            old_value TEXT,
            new_value TEXT,
            detected_at TEXT,
            notified INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

# ---------------------------------------------------------------------------
# Shopify 商品拉取
# ---------------------------------------------------------------------------
def fetch_all_products(shop_url, user_agents):
    """翻页拉取全部商品"""
    all_products = []
    page = 1
    limit = 250

    while True:
        url = f"{shop_url}/products.json?limit={limit}&page={page}"
        ua = random.choice(user_agents)
        headers = {
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        }

        for attempt in range(3):
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 30))
                    logging.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                products = data.get("products", [])
                all_products.extend(products)
                logging.info(f"Page {page}: fetched {len(products)} products (total: {len(all_products)})")
                break
            except Exception as e:
                logging.warning(f"Page {page} attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    logging.error(f"Failed to fetch page {page} after 3 attempts")
                    return all_products

        if len(products) < limit:
            break
        page += 1
        time.sleep(random.uniform(0.5, 1.5))

    return all_products


def normalize_product(p):
    """从 Shopify product JSON 提取关键字段"""
    variant = p.get("variants", [{}])[0] if p.get("variants") else {}
    image = p.get("images", [{}])[0] if p.get("images") else {}
    return {
        "id": p["id"],
        "title": p["title"],
        "handle": p["handle"],
        "vendor": p.get("vendor", ""),
        "tags": ",".join(p.get("tags", [])),
        "price": int(float(variant.get("price", 0))),
        "available": 1 if variant.get("available") else 0,
        "sku": variant.get("sku", ""),
        "image_url": image.get("src", ""),
        "url": f"https://jumpshop-benelic.com/products/{p['handle']}",
        "published_at": p.get("published_at", ""),
        "updated_at": p.get("updated_at", ""),
    }

# ---------------------------------------------------------------------------
# 变更检测
# ---------------------------------------------------------------------------
def detect_changes(conn, products, cfg):
    """对比数据库，检测变更"""
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    changes = []
    existing_ids = set()

    cur = conn.execute("SELECT id, price, available, updated_at FROM products")
    for row in cur.fetchall():
        existing_ids.add(row[0])

    for p in products:
        pid = p["id"]
        cur.execute("SELECT price, available, updated_at FROM products WHERE id=?", (pid,))
        old = cur.fetchone()

        if old is None:
            # 新商品
            if cfg["monitor_options"]["detect_new_products"]:
                changes.append({
                    "product_id": pid,
                    "change_type": "new",
                    "old_value": None,
                    "new_value": f"{p['title']} | ¥{p['price']} | {'在庫あり' if p['available'] else '在庫なし'}",
                    "product": p,
                })
        else:
            old_price, old_available, old_updated = old
            # 补货
            if cfg["monitor_options"]["detect_restocks"] and old_available == 0 and p["available"] == 1:
                changes.append({
                    "product_id": pid,
                    "change_type": "restock",
                    "old_value": "在庫なし",
                    "new_value": "在庫あり",
                    "product": p,
                })
            # 售罄
            if cfg["monitor_options"]["detect_sold_out"] and old_available == 1 and p["available"] == 0:
                changes.append({
                    "product_id": pid,
                    "change_type": "sold_out",
                    "old_value": "在庫あり",
                    "new_value": "在庫なし",
                    "product": p,
                })
            # 价格变动
            if cfg["monitor_options"]["detect_price_changes"] and old_price != p["price"] and old_price != 0:
                changes.append({
                    "product_id": pid,
                    "change_type": "price_change",
                    "old_value": f"¥{old_price}",
                    "new_value": f"¥{p['price']}",
                    "product": p,
                })

    return changes, now_str

# ---------------------------------------------------------------------------
# 更新数据库
# ---------------------------------------------------------------------------
def update_db(conn, products, now_str):
    for p in products:
        conn.execute("""
            INSERT INTO products (id, title, handle, vendor, tags, price, available, sku, image_url, url, published_at, updated_at, first_seen, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, handle=excluded.handle, vendor=excluded.vendor,
                tags=excluded.tags, price=excluded.price, available=excluded.available,
                sku=excluded.sku, image_url=excluded.image_url, url=excluded.url,
                published_at=excluded.published_at, updated_at=excluded.updated_at,
                last_checked=excluded.last_checked
        """, (
            p["id"], p["title"], p["handle"], p["vendor"], p["tags"],
            p["price"], p["available"], p["sku"], p["image_url"], p["url"],
            p["published_at"], p["updated_at"], now_str, now_str
        ))
    conn.commit()

def log_changes(conn, changes, now_str):
    for c in changes:
        conn.execute(
            "INSERT INTO change_log (product_id, change_type, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
            (c["product_id"], c["change_type"], c["old_value"], c["new_value"], now_str)
        )
    conn.commit()

# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
CHANGE_LABELS = {
    "new": "新商品",
    "restock": "補貨",
    "sold_out": "售罄",
    "price_change": "価格変更",
}

def format_message(changes, now_str):
    """构造统一的通知文本"""
    total = len(changes)
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out")
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    lines = [
        f"JUMP SHOP 監控通知 - {now_str}",
        f"検出 {total} 件変更: 新商品 {new_count} / 補貨 {restock_count} / 售罄 {soldout_count} / 価格変更 {price_count}",
        "",
    ]

    # 按类型分组，最多显示前20条
    for ctype in ["new", "restock", "sold_out", "price_change"]:
        items = [c for c in changes if c["change_type"] == ctype]
        if not items:
            continue
        lines.append(f"━━━ {CHANGE_LABELS[ctype]} ({len(items)}件) ━━━")
        for c in items[:20]:
            p = c["product"]
            status = "✅在庫あり" if p["available"] else "❌在庫なし"
            lines.append(f"・{p['title']}")
            lines.append(f"  {p['url']}")
            lines.append(f"  ¥{p['price']} | {status} | {p.get('vendor','')}")
            if ctype == "price_change":
                lines.append(f"  {c['old_value']} → {c['new_value']}")
        if len(items) > 20:
            lines.append(f"  ... 他 {len(items)-20} 件")

    return "\n".join(lines)


def send_feishu(webhook_url, changes, now_str):
    """飞书机器人通知"""
    text = format_message(changes, now_str)
    payload = {
        "msg_type": "text",
        "content": {"text": text}
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            logging.info(f"Feishu notification sent successfully")
        else:
            logging.error(f"Feishu error: {result}")
    except Exception as e:
        logging.error(f"Feishu send failed: {e}")


def send_wechat_work(webhook_url, changes, now_str):
    """企业微信机器人通知"""
    text = format_message(changes, now_str)
    payload = {
        "msgtype": "text",
        "text": {"content": text}
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logging.info(f"WeChat Work notification sent successfully")
        else:
            logging.error(f"WeChat Work error: {result}")
    except Exception as e:
        logging.error(f"WeChat Work send failed: {e}")


def send_email(smtp_config, changes, now_str):
    """邮件通知"""
    text = format_message(changes, now_str)
    html = text.replace("\n", "<br>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"JUMP SHOP 監控通知 - {now_str}"
    msg["From"] = smtp_config["smtp_user"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(f"<pre>{html}</pre>", "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_config["smtp_host"], smtp_config["smtp_port"], timeout=15) as server:
            server.starttls()
            server.login(smtp_config["smtp_user"], smtp_config["smtp_pass"])
            server.sendmail(smtp_config["smtp_user"], smtp_config["to_emails"], msg.as_string())
        logging.info(f"Email sent to {smtp_config['to_emails']}")
    except Exception as e:
        logging.error(f"Email send failed: {e}")


def send_notifications(cfg, changes, now_str):
    nc = cfg["notifications"]
    if nc["feishu"]["enabled"] and changes:
        send_feishu(nc["feishu"]["webhook_url"], changes, now_str)
    if nc["wechat_work"]["enabled"] and changes:
        send_wechat_work(nc["wechat_work"]["webhook_url"], changes, now_str)
    if nc["email"]["enabled"] and changes:
        send_email(nc["email"], changes, now_str)

# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run_once(cfg, conn, is_first_run=False):
    logging.info("=" * 50)
    logging.info("Fetching products from Jump Shop...")
    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    products = [normalize_product(p) for p in products_raw]
    logging.info(f"Total products fetched: {len(products)}")

    changes, now_str = detect_changes(conn, products, cfg)

    if changes:
        logging.info(f"Detected {len(changes)} changes:")
        for c in changes:
            p = c["product"]
            logging.info(f"  [{CHANGE_LABELS[c['change_type']]}] {p['title']} | ¥{p['price']} | {'在庫あり' if p['available'] else '在庫なし'}")

        if not is_first_run or cfg["monitor_options"]["notify_on_first_run"]:
            send_notifications(cfg, changes, now_str)
        log_changes(conn, changes, now_str)
    else:
        logging.info("No changes detected")

    update_db(conn, products, now_str)
    logging.info(f"Database updated. {len(products)} products tracked.")
    return len(changes)


def main():
    cfg = load_config()
    setup_logging(cfg["log_file"])
    conn = init_db(cfg["database_path"])

    # 判断是否首次运行
    cur = conn.execute("SELECT COUNT(*) FROM products")
    count = cur.fetchone()[0]
    is_first_run = (count == 0)

    if is_first_run:
        logging.info("First run: initializing product database (no notifications will be sent)")

    try:
        run_once(cfg, conn, is_first_run=is_first_run)
    except Exception as e:
        logging.error(f"Monitor run failed: {e}", exc_info=True)
    finally:
        conn.close()

    logging.info("Monitor run completed")


if __name__ == "__main__":
    main()
