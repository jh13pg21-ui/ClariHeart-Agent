const ACTIVE_SESSION_KEY = "mindbridge.activeSessionId";

const state = {
  sessionId: localStorage.getItem(ACTIVE_SESSION_KEY),
  sending: false,
  profile: null,
  modelName: "mock",
  conversations: [],
  memories: []
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chatForm"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  newSession: document.querySelector("#newSession"),
  sessionBadge: document.querySelector("#sessionBadge"),
  conversationList: document.querySelector("#conversationList"),
  conversationTitle: document.querySelector("#conversationTitle"),
  historyRefresh: document.querySelector("#historyRefresh"),
  memoryRefresh: document.querySelector("#memoryRefresh"),
  memoryList: document.querySelector("#memoryList")
};

function csrfToken() {
  const cookie = document.cookie.split("; ").find((item) => item.startsWith("mindbridge_csrf="));
  return cookie ? decodeURIComponent(cookie.split("=", 2)[1]) : "";
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (["POST", "PUT", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase())) {
    headers["X-CSRF-Token"] = csrfToken();
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    localStorage.removeItem(ACTIVE_SESSION_KEY);
    window.location.replace("/");
    throw new Error("登录状态已失效");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${text || response.statusText}`);
  }
  return response;
}

function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function displayModel(model) {
  return (model || "").includes("mindbridge-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health", { credentials: "same-origin" });
    const body = await response.json();
    setPill(
      els.serviceState,
      body.status === "UP" ? "服务正常" : `服务 ${body.status}`,
      body.status === "UP" ? "ok" : "danger"
    );
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (isAdmin(profile)) {
      window.location.replace("/admin.html");
      return null;
    }
    state.profile = profile;
    els.activeAccount.textContent = profile.displayName || profile.username;
    return profile;
  } catch {
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(els.modelState, "mock 演示", "warn");
  }
}

function clearWelcome() {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
}

function renderAssistantContent(element, content) {
  element.classList.add("markdown");
  if (window.MindBridgeMarkdown) {
    element.innerHTML = window.MindBridgeMarkdown.render(content || "");
  } else {
    element.textContent = content || "";
  }
}

function addMessage(role, content) {
  clearWelcome();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  row.innerHTML = `
    <div class="message-role">${role === "user" ? "我" : "MindBridge"}</div>
    <div class="bubble"></div>
  `;
  const bubble = row.querySelector(".bubble");
  if (role === "assistant") {
    renderAssistantContent(bubble, content);
  } else {
    bubble.textContent = content;
  }
  els.messages.append(row);
  els.messages.scrollTop = els.messages.scrollHeight;
  return bubble;
}

function parseSse(buffer, onEvent) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop();
  for (const part of parts) {
    const data = part
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    try {
      onEvent(JSON.parse(data));
    } catch {
      onEvent({ type: "error", message: "服务端返回了无法解析的流式数据。" });
    }
  }
  return rest;
}

function formatHistoryTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function renderHistory() {
  els.conversationList.replaceChildren();
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "暂无历史会话";
    els.conversationList.append(empty);
    return;
  }
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    if (conversation.sessionId === state.sessionId) button.classList.add("active");

    const title = document.createElement("strong");
    title.textContent = conversation.title || "未命名会话";
    const preview = document.createElement("span");
    preview.textContent = conversation.lastMessage || "暂无消息";
    const time = document.createElement("time");
    time.dateTime = conversation.updatedAt;
    time.textContent = formatHistoryTime(conversation.updatedAt);
    button.append(title, time, preview);
    button.addEventListener("click", () => loadConversation(conversation.sessionId));
    els.conversationList.append(button);
  }
}

async function loadHistory({ restoreActive = false } = {}) {
  try {
    const response = await api("/api/conversations");
    state.conversations = await response.json();
    renderHistory();
    const active = state.conversations.find((item) => item.sessionId === state.sessionId);
    if (active) els.conversationTitle.textContent = active.title || "心理陪伴对话";
    if (restoreActive && state.sessionId) {
      const exists = state.conversations.some((item) => item.sessionId === state.sessionId);
      if (exists) {
        await loadConversation(state.sessionId);
      } else {
        resetSession();
      }
    }
  } catch (error) {
    els.conversationList.replaceChildren();
    const message = document.createElement("p");
    message.className = "history-empty error";
    message.textContent = `历史会话读取失败：${error.message}`;
    els.conversationList.append(message);
  }
}

const MEMORY_TYPE_LABELS = {
  PROFILE: "个人背景",
  PREFERENCE: "互动偏好",
  SUPPORT: "有效支持",
  CONTEXT: "长期事项"
};

function renderMemories() {
  els.memoryList.replaceChildren();
  if (!state.memories.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "暂无长期记忆";
    els.memoryList.append(empty);
    return;
  }
  for (const memory of state.memories) {
    const item = document.createElement("article");
    item.className = "memory-item";

    const heading = document.createElement("div");
    heading.className = "memory-item-head";
    const name = document.createElement("strong");
    name.textContent = memory.name || "未命名记忆";
    const type = document.createElement("span");
    type.textContent = MEMORY_TYPE_LABELS[memory.type] || memory.type;
    heading.append(name, type);

    const body = document.createElement("p");
    body.textContent = memory.body || memory.description || "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "memory-delete";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除长期记忆：${memory.name || ""}`);
    remove.addEventListener("click", () => deleteMemory(memory));
    item.append(heading, body, remove);
    els.memoryList.append(item);
  }
}

