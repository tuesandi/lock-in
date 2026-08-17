"use strict";

const today = new Date().toISOString().split("T")[0];

document.getElementById("current-date").textContent = new Date().toLocaleDateString("de-DE", {
  weekday: "long", year: "numeric", month: "long", day: "numeric",
});

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 204) return null;
  return res.json();
}

// ── Calories ──────────────────────────────────────────────────────────────────

let calorieGoal = 2500;
let entries = [];

async function loadGoal() {
  // Show localStorage value immediately to avoid flicker on slow connections
  const cached = parseInt(localStorage.getItem("calorieGoal") || "0", 10);
  if (cached > 0) {
    calorieGoal = cached;
    document.getElementById("calorie-goal-input").value = calorieGoal;
  }

  try {
    const data = await api("/api/calories/goal");
    if (data && data.goal) {
      calorieGoal = data.goal;
      document.getElementById("calorie-goal-input").value = calorieGoal;
      localStorage.setItem("calorieGoal", String(calorieGoal));
    }
  } catch {
    // Network failed — keep localStorage value
  }
}

async function saveGoal() {
  const val = parseInt(document.getElementById("calorie-goal-input").value, 10);
  if (!val || val < 0) return;
  // Persist locally first so a reload always shows the correct value
  localStorage.setItem("calorieGoal", String(val));
  calorieGoal = val;
  updateProgress();
  try {
    await api("/api/calories/goal", {
      method: "PUT",
      body: JSON.stringify({ goal: val }),
    });
  } catch {
    // DB sync failed; localStorage already updated as backup
  }
}

document.getElementById("save-goal-btn").addEventListener("click", saveGoal);
document.getElementById("calorie-goal-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveGoal();
});

async function loadEntries() {
  entries = await api(`/api/calories/entries?date=${today}`);
  renderEntries();
  updateProgress();
}

function renderEntries() {
  const list = document.getElementById("calorie-entries");
  if (!entries.length) {
    list.innerHTML = '<li class="empty-hint">Noch keine Einträge heute.</li>';
    return;
  }
  list.innerHTML = entries
    .map(
      (e) => `
    <li class="entry-item">
      <span class="entry-desc">${esc(e.description)}</span>
      <span class="entry-kcal">${e.calories} kcal</span>
      <button class="del-btn" data-id="${e.id}" title="Löschen">✕</button>
    </li>`
    )
    .join("");
  list.querySelectorAll(".del-btn").forEach((btn) =>
    btn.addEventListener("click", () => deleteEntry(+btn.dataset.id))
  );
}

function updateProgress() {
  const consumed = entries.reduce((s, e) => s + e.calories, 0);
  const remaining = calorieGoal - consumed;
  const pct = calorieGoal > 0 ? Math.min((consumed / calorieGoal) * 100, 100) : 0;
  const over = remaining < 0;

  document.getElementById("calories-consumed").textContent = consumed;

  const remEl = document.getElementById("calories-remaining");
  remEl.textContent = Math.abs(remaining);
  remEl.className = "stat-value secondary" + (over ? " over" : "");

  document.getElementById("remaining-label").textContent = over ? "überschritten" : "verbleibend";

  const bar = document.getElementById("calorie-progress");
  bar.style.width = pct + "%";
  bar.className = "progress-bar" + (over ? " over" : "");
}

document.getElementById("calorie-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const desc = document.getElementById("food-description").value.trim();
  const kcal = parseInt(document.getElementById("food-calories").value, 10);
  if (!desc || !kcal) return;
  await api("/api/calories/entries", {
    method: "POST",
    body: JSON.stringify({ description: desc, calories: kcal, date: today }),
  });
  e.target.reset();
  await loadEntries();
});

async function deleteEntry(id) {
  await api(`/api/calories/entries/${id}`, { method: "DELETE" });
  await loadEntries();
}

// ── To-Dos ────────────────────────────────────────────────────────────────────

let todos = [];

async function loadTodos() {
  todos = await api("/api/todos");
  renderTodos();
}

function renderTodos() {
  const open = todos.filter((t) => !t.completed);
  const done = todos.filter((t) => t.completed);
  document.getElementById("todo-count").textContent = `${open.length} offen`;

  const list = document.getElementById("todo-list");
  if (!todos.length) {
    list.innerHTML = '<li class="empty-hint">Keine Aufgaben. Gut gemacht!</li>';
    return;
  }

  list.innerHTML = [...open, ...done]
    .map((t) => {
      let due = "";
      if (t.due_date) {
        const d = new Date(t.due_date + "T12:00:00");
        due = `<span class="todo-due">${d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}</span>`;
      }
      return `
      <li class="todo-item${t.completed ? " done" : ""}" data-id="${t.id}">
        <input type="checkbox" ${t.completed ? "checked" : ""}>
        <span class="todo-title">${esc(t.title)}</span>
        ${due}
        <button class="del-btn" title="Löschen">✕</button>
      </li>`;
    })
    .join("");

  list.querySelectorAll(".todo-item").forEach((li) => {
    const id = +li.dataset.id;
    li.querySelector("input[type=checkbox]").addEventListener("change", function () {
      toggleTodo(id, this.checked);
    });
    li.querySelector(".del-btn").addEventListener("click", () => deleteTodo(id));
  });
}

