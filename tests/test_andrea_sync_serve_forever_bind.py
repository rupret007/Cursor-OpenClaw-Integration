"""Regression: bind collision on andrea_sync listen should exit cleanly (no traceback storm)."""
from __future__ import annotations

import errno
import io
import sys
import unittest
from unittest.mock import MagicMock, patch

from services.andrea_sync import server as server_mod


class ServeForeverBindTests(unittest.TestCase):
    def test_eaddrinuse_exits_1_with_stderr_message(self) -> None:
        stderr = io.StringIO()
        fake_sync = MagicMock()
        fake_sync.db_path = "/tmp/test.db"

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EADDRINUSE, "Address already in use")

        with patch.object(server_mod, "SyncServer", return_value=fake_sync), patch.object(
            server_mod, "make_handler", return_value=MagicMock()
        ), patch.object(server_mod, "AndreaThreadingHTTPServer", side_effect=boom), patch.object(
            sys, "stderr", stderr
        ):
            with self.assertRaises(SystemExit) as ctx:
                server_mod.serve_forever(host="127.0.0.1", port=59999)
        self.assertEqual(ctx.exception.code, 1)
        err = stderr.getvalue()
        self.assertIn("cannot bind", err)
        self.assertIn("59999", err)
        self.assertIn("andrea_services.sh", err)

    def test_andrea_threading_server_sets_reuse_address(self) -> None:
        self.assertTrue(server_mod.AndreaThreadingHTTPServer.allow_reuse_address)


if __name__ == "__main__":
    unittest.main()
