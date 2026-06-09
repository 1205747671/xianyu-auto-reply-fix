import asyncio
import concurrent.futures
import contextlib
import sys
import tempfile
import threading
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import cookie_manager


def _make_runtime_manager(*, async_result=True, async_side_effect=None):
    return SimpleNamespace(
        invalidate_runtime=mock.AsyncMock(
            return_value=async_result,
            side_effect=async_side_effect,
        ),
        invalidate_runtime_sync=mock.Mock(return_value=True),
    )


class _FakeSyncRuntimeContext:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSyncRuntime:
    def __init__(self, runtime_label, profile_dir):
        self.runtime_label = runtime_label
        self.profile_dir = profile_dir
        self.context = _FakeSyncRuntimeContext()
        self.close_reason = None


class _BlockingSyncRuntimeCloser:
    def __init__(self):
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def __call__(self, runtime, *, reason):
        self.close_started.set()
        self.allow_close.wait(timeout=3.0)
        runtime.close_reason = reason
        runtime.context.close()


class _FakeAsyncRuntime:
    def __init__(self, runtime_label, profile_dir):
        self.runtime_label = runtime_label
        self.profile_dir = profile_dir
        self.context = SimpleNamespace(is_closed=lambda: False)
        self.close_reason = None


class _BlockingAsyncPage:
    def __init__(self, close_started, allow_close):
        self.close_started = close_started
        self.allow_close = allow_close
        self.closed = False

    async def close(self):
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True


class _BlockingAsyncNewPageContext:
    def __init__(self, page, create_started, allow_create):
        self.page = page
        self.pages = []
        self.create_started = create_started
        self.allow_create = allow_create

    def is_closed(self):
        return False

    async def new_page(self):
        self.create_started.set()
        await self.allow_create.wait()
        self.pages.append(self.page)
        return self.page


class CookieManagerAccountRuntimeAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        load_patcher = mock.patch.object(cookie_manager.CookieManager, "_load_from_db", lambda _self: None)
        load_patcher.start()
        self.addAsyncCleanup(load_patcher.stop)
        self.manager = cookie_manager.CookieManager(asyncio.get_running_loop())
        self.runtime_manager = _make_runtime_manager()

    async def test_add_cookie_restart_invalidates_account_runtime(self):
        existing_task = asyncio.get_running_loop().create_future()
        existing_task.set_result(None)
        self.manager.tasks["acc-restart-1"] = existing_task

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie"), \
             mock.patch.object(cookie_manager.db_manager, "get_cookie_details", return_value={"user_id": 1}), \
             mock.patch.object(self.manager, "_run_xianyu", new=mock.AsyncMock(return_value=None)):
            await self.manager._add_cookie_async("acc-restart-1", "cookie=value", user_id=1)
            await asyncio.sleep(0)

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-restart-1",
            reason="task_restarted",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-restart-1",
            reason="task_restarted",
        )

    async def test_remove_cookie_invalidates_account_runtime(self):
        existing_task = asyncio.get_running_loop().create_future()
        existing_task.set_result(None)
        self.manager.tasks["acc-remove-1"] = existing_task
        self.manager.cookies["acc-remove-1"] = "cookie=value"
        self.manager.keywords["acc-remove-1"] = [("k", "v")]
        self.manager.cookie_status["acc-remove-1"] = False
        self.manager.auto_confirm_settings["acc-remove-1"] = False
        self.manager.live_instances["acc-remove-1"] = object()
        self.manager._task_locks["acc-remove-1"] = asyncio.Lock()

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "delete_cookie"):
            await self.manager._remove_cookie_async("acc-remove-1")

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-remove-1",
            reason="account_removed",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-remove-1",
            reason="account_removed",
        )
        self.assertNotIn("acc-remove-1", self.manager.cookies)
        self.assertNotIn("acc-remove-1", self.manager.keywords)
        self.assertNotIn("acc-remove-1", self.manager.cookie_status)
        self.assertNotIn("acc-remove-1", self.manager.auto_confirm_settings)
        self.assertNotIn("acc-remove-1", self.manager.live_instances)
        self.assertNotIn("acc-remove-1", self.manager._task_locks)
        self.assertNotIn("acc-remove-1", self.manager.tasks)

    async def test_invalidate_account_runtime_still_calls_sync_when_async_invalidation_fails(self):
        runtime_manager = _make_runtime_manager(async_side_effect=RuntimeError("async invalidate failed"))

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=runtime_manager):
            await self.manager._invalidate_account_runtime("acc-runtime-fallback-1", reason="task_stopped")

        runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-runtime-fallback-1",
            reason="task_stopped",
        )
        runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-runtime-fallback-1",
            reason="task_stopped",
        )

    async def test_start_runtime_task_cleans_finished_task_reference(self):
        with mock.patch.object(self.manager, "_run_xianyu", new=mock.AsyncMock(return_value=None)):
            task = self.manager.start_runtime_task("acc-finished-1", "cookie=value", user_id=1)
            await task
            await asyncio.sleep(0)

        self.assertNotIn("acc-finished-1", self.manager.tasks)

    async def test_completed_runtime_task_invalidates_account_runtime(self):
        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(self.manager, "_run_xianyu", new=mock.AsyncMock(return_value=None)):
            task = self.manager.start_runtime_task("acc-finished-runtime-1", "cookie=value", user_id=1)
            await task
            await asyncio.sleep(0)

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-finished-runtime-1",
            reason="task_exited",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-finished-runtime-1",
            reason="task_exited",
        )
        self.assertNotIn("acc-finished-runtime-1", self.manager.tasks)

    async def test_stale_done_callback_does_not_remove_new_runtime_indexes(self):
        old_task = asyncio.get_running_loop().create_future()
        new_task = asyncio.get_running_loop().create_future()
        new_live = object()

        self.manager.tasks["acc-stale-done-1"] = new_task
        self.manager.live_instances["acc-stale-done-1"] = new_live

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager):
            self.manager._handle_runtime_task_done("acc-stale-done-1", old_task)
            await asyncio.sleep(0)

        self.runtime_manager.invalidate_runtime.assert_not_awaited()
        self.runtime_manager.invalidate_runtime_sync.assert_not_called()
        self.assertIs(self.manager.tasks["acc-stale-done-1"], new_task)
        self.assertIs(self.manager.live_instances["acc-stale-done-1"], new_live)

    async def test_run_xianyu_passes_account_id_alias_to_live_constructor(self):
        fake_live = mock.Mock()
        fake_live.main = mock.AsyncMock(return_value=None)
        fake_module = types.ModuleType("XianyuAutoAsync")
        fake_module.XianyuLive = mock.Mock(return_value=fake_live)

        with mock.patch.dict(sys.modules, {"XianyuAutoAsync": fake_module}):
            await self.manager._run_xianyu("acc-live-alias-1", "cookie=value", user_id=7)

        fake_module.XianyuLive.assert_called_once_with(
            "cookie=value",
            account_id="acc-live-alias-1",
            user_id=7,
        )
        fake_live.main.assert_awaited_once_with()
        self.assertNotIn("acc-live-alias-1", self.manager.live_instances)


