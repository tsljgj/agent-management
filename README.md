# agent-management (`agentman`)

在一台机器上管理多个 **Claude Code** / **Codex** 账号，并实时查看每个账号的订阅用量：5 小时窗口、周窗口、Opus/Sonnet 周额度、extra usage 和 credits。

- 零依赖：纯 Python 3.11+ 标准库
- 支持 Linux / macOS / Windows。macOS 会读取 Claude Code 存在 Keychain 里的凭据
- 默认**只读**：只读取 CLI 已保存的 token，不会去刷新，也不会改动你的登录状态
- 提供 CLI 表格、`watch` 刷新模式、JSON 输出，以及本地 Web 仪表盘
- **Windows 托盘程序**：常驻在任务栏的“显示隐藏的图标”里，点开是一个赛博朋克风格的控制台

<p align="center"><img src="docs/console.png" width="380" alt="agentman console"></p>

## Windows 托盘控制台

### 安装

**方式 A：下载 exe（不需要 Python）**

在 GitHub → Actions → `windows-tray` 中打开最新一次运行，下载 artifact `agentman-tray-windows`，得到 `agentman-tray.exe`。打过 `v*` tag 的版本也会附在 Releases 里。

双击运行后，程序会常驻在任务栏右下角的 `^`（显示隐藏的图标）里。可以把图标拖到任务栏上，让它一直显示。

**方式 B：从源码运行**

```powershell
pip install ".[tray]"
agentman tray              # 或 pythonw -m agentman tray（不弹出黑窗口）
.\scripts\build_windows.ps1   # 自己打包 dist\agentman-tray.exe
```

### 托盘图标

- 图标是一个圆环，按所有账号中**最高的 5h 用量**填充并变色：绿色低于 70%，黄色 70–90%，红色 90% 以上。有账号出错时变成品红色。
- 鼠标悬停显示每个账号的 5h 用量。
- 用量越过 80% 或 95% 时弹出 Windows 通知；窗口重置后也会通知一次。
- 右键菜单：
  - Open console
  - Jack in ▸：选一个账号，直接开一个已登录该账号的终端
  - Refresh now
  - Open in browser
  - Start with Windows：开机自启，写入 HKCU Run 注册表项，不需要管理员权限
  - Quit

### 控制台

左键点击托盘图标，会在屏幕右下角弹出一个无边框窗口。它用的是 Windows 自带的 Edge WebView2。

- 顶部状态条：在线节点数、最高 5h 用量、Claude 和 Codex 各自剩余额度最多的账号。
- 每个账号一张卡片：分段霓虹进度条和每秒刷新的重置倒计时（`RESET T-02:13:04`）。卡片上的 **JACK IN** 按钮会开一个 `cmd` 窗口，以该账号运行 `claude` 或 `codex`。
- 底部是日志区和命令行。支持 Tab 补全、↑/↓ 历史，Esc 收回托盘：

```
help                      列出命令
ls                        列出所有账号及用量
refresh | r               立即同步
best [claude|codex]       剩余 5h 额度最多的账号
jack <name>               开一个以 <name> 身份运行 claude/codex 的终端
login <name>              打开 <name> 的登录流程
add <claude|codex> <name> 新增一个隔离的账号
import                    导入默认的 ~/.claude 和 ~/.codex
rm <name> -y              取消登记（登录文件保留）
rain [on|off]             数字雨背景开关
clear / hide
```

第一次使用：在控制台里输入 `import`，再用 `add claude work2` 加账号，最后 `login work2` 登录。

> 控制台只监听 `127.0.0.1`。每次启动会生成一个随机 token，并校验 Host 头，所以其他网页无法借用本地端口去开终端。
> Windows 11 自带 WebView2；如果旧版 Windows 10 上窗口打不开，需要安装 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

## 原理

每个账号对应一个独立的配置目录：

| Provider | 目录环境变量 | 凭据文件 | 用量接口 |
|---|---|---|---|
| Claude | `CLAUDE_CONFIG_DIR` | `.credentials.json`（macOS 存在 Keychain: `Claude Code-credentials-<hash>`） | `GET https://api.anthropic.com/api/oauth/usage` |
| Codex | `CODEX_HOME` | `auth.json` | `GET https://chatgpt.com/backend-api/wham/usage` |

这两个接口就是 `claude` 的 `/usage` 和 `codex` 的 `/status` 背后调用的接口，返回的百分比和官方显示的一致。

## 安装

```bash
git clone https://github.com/tsljgj/agent-management && cd agent-management
pip install -e .          # 或者直接 python -m agentman ...
```

## 使用

