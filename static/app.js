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

// ── Init ──────────────────────────────────────────────────────────────────────

(async () => {
  await Promise.all([loadGoal(), loadEntries(), loadTodos()]);
  initCalendar();
})();
