# redline workspaces：仓库 ↔ AI 账号 ↔ 订阅，一套系统管

状态：设计稿（2026-10-24），未实现。适用范围：Windows 上的 Claude Code 和 Codex。

## 0. 结论先说

- **做**：redline 增加"工作区"（workspace），把 `claude_profile.py` 的登记表和建目录功能收进来。"use in VS Code" 从只有一个默认槽位，推广到 N 个槽位：点了以后先选应用到哪个工作区。
- **像 git 的是用法，不是存储**：`redline ws status / use / init`，在仓库里运行时自动认出当前工作区。登记表集中放在 `~/.redline/`，不往仓库里写标记文件。
- **登录只移动，不复制**：一份登录同一时刻只放在一个目录里。账号换到别的工作区时，它离开的那个工作区退出登录，要先确认。
- **现在不做 VS Code 插件**：Profile 加设置已经能把账号分到各个窗口。插件只多一个窗口内的状态栏，不值得为它多维护一条 TypeScript 发布线。

## 1. 问题：两套系统各管一半

| | redline | `claude_profile.py` |
|---|---|---|
| 登记表 | `~/.redline/config.json`：账号 → 账号自己的目录 | `~/.claude/profiles.json`：配置目录 → 仓库 + VS Code Profile |
| 切换账号 | `slots.activate`：只能换 `~/.claude` / `~/.codex` 这一个默认槽位 | 不能切换，只能在目录里重新 `login` |
| 登录 | 在绑定的 Chrome profile 里自动登录，并核对邮箱 | `claude auth login`，用默认浏览器（要提醒用户开无痕窗口） |
| 额度 | 自己拉取 | 调 `usage.py`，读 redline 的缓存，按邮箱对上 |
| 被外部换号 | `reconcile` 跟踪，但只看默认槽位 | 不跟踪 |
| Codex | 只有默认槽位 | 不管 |

缺口和重复：
1. **redline 不知道 quant / osworld 两个目录的存在**，所以没法把账号应用过去。这两个目录里的会话也不会出现在任何账号的"最近项目"里。
2. **两份登记表、两套登录流程、两种 status**，彼此没有关联。
3. **没人检查漂移**。本机现在的实际情况（2026-10-24 只读核对）：

| 工作区 | 配置目录 | VS Code Profile | VS Code 记住的文件夹 | 目录里的登录 |
|---|---|---|---|---|
| default | `C:\Users\ALW\.claude` | Default | 大部分文件夹，**包括 OSWorld** | tsljgj（redline 的默认账号） |
| quant | `D:\claude-config-quant` | quant | cn-equity-research-canonical，以及 `C:\Users\ALW`、mmlm-project | `.claude.json` 记的是 **tsljgj**，全局 CLAUDE.md 写的是 zhihaoyuan2005 |
| osworld | `D:\claude-config-osworld` | osworld | **没有**（OSWorld 关联在 Default 上） | **没有登录** |

也就是说，三个工作区现在实际上**用的都是 tsljgj**，和"分散花费"的本意相反，而且没有任何工具发现了这一点。

另外两个事实决定了设计：
- **Claude 扩展**不认进程的 `CLAUDE_CONFIG_DIR`（anthropics/claude-code#30538），但认设置项 `claudeCode.environmentVariables`。这个设置是 machine 作用域，不能写进工作区设置，所以只能写进 **VS Code Profile** 的用户设置。这就是 quant 能分开的原因。
- **Codex 扩展**没有任何能设环境变量的设置项（已查 `openai.chatgpt` 的 package.json）。各 Profile 里和用户环境变量里也都没有设 `CODEX_HOME`。所以 Codex 现在只有默认槽位 `~/.codex`。按工作区分 Codex 的话，只能做到终端这一层（用 `terminal.integrated.env.windows`）。

## 2. 数据模型

三层概念，把现在混在一起的"账号 = 目录"拆开：

