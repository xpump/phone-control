---
name: phone-control
description: 控制连接到电脑的手机或平板，优先使用 agent-device，无法继续时回退到 scrcpy 镜像与桌面操作。适用于打开应用、查看界面、点击、滑动、输入及手机控制故障排查；支持发现现有安装并记住验证成功的设备启动配置。
---

# phone control

## 选择控制方式

顺序固定为 **agent-device MCP → agent-device CLI → scrcpy**。复用已安装组件，沿用当前任务授权。切换工具不代表可以重复提交、发送或扩大任务范围。

1. 优先使用可用的 agent-device MCP。已知目标应用时直接 `open` 并指定设备和本任务会话；已有会话继续使用，不反复重开。未知应用标识时按工具要求发现设备和应用，不猜包名。
2. MCP 缺失或通信失败时使用本机 `agent-device` CLI 包装器。不要绕过包装器执行内部 Node 文件，否则可能丢失 SDK、外置磁盘或状态目录配置。
3. 按当前版本的工具说明操作：优先使用新鲜元素引用，其次选择器；无有效元素树时先尝试 agent-device 截图与坐标能力。操作后核对预期结果；界面变化后重新取得有效引用。
4. 按错误提示进行一次针对性恢复；仍无法继续，才使用 scrcpy。超时后的提交、发送等操作必须先查明是否生效，再决定是否重试。
5. **其他活跃任务占用、USB 未授权、离线、锁屏或受保护画面，不是绕过限制的回退理由。** 不抢占会话、不重启共享 ADB、不强停其他进程。仅当 `--session` 确为本任务此前创建的会话时，才可用它通过占用检查。

scrcpy 和辅助脚本面向 Android／ADB 兼容设备。iPhone、iPad 在 agent-device 支持时使用它；失败时说明具体限制，不尝试 scrcpy。

## 发现和检查工具

辅助脚本为 `scripts/phone_control.py`，只依赖 Python 3.10+ 标准库。将 `<skill>` 换成实际技能目录。

```sh
python3 "<skill>/scripts/phone_control.py" doctor
```

显式 `--agent-device-path`、`--scrcpy-path`、`--adb-path` 优先；也支持 `PHONE_CONTROL_AGENT_DEVICE`、`PHONE_CONTROL_SCRCPY`、`PHONE_CONTROL_ADB`。自动发现顺序为 **PATH → 当前系统常见安装目录 → 本地候选位置和已验证的设备记忆**。明确指定的无效路径会报错，不悄悄换用其他程序。全部自动候选失败后，才询问用户安装位置。

脚本读取实际程序的版本和 `--help`，按支持的参数构造命令，每次重新验证。agent-device 具体命令形状不清楚时读取该版本的 `help <命令>`；不要照搬其他版本的参数。`doctor` 用于诊断和环境发现，不替代 agent-device 已知应用直接 `open` 的正常入口。

`doctor` 检查 ADB、设备状态、agent-device 会话占用和 scrcpy 进程。单设备自动选择，多设备明确 `--serial`。后续固定设备与本任务会话。

## scrcpy 回退

需要回退时读取 [references/scrcpy.md](references/scrcpy.md)。优先复用同一设备已有镜像；同名应用有多个副本时，按实际进程和完整应用路径绑定桌面工具。

```sh
python3 "<skill>/scripts/phone_control.py" scrcpy --serial <serial> --session <本任务会话> --dry-run
python3 "<skill>/scripts/phone_control.py" scrcpy --serial <serial> --session <本任务会话>
```

没有本任务 agent-device 会话时省略 `--session`。预演不启动镜像、不写成功记忆。没有定制 Scrcpy.app 时直接运行官方程序，显式选择设备并匹配 server。默认 SDK 键盘；Mac 输入法桥接可选，已安装且需要时才选 `--input-mode mac-ime`。

执行后返回 `pending_verification` 和本地 `receipt`。通过桌面工具读取窗口，核对设备、实时画面及目标界面；不能仅凭 PID、日志或未辨认的截图判定成功。操作在镜像窗口内进行，每次动作后重新观察。

## 自动记住已验证的启动配置

确认界面可用后，**由助手自动执行**，不要求用户手动保存：

```sh
python3 "<skill>/scripts/phone_control.py" remember --receipt <receipt> --verified --session <本任务会话>
```

agent-device 成功后保存实际使用方式：

```sh
python3 "<skill>/scripts/phone_control.py" remember --backend agent-device --transport mcp --serial <serial> --session <本任务会话> --verified
```

CLI 成功时使用 `--transport cli`。`--verified` 是助手已经核对界面的声明，不是脚本自行证明成功，不得提前传入。

记忆按电脑和设备隔离，只记录工具路径、版本及允许的启动参数。路径仍需重新验证，记忆不改变 PATH 优先顺序。文件位于系统用户数据目录的 `phone-control` 子目录；可用 `--state-dir` 或 `PHONE_CONTROL_STATE_DIR` 覆盖，必须放在技能／Git 工作副本之外。

成功记忆不包含应用内容、输入文本、密码或截图。临时回执及日志也只留在本机。任务结束时，只清理本任务创建且不再需要的窗口／进程，不关闭复用的用户窗口。报告实际结果与未验证部分。
