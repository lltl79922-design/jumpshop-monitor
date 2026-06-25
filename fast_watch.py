#!/usr/bin/env python3
"""
Fast Watch — 指定商品高频轮询
用途: 对热门商品每30秒检查一次库存，抢在bot前发现补货/上新
与 monitor.py 互补: monitor 负责全局扫描，fast_watch 负责特定商品秒级盯梢
"""

import json
import os
import time
import random
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
WATCHLIST_FILE = DATA_DIR / "fast_watchlist.json"
STATE_FILE = DATA_DIR / "fast_watch_state.json"
LOG_FILE = SCRIPT_DIR / "logs" / "fast_watch.log"

# ---------------------------------------------------------------------------
def setup_logging():
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

# ---------------------------------------------------------------------------
def load_watchlist():
    """加载盯梢清单"""
    if WATCHLIST_FILE.exists():
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # 默认空清单
    return {"products": [], "config": {"interval_seconds": 30, "user_agents": []}}

def save_watchlist(wl):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)

def load_state():
    """加载上次状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
def check_product(product_id, user_agents):
    """检查单个商品的库存状态"""
    url = f"https://jumpshop-benelic.com/products/{product_id}.json"
    ua = random.choice(user_agents) if user_agents else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            return {"id": product_id, "status": "not_found", "title": None, "price": None}
        resp.raise_for_status()
        p = resp.json().get("product", {})
        v = p.get("variants", [{}])[0] if p.get("variants") else {}
        return {
            "id": product_id,
            "title": p.get("title"),
            "price": int(float(v.get("price", 0))),
            "available": v.get("available", False),
            "status": "available" if v.get("available") else "sold_out",
            "updated_at": p.get("updated_at"),
            "handle": p.get("handle"),
            "url": f"https://jumpshop-benelic.com/products/{p.get('handle','')}" if p.get("handle") else None,
        }
    except requests.exceptions.Timeout:
        return {"id": product_id, "status": "timeout", "title": None, "price": None}
    except Exception as e:
        return {"id": product_id, "status": "error", "title": str(e), "price": None}

# ---------------------------------------------------------------------------
def send_feishu_alert(webhook_url, product_info, change_type):
    """飞书即时告警"""
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    emoji = {"restock": "🟢", "sold_out": "🔴", "new": "🆕", "price_change": "💰"}

    if change_type == "restock":
        title = f"{emoji[change_type]} 补货告警！{product_info['title']}"
    elif change_type == "new":
        title = f"{emoji[change_type]} 新商品上架！{product_info['title']}"
    elif change_type == "sold_out":
        title = f"{emoji[change_type]} 售罄确认：{product_info['title']}"
    else:
        title = f"{emoji.get(change_type,'')} {product_info['title']}"

    text = f"""{title}
