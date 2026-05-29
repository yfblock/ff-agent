#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from openai import OpenAIError

from agent.core import Agent
from agent.errors import format_api_error
from agent.session_hub import SessionHub


def run_plain(agent: Agent) -> None:
    print(f"{agent.config.agent_name} 已启动（输入 exit 退出，/reset 清空会话）")
    print(
        f"已加载 {len(agent.skills)} 个 skill（/skills 查看列表），"
        f"记忆文件: {agent.config.memory_path}（/memory help 查看记忆命令）"
    )
    if agent.history:
        print(f"对话历史: {agent.history.path}")
    if agent.has_restored_history():
        turns = sum(1 for msg in agent.messages if msg.get("role") == "user")
        print(f"已恢复 {turns} 轮对话上下文。")
    print()

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue

        from agent.commands import execute_command

        command_result, should_exit = execute_command(agent, user_input)
        if should_exit:
            print("再见。")
            break
        if command_result is not None:
            print(f"\n{command_result}\n")
            continue

        try:
            reply = agent.chat(user_input)
        except OpenAIError as exc:
            print(f"\nAPI 错误: {format_api_error(exc)}\n", file=sys.stderr)
            continue
        print(f"\nAgent: {reply}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="支持 skills 与长期记忆的简单 Agent")
    parser.add_argument("-m", "--message", help="单次对话后退出")
    parser.add_argument("--list-skills", action="store_true", help="列出 skills 后退出")
    parser.add_argument("--plain", action="store_true", help="使用纯文本交互模式（非 TUI）")
    parser.add_argument(
        "--channel-login",
        metavar="CHANNEL",
        help="扫码登录消息渠道（当前支持: wechat）",
    )
    parser.add_argument(
        "--channel-gateway",
        action="store_true",
        help="启动 Channel Gateway（接入微信等消息渠道）",
    )
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="列出已启用的 channel 配置",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动 Web 界面与 API（聊天与设置）",
    )
    parser.add_argument("--env", help=".env 文件路径")
    args = parser.parse_args()

    if args.list_channels:
        try:
            from agent.channels.registry import list_channel_specs
            from agent.config import load_channel_config

            channel_config = load_channel_config(args.env)
            specs = list_channel_specs(channel_config)
            if not specs:
                print("当前未启用任何 channel。请在 .env 设置 CHANNELS=wechat 并配置微信参数。")
                return 0
            print("已启用的 Channel:\n")
            for spec in specs:
                print(f"- {spec.id}: {spec.title}")
                print(f"  {spec.description}")
            cfg = load_channel_config(args.env)
            if cfg.wechat_ilink.enabled:
                print("\n模式: 微信 iLink 扫码登录（默认，同 OpenClaw）")
                print(f"凭证文件: {cfg.wechat_ilink.credentials_path}")
                print("登录命令: python main.py --channel-login wechat")
                print("启动 TUI（自动接入微信）: python main.py")
                print("仅 Gateway: python main.py --channel-gateway")
            if cfg.wechat_official.enabled:
                print("\n模式: 微信公众号 Webhook")
                print(f"Webhook 路径: {cfg.wechat_official.webhook_path}")
                print(
                    f"Gateway 地址: http://{cfg.gateway_host}:{cfg.gateway_port}{cfg.wechat_official.webhook_path}"
                )
            return 0
        except ValueError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 1

    if args.channel_login:
        channel_name = args.channel_login.strip().lower()
        if channel_name != "wechat":
            print(f"未知 channel: {channel_name}。当前支持: wechat", file=sys.stderr)
            return 1
        try:
            from agent.config import load_channel_config
            from agent.channels.wechat.ilink import login_wechat_ilink

            channel_config = load_channel_config(args.env)
            if not channel_config.wechat_ilink.enabled:
                print(
                    "当前未启用微信 iLink 模式。请在 .env 设置 CHANNELS=wechat（默认 WECHAT_MODE=ilink）。",
                    file=sys.stderr,
                )
                return 1
            login_wechat_ilink(channel_config.wechat_ilink)
        except Exception as exc:
            print(f"微信登录失败: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.channel_gateway:
        try:
            from agent.channels.gateway import run_channel_gateway

            run_channel_gateway(args.env, hub=SessionHub(load_config(args.env)))
        except ValueError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 1
        except ImportError as exc:
            print(f"导入失败: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.web:
        try:
            from agent.config import load_config
            from agent.web.server import run_web_server

            config = load_config(args.env)
            hub = SessionHub(config)
            run_web_server(args.env, hub=hub)
        except ValueError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 1
        except ImportError as exc:
            print(f"导入失败: {exc}（请安装 fastapi uvicorn）", file=sys.stderr)
            return 1
        return 0

    if args.list_skills:
        try:
            from agent.config import load_config
            from agent.skills import format_skills_list, load_skills

            config = load_config(args.env, require_api_key=False)
            skills = load_skills(config.skills_dirs)
            print(format_skills_list(skills, config.skills_dirs))
        except ValueError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        from agent.config import load_channel_config, load_config

        config = load_config(args.env)
        if args.message:
            agent = Agent(config, session_key=None)
        else:
            hub = SessionHub(config)
            agent = hub.local_agent
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    if args.message:
        try:
            print(agent.chat(args.message))
        except OpenAIError as exc:
            print(f"API 错误: {format_api_error(exc)}", file=sys.stderr)
            return 1
        return 0

    gateway_active = False
    web_url = ""
    if not args.plain:
        from agent.channels.gateway import start_channel_gateway_background

        channel_config = load_channel_config(args.env)
        if channel_config.enabled and config.auto_start_gateway:
            _, _ = start_channel_gateway_background(args.env, hub)
            gateway_active = True

        if config.auto_start_web:
            from agent.web.server import start_web_server_background

            web_url, _ = start_web_server_background(args.env, hub)

    if args.plain:
        run_plain(agent)
    else:
        from agent.tui import run_tui

        run_tui(agent, gateway_active=gateway_active, web_url=web_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