class CookieManagerAccountRuntimeSyncTest(unittest.TestCase):
    def setUp(self):
        load_patcher = mock.patch.object(cookie_manager.CookieManager, "_load_from_db", lambda _self: None)
        load_patcher.start()
        self.addCleanup(load_patcher.stop)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.manager = cookie_manager.CookieManager(self.loop)
        self.runtime_manager = _make_runtime_manager()

    def test_disabling_account_stops_task_and_invalidates_runtime(self):
        self.manager.cookies["acc-disable-1"] = "cookie=value"
        self.manager.cookie_status["acc-disable-1"] = True
        self.manager.live_instances["acc-disable-1"] = object()
        self.manager.tasks["acc-disable-1"] = SimpleNamespace(
            done=lambda: True,
            cancel=mock.Mock(),
        )

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie_status"):
            self.manager.update_cookie_status("acc-disable-1", False)

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-disable-1",
            reason="task_stopped",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-disable-1",
            reason="task_stopped",
        )
        self.assertNotIn("acc-disable-1", self.manager.live_instances)

    def test_update_cookie_preserves_disabled_account_without_restart(self):
        stale_task = self.loop.create_future()
        stale_task.set_result(None)
        self.manager.tasks["acc-update-1"] = stale_task
        self.manager.cookies["acc-update-1"] = "old=value"
        self.manager.keywords["acc-update-1"] = [("hello", "world")]
        self.manager.cookie_status["acc-update-1"] = False

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "get_cookie_details", return_value={"user_id": 9}), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie") as save_cookie_mock, \
             mock.patch.object(self.manager, "start_runtime_task") as start_runtime_task_mock:
            self.manager.update_cookie("acc-update-1", "new=value", save_to_db=True)

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-update-1",
            reason="task_restarted",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-update-1",
            reason="task_restarted",
        )
        save_cookie_mock.assert_called_once_with("acc-update-1", "new=value", 9)
        start_runtime_task_mock.assert_not_called()
        self.assertEqual(self.manager.cookies["acc-update-1"], "new=value")
        self.assertEqual(self.manager.keywords["acc-update-1"], [("hello", "world")])
        self.assertFalse(self.manager.cookie_status["acc-update-1"])

    def test_update_cookie_can_skip_db_write_and_still_restart_same_account(self):
        self.manager.cookies["acc-update-2"] = "old=value"
        self.manager.keywords["acc-update-2"] = [("k", "v")]
        self.manager.cookie_status["acc-update-2"] = True

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "get_cookie_details", return_value={"user_id": 5}), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie") as save_cookie_mock, \
             mock.patch.object(self.manager, "start_runtime_task") as start_runtime_task_mock:
            self.manager.update_cookie("acc-update-2", "new=value", save_to_db=False)

        save_cookie_mock.assert_not_called()
        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-update-2",
            reason="task_restarted",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-update-2",
            reason="task_restarted",
        )
        start_runtime_task_mock.assert_called_once_with("acc-update-2", "new=value", 5)
        self.assertEqual(self.manager.cookies["acc-update-2"], "new=value")
        self.assertEqual(self.manager.keywords["acc-update-2"], [("k", "v")])
        self.assertTrue(self.manager.cookie_status["acc-update-2"])

    def test_enabling_account_reuses_same_account_key(self):
        self.manager.cookies["acc-enable-1"] = "cookie=value"
        self.manager.cookie_status["acc-enable-1"] = False

        with mock.patch.object(cookie_manager.db_manager, "save_cookie_status"), \
             mock.patch.object(self.manager, "_start_cookie_task") as start_task_mock:
            self.manager.update_cookie_status("acc-enable-1", True)

        start_task_mock.assert_called_once_with("acc-enable-1")

    def test_update_cookie_status_rolls_back_memory_state_when_db_write_fails(self):
        self.manager.cookies["acc-status-rollback-1"] = "cookie=value"
        self.manager.cookie_status["acc-status-rollback-1"] = True

        with mock.patch.object(
            cookie_manager.db_manager,
            "save_cookie_status",
            side_effect=RuntimeError("save status exploded"),
        ), mock.patch.object(self.manager, "_stop_cookie_task") as stop_task_mock:
            with self.assertRaisesRegex(RuntimeError, "save status exploded"):
                self.manager.update_cookie_status("acc-status-rollback-1", False)

        stop_task_mock.assert_not_called()
        self.assertTrue(self.manager.cookie_status["acc-status-rollback-1"])

    def test_get_xianyu_instance_uses_account_key(self):
        fake_live = object()
        self.manager.live_instances["acc-live-1"] = fake_live

        self.assertIs(self.manager.get_xianyu_instance("acc-live-1"), fake_live)
        self.assertIsNone(self.manager.get_xianyu_instance("acc-missing"))

    def test_stop_cookie_task_uses_manager_loop_when_not_running(self):
        self.manager.cookies["acc-stop-1"] = "cookie=value"
        pending_task = self.loop.create_task(asyncio.sleep(60))
        self.manager.tasks["acc-stop-1"] = pending_task

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager):
            self.manager._stop_cookie_task("acc-stop-1")

        self.assertNotIn("acc-stop-1", self.manager.tasks)
        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-stop-1",
            reason="task_stopped",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-stop-1",
            reason="task_stopped",
        )

    def test_start_cookie_task_prunes_finished_task_before_restart(self):
        self.manager.cookies["acc-restart-finished"] = "cookie=value"
        stale_task = self.loop.create_task(asyncio.sleep(0))
        self.loop.run_until_complete(stale_task)
        self.manager.tasks["acc-restart-finished"] = stale_task

        dispatch_calls = []

        def _dispatch(coroutine, *, timeout=None):
            dispatch_calls.append(timeout)
            self.loop.run_until_complete(coroutine)
            return None

        with mock.patch.object(cookie_manager.db_manager, "get_cookie_details", return_value={"user_id": 7}), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie"), \
             mock.patch.object(self.manager, "_run_xianyu", new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(self.manager, "_dispatch_manager_coroutine", side_effect=_dispatch):
            self.manager._start_cookie_task("acc-restart-finished")

        self.assertEqual(dispatch_calls, [5])
        current_task = self.manager.tasks.get("acc-restart-finished")
        self.assertNotEqual(current_task, stale_task)
        if current_task is not None and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                self.loop.run_until_complete(current_task)

    def test_dispatch_manager_coroutine_timeout_cancels_scheduled_coroutine(self):
        loop_started = threading.Event()
        cancel_seen = threading.Event()
        side_effect_ran = threading.Event()

        def run_loop():
            asyncio.set_event_loop(self.loop)
            loop_started.set()
            self.loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()
        self.assertTrue(loop_started.wait(timeout=1))

        async def delayed_mutation():
            try:
                await asyncio.sleep(0.2)
                side_effect_ran.set()
            except asyncio.CancelledError:
                cancel_seen.set()
                raise

        try:
            with self.assertRaises(concurrent.futures.TimeoutError):
                self.manager._dispatch_manager_coroutine(
                    delayed_mutation(),
                    timeout=0.01,
                )

            self.assertTrue(cancel_seen.wait(timeout=1))
            self.assertFalse(side_effect_ran.is_set())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            loop_thread.join(timeout=1)

    def test_reload_from_db_reconciles_removed_new_disabled_and_changed_accounts(self):
        self.manager.cookies = {
            "acc-keep-1": "cookie-keep-old",
            "acc-remove-1": "cookie-remove-old",
            "acc-disabled-1": "cookie-disabled-old",
            "acc-changed-1": "cookie-changed-old",
        }
        self.manager.keywords = {
            "acc-keep-1": [("keep", "reply")],
            "acc-remove-1": [("remove", "reply")],
            "acc-disabled-1": [("disabled", "reply")],
            "acc-changed-1": [("changed", "reply")],
        }
        self.manager.cookie_status = {
            "acc-keep-1": True,
            "acc-remove-1": True,
            "acc-disabled-1": True,
            "acc-changed-1": True,
        }
        self.manager.auto_confirm_settings = {
            "acc-keep-1": True,
            "acc-remove-1": True,
            "acc-disabled-1": True,
            "acc-changed-1": True,
        }
        self.manager.tasks = {
            "acc-keep-1": object(),
            "acc-remove-1": object(),
            "acc-disabled-1": object(),
            "acc-changed-1": object(),
        }
        self.manager.live_instances = {
            "acc-remove-1": object(),
            "acc-disabled-1": object(),
            "acc-changed-1": object(),
        }
        self.manager._task_locks = {
            "acc-remove-1": asyncio.Lock(),
            "acc-disabled-1": asyncio.Lock(),
            "acc-changed-1": asyncio.Lock(),
        }

        def _reload_snapshot():
            self.manager.cookies = {
                "acc-keep-1": "cookie-keep-old",
                "acc-disabled-1": "cookie-disabled-old",
                "acc-changed-1": "cookie-changed-new",
                "acc-new-1": "cookie-new-1",
            }
            self.manager.keywords = {
                "acc-keep-1": [("keep", "reply")],
                "acc-disabled-1": [("disabled", "reply")],
                "acc-changed-1": [("changed", "reply")],
                "acc-new-1": [("new", "reply")],
            }
            self.manager.cookie_status = {
                "acc-keep-1": True,
                "acc-disabled-1": False,
                "acc-changed-1": True,
                "acc-new-1": True,
            }
            self.manager.auto_confirm_settings = {
                "acc-keep-1": True,
                "acc-disabled-1": False,
                "acc-changed-1": True,
                "acc-new-1": True,
            }

        with mock.patch.object(self.manager, "_load_from_db", side_effect=_reload_snapshot), \
             mock.patch.object(self.manager, "_stop_cookie_task") as stop_task_mock, \
             mock.patch.object(self.manager, "_start_cookie_task") as start_task_mock, \
             mock.patch.object(self.manager, "update_cookie") as update_cookie_mock:
            self.manager.reload_from_db()

        stop_task_mock.assert_has_calls(
            [
                mock.call("acc-remove-1"),
                mock.call("acc-disabled-1"),
            ],
            any_order=True,
        )
        start_task_mock.assert_called_once_with("acc-new-1")
        update_cookie_mock.assert_called_once_with(
            "acc-changed-1",
            "cookie-changed-new",
            save_to_db=False,
        )

    def test_reload_from_db_prunes_removed_account_runtime_indexes(self):
        self.manager.cookies = {"acc-remove-2": "cookie-remove-old"}
        self.manager.keywords = {"acc-remove-2": [("remove", "reply")]}
        self.manager.cookie_status = {"acc-remove-2": True}
        self.manager.auto_confirm_settings = {"acc-remove-2": True}
        self.manager.live_instances = {"acc-remove-2": object()}
        self.manager._task_locks = {"acc-remove-2": asyncio.Lock()}

        def _reload_snapshot():
            self.manager.cookies = {}
            self.manager.keywords = {}
            self.manager.cookie_status = {}
            self.manager.auto_confirm_settings = {}

        with mock.patch.object(self.manager, "_load_from_db", side_effect=_reload_snapshot), \
             mock.patch.object(self.manager, "_stop_cookie_task") as stop_task_mock:
            self.manager.reload_from_db()

        stop_task_mock.assert_called_once_with("acc-remove-2")
        self.assertNotIn("acc-remove-2", self.manager.live_instances)
        self.assertNotIn("acc-remove-2", self.manager._task_locks)

    def test_reload_from_db_invalidates_runtime_before_restart_when_cookie_changed_without_active_task(self):
        self.manager.cookies = {"acc-cookie-change-1": "cookie-old"}
        self.manager.keywords = {"acc-cookie-change-1": [("k", "v")]}
        self.manager.cookie_status = {"acc-cookie-change-1": True}
        self.manager.auto_confirm_settings = {"acc-cookie-change-1": True}

        def _reload_snapshot():
            self.manager.cookies = {"acc-cookie-change-1": "cookie-new"}
            self.manager.keywords = {"acc-cookie-change-1": [("k", "v")]}
            self.manager.cookie_status = {"acc-cookie-change-1": True}
            self.manager.auto_confirm_settings = {"acc-cookie-change-1": True}

        with mock.patch.object(self.manager, "_load_from_db", side_effect=_reload_snapshot), \
             mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(self.manager, "_start_cookie_task") as start_task_mock:
            self.manager.reload_from_db()

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-cookie-change-1",
            reason="task_restarted",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-cookie-change-1",
            reason="task_restarted",
        )
        start_task_mock.assert_called_once_with("acc-cookie-change-1")

    def test_reload_from_db_invalidates_stale_live_instance_before_restart_when_task_missing(self):
        self.manager.cookies = {"acc-stale-live-1": "cookie-same"}
        self.manager.keywords = {"acc-stale-live-1": [("k", "v")]}
        self.manager.cookie_status = {"acc-stale-live-1": True}
        self.manager.auto_confirm_settings = {"acc-stale-live-1": True}
        self.manager.live_instances = {"acc-stale-live-1": object()}

        def _reload_snapshot():
            self.manager.cookies = {"acc-stale-live-1": "cookie-same"}
            self.manager.keywords = {"acc-stale-live-1": [("k", "v")]}
            self.manager.cookie_status = {"acc-stale-live-1": True}
            self.manager.auto_confirm_settings = {"acc-stale-live-1": True}

        with mock.patch.object(self.manager, "_load_from_db", side_effect=_reload_snapshot), \
             mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(self.manager, "_start_cookie_task") as start_task_mock:
            self.manager.reload_from_db()

        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-stale-live-1",
            reason="task_restarted",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-stale-live-1",
            reason="task_restarted",
        )
        start_task_mock.assert_called_once_with("acc-stale-live-1")

    def test_reload_from_db_stops_disabled_account_when_current_runtime_still_exists(self):
        self.manager.cookies = {"acc-disabled-runtime-1": "cookie-same"}
        self.manager.keywords = {"acc-disabled-runtime-1": [("k", "v")]}
        self.manager.cookie_status = {"acc-disabled-runtime-1": True}
        self.manager.auto_confirm_settings = {"acc-disabled-runtime-1": True}

        def _reload_snapshot():
            self.manager.cookies = {"acc-disabled-runtime-1": "cookie-same"}
            self.manager.keywords = {"acc-disabled-runtime-1": [("k", "v")]}
            self.manager.cookie_status = {"acc-disabled-runtime-1": False}
            self.manager.auto_confirm_settings = {"acc-disabled-runtime-1": False}
            self.manager.live_instances["acc-disabled-runtime-1"] = object()

        with mock.patch.object(self.manager, "_load_from_db", side_effect=_reload_snapshot), \
             mock.patch.object(self.manager, "_stop_cookie_task") as stop_task_mock:
            self.manager.reload_from_db()

        stop_task_mock.assert_called_once_with("acc-disabled-runtime-1")


