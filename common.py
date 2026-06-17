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
