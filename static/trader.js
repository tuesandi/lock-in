"use strict";

// ── Trader-Werkzeuge ──────────────────────────────────────────────────────────
// Nutzt api() und esc() aus app.js. Alle Beträge in der Basiswährung.

(() => {
  const $ = (id) => document.getElementById(id);
  let settings = null;
  let targets = [];

  // Escaping auch für Attribute (esc() aus app.js lässt Anführungszeichen stehen)
  const attr = (s) => esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const safeUrl = (u) => (/^https:\/\//i.test(u || "") ? attr(u) : "#");

  const cur = () => (settings && settings.base_currency) || "CHF";
  const num = (v, d = 2) =>
    v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-CH", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v) => (v == null ? "–" : `${num(v, 2)} ${cur()}`);
  const usd = (v) => (v == null ? "–" : `$${Math.round(v).toLocaleString("de-CH")}`);
  const pct = (v, d = 1) => (v == null ? "–" : `${num(v, d)} %`);
  const signed = (v, d = 1) => (v == null ? "–" : `${v > 0 ? "+" : ""}${num(v, d)}`);

  const loading = (box, text) => {
    box.hidden = false;
    box.innerHTML = `<div class="analysis-loading">${esc(text)}</div>`;
  };
  const showError = (box, msg) => {
    box.hidden = false;
    box.innerHTML = `<div class="analysis-error">${esc(msg)}</div>`;
  };
  const notesList = (notes) =>
    notes && notes.length ? `<ul class="tr-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";

  async function call(url, opts, box, waitText) {
    if (box) loading(box, waitText || "Lade…");
    try {
      const res = await api(url, opts);
      if (!res) return null;
      if (res.error) {
        if (box) showError(box, res.error);
        return null;
      }
      return res;
    } catch (err) {
      if (box) showError(box, "Keine gültige Antwort vom Server: " + err.message);
      return null;
    }
  }

  // ── Tabs ────────────────────────────────────────────────────────────────────

  document.querySelectorAll(".tr-tab").forEach((btn) => {
    btn.addEventListener("click", () => openPanel(btn.dataset.panel));
  });

  function openPanel(name) {
    document.querySelectorAll(".tr-tab").forEach((b) => b.classList.toggle("active", b.dataset.panel === name));
    document.querySelectorAll(".tr-panel").forEach((p) => p.classList.toggle("active", p.id === `tr-panel-${name}`));
    if (name === "copycat" && !$("tr-filers").children.length) searchFilers("");
  }

  // ── Einstellungen ───────────────────────────────────────────────────────────

  const FIELDS = {
    "tr-base": "base_currency", "tr-cash": "cash", "tr-buffer": "cash_buffer_pct",
    "tr-cap": "max_position_pct", "tr-max-order": "max_order_usd", "tr-min-order": "min_order_usd",
    "tr-band": "rebalance_band_pct", "tr-max-day": "max_orders_per_day",
  };

  function fillSettings() {
    for (const [id, key] of Object.entries(FIELDS)) $(id).value = settings[key] ?? "";
    $("tr-blocked").value = (settings.blocked || []).join(", ");
    $("tr-members").value = (settings.pelosi_members || []).join(", ");
    $("tr-fractional").checked = !!settings.fractional;
    $("tr-push").checked = !!settings.pelosi_push;
    $("tr-currency-badge").textContent = cur();
    if (!$("tr-pelosi-members").dataset.touched) $("tr-pelosi-members").value = $("tr-members").value;
    const hint = $("tr-sec-hint");
    if (settings.sec_ready) {
      hint.textContent = "SEC-Zugang aktiv (Copycat und 8-K-Meldungen).";
      hint.classList.remove("warn");
    } else {
      hint.textContent = "SEC_USER_AGENT fehlt in der .env: Copycat und 8-K-Meldungen sind aus.";
      hint.classList.add("warn");
    }
  }

  async function loadSettings() {
    const s = await call("/api/trader/settings");
    if (s) {
      settings = s;
      fillSettings();
    }
  }

  $("tr-save-settings").addEventListener("click", async () => {
    const body = {};
    for (const [id, key] of Object.entries(FIELDS)) body[key] = $(id).value;
    body.blocked = $("tr-blocked").value;
    body.pelosi_members = $("tr-members").value;
    body.fractional = $("tr-fractional").checked;
    body.pelosi_push = $("tr-push").checked;
    const btn = $("tr-save-settings");
    const oldBase = cur();
    // Ein Knopf für Profil (app.js) und Regeln
    const [res, profile] = await Promise.all([
      api("/api/trader/settings", { method: "PUT", body: JSON.stringify(body) }).catch(() => null),
      typeof saveProfile === "function" ? saveProfile().catch(() => null) : Promise.resolve({}),
    ]);
    if (!res || res.error || !profile || profile.error) {
      btn.textContent = (res && res.error) || (profile && profile.error) || "Fehler";
    } else {
      settings = res;
      fillSettings();
      btn.textContent = "✓ Gespeichert";
      if (cur() !== oldBase && typeof loadLivePositions === "function") loadLivePositions();
    }
    setTimeout(() => { btn.textContent = "Speichern"; }, 2500);
  });

  $("tr-pelosi-members").addEventListener("input", (e) => { e.target.dataset.touched = "1"; });

  // ── Zielgewichte ────────────────────────────────────────────────────────────

  function renderTargets(source) {
    const box = $("tr-targets");
    $("tr-target-source").textContent = source ? `· ${source}` : "";
    if (!targets.length) {
      box.innerHTML = '<div class="empty-hint">Noch kein Ziel. Übernimm dein Portfolio oder ein 13F-Portfolio – oder füge Titel hinzu.</div>';
    } else {
      box.innerHTML = targets
        .map((t, i) => `
        <div class="tr-target" data-i="${i}">
          <input class="t-ticker" value="${attr(t.ticker)}" placeholder="Ticker" aria-label="Ticker">
          <input class="t-name" value="${attr(t.name || "")}" placeholder="Name (optional)" aria-label="Name">
          <input class="t-weight" type="number" min="0" step="any" value="${attr(+Number(t.weight).toFixed(2))}" aria-label="Gewicht in Prozent">
          <span class="t-pct">%</span>
          <button class="del-btn" title="Entfernen">✕</button>
        </div>`)
        .join("");
      box.querySelectorAll(".tr-target").forEach((row) => {
        const i = +row.dataset.i;
        row.querySelector(".t-ticker").addEventListener("input", (e) => { targets[i].ticker = e.target.value.trim().toUpperCase(); });
        row.querySelector(".t-name").addEventListener("input", (e) => { targets[i].name = e.target.value; });
        row.querySelector(".t-weight").addEventListener("input", (e) => { targets[i].weight = parseFloat(e.target.value) || 0; updateSum(); });
        row.querySelector(".del-btn").addEventListener("click", () => { targets.splice(i, 1); renderTargets(source); });
      });
    }
    updateSum();
  }

  function updateSum() {
    const sum = targets.reduce((s, t) => s + (Number(t.weight) || 0), 0);
    $("tr-target-sum").textContent = targets.length
      ? `Summe ${num(sum, 1)} %${Math.abs(sum - 100) > 0.05 ? " – wird beim Speichern auf 100 % skaliert" : ""}`
      : "";
  }

  async function loadTargets() {
    const r = await call("/api/trader/targets");
    if (r) {
      targets = r.targets;
      renderTargets(r.source);
    }
  }

  $("tr-add-target").addEventListener("click", () => {
    targets.push({ ticker: "", name: "", weight: 0 });
    renderTargets($("tr-target-source").textContent.replace(/^· /, ""));
    const inputs = $("tr-targets").querySelectorAll(".t-ticker");
    inputs[inputs.length - 1].focus();
  });

  $("tr-save-targets").addEventListener("click", async () => {
    const btn = $("tr-save-targets");
    const clean = targets.filter((t) => t.ticker && Number(t.weight) > 0);
    const res = await api("/api/trader/targets", {
      method: "PUT",
      body: JSON.stringify({ targets: clean, source: "Manuell" }),
    }).catch(() => null);
    if (!res || res.error) {
      btn.textContent = (res && res.error) || "Fehler";
    } else {
      targets = res.targets;
      renderTargets(res.source);
      btn.textContent = "✓ Gespeichert";
    }
    setTimeout(() => { btn.textContent = "Ziel speichern"; }, 2500);
  });

  $("tr-from-portfolio").addEventListener("click", async () => {
    const btn = $("tr-from-portfolio");
    btn.disabled = true;
    btn.textContent = "Lade Kurse…";
    const r = await call("/api/trader/targets/from-portfolio", { method: "POST" });
    btn.disabled = false;
    btn.textContent = "Aktuelles Portfolio übernehmen";
    if (r) {
      targets = r.targets;
      renderTargets(r.source);
    } else {
      alert("Konnte das Portfolio nicht übernehmen (Kurse nicht verfügbar?).");
    }
  });

  // ── Orders (Rebalancing + Budget) ───────────────────────────────────────────

  function ordersHtml(orders) {
    if (!orders || !orders.length) return '<div class="analysis-para">Keine Trades nötig.</div>';
    return `<ul class="tr-orders">${orders
      .map((o) => {
        const pass = o.verdict === "PASS";
        const amount = o.notional != null ? money(Number(o.notional)) : `${esc(o.qty)} Stück${o.value != null ? ` ≈ ${money(o.value)}` : ""}`;
        const reasons = (o.reasons || []).concat(o.warnings || []);
        return `
        <li class="tr-order ${pass ? "pass" : "block"}">
          <div class="tr-order-head">
            <span class="tr-pill ${o.side === "buy" ? "buy" : "sell"}">${o.side === "buy" ? "Kaufen" : "Verkaufen"}</span>
            <strong>${esc(o.symbol)}</strong>
            <span>${amount}</span>
            <span class="tr-pill ${pass ? "pass" : "block"}" title="Risiko-Check">${pass ? "Risiko OK" : "Blockiert"}</span>
          </div>
          ${o.why ? `<div class="tr-sub">${esc(o.why)}</div>` : ""}
          ${reasons.length ? `<ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        </li>`;
      })
      .join("")}</ul>`;
  }

  // ── Rebalancing ─────────────────────────────────────────────────────────────

  const ACTIONS = {
    buy: ["buy", "Kaufen"], sell: ["sell", "Verkaufen"], "sell all": ["sell", "Alles verkaufen"],
    hold: ["hold", "Halten"], "untracked, left alone": ["neutral", "Nicht im Ziel"],
  };

  $("tr-rebalance-btn").addEventListener("click", async () => {
    const btn = $("tr-rebalance-btn");
    const box = $("tr-rebalance-out");
    btn.disabled = true;
    const r = await call(
      "/api/trader/rebalance",
      { method: "POST", body: JSON.stringify({ sell_untracked: $("tr-sell-untracked").checked }) },
      box,
      "Kurse werden geladen und das Rebalancing berechnet…",
    );
    btn.disabled = false;
    if (!r) return;
    const rows = r.rows
      .map((row) => {
        const [cls, label] = ACTIONS[row.action] || ["neutral", row.action];
        const d = row.drift;
        return `<tr>
          <td><strong>${esc(row.ticker)}</strong><span class="tr-sub">${esc(row.name || "")}</span></td>
          <td class="num">${money(row.value)}</td>
          <td class="num">${pct(row.current_pct)}</td>
          <td class="num">${row.tracked ? pct(row.target_pct) : "–"}</td>
          <td class="num ${Math.abs(d) > r.band ? (d > 0 ? "neg" : "pos") : ""}">${row.tracked ? signed(d) : "–"}</td>
          <td><span class="tr-pill ${cls}">${label}</span></td>
        </tr>`;
      })
      .join("");
    box.innerHTML = `
      <div class="tr-summary">
        <div class="tr-stat"><b>${money(r.account_value)}</b><span>Depotwert</span></div>
        <div class="tr-stat"><b>${money(r.cash)}</b><span>Cash</span></div>
        <div class="tr-stat"><b>${money(r.cash_buffer)}</b><span>Puffer</span></div>
        <div class="tr-stat"><b>${num(r.band, 1)} Pkt.</b><span>Band</span></div>
      </div>
      <div class="tr-section-title">Abweichung vom Ziel</div>
      <div class="tr-table-wrap"><table class="tr-table">
        <thead><tr><th>Titel</th><th class="num">Wert</th><th class="num">Ist</th><th class="num">Ziel</th><th class="num">Drift</th><th>Aktion</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <div class="tr-section-title">Vorgeschlagene Trades</div>
      ${ordersHtml(r.orders)}
      ${notesList(r.notes)}`;
  });

  // ── Neues Geld ──────────────────────────────────────────────────────────────

  async function runBudget() {
    const btn = $("tr-budget-btn");
    const box = $("tr-budget-out");
    const amount = parseFloat($("tr-amount").value);
    if (!amount || amount <= 0) {
      showError(box, "Bitte einen Betrag eingeben.");
      return;
    }
    btn.disabled = true;
    const r = await call(
      "/api/trader/budget",
      { method: "POST", body: JSON.stringify({ amount, whole_shares: $("tr-whole").checked }) },
      box,
      "Kurse werden geladen und der Betrag verteilt…",
    );
    btn.disabled = false;
    if (!r) return;
    const rows = r.positions
      .map((p) => `<tr>
        <td><strong>${esc(p.ticker)}</strong><span class="tr-sub">${esc(p.name || "")}</span></td>
        <td class="num">${pct(p.weight)}</td>
        <td class="num">${money(p.dollars)}${p.capped ? ' <span class="tr-pill warn" title="Auf die Obergrenze pro Titel gekappt">Cap</span>' : ""}</td>
        <td class="num">${p.shares != null ? num(p.shares, r.whole_shares ? 0 : 4) : "–"}</td>
      </tr>`)
      .join("");
    const dropped = (r.dropped || []).map((d) => `${d.ticker}: ${d.reason}`);
    box.innerHTML = `
      <div class="tr-summary">
        <div class="tr-stat"><b>${money(r.budget)}</b><span>Betrag</span></div>
        <div class="tr-stat"><b>${money(r.invested)}</b><span>Investiert</span></div>
        <div class="tr-stat"><b>${money(r.cash_buffer)}</b><span>Puffer</span></div>
        <div class="tr-stat"><b>${money(r.idle)}</b><span>Übrig</span></div>
      </div>
      <div class="tr-table-wrap"><table class="tr-table">
        <thead><tr><th>Titel</th><th class="num">Gewicht</th><th class="num">Betrag</th><th class="num">≈ Stück</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <div class="tr-section-title">Vorgeschlagene Käufe</div>
      ${ordersHtml(r.orders)}
      ${notesList(dropped.concat(r.notes || []))}`;
  }
  $("tr-budget-btn").addEventListener("click", runBudget);
  $("tr-amount").addEventListener("keydown", (e) => { if (e.key === "Enter") runBudget(); });

  // ── News ────────────────────────────────────────────────────────────────────

  const TAGS = {
    earnings: "Zahlen", analyst: "Analysten", legal: "Recht", deal: "Übernahme",
    capital: "Dividende/Kapital", people: "Management", risk: "Risiko",
  };

  $("tr-news-btn").addEventListener("click", async () => {
    const btn = $("tr-news-btn");
    const box = $("tr-news-out");
    btn.disabled = true;
    const r = await call(`/api/trader/news?hours=${$("tr-news-hours").value}`, {}, box, "Schlagzeilen und Meldungen werden geladen…");
    btn.disabled = false;
    if (!r) return;
    const items = r.items
      .map((it) => {
        const when = it.time ? new Date(it.time).toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
        const who = (it.touches && it.touches.length ? it.touches : [it.ticker]).map((t) => `<span class="tr-pill info">${esc(t)}</span>`).join("");
        const tags = (it.tags || []).map((t) => `<span class="tr-pill neutral">${esc(TAGS[t] || t)}</span>`).join("");
        const imp = it.important ? '<span class="tr-pill warn">Wichtig</span>' : "";
        return `<li>
          <a href="${safeUrl(it.link)}" target="_blank" rel="noopener noreferrer">${esc(it.title)}</a>
          <div class="tr-news-meta">${imp}${who}${tags}<span>${esc(it.source)} · ${when}</span></div>
        </li>`;
      })
      .join("");
    const notes = r.errors.slice(0, 6);
    if (!r.sec) notes.push("SEC-8-K-Meldungen sind aus, weil SEC_USER_AGENT fehlt.");
    notes.push("Schlagzeilen sind Hinweise, keine Fakten – vor dem Handeln den Artikel lesen.");
    box.innerHTML = `${items ? `<ul class="tr-news">${items}</ul>` : '<div class="analysis-para">Keine Meldungen in diesem Zeitraum.</div>'}${notesList(notes)}`;
  });

  // ── Copycat ─────────────────────────────────────────────────────────────────

  let filerTimer = null;
  $("tr-filer-q").addEventListener("input", (e) => {
    clearTimeout(filerTimer);
    filerTimer = setTimeout(() => searchFilers(e.target.value.trim()), 350);
  });

  async function searchFilers(q) {
    const list = $("tr-filers");
    const r = await call(`/api/trader/copycat/filers?q=${encodeURIComponent(q)}`);
    if (!r) return;
    if (!r.filers.length) {
      list.innerHTML = '<li class="empty-hint">Kein Investor gefunden.</li>';
      return;
    }
    list.innerHTML = r.filers
      .map((f) => `<li><button data-cik="${attr(f.cik)}" data-name="${attr(f.name)}">${esc(f.name)}
        ${f.person ? `<span class="tr-sub">${esc(f.person)}</span>` : ""}</button></li>`)
      .join("");
    list.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        list.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
        loadCopycat(b.dataset.cik);
      }),
    );
  }

  const STATUS = {
    NEW: ["new-pos", "Neu"], "SOLD OUT": ["sold", "Verkauft"], ADDED: ["buy", "Aufgestockt"], TRIMMED: ["sell", "Reduziert"],
  };

  async function loadCopycat(cik, top = 10) {
    const box = $("tr-copycat-out");
    const r = await call(`/api/trader/copycat/${encodeURIComponent(cik)}?top=${top}`, {}, box,
      "13F-Meldungen werden bei der SEC geladen… (kann 10–20 Sekunden dauern)");
    if (!r) return;
    const m = r.meta;
    const changes = r.changes
      .slice(0, 15)
      .map((c) => {
        const [cls, label] = STATUS[c.status] || ["neutral", c.status];
        return `<tr><td><span class="tr-pill ${cls}">${label}</span></td>
          <td><strong>${esc(c.ticker || "–")}</strong><span class="tr-sub">${esc(c.issuer)}</span></td>
          <td class="num">${c.change_pct != null ? signed(c.change_pct, 0) + " %" : ""}</td>
          <td class="num">${usd(c.value_after || c.value_before)}</td></tr>`;
      })
      .join("");
    const weights = r.weights
      .map((w) => `<tr><td><strong>${esc(w.ticker)}</strong>${w.check_ticker ? ' <span class="tr-pill warn" title="Ticker automatisch zugeordnet – bitte prüfen">prüfen</span>' : ""}
          <span class="tr-sub">${esc(w.name)}</span></td>
          <td class="num">${pct(w.pct_of_13f, 2)}</td><td class="num"><strong>${pct(w.weight, 2)}</strong></td>
          <td class="num">${usd(w.value)}</td></tr>`)
      .join("");
    box.innerHTML = `
      <div class="tr-summary">
        <div class="tr-stat"><b>${esc(m.filer)}</b><span>Investor</span></div>
        <div class="tr-stat"><b>${esc(m.period)}</b><span>Stichtag</span></div>
        <div class="tr-stat"><b>${esc(m.filed)}</b><span>Eingereicht</span></div>
        <div class="tr-stat"><b>${usd(r.total)}</b><span>13F-Volumen</span></div>
      </div>
      <div class="tr-row">
        <label class="tr-hint" for="tr-copy-top">Top</label>
        <select id="tr-copy-top" class="tr-input" style="flex:0 0 auto">
          ${[5, 10, 15, 20].map((n) => `<option value="${n}" ${n === top ? "selected" : ""}>${n} Positionen</option>`).join("")}
        </select>
        <button class="btn-analyze btn-discover" id="tr-copy-apply" style="flex:1 1 200px">Als Zielgewichte übernehmen</button>
      </div>
      <div class="tr-section-title">Zielgewichte (Top ${top}, auf 100 % skaliert)</div>
      <div class="tr-table-wrap"><table class="tr-table">
        <thead><tr><th>Titel</th><th class="num">Anteil 13F</th><th class="num">Ziel</th><th class="num">Wert</th></tr></thead>
        <tbody>${weights}</tbody></table></div>
      ${changes ? `<div class="tr-section-title">Änderungen seit ${esc(r.prior_period || "Vorquartal")}</div>
      <div class="tr-table-wrap"><table class="tr-table"><tbody>${changes}</tbody></table></div>` : ""}
      ${notesList((m.notes || []).concat(r.caveats || []))}`;
    $("tr-copy-top").addEventListener("change", (e) => loadCopycat(cik, +e.target.value));
    $("tr-copy-apply").addEventListener("click", async () => {
      const btn = $("tr-copy-apply");
      btn.disabled = true;
      const res = await call(`/api/trader/copycat/${encodeURIComponent(cik)}/apply`, {
        method: "POST", body: JSON.stringify({ top }),
      });
      btn.disabled = false;
      if (!res) {
        btn.textContent = "Fehler beim Übernehmen";
        return;
      }
      targets = res.targets;
      renderTargets(res.source);
      openPanel("rebalance");
      $("tr-targets").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  // ── Kongress (PTR) ──────────────────────────────────────────────────────────

  const OWNER = { self: "selbst", spouse: "Ehepartner", joint: "gemeinsam", "dependent child": "Kind" };
  const TXN = { purchase: ["buy", "Kauf"], sale: ["sell", "Verkauf"], "partial sale": ["sell", "Teilverkauf"], exchange: ["neutral", "Tausch"] };

  $("tr-pelosi-btn").addEventListener("click", async () => {
    const btn = $("tr-pelosi-btn");
    const box = $("tr-pelosi-out");
    btn.disabled = true;
    const members = $("tr-pelosi-members").value.trim();
    const r = await call(
      `/api/trader/pelosi?members=${encodeURIComponent(members)}&days=${$("tr-pelosi-days").value}`, {}, box,
      "Index des House Clerk wird geladen…",
    );
    btn.disabled = false;
    if (!r) return;
    if (!r.filings.length) {
      box.innerHTML = `<div class="analysis-para">Keine Meldungen${members ? ` von ${esc(members)}` : ""} in den letzten ${r.days} Tagen.</div>`;
      return;
    }
    box.innerHTML = r.filings
      .map((f) => `
      <div class="tr-filing" data-doc="${attr(f.doc_id)}" data-year="${attr(f.year)}" data-filed="${attr(f.filed || "")}">
        <div class="tr-filing-head">
          ${f.new ? '<span class="tr-pill info">Neu</span>' : ""}
          <strong>${esc(f.name)}</strong><span class="tr-hint">${esc(f.district)} · eingereicht ${esc(f.filed || "?")}</span>
          <button class="tr-link tr-ptr-open">Trades anzeigen</button>
          <a class="tr-link" href="${safeUrl(f.url)}" target="_blank" rel="noopener noreferrer">PDF</a>
        </div>
        <div class="tr-filing-body" hidden></div>
      </div>`)
      .join("") + notesList(["Öffentliche Meldungen nach dem STOCK Act – kein Insiderwissen. Beträge sind Spannen in USD."]);
    box.querySelectorAll(".tr-ptr-open").forEach((b) => b.addEventListener("click", () => openPtr(b.closest(".tr-filing"), b)));
  });

  async function openPtr(el, btn) {
    const body = el.querySelector(".tr-filing-body");
    if (!body.hidden && body.innerHTML) {
      body.hidden = true;
      btn.textContent = "Trades anzeigen";
      return;
    }
    btn.textContent = "Ausblenden";
    const { doc, year, filed } = el.dataset;
    const r = await call(`/api/trader/pelosi/ptr/${encodeURIComponent(doc)}?year=${year}&filed=${encodeURIComponent(filed)}`, {}, body,
      "PDF wird geladen und gelesen…");
    if (!r) return;
    if (!r.rows.length) {
      body.innerHTML = '<div class="analysis-para">Keine Transaktionen erkannt – bitte das PDF direkt öffnen.</div>';
      return;
    }
    body.innerHTML = `<div class="tr-table-wrap"><table class="tr-table">
      <thead><tr><th>Art</th><th>Titel</th><th>Betrag</th><th>Datum</th><th>Wer</th></tr></thead>
      <tbody>${r.rows
        .map((t) => {
          const [cls, label] = TXN[t.type_name] || ["neutral", t.type_name];
          let detail = "";
          if (t.option) detail = `${t.option.contracts} ${t.option.kind === "call" ? "Calls" : "Puts"}, Strike $${t.option.strike}, bis ${t.option.expires}`;
          else if (t.shares) detail = `${t.shares.toLocaleString("de-CH")} Aktien`;
          const late = t.late ? ' <span class="tr-pill warn" title="Mehr als 45 Tage nach dem Trade gemeldet">verspätet</span>' : "";
          return `<tr class="${t.in_portfolio ? "mine" : ""}">
            <td><span class="tr-pill ${cls}">${esc(label)}</span></td>
            <td><strong>${esc(t.ticker || "–")}</strong>${t.in_portfolio ? ' <span class="tr-pill info">in deinem Portfolio</span>' : ""}
              <span class="tr-sub">${esc(t.asset)}${detail ? ` · ${esc(detail)}` : ""}</span></td>
            <td>${esc(t.amount)}</td>
            <td>${esc(t.date || "")}${t.days_to_disclose != null ? `<span class="tr-sub">+${t.days_to_disclose} Tage gemeldet${late}</span>` : ""}</td>
            <td>${esc(OWNER[t.owner_name] || t.owner_name || "")}</td>
          </tr>`;
        })
        .join("")}</tbody></table></div>`;
  }

  // ── Init ────────────────────────────────────────────────────────────────────

  loadSettings();
  loadTargets();
})();
