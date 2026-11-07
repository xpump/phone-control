#!/usr/bin/env python3
"""Discover existing phone tools, prepare scrcpy, and remember verified setups.

Python 3.10+, standard library only. GUI verification belongs to the caller.
No command sends input to the phone, installs packages, or stops shared services.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

TOOLS = ("agent-device", "scrcpy", "adb")
SKILL_ROOT = Path(__file__).resolve().parent.parent


class ControlError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details

    def as_dict(self):
        return {"ok": False, "code": self.code, "message": str(self), **self.details}


def now():
    return datetime.now(timezone.utc).isoformat()


def host_key():
    identity = "\0".join((platform.node(), platform.system(), platform.machine()))
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def default_state_dir(system=None, env=None, home=None):
    system, env = system or platform.system(), os.environ if env is None else env
    home = Path(home or Path.home())
    if env.get("PHONE_CONTROL_STATE_DIR"):
        return Path(env["PHONE_CONTROL_STATE_DIR"]).expanduser()
    if system == "Darwin":
        return home / "Library/Application Support/phone-control"
    if system == "Windows":
        return Path(env.get("LOCALAPPDATA", home / "AppData/Local")) / "phone-control"
    return Path(env.get("XDG_STATE_HOME", home / ".local/state")) / "phone-control"


def validate_state_dir(path):
    path = Path(path).expanduser().resolve()
    if path == SKILL_ROOT or SKILL_ROOT in path.parents:
        raise ControlError("state_inside_repository", "状态目录必须位于技能目录之外。")
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise ControlError("state_inside_repository", "状态目录不能位于 Git 工作副本内。")
    return path


def read_json(path, default):
    if not Path(path).exists():
        return default
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ControlError("invalid_local_state", f"无法读取本地 JSON：{path}", reason=str(exc))
    if not isinstance(result, dict):
        raise ControlError("invalid_local_state", f"本地 JSON 必须是对象：{path}")
    return result


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".phone-control-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / ".write-lock"
    deadline = time.monotonic() + 5
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ControlError("state_locked", "本地记忆正在写入；未覆盖现有文件。")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.rmdir()


def load_state(directory):
    result = read_json(directory / "devices.json", {"schema": 1, "hosts": {}})
    if result.get("schema") != 1 or not isinstance(result.get("hosts"), dict):
        raise ControlError("invalid_local_state", "不支持的设备记忆格式。")
    for devices in result["hosts"].values():
        if not isinstance(devices, dict) or not all(isinstance(v, dict) for v in devices.values()):
            raise ControlError("invalid_local_state", "设备记忆记录格式错误。")
    return result


def saved_profile(directory, serial):
    return load_state(directory)["hosts"].get(host_key(), {}).get(serial, {})


def save_profile(directory, serial, profile):
    with state_lock(directory):
        data = load_state(directory)
        data["hosts"].setdefault(host_key(), {})[serial] = {**profile, "verified_at": now()}
        atomic_json(directory / "devices.json", data)


def run(argv, timeout=15, env=None):
    """Never invoke a shell for native executables; npm's Windows .cmd is special."""
    argv = [str(a) for a in argv]
    child_env = dict(os.environ if env is None else env)
    if os.name == "nt" and Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
        # Only our fixed, read-only probe arguments reach batch launchers.
        if any(not re.fullmatch(r"[A-Za-z0-9_.:/=-]+", a) for a in argv[1:]):
            raise ControlError("unsupported_batch_arguments", "Windows 包装器探测参数无效。")
        child_env["PHONE_CONTROL_BATCH_PATH"] = argv[0]
        command = '""%PHONE_CONTROL_BATCH_PATH%" ' + " ".join('"' + a + '"' for a in argv[1:]) + '"'
        argv = [child_env.get("COMSPEC", "cmd.exe"), "/d", "/v:off", "/s", "/c", command]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, env=child_env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError("command_unavailable", "程序无法运行或探测超时。", reason=str(exc))
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def common_candidates(tool, system=None, env=None, home=None):
    """Bounded directory globs; no recursive whole-drive search or installation."""
    system, env = system or platform.system(), os.environ if env is None else env
    home = Path(home or Path.home())
    patterns = []
    if system == "Windows":
        suffix = ".cmd" if tool == "agent-device" else ".exe"
        local = Path(env.get("LOCALAPPDATA", home / "AppData/Local"))
        roaming = Path(env.get("APPDATA", home / "AppData/Roaming"))
        program = Path(env.get("ProgramFiles", "C:/Program Files"))
        patterns += [str(roaming / "npm" / (tool + suffix)),
                     str(home / "scoop/shims" / (tool + suffix)),
                     str(home / "scoop/apps" / tool / "current" / (tool + suffix)),
                     str(Path(env.get("ChocolateyInstall", "C:/ProgramData/chocolatey")) / "bin" / (tool + suffix)),
                     str(local / "Microsoft/WinGet/Links" / (tool + suffix)),
                     str(local / "Microsoft/WinGet/Packages" / f"*{tool}*" / (tool + suffix)),
                     str(local / "Microsoft/WinGet/Packages" / f"*{tool}*" / "*" / (tool + suffix)),
                     str(program / tool / (tool + suffix)),
                     str(home / "Downloads" / f"{tool}*" / (tool + suffix))]
    else:
        patterns += [str(p / tool) for p in (home / ".local/bin", home / ".npm-global/bin",
                                            home / ".local/share/pnpm", Path("/usr/local/bin"),
                                            Path("/usr/bin"), Path("/snap/bin"))]
        patterns += [str(home / ".nvm/versions/node/*/bin" / tool),
                     str(home / "Downloads" / f"{tool}*" / tool),
                     str(Path("/opt") / f"{tool}*" / tool)]
        if system == "Darwin":
            patterns.insert(0, str(Path("/opt/homebrew/bin") / tool))
            if tool == "scrcpy":
                patterns += ["/Applications/[Ss]crcpy.app",
                             str(home / "Applications/[Ss]crcpy.app"),
                             str(home / "Downloads/scrcpy*/[Ss]crcpy.app")]
    if tool == "adb":
        for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            if env.get(key):
                patterns.insert(0, str(Path(env[key]) / "platform-tools" / ("adb.exe" if system == "Windows" else "adb")))
        sdk = (home / "Library/Android/sdk" if system == "Darwin" else
               Path(env.get("LOCALAPPDATA", home / "AppData/Local")) / "Android/Sdk" if system == "Windows"
               else home / "Android/Sdk")
        patterns += [str(sdk / "platform-tools" / ("adb.exe" if system == "Windows" else "adb"))]
        # scrcpy official archives and macOS launchers also carry ADB.
        patterns += [str(home / "Downloads/scrcpy*" / ("adb.exe" if system == "Windows" else "adb"))]
    results = []
    for pattern in patterns:
        results.extend(sorted(glob.glob(pattern)) if glob.has_magic(pattern) else [pattern])
    return results


