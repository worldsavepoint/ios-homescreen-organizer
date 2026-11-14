# MCP 配置

[README](../README.zh-CN.md) · [English](MCP.md) · [工作原理](HOW-IT-WORKS.md)

Home Screen Organizer（主屏幕整理）提供本地 <strong>stdio MCP 服务器</strong>，支持查看布局、编辑草稿、排序、预览和备份。客户端在连接设备的 Mac 上启动进程，无需 HTTP 地址或 API 密钥。

## 选择启动方式

| 安装方式 | 可执行文件 | 参数 |
| --- | --- | --- |
| Applications 中的 Mac App | `/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine` | `mcp` |
| [源码安装](../macapp/BUILDING.md#source-installation) | `/absolute/path/ios-homescreen-organizer/.venv/bin/home-screen-mcp` | 无 |
| 独立 MCPB | 客户端管理的 `server/launch.sh` | 由清单管理 |

客户端设置中使用可执行文件的绝对路径。若 App 安装在其他位置，请采用实际路径。Mac App 的 <strong>Device（设备）→ MCP</strong> 菜单可根据当前位置生成 <strong>Copy Launch Command</strong>、<strong>Copy Cursor / Claude JSON</strong>、<strong>Copy Codex TOML</strong> 和 <strong>Copy VS Code / Copilot JSON</strong>。所有复制的配置均设置 `HOMESCREEN_READ_ONLY=1`。

客户端负责启动并维护 stdio 进程。直接在终端运行时，进程等待 MCP 输入，不会打开编辑器：

```sh
HOMESCREEN_READ_ONLY=1 '/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

源码安装使用：

```sh
HOMESCREEN_READ_ONLY=1 /absolute/path/ios-homescreen-organizer/.venv/bin/home-screen-mcp
```

稳定的服务器 ID 为 `ios-homescreen-mcp`。旧命令 `ios-homescreen-mcp` 和 `homescreen-mcp` 仍是 `home-screen-mcp` 的别名。

## 客户端指南

添加配置时保留已有服务器。修改后重启或重新连接客户端，并确认主屏幕整理的工具已出现。

<a id="codex"></a>

<details open>
<summary><strong>Codex — CLI 或 TOML</strong></summary>

在终端注册 App 内置引擎：

```sh
codex mcp add ios-homescreen-mcp --env HOMESCREEN_READ_ONLY=1 -- \
  '/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

或将以下内容添加到 `~/.codex/config.toml`：

```toml
[mcp_servers.ios-homescreen-mcp]
command = "/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine"
args = ["mcp"]

[mcp_servers.ios-homescreen-mcp.env]
HOMESCREEN_READ_ONLY = "1"
```

源码安装将 `command` 改为 `.venv/bin/home-screen-mcp` 的绝对路径并移除 `args`；CLI 也可直接注册该文件。参见 [Codex 官方 MCP 指南](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

</details>

<a id="cursor-and-claude"></a>

<details>
<summary><strong>Cursor / Claude — JSON 或 Claude Code CLI</strong></summary>

将以下 JSON 用于 Cursor 的 `~/.cursor/mcp.json`（全局）或 `.cursor/mcp.json`（项目），或 Claude Desktop 的 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "ios-homescreen-mcp": {
      "command": "/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine",
      "args": ["mcp"],
      "env": { "HOMESCREEN_READ_ONLY": "1" }
    }
  }
}
```

Claude Code 可通过以下命令注册：

```sh
claude mcp add --env HOMESCREEN_READ_ONLY=1 --transport stdio ios-homescreen-mcp -- \
  '/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

