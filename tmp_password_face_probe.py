import asyncio
import hashlib
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
from utils.account_browser_runtime import (
    account_browser_runtime_manager,
    resolve_runtime_attach_metadata,
)
from utils.xianyu_slider_stealth import XianyuSliderStealth
from XianyuAutoAsync import XianyuLive


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNT_ID = "10"
USER_ID = 1
USERNAME = "15614318625"
PASSWORD = "qq1205747671"
HEADLESS = str(os.environ.get("XY_FACE_PROBE_HEADLESS", "")).strip().lower() in {"1", "true", "yes", "on"}
IM_SETTLE_SECONDS = max(6, int(str(os.environ.get("XY_FACE_PROBE_IM_SETTLE_SECONDS", "18")).strip() or "18"))
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


def summarize_text(value: Any, limit: int = 1200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def should_capture_api_body(url: Optional[str]) -> bool:
    text = normalize_url(url).lower()
    if not text:
        return False
    keywords = (
        "mtop.taobao.idlemessage.pc.login.token",
        "mtop.taobao.idlemessage.pc.loginuser.get",
        "mtop.taobao.idlemessage.pc.accs.token",
        "mtop.taobao.idlemessage.pc.session.sync",
        "_____tmd_____",
    )
    return any(keyword in text for keyword in keywords)


def summarize_headers(headers: Any) -> Dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    interesting_keys = (
        "user-agent",
        "referer",
        "origin",
        "content-type",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "x-requested-with",
    )
    result: Dict[str, str] = {}
    for key in interesting_keys:
        value = headers.get(key) or headers.get(key.title()) or headers.get(key.upper())
        if value:
            result[key] = str(value)
    return result


def build_cookie_meta(cookie_dict: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for name, value in sorted((cookie_dict or {}).items()):
        text = str(value or "")
        meta[str(name)] = {
            "length": len(text),
            "sha256_12": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else None,
        }
    return meta


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
            headers = safe_call(request, "headers", {}) or {}
            post_data = safe_call(request, "post_data", None)
            record_event(
                "request",
                page=page_label,
                method=safe_call(request, "method", ""),
                resource_type=safe_call(request, "resource_type", ""),
                url=url,
                header_subset=summarize_headers(headers),
                post_data_preview=summarize_text(post_data, limit=800),
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
            body_preview = None
            if should_capture_api_body(url):
                try:
                    body_preview = summarize_text(response.text(), limit=1500)
                except Exception as body_exc:
                    body_preview = f"<body_read_failed:{body_exc}>"
            record_event(
                "response",
                page=page_label,
                status=safe_call(response, "status", None),
                url=url,
                has_set_cookie=bool(set_cookie),
                set_cookie_preview=(set_cookie[:240] if isinstance(set_cookie, str) else None),
                body_preview=body_preview,
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


def capture_page_storage(page: Any, label: str) -> None:
    try:
        storage_snapshot = page.evaluate(
            """
            () => {
                const dump = (storage) => {
                    const out = {};
                    for (let i = 0; i < storage.length; i += 1) {
                        const key = storage.key(i);
                        const value = storage.getItem(key) || "";
                        out[key] = value.slice(0, 400);
                    }
                    return out;
                };
                return {
                    href: location.href,
                    title: document.title,
                    cookieKeys: document.cookie.split(';').map((part) => part.split('=')[0].trim()).filter(Boolean),
                    localStorage: dump(window.localStorage),
                    sessionStorage: dump(window.sessionStorage),
                };
            }
            """
        )
        record_event(
            "storage_snapshot",
            label=label,
            href=storage_snapshot.get("href"),
            title=storage_snapshot.get("title"),
            cookie_keys=storage_snapshot.get("cookieKeys"),
            local_storage=storage_snapshot.get("localStorage"),
            session_storage=storage_snapshot.get("sessionStorage"),
        )
    except Exception as exc:
        record_event("storage_snapshot_error", label=label, error=str(exc))


def pick_better_cookie_snapshot(
    slider: XianyuSliderStealth,
    current_best: Optional[Dict[str, str]],
    candidate: Optional[Dict[str, str]],
) -> Dict[str, str]:
    current_best = dict(current_best or {})
    candidate = dict(candidate or {})
    if not candidate:
        return current_best
    if not current_best:
        return candidate

    best_summary = slider._summarize_cookie_dict_for_debug(current_best)
    candidate_summary = slider._summarize_cookie_dict_for_debug(candidate)
    best_missing_protected = len(best_summary.get("missing_protected_fields") or [])
    candidate_missing_protected = len(candidate_summary.get("missing_protected_fields") or [])
    if candidate_missing_protected < best_missing_protected:
        return candidate
    if candidate_missing_protected > best_missing_protected:
        return current_best

    best_missing_required = len(best_summary.get("missing_required_fields") or [])
    candidate_missing_required = len(candidate_summary.get("missing_required_fields") or [])
    if candidate_missing_required < best_missing_required:
        return candidate
    if candidate_missing_required > best_missing_required:
        return current_best

    return candidate if len(candidate) >= len(current_best) else current_best


def record_cookie_snapshot(
    slider: XianyuSliderStealth,
    context: Any,
    page: Any,
    *,
    label: str,
) -> Dict[str, str]:
    cookies_dict = slider._snapshot_context_cookies(context, page=page)
    summary = slider._summarize_cookie_dict_for_debug(cookies_dict)
    record_event(
        "cookie_snapshot",
        label=label,
        page_url=safe_call(page, "url", ""),
        summary=summary,
        cookie_meta=build_cookie_meta(cookies_dict),
    )
    return cookies_dict


def run_live_im_diagnostics(
    slider: XianyuSliderStealth,
    context: Any,
    page: Any,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "settle_seconds": IM_SETTLE_SECONDS,
        "page_url_before": safe_call(page, "url", ""),
    }
    best_cookies: Dict[str, str] = {}
    try:
        if normalize_url(safe_call(page, "url", "")).lower() != "https://www.goofish.com/im":
            try:
                page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=20000)
                time.sleep(2)
            except Exception as nav_exc:
                record_event("live_im_navigation_error", error=str(nav_exc), current_url=safe_call(page, "url", ""))

        capture_page_storage(page, "im_main_before_wait")
        best_cookies = record_cookie_snapshot(slider, context, page, label="im_main_before_wait")

        for waited in range(3, IM_SETTLE_SECONDS + 1, 3):
            time.sleep(3)
            candidate = record_cookie_snapshot(
                slider,
                context,
                page,
                label=f"im_main_wait_t+{waited}s",
            )
            best_cookies = pick_better_cookie_snapshot(slider, best_cookies, candidate)

        capture_page_storage(page, "im_main_after_wait")

        fresh_page = None
        try:
            fresh_page = context.new_page()
            time.sleep(0.5)
            fresh_page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)
            capture_page_storage(fresh_page, "im_fresh_before_wait")
            candidate = record_cookie_snapshot(slider, context, fresh_page, label="im_fresh_before_wait")
            best_cookies = pick_better_cookie_snapshot(slider, best_cookies, candidate)

            fresh_wait_seconds = max(6, min(12, IM_SETTLE_SECONDS))
            for waited in range(3, fresh_wait_seconds + 1, 3):
                time.sleep(3)
                candidate = record_cookie_snapshot(
                    slider,
                    context,
                    fresh_page,
                    label=f"im_fresh_wait_t+{waited}s",
                )
                best_cookies = pick_better_cookie_snapshot(slider, best_cookies, candidate)

            capture_page_storage(fresh_page, "im_fresh_after_wait")
        except Exception as fresh_exc:
            record_event("live_im_fresh_page_error", error=str(fresh_exc))
        finally:
            if fresh_page is not None:
                try:
                    fresh_page.close()
                except Exception:
                    pass

        result["best_cookie_summary"] = slider._summarize_cookie_dict_for_debug(best_cookies)
        result["best_cookies"] = best_cookies
        return result
    except Exception as exc:
        record_event("live_im_diagnostics_error", error=str(exc))
        result["error"] = str(exc)
        result["best_cookies"] = best_cookies
        if best_cookies:
            result["best_cookie_summary"] = slider._summarize_cookie_dict_for_debug(best_cookies)
        return result


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

    slider = None
    try:
        ensure_account_row()
        write_state(status="account_ready")
        record_event("account_ready", account_id=ACCOUNT_ID, user_id=USER_ID)

        slider = XianyuSliderStealth(
            user_id=ACCOUNT_ID,
            headless=HEADLESS,
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
            headless=HEADLESS,
            launch_options={
                "headless": launch_options.get("headless"),
                "humanize": launch_options.get("humanize"),
                "human_preset": launch_options.get("human_preset"),
            },
        )

        def run_login_flow():
            lease = None
            try:
                lease = account_browser_runtime_manager.acquire_runtime_sync(
                    account_id=ACCOUNT_ID,
                    purpose="manual_password_face_probe",
                    exclusive=True,
                    runtime_request=runtime_request,
                )
                page, context = account_browser_runtime_manager.get_fresh_page_sync(lease)
                attach_context_watchers(context)
                runtime_browser_features, runtime_profile_id = resolve_runtime_attach_metadata(
                    lease.runtime,
                    runtime_request,
                )

                slider.attach_managed_runtime(
                    lease=lease,
                    runtime=lease.runtime,
                    browser=getattr(lease.runtime, "browser", None),
                    context=context,
                    page=page,
                    playwright=getattr(lease.runtime, "playwright", None),
                    browser_features=runtime_browser_features,
                    profile_id=runtime_profile_id,
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
                    show_browser=not HEADLESS,
                    notification_callback=verification_callback,
                    require_managed_runtime=True,
                )
                elapsed = round(time.time() - start_ts, 2)
                settled_diag = {}
                settled_cookies = {}
                if cookies:
                    settled_diag = run_live_im_diagnostics(slider, context, page)
                    settled_cookies = dict(settled_diag.get("best_cookies") or {})
                return {
                    "cookies": cookies,
                    "elapsed": elapsed,
                    "page_url": safe_call(page, "url", ""),
                    "last_verification_feedback": getattr(slider, "last_verification_feedback", None),
                    "settled_diag": settled_diag,
                    "settled_cookies": settled_cookies,
                }
            finally:
                if lease is not None:
                    try:
                        account_browser_runtime_manager.release_runtime_sync(
                            lease,
                            reason="manual_password_face_probe_login_finished",
                        )
                    except Exception as release_exc:
                        record_event("release_runtime_error", error=str(release_exc))
                try:
                    slider._detach_managed_runtime()
                except Exception:
                    pass

        login_result = account_browser_runtime_manager.run_sync_task_on_account_thread(
            ACCOUNT_ID,
            run_login_flow,
        )
        cookies = login_result.get("cookies")
        elapsed = login_result.get("elapsed")

        if not cookies:
            error_message = getattr(slider, "last_login_error", None) or "login returned empty cookies"
            write_state(
                status="login_failed",
                elapsed_seconds=elapsed,
                last_login_error=error_message,
                last_verification_feedback=login_result.get("last_verification_feedback"),
            )
            record_event(
                "login_failed",
                elapsed_seconds=elapsed,
                error=error_message,
                last_verification_feedback=login_result.get("last_verification_feedback"),
            )
            return 1

        cookie_summary = slider._summarize_cookie_dict_for_debug(cookies)
        settled_diag = login_result.get("settled_diag") or {}
        settled_cookies = dict(login_result.get("settled_cookies") or {})
        settled_cookie_summary = (
            slider._summarize_cookie_dict_for_debug(settled_cookies)
            if settled_cookies else None
        )
        postflight_cookies = settled_cookies or cookies
        write_state(
            status="login_success",
            elapsed_seconds=elapsed,
            cookie_summary=cookie_summary,
            settled_cookie_summary=settled_cookie_summary,
            live_im_diag=settled_diag,
            last_verification_feedback=login_result.get("last_verification_feedback"),
            page_url=login_result.get("page_url", ""),
        )
        record_event(
            "login_success",
            elapsed_seconds=elapsed,
            cookie_summary=cookie_summary,
            settled_cookie_summary=settled_cookie_summary,
            live_im_diag=settled_diag,
            last_verification_feedback=login_result.get("last_verification_feedback"),
        )
        record_event(
            "runtime_released_before_postflight",
            postflight_cookie_source=("settled_runtime_snapshot" if settled_cookies else "login_return"),
            postflight_cookie_summary=slider._summarize_cookie_dict_for_debug(postflight_cookies),
        )

        write_state(status="postflight_running")
        postflight = run_coro_blocking(run_postflight(postflight_cookies))
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
        if slider is not None:
            try:
                slider._detach_managed_runtime()
            except Exception:
                pass
        try:
            closed_counts = run_coro_blocking(
                account_browser_runtime_manager.close_all_runtimes(
                    reason="face_probe_exit_cleanup",
                )
            )
            if (closed_counts or {}).get("async") or (closed_counts or {}).get("sync"):
                record_event("runtime_cleanup", **closed_counts)
        except Exception as cleanup_exc:
            record_event("runtime_cleanup_error", error=str(cleanup_exc))
        record_event("script_end")


if __name__ == "__main__":
    raise SystemExit(main())
