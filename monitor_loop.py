#!/usr/bin/env python3
"""
Jump Shop 持续监控 — 商品上新+补货通知 (飞书卡片+图片预览)
用法:
  python monitor_loop.py --once --state-file=data/jumpshop_state.json --db=data/products.db
  python monitor_loop.py                          # 本地持续模式 (DB 模式)
Ctrl+C 停止
"""

import json
import os
import sys
import time
import random
import logging
from datetime import datetime
from pathlib import Path

import requests

from common import (
    JST,
    setup_logging, log_changes,
    ensure_image_keys,
    build_feishu_cards, send_feishu_card,
    maybe_send_bot_alert,
    load_json_state, save_json_state,
    detect_all_changes_from_state, detect_lightning_from_state,
    build_new_state, validate_state_integrity,
    ShopConfig, MonitorRunner,
)

# ---------------------------------------------------------------------------
# Jump Shop Runner
# ---------------------------------------------------------------------------

JUMP_SHOP = ShopConfig(
    name="JUMP SHOP",
    template_color="red",
    footer="Jump Shop Monitor",
    subtitle_field="vendor",
    shop_url="https://jumpshop-benelic.com",
    config_path="config.json",
    config_example_path="config.example.json",
    db_path_default="data/products.db",
    state_file_default="data/jumpshop_state.json",
    min_product_count=100,
)


