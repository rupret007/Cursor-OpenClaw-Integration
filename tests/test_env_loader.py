import importlib.util
import os
import pathlib
import tempfile
import unittest


LOADER_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "env_loader.py"
SPEC = importlib.util.spec_from_file_location("env_loader", LOADER_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class EnvLoaderTests(unittest.TestCase):
    def test_parse_json_value(self):
        self.assertEqual(MODULE.parse_env_line('FOO="bar baz"'), ("FOO", "bar baz"))
        self.assertEqual(MODULE.parse_env_line('X=""'), ("X", ""))

    def test_parse_unquoted_shlex(self):
        self.assertEqual(MODULE.parse_env_line("CURSOR_BASE_URL=https://api.cursor.com"), ("CURSOR_BASE_URL", "https://api.cursor.com"))
        self.assertEqual(MODULE.parse_env_line(r"KEY=with\ space"), ("KEY", "with space"))

    def test_merge_respects_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = pathlib.Path(tmp) / ".env"
            env_path.write_text('FROM_FILE="should_not_apply"\n', encoding="utf-8")
            os.environ["FROM_FILE"] = "already_set"
            try:
                MODULE.merge_dotenv_paths([env_path], override=False)
                self.assertEqual(os.environ.get("FROM_FILE"), "already_set")
            finally:
                os.environ.pop("FROM_FILE", None)

    def test_merge_fills_missing(self):
        key = "ENV_LOADER_TEST_KEY_XYZ"
        with tempfile.TemporaryDirectory() as tmp:
            env_path = pathlib.Path(tmp) / ".env"
            env_path.write_text(f'{key}="filled_from_file"\n', encoding="utf-8")
            old = os.environ.pop(key, None)
            try:
                MODULE.merge_dotenv_paths([env_path], override=False)
                self.assertEqual(os.environ.get(key), "filled_from_file")
            finally:
                if old is not None:
                    os.environ[key] = old
                else:
                    os.environ.pop(key, None)

    def test_merge_override_replaces_existing_value(self):
        key = "ENV_LOADER_OVERRIDE_TEST_KEY"
        with tempfile.TemporaryDirectory() as tmp:
            env_path = pathlib.Path(tmp) / "override.env"
            env_path.write_text(f'{key}="override_value"\n', encoding="utf-8")
            old = os.environ.get(key)
            os.environ[key] = "base_value"
            try:
                MODULE.merge_dotenv_paths([env_path], override=True)
                self.assertEqual(os.environ.get(key), "override_value")
            finally:
                if old is not None:
                    os.environ[key] = old
                else:
                    os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
