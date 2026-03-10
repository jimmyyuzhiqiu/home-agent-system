(function () {
  const root = document.documentElement;
  const themeSelect = document.getElementById("themeSelect");
  const storageKey = "themeMode";
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme(mode) {
    const actual = mode === "auto" ? (media.matches ? "dark" : "light") : mode;
    root.setAttribute("data-theme", actual);
  }

  const saved = localStorage.getItem(storageKey) || "auto";
  applyTheme(saved);

  if (themeSelect) {
    themeSelect.value = saved;
    themeSelect.addEventListener("change", () => {
      localStorage.setItem(storageKey, themeSelect.value);
      applyTheme(themeSelect.value);
    });
  }

  media.addEventListener("change", () => {
    const current = localStorage.getItem(storageKey) || "auto";
    if (current === "auto") {
      applyTheme("auto");
    }
  });

  const appShell = document.getElementById("appShell");
  const sidebarToggle = document.getElementById("sidebarToggle");
  const sidebarOverlay = document.getElementById("sidebarOverlay");
  const contextColumn = document.getElementById("contextColumn");
  if (appShell && sidebarToggle && sidebarOverlay) {
    const closeMenu = () => appShell.classList.remove("menu-open");
    sidebarToggle.addEventListener("click", () => appShell.classList.add("menu-open"));
    sidebarOverlay.addEventListener("click", closeMenu);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMenu();
        contextColumn?.classList.remove("is-open");
      }
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 1180) {
        closeMenu();
      }
      if (window.innerWidth > 980) {
        contextColumn?.classList.remove("is-open");
      }
    });
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-btn");
    if (!button) {
      return;
    }
    const text = button.dataset.copyText || "";
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "已复制";
      window.setTimeout(() => {
        button.textContent = "复制";
      }, 1500);
    } catch (error) {
      button.textContent = "复制失败";
      window.setTimeout(() => {
        button.textContent = "复制";
      }, 1500);
    }
  });
})();