class CookieManagerLoadFromDbTest(unittest.TestCase):
    def test_load_from_db_ignores_blank_cookie_accounts_for_runtime_tracking(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        with mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookies",
            return_value={
                "acc-empty-1": "",
                "acc-space-1": "   ",
                "acc-live-1": " cookie=value; foo=bar ",
            },
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_keywords",
            return_value={},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookie_status",
            return_value={"acc-empty-1": True, "acc-live-1": False},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_auto_confirm",
            return_value=False,
        ):
            manager = cookie_manager.CookieManager(loop)

        self.assertEqual(
            {"acc-live-1": "cookie=value; foo=bar"},
            manager.cookies,
        )
        self.assertEqual({"acc-live-1": False}, manager.cookie_status)
        self.assertEqual({"acc-live-1": False}, manager.auto_confirm_settings)

    def test_load_from_db_stops_masking_keyword_and_status_load_failures(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        for failure_target, failure_message in (
            ("get_all_keywords", "keywords exploded"),
            ("get_all_cookie_status", "status exploded"),
        ):
            with self.subTest(failure_target=failure_target):
                get_all_keywords_patch = mock.patch.object(
                    cookie_manager.db_manager,
                    "get_all_keywords",
                    side_effect=RuntimeError(failure_message),
                ) if failure_target == "get_all_keywords" else mock.patch.object(
                    cookie_manager.db_manager,
                    "get_all_keywords",
                    return_value={"acc-live-1": [("你好", "您好")]},
                )
                get_all_cookie_status_patch = mock.patch.object(
                    cookie_manager.db_manager,
                    "get_all_cookie_status",
                    side_effect=RuntimeError(failure_message),
                ) if failure_target == "get_all_cookie_status" else mock.patch.object(
                    cookie_manager.db_manager,
                    "get_all_cookie_status",
                    return_value={"acc-live-1": True},
                )

                with mock.patch.object(
                    cookie_manager.db_manager,
                    "get_all_cookies",
                    return_value={"acc-live-1": " cookie=value; foo=bar "},
                ), get_all_keywords_patch, get_all_cookie_status_patch, mock.patch.object(
                    cookie_manager.db_manager,
                    "get_auto_confirm",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, failure_message):
                        cookie_manager.CookieManager(loop)

    def test_reload_from_db_keeps_existing_cache_when_status_reload_fails(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        with mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookies",
            return_value={"acc-old-1": "cookie-old"},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_keywords",
            return_value={"acc-old-1": [("你好", "您好")]},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookie_status",
            return_value={"acc-old-1": False},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_auto_confirm",
            return_value=False,
        ):
            manager = cookie_manager.CookieManager(loop)

        with mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookies",
            return_value={"acc-new-1": "cookie-new"},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_keywords",
            return_value={"acc-new-1": [("新的", "回复")]},
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_all_cookie_status",
            side_effect=RuntimeError("status reload exploded"),
        ), mock.patch.object(
            cookie_manager.db_manager,
            "get_auto_confirm",
            side_effect=AssertionError("status reload failed after cache assignment"),
        ):
            with self.assertRaisesRegex(RuntimeError, "status reload exploded"):
                manager.reload_from_db()

        self.assertEqual({"acc-old-1": "cookie-old"}, manager.cookies)
        self.assertEqual({"acc-old-1": [("你好", "您好")]}, manager.keywords)
        self.assertEqual({"acc-old-1": False}, manager.cookie_status)
        self.assertEqual({"acc-old-1": False}, manager.auto_confirm_settings)


class CookieManagerAccountRuntimeSameLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        load_patcher = mock.patch.object(cookie_manager.CookieManager, "_load_from_db", lambda _self: None)
        load_patcher.start()
        self.addAsyncCleanup(load_patcher.stop)
        self.manager = cookie_manager.CookieManager(asyncio.get_running_loop())
        self.runtime_manager = _make_runtime_manager()

    async def test_stop_cookie_task_inside_manager_loop_does_not_cross_dispatch(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def long_running():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                finished.set()

        task = asyncio.create_task(long_running())
        self.manager.tasks["acc-stop-loop"] = task
        await started.wait()

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager):
            stop_result = self.manager._stop_cookie_task("acc-stop-loop")
            self.assertIsInstance(stop_result, asyncio.Task)
            await stop_result

        await asyncio.wait_for(finished.wait(), timeout=1)
        self.assertNotIn("acc-stop-loop", self.manager.tasks)
        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-stop-loop",
            reason="task_stopped",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-stop-loop",
            reason="task_stopped",
        )

    async def test_stop_cookie_task_for_managed_runtime_task_invalidates_only_task_stopped(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def managed_run(account_id, cookie_value, user_id=None):
            _ = account_id, cookie_value, user_id
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                finished.set()

        with mock.patch.object(self.manager, "_run_xianyu", new=managed_run), \
             mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager):
            task = self.manager.start_runtime_task("acc-stop-reason-1", "cookie=value", user_id=1)
            await started.wait()
            stop_result = self.manager._stop_cookie_task("acc-stop-reason-1")
            self.assertIsInstance(stop_result, asyncio.Task)
            await stop_result
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)

        self.assertTrue(task.done())
        self.assertEqual(
            self.runtime_manager.invalidate_runtime.await_args_list,
            [mock.call("acc-stop-reason-1", reason="task_stopped")],
        )
        self.assertEqual(
            self.runtime_manager.invalidate_runtime_sync.call_args_list,
            [mock.call("acc-stop-reason-1", reason="task_stopped")],
        )

    async def test_add_cookie_restart_for_managed_runtime_task_invalidates_only_task_restarted(self):
        run_started = asyncio.Queue()
        release_events = {}

        async def managed_run(account_id, cookie_value, user_id=None):
            _ = account_id, user_id
            release_event = asyncio.Event()
            release_events[cookie_value] = release_event
            await run_started.put(cookie_value)
            await release_event.wait()

        with mock.patch.object(self.manager, "_run_xianyu", new=managed_run), \
             mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager), \
             mock.patch.object(cookie_manager.db_manager, "save_cookie"), \
             mock.patch.object(cookie_manager.db_manager, "get_cookie_details", return_value={"user_id": 1}):
            self.manager.start_runtime_task("acc-restart-reason-1", "cookie=old", user_id=1)
            self.assertEqual(await asyncio.wait_for(run_started.get(), timeout=1), "cookie=old")

            await self.manager._add_cookie_async("acc-restart-reason-1", "cookie=new", user_id=1)
            self.assertEqual(await asyncio.wait_for(run_started.get(), timeout=1), "cookie=new")
            await asyncio.sleep(0)

            self.assertEqual(
                self.runtime_manager.invalidate_runtime.await_args_list,
                [mock.call("acc-restart-reason-1", reason="task_restarted")],
            )
            self.assertEqual(
                self.runtime_manager.invalidate_runtime_sync.call_args_list,
                [mock.call("acc-restart-reason-1", reason="task_restarted")],
            )

            release_events["cookie=new"].set()
            new_task = self.manager.tasks["acc-restart-reason-1"]
            await asyncio.wait_for(new_task, timeout=1)

    async def test_stop_cookie_task_timeout_still_invalidates_runtime_and_cleans_indexes(self):
        started = asyncio.Event()
        cancel_started = asyncio.Event()
        allow_exit = asyncio.Event()

        async def stubborn_task():
            try:
                started.set()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancel_started.set()
                while not allow_exit.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
                raise

        task = asyncio.create_task(stubborn_task())
        self.manager.tasks["acc-stop-timeout"] = task
        self.manager.live_instances["acc-stop-timeout"] = object()
        self.manager._task_stop_timeout_seconds = 0.01
        await started.wait()

        with mock.patch.object(self.manager, "_get_account_browser_runtime_manager", return_value=self.runtime_manager):
            stop_result = self.manager._stop_cookie_task("acc-stop-timeout")
            self.assertIsInstance(stop_result, asyncio.Task)
            try:
                await asyncio.wait_for(stop_result, timeout=0.1)
            finally:
                allow_exit.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        await cancel_started.wait()
        self.assertNotIn("acc-stop-timeout", self.manager.tasks)
        self.assertNotIn("acc-stop-timeout", self.manager.live_instances)
        self.runtime_manager.invalidate_runtime.assert_awaited_once_with(
            "acc-stop-timeout",
            reason="task_stopped",
        )
        self.runtime_manager.invalidate_runtime_sync.assert_called_once_with(
            "acc-stop-timeout",
            reason="task_stopped",
        )