async function loadMemories() {
  try {
    const response = await api("/api/memories");
    state.memories = await response.json();
    renderMemories();
  } catch (error) {
    els.memoryList.replaceChildren();
    const message = document.createElement("p");
    message.className = "history-empty error";
    message.textContent = `长期记忆读取失败：${error.message}`;
    els.memoryList.append(message);
  }
}

async function deleteMemory(memory) {
  if (!window.confirm(`确认删除长期记忆“${memory.name}”吗？`)) return;
  try {
    await api(`/api/memories/${encodeURIComponent(memory.id)}`, {
      method: "DELETE"
    });
    state.memories = state.memories.filter((item) => item.id !== memory.id);
    renderMemories();
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
  }
}

async function loadConversation(sessionId) {
  if (state.sending || !sessionId) return;
  setPill(els.sessionBadge, "LOADING", "warn");
  try {
    const response = await api(`/api/conversations/${encodeURIComponent(sessionId)}`);
    const conversation = await response.json();
    state.sessionId = conversation.sessionId;
    localStorage.setItem(ACTIVE_SESSION_KEY, state.sessionId);
    els.conversationTitle.textContent = conversation.title || "心理陪伴对话";
    els.messages.replaceChildren();
    for (const message of conversation.messages) {
      const role = String(message.role || "").toLowerCase();
      addMessage(role === "user" ? "user" : "assistant", message.content);
    }
    if (!conversation.messages.length) {
      els.messages.innerHTML = `<div class="empty"><strong>这段会话还没有消息</strong><p>你可以从现在开始表达。</p></div>`;
    }
    renderHistory();
    setPill(els.sessionBadge, "READY", "ok");
  } catch (error) {
    if (String(error.message).startsWith("404 ")) {
      resetSession();
      await loadHistory();
    }
    setPill(els.sessionBadge, "ERROR", "danger");
  }
}

function handleStreamEvent(eventData, assistant, streamState) {
  if (eventData.type === "meta") {
    state.sessionId = eventData.sessionId;
    localStorage.setItem(ACTIVE_SESSION_KEY, state.sessionId);
    renderHistory();
  }
  if (eventData.type === "token") {
    streamState.raw += eventData.content || "";
    renderAssistantContent(assistant, streamState.raw);
  }
  if (eventData.type === "message" || eventData.type === "replace") {
    streamState.raw = eventData.content || "";
    renderAssistantContent(assistant, streamState.raw);
  }
  if (eventData.type === "error") {
    streamState.failed = true;
    if (eventData.resetSession) {
      state.sessionId = null;
      localStorage.removeItem(ACTIVE_SESSION_KEY);
      renderHistory();
    }
    if (!streamState.raw) assistant.textContent = eventData.message || "服务暂时不可用";
    setPill(els.sessionBadge, "ERROR", "danger");
  }
  els.messages.scrollTop = els.messages.scrollHeight;
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.sending) return;
  const message = els.messageInput.value.trim();
  if (!message) return;
  state.sending = true;
  els.sendButton.disabled = true;
  setPill(els.sessionBadge, "THINKING", "warn");
  els.messageInput.value = "";
  addMessage("user", message);
  const assistant = addMessage("assistant", "");
  const streamState = { raw: "", failed: false };

  try {
    const response = await api("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.sessionId, message })
    });
    if (!response.body) throw new Error("当前浏览器不支持流式响应");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSse(buffer, (eventData) => handleStreamEvent(eventData, assistant, streamState));
    }
    buffer += decoder.decode();
    parseSse(`${buffer}\n\n`, (eventData) => handleStreamEvent(eventData, assistant, streamState));
    if (!streamState.failed) {
      setPill(els.sessionBadge, "DONE", "ok");
      await loadHistory();
      await loadMemories();
    }
  } catch (error) {
    assistant.classList.remove("markdown");
    assistant.textContent = `发送失败：${error.message}`;
    setPill(els.sessionBadge, "ERROR", "danger");
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
  }
}

function resetSession() {
  if (state.sending) return;
  state.sessionId = null;
  localStorage.removeItem(ACTIVE_SESSION_KEY);
  els.conversationTitle.textContent = "心理陪伴对话";
  els.messages.innerHTML = `<div class="empty"><strong>新会话已开始</strong><p>你可以继续输入新的问题。</p></div>`;
  renderHistory();
  setPill(els.sessionBadge, "READY");
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    localStorage.removeItem(ACTIVE_SESSION_KEY);
    window.location.assign("/");
  }
}

document.querySelectorAll("[data-quick]").forEach((button) => {
  button.addEventListener("click", () => {
    els.messageInput.value = button.dataset.quick;
    els.messageInput.focus();
  });
});
els.chatForm.addEventListener("submit", sendMessage);
els.newSession.addEventListener("click", resetSession);
els.historyRefresh.addEventListener("click", () => loadHistory());
els.memoryRefresh.addEventListener("click", () => loadMemories());
els.switchAccount.addEventListener("click", logout);

checkHealth();
loadProfile().then((profile) => {
  if (profile) {
    loadAgentStatus();
    loadHistory({ restoreActive: true });
    loadMemories();
  }
});
