const t = (key, values) => PanelI18n.t(key, values);
const formatDate = value => new Date(value).toLocaleString(PanelI18n.locale());

const AUDIT_ACTIONS = {
  upload_created: "Upload created", message_created: "Message created",
  message_updated: "Message updated", message_deleted: "Message deleted",
  messages_imported: "Messages imported", profile_saved: "Profile saved",
  reaction_role_created: "Reaction rule created", reaction_role_deleted: "Reaction rule deleted",
  reaction_role_added: "Role granted", reaction_role_removed: "Role removed",
  access_changed: "Access changed", user_blocked: "User blocked", user_unblocked: "User unblocked",
};
const AUDIT_FIELDS = {
  id: "Internal ID", record_id: "Record ID", upload_id: "Upload ID", name: "Name",
  size: "File size", guild_id: "Server", channel_id: "Channel", message_id: "Message ID",
  role_id: "Role", user_id: "User ID", role: "Access", channels: "Channels", scanned: "Scanned",
  imported: "Imported", created: "Created", updated: "Updated", skipped_channels: "Skipped channels",
};

function formatAuditDetails(details) {
  const display = (key, value) => key === "role" ? t(value === "operator" ? "Operator" : value === "viewer" ? "Viewer" : "No access") :
    Array.isArray(value) ? value.join(", ") : value && typeof value === "object" ? JSON.stringify(value) : String(value);
  return Object.entries(details).map(([key, value]) =>
    `${t(AUDIT_FIELDS[key] || key)}: ${display(key, value)}`
  ).join(" · ");
}

function botDisplayName(bot) {
  if (!bot) return "";
  return bot.id === "default" && bot.name === "Основной бот" ? t("Primary bot") : bot.name;
}

function apiError(body, status) {
  const detail = body?.detail;
  if (typeof detail === "string") return PanelI18n.translateMessage(detail);
  if (Array.isArray(detail)) return detail.map(error => {
    const field = error.loc?.filter(part => part !== "body").join(".");
    const message = PanelI18n.translateMessage(error.msg || t("Invalid input"));
    return field ? `${field}: ${message}` : message;
  }).join("; ");
  return `${t("Request failed")} (HTTP ${status})`;
}

function localizedOption(text) {
  const option = new Option(text, "");
  const key = PanelI18n.keyFor(text);
  if (key) option.dataset.i18n = key;
  return option;
}

const state = {
  botId: null,
  generation: 0,
  bots: [],
  guilds: [],
  messages: [],
  uploads: [],
  preservedAttachments: [],
  editingId: null,
  embedTimestamp: null,
  composeChannels: [],
  composeRoles: [],
  emojiCatalog: null,
  reactionRoles: [],
  auditEntries: [],
  users: [],
  auth: {mode: "local", authenticated: true, owner: true, csrf_token: null},
  mentionAutocomplete: {
    textarea: null,
    start: 0,
    query: "",
    items: [],
    active: 0,
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

class StaleRequest extends Error {}

function scopedUrl(url) {
  if (url === "/api/bots" || url.startsWith("/api/bots/") || url.startsWith("/api/auth/") || url.startsWith("/api/admin/") || url === "/api/emojis") return url;
  if (!state.botId) throw new Error(t("Select a bot profile"));
  return url.replace("/api/", `/api/bots/${state.botId}/`);
}

async function api(url, options = {}) {
  const generation = state.generation;
  try {
    const response = await fetch(scopedUrl(url), {
      ...options,
      headers: { "Content-Type": "application/json", "Accept-Language": PanelI18n.language(), ...(state.auth.csrf_token ? {"X-CSRF-Token": state.auth.csrf_token} : {}), ...(options.headers || {}) },
    });
    const body = response.headers.get("content-type")?.includes("application/json")
      ? await response.json()
      : null;
    if (response.status === 401 && state.auth.mode === "discord") {
      window.location.assign("/login");
      throw new StaleRequest();
    }
    if (!response.ok) throw new Error(apiError(body, response.status));
    if (generation !== state.generation) throw new StaleRequest();
    return body;
  } catch (error) {
    if (generation !== state.generation) throw new StaleRequest();
    if (error instanceof TypeError) throw new Error(t("Network error. Check your connection to the panel."));
    throw error;
  }
}

function showToast(message, error = false) {
  if (!message) return;
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3500);
}

function setStatus(selector, message, error = false) {
  const element = $(selector);
  element.textContent = message;
  element.style.color = error ? "var(--danger)" : "var(--muted)";
}

function switchTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "messages") loadMessages();
  if (name === "roles") loadReactionRoles();
  if (name === "audit") loadAudit();
  if (name === "users" && state.auth.owner) loadUsers();
}

function fillSelect(select, items, placeholder, label) {
  select.replaceChildren(localizedOption(placeholder));
  items.forEach((item) => select.add(new Option(label(item), item.id)));
  select.disabled = false;
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    const element = $("#connection");
    element.classList.toggle("online", status.connected);
    element.classList.toggle("error", Boolean(status.last_error));
    element.querySelector("strong").textContent = status.connected
      ? `${status.user} · ${status.guild_count}`
      : (status.last_error && PanelI18n.translateMessage(status.last_error)) || ({disabled: t("Bot disabled"), missing_token: t("Set the token in .env and restart the panel"), connecting: t("Connecting…")}[status.state] || t("Not connected"));
    if (status.connected && state.guilds.length === 0) await loadGuilds();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    $("#connection").classList.add("error");
    $("#connection strong").textContent = error.message;
  }
}

