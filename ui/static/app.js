const spans = document.querySelectorAll("[data-span]");
const themeToggle = document.getElementById("theme-toggle");
const tokenPie = document.getElementById("token-pie");
const pieDesc = document.getElementById("pie-desc");
const pieLegend = document.getElementById("pie-legend");
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
const addProvider = document.getElementById("add-provider");
const providerBody = document.querySelector("#provider-matrix tbody");
const providerCompare = document.getElementById("provider-compare");
const projectFilter = document.getElementById("project-filter");
const userFilter = document.getElementById("user-filter");
const userFilterWrap = document.getElementById("user-filter-wrap");
const modelFilter = document.getElementById("model-filter");
let span = "24h";
let canEdit = false;
let lastReport = null;
let useBreakGlass = false;
let maasModels = [];
const OTHER_MODEL = "__other__";

const KINDS = ["input", "cache", "decoded"];

function token() {
  return sessionStorage.getItem("tokenomicsToken") || "";
}

function headers(json) {
  const result = {};
  if (useBreakGlass && token()) result.Authorization = "Bearer " + token();
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
    ? "Add a provider and set its input, cache, and decoded prices. The list is the models MaaS is serving. Choose Other to type a name that is not registered. A model uses the row with that model name. The showback row prices every other model."
    : "Prices are read-only. An OpenShift AI administrator maps a cost to every provider.";
  document.querySelectorAll("#pricing-form input, #pricing-form select").forEach((input) => {
    input.disabled = !canEdit;
  });
  document.querySelectorAll('[data-field="model-select"]').forEach(syncModelField);
  savePrices.classList.toggle("hidden", !canEdit);
  addProvider.classList.toggle("hidden", !canEdit);
  document.querySelectorAll(".remove-provider").forEach((button) => {
    button.classList.toggle("hidden", !canEdit);
  });
  userFilterWrap.classList.toggle("hidden", !canEdit);
}

function formatRate(value) {
  if (!Number.isFinite(value)) return "";
  return value.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
}

function textCell(field, value, required) {
  const input = document.createElement("input");
  input.type = "text";
  input.dataset.field = field;
  input.value = value;
  input.required = required;
  input.maxLength = field === "name" ? 80 : 200;
  input.disabled = !canEdit;
  input.addEventListener("input", renderComparison);
  return input;
}

function rateCell(kind, unit, value) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "any";
  input.required = true;
  input.dataset.kind = kind;
  input.dataset.unit = unit;
  input.value = formatRate(value);
  input.disabled = !canEdit;
  input.addEventListener("input", () => {
    const row = input.closest("tr");
    const otherUnit = unit === "token" ? "million" : "token";
    const other = row.querySelector('[data-kind="' + kind + '"][data-unit="' + otherUnit + '"]');
    const number = Number(input.value || 0);
    other.value = formatRate(unit === "token" ? number * 1_000_000 : number / 1_000_000);
    renderComparison();
  });
  return input;
}

function syncModelField(select) {
  const custom = select.parentElement.querySelector('[data-field="model-custom"]');
  const customMode = select.value === OTHER_MODEL;
  custom.classList.toggle("hidden", !customMode);
  custom.required = customMode && canEdit;
  custom.disabled = !canEdit || !customMode;
  select.disabled = !canEdit;
}

function modelField(value) {
  const wrap = document.createElement("div");
  wrap.className = "model-field";
  const select = document.createElement("select");
  select.dataset.field = "model-select";
  const custom = document.createElement("input");
  custom.type = "text";
  custom.dataset.field = "model-custom";
  custom.maxLength = 200;
  custom.placeholder = "Model name";
  const current = value || "*";
  const choices = ["*"].concat(maasModels.filter((name) => name && name !== "*"));
  const known = choices.find((name) => name.toLowerCase() === String(current).toLowerCase());
  choices.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === "*" ? "* (other models)" : name;
    select.appendChild(option);
  });
  const other = document.createElement("option");
  other.value = OTHER_MODEL;
  other.textContent = "Other";
  select.appendChild(other);
  if (known) {
    select.value = known;
    custom.value = known;
  } else {
    select.value = OTHER_MODEL;
    custom.value = current;
  }
  select.addEventListener("change", () => {
    if (select.value === OTHER_MODEL) custom.value = "";
    else custom.value = select.value;
    syncModelField(select);
    renderComparison();
  });
  custom.addEventListener("input", renderComparison);
  wrap.append(select, custom);
  syncModelField(select);
  return wrap;
}

