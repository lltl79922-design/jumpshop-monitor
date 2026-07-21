#!/usr/bin/env python3
"""Jump Shop + ufotable WEBSHOP 监控共享模块 — 飞书API、售罄快照、通知卡片、监控基类"""

import json
import os
import sys
import time
import sqlite3
import signal
import random
import logging
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))

CHANGE_LABELS = {
    "new": "[NEW]",
    "restock": "[RESTOCK]",
    "sold_out": "[SOLD OUT]",
    "price_change": "[PRICE]",
}


# =============================================================================
# 时间解析 — 支持多种时间戳格式
# =============================================================================
def parse_timestamp(ts):
    """解析各种时间戳格式为 timezone-aware datetime。
    支持: ISO 8601 ('2024-01-15T14:32:15+09:00'),
          JST 标签 ('2026-06-11 14:35:00 JST'),
          纯 datetime ('2024-01-15 14:32:15'),
          纯日期 ('2024-01-15')
    返回 None 如果解析失败。
    """
    if not ts or not isinstance(ts, str) or not ts.strip():
        return None
    ts = ts.strip()
    try:
        # ISO 8601 检测: 日期与时间之间有 'T' 分隔符 (e.g. "2024-01-15T14:32:15+09:00")
        # 不能只用 'T' in ts，因为 "JST" 中也有 T
        is_iso = ('T' in ts and ts.index('T') >= 8)  # T 出现在日期部分之后
        if is_iso:
            try:
                return datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass  # 可能不是标准 ISO，继续尝试其他格式
        # JST 标签格式: "2026-06-11 14:35:00 JST"
        if 'JST' in ts:
            return datetime.strptime(ts.replace(' JST', ''), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        # 标准 datetime: "2024-01-15 14:32:15"
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    try:
        # 纯日期: "2024-01-15"
        return datetime.strptime(ts, "%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    return None


def format_duration(seconds):
    """格式化秒数为人类可读的时长字符串"""
    if seconds is None:
        return "时间未知"
    seconds = int(seconds)
    if seconds < 60:
        return f"约{seconds}秒内"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        if secs == 0:
            return f"约{minutes}分内"
        return f"约{minutes}分{secs}秒内"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes == 0:
            return f"约{hours}小时内"
        return f"约{hours}小时{minutes}分内"

# =============================================================================
# 日志
# =============================================================================
def setup_logging(log_file, fmt=None):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    if fmt is None:
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

# =============================================================================
# 飞书 API
# =============================================================================
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
        # 根据扩展名推断 content-type
        ext = image_url.rsplit(".", 1)[-1].split("?")[0].lower() if "." in image_url else "jpg"
        mime = f"image/{ext}" if ext in ("jpg", "jpeg", "png", "gif", "webp") else "image/jpeg"
        r = requests.post("https://open.feishu.cn/open-apis/im/v1/images",
                          headers={"Authorization": f"Bearer {token}"},
                          files={"image": (f"product.{ext}", img_data, mime)},
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
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    if not app_id or not app_secret:
        return

    for c in changes:
        p = c["product"]
        if not p.get("image_url"):
            continue

        row = conn.execute("SELECT feishu_img_key FROM products WHERE id=?", (p["id"],)).fetchone()
        if row and row[0]:
            p["feishu_img_key"] = row[0]
            continue

        logging.info(f"Uploading image for: {p['title'][:40]}...")
        img_key = upload_image_to_feishu(p["image_url"], app_id, app_secret)
        if img_key:
            p["feishu_img_key"] = img_key
            conn.execute("UPDATE products SET feishu_img_key=? WHERE id=?", (img_key, p["id"]))
            conn.commit()
        time.sleep(0.2)

# =============================================================================
# 售罄快照对比
# =============================================================================
def detect_soldout_delta(conn, products, detect_sold_out_enabled, now_str):
    if not detect_sold_out_enabled:
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


# =============================================================================
# 闪电售罄检测 — 精确到秒的售罄速度分析
# =============================================================================
def detect_lightning_sellouts(conn, changes, now_str, threshold_seconds=300, publish_field="published_at"):
    """分析售罄商品的实际售罄速度，标记闪电售罄。

    时间源优先级（取最早可用的"有货时间"）:
      1. last_available_at — 上次检测时还有货 (最准确)
      2. {publish_field} — 商品上架/发布时间 (秒级精度)
      3. first_seen — 首次被监控发现的时间 (兜底)

    将闪电信息写入 change dict: c["lightning"] = {sellout_seconds, source, display}
    """
    if threshold_seconds <= 0:
        return

    now_dt = parse_timestamp(now_str)
    if not now_dt:
        logging.warning("Cannot parse now_str for lightning detection: %s", now_str)
        return

    lightning_count = 0

    for c in changes:
        if c["change_type"] != "sold_out":
            continue

        pid = c["product_id"]
        row = conn.execute(
            f"SELECT last_available_at, {publish_field}, first_seen FROM products WHERE id=?",
            (pid,)
        ).fetchone()

        if not row:
            continue

        last_avail, pub_ts, first_seen = row

        # 按优先级尝试各时间源
        sellout_seconds = None
        source = None

        for src_label, ts_val in [
            ("last_available", last_avail),
            ("published", pub_ts),
            ("first_seen", first_seen),
        ]:
            ref_dt = parse_timestamp(ts_val) if ts_val else None
            if ref_dt:
                delta = (now_dt - ref_dt).total_seconds()
                if delta >= 0:
                    sellout_seconds = delta
                    source = src_label
                    break

        if sellout_seconds is not None and sellout_seconds <= threshold_seconds:
            c["lightning"] = {
                "sellout_seconds": int(sellout_seconds),
                "source": source,
                "display": format_duration(int(sellout_seconds)),
            }
            lightning_count += 1
            logging.info(
                "  ⚡ Lightning sellout: %s | %s | source=%s",
                c["product"].get("title", str(pid))[:50],
                format_duration(int(sellout_seconds)),
                source
            )

    if lightning_count:
        logging.info("Lightning sellout summary: %d/%d sold_out items", lightning_count,
                     sum(1 for c in changes if c["change_type"] == "sold_out"))


# =============================================================================
# Bot 扫货预警
# =============================================================================
def build_bot_alert_card(lightning_items, now_str, shop_config):
    """构建 Bot 扫货预警的独立飞书卡片 (红色警告)"""
    header_title = f"  {shop_config['name']} Bot 掃貨警報"

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"  本次检测发现 **{len(lightning_items)} 件**商品在极短时间内售罄，\n  疑似自动化扫货程序活动。"
            }
        },
        {"tag": "hr"},
    ]

    for c in lightning_items[:20]:
        p = c["product"]
        linfo = c.get("lightning", {})
        duration = linfo.get("display", "极短时间")
        price_yen = f"  {p['price']:,}"
        subtitle_field = shop_config.get("subtitle_field", "vendor")
        subtitle = p.get(subtitle_field, "")

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{p['title']}**\n{price_yen} | {subtitle} |   售罄耗时: **{duration}**\n[商品ページ]({p['url']})"
            }
        })

    if len(lightning_items) > 20:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"  他 {len(lightning_items) - 20} 件省略"}
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"  {now_str}  |  {shop_config['footer']} 自動警報"}]
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": "red"
            },
            "elements": elements
        }
    }