async function loadGuilds() {
  try {
    state.guilds = await api("/api/guilds");
    [$("#guild"), $("#rr-guild"), $("#import-guild")].forEach((select) => {
      const current = select.value;
      fillSelect(select, state.guilds, t("Select a server"), (guild) => guild.name);
      if (state.guilds.some((guild) => String(guild.id) === current)) select.value = current;
    });
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

async function loadChannels(guildId, select) {
  if (!guildId) {
    select.replaceChildren(localizedOption(t("Select a server")));
    select.disabled = true;
    return;
  }
  select.disabled = true;
  select.replaceChildren(localizedOption(t("Loading…")));
  try {
    const channels = await api(`/api/guilds/${guildId}/channels`);
    fillSelect(select, channels, t("Select a channel"), (channel) =>
      channel.category ? `${channel.category} / #${channel.name}` : `#${channel.name}`
    );
    return channels;
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
    return [];
  }
}

async function loadRoles(guildId) {
  const select = $("#rr-role");
  select.disabled = true;
  select.replaceChildren(localizedOption(t("Loading…")));
  try {
    const roles = await api(`/api/guilds/${guildId}/roles`);
    fillSelect(select, roles.filter((role) => !role.managed), t("Select a role"), (role) => role.name);
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

function renderEmojiCatalog(query = "") {
  const root = $("#emoji-grid");
  const status = $("#emoji-picker-status");
  const normalized = query.trim().toLowerCase();
  const matches = (state.emojiCatalog || []).filter((item) =>
    !normalized || item.name.toLowerCase().includes(normalized) || item.emoji.includes(normalized)
  );
  const fragment = document.createDocumentFragment();
  matches.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "emoji-option";
    button.dataset.emoji = item.emoji;
    button.title = item.name;
    button.setAttribute("aria-label", item.name);
    button.textContent = item.emoji;
    fragment.append(button);
  });
  root.replaceChildren(fragment);
  status.textContent = matches.length
    ? t("{count} standard Unicode emoji", {count: matches.length})
    : t("Nothing found");
}

async function openEmojiPicker() {
  const picker = $("#emoji-picker");
  picker.classList.remove("hidden");
  $("#emoji-search").focus();
  if (state.emojiCatalog) {
    renderEmojiCatalog($("#emoji-search").value);
    return;
  }
  $("#emoji-picker-status").textContent = t("Loading the full Unicode catalog…");
  try {
    state.emojiCatalog = await api("/api/emojis");
    renderEmojiCatalog($("#emoji-search").value);
  } catch (error) {
    if (error instanceof StaleRequest) return;
    $("#emoji-picker-status").textContent = error.message;
    showToast(error.message, true);
  }
}

function closeEmojiPicker() {
  $("#emoji-picker").classList.add("hidden");
}

function resetMentionOptions() {
  state.composeChannels = [];
  state.composeRoles = [];
  fillSelect($("#mention-channel"), [], t("# Insert channel"), () => "");
  fillSelect($("#mention-role"), [], t("@ Insert role"), () => "");
  $("#mention-channel").disabled = true;
  $("#mention-role").disabled = true;
  updateChannelPermissionHint();
}

async function loadComposerResources(guildId) {
  const generation = state.generation;
  if (!guildId) {
    await loadChannels("", $("#channel"));
    resetMentionOptions();
    return;
  }
  try {
    const [channels, roles] = await Promise.all([
      loadChannels(guildId, $("#channel")),
      api(`/api/guilds/${guildId}/roles`),
    ]);
    if (generation !== state.generation) return;
    state.composeChannels = channels || [];
    state.composeRoles = roles;
    fillSelect(
      $("#mention-channel"),
      state.composeChannels,
      t("# Insert channel"),
      (channel) => channel.category ? `${channel.category} / #${channel.name}` : `#${channel.name}`,
    );
    fillSelect(
      $("#mention-role"),
      state.composeRoles,
      t("@ Insert role"),
      (role) => `@${role.name}`,
    );
    updateChannelPermissionHint();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    resetMentionOptions();
    showToast(error.message, true);
  }
}

function updateChannelPermissionHint() {
  const hint = $("#channel-permission-hint");
  const channel = state.composeChannels.find((item) => String(item.id) === $("#channel").value);
  if (!channel || (channel.can_send && channel.can_embed_links)) {
    hint.classList.add("hidden");
    hint.textContent = "";
    return;
  }
  hint.classList.remove("hidden");
  hint.textContent = !channel.can_send
    ? t("The bot lacks Send Messages in this channel.")
    : t("The bot lacks Embed Links; link previews and embeds will not appear.");
}

function insertWrapped(textarea, before, after, placeholder) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selected = textarea.value.slice(start, end) || placeholder;
  const replacement = `${before}${selected}${after}`;
  textarea.setRangeText(replacement, start, end, "end");
  if (start === end) {
    textarea.setSelectionRange(start + before.length, start + before.length + selected.length);
  }
  textarea.focus();
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function insertLinePrefix(textarea, prefix, placeholder) {
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selected = textarea.value.slice(start, end) || placeholder;
  const replacement = selected.split("\n").map((line) => `${prefix}${line}`).join("\n");
  textarea.setRangeText(replacement, start, end, "end");
  textarea.focus();
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function applyMarkdown(textarea, format) {
  const wrappers = {
    bold: ["**", "**", t("bold text")],
    italic: ["*", "*", t("italic text")],
    underline: ["__", "__", t("underlined text")],
    strike: ["~~", "~~", t("strikethrough text")],
    spoiler: ["||", "||", t("hidden text")],
  };
  if (wrappers[format]) {
    insertWrapped(textarea, ...wrappers[format]);
    return;
  }
  if (format === t("code")) {
    const selected = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd);
    insertWrapped(textarea, selected.includes("\n") ? "```\n" : "`", selected.includes("\n") ? "\n```" : "`", t("code"));
    return;
  }
  if (format === "quote") {
    insertLinePrefix(textarea, "> ", t("quote text"));
    return;
  }
  if (format === "list") {
    insertLinePrefix(textarea, "- ", t("list item"));
    return;
  }
  if (format === "raw-link") {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const selected = textarea.value.slice(start, end).trim();
    const duplicateMasked = selected.match(/^\[(https?:\/\/[^\]\s]+)\]\(\1\)$/);
    let url = duplicateMasked?.[1] || (/^https?:\/\/\S+$/.test(selected) ? selected : "");
    if (!url) {
      const prompted = window.prompt("URL for automatic Discord preview", "https://");
      if (prompted === null) return;
      url = prompted.trim();
    }
    textarea.setRangeText(url, start, end, "end");
    textarea.focus();
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  if (format === "link") {
    const selected = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd).trim();
    if (/^https?:\/\/\S+$/.test(selected)) {
      const label = window.prompt("Link display text", t("Open link"));
      if (label === null) return;
      textarea.setRangeText(
        `[${label.trim() || t("Open link")}](${selected})`,
        textarea.selectionStart,
        textarea.selectionEnd,
        "end",
      );
      textarea.focus();
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    const url = window.prompt("Link URL", "https://");
    if (url === null) return;
    insertWrapped(textarea, "[", `](${url.trim()})`, selected || t("link text"));
  }
}

function insertMention(value, enablePing = false) {
  const textarea = $("#content");
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const before = start > 0 && !/\s/.test(textarea.value[start - 1]) ? " " : "";
  const after = end < textarea.value.length && !/\s/.test(textarea.value[end]) ? " " : "";
  textarea.setRangeText(`${before}${value}${after}`, start, end, "end");
  if (enablePing) $("#allow-mentions").checked = true;
  textarea.focus();
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function mentionQueryAtCursor(textarea) {
  if (textarea.selectionStart !== textarea.selectionEnd) return null;
  const before = textarea.value.slice(0, textarea.selectionStart);
  const match = before.match(/(^|\s)([@#])([^\s@#]*)$/u);
  if (!match) return null;
  return {
    marker: match[2],
    query: match[3],
    start: textarea.selectionStart - match[2].length - match[3].length,
  };
}

function mentionItems(marker, query) {
  const normalized = query.toLocaleLowerCase();
  if (marker === "#") {
    return state.composeChannels
      .filter((channel) => channel.name.toLocaleLowerCase().includes(normalized))
      .slice(0, 15)
      .map((channel) => ({
        kind: "channel",
        name: channel.name,
        token: `<#${channel.id}>`,
        icon: "#",
        detail: channel.category || t("Channel"),
      }));
  }

  const special = [
    { kind: "everyone", name: "everyone", token: "@everyone", icon: "@", detail: t("All members") },
    { kind: "here", name: "here", token: "@here", icon: "@", detail: t("Online members") },
  ].filter((item) => item.name.includes(normalized));
  const roles = state.composeRoles
    .filter((role) => role.name.toLocaleLowerCase().includes(normalized))
    .slice(0, Math.max(0, 15 - special.length))
    .map((role) => ({
      kind: "role",
      name: role.name,
      token: `<@&${role.id}>`,
      icon: "@",
      detail: t("Role"),
    }));
  return [...special, ...roles];
}

function closeMentionAutocomplete() {
  const autocomplete = state.mentionAutocomplete;
  autocomplete.textarea = null;
  autocomplete.items = [];
  autocomplete.active = 0;
  $("#mention-autocomplete").classList.add("hidden");
  $("#mention-autocomplete").replaceChildren();
}

function positionMentionAutocomplete(textarea) {
  const root = $("#mention-autocomplete");
  const rect = textarea.getBoundingClientRect();
  const width = Math.min(rect.width, 420, window.innerWidth - 24);
  const left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12);
  const below = rect.bottom + 6;
  const top = below + 290 <= window.innerHeight
    ? below
    : Math.max(8, rect.top - 296);
  root.style.width = `${width}px`;
  root.style.left = `${left}px`;
  root.style.top = `${top}px`;
}

function paintMentionAutocomplete() {
  const autocomplete = state.mentionAutocomplete;
  const root = $("#mention-autocomplete");
  const fragment = document.createDocumentFragment();
  autocomplete.items.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `mention-suggestion${index === autocomplete.active ? " active" : ""}`;
    button.dataset.index = String(index);
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === autocomplete.active));
    const icon = document.createElement("span");
    icon.className = "mention-suggestion-icon";
    icon.textContent = item.icon;
    const description = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = `${item.icon}${item.name}`;
    const detail = document.createElement("small");
    detail.textContent = item.detail;
    description.append(name, detail);
    button.append(icon, description);
    fragment.append(button);
  });
  root.replaceChildren(fragment);
  root.classList.toggle("hidden", autocomplete.items.length === 0);
  if (autocomplete.textarea && autocomplete.items.length) {
    positionMentionAutocomplete(autocomplete.textarea);
    root.querySelector(".active")?.scrollIntoView({ block: "nearest" });
  }
}

function updateMentionAutocomplete(textarea) {
  const match = mentionQueryAtCursor(textarea);
  if (!match || (match.marker === "#" && state.composeChannels.length === 0)) {
    closeMentionAutocomplete();
    return;
  }
  const autocomplete = state.mentionAutocomplete;
  autocomplete.textarea = textarea;
  autocomplete.start = match.start;
  autocomplete.query = match.query;
  autocomplete.items = mentionItems(match.marker, match.query);
  autocomplete.active = 0;
  paintMentionAutocomplete();
}

function chooseMention(index = state.mentionAutocomplete.active) {
  const autocomplete = state.mentionAutocomplete;
  const textarea = autocomplete.textarea;
  const item = autocomplete.items[index];
  if (!textarea || !item) return;
  const end = textarea.selectionStart;
  textarea.setRangeText(`${item.token} `, autocomplete.start, end, "end");
  if (textarea.id === "content" && ["role", "everyone", "here"].includes(item.kind)) {
    $("#allow-mentions").checked = true;
  }
  closeMentionAutocomplete();
  textarea.focus();
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function handleMentionKeydown(event) {
  const autocomplete = state.mentionAutocomplete;
  if (autocomplete.textarea !== event.currentTarget || !autocomplete.items.length) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const direction = event.key === "ArrowDown" ? 1 : -1;
    autocomplete.active = (
      autocomplete.active + direction + autocomplete.items.length
    ) % autocomplete.items.length;
    paintMentionAutocomplete();
    return;
  }
  if (event.key === "Enter" || event.key === "Tab") {
    event.preventDefault();
    chooseMention();
    return;
  }
  if (event.key === " " && autocomplete.query) {
    const exactIndex = autocomplete.items.findIndex(
      (item) => item.name.toLocaleLowerCase() === autocomplete.query.toLocaleLowerCase()
    );
    if (exactIndex >= 0) {
      event.preventDefault();
      chooseMention(exactIndex);
    }
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    closeMentionAutocomplete();
  }
}

function bindMentionAutocomplete(textarea) {
  textarea.addEventListener("input", () => updateMentionAutocomplete(textarea));
  textarea.addEventListener("click", () => updateMentionAutocomplete(textarea));
  textarea.addEventListener("keydown", handleMentionKeydown);
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function renderDiscordMarkdown(value) {
  let html = escapeHtml(value);
  const links = [];
  html = html.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => {
    const token = `\u0000LINK${links.length}\u0000`;
    links.push(`<a class="markdown-link" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    return token;
  });
  html = html.replace(/&lt;#(\d+)&gt;/g, (_, id) => {
    const channel = state.composeChannels.find((item) => String(item.id) === id);
    return `<span class="discord-mention">#${channel ? escapeHtml(channel.name) : id}</span>`;
  });
  html = html.replace(/&lt;@&amp;(\d+)&gt;/g, (_, id) => {
    const role = state.composeRoles.find((item) => String(item.id) === id);
    return `<span class="discord-mention">@${role ? escapeHtml(role.name) : id}</span>`;
  });
  html = html.replace(/(^|\s)@(everyone|here)\b/g, '$1<span class="discord-mention">@$2</span>');
  html = html.replace(/https?:\/\/[^\s<\u0000]+/g, (url) =>
    `<a class="markdown-link" href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_\n]+)__/g, "<u>$1</u>");
  html = html.replace(/~~([^~\n]+)~~/g, "<s>$1</s>");
  html = html.replace(/\|\|([^|\n]+)\|\|/g, '<span class="markdown-spoiler">$1</span>');
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/(^|\n)&gt; ([^\n]*)/g, '$1<span class="markdown-quote">$2</span>');
  html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  html = html.replace(/\u0000LINK(\d+)\u0000/g, (_, index) => links[Number(index)]);
  return html.replace(/\n/g, "<br>");
}

function addEmbedField(value = {}) {
  const row = document.createElement("div");
  row.className = "field-row";
  row.innerHTML = `
    <input class="field-name" maxlength="256" placeholder="${t('Name')}" data-i18n-placeholder="Name">
    <input class="field-value" maxlength="1024" placeholder="${t('Value')}" data-i18n-placeholder="Value">
    <label class="inline-label"><input class="field-inline" type="checkbox"><span data-i18n="Inline">${t('Inline')}</span></label>
    <button class="button ghost danger small remove-field" type="button" data-i18n="Delete">${t('Delete')}</button>
  `;
  row.querySelector(".field-name").value = value.name || "";
  row.querySelector(".field-value").value = value.value || "";
  row.querySelector(".field-inline").checked = Boolean(value.inline);
  row.querySelector(".remove-field").addEventListener("click", () => {
    row.remove();
    updatePreview();
  });
  row.querySelectorAll("input").forEach((input) => input.addEventListener("input", updatePreview));
  bindMentionAutocomplete(row.querySelector(".field-value"));
  PanelI18n.apply(row);
  $("#embed-fields").append(row);
  updatePreview();
}

function collectFields() {
  return $$(".field-row").map((row) => ({
    name: row.querySelector(".field-name").value,
    value: row.querySelector(".field-value").value,
    inline: row.querySelector(".field-inline").checked,
  })).filter((field) => field.name || field.value);
}

function collectEmbed() {
  if (!$("#embed-enabled").checked) return null;
  return {
    title: $("#embed-title").value,
    description: $("#embed-description").value,
    url: $("#embed-url").value,
    color: $("#embed-color").value,
    author: {
      name: $("#author-name").value,
      url: $("#author-url").value,
      icon_url: $("#author-icon").value,
    },
    image_url: $("#image-url").value,
    thumbnail_url: $("#thumbnail-url").value,
    footer: { text: $("#footer-text").value, icon_url: $("#footer-icon").value },
    timestamp: $("#embed-timestamp").checked ? (state.embedTimestamp || true) : false,
    fields: collectFields(),
  };
}

function updatePreview() {
  const content = $("#content").value;
  $("#content-count").textContent = content.length;
  $("#preview-content").innerHTML = renderDiscordMarkdown(content || t("Message text"));
  $("#preview-content").classList.toggle("muted", !content);

  const enabled = $("#embed-enabled").checked;
  $("#embed-editor").classList.toggle("disabled", !enabled);
  $("#preview-embed").classList.toggle("hidden", !enabled);
  if (!enabled) return;

  const embed = collectEmbed();
  const preview = $("#preview-embed");
  preview.style.borderLeftColor = embed.color || "#d5ff38";
  $("#preview-title").textContent = embed.title || t("Post title");
  $("#preview-title").href = embed.url || "#";
  $("#preview-description").innerHTML = renderDiscordMarkdown(embed.description || t("Your post description will appear here."));
  $("#preview-description").classList.toggle("muted", !embed.description);

  const author = $("#preview-author");
  author.textContent = embed.author.name;
  author.classList.toggle("hidden", !embed.author.name);
  const footer = $("#preview-footer");
  footer.textContent = [embed.footer.text, embed.timestamp ? t("now") : ""].filter(Boolean).join(" · ");
  footer.classList.toggle("hidden", !footer.textContent);

  const fields = $("#preview-fields");
  fields.replaceChildren();
  embed.fields.forEach((field) => {
    const node = document.createElement("div");
    node.className = `preview-field${field.inline ? "" : " wide"}`;
    const title = document.createElement("strong");
    title.textContent = field.name || "\u200b";
    const value = document.createElement("span");
    value.innerHTML = renderDiscordMarkdown(field.value || "\u200b");
    node.append(title, value);
    fields.append(node);
  });
  const hasImage = Boolean(embed.image_url || state.uploads.some((upload) => upload.main));
  $("#preview-image").classList.toggle("hidden", !hasImage);
}

async function uploadFiles(fileList) {
  const generation = state.generation;
  for (const file of fileList) {
    if (state.uploads.length >= 10) {
      showToast("A message can have at most 10 files", true);
      break;
    }
    const data = new FormData();
    data.append("file", file);
    setStatus("#form-status", t("Uploading {name}…", {name: file.name}));
    try {
      const response = await fetch(scopedUrl("/api/uploads"), { method: "POST", body: data, headers: {"Accept-Language": PanelI18n.language(), ...(state.auth.csrf_token ? {"X-CSRF-Token": state.auth.csrf_token} : {})} });
      const result = await response.json();
      if (generation !== state.generation) return;
      if (!response.ok) throw new Error(apiError(result, response.status));
      state.uploads.push({ ...result, main: false });
      renderUploads();
    } catch (error) {
      if (generation !== state.generation || error instanceof StaleRequest) return;
      showToast(error instanceof TypeError ? t("Network error. Check your connection to the panel.") : error.message, true);
    }
  }
  setStatus("#form-status", "");
  $("#files").value = "";
}

function renderUploads() {
  const root = $("#uploads");
  root.replaceChildren();
  state.uploads.forEach((upload, index) => {
    const row = document.createElement("div");
    row.className = "upload-item";
    const name = document.createElement("span");
    name.textContent = upload.name || t("Saved file · {id}", {id: upload.id.slice(0, 8)});
    const main = document.createElement("label");
    main.className = "switch-row";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "main-image";
    radio.checked = upload.main;
    radio.addEventListener("change", () => {
      state.uploads.forEach((item) => { item.main = false; });
      upload.main = true;
      renderUploads();
      updatePreview();
    });
    main.append(radio, document.createTextNode(t("Main")));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button ghost danger small";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.uploads.splice(index, 1);
      renderUploads();
      updatePreview();
    });
    row.append(name, main, remove);
    root.append(row);
  });
  state.preservedAttachments.forEach((attachment, index) => {
    const row = document.createElement("div");
    row.className = "upload-item";
    const name = document.createElement("span");
    name.textContent = attachment.name || t("Discord attachment · {id}", {id: attachment.id});
    const source = document.createElement("span");
    source.className = "field-hint";
    source.textContent = t("Discord attachment");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button ghost danger small";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.preservedAttachments.splice(index, 1);
      if ($("#image-url").value === `attachment://${attachment.name}`) $("#image-url").value = "";
      if ($("#thumbnail-url").value === `attachment://${attachment.name}`) $("#thumbnail-url").value = "";
      renderUploads();
      updatePreview();
    });
    row.append(name, source, remove);
    root.append(row);
  });
  updatePreview();
}