| 概念 | 是什么 | 存在哪 |
|---|---|---|
| **账号** account | 一个邮箱 + 一份额度 + 绑定的 Chrome。`home` 是它的"停车位"：没在任何工作区用时，登录放在这里 | `config.json`（不变） |
| **槽位** slot | 某个 provider 读登录的一个目录（`CLAUDE_CONFIG_DIR` / `CODEX_HOME` / 默认目录）。同一时刻里面最多一份登录 | `slots.json`（扩展，见 §3.3） |
| **工作区** workspace | 名字 + 若干仓库根目录 + 每个 provider 一个槽位 + 一个 VS Code Profile | 新增 `~/.redline/workspaces.json` |

```json
{"version": 1, "workspaces": [
  {"name": "default", "profile": null, "repos": [],
   "slots": {"claude": "~/.claude", "codex": "~/.codex"}},
  {"name": "quant", "profile": "quant", "repos": ["D:\\cn-equity-research-canonical"],
   "slots": {"claude": "D:\\claude-config-quant"}, "source": "profiles.json"}
]}
```

工作区里**不存**"当前是哪个账号"。这个信息由槽位里的登录（按邮箱）实时决定，由 `slots.json` 记录归属和历史。只存一处，就不会有两处说法不一致。

**集中登记还是仓库里放标记文件**：推荐集中登记，按 git 的方式找。在任意子目录里运行时，从 cwd 往上走，匹配登记过的仓库根目录。不在仓库里放 `.redline` 文件，原因有三：
- OSWorld 这类仓库是公开的或别人的，标记会被提交，也会泄露本机路径；
- 一个工作区可以对应好几个仓库（quant 现在就关联了三个文件夹）；
- 换机器以后，标记里的路径就失效了。

**发现**（`redline ws scan`，只读，首次启动自动跑一次）：
1. `profiles.json` → 工作区（name、config_dir、repo、profile）。
2. `%APPDATA%\Code\User\globalStorage\storage.json` 的 `userDataProfiles` 给出 Profile 名 → 目录名。再读 `profiles\<目录名>\settings.json` 里 `claudeCode.environmentVariables` 的 `CLAUDE_CONFIG_DIR`，以及 `terminal.integrated.env.windows` 的 `CODEX_HOME`，和 `profiles.json` 对照。设了配置目录却没登记的 Profile，也列为工作区。
3. 同一个文件的 `profileAssociations.workspaces` 给出文件夹 → Profile，用来填 `repos`，并发现漂移（例如 OSWorld 在 Default 下打开）。
4. 没设变量的 Profile 一律归 default 工作区。
5. `.claude-experiment*` 永远不当作工作区。redline 现有几个账号的 `home` 在这类目录里，它们只是停车位，不受影响。

redline **只读** `storage.json`，不写。VS Code 运行时会覆盖这个文件。要修正关联，就用 `code --profile <P> <文件夹>`，让 VS Code 自己记下。

## 3. "这个账号应用到哪个工作区"

### 3.1 控制台流程

1. 在账号卡片上点 **◆ use in VS Code**。
2. 这个 provider 只有一个工作区（Codex 现在就是这样）时，行为和现在一样。
3. 有多个时，展开一个小列表，每行显示：工作区名 · 仓库名 · 当前账号 · 它的 5h。这个账号正在用的那一行带标记。不显示目录路径。
4. 选一行：
   - 账号的登录**在停车位上**：立即移过去。原来占着槽位的登录回到它自己账号的停车位。提示"quant → X · 重载 quant 窗口"。
   - 账号**正在另一个工作区 W 用**：同一处再出一步确认，按钮是 `移过来，W 退出登录` / `取消`。W 有正在进行的对话时（复用 `sessions.recent` 的绿点）按钮变红。Phase 4 加第三个按钮 `为 quant 另登一份`。
5. 卡片详情里原来的"默认"标记，换成该账号所在工作区的名字。

### 3.2 登录离开的那个工作区怎么办

