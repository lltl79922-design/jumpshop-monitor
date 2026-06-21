#!/usr/bin/env python3
"""
Self-Heal 自愈脚本
检测状态文件健康度，>90min 未更新自动重启 monitor
用法:
  python self_heal.py
环境变量:
  GITHUB_TOKEN, GITHUB_REPOSITORY, FEISHU_WEBHOOK_URL (可选)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))
API_BASE = "https://api.github.com"

STATE_CHECKS = [
    {
        "name": "JumpShop",
        "file": "data/jumpshop_state.json",
        "workflow": "monitor.yml",
        "min_stale": 90,
    },
    {
        "name": "ufotable",
        "file": "data/ufotable_state.json",
        "workflow": "ufotable-monitor.yml",
        "min_stale": 90,
    },
]


def get_gh_headers():
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
    }


def trigger_workflow(wf_name, reason, headers):
    """通过 GitHub REST API 触发 workflow_dispatch"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print(f"  [HEAL] GITHUB_REPOSITORY not set, cannot trigger {wf_name}")
        return False, f"GITHUB_REPOSITORY not set"

    url = f"{API_BASE}/repos/{repo}/actions/workflows/{wf_name}/dispatches"
    print(f"  [HEAL] Triggering {wf_name} (reason: {reason})")

    try:
        resp = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
        if resp.status_code == 204:
            print(f"  [HEAL] {wf_name} triggered OK (HTTP 204)")
            return True, f"OK"
        else:
            print(f"  [HEAL] {wf_name} trigger FAILED (HTTP {resp.status_code}): {resp.text[:200]}")
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        print(f"  [HEAL] {wf_name} trigger ERROR: {e}")
        return False, str(e)


def check_state_file(check, headers):
    """检查单个状态文件, 返回 (alerts, healed, should_trigger)"""
    name = check["name"]
    filepath = check["file"]
    wf = check["workflow"]
    min_stale = check["min_stale"]

    alerts = []
    healed = []

    # git sync
    os.system("git fetch origin main 2>/dev/null && git reset --hard origin/main 2>/dev/null || true")

    if not os.path.exists(filepath):
        print(f"[{name}] State file MISSING")
        # Try recover from git
        import subprocess
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H", "--", filepath],
            capture_output=True, text=True
        )
        last_commit = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
        if last_commit:
            os.system(f"git show {last_commit}:{filepath} > {filepath} 2>/dev/null || true")
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        s = json.load(f)
                    if len(s.get("products", {})) > 0:
                        print(f"[{name}] Recovered from git history")
                        healed.append(f"{name} state file recovered from git")
                    else:
                        _init_state(filepath)
                except Exception:
                    _init_state(filepath)
            else:
                _init_state(filepath)
        else:
            _init_state(filepath)
        ok, msg = trigger_workflow(wf, "state file missing", headers)
        if ok:
            healed.append(f"Restarted {wf} (state file missing)")
        else:
            alerts.append(f"Cannot restart {wf}: {msg} (state file missing)")
        return alerts, healed

    # Check corruption
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        print(f"[{name}] State file CORRUPTED")
        # Try recover
        import subprocess
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H", "--", filepath],
            capture_output=True, text=True
        )
        last_commit = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
        if last_commit:
            os.system(f"git show {last_commit}:{filepath} > {filepath} 2>/dev/null || true")
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception:
            _init_state(filepath)
        ok, msg = trigger_workflow(wf, "state file corrupted", headers)
        if ok:
            healed.append(f"Restarted {wf} (state file corrupted)")
        else:
            alerts.append(f"Cannot restart {wf}: {msg} (state file corrupted)")
        return alerts, healed

    # Check staleness
    ts = state.get("updated_at", "")
    age_min = 9999
    if ts and "JST" in ts:
        try:
            dt = datetime.strptime(ts.replace(" JST", ""), "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=JST)
            age_min = int((datetime.now(JST) - dt).total_seconds() / 60)
        except Exception:
            pass

    print(f"[{name}] State age: {age_min}min (threshold: {min_stale}min)")
    if age_min > min_stale:
        print(f"[{name}] DEAD MAN'S SWITCH: {age_min}min stale -> auto-restart")
        ok, msg = trigger_workflow(wf, f"dead man switch: {age_min}min stale", headers)
        if ok:
            healed.append(f"Restarted {wf} (stale {age_min}min)")
        else:
            alerts.append(f"Cannot restart {wf}: {msg} (stale {age_min}min)")
    else:
        print(f"[{name}] OK ({age_min}min)")

    return alerts, healed


def _init_state(filepath):
    """初始化空白状态文件"""
    empty = {
        "version": 2,
        "products": {},
        "soldout_ids": [],
        "updated_at": "",
        "total_products": 0,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(empty, f, ensure_ascii=False)
    print(f"  Initialized fresh state file: {filepath}")


def send_feishu_alert(webhook_url, text):
    """发送飞书告警"""
    if not webhook_url or webhook_url.startswith("YOUR_"):
        return
    try:
        requests.post(
            webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
    except Exception:
        pass


def main():
    headers = get_gh_headers()
    all_alerts = []
    all_healed = []

    for check in STATE_CHECKS:
        print(f"\n=== {check['name']} Self-Heal {datetime.now(JST).isoformat()} ===")
        try:
            alerts, healed = check_state_file(check, headers)
            all_alerts.extend(alerts)
            all_healed.extend(healed)
        except Exception as e:
            print(f"[{check['name']}] check_state_file ERROR: {e}")
            all_alerts.append(f"{check['name']}: {e}")

    # Summary
    if all_healed:
        print("\n=== HEALING ACTIONS ===")
        for h in all_healed:
            print(f"  [HEALED] {h}")

    if all_alerts:
        print("\n=== UNRESOLVED ALERTS ===")
        for a in all_alerts:
            print(f"  [ALERT] {a}")
        webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        if webhook:
            text = "Self-Heal Alerts:\n" + "\n".join(f"  - {a}" for a in all_alerts)
            send_feishu_alert(webhook, text)

    print("\nSelf-heal complete.")
    return 0 if not all_alerts else 1


if __name__ == "__main__":
    sys.exit(main())
