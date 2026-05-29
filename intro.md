# ff-agent 项目介绍

## 🧠 一句话概括

**ff-agent** 是一个支持 **Skills、角色切换、长期记忆和规划执行**的 AI Agent，基于 OpenAI 兼容接口（默认 DeepSeek），提供 TUI 交互界面、命令行模式，并支持接入微信消息渠道。

---

## 📁 项目结构

```
ff-agent/
├── agent/                   # 核心代码
│   ├── core.py              # Agent 主循环：system prompt 构建、工具调用、对话
│   ├── config.py            # 配置加载（.env）
│   ├── commands.py          # 斜杠命令：/role /memory /plan /skills 等
│   ├── llm.py               # LLM 流式调用，支持 DeepSeek thinking 模式
│   ├── memory.py            # 长期记忆存储（JSON 文件）
│   ├── skills.py            # Skills 加载与格式化
│   ├── roles.py             # Roles 加载与格式化
│   ├── planner.py           # 规划与执行（create_plan → 逐步执行）
│   ├── executor.py          # 工作区执行器（读写文件、shell 命令）
│   ├── tools.py             # 工具定义（给 LLM 用的 function calling schema）
│   ├── tool_display.py      # 工具调用的 UI 显示格式化
│   ├── tui.py               # Textual TUI 终端界面（带实时活动面板）
│   ├── errors.py            # 错误处理
│   └── channels/            # 消息渠道（微信接入）
│       └── wechat/          # 微信 iLink 扫码登录 / 公众号 Webhook
├── roles/                   # 身份角色
│   ├── default/             # 默认通用助手
│   ├── coder/               # 工程师模式
│   ├── writer/              # 写作模式
│   └── lianzige/            # 自定义角色
├── skills/                  # 专项技能
│   ├── example/             # 示例 skill
│   ├── plan-and-execute/    # 规划执行工作流
│   └── role-manager/        # 角色管理
├── data/                    # 数据存储（记忆 JSON）
├── main.py                  # 入口文件
├── pyproject.toml           # 项目依赖 & 构建
└── .env                     # 环境配置
```

---

## 🔧 核心特性

| 特性 | 说明 |
|------|------|
| **Skills** | 专项技能指令，放 `skills/<name>/SKILL.md`，自动加载进 system prompt |
| **角色切换** | `/role coder` 切换身份，共享聊天历史，人格和语气跟着变 |
| **长期记忆** | 对话中自动保存/检索，支持 `/memory add` / `/memory delete` 手动管理 |
| **规划执行** | 复杂任务自动拆步骤 → `create_plan` → 逐步执行 → `complete_plan` |
| **工具调用** | 读写文件、执行 shell 命令（工作区限定）、内存操作、角色管理 |
| **TUI 界面** | 基于 Textual 构建，实时显示思考过程、工具调用、回复生成 |
| **微信接入** | 支持 iLink 扫码登录（类似 OpenClaw）和微信公众号 Webhook |
| **DeepSeek** | 默认配置 DeepSeek API，支持 thinking/reasoner 推理模式 |

---

## 🚀 使用方法

```bash
# TUI 模式（默认）
python main.py

# 纯文本交互
python main.py --plain

# 单次对话
python main.py -m "你好"

# 微信扫码登录
python main.py --channel-login wechat

# 启动微信 Gateway
python main.py --channel-gateway
```

### 🔤 斜杠命令

| 命令 | 作用 |
|------|------|
| `/role` | 列出角色 / 切换身份 |
| `/memory` | 查看/管理长期记忆 |
| `/plan` | 查看当前执行计划 |
| `/skills` | 重新加载 skills |
| `/reset` | 清空会话（保留记忆） |
| `/exit` | 退出 |

---

## 🛠 技术栈

- **Python ≥ 3.10**
- **OpenAI SDK** — 兼容 DeepSeek / 任何 OpenAI 格式接口
- **Textual** — TUI 终端 UI 框架
- **wechatpy** — 微信消息处理
- **python-dotenv** — 配置管理
- **PyYAML** — SKILL.md YAML frontmatter 解析
