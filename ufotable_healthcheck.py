#!/usr/bin/env python3
"""ufotable WEBSHOP 凌晨自检 - 每天 2:00/4:00/6:00 北京时间运行"""

import json
import os
import sys
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

import requests

from common import JST, setup_logging

API_BASE = "https://client-api.modd.com/UFWE"


def load_config():
    path = "ufotable_config.json" if os.path.exists("ufotable_config.json") else "ufotable_config.example.json"
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    env_map = {
        "FEISHU_WEBHOOK_URL": "webhook_url",
        "FEISHU_APP_ID": "app_id",
        "FEISHU_APP_SECRET": "app_secret",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg.setdefault("notifications", {}).setdefault("feishu", {})[cfg_key] = os.environ[env_key]
    return cfg


def send_report(cfg, status, details):
    nc = cfg["notifications"]["feishu"]
    if not nc.get("webhook_url"):
        return
    icons = {"ok": "", "warn": "", "fail": ""}
    texts = {"ok": "正常", "warn": "已修复", "fail": "需要确认"}
    msg = f"{icons.get(status, '')} **ufotable WEBSHOP 自检报告**\n\n" \
          f"状态: **{texts.get(status, '')}**\n时间: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n\n{details}"
    try:
        r = requests.post(nc["webhook_url"], json={"msg_type": "text", "content": {"text": msg}}, timeout=15)
        if r.json().get("code") == 0:
            logging.info("Health report sent")
    except Exception as e:
        logging.error(f"Report send error: {e}")


def check_db(db_path):
    if not Path(db_path).exists():
        return False, "DB不存在"
    for f in [db_path + "-wal", db_path + "-shm"]:
        if Path(f).exists():
            Path(f).unlink()
    try:
        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] == "ok":
            count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM products").fetchone()[0]
            return True, f"DB完好 ({count}件商品)"
        return False, f"DB损坏: {result[0]}"
    except Exception as e:
        return False, f"DB错误: {e}"


def check_api():
    try:
        r = requests.get(f"{API_BASE}/product", timeout=20,
                         headers={"User-Agent": "HealthCheck/1.0", "Origin": "https://webshop.ufotable.co.jp"})
        if r.status_code == 200:
            count = len(r.json().get("products", []))
            return True, f"API可访问 ({count}件商品)"
        return False, f"API状态码: {r.status_code}"
    except Exception as e:
        return False, f"API错误: {e}"


def main():
    setup_logging("data/ufotable_healthcheck.log")
    cfg = load_config()
    db_path = cfg.get("database_path", "data/ufotable.db")

    logging.info("=== ufotable WEBSHOP 自检开始 ===")

    api_ok, api_msg = check_api()
    db_ok, db_msg = check_db(db_path)

    logging.info(f"API: {api_msg}")
    logging.info(f"DB: {db_msg}")

    if api_ok and db_ok:
        status, details = "ok", f"  全部正常\n  API: {api_msg}\n  DB: {db_msg}"
    elif not db_ok:
        status, details = "warn", f"  数据库问题: {db_msg}\n  自动重建中..."
        for f in [Path(db_path), Path(db_path + "-wal"), Path(db_path + "-shm")]:
            if f.exists():
                f.unlink()
        details += "\n  已清除损坏数据，下次监控将重建基线"
    else:
        status, details = "fail", f"  API不通: {api_msg}\n  需要检查ufotable网站状态"

    logging.info(f"结论: {status}")
    logging.info(details)
    send_report(cfg, status, details)


if __name__ == "__main__":
    main()