document.getElementById("todo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = document.getElementById("todo-title").value.trim();
  const dueDate = document.getElementById("todo-due-date").value || null;
  if (!title) return;
  await api("/api/todos", {
    method: "POST",
    body: JSON.stringify({ title, due_date: dueDate }),
  });
  e.target.reset();
  await loadTodos();
});

async function toggleTodo(id, completed) {
  await api(`/api/todos/${id}`, {
    method: "PUT",
    body: JSON.stringify({ completed: completed ? 1 : 0 }),
  });
  await loadTodos();
}

async function deleteTodo(id) {
  await api(`/api/todos/${id}`, { method: "DELETE" });
  await loadTodos();
}

// ── Termine (FullCalendar) ────────────────────────────────────────────────────

let selectedDate = today;
let calendar;

function formatDayTitle(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  if (dateStr === today) return "Heute";
  return d.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
}

function selectDay(dateStr) {
  selectedDate = dateStr;

  // Remove previous highlight
  document.querySelectorAll(".fc-day-selected").forEach((el) =>
    el.classList.remove("fc-day-selected")
  );
  // Add highlight to clicked day cell
  const dayEl = document.querySelector(`[data-date="${dateStr}"]`);
  if (dayEl) dayEl.classList.add("fc-day-selected");

  document.getElementById("day-panel-title").textContent = formatDayTitle(dateStr);
  loadTermineForDate(dateStr);
}

async function loadTermineForDate(dateStr) {
  const rows = await api(`/api/termine?date=${dateStr}`);
  renderTermine(rows);

  if (dateStr === today) {
    document.getElementById("termin-count").textContent = `${rows.length} heute`;
  }
}

function renderTermine(data) {
  const list = document.getElementById("termin-list");
  if (!data.length) {
    list.innerHTML = '<li class="empty-hint">Keine Termine an diesem Tag.</li>';
    return;
  }
  list.innerHTML = data
    .map((t) => {
      const time = t.time
        ? `<span class="termin-time">${esc(t.time)}</span>`
        : `<span class="termin-time" style="opacity:.3">––</span>`;
      return `
      <li class="termin-item">
        ${time}
        <span class="termin-title">${esc(t.title)}</span>
        <button class="del-btn" data-id="${t.id}" title="Löschen">✕</button>
      </li>`;
    })
    .join("");

  list.querySelectorAll(".del-btn").forEach((btn) =>
    btn.addEventListener("click", () => deleteTermin(+btn.dataset.id))
  );
}

document.getElementById("termin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = document.getElementById("termin-title").value.trim();
  const time = document.getElementById("termin-time").value || null;
  if (!title) return;
  await api("/api/termine", {
    method: "POST",
    body: JSON.stringify({ title, time, date: selectedDate }),
  });
  document.getElementById("termin-title").value = "";
  document.getElementById("termin-time").value = "";
  calendar.refetchEvents();
  await loadTermineForDate(selectedDate);
});

async function deleteTermin(id) {
  await api(`/api/termine/${id}`, { method: "DELETE" });
  calendar.refetchEvents();
  await loadTermineForDate(selectedDate);
}

function initCalendar() {
  const el = document.getElementById("termine-calendar");

  calendar = new FullCalendar.Calendar(el, {
    initialView: "dayGridMonth",
    locale: "de",
    height: "auto",
    firstDay: 1,
    headerToolbar: {
      left: "prev",
      center: "title",
      right: "next today",
    },
    events: async function (fetchInfo, successCallback, failureCallback) {
      try {
        const start = fetchInfo.startStr.slice(0, 10);
        const end   = fetchInfo.endStr.slice(0, 10);
        const rows  = await api(`/api/termine/range?start=${start}&end=${end}`);
        const events = rows.map((t) => ({
          id: String(t.id),
          title: t.title,
          start: t.time ? `${t.date}T${t.time}:00` : t.date,
          allDay: !t.time,
          extendedProps: { dbId: t.id },
        }));
        successCallback(events);
      } catch (err) {
        failureCallback(err);
      }
    },
    dateClick: function (info) {
      selectDay(info.dateStr);
    },
    eventClick: function (info) {
      info.jsEvent.preventDefault();
      selectDay(info.event.startStr.slice(0, 10));
    },
    // Re-apply selected day highlight after calendar re-render
    datesSet: function () {
      const dayEl = document.querySelector(`[data-date="${selectedDate}"]`);
      if (dayEl) dayEl.classList.add("fc-day-selected");
    },
  });

  calendar.render();
  selectDay(today);
}