def maybe_send_bot_alert(webhook_url, changes, now_str, shop_config, min_count=3):
    """如果闪电售罄数量达到阈值，发送独立的 Bot 扫货预警卡片"""
    if not webhook_url or min_count <= 0:
        return

    lightning_items = [c for c in changes if c.get("lightning")]
    if len(lightning_items) < min_count:
        return

    logging.info("Bot alert triggered: %d lightning sellouts >= threshold %d",
                 len(lightning_items), min_count)
    card = build_bot_alert_card(lightning_items, now_str, shop_config)
    send_feishu_card(webhook_url, card)
def log_changes(conn, changes, now_str):
    for c in changes:
        conn.execute(
            "INSERT INTO change_log (product_id, change_type, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
            (c["product_id"], c["change_type"], c["old_value"], c["new_value"], now_str))
    conn.commit()

# =============================================================================
# 飞书交互式卡片
# =============================================================================
def build_feishu_cards(changes, now_str, shop_config, max_per_card=50, max_cards=10):
    """
    返回 list of card payloads — 当变更超过 max_per_card 件时自动分页。
    每张卡片独立发送，header 标注页码 (例: "1/3")。
    超过 max_cards 张卡片时截断，末尾标注省略数量。

    shop_config 字段:
      - name: 商店显示名称
      - template_color: 卡片头部颜色 (red/blue)
      - footer: 页脚文字
      - subtitle_field: 商品副标题字段 ("vendor" 或 "works")
    """
    # ---- 去重: 防止同一商品同一变更类型重复出现 (product_id + change_type) ----
    seen = set()
    deduped = []
    for c in changes:
        key = (c["product_id"], c["change_type"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    if len(deduped) < len(changes):
        logging.warning(
            "build_feishu_cards: dedup removed %d duplicate change entries",
            len(changes) - len(deduped))
    changes = deduped
    total = len(changes)
    if total == 0:
        return []

    new_count = sum(1 for c in changes if c["change_type"] == "new")
    restock_count = sum(1 for c in changes if c["change_type"] == "restock")
    lightning_count = sum(1 for c in changes if c["change_type"] == "sold_out" and c.get("lightning"))
    normal_soldout_count = sum(1 for c in changes if c["change_type"] == "sold_out" and not c.get("lightning"))
    soldout_count = lightning_count + normal_soldout_count
    price_count = sum(1 for c in changes if c["change_type"] == "price_change")

    parts = []
    if new_count: parts.append(f"上新 {new_count}")
    if restock_count: parts.append(f"補貨 {restock_count}")
    if lightning_count: parts.append(f"⚡閃電售罄 {lightning_count}")
    if normal_soldout_count: parts.append(f"售罄 {normal_soldout_count}")
    elif soldout_count and not lightning_count: parts.append(f"售罄 {soldout_count}")
    if price_count: parts.append(f"価格変更 {price_count}")
    summary = "  |  ".join(parts) if parts else "  状態変更なし"

    base_header_title = f"  {shop_config['name']} 商品監視"

    # 渲染顺序: 上新 → 補貨 → 闪电售罄 → 普通售罄 → 価格変更
    section_order = [
        ("new", "  新商品上架", False),
        ("restock", "  補貨", False),
        ("sold_out", "   閃電售罄", True),
        ("sold_out", "  售罄", False),
    ]

    # 按优先级展开所有 items 为有序列表
    subtitle_field = shop_config.get("subtitle_field", "vendor")
    ordered_items = []

    for ctype, header, want_lightning in section_order:
        if ctype == "sold_out":
            if want_lightning:
                items = [c for c in changes if c["change_type"] == "sold_out" and c.get("lightning")]
            else:
                items = [c for c in changes if c["change_type"] == "sold_out" and not c.get("lightning")]
        else:
            items = [c for c in changes if c["change_type"] == ctype]

        if items:
            ordered_items.append((ctype, header, items))

    price_items = [c for c in changes if c["change_type"] == "price_change"]
    if price_items:
        ordered_items.append(("price_change", "  価格変更", price_items))

    # 渲染单个商品条目为 elements 片段 (无 header/footer)
    def render_product_item(elements, c, extra_info=""):
        p = c["product"]
        status_icon = "  " if p["available"] else "  "
        status_text = "在庫あり" if p["available"] else "在庫なし"
        price_yen = f"  {p['price']:,}"
        subtitle = p.get(subtitle_field, "")
        img_key = p.get("feishu_img_key", "")

        product_md = f"**{p['title']}**\n{price_yen} | {subtitle} | {status_icon} {status_text}"
        if extra_info:
            product_md += f"\n{extra_info}"
        ctype = c.get("change_type", "")
        if ctype == "price_change":
            product_md += f"\n  {c['old_value']}    {c['new_value']}"

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

        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "  商品ページ"},
                "type": "default",
                "url": p["url"]
            }]
        })

    # 将 ordered_items 按 max_per_card 分页
    # 策略: 逐个 item 放入当前卡片，超过 max_per_card 时开新卡片
    # 每个 section header 也算一个"逻辑条目"，但如果一个卡片装不下当前 section 的全部 item，
    # 会在下一个卡片重复 section header

    cards = []
    current_page_items = []  # [(ctype, header, [items_for_this_section_on_this_page])]
    current_count = 0

    for ctype, header, items in ordered_items:
        # 如果当前卡片里已经有这个 section 的内容，且没有装满，继续追加
        remaining = items[:]

        if current_count >= max_per_card:
            # 当前卡片已满，开新卡片
            cards.append(current_page_items)
            current_page_items = []
            current_count = 0

        # 计算当前卡片还能装多少
        space = max_per_card - current_count
        if len(remaining) <= space:
            # 全部装下
            current_page_items.append((ctype, header, remaining))
            current_count += len(remaining)
        else:
            # 需要拆分 section
            first_chunk = remaining[:space]
            current_page_items.append((ctype, header, first_chunk))
            current_count += len(first_chunk)
            remaining = remaining[space:]

            # 当前卡片满了，开新卡片继续放剩余的
            while remaining:
                cards.append(current_page_items)
                current_page_items = []
                current_count = 0
                chunk = remaining[:max_per_card]
                current_page_items.append((ctype, header, chunk))
                current_count += len(chunk)
                remaining = remaining[max_per_card:]

    # 最后一个卡片
    if current_page_items:
        cards.append(current_page_items)

    # 如果只有一页，不需要页码
    total_pages = len(cards)
    if total_pages == 0:
        return []

    # 截断: 超过 max_cards 张时只保留前 N 张
    truncated = False
    if total_pages > max_cards:
        cards = cards[:max_cards]
        truncated = True

    # 统计实际展示的 item 数
    shown_total = sum(1 for page in cards for (_, _, items) in page for _ in items)
    omitted = total - shown_total

    # 构建每张卡片
    result = []
    for page_idx, page_sections in enumerate(cards):
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"  {summary}"
                }
            },
            {"tag": "hr"}
        ]

        for ctype, header, items in page_sections:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{header} ({len(items)}件)**"}
            })
            for c in items:
                extra = ""
                if c.get("lightning"):
                    linfo = c["lightning"]
                    source_note = {
                        "last_available": "前回検出時",
                        "published": "上架後",
                        "first_seen": "初回発見時",
                    }.get(linfo.get("source", ""), "")
                    extra = f"    {source_note}{linfo.get('display', '')}に完売"
                render_product_item(elements, c, extra)

        # 截断提示 (放在 footer 前)
        if truncated and page_idx == len(cards) - 1 and omitted > 0:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"  ...他 {omitted} 件の変更は省略されました（表示上限 {max_cards * max_per_card} 件）"}
            })

        elements.append({"tag": "hr"})

        # 页脚: 页码信息
        shown_pages = max_cards if truncated else total_pages
        page_info = f"  {now_str}  |  {shop_config['footer']}"
        if shown_pages > 1:
            page_info = f"  Page {page_idx+1}/{shown_pages}  |  {now_str}  |  {shop_config['footer']}"

        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": page_info}]
        })

        # 标题 + 页码
        header_title = base_header_title
        if shown_pages > 1:
            header_title = f"  {shop_config['name']} 商品監視 ({page_idx+1}/{shown_pages})"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": shop_config["template_color"]
            },
            "elements": elements
        }
        result.append({"msg_type": "interactive", "card": card})

    return result


