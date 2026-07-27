// Read-only player-facing map viewer, served at /view/<token> with no login.
// A deliberately small subset of app.js: fetch + render, no editing, no
// world-file management, no auth. All data comes from the /api/public/<token>/*
// routes, which already have GM notes stripped server-side.

const TOKEN = window.location.pathname.split("/").pop();

let world = null;
let currentStyle = "classic";
let boundaryToggles = { duchy: false, county: false, barony: false };

const el = (id) => document.getElementById(id);

function showTab(tab) {
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.hidden = page.dataset.tabPage !== tab;
  });
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

function refreshImage() {
  const boundaryParams = Object.entries(boundaryToggles)
    .map(([level, on]) => `${level}=${on ? 1 : 0}`)
    .join("&");
  el("map-img").src = `/api/public/${TOKEN}/render.png?style=${currentStyle}&${boundaryParams}&t=${Date.now()}`;
  el("legend-img").src = `/api/public/${TOKEN}/legend.png?style=${currentStyle}&t=${Date.now()}`;
}

async function loadWorld() {
  const res = await fetch(`/api/public/${TOKEN}/world`);
  if (!res.ok) {
    el("view-main").hidden = true;
    const e = el("view-error");
    e.textContent = "This share link is invalid or has been revoked. Ask your GM for a new one.";
    e.hidden = false;
    return;
  }
  world = await res.json();
  renderAll();
  refreshImage();
}

function renderAll() {
  el("map-title-header").textContent = world.title || "World Map";
  el("nation-count").textContent = world.nations.length;
  el("settlement-count").textContent = world.settlements.length;
  el("poi-count").textContent = world.points_of_interest.length;
  renderMarkers();
  renderNationList();
  renderSettlementTable(el("settlement-search").value);
  renderPOIList();
  renderNationsInView();
}

function renderNationsInView() {
  const present = world.nations.filter((n) => n.x_pct !== null).map((n) => n.name);
  const target = el("nations-in-view");
  if (present.length === 0) {
    target.textContent = "No nation controls this area.";
  } else if (present.length === 1) {
    target.textContent = `This area belongs to: ${present[0]}`;
  } else {
    target.textContent = `Nations visible here: ${present.join(", ")}`;
  }
}

function renderMarkers() {
  const layer = el("marker-layer");
  layer.innerHTML = "";

  for (const n of world.nations) {
    if (n.x_pct === null) continue;
    const div = document.createElement("div");
    div.className = "marker marker-nation";
    div.style.left = `${n.x_pct}%`;
    div.style.top = `${n.y_pct}%`;
    div.textContent = n.name.toUpperCase();
    div.addEventListener("click", () => showNationDetail(n.id));
    layer.appendChild(div);
  }

  for (const s of world.settlements) {
    const div = document.createElement("div");
    div.className = `marker marker-settlement tier-${s.tier}`;
    div.style.left = `${s.x_pct}%`;
    div.style.top = `${s.y_pct}%`;
    div.title = `${s.name} (${s.tier})`;
    div.addEventListener("click", (e) => {
      e.stopPropagation();
      showSettlementDetail(s.id);
    });
    layer.appendChild(div);
  }

  for (const p of world.points_of_interest) {
    const div = document.createElement("div");
    div.className = "marker marker-poi";
    div.style.left = `${p.x_pct}%`;
    div.style.top = `${p.y_pct}%`;
    div.title = `${p.name} (${p.kind})`;
    div.addEventListener("click", (e) => {
      e.stopPropagation();
      showPOIDetail(p.id);
    });
    layer.appendChild(div);
  }
}

function renderNationList() {
  const list = el("nation-list");
  list.innerHTML = "";
  for (const n of world.nations) {
    const li = document.createElement("li");
    li.textContent = `${n.name} — capital: ${n.capital_name ?? "none"} — ${n.settlement_count} settlements`;
    li.addEventListener("click", () => showNationDetail(n.id));
    list.appendChild(li);
  }
}

function renderSettlementTable(filter) {
  const q = (filter || "").trim().toLowerCase();
  const tbody = el("settlement-rows");
  tbody.innerHTML = "";
  const nationName = (nid) => {
    const n = world.nations.find((n) => n.id === nid);
    return n ? n.name : "—";
  };
  for (const s of world.settlements) {
    if (q && !s.name.toLowerCase().includes(q)) continue;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${s.name}</td><td>${s.tier}</td><td>${nationName(s.nation_id)}</td>` +
      `<td>${s.population.toLocaleString()}</td>`;
    tr.addEventListener("click", () => showSettlementDetail(s.id));
    tbody.appendChild(tr);
  }
}

function renderPOIList() {
  const list = el("poi-list");
  list.innerHTML = "";
  for (const p of world.points_of_interest) {
    const li = document.createElement("li");
    li.textContent = `${p.name} (${p.kind})`;
    li.addEventListener("click", () => showPOIDetail(p.id));
    list.appendChild(li);
  }
}

function showDetail(html) {
  showTab("selection");
  el("detail-empty").hidden = true;
  el("detail-body").hidden = false;
  el("detail-body").innerHTML = html;
}

function showSettlementDetail(id) {
  const s = world.settlements.find((s) => s.id === id);
  if (!s) return;
  const nation = world.nations.find((n) => n.id === s.nation_id);
  showDetail(
    `<h3>${s.name}</h3>` +
    `<p class="muted">${s.tier[0].toUpperCase()}${s.tier.slice(1)} &mdash; ${nation ? nation.name : "unclaimed"}</p>` +
    `<p class="muted">Population: ${s.population.toLocaleString()}` +
    `${s.resource ? ` &mdash; Resource: ${s.resource}` : ""}</p>`
  );
}

function showNationDetail(id) {
  const n = world.nations.find((n) => n.id === id);
  if (!n) return;
  showDetail(
    `<h3>${n.name}</h3>` +
    `<p class="muted">Capital: ${n.capital_name ?? "none"} &mdash; ${n.settlement_count} settlements</p>` +
    `<p class="muted">Population: ${n.total_population.toLocaleString()}</p>`
  );
}

function showPOIDetail(id) {
  const p = world.points_of_interest.find((p) => p.id === id);
  if (!p) return;
  showDetail(`<h3>${p.name}</h3><p class="muted">${p.kind}</p>`);
}

el("settlement-search").addEventListener("input", (e) => renderSettlementTable(e.target.value));
el("style-select").addEventListener("change", (e) => {
  currentStyle = e.target.value;
  refreshImage();
});
for (const level of Object.keys(boundaryToggles)) {
  el(`toggle-${level}`).addEventListener("change", (e) => {
    boundaryToggles[level] = e.target.checked;
    refreshImage();
  });
}

loadWorld();
