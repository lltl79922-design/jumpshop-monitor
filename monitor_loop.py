#!/usr/bin/env python3
"""
Jump Shop 持续监控版本 - 商品上新+补货通知 (飞书卡片+图片预览)
用法: python monitor_loop.py
      python monitor_loop.py --once --state-file=data/jumpshop_state.json --db=data/products.db
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
from datetime import datetime
from pathlib import Path

import requests

from common import (
    JST, CHANGE_LABELS,
    setup_logging, log_changes,
    ensure_image_keys,
    detect_soldout_delta, detect_lightning_sellouts,
    build_feishu_cards, send_feishu_card,
    maybe_send_bot_alert,
    save_state_snapshot, build_snapshot_from_db,
    load_state_snapshot, detect_changes_from_snapshot,
    load_json_state, save_json_state,
    detect_all_changes_from_state, detect_lightning_from_state,
    build_new_state,
)

running = True

JUMP_SHOP_CARD = {
    "name": "JUMP SHOP",
    "template_color": "red",
    "footer": "Jump Shop Monitor",
    "subtitle_field": "vendor",
}

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if os.environ.get("FEISHU_WEBHOOK_URL"):
        cfg["notifications"]["feishu"]["webhook_url"] = os.environ["FEISHU_WEBHOOK_URL"]
    if os.environ.get("FEISHU_APP_ID"):
        cfg["notifications"]["feishu"]["app_id"] = os.environ["FEISHU_APP_ID"]
    if os.environ.get("FEISHU_APP_SECRET"):
        cfg["notifications"]["feishu"]["app_secret"] = os.environ["FEISHU_APP_SECRET"]

    return cfg

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
    """安全解析商品数据，处理缺失/异常字段"""
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

# ---------------------------------------------------------------------------
# 数据库更新
# ---------------------------------------------------------------------------
def update_db(conn, products, now_str):
    for p in products:
        conn.execute("""
            INSERT INTO products (id, title, handle, vendor, tags, price, available, sku, image_url, url, published_at, updated_at, first_seen, last_checked, last_available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, handle=excluded.handle, vendor=excluded.vendor,
                tags=excluded.tags, price=excluded.price, available=excluded.available,
                sku=excluded.sku, image_url=excluded.image_url, url=excluded.url,
                published_at=excluded.published_at, updated_at=excluded.updated_at,
                last_checked=excluded.last_checked,
                last_available_at=CASE
                    WHEN excluded.available = 1 THEN excluded.last_checked
                    ELSE products.last_available_at
                END
        """, (p["id"], p["title"], p["handle"], p["vendor"], p["tags"],
              p["price"], p["available"], p["sku"], p["image_url"], p["url"],
              p["published_at"], p["updated_at"], now_str, now_str, now_str))
    conn.commit()

# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
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
    """飞书交互式卡片通知(含图片预览), 超过50件自动分页"""
    webhook_url = feishu_cfg["webhook_url"]
    cards = build_feishu_cards(changes, now_str, JUMP_SHOP_CARD)
    fallback = "JUMP SHOP Monitor\n\n" + format_text_message(changes, now_str)[:8000]
    send_feishu_card(webhook_url, cards, fallback)


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

    if nc.get("feishu", {}).get("enabled"):
        feishu_cfg = nc["feishu"]
        if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
            ensure_image_keys(conn, changes, feishu_cfg)
        send_feishu(feishu_cfg, changes, now_str)

        # Bot 扫货预警: 闪电售罄数量达到阈值时发送独立红色报警卡片
        mo = cfg.get("monitor_options", {})
        if mo.get("bot_alert_enabled", True):
            bot_min = mo.get("bot_alert_min_count", 3)
            maybe_send_bot_alert(
                feishu_cfg["webhook_url"], changes, now_str,
                JUMP_SHOP_CARD, min_count=bot_min
            )

    if nc.get("wechat_work", {}).get("enabled"):
        send_wechat_work(nc["wechat_work"]["webhook_url"], changes, now_str)
    if nc.get("email", {}).get("enabled"):
        send_email(nc["email"], changes, now_str)

# ---------------------------------------------------------------------------
# 单次检查
# ---------------------------------------------------------------------------
def _dispatch_notifications(cfg, conn, changes, now_str, is_first_run, silent):
    """统一的变更通知分发逻辑 (JSON State / DB 模式共用)"""
    if not changes:
        return

    logging.info(f"Detected {len(changes)} changes")
    for c in changes[:10]:
        p = c["product"]
        label = CHANGE_LABELS[c['change_type']]
        if c.get("lightning"):
            label += " ⚡"
        logging.info(f"  {label} {p['title'][:60]} | Y{p['price']}")
    if len(changes) > 10:
        logging.info(f"  ... and {len(changes)-10} more")

    # 熔断: 上新数超过阈值
    new_count = sum(1 for c in changes if c["change_type"] == "new")
    fuse_threshold = cfg["monitor_options"].get("new_product_fuse_threshold", 150)
    if new_count > fuse_threshold:
        logging.warning(
            f"CIRCUIT BREAKER: {new_count} new products exceeds threshold {fuse_threshold}"
        )
        if not silent and (not is_first_run or cfg["monitor_options"].get("notify_on_first_run")):
            summary_text = (
                f"JUMP SHOP 異常検知\n\n"
                f"新商品数 {new_count} 件が闾値 {fuse_threshold} を超えました。\n"
                f"キャッシュ破損の可能性あり。データは正常に更新済みです。\n"
                f"他: 補貨 {sum(1 for c in changes if c['change_type']=='restock')} / "
                f"售羄 {sum(1 for c in changes if c['change_type']=='sold_out')} / "
                f"価格変更 {sum(1 for c in changes if c['change_type']=='price_change')}\n\n"
                f"{now_str} | Jump Shop Monitor"
            )
            feishu_cfg = cfg["notifications"].get("feishu", {})
            if feishu_cfg.get("enabled") and feishu_cfg.get("webhook_url"):
                send_feishu_card(
                    feishu_cfg["webhook_url"],
                    {"msg_type": "text", "content": {"text": summary_text}},
                )
    elif silent:
        logging.info("Silent mode - skipping notifications")
    elif is_first_run and not cfg["monitor_options"].get("notify_on_first_run"):
        logging.info("First run - skipping notifications")
    else:
        send_notifications(cfg, conn, changes, now_str)


def run_once(cfg, conn, is_first_run=False, silent=False, recover_from=None, state_file=None):
    start = time.time()
    logging.info("Checking Jump Shop...")

    products_raw = fetch_all_products(cfg["shop_url"], cfg["user_agents"])
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p) for p in products_raw]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # ---- 产品数异常检测 (API返回部分数据会导致假上新) ----
    min_expected = cfg["monitor_options"].get("min_product_count", 100)
    if len(products) < min_expected:
        logging.error(
            "PRODUCT COUNT ANOMALY: got %d products, expected >= %d. "
            "API may be returning partial data — skipping this cycle.",
            len(products), min_expected
        )
        return 0

    # ---- JSON State 模式 (主路径, Git持久化, 可靠) ----
    if state_file:
        old_state = load_json_state(state_file)
        first_run = len(old_state.get("products", {})) == 0

        if first_run:
            logging.info("First run with JSON state - building baseline...")

        # 产品数突变检测
        old_total = old_state.get("total_products", 0)
        if old_total > 0:
            ratio = len(products) / old_total
            if ratio < 0.5 or ratio > 1.5:
                logging.warning(
                    "PRODUCT COUNT DRIFT: %d → %d (%.0f%%). "
                    "Possible API issue or massive inventory change.",
                    old_total, len(products), ratio * 100
                )

        # 变更检测 (new/restock/sold_out/price_change + soldout_delta 一次完成)
        changes, soldout_ids = detect_all_changes_from_state(old_state, products, now_str, cfg)

        # 闪电售罄检测
        lightning_threshold = cfg["monitor_options"].get("lightning_sellout_threshold_seconds", 300)
        detect_lightning_from_state(old_state, changes, now_str, lightning_threshold)

        # 构建并保存新状态
        new_products = build_new_state(products, old_state, now_str)
        new_state = {
            "version": 2,
            "updated_at": now_str,
            "total_products": len(products),
            "soldout_ids": soldout_ids,
            "products": new_products,
        }

        _dispatch_notifications(cfg, conn, changes, now_str,
                                is_first_run=first_run, silent=silent)

        # 记录变更到 DB (如果能写的话, 供 analysis.py 使用)
        if changes and conn:
            try:
                log_changes(conn, changes, now_str)
            except Exception as e:
                logging.warning("Failed to log changes to DB: %s", e)

        save_json_state(new_state, state_file)

        # 同步 DB (可选, 供本地 analysis.py)
        if conn:
            try:
                update_db(conn, products, now_str)
            except Exception as e:
                logging.warning("Failed to update DB: %s", e)

        elapsed = time.time() - start
        logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked (JSON state)")
        return len(changes)

    # ---- DB 模式 (向后兼容, 本地开发/旧workflow) ----
    recovered_changes = []
    if recover_from:
        old_snapshot = load_state_snapshot(recover_from)
        if old_snapshot:
            recovered_changes = detect_changes_from_snapshot(old_snapshot, products, cfg)
            logging.info(f"Recovered {len(recovered_changes)} changes from snapshot ({len(old_snapshot)} old products)")

    changes, _ = detect_changes(conn, products, cfg)

    detect_sold_out = cfg["monitor_options"].get("detect_sold_out", True)
    snapshot_changes = detect_soldout_delta(conn, products, detect_sold_out, now_str)

    existing_ids = {c["product_id"]: c for c in changes}
    for sc in snapshot_changes:
        if sc["product_id"] not in existing_ids:
            changes.append(sc)
            existing_ids.add(sc["product_id"])
    for rc in recovered_changes:
        if rc["product_id"] not in existing_ids:
            changes.append(rc)

    lightning_threshold = cfg["monitor_options"].get("lightning_sellout_threshold_seconds", 300)
    detect_lightning_sellouts(conn, changes, now_str, lightning_threshold, publish_field="published_at")

    # 恢复模式下仅发从快照恢复的变更
    notify_is_first = is_first_run and bool(recovered_changes) and not cfg["monitor_options"].get("notify_on_first_run")
    if notify_is_first:
        notify_changes = [c for c in changes if c["product_id"] in {rc["product_id"] for rc in recovered_changes}]
        logging.info(f"Recovery mode: sending {len(notify_changes)}/{len(changes)} recovered changes")
        _dispatch_notifications(cfg, conn, notify_changes, now_str,
                                is_first_run=False, silent=silent)
    else:
        _dispatch_notifications(cfg, conn, changes, now_str,
                                is_first_run=is_first_run and not recovered_changes,
                                silent=silent)

    if changes:
        log_changes(conn, changes, now_str)

    update_db(conn, products, now_str)

    # 状态快照备份 (灾难恢复用)
    snapshot_path = cfg.get("state_snapshot_path", "data/state_snapshot.json")
    try:
        snapshot_data = build_snapshot_from_db(conn)
        save_state_snapshot(snapshot_data, snapshot_path)
    except Exception as e:
        logging.warning(f"Failed to save state snapshot: {e}")

    elapsed = time.time() - start
    logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked (DB mode)")
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

    # 解析命令行参数
    once = "--once" in sys.argv
    silent = "--silent" in sys.argv

    # --state-file=<path>: JSON State 模式 (Git 持久化)
    state_file = None
    for arg in sys.argv:
        if arg.startswith("--state-file="):
            state_file = arg.split("=", 1)[1]
        elif arg == "--state-file" and sys.argv.index(arg) + 1 < len(sys.argv):
            state_file = sys.argv[sys.argv.index(arg) + 1]

    # --db=<path>: 可选 DB (仅用于 change_log, 不影响变更检测)
    db_path = None
    for arg in sys.argv:
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
        elif arg == "--db" and sys.argv.index(arg) + 1 < len(sys.argv):
            db_path = sys.argv[sys.argv.index(arg) + 1]

    # --recover-from=<path>: 灾难恢复 (仅 DB 模式)
    recover_from = None
    for arg in sys.argv:
        if arg.startswith("--recover-from="):
            recover_from = arg.split("=", 1)[1]
        elif arg == "--recover-from" and sys.argv.index(arg) + 1 < len(sys.argv):
            recover_from = sys.argv[sys.argv.index(arg) + 1]

    # 初始化 DB (change_log + 向后兼容)
    conn = None
    if state_file:
        # JSON State 模式: DB 可选, 仅用于 change_log
        if db_path:
            try:
                conn = init_db(db_path)
            except Exception as e:
                logging.warning("DB init failed, change_log disabled: %s", e)
        else:
            default_db = cfg.get("database_path", "data/products.db")
            try:
                conn = init_db(default_db)
            except Exception as e:
                logging.warning("DB init failed (non-fatal in state-file mode): %s", e)
    else:
        # DB 模式: DB 必须可用
        conn = init_db(cfg["database_path"])

    # 判断是否首次运行
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

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if once:
        run_once(cfg, conn, is_first_run=is_first_run, silent=silent,
                 recover_from=recover_from, state_file=state_file)
        if conn:
            conn.close()
        return

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info(f"Continuous monitoring started (interval={interval}s). Press Ctrl+C to stop.")

    while running:
        try:
            run_once(cfg, conn, is_first_run=is_first_run, silent=silent,
                     recover_from=recover_from, state_file=state_file)
            is_first_run = False
            recover_from = None
        except KeyboardInterrupt:
            logging.info("Keyboard interrupt received")
            break
        except SystemExit:
            break
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

    if conn:
        conn.close()
    logging.info("Monitor stopped")

if __name__ == "__main__":
    main()