// ── Finance / Investitionsberater ─────────────────────────────────────────────

let portfolio = [];
let editingPortfolioId = null;

async function loadPortfolio() {
  portfolio = await api("/api/portfolio");
  renderPortfolio();
  document.getElementById("portfolio-count").textContent = `${portfolio.length} Position${portfolio.length !== 1 ? "en" : ""}`;
}

function renderPortfolio() {
  const list = document.getElementById("portfolio-list");
  if (!portfolio.length) {
    list.innerHTML = '<li class="empty-hint">Noch keine Positionen eingetragen.</li>';
    return;
  }
  list.innerHTML = portfolio
    .map((p) => {
      const total = (Number(p.quantity) * Number(p.buy_price)).toLocaleString("de-CH", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
      const qtyFmt = Number(p.quantity).toLocaleString("de-CH", { maximumFractionDigits: 6 });
      const priceFmt = Number(p.buy_price).toLocaleString("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
      return `
      <li class="portfolio-item" data-id="${p.id}">
        <div class="portfolio-item-main">
          <span class="portfolio-name">${esc(p.asset_name)}</span>
          ${p.ticker ? `<span class="portfolio-ticker">${esc(p.ticker.toUpperCase())}</span>` : ""}
          <span class="portfolio-type-badge">${esc(p.asset_type)}</span>
        </div>
        <div class="portfolio-item-details">
          <span class="portfolio-qty">${qtyFmt} × ${priceFmt}</span>
          <span class="portfolio-sep">≈</span>
          <span class="portfolio-total">${total}</span>
        </div>
        <div class="portfolio-item-actions">
          <button class="btn-icon edit-btn" data-id="${p.id}" title="Bearbeiten">✎</button>
          <button class="del-btn" data-id="${p.id}" title="Löschen">✕</button>
        </div>
      </li>`;
    })
    .join("");

  list.querySelectorAll(".del-btn").forEach((btn) =>
    btn.addEventListener("click", () => deletePortfolioEntry(+btn.dataset.id))
  );
  list.querySelectorAll(".edit-btn").forEach((btn) =>
    btn.addEventListener("click", () => startEditPortfolio(+btn.dataset.id))
  );
}

document.getElementById("portfolio-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const assetName = document.getElementById("pf-name").value.trim();
  const ticker = document.getElementById("pf-ticker").value.trim();
  const assetType = document.getElementById("pf-type").value;
  const quantity = parseFloat(document.getElementById("pf-quantity").value);
  const buyPrice = parseFloat(document.getElementById("pf-buy-price").value);
  if (!assetName || !quantity || isNaN(buyPrice)) return;

  const body = JSON.stringify({ asset_name: assetName, ticker, asset_type: assetType, quantity, buy_price: buyPrice });

  if (editingPortfolioId) {
    await api(`/api/portfolio/${editingPortfolioId}`, { method: "PUT", body });
    editingPortfolioId = null;
    document.getElementById("pf-submit-btn").textContent = "+";
  } else {
    await api("/api/portfolio", { method: "POST", body });
  }

  e.target.reset();
  await loadPortfolio();
});

function startEditPortfolio(id) {
  const p = portfolio.find((x) => x.id === id);
  if (!p) return;
  editingPortfolioId = id;
  document.getElementById("pf-name").value = p.asset_name;
  document.getElementById("pf-ticker").value = p.ticker || "";
  document.getElementById("pf-type").value = p.asset_type;
  document.getElementById("pf-quantity").value = p.quantity;
  document.getElementById("pf-buy-price").value = p.buy_price;
  document.getElementById("pf-submit-btn").textContent = "✓";
  document.getElementById("pf-name").focus();
  document.getElementById("pf-name").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function deletePortfolioEntry(id) {
  await api(`/api/portfolio/${id}`, { method: "DELETE" });
  if (editingPortfolioId === id) {
    editingPortfolioId = null;
    document.getElementById("portfolio-form").reset();
    document.getElementById("pf-submit-btn").textContent = "+";
  }
  await loadPortfolio();
}

// ── Financial Profile ─────────────────────────────────────────────────────────

async function loadProfile() {
  try {
    const p = await api("/api/financial-profile");
    if (!p) return;
    if (p.risk_tolerance) document.getElementById("pf-risk").value = p.risk_tolerance;
    if (p.investment_goals) document.getElementById("pf-goals").value = p.investment_goals;
    if (p.monthly_budget) document.getElementById("pf-budget").value = p.monthly_budget;
  } catch {}
}

document.getElementById("save-profile-btn").addEventListener("click", async () => {
  const risk = document.getElementById("pf-risk").value || null;
  const goals = document.getElementById("pf-goals").value.trim() || null;
  const budget = parseFloat(document.getElementById("pf-budget").value) || null;
  await api("/api/financial-profile", {
    method: "PUT",
    body: JSON.stringify({ risk_tolerance: risk, investment_goals: goals, monthly_budget: budget }),
  });
  const btn = document.getElementById("save-profile-btn");
  const orig = btn.textContent;
  btn.textContent = "✓ Gespeichert";
  setTimeout(() => { btn.textContent = orig; }, 2000);
});

// ── Portfolio Analysis ────────────────────────────────────────────────────────

function renderAnalysis(text) {
  return text
    .split("\n")
    .map((line) => {
      if (line.startsWith("### ")) {
        return `<h4 class="analysis-heading">${esc(line.slice(4))}</h4>`;
      }
      if (!line.trim()) return '<div style="height:.35rem"></div>';

      const escaped = esc(line);
      const withBold = escaped.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");

      const up = line.toUpperCase();
      if (up.includes("→ KAUFEN") || up.includes("→ AUFSTOCKEN")) {
        return `<div class="rec-line rec-buy">${withBold}</div>`;
      }
      if (up.includes("→ VERKAUFEN")) {
        return `<div class="rec-line rec-sell">${withBold}</div>`;
      }
      if (up.includes("→ HALTEN")) {
        return `<div class="rec-line rec-hold">${withBold}</div>`;
      }
      return `<div class="analysis-para">${withBold}</div>`;
    })
    .join("");
}

// ── Tab Navigation ────────────────────────────────────────────────────────────

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const pane = document.getElementById("tab-" + btn.dataset.tab);
    if (pane) pane.classList.add("active");
    if (btn.dataset.tab === "planer" && typeof calendar !== "undefined" && calendar) {
      setTimeout(() => calendar.updateSize(), 50);
    }
  });
});

