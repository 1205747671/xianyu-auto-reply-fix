import asyncio
import json
import os
import sys
import time
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, Optional
from queue import Queue

from db_manager import db_manager
from utils.account_browser_runtime import account_browser_runtime_manager
from utils.xianyu_slider_stealth import XianyuSliderStealth
from XianyuAutoAsync import XianyuLive


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNT_ID = "10"
USER_ID = 1
USERNAME = "15614318625"
PASSWORD = "qq1205747671"
STATE_PATH = LOG_DIR / f"account_{ACCOUNT_ID}_face_probe_state.json"
EVENTS_PATH = LOG_DIR / f"account_{ACCOUNT_ID}_face_probe_events.jsonl"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def write_state(**updates: Any) -> None:
    state: Dict[str, Any] = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state.update(updates)
    state["updated_at"] = now_text()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def record_event(kind: str, **payload: Any) -> None:
    event = {
        "time": now_text(),
        "kind": kind,
        **payload,
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=True), flush=True)


def normalize_url(url: Optional[str]) -> str:
    return str(url or "").strip()


def is_interesting_url(url: Optional[str]) -> bool:
    text = normalize_url(url).lower()
    if not text:
        return False
    keywords = (
        "goofish",
        "xianyu",
        "taobao",
        "passport",
        "login",
        "captcha",
        "verify",
        "h5api",
        "mtop",
        "websocket",
    )
    return any(keyword in text for keyword in keywords)


def safe_call(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, attr)
        return value() if callable(value) else value
    except Exception:
        return default


def build_cookie_string(cookie_dict: Dict[str, str]) -> str:
    parts = []
    for name, value in cookie_dict.items():
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def run_coro_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_queue: "Queue[tuple[bool, Any]]" = Queue(maxsize=1)

    def _runner() -> None:
        try:
            result = asyncio.run(coro)
            result_queue.put((True, result))
        except Exception as exc:
            result_queue.put((False, exc))

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    ok, payload = result_queue.get()
    if ok:
        return payload
    raise payload


def attach_page_watchers(page: Any, page_label: str) -> None:
    def on_request(request: Any) -> None:
        try:
            url = request.url
            if not is_interesting_url(url):
                return
            record_event(
                "request",
                page=page_label,
                method=safe_call(request, "method", ""),
                resource_type=safe_call(request, "resource_type", ""),
                url=url,
            )
        except Exception as exc:
            record_event("watcher_error", page=page_label, scope="request", error=str(exc))

    def on_response(response: Any) -> None:
        try:
            url = response.url
            if not is_interesting_url(url):
                return
            headers = {}
            try:
                headers = response.headers or {}
            except Exception:
                headers = {}
            set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie")
            record_event(
                "response",
                page=page_label,
                status=safe_call(response, "status", None),
                url=url,
                has_set_cookie=bool(set_cookie),
                set_cookie_preview=(set_cookie[:240] if isinstance(set_cookie, str) else None),
            )
        except Exception as exc:
            record_event("watcher_error", page=page_label, scope="response", error=str(exc))

    def on_framenavigated(frame: Any) -> None:
        try:
            url = frame.url
            if not is_interesting_url(url):
                return
            record_event("frame_navigated", page=page_label, url=url, name=safe_call(frame, "name", ""))
        except Exception as exc:
            record_event("watcher_error", page=page_label, scope="framenavigated", error=str(exc))

    try:
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("framenavigated", on_framenavigated)
        record_event(
            "page_attached",
            page=page_label,
            url=safe_call(page, "url", ""),
            title=safe_call(page, "title", ""),
        )
    except Exception as exc:
        record_event("watcher_error", page=page_label, scope="attach", error=str(exc))


