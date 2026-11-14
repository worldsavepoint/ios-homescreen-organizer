/* Manifest-driven language selection, translation and formatting. */
(() => {
  "use strict";
  const storageKey = "screenOrganizer.language";
  const base = typeof document !== "undefined" && document.currentScript?.src
    ? new URL("locales/", document.currentScript.src).href : "/static/locales/";
  let manifest, english = {}, catalog = {}, locale = "en", preference = "system", revision = 0;
  const listeners = new Set(), transitions = new Set(), catalogs = new Map();
  let requested, committedTarget, pending = false, initialized = false, resolveReady, rejectReady;
  let numberFormatter, pickerIntent, nativeSystemLocale;
  const host = globalThis.screenOrganizerHost;
  const legacyHost = host?.native && host.languageSync !== "native-origin";
  let awaitingHostEcho = [];
  const dateFormatters = new Map();
  try { preference = localStorage.getItem(storageKey) || "system"; } catch (_) { /* storage unavailable */ }

  function canonical(value) {
    try { return Intl.getCanonicalLocales(String(value).replace(/_/g, "-"))[0] || ""; }
    catch (_) { return ""; }
  }

  function match(value) {
    const code = canonical(value);
    if (!code) return null;
    const exact = manifest.languages.find(item => canonical(item.code) === code);
    if (exact) return exact.code;
    const parts = code.split("-");
    const candidates = [];
    for (let length = parts.length; length > 0; length--) candidates.push(parts.slice(0, length).join("-"));
    for (const candidate of candidates) {
      const language = manifest.languages.find(item => [item.code, ...item.aliases].some(tag => canonical(tag) === candidate));
      if (language) return language.code;
    }
    return null;
  }

  function resolveLocale(request, preferredLanguages = globalThis.navigator?.languages || []) {
    if (request !== "system") return match(request) || manifest.default;
    for (const language of preferredLanguages) {
      const resolved = match(language);
      if (resolved) return resolved;
    }
    return manifest.default;
  }

  async function loadJSON(file) {
    const response = await fetch(base + file, { credentials: "same-origin" });
    if (!response.ok) throw new Error("Could not load translations: " + file);
    return response.json();
  }

  function loadCatalog(code) {
    if (!catalogs.has(code)) {
      const promise = loadJSON(code + ".json").catch(error => {
        if (catalogs.get(code) === promise) catalogs.delete(code);
        throw error;
      });
      catalogs.set(code, promise);
    }
    return catalogs.get(code);
  }

  function t(key, parameters = {}) {
    const text = Object.hasOwn(catalog, key) ? catalog[key] : english[key];
    if (typeof text !== "string") return key;
    return text.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (token, name) =>
      Object.hasOwn(parameters, name) ? String(parameters[name]) : token);
  }

  function translateDOM(root = document) {
    const attributes = {"data-i18n": null, "data-i18n-title": "title",
      "data-i18n-placeholder": "placeholder", "data-i18n-aria-label": "aria-label"};
    const selector = Object.keys(attributes).map(name => "[" + name + "]").join(",");
    const nodes = [...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)];
    for (const node of nodes) for (const [source, target] of Object.entries(attributes)) {
      const key = node.getAttribute(source);
      if (key === null) continue;
      if (target) node.setAttribute(target, t(key));
      else node.textContent = t(key);
    }
  }

  function notify(phase, source, revision) {
    for (const listener of transitions) listener({phase, source, revision});
  }

  async function select(request, source, current, resolvedLocale) {
    if (!initialized) await resources;
    if (current !== revision) return locale;
    requested = request === "system" ? request : match(request) || "system";
    if (host?.native && requested === "system" && ["native", "bootstrap"].includes(source)) {
      nativeSystemLocale = match(resolvedLocale) || nativeSystemLocale;
    }
    const target = host?.native && requested === "system" && nativeSystemLocale ? nativeSystemLocale : resolveLocale(requested);
    if (!pending && preference === requested && committedTarget === target && locale === target) return locale;
    const nextPreference = requested;
    if (source === "picker") pickerIntent = nextPreference;
    pending = true;
    notify("start", source, current);
    let nextCatalog, effective = target;
    try { nextCatalog = await loadCatalog(target); }
    catch (error) {
      if (current !== revision) return locale;
      if (target === manifest.default) throw error;
      nextCatalog = english;
      effective = manifest.default;
    }
    if (current !== revision) return locale;
    const changed = catalog !== nextCatalog || locale !== effective;
    if (locale !== effective) { numberFormatter = undefined; dateFormatters.clear(); }
    preference = nextPreference;
    committedTarget = target;
    locale = effective;
    catalog = nextCatalog;
    pending = false;
    try { localStorage.setItem(storageKey, preference); } catch (_) { /* storage unavailable */ }
    if (changed) {
      if (typeof document !== "undefined") {
        document.documentElement.lang = locale;
        translateDOM(document);
      }
      for (const listener of listeners) listener(locale);
    }
    const fromPicker = pickerIntent === nextPreference;
    pickerIntent = null;
    if (fromPicker && legacyHost) {
      awaitingHostEcho = awaitingHostEcho.filter(value => value !== nextPreference);
      awaitingHostEcho.push(nextPreference);
    }
    notify("settle", fromPicker ? "picker" : source, current);
    resolveReady();
    return locale;
  }

  function setLocale(request, source = "host", resolvedLocale) {
    if (source === "native") awaitingHostEcho = [];
    if (source === "host" && legacyHost && initialized) {
      const owner = request === "system" ? request : match(request) || "system";
      const acknowledged = awaitingHostEcho.indexOf(owner);
      if (acknowledged !== -1) {
        // Native can coalesce rapid updates and confirm only the most recent one.
        awaitingHostEcho.splice(0, acknowledged + 1);
        // Native echoes a committed picker choice. A newer user choice may already be loading.
        if (owner !== requested || (pending && pickerIntent === owner)) return Promise.resolve(locale);
      } else {
        awaitingHostEcho = [];
      }
    }
    if (source !== "system") pickerIntent = null;
    requested = request;
    return select(request, source, ++revision, resolvedLocale);
  }

  const api = {
    ready: new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; }),
    get pending() { return pending; },
    get locale() { return locale; },
    get preference() { return preference; },
    get languages() { return manifest ? manifest.languages : []; },
    t, resolveLocale, translateDOM, setLocale,
    formatNumber(value) { return (numberFormatter ||= new Intl.NumberFormat(locale)).format(value); },
    formatDate(value, options) {
      const key = JSON.stringify(options || {});
      if (!dateFormatters.has(key)) dateFormatters.set(key, new Intl.DateTimeFormat(locale, options));
      return dateFormatters.get(key).format(value);
    },
    onChange(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    onTransition(listener) { transitions.add(listener); return () => transitions.delete(listener); },
  };
  globalThis.ScreenOrganizerI18n = api;
  const resources = (async () => {
    manifest = await loadJSON("manifest.json");
    english = await loadCatalog(manifest.default);
    initialized = true;
  })();
  setLocale(host?.native && typeof host.languagePreference === "string" ? host.languagePreference : preference,
    "bootstrap", host?.languageLocale).catch(rejectReady);
  globalThis.addEventListener?.("languagechange", () => {
    if (requested === "system") setLocale("system", "system");
  });
})();