function collectPayload() {
  return {
    guild_id: $("#guild").value,
    channel_id: $("#channel").value,
    content: $("#content").value,
    embed: collectEmbed(),
    upload_ids: state.uploads.map((upload) => upload.id),
    image_upload_id: state.uploads.find((upload) => upload.main)?.id || null,
    preserved_attachment_ids: state.preservedAttachments.map((attachment) => attachment.id),
    preserved_attachment_names: Object.fromEntries(
      state.preservedAttachments.filter(attachment => attachment.name).map((attachment) => [attachment.id, attachment.name])
    ),
    allow_mentions: $("#allow-mentions").checked,
  };
}

function resetComposer() {
  state.editingId = null;
  state.uploads = [];
  state.preservedAttachments = [];
  state.embedTimestamp = null;
  $("#message-form").reset();
  $("#embed-enabled").checked = true;
  $("#embed-color").value = "#d5ff38";
  $("#embed-fields").replaceChildren();
  $("#composer-title").textContent = t("Create post");
  $("#submit-message").textContent = t("Send message");
  $("#cancel-edit").classList.add("hidden");
  $("#guild").disabled = false;
  $("#channel").disabled = !$("#guild").value;
  resetMentionOptions();
  renderUploads();
  updatePreview();
}

async function submitMessage(event) {
  const generation = state.generation;
  event.preventDefault();
  const payload = collectPayload();
  const button = $("#submit-message");
  button.disabled = true;
  setStatus("#form-status", state.editingId ? t("Saving…") : t("Sending…"));
  try {
    await api(state.editingId ? `/api/messages/${state.editingId}` : "/api/messages", {
      method: state.editingId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    showToast(state.editingId ? t("Message updated") : t("Message sent"));
    resetComposer();
    await loadMessages();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    setStatus("#form-status", error.message, true);
    showToast(error.message, true);
  } finally {
    if (generation === state.generation) button.disabled = false;
  }
}

async function loadImportChannels(guildId) {
  const generation = state.generation;
  const select = $("#import-channel");
  const channels = await loadChannels(guildId, select);
  if (generation !== state.generation) return;
  if (select.options.length) {
    select.options[0].dataset.i18n = "All available channels";
    select.options[0].textContent = t("All available channels");
  }
  return channels;
}

async function submitImport(event) {
  const generation = state.generation;
  event.preventDefault();
  const scanAllHistory = $("#import-all").checked;
  if (scanAllHistory && !$("#import-channel").value) {
    const confirmed = window.confirm(
      t("Scan the entire history of all available channels? This may take a long time.")
    );
    if (!confirmed) return;
  }
  const button = $("#import-submit");
  button.disabled = true;
  setStatus("#import-status", t("Scanning Discord history… Keep this page open."));
  try {
    const result = await api("/api/messages/import", {
      method: "POST",
      body: JSON.stringify({
        guild_id: $("#import-guild").value,
        channel_id: $("#import-channel").value || null,
        limit_per_channel: scanAllHistory ? null : Number($("#import-limit").value),
      }),
    });
    const skipped = result.skipped_channels.length
      ? t(" Skipped channels without access: {channels}.", {channels: result.skipped_channels.join(", ")})
      : "";
    const summary = t("Found {imported}: {created} new, {updated} updated. Messages scanned: {scanned}.{skipped}", {...result, skipped});
    setStatus("#import-status", summary);
    showToast(t("Messages imported: {count}", {count: result.imported}));
    await loadMessages();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    setStatus("#import-status", error.message, true);
    showToast(error.message, true);
  } finally {
    if (generation === state.generation) button.disabled = false;
  }
}

async function loadMessages() {
  try {
    state.messages = await api("/api/messages");
    renderMessages();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

function renderMessages() {
  const root = $("#messages-list");
  root.replaceChildren();
  root.classList.toggle("empty-state", state.messages.length === 0);
  if (!state.messages.length) {
    root.textContent = t("No messages yet.");
    return;
  }
  state.messages.forEach((record) => {
    const row = document.createElement("div");
    row.className = "record";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = record.payload.embed?.title || record.payload.content?.slice(0, 80) || t("Message with attachment");
    const meta = document.createElement("p");
    meta.textContent = t("Message {message} · Channel {channel} · {date}", {message: record.message_id, channel: record.channel_id, date: formatDate(record.updated_at)});
    info.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "record-actions";
    const writable = canWriteBot();
    const edit = document.createElement("button");
    edit.className = "button ghost small";
    edit.textContent = t("Edit");
    edit.addEventListener("click", () => editMessage(record));
    const open = document.createElement("a");
    open.className = "button ghost small";
    open.textContent = t("Open");
    open.href = `https://discord.com/channels/${record.guild_id}/${record.channel_id}/${record.message_id}`;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    const remove = document.createElement("button");
    remove.className = "button ghost danger small";
    remove.textContent = t("Delete");
    remove.addEventListener("click", () => deleteMessage(record));
    actions.append(open);
    if (writable) actions.append(edit, remove);
    row.append(info, actions);
    root.append(row);
  });
}

async function editMessage(record) {
  const generation = state.generation;
  if (record.bot_id !== state.botId) return;
  const payload = record.payload;
  state.editingId = record.id;
  $("#guild").value = String(record.guild_id);
  await loadComposerResources(record.guild_id);
  if (generation !== state.generation) return;
  $("#channel").value = String(record.channel_id);
  updateChannelPermissionHint();
  $("#guild").disabled = true;
  $("#channel").disabled = true;
  $("#content").value = payload.content || "";
  const embed = payload.embed;
  $("#embed-enabled").checked = Boolean(embed);
  $("#embed-title").value = embed?.title || "";
  $("#embed-description").value = embed?.description || "";
  $("#embed-url").value = embed?.url || "";
  $("#embed-color").value = embed?.color || "#d5ff38";
  $("#author-name").value = embed?.author?.name || "";
  $("#author-url").value = embed?.author?.url || "";
  $("#author-icon").value = embed?.author?.icon_url || "";
  $("#image-url").value = embed?.image_url || "";
  $("#thumbnail-url").value = embed?.thumbnail_url || "";
  $("#footer-text").value = embed?.footer?.text || "";
  $("#footer-icon").value = embed?.footer?.icon_url || "";
  state.embedTimestamp = typeof embed?.timestamp === "string" ? embed.timestamp : null;
  $("#embed-timestamp").checked = Boolean(embed?.timestamp);
  $("#allow-mentions").checked = Boolean(payload.allow_mentions);
  $("#embed-fields").replaceChildren();
  (embed?.fields || []).forEach(addEmbedField);
  state.uploads = (payload.upload_ids || []).map((id) => ({
    id,
    name: null,
    main: id === payload.image_upload_id,
  }));
  state.preservedAttachments = (payload.preserved_attachment_ids || []).map((id) => ({
    id,
    name: payload.preserved_attachment_names?.[id] || null,
  }));
  $("#composer-title").textContent = t("Edit post");
  $("#submit-message").textContent = t("Save changes");
  $("#cancel-edit").classList.remove("hidden");
  renderUploads();
  updatePreview();
  switchTab("composer");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function deleteMessage(record) {
  if (!confirm(t("Delete message {id} from Discord?", {id: record.message_id}))) return;
  try {
    await api(`/api/messages/${record.id}`, { method: "DELETE" });
    showToast("Message deleted");
    await loadMessages();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

async function submitReactionRole(event) {
  event.preventDefault();
  const payload = {
    guild_id: $("#rr-guild").value,
    channel_id: $("#rr-channel").value,
    message_id: $("#rr-message").value,
    emoji: $("#rr-emoji").value,
    role_id: $("#rr-role").value,
    remove_on_unreact: $("#rr-remove").checked,
  };
  setStatus("#rr-status", t("Creating…"));
  try {
    await api("/api/reaction-roles", { method: "POST", body: JSON.stringify(payload) });
    setStatus("#rr-status", t("Done"));
    $("#rr-message").value = "";
    $("#rr-emoji").value = "";
    showToast("Reaction rule created");
    await loadReactionRoles();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    setStatus("#rr-status", error.message, true);
    showToast(error.message, true);
  }
}

async function loadReactionRoles() {
  try {
    state.reactionRoles = await api("/api/reaction-roles");
    renderReactionRoles();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

function renderReactionRoles() {
  const rules = state.reactionRoles;
  const root = $("#reaction-list");
  root.replaceChildren();
  root.classList.toggle("empty-state", rules.length === 0);
  if (!rules.length) {
    root.textContent = t("No rules yet.");
    return;
  }
  rules.forEach((rule) => {
    const row = document.createElement("div");
    row.className = "record";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = t("{emoji} → Role {role}", {emoji: rule.emoji_display, role: rule.role_id});
    const meta = document.createElement("p");
    meta.textContent = t("Message {message} · {mode}", {message: rule.message_id, mode: rule.remove_on_unreact ? t("Grant and remove") : t("Grant only")});
    info.append(title, meta);
    const remove = document.createElement("button");
    remove.className = "button ghost danger small";
    remove.textContent = t("Delete");
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/reaction-roles/${rule.id}`, { method: "DELETE" });
        await loadReactionRoles();
      } catch (error) {
        if (error instanceof StaleRequest) return;
        showToast(error.message, true);
      }
    });
    row.append(info);
    if (canWriteBot()) row.append(remove);
    root.append(row);
  });
}

async function loadAudit() {
  try {
    state.auditEntries = await api("/api/audit?limit=100");
    renderAudit();
  } catch (error) {
    if (error instanceof StaleRequest) return;
    showToast(error.message, true);
  }
}

function renderAudit() {
  const entries = state.auditEntries;
  const root = $("#audit-list");
  root.replaceChildren();
  root.classList.toggle("empty-state", entries.length === 0);
  if (!entries.length) {
    root.textContent = t("The audit log is empty.");
    return;
  }
  entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "audit-row";
    const time = document.createElement("time");
    time.textContent = formatDate(entry.created_at);
    const action = document.createElement("strong");
    action.textContent = t(AUDIT_ACTIONS[entry.action] || entry.action);
    const details = document.createElement("span");
    const actor = entry.actor_id === "local" ? t("Local administrator") : entry.actor_id || t("Bot / system");
    details.textContent = `${t("Actor: {actor}", {actor})} · ${formatAuditDetails(entry.details)}`;
    row.append(time, action, details);
    root.append(row);
  });
}

function bindEvents() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  $("#guild").addEventListener("change", (event) => loadComposerResources(event.target.value));
  $("#channel").addEventListener("change", updateChannelPermissionHint);
  $("#rr-guild").addEventListener("change", async (event) => {
    await Promise.all([loadChannels(event.target.value, $("#rr-channel")), loadRoles(event.target.value)]);
  });
  $("#import-guild").addEventListener("change", (event) => loadImportChannels(event.target.value));
  $("#import-all").addEventListener("change", (event) => {
    $("#import-limit").disabled = event.target.checked;
  });
  $("#import-form").addEventListener("submit", submitImport);
  $("#add-field").addEventListener("click", () => addEmbedField());
  $$(".markdown-toolbar button").forEach((button) => button.addEventListener("click", () => {
    applyMarkdown($(`#${button.closest(".markdown-toolbar").dataset.target}`), button.dataset.format);
  }));
  $("#insert-everyone").addEventListener("click", () => insertMention("@everyone", true));
  $("#mention-role").addEventListener("change", (event) => {
    if (event.target.value) insertMention(`<@&${event.target.value}>`, true);
    event.target.value = "";
  });
  $("#mention-channel").addEventListener("change", (event) => {
    if (event.target.value) insertMention(`<#${event.target.value}>`);
    event.target.value = "";
  });
  $("#open-emoji-picker").addEventListener("click", openEmojiPicker);
  $("#close-emoji-picker").addEventListener("click", closeEmojiPicker);
  $("#emoji-search").addEventListener("input", (event) => renderEmojiCatalog(event.target.value));
  $("#emoji-grid").addEventListener("click", (event) => {
    const button = event.target.closest(".emoji-option");
    if (!button) return;
    $("#rr-emoji").value = button.dataset.emoji;
    closeEmojiPicker();
    $("#rr-emoji").focus();
  });
  [$("#content"), $("#embed-description")].forEach(bindMentionAutocomplete);
  $("#mention-autocomplete").addEventListener("mousedown", (event) => {
    const button = event.target.closest(".mention-suggestion");
    if (!button) return;
    event.preventDefault();
    chooseMention(Number(button.dataset.index));
  });
  document.addEventListener("mousedown", (event) => {
    if (
      !event.target.closest("#mention-autocomplete")
      && event.target !== $("#content")
      && event.target !== $("#embed-description")
      && !event.target.matches(".field-value")
    ) closeMentionAutocomplete();
  });
  $("#files").addEventListener("change", (event) => uploadFiles(event.target.files));
  $("#embed-timestamp").addEventListener("change", () => { state.embedTimestamp = null; });
  $("#message-form").addEventListener("submit", submitMessage);
  $("#reaction-form").addEventListener("submit", submitReactionRole);
  $("#cancel-edit").addEventListener("click", resetComposer);
  $("#refresh-messages").addEventListener("click", loadMessages);
  $("#refresh-roles").addEventListener("click", loadReactionRoles);
  $("#refresh-audit").addEventListener("click", loadAudit);
  $$("#message-form input, #message-form textarea").forEach((input) => {
    if (input.id !== "files") input.addEventListener("input", updatePreview);
  });
}

PanelI18n.apply();
bindEvents();
updatePreview();
initializeAccount();
setInterval(() => { if (state.botId) refreshStatus(); }, 5000);


async function loadBots(preferred = state.botId) {
  try {
    state.bots = await api("/api/bots");
    const select = $("#bot-select");
    select.replaceChildren(...state.bots.map(bot => new Option(botDisplayName(bot), bot.id)));
    select.disabled = state.bots.length === 0;
    const id = state.bots.some(bot => bot.id === preferred) ? preferred : state.bots[0]?.id;
    if (id) await selectBot(id);
    else {
      state.generation++;
      state.botId = null;
      state.messages = []; state.reactionRoles = []; state.auditEntries = [];
      resetComposer();
      renderMessages(); renderReactionRoles(); renderAudit();
      $("#connection strong").textContent = t("No bots available");
    }
    fillSelect($("#grant-bot"), state.bots, t("Select a bot profile"), botDisplayName);
    applyAccessUI();
  } catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
}

async function selectBot(id) {
  state.generation++;
  state.botId = id;
  $("#bot-select").value = id;
  state.guilds = [];
  state.messages = [];
  state.reactionRoles = [];
  state.auditEntries = [];
  $("#submit-message").disabled = false;
  $("#import-submit").disabled = false;
  resetComposer();
  $("#reaction-form").reset();
  $("#import-form").reset();
  $("#import-limit").disabled = false;
  closeMentionAutocomplete();
  closeEmojiPicker();
  for (const selector of ["#guild", "#rr-guild", "#import-guild", "#channel", "#rr-channel", "#rr-role", "#import-channel"]) {
    fillSelect($(selector), [], t("Select a server"), () => "");
    $(selector).disabled = true;
  }
  for (const selector of ["#messages-list", "#reaction-list", "#audit-list"]) $(selector).replaceChildren();
  for (const selector of ["#form-status", "#rr-status", "#import-status"]) setStatus(selector, "");
  $("#preview-bot").textContent = botDisplayName(state.bots.find(bot => bot.id === id)) || id;
  $("#bot-form").classList.add("hidden");
  applyAccessUI();
  await Promise.all([refreshStatus(), loadMessages(), loadReactionRoles(), loadAudit()]);
}

function profileForm(isNew) {
  const bot = isNew ? {} : state.bots.find(bot => bot.id === state.botId) || {};
  $("#bot-form").dataset.mode = isNew ? "new" : "edit";
  $("#bot-id").value = bot.id || "";
  $("#bot-id").disabled = !isNew;
  $("#bot-name").value = bot.name || "";
  $("#bot-application").value = bot.application_id || "";
  $("#bot-env").value = bot.token_env || "";
  $("#bot-enabled").checked = isNew || Boolean(bot.enabled);
  $("#bot-privileged").checked = Boolean(bot.allow_privileged_roles);
  $("#bot-form").classList.remove("hidden");
}

$("#bot-select").addEventListener("change", event => { if (event.target.value) selectBot(event.target.value); });
$("#bot-new").addEventListener("click", () => profileForm(true));
$("#bot-edit").addEventListener("click", () => profileForm(false));
$("#bot-cancel").addEventListener("click", () => $("#bot-form").classList.add("hidden"));
$("#bot-reconnect").addEventListener("click", async () => {
  try { await api(`/api/bots/${state.botId}/reconnect`, {method: "POST"}); await refreshStatus(); }
  catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
});
$("#bot-form").addEventListener("submit", async event => {
  event.preventDefault();
  const profile = {
    id: $("#bot-id").value, name: $("#bot-name").value,
    application_id: $("#bot-application").value || null,
    token_env: $("#bot-env").value, enabled: $("#bot-enabled").checked,
    allow_privileged_roles: $("#bot-privileged").checked,
  };
  const isNew = $("#bot-form").dataset.mode === "new";
  try {
    await api(isNew ? "/api/bots" : `/api/bots/${profile.id}`, {method: isNew ? "POST" : "PUT", body: JSON.stringify(profile)});
    await loadBots(profile.id);
  } catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
});


function repaintLanguage() {
  // Only UI text is repainted. The selected bot, draft, files and requests stay intact.
  $("#bot-select").replaceChildren(...state.bots.map(bot => new Option(botDisplayName(bot), bot.id)));
  $("#bot-select").value = state.botId || "";
  $("#preview-bot").textContent = botDisplayName(state.bots.find(bot => bot.id === state.botId)) || "Discord Bot";
  $("#composer-title").textContent = t(state.editingId ? "Edit post" : "Create post");
  $("#submit-message").textContent = t(state.editingId ? "Save changes" : "Send message");
  for (const selector of ["#form-status", "#rr-status", "#import-status", "#toast", "#connection strong", "#emoji-picker-status"]) {
    const element = $(selector);
    if (element.textContent) element.textContent = PanelI18n.translateMessage(element.textContent);
  }
  closeMentionAutocomplete();
  renderUploads();
  updateChannelPermissionHint();
  renderMessages();
  renderReactionRoles();
  renderAudit();
  if (state.emojiCatalog) renderEmojiCatalog($("#emoji-search").value);
  applyAccessUI();
  if (state.auth.owner) renderUsers();
}

$("#language-select").addEventListener("change", event => PanelI18n.setLanguage(event.target.value));
document.addEventListener("panel-language-change", repaintLanguage);


function canWriteBot() {
  const bot = state.bots.find(bot => bot.id === state.botId);
  return Boolean(bot && (state.auth.owner || bot.access_role === "operator"));
}

function applyAccessUI() {
  const owner = state.auth.owner;
  for (const selector of ["#bot-new", "#bot-edit", "#bot-reconnect", "#users-tab", "#manage-users"]) $(selector).classList.toggle("hidden", !owner);
  $("#logout").classList.toggle("hidden", state.auth.mode !== "discord");
  $("#auth-mode-notice").classList.toggle("hidden", state.auth.mode !== "local");
  $("#account-name").textContent = state.auth.mode === "local" ? t("Local administrator") : state.auth.user?.display_name || "";
  for (const selector of ["#submit-message", "#import-submit", "#reaction-form button[type=submit]", "#files"]) $(selector).disabled = !canWriteBot();
  const noAccess = state.bots.length === 0;
  $("#access-notice").classList.toggle("hidden", !noAccess && canWriteBot());
  $("#access-notice").textContent = noAccess ? t("You have no assigned bots. Ask the panel owner for access.") : t("You have read-only access to this bot.");
}

async function initializeAccount() {
  try {
    state.auth = await api("/api/auth/me");
    if (!state.auth.authenticated) { window.location.assign("/login"); return; }
    applyAccessUI();
    await loadBots();
  } catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
}

async function loadUsers() {
  try { state.users = await api("/api/admin/users"); renderUsers(); }
  catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
}

function renderUsers() {
  const root = $("#users-list");
  root.replaceChildren();
  if (!state.users.length) { root.textContent = t("No users yet."); return; }
  for (const user of state.users) {
    const row = document.createElement("div"); row.className = "record";
    const info = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = `${user.display_name} · ${user.id}`;
    const permissions = document.createElement("p");
    permissions.textContent = user.owner ? t("Owner") : user.blocked ? t("Blocked") :
      user.grants.map(grant => `${botDisplayName(state.bots.find(bot => bot.id === grant.bot_id)) || grant.bot_id}: ${t(grant.role === "operator" ? "Operator" : "Viewer")}`).join(" · ") || t("No access");
    info.append(name, permissions); row.append(info);
    if (!user.owner) {
      const actions = document.createElement("div"); actions.className = "record-actions";
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "button ghost small"; edit.textContent = t("Edit access");
      edit.addEventListener("click", () => { $("#grant-user").value = user.id; $("#grant-user").focus(); });
      const block = document.createElement("button"); block.type = "button"; block.className = "button ghost danger small"; block.textContent = t(user.blocked ? "Unblock" : "Block user");
      block.addEventListener("click", async () => {
        if (!confirm(t(user.blocked ? "Unblock this user?" : "Block this user and end all their sessions?"))) return;
        try { await api(`/api/admin/users/${user.id}`, {method:"PUT", body: JSON.stringify({blocked: !user.blocked})}); await loadUsers(); }
        catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
      });
      actions.append(edit, block); row.append(actions);
    }
    root.append(row);
  }
}

$("#manage-users").addEventListener("click", () => switchTab("users"));
$("#refresh-users").addEventListener("click", loadUsers);
$("#grant-form").addEventListener("submit", async event => {
  event.preventDefault();
  const role = $("#grant-role").value;
  try {
    await api(`/api/admin/users/${$("#grant-user").value}/bots/${$("#grant-bot").value}`, {method:"PUT", body:JSON.stringify({role: role === "none" ? null : role})});
    showToast(t("Access saved")); await loadUsers();
  } catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
});
$("#logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", {method:"POST"});
    window.location.assign("/login");
  } catch (error) { if (!(error instanceof StaleRequest)) showToast(error.message, true); }
});