# 向后兼容：旧代码调用 build_feishu_card 返回第一张卡片（如果只有一张的话等同于原来）
def build_feishu_card(changes, now_str, shop_config):
    cards = build_feishu_cards(changes, now_str, shop_config)
    return cards[0] if cards else None


def send_feishu_card(webhook_url, card_payload, fallback_text=""):
    """发送飞书卡片(支持单张或多张)，失败时降级为纯文本"""
    # 支持多卡片批量发送
    if card_payload is None:
        return
    if isinstance(card_payload, list):
        for i, payload in enumerate(card_payload):
            if i > 0:
                time.sleep(0.6)  # 卡片间短暂间隔，避免飞书限流
            _send_single_feishu_card(webhook_url, payload, fallback_text if i == 0 else "")
        return
    _send_single_feishu_card(webhook_url, card_payload, fallback_text)


def _send_single_feishu_card(webhook_url, card_payload, fallback_text=""):
    """发送单张飞书卡片"""
    try:
        resp = requests.post(webhook_url, json=card_payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            logging.info("Feishu card notification sent")
        else:
            logging.error(f"Feishu card error: {result}")
            if fallback_text:
                resp2 = requests.post(webhook_url,
                                      json={"msg_type": "text", "content": {"text": fallback_text}},
                                      timeout=15)
                if resp2.json().get("code") == 0:
                    logging.info("Feishu fallback text sent")
    except Exception as e:
        logging.error(f"Feishu send failed: {e}")


# =============================================================================
# 状态快照 — 灾难恢复，防止自愈/缓存故障吞掉售罄和补货事件
# =============================================================================
def save_state_snapshot(snapshot_data, filepath):
    """保存状态快照到 JSON 文件，用于灾难恢复"""
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(snapshot_data, f, ensure_ascii=False)
    logging.info(f"State snapshot saved: {len(snapshot_data)} products")


def build_snapshot_from_db(conn):
    """从 Jump Shop DB 构建状态快照 dict"""
    rows = conn.execute(
        "SELECT id, title, available, price, image_url, url, vendor, handle FROM products"
    ).fetchall()
    snapshot = {}
    for row in rows:
        pid, title, available, price, image_url, url, vendor, handle = row
        snapshot[str(pid)] = {
            "title": title,
            "available": available,
            "price": price,
            "image_url": image_url,
            "url": url,
            "vendor": vendor,
            "handle": handle,
        }
    return snapshot


def load_state_snapshot(filepath):
    """加载状态快照"""
    if not Path(filepath).exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def detect_changes_from_snapshot(old_snapshot, products, cfg):
    """对比旧快照与当前API数据，检测遗漏的售罄/补货/上新/价格变更"""
    monitor_opts = cfg.get("monitor_options", {})
    changes = []
    product_map = {str(p["id"]): p for p in products}
    old_ids = set(old_snapshot.keys())
    current_ids = {str(p["id"]) for p in products}

    for pid_str in old_ids & current_ids:
        old = old_snapshot[pid_str]
        p = product_map[pid_str]

        if monitor_opts.get("detect_restocks", True) and old["available"] == 0 and p["available"] == 1:
            changes.append({
                "product_id": int(pid_str), "change_type": "restock",
                "old_value": "out of stock", "new_value": "in stock (recovered)",
                "product": p,
            })
        if monitor_opts.get("detect_sold_out", True) and old["available"] == 1 and p["available"] == 0:
            changes.append({
                "product_id": int(pid_str), "change_type": "sold_out",
                "old_value": "in stock", "new_value": "sold out (recovered)",
                "product": p,
            })
        if monitor_opts.get("detect_price_changes", True) and old["price"] != p["price"] and old["price"] != 0:
            changes.append({
                "product_id": int(pid_str), "change_type": "price_change",
                "old_value": f"Y{old['price']}", "new_value": f"Y{p['price']}",
                "product": p,
            })

    if monitor_opts.get("detect_new_products", True):
        for pid_str in current_ids - old_ids:
            p = product_map[pid_str]
            changes.append({
                "product_id": int(pid_str), "change_type": "new",
                "old_value": None,
                "new_value": f"{p['title']} | Y{p['price']} | {'in stock' if p['available'] else 'out of stock'}",
                "product": p,
            })

    return changes


# =============================================================================
# JSON 状态持久化 (v2) — 替代 SQLite 做变更检测，杜绝缓存损坏导致的误报
# =============================================================================

def load_json_state(filepath):
    """加载 JSON 状态文件。不存在或损坏时返回空状态。"""
    if not Path(filepath).exists():
        return {"version": 2, "products": {}, "soldout_ids": [], "updated_at": ""}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logging.warning("JSON state file corrupted, starting fresh: %s", e)
        return {"version": 2, "products": {}, "soldout_ids": [], "updated_at": ""}

    # 兼容旧格式 (v1: 纯 product dict, 无 soldout_ids)
    if "version" not in state:
        state = {"version": 2, "products": state, "soldout_ids": [],
                 "updated_at": state.get("updated_at", "") if isinstance(state, dict) else ""}
    if "soldout_ids" not in state:
        state["soldout_ids"] = []
    if "products" not in state:
        state["products"] = {}
    return state


def save_json_state(state, filepath):
    """保存 JSON 状态文件（原子写入：先写临时文件再 rename）"""
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    state["version"] = 2
    tmp = filepath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, filepath)
    logging.info("JSON state saved: %d products, %d soldout_ids",
                 len(state.get("products", {})), len(state.get("soldout_ids", [])))


