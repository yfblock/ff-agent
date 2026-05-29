const state = {
  settings: null,
  busy: false,
  draftSettings: {},
  historyTotal: 0,
};

const el = {
  agentName: document.getElementById("agent-name"),
  messages: document.getElementById("messages"),
  activity: document.getElementById("activity"),
  activityText: document.getElementById("activity-text"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  btnSend: document.getElementById("btn-send"),
  btnReset: document.getElementById("btn-reset"),
  settingsPanel: document.getElementById("settings-panel"),
  overlay: document.getElementById("overlay"),
  btnToggleSettings: document.getElementById("btn-toggle-settings"),
  btnCloseSettings: document.getElementById("btn-close-settings"),
  modelSelect: document.getElementById("model-select"),
  modelDetail: document.getElementById("model-detail"),
  roleSelect: document.getElementById("role-select"),
  roleDetail: document.getElementById("role-detail"),
  workspaceInput: document.getElementById("workspace-input"),
  workspaceDetail: document.getElementById("workspace-detail"),
  btnWorkspaceDefault: document.getElementById("btn-workspace-default"),
  thinkingToggle: document.getElementById("thinking-toggle"),
  thinkingDetail: document.getElementById("thinking-detail"),
  btnSaveSettings: document.getElementById("btn-save-settings"),
  settingsStatus: document.getElementById("settings-status"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!response.ok) {
    const message = data?.detail || response.statusText || "请求失败";
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function displayRole(entryType, text) {
  if (entryType === "user") return "你";
  if (entryType === "assistant") return "Agent";
  if (entryType === "thinking") return "思考";
  if (entryType === "tool") return "工具";
  return "系统";
}

function displayClass(entryType) {
  if (entryType === "user") return "user";
  if (entryType === "assistant") return "assistant";
  return "system";
}

function renderDisplayEntries(entries, { replace = false } = {}) {
  if (replace) {
    el.messages.innerHTML = "";
  }
  if (replace && !entries.length) {
    appendSystemMessage("开始对话吧。与 TUI 共享同一份 chat_history 展示记录。");
    return;
  }
  for (const entry of entries) {
    renderDisplayEntry(entry);
  }
  scrollToBottom();
}

function renderDisplayEntry(entry) {
  const entryType = String(entry.type || "system");
  const text = String(entry.text || "");
  if (!text.trim()) return null;
  if (entryType === "thinking") {
    showActivity(text);
    return null;
  }
  const node = document.createElement("div");
  node.className = `message ${displayClass(entryType)}`;
  node.innerHTML = `<div class="message-meta">${displayRole(entryType, text)}</div>${escapeHtml(text)}`;
  el.messages.appendChild(node);
  return node;
}

function appendSystemMessage(text) {
  renderDisplayEntry({ type: "system", text });
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

function setBusy(busy) {
  state.busy = busy;
  el.btnSend.disabled = busy;
  el.chatInput.disabled = busy;
}

function showActivity(text) {
  if (!text) {
    el.activity.classList.add("hidden");
    el.activityText.textContent = "";
    return;
  }
  el.activity.classList.remove("hidden");
  el.activityText.textContent = text;
}

function fillSettingsForm(settings) {
  state.settings = settings;
  state.draftSettings = {
    model: settings.model.profile,
    role: settings.role.name,
    workspace: settings.workspace.path,
    thinking_mode: settings.model.thinking_mode,
  };

  el.agentName.textContent = settings.agent_name;
  el.modelSelect.innerHTML = settings.models
    .map(
      (item) =>
        `<option value="${escapeHtml(item.name)}" ${item.current ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.model)}</option>`,
    )
    .join("");
  updateModelDetail();

  el.roleSelect.innerHTML = settings.roles
    .map(
      (item) =>
        `<option value="${escapeHtml(item.name)}" ${item.current ? "selected" : ""}>${escapeHtml(item.title)} (${escapeHtml(item.name)})</option>`,
    )
    .join("");
  updateRoleDetail();

  el.workspaceInput.value = settings.workspace.path;
  el.workspaceDetail.textContent = `显示: ${settings.workspace.display}${settings.workspace.is_default ? "（默认）" : ""}`;

  el.thinkingToggle.checked = settings.model.thinking_mode;
  el.thinkingToggle.disabled = settings.model.thinking_mode_locked;
  el.thinkingDetail.textContent = settings.model.thinking_mode_locked
    ? "已在 .env 中固定，无法在线修改"
    : "开启后模型会进行更深度推理（若 API 支持）";
}

function updateModelDetail() {
  const name = el.modelSelect.value;
  const item = state.settings?.models.find((m) => m.name === name);
  if (!item) return;
  el.modelDetail.textContent = `${item.model} @ ${item.base_url}`;
}

function updateRoleDetail() {
  const name = el.roleSelect.value;
  const item = state.settings?.roles.find((r) => r.name === name);
  if (!item) return;
  el.roleDetail.textContent = item.description || "无描述";
}

function collectSettingsPatch() {
  const patch = {};
  if (el.modelSelect.value !== state.settings.model.profile) {
    patch.model = el.modelSelect.value;
  }
  if (el.roleSelect.value !== state.settings.role.name) {
    patch.role = el.roleSelect.value;
  }
  const workspace = el.workspaceInput.value.trim();
  if (workspace && workspace !== state.settings.workspace.path) {
    patch.workspace = workspace;
  }
  if (!state.settings.model.thinking_mode_locked) {
    const thinking = el.thinkingToggle.checked;
    if (thinking !== state.settings.model.thinking_mode) {
      patch.thinking_mode = thinking;
    }
  }
  return patch;
}

async function loadSettings() {
  const settings = await api("/api/settings");
  fillSettingsForm(settings);
}

async function applyHistoryPayload(data, { replace = false } = {}) {
  if (replace) {
    renderDisplayEntries(data.entries || [], { replace: true });
  } else if (data.entries?.length) {
    for (const entry of data.entries) {
      renderDisplayEntry(entry);
    }
    scrollToBottom();
  }
  if (typeof data.total === "number") {
    state.historyTotal = data.total;
  }
  if (data.settings) {
    fillSettingsForm(data.settings);
  }
}

async function loadMessages() {
  const data = await api("/api/messages");
  await applyHistoryPayload(data, { replace: true });
}

async function pollHistory() {
  if (state.busy) return;
  try {
    const data = await api(`/api/messages?since=${state.historyTotal}`);
    await applyHistoryPayload(data);
  } catch {
    // 忽略轮询失败
  }
}

async function saveSettings() {
  const patch = collectSettingsPatch();
  el.settingsStatus.textContent = "";
  el.settingsStatus.className = "status";
  if (!Object.keys(patch).length) {
    el.settingsStatus.textContent = "没有需要保存的更改";
    return;
  }
  try {
    const result = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    fillSettingsForm(result.settings);
    el.settingsStatus.textContent = result.changes?.length
      ? `已保存: ${result.changes.join("；")}`
      : "已保存";
    el.settingsStatus.className = "status ok";
  } catch (error) {
    el.settingsStatus.textContent = error.message;
    el.settingsStatus.className = "status error";
  }
}

async function resetChat() {
  if (!confirm("确定清空当前会话？")) return;
  const result = await api("/api/reset", { method: "POST", body: "{}" });
  state.historyTotal = result.total || 0;
  renderDisplayEntries([], { replace: true });
  appendSystemMessage(result.message);
}

async function sendMessage(text) {
  setBusy(true);
  showActivity("");
  el.chatInput.value = "";

  let assistantNode = null;
  let assistantText = "";

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(err.detail || "流式请求失败");
    }

    await applyHistoryPayload(await api(`/api/messages?since=${state.historyTotal}`));

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";

      for (const chunk of chunks) {
        const line = chunk.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const payload = JSON.parse(line.slice(6));
        handleStreamEvent(payload, {
          getAssistantNode: () => {
            if (!assistantNode) {
              assistantNode = renderDisplayEntry({ type: "assistant", text: "" });
              if (!assistantNode) {
                assistantNode = document.createElement("div");
                assistantNode.className = "message assistant";
                el.messages.appendChild(assistantNode);
              }
            }
            return assistantNode;
          },
          setAssistantText: (value) => {
            assistantText = value;
          },
          getAssistantText: () => assistantText,
        });
      }
    }
    scrollToBottom();
  } catch (error) {
    appendSystemMessage(`错误: ${error.message}`);
  } finally {
    setBusy(false);
    showActivity("");
    await pollHistory();
  }
}

function handleStreamEvent(event, ctx) {
  const type = event.type;
  if (type === "thinking_delta") {
    showActivity(event.text || "");
    return;
  }
  if (type === "thinking_done") {
    showActivity("");
    return;
  }
  if (type === "content_delta") {
    const node = ctx.getAssistantNode();
    const text = event.text || "";
    ctx.setAssistantText(text);
    node.innerHTML = `<div class="message-meta">Agent</div>${escapeHtml(text)}`;
    scrollToBottom();
    return;
  }
  if (type === "tool_call_delta" || type === "tool_start") {
    const title = event.title || event.name || "工具";
    showActivity(`工具: ${title}`);
    return;
  }
  if (type === "command") {
    renderDisplayEntry({ type: "system", text: event.reply || "" });
    return;
  }
  if (type === "done") {
    const reply = event.reply || ctx.getAssistantText();
    if (reply) {
      const node = ctx.getAssistantNode();
      if (node) {
        node.innerHTML = `<div class="message-meta">Agent</div>${escapeHtml(reply)}`;
      } else {
        renderDisplayEntry({ type: "assistant", text: reply });
      }
    }
    if (typeof event.total === "number") {
      state.historyTotal = event.total;
    }
    return;
  }
  if (type === "error") {
    appendSystemMessage(`错误: ${event.message || "未知错误"}`);
  }
}

function toggleSettings(open) {
  const isMobile = window.matchMedia("(max-width: 900px)").matches;
  if (!isMobile) return;
  const shouldOpen = open ?? el.settingsPanel.classList.contains("hidden-mobile");
  el.settingsPanel.classList.toggle("hidden-mobile", !shouldOpen);
  el.overlay.classList.toggle("hidden", !shouldOpen);
}

el.chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = el.chatInput.value.trim();
  if (!text || state.busy) return;
  sendMessage(text);
});

el.chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.chatForm.requestSubmit();
  }
});

el.btnReset.addEventListener("click", resetChat);
el.btnSaveSettings.addEventListener("click", saveSettings);
el.modelSelect.addEventListener("change", updateModelDetail);
el.roleSelect.addEventListener("change", updateRoleDetail);
el.btnWorkspaceDefault.addEventListener("click", () => {
  if (state.settings) {
    el.workspaceInput.value = state.settings.workspace.default_path;
  }
});
el.btnToggleSettings.addEventListener("click", () => toggleSettings(true));
el.btnCloseSettings.addEventListener("click", () => toggleSettings(false));
el.overlay.addEventListener("click", () => toggleSettings(false));

async function bootstrap() {
  try {
    await loadSettings();
    await loadMessages();
    setInterval(pollHistory, 2000);
  } catch (error) {
    appendSystemMessage(`初始化失败: ${error.message}`);
  }
}

bootstrap();
