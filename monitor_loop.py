#!/usr/bin/env python3
"""
Jump Shop 持续监控版本 - 商品上新+补货通知 (飞书卡片+图片预览)
用法: python monitor_loop.py
      python monitor_loop.py --once
Ctrl+C 停止
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

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 环境变量覆盖敏感信息 (用于 GitHub Actions)
    if os.environ.get("FEISHU_WEBHOOK_URL"):
        cfg["notifications"]["feishu"]["webhook_url"] = os.environ["FEISHU_WEBHOOK_URL"]
    if os.environ.get("FEISHU_APP_ID"):
        cfg["notifications"]["feishu"]["app_id"] = os.environ["FEISHU_APP_ID"]
    if os.environ.get("FEISHU_APP_SECRET"):
        cfg["notifications"]["feishu"]["app_secret"] = os.environ["FEISHU_APP_SECRET"]

    return cfg

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
            title TEXT, handle TEXT, vendor TEXT, tags TEXT,
            price INTEGER, available INTEGER, sku TEXT,
            image_url TEXT, url TEXT,
            published_at TEXT, updated_at TEXT,
            first_seen TEXT, last_checked TEXT,
            feishu_img_key TEXT DEFAULT ''
        )
    """)
    # 迁移: 如果旧表没有 feishu_img_key 列则添加
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