def executable_path(candidate, tool):
    path = Path(candidate).expanduser().absolute()
    if path.is_dir():
        suffix = ".exe" if os.name == "nt" and tool != "agent-device" else ".cmd" if os.name == "nt" else ""
        options = [path / (tool + suffix)]
        if tool == "scrcpy" and path.suffix.lower() == ".app":
            options = [path / "Contents/Helpers/ScrcpyMirror.app/Contents/MacOS/scrcpy",
                       path / "Contents/Resources/bin/scrcpy",
                       path / "Contents/MacOS/scrcpy"]
        elif path.suffix.lower() == ".app":
            options = [path / "Contents/Resources/bin" / tool]
        path = next((p for p in options if p.is_file()), path)
    if not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        raise ControlError("invalid_tool_path", f"不是可执行的 {tool} 程序：{path}")
    # Keep the wrapper/symlink path, since it may encode the user's environment.
    return path


def fingerprint(path):
    stat = Path(path).stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def app_paths(path):
    apps = [p for p in Path(path).resolve().parents if p.suffix.lower() == ".app"]
    return {"app_path": str(apps[0]) if apps else None,
            "bundle_root": str(apps[-1]) if apps else None}


def probe(tool, candidate):
    path = executable_path(candidate, tool)
    version_args = ["version"] if tool == "adb" else ["--version"]
    code, version, error = run([path, *version_args])
    if code:
        raise ControlError("tool_probe_failed", f"{tool} 版本探测失败。", reason=(error or version)[:2000])
    code, help_text, error = run([path, "--help"])
    help_text = "\n".join(filter(None, (help_text, error)))
    if not help_text or code:
        raise ControlError("tool_probe_failed", f"{tool} --help 探测失败。", reason=help_text[:2000])
    identity = {"adb": "android debug bridge", "scrcpy": "scrcpy", "agent-device": "agent-device"}[tool]
    if identity not in (version + "\n" + help_text).lower():
        raise ControlError("wrong_program", f"路径没有返回可识别的 {tool} 帮助。")
    flags = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", help_text)))
    short_flags = sorted(set(re.findall(r"(?<![\w-])-[A-Za-z](?![\w-])", help_text)))
    keyboard_help = re.search(r"(?m)^[ \t]*--keyboard[^\n]*(?:\n(?![ \t]*--[a-z]|[ \t]*-[A-Za-z](?:,|\s))[^\n]*)*", help_text)
    modes = sorted(set(re.findall(r"\b(?:sdk|uhid|aoa)\b", keyboard_help.group() if keyboard_help else "")))
    return {"path": str(path), "version": version.splitlines()[0], "fingerprint": fingerprint(path),
            "flags": flags, "short_flags": short_flags, "keyboard_modes": modes, "help_checked": True,
            **(app_paths(path) if tool == "scrcpy" else {})}