def attach_context_watchers(context: Any) -> None:
    known_pages = set()

    def bind_page(page: Any) -> None:
        page_id = id(page)
        if page_id in known_pages:
            return
        known_pages.add(page_id)
        attach_page_watchers(page, f"page_{len(known_pages)}")

    try:
        for existing_page in list(getattr(context, "pages", []) or []):
            bind_page(existing_page)
    except Exception as exc:
        record_event("watcher_error", scope="existing_pages", error=str(exc))

    def on_page(page: Any) -> None:
        bind_page(page)
        try:
            record_event("new_page", url=safe_call(page, "url", ""))
        except Exception:
            pass

    try:
        context.on("page", on_page)
    except Exception as exc:
        record_event("watcher_error", scope="context_page", error=str(exc))


def verification_callback(message: str, *_args: Any, **kwargs: Any) -> None:
    verification_type = kwargs.get("verification_type")
    pending_completion = bool(kwargs.get("verification_pending_completion"))
    frame_url = _args[1] if len(_args) >= 2 else None
    screenshot_path = _args[2] if len(_args) >= 3 else None

    payload = {
        "status": "verification_pending" if pending_completion else "verification_required",
        "verification_type": verification_type,
        "verification_url": frame_url,
        "screenshot_path": screenshot_path,
        "message": message,
    }
    write_state(**payload)
    record_event("verification_callback", **payload)


