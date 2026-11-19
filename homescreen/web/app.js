// Screen Organizer edits local drafts through /api/op and returns an undoable view.
// Apply and Restore write layouts to the device after creating a backup.
"use strict";

const t = (key, params) => globalThis.ScreenOrganizerI18n?.t(key, params) || key;
const number = value => ScreenOrganizerI18n.formatNumber(value);

// A tab keeps its editor credential across reloads at this origin.
const editorSessionKey = "screenOrganizer.editorSession";
let editorCredential = "";
function withTabStorage(action) {
  try { return action(globalThis.sessionStorage); }
  catch (_) { /* The current page can use its in-memory credential. */ }
}
const authentication = (async () => {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  if (!token) {
    editorCredential = withTabStorage(store => store?.getItem(editorSessionKey)) || "";
    return;
  }
  withTabStorage(store => store?.removeItem(editorSessionKey));
  const response = await fetch("/v1/authenticate", {
    method: "POST", headers: { "X-Token": token }, mode: "same-origin", credentials: "omit", redirect: "error",
  });
  if (!response.ok) return {code: "invalid_credentials"};
  const {credential} = await response.json();
  if (typeof credential !== "string" || !credential) return {code: "invalid_credentials"};
  editorCredential = credential;
  withTabStorage(store => store?.setItem(editorSessionKey, credential));
})().catch(() => ({code: "invalid_credentials"}));

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

