/* Language controls, host setup entry and per-session browser consent. */
(() => {
  "use strict";
  const i18n = window.ScreenOrganizerI18n;
  const host = window.screenOrganizerHost;
  const native = host?.native === true;
  const $ = selector => document.querySelector(selector);
  const send = message => window.webkit?.messageHandlers?.screenOrganizer?.postMessage(message);
  let accepted = native && host.consentAccepted === true;
  let reachedEnd = false, declined = false, activeDialog = null;
  let languagePending = true;
  let resolveConsent;
  const consent = new Promise(resolve => { resolveConsent = resolve; });
  if (accepted) resolveConsent();

  function syncInert() {
    for (const node of document.querySelectorAll("header,main,footer,#selection-bar,#drawer,#toast,#busy,#modal,#mcp-dialog,#consent-dialog")) {
      node.inert = activeDialog ? node !== activeDialog.root : !accepted;
    }
  }
  function openDialog(root, focus, dismiss) {
    if (activeDialog) return false;
    activeDialog = {root, opener: document.activeElement, dismiss};
    root.hidden = false;
    syncInert();
    focus.focus();
    return true;
  }
  function closeDialog(root) {
    if (activeDialog?.root !== root) return false;
    const {opener} = activeDialog;
    activeDialog = null;
    root.hidden = true;
    syncInert();
    const restore = opener && opener !== document.body && document.body.contains(opener) && !opener.disabled &&
      !opener.closest("[hidden],[inert]") ? opener : $("#read");
    restore.focus();
    return true;
  }
  function updateConsent() {
    const text = $("#consent-text");
    const height = text.clientHeight, extent = text.scrollHeight;
    const rendered = !$("#consent-dialog").hidden && height > 0 && extent > 0;
    if (rendered && !languagePending && text.scrollTop + height >= extent - 2) reachedEnd = true;
    $("#consent-continue").disabled = declined || languagePending || !rendered || !reachedEnd || !$("#consent-agree").checked;
  }
  function resetConsent() {
    reachedEnd = false;
    $("#consent-agree").checked = false;
    $("#consent-text").scrollTop = 0;
    updateConsent();
    requestAnimationFrame(updateConsent);
  }
  function languageOptions() {
    for (const selector of ["#language-select", "#consent-language-select"]) {
      const select = $(selector);
      select.replaceChildren();
      for (const language of [{code: "system", name: i18n.t("common.system")}, ...i18n.languages]) {
        const option = document.createElement("option");
        option.value = language.code;
        option.textContent = language.name;
        select.appendChild(option);
      }
      select.value = i18n.preference;
    }
  }
  function updateTitle() {
    document.title = i18n.t("app.name") + " · " + i18n.t("app.subtitle");
  }
  function hideSetup() { closeDialog($("#mcp-dialog")); }
  const serverID = "ios-homescreen-mcp";
  const entry = {command: "home-screen-mcp", args: [], env: {HOMESCREEN_READ_ONLY: "1"}};
  const examples = [
    ["codex", "Codex CLI", `codex mcp add ${serverID} --env HOMESCREEN_READ_ONLY=1 -- home-screen-mcp`],
    ["claude", "Claude Code CLI", `claude mcp add --env HOMESCREEN_READ_ONLY=1 --transport stdio ${serverID} -- home-screen-mcp`],
    ["json", "mcp.copyJSON", JSON.stringify({mcpServers: {[serverID]: entry}}, null, 2)],
    ["toml", "mcp.copyCodex", `[mcp_servers.${serverID}]\ncommand = "home-screen-mcp"\nargs = []\nenvironment_id = "local"\n\n[mcp_servers.${serverID}.env]\nHOMESCREEN_READ_ONLY = "1"`],
    ["vscode", "mcp.copyVSCode", JSON.stringify({servers: {[serverID]: {type:"stdio", ...entry}}}, null, 2)],
    ["copilot", "mcp.copyCopilot", JSON.stringify({mcpServers: {[serverID]: {type:"stdio", ...entry, tools:["*"]}}}, null, 2)],
  ];
  function setupExamples() {
    const root = $("#mcp-examples");
    for (const [id, label, text] of examples) {
      const section = document.createElement("section");
      const pre = document.createElement("pre");
      pre.tabIndex = 0;
      pre.textContent = text;
      const button = document.createElement("button");
      button.id = "mcp-copy-" + id;
      if (label.startsWith("mcp.")) button.setAttribute("data-i18n", label);
      else {
        button.textContent = i18n.t("common.copy") + " · " + label;
        i18n.onChange(() => { button.textContent = i18n.t("common.copy") + " · " + label; });
      }
      button.onclick = async () => {
        try {
          await navigator.clipboard.writeText(text);
          $("#mcp-copy-status").setAttribute("data-i18n", "common.copied");
          i18n.translateDOM($("#mcp-copy-status"));
        } catch (_) {
          $("#mcp-copy-status").setAttribute("data-i18n", "mcp.copyFailed");
          i18n.translateDOM($("#mcp-copy-status"));
          pre.focus();
        }
      };
      section.append(pre, button);
      root.appendChild(section);
    }
    i18n.translateDOM(root);
  }
  $("#mcp-setup").onclick = () => {
    if (!accepted || activeDialog) return;
    if (native) { send({action: "openMCPSetup"}); return; }
    $("#mcp-copy-status").removeAttribute("data-i18n");
    $("#mcp-copy-status").textContent = "";
    openDialog($("#mcp-dialog"), $("#mcp-title"), hideSetup);
    $("#mcp-dialog .modal-card").scrollTop = 0;
  };
  $("#mcp-close").onclick = hideSetup;
  i18n.onTransition(({phase, source}) => {
    languagePending = phase === "start";
    if (!accepted) resetConsent();
    if (!languagePending) {
      languageOptions();
      if (source === "picker" && native) send({action: "setLanguage", preference: i18n.preference});
    }
  });
  for (const selector of ["#language-select", "#consent-language-select"]) $(selector).onchange = event =>
    i18n.setLocale(event.target.value, "picker");
  $("#consent-text").addEventListener("scroll", updateConsent);
  $("#consent-agree").onchange = updateConsent;
  $("#consent-continue").onclick = () => {
    updateConsent();
    if ($("#consent-continue").disabled || accepted) return;
    accepted = true;
    closeDialog($("#consent-dialog"));
    resolveConsent();
    window.dispatchEvent(new CustomEvent("screen-organizer-consent-accepted"));
    $("#read").focus();
  };
  $("#consent-decline").onclick = () => {
    declined = true;
    updateConsent();
    $("#consent-declined").hidden = false;
    // Request tab closure; a browser that keeps the tab open continues showing the declined state.
    window.close();
  };
  document.addEventListener("keydown", event => {
    if (!activeDialog) return;
    if (event.key === "Escape") {
      event.preventDefault();
      activeDialog.dismiss?.();
    } else if (event.key === "Tab") {
      const controls = [...activeDialog.root.querySelectorAll("button,input,select,textarea,summary,[href],[tabindex]")]
        .filter(node => !node.disabled && node.getAttribute("tabindex") !== "-1" && !node.closest("[hidden],[inert]") &&
          (!node.getClientRects || node.getClientRects().length));
      const index = controls.indexOf(document.activeElement);
      if (event.shiftKey ? index <= 0 : index === controls.length - 1) {
        event.preventDefault();
        (event.shiftKey ? controls.at(-1) : controls[0])?.focus();
      }
    }
  });
  const ready = (async () => {
    await i18n.ready;
    languagePending = i18n.pending;
    setupExamples();
    for (const id of ["consent-brand", "mcp-brand"]) $("#" + id).appendChild($(".brand svg").cloneNode(true));
    languageOptions();
    updateTitle();
    syncInert();
    i18n.onChange(updateTitle);
    if (!accepted) {
      openDialog($("#consent-dialog"), $("#consent-text"));
      requestAnimationFrame(updateConsent);
    }
    await consent;
  })();
  window.ScreenOrganizerOnboarding = {ready, openDialog, closeDialog, get dialog() { return activeDialog?.root; }, get accepted() { return accepted; }};
})();
