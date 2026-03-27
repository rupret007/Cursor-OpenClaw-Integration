from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from services.andrea_sync.backends.cursor_control import cancel_all_jobs, list_active_jobs


class TestCursorControlBackend(unittest.TestCase):
    @mock.patch("services.andrea_sync.backends.cursor_control.subprocess.run")
    def test_cancel_all_jobs_summarizes_terminal_and_canceled(self, run_mock: mock.MagicMock) -> None:
        run_mock.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "response": {
                            "agents": [
                                {"id": "job_1", "status": "RUNNING"},
                                {"id": "job_2", "status": "FINISHED"},
                            ]
                        },
                    }
                ),
                stderr="",
            ),
            mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "response": {"status": "STOPPED"}}),
                stderr="",
            ),
        ]
        result = cancel_all_jobs(repo_root=Path("."))
        self.assertEqual(result.canceled_count, 1)
        self.assertEqual(result.terminal_already_count, 1)
        self.assertEqual(
            [item.status for item in result.results],
            ["canceled", "already_finished"],
        )

    @mock.patch("services.andrea_sync.backends.cursor_control.subprocess.run")
    def test_list_active_jobs_filters_terminal_rows(self, run_mock: mock.MagicMock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "response": {
                        "agents": [
                            {"id": "job_1", "status": "RUNNING"},
                            {"id": "job_2", "status": "FAILED"},
                            {"id": "job_3", "status": "PENDING"},
                        ]
                    },
                }
            ),
            stderr="",
        )
        result = list_active_jobs(repo_root=Path("."))
        self.assertEqual(result.active_count, 2)
        self.assertEqual(
            [item.id for item in result.results],
            ["job_1", "job_3"],
        )


if __name__ == "__main__":
    unittest.main()