| 方案 | 做法 | 优点 | 问题 |
|---|---|---|---|
| A 拒绝 | 账号在别处用着就不让应用 | 最安全 | 用户还是得手动把它挪走，等于多点一步 |
| **B 移走，旧工作区退出登录** | 登录移到目标槽位，W 变成空槽位 | 不会出现意外的账号；行为和"refresh token 只能有一份"的物理事实一致 | W 下次用时要重新选账号；在 W 里跑着的对话会在下次请求或续期时断掉 |
| C 互换 | 目标槽位原来的登录移到 W | 两边都还能用 | **悄悄换掉了 W 的账号和额度**，用户没选过；连续切几次以后没人说得清谁在哪 |
| D 另登一份 | 用 X 的 Chrome profile 再走一次 OAuth，给目标槽位一份独立的登录 | 什么都不用移，W 不受影响 | 一个账号有了多份登录，要改模型（§2 的账号要能有多个登录）；要开浏览器 |

**推荐 B，确认后执行**。Phase 4 再加 D 作为可选项。D 是可行的：quant 和 default 现在各有一份独立的 tsljgj 登录，都能正常用。说明限制只是"每份登录只能在一处"，不是"一个邮箱只能在一处"。C 不做。

被挤出来的登录回到它主人的停车位。如果停车位里已经有同一邮箱的另一份登录（D 方案或采纳旧目录时会出现），**不覆盖，存进 `~/.redline/stash/`**。现在的代码只在邮箱不同时才 stash，这里要改。

### 3.3 `slots.py` 从 1 个槽位推广到 N 个

- **状态**：`slots.json` 保留现有的 `"claude"` / `"codex"` 两个键，表示默认槽位，格式不变；新增 `"slots": {"claude:D:\\claude-config-quant": {home, since, email, history}}`。旧版 exe 读到新文件只是忽略新键，默认槽位照常工作。
- **API**：
  - `slot_dir(provider)` → `slots(provider) -> list[Slot]`；
  - `is_active(acct)` → `slot_of(acct) -> Slot | None`；
  - `effective(acct)` 返回它所在槽位的目录（wake、续期、终端都已经走这里）；
  - `active_periods(acct)` 按槽位分别算，这样 quant 目录里的会话也能归到对的账号；
  - `activate(key)` → `activate(key, slot)`。
- **锁**：要锁目标槽位、账号停车位，以及（如果登录在另一个槽位里）源槽位。多把锁一律**按解析后的路径排序再加**，避免两个 redline 进程互相等死。其余规则照旧：
  - 加锁后先 `reconcile`；
  - `slots.json` 用 strict 模式读；
  - 有登录任务在跑就拒绝；
  - `.claude.json` 读不了就拒绝；
  - 每个槽位的身份文件，默认槽位是 `~/.claude.json`，其他是 `<目录>\.claude.json`，`_claude_config` 已经这样区分。
- **`reconcile`**：遍历所有槽位。非默认槽位被人在面板里换了号，处理和默认槽位一样。如果那个邮箱的账号已经在别处有登录，就记成该账号的额外登录，不新建账号（遵守"一个邮箱一个账号"）。
- **登录离开源槽位时**：同时删掉源槽位 `.claude.json` 里的 `oauthAccount`，以免那个目录还自称是 X。

### 3.4 迁移安全

1. **采纳是只读的**：发现 quant 这类旧目录时，只登记槽位，并按邮箱记下归属，不移动任何文件。第一次 `use` 时才开始管理。
2. 采纳时，槽位里的登录如果对不上任何账号：按现有规则新建账号；对得上、但账号在别处已有登录：记成额外登录。
3. `ws use --dry-run` 打印要移动、要 stash 的每个文件，什么都不改。
4. 测试矩阵用临时目录逐条复现。场景包括：
   - 停车位 → 工作区；工作区 → 工作区；
   - 目标槽位里是外来登录；停车位里已有同一邮箱的登录；
   - `slots.json` 损坏；锁被占用；登录任务在跑；macOS 拒绝；
   - 两个进程同时切换。
5. 先放在设置项 `workspaces` 后面，只开给本机。默认槽位的代码路径保持不变，回归测试全部要过。

## 4. 像 git 的命令行

在仓库目录里运行时，`--ws` 可以省略，由 cwd 决定当前工作区。

