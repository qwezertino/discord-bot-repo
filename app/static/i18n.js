/* Translation is explicit: user message content, names and IDs are never scanned. */
globalThis.PanelI18n = (() => {
  const catalog = JSON.parse(document.getElementById("i18n-catalog").textContent);
  const supported = new Set(["uk", "en"]);
  const storageKey = "discord-panel-language";
  let language = "uk";
  try {
    const saved = localStorage.getItem(storageKey);
    if (supported.has(saved)) language = saved;
  } catch { /* The UI still works when browser storage is unavailable. */ }
  const rendered = new Map();

  function t(key, values = {}) {
    const template = catalog.messages[key]?.[language] || key;
    const result = template.replace(/\{(\w+)\}/g, (match, name) => String(values[name] ?? match));
    // Only short-lived UI feedback needs to be repainted after a language change.
    if (rendered.size > 1000) rendered.clear();
    rendered.set(result, {key, values});
    return result;
  }

  function translateMessage(message) {
    if (typeof message !== "string") return t("Request failed");
    const previous = rendered.get(message);
    if (previous) {
      const values = {...previous.values};
      if (values.skipped) values.skipped = translateMessage(values.skipped);
      if (values.detail) {
        const prefix = values.detail.startsWith(": ") ? ": " : "";
        values.detail = prefix + translateMessage(values.detail.slice(prefix.length));
      }
      return t(previous.key, values);
    }
    const source = catalog.sources[message];
    if (source) return t(source);
    for (const [key, translations] of Object.entries(catalog.messages)) {
      for (const text of Object.values(translations)) {
        if (message === text) return t(key);
        if (!text.includes("{")) continue;
        const names = [];
        const pattern = text.split(/(\{\w+\})/g).map(part => {
          if (/^\{\w+\}$/.test(part)) {
            names.push(part.slice(1, -1));
            return "([\\s\\S]*?)";
          }
          return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        }).join("");
        const match = message.match(new RegExp(`^${pattern}$`));
        if (match) {
          const values = Object.fromEntries(names.map((name, i) => [name, match[i + 1]]));
          if (values.skipped) values.skipped = translateMessage(values.skipped);
          if (values.detail) {
            const prefix = values.detail.startsWith(": ") ? ": " : "";
            values.detail = prefix + translateMessage(values.detail.slice(prefix.length));
          }
          return t(key, values);
        }
      }
    }
    return message;
  }

  function apply(root = document) {
    for (const attribute of ["text", "placeholder", "title", "aria-label"]) {
      const data = attribute === "text" ? "data-i18n" : `data-i18n-${attribute}`;
      root.querySelectorAll(`[${data}]`).forEach(element => {
        const text = t(element.getAttribute(data));
        if (attribute === "text") element.textContent = text;
        else element.setAttribute(attribute, text);
      });
    }
    document.documentElement.lang = language;
    const select = document.getElementById("language-select");
    if (select) select.value = language;
  }

  function setLanguage(value) {
    if (!supported.has(value) || value === language) return;
    language = value;
    try { localStorage.setItem(storageKey, language); } catch { /* Optional storage. */ }
    apply();
    document.dispatchEvent(new Event("panel-language-change"));
  }

  return {
    t, apply, setLanguage, translateMessage,
    language: () => language,
    locale: () => language === "uk" ? "uk-UA" : "en-GB",
    keyFor: text => rendered.get(text)?.key,
  };
})();
