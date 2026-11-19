<h1 align="center">屏幕整理</h1>
<p align="center"><strong>Screen Organizer · for iOS · 在 Mac 上整理 iPhone 或 iPad 的主屏幕。</strong><br>macOS 14+ · Apple silicon 与 Intel · 支持 iOS 16+</p>

<p align="center">
  <a href="https://github.com/worldsavepoint/ios-homescreen-organizer/releases"><img alt="发行版" src="https://img.shields.io/badge/download-Releases-blue"></a>
  <a href="#compatibility"><img alt="支持 iOS 16 及以上版本" src="https://img.shields.io/badge/iOS-16%2B-007AFF"></a>
  <a href="LICENSE"><img alt="原始源码采用 MIT 许可证" src="https://img.shields.io/badge/original_source-MIT-blue"></a>
  <a href="macapp/BUILDING.md"><img alt="macOS 14 或更高版本" src="https://img.shields.io/badge/macOS-14%2B-222222"></a>
</p>
<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>
<p align="center"><a href="#install">下载</a> · <a href="#get-started">快速开始</a> · <a href="#compatibility">兼容性</a> · <a href="#mcp">MCP 配置</a> · <a href="#development">开发</a></p>

拖动真实 App 图标，整理文件夹和小组件，按颜色或使用情况排序，比较草稿后通过 USB 应用布局。可使用 Mac App、本地浏览器编辑器、CLI 或 MCP 助手。免费开源，无需账户或越狱。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/app-dark.png">
  <img alt="屏幕整理编辑器：App 图标、小组件、App 资源库和使用情况" src="docs/app-light.png" width="1200">
</picture>
<p align="center"><sub>使用合成布局展示当前编辑器。界面基于 Jonathan Kats 的上游 <a href="https://github.com/kats1123/homescreen-mcp">Homescreen 项目</a>。</sub></p>

## 可以做什么

| 整理布局 | 审阅改动 | 选择操作方式 |
| --- | --- | --- |
| 拖动 App、文件夹、小组件和程序坞项目 | 比较设备布局与草稿 | 原生 Mac App 或本地浏览器 |
| 按颜色、字母、类别或使用情况排序 | 撤销、重做草稿编辑 | CLI 与标准 stdio MCP |
| 将 App 移入资源库或 App Bank 暂存区 | 写入前备份，写入后回读核对 | 每台设备分别保存草稿与备份 |