| 命令 | 作用 |
|---|---|
| `redline ws list` | 列出所有工作区：名字、Profile、仓库、每个 provider 的账号 |
| `redline ws status [ws]` | 见下面的示例；带 `--json` 给其他工具读 |
| `redline ws use <account> [--ws X] [--dry-run]` | 把账号应用到工作区。不在仓库里、也没给 `--ws`，并且有多个工作区时：终端里列出来让人选，非交互模式下报错 |
| `redline ws init <name> --repo <dir> [--dir D:\claude-config-<name>] [--account X]` | 接手 `claude_profile.py new`：建配置目录，复制 settings 和本仓库的记忆，建 Profile，写 `claudeCode.environmentVariables`，登记；给了 `--account` 就接着 `use` |
| `redline ws open [ws\|repo]` | `code --profile <P> <repo>`，顺便修正 VS Code 的文件夹关联；取代 `open-*.cmd` |
| `redline ws repo add\|rm <ws> <dir>` | 一个工作区挂多个仓库 |
| `redline ws scan` | 重新发现（§2），只报告差异，不覆盖用户改过的内容 |
| `redline ws rm <ws>` | 注销工作区：登录回到主人的停车位，目录和 Profile 留在磁盘上 |

`status` 示例：

```
quant      D:\cn-equity-research-canonical  (+2 folders)            profile quant
  claude   tsljgj@outlook.com     5h 42% ↻1h20m   7d 76% ↻2d     ✓
osworld    C:\Users\ALW\Desktop\OSWorld                             profile osworld
  claude   — not signed in                                         ! use: redline ws use <account> --ws osworld
  !        OSWorld opens in the Default profile                    fix: redline ws open osworld
default    (all other folders)
  claude   tsljgj@outlook.com     5h 42% ↻1h20m   7d 76% ↻2d     = quant (shared quota)
  codex    chatgpt4miltown@…      5h  3%          7d 11%
```

同一个账号出现在两个工作区时，标出"共享额度"，防止误以为有两份额度。

## 5. 收编 `claude_profile.py`

| 功能 | 去向 |
|---|---|
| 登记表 `profiles.json` | redline 的 `workspaces.json` 成为唯一的写入方，并**继续按原格式导出 `profiles.json`**，让 hook-sync 不用改 |
| `new` | `redline ws init` |
| `login` | `redline login` / `ws use`，用 Chrome profile 自动登录，并核对邮箱 |
| `status` | `redline ws status` |
| `open-*.cmd` / `login-*.cmd` | `redline ws open`；`.cmd` 可以选择继续生成 |
| skills / CLAUDE.md / agents / commands 同步、`hook-sync`、`install-hooks` | **先留在原脚本**。它在每个 Claude 会话的 hook 里运行，要快，也不能依赖 redline 托盘是否在跑；而且它管的是环境，不是登录 |

迁移步骤：
1. 第 1 阶段只读：redline 读 `profiles.json`，两边并存。
2. 第 3 阶段：`ws init` 上线后，redline 接管写入，并原样导出 `profiles.json`。`claude_profile.py new/login/status` 改成打印"请用 `redline ws …`"后退出。
3. 改写 skill `workspace-profile` 和全局 CLAUDE.md 的相关段落，把"各目录登录的是哪个账号"这类会过时的事实删掉，改为"以 `redline ws status` 为准"。
4. 同步脚本以后可以改读 `workspaces.json`，要不要把同步并进 redline，再单独决定（见 §8）。

写 VS Code Profile 的 `settings.json` 时要注意：现在的脚本用 `json.dumps` 整个重写，会丢掉文件里的注释。redline 改成**只替换那一个键**，改之前先备份。

## 6. VS Code 插件：现在不做

