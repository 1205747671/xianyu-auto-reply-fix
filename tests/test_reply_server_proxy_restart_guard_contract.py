from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReplyServerProxyRestartGuardContractTest(unittest.TestCase):
    def test_proxy_update_skips_restart_when_config_is_unchanged(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("def _normalize_proxy_config_snapshot(", source)
        self.assertIn("if current_proxy_config == desired_proxy_config:", source)
        self.assertIn("'task_restarted': False", source)
        self.assertIn("Proxy config unchanged; skip runtime restart", source)

    def test_proxy_update_only_restarts_for_effective_proxy_change(self):
        source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

        self.assertIn("current_effective_proxy_config", source)
        self.assertIn("desired_effective_proxy_config", source)
        self.assertIn("if current_effective_proxy_config == desired_effective_proxy_config:", source)
        self.assertIn("Proxy storage changed but effective config unchanged; skip runtime restart", source)
        self.assertIn("'task_restarted': bool(cookie_value)", source)


if __name__ == "__main__":
    unittest.main()
