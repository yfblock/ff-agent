---
name: role-manager
description: 用户要创建、编辑、删除、列出或切换 role（身份/角色）时使用；也适用于询问 role 文件格式、/role 命令、如何定制助手人格与偏好。
---

# Role 创建与管理

当用户涉及 **role / 身份 / 角色 / 人格 / 偏好切换** 时，按本 skill 协助。Role 只影响 system prompt 中的身份与风格，**不清空聊天历史**。

## 核心原则

**必须自动写文件，禁止让用户手动复制粘贴。**

- 创建或修改 role → 调用 **`save_role`** 工具
- 删除 role → 调用 **`delete_role`** 工具
- 查看已有 role → 调用 **`list_roles`** 工具，或让用户执行 `/role`

工具会自动写入 `roles/<name>/ROLE.md` 并 reload，无需用户手动保存。

## 可用命令（用户侧）

- `/role` — 列出全部 role 及当前身份
- `/role <name>` — 切换身份（保留历史）
- `/role reload` — 重新扫描 roles 目录（工具保存后通常已自动生效）
- `/role help` — 命令帮助

配置：`.env` 中 `ROLES_DIR=./roles`，`DEFAULT_ROLE=default`

## 使用 save_role 创建/更新

收集需求后，直接调用工具，参数如下：

| 参数 | 说明 |
|------|------|
| `name` | 小写英文/数字/连字符，如 `reviewer` |
| `title` | 展示名称，如「代码审查员」 |
| `description` | 一句话用途说明 |
| `body` | Markdown 正文（**不含** YAML frontmatter） |
| `switch_to` | 默认 `true`，保存后立即切换 |

`body` 建议结构：

```markdown
# 身份与风格

- 身份定位
- 回答风格与偏好
- 语言、格式、禁忌
```

## 使用 delete_role 删除

传入 `name` 即可删除整个 `roles/<name>/` 目录。删除前确认用户意图；若删的是当前 role，系统会自动回退到其他可用 role。

## 编辑已有 role

1. 先用 `list_roles` 或 `/role` 确认目标
2. 读取用户需求，调用 **`save_role`** 覆写（同名即更新）
3. 告知用户文件路径与是否已切换

## 协助切换身份

用户说「换成工程师模式」等：

1. 已有合适 role → 建议 `/role coder`，或创建后 `save_role` 并 `switch_to: true`
2. 没有合适 role → 询问关键偏好后 **`save_role` 自动创建**

## 与 skill 的区分

- **role** = 稳定身份与人格偏好（谁在说、怎么说）
- **skill** = 特定任务工作流（做什么事、按什么步骤）

## 回复要求

操作完成后简要说明：

- 创建/更新了哪个 role
- 文件路径（工具返回的 `path`）
- 是否已切换为当前身份
- 如何用 `/role <name>` 手动切换
