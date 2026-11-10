/**
 * BRIDGE 0.0.9 侧栏主界面（CARD-09b）。
 *
 * 只有这一份界面：点工具栏图标 → background.js 把面板行为设成 openPanelOnActionClick，
 * 直接开 sidepanel.html。没有 popup，所以不存在两个页面同时轮询、互相覆盖状态。
 *
 * 边界（卡面第 4、6 条）：
 * - token 只放 chrome.storage.local，启动时 setAccessLevel 收成 TRUSTED_CONTEXTS；
 *   不写日志、不进 URL、不回显在页面上。
 * - /health 3 秒超时 → 顶部红字「后端离线」；401 → 红字「请重新配对」。
 * - job_id 存 storage.local（带创建时间，24h 清理）；重开面板先跑一轮 /jobs/<id>。
 * - 轮询用 PollScheduler：单例定时器，连点/重开都不叠加。
 */
import { BridgeError, JOB_TTL_MS, ask, endpoints as fetchEndpoints, health, job as fetchJob, moveCard, workflow as fetchWorkflow } from "./bridge-api.js";
import { PollScheduler } from "./poll-scheduler.js";

const DRAFT_KEY = "bridge.v009.interfaceDraft";
const TOKEN_KEY = "bridge.v009.token";
const JOBS_KEY = "bridge.v009.jobs";
/* T4-2 F4：工作流卡片。**测试模式默认开**——默认安全侧：就算验收方忘了切换，
   也只会动 progress-test.json，碰不到正式 progress.json。 */
const TEST_KEY = "bridge.v009.testMode";
const WORKFLOW_KEY = "bridge.v009.workflow";
const ERRORS_KEY = "bridge.v009.errors";
const WORKFLOW_TEST_FILE = "progress-test.json";
/* 只用来给「推进」按钮选一个默认目标；合法与否由服务端状态机裁决（非法迁移返回 409）。
   这份表必须与 docs/workflow/workflow.json 的 transitions 首项一致，测试会校验。 */
const WORKFLOW_NEXT = {
  draft: "sent", sent: "running", running: "done", done: "verifying",
  failed: "sent", stuck: "sent", verifying: "pass", block: null,
  hold: "verifying", pass: null,
};
const ACTIVE_INTERVAL_MS = 3000;
const IDLE_INTERVAL_MS = 20000;
const PROMPT_PREVIEW = 80;
const FOLLOW = "跟随当前设置";
const PROVIDERS = {
  chatgpt: { name: "ChatGPT", models: ["GPT-5", "GPT-5 Thinking"] },
  gemini: { name: "Gemini", models: ["Gemini Pro", "Gemini Flash"] },
  deepseek: { name: "DeepSeek", models: ["DeepSeek", "DeepSeek Reasoner"] },
  grok: { name: "Grok", models: ["Grok", "Grok Fast"] },
  codex: { name: "Codex", models: ["GPT-5 Codex", "GPT-5"] },
  opencode: { name: "OpenCode", models: ["自定义模型 A", "自定义模型 B"] },
  workbuddy: { name: "WorkBuddy", models: ["自定义模型 A", "自定义模型 B"] },
  dsh: { name: "DSH", models: ["自定义模型 A", "自定义模型 B"] },
};
const RUN_MODES = [FOLLOW, "标准", "思考", "研究"];
const EFFORTS = [FOLLOW, "低", "中", "高", "极高"];
const ROLE_NAMES = { summary: "结论汇总", builder: "施工方", reviewer: "验收方" };
const PURPOSES = [
  ["test", "测试", "purpose-test"],
  ["review", "会审", "purpose-review"],
  ["accept", "验收", "purpose-accept"],
];
const TERMINAL = new Set(["done", "partial", "failed"]);
const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 小工具 */
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(className, text) {
  const element = node("button", className, text);
  element.type = "button";
  return element;
}

function logo(provider, className) {
  const image = node("img", className);
  image.src = `assets/${provider}.svg`;
  image.alt = "";
  image.draggable = false;
  image.width = 28;
  image.height = 28;
  return image;
}