# ---------------------------------------------------------------------------
# 商品拉取
# ---------------------------------------------------------------------------
def fetch_all_products(shop_url, user_agents):
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
                break
            except Exception as e:
                logging.warning(f"Page {page} attempt {attempt+1}/3: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    return all_products

        if len(products) < limit:
            break
        page += 1
        time.sleep(random.uniform(0.3, 1.0))

    return all_products


def normalize_product(p):
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
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    changes = []

    for p in products:
        pid = p["id"]
        cur = conn.execute("SELECT price, available, updated_at FROM products WHERE id=?", (pid,))
        old = cur.fetchone()

        if old is None:
            if cfg["monitor_options"]["detect_new_products"]:
                changes.append({
                    "product_id": pid, "change_type": "new",
                    "old_value": None,
                    "new_value": f"{p['title']} | Y{p['price']} | {'in stock' if p['available'] else 'out of stock'}",
                    "product": p,
                })
        else:
            old_price, old_available, old_updated = old
            if cfg["monitor_options"]["detect_restocks"] and old_available == 0 and p["available"] == 1:
                changes.append({
                    "product_id": pid, "change_type": "restock",
                    "old_value": "out of stock", "new_value": "in stock", "product": p,
                })
            if cfg["monitor_options"]["detect_sold_out"] and old_available == 1 and p["available"] == 0:
                changes.append({
                    "product_id": pid, "change_type": "sold_out",
                    "old_value": "in stock", "new_value": "out of stock", "product": p,
                })
            if cfg["monitor_options"]["detect_price_changes"] and old_price != p["price"] and old_price != 0:
                changes.append({
                    "product_id": pid, "change_type": "price_change",
                    "old_value": f"Y{old_price}", "new_value": f"Y{p['price']}", "product": p,
                })

    return changes, now_str


def detect_soldout_delta(conn, products, cfg, now_str):
    """快照对比: 检测本轮 vs 上轮售罄商品集合的变化"""
    if not cfg["monitor_options"].get("detect_sold_out", True):
        return []

    current_soldout = {p["id"] for p in products if p["available"] == 0}

    row = conn.execute("SELECT soldout_ids FROM soldout_snapshot WHERE id=1").fetchone()
    if not row or not row[0]:
        conn.execute("UPDATE soldout_snapshot SET soldout_ids=?, updated_at=? WHERE id=1",
                     (json.dumps(list(current_soldout)), now_str))
        conn.commit()
        return []

    try:
        last_soldout = set(json.loads(row[0]))
    except (json.JSONDecodeError, TypeError):
        last_soldout = set()

    newly_soldout_ids = current_soldout - last_soldout
    newly_restocked_ids = last_soldout - current_soldout

    conn.execute("UPDATE soldout_snapshot SET soldout_ids=?, updated_at=? WHERE id=1",
                 (json.dumps(list(current_soldout)), now_str))
    conn.commit()

    changes = []
    product_map = {p["id"]: p for p in products}

    for pid in newly_soldout_ids:
        p = product_map.get(pid)
        if p:
            changes.append({
                "product_id": pid, "change_type": "sold_out",
                "old_value": "in stock", "new_value": "sold out (snapshot)",
                "product": p,
            })

    for pid in newly_restocked_ids:
        p = product_map.get(pid)
        if p:
            changes.append({
                "product_id": pid, "change_type": "restock",
                "old_value": "sold out", "new_value": "in stock (snapshot)",
                "product": p,
            })

    return changes


# ---------------------------------------------------------------------------
# 数据库更新
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
        """, (p["id"], p["title"], p["handle"], p["vendor"], p["tags"],
              p["price"], p["available"], p["sku"], p["image_url"], p["url"],
              p["published_at"], p["updated_at"], now_str, now_str))
    conn.commit()

def log_changes(conn, changes, now_str):
    for c in changes:
        conn.execute(
            "INSERT INTO change_log (product_id, change_type, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
            (c["product_id"], c["change_type"], c["old_value"], c["new_value"], now_str))
    conn.commit()

# ---------------------------------------------------------------------------
# 飞书 API 工具
# ---------------------------------------------------------------------------
# token 缓存 (有效期2小时)
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
    """下载商品图并上传飞书, 返回 image_key 或空字符串"""
    try:
        token = get_feishu_token(app_id, app_secret)
        img_data = requests.get(image_url, timeout=15).content
        r = requests.post("https://open.feishu.cn/open-apis/im/v1/images",
                          headers={"Authorization": f"Bearer {token}"},
                          files={"image": ("product.jpg", img_data, "image/jpeg")},
                          data={"image_type": "message"},
                          timeout=20)
        result = r.json()
        if result.get("code") == 0:
            return result["data"]["image_key"]
        else:
            logging.warning(f"Feishu image upload failed: {result}")
            return ""
    except Exception as e:
        logging.warning(f"Feishu image upload error: {e}")
        return ""


def ensure_image_keys(conn, changes, feishu_cfg):
    """为变更商品获取飞书图片key, 优先用缓存"""
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

        # 查数据库缓存
        row = conn.execute("SELECT feishu_img_key FROM products WHERE id=?", (p["id"],)).fetchone()
        if row and row[0]:
            p["feishu_img_key"] = row[0]
            count += 1
            continue

        # 上传图片
        logging.info(f"Uploading image for: {p['title'][:40]}...")
        img_key = upload_image_to_feishu(p["image_url"], app_id, app_secret)
        if img_key:
            p["feishu_img_key"] = img_key
            conn.execute("UPDATE products SET feishu_img_key=? WHERE id=?", (img_key, p["id"]))
            conn.commit()
            count += 1
        time.sleep(0.2)  # 避免请求过快

# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
CHANGE_LABELS = {
    "new": "[NEW]",
    "restock": "[RESTOCK]",
    "sold_out": "[SOLD OUT]",
    "price_change": "[PRICE]",
}


def build_feishu_card(changes, now_str):
    """构建飞书交互式卡片 - 带图片预览 + 商品链接按钮"""
    total = len(changes)
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out")
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"  {new_count}  **|**    {restock_count}  **|**    {soldout_count}  **|**    {price_count}"
            }
        },
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

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{header} ({len(items)}件)**"}
        })

        for c in items:
            if shown >= max_items:
                break

            p = c["product"]
            status_icon = "  " if p["available"] else "  "
            status_text = "在庫あり" if p["available"] else "在庫なし"
            price_yen = f"  {p['price']:,}"
            vendor = p.get("vendor", "")
            img_key = p.get("feishu_img_key", "")

            product_md = f"**{p['title']}**\n{price_yen} | {vendor} | {status_icon} {status_text}"
            if ctype == "price_change":
                product_md += f"\n{c['old_value']}    {c['new_value']}"

            # 有图片: 左右分栏布局 (图片 | 信息)
            if img_key:
                elements.append({
                    "tag": "column_set",
                    "flex_mode": "bisect",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 2,
                            "elements": [{
                                "tag": "img",
                                "img_key": img_key,
                                "alt": {"tag": "plain_text", "content": ""},
                                "preview": True,
                                "mode": "fit_horizontal"
                            }]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 3,
                            "elements": [{
                                "tag": "div",
                                "text": {"tag": "lark_md", "content": product_md}
                            }]
                        }
                    ]
                })
            else:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": product_md}
                })

            # 按钮
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "  商品ページ"},
                    "type": "default",
                    "url": p["url"]
                }]
            })

            shown += 1

        if shown >= max_items:
            break

    if total > max_items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"...    他 {total - max_items} 件  変更"}
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"  {now_str}  |  Jump Shop Monitor"}]
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "  JUMP SHOP 商品監視"},
            "template": "red"
        },
        "elements": elements
    }

    return {"msg_type": "interactive", "card": card}


def format_text_message(changes, now_str):
    """纯文本通知(企业微信/邮件等)"""
    total = len(changes)
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out")
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    lines = [
        f"JUMP SHOP Monitor - {now_str}",
        f"Changes: {total} (New:{new_count} Restock:{restock_count} SoldOut:{soldout_count} Price:{price_count})",
        "",
    ]

    type_order = [
        ("new", "=== NEW PRODUCTS ==="),
        ("restock", "=== RESTOCKS ==="),
        ("sold_out", "=== SOLD OUT ==="),
        ("price_change", "=== PRICE CHANGES ==="),
    ]

    for ctype, header in type_order:
        items = [c for c in changes if c["change_type"] == ctype]
        if not items:
            continue
        lines.append(header)
        for c in items[:30]:
            p = c["product"]
            status = "[IN STOCK]" if p["available"] else "[SOLD OUT]"
            lines.append(f"  {p['title']}")
            lines.append(f"  {p['url']}")
            lines.append(f"  Y{p['price']} | {status} | {p.get('vendor','')}")
            if ctype == "price_change":
                lines.append(f"  {c['old_value']} -> {c['new_value']}")
        if len(items) > 30:
            lines.append(f"  ... and {len(items)-30} more")

    return "\n".join(lines)


def send_feishu(feishu_cfg, changes, now_str):
    """飞书交互式卡片通知(含图片预览)"""
    webhook_url = feishu_cfg["webhook_url"]
    payload = build_feishu_card(changes, now_str)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            logging.info("Feishu card notification sent")
        else:
            logging.error(f"Feishu card error: {result}")
            # 降级为纯文本
            text = "JUMP SHOP Monitor\n\n" + format_text_message(changes, now_str)[:8000]
            resp2 = requests.post(webhook_url, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
            if resp2.json().get("code") == 0:
                logging.info("Feishu fallback text sent")
    except Exception as e:
        logging.error(f"Feishu send failed: {e}")


def send_wechat_work(webhook_url, changes, now_str):
    text = format_text_message(changes, now_str)
    payload = {"msgtype": "text", "text": {"content": text[:4000]}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.json().get("errcode") == 0:
            logging.info("WeChat Work notification sent")
        else:
            logging.error(f"WeChat Work error: {resp.json()}")
    except Exception as e:
        logging.error(f"WeChat Work send failed: {e}")


def send_email(smtp_config, changes, now_str):
    text = format_text_message(changes, now_str)
    html = text.replace("\n", "<br>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"JUMP SHOP Monitor - {now_str}"
    msg["From"] = smtp_config["smtp_user"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(f"<pre>{html}</pre>", "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp_config["smtp_host"], smtp_config["smtp_port"], timeout=15) as server:
            server.starttls()
            server.login(smtp_config["smtp_user"], smtp_config["smtp_pass"])
            server.sendmail(smtp_config["smtp_user"], smtp_config["to_emails"], msg.as_string())
        logging.info("Email sent")
    except Exception as e:
        logging.error(f"Email send failed: {e}")


def send_notifications(cfg, conn, changes, now_str):
    if not changes:
        return
    nc = cfg["notifications"]

    # 飞书: 先上传图片再发卡片
    if nc.get("feishu", {}).get("enabled"):
        feishu_cfg = nc["feishu"]
        if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
            ensure_image_keys(conn, changes, feishu_cfg)
        send_feishu(feishu_cfg, changes, now_str)

    if nc.get("wechat_work", {}).get("enabled"):
        send_wechat_work(nc["wechat_work"]["webhook_url"], changes, now_str)
    if nc.get("email", {}).get("enabled"):
        send_email(nc["email"], changes, now_str)

# ---------------------------------------------------------------------------
# 单次检查
# ---------------------------------------------------------------------------
def run_once(cfg, conn, is_first_run=False):
    start = time.time()
    logging.info("Checking Jump Shop...")

    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p) for p in products_raw]
    changes, now_str = detect_changes(conn, products, cfg)

    # 快照对比: 补充 per-product 检测可能漏掉的售罄/補貨
    snapshot_changes = detect_soldout_delta(conn, products, cfg, now_str)
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

        if not is_first_run or cfg["monitor_options"].get("notify_on_first_run"):
            send_notifications(cfg, conn, changes, now_str)
        log_changes(conn, changes, now_str)
    else:
        logging.info("No changes")

    update_db(conn, products, now_str)
    elapsed = time.time() - start
    logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked")
    return len(changes)

# ---------------------------------------------------------------------------
# 持续循环
# ---------------------------------------------------------------------------
def signal_handler(sig, frame):
    global running
    logging.info("Shutting down...")
    running = False

def main():
    global running
    cfg = load_config()
    setup_logging(cfg["log_file"])
    conn = init_db(cfg["database_path"])

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cur = conn.execute("SELECT COUNT(*) FROM products")
    is_first_run = cur.fetchone()[0] == 0

    if is_first_run:
        logging.info("First run - building baseline database...")

    once = "--once" in sys.argv

    if once:
        run_once(cfg, conn, is_first_run=is_first_run)
        conn.close()
        return

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info(f"Continuous monitoring started (interval={interval}s). Press Ctrl+C to stop.")

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
