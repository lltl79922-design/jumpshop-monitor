#!/usr/bin/env python3
"""Jump Shop + ufotable WEBSHOP 监控共享模块 — 飞书API、售罄快照、通知卡片"""

import json
import time
import logging
from pathlib import Path
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

    max_images = feishu_cfg.get("max_image_items", 10)
    count = 0

    for c in changes:
        if count >= max_images:
            break
        p = c["product"]
        if not p.get("image_url"):
            continue

        row = conn.execute("SELECT feishu_img_key FROM products WHERE id=?", (p["id"],)).fetchone()
        if row and row[0]:
            p["feishu_img_key"] = row[0]
            count += 1
            continue

        logging.info(f"Uploading image for: {p['title'][:40]}...")
        img_key = upload_image_to_feishu(p["image_url"], app_id, app_secret)
        if img_key:
            p["feishu_img_key"] = img_key
            conn.execute("UPDATE products SET feishu_img_key=? WHERE id=?", (img_key, p["id"]))
            conn.commit()
            count += 1
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
# 变更日志
# =============================================================================
def log_changes(conn, changes, now_str):
    for c in changes:
        conn.execute(
            "INSERT INTO change_log (product_id, change_type, old_value, new_value, detected_at) VALUES (?, ?, ?, ?, ?)",
            (c["product_id"], c["change_type"], c["old_value"], c["new_value"], now_str))
    conn.commit()

# =============================================================================
# 飞书交互式卡片
# =============================================================================
def build_feishu_card(changes, now_str, shop_config):
    """
    shop_config 字段:
      - name: 商店显示名称
      - template_color: 卡片头部颜色 (red/blue)
      - footer: 页脚文字
      - subtitle_field: 商品副标题字段 ("vendor" 或 "works")
    """
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
    subtitle_field = shop_config.get("subtitle_field", "vendor")

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
            subtitle = p.get(subtitle_field, "")
            img_key = p.get("feishu_img_key", "")

            product_md = f"**{p['title']}**\n{price_yen} | {subtitle} | {status_icon} {status_text}"
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
        "elements": [{"tag": "plain_text", "content": f"  {now_str}  |  {shop_config['footer']}"}]
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"  {shop_config['name']} 商品監視"},
            "template": shop_config["template_color"]
        },
        "elements": elements
    }

    return {"msg_type": "interactive", "card": card}


def send_feishu_card(webhook_url, card_payload, fallback_text=""):
    """发送飞书卡片，失败时降级为纯文本"""
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