def remembered_candidates(directory, tool, serial=None):
    config = read_json(directory / "config.json", {})
    candidates = config.get("candidates", {})
    if not isinstance(candidates, dict):
        raise ControlError("invalid_local_state", "config.json 的 candidates 必须是对象。")
    values = candidates.get(tool, [])
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ControlError("invalid_local_state", "本地候选位置必须是字符串列表。")
    records = load_state(directory)["hosts"].get(host_key(), {})
    profiles = ([records[serial]] if serial in records else []) if serial else []
    profiles += [p for key, p in records.items() if key != serial]
    for profile in profiles:
        tools = profile.get("tools", {})
        if isinstance(tools, dict) and isinstance(tools.get(tool), dict):
            value = tools[tool].get("path")
            if isinstance(value, str):
                values = [*values, value]
    return values


def discover(tool, directory, explicit=None, serial=None):
    if explicit:
        try:
            return {**probe(tool, explicit), "source": "explicit"}
        except ControlError as exc:
            raise ControlError("explicit_path_invalid", str(exc), tool=tool, reason=exc.details)
    candidates = []
    found = shutil.which(tool)
    if found:
        candidates.append(("PATH", found))
    candidates += [("common", p) for p in common_candidates(tool)]
    candidates += [("remembered", p) for p in remembered_candidates(directory, tool, serial)]
    seen, failures = set(), []
    for source, candidate in candidates:
        candidate = str(Path(candidate).expanduser().absolute())
        if candidate in seen:
            continue
        seen.add(candidate)
        if not Path(candidate).exists():
            continue
        try:
            return {**probe(tool, candidate), "source": source}
        except ControlError as exc:
            failures.append({"path": candidate, "code": exc.code, "message": str(exc), **exc.details})
    raise ControlError("tool_not_found", f"未发现可运行的 {tool}；已检查 PATH、常见目录及本地记忆。",
                       tool=tool, attempts=failures, next_step=f"请提供 {tool} 安装位置，使用显式路径参数。")


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] in ("List", "*") or line.startswith("adb "):
            continue
        if fields[1] not in ("device", "offline", "unauthorized", "no"):
            continue
        values = dict(p.split(":", 1) for p in fields[2:] if ":" in p)
        devices.append({"serial": fields[0], "state": fields[1], "model": values.get("model")})
    return devices


