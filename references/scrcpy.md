# scrcpy 回退与本机环境

## 读取和启动

先确认 agent-device 无法继续且不是其他任务占用、USB 调试授权或连接问题。`doctor` 返回实际路径、版本、帮助中识别的参数、ADB 设备和会话检查结果。脚本选项以它自身的 `--help` 为准。

先执行 `scrcpy --dry-run` 子命令，查看将复用的进程或启动参数。预演和版本探测不算成功启动，不写设备记忆。实际启动只返回待验证回执；确认窗口与设备后再 `remember --verified`。

官方程序可独立启动，无需定制应用。脚本设置匹配的 `ADB` 和 `SCRCPY_SERVER_PATH`，使用当前帮助支持的 `--serial` 或 `-s`。可选参数仅在支持时加入。找不到 server 时可用 `--server-path`，不要混用其他版本下载包的 server。

macOS 支持定制的 `Contents/Helpers/ScrcpyMirror.app` 和普通内置二进制布局，可通过 `--scrcpy-path /实际位置/Scrcpy.app` 指定。多个应用共享 bundle ID 时，使用返回的完整 `app_path`。

本地 `config.json` 可添加特殊候选位置，位于 `doctor` 报告的 `state_dir` 下，排在 PATH 和常见目录之后：

```json
{"candidates": {"scrcpy": ["/my/external/apps/Scrcpy.app"], "adb": ["/my/android/platform-tools/adb"]}}
```

真实本机路径只写本地配置，不修改公开说明。

## 桌面控制

scrcpy 通常没有手机内部的桌面可访问性元素；标题栏元素不是手机按钮。使用宿主桌面工具读取实际截图，在截图坐标系内点击、拖动和输入。

Codex 提供 `mcp__cua_repl` 时，按入口文档绑定已运行应用／窗口；macOS 优先使用完整 `.app` 路径，Windows／Linux 按实际窗口 ID 绑定。根据返回的实时 API 操作，不猜接口。窗口缩放、旋转或移动后重新截图，不把 Android 原始像素坐标当成窗口坐标。每组动作后重新观察预期页面或状态。

## 输入文字

- 默认 SDK 键盘，不保证能直接输入任意中文文本。
- 已有定制 Mac 桥接时，`--input-mode mac-ime` 可使用它。脚本只为该子进程配置现有桥接库，缺失时不安装其他文件。
- `--input-mode uhid` 需要当前版本与设备支持；失败后先核对输入框内容。
- 使用桌面工具粘贴前确认焦点，按实际接口文档操作。文字可能进入手机剪贴板，受保护输入框可能拒绝粘贴，不反复提交。
- `auto` 复用该设备上次成功的输入模式；可选桥接消失时退回 SDK 并说明原因；明确要求 `mac-ime` 却缺少桥接则报错。

## 失败处理

- `unauthorized`：需要在设备上完成 USB 调试授权。
- `offline`／未连接：检查线缆和连接状态，不运行 `kill-server` 干扰共享会话。
- 其他会话占用：等待释放，不借用对方会话名称或使用 scrcpy 绕过。
- 会话检查不可用：脚本报告未知。结合当前任务和已知占用信息确认无冲突；先前已知占用时，不因探测失败而继续。
- 外置磁盘未挂载：包装器可能存在但其依赖缺失。报告实际错误，检查其他有效安装，不更改包装器或自动重装。
- 受保护画面／锁屏黑屏：请用户在设备上处理，不关闭安全键盘或绕过 `FLAG_SECURE`。
- 启动报错或窗口不可用：不执行 `remember --verified`，根据本地错误信息做一次针对性排查。

## 状态目录

- macOS：`~/Library/Application Support/phone-control`
- Windows：`%LOCALAPPDATA%/phone-control`
- Linux：`${XDG_STATE_HOME:-~/.local/state}/phone-control`

`devices.json` 保存成功配置，`config.json` 保存本地候选位置，`runs/` 保存回执和日志。设备记忆按主机散列和设备标识隔离，原子更新并使用短时写锁。脚本拒绝在技能目录或其他 Git 工作树内保存状态。
