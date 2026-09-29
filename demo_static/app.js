(() => {
  "use strict";

  let token = null;
  let sessionId = null;
  let expiresAt = null;
  let demoOrders = null;
  let pendingAction = null;
  let busy = false;
  const leaseStorageKey = "group-buy-demo-lease-token";

  const startPanel = document.querySelector("#start-panel");
  const startButton = document.querySelector("#start-button");
  const startNotice = document.querySelector("#start-notice");
  const workspace = document.querySelector("#workspace");
  const sessionStatus = document.querySelector("#session-status");
  const ordersElement = document.querySelector("#orders");
  const messages = document.querySelector("#messages");
  const progress = document.querySelector("#progress");
  const progressText = document.querySelector("#progress-text");
  const composer = document.querySelector("#composer");
  const messageInput = document.querySelector("#message-input");
  const sendButton = document.querySelector("#send-button");
  const releaseButton = document.querySelector("#release-button");

  const orderLabels = {
    unpaid: "未付款",
    paid_unformed: "已付款未成团",
    paid_formed: "已付款已成团",
    closed: "已关闭",
  };

  function setBusy(value) {
    busy = value;
    sendButton.disabled = value;
    messageInput.disabled = value;
    releaseButton.disabled = value;
    document.querySelectorAll("[data-question]").forEach((button) => {
      button.disabled = value;
    });
  }

  function addMessage(role, text, kind = null) {
    const article = document.createElement("article");
    article.className = `message ${role}`;
    if (kind === "HANDOFF") article.classList.add("handoff");

    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "AI";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    article.append(avatar, bubble);
    messages.append(article);
    messages.scrollTop = messages.scrollHeight;
  }

  function showProgress(message) {
    progressText.textContent = message || "正在处理";
    progress.classList.remove("hidden");
  }

  function hideProgress() {
    progress.classList.add("hidden");
  }

  function renderOrders() {
    ordersElement.replaceChildren();
    Object.entries(demoOrders).forEach(([key, value]) => {
      const card = document.createElement("div");
      card.className = "order-card";
      const label = document.createElement("strong");
      label.textContent = orderLabels[key];
      const order = document.createElement("code");
      order.textContent = value;
      card.append(label, order);
      ordersElement.append(card);
    });
  }

  function questionFor(name) {
    if (!demoOrders) return "";
    const questions = {
      order: `查询订单 ${demoOrders.unpaid} 现在是什么状态？`,
      rule: "钱已经付了，但团还没凑齐，这种情况能直接退款吗？",
      unknown: "拼团商品包邮吗？",
      refund: `帮我退掉订单 ${demoOrders.unpaid}`,
    };
    return questions[name] || "";
  }

  function clearSession() {
    token = null;
    sessionId = null;
    expiresAt = null;
    demoOrders = null;
    pendingAction = null;
    setBusy(true);
    sessionStatus.textContent = "体验已结束";
    sessionStatus.classList.remove("active");
    releaseButton.classList.add("hidden");
    workspace.classList.add("hidden");
    startPanel.classList.remove("hidden");
    startButton.disabled = false;
  }

  async function startDemo() {
    startButton.disabled = true;
    startNotice.textContent = "正在准备演示环境……";
    try {
      const savedLeaseToken = sessionStorage.getItem(leaseStorageKey);
      const response = await fetch("/demo/session", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(
          savedLeaseToken ? { lease_token: savedLeaseToken } : {}
        ),
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(
          typeof payload.detail === "string"
            ? payload.detail
            : "暂时无法开始体验。"
        );
      }
      token = payload.token;
      sessionStorage.setItem(leaseStorageKey, payload.lease_token);
      expiresAt = Date.parse(payload.expires_at);
      demoOrders = payload.demo_orders;
      sessionId = `demo-${crypto.randomUUID()}`;
      renderOrders();
      startPanel.classList.add("hidden");
      workspace.classList.remove("hidden");
      sessionStatus.textContent = "体验中 · 30 分钟";
      sessionStatus.classList.add("active");
      releaseButton.classList.remove("hidden");
      startNotice.textContent = "";
      setBusy(false);
      messageInput.focus();
    } catch (error) {
      startNotice.textContent = error instanceof Error
        ? error.message
        : "暂时无法开始体验。";
      startButton.disabled = false;
    }
  }

  function parseSseBlock(block) {
    let eventName = "message";
    let data = "";
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      if (line.startsWith("data:")) data += line.slice(5).trim();
    });
    if (!data) return null;
    return { eventName, data: JSON.parse(data) };
  }

  function renderActionProposal(data) {
    pendingAction = {
      actionId: data.action_id,
      credential: data.credential,
      expiresAt: data.expires_at,
    };

    const preview = data.preview || {};
    const card = document.createElement("section");
    card.className = "action-card";
    const title = document.createElement("h3");
    title.textContent = "退款提议已生成";
    const orderStatus = document.createElement("p");
    orderStatus.textContent = `订单状态：${preview.orderStatus || "未知"}`;
    const refundType = document.createElement("p");
    refundType.textContent = `退款类型：${preview.refundType || "未知"}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "confirm-button";
    button.textContent = "确认退款";
    const result = document.createElement("p");
    result.className = "action-result";
    result.setAttribute("role", "status");
    button.addEventListener("click", () => confirmRefund(button, result));
    card.append(title, orderStatus, refundType, button, result);
    messages.append(card);
    messages.scrollTop = messages.scrollHeight;
  }

  async function handleEvent(eventName, data) {
    if (eventName === "progress") {
      showProgress(data.message || "正在处理");
      return;
    }
    if (eventName === "action_proposed") {
      renderActionProposal(data);
      return;
    }
    if (eventName === "timeout") {
      hideProgress();
      addMessage("assistant", data.answer, "HANDOFF");
      return;
    }
    if (eventName === "final") {
      hideProgress();
      addMessage("assistant", data.answer, data.kind);
      if (data.kind === "HANDOFF") {
        addMessage("assistant", "这个问题需要人工客服继续处理。", "HANDOFF");
      }
    }
  }

  async function streamChat(message) {
    const response = await fetch("/api/v1/chat/stream", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({}));
      const detail = typeof payload.detail === "string"
        ? payload.detail
        : payload.detail?.message;
      const error = new Error(detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const parsed = parseSseBlock(block);
        if (parsed) await handleEvent(parsed.eventName, parsed.data);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const parsed = parseSseBlock(buffer);
      if (parsed) await handleEvent(parsed.eventName, parsed.data);
    }
  }

  async function sendMessage(message) {
    if (busy || !token || !message.trim()) return;
    if (Date.now() >= expiresAt) {
      clearSession();
      startNotice.textContent = "本次体验已到期，正在安全恢复演示数据……";
      await startDemo();
      return;
    }
    setBusy(true);
    addMessage("user", message.trim());
    showProgress("正在理解问题");
    try {
      await streamChat(message.trim());
    } catch (error) {
      hideProgress();
      if (error instanceof Error && error.status === 401) {
        clearSession();
        startNotice.textContent = "会话已空闲过期，正在安全恢复演示数据……";
        await startDemo();
        return;
      }
      addMessage(
        "assistant",
        error instanceof Error ? error.message : "请求失败，请稍后再试。",
        "HANDOFF"
      );
    } finally {
      setBusy(false);
      messageInput.focus();
    }
  }

  async function confirmRefund(button, result) {
    if (!pendingAction || !token) return;
    button.disabled = true;
    button.textContent = "确认中……";
    try {
      const response = await fetch(
        `/v1/actions/${encodeURIComponent(pendingAction.actionId)}/confirm`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ credential: pendingAction.credential }),
        }
      );
      const payload = await response.json();
      result.textContent = `${payload.status || "UNKNOWN"} · ${payload.message || "确认请求已提交"}`;
      if (["SUCCEEDED", "FAILED"].includes(payload.status)) {
        pendingAction = null;
        button.classList.add("hidden");
      } else {
        button.disabled = false;
        button.textContent = "查看当前状态";
      }
    } catch {
      result.textContent = "确认请求失败，请稍后再试。";
      button.disabled = false;
      button.textContent = "重新确认";
    }
  }

  async function releaseDemo() {
    if (!token || busy) return;
    setBusy(true);
    releaseButton.disabled = true;
    try {
      const response = await fetch("/demo/release", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof payload.detail === "string"
          ? payload.detail
          : payload.detail?.message;
        throw new Error(detail || "暂时无法结束体验，请稍后再试。");
      }
      sessionStorage.removeItem(leaseStorageKey);
      clearSession();
      messages.replaceChildren();
      startNotice.textContent = "体验已结束，演示数据已恢复。";
    } catch (error) {
      addMessage(
        "assistant",
        error instanceof Error ? error.message : "暂时无法结束体验，请稍后再试。",
        "HANDOFF"
      );
      setBusy(false);
    } finally {
      releaseButton.disabled = false;
    }
  }

  startButton.addEventListener("click", startDemo);
  releaseButton.addEventListener("click", releaseDemo);
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = messageInput.value;
    messageInput.value = "";
    sendMessage(message);
  });
  document.querySelectorAll("[data-question]").forEach((button) => {
    button.addEventListener("click", () => {
      const question = questionFor(button.dataset.question);
      if (question) sendMessage(question);
    });
  });

  window.addEventListener("pagehide", () => {
    token = null;
    pendingAction = null;
  });

  if (sessionStorage.getItem(leaseStorageKey)) {
    startDemo();
  }
})();
