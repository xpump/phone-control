"""Device-free behavioral tests: no connected phone or external tools required."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location(
    "phone_control", Path(__file__).resolve().parents[1] / "scripts/phone_control.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phone control test ")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.scrcpy = self.tool("official package/scrcpy", "scrcpy")
        self.adb = self.tool("android tools/adb", "adb")
        self.agent = self.tool("npm wrapper/agent-device", "agent-device")
        Path(self.scrcpy["path"]).with_name("scrcpy-server").write_bytes(b"test fixture")
        self.devices = [{"serial": "TEST-A", "state": "device", "model": "TestPhone"}]
        self.tools = {"scrcpy": self.scrcpy, "adb": self.adb, "agent-device": self.agent}
        self.free = {"known": True, "devices": [{"id": "TEST-A"}]}

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, relative, name):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
        path.chmod(0o700)
        return {"path": str(path), "version": name + " test", "fingerprint": pc.fingerprint(path),
                "flags": ["--serial", "--keyboard", "--no-audio", "--window-title"],
                "short_flags": ["-s", "-K"], "keyboard_modes": ["sdk", "uhid"],
                "help_checked": True, "app_path": None, "bundle_root": None}

    def args(self, *args):
        return pc.parser().parse_args([args[0], "--state-dir", str(self.state), *args[1:]])

    def environment(self, mirrors=None, claims=None):
        stack = ExitStack()
        stack.enter_context(patch.object(pc, "get_tools", return_value=(self.tools, {})))
        stack.enter_context(patch.object(pc, "adb_devices", return_value=self.devices))
        stack.enter_context(patch.object(pc, "claim_report", return_value=claims or self.free))
        stack.enter_context(patch.object(pc, "running_scrcpy", return_value=mirrors or []))
        return stack

    def assert_code(self, code, fn, *args):
        with self.assertRaises(pc.ControlError) as error:
            fn(*args)
        self.assertEqual(error.exception.code, code)

    def discover_with(self, found, common, memory, probe):
        stack = ExitStack()
        stack.enter_context(patch.object(pc.shutil, "which", return_value=found))
        stack.enter_context(patch.object(pc, "common_candidates", return_value=common))
        stack.enter_context(patch.object(pc, "remembered_candidates", return_value=memory))
        stack.enter_context(patch.object(pc, "probe", side_effect=probe))
        return stack

    def test_path_precedes_common_and_memory(self):
        probe = Mock(return_value=self.scrcpy)
        with self.discover_with(self.scrcpy["path"], [self.agent["path"]], [self.adb["path"]], probe):
            result = pc.discover("scrcpy", self.state)
        self.assertEqual(result["source"], "PATH")
        probe.assert_called_once_with("scrcpy", self.scrcpy["path"])

    def test_explicit_path_precedes_lookup(self):
        with patch.object(pc.shutil, "which") as which, patch.object(pc, "probe", return_value=self.scrcpy):
            result = pc.discover("scrcpy", self.state, self.scrcpy["path"])
        self.assertEqual(result["source"], "explicit")
        which.assert_not_called()

    def test_invalid_explicit_path_is_not_replaced(self):
        with patch.object(pc.shutil, "which") as which:
            self.assert_code("explicit_path_invalid", pc.discover, "scrcpy", self.state, self.root / "missing")
        which.assert_not_called()

    def test_broken_path_install_uses_common_before_memory(self):
        def probe(tool, path):
            if path == self.agent["path"]:
                raise pc.ControlError("tool_probe_failed", "external volume missing")
            return self.scrcpy
        with self.discover_with(self.agent["path"], [self.scrcpy["path"]], [self.adb["path"]], probe):
            result = pc.discover("scrcpy", self.state)
        self.assertEqual(result["source"], "common")

    def test_memory_after_common(self):
        with self.discover_with(None, [str(self.root / "missing")], [self.scrcpy["path"]], Mock(return_value=self.scrcpy)):
            self.assertEqual(pc.discover("scrcpy", self.state)["source"], "remembered")

    def test_exhausted_search_requests_path(self):
        with self.discover_with(None, [], [], Mock()):
            self.assert_code("tool_not_found", pc.discover, "scrcpy", self.state)

    def test_platform_locations(self):
        env = {"LOCALAPPDATA": str(self.root / "Local"), "APPDATA": str(self.root / "Roaming"),
               "ANDROID_HOME": str(self.root / "sdk"), "XDG_STATE_HOME": str(self.root / "xdg")}
        for system in ("Darwin", "Windows", "Linux"):
            self.assertTrue(any("platform-tools" in p for p in pc.common_candidates("adb", system, env, self.root)))
            self.assertEqual(pc.default_state_dir(system, env, self.root).name, "phone-control")
        for tool, system, expected in (("scrcpy", "Darwin", "homebrew"), ("scrcpy", "Windows", "scoop"),
                                       ("agent-device", "Windows", "npm"), ("scrcpy", "Linux", "snap")):
            self.assertTrue(any(expected in p for p in pc.common_candidates(tool, system, env, self.root)))

    def test_mac_bundle_and_matching_server(self):
        root = self.root / "custom apps/Scrcpy.app"
        binary = root / "Contents/Helpers/ScrcpyMirror.app/Contents/MacOS/scrcpy"
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture")
        binary.chmod(0o700)
        server = root / "Contents/Resources/bin/scrcpy-server"
        server.parent.mkdir(parents=True)
        server.write_bytes(b"server")
        self.assertEqual(pc.executable_path(root, "scrcpy"), binary)
        paths = pc.app_paths(binary)
        self.assertEqual(paths["bundle_root"], str(root.resolve()))
        self.assertEqual(pc.server_path({"path": str(binary), **paths}), str(server.resolve()))

    def test_probe_rechecks_help_and_version(self):
        outputs = [(0, "scrcpy 1.0", ""), (0, "scrcpy\n -s serial\n --no-audio", ""),
                   (0, "scrcpy 2.0", ""), (0, "scrcpy\n --serial value\n --keyboard sdk uhid", "")]
        with patch.object(pc, "run", side_effect=outputs) as run:
            old = pc.probe("scrcpy", self.scrcpy["path"])
            new = pc.probe("scrcpy", self.scrcpy["path"])
        self.assertNotEqual(old["version"], new["version"])
        self.assertNotIn("--serial", old["flags"])
        self.assertIn("--serial", new["flags"])
        self.assertEqual(run.call_count, 4)

    def test_device_states_and_selection(self):
        devices = pc.parse_devices("List of devices attached\nTEST-A device model:TestPhone\nB unauthorized\nC offline\n")
        self.assertEqual(pc.select_device(devices, "TEST-A")["serial"], "TEST-A")
        self.assert_code("device_selection_required", pc.select_device, devices)
        self.assert_code("device_not_ready", pc.select_device, devices, "B")
        self.assert_code("device_not_ready", pc.select_device, devices, "C")
        self.assert_code("no_device", pc.select_device, [])

    def test_keyboard_modes_come_from_flag_definition_not_earlier_mentions(self):
        help_text = ('scrcpy\nSee also --keyboard and --mouse.\n'
                     '    --keyboard=mode\n        Possible values are "sdk", "uhid" and "aoa".\n'
                     '    --mouse=mode\n        Something else.\n')
        with patch.object(pc, "run", side_effect=[(0, "scrcpy 5.0", ""), (0, help_text, "")]):
            info = pc.probe("scrcpy", self.scrcpy["path"])
        self.assertEqual(info["keyboard_modes"], ["aoa", "sdk", "uhid"])

    def test_process_serial_syntax(self):
        for text in ('scrcpy --serial=TEST-A', 'scrcpy -s TEST-A', 'scrcpy --serial "TEST-A"',
                     '"C:\\Program Files\\scrcpy.exe" --serial=TEST-A'):
            self.assertEqual(pc.parse_serial(text), "TEST-A")
        self.assertIsNone(pc.parse_serial("scrcpy --no-audio"))

    def test_official_argv_and_server_without_bridge(self):
        plan = pc.build_launch(self.scrcpy, self.adb, "TEST-A")
        self.assertEqual(plan["argv"][:3], [self.scrcpy["path"], "--serial", "TEST-A"])
        self.assertEqual(plan["env"]["ADB"], self.adb["path"])
        self.assertNotIn("DYLD_INSERT_LIBRARIES", plan["env"])
        self.assertTrue(Path(plan["env"]["SCRCPY_SERVER_PATH"]).is_file())

    def test_old_version_omits_unsupported_flags(self):
        old = {**self.scrcpy, "flags": [], "short_flags": ["-s"]}
        self.assertEqual(pc.build_launch(old, self.adb, "TEST-A")["argv"], [self.scrcpy["path"], "-s", "TEST-A"])
        self.assert_code("unsupported_version", pc.build_launch, old, self.adb, "TEST-A", "uhid")

    def test_bridge_is_optional(self):
        plan = pc.build_launch(self.scrcpy, self.adb, "TEST-A", "auto", {"input_mode": "mac-ime"})
        self.assertEqual(plan["input_mode"], "sdk")
        self.assertTrue(plan["warnings"])
        self.assert_code("optional_bridge_missing", pc.build_launch, self.scrcpy, self.adb, "TEST-A", "mac-ime")

    def test_missing_server(self):
        Path(self.scrcpy["path"]).with_name("scrcpy-server").unlink()
        self.assert_code("server_not_found", pc.build_launch, self.scrcpy, self.adb, "TEST-A")

    def test_same_version_remembers_custom_server_and_options(self):
        original = Path(self.scrcpy["path"]).with_name("scrcpy-server")
        custom = self.root / "separate server"
        original.rename(custom)
        profile = {"tools": {"scrcpy": self.scrcpy}, "launch": {
            "env": {"SCRCPY_SERVER_PATH": str(custom)}, "options": ["--no-audio", "--record"]}}
        plan = pc.build_launch(self.scrcpy, self.adb, "TEST-A", profile=profile)
        self.assertEqual(plan["env"]["SCRCPY_SERVER_PATH"], str(custom))
        self.assertNotIn("--record", plan["argv"])
        self.assert_code("server_not_found", pc.build_launch,
                         {**self.scrcpy, "version": "different"}, self.adb, "TEST-A", "auto", profile)

    def test_unknown_uhid_support_is_not_assumed(self):
        tool = {**self.scrcpy, "keyboard_modes": ["sdk"], "short_flags": ["-s"]}
        self.assert_code("unsupported_version", pc.build_launch, tool, self.adb, "TEST-A", "uhid")
        plan = pc.build_launch(tool, self.adb, "TEST-A", "auto", {"input_mode": "uhid"})
        self.assertEqual(plan["input_mode"], "sdk")

    def test_runtime_memory_excludes_recording_and_window_title(self):
        options = pc.runtime_options('scrcpy --serial A --keyboard=uhid --no-audio --record private.mp4 --window-title secret')
        self.assertEqual(options, {"input_mode": "uhid", "options": ["--no-audio"]})

    def test_other_session_blocks_existing_mirror(self):
        claim = {"known": True, "devices": [{"id": "TEST-A", "claimedBy": {"session": "other"}}]}
        with self.environment([{"pid": 123, "path": self.scrcpy["path"], "serial": "TEST-A"}], claim), \
             patch.object(pc.subprocess, "Popen") as popen:
            self.assert_code("device_in_use", pc.start_scrcpy, self.args("scrcpy"), self.state)
        popen.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_same_task_reuses_mirror_without_success_memory(self):
        claim = {"known": True, "devices": [{"id": "TEST-A", "claimedBy": {"session": "ours"}}]}
        with self.environment([{"pid": 123, "path": self.scrcpy["path"], "serial": "TEST-A"}], claim), \
             patch.object(pc, "probe", return_value=self.scrcpy), patch.object(pc.subprocess, "Popen") as popen:
            result = pc.start_scrcpy(self.args("scrcpy", "--session", "ours"), self.state)
        self.assertEqual(result["action"], "reuse")
        self.assertEqual(result["status"], "pending_verification")
        self.assertFalse((self.state / "devices.json").exists())
        popen.assert_not_called()

    def test_dry_run_does_not_write_or_launch(self):
        with self.environment(), patch.object(pc.subprocess, "Popen") as popen:
            self.assertEqual(pc.start_scrcpy(self.args("scrcpy", "--dry-run"), self.state)["status"], "dry_run")
        self.assertFalse(self.state.exists())
        popen.assert_not_called()

    def test_started_process_is_not_success(self):
        child = Mock(pid=987)
        child.poll.return_value = None
        with self.environment(), patch.object(pc.subprocess, "Popen", return_value=child) as popen, \
             patch.object(pc.time, "sleep"):
            result = pc.start_scrcpy(self.args("scrcpy"), self.state)
        self.assertEqual(result["status"], "pending_verification")
        self.assertTrue(Path(result["receipt"]).exists())
        self.assertFalse((self.state / "devices.json").exists())
        self.assertIsInstance(popen.call_args.args[0], list)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_failed_start_never_writes_memory(self):
        child = Mock(pid=987, returncode=1)
        child.poll.return_value = 1
        with self.environment(), patch.object(pc.subprocess, "Popen", return_value=child), patch.object(pc.time, "sleep"):
            self.assert_code("launch_failed", pc.start_scrcpy, self.args("scrcpy"), self.state)
        self.assertFalse((self.state / "devices.json").exists())

    def test_remember_requires_verification(self):
        self.assert_code("verification_required", pc.remember, self.args("remember", "--serial", "TEST-A"), self.state)
        self.assertFalse(self.state.exists())

    def reused_receipt(self):
        with self.environment([{"pid": 123, "path": self.scrcpy["path"], "serial": "TEST-A"}]), \
             patch.object(pc, "probe", return_value=self.scrcpy):
            return pc.start_scrcpy(self.args("scrcpy"), self.state)["receipt"]

    def test_verified_live_receipt_is_remembered(self):
        receipt = self.reused_receipt()
        with self.environment([{"pid": 123, "path": self.scrcpy["path"], "serial": "TEST-A"}]), \
             patch.object(pc, "probe", return_value=self.scrcpy):
            result = pc.remember(self.args("remember", "--receipt", receipt, "--verified"), self.state)
        self.assertEqual(result["status"], "remembered")
        profile = pc.saved_profile(self.state, "TEST-A")
        self.assertEqual(profile["tools"]["scrcpy"]["path"], self.scrcpy["path"])
        self.assertNotIn("pid", profile)

    def test_exited_or_changed_mirror_is_not_remembered(self):
        receipt = self.reused_receipt()
        with self.environment():
            self.assert_code("verification_expired", pc.remember,
                             self.args("remember", "--receipt", receipt, "--verified"), self.state)
        with self.environment([{"pid": 123, "path": self.scrcpy["path"], "serial": "TEST-A"}]), \
             patch.object(pc, "probe", return_value={**self.scrcpy, "version": "new version"}):
            self.assert_code("verification_expired", pc.remember,
                             self.args("remember", "--receipt", receipt, "--verified"), self.state)

    def test_primary_success_does_not_launch_scrcpy(self):
        with self.environment(), patch.object(pc.subprocess, "Popen") as popen:
            result = pc.remember(self.args("remember", "--serial", "TEST-A", "--verified"), self.state)
        self.assertEqual(result["backend"], "agent-device")
        popen.assert_not_called()

    def test_host_and_device_isolation(self):
        with patch.object(pc, "host_key", return_value="one"):
            pc.save_profile(self.state, "A", {"backend": "agent-device"})
            pc.save_profile(self.state, "B", {"backend": "scrcpy"})
        with patch.object(pc, "host_key", return_value="two"):
            self.assertEqual(pc.saved_profile(self.state, "A"), {})
            pc.save_profile(self.state, "A", {"backend": "scrcpy"})
        with patch.object(pc, "host_key", return_value="one"):
            self.assertEqual(pc.saved_profile(self.state, "A")["backend"], "agent-device")
            self.assertEqual(pc.saved_profile(self.state, "B")["backend"], "scrcpy")

    def test_concurrent_memory_writes_preserve_devices(self):
        errors = []
        def write(serial):
            try:
                pc.save_profile(self.state, serial, {"backend": "scrcpy"})
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=write, args=(str(i),)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(pc.load_state(self.state)["hosts"][pc.host_key()]), 6)

    def test_corrupt_memory_not_overwritten(self):
        self.state.mkdir()
        (self.state / "devices.json").write_text("broken")
        self.assert_code("invalid_local_state", pc.save_profile, self.state, "A", {})
        self.assertEqual((self.state / "devices.json").read_text(), "broken")

    def test_state_outside_git_worktree(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: elsewhere")
        self.assert_code("state_inside_repository", pc.validate_state_dir, repo / "state")
        self.assert_code("state_inside_repository", pc.validate_state_dir, pc.SKILL_ROOT / "state")

    def test_doctor_is_read_only_and_reports_busy(self):
        claim = {"known": True, "devices": [{"id": "TEST-A", "claimedBy": {"session": "other"}}]}
        with self.environment(claims=claim), patch.object(pc.subprocess, "Popen") as popen:
            report = pc.doctor(self.args("doctor"), self.state)
        self.assertFalse(report["ready"])
        self.assertEqual(report["occupation"]["code"], "device_in_use")
        popen.assert_not_called()
        self.assertFalse(self.state.exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture")
    def test_executable_path_with_spaces(self):
        path = self.root / "official scrcpy folder/scrcpy"
        path.parent.mkdir()
        path.write_text(f"#!{sys.executable}\nimport sys\nprint('scrcpy 5.0' if '--version' in sys.argv else 'scrcpy --serial --no-audio')\n")
        path.chmod(0o700)
        report = pc.probe("scrcpy", path)
        self.assertEqual(report["path"], str(path))
        self.assertIn("--serial", report["flags"])

    @unittest.skipUnless(os.name == "nt", "Windows npm batch wrapper")
    def test_windows_batch_wrapper_with_spaces(self):
        path = self.root / "npm install/agent-device.cmd"
        path.parent.mkdir()
        path.write_text('@echo off\nif "%~1"=="--version" (echo 0.21.36) else (echo agent-device --json)\n')
        self.assertEqual(pc.probe("agent-device", path)["version"], "0.21.36")


if __name__ == "__main__":
    unittest.main()
