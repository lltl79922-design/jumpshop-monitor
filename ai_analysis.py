#!/usr/bin/env python3
"""
Jump Shop 监控 — DeepSeek AI 分析模块
功能: 商品变化智能摘要、趋势分析
模型: deepseek-chat (成本 ~¥0.001/次)
用法:
  from ai_analysis import summarize_changes
  summary = summarize_changes(changes, shop_name="JUMP SHOP", cfg=deepseek_cfg)
"""

import json
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DeepSeek API (OpenAI 兼容)
# ---------------------------------------------------------------------------
DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"


def _call_deepseek(api_key: str, model: str, messages: list,
                   max_tokens: int = 500, temperature: float = 0.7,
                   timeout: int = 30) -> Optional[str]:
    """调用 DeepSeek Chat API, 返回回复文本或 None"""
    if not api_key or api_key.startswith("YOUR_"):
        logger.debug("DeepSeek API key not configured, skipping")
        return None

    try:
        resp = requests.post(
            DEEPSEEK_API,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"DeepSeek API error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        logger.info(
            f"DeepSeek: {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} "
            f"tokens, cost ~¥{_estimate_cost(usage):.4f}"
        )
        return content.strip()

    except Exception as e:
        logger.warning(f"DeepSeek API exception: {e}")
        return None


def _estimate_cost(usage: dict) -> float:
    """估算 DeepSeek 成本 (RMB)
    deepseek-chat: input ¥0.001/1K tokens, output ¥0.002/1K tokens
    """
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cost = (prompt_tokens * 0.001 + completion_tokens * 0.002) / 1000
    return cost


# ---------------------------------------------------------------------------
# 变化摘要
# ---------------------------------------------------------------------------

def summarize_changes(changes: dict, shop_name: str,
                      deepseek_cfg: Optional[dict] = None) -> Optional[str]:
    """用 DeepSeek 生成商品变化摘要 (中文/日文混合)

    Args:
        changes: 来自 detect_all_changes_from_state 的变化字典
        shop_name: "JUMP SHOP" 或 "ufotable WEBSHOP"
        deepseek_cfg: {"api_key": "sk-...", "model": "deepseek-chat", ...}

    Returns:
        AI 生成的摘要文本, 或 None (如果变更不足以生成摘要或 API 不可用)
    """
    if not deepseek_cfg or not deepseek_cfg.get("enabled"):
        return None

    api_key = deepseek_cfg.get("api_key", "")
    model = deepseek_cfg.get("model", "deepseek-chat")
    max_tokens = deepseek_cfg.get("max_tokens", 500)
    temperature = deepseek_cfg.get("temperature", 0.7)
    min_changes = deepseek_cfg.get("summary_min_changes", 3)

    # 收集所有变化
    new_products = changes.get("new_products", [])
    restocks = changes.get("restocks", [])
    sold_out = changes.get("sold_out", [])
    price_changes = changes.get("price_changes", [])

    total = len(new_products) + len(restocks) + len(sold_out) + len(price_changes)
    if total < min_changes:
        logger.debug(f"Only {total} changes (<{min_changes}), skipping AI summary")
        return None

    # 构建变化摘要文本
    lines = [f"{shop_name} 商品变化检测报告"]
    if new_products:
        items = [f"• {p.get('title', '?')} (¥{p.get('price', '?')})" for p in new_products[:10]]
        if len(new_products) > 10:
            items.append(f"  ... 及其他 {len(new_products) - 10} 件")
        lines.append(f"\n🆕 新商品 ({len(new_products)}件):")
        lines.extend(items)
    if restocks:
        items = [f"• {p.get('title', '?')} (¥{p.get('price', '?')})" for p in restocks[:10]]
        if len(restocks) > 10:
            items.append(f"  ... 及其他 {len(restocks) - 10} 件")
        lines.append(f"\n🔄 补货 ({len(restocks)}件):")
        lines.extend(items)
    if sold_out:
        items = [f"• {p.get('title', '?')} (¥{p.get('price', '?')})" for p in sold_out[:10]]
        if len(sold_out) > 10:
            items.append(f"  ... 及其他 {len(sold_out) - 10} 件")
        lines.append(f"\n📤 售罄 ({len(sold_out)}件):")
        lines.extend(items)
    if price_changes:
        items = []
        for p in price_changes[:5]:
            old_price = p.get("old_price", "?")
            new_price = p.get("new_price", "?")
            items.append(f"• {p.get('title', '?')}: ¥{old_price} → ¥{new_price}")
        lines.append(f"\n💰 价格变动 ({len(price_changes)}件):")
        lines.extend(items)

    changes_text = "\n".join(lines)

    # 构建 prompt
    system_prompt = (
        "你是一个电商监控AI助手。根据商品变化数据，用中文生成简洁的监控摘要。"
        "风格：精准、简洁、有洞察。50-150字。"
        "如果售罄的商品是热门IP（鬼灭之刃、咒术回战、海贼王、排球少年等），请特别标注。"
        "如果补货值得关注，也请指出。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"请为以下商品变化生成监控摘要:\n\n{changes_text}"},
    ]

    logger.info(f"Calling DeepSeek for {shop_name} summary ({total} changes)...")
    result = _call_deepseek(api_key, model, messages,
                            max_tokens=max_tokens, temperature=temperature)

    if result:
        return f"🤖 AI 摘要 ({shop_name}):\n{result}"
    return None


# ---------------------------------------------------------------------------
# 智能告警过滤 (可选, 未来扩展)
# ---------------------------------------------------------------------------

def should_alert(changes: dict, deepseek_cfg: Optional[dict] = None) -> bool:
    """快速判断是否应该发送告警 (本地规则, 不调用AI)

    当前逻辑: 有变化就告警, 未来可接入 AI 过滤噪音。
    """
    total = (len(changes.get("new_products", [])) +
             len(changes.get("restocks", [])) +
             len(changes.get("sold_out", [])) +
             len(changes.get("price_changes", [])))
    return total > 0
