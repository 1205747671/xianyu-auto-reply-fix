import asyncio
import unittest
from unittest import mock

import XianyuAutoAsync
from XianyuAutoAsync import XianyuLive


class XianyuAsyncBrowserRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_launch_browser_safe_delegates_to_provider_helper(self):
        sentinel_browser = object()

        with mock.patch.object(XianyuAutoAsync, "_is_docker_env", return_value=False), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "launch_browser_async",
                 new=mock.AsyncMock(return_value=sentinel_browser),
             ) as launch_browser_async:
            browser = await XianyuAutoAsync._launch_browser_safe(
                "runtime-test",
                headless=True,
                args=["--flag"],
            )

        self.assertIs(browser, sentinel_browser)
        launch_browser_async.assert_awaited_once_with(
            headless=True,
            args=["--flag"],
        )

    async def test_launch_browser_safe_keeps_docker_event_loop_policy_compatibility(self):
        sentinel_browser = object()
        original_policy = asyncio.get_event_loop_policy()
        observed_policy_names = []

        async def fake_launch_browser_async(**_kwargs):
            observed_policy_names.append(type(asyncio.get_event_loop_policy()).__name__)
            return sentinel_browser

        with mock.patch.object(XianyuAutoAsync, "_is_docker_env", return_value=True), \
             mock.patch.object(
                 XianyuAutoAsync,
                 "launch_browser_async",
                 side_effect=fake_launch_browser_async,
             ) as launch_browser_async:
            browser = await XianyuAutoAsync._launch_browser_safe("docker-runtime-test")

        self.assertIs(browser, sentinel_browser)
        self.assertEqual(observed_policy_names, ["_DockerEventLoopPolicy"])
        self.assertIs(asyncio.get_event_loop_policy(), original_policy)
        launch_browser_async.assert_awaited_once_with()

    async def test_async_close_browser_closes_context_before_browser(self):
        close_order = []

        async def close_context():
            close_order.append("context.close")

        async def close_browser():
            close_order.append("browser.close")

        browser = mock.Mock()
        browser.close = mock.AsyncMock(side_effect=close_browser)

        context = mock.Mock()
        context.close = mock.AsyncMock(side_effect=close_context)

        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "runtime-close-test"

        await live._async_close_browser(
            browser=browser,
            context=context,
        )

        self.assertEqual(close_order, ["context.close", "browser.close"])
        context.close.assert_awaited_once_with()
        browser.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
