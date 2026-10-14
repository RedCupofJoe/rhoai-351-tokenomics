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
let span = "24h";

const pairs = [
  ["input-token", "input-million"],
  ["cache-token", "cache-million"],
  ["decoded-token", "decoded-million"],
];

function token() {
  return sessionStorage.getItem("tokenomicsToken") || "";
}

function headers(json) {
  const result = { Authorization: "Bearer " + token() };
  if (json) result["Content-Type"] = "application/json";
  return result;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    sessionStorage.removeItem("tokenomicsToken");
    showLogin("The admin token was rejected.");
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
  document.getElementById("session").textContent = "Authenticated";
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
      millionInput.value = formatRate(Number(tokenInput.value || 0) * 1_000_000);
    });
    millionInput.addEventListener("input", () => {
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

function renderReport(report) {
  rows.replaceChildren();
  reportError.textContent = "";
  reportMeta.textContent = report.currency + " · " + report.span;
  empty.classList.toggle("hidden", report.users.length > 0);
  for (const user of report.users) {
    const tr = document.createElement("tr");
    const cells = [
      user.user,
      (user.models || []).join(", "),
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
      if (index > 1) td.className = "num";
      tr.appendChild(td);
    });
    rows.appendChild(tr);
  }
  totals.replaceChildren();
  const totalCells = [
    "Total",
    "",
    tokens(report.totals.tokens.input),
    tokens(report.totals.tokens.cache),
    tokens(report.totals.tokens.decoded),
    money(report.totals.costs.input_cost, report.currency),
    money(report.totals.costs.cache_cost, report.currency),
    money(report.totals.costs.decoded_cost, report.currency),
    money(report.totals.costs.total_cost, report.currency),
  ];
  totalCells.forEach((value, index) => {
    const td = document.createElement("td");
    td.textContent = value;
    if (index > 1) td.className = "num";
    totals.appendChild(td);
  });
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

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  sessionStorage.setItem("tokenomicsToken", document.getElementById("token").value.trim());
  try {
    await loadPricing();
    showApp();
    await loadShowback();
  } catch (err) {
    if (err.message !== "unauthorized") showLogin(err.message);
  }
});

document.getElementById("pricing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
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

bindPairs();
if (token()) {
  loadPricing().then(showApp).then(loadShowback).catch((err) => showLogin(err.message));
}