def adb_devices(adb):
    code, out, error = run([adb["path"], "devices", "-l"])
    if code:
        raise ControlError("adb_failed", "ADB 设备检查失败。", reason=(error or out)[:2000])
    return parse_devices(out)


def select_device(devices, serial=None):
    if serial:
        selected = next((d for d in devices if d["serial"] == serial), None)
        if selected is None:
            raise ControlError("device_not_found", "指定设备未连接。", serial=serial, devices=devices)
    else:
        if not devices:
            raise ControlError("no_device", "未发现设备；检查连接和 USB 调试。")
        if len(devices) != 1:
            raise ControlError("device_selection_required", "发现多个设备，请明确 --serial。", devices=devices)
        selected = devices[0]
    if selected["state"] != "device":
        raise ControlError("device_not_ready", "设备未就绪；请检查调试授权或连接，不切换工具绕过。",
                           device=selected)
    return selected


def claim_report(agent):
    if not agent:
        return {"known": False, "message": "agent-device 不可用，无法检查会话占用。"}
    try:
        # Confirm this installed CLI exposes device discovery before calling it.
        code, out, err = run([agent["path"], "help", "devices"])
        if code or "devices" not in (out + err):
            return {"known": False, "message": "当前版本未提供可识别的设备发现帮助。"}
        code, out, err = run([agent["path"], "devices", "--platform", "android", "--json"], timeout=30)
        if code:
            return {"known": False, "message": (err or out)[:2000]}
        data = json.loads(out)
        devices = data.get("data", {}).get("devices")
        if data.get("success") is not True or not isinstance(devices, list):
            return {"known": False, "message": "无法识别 agent-device 设备响应。"}
        return {"known": True, "devices": devices}
    except (ControlError, ValueError, TypeError, AttributeError) as exc:
        return {"known": False, "message": str(exc)}


def check_claim(report, serial, session=None):
    if not report.get("known"):
        return [report.get("message", "会话占用未知。") + " 先前已知占用时不得继续。"]
    device = next((d for d in report["devices"] if d.get("id") == serial), {})
    claim = device.get("claimedBy")
    if claim and (not session or claim.get("session") != session):
        raise ControlError("device_in_use", "设备由其他会话占用，未启动或接管镜像。", claimed_by=claim)
    return []


def parse_serial(arguments):
    # adb serials contain no whitespace. Support both POSIX and Windows quoting.
    match = re.search(r'(?:^|\s)(?:--serial(?:=|\s+)|-s\s+)(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))', arguments)
    return next((v for v in match.groups() if v is not None), None) if match else None


def runtime_options(command):
    """Persist only control options, never arbitrary command lines or titles."""
    match = re.search(r"(?:^|\s)--keyboard(?:=|\s+)(sdk|uhid|aoa)(?:\s|$)", command)
    mode = match.group(1) if match else "uhid" if re.search(r"(?:^|\s)-K(?:\s|$)", command) else "sdk"
    options = []
    for flag in ("--no-audio", "--stay-awake", "--turn-screen-off", "--always-on-top"):
        if re.search(r"(?:^|\s)" + re.escape(flag) + r"(?:\s|$)", command):
            options.append(flag)
    return {"input_mode": mode, "options": options}


