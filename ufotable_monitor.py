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
    detect_soldout_delta, detect_lightning_sellouts,
    build_feishu_cards, send_feishu_card,
    maybe_send_bot_alert,
    save_state_snapshot, load_state_snapshot, detect_changes_from_snapshot,
    load_json_state, save_json_state,
    detect_all_changes_from_state, detect_lightning_from_state,
    build_new_state,
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
    """安全解析 ufotable 商品数据，处理缺失/异常字段"""
    var = (p.get("variations") or [{}])[0]
    if not isinstance(var, dict):
        var = {}
    code = var.get("productCode", "") or ""
    images = p.get("images") or []
    image_url = ""
    if images and isinstance(images[0], dict):
        image_url = images[0].get("url", "") or ""

    works = ""
    category = ""
    for cat in (p.get("categories") or []):
        if not isinstance(cat, dict):
            continue
        if cat.get("groupName") == "works":
            works = cat.get("displayName", "") or ""
        if cat.get("groupName") == "category":
            category = cat.get("displayName", "") or ""

    return {
        "id": p.get("id", 0),
        "product_code": code,
        "title": p.get("title", ""),
        "works": works,
        "category": category,
        "price": var.get("price", 0) or 0,
        "available": 1 if stock_map.get(code, False) else 0,
        "image_url": image_url,
        "url": f"{SHOP_URL}/product/{code}" if code else SHOP_URL,
        "valid_after": p.get("validAfter", "") or "",
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
            INSERT INTO products (id, product_code, title, works, category, price, available, image_url, url, valid_after, first_seen, last_checked, last_available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                product_code=excluded.product_code, title=excluded.title,
                works=excluded.works, category=excluded.category,
                price=excluded.price, available=excluded.available,
                image_url=excluded.image_url, url=excluded.url,
                valid_after=excluded.valid_after, last_checked=excluded.last_checked,
                last_available_at=CASE
                    WHEN excluded.available = 1 THEN excluded.last_checked
                    ELSE products.last_available_at
                END
        """, (p["id"], p["product_code"], p["title"], p["works"], p["category"],
              p["price"], p["available"], p["image_url"], p["url"],
              p["valid_after"], now_str, now_str, now_str))
    conn.commit()


def send_feishu(feishu_cfg, changes, now_str):
    """飞书交互式卡片通知(蓝色模板), 超过50件自动分页"""
    webhook_url = feishu_cfg["webhook_url"]
    cards = build_feishu_cards(changes, now_str, UFOTABLE_CARD)
    send_feishu_card(webhook_url, cards)


def send_notifications(cfg, conn, changes, now_str):
    if not changes:
        return
    nc = cfg.get("notifications", {})
    if nc.get("feishu", {}).get("enabled"):
        feishu_cfg = nc["feishu"]
        if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
            ensure_image_keys(conn, changes, feishu_cfg)
        send_feishu(feishu_cfg, changes, now_str)

        # Bot 扫货预警
        mo = cfg.get("monitor_options", {})
        if mo.get("bot_alert_enabled", True):
            bot_min = mo.get("bot_alert_min_count", 3)
            maybe_send_bot_alert(
                feishu_cfg["webhook_url"], changes, now_str,
                UFOTABLE_CARD, min_count=bot_min
            )


def _dispatch_notifications_ufo(cfg, conn, changes, now_str, is_first_run, silent):
    """统一的变更通知分发逻辑"""
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

    new_count = sum(1 for c in changes if c["change_type"] == "new")
    fuse_threshold = cfg.get("monitor_options", {}).get("new_product_fuse_threshold", 150)
    if new_count > fuse_threshold:
        logging.warning(
            f"CIRCUIT BREAKER: {new_count} new products exceeds threshold {fuse_threshold}"
        )
        if not silent and (not is_first_run or cfg.get("monitor_options", {}).get("notify_on_first_run")):
            summary_text = (
                f"ufotable WEBSHOP 異常検知\n\n"
                f"新商品数 {new_count} 件が闘値 {fuse_threshold} を超えました。\n"
                f"キャッシュ破損の可能性あり。データは正常に更新済みです。\n"
                f"他: 補貨 {sum(1 for c in changes if c['change_type']=='restock')} / "
                f"售罄 {sum(1 for c in changes if c['change_type']=='sold_out')} / "
                f"価格変更 {sum(1 for c in changes if c['change_type']=='price_change')}\n\n"
                f"{now_str} | ufotable Monitor"
            )
            feishu_cfg = cfg.get("notifications", {}).get("feishu", {})
            if feishu_cfg.get("enabled") and feishu_cfg.get("webhook_url"):
                send_feishu_card(
                    feishu_cfg["webhook_url"],
                    {"msg_type": "text", "content": {"text": summary_text}},
                )
    elif silent:
        logging.info("Silent mode - skipping notifications")
    elif is_first_run and not cfg.get("monitor_options", {}).get("notify_on_first_run"):
        logging.info("First run - skipping notifications")
    else:
        send_notifications(cfg, conn, changes, now_str)


def run_once(cfg, conn, is_first_run=False, silent=False, recover_from=None, state_file=None):
    start = time.time()
    logging.info("Checking ufotable WEBSHOP...")

    products_raw, stock_map = fetch_data()
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [normalize_product(p, stock_map) for p in products_raw]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # ---- 产品数异常检测 ----
    min_expected = cfg.get("monitor_options", {}).get("min_product_count", 10)
    if len(products) < min_expected:
        logging.error(
            "PRODUCT COUNT ANOMALY: got %d products, expected >= %d. Skipping cycle.",
            len(products), min_expected
        )
        return 0

    # ---- JSON State 模式 (主路径) ----
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
                    "PRODUCT COUNT DRIFT: %d → %d (%.0f%%). Possible API issue.",
                    old_total, len(products), ratio * 100
                )

        changes, soldout_ids = detect_all_changes_from_state(old_state, products, now_str, cfg)

        lightning_threshold = cfg.get("monitor_options", {}).get("lightning_sellout_threshold_seconds", 300)
        detect_lightning_from_state(old_state, changes, now_str, lightning_threshold)

        new_products = build_new_state(products, old_state, now_str)
        new_state = {
            "version": 2,
            "updated_at": now_str,
            "total_products": len(products),
            "soldout_ids": soldout_ids,
            "products": new_products,
        }

        _dispatch_notifications_ufo(cfg, conn, changes, now_str,
                                    is_first_run=first_run, silent=silent)

        if changes and conn:
            try:
                log_changes(conn, changes, now_str)
            except Exception as e:
                logging.warning("Failed to log changes to DB: %s", e)

        save_json_state(new_state, state_file)

        if conn:
            try:
                update_db(conn, products, now_str)
            except Exception as e:
                logging.warning("Failed to update DB: %s", e)

        elapsed = time.time() - start
        logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked (JSON state)")
        return len(changes)

    # ---- DB 模式 (向后兼容) ----
    recovered_changes = []
    if recover_from:
        old_snapshot = load_state_snapshot(recover_from)
        if old_snapshot:
            recovered_changes = detect_changes_from_snapshot(old_snapshot, products, cfg)
            logging.info(f"Recovered {len(recovered_changes)} changes from snapshot ({len(old_snapshot)} old products)")

    changes, _ = detect_changes(conn, products, cfg)

    detect_sold_out = cfg.get("monitor_options", {}).get("detect_sold_out", True)
    snapshot_changes = detect_soldout_delta(conn, products, detect_sold_out, now_str)

    existing_ids = {c["product_id"]: c for c in changes}
    for sc in snapshot_changes:
        if sc["product_id"] not in existing_ids:
            changes.append(sc)
            existing_ids.add(sc["product_id"])
    for rc in recovered_changes:
        if rc["product_id"] not in existing_ids:
            changes.append(rc)

    lightning_threshold = cfg.get("monitor_options", {}).get("lightning_sellout_threshold_seconds", 300)
    detect_lightning_sellouts(conn, changes, now_str, lightning_threshold, publish_field="valid_after")

    notify_is_first = is_first_run and bool(recovered_changes) and not cfg.get("monitor_options", {}).get("notify_on_first_run")
    if notify_is_first:
        notify_changes = [c for c in changes if c["product_id"] in {rc["product_id"] for rc in recovered_changes}]
        logging.info(f"Recovery mode: sending {len(notify_changes)}/{len(changes)} recovered changes")
        _dispatch_notifications_ufo(cfg, conn, notify_changes, now_str,
                                    is_first_run=False, silent=silent)
    else:
        _dispatch_notifications_ufo(cfg, conn, changes, now_str,
                                    is_first_run=is_first_run and not recovered_changes,
                                    silent=silent)

    if changes:
        log_changes(conn, changes, now_str)

    update_db(conn, products, now_str)

    snapshot_path = cfg.get("state_snapshot_path", "data/ufotable_state_snapshot.json")
    try:
        rows = conn.execute(
            "SELECT id, title, available, price, image_url, url, works FROM products"
        ).fetchall()
        snap = {}
        for row in rows:
            pid, title, available, price, image_url, url, works = row
            snap[str(pid)] = {
                "title": title, "available": available, "price": price,
                "image_url": image_url, "url": url, "vendor": works,
                "handle": "",
            }
        save_state_snapshot(snap, snapshot_path)
    except Exception as e:
        logging.warning(f"Failed to save state snapshot: {e}")

    elapsed = time.time() - start
    logging.info(f"Done in {elapsed:.1f}s - {len(products)} products tracked (DB mode)")
    return len(changes)


def signal_handler(sig, frame):
    global running
    logging.info("Shutting down...")
    running = False


def main():
    global running
    cfg = load_config()
    db_path_default = cfg.get("database_path", "data/ufotable.db")
    setup_logging(cfg.get("log_file", "data/ufotable_monitor.log"),
                  fmt="%(asctime)s [UFOTABLE] %(message)s")

    # 解析命令行参数
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

    recover_from = None
    for arg in sys.argv:
        if arg.startswith("--recover-from="):
            recover_from = arg.split("=", 1)[1]
        elif arg == "--recover-from" and sys.argv.index(arg) + 1 < len(sys.argv):
            recover_from = sys.argv[sys.argv.index(arg) + 1]

    # 初始化 DB
    conn = None
    if state_file:
        target_db = db_path or db_path_default
        try:
            conn = init_db(target_db)
        except Exception as e:
            logging.warning("DB init failed (non-fatal in state-file mode): %s", e)
    else:
        conn = init_db(db_path or db_path_default)

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

    if "--once" in sys.argv:
        run_once(cfg, conn, is_first_run=is_first_run, silent=silent,
                 recover_from=recover_from, state_file=state_file)
        if conn:
            conn.close()
        return

    interval = cfg.get("poll_interval_seconds", 300)
    logging.info(f"Continuous monitoring started (interval={interval}s)")

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
