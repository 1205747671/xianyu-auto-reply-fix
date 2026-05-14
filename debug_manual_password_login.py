#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import sys
from pathlib import Path


def _read_cookie_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot manual password login debug entry")
    parser.add_argument("--account-id", default="manual_password_debug", help="logical account id for logs and browser profile")
    parser.add_argument("--account", required=True, help="login account")
    parser.add_argument("--password", required=True, help="login password")
    parser.add_argument("--headless", action="store_true", help="run in headless mode")
    parser.add_argument("--show-browser", action="store_true", help="force headed mode")
    parser.add_argument("--force-clean-context", action="store_true", help="use clean browser context instead of persistent profile")
    parser.add_argument("--stealth-mode", choices=["auto", "off", "lite", "full"], default="auto")
    parser.add_argument("--max-retries", type=int, default=1, help="slider retries for this manual run")
    parser.add_argument("--initial-cookie", help="optional initial cookie string")
    parser.add_argument("--initial-cookie-file", help="optional file containing initial cookie string")
    parser.add_argument("--browser-channel", help="browser channel, such as chrome/msedge")
    parser.add_argument("--browser-path", help="custom browser executable_path")
    parser.add_argument("--proxy-type", default="none", help="none/http/https/socks5")
    parser.add_argument("--proxy-host", default="", help="proxy host")
    parser.add_argument("--proxy-port", type=int, default=0, help="proxy port")
    parser.add_argument("--proxy-user", default="", help="proxy username")
    parser.add_argument("--proxy-pass", default="", help="proxy password")
    parser.add_argument("--verification-wait-timeout", type=int, default=30, help="seconds to wait when QR/face verification is required")
    parser.add_argument("--keep-verification-screenshot", action="store_true", help="do not delete verification screenshot after the run")
    args = parser.parse_args()

    if args.stealth_mode != "auto":
        os.environ["XY_SLIDER_STEALTH_MODE"] = args.stealth_mode
    os.environ["XY_VERIFICATION_WAIT_TIMEOUT"] = str(max(5, args.verification_wait_timeout))
    if args.keep_verification_screenshot:
        os.environ["XY_KEEP_VERIFICATION_SCREENSHOT"] = "1"

    initial_cookie = ""
    if args.initial_cookie:
        initial_cookie = args.initial_cookie.strip().lstrip("\ufeff")
    elif args.initial_cookie_file:
        initial_cookie = _read_cookie_text(args.initial_cookie_file)

    proxy = {
        "proxy_type": args.proxy_type,
        "proxy_host": args.proxy_host,
        "proxy_port": args.proxy_port,
        "proxy_user": args.proxy_user,
        "proxy_pass": args.proxy_pass,
    }

    from utils.xianyu_slider_stealth import XianyuSliderStealth

    headless = True if args.headless else not args.show_browser
    print(f"account_id={args.account_id}")
    print(f"headless={headless}")
    print("browser_provider=cloakbrowser")
    print(f"stealth_mode={os.environ.get('XY_SLIDER_STEALTH_MODE', 'auto')}")
    print(f"force_clean_context={args.force_clean_context}")
    print(f"max_retries={args.max_retries}")
    print(f"verification_wait_timeout={os.environ.get('XY_VERIFICATION_WAIT_TIMEOUT', '')}")
    print(f"keep_verification_screenshot={bool(args.keep_verification_screenshot)}")

    slider = XianyuSliderStealth(
        user_id=args.account_id,
        enable_learning=True,
        headless=headless,
        initial_cookies=initial_cookie,
        proxy=proxy,
        browser_channel=args.browser_channel,
        executable_path=args.browser_path,
        slider_max_retries=args.max_retries,
        # 按 account_id 复用账号持久化画像目录，便于复现“已登录直接复用”行为。
        use_account_persistent_profile=True,
    )
    print(f"local_browser_info={slider.local_browser_info}")

    def verification_callback(
        message,
        screenshot_path=None,
        verification_url=None,
        screenshot_path_new=None,
        verification_type=None,
        **kwargs,
    ):
        actual_screenshot_path = screenshot_path_new or screenshot_path or ""
        print("verification_required=True")
        print(f"verification_type={verification_type or ''}")
        print(f"verification_screenshot={actual_screenshot_path}")
        print(f"verification_url={verification_url or ''}")
        # Windows 下自动打开截图，方便人工人脸/扫码操作
        try:
            if actual_screenshot_path and os.name == "nt" and os.path.exists(actual_screenshot_path):
                os.startfile(actual_screenshot_path)
        except Exception:
            pass

    result = slider.login_with_password_browser(
        account=args.account,
        password=args.password,
        show_browser=args.show_browser,
        force_clean_context=args.force_clean_context,
        notification_callback=verification_callback,
    )
    success = bool(result)
    print(f"success={success}")
    print(f"last_login_error={getattr(slider, 'last_login_error', '')}")
    if result:
        print(f"cookie_count={len(result)}")
        print(f"has_x5sec={'x5sec' in result}")
    return 0 if success else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"debug_manual_password_login failed: {exc}", file=sys.stderr)
        raise