def running_scrcpy():
    result = []
    try:
        if os.name == "nt":
            command = ("Get-CimInstance Win32_Process -Filter \"Name='scrcpy.exe'\" | "
                       "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
            code, out, _ = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command])
            if code:
                raise ControlError("process_inspection_failed", "无法检查现有 scrcpy 进程。")
            records = json.loads(out) if out else []
            records = records if isinstance(records, list) else [records]
            for item in records:
                if item.get("ExecutablePath"):
                    result.append({"pid": int(item["ProcessId"]), "path": item["ExecutablePath"],
                                   "serial": parse_serial(item.get("CommandLine") or ""),
                                   **runtime_options(item.get("CommandLine") or "")})
        else:
            code, out, _ = run(["ps", "-axww", "-o", "pid=,comm="])
            if code:
                raise ControlError("process_inspection_failed", "无法检查现有 scrcpy 进程。")
            for line in out.splitlines():
                fields = line.strip().split(None, 1)
                if len(fields) != 2 or Path(fields[1]).name != "scrcpy":
                    continue
                pid, path = int(fields[0]), fields[1]
                code, command, _ = run(["ps", "-p", str(pid), "-ww", "-o", "command="])
                if not code:
                    result.append({"pid": pid, "path": path, "serial": parse_serial(command),
                                   **runtime_options(command)})
    except (ValueError, TypeError, OSError) as exc:
        raise ControlError("process_inspection_failed", "无法解析 scrcpy 进程信息。", reason=str(exc))
    return result


def server_path(scrcpy, explicit=None):
    if explicit:
        path = Path(explicit).expanduser().absolute()
        if not path.is_file():
            raise ControlError("server_not_found", "指定的 scrcpy-server 不存在。", path=str(path))
        return str(path)
    binary = Path(scrcpy["path"]).resolve()
    paths = [binary.parent / "scrcpy-server",
             binary.parent.parent / "share/scrcpy/scrcpy-server"]
    if scrcpy.get("bundle_root"):
        paths.insert(0, Path(scrcpy["bundle_root"]) / "Contents/Resources/bin/scrcpy-server")
    if binary.parent in (Path("/usr/bin"), Path("/usr/local/bin")):
        paths.append(binary.parent.parent / "share/scrcpy/scrcpy-server")
    for path in paths:
        if path.is_file():
            return str(path)
    raise ControlError("server_not_found", "无法定位同套安装的 scrcpy-server，请指定 --server-path。",
                       checked=[str(p) for p in paths])


def bridge_path(scrcpy):
    root = scrcpy.get("bundle_root")
    if root:
        candidate = Path(root) / "Contents/Helpers/ScrcpyMirror.app/Contents/Frameworks/MirrorMenuBridge.dylib"
        if candidate.is_file():
            return candidate
    return None


