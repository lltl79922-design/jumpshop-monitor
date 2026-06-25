#!/usr/bin/env python3
"""
Auto Buy — 补货后自动下单
用Playwright自带浏览器+保存登录态，无需你输入密码
步骤: 登录一次(login) → 以后自动用(use)
"""

import json
import os
import sys
import time
import logging
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
LOG_FILE = SCRIPT_DIR / "logs" / "auto_buy.log"
PROFILE_FILE = DATA_DIR / "buy_profile.json"
AUTH_FILE = DATA_DIR / "jumpshop_auth.json"  # 登录态存储

def setup_logging():
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
    )

def load_profile():
    if PROFILE_FILE.exists():
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_profile(profile):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
def login_and_save():
    """打开浏览器 → 你手动登录Jump Shop → 自动保存登录态"""
    print("正在打开浏览器...")
    print("请在浏览器里登录Jump Shop，登录完成后回到这里按Enter")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        page = context.new_page()
        page.goto("https://jumpshop-benelic.com/account/login", wait_until="networkidle")
        print("浏览器已打开 → 请手动登录")
        print("登录成功后按Enter继续...")
        input()

        # 保存登录态
        context.storage_state(path=str(AUTH_FILE))
        print(f"登录态已保存 → {AUTH_FILE}")
        browser.close()


def buy_product(product_url):
    """自动下单: 用保存的登录态打开 → 加购 → 填地址 → 你确认付款"""
    profile = load_profile()

    if not AUTH_FILE.exists():
        print("尚未登录！请先运行: python auto_buy.py login")
        return False, "未登录"

    if not profile.get("address_ready"):
        print("尚未设置地址！请先运行: python auto_buy.py setup")
        return False, "未设置地址"

    shipping = profile.get("shipping", {})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                storage_state=str(AUTH_FILE),  # 加载登录态
            )
            page = context.new_page()

            # Step 1: 打开商品页
            logging.info(f"Opening: {product_url}")
            page.goto(product_url, wait_until="networkidle", timeout=30000)

            # 检查售罄
            sold_out_text = page.get_by_text("売り切れ").first
            try:
                sold_out_text.wait_for(state="visible", timeout=3000)
                logging.info("SOLD OUT — aborting")
                browser.close()
                return False, "商品已售罄"
            except:
                pass

            # Step 2: 加购
            try:
                page.click('text=カートに入れる', timeout=5000)
                logging.info("Added to cart")
            except:
                try:
                    page.locator('[value="カートに入れる"]').click(timeout=3000)
                except:
                    try:
                        page.locator('button').filter(has_text="カート").first.click(timeout=3000)
                    except:
                        logging.error("Cannot find Add to Cart button")
                        browser.close()
                        return False, "找不到加购按钮"

            page.wait_for_timeout(2000)

            # Step 3: 去购物车 → 结账
            page.goto("https://jumpshop-benelic.com/cart", wait_until="networkidle", timeout=15000)

            try:
                page.click('text=ご購入手続き', timeout=5000)
            except:
                try:
                    page.click('text=Checkout', timeout=3000)
                except:
                    try:
                        page.locator('[name="checkout"]').click(timeout=3000)
                    except:
                        logging.error("Cannot navigate to checkout")
                        browser.close()
                        return False, "无法进入结账"

            page.wait_for_timeout(3000)

            # Step 4: 填地址
            try:
                # 日本Shopify结账页字段名（常见）
                field_map = {
                    "last_name": shipping.get("last_name", ""),
                    "first_name": shipping.get("first_name", ""),
                    "zip": shipping.get("zip", ""),
                    "address1": shipping.get("address", ""),
                    "phone": shipping.get("phone", ""),
                }
                for field, value in field_map.items():
                    if not value:
                        continue
                    try:
                        inp = page.locator(f'input[name*="{field}"], input[placeholder*="{field}"]').first
                        if inp.is_visible(timeout=1000):
                            inp.fill(value)
                    except:
                        pass
                logging.info("Shipping filled (auto-detected fields)")
            except Exception as e:
                logging.warning(f"Shipping fill partial: {e}")

            # Step 5: 截图 → 你确认付款
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(DATA_DIR / "checkout.png"), full_page=True)
            logging.info("Checkout screenshot saved → data/checkout.png")
            print()
            print("========================================")
            print("已到结账页面！请在浏览器里确认付款信息")
            print("确认无误后，手动点击'注文を確定する'")
            print("完成后回到这里按Enter")
            print("========================================")
            input()
            browser.close()
            return True, "手动确认完成"

    except Exception as e:
        logging.error(f"Auto buy error: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    setup_logging()

    if len(sys.argv) < 2:
        print("Auto Buy — 浏览器自动下单（你的登录态+你的地址）")
        print("")
        print("  python auto_buy.py login               登录一次（以后不用再登）")
        print("  python auto_buy.py setup               设置收货地址")
        print("  python auto_buy.py buy <商品URL>        补货后自动加购+结账")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "login":
        login_and_save()

    elif cmd == "setup":
        print("=== 日本收货地址 ===")
        profile = load_profile()
        s = profile.get("shipping", {})

        s["last_name"] = input(f"姓 [{s.get('last_name','')}]: ") or s.get('last_name', '')
        s["first_name"] = input(f"名 [{s.get('first_name','')}]: ") or s.get('first_name', '')
        s["zip"] = input(f"邮编(7位) [{s.get('zip','')}]: ") or s.get('zip', '')
        s["address"] = input(f"地址 [{s.get('address','')}]: ") or s.get('address', '')
        s["phone"] = input(f"电话 [{s.get('phone','')}]: ") or s.get('phone', '')

        profile["shipping"] = s
        profile["address_ready"] = bool(s.get("last_name") and s.get("address") and s.get("zip"))
        save_profile(profile)
        print(f"保存完成。address_ready = {profile['address_ready']}")

    elif cmd == "buy":
        if len(sys.argv) < 3:
            print("用法: python auto_buy.py buy <商品URL>")
            print("示例: python auto_buy.py buy https://jumpshop-benelic.com/products/18045905")
        else:
            ok, msg = buy_product(sys.argv[2])
            print(f"结果: {'OK' if ok else '失败'} — {msg}")

    else:
        print(f"未知命令: {cmd}")