多选、搜索、页面编辑和多页文件夹便于整理大型布局。类别查询和使用情况排序各有对应的[数据来源与权限](docs/HOW-IT-WORKS.md#data-and-privacy)。

<a id="install"></a>

## 下载

打开[发行版页面](https://github.com/worldsavepoint/ios-homescreen-organizer/releases)，根据 <strong>Apple 菜单 → 关于本机 → 芯片 / 处理器</strong>选择下载文件。

| Mac 类型 | 下载文件 |
| --- | --- |
| Apple silicon（M 系列） | `home-screen-organizer-<version>-macos-arm64.dmg` |
| Intel | `home-screen-organizer-<version>-macos-x86_64.dmg` |
| 支持 MCPB 的客户端 | `home-screen-organizer-<version>-macos.mcpb` — 包含两种架构；[安装指南](docs/MCP.zh-CN.md#standalone-mcpb) |

打开 DMG，将 <strong>Screen Organizer（屏幕整理）</strong>拖入 <strong>Applications（应用程序）</strong>。也可选择 ZIP 压缩包。App 已包含 Python 引擎和依赖，需要 <strong>macOS 14 或更高版本</strong>。

App 使用 <strong>ad-hoc 签名</strong>。若 macOS 要求批准，请在 <strong>系统设置 → 隐私与安全性 → 仍要打开</strong>中允许，然后重新启动。

将 `SHA256SUMS.txt` 与下载的压缩包放在同一目录，运行以下命令校验：

```sh
shasum -a 256 --check --ignore-missing SHA256SUMS.txt
```

源码安装、原生构建和 Actions 构建产物见[构建指南](macapp/BUILDING.md)。

<a id="get-started"></a>

## 快速开始

1. 打开屏幕整理，选择语言，将协议滚动至末尾并勾选同意。每次启动都会显示协议；拒绝后 App 退出。通过 USB 连接并解锁 iPhone 或 iPad，出现提示时轻点 <strong>信任</strong>。
2. 选择设备，点击 <strong>Read（读取）</strong>，从当前布局创建草稿。
3. 拖动项目或选择排序操作，清空 <strong>App Bank 暂存区</strong>，再检查布局差异。
4. <strong>应用前：</strong>在设备上取消隐藏主屏幕页面（长按主屏幕后轻点页面圆点），并在编辑器中重新建立需要的空位。选择 <strong>Review & Apply（审阅并应用）</strong>，保存备份、写入草稿并回读核对。

编辑只改变草稿，应用后才写入设备。设置 `HOMESCREEN_READ_ONLY=1` 可读取并编辑草稿，同时<strong>禁用应用与恢复</strong>。源码安装可用以下命令启动此模式：

```sh
HOMESCREEN_READ_ONLY=1 .venv/bin/home-screen-organizer ui
```

写入中断或回读不匹配后，请先重新 <strong>Read（读取）</strong>再继续。详见[隐藏页面、空位与备份](docs/HOW-IT-WORKS.md#layout-writes)。

<a id="compatibility"></a>

## 兼容性

以下版本系列采用对应的布局行为。细节见[兼容性与诊断指南](docs/COMPATIBILITY.md)。

| iOS 系列 | 布局模式 |
| --- | --- |
| 16 | 按顺序排列 App；文件夹放在原本位于页首的小组件之后 |
| 17 / 18 / 26 / 27 | 固定位置编辑、App 与文件夹整理、小组件配置保留 |

所有模式均包含程序坞与 App 资源库整理。iPad 的网格、程序坞和文件夹容量采用设备报告的参数。App 资源库内容根据已安装 App 和导出的布局推算。

<a id="mcp"></a>

## 使用 MCP 助手

打开 App 的 <strong>MCP 配置</strong>，通过原生客户端面板注册或更新 <strong>Codex、Claude Code</strong>，或复制适用于 <strong>Cursor、Claude Desktop、VS Code / Copilot</strong> 及其他 stdio 客户端的设置。新注册默认只读；高级设置展示并导出所选访问模式。配置状态、启动检查与实时连接状态分别显示。

任何本地 stdio MCP 客户端都可以启动 App 内置引擎：

```sh
HOMESCREEN_READ_ONLY=1 '/Applications/Screen Organizer.app/Contents/Resources/engine/engine' mcp
```

从旧名称升级或移动 App 后，请在 MCP 配置中选择<strong>更新</strong>，或修改客户端的可执行文件路径。发行文件名、命令与已保存的数据保持兼容。

源码安装使用 `.venv/bin/home-screen-mcp`。可让助手“按颜色整理 App”或“生成将最常用 App 放到第一页的草稿”。应用前检查 `diff` 与预览。

<strong>[MCP 配置指南](docs/MCP.zh-CN.md)</strong> · [English guide](docs/MCP.md) · [Codex](docs/MCP.zh-CN.md#codex) · [Cursor / Claude](docs/MCP.zh-CN.md#cursor-and-claude) · [VS Code / Copilot](docs/MCP.zh-CN.md#vs-code-and-copilot) · [MCPB](docs/MCP.zh-CN.md#standalone-mcpb)

服务器在 Mac 上运行，工具结果会交给 MCP 客户端；后续处理取决于客户端的模型和隐私设置。助手写入还需启用独立的应用开关。详见[数据处理与写入控制](docs/MCP.zh-CN.md#data-and-write-controls)。

<a id="development"></a>

## 开发与深入了解

- [源码安装、原生构建与发行](macapp/BUILDING.md)
- [提交或新增翻译](docs/TRANSLATING.md) — 英语、简体中文、繁體中文、西班牙语、德语、法语、俄语和葡萄牙语共用语言目录。
- [USB 协议、小组件、空位与使用估算](docs/HOW-IT-WORKS.md)
- [本地编辑器会话与 API](docs/LOCAL-EDITOR.md)
- [布局模式与诊断](docs/COMPATIBILITY.md)
- [MCP 分发与 Registry 发布](docs/MCP.zh-CN.md#distribution-and-discovery)

主要命令为 `home-screen-organizer` 和 `home-screen-mcp`。保留 `ios-homescreen-organizer`、`ios-homescreen-mcp`、`homescreen`、`homescreen-mcp` 别名。源码克隆目录仍为 `ios-homescreen-organizer`。

## 致谢与许可证

<strong>Jonathan Kats</strong> 创建了 Homescreen。原始 App、编辑器、截图和 SpringBoard 研究来自[上游项目](https://github.com/kats1123/homescreen-mcp)及其贡献者。本 fork 维护设备会话、备份、本地编辑器、Mac 启动器与发行流程。感谢 [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 提供 USB 接口。

Homescreen 原始源码采用 [MIT 许可证](LICENSE)。Copyright © 2026 Jonathan Kats。随附的 Python 引擎按 GPL 第 3 版或更高版本分发。发行版附有各组件的许可证、对应源码和重新构建说明。[下载对应源码](https://github.com/worldsavepoint/ios-homescreen-organizer/releases/download/v1.3.0/home-screen-organizer-1.3.0-corresponding-source.tar.gz)。

[反馈本 fork 的问题](https://github.com/worldsavepoint/ios-homescreen-organizer/issues)时，请附安装方式、复现步骤及脱敏后的错误信息。[上游源码](https://github.com/kats1123/homescreen-mcp) · [上游问题反馈](https://github.com/kats1123/homescreen-mcp/issues) · [上游发行版](https://github.com/kats1123/homescreen-mcp/releases)。

## 免责声明

Screen Organizer 与 Apple Inc. 无关联，未获其认可或赞助。

Mac、macOS、iPhone、iPad 和 Screen Time 是 Apple Inc. 的商标。IOS 是 Cisco 在美国及其他国家或地区的商标或注册商标，由 Apple 经许可使用。

本工具使用的私有设备接口可能随 iOS 更新而变化。应用或恢复布局前会保存主屏幕布局备份，并尝试通过回读核对结果。请另行保留 iPhone 或 iPad 最新的完整备份。适用的担保条款见随附的许可证。

<p align="center">
  <sub>README 访问计数</sub><br>
  <a href="https://github.com/journey-ad/Moe-Counter"><img src="https://count.moeyy.cn/@worldsavepoint.homescreen?theme=gelbooru&amp;padding=7&amp;scale=0.7&amp;darkmode=auto" alt="Screen Organizer README 访问计数 — Moe Counter" width="320"></a>
</p>