# =============================================================================
# 状态完整性校验 (v2.1) — 多层防御杜绝断路器误触发
# =============================================================================
def validate_state_integrity(state, expected_min_products=50):
    """校验 JSON 状态文件的完整性。

    防御层:
      1. total_products 与 len(products) 一致性检查
      2. 历史大状态突然变空 → 强制 first_run
      3. soldout_ids 合理性检查

    返回: (is_valid: bool, force_first_run: bool, reason: str)
    """
    products = state.get("products", {})
    actual_total = len(products)
    reported_total = state.get("total_products", 0)
    soldout_ids = state.get("soldout_ids", [])

    # 1. total_products 一致性: 偏差 >5% 标记异常 (v2.4: 从15%收紧至5%)
    if reported_total > 0 and actual_total > 0:
        drift_pct = abs(actual_total - reported_total) / max(reported_total, 1)
        if drift_pct > 0.05:
            return False, True, (
                f"total_products mismatch: reported={reported_total}, actual={actual_total}"
            )

    # 2. 历史大状态突然变空 → 强制 first_run (防止部分损坏后漏过断路器)
    if reported_total > expected_min_products and actual_total == 0:
        return False, True, (
            f"State emptied: was {reported_total} products, now 0"
        )

    # 3. soldout_ids 孤儿检测: >50% soldout_ids 不在 products 中 → 异常
    if soldout_ids and actual_total > 0:
        valid_ids = set(products.keys())
        orphan_soldout = [sid for sid in soldout_ids if str(sid) not in valid_ids]
        if len(orphan_soldout) > len(soldout_ids) * 0.5:
            return False, False, (
                f"Too many orphan soldout_ids: {len(orphan_soldout)}/{len(soldout_ids)}"
            )

    # 4. updated_at 存在性检查: products 有数据但无时间戳 → 异常恢复产物
    if actual_total > expected_min_products and not state.get("updated_at", "").strip():
        return False, True, (
            f"Missing updated_at with {actual_total} products — likely recovery artifact"
        )

    return True, False, "OK"


def _product_entry(p):
    """从 normalize 后的 product dict 提取状态条目"""
    return {
        "title": p.get("title", ""),
        "available": p.get("available", 0),
        "price": p.get("price", 0),
        "image_url": p.get("image_url", ""),
        "url": p.get("url", ""),
        "vendor": p.get("vendor", p.get("works", "")),
        "handle": p.get("handle", p.get("product_code", "")),
        "published_at": p.get("published_at", p.get("valid_after", "")),
        "first_seen": "",
        "last_available_at": "",
    }