// ── Portfolio Analyze ──────────────────────────────────────────────────────────

document.getElementById("analyze-btn").addEventListener("click", async () => {
  const btn = document.getElementById("analyze-btn");
  const box = document.getElementById("analysis-box");
  const content = document.getElementById("analysis-content");

  btn.disabled = true;
  btn.textContent = "Analysiere… ⏳";
  box.style.display = "block";
  content.innerHTML =
    '<div class="analysis-loading">Marktdaten werden geladen und KI analysiert dein Portfolio…<br>Das kann 15–30 Sekunden dauern.</div>';
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    const result = await api("/api/finance/analyze", { method: "POST" });

    if (result.error) {
      content.innerHTML = `<div class="analysis-error">${esc(result.error)}</div>`;
    } else {
      let html = "";
      if (result.portfolio_value) {
        const valFmt = Number(result.portfolio_value).toLocaleString("de-CH", {
          minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
        html += `<div class="analysis-total">Gesamtportfolio-Wert (Einkaufspreise): $${valFmt}</div>`;
      }
      html += renderAnalysis(result.analysis);
      content.innerHTML = html;
    }
  } catch (err) {
    content.innerHTML = `<div class="analysis-error">Netzwerkfehler: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Portfolio jetzt analysieren 🧠";
  }
});

// ── Discover New Investments ──────────────────────────────────────────────────

document.getElementById("discover-btn").addEventListener("click", async () => {
  const btn = document.getElementById("discover-btn");
  const box = document.getElementById("discover-box");
  const content = document.getElementById("discover-content");

  btn.disabled = true;
  btn.textContent = "Suche… ⏳";
  box.style.display = "block";
  content.innerHTML =
    '<div class="analysis-loading">KI analysiert Diversifikation und sucht passende Anlagen…<br>Einen Moment bitte.</div>';
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });

  try {
    const result = await api("/api/finance/recommend_investments", { method: "POST" });
    if (result.error) {
      content.innerHTML = `<div class="analysis-error">${esc(result.error)}</div>`;
    } else {
      content.innerHTML = renderAnalysis(result.recommendations);
    }
  } catch (err) {
    content.innerHTML = `<div class="analysis-error">Netzwerkfehler: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Neue Anlagen entdecken 🔍";
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

(async () => {
  await Promise.all([loadGoal(), loadEntries(), loadTodos(), loadPortfolio(), loadProfile()]);
  initCalendar();
})();
