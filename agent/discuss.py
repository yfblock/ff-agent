from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openai import OpenAIError

from agent.config import Config
from agent.llm import LLMClient, serialize_assistant_message
from agent.model_settings import ModelProfile, thinking_mode_for_model
from agent.tool_display import format_tool_detail, format_tool_title, try_parse_tool_args

DISCUSS_COMMAND_HELP = """讨论命令:
  /discuss                              显示帮助
  /discuss status                       查看进行中的讨论
  /discuss stop                         停止讨论
  /discuss <模型...> | <主题>           启动多模型讨论

示例:
  /discuss deepseek/chat,openai/gpt-4o | 如何设计 API 缓存层

规则:
  - 至少指定 2 个模型（名称同 /model list）
  - 讨论基于当前工作区，可使用 read_file / list_directory 读取文件后再发言
  - 模型达成一致 (CONSENSUS) 或你发消息 (/discuss stop) 结束
  - 停止后各模型分别给出自己的完整方案"""


DiscussResultKind = Literal["consensus", "stopped", "max_rounds"]
MAX_DISCUSS_TOOL_STEPS = 8


@dataclass(frozen=True)
class DiscussStart:
    profiles: tuple[ModelProfile, ...]
    topic: str


@dataclass
class DiscussTurn:
    round_num: int
    profile_name: str
    model: str
    content: str
    status: str


@dataclass
class DiscussFinal:
    profile_name: str
    model: str
    content: str


@dataclass
class UserSupplement:
    question: str
    answer: str


@dataclass
class DiscussState:
    topic: str
    profiles: tuple[ModelProfile, ...]
    workspace_context: str = ""
    transcript: list[DiscussTurn] = field(default_factory=list)
    user_supplements: list[UserSupplement] = field(default_factory=list)
    stop_reason: str = ""
    consensus_reached: bool = False
    consensus_summary: str = ""
    waiting_for_user: bool = False
    pending_question: str = ""


@dataclass(frozen=True)
class DiscussResult:
    kind: DiscussResultKind
    text: str


def build_workspace_context(
    workspace_dir: Path,
    list_entries: Callable[[], list[str]],
    *,
    display_name: str | None = None,
    max_entries: int = 100,
) -> str:
    path = workspace_dir.resolve()
    display = display_name or str(path)
    lines = [f"路径: {path}", f"显示: {display}"]
    try:
        entries = list_entries()
    except (OSError, ValueError) as exc:
        lines.append(f"目录列表不可用: {exc}")
        return "\n".join(lines)

    if not entries:
        lines.append("（工作区为空或无可列出的条目）")
        return "\n".join(lines)

    lines.append("根目录结构:")
    for entry in entries[:max_entries]:
        lines.append(f"  {entry}")
    if len(entries) > max_entries:
        lines.append(f"  …（共 {len(entries)} 项，已截断）")
    return "\n".join(lines)


def parse_discuss_models(raw: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,，\s]+", raw.strip()):
        part = part.strip()
        if not part or part in seen:
            continue
        seen.add(part)
        names.append(part)
    return names


def parse_discuss_start(text: str, resolve_profile: Callable[[str], ModelProfile]) -> DiscussStart | str:
    rest = text[len("/discuss") :].strip()
    if not rest:
        return DISCUSS_COMMAND_HELP

    if "|" in rest:
        models_part, topic = rest.split("|", 1)
    else:
        match = re.match(r"^(.+?)\s+(\S.+)$", rest)
        if not match or "," not in match.group(1):
            return "用法: /discuss model1,model2 | 讨论主题"
        models_part, topic = match.group(1), match.group(2)

    model_names = parse_discuss_models(models_part)
    topic = topic.strip()
    if len(model_names) < 2:
        return "至少需要 2 个模型，例如: /discuss deepseek/chat,openai/gpt-4o | 主题"
    if not topic:
        return "讨论主题不能为空"

    profiles: list[ModelProfile] = []
    seen_models: set[str] = set()
    for name in model_names:
        try:
            profile = resolve_profile(name)
        except ValueError as exc:
            return str(exc)
        key = f"{profile.name}:{profile.model}@{profile.base_url}"
        if key in seen_models:
            continue
        seen_models.add(key)
        profiles.append(profile)

    if len(profiles) < 2:
        return "至少需要 2 个不同的模型配置"
    return DiscussStart(profiles=tuple(profiles), topic=topic)