class AccountBrowserRuntimeManagerSyncThreadClosureTest(unittest.TestCase):
    def _make_manager(self, temp_dir, *, created_runtimes, closer_thread_ids):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeSyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append((generation, runtime))
            return runtime

        def runtime_closer(runtime, *, reason):
            closer_thread_ids.append(threading.get_ident())
            runtime.close_reason = reason
            runtime.context.close()

        return AccountBrowserRuntimeManager(
            base_dir=temp_dir,
            sync_runtime_factory=runtime_factory,
            sync_runtime_closer=runtime_closer,
        )

    def test_sync_rebuild_closes_worker_owned_runtime_on_owner_thread_before_recreate(self):
        created_runtimes = []
        closer_thread_ids = []
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._make_manager(
                temp_dir,
                created_runtimes=created_runtimes,
                closer_thread_ids=closer_thread_ids,
            )

            def create_runtime_on_account_worker():
                lease = manager.acquire_runtime_sync("account-42", "password_login", exclusive=True)
                runtime = lease.runtime
                owner_thread_id = threading.get_ident()
                manager.release_runtime_sync(lease)
                return owner_thread_id, runtime

            owner_thread_id, first_runtime = manager.run_sync_task_on_account_thread(
                "account-42",
                create_runtime_on_account_worker,
                timeout=3.0,
            )

            second_lease = manager.acquire_runtime_sync("account-42", "manual_cookie_import", exclusive=True)
            snapshot = manager.get_account_runtime_state_snapshot("account-42")

            self.assertEqual(len(created_runtimes), 2)
            self.assertEqual(second_lease.generation, 1)
            self.assertIsNot(first_runtime, second_lease.runtime)
            self.assertEqual(first_runtime.close_reason, "thread_changed")
            self.assertEqual(closer_thread_ids, [owner_thread_id])
            self.assertEqual(snapshot["sync_pending_closures"], 0)

            manager.release_runtime_sync(second_lease)

    def test_close_all_sync_closes_worker_owned_runtime_on_owner_thread(self):
        created_runtimes = []
        closer_thread_ids = []
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._make_manager(
                temp_dir,
                created_runtimes=created_runtimes,
                closer_thread_ids=closer_thread_ids,
            )

            def create_runtime_on_account_worker():
                lease = manager.acquire_runtime_sync("account-42", "manual_cookie_import", exclusive=False)
                runtime = lease.runtime
                owner_thread_id = threading.get_ident()
                manager.release_runtime_sync(lease)
                return owner_thread_id, runtime

            owner_thread_id, worker_runtime = manager.run_sync_task_on_account_thread(
                "account-42",
                create_runtime_on_account_worker,
                timeout=3.0,
            )

            closed_count = manager.close_all_runtimes_sync(reason="shutdown_cleanup")

            self.assertEqual(closed_count, 1)
            self.assertEqual(worker_runtime.close_reason, "shutdown_cleanup")
            self.assertEqual(closer_thread_ids, [owner_thread_id])

    def test_close_all_sync_stops_idle_account_worker_after_runtime_close(self):
        created_runtimes = []
        closer_thread_ids = []
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._make_manager(
                temp_dir,
                created_runtimes=created_runtimes,
                closer_thread_ids=closer_thread_ids,
            )

            def create_runtime_on_account_worker():
                lease = manager.acquire_runtime_sync("account-42", "manual_cookie_import", exclusive=False)
                manager.release_runtime_sync(lease)
                return threading.get_ident()

            owner_thread_id = manager.run_sync_task_on_account_thread(
                "account-42",
                create_runtime_on_account_worker,
                timeout=3.0,
            )
            worker_state = manager._get_sync_account_worker_state("account-42")
            with worker_state.lock:
                worker_thread = worker_state.thread

            closed_count = manager.close_all_runtimes_sync(reason="shutdown_cleanup")

            self.assertEqual(closed_count, 1)
            self.assertEqual(closer_thread_ids, [owner_thread_id])
            self.assertIsNotNone(worker_thread)
            self.assertFalse(worker_thread.is_alive())
            self.assertNotIn("account-42", manager._sync_account_workers)

    def test_close_all_sync_keeps_profile_claim_until_runtime_close_finishes(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []

        def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeSyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        blocking_closer = _BlockingSyncRuntimeCloser()
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                sync_runtime_factory=runtime_factory,
                sync_runtime_closer=blocking_closer,
            )

            lease = manager.acquire_runtime_sync("account-42", "manual_cookie_import", exclusive=True)
            runtime = lease.runtime
            manager.release_runtime_sync(lease)

            result = {}

            def run_close_all():
                result["closed_count"] = manager.close_all_runtimes_sync(reason="shutdown_cleanup")

            close_thread = threading.Thread(target=run_close_all)
            close_thread.start()
            self.assertTrue(blocking_closer.close_started.wait(timeout=1.0))

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["sync_claimed_profile_dir"])
            self.assertTrue(close_thread.is_alive())

            blocking_closer.allow_close.set()
            close_thread.join(timeout=3.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(result["closed_count"], 1)
            self.assertEqual(runtime.close_reason, "shutdown_cleanup")
            self.assertFalse(snapshot["sync_runtime_exists"])
            self.assertIsNone(snapshot["sync_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    def test_idle_cleanup_closes_worker_owned_runtime_on_owner_thread_before_stopping_worker(self):
        created_runtimes = []
        closer_thread_ids = []
        now = 1000.0

        def time_fn():
            return now

        with tempfile.TemporaryDirectory() as temp_dir:
            from utils.account_browser_runtime import AccountBrowserRuntimeManager

            def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
                runtime = _FakeSyncRuntime(len(created_runtimes) + 1, profile_dir)
                created_runtimes.append((generation, runtime))
                return runtime

            def runtime_closer(runtime, *, reason):
                closer_thread_ids.append(threading.get_ident())
                runtime.close_reason = reason
                runtime.context.close()

            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                sync_runtime_factory=runtime_factory,
                sync_runtime_closer=runtime_closer,
                time_fn=time_fn,
                idle_timeout_seconds=30.0,
            )

            def create_runtime_on_account_worker():
                lease = manager.acquire_runtime_sync("account-42", "manual_cookie_import", exclusive=False)
                runtime = lease.runtime
                owner_thread_id = threading.get_ident()
                manager.release_runtime_sync(lease)
                return owner_thread_id, runtime

            owner_thread_id, worker_runtime = manager.run_sync_task_on_account_thread(
                "account-42",
                create_runtime_on_account_worker,
                timeout=3.0,
            )

            now += 31.0
            cleanup_count = manager.cleanup_idle_runtimes_sync()
            snapshot = manager.get_account_runtime_state_snapshot("account-42")

            self.assertGreaterEqual(cleanup_count, 1)
            self.assertEqual(worker_runtime.close_reason, "idle_timeout")
            self.assertEqual(closer_thread_ids, [owner_thread_id])
            self.assertFalse(snapshot["sync_runtime_exists"])
            self.assertEqual(snapshot["sync_pending_closures"], 0)
            self.assertNotIn("account-42", manager._sync_account_workers)

    def test_cross_thread_release_closes_pending_worker_owned_runtime_on_owner_thread(self):
        created_runtimes = []
        closer_thread_ids = []
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._make_manager(
                temp_dir,
                created_runtimes=created_runtimes,
                closer_thread_ids=closer_thread_ids,
            )

            def acquire_runtime_on_account_worker():
                lease = manager.acquire_runtime_sync("account-42", "password_login", exclusive=True)
                return threading.get_ident(), lease, lease.runtime

            owner_thread_id, lease, runtime = manager.run_sync_task_on_account_thread(
                "account-42",
                acquire_runtime_on_account_worker,
                timeout=3.0,
            )

            self.assertTrue(manager.invalidate_runtime_sync("account-42", reason="release_race"))
            manager.release_runtime_sync(lease, reason="main_thread_release")
            snapshot = manager.get_account_runtime_state_snapshot("account-42")

            self.assertEqual(runtime.close_reason, "release_race")
            self.assertEqual(closer_thread_ids, [owner_thread_id])
            self.assertFalse(snapshot["sync_runtime_exists"])
            self.assertEqual(snapshot["sync_pending_closures"], 0)
            self.assertIsNone(snapshot["sync_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)


class AccountBrowserRuntimeManagerAsyncReleaseCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_async_runtime_closer_continues_after_page_close_cancelled(self):
        from utils.account_browser_runtime import _default_async_runtime_closer

        close_calls = []

        class CancelledPage:
            async def close(self):
                close_calls.append("page.close")
                raise asyncio.CancelledError()

        class ClosableContext:
            async def close(self):
                close_calls.append("context.close")

        class ClosableBrowser:
            async def close(self):
                close_calls.append("browser.close")

        runtime = types.SimpleNamespace(
            page=CancelledPage(),
            context=ClosableContext(),
            browser=ClosableBrowser(),
            playwright=None,
        )

        returned_runtime = await _default_async_runtime_closer(
            runtime,
            reason="unit-test-page-close-cancelled",
        )

        self.assertIs(returned_runtime, runtime)
        self.assertEqual(
            close_calls,
            ["page.close", "context.close", "browser.close"],
        )

    async def test_cancelled_async_release_waits_for_pending_runtime_close_before_releasing_claim(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            runtime.close_reason = reason

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=True)
            runtime = lease.runtime
            self.assertTrue(await manager.invalidate_runtime("account-42", reason="client_cancelled"))

            release_task = asyncio.create_task(
                manager.release_runtime(lease, reason="release_after_cancel")
            )
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            release_task.cancel()
            await asyncio.sleep(0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["async_claimed_profile_dir"])
            self.assertFalse(release_task.done())

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(release_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertEqual(runtime.close_reason, "client_cancelled")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertEqual(snapshot["async_pending_closures"], 0)
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_async_release_preserves_cancellation_when_runtime_close_fails(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            raise RuntimeError("close exploded after cancellation")

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=True)
            self.assertTrue(await manager.invalidate_runtime("account-42", reason="client_cancelled"))

            release_task = asyncio.create_task(
                manager.release_runtime(lease, reason="release_after_cancel")
            )
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            release_task.cancel()
            await asyncio.sleep(0)

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(release_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertEqual(snapshot["async_pending_closures"], 0)
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_async_release_waits_for_page_close_before_releasing_owner_mode(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=False)
            page = _BlockingAsyncPage(close_started, allow_close)
            lease.pages.append(page)

            release_task = asyncio.create_task(manager.release_runtime(lease))
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            release_task.cancel()
            await asyncio.sleep(0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertFalse(page.closed)
            self.assertFalse(release_task.done())
            self.assertEqual(snapshot_during_close["owner_mode_active_count"], 1)

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(release_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertTrue(page.closed)
            self.assertEqual(snapshot["async_active_leases"], 0)
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_get_fresh_page_tracks_created_page_before_reraising(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        create_started = asyncio.Event()
        allow_create = asyncio.Event()
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        page = _BlockingAsyncPage(close_started, allow_close)

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            runtime.context = _BlockingAsyncNewPageContext(
                page,
                create_started,
                allow_create,
            )
            created_runtimes.append(runtime)
            return runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=False)
            page_task = asyncio.create_task(manager.get_fresh_page(lease))
            await asyncio.wait_for(create_started.wait(), timeout=5.0)
            page_task.cancel()
            await asyncio.sleep(0)

            self.assertFalse(page_task.done())
            self.assertEqual(lease.pages, [])

            allow_create.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(page_task, timeout=5.0)

            self.assertEqual(lease.pages, [page])
            self.assertIs(lease.runtime.page, page)

            release_task = asyncio.create_task(manager.release_runtime(lease))
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            allow_close.set()
            await asyncio.wait_for(release_task, timeout=5.0)
            self.assertTrue(page.closed)

    async def test_cancelled_async_invalidate_waits_for_runtime_close_before_releasing_claim(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            runtime.close_reason = reason

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=True)
            runtime = lease.runtime
            await manager.release_runtime(lease)

            invalidate_task = asyncio.create_task(
                manager.invalidate_runtime("account-42", reason="manual_invalidate")
            )
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            invalidate_task.cancel()
            await asyncio.sleep(0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["async_claimed_profile_dir"])
            self.assertFalse(invalidate_task.done())

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(invalidate_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertEqual(runtime.close_reason, "manual_invalidate")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertEqual(snapshot["async_pending_closures"], 0)
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_async_idle_cleanup_waits_for_runtime_close_before_releasing_claim(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        now = 1000.0

        def time_fn():
            return now

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            runtime.close_reason = reason

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
                time_fn=time_fn,
                idle_timeout_seconds=30.0,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=True)
            runtime = lease.runtime
            await manager.release_runtime(lease)

            now += 31.0
            cleanup_task = asyncio.create_task(manager.cleanup_idle_runtimes())
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            cleanup_task.cancel()
            await asyncio.sleep(0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["async_claimed_profile_dir"])
            self.assertFalse(cleanup_task.done())

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(cleanup_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertEqual(runtime.close_reason, "idle_timeout")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertEqual(snapshot["async_pending_closures"], 0)
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_close_all_async_keeps_profile_claim_until_runtime_close_finishes(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            runtime.close_reason = reason

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            lease = await manager.acquire_runtime("account-42", "item_search", exclusive=True)
            runtime = lease.runtime
            await manager.release_runtime(lease)

            close_all_task = asyncio.create_task(
                manager.close_all_runtimes(reason="shutdown_cleanup")
            )
            await asyncio.wait_for(close_started.wait(), timeout=5.0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["async_claimed_profile_dir"])
            self.assertFalse(close_all_task.done())

            allow_close.set()
            closed_counts = await asyncio.wait_for(close_all_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertEqual(closed_counts["async"], 1)
            self.assertEqual(runtime.close_reason, "shutdown_cleanup")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_async_stale_rebuild_waits_for_old_runtime_close_and_releases_claim(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive, *, runtime_request=None):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            await allow_close.wait()
            runtime.close_reason = reason

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            first_lease = await manager.acquire_runtime(
                "account-42",
                "item_search",
                exclusive=True,
                runtime_request={
                    "profile_dir": manager.resolve_profile_dir("account-42"),
                    "use_persistent_context": True,
                    "launch_options": {"headless": True},
                },
            )
            old_runtime = first_lease.runtime
            await manager.release_runtime(first_lease)

            acquire_task = asyncio.create_task(
                manager.acquire_runtime(
                    "account-42",
                    "qr_login_verification",
                    exclusive=True,
                    runtime_request={
                        "profile_dir": manager.resolve_profile_dir("account-42"),
                        "use_persistent_context": True,
                        "launch_options": {"headless": False},
                    },
                )
            )
            await asyncio.wait_for(close_started.wait(), timeout=5.0)
            acquire_task.cancel()
            await asyncio.sleep(0)

            snapshot_during_close = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_close["async_claimed_profile_dir"])
            self.assertFalse(acquire_task.done())

            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(acquire_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertEqual(old_runtime.close_reason, "stale_runtime")
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_async_stale_rebuild_releases_claim_when_old_runtime_close_is_cancelled(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        created_runtimes = []
        close_started = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive, *, runtime_request=None):
            runtime = _FakeAsyncRuntime(len(created_runtimes) + 1, profile_dir)
            created_runtimes.append(runtime)
            return runtime

        async def runtime_closer(runtime, *, reason):
            close_started.set()
            raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
                runtime_closer=runtime_closer,
            )

            first_lease = await manager.acquire_runtime(
                "account-42",
                "item_search",
                exclusive=True,
                runtime_request={
                    "profile_dir": manager.resolve_profile_dir("account-42"),
                    "use_persistent_context": True,
                    "launch_options": {"headless": True},
                },
            )
            await manager.release_runtime(first_lease)

            with self.assertRaises(asyncio.CancelledError):
                await manager.acquire_runtime(
                    "account-42",
                    "qr_login_verification",
                    exclusive=True,
                    runtime_request={
                        "profile_dir": manager.resolve_profile_dir("account-42"),
                        "use_persistent_context": True,
                        "launch_options": {"headless": False},
                    },
                )

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertTrue(close_started.is_set())
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)

    async def test_cancelled_async_factory_creation_releases_claim_and_owner_mode(self):
        from utils.account_browser_runtime import AccountBrowserRuntimeManager

        factory_started = asyncio.Event()
        allow_factory_exit = asyncio.Event()
        factory_cancelled = asyncio.Event()

        async def runtime_factory(account_id, profile_dir, generation, purpose, exclusive):
            factory_started.set()
            try:
                await allow_factory_exit.wait()
            except asyncio.CancelledError:
                factory_cancelled.set()
                raise
            return _FakeAsyncRuntime(1, profile_dir)

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = AccountBrowserRuntimeManager(
                base_dir=temp_dir,
                runtime_factory=runtime_factory,
            )

            acquire_task = asyncio.create_task(
                manager.acquire_runtime("account-42", "item_search", exclusive=True)
            )
            await asyncio.wait_for(factory_started.wait(), timeout=5.0)

            snapshot_during_factory = manager.get_account_runtime_state_snapshot("account-42")
            self.assertIsNotNone(snapshot_during_factory["async_claimed_profile_dir"])
            self.assertEqual(snapshot_during_factory["owner_mode_active_count"], 1)

            acquire_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(acquire_task, timeout=5.0)

            snapshot = manager.get_account_runtime_state_snapshot("account-42")
            self.assertTrue(factory_cancelled.is_set())
            self.assertFalse(snapshot["async_runtime_exists"])
            self.assertIsNone(snapshot["async_claimed_profile_dir"])
            self.assertEqual(snapshot["owner_mode_active_count"], 0)


if __name__ == "__main__":
    unittest.main()
