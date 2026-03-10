(function () {
  const root = document.querySelector("[data-runtime-setup]");
  if (!root) return;

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
  const providerStatusUrl = root.dataset.providerStatusUrl;
  const oauthStartUrl = root.dataset.oauthStartUrl;
  const oauthSessionUrlTemplate = root.dataset.oauthSessionUrlTemplate || "";
  const oauthCancelUrlTemplate = root.dataset.oauthCancelUrlTemplate || "";
  const oauthStartButton = document.getElementById("oauthStartButton");
  const oauthCancelButton = document.getElementById("oauthCancelButton");
  const oauthSessionPanel = document.getElementById("oauthSessionPanel");
  const runtimeStateText = document.getElementById("runtimeStateText");
  const providerStateText = document.getElementById("providerStateText");
  const preferredModelText = document.getElementById("preferredModelText");
  const setupCompleteButton = document.getElementById("setupCompleteButton");
  const oauthStatusText = document.getElementById("oauthStatusText");
  const oauthUrlWrap = document.getElementById("oauthUrlWrap");
  const oauthAuthLink = document.getElementById("oauthAuthLink");
  const oauthCodeWrap = document.getElementById("oauthCodeWrap");
  const oauthDeviceCode = document.getElementById("oauthDeviceCode");
  const oauthLog = document.getElementById("oauthLog");
  const statusLabels = {
    pending: "等待开始",
    running: "进行中",
    ready: "已就绪",
    failed: "失败",
    cancelled: "已取消",
  };

  let activeSessionId = root.dataset.oauthSessionId || "";
  let pollTimer = null;

  function post(url) {
    return fetch(url, {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken,
      },
      credentials: "same-origin",
    }).then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || "请求失败");
      }
      return data;
    });
  }

  function applyProviderStatus(payload) {
    if (!payload) return;
    const runtime = payload.runtime || {};
    const provider = payload.provider || {};
    const runtimeProvider = payload.runtime_provider || {};
    if (runtimeStateText) {
      runtimeStateText.textContent = runtime.compact_label || runtime.status_label || "运行时未连接";
    }
    if (providerStateText) {
      const providerStatus = runtimeProvider.provider_ready ? "ready" : (provider.status || "pending");
      providerStateText.textContent = statusLabels[providerStatus] || providerStatus;
    }
    if (preferredModelText) {
      preferredModelText.textContent = runtimeProvider.default_model || provider.default_model || payload.setup?.preferred_model || preferredModelText.textContent;
    }
    if (setupCompleteButton) {
      setupCompleteButton.disabled = !(runtime.chat_ready && runtimeProvider.provider_ready);
    }
  }

  function applyOAuthSession(session) {
    if (!oauthSessionPanel || !session) return;
    oauthSessionPanel.hidden = false;
    if (oauthCancelButton) {
      oauthCancelButton.hidden = !["pending", "running"].includes(session.status);
    }
    if (oauthStatusText) oauthStatusText.textContent = statusLabels[session.status] || session.status || "等待开始";
    if (oauthAuthLink && oauthUrlWrap) {
      const hasUrl = Boolean(session.auth_url);
      oauthUrlWrap.hidden = !hasUrl;
      if (hasUrl) {
        oauthAuthLink.href = session.auth_url;
        oauthAuthLink.textContent = session.auth_url;
      }
    }
    if (oauthDeviceCode && oauthCodeWrap) {
      const hasCode = Boolean(session.device_code);
      oauthCodeWrap.hidden = !hasCode;
      if (hasCode) oauthDeviceCode.textContent = session.device_code;
    }
    if (oauthLog) {
      oauthLog.textContent = session.output_log || "";
      oauthLog.scrollTop = oauthLog.scrollHeight;
    }
  }

  function pollOAuthSession() {
    if (!activeSessionId || !oauthSessionUrlTemplate) return;
    const url = oauthSessionUrlTemplate.replace("__SESSION__", encodeURIComponent(activeSessionId));
    fetch(url, { credentials: "same-origin" })
      .then((response) => response.json())
      .then((data) => {
        if (!data.ok) return;
        applyOAuthSession(data.session);
        return refreshProviderStatus();
      })
      .finally(() => {
        if (pollTimer) window.clearTimeout(pollTimer);
        if (activeSessionId) {
          pollTimer = window.setTimeout(pollOAuthSession, 2000);
        }
      });
  }

  function refreshProviderStatus() {
    if (!providerStatusUrl) return Promise.resolve();
    return fetch(providerStatusUrl, { credentials: "same-origin" })
      .then((response) => response.json())
      .then((data) => applyProviderStatus(data))
      .catch(() => {});
  }

  if (oauthStartButton && oauthStartUrl) {
    oauthStartButton.addEventListener("click", () => {
      oauthStartButton.disabled = true;
      post(oauthStartUrl)
        .then((data) => {
          const session = data.session || {};
          activeSessionId = session.id || "";
          applyOAuthSession(session);
          pollOAuthSession();
        })
        .catch((error) => {
          if (oauthStatusText) oauthStatusText.textContent = error.message;
          if (oauthSessionPanel) oauthSessionPanel.hidden = false;
        })
        .finally(() => {
          oauthStartButton.disabled = false;
        });
    });
  }

  if (oauthCancelButton && oauthCancelUrlTemplate) {
    oauthCancelButton.addEventListener("click", () => {
      if (!activeSessionId) return;
      const url = oauthCancelUrlTemplate.replace("__SESSION__", encodeURIComponent(activeSessionId));
      post(url).finally(() => {
        activeSessionId = "";
        if (pollTimer) window.clearTimeout(pollTimer);
        if (oauthCancelButton) oauthCancelButton.hidden = true;
        if (oauthStatusText) oauthStatusText.textContent = statusLabels.cancelled;
      });
    });
  }

  refreshProviderStatus();
  if (activeSessionId) {
    pollOAuthSession();
  }
})();
