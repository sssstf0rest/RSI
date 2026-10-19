"""Runtime visibility for the restricted, no-inference sandbox launcher."""

from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from rsi.policy_runner import PolicyRunner
from rsi.process import execute
from test_seed import TemporaryWorkspace


class SandboxCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryWorkspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "isolated codex home"
        self.policy = self.root / "source"
        self.policy.mkdir()
        (self.policy / "policy.py").write_text(
            'print(\'{"selected_ids":[],"instructions":""}\')\n', encoding="utf-8")

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
        return path.resolve()

    def profile(self, launcher):
        def fixture(command, **kwargs):
            self.command, self.env = command, kwargs["env"]
            # These fixed fixture programs alone bypass OS isolation.
            return execute(command[command.index("--") + 1:], **kwargs)
        runner = PolicyRunner(str(launcher), self.home, executor=fixture)
        with patch("rsi.core.sys.platform", "linux"), patch("rsi.core.shutil.which", return_value=str(launcher)):
            runner.run(self.policy, {"query": "fixture", "documents": [], "experience": [],
                                    "max_context_chars": 100}, artifacts=self.root / "artifacts")
        override = next(value for value in self.command if value.startswith("permissions.rsi.filesystem="))
        return tomllib.loads(override)["permissions"]["rsi"]["filesystem"]

    def test_linux_bootstrap_paths_readable_without_opening_codex_home(self):
        native = self.file("installation/codex")
        filesystem = self.profile(native)
        self.assertEqual(filesystem.get(str(native)), "read", "bubblewrap must see the native executable")
        for name in ("arg0", "arg0-linux"):
            alias_root = str((self.home / "tmp" / name).resolve())
            self.assertEqual(filesystem.get(alias_root), "read", "old bubblewrap must find the live helper alias")
        self.assertEqual(self.env["CODEX_HOME"], str(self.home))
        self.assertNotIn(str(self.home.resolve()), filesystem)
        self.assertNotIn(str(self.home.parent.resolve()), filesystem)
        self.assertEqual(filesystem[":root"], "deny")
        self.assertEqual(filesystem[":slash_tmp"], "deny")
        self.assertEqual(filesystem[":workspace_roots"]["."], "read")
        self.assertEqual(filesystem[":workspace_roots"][".env"], "deny")
        self.assertIn("permissions.rsi.network.enabled=false", self.command)

    def test_npm_native_executable_visible_in_supported_package_layouts(self):
        for layout in ("bundled", "nested", "hoisted", "legacy"):
            with self.subTest(layout=layout), TemporaryWorkspace() as directory:
                self.root = Path(directory)
                self.home = self.root / "sandbox-home"
                self.policy = self.root / "policy"
                self.policy.mkdir()
                (self.policy / "policy.py").write_text(
                    'print(\'{"selected_ids":[],"instructions":""}\')\n', encoding="utf-8")
                package = "node/lib/node_modules/@openai/codex"
                launcher = self.file(package + "/bin/codex.js")
                vendor = {
                    "bundled": package + "/vendor",
                    "nested": package + "/node_modules/@openai/codex-linux-x64/vendor",
                    "hoisted": "node/lib/node_modules/@openai/codex-linux-arm64/vendor",
                    "legacy": package + "/vendor",
                }[layout]
                target = "aarch64-unknown-linux-musl" if layout == "hoisted" else "x86_64-unknown-linux-musl"
                binary = "codex/codex" if layout == "legacy" else "bin/codex"
                native = self.file(f"{vendor}/{target}/{binary}")
                filesystem = self.profile(launcher)
                self.assertEqual(filesystem.get(str(native)), "read", "npm's native target must survive the mount namespace")
                self.assertNotIn(str(launcher.parent.parent), filesystem)
                self.assertNotIn(str(native.parent), filesystem)


if __name__ == "__main__":
    unittest.main()
