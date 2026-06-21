#!/usr/bin/env python3
"""
ufotable WEBSHOP (webshop.ufotable.co.jp) 商品监控
API: MODD platform (client-api.modd.com/UFWE)
检测: 新商品 / 補貨 / 售罄 / 価格変更
用法:
  python ufotable_monitor.py --once --state-file=data/ufotable_state.json --db=data/ufotable.db
"""

import json
import os
import sys
import time
import sqlite3
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
# ufotable Runner
# ---------------------------------------------------------------------------

API_BASE = "https://client-api.modd.com/UFWE"
SHOP_URL = "https://webshop.ufotable.co.jp"

UFOTABLE = ShopConfig(
    name="ufotable WEBSHOP",
    template_color="blue",
    footer="ufotable WEBSHOP Monitor",
    subtitle_field="works",
    shop_url=SHOP_URL,
    api_base=API_BASE,
    config_path="ufotable_config.json",
    config_example_path="ufotable_config.example.json",
    db_path_default="data/ufotable.db",
    state_file_default="data/ufotable_state.json",
    min_product_count=10,
)


class UfotableRunner(MonitorRunner):
    """ufotable WEBSHOP 监控运行器"""

    # ---- Config ----

    def load_config(self, path="ufotable_config.json"):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = json.load(open("ufotable_config.example.json",
                                "r", encoding="utf-8"))
        env_map = {
            "FEISHU_WEBHOOK_URL": "webhook_url",
            "FEISHU_APP_ID": "app_id",
            "FEISHU_APP_SECRET": "app_secret",
        }
        for env_key, cfg_key in env_map.items():
            if os.environ.get(env_key):
                nc = cfg.setdefault("notifications", {}).setdefault("feishu", {})
                nc[cfg_key] = os.environ[env_key]
        if os.environ.get("DEEPSEEK_API_KEY"):
            cfg.setdefault("deepseek", {})["api_key"] = os.environ["DEEPSEEK_API_KEY"]
            cfg["deepseek"].setdefault("enabled", True)
            cfg["deepseek"].setdefault("summary_enabled", True)
        return cfg

    # ---- DB ----

    def init_db(self, db_path):
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
        conn.commit()
        return conn

    def update_db(self, conn, products, now_str):
        for p in products:
            conn.execute("""
                INSERT INTO products (id, product_code, title, works, category,
                    price, available, image_url, url, valid_after,
                    first_seen, last_checked, last_available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    product_code=excluded.product_code,
                    title=excluded.title,
                    works=excluded.works, category=excluded.category,
                    price=excluded.price, available=excluded.available,
                    image_url=excluded.image_url, url=excluded.url,
                    valid_after=excluded.valid_after,
                    last_checked=excluded.last_checked,
                    last_available_at=CASE
                        WHEN excluded.available = 1 THEN excluded.last_checked
                        ELSE products.last_available_at
                    END
            """, (p["id"], p["product_code"], p["title"], p["works"],
                  p["category"], p["price"], p["available"], p["image_url"],
                  p["url"], p["valid_after"], now_str, now_str, now_str))
        conn.commit()

    # ---- 商品拉取 ----

    def fetch_products(self, cfg):
        """ufotable: 需要分别请求 product + productStock"""
        products_raw, stock_map = _fetch_data()
        return [_normalize_product(p, stock_map) for p in products_raw]


# ---------------------------------------------------------------------------
# ufotable API
# ---------------------------------------------------------------------------