function modelValue(row) {
  const select = row.querySelector('[data-field="model-select"]');
  const custom = row.querySelector('[data-field="model-custom"]');
  if (!select || select.value === OTHER_MODEL) return (custom && custom.value.trim()) || "*";
  return select.value.trim() || "*";
}

function appendProvider(provider) {
  const row = document.createElement("tr");
  const localCell = document.createElement("td");
  const local = document.createElement("input");
  local.type = "radio";
  local.name = "showback-provider";
  local.dataset.field = "local";
  local.checked = Boolean(provider.local);
  local.disabled = !canEdit;
  local.addEventListener("change", () => {
    providerBody.querySelectorAll('[data-field="local"]').forEach((item) => {
      item.checked = item === local;
    });
  });
  localCell.appendChild(local);
  row.appendChild(localCell);
  const nameCell = document.createElement("td");
  nameCell.appendChild(textCell("name", provider.name || "", true));
  row.appendChild(nameCell);
  const modelCell = document.createElement("td");
  modelCell.appendChild(modelField(provider.model || "*"));
  row.appendChild(modelCell);
  for (const kind of KINDS) {
    const tokenCell = document.createElement("td");
    const millionCell = document.createElement("td");
    tokenCell.appendChild(rateCell(kind, "token", provider.rates_per_token[kind]));
    millionCell.appendChild(rateCell(kind, "million", provider.rates_per_million[kind]));
    row.append(tokenCell, millionCell);
  }
  const action = document.createElement("td");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-provider";
  remove.textContent = "Remove";
  remove.classList.toggle("hidden", !canEdit);
  remove.addEventListener("click", () => {
    if (providerBody.children.length <= 1) return;
    const wasLocal = local.checked;
    row.remove();
    if (wasLocal) providerBody.querySelector('[data-field="local"]').checked = true;
    renderComparison();
  });
  action.appendChild(remove);
  row.appendChild(action);
  providerBody.appendChild(row);
}

function fillPricing(pricing) {
  document.getElementById("currency").value = pricing.currency || "USD";
  providerBody.replaceChildren();
  const providers = pricing.providers && pricing.providers.length
    ? pricing.providers
    : [{
      name: "Local MaaS",
      model: "*",
      local: true,
      rates_per_token: pricing.rates_per_token || { input: 0, cache: 0, decoded: 0 },
      rates_per_million: pricing.rates_per_million || { input: 0, cache: 0, decoded: 0 },
    }];
  providers.forEach((provider) => {
    appendProvider({
      name: provider.name,
      model: provider.model,
      local: provider.local,
      rates_per_token: provider.rates_per_token || { input: 0, cache: 0, decoded: 0 },
      rates_per_million: provider.rates_per_million || { input: 0, cache: 0, decoded: 0 },
    });
  });
  renderComparison();
}

function providersFromForm() {
  return [...providerBody.querySelectorAll("tr")].map((row) => ({
    name: row.querySelector('[data-field="name"]').value.trim(),
    model: modelValue(row),
    local: row.querySelector('[data-field="local"]').checked,
    rates: Object.fromEntries(KINDS.map((kind) => [
      kind,
      Number(row.querySelector('[data-kind="' + kind + '"][data-unit="token"]').value || 0),
    ])),
  }));
}

function renderComparison() {
  providerCompare.replaceChildren();
  if (!lastReport) return;
  const totals = { input: 0, cache: 0, decoded: 0 };
  for (const row of visibleRows(lastReport)) {
    for (const kind of KINDS) totals[kind] += row.tokens[kind];
  }
  const currency = document.getElementById("currency").value.trim() || lastReport.currency;
  for (const provider of providersFromForm()) {
    const costs = {};
    let total = 0;
    for (const kind of KINDS) {
      costs[kind] = totals[kind] * (Number(provider.rates[kind]) || 0);
      total += costs[kind];
    }
    const tr = document.createElement("tr");
    const cells = [
      provider.name || "Provider",
      provider.model,
      money(costs.input, currency),
      money(costs.cache, currency),
      money(costs.decoded, currency),
      money(total, currency),
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index > 1) td.className = "num";
      tr.appendChild(td);
    });
    providerCompare.appendChild(tr);
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

const SLICE_COLORS = {
  input: "var(--slice-input)",
  cache: "var(--slice-cache)",
  decoded: "var(--slice-decoded)",
};

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  themeToggle.setAttribute("aria-pressed", dark ? "true" : "false");
  themeToggle.textContent = dark ? "Light mode" : "Dark mode";
  localStorage.setItem("tokenomicsTheme", dark ? "dark" : "light");
}

