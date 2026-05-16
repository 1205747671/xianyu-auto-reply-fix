import unittest
from unittest import mock

import utils.xianyu_slider_stealth as slider_module


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeCookies:
    def __init__(self):
        self.values = {}

    def update(self, data):
        self.values.update(data)

    def get_dict(self):
        return dict(self.values)


class _FakeSession:
    def __init__(self, payload, response_cookies=None):
        self.headers = {}
        self.cookies = _FakeCookies()
        self._payload = payload
        self._response_cookies = dict(response_cookies or {})
        self.post_calls = []

    def post(self, url, params=None, data=None, proxies=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "params": params,
                "data": data,
                "proxies": proxies,
                "timeout": timeout,
            }
        )
        self.cookies.update(self._response_cookies)
        return _FakeResponse(self._payload)


class ManualCookieImportPrecheckTest(unittest.TestCase):
    def _run_probe(self, payload, response_cookies=None):
        fake_session = _FakeSession(payload, response_cookies=response_cookies)
        with mock.patch("requests.Session", return_value=fake_session):
            result = slider_module.probe_cookie_verification_from_cookie(
                "unb=test_user; _m_h5_tk=test_token_12345; cookie2=dummy",
                proxy=None,
            )
        return result, fake_session

    def test_probe_cookie_verification_returns_verification_url_when_present(self):
        result, fake_session = self._run_probe(
            {
                "ret": ["FAIL_SYS_USER_VALIDATE::校验失败"],
                "data": {"url": "https://passport.goofish.com/identity_verify"},
                "v": "1.0",
            }
        )

        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(
            result["verification_url"],
            "https://passport.goofish.com/identity_verify",
        )
        self.assertEqual(result["session_cookies"]["unb"], "test_user")
        self.assertEqual(fake_session.post_calls[0]["timeout"], 30)

    def test_probe_cookie_verification_treats_success_token_payload_as_cookie_valid(self):
        result, _fake_session = self._run_probe(
            {
                "ret": ["SUCCESS::调用成功"],
                "data": {
                    "accessToken": "oauth_access_token",
                    "refreshToken": "oauth_refresh_token",
                },
                "v": "1.0",
            },
            response_cookies={
                "_m_h5_tk": "refreshed_token_12345",
                "cookie2": "updated_cookie2",
            },
        )

        self.assertEqual(result["status"], "cookie_valid")
        self.assertIsNone(result["verification_url"])
        self.assertTrue(result["payload"]["data"]["accessToken"])
        self.assertEqual(
            result["session_cookies"]["_m_h5_tk"],
            "refreshed_token_12345",
        )
        self.assertEqual(
            result["session_cookies"]["cookie2"],
            "updated_cookie2",
        )

    def test_probe_cookie_verification_uses_runtime_browser_identity_headers(self):
        runtime_identity = {
            "user_agent": "Mozilla/5.0 test Chrome/146.0.7680.177 Safari/537.36",
            "sec_ch_ua": '"Not.A/Brand";v="8", "Chromium";v="146", "Google Chrome";v="146"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "accept_language": "zh-CN,zh;q=0.9",
        }

        result, fake_session = None, None
        with mock.patch.object(
            slider_module,
            "get_runtime_browser_identity",
            return_value=runtime_identity,
        ):
            result, fake_session = self._run_probe(
                {
                    "ret": ["SUCCESS::璋冪敤鎴愬姛"],
                    "data": {
                        "accessToken": "oauth_access_token",
                        "refreshToken": "oauth_refresh_token",
                    },
                    "v": "1.0",
                }
            )

        self.assertEqual(fake_session.headers["user-agent"], runtime_identity["user_agent"])
        self.assertEqual(fake_session.headers["sec-ch-ua"], runtime_identity["sec_ch_ua"])
        self.assertEqual(fake_session.headers["sec-ch-ua-mobile"], runtime_identity["sec_ch_ua_mobile"])
        self.assertEqual(fake_session.headers["sec-ch-ua-platform"], runtime_identity["sec_ch_ua_platform"])


if __name__ == "__main__":
    unittest.main()