// Bind generated text directly to its source so user-provided names stay verbatim.
function localized(node, value, attribute = "textContent") {
  if (typeof value !== "function") {
    if (node.translations) delete node.translations[attribute];
    node[attribute] = value;
    return node;
  }
  node.setAttribute("data-localized", "");
  (node.translations ||= {})[attribute] = value;
  node[attribute] = value();
  return node;
}
function localizedEl(tag, cls, value) { return localized(el(tag, cls), value); }
function refreshText() {
  for (const node of document.querySelectorAll("[data-localized]")) {
    for (const [attribute, value] of Object.entries(node.translations)) node[attribute] = value();
  }
}
function locationText(descriptor) {
  if (!descriptor) return "";
  if (descriptor.kind === "library") return t("editor.library");
  if (descriptor.kind === "bank") return t("editor.bank");
  const params = Object.fromEntries(Object.entries(descriptor).map(([key, value]) =>
    [key, typeof value === "number" ? number(value) : value]));
  const label = t(descriptor.row ? "location.cell" : "location." + descriptor.kind, params);
  return descriptor.folder_id ? label + " [" + descriptor.folder_id + "]" : label;
}
function locationLabel(raw) { return V.location_labels?.[raw] ? locationText(V.location_labels[raw]) : raw; }
function diagnostic(message) {
  const details = el("details");
  details.append(localizedEl("summary", null, () => t("common.details")), el("pre", "diagnostic", message));
  return details;
}
function errorKey(message, code) {
  message = message.replace(/^(?:couldn't read the device|apply failed|restore failed): /, "");
  if (code === "read_only" || message === "read-only mode: device writes are disabled") return "editor.readOnly";
  if (code === "invalid_credentials") return "editor.authenticationFailed";
  if (/ (?:doesn't fit|can't be pinned) at row \d+, column \d+$/.test(message)) return "error.position";
  if (code === "stale_draft" || message === "the saved draft changed in another session; reopen it or read the device again") return "error.staleDraft";
  if (message === "the device placement changed in another session; read the device again" ||
      message === "device grid metrics must be positive integers; read the device again" ||
      message === "exported page grid dimensions are invalid or inconsistent") return "error.readAgain";
  if (message === "the USB root archive uses one display identifier for different apps") return "error.wireConflict";
  if (message === "keep a Home Screen page to preserve this archive's grid dimensions") return "error.keepPage";
  if (message === "list-format writes cannot keep apps in the App Library") return "editor.libraryUnavailable";
  if (message === "backup pin identifier format is unsupported" ||
      message === "backup folder instance does not identify a pinned folder on its page") return "error.savedPosition";
  if (message === "widgets can't go in the dock") return "error.widgetsDock";
  if (/^a (folder|widget) can't go inside a folder$/.test(message)) return "error.folderItem";
  if (/^only apps go in the App Library/.test(message)) return "editor.onlyAppsInLibrary";
  if (/^this device interface doesn't expose the App Library/.test(message) ||
      message === "page isn't empty and this device has no App Library to send its apps to") return "editor.libraryUnavailable";
  if (message === "move or remove the widgets on this page first") return "error.moveWidgets";
  if (/^another Session operation/.test(message)) return "error.busy";
  if (/^(no layout loaded|device identity|a pending write|the phone's|token doesn't match)/.test(message)) return "error.readAgain";
  if (message === "invalid saved pin memory; read again" ||
      message === "exported pin metadata has invalid page identifiers") return "error.readAgain";
  if (message === "list-format writes cannot send fixed positions; use ordinary page order") return "error.writeFormat";
  if (/^device grid changed(?: during read; no layout was loaded|; explicitly read the intended device again)$/.test(message) ||
      /^saved draft (?:identity is invalid|pin identity is unavailable|pin identity is ambiguous); call read again$/.test(message)) return "error.readAgain";
  if (message === "iOS 16 does not preserve fixed icon positions; use ordinary page order") return "error.ios16";
  if (message === "iOS 16 keeps these widgets ahead of folders; place the folder after the widgets") return "error.ios16FolderWidgets";
  if (/^ambiguous pin identifier on a (layout|backup) page$/.test(message)) return "error.ambiguousIdentifier";
  if (/ has no (known wire identifier|wire identifier for its pin); use ordinary page order$/.test(message)) return "error.pinIdentifier";
  if (message === "pinned backup has no saved grid dimensions; use a backup with grid provenance" ||
      message === "backup grid dimensions must be positive integers") return "error.backupGrid";
  if (["pins must identify items on each page", "invalid saved pin memory", "saved pin footprint does not fit the loaded grid"].includes(message) ||
      /^(backup pins must identify |backup pin is outside |backup pin footprint |backup layout has problems, not restoring: |invalid pin cell |pin identifiers do not match |pins do not fit the loaded grid:)/.test(message)) return "error.savedPosition";
  if (/^(backup |no backup named)/.test(message)) return "error.backup";
  if (/^draft has problems/.test(message)) return "validation.unknown";
  if (/^(can't place |cells are |that spot is taken |page \d+ has no room )/.test(message)) return "error.position";
  return "error.operation";
}
function usageError(message) {
  if (message?.startsWith("No synced Screen Time data on this Mac.")) return () => t("error.usageSync");
  if (message?.startsWith("macOS blocked access to Screen Time data.")) return () => t("error.usageAccess");
  if (message?.startsWith("This device has no memory reports yet")) return () => t("error.usageReports");
  if (message === "This source can't report usage.") return () => t("error.usageSource");
  const days = message?.match(/^No Screen Time data from this iPhone in the last (\d+) days\.$/);
  return days ? () => t("error.usageDays", {days: number(+days[1])}) : null;
}
function widgetSize(size) {
  return ["small", "medium", "large", "extralarge"].includes(size) ? t("editor.size." + size) : size;
}

function showError(error) {
  const key = errorKey(error.message, error.code);
  const usageMessage = key === "error.operation" && usageError(error.message);
  toast(usageMessage || (() => t(key)), "error", error.message);
}

let V = null;               // current view from the server
let movedIds = new Set();
const sel = new Set();      // selected item ids
let openFolder = null;      // id of the folder shown in the drawer
let folderOpener = null;
let drag = null;            // { ids }
let query = "";

// ---- server ----------------------------------------------------------------------------

async function editorFetch(path, options = {}) {
  const authenticationFailure = await authentication;
  if (authenticationFailure) {
    await globalThis.ScreenOrganizerI18n?.ready;
    const error = new Error(t("editor.authenticationFailed"));
    error.code = authenticationFailure.code;
    throw error;
  }
  const headers = {...options.headers};
  if (editorCredential) headers["X-Editor-Session"] = editorCredential;
  return fetch(path, {...options, headers, mode: "same-origin", credentials: "omit", redirect: "error"});
}

async function api(path, body, binding = V?.binding) {
  await globalThis.ScreenOrganizerOnboarding?.ready;
  if (V && V.read_only && ["/api/apply", "/api/restore"].includes(path)) {
    throw Object.assign(new Error(t("editor.readOnly")), {code: "read_only"});
  }
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({...body, binding}),
  };
  const r = await editorFetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) {
    if (data.view) setView(data.view);
    const message = typeof data.error === "string" ? data.error : data.error?.message;
    const error = new Error(message || t("common.requestFailed", {status: number(r.status)}));
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

async function op(name, args = {}, onSuccess, binding = V?.binding) {
  try {
    const view = await api("/api/op", { op: name, args }, binding);
    onSuccess?.();
    setView(view);
    return true;
  } catch (e) {
    showError(e);
    return false;
  }
}

async function withBusy(text, fn) {
  localized($("#busy-text"), text);
  const root = $("#busy");
  if (!ScreenOrganizerOnboarding.openDialog(root, root)) throw new Error(t("error.busy"));
  try { return await fn(); } finally { ScreenOrganizerOnboarding.closeDialog(root); }
}

// ---- helpers ---------------------------------------------------------------------------

const iconWork = new Map();
const pendingIcons = new Map();

function acquireIcon(bundle, revision) {
  const device = V?.binding?.device ?? V?.device_id;
  const key = JSON.stringify([device, revision, bundle]);
  let request = pendingIcons.get(key);
  if (!request) {
    request = {controller: new AbortController(), users: 0};
    pendingIcons.set(key, request);
    const read = () => editorFetch("/icon?id=" + encodeURIComponent(bundle), {
      headers: revision ? {"X-Draft-Revision": revision} : {}, signal: request.controller.signal,
    });
    request.result = read().then(async response => {
      // A rejected edit can replace the layout without changing its saved revision.
      if (response.status === 409 && pendingIcons.get(key) === request &&
          device === (V?.binding?.device ?? V?.device_id) && revision === V?.binding?.revision) {
        response = await read();
      }
      return {status: response.status, bytes: response.ok ? await response.blob() : null};
    }).finally(() => {
      if (pendingIcons.get(key) === request) pendingIcons.delete(key);
    });
  }
  request.users++;
  return {result: request.result, release() {
    if (--request.users === 0 && pendingIcons.get(key) === request) {
      pendingIcons.delete(key);
      request.controller.abort();
    }
  }};
}

function leaveIconRead(work) {
  work.interest?.release();
  work.interest = null;
}

const visibleIcons = new IntersectionObserver(entries => {
  for (const {target, isIntersecting} of entries) {
    const work = iconWork.get(target);
    if (!work) continue;
    if (isIntersecting && !work.interest) loadIcon(target, work);
    if (!isIntersecting && !work.url) leaveIconRead(work);
  }
}, {rootMargin: "100px"});

function releaseIcon(img) {
  const work = iconWork.get(img);
  if (!work) return;
  visibleIcons.unobserve(img);
  leaveIconRead(work);
  if (work.url) URL.revokeObjectURL(work.url);
  img.onload = img.onerror = null;
  iconWork.delete(img);
}

new MutationObserver(records => {
  if (!iconWork.size) return;
  for (const record of records) {
    for (const node of record.removedNodes) {
      if (node.nodeType !== 1 || node.isConnected) continue;
      if (node.tagName === "IMG") releaseIcon(node);
      node.querySelectorAll("img").forEach(releaseIcon);
    }
  }
}).observe(document, {childList: true, subtree: true});

async function loadIcon(img, work) {
  const revision = V?.binding?.revision;
  const interest = acquireIcon(work.bundle, revision);
  work.interest = interest;
  const current = () => img.isConnected && iconWork.get(img) === work && work.interest === interest;
  const failed = () => {
    if (!current()) return;
    const wrap = img.parentNode;
    releaseIcon(img);
    img.remove();
    fallback(wrap, work.name);
  };
  try {
    const {status, bytes} = await interest.result;
    if (!current()) return;
    if (status === 409 && revision !== V?.binding?.revision) {
      leaveIconRead(work);
      return loadIcon(img, work);
    }
    if (!bytes) return failed();
    work.url = URL.createObjectURL(bytes);
    img.onload = () => releaseIcon(img);
    img.onerror = failed;
    img.src = work.url;
  } catch (_) {
    failed();
  }
}

function iconImg(item, cls) {
  const wrap = el("div", cls || "tile");
  if (item.icon) {
    const img = new Image();
    img.alt = "";
    wrap.appendChild(img);
    iconWork.set(img, {bundle: item.icon, name: item.name});
    visibleIcons.observe(img);
  } else {
    fallback(wrap, item.name);
  }
  return wrap;
}

function fallback(wrap, name) {
  let h = 0;
  for (const c of name || "?") h = (h * 31 + c.charCodeAt(0)) % 360;
  wrap.style.background = `hsl(${h} 45% 52%)`;
  wrap.textContent = (name || "?").trim().charAt(0).toUpperCase();
}

// the device being edited: "iPad" or "iPhone", and its grid (iPads report their own)
const D = () => ((V && V.device && V.device.ProductType) || "").startsWith("iPad") ? "iPad" : "iPhone";
const G = () => (V && V.grid) || { cols: 4, rows: 6, dock: 4, folder: 9 };

const matches = (item) => !query || item.name.toLowerCase().includes(query) || item.id.toLowerCase().includes(query)
  || item.bundle_id?.toLowerCase().includes(query);

function findItem(id) {
  if (!V || !V.loaded) return null;
  const pools = [V.dock, ...V.pages, V.library || [], V.bank || []];
  for (const pool of pools) {
    for (const it of pool) {
      if (it.id === id) return it;
      for (const fp of it.pages || []) for (const c of fp) if (c.id === id) return c;
    }
  }
  return null;
}

function findItems(ids) {
  const wanted = new Set(ids), found = new Map();
  if (!V?.loaded || !wanted.size) return found;
  const visit = item => {
    const id = item.id;
    if (wanted.delete(id)) found.set(id, item);
    return !wanted.size;
  };
  for (const pool of [V.dock, ...V.pages, V.library || [], V.bank || []]) {
    for (const item of pool) {
      if (visit(item)) return found;
      for (const page of item.pages || []) for (const child of page) if (visit(child)) return found;
    }
  }
  return found;
}

let pendingDiagnostic = null;
function toast(message, kind = "", detail, backup, result) {
  const latest = {message, kind, detail, backup, result};
  localized($("#operation-status"), result ? () => [typeof message === "function" ? message() : message,
    backup && t("editor.backupSaved", {backup}), t("editor.verificationScope")].filter(Boolean).join(" ") : message);
  const box = $("#toast");
  const previous = box.querySelector("details");
  if (!box.hidden && previous && (pendingDiagnostic || previous.open || box.contains(document.activeElement))) {
    if (detail || result) {
      pendingDiagnostic = latest;
      box.querySelector(".pending-result")?.remove();
      const pending = el("section", "pending-result");
      pending.appendChild(localizedEl("p", null, message));
      if (result) resultDetails(pending, result);
      box.appendChild(pending);
    }
    return;
  }
  displayToast(latest);
}

function displayToast({message, kind, detail, backup, result}) {
  const box = $("#toast");
  pendingDiagnostic = null;
  clearTimeout(toast.timer);
  box.replaceChildren(localizedEl("span", null, message));
  box.className = "toast " + kind;
  box.hidden = false;
  if (detail || backup || result) {
    const close = localizedEl("button", null, () => t("common.close"));
    close.onclick = () => {
      if (pendingDiagnostic) {
        displayToast(pendingDiagnostic);
        (box.querySelector("summary") || box.querySelector("button")).focus();
      } else {
        box.hidden = true;
        $("#read").focus();
      }
    };
    if (result) resultDetails(box, result);
    else {
      if (detail) box.appendChild(diagnostic(detail));
      if (backup) box.appendChild(localizedEl("p", null, () => t("editor.backupSaved", {backup})));
    }
    box.appendChild(close);
  } else {
    toast.timer = setTimeout(() => { box.hidden = true; }, kind === "error" ? 6000 : 3000);
  }
}

function modal({ title, body, actions, initialInput }) {
  const dialogs = ScreenOrganizerOnboarding;
  if (dialogs.dialog) return Promise.resolve(null);
  return new Promise(resolve => {
    const root = $("#modal");
    let closed = false;
    const close = value => {
      if (closed || !dialogs.closeDialog(root)) return;
      closed = true;
      if (initialInput) initialInput.onkeydown = null;
      resolve(value);
    };
    localized($("#modal-title"), title);
    const b = $("#modal-body");
    b.replaceChildren();
    if (typeof body === "string") b.appendChild(el("p", null, body));
    else if (typeof body === "function") b.appendChild(localizedEl("p", null, body));
    else if (body) b.appendChild(body);
    const a = $("#modal-actions");
    a.replaceChildren();
    for (const act of actions) {
      const btn = localizedEl("button", act.primary ? "primary" : act.danger ? "danger" : "", act.label);
      btn.onclick = () => close(act.value ? act.value() : null);
      a.appendChild(btn);
    }
    for (const button of b.querySelectorAll("[data-backup]")) button.onclick = () => close(button.dataset.backup);
    dialogs.openDialog(root, initialInput || a.firstChild, () => close(null));
    if (initialInput) {
      const confirm = a.querySelector(".primary");
      initialInput.select();
      initialInput.onkeydown = event => {
        if (event.key === "Enter" && !event.isComposing) {
          event.preventDefault();
          confirm.click();
        }
      };
    }
  });
}

async function promptText(title, label, value) {
  const wrap = el("div");
  if (label) {
    const description = localizedEl("p", null, label);
    description.id = "folder-prompt-description";
    wrap.appendChild(description);
  }
  const inp = el("input");
  inp.setAttribute("data-i18n-aria-label", "editor.folderName");
  inp.setAttribute("aria-label", t("editor.folderName"));
  if (label) inp.setAttribute("aria-describedby", "folder-prompt-description");
  inp.value = value || "";
  wrap.appendChild(inp);
  return modal({ title, body: wrap, initialInput: inp, actions: [
    { label: () => t("common.cancel"), value: () => null },
    { label: () => t("common.ok"), primary: true, value: () => inp.value.trim() || null },
  ] });
}

const confirmBox = (title, text, label = () => t("common.continue"), danger = false) => modal({ title, body: text, actions: [
  { label: () => t("common.cancel"), value: () => false },
  { label, primary: !danger, danger, value: () => true },
] });

// ---- rendering --------------------------------------------------------------------------

function clearEditor() {
  sel.clear();
  openFolder = null;
  folderOpener = null;
  drag = null;
}

function setView(v) {
  const deviceChanged = V && V.device_id !== v.device_id;
  if (!v.loaded || deviceChanged) {
    clearEditor();
    for (const img of iconWork.keys()) releaseIcon(img);
    libraryData = null;
    libraryRows.clear();
    $("#library").replaceChildren();
  }
  V = v;
  movedIds = new Set(v.loaded ? v.moved || [] : []);
  const selected = findItems(sel);
  for (const id of [...sel]) if (!selected.has(id)) sel.delete(id);
  if (openFolder && !findItem(openFolder)) {
    openFolder = null;
    folderOpener = null;
  }
  render(!deviceChanged, selected);
}

function focusItem(item, reveal = false) {
  if (!item) return false;
  const controls = [...document.querySelectorAll("[data-focus-context]")];
  const visible = node => !node.disabled && !node.closest("[inert]")?.inert &&
    (!node.getClientRects || node.getClientRects().length);
  const node = controls.find(node => node.dataset.id === item.id && node.dataset.focusContext === item.context && visible(node))
    || controls.find(node => item.id && node.dataset.id === item.id && visible(node))
    || controls.find(node => item.page && node.dataset.focusContext.startsWith(`page:${item.page}:`) && visible(node));
  if (!node) return false;
  node.focus({preventScroll: !reveal});
  if (!reveal) return true;
  const rect = node.getBoundingClientRect();
  return document.activeElement === node &&
    node.contains(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2));
}

function focusEditorContext() {
  const field = openFolder ? $("#folder-name") : $("#search");
  (field.disabled ? $("#read") : field).focus();
}

function render(restoreFocus = true, selected) {
  const active = restoreFocus && document.activeElement?.closest("[data-focus-context]");
  const focused = active && {id: active.dataset.id, context: active.dataset.focusContext};
  $("#read-only-notice").hidden = !(V && V.read_only);
  const loaded = V && V.loaded;
  const recovery = $("#draft-recovery");
  const draftError = !loaded && V?.draft_error;
  recovery.hidden = !draftError;
  if (draftError && recovery.querySelector("pre")?.textContent !== draftError) {
    const notice = localizedEl("p", null, () => t("editor.savedDraftUnavailable"));
    notice.setAttribute("role", "status");
    recovery.replaceChildren(notice, diagnostic(draftError));
  } else if (!draftError && recovery.firstChild) {
    recovery.replaceChildren();
  }
  $("#empty").hidden = loaded;
  $("#board").hidden = !loaded;
  $("#library-panel").hidden = !loaded;
  for (const id of ["undo", "redo", "reset", "organize-btn", "search"]) $("#" + id).disabled = !loaded;
  if (!loaded) {
    if (!$("#organize-menu").hidden) {
      const focused = $("#organize-menu").contains(document.activeElement);
      setPopup("organize", false);
      if (focused) $("#read-empty").focus();
    }
    $("#device").textContent = V && V.source && V.source !== "phone" ? V.source : (deviceLabel() || t("editor.noDevice"));
    $("#read").textContent = t("editor.readDevice");
    $("#drawer").hidden = true;
    syncDrawerCoverage();
    $("#folder-pages").replaceChildren();
    $("#folder-name").value = "";
    $("#selection-bar").hidden = true;
    $("#sel-count").textContent = "";
    renderFooter();
    return;
  }
  const d = V.device || {};
  $("#device").textContent = [d.DeviceName, d.ProductVersion && (D() === "iPad" ? "iPadOS " : "iOS ") + d.ProductVersion].filter(Boolean).join(" · ") || V.source;
  $("#read").textContent = t("editor.readNamedDevice", {device: D()});
  $("#undo").disabled = !V.history.undo;
  $("#redo").disabled = !V.history.redo;
  renderDock();
  renderPages();
  renderLibrary();
  renderBank();
  renderUsage();
  renderDrawer();
  renderSelection(selected);
  renderFooter();
  focusItem(focused);
  positionPopups();
}

function itemControl(node, item, context) {
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", item.kind === "widget" ? node.title : item.name);
  if (item.kind === "widget") node.setAttribute("aria-haspopup", "dialog");
  else node.setAttribute("aria-pressed", String(sel.has(item.id)));
  node.dataset.focusContext = context;
  if (item.kind === "folder") {
    node.setAttribute("aria-expanded", String(openFolder === item.id));
    node.setAttribute("aria-controls", "drawer");
  }
  const activate = event => onItemClick(event, node.libraryItem || item, context);
  node.addEventListener("click", activate);
  node.addEventListener("keydown", event => {
    if (!["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    if (!event.repeat) activate(event);
  });
}

function renderItem(item, container) {
  const moved = movedIds.has(item.id);
  if (item.kind === "widget") {
    const w = el("div", `widget w-${item.size}`);
    w.dataset.id = item.id;
    const ic = iconImg(item, "wicon");
    w.append(ic, el("div", "wname", item.name), el("div", "wkind", `${item.stack ? t("editor.smartStack") : t("editor.widget")} · ${widgetSize(item.size)}`));
    w.title = t("editor.widgetTitle", {name: item.name, kind: t(item.stack ? "editor.smartStack" : "editor.widget"), size: widgetSize(item.size)});
    itemControl(w, item, container.target);
    wireDrag(w, item, container);
    return w;
  }
  const node = el("div", "item" + (item.kind === "folder" ? " folder" : ""));
  node.dataset.id = item.id;
  if (item.kind === "folder") {
    const tile = el("div", "tile");
    const kids = (item.pages || []).flat().slice(0, 9);
    for (const k of kids) tile.appendChild(iconImg(k, "mini"));
    node.appendChild(tile);
  } else {
    node.appendChild(iconImg(item));
  }
  node.appendChild(el("div", "name", item.name));
  node.title = item.kind === "folder"
    ? t("editor.folderTitle", {name: item.name, count: number((item.pages || []).flat().length)}) : `${item.name}\n${item.id}`;
  if (sel.has(item.id)) node.classList.add("selected");
  if (moved) node.classList.add("moved");
  if (query) node.classList.add(matches(item) || (item.pages || []).flat().some(matches) ? "hit" : "dim");
  itemControl(node, item, container.target);
  wireDrag(node, item, container);
  return node;
}

function renderDock() {
  const dock = $("#dock");
  dock.style.setProperty("--dock-cols", Math.max(4, V.dock.length + (V.dock.length < G().dock ? 1 : 0)));
  dock.replaceChildren(...V.dock.map((it) => renderItem(it, { target: "dock" })));
}

function renderPages() {
  const box = $("#pages");
  box.replaceChildren();
  V.pages.forEach((page, i) => {
    const n = i + 1;
    const card = el("div", "page");
    const head = el("div", "page-head");
    head.appendChild(localizedEl("span", "num", () => t("editor.page", {number: number(n)})));
    const used = page.reduce((a, it) => a + (it.slots || 1), 0);
    const over = V.diff.problems.some((p) => p.startsWith(`page ${n} needs`));
    const trusted = used > V.slots.page && !over;  // an iPad widget page as iPadOS laid it out
    const slots = el("span", "slots" + (over ? " over" : ""), trusted ? t("editor.onDevice") : `${number(used)}/${number(V.slots.page)}`);
    if (trusted) slots.title = t("editor.widgetGridHint");
    head.appendChild(slots);
    const tools = el("div", "tools");
    const action = async (name, args, page, control, binding = V?.binding) => {
      if (!await op(name, args, undefined, binding)) return;
      const target = Math.min(page, V.pages.length);
      focusItem(target ? {context: `page:${target}:${control}`, page: target} : {context: "add-page"});
    };
    const tb = (control, label, title, fn, disabled) => {
      const b = el("button", null, label);
      b.title = title;
      b.setAttribute("aria-label", title);
      b.disabled = !!disabled;
      b.dataset.focusContext = `page:${n}:${control}`;
      b.onclick = fn;
      tools.appendChild(b);
    };
    tb("left", "◀", t("editor.movePageLeft"), () => action("move_page", { page: n, to: n - 1 }, n - 1, "left"), n === 1);
    tb("right", "▶", t("editor.movePageRight"), () => action("move_page", { page: n, to: n + 1 }, n + 1, "right"), n === V.pages.length);
    tb("insert", "＋", t("editor.insertPage"), () => action("insert_page", { page: n + 1 }, n + 1, "insert"));
    tb("delete", "✕", t("editor.deletePageHint"), async () => {
      const binding = V?.binding;
      if (page.length && !(await confirmBox(() => t("editor.deletePageTitle", {number: number(n)}), () =>
        t("editor.deletePageBody"), () => t("editor.deletePage"), true))) return;
      action("delete_page", { page: n }, n, "delete", binding);
    });
    head.appendChild(tools);
    card.appendChild(head);
    const grid = el("div", "grid");
    const { cols, rows } = G();
    grid.style.setProperty("--cols", cols);
    grid.style.setProperty("--rows", rows);
    const ctx = { target: `page:${n}` };
    const taken = new Set();
    for (const it of page) {
      const node = renderItem(it, ctx);
      if (it.pos) {
        const [r, c, w, h] = it.pos;
        node.style.gridRow = `${r + 1} / span ${h}`;
        node.style.gridColumn = `${c + 1} / span ${w}`;
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) taken.add((r + y) * cols + c + x);
      }
      if (it.pinned) node.classList.add("pinned");
      grid.appendChild(node);
    }
    for (let cell = 0; cell < cols * rows && !trusted; cell++) {
      if (taken.has(cell)) continue;
      const slot = el("div", "slot");
      slot.style.gridRow = `${Math.floor(cell / cols) + 1}`;
      slot.style.gridColumn = `${(cell % cols) + 1}`;
      slot.title = t("editor.dropExact");
      wireSlot(slot, n, cell);
      grid.appendChild(slot);
    }
    card.appendChild(grid);
    wireZone(card, ctx);
    box.appendChild(card);
  });
  const add = localizedEl("button", "add-page", () => t("editor.newPage"));
  add.dataset.focusContext = "add-page";
  add.title = t("editor.newPageHint");
  add.onclick = () => op("insert_page", { page: V.pages.length + 1 });
  wireZone(add, { target: `page:${V.pages.length + 1}`, newPage: true });
  box.appendChild(add);
}

function renderSearch() {
  if (!V?.loaded) return;
  const items = new Map();
  for (const pool of [V.dock, ...V.pages, V.library || [], V.bank || []]) {
    for (const item of pool) {
      items.set(item.id, item);
      for (const page of item.pages || []) for (const child of page) items.set(child.id, child);
    }
  }
  for (const node of document.querySelectorAll(".item[data-id]")) {
    if (node.closest("#folder-pages") && !openFolder) continue;
    const item = items.get(node.dataset.id);
    const hit = matches(item) || (item.pages || []).flat().some(matches);
    node.classList.toggle("hit", Boolean(query) && hit);
    node.classList.toggle("dim", Boolean(query) && !hit);
  }
  renderLibrary();
}

let libraryCollator, libraryLocale, libraryData, libraryOrder = [];
let libraryRows = new Map();
function renderLibrary() {
  const list = $("#library");
  if (libraryLocale !== ScreenOrganizerI18n.locale) {
    libraryLocale = ScreenOrganizerI18n.locale;
    libraryCollator = new Intl.Collator(libraryLocale);
    libraryData = null;
    libraryRows.clear();
    list.replaceChildren();
  }
  const items = V.library;
  if (items && (!libraryData || items.length !== libraryData.length || (items !== libraryData &&
      items.some((item, i) => item.id !== libraryData[i].id || item.name !== libraryData[i].name)))) {
    libraryOrder = [...items.keys()].sort((a, b) => libraryCollator.compare(items[a].name, items[b].name));
  }
  libraryData = items;
  const visible = items ? libraryOrder.map(i => items[i]).filter(matches) : [];
  $("#lib-count").textContent = !items ? "" : query
    ? t("editor.matchCount", {count: number(visible.length), total: number(items.length)}) : number(items.length);
  if (!visible.length) {
    libraryRows.clear();
    const key = !items ? "editor.libraryUnavailable" : query ? "editor.noMatches" : "editor.libraryEmpty";
    list.replaceChildren(localizedEl("div", "lib-empty", () => t(key)));
    return;
  }
  const next = new Map();
  for (const item of visible) {
    let row = libraryRows.get(item.id);
    const old = row?.libraryItem;
    if (!old || old.name !== item.name || old.icon !== item.icon || old.kind !== item.kind) {
      row = el("div", "lib-row");
      row.dataset.id = row.title = item.id;
      row.append(iconImg(item), el("span", "n", item.name));
      itemControl(row, item, "library");
      wireDrag(row, item, {target: "library"});
    }
    row.libraryItem = item;
    setSelected(row, sel.has(item.id));
    const moved = movedIds.has(item.id);
    if (row.classList.contains("moved") !== moved) row.classList.toggle("moved", moved);
    next.set(item.id, row);
  }
  if (!libraryRows.size) {
    list.replaceChildren(...next.values());
  } else {
    for (const [id, row] of libraryRows) if (next.get(id) !== row) row.remove();
    let i = 0;
    for (const row of next.values()) {
      if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
      i++;
    }
  }
  libraryRows = next;
}

function renderBank() {
  const bank = $("#bank");
  bank.replaceChildren(...V.bank.map((it) => renderItem(it, { target: "bank" })));
  if (!V.bank.length) bank.appendChild(localizedEl("div", "empty-note", () => t("editor.bankEmpty")));
  $("#bank-count").textContent = V.bank.length ? number(V.bank.length) : "";
}

function renderUsage() {
  const box = $("#usage");
  box.replaceChildren(localizedEl("h2", null, () => t("editor.usageTitle")));
  const u = V.usage;
  if (!u || !u.ok) {
    box.appendChild(localizedEl("p", "note", usageError(u?.error) || (() => t(u?.error ? "error.usage" : "editor.usageHint", {device: D()}))));
    if (u?.error) box.appendChild(diagnostic(u.error));
    const row = el("div", "usage-actions");
    const a = localizedEl("button", "primary", () => t("editor.usageFromDevice", {device: D()}));
    a.onclick = () => withBusy(() => t("editor.usageReadingDevice", {device: D()}), () => op("load_usage", { source: "device" }));
    const b = localizedEl("button", null, () => t("editor.usageFromMac"));
    b.title = t("editor.usageMacHint");
    b.onclick = () => withBusy(() => t("editor.usageReadingMac"), () => op("load_usage", { source: "mac" }));
    row.append(a, b);
    box.appendChild(row);
    return;
  }
  const max = Math.max(u.original, u.now, 1);
  const bars = el("div", "bars");
  for (const [label, val, cls] of [[t("editor.usageOnDevice", {device: D()}), u.original, ""], [t("editor.thisDraft"), u.now, "now"]]) {
    const b = el("div", "ubar " + cls);
    const i = el("i");
    i.style.height = `${Math.max(4, Math.round((val / max) * 62))}px`;
    b.append(el("b", null, number(val)), i, el("span", null, label));
    bars.appendChild(b);
  }
  box.appendChild(el("p", "note", u.source === "device"
    ? t("editor.usageReports", {reports: number(u.reports), device: D(), days: number(u.days)})
    : t("editor.usageScreenTime", {days: number(u.days)})));
  box.appendChild(bars);
  if (u.original) {
    const pct = Math.round((1 - u.now / u.original) * 100);
    box.appendChild(el("div", "gain" + (pct < 0 ? " worse" : ""),
      pct > 0 ? t("editor.fewerSwipes", {percent: number(pct)}) : pct < 0 ? t("editor.moreSwipes", {percent: number(-pct)}) : t("editor.sameSwipes", {device: D()})));
  }
  const ul = el("ul", "top");
  for (const a of u.top) {
    const li = el("li");
    li.append(iconImg({ icon: a.id, name: a.name }), el("span", "n", a.name),
      el("span", "w", u.source === "device" ? locationLabel(a.where) : `${number(a.opens)} · ${locationLabel(a.where)}`));
    ul.appendChild(li);
  }
  box.appendChild(ul);
  if (u.background && u.background.length) {
    const n = localizedEl("p", "note bg", () => t("editor.backgroundApps", {apps: u.background.slice(0, 8).join(", ") + (u.background.length > 8 ? "…" : "")}));
    box.appendChild(n);
  }
}

function renderDrawer() {
  const folder = openFolder && findItem(openFolder);
  $("#drawer").hidden = !folder;
  syncDrawerCoverage();
  if (!folder) return;
  const name = $("#folder-name");
  if (document.activeElement !== name) name.value = folder.name;
  const box = $("#folder-pages");
  box.replaceChildren();
  folder.pages.forEach((page, i) => {
    const fp = el("div", "fpage");
    fp.appendChild(localizedEl("div", "num", () => t("editor.folderPage", {number: number(i + 1), count: number(page.length), capacity: number(G().folder)})));
    const g = el("div", "fgrid");
    g.style.setProperty("--fcols", G().folder_columns || Math.round(Math.sqrt(G().folder)));
    const ctx = { target: `${folder.id}:${i + 1}` };
    for (const it of page) g.appendChild(renderItem(it, ctx));
    fp.appendChild(g);
    wireZone(fp, ctx);
    box.appendChild(fp);
  });
}

function showFolder(id) {
  for (const folderId of new Set([openFolder, id])) {
    if (folderId === null) continue;
    for (const control of document.querySelectorAll(`[data-id="${CSS.escape(folderId)}"]`)) {
      if (control.getAttribute("aria-controls") === "drawer") {
        control.setAttribute("aria-expanded", String(folderId === id));
      }
    }
  }
  openFolder = id;
  renderDrawer();
  positionPopups();
}

function syncDrawerCoverage() {
  const drawer = $("#drawer"), main = $("#main"), board = $("#board"), library = $("#library-panel");
  const open = !drawer.hidden;
  main.classList.toggle("drawer-open", open);
  let covered = false;
  if (open) {
    const grid = board.querySelector(".grid") || $("#dock");
    const cellWidth = parseFloat(getComputedStyle(grid).gridTemplateColumns);
    const exposed = drawer.getBoundingClientRect().left - board.getBoundingClientRect().left;
    const available = Math.min(exposed, $("#pages").getBoundingClientRect().width);
    covered = !(cellWidth > 0 && available >= cellWidth);
  }
  const active = document.activeElement;
  const redirect = open && (library.contains(active) || (covered && (board.contains(active) || active === main)));
  board.inert = covered;
  library.inert = open;
  if (covered) main.setAttribute("tabindex", "-1");
  else main.removeAttribute("tabindex");
  if (redirect && !ScreenOrganizerOnboarding.dialog) $("#folder-name").focus();
}

function renderSelection(selected = findItems(sel)) {
  const bar = $("#selection-bar");
  bar.hidden = sel.size === 0;
  $("#sel-count").textContent = t("editor.selected", {count: number(sel.size)});
  const eligible = selectedApps(sel, selected);
  $("#sel-folder").disabled = !eligible.length;
  $("#sel-library").disabled = V.library === null || !eligible.length;
  $("#sel-place").disabled = sel.size !== 1 || !["app", "webclip", "folder"].includes(selected.get([...sel][0])?.kind);
  $("#sel-unpin").hidden = ![...sel].some((i) => selected.get(i)?.pinned);
}

function setSelected(node, selected) {
  if (node.classList.contains("selected") !== selected) node.classList.toggle("selected", selected);
  if (node.getAttribute("aria-pressed") !== String(selected)) node.setAttribute("aria-pressed", String(selected));
}

function updateSelection(previous) {
  for (const id of new Set([...previous, ...sel])) {
    const selected = sel.has(id);
    if (previous.has(id) === selected) continue;
    for (const node of document.querySelectorAll(`[data-id="${CSS.escape(id)}"]`)) setSelected(node, selected);
  }
  renderSelection();
}

function clearSelection() {
  const previous = new Set(sel);
  sel.clear();
  updateSelection(previous);
  return previous;
}

function renderFooter() {
  const s = $("#summary");
  const btn = $("#review");
  if (!V || !V.loaded) { s.textContent = t("editor.readToStart"); btn.disabled = true; return; }
  const d = V.diff;
  const parts = [];
  if (!d.has_changes) parts.push(t("editor.noChanges"));
  else {
    if (d.items_moved) parts.push(t("editor.appsMoved", {count: number(d.items_moved)}));
    else parts.push(t("editor.layoutChanged"));
    if (d.to_app_library) parts.push(t("editor.toLibraryCount", {count: number(d.to_app_library)}));
    if (d.pages_before !== d.pages_after) parts.push(t("editor.pagesChanged", {before: number(d.pages_before), after: number(d.pages_after)}));
    if (d.gaps_changed) parts.push(t("editor.gapsChanged", {count: number(d.gaps_changed)}));
  }
  s.textContent = parts.join(" · ");
  if (d.problems.length) s.appendChild(el("span", "problem", " · " + t("editor.problemCount", {count: number(d.problems.length)})));
  btn.disabled = V.read_only || !d.has_changes;
}

// ---- interaction -----------------------------------------------------------------------

function onItemClick(e, item, context) {
  e.stopPropagation();
  if (item.kind === "widget") {
    if (!e.metaKey && !e.ctrlKey && !e.shiftKey) editPlacement(item, context);
    return;
  }
  if (e.metaKey || e.ctrlKey || e.shiftKey) {
    const previous = new Set(sel);
    sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
    updateSelection(previous);
    return;
  }
  if (item.kind === "folder") {
    folderOpener = {id: item.id, context};
    clearSelection();
    showFolder(item.id);
    $("#folder-name").focus();
    return;
  }
  const previous = new Set(sel);
  if (sel.size && !sel.has(item.id)) sel.clear();
  sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
  updateSelection(previous);
}

async function editPlacement(item, context) {
  if (ScreenOrganizerOnboarding.dialog) return;
  const widget = item.kind === "widget", prefix = widget ? "widget" : "item";
  const binding = V?.binding, grid = G();
  const body = el("div", "widget-fields");
  const field = (id, key, options, value) => {
    const label = el("label");
    label.appendChild(localizedEl("span", null, () => t(key)));
    const select = el("select");
    select.id = id;
    for (const [value, text] of options) {
      const option = localizedEl("option", null, text);
      option.value = value;
      select.appendChild(option);
    }
    select.value = value;
    label.appendChild(select);
    body.appendChild(label);
    return select;
  };
  const pages = V.pages.map((_, i) => [`page:${i + 1}`, () => t("editor.page", {number: number(i + 1)})]);
  const targets = widget ? [["bank", () => t("editor.bank")], ...pages] : [...pages,
    [`page:${V.pages.length + 1}`, () => t("editor.newPage")], ["dock", () => t("editor.dock")], ["bank", () => t("editor.bank")]];
  if (!widget && item.kind !== "folder") {
    if (V.library !== null) targets.push(["library", () => t("editor.library")]);
    const options = [], counts = new Map();
    for (const folder of [V.dock, ...V.pages, V.bank].flat().filter(it => it.kind === "folder")) {
      // HTML option labels strip and collapse ASCII whitespace.
      const label = folder.name.replace(/[\t\n\f\r ]+/g, " ").replace(/^ | $/g, "");
      for (let page = 1; page <= folder.pages.length + 1; page++) {
        const key = JSON.stringify([label, page]);
        counts.set(key, (counts.get(key) || 0) + 1);
        options.push({folder, page, key});
        if (!folder.pages[page - 1]?.length) break;
      }
    }
    for (const {folder, page, key} of options) {
      const qualifier = counts.get(key) > 1 ? ` [${folder.id}]` : "";
      targets.push([`${folder.id}:${page}`, () =>
        `${folder.name} · ${t("editor.page", {number: number(page)})}${qualifier}`]);
    }
  }
  const target = field(`${prefix}-target`, "editor.destination", targets, context);
  const row = field(`${prefix}-row`, "editor.row", Array.from({length: grid.rows}, (_, i) => [String(i), () => number(i + 1)]), String(item.pos?.[0] || 0));
  const column = field(`${prefix}-column`, "editor.column", Array.from({length: grid.cols}, (_, i) => [String(i), () => number(i + 1)]), String(item.pos?.[1] || 0));
  const choice = modal({title: () => t("editor.editWidget", {name: item.name}), body, actions: [
    {label: () => t("common.cancel"), value: () => null},
    {label: () => t(widget ? "editor.moveWidget" : "editor.moveItem"), primary: true, value: () => "move"},
    {label: () => t("editor.placeWidget"), value: () => "pin"},
  ]});
  const update = () => {
    row.disabled = column.disabled = $("#modal-actions").lastChild.disabled = !target.value.startsWith("page:");
  };
  target.onchange = update;
  update();
  const operation = await choice;
  let destination = context;
  if (operation) {
    const args = operation === "pin" ? {id: item.id, page: Number(target.value.slice(5)), cell: Number(row.value) * grid.cols + Number(column.value)}
      : {id: item.id, target: target.value};
    if (await op(operation, args, widget ? undefined : () => sel.clear(), binding)) destination = target.value;
  }
  if (ScreenOrganizerOnboarding.dialog || focusItem({id: item.id, context: destination}, true)) return;
  if (destination.startsWith("folder:") &&
      focusItem({id: destination.slice(0, destination.lastIndexOf(":"))}, true)) return;
  focusEditorContext();
}

function clearMarks() {
  document.querySelectorAll(".ins-before,.ins-after,.ins-onto,.drop-zone,.drop-cell").forEach((n) =>
    n.classList.remove("ins-before", "ins-after", "ins-onto", "drop-zone", "drop-cell"));
}

function zoneOf(e, node, item, ctx) {
  const r = node.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  if (item.kind !== "widget" && x > 0.28 && x < 0.72 && !drag.ids.includes(item.id)) {
    if (!ctx.target.startsWith("folder:") || (e.altKey && drag.ids.length === 1)) return "onto";
    const siblings = [...node.parentElement.children].map(child => child.dataset.id);
    const from = siblings.findIndex(id => drag.ids.includes(id));
    return from >= 0 && from < siblings.indexOf(item.id) ? "after" : "before";
  }
  return x < 0.5 ? "before" : "after";
}

function wireDrag(node, item, ctx) {
  node.draggable = true;
  node.addEventListener("dragstart", (e) => {
    const ids = sel.has(item.id) ? [...sel] : [item.id];
    drag = { ids };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", ids.join(","));
    requestAnimationFrame(() => node.classList.add("dragging"));
  });
  node.addEventListener("dragend", () => { node.classList.remove("dragging"); drag = null; clearMarks(); });
  if (ctx.target === "library") return; // library rows aren't insertion points
  node.addEventListener("dragover", (e) => {
    if (!drag) return;
    e.preventDefault();
    e.stopPropagation();
    clearMarks();
    node.classList.add("ins-" + zoneOf(e, node, item, ctx));
  });
  node.addEventListener("drop", (e) => {
    if (!drag) return;
    e.preventDefault();
    e.stopPropagation();
    const zone = zoneOf(e, node, item, ctx);
    const ids = drag.ids.filter((i) => i !== item.id);
    clearMarks();
    if (!ids.length) return;
    if (zone === "onto" && e.altKey && ids.length === 1) return op("swap", { a: ids[0], b: item.id });
    if (zone === "onto") return dropOnto(item, ids, ctx);
    const siblings = [...node.parentElement.children].map((c) => c.dataset.id).filter(Boolean);
    let before = item.id;
    if (zone === "after") {
      const rest = siblings.slice(siblings.indexOf(item.id) + 1).filter((i) => !ids.includes(i));
      before = rest[0] || null;
    }
    moveIds(ids, ctx.target, before);
  });
}

function wireSlot(slot, page, cell) {
  slot.addEventListener("dragover", (e) => {
    if (!drag || drag.ids.length !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    clearMarks();
    slot.classList.add("drop-cell");
  });
  slot.addEventListener("dragleave", () => slot.classList.remove("drop-cell"));
  slot.addEventListener("drop", (e) => {
    if (!drag || drag.ids.length !== 1) return;
    e.preventDefault();
    e.stopPropagation();
    const id = drag.ids[0];
    clearMarks();
    op("pin", { id, page, cell }, () => sel.clear());
  });
}

function wireZone(node, ctx) {
  node.addEventListener("dragover", (e) => {
    if (!drag || (ctx.target === "library" && !libraryApps(drag.ids).length)) return;
    e.preventDefault();
    clearMarks();
    node.classList.add("drop-zone");
  });
  node.addEventListener("dragleave", (e) => { if (!node.contains(e.relatedTarget)) node.classList.remove("drop-zone"); });
  node.addEventListener("drop", (e) => {
    if (!drag) return;
    e.preventDefault();
    const ids = drag.ids;
    clearMarks();
    moveIds(ids, ctx.target, null);
  });
}

async function moveIds(ids, target, before) {
  if (target === "library") ids = libraryApps(ids);
  if (!ids.length) return;
  if (ids.length === 1) return op("move", { id: ids[0], target, before }, () => sel.clear());
  return op("move_many", { ids, target, before }, () => sel.clear());
}

async function dropOnto(item, ids, ctx) {
  const binding = V?.binding;
  if (item.kind === "folder") {
    if (await op("move_many", { ids, target: item.id }, () => sel.clear())) toast(() => t("editor.movedInto", {name: item.name}));
    return;
  }
  const name = await promptText(() => t("editor.newFolder"), () => t("editor.folderWith", {name: item.name, count: number(ids.length)}), t("editor.folderDefault"));
  if (!name) return;
  const page = ctx.target;
  const siblings = locateIndex(item.id, page);
  await op("create_folder", { name, ids: [item.id, ...ids], target: page, index: siblings }, () => sel.clear(), binding);
}

function locateIndex(id, target) {
  const list = target === "dock" ? V.dock : target.startsWith("page:") ? V.pages[+target.slice(5) - 1] : [];
  const i = (list || []).findIndex((x) => x.id === id);
  return i < 0 ? null : i;
}

async function newFolderFromSelection() {
  const binding = V?.binding;
  const ids = selectedApps(sel);
  if (!ids.length) return;
  const name = await promptText(() => t("editor.newFolder"), () => t("editor.folderFromSelection", {count: number(ids.length)}), t("editor.folderDefault"));
  if (!name) return;
  // put it where the first selected app on a page is, else at the end of the last page
  let target = `page:${V.pages.length || 1}`, index = null;
  for (let p = 0; p < V.pages.length; p++) {
    const i = V.pages[p].findIndex((x) => ids.includes(x.id));
    if (i >= 0) { target = `page:${p + 1}`; index = i; break; }
  }
  await op("create_folder", { name, ids, target, index }, () => sel.clear(), binding);
}

function selectedApps(ids, selected = findItems(ids)) {
  return [...ids].filter(id => ["app", "webclip"].includes(selected.get(id)?.kind));
}

function libraryApps(ids) {
  return V?.library == null ? [] : selectedApps(ids);
}

function selectionToLibrary() {
  return moveIds([...sel], "library", null);
}

// ---- devices ------------------------------------------------------------------------------

let DEV = { switching: false, current: null, devices: [] };

const deviceLabel = () => {
  const d = DEV.devices.find((x) => x.udid === DEV.current);
  return d ? d.name || d.udid : "";
};

async function loadDevices() {
  try { DEV = await api("/api/devices"); }
  catch (_) { DEV = {...DEV, discovery: "unavailable", devices: DEV.devices.map(d => ({...d, connected: null}))}; }
  const button = $("#device-btn"), available = DEV.switching || DEV.discovery === "unavailable";
  button.classList.toggle("static", !available);
  button.disabled = !available;
  if (available) {
    button.removeAttribute("tabindex");
    button.setAttribute("aria-controls", "device-menu");
    button.setAttribute("aria-expanded", String(!$("#device-menu").hidden));
    button.setAttribute("data-i18n-title", "editor.chooseDevice");
    button.title = t("editor.chooseDevice");
  } else {
    setPopup("device", false);
    button.setAttribute("tabindex", "-1");
    for (const attribute of ["aria-controls", "aria-expanded", "data-i18n-title", "title"]) button.removeAttribute(attribute);
  }
  // nothing chosen yet: take the first connected device
  if (DEV.switching && DEV.discovery === "complete" && !DEV.devices.some((d) => d.udid === DEV.current)) {
    const first = DEV.devices.find((d) => d.connected);
    if (first) return selectDevice(first.udid, true);
  }
  renderDeviceMenu();
  if (!V || !V.loaded) render();
}

function renderDeviceMenu() {
  const m = $("#device-menu");
  const focused = m.contains(document.activeElement) ? document.activeElement.closest("button") : null;
  m.replaceChildren();
  if (DEV.discovery === "unavailable") {
    const note = localizedEl("div", "menu-note", () => t("editor.discoveryUnavailable"));
    note.setAttribute("role", "status");
    m.appendChild(note);
  } else if (!DEV.devices.length) {
    m.appendChild(localizedEl("div", "menu-note", () => t("editor.noDevices")));
  }
  for (const d of DEV.devices) {
    const b = el("button", "device-row" + (d.udid === DEV.current ? " current" : ""));
    b.dataset.deviceId = d.udid;
    const dot = el("i", d.connected === true ? "on" : d.connected === false ? "off" : "unknown");
    const text = el("span", "t");
    text.append(el("b", null, d.name || d.udid),
      el("small", null, [d.version && ((d.model || "").startsWith("iPad") ? "iPadOS " : "iOS ") + d.version,
        d.connected === true ? d.connection : t(d.connected === false ? "editor.notConnected" : "editor.connectionUnknown"),
        d.draft && t("editor.draftSaved")].filter(Boolean).join(" · ")));
    b.append(dot, text);
    b.onclick = () => { setPopup("device", false, true); if (d.udid !== DEV.current) selectDevice(d.udid); };
    m.appendChild(b);
  }
  const r = localizedEl("button", "menu-foot", () => t("editor.refreshDevices"));
  r.onclick = (e) => { e.stopPropagation(); return loadDevices(); };
  m.appendChild(r);
  if (!m.hidden && focused) {
    const row = [...m.querySelectorAll(".device-row")].find(node => node.dataset.deviceId === focused.dataset.deviceId);
    (row || r).focus();
  }
  positionPopup("device");
}

async function selectDevice(udid, quiet) {
  if (!quiet && V && V.loaded && V.diff.has_changes) {
    toast(() => t("editor.draftKept"), "ok");
  }
  try {
    const view = await api("/api/device", { udid });
    DEV.current = udid;
    clearEditor();
    setView(view);
    renderDeviceMenu();
  } catch (e) { showError(e); }
}

// ---- top-level actions ----------------------------------------------------------------

async function readDevice() {
  const binding = V?.binding;
  const savedDraft = !V?.loaded && V?.draft_error;
  if (savedDraft || (V?.loaded && (V.diff.has_changes || V.history.undo))) {
    if (!(await confirmBox(() => t("editor.readAgainTitle"), () => savedDraft
      ? t("editor.replaceSavedDraft") : t("editor.readAgainBody", {device: D()}), () => t("editor.read"), true))) return;
  }
  try {
    const v = await withBusy(() => t("editor.reading", {device: V && V.loaded ? D() : t("editor.device")}), () => api("/api/read", {}, binding));
    const sameDevice = V?.loaded && V.device_id === v.device_id;
    clearEditor();
    setView(v);
    if (sameDevice) {
      for (const row of libraryRows.values()) {
        const item = row.libraryItem, tile = row.firstChild;
        if (item.icon && !tile.querySelector("img")) tile.replaceWith(iconImg(item));
      }
    }
    toast(() => t("editor.readSuccess"), "ok");
  } catch (e) {
    showError(e);
  }
}

async function review() {
  if (!V?.loaded) return;
  if (V.read_only) { toast(() => t("editor.readOnly"), "error"); return; }
  const d = V.diff;
  const binding = V.binding;
  if (!d.has_changes || ScreenOrganizerOnboarding.dialog) return;
  const token = d.apply_token;
  if (typeof token !== "string" || !token) return;
  const body = el("div");
  const stats = el("div", "stats");
  const stat = (value, key) => {
    const x = el("div");
    x.append(localizedEl("b", null, value), localizedEl("span", null, () => t(key)));
    stats.appendChild(x);
  };
  stat(() => number(d.items_moved), "editor.appsMovedLabel");
  stat(() => number(d.to_app_library), "editor.toLibrary");
  stat(() => `${number(d.pages_before)}→${number(d.pages_after)}`, "editor.pagesLabel");
  body.appendChild(stats);
  if (d.problems.length) {
    const ul = el("ul", "problems");
    for (const [index, problem] of d.problems.entries()) {
      const detail = d.problem_details?.[index];
      const li = el("li");
      li.appendChild(localizedEl("span", null, () => t(detail?.key || "validation.unknown",
        Object.fromEntries(Object.entries(detail?.params || {}).map(([key, value]) => [key, typeof value === "number" ? number(value) : value])))));
      if (!detail || detail.diagnostic) li.appendChild(diagnostic(detail?.diagnostic || problem));
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
  const ul = el("ul", "moves");
  for (const [k, v] of Object.entries(V.moves)) {
    const li = el("li");
    const detail = V.move_details?.[k];
    li.append(el("span", null, detail ? detail.name : k),
      detail ? localizedEl("span", "to", () => locationText(detail.from) + " → " + locationText(detail.to)) : el("span", "to", v));
    ul.appendChild(li);
  }
  for (const change of d.placement_changes || []) {
    const li = el("li");
    const name = change.before_name !== undefined ? change.before_name + " → " + change.name : change.name;
    li.append(el("span", null, name), localizedEl("span", "to", () => {
      const endpoints = [change.from, change.to].filter(Boolean).map(locationText);
      return t("placement." + change.reason) + " · " + [...new Set(endpoints)].join(" → ");
    }));
    ul.appendChild(li);
  }
  if (!d.items_moved) body.appendChild(localizedEl("p", null, () => t("editor.layoutChanged")));
  body.appendChild(ul);
  if (V.gaps && !V.gaps.readable) {
    body.appendChild(localizedEl("p", null, () => t("editor.gapsWarning", {device: D()}) + " "
      + (V.gaps.count ? t("editor.placedItemsKept", {count: number(V.gaps.count)}) : t("editor.keepGapHint"))));
  }
  body.appendChild(localizedEl("p", null, () => t("editor.applyBackupHint", {device: D()})));
  const actions = [{ label: () => d.problems.length ? t("common.close") : t("common.cancel"), value: () => false }];
  if (!d.problems.length) actions.push({ label: () => t("editor.apply"), primary: true, value: () => true });
  if (!(await modal({ title: () => d.problems.length ? t("editor.fixFirst") : t("editor.applyTitle", {device: D()}), body, actions }))) return;
  if (!V?.loaded || V.read_only || V.diff.apply_token !== token || !V.diff.has_changes || V.diff.problems.length) return;
  try {
    const res = await withBusy(() => t("editor.writing", {device: D()}), () => api("/api/apply", { token }, binding));
    showWriteOutcome(res, () => t("editor.applied", {device: D()}));
  } catch (e) {
    showError(e);
  }
}

function showWriteOutcome({view, result}, success) {
  setView(view);
  if (result.ok) toast(success, "ok", result.error, result.backup, result);
  else showResult(result);
}

function showResult(r) {
  if (r.hidden_webclips?.length) return showHiddenClips(r);
  const body = el("div");
  const compared = r.mismatches && Object.keys(r.mismatches).length;
  const outcome = () => t(r.readback === "matched" && r.local_adoption === "failed" ? "editor.resultLocalSave"
    : compared ? "editor.resultMismatch" : "editor.resultUnknown", {device: D()});
  localized($("#operation-status"), outcome);
  body.appendChild(localizedEl("p", null, outcome));
  resultDetails(body, r);
  modal({ title: () => t("editor.checkResult"), body, actions: [{ label: () => t("common.ok"), primary: true, value: () => null }] });
}

function resultDetails(body, result) {
  body.appendChild(localizedEl("p", null, () => t("editor.verificationScope")));
  if (result.write_error) body.appendChild(localizedEl("p", null, () => t("editor.writeStage", {error: result.write_error})));
  if (result.readback_error) body.appendChild(localizedEl("p", null, () => t("editor.readbackStage", {error: result.readback_error})));
  if (result.recovery === "read") body.appendChild(localizedEl("p", null, () => t("editor.recoveryRead")));
  if (result.error) body.appendChild(diagnostic(result.error));
  if (result.mismatches && Object.keys(result.mismatches).length)
    body.appendChild(el("pre", "diagnostic", JSON.stringify(result.mismatches, null, 1)));
  if (result.backup) body.appendChild(localizedEl("p", null, () => t("editor.backupSaved", {backup: result.backup})));
}

function showHiddenClips(r) {
  const body = el("div");
  const message = () => t("editor.hiddenClipsBody", {device: D(), count: number(r.hidden_webclips.length)});
  localized($("#operation-status"), message);
  body.appendChild(localizedEl("p", null, message));
  const ul = el("ul", "moves");
  for (const n of r.hidden_webclips) ul.appendChild(el("li", null, n));
  body.appendChild(ul);
  body.appendChild(localizedEl("p", null, () => t("editor.hiddenClipsHint")));
  resultDetails(body, r);
  modal({ title: () => t("editor.hiddenClipsTitle"), body, actions: [{ label: () => t("common.close") }] });
}

function backupLabel(name) {
  const m = name.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-before-(apply|restore)-([a-f0-9]{32})\.plist$/);
  if (!m) return name;
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, 0);
  if (!year || date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month || date.getUTCDate() !== day ||
      hour > 23 || minute > 59 || second > 59) return name;
  const when = ScreenOrganizerI18n.formatDate(date, {timeZone: "UTC", year:"numeric", month:"numeric", day:"numeric"});
  const time = ScreenOrganizerI18n.formatDate(date, {timeZone: "UTC", hour:"2-digit", minute:"2-digit"});
  return when + " " + time + " · " + t(m[7] === "apply" ? "editor.beforeApply" : "editor.beforeRestore") + " · " + m[8];
}

async function showBackups() {
  const binding = V?.binding;
  const { backups, binding: listed } = await api("/api/backups");
  if (binding?.device !== listed?.device || binding?.revision !== listed?.revision) {
    setView(await api("/api/state"));
    throw Object.assign(new Error("Backup list device or draft revision changed."), {code: "stale_draft"});
  }
  const body = el("div");
  if (!backups.length) body.appendChild(localizedEl("p", null, () => t("editor.noBackups")));
  for (const [index, name] of backups.entries()) {
    const row = el("div", "backup-row");
    const label = localizedEl("span", null, () => backupLabel(name));
    label.id = `backup-label-${index}`;
    const btn = localizedEl("button", null, () => t("editor.restore"));
    btn.id = `backup-restore-${index}`;
    btn.setAttribute("aria-labelledby", btn.id + " " + label.id);
    btn.disabled = Boolean(V && V.read_only);
    btn.setAttribute("data-backup", name);
    row.append(label, btn);
    body.appendChild(row);
  }
  const name = await modal({ title: () => t("editor.backups"), body, actions: [{ label: () => t("common.close") }] });
  if (!name) return;
  const { review } = await api("/api/backup-review", { name }, binding);
  const confirmation = el("div");
  confirmation.append(localizedEl("p", null, () => backupLabel(name)),
    localizedEl("p", null, () => t("editor.restoreBody", {device: D()})));
  if (!(await confirmBox(() => t("editor.restoreTitle"), confirmation, () => t("editor.restore")))) return;
  try {
    const res = await withBusy(() => t("editor.restoring"), () => api("/api/restore", { name, review }, binding));
    showWriteOutcome(res, () => t("editor.restored"));
  } catch (e) { showError(e); }
}

async function organize(name) {
  const binding = V?.binding;
  setPopup("organize", false, true);
  const keep = $("#keep-first").checked;
  if (name === "group_categories") {
    const ok = await confirmBox(() => t("editor.groupTitle"), () =>
      t("editor.groupBody"), () =>
      t("editor.lookupGroup"));
    if (!ok) return;
    return withBusy(() => t("editor.lookingUp"), () => op(name, { keep_first_page: keep }, undefined, binding));
  }
  const label = { sort_color: "editor.sortingColor", optimize_usage: "editor.optimizingUsage" }[name];
  const args = ["sort_color", "sort_alpha", "optimize_usage"].includes(name) ? { keep_first_page: keep } : {};
  return label ? withBusy(() => t(label), () => op(name, args)) : op(name, args);
}

// ---- wiring ------------------------------------------------------------------------------

// the dock and library containers live for the whole session: wire them once
wireZone($("#dock"), { target: "dock" });
wireZone($("#library"), { target: "library" });
wireZone($("#bank"), { target: "bank" });
$("#read").onclick = readDevice;
$("#device-btn").onclick = (e) => {
  if (!DEV.switching && DEV.discovery !== "unavailable") return;
  e.stopPropagation();
  const m = $("#device-menu");
  setPopup("device", m.hidden);
  if (!m.hidden) loadDevices();
};
$("#help").onclick = () => {
  const tips = [
    ["editor.help.dragTitle", "editor.help.dragBody"],
    ["editor.help.folderTitle", "editor.help.folderBody"],
    ["editor.help.intoFolderTitle", "editor.help.intoFolderBody"],
    ["editor.help.swapTitle", "editor.help.swapBody"],
    ["editor.help.gapTitle", "editor.help.gapBody"],
    ["editor.help.selectTitle", "editor.help.selectBody"],
    ["editor.help.deleteTitle", "editor.help.deleteBody"],
    ["editor.bank", "editor.help.bankBody"],
    ["editor.undoShortcut", "editor.help.undoBody"],
    ["editor.search", "editor.help.searchBody"],
    ["editor.usageTitle", "editor.help.usageBody"],
    ["editor.reviewApply", "editor.help.reviewBody"],
  ];
  const body = el("div");
  const ul = el("ul", "moves");
  for (const [k, v] of tips) { const li = el("li"); li.append(localizedEl("b", null, () => t(k)), localizedEl("span", "to", () => t(v))); ul.appendChild(li); }
  body.appendChild(ul);
  body.appendChild(localizedEl("p", null, () => t("editor.helpApplyHint")));
  modal({ title: () => t("editor.helpTitle"), body, actions: [{ label: () => t("editor.gotIt"), primary: true, value: () => null }] });
};
$("#read-empty").onclick = readDevice;
$("#undo").onclick = () => op("undo");
$("#redo").onclick = () => op("redo");
$("#reset").onclick = async () => {
  const binding = V?.binding;
  if (await confirmBox(() => t("editor.resetTitle"), () => t("editor.resetBody", {device: D()}), () => t("editor.reset"), true)) op("reset", {}, undefined, binding);
};
$("#review").onclick = review;
$("#backups-btn").onclick = () => showBackups().catch((e) => showError(e));
function setPopup(name, open, restoreFocus = false) {
  const menu = $("#" + name + "-menu"), button = $("#" + name + "-btn");
  const focused = menu.contains(document.activeElement);
  menu.hidden = !open;
  if (!button.disabled || name === "organize") button.setAttribute("aria-expanded", String(open));
  if (open) {
    const other = name === "device" ? "organize" : "device";
    if (!$("#" + other + "-menu").hidden) setPopup(other, false);
    positionPopup(name);
  } else if (restoreFocus && focused) button.focus();
}
function positionPopup(name) {
  const menu = $("#" + name + "-menu");
  if (menu.hidden) return;
  const margin = 12, trigger = $("#" + name + "-btn").getBoundingClientRect();
  menu.style.left = `${margin}px`;
  menu.style.top = `${trigger.bottom + 6}px`;
  const {width, height} = menu.getBoundingClientRect();
  const left = name === "device" ? trigger.left : trigger.right - width;
  menu.style.left = `${Math.max(margin, Math.min(left, innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(trigger.bottom + 6, innerHeight - height - margin))}px`;
}
function positionPopups() { positionPopup("device"); positionPopup("organize"); }
$("#organize-btn").onclick = (e) => {
  e.stopPropagation();
  setPopup("organize", $("#organize-menu").hidden);
};
window.addEventListener("resize", () => { positionPopups(); syncDrawerCoverage(); });
document.querySelectorAll("#organize-menu [data-op]").forEach((b) => { b.onclick = () => organize(b.dataset.op); });
document.addEventListener("click", (e) => {
  if (!$("#organize-menu").contains(e.target)) setPopup("organize", false);
  if (!$("#device-menu").contains(e.target)) setPopup("device", false);
});
try { $("#keep-first").checked = localStorage.getItem("hs.keepFirst") !== "0"; } catch (_) {}
$("#keep-first").onchange = (e) => { try { localStorage.setItem("hs.keepFirst", e.target.checked ? "1" : "0"); } catch (_) {} };
$("#search").oninput = (e) => { query = e.target.value.trim().toLowerCase(); renderSearch(); };
$("#sel-folder").onclick = newFolderFromSelection;
$("#sel-place").onclick = () => {
  if (sel.size !== 1) return;
  const item = findItem([...sel][0]);
  if (!item || !["app", "webclip", "folder"].includes(item.kind)) return;
  const control = [...document.querySelectorAll("[data-focus-context]")].find(node => node.dataset.id === item.id);
  editPlacement(item, control?.dataset.focusContext || "bank");
};
$("#sel-library").onclick = selectionToLibrary;
$("#sel-unpin").onclick = () => op("unpin", { ids: [...sel] }, () => sel.clear());
$("#sel-clear").onclick = () => {
  if (![...clearSelection()].some(id => focusItem({id}, true))) focusEditorContext();
};
function closeFolder() {
  if (!openFolder) return;
  const opener = folderOpener;
  folderOpener = null;
  showFolder(null);
  focusItem(opener, true);
}
$("#drawer-close").onclick = closeFolder;
$("#folder-name").onchange = (e) => {
  const name = e.target.value.trim();
  if (openFolder && name) op("rename_folder", { id: openFolder, name });
};
$("#folder-name").onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) e.target.blur(); };
$("#folder-add-page").onclick = () => {
  const f = findItem(openFolder);
  if (!f) return;
  // an empty folder page appears once something is dropped on it; show a target now
  if (!f.pages.length || f.pages.at(-1).length) f.pages.push([]);
  renderDrawer();
};
$("#folder-dissolve").onclick = async () => {
  const f = findItem(openFolder);
  const binding = V?.binding;
  if (f && await confirmBox(() => t("editor.removeFolderTitle", {name: f.name}), () => t("editor.removeFolderBody"), () => t("editor.removeFolder"), true)) {
    op("dissolve_folder", { id: f.id }, () => { openFolder = null; folderOpener = null; }, binding);
  }
};
$("#board").addEventListener("click", (e) => {
  if (!e.target.closest(".item, .widget, button") && sel.size) clearSelection();
});
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.isComposing || !ScreenOrganizerOnboarding.accepted || ScreenOrganizerOnboarding.dialog) return;
  if (e.key === "Escape") {
    const popup = ["organize", "device"].find(name => !$("#" + name + "-menu").hidden);
    if (popup) {
      e.preventDefault();
      setPopup(popup, false);
      $("#" + popup + "-btn").focus();
      return;
    }
    if (openFolder && (e.target === $("#folder-name") || !e.target.matches("input,select,textarea"))) {
      e.preventDefault();
      if (e.target === $("#folder-name")) e.target.value = findItem(openFolder).name;
      clearSelection();
      closeFolder();
      return;
    }
  }
  if (e.target.isContentEditable || e.target.matches("input,select,textarea")) return;
  const mod = e.metaKey || e.ctrlKey;
  if (Boolean(e.metaKey) !== Boolean(e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "z") {
    e.preventDefault(); op(e.shiftKey ? "redo" : "undo");
  }
  else if (e.key === "Escape" && sel.size) clearSelection();
  else if ((e.key === "Backspace" || e.key === "Delete") && sel.size) { e.preventDefault(); selectionToLibrary(); }
  else if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); $("#search").focus(); }
});
window.addEventListener("beforeunload", (e) => {
  if (V && V.loaded && V.diff.has_changes) { e.preventDefault(); e.returnValue = ""; }
});

ScreenOrganizerI18n.onChange(() => {
  const folderDraft = $("#folder-name").value;
  render();
  if (openFolder) $("#folder-name").value = folderDraft;
  renderDeviceMenu();
  refreshText();
  positionPopups();
});
ScreenOrganizerOnboarding.ready.then(() => api("/api/state")).then(setView).catch((e) => { showError(e); setView({ loaded: false }); }).then(loadDevices);