def build_launch(scrcpy, adb, serial, mode="auto", profile=None, server=None):
    flags = set(scrcpy["flags"])
    if "--serial" in flags:
        args = ["--serial", serial]
    elif "-s" in scrcpy["short_flags"]:
        args = ["-s", serial]
    else:
        raise ControlError("unsupported_version", "实际 scrcpy 帮助未列出设备选择参数。")
    warnings = []
    effective = (profile or {}).get("input_mode", "sdk") if mode == "auto" else mode
    if effective not in ("sdk", "uhid", "mac-ime"):
        effective = "sdk"
    bridge = bridge_path(scrcpy)
    if effective == "mac-ime" and bridge is None:
        if mode != "auto":
            raise ControlError("optional_bridge_missing", "未安装可选 Mac 输入法桥接；可改用 sdk。")
        warnings.append("历史 Mac 输入法桥接已不可用，使用 SDK 键盘。")
        effective = "sdk"
    uhid_supported = ("--keyboard" in flags and "uhid" in scrcpy.get("keyboard_modes", [])) or "-K" in scrcpy["short_flags"]
    if effective == "uhid" and not uhid_supported:
        if mode != "auto":
            raise ControlError("unsupported_version", "当前版本帮助没有确认 UHID 键盘支持。")
        warnings.append("当前版本未确认 UHID 支持，使用 SDK 键盘。")
        effective = "sdk"
    if "--keyboard" in flags and (effective != "uhid" or "uhid" in scrcpy.get("keyboard_modes", [])):
        args.append("--keyboard=" + ("uhid" if effective == "uhid" else "sdk"))
    elif effective == "uhid":
        if "-K" not in scrcpy["short_flags"]:
            raise ControlError("unsupported_version", "实际帮助未提供所需键盘参数。")
        args.append("-K")
    saved_options = (profile or {}).get("launch", {}).get("options", ["--no-audio"])
    options = [f for f in saved_options if f in flags and f in
               ("--no-audio", "--stay-awake", "--turn-screen-off", "--always-on-top")]
    args.extend(options)
    if "--window-title" in flags:
        args += ["--window-title", "phone-control " + serial]
    try:
        selected_server = server_path(scrcpy, server)
    except ControlError:
        previous = (profile or {}).get("tools", {}).get("scrcpy", {})
        remembered_server = (profile or {}).get("launch", {}).get("env", {}).get("SCRCPY_SERVER_PATH")
        if (server or not remembered_server or previous.get("path") != scrcpy["path"]
                or previous.get("version") != scrcpy["version"]):
            raise
        selected_server = server_path(scrcpy, remembered_server)
    env = {"ADB": adb["path"], "SCRCPY_SERVER_PATH": selected_server}
    if effective == "mac-ime":
        env["DYLD_INSERT_LIBRARIES"] = str(bridge)
        env["SCRCPY_MAC_IME_BRIDGE"] = "1"
    return {"argv": [scrcpy["path"], *args], "env": env, "input_mode": effective,
            "options": options, "warnings": warnings}


def get_tools(args, directory):
    found, errors = {}, {}
    for tool in TOOLS:
        explicit = getattr(args, tool.replace("-", "_") + "_path")
        try:
            found[tool] = discover(tool, directory, explicit, args.serial)
        except ControlError as exc:
            errors[tool] = exc.as_dict()
    return found, errors


def require_tool(tools, errors, name):
    if name not in tools:
        error = errors[name]
        raise ControlError(error["code"], error["message"],
                           **{k: v for k, v in error.items() if k not in ("ok", "code", "message")})
    return tools[name]


def doctor(args, directory):
    tools, errors = get_tools(args, directory)
    devices, selection, selection_error = [], None, None
    if "adb" in tools:
        try:
            devices = adb_devices(tools["adb"])
            selection = select_device(devices, args.serial)
        except ControlError as exc:
            selection_error = exc.as_dict()
    claims = claim_report(tools.get("agent-device"))
    try:
        mirrors = running_scrcpy()
        process_error = None
    except ControlError as exc:
        mirrors, process_error = [], exc.as_dict()
    occupation = None
    if selection:
        try:
            check_claim(claims, selection["serial"], args.session)
        except ControlError as exc:
            occupation = exc.as_dict()
    ready = bool(selection and not occupation and ("agent-device" in tools or "scrcpy" in tools))
    return {"ok": True, "ready": ready, "state_dir": str(directory), "tools": tools,
            "tool_errors": errors, "devices": devices, "selected_device": selection,
            "device_error": selection_error, "claims": claims, "occupation": occupation,
            "mirrors": mirrors, "process_error": process_error}