商品: {product_info['title']}
价格: ¥{product_info['price']}
状态: {'在庫あり' if product_info.get('available') else '在庫なし'}
链接: {product_info.get('url', f'https://jumpshop-benelic.com/products/{product_info["id"]}')}
检测时间: {now_str}"""

    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.json().get("code") == 0:
            logging.info(f"Feishu alert sent: {change_type} - {product_info['title']}")
        else:
            logging.error(f"Feishu error: {resp.json()}")
    except Exception as e:
        logging.error(f"Feishu send failed: {e}")

# ---------------------------------------------------------------------------
def main_loop():
    setup_logging()
    logging.info("Fast Watch starting...")

    wl = load_watchlist()
    if not wl["products"]:
        logging.warning("Watchlist is empty. Add products via fast_watchlist.json")
        return

    interval = wl["config"].get("interval_seconds", 30)
    user_agents = wl["config"].get("user_agents", [])
    feishu_url = wl["config"].get("feishu_webhook_url", "")

    logging.info(f"Watching {len(wl['products'])} products every {interval}s")

    # 加载上次状态，用于对比
    state = load_state()

    while True:
        try:
            new_state = {}
            for item in wl["products"]:
                pid = str(item["id"])
                product = check_product(pid, user_agents)

                # 记录状态
                new_state[pid] = {
                    "status": product["status"],
                    "available": product.get("available", False),
                    "price": product.get("price"),
                    "title": product.get("title"),
                    "updated_at": product.get("updated_at"),
                }

                # 对比变更
                old = state.get(pid, {})
                old_status = old.get("status", "unknown")
                new_status = product["status"]

                if old_status == "unknown" and new_status in ("available", "sold_out"):
                    # 首次运行，静默记录
                    logging.info(f"[INIT] {pid} {product.get('title','?')}: {new_status}")
                elif old_status == "sold_out" and new_status == "available":
                    logging.info(f"[RESTOCK] {pid} {product.get('title','?')}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "restock")
                elif old_status == "available" and new_status == "sold_out":
                    logging.info(f"[SOLD_OUT] {pid} {product.get('title','?')}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "sold_out")
                elif old_status == "not_found" and new_status != "not_found":
                    logging.info(f"[NEW] {pid} {product.get('title','?')} just appeared!")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "new")
                elif old.get("price") and product.get("price") and old["price"] != product["price"]:
                    logging.info(f"[PRICE] {pid} {old['price']} -> {product['price']}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "price_change")

                # 请求间隔（针对单个商品）
                time.sleep(random.uniform(0.5, 1.5))

            # 保存最新状态
            state = new_state
            save_state(state)

            # 轮次间隔
            logging.debug(f"Watch cycle complete. Next in {interval}s...")
            time.sleep(interval)

        except KeyboardInterrupt:
            logging.info("Fast Watch stopped by user")
            break
        except Exception as e:
            logging.error(f"Watch cycle error: {e}", exc_info=True)
            time.sleep(interval)

if __name__ == "__main__":
    # ---- 命令行操作 ----
    import sys

    if len(sys.argv) >= 2:
        cmd = sys.argv[1]

        if cmd == "add":
            # python fast_watch.py add 18045905
            if len(sys.argv) >= 3:
                pid = sys.argv[2]
                wl = load_watchlist()
                if pid not in [str(p["id"]) for p in wl["products"]]:
                    wl["products"].append({"id": pid, "label": input("Label (optional): ").strip() or ""})
                    save_watchlist(wl)
                    print(f"Added product {pid} to watchlist")
                else:
                    print(f"Product {pid} already in watchlist")
            else:
                print("Usage: python fast_watch.py add <product_id>")

        elif cmd == "remove":
            # python fast_watch.py remove 18045905
            if len(sys.argv) >= 3:
                pid = sys.argv[2]
                wl = load_watchlist()
                wl["products"] = [p for p in wl["products"] if str(p["id"]) != pid]
                save_watchlist(wl)
                print(f"Removed product {pid} from watchlist")
            else:
                print("Usage: python fast_watch.py remove <product_id>")

        elif cmd == "list":
            wl = load_watchlist()
            print(f"Watchlist ({len(wl['products'])} products):")
            for p in wl["products"]:
                print(f"  {p['id']}: {p.get('label','(no label)')}")

        elif cmd == "check":
            # python fast_watch.py check  —  一次性检查所有商品（仅打印）
            setup_logging()
            wl = load_watchlist()
            ua = wl["config"].get("user_agents", [])
            for item in wl["products"]:
                pid = str(item["id"])
                p = check_product(pid, ua)
                status_icon = "[OK]" if p["status"] == "available" else ("[SOLD]" if p["status"] == "sold_out" else "[???]")
                print(f"{status_icon} {pid}: {p.get('title','?')} | {p['status']} | price={p.get('price','?')}")

        elif cmd == "once":
            # python fast_watch.py once  —  单轮检查+状态对比+飞书告警（给cron用）
            setup_logging()
            wl = load_watchlist()
            ua = wl["config"].get("user_agents", [])
            feishu_url = wl["config"].get("feishu_webhook_url", "")
            state = load_state()
            new_state = {}

            for item in wl["products"]:
                pid = str(item["id"])
                product = check_product(pid, ua)

                new_state[pid] = {
                    "status": product["status"],
                    "available": product.get("available", False),
                    "price": product.get("price"),
                    "title": product.get("title"),
                    "updated_at": product.get("updated_at"),
                }

                old = state.get(pid, {})
                old_status = old.get("status", "unknown")
                new_status = product["status"]

                if old_status == "unknown":
                    logging.info(f"[INIT] {pid} {product.get('title','?')}: {new_status}")
                elif old_status == "sold_out" and new_status == "available":
                    logging.info(f"[RESTOCK!] {pid} {product.get('title','?')}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "restock")
                elif old_status == "available" and new_status == "sold_out":
                    logging.info(f"[SOLD_OUT] {pid} {product.get('title','?')}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "sold_out")
                elif old_status == "not_found" and new_status != "not_found":
                    logging.info(f"[NEW!] {pid} just appeared: {product.get('title','?')}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "new")
                elif old.get("price") and product.get("price") and old["price"] != product["price"]:
                    logging.info(f"[PRICE] {pid} {old['price']} -> {product['price']}")
                    if feishu_url:
                        send_feishu_alert(feishu_url, product, "price_change")

                time.sleep(random.uniform(0.5, 1.5))

            save_state(new_state)
            logging.info(f"Watch once complete. {len(wl['products'])} products checked.")

        elif cmd == "run":
            # python fast_watch.py run  —  持续运行
            main_loop()

        elif cmd == "config":
            # python fast_watch.py config feishu_webhook https://...
            if len(sys.argv) >= 4:
                key = sys.argv[2]
                val = sys.argv[3]
                wl = load_watchlist()
                wl["config"][key] = val
                save_watchlist(wl)
                print(f"Set config.{key} = {val}")
            else:
                wl = load_watchlist()
                print("Current config:", json.dumps(wl["config"], indent=2))

        else:
            print(f"Unknown command: {cmd}")
            print("Commands: add <id> | remove <id> | list | check | run | config <key> <val>")
    else:
        print("Fast Watch — 指定商品高频轮询")
        print("Commands: add <id> | remove <id> | list | check | run | config <key> <val>")
