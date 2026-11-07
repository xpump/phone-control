# phone control

供 Codex 等技能宿主使用的手机、平板控制技能。优先使用 **agent-device MCP → agent-device CLI → scrcpy**，复用本机安装，不包含工具二进制或 Android SDK。

需要 Python 3.10+，以及已安装的 [agent-device](https://github.com/callstack/agent-device)、[scrcpy](https://github.com/Genymobile/scrcpy) 和 ADB。scrcpy 界面操作还需要宿主提供桌面控制能力。辅助脚本面向 Android／ADB 兼容设备；iOS 使用 agent-device 本身支持的能力。

## 安装与调用

将仓库克隆到技能目录，例如 Codex：

```sh
git clone https://github.com/xpump/phone-control.git ~/.codex/skills/phone-control
```

在加载技能后的会话中说“用手机打开某应用”，或明确调用 `$phone-control`。如果当前会话的技能列表尚未刷新，可在新会话中调用，也可直接指定本目录的 `SKILL.md`。

```sh
python3 ~/.codex/skills/phone-control/scripts/phone_control.py doctor
python3 ~/.codex/skills/phone-control/scripts/phone_control.py scrcpy --serial DEVICE_SERIAL --dry-run
```

Windows 使用可用的 `python` 或 `py -3`，将技能目录替换为实际路径。各子命令都有 `--help`。普通控制优先由 agent-device 完成；`scrcpy` 子命令用于需要的回退，脚本不会自行操作目标应用或判断业务任务完成。

## 发现、回退和记忆

- 显式工具路径优先；自动发现依次检查 PATH、系统常见目录、本地候选位置和成功记忆。每次读取实际版本及帮助，避免沿用过期参数。
- 支持官方 scrcpy 直接启动、匹配 ADB 和 server、识别已有镜像。定制 Mac 应用与输入法桥接均为可选能力。
- 检查连接、调试授权和已知 agent-device 占用，其他会话占用时拒绝接管，不强停进程或重启共享 ADB。
- 启动只生成待验证回执；助手确认设备、实时画面及目标界面后，自动执行 `remember --receipt ... --verified`。只看到进程存活不能保存成功记忆。
- 主机与设备分别记录，记忆原子更新。文件放在系统用户数据目录，不含手机画面、输入文本或密码，不进入 Git 仓库。

默认状态目录：macOS 为 `~/Library/Application Support/phone-control`，Windows 为 `%LOCALAPPDATA%/phone-control`，Linux 为 `${XDG_STATE_HOME:-~/.local/state}/phone-control`。`--state-dir` 可覆盖，但不能指向技能或 Git 工作副本内部。

完整规则见 [SKILL.md](SKILL.md)，回退、中文输入及本地配置见 [references/scrcpy.md](references/scrcpy.md)。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时目录和模拟设备，覆盖发现顺序、版本变化、官方启动参数、占用阻断、镜像复用、待验证回执、记忆隔离和并发写入，不需要连接手机。GitHub Actions 在 macOS、Windows、Linux 上运行相同测试。

测试不等同于真实手机的点击、输入或中文输入法验收。真实操作仍需设备可用、授权正常、桌面工具可用，并核对界面。本机 macOS 已做真实环境只读检查；其他平台的硬件控制需在目标环境验证。