def prepare_scrcpy(args, directory):
    tools, errors = get_tools(args, directory)
    adb = require_tool(tools, errors, "adb")
    device = select_device(adb_devices(adb), args.serial)
    serial = device["serial"]
    warnings = check_claim(claim_report(tools.get("agent-device")), serial, args.session)
    mirrors = running_scrcpy()
    matching = [p for p in mirrors if p["serial"] == serial]
    if len(matching) > 1:
        raise ControlError("multiple_mirrors", "同一设备存在多个镜像，请先通过桌面窗口确定目标。",
                           mirrors=matching)
    if matching:
        mirror = matching[0]
        if args.scrcpy_path:
            explicit = require_tool(tools, errors, "scrcpy")
            if Path(explicit["path"]).resolve() != Path(mirror["path"]).resolve():
                raise ControlError("mirror_path_conflict", "现有镜像与显式指定程序不同，未接管或重复启动。")
        actual = probe("scrcpy", mirror["path"])
        observed_mode = mirror.get("input_mode", "sdk")
        if args.input_mode != "auto" and args.input_mode != observed_mode:
            raise ControlError("mirror_settings_conflict", "现有镜像输入模式与指定模式不同，未重启用户窗口。")
        if actual.get("bundle_root") and bridge_path(actual):
            warnings.append("复用现有镜像：可以确认键盘参数，但未探测其可选输入法桥接环境。")
        return {"action": "reuse", "serial": serial, "device": device, "pid": mirror["pid"],
                "tools": {"scrcpy": actual, "adb": adb},
                "input_mode": observed_mode, "options": mirror.get("options", []), "warnings": warnings,
                "app_path": actual.get("app_path")}
    if any(p["serial"] is None for p in mirrors):
        raise ControlError("unidentified_mirror", "已有镜像未显式标明设备，请先通过窗口确认，避免重复启动。",
                           mirrors=mirrors)
    scrcpy = require_tool(tools, errors, "scrcpy")
    plan = build_launch(scrcpy, adb, serial, args.input_mode, saved_profile(directory, serial), args.server_path)
    return {"action": "start", "serial": serial, "device": device,
            "tools": {"scrcpy": scrcpy, "adb": adb}, "app_path": scrcpy.get("app_path"),
            **plan, "warnings": [*warnings, *plan["warnings"]]}


def start_scrcpy(args, directory):
    plan = prepare_scrcpy(args, directory)
    if args.dry_run:
        return {"ok": True, "status": "dry_run", **plan}
    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = runs / (uuid.uuid4().hex + ".json")
    if plan["action"] == "start":
        log = receipt.with_suffix(".log")
        env = {**os.environ, **plan["env"]}
        # Optional bridge belongs only to an explicitly selected bridge launch.
        if plan["input_mode"] != "mac-ime":
            env.pop("SCRCPY_MAC_IME_BRIDGE", None)
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        try:
            with open(log, "xb") as output:
                if os.name != "nt":
                    os.chmod(log, 0o600)
                process = subprocess.Popen(plan["argv"], stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, env=env, **kwargs)
            time.sleep(0.4)
            if process.poll() is not None:
                raise ControlError("launch_failed", "scrcpy 已退出，未保存成功记忆。", log=str(log),
                                   exit_code=process.returncode)
        except OSError as exc:
            raise ControlError("launch_failed", "scrcpy 无法启动。", reason=str(exc), log=str(log))
        plan.update(pid=process.pid, log=str(log))
    data = {"schema": 1, "host": host_key(), "created_at": now(), "backend": "scrcpy", **plan}
    atomic_json(receipt, data)
    return {"ok": True, "status": "pending_verification", "receipt": str(receipt), **plan,
            "next_step": "确认设备、实时镜像和目标界面后，再执行 remember --receipt ... --verified。"}


def compact_tool(info):
    return {k: info[k] for k in ("path", "version", "fingerprint") if k in info}


