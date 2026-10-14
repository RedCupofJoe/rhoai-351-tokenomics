const spans = document.querySelectorAll("[data-span]");
const login = document.getElementById("login");
const app = document.getElementById("app");
const loginError = document.getElementById("login-error");
const rows = document.getElementById("rows");
const totals = document.getElementById("totals");
const empty = document.getElementById("empty");
const reportError = document.getElementById("report-error");
const reportMeta = document.getElementById("report-meta");
const pricingStatus = document.getElementById("pricing-status");
const pricingNote = document.getElementById("pricing-note");
const savePrices = document.getElementById("save-prices");
const projectFilter = document.getElementById("project-filter");
const userFilter = document.getElementById("user-filter");
const userFilterWrap = document.getElementById("user-filter-wrap");
const modelFilter = document.getElementById("model-filter");
let span = "24h";
let canEdit = false;
let lastReport = null;

const pairs = [
  ["input-token", "input-million"],
  ["cache-token", "cache-million"],
  ["decoded-token", "decoded-million"],
];

function token() {
  return sessionStorage.getItem("tokenomicsToken") || "";
}

function headers(json) {
  const result = {};
  if (token()) result.Authorization = "Bearer " + token();
  if (json) result["Content-Type"] = "application/json";
  return result;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    const hadToken = Boolean(token());
    sessionStorage.removeItem("tokenomicsToken");
    showLogin(hadToken ? "The admin token was rejected." : "");
    throw new Error("unauthorized");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function showLogin(message) {
  login.classList.remove("hidden");
  app.classList.add("hidden");
  loginError.textContent = message || "";
}

function showApp() {
  login.classList.add("hidden");
  app.classList.remove("hidden");
}

function applyRole(me) {
  canEdit = Boolean(me.admin);
  const who = me.method === "token" ? "Administrator" : me.user;
  document.getElementById("session").textContent = canEdit
    ? who + " · cluster usage"
    : who + " · personal usage";
  document.getElementById("showback-title").textContent = canEdit ? "Cluster usage" : "Your usage";
  pricingNote.textContent = canEdit
    ? "Saving updates the per-token rates used for every user."
    : "Prices are read-only. An OpenShift AI administrator sets the per-token rates.";
  document.querySelectorAll("#pricing-form input").forEach((input) => {
    input.disabled = !canEdit;
  });
  savePrices.classList.toggle("hidden", !canEdit);
  userFilterWrap.classList.toggle("hidden", !canEdit);
}

function formatRate(value) {
  if (!Number.isFinite(value)) return "";
  return value.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
}

function bindPairs() {
  for (const [tokenId, millionId] of pairs) {
    const tokenInput = document.getElementById(tokenId);
    const millionInput = document.getElementById(millionId);
    tokenInput.addEventListener("input", () => {
      if (tokenInput.disabled) return;
      millionInput.value = formatRate(Number(tokenInput.value || 0) * 1_000_000);
    });
    millionInput.addEventListener("input", () => {
      if (millionInput.disabled) return;
      tokenInput.value = formatRate(Number(millionInput.value || 0) / 1_000_000);
    });
  }
}

function fillPricing(pricing) {
  document.getElementById("currency").value = pricing.currency || "USD";
  for (const kind of ["input", "cache", "decoded"]) {
    document.getElementById(kind + "-token").value = formatRate(pricing.rates_per_token[kind]);
    document.getElementById(kind + "-million").value = formatRate(pricing.rates_per_million[kind]);
  }
}

function money(value, currency) {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format(value);
  } catch (_err) {
    return String(value);
  }
}

function tokens(value) {
  return new Intl.NumberFormat().format(value);
}

function fillSelect(select, values, current) {
  const options = [""].concat(values);
  select.replaceChildren();
  for (const value of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value || "All";
    select.appendChild(option);
  }
  select.value = options.includes(current) ? current : "";
}

function visibleRows(report) {
  const project = projectFilter.value;
  const user = userFilter.value;
  const model = modelFilter.value;
  return report.users.filter((row) => {
    if (project && row.namespace !== project) return false;
    if (user && row.user !== user) return false;
    if (model && row.model !== model) return false;
    return true;
  });
}

let rendering = false;

