import typing
import unittest
from unittest import mock

import utils.captcha_remote_control as captcha_remote_control
import utils.item_search as item_search
import utils.order_detail_fetcher as order_detail_fetcher
import utils.qr_login as qr_login


class BrowserSidecarsProviderMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_item_search_init_browser_uses_provider_persistent_context_launcher(self):
        fake_page = mock.Mock()
        fake_context = mock.Mock()
        fake_context.browser = object()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)

        searcher = item_search.XianyuSearcher()

        with mock.patch.object(item_search, "PLAYWRIGHT_AVAILABLE", True), \
             mock.patch.object(
                 item_search,
                 "launch_browser_persistent_context_async",
                 new=mock.AsyncMock(return_value=fake_context),
                 create=True,
             ) as launch_browser_persistent_context_async:
            await searcher.init_browser()

        self.assertIs(searcher.context, fake_context)
        self.assertIs(searcher.browser, fake_context.browser)
        self.assertIs(searcher.page, fake_page)
        launch_browser_persistent_context_async.assert_awaited_once()

    async def test_order_detail_fetcher_init_browser_uses_provider_launcher(self):
        fake_page = mock.Mock()
        fake_browser = mock.Mock()
        fake_browser.new_context = mock.AsyncMock()
        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.set_extra_http_headers = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)
        fake_browser.new_context.return_value = fake_context

        fetcher = order_detail_fetcher.OrderDetailFetcher(cookie_string="a=b", headless=True)

        with mock.patch.object(
            order_detail_fetcher,
            "launch_browser_async",
            new=mock.AsyncMock(return_value=fake_browser),
            create=True,
        ) as launch_browser_async:
            result = await fetcher.init_browser()

        self.assertTrue(result)
        self.assertIs(fetcher.browser, fake_browser)
        self.assertIs(fetcher.context, fake_context)
        self.assertIs(fetcher.page, fake_page)
        launch_browser_async.assert_awaited_once()

    async def test_qr_login_verification_page_uses_provider_launcher(self):
        manager = qr_login.QRLoginManager()
        session = qr_login.QRLoginSession("session-1")
        session.verification_url = "https://passport.goofish.com/iv/test"
        session.status = "success"
        manager.sessions[session.session_id] = session

        fake_page = mock.Mock()
        fake_page.goto = mock.AsyncMock()
        fake_page.wait_for_timeout = mock.AsyncMock()
        fake_page.screenshot = mock.AsyncMock(return_value=b"image-bytes")
        fake_page.close = mock.AsyncMock()
        fake_page.url = "https://www.goofish.com/im"

        fake_context = mock.Mock()
        fake_context.add_cookies = mock.AsyncMock()
        fake_context.new_page = mock.AsyncMock(return_value=fake_page)
        fake_context.close = mock.AsyncMock()

        fake_browser = mock.Mock()
        fake_browser.new_context = mock.AsyncMock(return_value=fake_context)
        fake_browser.close = mock.AsyncMock()

        with mock.patch.object(
            qr_login,
            "launch_browser_async",
            new=mock.AsyncMock(return_value=fake_browser),
            create=True,
        ) as launch_browser_async, \
             mock.patch("utils.qr_login.image_manager.save_image", return_value="saved.png"):
            await manager._launch_verification_page(session.session_id)

        launch_browser_async.assert_awaited_once()
        fake_context.new_page.assert_awaited_once_with()
        fake_page.close.assert_awaited_once_with()
        fake_context.close.assert_awaited_once_with()
        fake_browser.close.assert_awaited_once_with()


class CaptchaRemoteControlTypingGuardTest(unittest.TestCase):
    def test_captcha_remote_control_uses_provider_neutral_page_annotations(self):
        controller_type_hints = typing.get_type_hints(
            captcha_remote_control.CaptchaRemoteController.create_session
        )
        self.assertIs(controller_type_hints["page"], typing.Any)


if __name__ == "__main__":
    unittest.main()
