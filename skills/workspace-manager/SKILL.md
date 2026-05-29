---
name: workspace-manager
description: 用户要切换工作区、更换项目目录、指定代码仓库路径，或询问当前工作区时使用。
---

# 工作区切换与管理

当用户涉及 **工作区 / 项目目录 / 换目录 / 切换到某仓库** 时，按本 skill 协助。

工作区是 Agent 执行 `read_file`、`write_file`、`list_directory`、`run_command` 的根目录；切换后上述工具只在该目录内生效。

## 核心原则

**必须调用工具切换，不要只告诉用户去改 .env 或手动 cd。**

- 查看当前工作区 → **`get_workspace`**
- 切换到新目录 → **`set_workspace(path=...)`
- 恢复 `.env` 默认 → **`set_workspace(path="default")`**

切换后会自动更新 system prompt 中的工作区路径，并写入对话历史，重启后仍生效。

## 用户侧命令（可选）

- `/workspace` — 查看当前工作区
- `/workspace /path/to/project` — 切换到指定目录
- `/workspace default` — 恢复 `WORKSPACE_DIR` 默认值
- `/workspace help` — 命令帮助

## set_workspace 参数

| 值 | 说明 |
|----|------|
| 绝对路径 | 如 `/home/user/Code/my-app` |
| `~` 或相对路径 | 如 `~/Code/ff-agent`、`../other-project` |
| `default` | 恢复 `.env` 中 `WORKSPACE_DIR` |

路径必须是**已存在的目录**，否则工具会返回错误。

## 典型流程

1. 用户说「把工作区换成 ~/Code/bar」→ 调用 **`set_workspace(path="~/Code/bar")`**
2. 切换成功后，可调用 **`list_directory(path=".")`** 列出根目录，向用户确认
3. 告知用户：后续文件读写与命令均在新工作区执行；TUI 底部状态栏会显示当前路径

## 与长期记忆

若用户经常在某几个项目间切换，可将常用路径写入 **`save_memory`**（如「默认前端项目在 ~/Code/web-app」），便于日后快速切换。

## 回复要求

操作完成后简要说明：

- 已切换到哪个目录（`display` 友好路径）
- 是否为默认工作区
- 后续工具操作的范围已变更
