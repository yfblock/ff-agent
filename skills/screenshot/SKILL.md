---
name: screenshot
description: 用户要看屏幕、桌面、当前界面、截屏时使用；微信里让用户看电脑画面也适用。
---

# 屏幕截图（skill 工作流）

**没有**专用截图工具。用 `run_command` 截图，微信下再 `send_attachment`。

## GNOME 用户必读

| 命令 | 结果 |
|------|------|
| `grim` | ❌ `compositor doesn't support the screen capture protocol`（GNOME 不支持） |
| `gdbus … org.gnome.Shell.Screenshot` | ❌ `Screenshot is not allowed`（已禁止第三方直接调用） |
| **本项目脚本** | ✅ 走 **xdg-desktop-portal**（正确方式） |

## 推荐命令

```bash
bash scripts/capture-screen.sh screenshots/screen.png
```

或：

```bash
python3 scripts/portal-screenshot.py screenshots/screen.png
```

首次若失败，在本机**图形界面终端**（非 SSH）试一次交互模式：

```bash
python3 scripts/portal-screenshot.py screenshots/screen.png --interactive
```

并在 **设置 → 隐私 → 屏幕截图** 中允许 Terminal / Python。

## 微信发送

确认 PNG 已生成后，**只调用一次**：

```text
send_attachment(path="screenshots/screen.png")
```

## Agent 禁止

- 在 GNOME 上使用 `grim` 或 Shell D-Bus 截图。
- 截图/发送成功后不要重复执行。
- Gateway 需在与微信相同的**桌面登录会话**中运行（`python main.py --channel-gateway`），否则 portal 无权限。