def detect_all_changes_from_state(old_state, products, now_str, cfg):
    """从 JSON 状态检测所有变更 (new/restock/sold_out/price_change + soldout_delta)。
    一次性替代 detect_changes() + detect_soldout_delta()。

    返回: (changes, current_soldout_ids)
    """
    monitor_opts = cfg.get("monitor_options", {})
    old_products = old_state.get("products", {})
    old_soldout = set(old_state.get("soldout_ids", []))

    changes = []
    existing_ids = set()   # 用于 soldout_delta 去重
    current_soldout = set()
    product_map = {}

    # ---- 产品数突变检测 (防御层1: 在变更检测前拦截状态损坏) ----
    old_total = len(old_products)
    new_total = len(products)
    # 同时检查 metadata 中的 total_products 与实际 products dict 长度是否一致
    reported_total = old_state.get("total_products", 0)
    meta_drift = False
    if reported_total > 50 and old_total > 0:
        meta_ratio = old_total / max(reported_total, 1)
        if meta_ratio < 0.5:
            logging.warning(
                "STATE METADATA DRIFT: total_products=%d but actual products=%d (ratio=%.2f). "
                "State file partially corrupted — suppressing change detection.",
                reported_total, old_total, meta_ratio
            )
            meta_drift = True

    if old_total > 50 and new_total > 0:
        ratio = old_total / max(new_total, 1)
        # 如果旧状态产品数相比当前 API 产品数突变 (ratio<0.5 或 >2.0)，
        # 可能是状态文件损坏导致部分产品丢失 → 静默返回空变更
        # v2.2: 阈值从 0.3 收紧到 0.5，更激进地拦截部分损坏
        if ratio < 0.5 or ratio > 2.0 or meta_drift:
            logging.warning(
                "STATE INTEGRITY: old=%d products, new=%d (old/new=%.2f). "
                "Possible state corruption — suppressing change detection.",
                old_total, new_total, ratio
            )
            return [], sorted(current_soldout)

    for p in products:
        pid = str(p["id"])
        product_map[pid] = p
        if p["available"] == 0:
            current_soldout.add(pid)

        old = old_products.get(pid)

        if old is None:
            if monitor_opts.get("detect_new_products", True):
                changes.append({
                    "product_id": int(pid), "change_type": "new",
                    "old_value": None,
                    "new_value": f"{p['title']} | Y{p['price']} | {'in stock' if p['available'] else 'out of stock'}",
                    "product": p,
                })
                existing_ids.add(pid)
        else:
            if monitor_opts.get("detect_restocks", True) and old["available"] == 0 and p["available"] == 1:
                changes.append({
                    "product_id": int(pid), "change_type": "restock",
                    "old_value": "out of stock", "new_value": "in stock",
                    "product": p,
                })
                existing_ids.add(pid)
            if monitor_opts.get("detect_sold_out", True) and old["available"] == 1 and p["available"] == 0:
                changes.append({
                    "product_id": int(pid), "change_type": "sold_out",
                    "old_value": "in stock", "new_value": "out of stock",
                    "product": p,
                })
                existing_ids.add(pid)
            if monitor_opts.get("detect_price_changes", True) and old["price"] != p["price"] and old["price"] != 0:
                changes.append({
                    "product_id": int(pid), "change_type": "price_change",
                    "old_value": f"Y{old['price']}", "new_value": f"Y{p['price']}",
                    "product": p,
                })
                existing_ids.add(pid)

    # Soldout delta 快照对比 (补充 per-product 检测的遗漏)
    if monitor_opts.get("detect_sold_out", True):
        newly_soldout = current_soldout - old_soldout
        newly_restocked = old_soldout - current_soldout

        for pid_str in newly_soldout:
            if pid_str not in existing_ids:
                # 交叉校验: 旧 per-product 状态如果已经是售罄, 跳过(已通知过)
                old = old_products.get(pid_str)
                if old and old.get("available") == 0:
                    continue
                p = product_map.get(pid_str)
                if p:
                    changes.append({
                        "product_id": int(pid_str), "change_type": "sold_out",
                        "old_value": "in stock", "new_value": "sold out (snapshot)",
                        "product": p,
                    })

        for pid_str in newly_restocked:
            if pid_str not in existing_ids:
                # 交叉校验: 旧 per-product 状态如果已经是补货, 跳过
                old = old_products.get(pid_str)
                if old and old.get("available") == 1:
                    continue
                p = product_map.get(pid_str)
                if p:
                    changes.append({
                        "product_id": int(pid_str), "change_type": "restock",
                        "old_value": "sold out", "new_value": "in stock (snapshot)",
                        "product": p,
                    })

    return changes, sorted(current_soldout)


def detect_lightning_from_state(old_state, changes, now_str, threshold_seconds=300):
    """从 JSON 状态检测闪电售罄（不依赖 DB）"""
    if threshold_seconds <= 0:
        return

    now_dt = parse_timestamp(now_str)
    if not now_dt:
        return

    old_products = old_state.get("products", {})
    lightning_count = 0

    for c in changes:
        if c["change_type"] != "sold_out":
            continue

        pid = str(c["product_id"])
        old = old_products.get(pid, {})

        sellout_seconds = None
        source = None

        for src_label, field in [
            ("last_available", old.get("last_available_at", "")),
            ("published", old.get("published_at", "")),
            ("first_seen", old.get("first_seen", "")),
        ]:
            if not field:
                continue
            ref_dt = parse_timestamp(field)
            if ref_dt:
                delta = (now_dt - ref_dt).total_seconds()
                if 0 <= delta:
                    sellout_seconds = delta
                    source = src_label
                    break

        if sellout_seconds is not None and sellout_seconds <= threshold_seconds:
            c["lightning"] = {
                "sellout_seconds": int(sellout_seconds),
                "source": source,
                "display": format_duration(int(sellout_seconds)),
            }
            lightning_count += 1
            logging.info(
                "  ⚡ Lightning sellout: %s | %s | source=%s",
                c["product"].get("title", str(pid))[:50],
                format_duration(int(sellout_seconds)),
                source
            )

    if lightning_count:
        logging.info("Lightning sellout summary: %d/%d sold_out items", lightning_count,
                     sum(1 for c in changes if c["change_type"] == "sold_out"))


