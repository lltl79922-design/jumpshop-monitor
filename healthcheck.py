#!/usr/bin/env python3
"""
凌晨自检脚本 - 每天 2:00/4:00/6:00 (北京时间) 运行
自动检测并修复常见故障:
  - 数据库损坏 → 重建
  - 缓存失效 → 重建基线
  - API 不通 → 告警
  - 连续失败 → 紧急通知
"""

import json
import os
import sys
import sqlite3
import time
import random
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
HEALTHCHECK_LOG = "data/healthcheck.log"


def setup_logging():
    Path(HEALTHCHECK_LOG).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [HEALTH] %(message)s",
        handlers=[
            logging.FileHandler(HEALTHCHECK_LOG, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )


def load_config():
    with open("config.json" if os.path.exists("config.json") else "config.example.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ["FEISHU_WEBHOOK_URL", "FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
        if os.environ.get(key):
            cfg["notifications"]["feishu"][key.lower().replace("feishu_", "")] = os.environ[key]
    return cfg


def send_health_report(cfg, status, details):
    """发送自检报告到飞书"""
    nc = cfg["notifications"]["feishu"]
    if not nc.get("webhook_url"):
        return

    status_icon = {"ok": "", "warn": "", "fail": ""}.get(status, "")
    status_text = {"ok": "正常", "warn": "已修复", "fail": "需要人工处理"}.get(status, "")

    text = f"{status_icon} **Jump Shop 凌晨自检报告** {status_icon}\n\n" \
           f"状态: **{status_text}**\n" \
           f"时间: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n\n" \
           f"{details}"

    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        r = requests.post(nc["webhook_url"], json=payload, timeout=15)
        if r.json().get("code") == 0:
            logging.info("Health report sent to Feishu")
        else:
            logging.error(f"Health report failed: {r.json()}")
    except Exception as e:
        logging.error(f"Health report send error: {e}")


def check_database(db_path):
    """检查数据库完整性"""
    if not Path(db_path).exists():
        return False, "数据库文件不存在 (首次运行?)"

    # 清理残留 WAL
    wal = db_path + "-wal"
    shm = db_path + "-shm"
    for f in [wal, shm]:
        if Path(f).exists():
            Path(f).unlink()
            logging.info(f"Cleaned stale file: {f}")

    try:
        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] == "ok":
            count = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM products").fetchone()[0]
            return True, f"数据库完好 ({count} 件商品)"
        else:
            return False, f"数据库完整性检查失败: {result[0]}"
    except Exception as e:
        return False, f"数据库错误: {e}"


def repair_database(db_path):
    """修复/重建数据库"""
    logging.info("Attempting database repair...")
    # 备份损坏的数据库
    backup = db_path + ".broken"
    if Path(db_path).exists():
        Path(db_path).rename(backup)
        logging.info(f"Corrupted DB backed up to {backup}")

    # 清理 WAL
    for suffix in ["-wal", "-shm", "-broken"]:
        p = Path(db_path + suffix)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    # 重新初始化
    try:
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
        conn.close()
        logging.info("Database reinitialized successfully")
        return True
    except Exception as e:
        logging.error(f"Database repair failed: {e}")
        return False


def check_api_reachable(shop_url):
    """检查 Shopify API 是否可访问"""
    try:
        r = requests.get(f"{shop_url}/products.json?limit=1", timeout=20,
                         headers={"User-Agent": "HealthCheck/1.0", "Accept": "application/json"})
        if r.status_code == 200:
            data = r.json()
            count = len(data.get("products", []))
            return True, f"API 可访问 (返回 {count} 件商品)"
        else:
            return False, f"API 返回异常状态码: {r.status_code}"
    except requests.exceptions.Timeout:
        return False, "API 超时"
    except requests.exceptions.ConnectionError:
        return False, "API 连接失败 (DNS/网络问题)"
    except Exception as e:
        return False, f"API 错误: {e}"


def check_recent_runs():
    """检查最近的 workflow 运行记录（通过 GitHub API）"""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return True, "跳过 (无 GitHub Token)"

    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        runs_url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=5"
        r = requests.get(runs_url, headers=headers, timeout=15)
        runs = r.json().get("workflow_runs", [])

        recent_failures = 0
        for run in runs[:5]:
            if run["conclusion"] == "failure":
                recent_failures += 1

        if recent_failures >= 3:
            return False, f"最近 5 次运行中 {recent_failures} 次失败 (连续故障)"
        elif recent_failures > 0:
            return True, f"近期 {recent_failures} 次失败 (已修复)"
        else:
            return True, "近期运行正常"
    except Exception as e:
        return True, f"无法检查运行记录: {e}"


def run_full_check(cfg):
    """执行完整自检"""
    issues = []
    fixes = []

    # 1. 检查 API
    logging.info("=== 自检开始 ===")
    api_ok, api_msg = check_api_reachable(cfg["shop_url"])
    logging.info(f"API 检查: {api_msg}")
    if not api_ok:
        issues.append(f"[API] {api_msg}")

    # 2. 检查数据库
    db_path = cfg.get("database_path", "data/products.db")
    db_ok, db_msg = check_database(db_path)
    logging.info(f"数据库检查: {db_msg}")
    if not db_ok:
        issues.append(f"[数据库] {db_msg}")
        if repair_database(db_path):
            fixes.append("[数据库] 已重建")
        else:
            issues.append("[数据库] 修复失败")

    # 3. 检查近期运行
    run_ok, run_msg = check_recent_runs()
    logging.info(f"运行记录: {run_msg}")
    if not run_ok:
        issues.append(f"[运行] {run_msg}")

    # 汇总
    details = []
    if api_ok and db_ok and run_ok and not issues:
        status = "ok"
        details.append("  所有检查通过: API / 数据库 / 运行记录")
        details.append(f"  数据库: {db_msg}")
    elif issues and fixes:
        status = "warn"
        details.append("  发现问题:")
        for i in issues:
            details.append(f"  {i}")
        details.append("  自动修复:")
        for f in fixes:
            details.append(f"  {f}")
    else:
        status = "fail"
        details.append("  严重问题 (需要人工处理):")
        for i in issues:
            details.append(f"  {i}")

    details_str = "\n".join(details)
    logging.info(f"自检结论: {status}")
    logging.info(details_str)

    # 发送报告
    send_health_report(cfg, status, details_str)

    return status


def main():
    setup_logging()
    cfg = load_config()

    # 如果是完整重建模式
    if "--rebuild" in sys.argv:
        db_path = cfg.get("database_path", "data/products.db")
        repair_database(db_path)
        logging.info("Database rebuild complete. Running full scan...")
        # 运行 monitor 重建数据
        import monitor_loop
        conn = monitor_loop.init_db(db_path)
        monitor_loop.run_once(cfg, conn, is_first_run=True)
        conn.close()
        logging.info("Rebuild + full scan complete")
        return

    # 标准自检
    run_full_check(cfg)


if __name__ == "__main__":
    main()