def remember(args, directory):
    if not args.verified:
        raise ControlError("verification_required", "尚未声明界面验证成功，不保存设备记忆。")
    tools, errors = get_tools(args, directory)
    adb = require_tool(tools, errors, "adb")
    if args.receipt:
        path = Path(args.receipt).expanduser().resolve()
        runs = (directory / "runs").resolve()
        if path.parent != runs or not path.is_file():
            raise ControlError("invalid_receipt", "只能读取当前状态目录 runs/ 中的启动回执。")
        receipt = read_json(path, {})
        if receipt.get("schema") != 1 or receipt.get("host") != host_key() or receipt.get("backend") != "scrcpy":
            raise ControlError("invalid_receipt", "回执不属于本机或格式无效。")
        serial = receipt.get("serial")
        if not isinstance(serial, str) or (args.serial and args.serial != serial):
            raise ControlError("invalid_receipt", "回执与指定设备不一致。")
        select_device(adb_devices(adb), serial)
        check_claim(claim_report(tools.get("agent-device")), serial, args.session)
        mirror = next((p for p in running_scrcpy()
                       if p["pid"] == receipt.get("pid") and p["serial"] == serial), None)
        if not mirror:
            raise ControlError("verification_expired", "回执中的镜像已退出或设备不匹配，不保存成功记忆。")
        previous = receipt.get("tools", {}).get("scrcpy", {})
        current = probe("scrcpy", mirror["path"])
        if (Path(current["path"]).resolve() != Path(previous.get("path", "")).resolve()
                or current["fingerprint"] != previous.get("fingerprint")
                or current["version"] != previous.get("version")):
            raise ControlError("verification_expired", "程序在启动后发生变化，请重新验证。")
        old = saved_profile(directory, serial)
        profile = {"backend": "scrcpy", "transport": "desktop", "input_mode": receipt.get("input_mode", "sdk"),
                   "tools": {**old.get("tools", {}), "scrcpy": compact_tool(current), "adb": compact_tool(adb)},
                   "launch": {"mode": receipt["action"], "options": receipt.get("options", [])}}
        if receipt["action"] == "start":
            # argv/env were generated by this helper; never persist arbitrary CLI strings.
            profile["launch"].update(argv=receipt["argv"], env=receipt["env"])
    else:
        if args.backend != "agent-device":
            raise ControlError("receipt_required", "scrcpy 记忆需要经过验证的启动回执。")
        agent = require_tool(tools, errors, "agent-device")
        device = select_device(adb_devices(adb), args.serial)
        serial = device["serial"]
        check_claim(claim_report(agent), serial, args.session)
        profile = {"backend": "agent-device", "transport": args.transport,
                   "tools": {"agent-device": compact_tool(agent), "adb": compact_tool(adb)}}
        old = saved_profile(directory, serial)
        # Keep successful scrcpy discovery/input choices across agent-device tasks.
        if old.get("input_mode"):
            profile["input_mode"] = old["input_mode"]
        profile["tools"] = {**old.get("tools", {}), **profile["tools"]}
        if old.get("launch"):
            profile["launch"] = old["launch"]
    save_profile(directory, serial, profile)
    return {"ok": True, "status": "remembered", "serial": serial,
            "state_file": str(directory / "devices.json"), "backend": profile["backend"]}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("doctor", "scrcpy", "remember"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", type=Path, default=default_state_dir())
        sub.add_argument("--serial")
        sub.add_argument("--session", help="Only the session created by this task; never borrow another owner's.")
        for tool in TOOLS:
            key = "PHONE_CONTROL_" + tool.upper().replace("-", "_")
            sub.add_argument("--" + tool + "-path", default=os.environ.get(key))
        if name == "scrcpy":
            sub.add_argument("--server-path", default=os.environ.get("SCRCPY_SERVER_PATH"))
            sub.add_argument("--input-mode", choices=("auto", "sdk", "mac-ime", "uhid"), default="auto")
            sub.add_argument("--dry-run", action="store_true")
        if name == "remember":
            sub.add_argument("--receipt")
            sub.add_argument("--verified", action="store_true")
            sub.add_argument("--backend", choices=("agent-device", "scrcpy"), default="agent-device")
            sub.add_argument("--transport", choices=("mcp", "cli"), default="mcp")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        directory = validate_state_dir(args.state_dir)
        result = {"doctor": doctor, "scrcpy": start_scrcpy, "remember": remember}[args.command](args, directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ControlError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "code": "filesystem_error", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
