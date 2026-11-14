// Screen Organizer edits local drafts through /api/op and returns an undoable view.
// Apply and Restore write layouts to the device after creating a backup.
"use strict";

const t = (key, params) => globalThis.ScreenOrganizerI18n?.t(key, params) || key;
const number = value => ScreenOrganizerI18n.formatNumber(value);

// Read the launcher credential from the fragment and remove the fragment.
// Authenticate with /v1/authenticate; reloads reuse the HttpOnly session cookie.
const authentication = (async () => {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  if (!token) return;
  const response = await fetch("/v1/authenticate", {
    method: "POST", headers: { "X-Token": token }, credentials: "same-origin",
  });
  if (!response.ok) return {code: "invalid_credentials"};
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
  if (message === "the saved draft changed in another session; reopen it or read the device again") return "error.staleDraft";
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
  if (/^iOS 16 /.test(message)) return "error.ios16";
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
const sel = new Set();      // selected item ids
let openFolder = null;      // id of the folder shown in the drawer
let drag = null;            // { ids }
let query = "";

// ---- server ----------------------------------------------------------------------------

async function api(path, body) {
  await globalThis.ScreenOrganizerOnboarding?.ready;
  const authenticationFailure = await authentication;
  if (authenticationFailure) {
    await globalThis.ScreenOrganizerI18n?.ready;
    const error = new Error(t("editor.authenticationFailed"));
    error.code = authenticationFailure.code;
    throw error;
  }
  if (V && V.read_only && ["/api/apply", "/api/restore"].includes(path)) {
    throw Object.assign(new Error(t("editor.readOnly")), {code: "read_only"});
  }
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
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

async function op(name, args = {}, onSuccess) {
  try {
    const view = await api("/api/op", { op: name, args });
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

function iconImg(item, cls) {
  const wrap = el("div", cls || "tile");
  if (item.icon) {
    const img = new Image();
    img.loading = "lazy";
    img.alt = "";
    img.src = "/icon?id=" + encodeURIComponent(item.icon);
    img.onerror = () => { img.remove(); fallback(wrap, item.name); };
    wrap.appendChild(img);
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

const matches = (item) => !query || item.name.toLowerCase().includes(query) || item.id.toLowerCase().includes(query);

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

let pendingDiagnostic = null;
function toast(message, kind = "", detail, backup) {
  localized($("#operation-status"), message);
  const box = $("#toast");
  const previous = box.querySelector("details");
  if (!box.hidden && previous && (pendingDiagnostic || previous.open || box.contains(document.activeElement))) {
    if (detail) pendingDiagnostic = {message, kind, detail, backup};
    return;
  }
  displayToast({message, kind, detail, backup});
}

function displayToast({message, kind, detail, backup}) {
  const box = $("#toast");
  pendingDiagnostic = null;
  clearTimeout(toast.timer);
  box.replaceChildren(localizedEl("span", null, message));
  box.className = "toast " + kind;
  box.hidden = false;
  if (detail) {
    const close = localizedEl("button", null, () => t("common.close"));
    close.onclick = () => {
      if (pendingDiagnostic) {
        displayToast(pendingDiagnostic);
        box.querySelector("summary").focus();
      } else {
        box.hidden = true;
        $("#read").focus();
      }
    };
    box.appendChild(diagnostic(detail));
    if (backup) box.appendChild(localizedEl("p", null, () => t("editor.backupSaved", {backup})));
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
  if (label) wrap.appendChild(localizedEl("p", null, label));
  const inp = el("input");
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
  drag = null;
}

function setView(v) {
  if (!v.loaded || (V && V.device_id !== v.device_id)) clearEditor();
  V = v;
  for (const id of [...sel]) if (!findItem(id)) sel.delete(id);
  if (openFolder && !findItem(openFolder)) openFolder = null;
  render();
}

function render() {
  $("#read-only-notice").hidden = !(V && V.read_only);
  const loaded = V && V.loaded;
  $("#empty").hidden = loaded;
  $("#board").hidden = !loaded;
  $("#library-panel").hidden = !loaded;
  for (const id of ["undo", "redo", "reset", "organize-btn", "search"]) $("#" + id).disabled = !loaded;
  if (!loaded) {
    $("#device").textContent = V && V.source && V.source !== "phone" ? V.source : (deviceLabel() || t("editor.noDevice"));
    $("#read").textContent = t("editor.readDevice");
    $("#drawer").hidden = true;
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
  renderSelection();
  renderFooter();
}

function renderItem(item, container) {
  const moved = V.moved.includes(item.id);
  if (item.kind === "widget") {
    const w = el("div", `widget w-${item.size}`);
    w.dataset.id = item.id;
    const ic = iconImg(item, "wicon");
    w.append(ic, el("div", "wname", item.name), el("div", "wkind", `${item.stack ? t("editor.smartStack") : t("editor.widget")} · ${widgetSize(item.size)}`));
    w.title = t("editor.widgetTitle", {name: item.name, kind: t(item.stack ? "editor.smartStack" : "editor.widget"), size: widgetSize(item.size)});
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
  node.addEventListener("click", (e) => onItemClick(e, item));
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
    const tb = (label, title, fn, disabled) => {
      const b = el("button", null, label);
      b.title = title;
      b.setAttribute("aria-label", title);
      b.disabled = !!disabled;
      b.onclick = fn;
      tools.appendChild(b);
    };
    tb("◀", t("editor.movePageLeft"), () => op("move_page", { page: n, to: n - 1 }), n === 1);
    tb("▶", t("editor.movePageRight"), () => op("move_page", { page: n, to: n + 1 }), n === V.pages.length);
    tb("＋", t("editor.insertPage"), () => op("insert_page", { page: n + 1 }));
    tb("✕", t("editor.deletePageHint"), async () => {
      if (page.length && !(await confirmBox(() => t("editor.deletePageTitle", {number: number(n)}), () =>
        t("editor.deletePageBody"), () => t("editor.deletePage"), true))) return;
      op("delete_page", { page: n });
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
  const add = localizedEl("div", "add-page", () => t("editor.newPage"));
  add.title = t("editor.newPageHint");
  add.onclick = () => op("insert_page", { page: V.pages.length + 1 });
  wireZone(add, { target: `page:${V.pages.length + 1}`, newPage: true });
  box.appendChild(add);
}

function renderLibrary() {
  const list = $("#library");
  list.replaceChildren();
  if (V.library === null) {
    $("#lib-count").textContent = "";
    list.appendChild(localizedEl("div", "lib-empty", () => t("editor.libraryUnavailable")));
    return;
  }
  const items = V.library.filter(matches).sort((a, b) => a.name.localeCompare(b.name, ScreenOrganizerI18n.locale));
  $("#lib-count").textContent = query ? t("editor.matchCount", {count: number(items.length), total: number(V.library.length)}) : number(V.library.length);
  if (!items.length) list.appendChild(el("div", "lib-empty", query ? t("editor.noMatches") : t("editor.libraryEmpty")));
  for (const it of items) {
    const row = el("div", "lib-row");
    row.dataset.id = it.id;
    row.append(iconImg(it), el("span", "n", it.name));
    row.title = it.id;
    if (sel.has(it.id)) row.classList.add("selected");
    if (V.moved.includes(it.id)) row.classList.add("moved");
    row.addEventListener("click", (e) => onItemClick(e, it));
    wireDrag(row, it, { target: "library" });
    list.appendChild(row);
  }
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
  if (!folder) return;
  const name = $("#folder-name");
  if (document.activeElement !== name) name.value = folder.name;
  const box = $("#folder-pages");
  box.replaceChildren();
  folder.pages.forEach((page, i) => {
    const fp = el("div", "fpage");
    fp.appendChild(localizedEl("div", "num", () => t("editor.folderPage", {number: number(i + 1), count: number(page.length), capacity: number(G().folder)})));
    const g = el("div", "fgrid");
    g.style.setProperty("--fcols", Math.round(Math.sqrt(G().folder)));
    const ctx = { target: `${folder.id}:${i + 1}` };
    for (const it of page) g.appendChild(renderItem(it, ctx));
    fp.appendChild(g);
    wireZone(fp, ctx);
    box.appendChild(fp);
  });
}

function renderSelection() {
  const bar = $("#selection-bar");
  bar.hidden = sel.size === 0;
  $("#sel-count").textContent = t("editor.selected", {count: number(sel.size)});
  $("#sel-library").disabled = V.library === null;
  $("#sel-unpin").hidden = ![...sel].some((i) => (findItem(i) || {}).pinned);
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

function onItemClick(e, item) {
  e.stopPropagation();
  if (e.metaKey || e.ctrlKey || e.shiftKey) {
    if (item.kind === "widget") return;
    sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
    render();
    return;
  }
  if (item.kind === "folder") { openFolder = item.id; sel.clear(); render(); return; }
  if (sel.size && !sel.has(item.id)) sel.clear();
  sel.has(item.id) ? sel.delete(item.id) : sel.add(item.id);
  render();
}

function clearMarks() {
  document.querySelectorAll(".ins-before,.ins-after,.ins-onto,.drop-zone,.drop-cell").forEach((n) =>
    n.classList.remove("ins-before", "ins-after", "ins-onto", "drop-zone", "drop-cell"));
}

function zoneOf(e, node, item) {
  const r = node.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  if (item.kind !== "widget" && x > 0.28 && x < 0.72 && !drag.ids.includes(item.id)) return "onto";
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
    node.classList.add("ins-" + zoneOf(e, node, item));
  });
  node.addEventListener("drop", (e) => {
    if (!drag) return;
    e.preventDefault();
    e.stopPropagation();
    const zone = zoneOf(e, node, item);
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
    if (!drag) return;
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
  if (target === "library") {
    const bad = ids.map(findItem).filter((it) => it && it.kind !== "app" && it.kind !== "webclip");
    if (bad.length) return toast(() => t("editor.onlyAppsInLibrary"), "error");
  }
  if (ids.length === 1) return op("move", { id: ids[0], target, before }, () => sel.clear());
  return op("move_many", { ids, target, before }, () => sel.clear());
}

async function dropOnto(item, ids, ctx) {
  if (item.kind === "folder") {
    if (await op("move_many", { ids, target: item.id }, () => sel.clear())) toast(() => t("editor.movedInto", {name: item.name}));
    return;
  }
  const name = await promptText(() => t("editor.newFolder"), () => t("editor.folderWith", {name: item.name, count: number(ids.length)}), t("editor.folderDefault"));
  if (!name) return;
  const page = ctx.target;
  const siblings = locateIndex(item.id, page);
  await op("create_folder", { name, ids: [item.id, ...ids], target: page, index: siblings }, () => sel.clear());
}

function locateIndex(id, target) {
  const list = target === "dock" ? V.dock : target.startsWith("page:") ? V.pages[+target.slice(5) - 1] : [];
  const i = (list || []).findIndex((x) => x.id === id);
  return i < 0 ? null : i;
}

async function newFolderFromSelection() {
  const ids = [...sel].filter((i) => { const it = findItem(i); return it && it.kind !== "folder" && it.kind !== "widget"; });
  if (!ids.length) return;
  const name = await promptText(() => t("editor.newFolder"), () => t("editor.folderFromSelection", {count: number(ids.length)}), t("editor.folderDefault"));
  if (!name) return;
  // put it where the first selected app on a page is, else at the end of the last page
  let target = `page:${V.pages.length || 1}`, index = null;
  for (let p = 0; p < V.pages.length; p++) {
    const i = V.pages[p].findIndex((x) => ids.includes(x.id));
    if (i >= 0) { target = `page:${p + 1}`; index = i; break; }
  }
  await op("create_folder", { name, ids, target, index }, () => sel.clear());
}

async function selectionToLibrary() {
  const ids = [...sel].filter((i) => { const it = findItem(i); return it && (it.kind === "app" || it.kind === "webclip"); });
  if (ids.length) await moveIds(ids, "library", null);
}

// ---- devices ------------------------------------------------------------------------------

let DEV = { switching: false, current: null, devices: [] };

const deviceLabel = () => {
  const d = DEV.devices.find((x) => x.udid === DEV.current);
  return d ? d.name || d.udid : "";
};

async function loadDevices() {
  try { DEV = await api("/api/devices"); } catch (_) { return; }
  $("#device-btn").classList.toggle("static", !DEV.switching);
  // nothing chosen yet: take the first connected device
  if (DEV.switching && !DEV.devices.some((d) => d.udid === DEV.current)) {
    const first = DEV.devices.find((d) => d.connected);
    if (first) return selectDevice(first.udid, true);
  }
  renderDeviceMenu();
  if (!V || !V.loaded) render();
}

function renderDeviceMenu() {
  const m = $("#device-menu");
  m.replaceChildren();
  if (!DEV.devices.length) {
    m.appendChild(localizedEl("div", "menu-note", () => t("editor.noDevices")));
  }
  for (const d of DEV.devices) {
    const b = el("button", "device-row" + (d.udid === DEV.current ? " current" : ""));
    const dot = el("i", d.connected ? "on" : "off");
    const text = el("span", "t");
    text.append(el("b", null, d.name || d.udid),
      el("small", null, [d.version && ((d.model || "").startsWith("iPad") ? "iPadOS " : "iOS ") + d.version,
        d.connected ? d.connection : t("editor.notConnected"), d.draft && t("editor.draftSaved")].filter(Boolean).join(" · ")));
    b.append(dot, text);
    b.onclick = () => { m.hidden = true; if (d.udid !== DEV.current) selectDevice(d.udid); };
    m.appendChild(b);
  }
  const r = localizedEl("button", "menu-foot", () => t("editor.refreshDevices"));
  r.onclick = (e) => { e.stopPropagation(); loadDevices(); };
  m.appendChild(r);
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
  if (V && V.loaded && (V.diff.has_changes || V.history.undo)) {
    if (!(await confirmBox(() => t("editor.readAgainTitle"), () => t("editor.readAgainBody", {device: D()}), () => t("editor.read"), true))) return;
  }
  try {
    const v = await withBusy(() => t("editor.reading", {device: V && V.loaded ? D() : t("editor.device")}), () => api("/api/read", {}));
    clearEditor();
    setView(v);
    toast(() => t("editor.readSuccess"), "ok");
  } catch (e) {
    showError(e);
  }
}

async function review() {
  if (!V?.loaded) return;
  if (V.read_only) { toast(() => t("editor.readOnly"), "error"); return; }
  const d = V.diff;
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
    const res = await withBusy(() => t("editor.writing", {device: D()}), () => api("/api/apply", { token }));
    showWriteOutcome(res, () => t("editor.applied", {device: D()}));
  } catch (e) {
    showError(e);
  }
}

function showWriteOutcome({view, result}, success) {
  setView(view);
  if (result.ok) toast(success, "ok", result.error, result.backup);
  else showResult(result);
}

function showResult(r) {
  if (r.hidden_webclips?.length) return showHiddenClips(r);
  const body = el("div");
  const compared = r.mismatches && Object.keys(r.mismatches).length;
  const outcome = () => compared ? t("editor.resultMismatch", {device: D()}) : t("editor.resultUnknown");
  localized($("#operation-status"), outcome);
  body.appendChild(localizedEl("p", null, outcome));
  resultDetails(body, r);
  modal({ title: () => t("editor.checkResult"), body, actions: [{ label: () => t("common.ok"), primary: true, value: () => null }] });
}

function resultDetails(body, result) {
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
  const { backups } = await api("/api/backups");
  const body = el("div");
  if (!backups.length) body.appendChild(localizedEl("p", null, () => t("editor.noBackups")));
  for (const name of backups) {
    const row = el("div", "backup-row");
    const btn = localizedEl("button", null, () => t("editor.restore"));
    btn.disabled = Boolean(V && V.read_only);
    btn.setAttribute("data-backup", name);
    row.append(localizedEl("span", null, () => backupLabel(name)), btn);
    body.appendChild(row);
  }
  const name = await modal({ title: () => t("editor.backups"), body, actions: [{ label: () => t("common.close") }] });
  if (!name || !(await confirmBox(() => t("editor.restoreTitle"), () => t("editor.restoreBody", {device: D()}), () => t("editor.restore")))) return;
  try {
    const res = await withBusy(() => t("editor.restoring"), () => api("/api/restore", { name }));
    showWriteOutcome(res, () => t("editor.restored"));
  } catch (e) { showError(e); }
}

async function organize(name) {
  $("#organize-menu").hidden = true;
  const keep = $("#keep-first").checked;
  if (name === "group_categories") {
    const ok = await confirmBox(() => t("editor.groupTitle"), () =>
      t("editor.groupBody"), () =>
      t("editor.lookupGroup"));
    if (!ok) return;
    return withBusy(() => t("editor.lookingUp"), () => op(name, { keep_first_page: keep }));
  }
  const label = { sort_color: "editor.sortingColor", optimize_usage: "editor.optimizingUsage" }[name];
  const args = ["sort_color", "sort_alpha"].includes(name) ? { keep_first_page: keep }
    : name === "optimize_usage" ? { keep_first_page: false } : {};
  return label ? withBusy(() => t(label), () => op(name, args)) : op(name, args);
}

// ---- wiring ------------------------------------------------------------------------------

// the dock and library containers live for the whole session: wire them once
wireZone($("#dock"), { target: "dock" });
wireZone($("#library"), { target: "library" });
wireZone($("#bank"), { target: "bank" });
$("#read").onclick = readDevice;
$("#device-btn").onclick = (e) => {
  if (!DEV.switching) return;
  e.stopPropagation();
  const m = $("#device-menu");
  m.hidden = !m.hidden;
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
  if (await confirmBox(() => t("editor.resetTitle"), () => t("editor.resetBody", {device: D()}), () => t("editor.reset"), true)) op("reset");
};
$("#review").onclick = review;
$("#backups-btn").onclick = () => showBackups().catch((e) => showError(e));
$("#organize-btn").onclick = (e) => { e.stopPropagation(); $("#organize-menu").hidden = !$("#organize-menu").hidden; };
document.querySelectorAll("#organize-menu [data-op]").forEach((b) => { b.onclick = () => organize(b.dataset.op); });
document.addEventListener("click", (e) => {
  if (!$("#organize-menu").contains(e.target)) $("#organize-menu").hidden = true;
  if (!$("#device-menu").contains(e.target)) $("#device-menu").hidden = true;
});
try { $("#keep-first").checked = localStorage.getItem("hs.keepFirst") !== "0"; } catch (_) {}
$("#keep-first").onchange = (e) => { try { localStorage.setItem("hs.keepFirst", e.target.checked ? "1" : "0"); } catch (_) {} };
$("#search").oninput = (e) => { query = e.target.value.trim().toLowerCase(); render(); };
$("#sel-folder").onclick = newFolderFromSelection;
$("#sel-library").onclick = selectionToLibrary;
$("#sel-unpin").onclick = () => op("unpin", { ids: [...sel] }, () => sel.clear());
$("#sel-clear").onclick = () => { sel.clear(); render(); };
$("#drawer-close").onclick = () => { openFolder = null; render(); };
$("#folder-name").onchange = (e) => {
  const name = e.target.value.trim();
  if (openFolder && name) op("rename_folder", { id: openFolder, name });
};
$("#folder-name").onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) e.target.blur(); };
$("#folder-add-page").onclick = () => {
  const f = findItem(openFolder);
  if (!f) return;
  // an empty folder page appears once something is dropped on it; show a target now
  f.pages.push([]);
  renderDrawer();
};
$("#folder-dissolve").onclick = async () => {
  const f = findItem(openFolder);
  if (f && await confirmBox(() => t("editor.removeFolderTitle", {name: f.name}), () => t("editor.removeFolderBody"), () => t("editor.removeFolder"), true)) {
    op("dissolve_folder", { id: f.id }, () => { openFolder = null; });
  }
};
$("#board").addEventListener("click", (e) => {
  if (!e.target.closest(".item, .widget, button") && sel.size) { sel.clear(); render(); }
});
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || !ScreenOrganizerOnboarding.accepted || ScreenOrganizerOnboarding.dialog || e.target.matches("input,select,textarea")) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); op(e.shiftKey ? "redo" : "undo"); }
  else if (e.key === "Escape") { sel.clear(); openFolder = null; render(); }
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
});
ScreenOrganizerOnboarding.ready.then(() => api("/api/state")).then(setView).catch((e) => { showError(e); setView({ loaded: false }); }).then(loadDevices);
