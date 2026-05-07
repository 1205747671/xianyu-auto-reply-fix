import sys
import types
import unittest
from unittest import mock


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
    sys.modules["cloakbrowser"] = cloakbrowser_stub

from utils.xianyu_slider_stealth import XianyuSliderStealth


class _FakePage:
    def __init__(self, url):
        self.url = url
        self.frames = []
        self.close = mock.Mock()
        self.goto = mock.Mock()
        self.reload = mock.Mock()
        self.wait_for_load_state = mock.Mock()

    def is_closed(self):
        return False

    def title(self):
        return "闲鱼验证页"

    def inner_text(self, selector, timeout=None):
        if selector != "body":
            raise AssertionError(f"unexpected selector: {selector}")
        return ""


class SliderFreshTabRecoveryTest(unittest.TestCase):
    @mock.patch("utils.xianyu_slider_stealth.time.sleep", return_value=None)
    def test_solve_slider_retries_in_fresh_tab_after_primary_page_fails(self, _mock_sleep):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        original_page = _FakePage("https://passport.goofish.com/mini_login.htm")
        fresh_page = _FakePage("https://passport.goofish.com/mini_login.htm")
        context = mock.Mock()
        context.new_page.return_value = fresh_page

        slider.pure_user_id = "fresh_tab_retry"
        slider.page = original_page
        slider.context = context
        slider.last_verification_feedback = {}
        slider.enable_learning = False
        slider._save_debug_snapshot = mock.Mock()
        slider._harden_password_slider_runtime = mock.Mock()
        slider._detect_special_captcha_block = lambda _page=None: None
        slider._wait_for_punish_slider_dom_ready_if_needed = lambda page, block, scene: block
        slider._recover_punish_slider_shell_if_possible = lambda page, block, scene: block
        slider._probe_context_login_during_slider = lambda _page=None: (False, {})
        slider._snapshot_context_cookies = lambda *args, **kwargs: {}
        slider.click_to_reset_slider = mock.Mock(return_value=False)
        slider._page_has_slider = lambda page: page is fresh_page
        slider._should_abort_slider_retry_after_failure = lambda: (False, "")

        def find_slider_elements(fast_mode=False):
            if slider.page is original_page:
                return None, None, None
            return object(), object(), object()

        slider.find_slider_elements = find_slider_elements
        slider.calculate_slide_distance = lambda button, track: 120
        slider.generate_human_trajectory = lambda distance, attempt=1: [(0, 0), (distance, 0)]
        slider.simulate_slide = lambda button, trajectory: True
        slider.check_verification_success_fast = lambda button: True

        result = slider.solve_slider(max_retries=1, fast_mode=True)

        self.assertTrue(result)
        context.new_page.assert_called_once_with()
        fresh_page.goto.assert_called_once()
        self.assertIs(slider.page, fresh_page)

    @mock.patch("utils.xianyu_slider_stealth.time.sleep", return_value=None)
    def test_solve_slider_fresh_tab_retry_does_not_recurse_into_more_fresh_tabs(self, _mock_sleep):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        original_page = _FakePage("https://passport.goofish.com/mini_login.htm")
        fresh_page = _FakePage("https://passport.goofish.com/mini_login.htm")
        context = mock.Mock()
        context.new_page.return_value = fresh_page

        slider.pure_user_id = "fresh_tab_guard"
        slider.page = original_page
        slider.context = context
        slider.last_verification_feedback = {}
        slider.enable_learning = False
        slider._save_debug_snapshot = mock.Mock()
        slider._harden_password_slider_runtime = mock.Mock()
        slider._detect_special_captcha_block = lambda _page=None: None
        slider._wait_for_punish_slider_dom_ready_if_needed = lambda page, block, scene: block
        slider._recover_punish_slider_shell_if_possible = lambda page, block, scene: block
        slider._probe_context_login_during_slider = lambda _page=None: (False, {})
        slider._snapshot_context_cookies = lambda *args, **kwargs: {}
        slider.click_to_reset_slider = mock.Mock(return_value=False)
        slider._page_has_slider = lambda page: page is fresh_page
        slider._should_abort_slider_retry_after_failure = lambda: (False, "")
        slider._safe_page_url = lambda page: page.url
        slider.find_slider_elements = lambda fast_mode=False: (object(), object(), object())
        slider.calculate_slide_distance = lambda button, track: 120
        slider.generate_human_trajectory = lambda distance, attempt=1: [(0, 0), (distance, 0)]
        slider.simulate_slide = lambda button, trajectory: True
        slider.check_verification_success_fast = lambda button: False

        result = slider.solve_slider(max_retries=1, fast_mode=True)

        self.assertFalse(result)
        context.new_page.assert_called_once_with()
        fresh_page.close.assert_called_once_with()
        self.assertIs(slider.page, original_page)

    @mock.patch("utils.xianyu_slider_stealth.time.sleep", return_value=None)
    def test_stabilize_logged_in_context_cookies_uses_fresh_tabs_when_same_page_actions_do_not_fill_protected_fields(self, _mock_sleep):
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        current_page = _FakePage("https://www.goofish.com/im")
        fresh_page = _FakePage("https://www.goofish.com/im")
        context = mock.Mock()
        context.new_page.return_value = fresh_page

        missing_cookies = {
            "unb": "u",
            "sgcookie": "sg",
            "cookie2": "c2",
            "_m_h5_tk": "tk",
            "_m_h5_tk_enc": "tk_enc",
            "t": "t_cookie",
        }
        completed_cookies = dict(missing_cookies)
        completed_cookies.update({
            "havana_lgc2_77": "havana",
            "_tb_token_": "tb_token",
            "cna": "cna_cookie",
        })

        slider.pure_user_id = "fresh_tab_stabilize"
        slider._log_cookie_snapshot_integrity = mock.Mock()
        slider._perform_browser_cookie_warmup_probes = mock.Mock(return_value=missing_cookies)
        slider._get_context_pages = lambda _context, fallback_page=None: [fallback_page] if fallback_page else []

        def snapshot(_context, page=None):
            if page is fresh_page:
                return dict(completed_cookies)
            return dict(missing_cookies)

        slider._snapshot_context_cookies = snapshot

        result = slider._stabilize_logged_in_context_cookies(
            context,
            current_page,
            scene="单元测试登录完成后",
        )

        context.new_page.assert_called()
        self.assertEqual(result["havana_lgc2_77"], "havana")
        self.assertEqual(result["_tb_token_"], "tb_token")
        slider._perform_browser_cookie_warmup_probes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