async def run_postflight(cookie_dict: Dict[str, str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    cookies_str = build_cookie_string(cookie_dict)
    live = XianyuLive(
        cookies_str,
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        register_instance=False,
    )
    try:
        preflight = await live.preflight_token_after_password_login()
        result["preflight_token"] = preflight
        result["last_token_refresh_status"] = getattr(live, "last_token_refresh_status", None)
        result["last_token_refresh_error_message"] = getattr(live, "last_token_refresh_error_message", None)

        keepalive_ok = await live.keep_session_alive()
        result["keep_session_alive"] = keepalive_ok
        result["last_session_keepalive_status"] = getattr(live, "last_session_keepalive_status", None)
        result["last_session_keepalive_error_message"] = getattr(live, "last_session_keepalive_error_message", None)

        ws_headers = live._build_websocket_headers()
        ws_result: Dict[str, Any] = {"connected": False, "init_sent": False}
        try:
            async with await live._create_websocket_connection(ws_headers) as websocket:
                ws_result["connected"] = True
                await live.init(websocket)
                ws_result["init_sent"] = True
                try:
                    first_message = await asyncio.wait_for(websocket.recv(), timeout=20)
                    ws_result["first_message_preview"] = str(first_message)[:800]
                except Exception as ws_read_exc:
                    ws_result["first_message_error"] = str(ws_read_exc)
        except Exception as ws_exc:
            ws_result["connect_error"] = str(ws_exc)
        result["websocket"] = ws_result
        return result
    finally:
        session = getattr(live, "session", None)
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


def ensure_account_row() -> None:
    updated = db_manager.update_cookie_account_info(
        account_id=ACCOUNT_ID,
        cookie_value="",
        username=USERNAME,
        password=PASSWORD,
        user_id=USER_ID,
    )
    if not updated:
        # 记录可能已存在，但 cookie_value="" 创建路径没走成功时，退化为只更新账密。
        db_manager.update_cookie_account_info(
            account_id=ACCOUNT_ID,
            username=USERNAME,
            password=PASSWORD,
            user_id=USER_ID,
        )


def main() -> int:
    write_state(
        status="starting",
        account_id=ACCOUNT_ID,
        username=USERNAME,
        script=os.path.basename(__file__),
        pid=os.getpid(),
    )
    record_event("script_start", pid=os.getpid(), python=sys.executable)

    lease = None
    slider = None
    try:
        ensure_account_row()
        write_state(status="account_ready")
        record_event("account_ready", account_id=ACCOUNT_ID, user_id=USER_ID)

        slider = XianyuSliderStealth(
            user_id=ACCOUNT_ID,
            headless=False,
            use_account_persistent_profile=True,
        )
        slider.verification_wait_timeout = 1200
        slider.keep_verification_screenshots = True

        runtime_request = slider.build_managed_runtime_request(account_id=ACCOUNT_ID)
        launch_options = dict(runtime_request.get("launch_options") or {})
        record_event(
            "runtime_request",
            use_persistent_context=runtime_request.get("use_persistent_context"),
            profile_dir=runtime_request.get("profile_dir"),
            headless=launch_options.get("headless"),
            humanize=launch_options.get("humanize"),
            human_preset=launch_options.get("human_preset"),
            args=launch_options.get("args"),
        )
        write_state(
            status="runtime_launching",
            profile_dir=runtime_request.get("profile_dir"),
            launch_options={
                "headless": launch_options.get("headless"),
                "humanize": launch_options.get("humanize"),
                "human_preset": launch_options.get("human_preset"),
            },
        )

        lease = account_browser_runtime_manager.acquire_runtime_sync(
            account_id=ACCOUNT_ID,
            purpose="manual_password_face_probe",
            exclusive=True,
            runtime_request=runtime_request,
        )
        page, context = account_browser_runtime_manager.get_fresh_page_sync(lease)
        attach_context_watchers(context)

        slider.attach_managed_runtime(
            lease=lease,
            runtime=lease.runtime,
            browser=getattr(lease.runtime, "browser", None),
            context=context,
            page=page,
            playwright=getattr(lease.runtime, "playwright", None),
            browser_features=runtime_request.get("browser_features"),
            profile_id=runtime_request.get("profile_id"),
        )

        record_event(
            "runtime_ready",
            cdp_endpoint=getattr(lease.runtime, "cdp_endpoint", None),
            current_url=safe_call(page, "url", ""),
        )
        write_state(status="login_running", current_url=safe_call(page, "url", ""))

        start_ts = time.time()
        cookies = slider.login_with_password_browser(
            USERNAME,
            PASSWORD,
            show_browser=True,
            notification_callback=verification_callback,
            require_managed_runtime=True,
        )
        elapsed = round(time.time() - start_ts, 2)

        if not cookies:
            error_message = getattr(slider, "last_login_error", None) or "login returned empty cookies"
            write_state(
                status="login_failed",
                elapsed_seconds=elapsed,
                last_login_error=error_message,
                last_verification_feedback=getattr(slider, "last_verification_feedback", None),
            )
            record_event(
                "login_failed",
                elapsed_seconds=elapsed,
                error=error_message,
                last_verification_feedback=getattr(slider, "last_verification_feedback", None),
            )
            return 1

        cookie_summary = slider._summarize_cookie_dict_for_debug(cookies)
        write_state(
            status="login_success",
            elapsed_seconds=elapsed,
            cookie_summary=cookie_summary,
            last_verification_feedback=getattr(slider, "last_verification_feedback", None),
            page_url=safe_call(page, "url", ""),
        )
        record_event(
            "login_success",
            elapsed_seconds=elapsed,
            cookie_summary=cookie_summary,
            last_verification_feedback=getattr(slider, "last_verification_feedback", None),
        )

        if lease is not None:
            account_browser_runtime_manager.release_runtime_sync(
                lease,
                reason="manual_password_face_probe_before_postflight",
            )
            record_event("runtime_released_before_postflight")
            lease = None
        if slider is not None:
            try:
                slider._detach_managed_runtime()
            except Exception:
                pass

        write_state(status="postflight_running")
        postflight = run_coro_blocking(run_postflight(cookies))
        write_state(status="postflight_complete", postflight=postflight)
        record_event("postflight_complete", **postflight)
        return 0
    except Exception as exc:
        error_text = str(exc)
        write_state(
            status="script_error",
            error=error_text,
            traceback=traceback.format_exc(),
        )
        record_event("script_error", error=error_text, traceback=traceback.format_exc())
        return 2
    finally:
        if lease is not None:
            try:
                account_browser_runtime_manager.release_runtime_sync(
                    lease,
                    reason="manual_password_face_probe_finished",
                )
            except Exception as release_exc:
                record_event("release_runtime_error", error=str(release_exc))
        if slider is not None:
            try:
                slider._detach_managed_runtime()
            except Exception:
                pass
        record_event("script_end")


if __name__ == "__main__":
    raise SystemExit(main())