```bash
# 1. 把当前默认登录的 ~/.claude 和 ~/.codex 导入为账号
agentman import

# 2. 添加更多账号（比如 4 个 Claude 账号），每个账号有独立目录，再分别登录
agentman add claude work   --note work@company.com
agentman add claude alt1
agentman add codex  plus
agentman login work        # 以 CLAUDE_CONFIG_DIR=~/.agentman/accounts/claude-work 运行 claude，然后执行 /login
agentman login plus        # 以 CODEX_HOME=... 运行 codex login

# 3. 查看用量
agentman usage             # 所有账号
agentman usage work alt1   # 指定账号
agentman usage --json      # 输出 JSON，方便接入脚本、状态栏或告警
agentman watch -n 120      # 每 2 分钟刷新一次
agentman serve             # 在浏览器里打开同一个控制台 http://127.0.0.1:8765
agentman tray              # 托盘程序（需要 pip install ".[tray]"）

# 4. 用指定账号干活
agentman run work                     # = CLAUDE_CONFIG_DIR=... claude
agentman run work -- --resume         # 透传参数
eval "$(agentman env alt1)"           # 在当前 shell 切换到 alt1
agentman exec plus -- codex exec "..."
```

`agentman usage` 输出示例：

```
claude-work      claude  work@x.com (max 20x)
    5h         ████████░░░░░░░░░░░░  42%  resets in 2h12m
    7d         ███████████████░░░░░  76%  resets in 2d23h
    extra usage: 12.34 / 50.00 USD

codex-main       codex   me@x.com (pro)
    5h         ██░░░░░░░░░░░░░░░░░░  10%  resets in 3h59m
    7d         ███████████░░░░░░░░░  55%  resets in 5d23h
```

用 `agentman add ... --home <已有目录>` 可以登记已有的 `CLAUDE_CONFIG_DIR` 或 `CODEX_HOME`，不需要重新登录。

### Token 过期怎么办

Claude 和 OpenAI 的 refresh token 都是**一次性、会轮换**的。如果第三方工具自己刷新 token 却不写回，CLI 手里那份 refresh token 就会失效，导致被登出。所以：

- **默认行为**：access token 过期时只报错，提示你在该账号下运行一次 `claude` 或 `codex`，让 CLI 自己去刷新。
- **`--refresh-tokens`**：由 agentman 刷新 token，并把新 token **原子写回**该账号的凭据文件（文件权限 0600，同时保留 `mcpOAuth` 等其他字段），和 CLI 自己刷新的效果一样。存在 macOS Keychain 里的 Claude 凭据不会被改写。

### 注意

- Claude 用量接口的限流比较严格（会返回 429），轮询间隔不要短于 1 分钟。`tray` 和 `serve` 默认每 120 秒请求一次（用 `-n` 调整），手动刷新至少间隔 15 秒。
- 如果 Codex 配置了 `cli_auth_credentials_store = "keyring"`，token 存在系统 keyring 里，本工具目前读不到。
- 所有凭据只在本地读取，只发送给 Anthropic 和 OpenAI 的官方接口。

## 配置

账号列表保存在 `~/.agentman/config.json`，可以用 `AGENTMAN_HOME` 修改这个位置。新账号的目录默认建在 `~/.agentman/accounts/<provider>-<name>`。

## 类似项目调研（2026-09）

| 项目 | 形态 | 多账号 | Claude | Codex | 说明 |
|---|---|---|---|---|---|
| [steipete/CodexBar](https://github.com/steipete/CodexBar) | macOS 菜单栏 | ✓（token accounts） | ✓ | ✓ | 最成熟，支持几十个 provider，**仅限 macOS** |
| [f-is-h/Usage4Claude](https://github.com/f-is-h/usage4claude) | macOS 菜单栏 | ✓ | ✓ | ✓ | 支持多个 Claude 账号和组织，也支持 Codex |
| [crandrosoff/clauth](https://github.com/crandrosoff/clauth) | CLI / TUI / MCP | ✓ | ✓ | ✗ | 切换 Claude 账号，快到上限时自动切换，**只支持 Claude** |
| [CodeZeno/Claude-Code-Usage-Monitor](https://github.com/CodeZeno/Claude-Code-Usage-Monitor) | Windows 任务栏 | ✓ | ✓ | ✓ | **仅限 Windows** |
| [jens-duttke/usage-monitor-for-claude](https://github.com/jens-duttke/usage-monitor-for-claude) | Windows 托盘 | 每个账号开一个进程 | ✓ | ✗ | |
| [Maciek-roboblog/Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | 终端 | ✗ | ✓ | ✗ | 通过本地 jsonl 日志估算 token，**不是**官方百分比 |

本项目的定位：**跨平台（包括 Linux 和远程服务器）的 CLI + Web**，同时支持 Claude 和 Codex 的多账号。可以用 JSON 接入其他工具，也可以跑在一台服务器上集中看所有账号。

## 开发

```bash
pip install pytest && python -m pytest -q
```

## Roadmap

- [x] 快到上限时提醒（Windows 通知）
- [ ] 提醒推送到 webhook / Telegram
- [ ] 记录用量历史并画趋势图（SQLite）
- [ ] 自动推荐或切换到剩余额度最多的账号（`agentman pick claude`）
- [ ] 支持 Cursor / Gemini CLI / Copilot 等更多 provider