源码安装将 `command` 改为 `.venv/bin/home-screen-mcp` 的绝对路径并省略 `args`；CLI 命令使用该文件且不附加 `mcp` 参数。参见 [Cursor MCP 配置](https://cursor.com/docs/mcp)、[Claude Desktop 本地服务器](https://modelcontextprotocol.io/docs/develop/connect-local-servers)和 [Claude Code MCP](https://code.claude.com/docs/en/mcp)。

</details>

<a id="vs-code-and-copilot"></a>

<details>
<summary><strong>VS Code / Copilot — 原生 MCP 配置</strong></summary>

个人配置使用 `~/.copilot/mcp-config.json`；若已设置 `COPILOT_HOME`，使用 `$COPILOT_HOME/mcp-config.json`。工作区也可采用根目录中的 `.mcp.json`。

```json
{
  "mcpServers": {
    "ios-homescreen-mcp": {
      "type": "stdio",
      "command": "/Applications/Home Screen Organizer.app/Contents/Resources/engine/engine",
      "args": ["mcp"],
      "env": { "HOMESCREEN_READ_ONLY": "1" },
      "tools": ["*"]
    }
  }
}
```

在 VS Code 中选择 Copilot harness，运行 <strong>Chat: Open Customizations → MCP Servers → Add Server</strong>，再选择 <strong>Global → Copilot Global</strong>，即可使用相同的个人配置位置。`tools: ["*"]` 向 Copilot 开放全部工具；客户端批准流程和服务器写入控制仍然生效。

源码安装将 `command` 改为 `.venv/bin/home-screen-mcp` 的绝对路径并省略 `args`。配置位置与字段见 [VS Code / Copilot 官方配置参考](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)。

</details>

<a id="standalone-mcpb"></a>

## 独立 MCPB

从[发行版](https://github.com/worldsavepoint/ios-homescreen-organizer/releases)下载 `home-screen-organizer-<version>-macos.mcpb` 和 `SHA256SUMS.txt`，并[校验文件](../README.zh-CN.md#install)。包内包含 Apple silicon 与 Intel 引擎、依赖和许可证记录，需要 macOS 14+，无需另装 Python 或 Mac App。

通过客户端的 MCPB 安装操作导入文件。能否直接导入取决于客户端支持。清单与启动器默认使用只读模式；仅支持 stdio 配置的客户端可使用前述 Mac App 或源码可执行文件。

构建细节见[独立 MCP 包组装](../macapp/BUILDING.md#standalone-mcp-bundle)，格式说明见 [MCPB 官方项目](https://github.com/anthropics/mcpb#readme)。

## 草稿操作流程

1. 连接并解锁设备，信任 Mac 后，请助手调用 `read_phone`。重新读取会替换当前草稿。
2. 使用 `show_layout` 或 `find_apps` 检查布局，再移动、建文件夹或排序。可用 `undo`、`redo` 和 `reset_draft`。
3. 请求 `diff` 和 `render_preview`；预览工具返回本地 HTML 路径。清空 App Bank 并处理校验问题。
4. 应用前，<strong>在设备上取消隐藏主屏幕页面，并在编辑器中重新建立所需空位</strong>。审阅差异后，通过下述控制写入。

请求示例：“保留第一页，按颜色整理其他散放 App”“把这些 App 放入旅行文件夹”“比较草稿和当前布局”。iOS 16 放置规则与 iPad 容量见[布局模式](COMPATIBILITY.md)。

<a id="data-and-write-controls"></a>

## 数据与写入控制

### 客户端会收到什么

工具结果可能包含设备名称、型号和软件信息；App 名称与 bundle ID；文件夹名称、小组件尺寸与布局位置；图标颜色及按请求返回的图标图片；布局差异和校验结果；备份名称与本地预览路径。`read_phone` 返回的设备信息排除了 `UniqueDeviceID`。使用情况工具还可返回各 App 的使用估算、后台 App 分类和页面滑动估算。

这些结果会交给 MCP 客户端，后续在本地处理还是发送到服务，取决于客户端选择的模型、提供商及隐私设置。读取个人布局或使用记录前，请检查这些设置。草稿、备份、图标/类别缓存和预览存放于 `~/Library/Application Support/homescreen-mcp`，也可通过 `HOMESCREEN_DATA` 指定目录。

<strong>类别查询：</strong>编辑器发送 App bundle ID 到 Apple 公共查询 API 前会弹窗确认。MCP 工具说明要求助手先取得用户同意，确认方式由客户端的工具流程决定；MCP 不会显示编辑器的弹窗。查询结果在本地缓存。

<strong>使用数据：</strong>设备内存报告提供近似使用排名。可选的 Mac 来源读取同步到本机的 Biome `App.InFocus` 记录，需要启用屏幕使用时间的<strong>在设备之间共享</strong>，并为 App 或终端授予<strong>完全磁盘访问权限</strong>。该来源按 App 重合度选择并合并记录，排名可能反映多台同步设备。详见[数据来源与估算](HOW-IT-WORKS.md#data-and-privacy)。

### 启用写入

| 设置 | 效果 |
| --- | --- |
| `HOMESCREEN_READ_ONLY=1` | 可读取和编辑草稿；App、CLI、MCP 均禁止应用和恢复到设备。 |
| `HOMESCREEN_READ_ONLY=0` 或未设置 | App / CLI 可通过各自的审阅或确认流程写入设备。 |
| `HOMESCREEN_ALLOW_APPLY=1` | 在只读模式关闭时，启用 MCP 应用与恢复。 |

若保持助手写入关闭，可自行启动不含只读设置的 App / CLI 会话，检查草稿后选择 <strong>Review & Apply</strong>，或运行：

```sh
home-screen-organizer diff
home-screen-organizer apply <apply_token>
```

CLI 要求输入 `yes`，令牌取自最新的 `diff`。若需要助手写入，同时设置 <strong>`HOMESCREEN_READ_ONLY=0`</strong> 和 <strong>`HOMESCREEN_ALLOW_APPLY=1`</strong>，再重启服务器。工具说明要求用户明确提出写入请求；环境开关由服务器强制检查，不会额外弹出确认对话框。应用时检查令牌、校验草稿、刷新设备身份和布局、保存备份并核对回读。恢复时检查备份来源匹配。写入中断或结果不匹配后，请重新读取设备。

从 1.2.1 起，恢复含固定位置的备份需要备份中保存的网格尺寸。可信且不含固定位置的旧版备份仍受支持。恢复时映射保存的位置坐标，并要求每个项目的占用范围都能放入当前读取的网格。

<a id="distribution-and-discovery"></a>

## 分发与发现

GitHub Releases 提供 DMG、ZIP、双架构 MCPB、`server.json` 和统一校验清单。Registry 发布采用名称 <strong>`io.github.worldsavepoint/home-screen-organizer`</strong>，元数据指向指定版本 MCPB 下载地址与 SHA256。

发行工作流在 GitHub 发行成功后，通过 GitHub OIDC 发布 Registry 元数据。参见[自动发行](../macapp/BUILDING.md#automatic-releases)、[Registry 概览](https://modelcontextprotocol.io/registry/about)与 [GitHub Actions 发布指南](https://modelcontextprotocol.io/registry/github-actions)。

Registry 发布等待默认分支更新及正式发行。Glama 与 awesome-mcp-servers 是独立的社区发现渠道，计划在完成上述前提后提交。
