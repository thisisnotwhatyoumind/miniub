from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "miniub-0.5.0.plugin"
USERBOT = ROOT / "userbot.py"


class _Logger:
    def exception(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _install_stubs() -> None:
    base = types.ModuleType("base_plugin")

    class BasePlugin:
        def __init__(self):
            self.logger = _Logger()

        def get_setting(self, key, default=None):
            return default

        def set_setting(self, key, value, **kwargs):
            pass

        def add_on_send_message_hook(self):
            pass

        def add_hook(self, name):
            pass

    class HookResult:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class HookStrategy:
        MODIFY = "modify"
        CANCEL = "cancel"

    base.BasePlugin = BasePlugin
    base.HookResult = HookResult
    base.HookStrategy = HookStrategy
    sys.modules["base_plugin"] = base

    ui = types.ModuleType("ui")
    settings = types.ModuleType("ui.settings")

    class Widget:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    for name in ("Header", "Input", "Switch", "Text"):
        setattr(settings, name, type(name, (Widget,), {}))
    ui.settings = settings
    sys.modules["ui"] = ui
    sys.modules["ui.settings"] = settings

    file_utils = types.ModuleType("file_utils")
    file_utils.get_plugins_dir = lambda: str(ROOT)
    sys.modules["file_utils"] = file_utils

    client_utils = types.ModuleType("client_utils")
    client_utils.send_text = lambda *args, **kwargs: None
    sys.modules["client_utils"] = client_utils


def _load_loader_class():
    _install_stubs()
    namespace = {"__name__": "loader_test", "__file__": str(LOADER)}
    source = LOADER.read_text(encoding="utf-8")
    exec(compile(source, str(LOADER), "exec"), namespace, namespace)
    return namespace["MiniUBPlugin"]


class LoaderTest(unittest.TestCase):
    def test_injects_userbot_methods(self):
        plugin_cls = _load_loader_class()
        plugin = plugin_cls()
        plugin._loader_download = lambda: USERBOT.read_bytes()

        ok, error = plugin._loader_ensure()

        self.assertTrue(ok, error)
        self.assertTrue(plugin._loader_loaded)
        self.assertIn("create_settings", plugin.__dict__)
        self.assertIn("on_plugin_load", plugin.__dict__)
        remote_func = plugin.__dict__["on_plugin_load"].__func__
        self.assertIsNot(remote_func, plugin_cls.on_plugin_load)
        self.assertIn("SYSTEM_CORE", remote_func.__globals__)
        self.assertNotIn("_loader_ensure", plugin.__dict__)

        with tempfile.TemporaryDirectory() as modules_dir:
            plugin.get_setting = lambda key, default=None: (
                modules_dir if key == "modules_dir" else default
            )
            settings = plugin.create_settings()
        self.assertIsInstance(settings, list)
        self.assertTrue(settings)

    def test_rejects_bad_hash(self):
        plugin = _load_loader_class()()
        plugin._loader_download = lambda: b"not the userbot"

        ok, error = plugin._loader_ensure()

        self.assertFalse(ok)
        self.assertIn("SHA256", error)
        self.assertFalse(getattr(plugin, "_loader_loaded", False))


if __name__ == "__main__":
    unittest.main()
