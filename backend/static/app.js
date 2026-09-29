const $ = (sel) => document.querySelector(sel);

const RULE_NAMES = {
  CONVERTED_ANNIVERSARY: "Yıl Dönümü",
  RESULT_CODE_RETRY: "Sonuç Koduna Göre Yeniden Deneme",
};

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function toast(msg, ok = true) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (ok ? "" : " error");
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4500);
}

async function loadStats() {
  const s = await api("/api/stats");
  const cards = [
    { label: "Toplam Misafir", value: s.guests },
    { label: "Satın Alan (Converted)", value: s.converted },
    { label: "Potansiyel (Lead)", value: s.leads },
    { label: "Bekleyen Görev", value: s.pending_tasks },
    { label: "Vadesi Gelen Görev", value: s.due_tasks, hot: true },
  ];
  $("#kpis").innerHTML = cards.map((c) =>
    `<div class="kpi${c.hot ? " hot" : ""}">
       <span class="kpi-label">${c.label}</span>
       <span class="kpi-value">${c.value}</span>
     </div>`).join("");
}

function taskRow(t) {
  const today = new Date().toISOString().slice(0, 10);
  const state = t.overdue
    ? (t.scheduled_date === today
        ? '<span class="chip today">Bugün</span>'
        : '<span class="chip over">Vadesi geçti</span>')
    : '<span class="chip plan">Planlı</span>';
  const seg = t.segment === "converted"
    ? '<span class="badge conv">Satın Alan</span>'
    : '<span class="badge lead">Lead</span>';
  const notes = (t.notes || "").replace(/"/g, "&quot;");
  return `<tr>
    <td><div class="who"><strong>${t.name}</strong><span>${t.phone}</span></div></td>
    <td>${seg}</td>
    <td><div class="rule"><strong>${RULE_NAMES[t.trigger_rule] || t.trigger_rule}</strong><span>${t.result_code || "-"}</span></div></td>
    <td><div class="when"><strong>${t.scheduled_date}</strong>${state}</div></td>
    <td>${t.target_period || "-"}</td>
    <td><span class="badge p${t.priority}">${t.priority_name}</span></td>
    <td>${t.attempt_no}</td>
    <td class="notes" title="${notes}">${t.notes || "-"}</td>
  </tr>`;
}

async function loadTasks() {
  const seg = $("#f-segment").value;
  const due = $("#f-due").value;
  const data = await api(`/api/tasks?segment=${seg}&due=${due}`);
  document.querySelector("#task-table tbody").innerHTML =
    data.items.map(taskRow).join("");
  $("#task-empty").classList.toggle("hidden", data.items.length > 0);
}

async function loadRules() {
  const r = await api("/api/rules");
  document.querySelector("#rule-table tbody").innerHTML =
    r.lead_rules.map((x) => `<tr>
        <td><code>${x.code}</code></td>
        <td>${x.description}</td>
        <td>${x.strategy}</td>
        <td><span class="badge p${x.priority}">${x.priority_name}</span></td>
        <td>${x.max_attempts}</td>
      </tr>`).join("");
  const c = r.converted_rule;
  $("#converted-rule").innerHTML = [
    ["Kural", c.key],
    ["Tetikleme", `Rezervasyon +${c.trigger_after_months} ay`],
    ["Hedef Dönem", c.target],
    ["Öncelik", c.priority_name],
    ["Max Deneme", c.max_attempts],
  ].map(([k, v]) =>
    `<div class="kv"><span>${k}</span><strong>${v}</strong></div>`).join("");
  $("#tree-converted").textContent = r.converted_tree;
  $("#tree-lead").textContent = r.lead_tree;
}

async function loadSchema() {
  const s = await api("/api/schema");
  $("#schema-list").innerHTML = s.tables.map((t) => `<div class="tablecard">
      <div class="tc-head"><h3>${t.name}</h3><p>${t.description}</p></div>
      <table>
        <thead><tr><th>Kolon</th><th>Tip</th><th>Key</th></tr></thead>
        <tbody>
          ${t.columns.map((c) => `<tr>
            <td><code>${c.name}</code></td>
            <td>${c.type}</td>
            <td>${c.pk ? '<span class="badge pk">PK</span>' : ""}
                ${c.fk ? `<span class="badge fk">FK → ${c.fk}</span>` : ""}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`).join("");
}

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) =>
      b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === "panel-" + btn.dataset.tab));
    if (btn.dataset.tab === "rules") loadRules();
    if (btn.dataset.tab === "schema") loadSchema();
  }));

$("#f-segment").addEventListener("change", loadTasks);
$("#f-due").addEventListener("change", loadTasks);

$("#btn-engine").addEventListener("click", async () => {
  const btn = $("#btn-engine");
  btn.disabled = true;
  btn.textContent = "Çalıştırılıyor…";
  try {
    const r = await api("/api/engine/run", { method: "POST" });
    toast(`Motor çalıştı — ${r.created.converted} converted, ${r.created.non_converted} lead görevi oluşturuldu.`);
    await Promise.all([loadStats(), loadTasks()]);
  } catch (e) {
    toast("Motor çalıştırılamadı: " + e.message, false);
  }
  btn.disabled = false;
  btn.textContent = "⚙️ Motoru Çalıştır";
});

$("#btn-export").addEventListener("click", () => {
  window.location.href = "/api/export/csv";
});

loadStats();
loadTasks();