function id() {
  return globalThis.crypto?.randomUUID?.() || `seat-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function config(provider) {
  return { provider, model: FOLLOW, mode: FOLLOW, effort: FOLLOW };
}

function defaults() {
  return {
    version: 1,
    mode: "discuss",
    purpose: "review",
    prompt: "",
    seats: [
      { ...config("chatgpt"), id: "seat-chatgpt", selected: true, stance: "pro" },
      { ...config("gemini"), id: "seat-gemini", selected: true, stance: "con" },
      { ...config("deepseek"), id: "seat-deepseek", selected: true, stance: "pro" },
      { ...config("grok"), id: "seat-grok", selected: false, stance: "con" },
    ],
    roles: { summary: config("gemini"), builder: config("codex"), reviewer: config("opencode") },
  };
}

function cleanConfig(value, fallback) {
  const source = value && typeof value === "object" ? value : {};
  const provider = Object.hasOwn(PROVIDERS, source.provider) ? source.provider : fallback;
  return {
    provider,
    model: typeof source.model === "string" && source.model.trim() ? source.model.trim().slice(0, 120) : FOLLOW,
    mode: RUN_MODES.includes(source.mode) ? source.mode : FOLLOW,
    effort: EFFORTS.includes(source.effort) ? source.effort : FOLLOW,
  };
}

function restore(value) {
  const result = defaults();
  if (!value || value.version !== 1) return result;
  result.mode = value.mode === "debate" ? "debate" : "discuss";
  result.purpose = PURPOSES.some(([name]) => name === value.purpose) ? value.purpose : "review";
  result.prompt = typeof value.prompt === "string" ? value.prompt : "";
  if (Array.isArray(value.seats) && value.seats.length) {
    const seen = new Set();
    result.seats = value.seats.filter((seat) => seat && Object.hasOwn(PROVIDERS, seat.provider)).map((seat) => {
      let seatId = typeof seat.id === "string" && seat.id ? seat.id : id();
      if (seen.has(seatId)) seatId = id();
      seen.add(seatId);
      return { ...cleanConfig(seat, seat.provider), id: seatId, selected: seat.selected === true, stance: seat.stance === "con" ? "con" : "pro" };
    });
    if (!result.seats.length) result.seats = defaults().seats;
  }
  for (const [role, initial] of Object.entries(result.roles)) {
    result.roles[role] = cleanConfig(value.roles?.[role], initial.provider);
  }
  return result;
}

/* ------------------------------------------------------------------ 状态 */
let state = defaults();
let attachments = [];
let activeTab = "summary";
let dialogDraft = null;
let toastTimer;
let saveVersion = 0;
let lastSavedVersion = 0;
let token = "";
let allowedEndpoints = null;      // null = 还没读到 /endpoints
let backendOnline = false;
let submitting = false;
let trackedJobs = [];             // [{job_id, created_at, purpose, endpoints, prompt, status, lost}]
let selectedJobId = null;
let tickBusy = false;
const jobCache = new Map();       // job_id -> 后端完整 job 记录
let testMode = true;              // T4-2 F4：默认只读写 progress-test.json
let workflowMirror = null;        // {file, data, mtime} —— mtime 是乐观锁的依据
let workflowBusy = false;
const errorSeen = new Map();      // T4-2 F5：错误去重键 -> 次数
const fetchedJobs = new Set();    // 终态 job 只补拉一次，避免每 3 秒放大请求

/* ------------------------------------------------------------------ 存储 */
async function lockStorage() {
  const api = globalThis.chrome?.storage?.local?.setAccessLevel;
  if (typeof api !== "function") return;
  try {
    await api.call(chrome.storage.local, { accessLevel: "TRUSTED_CONTEXTS" });
  } catch {
    /* 老版本 Chrome 没有这个接口；不阻断界面。 */
  }
}

async function readLocal(key) {
  const bag = await chrome.storage.local.get([key]);
  return bag ? bag[key] : undefined;
}

async function writeLocal(key, value) {
  await chrome.storage.local.set({ [key]: value });
}

async function removeLocal(key) {
  await chrome.storage.local.remove(key);
}

/* ------------------------------------------------------------------ 提示 */
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  $("toast").classList.add("is-visible");
  toastTimer = setTimeout(() => {
    $("toast").classList.remove("is-visible");
    $("toast").hidden = true;
  }, 6200);
}

function setPairStatus(text, tone) {
  const element = $("pair-status");
  element.textContent = text;
  element.dataset.tone = tone || "quiet";
}

function setBackend(stateName, text) {
  backendOnline = stateName === "online";
  const badge = $("backend-badge");
  badge.dataset.state = stateName;
  $("backend-text").textContent = text;
}

/* ------------------------------------------------------------------ 草稿 */
function saveDraft() {
  const revision = ++saveVersion;
  const snapshot = JSON.parse(JSON.stringify(state));
  $("draft-status").textContent = "正在保存草稿…";
  $("draft-status").dataset.status = "saving";
  let write;
  try {
    write = chrome.storage.local.set({ [DRAFT_KEY]: snapshot });
  } catch (error) {
    write = Promise.reject(error);
  }
  Promise.resolve(write).then(() => {
    lastSavedVersion = Math.max(lastSavedVersion, revision);
    if (revision !== saveVersion) return;
    $("draft-status").textContent = "草稿已保存在本机";
    $("draft-status").dataset.status = "saved";
  }).catch(() => {
    if (revision !== saveVersion) return;
    $("draft-status").textContent = "草稿保存失败";
    $("draft-status").dataset.status = "error";
    toast("本地保存失败。当前内容仍在页面中，请暂时不要关闭面板。");
  });
}

/* ------------------------------------------------------------------ 席位 */
function shortSetting(value) {
  return value === FOLLOW ? "跟随当前" : value;
}

function fullConfig(item) {
  return `模型：${item.model}；模式：${item.mode}；思考强度：${item.effort}`;
}

function seatName(seat) {
  const same = state.seats.filter((item) => item.provider === seat.provider);
  const suffix = same.length > 1 ? ` · ${same.findIndex((item) => item.id === seat.id) + 1}` : "";
  return PROVIDERS[seat.provider].name + suffix;
}

function renderSeats() {
  const track = $("seat-track");
  const fragment = document.createDocumentFragment();
  for (const seat of state.seats) {
    const card = node("div", `seat-card${seat.selected ? " is-selected" : ""}`);
    card.dataset.seatId = seat.id;
    const toggle = button("seat-toggle");
    toggle.setAttribute("aria-pressed", String(seat.selected));
    toggle.setAttribute("aria-label", `${seat.selected ? "取消选择" : "选择"}${seatName(seat)}席位`);
    toggle.title = fullConfig(seat);
    toggle.append(logo(seat.provider, "seat-logo"));
    const content = node("span", "seat-content");
    const headline = node("span", "seat-head");
    headline.append(node("span", "seat-name", seatName(seat)));
    const stance = !seat.selected ? "＋加入" : state.mode === "debate" ? (seat.stance === "pro" ? "正方" : "反方") : "已加入";
    const stanceClass = !seat.selected ? " inactive" : state.mode === "debate" ? ` ${seat.stance}` : "";
    headline.append(node("span", `seat-role${stanceClass}`, stance));
    const summary = node("span", "seat-summary");
    summary.append(node("span", "seat-model", seat.model));
    const extras = [seat.mode, seat.effort].filter((value) => value !== FOLLOW);
    if (extras.length) summary.append(node("span", "seat-meta", ` · ${extras.join(" / ")}`));
    content.append(headline, summary);
    toggle.append(content);
    toggle.addEventListener("click", () => {
      seat.selected = !seat.selected;
      renderSeats();
      saveDraft();
      const nextCard = [...track.children].find((element) => element.dataset.seatId === seat.id);
      nextCard?.querySelector(".seat-toggle")?.focus({ preventScroll: true });
    });
    const settings = button("seat-settings", "···");
    settings.setAttribute("aria-label", `设置${seatName(seat)}席位`);
    settings.title = `设置${seatName(seat)}`;
    settings.addEventListener("click", () => openDialog("seat", seat.id));
    card.append(toggle, settings);
    fragment.append(card);
  }
  const oldScroll = track.scrollLeft;
  track.replaceChildren(fragment);
  track.scrollLeft = oldScroll;
  $("selected-count").textContent = `已选 ${state.seats.filter((seat) => seat.selected).length} / ${state.seats.length} 席`;
  requestAnimationFrame(updateScrollButtons);
}

function renderRole(role, elementId) {
  const item = state.roles[role];
  const element = $(elementId);
  element.title = fullConfig(item);
  element.setAttribute("aria-label", `设置${ROLE_NAMES[role]}：${PROVIDERS[item.provider].name}`);
  const copy = node("span", "role-copy");
  copy.append(node("span", "role-name", PROVIDERS[item.provider].name));
  const detail = [item.model, ...[item.mode, item.effort].filter((value) => value !== FOLLOW)].join(" · ");
  copy.append(node("span", "role-detail", detail));
  const chevron = node("span", "chevron", "⌄");
  chevron.setAttribute("aria-hidden", "true");
  element.replaceChildren(logo(item.provider, "role-logo"), copy, chevron);
}

function renderRoles() {
  renderRole("summary", "summary-select");
  renderRole("builder", "builder-select");
  renderRole("reviewer", "reviewer-select");
}

function renderMode() {
  for (const mode of ["discuss", "debate"]) {
    const element = $(`mode-${mode}`);
    const active = state.mode === mode;
    element.classList.toggle("is-active", active);
    element.setAttribute("aria-pressed", String(active));
  }
  for (const [name, , elementId] of PURPOSES) {
    const element = $(elementId);
    const active = state.purpose === name;
    element.classList.toggle("is-active", active);
    element.setAttribute("aria-pressed", String(active));
  }
}

function updateScrollButtons() {
  const track = $("seat-track");
  $("seat-prev").disabled = track.scrollLeft <= 2;
  $("seat-next").disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 2;
}

/* ------------------------------------------------------------------ 设置弹窗 */
function addField(parent, labelText, control, hint) {
  const field = node("div", "field");
  const label = node("label", "field-label", labelText);
  label.htmlFor = control.id;
  field.append(label, control);
  if (hint) {
    const description = node("p", "field-hint", hint);
    description.id = `${control.id}-hint`;
    control.setAttribute("aria-describedby", description.id);
    field.append(description);
  }
  parent.append(field);
  return field;
}

function selectControl(name, choices, value, change) {
  const select = node("select", "field-control");
  select.id = `config-${name}`;
  for (const choice of choices) {
    const option = node("option", "", choice);
    option.value = choice;
    select.append(option);
  }
  select.value = value;
  select.addEventListener("change", () => change(select.value));
  return select;
}

function renderDialog() {
  const { kind, target, value } = dialogDraft;
  const content = $("dialog-content");
  content.replaceChildren();
  $("dialog-title").textContent = kind === "add" ? "增加会审席位" : kind === "seat" ? `设置 ${seatName(state.seats.find((seat) => seat.id === target))}` : `设置${ROLE_NAMES[target]}`;
  $("dialog-subtitle").textContent = "配置与草稿只保存在本机；模型与强度是否真的生效，按后端档位表与回执为准。";
  $("dialog-save").textContent = kind === "add" ? "添加席位" : "保存配置";

  if (kind !== "seat") {
    const label = node("p", "field-label", kind === "add" ? "选择 AI" : "选择负责的 AI");
    label.id = "provider-label";
    const grid = node("div", "provider-grid");
    grid.setAttribute("role", "group");
    grid.setAttribute("aria-labelledby", label.id);
    for (const [provider, detail] of Object.entries(PROVIDERS)) {
      const option = button(`provider-option${value.provider === provider ? " is-active" : ""}`);
      option.setAttribute("aria-pressed", String(value.provider === provider));
      option.append(logo(provider, "role-logo"), node("span", "provider-name", detail.name));
      option.addEventListener("click", () => {
        if (value.provider === provider) return;
        Object.assign(value, config(provider));
        renderDialog();
        content.querySelector(".provider-option[aria-pressed=\"true\"]")?.focus();
      });
      grid.append(option);
    }
    content.append(label, grid);
  }

  const model = node("input", "field-control");
  model.id = "config-model";
  model.type = "text";
  model.maxLength = 120;
  model.autocomplete = "off";
  model.value = value.model;
  model.placeholder = "跟随当前设置，或填写模型名称";
  model.setAttribute("list", "model-suggestions");
  model.addEventListener("input", () => { value.model = model.value; });
  const modelField = addField(content, "模型", model, "可直接填写。下拉候选仅为界面示例，以 docs\\tiers.json 登记为准。");
  const list = node("datalist");
  list.id = "model-suggestions";
  for (const suggestion of [FOLLOW, ...PROVIDERS[value.provider].models]) {
    const option = node("option");
    option.value = suggestion;
    list.append(option);
  }
  modelField.append(list);
  addField(content, "运行模式", selectControl("mode", RUN_MODES, value.mode, (next) => { value.mode = next; }), "选项为界面示意；是否支持以接入后的平台能力为准。");
  addField(content, "思考强度", selectControl("effort", EFFORTS, value.effort, (next) => { value.effort = next; }), "不会自动提高强度或替换模型；默认跟随当前设置。");

  if (kind === "seat" || kind === "add") {
    const field = node("div", "field");
    const label = node("p", "field-label", "辩论立场");
    label.id = "stance-label";
    const choices = node("div", "choice-grid");
    choices.setAttribute("role", "group");
    choices.setAttribute("aria-labelledby", label.id);
    for (const [stance, name] of [["pro", "正方"], ["con", "反方"]]) {
      const choice = button(`choice-button${value.stance === stance ? " is-active" : ""}`, name);
      choice.dataset.stance = stance;
      choice.setAttribute("aria-pressed", String(value.stance === stance));
      choice.addEventListener("click", () => {
        value.stance = stance;
        for (const item of choices.children) {
          const selected = item.dataset.stance === stance;
          item.classList.toggle("is-active", selected);
          item.setAttribute("aria-pressed", String(selected));
        }
      });
      choices.append(choice);
    }
    field.append(label, choices, node("p", "field-hint", "仅在“正反辩论”时使用；普通会审按独立评审处理。"));
    content.append(field);
  } else {
    content.append(node("p", "dialog-note", "此角色的配置独立于会审席位；选择相同 AI 也不会覆盖其他角色的设置。"));
  }

  if (kind === "seat" && state.seats.length > 1) {
    const remove = button("remove-seat-button", "移除此席位");
    remove.addEventListener("click", () => {
      state.seats = state.seats.filter((seat) => seat.id !== target);
      $("config-dialog").close();
      renderSeats();
      saveDraft();
      toast("席位已移除，可随时重新添加。");
    });
    content.append(remove);
  }
}

function openDialog(kind, target) {
  const value = kind === "seat" ? state.seats.find((seat) => seat.id === target) : kind === "add" ? { ...config("chatgpt"), selected: true, stance: "pro" } : state.roles[target];
  if (!value) return;
  dialogDraft = { kind, target, value: { ...value } };
  renderDialog();
  $("config-dialog").showModal();
}

function saveDialog(event) {
  event.preventDefault();
  if (!dialogDraft) return;
  const { kind, target, value } = dialogDraft;
  const next = cleanConfig(value, value.provider);
  if (kind === "seat") {
    const seat = state.seats.find((item) => item.id === target);
    if (seat) Object.assign(seat, next, { stance: value.stance });
  } else if (kind === "add") {
    state.seats.push({ ...next, id: id(), selected: true, stance: value.stance });
  } else {
    state.roles[target] = next;
  }
  $("config-dialog").close();
  renderSeats();
  renderRoles();
  saveDraft();
  if (kind === "add") requestAnimationFrame(() => $("seat-track").scrollTo({ left: $("seat-track").scrollWidth, behavior: "smooth" }));
  toast(kind === "add" ? "席位已添加。" : "配置已保存。");
}

/* ------------------------------------------------------------------ 附件 */
function fileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachments() {
  const list = $("attachment-list");
  list.replaceChildren();
  for (const attachment of attachments) {
    const chip = node("span", "attachment-chip");
    const label = node("span", "attachment-name", attachment.name);
    label.title = attachment.name;
    const remove = button("attachment-remove", "×");
    remove.setAttribute("aria-label", `移除附件 ${attachment.name}`);
    remove.addEventListener("click", () => {
      attachments = attachments.filter((item) => item.id !== attachment.id);
      renderAttachments();
    });
    chip.append(label, node("span", "attachment-size", fileSize(attachment.size)), remove);
    list.append(chip);
  }
  if (attachments.length) list.append(node("p", "field-hint", "仅暂存文件名和大小，未读取或上传内容；关闭面板后清除。"));
}

/* ------------------------------------------------------------------ 后端状态 */
async function refreshHealth() {
  try {
    const data = await health();
    setBackend("online", `后端在线 ${String(data.version || "").trim()}`.trim());
    return true;
  } catch (error) {
    const reason = error instanceof BridgeError && error.code === "timeout" ? "3 秒无响应" : "连不上";
    setBackend("offline", `后端离线（${reason}）`);
    return false;
  }
}

function needPair(message) {
  $("pair-panel").hidden = false;               // 红字要看得见，别停在被折叠的面板里
  $("settings-toggle").setAttribute("aria-expanded", "true");
  setPairStatus(message, "error");
}

async function loadEndpoints() {
  if (!token) {
    allowedEndpoints = null;
    renderEndpointList();
    return;
  }
  try {
    const data = await fetchEndpoints(token);
    allowedEndpoints = (data.endpoints || []).map((row) => String(row.name));
    renderEndpointList();
  } catch (error) {
    if (error instanceof BridgeError && error.kind === "unauthorized") {
      needPair("请重新配对：token 无效或已被 rotate。");
    }
    allowedEndpoints = null;
    renderEndpointList();
  }
}

function renderEndpointList() {
  const holder = $("endpoint-list");
  if (!token) {
    holder.replaceChildren(node("span", "quiet", "配对后读取"));
    return;
  }
  if (!allowedEndpoints) {
    holder.replaceChildren(node("span", "quiet", "未读取"));
    return;
  }
  if (!allowedEndpoints.length) {
    holder.replaceChildren(node("span", "quiet", "档位表为空"));
    return;
  }
  holder.replaceChildren(...allowedEndpoints.map((name) => node("span", "endpoint-chip", name)));
}

/* ------------------------------------------------------------------ 任务 */
function jobLabel(status) {
  return {
    queued: "排队中", running: "执行中", done: "已完成", partial: "部分完成",
    failed: "失败", lost: "任务已丢失",
  }[status] || status || "未知";
}

/* 端脚本返回的 stage → 给人看的中文。
   放在这里而不是新开一个文件：静态断言只扫固定那几个 JS 文件，
   新文件等于给自己开一个测试盲区。 */
const STAGE_LABELS = {
  answered: "已回答",
  already_answered: "页面已有本轮回答",
  read: "已读取",
  timeout: "等回复超时",
  interrupted: "收集中断",
  conversation_changed: "会话或页面变了",
  copied_prompt: "复制到的是提问本身",
  draft_present: "输入框已有草稿",
  input_mismatch: "写入的文本和提问不一致",
  session_unproven: "无法证明是同一会话",
  tier_unconfirmed: "档位未确认",
  human_check: "遇到人机验证",
  /* R11：端点拒绝这一轮的三条结构判据（不猜原因，只报看到的事实） */
  submit_refused: "端把提交退回来了（未确认送达）",
  send_unavailable: "发送键一直不可用（一个字都没发出去）",
  accepted_silent: "端收下了但一直没有生成回复",
  /* B14：并发时前台是共享资源，抢不到就干净失败（不是"用户接管"） */
  focus_lost: "目标窗口拿不到前台",
  ui_busy: "端被别的席位占着（下一轮再试）",
  /* 采集停摆的具体原因（原来只显示"等回复超时"，看不出为什么） */
  STREAMING_IN_PROGRESS: "仍在生成中，继续等",
  CONTAINER_BOUNDARY_UNPROVEN: "有文字但证明不了消息边界",
  ATTRIBUTION_UNVERIFIED: "证明不了这是本轮答案",
  SESSION_ID_MISSING: "账本没有会话 id，无法自动恢复",
  REPLY_NEVER_APPEARED: "收下了但一直读不到正文",
  use_timeout: "单端等待超过预算，进程已终止",
  identity_unreadable: "读不出会话标题（不是换了会话）",
};

/* 执行阶段 → 中文（心跳用） */
const PROGRESS_STAGES = {
  dispatching: "已派发", preparing: "准备中", sending: "发送中",
  waiting: "等待回复", copying: "取回答", finished: "已结束",
  blind: "读不到页面", blocked_lock: "等锁",
};

function progressText(item) {
  const beat = item && item.progress;
  if (!beat || !beat.stage) return "";
  const bits = [PROGRESS_STAGES[beat.stage] || beat.stage];
  if (typeof beat.elapsed === "number") bits.push(`${Math.round(beat.elapsed)}s`);
  if (typeof beat.reads === "number") bits.push(`已读 ${beat.reads}`);
  if (beat.busy === true) bits.push("生成中");
  if (beat.accepted === true) bits.push("已收下");
  if (typeof beat.copies === "number") bits.push(`候选 ${beat.copies}`);
  if (beat.lock && beat.lock.owner) bits.push(`等 ${beat.lock.owner} 放锁`);
  return bits.join(" · ");
}

/* 需要人工先处理该端、再决定是否解锁重发的 stage */
const STUCK_STAGES = new Set(["submit_refused"]);

function stageOf(item) {
  if (!item) return null;
  const use = item.use || {};
  /* B11：timeout 只说明"没等到"，真正原因在 detail.error_code 里 */
  if (use.stage === "timeout" && use.detail && use.detail.error_code) {
    return use.detail.error_code;
  }
  if (use.stage) return use.stage;
  return item.error_code || null;
}

/* 服务端把 error 给成对象 {code,message,step,exit_code}，直接塞进 textContent
   会显示成 "[object Object]"。这里按优先级取人话。 */
function errorText(item) {
  const err = item && item.error;
  if (typeof err === "string" && err) return err;
  if (err && typeof err.message === "string" && err.message) return err.message;
  if (item && typeof item.error_message === "string" && item.error_message) return item.error_message;
  if (item && item.use && item.use.error_message) return item.use.error_message;
  return "（没有错误信息）";
}

function isActive(entry) {
  return !entry.lost && !TERMINAL.has(entry.status);
}

function pruneJobs(list) {
  const now = Date.now();
  return list.filter((entry) => typeof entry.created_at === "number" && now - entry.created_at < JOB_TTL_MS);
}

async function persistJobs() {
  trackedJobs = pruneJobs(trackedJobs);
  try {
    await writeLocal(JOBS_KEY, trackedJobs);
  } catch {
    /* 存储满之类的问题不该打断轮询；界面上还能看到当前这轮。 */
  }
}

/* ------------------------------------------------- T4-2 F5：错误去重（单条） */
/**
 * 一次请求生命周期内，同一 (job, 端, 码) 只算一条用户可见错误。
 *
 * 改造前的问题是：`renderOutput` 每次全量 `replaceChildren`，`tick` 每 3 秒跑一轮，
 * 同一个故障会被反复写进 DOM、反复触发 aria-live 播报。施工单要求「恰好一条」。
 *
 * key 里**不含 message 文本**：文案会变，故障不会。
 */
function errorKey(jobId, endpoint, error) {
  const code = (error && (error.code || error.kind)) || "unknown";
  return [jobId || "-", endpoint || "-", code].join("|");
}

function showError(jobId, endpoint, error, target) {
  const key = errorKey(jobId, endpoint, error);
  const seen = (errorSeen.get(key) || 0) + 1;
  errorSeen.set(key, seen);
  const message = (error && (error.code || error.message)) || "未知错误";
  if (target) {
    target.textContent = seen > 1 ? `${endpoint}：${message}（×${seen}）` : `${endpoint}：${message}`;
  }
  return seen;
}

/* ------------------------------------------------- 终态 job 的输出回填 */
/**
 * 已完成（done/partial/failed）的 job，`jobCache` 里没有记录时补拉一次。
 *
 * 不补的后果（改造前的真实缺陷）：重开面板后，任务列表上明明写着"已完成"，
 * 输出区却永远显示「会审结论将在这里汇集」——因为 `tick` 只轮询 active 的 job。
 */
async function ensureJob(entry) {
  if (!entry || !entry.job_id) return;
  if (jobCache.has(entry.job_id)) return;
  if (fetchedJobs.has(entry.job_id)) return;
  if (!isActive(entry) && !entry.lost) fetchedJobs.add(entry.job_id);
  try {
    const data = await fetchJob(token, entry.job_id);
    jobCache.set(entry.job_id, data.job || {});
    renderOutput();
  } catch {
    /* 拉不到就维持空态；下次点击该行再试 */
  }
}

/* ------------------------------------------------- T4-2 F4：工作流卡片 */
/** 当前该读写哪个文件。**只有测试模式才有值** —— progress.json 永久只读。 */
function workflowFile() {
  return testMode ? WORKFLOW_TEST_FILE : null;
}

function canWrite() {
  const file = workflowFile();
  return Boolean(file) && file === WORKFLOW_TEST_FILE;
}

/** 写操作的第一道硬闸：任何分支都不可能拼出 progress.json。 */
function assertWritable(file) {
  if (!canWrite() || file !== WORKFLOW_TEST_FILE) {
    throw new Error("正式 progress.json 只读；请打开测试模式（只写 progress-test.json）");
  }
}

async function loadWorkflow() {
  const file = workflowFile();
  if (!file || !token) return;
  try {
    const data = await fetchWorkflow(token, file);
    const next = { file, data: data.data || {}, mtime: data.mtime };
    workflowMirror = next;
    // 落盘镜像：插件重载 / Service Worker 重启后先渲染旧值，再等这一轮网络刷新。
    try {
      await writeLocal(WORKFLOW_KEY, next);
    } catch {
      /* 存储满之类不该打断读取；界面上还能看到这一轮的值 */
    }
    renderWorkflow();
  } catch (error) {
    if (error instanceof BridgeError && error.kind === "unauthorized") {
      needPair("请重新配对：token 无效或已被 rotate。");
    }
  }
}

async function moveWorkflowCard(card, to) {
  const file = workflowFile();
  if (!file) {
    toast("正式 progress.json 只读：先打开测试模式。");
    return;
  }
  if (!to) {
    toast("这张卡已经到终态了，没有可推进的下一步。");
    return;
  }
  assertWritable(file);
  if (workflowBusy) return;
  workflowBusy = true;
  try {
    // 乐观锁：写之前先比对 mtime。外部改过就刷新并让用户重看一眼，
    // 绝不拿旧状态盖掉别人的新状态。
    const fresh = await fetchWorkflow(token, file);
    if (workflowMirror && fresh.mtime !== workflowMirror.mtime) {
      workflowMirror = { file, data: fresh.data || {}, mtime: fresh.mtime };
      renderWorkflow();
      toast("progress-test.json 已被外部修改，已刷新；请确认后再点一次。");
      return;
    }
    await moveCard(token, { file, card, to });
    await loadWorkflow();
    toast(`卡片 ${card} → ${to}`);
  } catch (error) {
    if (!(error instanceof BridgeError)) {
      $("workflow-status").textContent = "不可写：请先打开测试模式";
      return;
    }
    if (error.kind === "unauthorized") {
      needPair("请重新配对：token 无效或已被 rotate。");
    } else if (error.kind === "offline") {
      setBackend("offline", "后端离线（连不上）");
    } else {
      toast(`工作流：${error.code || error.message}`);
    }
  } finally {
    workflowBusy = false;
  }
}

function renderWorkflow() {
  const mode = $("workflow-mode");
  if (mode) {
    mode.textContent = testMode ? "测试模式：开" : "测试模式：关（只读）";
    mode.setAttribute("aria-pressed", String(testMode));
  }
  const holder = $("workflow-list");
  const status = $("workflow-status");
  const file = workflowFile();
  if (!file) {
    if (status) status.textContent = "只读：正式 progress.json 不允许面板写入";
    if (holder) holder.replaceChildren(node("p", "field-hint", "需要改卡片时打开测试模式（只写 progress-test.json）。"));
    return;
  }
  if (!workflowMirror || workflowMirror.file !== file) {
    if (status) status.textContent = "未连接";
    if (holder) holder.replaceChildren(node("p", "field-hint", "还没有读到 progress-test.json。"));
    return;
  }
  const cards = (workflowMirror.data.workflow || {}).cards || {};
  const names = Object.keys(cards).sort();
  if (status) status.textContent = names.length ? `${names.length} 张 · ${file}` : `空 · ${file}`;
  if (!holder) return;
  if (!names.length) {
    holder.replaceChildren(node("p", "field-hint", "progress-test.json 里还没有卡片。"));
    return;
  }
  holder.replaceChildren(...names.map((name) => {
    const state = String((cards[name] || {}).state || "draft");
    const row = node("div", "workflow-row");
    row.setAttribute("role", "listitem");
    row.append(node("span", "workflow-card", name));
    row.append(node("span", `workflow-state is-${state}`, state));
    const next = WORKFLOW_NEXT[state];
    const advance = button("subtle-button", next ? `推进 → ${next}` : "终态");
    advance.type = "button";
    advance.disabled = !next;
    advance.addEventListener("click", () => { void moveWorkflowCard(name, next); });
    row.append(advance);
    return row;
  }));
}

function timeText(ms) {
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderJobs() {
  const holder = $("jobs-list");
  $("jobs-count").textContent = `${trackedJobs.length} 个`;
  $("jobs-empty").hidden = trackedJobs.length > 0;
  const rows = trackedJobs.slice().sort((a, b) => b.created_at - a.created_at).map((entry) => {
    const row = node("button", `job-row${entry.job_id === selectedJobId ? " is-active" : ""}`);
    row.type = "button";
    row.dataset.jobId = entry.job_id;
    row.setAttribute("role", "listitem");
    row.setAttribute("aria-pressed", String(entry.job_id === selectedJobId));
    const head = node("span", "job-head");
    head.append(node("span", `job-dot is-${entry.status || "queued"}`));
    head.append(node("span", "job-id", String(entry.job_id).slice(-13)));
    head.append(node("span", "job-status", jobLabel(entry.status)));
    head.append(node("span", "job-time", timeText(entry.created_at)));
    const meta = node("span", "job-meta");
    meta.textContent = `${entry.purpose} · ${(entry.endpoints || []).join(" / ")}`;
    if (entry.prompt) row.title = entry.prompt;
    row.append(head, meta);
    row.addEventListener("click", () => {
      selectedJobId = entry.job_id;
      renderJobs();
      renderOutput();
    });
    return row;
  });
  holder.replaceChildren(...rows);
}

async function pollJob(entry) {
  try {
    const data = await fetchJob(token, entry.job_id);
    const record = data.job || {};
    jobCache.set(entry.job_id, record);
    entry.status = record.status || entry.status;
    entry.lost = false;
  } catch (error) {
    if (!(error instanceof BridgeError)) return;
    if (error.kind === "not_found") {
      entry.lost = true;
      entry.status = "lost";
      if (entry.job_id === selectedJobId) $("result-status").textContent = "任务已丢失";
    } else if (error.kind === "unauthorized") {
      // 每 3 秒重来一轮，绝不能让同一句「请重新配对」反复播报。
      if (showError(entry.job_id, "认证", error) === 1) {
        needPair("请重新配对：token 无效或已被 rotate。");
      }
    } else if (error.kind === "offline") {
      if (showError(entry.job_id, "后端", error) === 1) {
        setBackend("offline", "后端离线（连不上）");
      }
    } else {
      // T4-2 F5：`bad_request` / `server` 原来在这里被**静默吞掉**，
      // 面板上一点反馈都没有（F5 场景会表现为"面板卡死")。
      if (entry.job_id === selectedJobId) {
        showError(entry.job_id, "后端", error, $("result-status"));
      } else {
        showError(entry.job_id, "后端", error);
      }
    }
  }
}

async function tick() {
  if (tickBusy) return;                      // 手动「立即刷新」和服务端定时器不叠着跑
  tickBusy = true;
  try {
    const online = await refreshHealth();
    const active = trackedJobs.filter(isActive);
    if (online && token) {
      for (const entry of active) await pollJob(entry);
      // T4-2 F4：外部改了 progress-test.json 要 ≤10s 反映到面板。
      // 复用现有 3 秒轮询，不新增定时器（也不许新增 setInterval）。
      await loadWorkflow();
      // 已完成的 job 只补拉一次，否则重开面板永远是空白。
      const selected = trackedJobs.find((item) => item.job_id === selectedJobId);
      if (selected) await ensureJob(selected);
      await persistJobs();
      renderJobs();
      renderOutput();
    }
    poll.interval = active.length ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
  } finally {
    tickBusy = false;
  }
}

const poll = new PollScheduler(tick, { interval: IDLE_INTERVAL_MS });

/* ------------------------------------------------------------------ 输出 */
function renderOutput() {
  const copy = {
    summary: ["会审结论将在这里汇集", "保留各方意见与未解决的分歧"],
    opinions: ["各方意见将在这里呈现", "按席位查看完整回答与对应配置"],
    disagreements: ["让不同观点清晰可见", "汇集争议、依据与需要你决定的事项"],
  };
  for (const tab of Object.keys(copy)) {
    const element = $(`tab-${tab}`);
    const active = activeTab === tab;
    element.classList.toggle("is-active", active);
    element.setAttribute("aria-selected", String(active));
    element.tabIndex = active ? 0 : -1;
  }
  $("output-panel").setAttribute("aria-labelledby", `tab-${activeTab}`);

  const entry = trackedJobs.find((item) => item.job_id === selectedJobId);
  const record = selectedJobId ? jobCache.get(selectedJobId) : null;
  const empty = !entry || !record;
  $("output-icon").hidden = !empty;
  $("output-empty").hidden = !empty;
  $("output-body").hidden = empty;
  $("output-provider").textContent = entry ? `用途 ${entry.purpose}` : "";
  if (empty) {
    $("output-title").textContent = entry && entry.lost ? "任务已丢失" : copy[activeTab][0];
    $("output-description").textContent = entry && entry.lost
      ? "后端重启后没有这个 job：重新提交一次即可。"
      : copy[activeTab][1];
    $("output-body").replaceChildren();
    return;
  }
  $("output-title").textContent = copy[activeTab][0];
  $("output-description").textContent = copy[activeTab][1];
  $("output-body").replaceChildren(...outputRows(record, activeTab));
}

function outputRows(record, tab) {
  const names = Object.keys(record.endpoints || {});
  const rows = [];
  if (tab === "summary") {
    const done = names.filter((name) => record.endpoints[name].status === "done").length;
    const line = record.quorum
      ? `状态 ${jobLabel(record.status)} · 已回收 ${done}/${names.length}（线 ${record.quorum.required}）`
      : `状态 ${jobLabel(record.status)} · ${done}/${names.length} 端完成`;
    rows.push(node("p", "output-line", line));
    rows.push(node("p", "output-line quiet", `job ${record.job_id} · 创建 ${record.created_at || ""}`));
    if (record.status === "partial" || record.status === "failed") {
      rows.push(node("p", "output-line error", "有端失败，点「分歧点」看错误码。"));
    }
    return rows;
  }
  if (tab === "opinions") {
    for (const name of names) {
      const item = record.endpoints[name] || {};
      rows.push(node("p", "output-label", `${name} · ${jobLabel(item.status)}`));
      rows.push(node("p", "output-text", item.reply ? String(item.reply).slice(0, 2000) : "（暂无回复）"));
      const beat = progressText(item);
      if (beat) rows.push(node("p", "output-line quiet", beat));
    }
    return rows;
  }
  for (const name of names) {
    const item = record.endpoints[name] || {};
    if (item.status === "done") continue;
    const stage = stageOf(item);
    const label = STAGE_LABELS[stage] || stage || jobLabel(item.status);
    rows.push(node("p", "output-label", `${name} · ${label}`));
    const text = errorText(item);
    rows.push(node("p", "output-text",
      STUCK_STAGES.has(stage)
        ? `${text}。需先处理该端，再用 tools/submission_clear.py 解锁重发。`
        : text));
  }
  if (!rows.length) rows.push(node("p", "output-line quiet", "各端都完成了，没有失败项。"));
  return rows;
}

/* ------------------------------------------------------------------ 提交 */
function selectedEndpoints() {
  const names = [];
  for (const seat of state.seats) {
    if (seat.selected && !names.includes(seat.provider)) names.push(seat.provider);
  }
  return names;
}

async function startAsk() {
  if (submitting) return;
  if (!token) {
    needPair("请先配对：粘贴 server.json 里的 token。");
    $("token-input").focus();
    return;
  }
  if (!state.prompt.trim()) {
    toast("请先输入要会审的任务或问题。");
    $("prompt-input").focus();
    return;
  }
  const names = selectedEndpoints();
  if (!names.length) {
    toast("请至少选择一个会审席位。");
    return;
  }
  if (state.mode === "debate" && (!state.seats.some((seat) => seat.selected && seat.stance === "pro") || !state.seats.some((seat) => seat.selected && seat.stance === "con"))) {
    toast("正反辩论需要正方和反方都至少有一个席位。");
    return;
  }
  if (allowedEndpoints && names.some((name) => !allowedEndpoints.includes(name))) {
    const missing = names.filter((name) => !allowedEndpoints.includes(name));
    toast(`档位表里没有：${missing.join(" / ")}；请去掉这些席位或补 docs\\tiers.json。`);
    return;
  }

  submitting = true;
  $("start-button").disabled = true;
  $("result-status").textContent = "正在提交…";
  try {
    const requestId = id();
    const data = await ask(token, {
      endpoints: names,
      prompt: state.prompt.trim(),
      purpose: state.purpose,
      requestId,
    });
    const entry = {
      job_id: data.job_id,
      created_at: Date.now(),
      purpose: state.purpose,
      endpoints: names,
      prompt: state.prompt.trim().slice(0, PROMPT_PREVIEW),
      status: data.status || "queued",
      lost: false,
      // T4-2：把 request_id 落盘。原来它现生成现丢，重开面板就对不上这次提交了。
      // 只作诊断与重复提交识别，**不做错误去重主键**（一个 request_id 可能对应多端）。
      request_id: requestId,
    };
    trackedJobs = pruneJobs(trackedJobs.filter((item) => item.job_id !== entry.job_id).concat(entry));
    selectedJobId = entry.job_id;
    await persistJobs();
    saveDraft();
    renderJobs();
    $("result-status").textContent = data.duplicate ? "同一个 request_id 已在跑" : "已提交，轮询中";
    poll.interval = ACTIVE_INTERVAL_MS;
    poll.kick();
    toast(`已提交：${data.job_id}`);
  } catch (error) {
    handleSubmitError(error);
  } finally {
    submitting = false;
    $("start-button").disabled = false;
  }
}

function handleSubmitError(error) {
  if (!(error instanceof BridgeError)) {
    $("result-status").textContent = "提交失败";
    toast("提交失败，请重试。");
    return;
  }
  if (error.kind === "unauthorized") {
    needPair("请重新配对：token 无效或已被 rotate。");
    $("result-status").textContent = "未授权";
    return;
  }
  if (error.kind === "offline") {
    setBackend("offline", "后端离线（连不上）");
    $("result-status").textContent = "后端离线";
    toast("后端没有响应：先运行 bridge_server.py start。");
    return;
  }
  $("result-status").textContent = `提交失败：${error.code || error.status}`;
  toast(error.code === "queue_full" ? "队列已满，等前面的任务结束再提交。" : `提交失败：${error.message}`);
}

/* ------------------------------------------------------------------ 配对 */
async function saveToken() {
  const value = $("token-input").value.trim();
  if (!value) {
    toast("请先粘贴 token。");
    return;
  }
  token = value;
  $("token-input").value = "";
  try {
    await writeLocal(TOKEN_KEY, token);
  } catch {
    toast("token 保存失败，请重试。");
    return;
  }
  setPairStatus("已配对：token 只保存在本机 chrome.storage.local。", "ok");
  $("pair-state").textContent = "已配对";
  await Promise.all([loadEndpoints(), refreshHealth()]);
  poll.kick();
}

async function clearToken() {
  token = "";
  allowedEndpoints = null;
  try {
    await removeLocal(TOKEN_KEY);
  } catch {
    /* 清不掉也要把界面上的状态改掉。 */
  }
  setPairStatus("已清除：token 不再保存在本机。", "quiet");
  $("pair-state").textContent = "未配对";
  renderEndpointList();
}

/* ------------------------------------------------------------------ 事件 */
function bindEvents() {
  $("seat-prev").addEventListener("click", () => $("seat-track").scrollBy({ left: -240, behavior: "smooth" }));
  $("seat-next").addEventListener("click", () => $("seat-track").scrollBy({ left: 240, behavior: "smooth" }));
  $("seat-track").addEventListener("scroll", updateScrollButtons, { passive: true });
  const track = $("seat-track");
  let drag = null;
  let suppressClickUntil = 0;
  track.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || event.target.closest(".seat-settings")) return;
    drag = { pointerId: event.pointerId, x: event.clientX, scrollLeft: track.scrollLeft, moved: false };
  });
  track.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId || track.scrollWidth <= track.clientWidth) return;
    const distance = event.clientX - drag.x;
    if (!drag.moved && Math.abs(distance) < 6) return;
    if (!drag.moved) {
      drag.moved = true;
      track.setPointerCapture(event.pointerId);
      track.classList.add("is-dragging");
    }
    event.preventDefault();
    track.scrollLeft = drag.scrollLeft - distance;
  });
  const endDrag = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.moved) suppressClickUntil = Date.now() + 200;
    if (track.hasPointerCapture?.(event.pointerId)) track.releasePointerCapture(event.pointerId);
    drag = null;
    track.classList.remove("is-dragging");
  };
  track.addEventListener("pointerup", endDrag);
  track.addEventListener("pointercancel", endDrag);
  track.addEventListener("lostpointercapture", endDrag);
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);
  track.addEventListener("click", (event) => {
    if (Date.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  window.addEventListener("resize", updateScrollButtons);

  $("add-seat").addEventListener("click", () => openDialog("add"));
  for (const mode of ["discuss", "debate"]) {
    $(`mode-${mode}`).addEventListener("click", () => {
      state.mode = mode;
      renderMode();
      renderSeats();
      saveDraft();
    });
  }
  for (const [name, , elementId] of PURPOSES) {
    $(elementId).addEventListener("click", () => {
      state.purpose = name;
      renderMode();
      saveDraft();
    });
  }
  $("summary-select").addEventListener("click", () => openDialog("role", "summary"));
  $("summary-settings").addEventListener("click", () => openDialog("role", "summary"));
  $("builder-select").addEventListener("click", () => openDialog("role", "builder"));
  $("reviewer-select").addEventListener("click", () => openDialog("role", "reviewer"));

  $("prompt-input").addEventListener("input", (event) => {
    state.prompt = event.target.value;
    saveDraft();
  });
  $("attach-button").addEventListener("click", () => $("attachment-input").click());
  $("attachment-input").addEventListener("change", (event) => {
    for (const file of event.target.files || []) {
      attachments.push({ id: id(), name: file.name, size: file.size });
    }
    event.target.value = "";
    renderAttachments();
  });
  $("start-button").addEventListener("click", startAsk);
  $("refresh-button").addEventListener("click", () => {
    $("result-status").textContent = "正在刷新…";
    void tick().then(() => {
      $("result-status").textContent = trackedJobs.length ? "已刷新" : "尚未提交任务";
    });
  });
  // T4-2 F4：测试模式开关。关掉就等于把面板对工作流的写入能力整个摘掉
  // （workflowFile 返回 null → canWrite 恒 false → 任何写分支都过不去）。
  $("workflow-mode")?.addEventListener("click", () => {
    testMode = !testMode;
    void writeLocal(TEST_KEY, testMode).catch(() => {});
    workflowMirror = testMode ? workflowMirror : null;
    renderWorkflow();
    if (testMode) void loadWorkflow();
    toast(testMode ? "测试模式已开：只读写 progress-test.json" : "测试模式已关：工作流卡片只读");
  });
  $("handoff-button").disabled = true;
  $("handoff-button").title = "交付施工不在本卡范围";

  $("token-save").addEventListener("click", saveToken);
  $("token-clear").addEventListener("click", clearToken);
  $("token-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void saveToken();
    }
  });
  $("settings-toggle").addEventListener("click", () => {
    const panel = $("pair-panel");
    const hidden = panel.hidden;
    panel.hidden = !hidden;
    $("settings-toggle").setAttribute("aria-expanded", String(hidden));
  });

  const tabs = ["summary", "opinions", "disagreements"];
  for (const tab of tabs) {
    $(`tab-${tab}`).addEventListener("click", () => { activeTab = tab; renderOutput(); });
    $(`tab-${tab}`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = tabs.indexOf(activeTab);
      activeTab = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs.at(-1) : tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      renderOutput();
      $(`tab-${activeTab}`).focus();
    });
  }

  $("dialog-form").addEventListener("submit", saveDialog);
  $("dialog-close").addEventListener("click", () => $("config-dialog").close());
  $("dialog-cancel").addEventListener("click", () => $("config-dialog").close());
  $("config-dialog").addEventListener("close", () => { dialogDraft = null; });
  window.addEventListener("pagehide", () => {
    poll.stop();
    if (saveVersion > lastSavedVersion) saveDraft();
  });
  chrome.storage.onChanged?.addListener((changes, area) => {
    if (area !== "local") return;
    // T4-2：开关与工作流镜像也要跨面板同步（原来只监听草稿，其余一律丢弃）。
    if (changes[TEST_KEY]) {
      testMode = changes[TEST_KEY].newValue !== false;
      workflowMirror = null;
      renderWorkflow();
      if (testMode) void loadWorkflow();
    }
    if (changes[WORKFLOW_KEY]?.newValue && typeof changes[WORKFLOW_KEY].newValue === "object") {
      workflowMirror = changes[WORKFLOW_KEY].newValue;
      renderWorkflow();
    }
    if (changes[ERRORS_KEY]) errorSeen.clear();
    if (!changes[DRAFT_KEY]?.newValue || saveVersion > lastSavedVersion) return;
    const next = restore(changes[DRAFT_KEY].newValue);
    if (JSON.stringify(next) === JSON.stringify(state)) return;
    if ($("config-dialog").open) {
      $("config-dialog").close();
      toast("配置已在别处更新，请重新打开设置。");
    }
    state = next;
    if ($("prompt-input").value !== state.prompt) $("prompt-input").value = state.prompt;
    renderMode();
    renderSeats();
    renderRoles();
    $("draft-status").textContent = "已同步本机插件配置";
  });
}

/* ------------------------------------------------------------------ 启动 */
async function initialize() {
  await lockStorage();                       // 先收紧 storage.local 的读写范围
  document.body.classList.add("extension-ready");
  let loadFailed = false;
  try {
    state = restore(await readLocal(DRAFT_KEY));
  } catch {
    loadFailed = true;
  }
  try {
    const stored = await readLocal(TOKEN_KEY);
    token = typeof stored === "string" ? stored : "";
  } catch {
    token = "";
  }
  try {
    const storedJobs = await readLocal(JOBS_KEY);
    trackedJobs = pruneJobs(Array.isArray(storedJobs) ? storedJobs : []);
  } catch {
    trackedJobs = [];
  }
  if (trackedJobs.length) selectedJobId = trackedJobs.slice().sort((a, b) => b.created_at - a.created_at)[0].job_id;

  // T4-2 F4：开关缺省为 true（安全侧）。镜像先渲染旧值，网络刷新随后覆盖。
  try {
    testMode = (await readLocal(TEST_KEY)) !== false;
  } catch {
    testMode = true;
  }
  try {
    const storedWorkflow = await readLocal(WORKFLOW_KEY);
    workflowMirror = storedWorkflow && typeof storedWorkflow === "object" ? storedWorkflow : null;
  } catch {
    workflowMirror = null;
  }
  try {
    const storedErrors = await readLocal(ERRORS_KEY);
    if (Array.isArray(storedErrors)) {
      for (const pair of storedErrors) {
        if (Array.isArray(pair) && pair.length === 2) errorSeen.set(String(pair[0]), Number(pair[1]) || 1);
      }
    }
  } catch {
    /* 诊断数据读不到不影响主流程 */
  }

  $("pair-state").textContent = token ? "已配对" : "未配对";
  if (token) setPairStatus("已配对：token 只保存在本机 chrome.storage.local。", "ok");
  $("prompt-input").value = state.prompt;
  $("draft-status").textContent = loadFailed ? "无法读取本地草稿" : "草稿仅保存在本机";
  bindEvents();
  renderMode();
  renderSeats();
  renderRoles();
  renderAttachments();
  renderEndpointList();
  renderJobs();
  renderOutput();
  renderWorkflow();
  if (loadFailed) toast("读取本地草稿失败，暂时显示默认配置；你可以继续编辑并尝试保存。");

  await Promise.all([loadEndpoints(), refreshHealth()]);
  // T4-2 F4：启动就读一次工作流（不等下一次 tick），否则面板初开是"未连接"。
  await loadWorkflow();
  // 面板重开先补一轮：未完成的 job 立刻恢复，而不是等下一个 3 秒。
  const active = trackedJobs.filter(isActive);
  poll.interval = active.length ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS;
  if (active.length) await tick();
  poll.start();
}

void initialize();
