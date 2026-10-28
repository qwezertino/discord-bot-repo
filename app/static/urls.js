/* All UI navigation and API requests share the server-configured mount path. */
globalThis.PanelURLs = (() => {
  const {base_path: basePath} = JSON.parse(document.getElementById("panel-config").textContent);
  return {path: path => {
    if (!path.startsWith("/") || path.startsWith("//")) throw new Error("Invalid panel URL");
    return basePath + path;
  }};
})();
