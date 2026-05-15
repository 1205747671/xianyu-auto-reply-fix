import asyncio
import contextlib
import sys
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


if __name__ == "__main__":
    unittest.main()