function renderReport(report) {
  rendering = true;
  try {
  lastReport = report;
  fillSelect(projectFilter, report.projects || [], projectFilter.value);
  const users = [...new Set(report.users.map((row) => row.user))].sort();
  fillSelect(userFilter, users, userFilter.value);
  const modelSource = report.users.filter((row) => {
    if (projectFilter.value && row.namespace !== projectFilter.value) return false;
    if (userFilter.value && row.user !== userFilter.value) return false;
    return true;
  });
  const models = [...new Set(modelSource.map((row) => row.model))].sort();
  fillSelect(modelFilter, models, modelFilter.value);
  const shown = visibleRows(report);
  rows.replaceChildren();
  reportError.textContent = "";
  const scope = report.scope === "self" ? "your usage" : "cluster";
  reportMeta.textContent = report.currency + " · " + report.span + " · " + scope;
  empty.classList.toggle("hidden", shown.length > 0);
  empty.textContent = report.scope === "self"
    ? "No token usage for your OpenShift user in this span. Usage is matched to your username."
    : "No token usage in this span. The exporter counts events it has observed since its checkpoint started.";
  for (const user of shown) {
    const tr = document.createElement("tr");
    const cells = [
      user.user,
      user.namespace || "unknown",
      user.model || "unknown",
      tokens(user.tokens.input),
      tokens(user.tokens.cache),
      tokens(user.tokens.decoded),
      money(user.costs.input_cost, report.currency),
      money(user.costs.cache_cost, report.currency),
      money(user.costs.decoded_cost, report.currency),
      money(user.costs.total_cost, report.currency),
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index > 2) td.className = "num";
      tr.appendChild(td);
    });
    rows.appendChild(tr);
  }
  const totalTokens = { input: 0, cache: 0, decoded: 0 };
  const totalCosts = { input_cost: 0, cache_cost: 0, decoded_cost: 0, total_cost: 0 };
  for (const user of shown) {
    for (const kind of ["input", "cache", "decoded"]) totalTokens[kind] += user.tokens[kind];
    for (const kind of Object.keys(totalCosts)) totalCosts[kind] += user.costs[kind];
  }
  totals.replaceChildren();
  const totalCells = [
    "Total",
    "",
    "",
    tokens(totalTokens.input),
    tokens(totalTokens.cache),
    tokens(totalTokens.decoded),
    money(totalCosts.input_cost, report.currency),
    money(totalCosts.cache_cost, report.currency),
    money(totalCosts.decoded_cost, report.currency),
    money(totalCosts.total_cost, report.currency),
  ];
  totalCells.forEach((value, index) => {
    const td = document.createElement("td");
    td.textContent = value;
    if (index > 2) td.className = "num";
    totals.appendChild(td);
  });
  } finally {
    rendering = false;
  }
}

async function loadPricing() {
  fillPricing(await api("/api/pricing", { headers: headers(false) }));
}

async function loadShowback() {
  try {
    renderReport(await api("/api/showback?span=" + encodeURIComponent(span), { headers: headers(false) }));
  } catch (err) {
    if (err.message !== "unauthorized") reportError.textContent = err.message;
  }
}

async function enter() {
  const me = await api("/api/me", { headers: headers(false) });
  applyRole(me);
  await loadPricing();
  showApp();
  await loadShowback();
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  sessionStorage.setItem("tokenomicsToken", document.getElementById("token").value.trim());
  try {
    await enter();
  } catch (err) {
    if (err.message !== "unauthorized") showLogin(err.message);
  }
});

document.getElementById("pricing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!canEdit) return;
  pricingStatus.textContent = "";
  const body = {
    currency: document.getElementById("currency").value.trim().toUpperCase(),
    rate_unit: "token",
    rates: {
      input: Number(document.getElementById("input-token").value),
      cache: Number(document.getElementById("cache-token").value),
      decoded: Number(document.getElementById("decoded-token").value),
    },
  };
  try {
    fillPricing(await api("/api/pricing", { method: "PUT", headers: headers(true), body: JSON.stringify(body) }));
    pricingStatus.textContent = "Saved.";
    await loadShowback();
  } catch (err) {
    pricingStatus.textContent = err.message;
  }
});

spans.forEach((button) => {
  button.addEventListener("click", async () => {
    span = button.dataset.span;
    spans.forEach((item) => item.classList.toggle("active", item === button));
    await loadShowback();
  });
});

function onFilterChange() {
  if (!rendering && lastReport) renderReport(lastReport);
}

projectFilter.addEventListener("change", onFilterChange);
userFilter.addEventListener("change", onFilterChange);
modelFilter.addEventListener("change", onFilterChange);

bindPairs();
enter().catch((err) => {
  if (err.message !== "unauthorized") showLogin(err.message);
});
