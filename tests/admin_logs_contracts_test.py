import asyncio
from collections import deque
import os
from pathlib import Path
import sys
import threading
import tempfile
import types
import unittest
from unittest import mock

if "loguru" not in sys.modules:
    loguru_stub = types.ModuleType("loguru")
    loguru_stub.logger = mock.Mock()
    sys.modules["loguru"] = loguru_stub

import reply_server
import file_log_collector


async def _read_streaming_response(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


class AdminLogsContractsTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)
        self.addCleanup(lambda: os.chdir(self.original_cwd))

        Path("logs").mkdir(exist_ok=True)
        logger_patcher = mock.patch.object(reply_server, "logger", mock.Mock())
        self.mock_logger = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        log_with_user_patcher = mock.patch.object(reply_server, "log_with_user")
        self.mock_log_with_user = log_with_user_patcher.start()
        self.addCleanup(log_with_user_patcher.stop)

        self.admin_user = {"username": "admin", "user_id": "admin-1", "is_admin": True}

    def _create_log_file(self, name: str, content, modified_ts: float) -> Path:
        path = Path("logs") / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        os.utime(path, (modified_ts, modified_ts))
        return path

    def test_system_logs_ignore_compressed_archives_when_loading_latest_text_log(self):
        current_log = self._create_log_file(
            "xianyu_2026-05-26.log",
            "line-1\nline-2\n",
            100.0,
        )
        self._create_log_file(
            "xianyu_2026-05-25.log.zip",
            b"PK\x03\x04compressed-log",
            200.0,
        )

        result = reply_server.get_system_logs(
            admin_user=self.admin_user,
            lines=10,
            level=None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["log_file"], current_log.name)
        self.assertEqual(result["logs"], ["line-1", "line-2"])

    def test_system_logs_level_filter_matches_padded_loguru_level_columns(self):
        self._create_log_file(
            "xianyu_2026-05-26.log",
            (
                "2026-05-26 10:00:00.000 | INFO     | app:run:1 - info-line\n"
                "2026-05-26 10:00:01.000 | ERROR    | app:run:2 - error-line\n"
            ),
            100.0,
        )

        result = reply_server.get_system_logs(
            admin_user=self.admin_user,
            lines=10,
            level="error",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["logs"], ["2026-05-26 10:00:01.000 | ERROR    | app:run:2 - error-line"])

    def test_file_log_collector_parses_padded_levels_and_filters_case_insensitively(self):
        collector = file_log_collector.FileLogCollector.__new__(file_log_collector.FileLogCollector)
        collector.max_logs = 10
        collector.logs = deque(maxlen=10)
        collector.lock = threading.Lock()

        collector.parse_log_line("2026-05-26 10:00:00.000 | INFO     | app:run:1 - info-line")
        collector.parse_log_line("2026-05-26 10:00:01.000 | ERROR    | app:run:2 - error-line")

        filtered_logs = collector.get_logs(lines=10, level_filter="error")

        self.assertEqual(len(filtered_logs), 1)
        self.assertEqual(filtered_logs[0]["level"], "ERROR")
        self.assertEqual(filtered_logs[0]["message"], "error-line")

    def test_log_file_list_includes_compressed_archives_and_sorts_by_modified_time(self):
        self._create_log_file(
            "xianyu_2026-05-24.log",
            "older-log\n",
            100.0,
        )
        newest_archive = self._create_log_file(
            "xianyu_2026-05-25.log.zip",
            b"PK\x03\x04archive-data",
            200.0,
        )
        self._create_log_file("notes.txt", "ignore-me", 300.0)

        result = reply_server.list_log_files(admin_user=self.admin_user)

        self.assertTrue(result["success"])
        self.assertEqual(
            [item["name"] for item in result["files"]],
            [newest_archive.name, "xianyu_2026-05-24.log"],
        )

    def test_log_export_rejects_files_outside_admin_log_whitelist(self):
        self._create_log_file("notes.txt", "not-a-log", 100.0)

        with self.assertRaises(reply_server.HTTPException) as raised:
            reply_server.export_log_file("notes.txt", admin_user=self.admin_user)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "日志文件不存在")

    def test_log_export_supports_compressed_archives_with_zip_media_type(self):
        archive_payload = b"PK\x03\x04archive-bytes"
        archive = self._create_log_file(
            "xianyu_2026-05-25.log.zip",
            archive_payload,
            100.0,
        )

        response = reply_server.export_log_file(archive.name, admin_user=self.admin_user)
        streamed_body = asyncio.run(_read_streaming_response(response))

        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(
            response.headers["content-disposition"],
            f'attachment; filename="{archive.name}"',
        )
        self.assertEqual(streamed_body, archive_payload)


if __name__ == "__main__":
    unittest.main()
