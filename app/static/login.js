function updateLoginLanguage() {
  PanelI18n.apply();
  document.getElementById("discord-login").href = `/auth/discord/login?lang=${PanelI18n.language()}`;
}
document.getElementById("language-select").addEventListener("change", event => PanelI18n.setLanguage(event.target.value));
document.addEventListener("panel-language-change", updateLoginLanguage);
updateLoginLanguage();