def _fetch_data():
    """拉取商品列表和库存"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36",
        "Origin": "https://webshop.ufotable.co.jp",
        "Referer": "https://webshop.ufotable.co.jp/",
    }

    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/product", headers=headers,
                               timeout=30)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products", [])
            break
        except Exception as e:
            logging.warning("Product fetch attempt %d/3: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)
            else:
                return [], {}

    stock_map = {}
    for attempt in range(3):
        try:
            resp = requests.get(f"{API_BASE}/productStock", headers=headers,
                               timeout=30)
            resp.raise_for_status()
            stock_list = resp.json()
            for s in stock_list:
                stock_map[s["productCode"]] = s.get("available", False)
            break
        except Exception as e:
            logging.warning("Stock fetch attempt %d/3: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)

    return products, stock_map


def _normalize_product(p, stock_map):
    """安全解析 ufotable 商品数据"""
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


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    runner = UfotableRunner(UFOTABLE)
    cfg = runner.load_config()
    setup_logging(cfg.get("log_file", "data/ufotable_monitor.log"),
                  fmt="%(asctime)s [UFOTABLE] %(message)s")

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

    # 初始化 DB
    conn = None
    target_db = db_path or cfg.get("database_path", "data/ufotable.db")
    try:
        conn = runner.init_db(target_db)
    except Exception as e:
        logging.warning("DB init failed (non-fatal): %s", e)

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

    if "--once" in sys.argv:
        # ---- JSON State 模式 (GitHub Actions 主路径) ----
        if state_file:
            runner.run_once(cfg, state_file, db_conn=conn, silent=silent)
        else:
            # ---- DB 模式 (向后兼容) ----
            _run_once_db(cfg, conn, is_first_run, silent)
        if conn:
            conn.close()
        return

    # ---- 持续循环 ----
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
    """DB 模式单次检查"""
    import signal as sig
    start = time.time()
    logging.info("Checking ufotable WEBSHOP (DB mode)...")

    products_raw, stock_map = _fetch_data()
    if not products_raw:
        logging.error("Failed to fetch products")
        return 0

    products = [_normalize_product(p, stock_map) for p in products_raw]
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    min_expected = cfg.get("monitor_options", {}).get("min_product_count", 10)
    if len(products) < min_expected:
        logging.error("PRODUCT COUNT ANOMALY: got %d, expected >= %d",
                      len(products), min_expected)
        return 0

    changes, _ = _detect_changes_db(conn, products, cfg)

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
        cur = conn.execute("SELECT price, available FROM products WHERE id=?",
                          (pid,))
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
            mo = cfg.get("monitor_options", {})
            if mo.get("detect_restocks", True) and \
               old_available == 0 and p["available"] == 1:
                changes.append({
                    "product_id": pid, "change_type": "restock",
                    "old_value": "out of stock", "new_value": "in stock",
                    "product": p,
                })
            if mo.get("detect_sold_out", True) and \
               old_available == 1 and p["available"] == 0:
                changes.append({
                    "product_id": pid, "change_type": "sold_out",
                    "old_value": "in stock", "new_value": "out of stock",
                    "product": p,
                })
            if mo.get("detect_price_changes", True) and \
               old_price != p["price"] and old_price != 0:
                changes.append({
                    "product_id": pid, "change_type": "price_change",
                    "old_value": f"Y{old_price}", "new_value": f"Y{p['price']}",
                    "product": p,
                })
    return changes, now_str


def _update_db_simple(conn, products, now_str):
    for p in products:
        conn.execute("""
            INSERT INTO products (id, product_code, title, works, category,
                price, available, image_url, url, valid_after,
                first_seen, last_checked, last_available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                product_code=excluded.product_code, title=excluded.title,
                works=excluded.works, category=excluded.category,
                price=excluded.price, available=excluded.available,
                image_url=excluded.image_url, url=excluded.url,
                valid_after=excluded.valid_after,
                last_checked=excluded.last_checked,
                last_available_at=CASE
                    WHEN excluded.available = 1 THEN excluded.last_checked
                    ELSE products.last_available_at
                END
        """, (p["id"], p["product_code"], p["title"], p["works"],
              p["category"], p["price"], p["available"], p["image_url"],
              p["url"], p["valid_after"], now_str, now_str, now_str))
    conn.commit()


def _dispatch_db(cfg, conn, changes, now_str, is_first_run, silent):
    if not changes:
        return
    logging.info("Detected %d changes", len(changes))
    for c in changes[:10]:
        label = {"new": "[NEW]", "restock": "[RESTOCK]",
                 "sold_out": "[SOLD OUT]", "price_change": "[PRICE]"}[c['change_type']]
        logging.info("  %s %s | Y%d", label,
                    c["product"]["title"][:60], c["product"]["price"])

    if is_first_run and not cfg.get("monitor_options", {}).get(
            "notify_on_first_run"):
        logging.info("First run - skipping all notifications (baseline build)")
        return
    if silent:
        return

    runner = UfotableRunner(UFOTABLE)
    runner._send_all_notifications(cfg, conn, changes, now_str)


def _run_continuous_db(cfg, conn, is_first_run):
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
