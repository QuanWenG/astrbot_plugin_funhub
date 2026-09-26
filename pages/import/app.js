/**
 * 数据导入页：上传 → 预览 → 确认 → 报告。
 *
 * 页面运行在受限 iframe 里，拿不到 Dashboard 的 cookie，所有请求都必须经过
 * window.AstrBotPluginPage bridge；endpoint 是插件内相对路径（不含插件名）。
 */

const bridge = window.AstrBotPluginPage;

const el = {
  heading: document.getElementById("heading"),
  lead: document.getElementById("lead"),
  drop: document.getElementById("drop"),
  dropText: document.getElementById("dropText"),
  file: document.getElementById("file"),
  status: document.getElementById("status"),
  previewCard: document.getElementById("previewCard"),
  previewTitle: document.getElementById("previewTitle"),
  previewFile: document.getElementById("previewFile"),
  previewStats: document.getElementById("previewStats"),
  previewIssues: document.getElementById("previewIssues"),
  apply: document.getElementById("apply"),
  cancel: document.getElementById("cancel"),
  reportCard: document.getElementById("reportCard"),
  reportTitle: document.getElementById("reportTitle"),
  reportFile: document.getElementById("reportFile"),
  reportStats: document.getElementById("reportStats"),
  reportIssues: document.getElementById("reportIssues"),
  pendingTitle: document.getElementById("pendingTitle"),
  pending: document.getElementById("pending"),
  refresh: document.getElementById("refresh"),
  lastCard: document.getElementById("lastCard"),
  lastTitle: document.getElementById("lastTitle"),
  lastStats: document.getElementById("lastStats"),
};

/** 当前预览中的文件名，确认导入时回传给后端。 */
let stagedFile = null;

function t(key, fallback) {
  return bridge.t(`pages.import.${key}`, fallback);
}

function setStatus(message, kind) {
  if (!message) {
    el.status.hidden = true;
    return;
  }
  el.status.hidden = false;
  el.status.textContent = message;
  el.status.dataset.kind = kind || "info";
}

function statRows(report) {
  const rows = [
    ["playersNew", "新玩家", report.players_new],
    ["playersUpdated", "更新玩家", report.players_updated],
    ["checkinsInserted", "新增签到记录", report.checkins_inserted],
    ["checkinsSkipped", "已存在记录", report.checkins_skipped],
    ["coinsGranted", "补发龟龟币", report.coins_granted],
    ["coinsPlayers", "受补发玩家", report.coins_players],
  ];
  return rows.map(([key, fallback, value]) => [t(key, fallback), value]);
}

function renderStats(target, report) {
  target.replaceChildren();
  for (const [label, value] of statRows(report)) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value ?? 0);
    target.append(dt, dd);
  }
}

function renderIssues(target, report) {
  const issues = [
    ...(report.errors || []).map((text) => `异常：${text}`),
    ...(report.warnings || []).map((text) => `提示：${text}`),
  ];
  target.replaceChildren();
  target.hidden = issues.length === 0;
  for (const text of issues) {
    const line = document.createElement("p");
    line.textContent = text;
    target.append(line);
  }
}

async function loadPending() {
  try {
    const data = await bridge.apiGet("import/list");
    renderPending(data?.files || []);
    renderLast(data?.last_report || null);
  } catch (error) {
    setStatus(`读取待导入文件失败：${error.message}`, "error");
  }
}

function renderPending(files) {
  el.pending.replaceChildren();
  if (!files.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = t("pendingEmpty", "import/ 目录里还没有 JSON 文件。");
    el.pending.append(empty);
    return;
  }
  for (const file of files) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = file.name;
    name.className = "file-name";
    const size = document.createElement("span");
    size.className = "muted";
    size.textContent = `${Math.max(1, Math.round(file.size / 1024))} KB`;
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "button small";
    preview.textContent = t("preview", "预览");
    preview.addEventListener("click", () => previewExisting(file.name));
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "button small primary";
    apply.textContent = t("import", "导入");
    apply.addEventListener("click", () => applyImport(file.name));
    item.append(name, size, preview, apply);
    el.pending.append(item);
  }
}

