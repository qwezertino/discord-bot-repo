const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const catalog = fs.readFileSync('app/static/translations.json', 'utf8');
const listeners = new Map();
const nodes = new Map();
const savedPreferences = new Map();
function cssClasses() {
  const values = new Set();
  return {add: value => values.add(value), remove: value => values.delete(value), contains: value => values.has(value),
    toggle(value, force) { const enabled = force ?? !values.has(value); if (enabled) values.add(value); else values.delete(value); return enabled; }};
}
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    value: '', disabled: false, checked: false, textContent: '', style: {}, dataset: {}, options: [],
    classList: cssClasses(),
    replaceChildren(...items) { this.options = items; },
    add(item) { this.options.push(item); }, reset() {},
    querySelector() { return node(selector + ' strong'); },
    querySelectorAll() { return []; },
    getAttribute(name) { return this.attributes?.[name]; },
    setAttribute(name, value) { (this.attributes ||= {})[name] = value; },
    addEventListener(type, handler) { listeners.set(selector + ':' + type, handler); },
  });
  return nodes.get(selector);
}
const context = vm.createContext({
  document: {querySelector: node, querySelectorAll: selector => {
    const attribute = selector.match(/^\[(data-i18n(?:-[a-z-]+)?)\]$/)?.[1];
    return attribute ? [...nodes.values()].filter(item => item.getAttribute(attribute)) : [];
  }, documentElement: {lang: 'uk'},
    getElementById: id => id === 'i18n-catalog' ? {textContent: catalog} : id === 'panel-config' ? {textContent: JSON.stringify({base_path:''})} : node('#' + id),
    addEventListener(type, handler) { listeners.set('document:' + type, handler); },
    dispatchEvent(event) { listeners.get('document:' + event.type)?.(event); },
  },
  Event: class { constructor(type) { this.type = type; } },
  localStorage: {getItem: key => savedPreferences.get(key), setItem: (key, value) => savedPreferences.set(key, value)},
  Option: function(label, value) { this.textContent = label; this.value = value; this.dataset = {}; },
  console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync('app/static/urls.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('app/static/i18n.js', 'utf8'), context);
// Suppress boot only; exercise the actual functions from the shipped script.
vm.runInContext(source.replace('bindEvents();\nupdatePreview();\ninitializeAccount();\nsetInterval(() => { if (state.botId) refreshStatus(); }, 5000);', ''), context);
const run = code => vm.runInContext(code, context);
async function main() {
  let resolve;
  context.fetch = url => new Promise(done => { resolve = () => done({ok: true, headers: {get: () => 'application/json'}, json: async () => [{id:'old', name:'old'}]}); });
  run('state.botId = "first"');
  const channels = run('loadChannels("1", $("#channel"))');
  run('state.botId = "second"; state.generation++');
  resolve();
  await channels;
  assert.equal(node('#channel').options.length, 1);
  assert.equal(node('#channel').options[0].value, '');
  let requested;
  context.fetch = async url => { requested = url; return {ok:true, headers:{get:()=> 'application/json'}, json:async()=>[]}; };
  await run('api("/api/messages")');
  assert.equal(requested, '/api/bots/second/messages');
  // An edit awaiting the old bot must not restore its draft after a switch.
  run('loadComposerResources = async () => new Promise(resolve => { globalThis.finishResources = resolve; })');
  const edit = run('editMessage({id: 7, bot_id: "second", guild_id: "1", channel_id: "2", payload:{content:"foreign draft"}})');
  run('state.botId = "third"; state.generation++; state.editingId = null; finishResources()');
  await edit;
  assert.equal(node('#content').value, '');
  assert.equal(run('state.editingId'), null);
  run('updatePreview = () => {}; renderUploads = () => {}; resetMentionOptions = () => {}; closeMentionAutocomplete = () => {}; closeEmojiPicker = () => {}');
  node('#message-form').reset = () => { node('#content').value = ''; };
  run('state.bots = [{id: "fourth", name: "Fourth"}]; state.uploads = [{id:"old"}]; state.preservedAttachments = [{id:"123"}]; state.editingId = 9; state.embedTimestamp = "old"');
  node('#content').value = 'old draft';
  await run('selectBot("fourth")');
  assert.equal(run('state.uploads.length'), 0);
  assert.equal(run('state.preservedAttachments.length'), 0);
  assert.equal(run('state.editingId'), null);
  assert.equal(run('state.embedTimestamp'), null);
  assert.equal(node('#content').value, '');
  assert.equal(node('#preview-bot').textContent, 'Fourth');
  // A network error from an old profile is also suppressed.
  let reject;
  context.fetch = () => new Promise((_, fail) => { reject = fail; });
  const pending = run('api("/api/status")');
  run('state.generation++');
  reject(new Error('old connection failure'));
  await assert.rejects(pending, error => error.constructor.name === 'StaleRequest');
  context.fetch = async () => ({ok: true, headers: {get: () => 'application/json'}, json: async () => [
    {id: 'iron_crown', name: 'Iron Crown'}, {id: 'perimeter', name: 'Perimeter'},
  ]});
  run('selectBot = async id => { globalThis.selectedBot = id; }');
  await run('loadBots("perimeter")');
  assert.deepEqual(node('#bot-select').options.map(option => option.textContent), ['Iron Crown', 'Perimeter']);
  assert.equal(run('selectedBot'), 'perimeter');
  // Switching languages must repaint UI labels without touching any draft data.
  run('state.botId = "perimeter"; state.editingId = 42; state.uploads = [{id:"upload", name:"Send message", main:true}]; state.preservedAttachments = [{id:"123456789012345678", name:"Готово.txt"}]; state.embedTimestamp = "2026-10-28T12:00:00Z"');
  node('#content').value = 'Надіслати повідомлення — user content stays unchanged';
  node('#embed-enabled').checked = true;
  node('#embed-title').value = 'Save changes';
  node('#embed-description').value = 'Власний опис';
  node('#guild').value = '123456789012345678';
  node('#channel').value = '223456789012345678';
  node('#allow-mentions').checked = true;
  node('#content').setAttribute('data-i18n-placeholder', 'Message text');
  run('collectFields = () => [{name:"Name", value:"Значення", inline:true}]');
  run('setStatus("#form-status", t("Sending…"))');
  const draft = run('JSON.stringify(collectPayload())');
  const generation = run('state.generation');
  run('PanelI18n.setLanguage("en")');
  assert.equal(context.document.documentElement.lang, 'en');
  assert.equal(node('#content').getAttribute('placeholder'), 'Message text');
  assert.equal(node('#submit-message').textContent, 'Save changes');
  assert.equal(node('#form-status').textContent, 'Sending…');
  assert.equal(run('JSON.stringify(collectPayload())'), draft);
  assert.equal(run('state.editingId'), 42);
  assert.equal(run('state.generation'), generation);
  assert.equal(node('#bot-select').value, 'perimeter');
  assert.equal(run('t("Uploading {name}…", {name:"<script>file</script>"})'), 'Uploading <script>file</script>…');
  let finishResponse;
  context.fetch = (url, options) => {
    assert.equal(options.headers['Accept-Language'], 'en');
    return new Promise(resolve => { finishResponse = resolve; });
  };
  const oldLanguageRequest = run('api("/api/messages")');
  run('PanelI18n.setLanguage("uk")');
  finishResponse({ok:false, status:400, headers:{get:()=> 'application/json'}, json:async()=>({detail:'Upload 123456789012345678 was not found'})});
  await assert.rejects(oldLanguageRequest, /Завантаження 123456789012345678 не знайдено/);
  assert.equal(node('#form-status').textContent, 'Надсилання…');
  assert.equal(run('JSON.stringify(collectPayload())'), draft);
  assert.equal(run('apiError({detail:[{loc:["body","content"],msg:"Message content is limited to 2000 characters"}]},422)'), 'content: Текст повідомлення обмежено 2000 символами');
  run('PanelI18n.setLanguage("en"); PanelI18n.setLanguage("ru")');
  assert.equal(run('PanelI18n.language()'), 'en');
  const reopened = vm.createContext({document: context.document, localStorage: context.localStorage});
  vm.runInContext(fs.readFileSync('app/static/i18n.js', 'utf8'), reopened);
  assert.equal(vm.runInContext('PanelI18n.language()', reopened), 'en');
  const noStorage = vm.createContext({document: context.document, localStorage: {getItem() {throw new Error('blocked');}}});
  vm.runInContext(fs.readFileSync('app/static/i18n.js', 'utf8'), noStorage);
  assert.equal(vm.runInContext('PanelI18n.language()', noStorage), 'uk');
  run('state.botId = null; state.auth = {mode:"local", owner:true, authenticated:true};');
  let accountURL;
  context.fetch = async url => { accountURL = url; return {ok:true, status:200, headers:{get:()=> 'application/json'}, json:async()=>({mode:"discord",authenticated:true,owner:false,user:{id:"123",display_name:"User"},csrf_token:"test-csrf"})}; };
  run('loadBots = async () => {}');
  await run('initializeAccount()');
  assert.equal(accountURL, '/api/auth/me');
  assert.equal(run('state.auth.csrf_token'), 'test-csrf');
  assert.equal(run('scopedUrl("/api/admin/users")'), '/api/admin/users');
  run('state.botId = "perimeter"; state.bots = [{id:"perimeter", name:"Perimeter", access_role:"viewer"}]; applyAccessUI()');
  assert.equal(run('canWriteBot()'), false);
  assert.equal(node('#submit-message').disabled, true);
  assert.equal(node('#import-submit').disabled, true);
  assert.equal(node('#bot-edit').classList.contains('hidden'), true);
  assert.equal(node('#users-tab').classList.contains('hidden'), true);
  run('state.bots[0].access_role = "operator"; applyAccessUI()');
  assert.equal(node('#submit-message').disabled, false);
  assert.equal(node('#bot-edit').classList.contains('hidden'), true);
  context.fetch = async (url, options) => {
    assert.equal(url, '/api/bots/perimeter/messages');
    assert.equal(options.headers['X-CSRF-Token'], 'test-csrf');
    return {ok:true, headers:{get:()=> 'application/json'}, json:async()=>({ok:true})};
  };
  await run('api("/api/messages", {method:"POST",body:"{}"})');
  run('state.auth.owner = true; applyAccessUI()');
  assert.equal(node('#users-tab').classList.contains('hidden'), false);
  assert.equal(node('#bot-edit').classList.contains('hidden'), false);
  let redirected;
  context.window = {location: {assign: url => { redirected = url; }}};
  context.fetch = async () => ({ok:true,headers:{get:()=> 'application/json'},json:async()=>({mode:"discord",authenticated:false,owner:false})});
  await run('initializeAccount()');
  assert.equal(redirected, '/login');
  const originalGetElement = context.document.getElementById;
  context.document.getElementById = id => id === 'panel-config' ? {textContent:JSON.stringify({base_path:'/panel'})} : originalGetElement(id);
  vm.runInContext(fs.readFileSync('app/static/urls.js','utf8'),context);
  run('state.botId="perimeter"; state.auth={mode:"discord",owner:false,authenticated:false}');
  assert.equal(run('scopedUrl("/api/auth/me")'),'/panel/api/auth/me');
  assert.equal(run('scopedUrl("/api/admin/users")'),'/panel/api/admin/users');
  assert.equal(run('scopedUrl("/api/messages")'),'/panel/api/bots/perimeter/messages');
  assert.equal(run('scopedUrl("/api/bots/perimeter/reconnect")'),'/panel/api/bots/perimeter/reconnect');
  assert.throws(()=>run('PanelURLs.path("//evil.example")'));
  context.fetch=async()=>({ok:false,status:401,headers:{get:()=> 'application/json'},json:async()=>({detail:'Sign in to continue'})});
  await assert.rejects(run('api("/api/messages")'),error=>error.constructor.name==='StaleRequest');
  assert.equal(redirected,'/panel/login');
  vm.runInContext(fs.readFileSync('app/static/login.js','utf8'),context);
  assert.equal(node('#discord-login').href,'/panel/auth/discord/login?lang=en');
  console.log('Frontend: profiles, UK/EN, draft preservation, roles, CSRF and /panel navigation passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