def build_new_state(products, old_state, now_str):
    """从当前 API 数据 + 旧状态构建新 JSON 状态 products 字典"""
    old_products = old_state.get("products", {})
    new_products = {}

    for p in products:
        pid = str(p["id"])
        old = old_products.get(pid, {})
        entry = _product_entry(p)

        # 保留 first_seen
        entry["first_seen"] = old.get("first_seen", "") or now_str

        # 更新 last_available_at: 有货→记录当前时间, 无货→保留旧值
        if p["available"] == 1:
            entry["last_available_at"] = now_str
        else:
            entry["last_available_at"] = old.get("last_available_at", "")

        new_products[pid] = entry

    return new_products


# =============================================================================
# ShopConfig + MonitorRunner — 统一监控基类 (v2.1)
# 消除 monitor_loop.py / ufotable_monitor.py 之间 ~300 行重复代码
# =============================================================================

@dataclass
class ShopConfig:
    """商店配置 — 替代分散的字典常量"""
    name: str
    template_color: str          # "red" | "blue"
    footer: str
    subtitle_field: str          # "vendor" | "works"
    shop_url: str
    api_base: str = ""
    state_file_default: str = ""
    db_path_default: str = ""
    config_path: str = ""
    config_example_path: str = ""
    min_product_count: int = 50


