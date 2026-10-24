"use strict";
const token = document.getElementById("session-token").content;
window.history.replaceState(null, "", location.pathname);
const $ = (id) => document.getElementById(id);
let language = navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
let currentPage = "overview";
let selected = null;
let continuation = null;
let historyBefore = null;
let searchFilters = {};
let searchIdentity = 0;
let browsePending = false;
let reviewId = null;
let lastOverview = null;
let lastBackup = null;
let lastPrivate = null;
let browseItems = [];
let lastScan = null;
const t = (key) => window.UI_STRINGS[language][key] || key;

// Stored values enter only text nodes. No memory field is interpreted as markup.
function node(tag, value, parent) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = String(value);
  if (parent) parent.appendChild(element);
  return element;
}
function display(id, value) {
  $(id).replaceChildren();
  node("pre", JSON.stringify(value, null, 2), $(id));
}
function formValues(id) {
  return Object.fromEntries([...new FormData($(id))].filter(([, value]) => value !== ""));
}
async function request(route, data = {}, post = false) {
  const options = {headers: {"X-Scope-Recall-Token": token}, cache: "no-store"};
  let url = "/api/" + route;
  if (post) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  } else {
    url += "?" + new URLSearchParams(data);
  }
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(t(result.code) + (result.field ? ": " + t(result.field) : ""));
  return result.data;
}
function run(action) {
  $("status").textContent = t("loading");
  return Promise.resolve().then(action).then(() => { $("status").textContent = ""; })
    .catch((error) => { $("status").textContent = t("error") + ": " + error.message; });
}
function overview(data) {
  lastOverview = data;
  $("overview-data").replaceChildren();
  for (const [key, label] of [["store_bytes", "bytes"], ["vector_bytes", "vectorBytes"], ["sources", "sourceCount"], ["pending_work", "pending"], ["failed_work", "failed"]]) {
    const card = node("div", undefined, $("overview-data"));
    card.className = "card";
    node("span", t(label), card);
    node("strong", data[key] ?? "—", card);
  }
  node("pre", JSON.stringify({work_error_counts: data.work_error_counts, recent_work_errors: data.recent_work_errors}, null, 2), $("overview-data"));
}
function backup(data) {
  lastBackup = data;
  $("backup-data").replaceChildren();
  node("h3", t("latestBundle"), $("backup-data"));
  node("p", data.latest_bundle || t("noBackup"), $("backup-data"));
  node("h3", t("problem"), $("backup-data"));
  node("p", data.problem || t("none"), $("backup-data"));
  node("pre", JSON.stringify(data.checks, null, 2), $("backup-data"));
}
function privateState(data) {
  lastPrivate = data;
  $("private-data").replaceChildren();
  node("h3", t("effective") + ": " + (data.mode === "full" ? t("full") : data.mode === "read-only" ? t("readOnly") : t("none")), $("private-data"));
  node("h3", t("rules"), $("private-data"));
  for (const rule of data.rules) {
    const row = node("article", undefined, $("private-data"));
    node("pre", JSON.stringify(rule, null, 2), row);
    const off = node("button", t("turnOff"), row);
    off.addEventListener("click", () => run(() => preview({action: "private-off", target: rule.target, session: rule.session, project: rule.project})));
  }
}
async function loadPage(page) {
  currentPage = page;
  for (const section of document.querySelectorAll(".page")) section.hidden = section.id !== page;
  for (const button of document.querySelectorAll("[data-page]")) {
    if (button.dataset.page === page) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (page === "overview") overview(await request("overview"));
  if (page === "browse") await browse(false);
  if (page === "private") privateState(await request("private"));
  if (page === "backup") backup(await request("backup"));
}
async function browse(more) {
  if (more && (browsePending || !continuation)) return;
  if (!more) {
    searchIdentity += 1;
    searchFilters = formValues("filters");
    browseItems = [];
    continuation = null;
    lastScan = null;
    $("items").replaceChildren();
    $("scan").textContent = "";
  }
  const identity = searchIdentity;
  browsePending = true;
  $("next").disabled = true;
  const values = {...searchFilters};
  if (more && continuation) values.cursor = continuation;
  try {
    const data = await request(values.query ? "search" : "list", values);
    // A new search owns the list and cursor, including while an older page is pending.
    if (identity !== searchIdentity) return;
    browseItems.push(...data.items);
    continuation = data.next_cursor;
    lastScan = data;
    renderBrowse();
  } catch (error) {
    if (identity === searchIdentity) throw error;
  } finally {
    if (identity === searchIdentity) {
      browsePending = false;
      $("next").disabled = !continuation;
    }
  }
}
function renderBrowse() {
  $("items").replaceChildren();
  for (const item of browseItems) {
    const button = node("button", (t(item.kind) || item.kind) + " · " + item.ref + "\n" + (item.content ?? ""), $("items"));
    button.addEventListener("click", () => run(() => detail(item.ref)));
  }
  $("next").disabled = browsePending || !continuation;
  $("scan").textContent = t("examined") + ": " + lastScan.examined + " · " + t(lastScan.scan_complete ? "complete" : "more");
  if (!browseItems.length) node("p", t("empty"), $("items"));
}
function sourceLinks(value, parent) {
  const seen = new Set();
  function link(ref, revision) {
    if (seen.has(ref)) return;
    seen.add(ref);
    const button = node("button", t("show") + " " + ref, parent);
    button.addEventListener("click", () => run(() => detail(ref, revision)));
  }
  function visit(part) {
    if (typeof part === "string") {
      const match = part.match(/^(event-[a-f0-9]{64})(?:@([0-9]+))?$/);
      if (match) link(match[1], match[2]);
    }
    if (!part || typeof part !== "object") return;
    const ref = part.source_ref || (String(part.ref).startsWith("event-") ? part.ref : null);
    if (typeof ref === "string" && !seen.has(ref)) {
      link(ref, part.source_revision || part.revision);
    }
    for (const child of Object.values(part)) visit(child);
  }
  visit(value);
}
async function loadHistory(more) {
  const data = await request("history", {ref: selected.ref, ...(more ? {before: historyBefore} : {})});
  if (!more) $("history-data").replaceChildren();
  for (const item of data.items) node("pre", JSON.stringify(item, null, 2), $("history-data"));
  historyBefore = data.next_before;
  $("history-next").disabled = historyBefore === null;
}
async function detail(ref, revision) {
  selected = await request("detail", {ref, ...(revision ? {revision} : {})});
  display("detail-data", selected);
  $("detail").hidden = false;
  $("correction").hidden = selected.kind !== "claim";
  if (selected.kind === "claim") {
    $("correction").elements.value.value = selected.value.payload.value_text || "";
    $("correction").elements.retract.checked = false;
  }
  $("sources").replaceChildren();
  sourceLinks(selected, $("sources"));
  await loadHistory(false);
  $("detail").scrollIntoView({block: "start"});
}
async function preview(values) {
  const data = await request("preview", values, true);
  reviewId = data.id;
  display("preview-data", data.preview);
  $("review").hidden = false;
  $("receipt").hidden = true;
  $("confirm").disabled = false;
  $("review").scrollIntoView({block: "start"});
}
function privateArguments(action) {
  const values = formValues("private-form");
  const result = {action, target: values.target};
  if (values.target === "session") result.session = values.targetValue || "";
  if (values.target === "project") result.project = values.targetValue || "";
  if (action === "private-on") {
    result.mode = values.mode;
    if (values.duration_seconds) result.duration_seconds = Number(values.duration_seconds);
  }
  return result;
}
function translate() {
  document.documentElement.lang = language;
  $("language").value = language;
  for (const element of document.querySelectorAll("[data-i18n]")) element.textContent = t(element.dataset.i18n);
  if (lastOverview) overview(lastOverview);
  if (lastBackup) backup(lastBackup);
  if (lastPrivate) privateState(lastPrivate);
  if (lastScan) renderBrowse();
  if (selected) { $("sources").replaceChildren(); sourceLinks(selected, $("sources")); }
}
$("language").addEventListener("change", () => { language = $("language").value; translate(); });
for (const button of document.querySelectorAll("[data-page]")) button.addEventListener("click", () => run(() => loadPage(button.dataset.page)));
$("refresh-overview").addEventListener("click", () => run(() => loadPage("overview")));
$("refresh-backup").addEventListener("click", () => run(() => loadPage("backup")));
$("filters").addEventListener("submit", (event) => { event.preventDefault(); run(() => browse(false)); });
$("next").addEventListener("click", () => run(() => browse(true)));
$("history-next").addEventListener("click", () => run(() => loadHistory(true)));
$("forget-session").addEventListener("click", () => run(() => preview({...formValues("filters"), action: "forget-session"})));
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => run(() => preview({action: button.dataset.action, ref: selected.ref, expected_revision: selected.revision})));
$("correction").addEventListener("submit", (event) => {
  event.preventDefault();
  run(() => preview({action: "revise", ref: selected.ref, expected_revision: selected.revision, value: $("correction").elements.value.value, retract: $("correction").elements.retract.checked}));
});
$("private-form").addEventListener("submit", (event) => { event.preventDefault(); run(() => preview(privateArguments("private-on"))); });
$("private-off").addEventListener("click", () => run(() => preview(privateArguments("private-off"))));
$("private-inspect").addEventListener("click", () => run(async () => {
  const {session, project} = privateArguments("private-off");
  privateState(await request("private", {...(session !== undefined ? {session} : {}), ...(project !== undefined ? {project} : {})}));
}));
$("export-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(() => preview({action: "export", out: $("export-form").elements.out.value, with_attachments: $("export-form").elements.with_attachments.checked}));
});
$("cancel").addEventListener("click", () => { reviewId = null; $("review").hidden = true; });
$("confirm").addEventListener("click", () => run(async () => {
  $("confirm").disabled = true;
  const data = await request("confirm", {id: reviewId, confirmed: true}, true);
  reviewId = null;
  $("review").hidden = true;
  display("receipt-data", data);
  $("receipt").hidden = false;
  if (["forget", "forget-session"].includes(data.action)) $("detail").hidden = true;
  if (["revise", "mute", "unmute"].includes(data.action) && selected) await detail(selected.ref);
  if (currentPage === "private") privateState(await request("private"));
  $("receipt").scrollIntoView({block: "start"});
}));
translate();
run(() => loadPage("overview"));
