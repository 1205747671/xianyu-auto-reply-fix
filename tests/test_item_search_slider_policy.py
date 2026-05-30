import sys
import types
import unittest
from unittest import mock


if "loguru" not in sys.modules:
    loguru_stub = types.ModuleType("loguru")
    loguru_stub.logger = mock.Mock()
    sys.modules["loguru"] = loguru_stub
elif not hasattr(sys.modules["loguru"].logger, "success"):
    sys.modules["loguru"].logger.success = mock.Mock()

if "cloakbrowser" not in sys.modules:
    cloakbrowser_stub = types.ModuleType("cloakbrowser")

    def _not_used(*args, **kwargs):
        raise AssertionError("cloakbrowser stub should be patched in tests")

    cloakbrowser_stub.launch = _not_used
    cloakbrowser_stub.launch_async = _not_used
    cloakbrowser_stub.launch_context = _not_used
    cloakbrowser_stub.launch_context_async = _not_used
    cloakbrowser_stub.launch_persistent_context = _not_used
    cloakbrowser_stub.launch_persistent_context_async = _not_used
    cloakbrowser_stub.ensure_binary = lambda: "C:/cloakbrowser/chrome.exe"
    cloakbrowser_stub.build_args = (
        lambda stealth_args, extra_args, timezone=None, locale=None, headless=True: list(extra_args or [])
    )
    cloakbrowser_stub.maybe_resolve_geoip = (
        lambda geoip, proxy, timezone, locale: (timezone, locale, None)
    )
    sys.modules["cloakbrowser"] = cloakbrowser_stub

import utils.item_search as item_search


class ItemSearchSliderPolicyTest(unittest.IsolatedAsyncioTestCase):
    def _make_searcher(self):
        return item_search.XianyuSearcher(account_id="test-account", cookie_value="test-cookie")

    def _make_scratch_page(self):
        slider_element = mock.Mock()
        slider_element.is_visible = mock.AsyncMock(return_value=True)

        async def query_selector(selector):
            if selector == "#scratch-captcha-btn":
                return slider_element
            return None

        page = mock.Mock()
        page.content = mock.AsyncMock(
            return_value="""
            <div id="nocaptcha">
              <div id="scratch-captcha-btn"></div>
              <div class="scratch-captcha-slider">Release the slider after pillows fully appears</div>
            </div>
            """
        )
        page.query_selector = mock.AsyncMock(side_effect=query_selector)
        page.frames = []
        page.main_frame = object()
        return page

    async def test_scratch_captcha_prefers_automatic_solver_before_any_manual_remote_control(self):
        searcher = self._make_searcher()
        searcher._handle_scratch_captcha_async = mock.AsyncMock(return_value=True)
        searcher._handle_scratch_captcha_manual = mock.AsyncMock(return_value=True)

        page = self._make_scratch_page()

        with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth") as slider_cls:
            result = await searcher.handle_slider_verification(
                page=page,
                context=object(),
                browser=object(),
                playwright=None,
                max_retries=2,
            )

        self.assertTrue(result)
        searcher._handle_scratch_captcha_async.assert_awaited_once_with(page, max_retries=2)
        searcher._handle_scratch_captcha_manual.assert_not_awaited()
        slider_cls.assert_not_called()

    async def test_scratch_captcha_falls_back_to_general_slider_solver_when_automatic_scratch_solver_fails(self):
        searcher = self._make_searcher()
        searcher._handle_scratch_captcha_async = mock.AsyncMock(return_value=False)
        searcher._handle_scratch_captcha_manual = mock.AsyncMock(return_value=True)

        fake_slider_handler = mock.Mock()
        fake_slider_handler.solve_slider.return_value = True
        page = self._make_scratch_page()
        context = object()
        browser = object()

        with mock.patch(
            "utils.xianyu_slider_stealth.XianyuSliderStealth",
            return_value=fake_slider_handler,
        ) as slider_cls:
            result = await searcher.handle_slider_verification(
                page=page,
                context=context,
                browser=browser,
                playwright=None,
                max_retries=2,
            )

        self.assertTrue(result)
        searcher._handle_scratch_captcha_async.assert_awaited_once_with(page, max_retries=2)
        searcher._handle_scratch_captcha_manual.assert_not_awaited()
        slider_cls.assert_called_once()
        fake_slider_handler.solve_slider.assert_called_once_with(max_retries=2)

    async def test_manual_remote_control_is_only_used_when_explicitly_enabled_for_debug(self):
        searcher = self._make_searcher()
        searcher.enable_manual_scratch_captcha_debug = True
        searcher.use_remote_control = True
        searcher._handle_scratch_captcha_async = mock.AsyncMock(return_value=False)
        searcher._handle_scratch_captcha_manual = mock.AsyncMock(return_value=True)

        fake_slider_handler = mock.Mock()
        fake_slider_handler.solve_slider.return_value = False
        page = self._make_scratch_page()

        with mock.patch(
            "utils.xianyu_slider_stealth.XianyuSliderStealth",
            return_value=fake_slider_handler,
        ):
            result = await searcher.handle_slider_verification(
                page=page,
                context=object(),
                browser=object(),
                playwright=None,
                max_retries=2,
            )

        self.assertTrue(result)
        searcher._handle_scratch_captcha_async.assert_awaited_once_with(page, max_retries=2)
        fake_slider_handler.solve_slider.assert_called_once_with(max_retries=2)
        searcher._handle_scratch_captcha_manual.assert_awaited_once_with(
            page,
            max_retries=3,
            wait_for_completion=True,
        )

    def test_manual_remote_control_session_id_uses_unique_search_scope_instead_of_plain_account_id(self):
        searcher = self._make_searcher()

        with mock.patch("utils.item_search.time.time_ns", return_value=123456789):
            session_id = searcher._build_remote_control_session_id()

        self.assertEqual(session_id, "item-search-test-account-123456789")
        self.assertNotEqual(session_id, "test-account")


if __name__ == "__main__":
    unittest.main()