class MonitorRunner:
    """监控运行器基类 — 统一 JSON State 模式的完整监控流程。

    子类只需实现:
      - fetch_products(cfg) → list[dict]  (已标准化的商品列表)
      - update_db(conn, products, now_str) (可选, DB 写入)

    用法:
      runner = JumpShopRunner(ShopConfig(...))
      cfg = runner.load_config()
      conn = runner.init_db(cfg["database_path"])
      runner.run_once(cfg, "data/state.json", db_conn=conn)
    """

    def __init__(self, shop: ShopConfig):
        self.shop = shop
        self.running = True

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    def fetch_products(self, cfg: dict) -> list:
        """拉取并返回已标准化的商品列表 (子类实现)"""
        raise NotImplementedError("Subclass must implement fetch_products()")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def load_config(self, path=None) -> dict:
        """加载配置, 自动从环境变量注入飞书凭据"""
        if path is None:
            path = self.shop.config_path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        elif os.path.exists(self.shop.config_example_path):
            cfg = json.load(open(self.shop.config_example_path, "r", encoding="utf-8"))
        else:
            cfg = {}

        # 注入环境变量 (GitHub Secrets)
        env_map = {
            "FEISHU_WEBHOOK_URL": "webhook_url",
            "FEISHU_APP_ID": "app_id",
            "FEISHU_APP_SECRET": "app_secret",
        }
        for env_key, cfg_key in env_map.items():
            if os.environ.get(env_key):
                nc = cfg.setdefault("notifications", {}).setdefault("feishu", {})
                nc[cfg_key] = os.environ[env_key]

        # 注入 DeepSeek API Key
        if os.environ.get("DEEPSEEK_API_KEY"):
            ds = cfg.setdefault("deepseek", {})
            ds["api_key"] = os.environ["DEEPSEEK_API_KEY"]
            ds.setdefault("enabled", True)
            ds.setdefault("summary_enabled", True)

        return cfg

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------

    def init_db(self, db_path: str) -> sqlite3.Connection:
        """初始化 SQLite DB (change_log 表), 子类可覆盖以扩展表结构"""
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
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

    # ------------------------------------------------------------------
    # 核心: 一次性监控检查
    # ------------------------------------------------------------------

    def run_once(self, cfg: dict, state_file: str,
                 db_conn=None, silent: bool = False) -> int:
        """执行一次完整的监控检查 (JSON State 模式)。

        流程: 拉取 → 校验 → 变更检测 → 闪电售罄 → 通知 → 保存状态
        返回: 检测到的变更数量
        """
        start = time.time()
        logging.info("Checking %s...", self.shop.name)

        # 1. 拉取商品
        products = self.fetch_products(cfg)
        if not products:
            logging.error("Failed to fetch products")
            return 0

        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

        # 2. 产品数异常检测 (API 返回部分数据)
        min_expected = cfg.get("monitor_options", {}).get(
            "min_product_count", self.shop.min_product_count)
        if len(products) < min_expected:
            logging.error(
                "PRODUCT COUNT ANOMALY: got %d products, expected >= %d. "
                "API may be returning partial data — skipping this cycle.",
                len(products), min_expected
            )
            return 0

        # 3. 加载旧状态 + 完整性校验
        old_state = load_json_state(state_file)
        valid, force_first, reason = validate_state_integrity(old_state)
        first_run = len(old_state.get("products", {})) == 0
        if force_first:
            logging.warning("State integrity — forcing first_run: %s", reason)
            first_run = True

        if first_run:
            logging.info("First run with JSON state - building baseline...")

        # 4. 产品数骤降防御 (防止部分API响应导致状态损坏 → 断路器误报)
        old_total = old_state.get("total_products", 0)
        if old_total > 200 and not first_run:
            drop_ratio = len(products) / old_total
            if drop_ratio < 0.85:
                # 状态过期时放宽保护：过期数据比潜在误报更危险
                stale_minutes = 9999
                old_ts = old_state.get("updated_at", "")
                if old_ts:
                    try:
                        ts_clean = old_ts.replace(" JST", "")
                        old_dt = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")
                        old_dt = old_dt.replace(tzinfo=timezone(timedelta(hours=9)))
                        stale_minutes = (datetime.now(timezone.utc) - old_dt).total_seconds() / 60
                    except Exception:
                        pass
                if stale_minutes > 360:  # 超过6h未更新，接受新数据
                    logging.warning(
                        "PRODUCT COUNT DROP overridden: %d → %d (%.0f%%) — "
                        "state is %.0fh stale, accepting new data to prevent drift.",
                        old_total, len(products), drop_ratio * 100, stale_minutes / 60
                    )
                else:
                    logging.error(
                        "PRODUCT COUNT DROP: %d → %d (%.0f%%). "
                        "Likely partial API fetch — skipping state update to prevent false new-product detection.",
                        old_total, len(products), drop_ratio * 100
                    )
                    return 0

        # 5. 产品数漂移日志 (仅警告, 不拦截)
        if old_total > 0 and not first_run:
            ratio = len(products) / old_total
            if ratio < 0.5 or ratio > 1.5:
                logging.warning(
                    "PRODUCT COUNT DRIFT: %d → %d (%.0f%%). "
                    "Possible API issue or massive inventory change.",
                    old_total, len(products), ratio * 100
                )

        # 6. 变更检测 (new/restock/sold_out/price_change + soldout_delta)
        changes, soldout_ids = detect_all_changes_from_state(
            old_state, products, now_str, cfg)

        # 7. 闪电售罄检测
        lightning_threshold = cfg.get("monitor_options", {}).get(
            "lightning_sellout_threshold_seconds", 300)
        detect_lightning_from_state(old_state, changes, now_str, lightning_threshold)

        # 8. 通知分发
        # old_total 优先使用 metadata, 但如果 metadata 为0而实际有历史数据, 用实际值
        # 防止 state corruption 导致 drift shield 盲区
        effective_old_total = old_total if old_total > 0 else len(old_state.get("products", {}))
        try:
            self._dispatch_notifications(
                cfg, db_conn, changes, now_str, first_run, silent,
                old_total=effective_old_total, old_state=old_state)

            # 9. 记录变更到 DB
            if changes and db_conn:
                try:
                    log_changes(db_conn, changes, now_str)
                except Exception as e:
                    logging.warning("Failed to log changes to DB: %s", e)
        finally:
            # 10. 构建并保存新状态 — finally 确保即使通知发送崩溃也一定保存状态
            # 这是防止"重复播报"的关键：状态不保存 → 下次运行从旧状态重新检测 → 同样变更再次播报
            new_products = build_new_state(products, old_state, now_str)
            new_state = {
                "version": 2,
                "updated_at": now_str,
                "total_products": len(products),
                "soldout_ids": soldout_ids,
                "products": new_products,
            }
            save_json_state(new_state, state_file)

        # 11. 同步 DB (可选, 供 analysis.py 使用)
        if db_conn:
            try:
                self.update_db(db_conn, products, now_str)
            except Exception as e:
                logging.warning("Failed to update DB: %s", e)

        elapsed = time.time() - start
        logging.info("Done in %.1fs - %d products tracked (JSON state)",
                     elapsed, len(products))
        return len(changes)

    def update_db(self, conn, products, now_str):
        """更新 DB 商品表 (子类覆盖以匹配各自表结构)"""
        pass

    # ------------------------------------------------------------------
    # 通知分发 (统一版: 断路 + 分页 + Bot 预警 + 多层防御)
    # ------------------------------------------------------------------

    def _dispatch_notifications(self, cfg, conn, changes, now_str,
                                 is_first_run, silent, old_total=0,
                                 old_state=None):
        """统一的通知分发逻辑。

        防御层 (按优先级):
          1. 首次运行静默 (不轰炸基线)
          2. Silent 模式
          3. **状态过期静默** — updated_at > 90min → 只保留上新，售罄/补货静默
          4. **BURST SHIELD** — 上新 > max(50, old_total×8%) → 完全静默 (v2.4)
          5. 正常通知 → 飞书卡片 + Bot 预警
        """
        if not changes:
            return

        # 简短的变更摘要
        logging.info("Detected %d changes", len(changes))
        for c in changes[:10]:
            p = c["product"]
            label = CHANGE_LABELS[c['change_type']]
            if c.get("lightning"):
                label += " ⚡"
            logging.info("  %s %s | Y%d", label, p.get('title', '')[:60],
                        p.get('price', 0))
        if len(changes) > 10:
            logging.info("  ... and %d more", len(changes) - 10)

        # --- 防御层 1: 首次运行静默 ---
        if is_first_run and not cfg.get("monitor_options", {}).get(
                "notify_on_first_run"):
            logging.info("First run - skipping all notifications (baseline build)")
            return

        # --- 防御层 2: Silent ---
        if silent:
            logging.info("Silent mode - skipping notifications")
            return

        new_count = sum(1 for c in changes if c["change_type"] == "new")

        # --- 防御层 2.5: 极端过期完全静默 (HARD STALE CAP) ---
        # 状态超过 24h 未更新 → 极可能为长期故障恢复/状态损坏
        # 此时大量"变更"实为历史累积 → 完全静默更新，不发任何通知
        # 防止用户收到 100+ 条重复飞书卡片轰炸（本次修复的核心原因）
        if old_state is not None:
            hard_stale_hours = cfg.get("monitor_options", {}).get(
                "hard_stale_silence_hours", 24)
            old_updated = old_state.get("updated_at", "")
            if old_updated and "JST" in old_updated:
                try:
                    old_dt = datetime.strptime(
                        old_updated.replace(" JST", ""), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=JST)
                    now_dt = datetime.now(JST)
                    stale_h = int((now_dt - old_dt).total_seconds() / 3600)
                    if stale_h > hard_stale_hours:
                        logging.error(
                            "HARD STALE CAP: state is %dh old (>%dh threshold). "
                            "Suppressing ALL notifications — state will sync silently. "
                            "Changes: %d total (new=%d, restock=%d, sold_out=%d, price=%d).",
                            stale_h, hard_stale_hours, len(changes), new_count,
                            sum(1 for c in changes if c["change_type"] == "restock"),
                            sum(1 for c in changes if c["change_type"] == "sold_out"),
                            sum(1 for c in changes if c["change_type"] == "price_change")
                        )
                        return  # 完全静默，不发送飞书/企微/邮件
                except Exception:
                    pass  # 时间解析失败不阻塞

        # --- 防御层 3: 状态过期静默 (STALE STATE GUARD) ---
        # 状态超过 90min 未更新 → 售罄/补货大概率是旧闻 → 静默同步
        # 只允许 genuine 新商品通知通过（上新不太可能"补发"）
        if old_state is not None:
            stale_threshold = cfg.get("monitor_options", {}).get(
                "stale_state_silence_minutes", 90)
            old_updated = old_state.get("updated_at", "")
            if old_updated and "JST" in old_updated:
                try:
                    old_dt = datetime.strptime(
                        old_updated.replace(" JST", ""), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=JST)
                    now_dt = datetime.now(JST)
                    stale_min = int((now_dt - old_dt).total_seconds() / 60)
                    if stale_min > stale_threshold:
                        stale_new = sum(1 for c in changes if c["change_type"] == "new")
                        stale_other = len(changes) - stale_new
                        if stale_other > 0 and stale_new <= 5:
                            logging.warning(
                                "STALE STATE SILENCE: state %dmin old, "
                                "suppressing %d non-new changes (%d new allowed). "
                                "State will sync silently.",
                                stale_min, stale_other, stale_new
                            )
                            # 只保留上新通知，售罄/补货/价格变动全部静默
                            changes[:] = [c for c in changes if c["change_type"] == "new"]
                            if not changes:
                                return
                except Exception:
                    pass  # 时间解析失败不阻塞

        # --- 防御层 4: BURST SHIELD (v2.4) ---
        # 上新数超过 max(50, old_total × 8%) → 极可能为状态损坏或版本回滚
        # 统一取代旧 DRIFT SHIELD (30%) + CIRCUIT BREAKER (12%+飞书文本通知)
        # 完全静默 — 不发送任何飞书通知，仅记录日志
        burst_threshold = cfg.get("monitor_options", {}).get(
            "new_product_fuse_threshold", None)
        if burst_threshold is None:
            burst_threshold = max(50, int(old_total * 0.08))
        if old_total > 50 and new_count > burst_threshold:
            logging.error(
                "BURST SHIELD: %d new products exceeds threshold %d "
                "(%.0f%% of %d old total). "
                "Suppressing ALL notifications — likely state corruption or rollback. "
                "State will be updated silently.",
                new_count, burst_threshold,
                new_count / max(old_total, 1) * 100, old_total
            )
            return  # 完全静默，不发送飞书

        # --- 正常通知 ---
        self._send_all_notifications(cfg, conn, changes, now_str)

    def _send_all_notifications(self, cfg, conn, changes, now_str):
        """发送所有已启用的通知 (飞书卡片 + Bot 预警 + AI 摘要).
        子类可覆盖以添加额外的通知渠道 (企业微信/邮件等).
        """
        nc = cfg.get("notifications", {})
        feishu_cfg = nc.get("feishu", {})

        if feishu_cfg.get("enabled") and feishu_cfg.get("webhook_url"):
            # 图片上传
            if feishu_cfg.get("image_preview") and feishu_cfg.get("app_id"):
                ensure_image_keys(conn, changes, feishu_cfg)

            # 主卡片通知
            shop_card = {
                "name": self.shop.name,
                "template_color": self.shop.template_color,
                "footer": self.shop.footer,
                "subtitle_field": self.shop.subtitle_field,
            }
            cards = build_feishu_cards(changes, now_str, shop_card)
            send_feishu_card(feishu_cfg["webhook_url"], cards)

            # Bot 扫货预警
            mo = cfg.get("monitor_options", {})
            if mo.get("bot_alert_enabled", True):
                bot_min = mo.get("bot_alert_min_count", 3)
                maybe_send_bot_alert(
                    feishu_cfg["webhook_url"], changes, now_str,
                    shop_card, min_count=bot_min)

            # AI 智能摘要 (DeepSeek)
            ds_cfg = cfg.get("deepseek", {})
            if ds_cfg.get("enabled") and ds_cfg.get("summary_enabled"):
                try:
                    from ai_analysis import summarize_changes
                    # Convert flat change list to dict format expected by summarize_changes
                    changes_dict = {
                        "new_products": [c["product"] for c in changes if c["change_type"] == "new"],
                        "restocks": [c["product"] for c in changes if c["change_type"] == "restock"],
                        "sold_out": [c["product"] for c in changes if c["change_type"] == "sold_out"],
                        "price_changes": [
                            {**c["product"], "old_price": c.get("old_value", "?"),
                             "new_price": c.get("new_value", "?")}
                            for c in changes if c["change_type"] == "price_change"
                        ],
                    }
                    summary = summarize_changes(
                        changes_dict, self.shop.name, deepseek_cfg=ds_cfg)
                    if summary:
                        requests.post(
                            feishu_cfg["webhook_url"],
                            json={"msg_type": "text", "content": {"text": summary}},
                            timeout=15)
                        logging.info("AI summary sent to Feishu")
                except ImportError:
                    logging.debug("ai_analysis module not available")
                except Exception as e:
                    logging.warning("AI summary failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # 持续运行
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self):
        """注册 SIGINT/SIGTERM 处理器"""
        def handler(sig, frame):
            logging.info("Shutting down...")
            self.running = False
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run_continuous(self, cfg, state_file, db_conn=None):
        """持续监控循环 (自循环模式, 用于本地开发)"""
        self._setup_signal_handlers()
        interval = cfg.get("poll_interval_seconds", 300)
        logging.info("Continuous monitoring started (interval=%ds). "
                     "Press Ctrl+C to stop.", interval)

        while self.running:
            try:
                self.run_once(cfg, state_file, db_conn=db_conn)
            except KeyboardInterrupt:
                logging.info("Keyboard interrupt received")
                break
            except SystemExit:
                break
            except Exception as e:
                logging.error("Run failed: %s", e, exc_info=True)

            if not self.running:
                break

            jitter = random.uniform(-0.2, 0.2) * interval
            wait = interval + jitter
            logging.info("Next check in %.0fs...", wait)
            for _ in range(int(wait)):
                if not self.running:
                    break
                time.sleep(1)
