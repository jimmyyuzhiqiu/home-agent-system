(function () {
  const form = document.getElementById("chatForm");
  if (!form) {
    return;
  }

  const workspace = document.getElementById("chatWorkspace");
  const backendReady = workspace?.dataset.backendReady === "true";
  const messageInput = document.getElementById("messageInput");
  const attachmentInput = document.getElementById("attachmentInput");
  const attachmentButton = document.getElementById("attachmentButton");
  const fileHint = document.getElementById("fileHint");
  const dropZone = document.getElementById("dropZone");
  const sendButton = document.getElementById("sendButton");
  const messageStream = document.getElementById("messageStream");
  const timelinePanel = document.getElementById("timelinePanel");

  const conversationToggle = document.getElementById("conversationToggle");
  const conversationDrawer = document.getElementById("conversationDrawer");
  const contextToggle = document.getElementById("contextToggle");
  const contextDrawer = document.getElementById("contextColumn");
  const drawerBackdrop = document.getElementById("chatDrawerBackdrop");

  let activeRunId = workspace?.dataset.activeRunId || "";
  let pollHandle = null;
  let lastDividerDay = "";
  const existingDividers = messageStream?.querySelectorAll(".message-date-divider span") || [];
  if (existingDividers.length) {
    lastDividerDay = existingDividers[existingDividers.length - 1].textContent.trim();
  }

  function autoResize() {
    if (!messageInput) {
      return;
    }
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 220) + "px";
  }

  function setComposerBusy(isBusy) {
    sendButton.disabled = isBusy || !backendReady;
    sendButton.textContent = isBusy ? "处理中…" : "发送任务";
    if (attachmentButton) {
      attachmentButton.disabled = isBusy || !backendReady;
    }
  }

  function escapeHtml(text) {
    return (text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function scrollToBottom() {
    messageStream.scrollTop = messageStream.scrollHeight;
  }

  function ensureDateDivider(dateValue) {
    const parsed = dateValue ? new Date(dateValue) : null;
    const now = parsed && !Number.isNaN(parsed.getTime()) ? parsed : new Date();
    const day = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    if (lastDividerDay === day) {
      return;
    }
    lastDividerDay = day;
    const divider = document.createElement("div");
    divider.className = "message-date-divider";
    divider.innerHTML = `<span>${day}</span>`;
    messageStream.appendChild(divider);
  }

  function appendMessage(role, html, text, meta) {
    if (meta?.messageId && document.getElementById(`message-${meta.messageId}`)) {
      return;
    }
    ensureDateDivider(meta?.time);
    const article = document.createElement("article");
    if (meta?.messageId) {
      article.id = `message-${meta.messageId}`;
    }
    const stateClass = meta?.state ? ` is-${meta.state}` : "";
    article.className = `message-bubble ${role === "user" ? "is-user" : "is-assistant"}${stateClass}`;
    article.innerHTML = `
      <div class="message-head">
        <div class="message-ident">
          <span class="badge ${role === "user" ? "badge-muted" : "badge-accent"}">${role === "user" ? "我" : "系统"}</span>
          ${meta?.state === "failed" ? '<span class="badge badge-failed">执行失败</span>' : ""}
          ${meta?.state === "blocked" ? '<span class="badge badge-blocked">权限阻塞</span>' : ""}
          <span class="message-time">${meta?.time || "刚刚"}</span>
        </div>
        ${role === "assistant" ? `<button type="button" class="copy-btn" data-copy-text="${escapeHtml(text)}">复制结果</button>` : ""}
      </div>
      <div class="message-body prose">${html}</div>
      ${meta?.attachmentName && meta?.attachmentPath ? `<a class="attachment-chip" href="/uploads/${meta.attachmentPath}" target="_blank">${escapeHtml(meta.attachmentName)}</a>` : ""}
    `;
    messageStream.querySelector(".empty-state")?.remove();
    messageStream.appendChild(article);
    scrollToBottom();
  }

  function setFileState(file) {
    if (!file) {
      fileHint.textContent = backendReady ? "支持图片、文档、压缩包，单文件上传。" : "执行后端未就绪，暂时无法上传处理。";
      return;
    }
    fileHint.textContent = `已选择：${file.name}`;
  }

  function renderRunHero(payload) {
    const host = document.querySelector(".run-hero");
    if (!host) {
      return;
    }
    const title = host.querySelector("h4");
    const badge = host.querySelector(".run-hero-head .badge");
    const progressBar = host.querySelector(".progress-bar");
    const progressMeta = host.querySelectorAll(".progress-meta span");
    if (title) {
      title.textContent = payload.public_status_label || payload.status;
    }
    if (badge) {
      badge.className = `badge badge-${payload.status}`;
      badge.textContent = payload.public_status_label || payload.status;
    }
    if (progressBar) {
      progressBar.style.width = `${payload.progress_percent || 0}%`;
    }
    if (progressMeta[0]) {
      progressMeta[0].textContent = `${payload.progress_percent || 0}%`;
    }
    if (progressMeta[1]) {
      progressMeta[1].textContent = payload.error_message || "系统会持续更新执行状态。";
    }
    const stageStrip = host.querySelector(".run-stage-strip");
    if (stageStrip && payload.timeline?.items) {
      stageStrip.innerHTML = payload.timeline.items
        .map(
          (item) => `
            <div class="stage-chip is-${item.status}">
              <span class="timeline-dot is-${item.status}"></span>
              <div>
                <strong>${escapeHtml(item.title)}</strong>
                <small>${escapeHtml(item.status_label || item.status)}</small>
              </div>
            </div>
          `
        )
        .join("");
    }
    const eventList = host.querySelector(".event-card-list");
    if (eventList && payload.public_events) {
      eventList.innerHTML = payload.public_events
        .slice(-6)
        .map(
          (item) => `
            <article class="event-card is-${item.status}">
              <div class="event-card-head">
                <strong>${escapeHtml(item.label)}</strong>
                <span class="badge badge-${item.status}">${escapeHtml(item.status)}</span>
              </div>
              <p>${escapeHtml(item.text || "处理中…")}</p>
            </article>
          `
        )
        .join("");
    }
  }

  function renderArtifacts(items) {
    const section = document.querySelector(".artifact-section");
    const grid = section?.querySelector(".artifact-grid");
    if (!section || !grid) {
      return;
    }
    if (!items || !items.length) {
      section.style.display = "none";
      return;
    }
    section.style.display = "";
    grid.innerHTML = items
      .map(
        (item) => `
          <article class="artifact-card">
            <div class="artifact-head">
              <span class="badge badge-muted">${escapeHtml(item.kind)}</span>
              ${item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" class="link-subtle">查看来源</a>` : ""}
            </div>
            <h5>${escapeHtml(item.title)}</h5>
            ${item.summary ? `<p class="muted">${escapeHtml(item.summary)}</p>` : ""}
            ${item.file_path ? `<a class="btn btn-secondary btn-compact" href="/uploads/${escapeHtml(item.file_path)}" target="_blank">打开文件</a>` : ""}
          </article>
        `
      )
      .join("");
  }

  function renderDeliveryStatus(status) {
    const card = document.querySelector(".delivery-card");
    if (!card) {
      return;
    }
    if (!status || status === "skipped") {
      card.style.display = "none";
      return;
    }
    card.style.display = "";
    card.className = `delivery-card is-${status}`;
    const title = card.querySelector("h4");
    const badge = card.querySelector(".badge");
    if (title) {
      title.textContent = status === "done" ? "已发送到绑定手机号" : status === "failed" ? "结果已生成，但回发失败" : "正在准备发送";
    }
    if (badge) {
      badge.className = `badge badge-${status}`;
      badge.textContent = status;
    }
  }

  function updateTimeline(payload) {
    if (!timelinePanel || !payload.timeline) {
      return;
    }
    const overallBadge = timelinePanel.querySelector(".panel-head .badge");
    if (overallBadge) {
      overallBadge.className = `badge badge-${payload.status}`;
      overallBadge.textContent = payload.timeline.overall_label;
    }
    const progressBar = timelinePanel.querySelector(".progress-bar");
    if (progressBar) {
      progressBar.style.width = `${payload.progress_percent || 0}%`;
    }
    const progressMeta = timelinePanel.querySelector(".progress-meta");
    if (progressMeta) {
      const spans = progressMeta.querySelectorAll("span");
      if (spans[0]) spans[0].textContent = `${payload.progress_percent || 0}%`;
      if (spans[1]) spans[1].textContent = payload.error_message || "最近一次任务状态";
    }
    payload.timeline.items.forEach((item) => {
      const row = timelinePanel.querySelector(`[data-stage="${item.key}"]`);
      if (!row) {
        return;
      }
      const dot = row.querySelector(".timeline-dot");
      const badge = row.querySelector(".badge");
      const summary = row.querySelector(".muted");
      if (dot) {
        dot.className = `timeline-dot is-${item.status}`;
      }
      if (badge) {
        badge.className = `badge badge-${item.status}`;
        badge.textContent = item.status_label || item.status;
      }
      if (summary) {
        summary.textContent = item.summary || "等待执行内容填充";
      }
    });
    renderRunHero(payload);
    renderArtifacts(payload.user_artifacts || []);
    renderDeliveryStatus(payload.delivery_status);
  }

  function showPendingAssistant() {
    if (document.getElementById("message-pending-assistant")) {
      return;
    }
    appendMessage("assistant", "<p>系统正在拆解任务并准备执行，请稍候…</p>", "系统正在拆解任务并准备执行，请稍候…", {
      time: "处理中",
      messageId: "pending-assistant",
      state: "pending",
    });
  }

  function clearPendingAssistant() {
    document.getElementById("message-pending-assistant")?.remove();
  }

  function openDrawer(target) {
    if (!target) {
      return;
    }
    [conversationDrawer, contextDrawer].forEach((drawer) => {
      if (drawer && drawer !== target) {
        drawer.classList.remove("is-open");
      }
    });
    target.classList.add("is-open");
    drawerBackdrop?.classList.add("is-visible");
  }

  function closeDrawers() {
    [conversationDrawer, contextDrawer].forEach((drawer) => drawer?.classList.remove("is-open"));
    drawerBackdrop?.classList.remove("is-visible");
  }

  async function pollRun(runId) {
    if (!runId) {
      return;
    }
    setComposerBusy(true);
    if (pollHandle) {
      clearInterval(pollHandle);
    }

    const tick = async () => {
      try {
        const response = await fetch(`/api/runs/${runId}/status`, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error("轮询状态失败");
        }
        const payload = await response.json();
        updateTimeline(payload);
        if (["done", "failed", "blocked"].includes(payload.status)) {
          clearInterval(pollHandle);
          pollHandle = null;
          activeRunId = "";
          workspace.dataset.activeRunId = "";
          clearPendingAssistant();
          appendMessage("assistant", payload.final_html, payload.final_text, {
            time: "刚刚",
            messageId: payload.assistant_message_id,
            state: payload.status === "blocked" ? "blocked" : payload.status === "failed" ? "failed" : "",
          });
          setComposerBusy(false);
        }
      } catch (error) {
        clearInterval(pollHandle);
        pollHandle = null;
        clearPendingAssistant();
        fileHint.textContent = "执行状态获取失败，请刷新页面查看最新结果。";
        appendMessage("assistant", "<p>执行状态获取失败，请刷新页面查看最新结果。</p>", "执行状态获取失败，请刷新页面查看最新结果。", {
          time: "刚刚",
          state: "failed",
        });
        setComposerBusy(false);
      }
    };

    pollHandle = setInterval(tick, 1500);
    tick();
  }

  messageInput?.addEventListener("input", autoResize);
  autoResize();

  attachmentButton?.addEventListener("click", () => attachmentInput?.click());
  attachmentInput?.addEventListener("change", () => {
    setFileState(attachmentInput.files?.[0]);
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      if (!backendReady) {
        return;
      }
      dropZone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    dropZone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("is-dragover");
    });
  });
  dropZone?.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (!backendReady || !files || !files[0] || !attachmentInput) {
      return;
    }
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(files[0]);
    attachmentInput.files = dataTransfer.files;
    setFileState(files[0]);
  });

  document.querySelectorAll(".prompt-chip").forEach((button) => {
    button.addEventListener("click", () => {
      messageInput.value = button.dataset.prompt || "";
      autoResize();
      messageInput.focus();
    });
  });

  conversationToggle?.addEventListener("click", () => openDrawer(conversationDrawer));
  contextToggle?.addEventListener("click", () => openDrawer(contextDrawer));
  drawerBackdrop?.addEventListener("click", closeDrawers);
  document.querySelectorAll("[data-close-drawer]").forEach((button) => {
    button.addEventListener("click", closeDrawers);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeDrawers();
    }
  });

  form.addEventListener("submit", async (event) => {
    if (form.dataset.async !== "true" || !window.fetch) {
      return;
    }
    event.preventDefault();
    if (activeRunId || !backendReady) {
      if (!backendReady) {
        fileHint.textContent = "执行后端未就绪，请联系管理员检查 Runtime 与 Provider。";
      }
      return;
    }

    const formData = new FormData(form);
    const text = messageInput.value.trim() || "[仅上传附件]";
    const file = attachmentInput.files?.[0];
    if (!messageInput.value.trim() && !file) {
      return;
    }

    setComposerBusy(true);
    let response;
    let payload;
    try {
      response = await fetch("/api/chat/send", {
        method: "POST",
        body: formData,
        headers: { Accept: "application/json" },
      });
      payload = await response.json();
    } catch (error) {
      setComposerBusy(false);
      fileHint.textContent = "发送失败，请检查网络后重试。";
      return;
    }

    if (!response.ok || !payload.ok) {
      setComposerBusy(false);
      fileHint.textContent = payload.error || payload.backend_reason || "发送失败";
      return;
    }

    appendMessage("user", `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>`, text, {
      time: payload.user_message_created_at || "刚刚",
      attachmentName: payload.attachment_name,
      attachmentPath: payload.attachment_path,
      messageId: payload.user_message_id,
    });

    messageInput.value = "";
    autoResize();
    if (attachmentInput) {
      attachmentInput.value = "";
    }
    setFileState(null);

    if (payload.command) {
      appendMessage("assistant", payload.assistant_html, payload.assistant_text, {
        time: "刚刚",
        messageId: payload.assistant_message_id,
      });
      setComposerBusy(false);
      return;
    }

    activeRunId = String(payload.run_id);
    workspace.dataset.activeRunId = activeRunId;
    showPendingAssistant();
    pollRun(activeRunId);
  });

  setFileState(null);
  setComposerBusy(false);

  if (activeRunId) {
    pollRun(activeRunId);
  }
})();