| 插件能多做的 | Profile + 设置能不能做 | 价值 |
|---|---|---|
| 窗口状态栏显示本窗口的账号和 5h/7d | 不能 | 中。先试零成本的替代：在每个配置目录的 `settings.json` 里设 Claude Code 的 `statusLine`，读 redline 缓存（终端 CLI 会显示，扩展面板会不会显示还没核实） |
| 命令面板里快速换号 | 不能，但 `redline ws use` 在终端里一行就行，托盘也能换 | 低 |
| 打开仓库时发现 Profile 不对，提示换窗口 | 不能。不过 `ws status` 能报告漂移，`ws open` 能修 | 中 |
| 换号后自动提示重载窗口 | 不能 | 低 |
| 让 Codex 扩展按窗口用不同账号 | **插件也做不到**，Codex 扩展不读任何环境设置 | — |

代价：
- 要另起一套 TypeScript 工具链和构建；
- 要发布到 marketplace（需要 publisher 账号）或者分发 VSIX；而扩展是按 Profile 启用的，每个 Profile 都要装；
- 更新独立于 redline 的自动更新；
- 每个 VS Code 和扩展的版本都要测。

redline 现在是纯 Python 标准库，这个代价不成比例。

**推荐：不做。** 第 3 阶段完成后，如果还觉得缺窗口内的状态显示，再做最小版：
- 只读，大约 150 行；
- 状态栏读 `redline ws status --json --cwd <folder>`，点击后弹出账号列表，调用 `redline ws use`；
- 发现窗口 Profile 和仓库所属工作区对不上时提示一次；
- VSIX 打包进 `redline.exe`，由 redline 用 `code --install-extension <vsix> --profile <P>` 装进每个 Profile，跟着 redline 一起更新。

## 7. 分阶段

| 阶段 | 内容 | 规模 | 风险 |
|---|---|---|---|
| **1 看得见** | `workspaces.json` + 只读发现 + `ws list/status` + 卡片上显示所在工作区 + 漂移警告 | 约 350 行 + 测试，1–2 天 | 低：不移动任何登录。马上能暴露上面那三处漂移 |
| **2 能应用** | `slots.py` 推广到 N 个槽位、控制台的工作区选择、`ws use`、方案 B、`--dry-run` | 约 500 行，测试比代码多，3–4 天 | **高**：直接改登录的移动逻辑。措施见 §3.4；默认槽位的行为不变 |
| **3 接手** | `ws init/open/repo/rm`，写 Profile 设置，导出 `profiles.json`，`claude_profile.py` 改为提示 | 约 400 行，2 天 | 中：要写 VS Code 设置（只改一个键，先备份）；还要和 hook-sync 并存 |
| **4 可选** | 方案 D（一个账号多份登录）、Codex 终端级工作区、同步并进 redline、插件最小版 | 各约 1–3 天 | D 要改账号模型和额度去重；Codex 只能做到终端 |

## 8. 请你决定

1. **账号离开原工作区时怎么办**：A 拒绝 / B 移走并让旧工作区退出登录 / C 互换 / D 另登一份。**推荐 B（先确认），第 4 阶段加 D**。
2. **登记方式**：集中登记，按 cwd 往上找 / 仓库里放标记文件 / 放在 `.git/` 里（不进提交）。**推荐集中登记**。
3. **skills 同步**：留在 `claude_profile.py`，改读 redline 的登记表 / 现在就并进 redline。**推荐留着**，hook 不能依赖托盘。
4. **VS Code 插件**：不做 / 做最小版 / 做完整版。**推荐不做**，先试 `statusLine`，第 3 阶段后再看。
5. **Codex 工作区**：只有默认槽位 / 按 Profile 设终端级 `CODEX_HOME`（扩展面板仍然用默认账号）。**推荐先只用默认槽位**，以免出现"终端一个号、面板另一个号"。
6. **现有的漂移**：quant 应该登哪个账号（现在是 tsljgj，CLAUDE.md 写的是 zhihaoyuan2005）？osworld 用哪个账号？**推荐等第 1 阶段的 `ws status` 上线后，用 `ws open osworld` 加 `ws use` 一次修好**；等不及的话，先用 `open-OSWorld.cmd` 打开 OSWorld，再登录 osworld。
7. **工作区命名**：沿用 `profiles.json` 和 VS Code Profile 的名字（default / quant / osworld）/ 另起名字。**推荐沿用**，一个名字通用三处。