function initialTheme() {
  const stored = localStorage.getItem("tokenomicsTheme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function piePath(start, end) {
  const large = end - start > Math.PI ? 1 : 0;
  const x1 = 60 + 52 * Math.sin(start);
  const y1 = 60 - 52 * Math.cos(start);
  const x2 = 60 + 52 * Math.sin(end);
  const y2 = 60 - 52 * Math.cos(end);
  return "M 60 60 L " + x1 + " " + y1 + " A 52 52 0 " + large + " 1 " + x2 + " " + y2 + " Z";
}

function renderPie(totalTokens) {
  const parts = [
    { key: "input", label: "Input", value: totalTokens.input },
    { key: "cache", label: "Cache", value: totalTokens.cache },
    { key: "decoded", label: "Decoded", value: totalTokens.decoded },
  ];
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  tokenPie.replaceChildren();
  pieLegend.replaceChildren();
  if (total <= 0) {
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", "60");
    circle.setAttribute("cy", "60");
    circle.setAttribute("r", "52");
    circle.setAttribute("fill", "var(--empty-slice)");
    tokenPie.appendChild(circle);
    pieDesc.textContent = "No tokens in this view.";
    return;
  }
  let cursor = 0;
  for (const part of parts) {
    if (part.value <= 0) continue;
    const sweep = (part.value / total) * Math.PI * 2;
    const shape = document.createElementNS("http://www.w3.org/2000/svg", part.value === total ? "circle" : "path");
    if (part.value === total) {
      shape.setAttribute("cx", "60");
      shape.setAttribute("cy", "60");
      shape.setAttribute("r", "52");
    } else {
      shape.setAttribute("d", piePath(cursor, cursor + sweep));
    }
    shape.setAttribute("fill", SLICE_COLORS[part.key]);
    tokenPie.appendChild(shape);
    cursor += sweep;
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = SLICE_COLORS[part.key];
    item.append(swatch, document.createTextNode(part.label + " · " + tokens(part.value) + " · " + Math.round((part.value / total) * 100) + "%"));
    pieLegend.appendChild(item);
  }
  pieDesc.textContent = tokens(total) + " tokens across input, cache, and decoded.";
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
  if (report.attribution === "model") reportMeta.textContent += " · model totals";
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
      user.provider || "Local MaaS",
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
      if (index > 3) td.className = "num";
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
    if (index > 3) td.className = "num";
    totals.appendChild(td);
  });
  renderPie(totalTokens);
  renderComparison();
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

async function openSession(me) {
  applyRole(me);
  showApp();
  try {
    const listed = await api("/api/models", { headers: headers(false) });
    maasModels = Array.isArray(listed.models) ? listed.models : [];
  } catch (err) {
    if (err.message === "unauthorized") throw err;
    maasModels = [];
  }
  await loadPricing();
  await loadShowback();
}

async function enter() {
  const openshift = await fetch("/api/me");
  if (openshift.ok) {
    useBreakGlass = false;
    await openSession(await openshift.json());
    return;
  }
  if (openshift.status !== 401 || !token()) {
    showLogin(openshift.status === 401 ? "" : (openshift.statusText || "Sign-in failed."));
    return;
  }
  useBreakGlass = true;
  await openSession(await api("/api/me", { headers: headers(false) }));
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  sessionStorage.setItem("tokenomicsToken", document.getElementById("token").value.trim());
  useBreakGlass = true;
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
  const providers = providersFromForm();
  if (providers.some((provider) => !provider.name)) {
    pricingStatus.textContent = "Provider name is required.";
    return;
  }
  const body = {
    currency: document.getElementById("currency").value.trim().toUpperCase(),
    rate_unit: "token",
    providers: providers.map((provider) => ({
      name: provider.name,
      model: provider.model,
      local: provider.local,
      rates: provider.rates,
    })),
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

addProvider.addEventListener("click", () => {
  appendProvider({
    name: "",
    model: "*",
    local: providerBody.children.length === 0,
    rates_per_token: { input: 0, cache: 0, decoded: 0 },
    rates_per_million: { input: 0, cache: 0, decoded: 0 },
  });
});

themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
applyTheme(initialTheme());
enter().catch((err) => {
  if (err.message === "unauthorized") return;
  if (login.classList.contains("hidden")) {
    pricingStatus.textContent = err.message;
    return;
  }
  showLogin(err.message);
});