def parse_status(content: str) -> tuple[str, str]:
    text = (content or "").strip()
    if not text:
        return "continue", ""
    first_line = text.splitlines()[0].strip()
    upper = first_line.upper()
    body = text[len(first_line) :].lstrip("\n").strip()

    if upper.startswith("CONSENSUS:"):
        summary = first_line.split(":", 1)[1].strip()
        return "consensus", body or summary
    if upper.startswith("NEED_USER:") or upper.startswith("NEED-USER:"):
        question = first_line.split(":", 1)[1].strip()
        return "need_user", body or question
    return "continue", text


def format_transcript(transcript: list[DiscussTurn]) -> str:
    if not transcript:
        return "（尚无发言）"
    lines: list[str] = []
    current_round = 0
    for turn in transcript:
        if turn.round_num != current_round:
            current_round = turn.round_num
            lines.append(f"\n--- 第 {current_round} 轮 ---")
        lines.append(f"[{turn.profile_name}] ({turn.status})\n{turn.content}")
    return "\n".join(lines).strip()


def format_user_supplements(supplements: list[UserSupplement]) -> str:
    if not supplements:
        return ""
    lines = ["用户已补充的信息:"]
    for idx, item in enumerate(supplements, start=1):
        lines.extend(
            [
                f"{idx}. 专家提问: {item.question}",
                f"   用户回复: {item.answer}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_need_user_prompt(question: str) -> str:
    return (
        "讨论已暂停，专家需要您补充信息：\n\n"
        f"{question.strip() or '（未说明）'}\n\n"
        "请直接回复上述信息，讨论将自动继续。\n"
        "若需终止讨论，请发送 /discuss stop。"
    )


def format_consensus_result(state: DiscussState) -> str:
    parts = [
        "讨论已达成一致。",
        f"主题: {state.topic}",
        "",
        state.consensus_summary or "（无摘要）",
    ]
    return "\n".join(parts)


def format_final_proposals(
    state: DiscussState,
    finals: list[DiscussFinal],
    *,
    stopped: bool,
) -> str:
    header = "讨论已停止，各模型最终方案：" if stopped else "讨论结束，各模型最终方案："
    parts = [header, f"主题: {state.topic}", ""]
    if state.stop_reason.strip():
        parts.extend([f"停止说明: {state.stop_reason.strip()}", ""])
    for item in finals:
        parts.extend(
            [
                f"=== {item.profile_name} ({item.model}) ===",
                item.content.strip() or "（无内容）",
                "",
            ]
        )
    return "\n".join(parts).strip()


class DiscussRunner:
    def __init__(
        self,
        *,
        config: Config,
        state: DiscussState,
        stop_event: threading.Event,
        wait_for_user: Callable[[str], str],
        handle_tool: Callable[[str, str], str],
        tools: list[dict[str, Any]],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        max_rounds: int = 10,
        max_tool_steps: int = MAX_DISCUSS_TOOL_STEPS,
    ) -> None:
        self.config = config
        self.state = state
        self.stop_event = stop_event
        self.wait_for_user = wait_for_user
        self.handle_tool = handle_tool
        self.tools = tools
        self.on_event = on_event
        self.max_rounds = max(1, max_rounds)
        self.max_tool_steps = max(1, max_tool_steps)
        self._clients: dict[str, LLMClient] = {}

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event:
            self.on_event(payload)

    def _client_key(self, profile: ModelProfile) -> str:
        return f"{profile.name}\0{profile.model}\0{profile.base_url}\0{profile.api_key}"

    def _client_for(self, profile: ModelProfile) -> LLMClient:
        key = self._client_key(profile)
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        client = LLMClient(self.config)
        client.apply_settings(
            model=profile.model,
            base_url=profile.base_url,
            api_key=profile.api_key,
            thinking_mode=thinking_mode_for_model(
                profile.model,
                locked=self.config.thinking_mode_locked,
                locked_value=self.config.thinking_mode,
            ),
        )
        self._clients[key] = client
        return client

    def _participant_system(self, profile: ModelProfile) -> str:
        others = ", ".join(
            p.name for p in self.state.profiles if p.name != profile.name
        )
        supplements = format_user_supplements(self.state.user_supplements)
        workspace_block = self.state.workspace_context.strip() or "（未提供）"
        supplement_block = supplements or "（尚无）"
        return (
            f"你是一位参与多专家讨论的 AI 助手（身份: {profile.name}，模型: {profile.model}）。\n"
            f"讨论主题: {self.state.topic}\n"
            f"其他参与者: {others}\n\n"
            f"当前工作区:\n{workspace_block}\n\n"
            f"{supplement_block}\n\n"
            "规则:\n"
            "1. 方案必须结合当前工作区内的实际项目情况，可引用目录中的模块/文件。\n"
            "2. 阅读全部讨论记录，在本轮给出清晰、可执行的观点。\n"
            "3. 第一行必须是状态行，只能是以下格式之一:\n"
            "   - CONSENSUS: <你认为此时已可全员采纳的完整方案>\n"
            "   - NEED_USER: <需要用户补充的关键信息，一条说清>\n"
            "   - CONTINUE: <简要说明为何仍需讨论>\n"
            "   然后从第三行起写本轮正式发言。\n"
            "4. 若缺少关键信息且无法从工作区或讨论记录推断，必须使用 NEED_USER；"
            "一旦发出 NEED_USER，讨论将立即暂停等待用户回复。\n"
            "5. 仅当方案已完整且可执行时才使用 CONSENSUS。\n"
            "6. 可使用工具 read_file、list_directory、get_workspace 读取工作区内容；"
            "需要看具体实现时请先读文件再下结论。\n"
            "7. 工具调用完成后，最终回复的第一行仍必须是上述状态行之一。\n"
            "8. 使用中文，聚焦可执行方案。"
        )

    def _round_user_prompt(self, round_num: int) -> str:
        history = format_transcript(self.state.transcript)
        if round_num == 1 and not self.state.transcript:
            return (
                "请基于当前工作区，开始第 1 轮讨论，给出你的初始观点。"
                "若信息不足，请使用 NEED_USER。"
            )
        return (
            f"截至目前的讨论:\n{history}\n\n"
            f"请给出第 {round_num} 轮发言。"
        )

    def _complete_with_tools(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        *,
        profile: ModelProfile,
        round_num: int,
    ) -> str:
        for _ in range(self.max_tool_steps):
            if self.stop_event.is_set():
                break
            response = client.chat(messages, tools=self.tools)
            message = response.choices[0].message
            if not message.tool_calls:
                return (message.content or "").strip()
            messages.append(serialize_assistant_message(message))
            for call in message.tool_calls:
                name = call.function.name
                arguments = call.function.arguments or "{}"
                args = try_parse_tool_args(arguments)
                self._emit(
                    {
                        "type": "discuss_tool",
                        "round": round_num,
                        "profile": profile.name,
                        "name": name,
                        "title": format_tool_title(name, args),
                        "detail": format_tool_detail(name, args),
                    }
                )
                result = self.handle_tool(name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    }
                )
        return ""

    def _ask_participant(self, profile: ModelProfile, round_num: int) -> DiscussTurn:
        client = self._client_for(profile)
        messages = [
            {"role": "system", "content": self._participant_system(profile)},
            {"role": "user", "content": self._round_user_prompt(round_num)},
        ]
        content = self._complete_with_tools(
            client,
            messages,
            profile=profile,
            round_num=round_num,
        )
        status, body = parse_status(content)
        return DiscussTurn(
            round_num=round_num,
            profile_name=profile.name,
            model=profile.model,
            content=body or content,
            status=status,
        )

    def _pause_for_user(self, question: str, round_num: int) -> None:
        self.state.waiting_for_user = True
        self.state.pending_question = question
        prompt = format_need_user_prompt(question)
        self._emit(
            {
                "type": "discuss_need_user",
                "round": round_num,
                "question": question,
                "message": prompt,
            }
        )
        answer = self.wait_for_user(question).strip()
        self.state.waiting_for_user = False
        self.state.pending_question = ""
        if self.stop_event.is_set():
            return
        if not answer:
            answer = "（用户未提供内容）"
        self.state.user_supplements.append(UserSupplement(question=question, answer=answer))
        self._emit(
            {
                "type": "discuss_user_supplement",
                "question": question,
                "answer": answer,
            }
        )

    def _round_has_consensus(self, round_num: int) -> bool:
        turns = [t for t in self.state.transcript if t.round_num == round_num]
        if len(turns) != len(self.state.profiles):
            return False
        return all(turn.status == "consensus" for turn in turns)

    def _build_consensus_summary(self, round_num: int) -> str:
        turns = [t for t in self.state.transcript if t.round_num == round_num]
        parts = [
            f"主题: {self.state.topic}",
            f"工作区: {self.state.workspace_context.splitlines()[0] if self.state.workspace_context else '未知'}",
            f"讨论轮次: {round_num}",
            "",
            "一致方案:",
        ]
        for turn in turns:
            parts.extend([f"[{turn.profile_name}]", turn.content, ""])
        supplements = format_user_supplements(self.state.user_supplements)
        if supplements:
            parts.extend(["", supplements])
        return "\n".join(parts).strip()

    def _collect_final_proposals(self) -> list[DiscussFinal]:
        finals: list[DiscussFinal] = []
        history = format_transcript(self.state.transcript)
        supplements = format_user_supplements(self.state.user_supplements)
        stop_note = self.state.stop_reason.strip() or "（无）"
        for profile in self.state.profiles:
            client = self._client_for(profile)
            prompt = (
                f"多专家讨论已结束。\n"
                f"主题: {self.state.topic}\n"
                f"当前工作区:\n{self.state.workspace_context}\n\n"
                f"{supplements}\n\n"
                f"结束原因: {'用户停止' if self.state.stop_reason else '未达成一致'}\n"
                f"用户停止时的消息: {stop_note}\n\n"
                f"讨论记录:\n{history}\n\n"
                "请独立给出你对该主题的完整最终方案（无需再考虑他人是否同意）。"
                "直接输出方案正文，不要加状态行。"
            )
            messages = [
                {"role": "system", "content": self._participant_system(profile)},
                {"role": "user", "content": prompt},
            ]
            try:
                content = self._complete_with_tools(
                    client,
                    messages,
                    profile=profile,
                    round_num=0,
                )
            except OpenAIError as exc:
                content = f"生成失败: {exc}"
            finals.append(
                DiscussFinal(
                    profile_name=profile.name,
                    model=profile.model,
                    content=content,
                )
            )
            self._emit(
                {
                    "type": "discuss_final",
                    "profile": profile.name,
                    "model": profile.model,
                    "content": content,
                }
            )
        return finals

    def run(self) -> DiscussResult:
        self._emit(
            {
                "type": "discuss_start",
                "topic": self.state.topic,
                "profiles": [p.name for p in self.state.profiles],
                "workspace": self.state.workspace_context.splitlines()[0]
                if self.state.workspace_context
                else "",
            }
        )

        for round_num in range(1, self.max_rounds + 1):
            if self.stop_event.is_set():
                break
            self._emit({"type": "discuss_round", "round": round_num})
            round_paused = False
            for profile in self.state.profiles:
                if self.stop_event.is_set():
                    break
                self._emit(
                    {
                        "type": "discuss_turn_start",
                        "round": round_num,
                        "profile": profile.name,
                        "model": profile.model,
                    }
                )
                try:
                    turn = self._ask_participant(profile, round_num)
                except OpenAIError as exc:
                    turn = DiscussTurn(
                        round_num=round_num,
                        profile_name=profile.name,
                        model=profile.model,
                        content=f"调用失败: {exc}",
                        status="continue",
                    )
                self.state.transcript.append(turn)
                self._emit(
                    {
                        "type": "discuss_turn",
                        "round": round_num,
                        "profile": profile.name,
                        "model": profile.model,
                        "status": turn.status,
                        "content": turn.content,
                    }
                )
                if turn.status == "need_user":
                    self._pause_for_user(turn.content, round_num)
                    round_paused = True
                    break

            if round_paused:
                if self.stop_event.is_set():
                    break
                continue

            if self._round_has_consensus(round_num):
                self.state.consensus_reached = True
                self.state.consensus_summary = self._build_consensus_summary(round_num)
                self._emit(
                    {
                        "type": "discuss_consensus",
                        "round": round_num,
                        "summary": self.state.consensus_summary,
                    }
                )
                text = format_consensus_result(self.state)
                self._emit({"type": "discuss_end", "result": text})
                return DiscussResult(kind="consensus", text=text)

        finals = self._collect_final_proposals()
        stopped = self.stop_event.is_set()
        text = format_final_proposals(self.state, finals, stopped=stopped)
        self._emit({"type": "discuss_end", "result": text})
        kind: DiscussResultKind = "stopped" if stopped else "max_rounds"
        return DiscussResult(kind=kind, text=text)