class JumpShopRunner(MonitorRunner):
    """Jump Shop 监控运行器"""

    # ---- Config (Jump Shop 特有加载逻辑) ----

    def load_config(self, path="config.json"):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 注入环境变量
        if os.environ.get("FEISHU_WEBHOOK_URL"):
            cfg["notifications"]["feishu"]["webhook_url"] = os.environ["FEISHU_WEBHOOK_URL"]
        if os.environ.get("FEISHU_APP_ID"):
            cfg["notifications"]["feishu"]["app_id"] = os.environ["FEISHU_APP_ID"]
        if os.environ.get("FEISHU_APP_SECRET"):
            cfg["notifications"]["feishu"]["app_secret"] = os.environ["FEISHU_APP_SECRET"]
        if os.environ.get("DEEPSEEK_API_KEY"):
            cfg.setdefault("deepseek", {})["api_key"] = os.environ["DEEPSEEK_API_KEY"]
            cfg["deepseek"].setdefault("enabled", True)
            cfg["deepseek"].setdefault("summary_enabled", True)
        return cfg

    # ---- DB (Jump Shop 表结构) ----

    def init_db(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
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
        # 兼容性迁移
        try:
            conn.execute("SELECT feishu_img_key FROM products LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE products ADD COLUMN feishu_img_key TEXT DEFAULT ''")
        try:
            conn.execute("SELECT last_available_at FROM products LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE products ADD COLUMN last_available_at TEXT DEFAULT ''")
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

    def update_db(self, conn, products, now_str):
        for p in products:
            conn.execute("""
                INSERT INTO products (id, title, handle, vendor, tags, price,
                    available, sku, image_url, url, published_at, updated_at,
                    first_seen, last_checked, last_available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, handle=excluded.handle,
                    vendor=excluded.vendor, tags=excluded.tags,
                    price=excluded.price, available=excluded.available,
                    sku=excluded.sku, image_url=excluded.image_url,
                    url=excluded.url, published_at=excluded.published_at,
                    updated_at=excluded.updated_at,
                    last_checked=excluded.last_checked,
                    last_available_at=CASE
                        WHEN excluded.available = 1 THEN excluded.last_checked
                        ELSE products.last_available_at
                    END
            """, (p["id"], p["title"], p["handle"], p["vendor"], p["tags"],
                  p["price"], p["available"], p["sku"], p["image_url"],
                  p["url"], p["published_at"], p["updated_at"],
                  now_str, now_str, now_str))
        conn.commit()

    # ---- 商品拉取 ----

    def fetch_products(self, cfg):
        raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
        return [normalize_product(p) for p in raw]

    # ---- 通知 (含企业微信 + 邮件) ----

    def _send_all_notifications(self, cfg, conn, changes, now_str):
        # 飞书 (基类处理)
        super()._send_all_notifications(cfg, conn, changes, now_str)
        # 企业微信
        nc = cfg.get("notifications", {})
        if nc.get("wechat_work", {}).get("enabled"):
            _send_wechat_work(nc["wechat_work"]["webhook_url"], changes, now_str)
        # 邮件
        if nc.get("email", {}).get("enabled"):
            _send_email(nc["email"], changes, now_str)


# ---------------------------------------------------------------------------
# Jump Shop API
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
                    logging.warning("Rate limited, waiting %ds...", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                products = data.get("products", [])
                all_products.extend(products)
                break
            except Exception as e:
                logging.warning("Page %d attempt %d/3: %s", page, attempt + 1, e)
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
    """安全解析 Jump Shop 商品数据"""
    variant = (p.get("variants") or [{}])[0]
    if not isinstance(variant, dict):
        variant = {}
    image = (p.get("images") or [{}])[0]
    if not isinstance(image, dict):
        image = {}
    return {
        "id": p.get("id", 0),
        "title": p.get("title", ""),
        "handle": p.get("handle", ""),
        "vendor": p.get("vendor", ""),
        "tags": ",".join(p.get("tags") or []),
        "price": int(float(variant.get("price", 0) or 0)),
        "available": 1 if variant.get("available") else 0,
        "sku": variant.get("sku", "") or "",
        "image_url": image.get("src", "") or "",
        "url": f"https://jumpshop-benelic.com/products/{p.get('handle', '')}",
        "published_at": p.get("published_at", "") or "",
        "updated_at": p.get("updated_at", "") or "",
    }


# ---------------------------------------------------------------------------
# 额外通知渠道 (Jump Shop 特有)
# ---------------------------------------------------------------------------

def _format_text_message(changes, now_str):
    total = len(changes)
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out")
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    lines = [
        f"JUMP SHOP Monitor - {now_str}",
        f"Changes: {total} (New:{new_count} Restock:{restock_count} "
        f"SoldOut:{soldout_count} Price:{price_count})",
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
            lines.append(f"  Y{p['price']} | {status} | {p.get('vendor', '')}")
            if ctype == "price_change":
                lines.append(f"  {c['old_value']} -> {c['new_value']}")
        if len(items) > 30:
            lines.append(f"  ... and {len(items) - 30} more")
    return "\n".join(lines)


def _send_wechat_work(webhook_url, changes, now_str):
    text = _format_text_message(changes, now_str)
    payload = {"msgtype": "text", "text": {"content": text[:4000]}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.json().get("errcode") == 0:
            logging.info("WeChat Work notification sent")
        else:
            logging.error("WeChat Work error: %s", resp.json())
    except Exception as e:
        logging.error("WeChat Work send failed: %s", e)


def _send_email(smtp_config, changes, now_str):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    text = _format_text_message(changes, now_str)
    html = text.replace("\n", "<br>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"JUMP SHOP Monitor - {now_str}"
    msg["From"] = smtp_config["smtp_user"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(f"<pre>{html}</pre>", "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp_config["smtp_host"], smtp_config["smtp_port"],
                          timeout=15) as server:
            server.starttls()
            server.login(smtp_config["smtp_user"], smtp_config["smtp_pass"])
            server.sendmail(smtp_config["smtp_user"],
                           smtp_config["to_emails"], msg.as_string())
        logging.info("Email sent")
    except Exception as e:
        logging.error("Email send failed: %s", e)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    cfg = JumpShopRunner(JUMP_SHOP).load_config()
    setup_logging(cfg["log_file"])

    # 解析命令行参数
    once = "--once" in sys.argv
    silent = "--silent" in sys.argv

    state_file = None
    for arg in sys.argv:
        if arg.startswith("--state-file="):
            state_file = arg.split("=", 1)[1]
        elif arg == "--state-file" and sys.argv.index(arg) + 1 < len(sys.argv):
            state_file = sys.argv[sys.argv.index(arg) + 1]

    db_path = None
    for arg in sys.argv:
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
        elif arg == "--db" and sys.argv.index(arg) + 1 < len(sys.argv):
            db_path = sys.argv[sys.argv.index(arg) + 1]

    runner = JumpShopRunner(JUMP_SHOP)

    # 初始化 DB
    conn = None
    target_db = db_path or cfg.get("database_path", "data/products.db")
    try:
        conn = runner.init_db(target_db)
    except Exception as e:
        logging.warning("DB init failed (non-fatal in state-file mode): %s", e)

    # 判断首次运行
    is_first_run = False
    if conn:
        try:
            cur = conn.execute("SELECT COUNT(*) FROM products")
            is_first_run = cur.fetchone()[0] == 0
        except Exception:
            is_first_run = True
    elif state_file:
        is_first_run = not Path(state_file).exists()

    if is_first_run:
        logging.info("First run - building baseline...")

    if once:
        # ---- JSON State 模式 (GitHub Actions 主路径) ----
        if state_file:
            runner.run_once(cfg, state_file, db_conn=conn, silent=silent)
        else:
            # ---- DB 模式 (向后兼容, 本地开发) ----
            _run_once_db(cfg, conn, is_first_run, silent)
        if conn:
            conn.close()
        return

    # ---- 持续循环 (本地开发) ----
    if state_file:
        runner.run_continuous(cfg, state_file, db_conn=conn)
    else:
        _run_continuous_db(cfg, conn, is_first_run)

    if conn:
        conn.close()
    logging.info("Monitor stopped")


# ---------------------------------------------------------------------------
# DB 模式 (向后兼容, 仅本地开发使用)
# ---------------------------------------------------------------------------

def _run_once_db(cfg, conn, is_first_run=False, silent=False):
    """DB 模式单次检查 (向后兼容)"""
    start = time.time()
    logging.info("Checking Jump Shop (DB mode)...")

    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p) for p in products_raw]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    min_expected = cfg["monitor_options"].get("min_product_count", 100)
    if len(products) < min_expected:
        logging.error("PRODUCT COUNT ANOMALY: got %d, expected >= %d",
                      len(products), min_expected)
        return 0

    changes, _ = _detect_changes_db(conn, products, cfg)
    _detect_soldout_delta_db(conn, products, changes, cfg, now_str)
    _detect_lightning_db(conn, changes, now_str, cfg)

    if changes:
        log_changes(conn, changes, now_str)
    _update_db_simple(conn, products, now_str)

    _dispatch_db(cfg, conn, changes, now_str, is_first_run, silent)

    elapsed = time.time() - start
    logging.info("Done in %.1fs - %d products (DB mode)", elapsed, len(products))
    return len(changes)


def _detect_changes_db(conn, products, cfg):
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    changes = []
    for p in products:
        pid = p["id"]
        cur = conn.execute(
            "SELECT price, available, updated_at FROM products WHERE id=?", (pid,))
        old = cur.fetchone()
        if old is None:
            if cfg["monitor_options"].get("detect_new_products", True):
                changes.append({
                    "product_id": pid, "change_type": "new",
                    "old_value": None,
                    "new_value": f"{p['title']} | Y{p['price']} | "
                                 f"{'in stock' if p['available'] else 'out of stock'}",
                    "product": p,
                })
        else:
            old_price, old_available, old_updated = old
            if cfg["monitor_options"].get("detect_restocks", True) and \
               old_available == 0 and p["available"] == 1:
                changes.append({
                    "product_id": pid, "change_type": "restock",
                    "old_value": "out of stock", "new_value": "in stock",
                    "product": p,
                })
            if cfg["monitor_options"].get("detect_sold_out", True) and \
               old_available == 1 and p["available"] == 0:
                changes.append({
                    "product_id": pid, "change_type": "sold_out",
                    "old_value": "in stock", "new_value": "out of stock",
                    "product": p,
                })
            if cfg["monitor_options"].get("detect_price_changes", True) and \
               old_price != p["price"] and old_price != 0:
                changes.append({
                    "product_id": pid, "change_type": "price_change",
                    "old_value": f"Y{old_price}", "new_value": f"Y{p['price']}",
                    "product": p,
                })
    return changes, now_str


def _detect_soldout_delta_db(conn, products, changes, cfg, now_str):
    """DB 模式 soldout delta (向后兼容)"""
    if not cfg["monitor_options"].get("detect_sold_out", True):
        return
    current_soldout = {p["id"] for p in products if p["available"] == 0}
    row = conn.execute(
        "SELECT soldout_ids FROM soldout_snapshot WHERE id=1").fetchone()
    last_soldout = set()
    if row and row[0]:
        try:
            last_soldout = set(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            pass

    product_map = {p["id"]: p for p in products}
    existing_ids = {c["product_id"] for c in changes}

    for pid in current_soldout - last_soldout:
        if pid not in existing_ids and pid in product_map:
            changes.append({
                "product_id": pid, "change_type": "sold_out",
                "old_value": "in stock", "new_value": "sold out (snapshot)",
                "product": product_map[pid],
            })

    conn.execute(
        "UPDATE soldout_snapshot SET soldout_ids=?, updated_at=? WHERE id=1",
        (json.dumps(list(current_soldout)), now_str))
    conn.commit()


def _detect_lightning_db(conn, changes, now_str, cfg):
    """DB 模式闪电售罄 (向后兼容)"""
    from common import parse_timestamp, format_duration
    threshold = cfg["monitor_options"].get("lightning_sellout_threshold_seconds", 300)
    if threshold <= 0:
        return
    now_dt = parse_timestamp(now_str)
    if not now_dt:
        return
    for c in changes:
        if c["change_type"] != "sold_out":
            continue
        pid = c["product_id"]
        row = conn.execute(
            "SELECT last_available_at, published_at, first_seen "
            "FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        for src_label, ts_val in [
            ("last_available", row[0]),
            ("published", row[1]),
            ("first_seen", row[2]),
        ]:
            if not ts_val:
                continue
            ref = parse_timestamp(ts_val)
            if ref:
                delta = (now_dt - ref).total_seconds()
                if 0 <= delta <= threshold:
                    c["lightning"] = {
                        "sellout_seconds": int(delta),
                        "source": src_label,
                        "display": format_duration(int(delta)),
                    }
                    break


def _update_db_simple(conn, products, now_str):
    for p in products:
        conn.execute("""
            INSERT INTO products (id, title, handle, vendor, tags, price,
                available, sku, image_url, url, published_at, updated_at,
                first_seen, last_checked, last_available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, handle=excluded.handle,
                vendor=excluded.vendor, tags=excluded.tags,
                price=excluded.price, available=excluded.available,
                sku=excluded.sku, image_url=excluded.image_url,
                url=excluded.url, published_at=excluded.published_at,
                updated_at=excluded.updated_at,
                last_checked=excluded.last_checked,
                last_available_at=CASE
                    WHEN excluded.available = 1 THEN excluded.last_checked
                    ELSE products.last_available_at
                END
        """, (p["id"], p["title"], p["handle"], p["vendor"], p["tags"],
              p["price"], p["available"], p["sku"], p["image_url"],
              p["url"], p["published_at"], p["updated_at"],
              now_str, now_str, now_str))
    conn.commit()


def _dispatch_db(cfg, conn, changes, now_str, is_first_run, silent):
    """DB 模式通知分发 (使用 MonitorRunner 的防御层)"""
    if not changes:
        return
    logging.info("Detected %d changes", len(changes))
    for c in changes[:10]:
        label = {"new": "[NEW]", "restock": "[RESTOCK]",
                 "sold_out": "[SOLD OUT]", "price_change": "[PRICE]"}[c['change_type']]
        if c.get("lightning"):
            label += " ⚡"
        logging.info("  %s %s | Y%d", label,
                    c["product"]["title"][:60], c["product"]["price"])
    if len(changes) > 10:
        logging.info("  ... and %d more", len(changes) - 10)

    if is_first_run and not cfg["monitor_options"].get("notify_on_first_run"):
        logging.info("First run - skipping all notifications (baseline build)")
        return
    if silent:
        logging.info("Silent mode - skipping notifications")
        return

    # Use MonitorRunner's dispatch via a temporary runner
    runner = JumpShopRunner(JUMP_SHOP)
    runner._send_all_notifications(cfg, conn, changes, now_str)


def _run_continuous_db(cfg, conn, is_first_run):
    """DB 模式持续循环 (向后兼容)"""
    import signal as sig
    running = True

    def handler(s, f):
        nonlocal running
        logging.info("Shutting down...")
        running = False
    sig.signal(sig.SIGINT, handler)
    sig.signal(sig.SIGTERM, handler)

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info("Continuous monitoring started (interval=%ds, DB mode). "
                 "Press Ctrl+C to stop.", interval)

    while running:
        try:
            _run_once_db(cfg, conn, is_first_run)
            is_first_run = False
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error("Run failed: %s", e, exc_info=True)
        if not running:
            break
        jitter = random.uniform(-0.2, 0.2) * interval
        wait = interval + jitter
        logging.info("Next check in %.0fs...", wait)
        for _ in range(int(wait)):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