function renderLast(report) {
  if (!report) {
    el.lastCard.hidden = true;
    return;
  }
  el.lastCard.hidden = false;
  el.lastStats.replaceChildren();
  const dl = el.lastStats;
  for (const [label, value] of statRows(report)) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value ?? 0);
    dl.append(dt, dd);
  }
  const files = (report.files || []).join("、");
  if (files) {
    const dt = document.createElement("dt");
    dt.textContent = "文件";
    const dd = document.createElement("dd");
    dd.textContent = files;
    dl.append(dt, dd);
  }
}

async function previewExisting(filename) {
  setStatus(`正在预览 ${filename} …`);
  el.reportCard.hidden = true;
  try {
    // 预览等价于"再上传一次同名文件"：后端按名字重算一遍，不写库。
    const report = await bridge.apiPost("import/preview", { filename });
    showPreview(filename, report);
    setStatus("");
  } catch (error) {
    setStatus(`预览失败：${error.message}`, "error");
  }
}

function showPreview(filename, report) {
  stagedFile = filename;
  el.previewCard.hidden = false;
  el.previewTitle.textContent = t("previewTitle", "预览");
  el.previewFile.textContent = `${filename} · ${t("dryRun", "预览（未写入数据库）")}`;
  renderStats(el.previewStats, report);
  renderIssues(el.previewIssues, report);
}

async function applyImport(filename) {
  const target = filename || stagedFile;
  if (!target) {
    setStatus("请先选择要导入的文件", "error");
    return;
  }
  el.apply.disabled = true;
  setStatus(`正在导入 ${target} …`);
  try {
    const report = await bridge.apiPost("import/apply", { filename: target });
    el.previewCard.hidden = true;
    stagedFile = null;
    el.reportCard.hidden = false;
    el.reportTitle.textContent = "导入完成";
    el.reportFile.textContent = target;
    renderStats(el.reportStats, report);
    renderIssues(el.reportIssues, report);
    setStatus("");
    await loadPending();
  } catch (error) {
    setStatus(`导入失败：${error.message}`, "error");
  } finally {
    el.apply.disabled = false;
  }
}

async function uploadFile(file) {
  if (!file) return;
  el.reportCard.hidden = true;
  setStatus(`正在上传 ${file.name} …`);
  try {
    const result = await bridge.upload("import/upload", file);
    const filename = result?.filename || file.name;
    const report = result?.preview || result;
    showPreview(filename, report);
    setStatus("");
    await loadPending();
  } catch (error) {
    setStatus(`上传失败：${error.message}`, "error");
  }
}

function wire() {
  el.file.addEventListener("change", () => uploadFile(el.file.files?.[0]));
  el.apply.addEventListener("click", () => applyImport(stagedFile));
  el.cancel.addEventListener("click", () => {
    stagedFile = null;
    el.previewCard.hidden = true;
    setStatus("已取消，文件仍留在 import/ 目录，可以稍后再导入。");
  });
  el.refresh.addEventListener("click", loadPending);

  for (const type of ["dragenter", "dragover"]) {
    el.drop.addEventListener(type, (event) => {
      event.preventDefault();
      el.drop.classList.add("active");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    el.drop.addEventListener(type, (event) => {
      event.preventDefault();
      el.drop.classList.remove("active");
    });
  }
  el.drop.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    uploadFile(file);
  });
}

function renderLabels() {
  el.heading.textContent = t("heading", "数据导入");
  el.lead.textContent = t(
    "lead",
    "上传老插件导出的签到 JSON。导入只新增数据：等级只升不降，签到记录按日期去重，龟龟币按历史记录补发。",
  );
  el.dropText.textContent = t("drop", "把 JSON 文件拖到这里，或");
  document.querySelector("label[for=file]").textContent = t("pick", "选择 JSON 文件");
  el.apply.textContent = t("apply", "确认导入");
  el.cancel.textContent = t("cancel", "取消");
  el.pendingTitle.textContent = t("pendingTitle", "待导入文件");
  el.refresh.textContent = t("refresh", "刷新");
  el.lastTitle.textContent = t("lastTitle", "上次导入");
}

async function main() {
  if (!bridge) {
    setStatus("插件页面 bridge 不可用，请从 AstrBot WebUI 的插件详情页打开本页。", "error");
    return;
  }
  await bridge.ready();
  renderLabels();
  bridge.onContext(renderLabels);
  wire();
  await loadPending();
}

main();
