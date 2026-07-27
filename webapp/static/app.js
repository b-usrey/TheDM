let world = null;
let selection = null; // { type: "settlement" | "nation" | "poi", id }
let currentStyle = "classic";
let boundaryToggles = { duchy: false, county: false, barony: false };
let gridSettings = { kind: "none", sizeMiles: 10 };
let dragStart = null;          // {x, y} in map-wrap-relative pixels, while dragging
let pendingRegion = null;      // {x0, y0, x1, y1} in world grid coords, once a selection is made
let placingPOI = false;        // armed via the "Place on Map" toggle in the POI tab
let placingSettlement = false; // armed via the "Place on Map" toggle in the Settlements tab
let measuringTravel = false;    // armed via "Pick Points on Map" in the GM Tools tab
let travelPoints = [];          // up to 2 {x, y} world-grid points collected while measuring
let pickingEncounter = false;   // armed via "Pick Spot on Map" in the GM Tools tab
let dynasty = null;
let selectedHouseId = null;

const el = (id) => document.getElementById(id);

// ---- Auth ----

function showAuthError(formPrefix, msg) {
  const e = el(`${formPrefix}-error`);
  e.textContent = msg;
  e.hidden = false;
}

async function startApp() {
  // Called right after login/signup, or on page load to check for an
  // existing session -- always resolves account info (including is_admin)
  // via /api/auth/me itself, one code path for every entry point. If the
  // session isn't actually logged in, this just leaves the login form
  // showing (it's visible by default) instead of showing the app.
  const meRes = await fetch("/api/auth/me");
  if (!meRes.ok) return;
  const me = await meRes.json();
  el("auth-overlay").hidden = true;
  el("app").hidden = false;
  el("account-username").textContent = me.username;
  el("admin-link").hidden = !me.is_admin;
  await fetchWorld();
  refreshImage();
}

async function handleLogin(e) {
  e.preventDefault();
  el("login-error").hidden = true;
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: el("login-username").value,
      password: el("login-password").value,
    }),
  });
  if (!res.ok) {
    showAuthError("login", (await res.json()).error || "login failed");
    return;
  }
  await startApp();
}

async function handleSignup(e) {
  e.preventDefault();
  el("signup-error").hidden = true;
  const res = await fetch("/api/auth/signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: el("signup-username").value,
      password: el("signup-password").value,
      invite_code: el("signup-invite").value,
    }),
  });
  if (!res.ok) {
    showAuthError("signup", (await res.json()).error || "signup failed");
    return;
  }
  await startApp();
}

async function handleLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.reload();
}

el("login-form").addEventListener("submit", handleLogin);
el("signup-form").addEventListener("submit", handleSignup);
el("btn-logout").addEventListener("click", handleLogout);
el("show-signup").addEventListener("click", (e) => {
  e.preventDefault();
  el("login-form").hidden = true;
  el("signup-form").hidden = false;
});
el("show-login").addEventListener("click", (e) => {
  e.preventDefault();
  el("signup-form").hidden = true;
  el("login-form").hidden = false;
});

// ---- Tabs ----

function showTab(tab) {
  document.querySelectorAll(".tab-page").forEach((page) => {
    page.hidden = page.dataset.tabPage !== tab;
  });
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  // The Dynasty tab replaces the map image with the family-tree canvas --
  // a tree is inherently wide/tall, so it gets the same main-pane real
  // estate the map itself uses instead of being squeezed into the sidebar.
  const isDynasty = tab === "dynasty";
  el("map-pane").hidden = isDynasty;
  el("dynasty-pane").hidden = !isDynasty;
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

// ---- World fetch / image refresh ----

async function fetchWorld() {
  const res = await fetch("/api/world");
  world = await res.json();
  renderAll();
  refreshMyFiles();
  refreshShareStatus();
  fetchDynasty();
}

async function refreshMyFiles() {
  const res = await fetch("/api/worlds/list");
  if (!res.ok) return;
  const data = await res.json();
  const select = el("my-files-select");
  select.innerHTML = "";
  for (const f of data.files) {
    const option = document.createElement("option");
    option.value = f.filename;
    option.textContent = f.current ? `${f.filename} (current)` : f.filename;
    if (f.current) option.selected = true;
    select.appendChild(option);
  }
}

async function handleSwitchWorld() {
  const filename = el("my-files-select").value;
  if (!filename) return;
  showFilesStatus(`Switching to ${filename}...`);
  const res = await fetch("/api/worlds/switch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!res.ok) {
    showFilesError((await res.json()).error || "failed to switch world");
    return;
  }
  clearSelection();
  await fetchWorld();
  refreshImage();
  showFilesStatus(`Switched to ${filename}.`);
}

async function handleDeleteWorld() {
  const filename = el("my-files-select").value;
  if (!filename) return;
  if (!confirm(`Delete ${filename}? This cannot be undone.`)) return;
  showFilesStatus(`Deleting ${filename}...`);
  const res = await fetch("/api/worlds/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!res.ok) {
    showFilesError((await res.json()).error || "failed to delete world");
    return;
  }
  await refreshMyFiles();
  showFilesStatus(`Deleted ${filename}.`);
}

function refreshImage() {
  const boundaryParams = Object.entries(boundaryToggles)
    .map(([level, on]) => `${level}=${on ? 1 : 0}`)
    .join("&");
  const gridParams = `grid=${gridSettings.kind}&grid_size=${gridSettings.sizeMiles}`;
  el("map-img").src = `/api/render.png?style=${currentStyle}&${boundaryParams}&${gridParams}&t=${Date.now()}`;
  el("legend-img").src = `/api/legend.png?style=${currentStyle}&t=${Date.now()}`;
}

// ---- Map drag: region-select or POI placement ----

function clampToImage(px, py) {
  const rect = el("map-img").getBoundingClientRect();
  const wrapRect = el("map-wrap").getBoundingClientRect();
  const imgX0 = rect.left - wrapRect.left;
  const imgY0 = rect.top - wrapRect.top;
  return {
    x: Math.min(Math.max(px, imgX0), imgX0 + rect.width),
    y: Math.min(Math.max(py, imgY0), imgY0 + rect.height),
    imgX0, imgY0, imgW: rect.width, imgH: rect.height,
  };
}

function handleMapMouseDown(e) {
  if (e.target.id !== "map-img" || e.button !== 0) return;
  const wrapRect = el("map-wrap").getBoundingClientRect();
  dragStart = { x: e.clientX - wrapRect.left, y: e.clientY - wrapRect.top };
  const box = el("selection-box");
  box.style.left = `${dragStart.x}px`;
  box.style.top = `${dragStart.y}px`;
  box.style.width = "0px";
  box.style.height = "0px";
  box.style.display = "block";
  e.preventDefault();
}

function handleMapMouseMove(e) {
  if (!dragStart) return;
  const wrapRect = el("map-wrap").getBoundingClientRect();
  const curX = e.clientX - wrapRect.left;
  const curY = e.clientY - wrapRect.top;
  const box = el("selection-box");
  box.style.left = `${Math.min(dragStart.x, curX)}px`;
  box.style.top = `${Math.min(dragStart.y, curY)}px`;
  box.style.width = `${Math.abs(curX - dragStart.x)}px`;
  box.style.height = `${Math.abs(curY - dragStart.y)}px`;
}

function handleMapMouseUp(e) {
  if (!dragStart) return;
  const wrapRect = el("map-wrap").getBoundingClientRect();
  const startPt = clampToImage(dragStart.x, dragStart.y);
  const endPt = clampToImage(e.clientX - wrapRect.left, e.clientY - wrapRect.top);
  dragStart = null;

  const fx0 = (Math.min(startPt.x, endPt.x) - startPt.imgX0) / startPt.imgW;
  const fy0 = (Math.min(startPt.y, endPt.y) - startPt.imgY0) / startPt.imgH;
  const fx1 = (Math.max(startPt.x, endPt.x) - startPt.imgX0) / startPt.imgW;
  const fy1 = (Math.max(startPt.y, endPt.y) - startPt.imgY0) / startPt.imgH;
  const isClick = fx1 - fx0 < 0.01 || fy1 - fy0 < 0.01;

  if (isClick && (placingPOI || placingSettlement || measuringTravel || pickingEncounter)) {
    const gx = Math.round(((startPt.x - startPt.imgX0) / startPt.imgW) * world.width);
    const gy = Math.round(((startPt.y - startPt.imgY0) / startPt.imgH) * world.height);
    el("selection-box").style.display = "none";
    if (placingPOI) {
      handleCreatePOI(gx, gy);
    } else if (placingSettlement) {
      handleCreateSettlement(gx, gy);
    } else if (measuringTravel) {
      handleTravelPoint(gx, gy);
    } else if (pickingEncounter) {
      disarmPickEncounter();
      rollEncounterAt(gx, gy);
    }
    return;
  }

  if (isClick) {
    handleZoomClear();
    return;
  }

  const gx0 = Math.max(0, Math.round(fx0 * world.width));
  const gy0 = Math.max(0, Math.round(fy0 * world.height));
  const gx1 = Math.min(world.width, Math.round(fx1 * world.width));
  const gy1 = Math.min(world.height, Math.round(fy1 * world.height));
  pendingRegion = { x0: gx0, y0: gy0, x1: gx1, y1: gy1 };

  el("zoom-hint").hidden = true;
  el("zoom-body").hidden = false;
  el("zoom-bounds").textContent =
    `Selected (${gx0}, ${gy0}) to (${gx1}, ${gy1}) — ${gx1 - gx0} x ${gy1 - gy0} cells ` +
    `of ${world.width} x ${world.height}.`;
}

function handleZoomClear() {
  pendingRegion = null;
  el("selection-box").style.display = "none";
  el("zoom-hint").hidden = false;
  el("zoom-body").hidden = true;
}

function showZoomStatus(msg) {
  el("zoom-error").hidden = true;
  el("zoom-status").textContent = msg;
}

function showZoomError(msg) {
  const e = el("zoom-error");
  e.textContent = msg;
  e.hidden = false;
  el("zoom-status").textContent = "";
}

async function handleZoomIn() {
  if (!pendingRegion) return;
  const resolution = el("zoom-resolution").value;
  const filename = el("zoom-filename").value || "region.npz";
  const btn = el("btn-zoom-in");
  btn.disabled = true;
  showZoomStatus("Generating region... this can take a minute or more.");
  try {
    const res = await fetch("/api/region", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...pendingRegion, resolution, filename }),
    });
    if (!res.ok) {
      showZoomError((await res.json()).error || "failed to generate region");
      return;
    }
    clearSelection();
    handleZoomClear();
    await fetchWorld();
    refreshImage();
    showZoomStatus(`Zoomed in, saved as ${filename}.`);
  } finally {
    btn.disabled = false;
  }
}

// ---- Export ----

function handleDownloadMap() {
  const legend = el("export-legend").checked ? 1 : 0;
  const boundaryParams = Object.entries(boundaryToggles)
    .map(([level, on]) => `${level}=${on ? 1 : 0}`)
    .join("&");
  const gridParams = `grid=${gridSettings.kind}&grid_size=${gridSettings.sizeMiles}`;
  const url = `/api/export.png?style=${currentStyle}&legend=${legend}&${boundaryParams}&${gridParams}`;

  el("export-status").textContent = "Preparing download...";
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  el("export-status").textContent = "Download started.";
}

// ---- Rendering lists / markers ----

function renderAll() {
  el("world-info").textContent =
    `${world.path} — seed ${world.seed} — ${world.width}x${world.height}`;
  el("nation-count").textContent = world.nations.length;
  el("settlement-count").textContent = world.settlements.length;
  el("poi-count").textContent = world.points_of_interest.length;
  el("map-title").value = world.title || "";
  el("current-filename").textContent = (world.path || "").split(/[\\/]/).pop();
  el("btn-undo").disabled = !world.can_undo;

  populateSelectOnce("poi-kind-select", world.poi_kinds);
  populateSelectOnce("edit-poi-kind", world.poi_kinds);

  renderMarkers();
  renderNationList();
  renderSettlementTable(el("settlement-search").value);
  renderPOIList();
  renderNationsInView();
}

function renderNationsInView() {
  // n.x_pct is null when a nation has no presence at all in the currently
  // loaded world (see server.py's _nation_centroid_pct) -- for a zoomed
  // region that's most nations, since only whichever one(s) actually
  // overlap the cropped area will have a non-null centroid.
  const present = world.nations.filter((n) => n.x_pct !== null).map((n) => n.name);
  const el_ = el("nations-in-view");
  if (present.length === 0) {
    el_.textContent = "No nation controls this area.";
  } else if (present.length === 1) {
    el_.textContent = `This area belongs to: ${present[0]}`;
  } else {
    el_.textContent = `Nations visible here: ${present.join(", ")}`;
  }
}

function populateSelectOnce(id, options) {
  const select = el(id);
  if (select.dataset.populated) return;
  select.innerHTML = "";
  for (const opt of options) {
    const option = document.createElement("option");
    option.value = opt;
    option.textContent = opt[0].toUpperCase() + opt.slice(1);
    select.appendChild(option);
  }
  select.dataset.populated = "1";
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
    div.title = `${n.name} (nation)`;
    div.addEventListener("click", () => selectNation(n.id));
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
      selectSettlement(s.id);
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
      selectPOI(p.id);
    });
    layer.appendChild(div);
  }
}

function renderNationList() {
  const list = el("nation-list");
  list.innerHTML = "";
  for (const n of world.nations) {
    const li = document.createElement("li");
    li.textContent = `${n.name} — capital: ${n.capital_name ?? "none"} — ` +
      `${n.settlement_count} settlements, pop. ${n.total_population.toLocaleString()}, ` +
      `${Math.round(n.total_gold).toLocaleString()} gold/yr`;
    li.addEventListener("click", () => selectNation(n.id));
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
      `<td>${s.population.toLocaleString()}</td><td>${s.resource || "—"}</td>`;
    tr.addEventListener("click", () => selectSettlement(s.id));
    tbody.appendChild(tr);
  }
}

function renderPOIList() {
  const list = el("poi-list");
  list.innerHTML = "";
  for (const p of world.points_of_interest) {
    const li = document.createElement("li");
    li.textContent = `${p.name} (${p.kind})`;
    li.addEventListener("click", () => selectPOI(p.id));
    list.appendChild(li);
  }
}

// ---- Selection / inspector ----

function selectSettlement(id) {
  const s = world.settlements.find((s) => s.id === id);
  if (!s) return;
  selection = { type: "settlement", id };
  showTab("selection");
  el("inspector-empty").hidden = true;
  el("inspector-body").hidden = false;
  el("inspector-error").hidden = true;
  el("edit-name").value = s.name;
  el("edit-notes").value = s.notes || "";
  el("edit-tier-field").hidden = false;
  el("edit-tier").value = s.tier;
  el("edit-settlement-stats-field").hidden = false;
  el("edit-population").value = s.population;
  el("edit-area-km2").value = s.area_km2;
  el("edit-economic-output").value = s.economic_output;
  el("edit-titles-field").hidden = true;
  el("edit-poi-kind-field").hidden = true;
  el("btn-delete").hidden = false;

  const fiefGold = (g) => Math.round(g).toLocaleString();
  const nation = world.nations.find((n) => n.id === s.nation_id);
  const fiefBits = [`Nation: ${nation ? nation.name : "unclaimed"}`];
  if (s.fief_rank) fiefBits.push(`Rank: ${s.fief_rank[0].toUpperCase()}${s.fief_rank.slice(1)}`);
  if (s.duchy_name) fiefBits.push(`Duchy: ${s.duchy_name} (${fiefGold(s.duchy_gold)} gold/yr)`);
  if (s.county_name) fiefBits.push(`County: ${s.county_name} (${fiefGold(s.county_gold)} gold/yr)`);
  if (s.barony_name) fiefBits.push(`Barony: ${s.barony_name} (${fiefGold(s.barony_gold)} gold/yr)`);

  el("inspector-stats").textContent =
    (s.resource ? `Resource: ${s.resource} — ` : "") +
    `Farmland ceiling: ${s.farmland_ceiling.toLocaleString()}` +
    (fiefBits.length ? ` — ${fiefBits.join(" — ")}` : "");
}

function selectNation(id) {
  const n = world.nations.find((n) => n.id === id);
  if (!n) return;
  selection = { type: "nation", id };
  showTab("selection");
  el("inspector-empty").hidden = true;
  el("inspector-body").hidden = false;
  el("inspector-error").hidden = true;
  el("edit-name").value = n.name;
  el("edit-notes").value = n.notes || "";
  el("edit-tier-field").hidden = true;
  el("edit-settlement-stats-field").hidden = true;
  el("edit-titles-field").hidden = false;
  el("edit-poi-kind-field").hidden = true;
  el("edit-title-duke").value = n.duke_title;
  el("edit-title-count").value = n.count_title;
  el("edit-title-baron").value = n.baron_title;
  el("btn-delete").hidden = true;
  el("inspector-stats").textContent =
    `Capital: ${n.capital_name ?? "none"} — Settlements: ${n.settlement_count} — ` +
    `Population: ${n.total_population.toLocaleString()} — ` +
    `Gold: ${Math.round(n.total_gold).toLocaleString()}/yr`;
}

function selectPOI(id) {
  const p = world.points_of_interest.find((p) => p.id === id);
  if (!p) return;
  selection = { type: "poi", id };
  showTab("selection");
  el("inspector-empty").hidden = true;
  el("inspector-body").hidden = false;
  el("inspector-error").hidden = true;
  el("edit-name").value = p.name;
  el("edit-notes").value = p.notes || "";
  el("edit-tier-field").hidden = true;
  el("edit-settlement-stats-field").hidden = true;
  el("edit-titles-field").hidden = true;
  el("edit-poi-kind-field").hidden = false;
  el("edit-poi-kind").value = p.kind;
  el("btn-delete").hidden = false;
  el("inspector-stats").textContent = `Location: (${Math.round(p.x)}, ${Math.round(p.y)})`;
}

function clearSelection() {
  selection = null;
  el("inspector-empty").hidden = false;
  el("inspector-body").hidden = true;
}

function showError(msg) {
  const e = el("inspector-error");
  e.textContent = msg;
  e.hidden = false;
}

async function handleSave() {
  if (!selection) return;

  if (selection.type === "poi") {
    const res = await fetch(`/api/pois/${selection.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: el("edit-name").value,
        kind: el("edit-poi-kind").value,
        notes: el("edit-notes").value,
      }),
    });
    if (!res.ok) {
      showError((await res.json()).error || "failed to save point of interest");
      return;
    }
    clearSelection();
    await fetchWorld();
    refreshImage();
    return;
  }

  const name = el("edit-name").value;
  const base = selection.type === "settlement"
    ? `/api/settlements/${selection.id}`
    : `/api/nations/${selection.id}`;

  const nameRes = await fetch(`${base}/name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!nameRes.ok) {
    showError((await nameRes.json()).error || "failed to rename");
    return;
  }

  const notesRes = await fetch(`${base}/notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: el("edit-notes").value }),
  });
  if (!notesRes.ok) {
    showError((await notesRes.json()).error || "failed to save notes");
    return;
  }

  if (selection.type === "settlement") {
    const tier = el("edit-tier").value;
    const tierRes = await fetch(`${base}/tier`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tier }),
    });
    if (!tierRes.ok) {
      showError((await tierRes.json()).error || "failed to change tier");
      return;
    }

    const statsRes = await fetch(`${base}/stats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        population: el("edit-population").value,
        area_km2: el("edit-area-km2").value,
        economic_output: el("edit-economic-output").value,
      }),
    });
    if (!statsRes.ok) {
      showError((await statsRes.json()).error || "failed to save population/area/economic output");
      return;
    }
  } else {
    const titlesRes = await fetch(`${base}/titles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        duke: el("edit-title-duke").value,
        count: el("edit-title-count").value,
        baron: el("edit-title-baron").value,
      }),
    });
    if (!titlesRes.ok) {
      showError((await titlesRes.json()).error || "failed to save titles");
      return;
    }
  }

  clearSelection();
  await fetchWorld();
  refreshImage();
}

async function handleDelete() {
  if (!selection) return;

  if (selection.type === "poi") {
    if (!confirm("Delete this point of interest? This cannot be undone.")) return;
    const res = await fetch(`/api/pois/${selection.id}`, { method: "DELETE" });
    if (!res.ok) {
      showError((await res.json()).error || "failed to delete");
      return;
    }
    clearSelection();
    await fetchWorld();
    refreshImage();
    return;
  }

  if (selection.type !== "settlement") return;
  if (!confirm("Delete this settlement? This cannot be undone.")) return;
  const res = await fetch(`/api/settlements/${selection.id}`, { method: "DELETE" });
  if (!res.ok) {
    showError((await res.json()).error || "failed to delete");
    return;
  }
  clearSelection();
  await fetchWorld();
  refreshImage();
}

// ---- Points of interest: placement ----

async function handleCreatePOI(x, y) {
  const kind = el("poi-kind-select").value;
  placingPOI = false;
  el("btn-place-poi").classList.remove("armed");
  el("btn-place-poi").textContent = "Place on Map";

  const res = await fetch("/api/pois", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ x, y, kind, name: `New ${kind}` }),
  });
  if (!res.ok) {
    const e = el("poi-error");
    e.textContent = (await res.json()).error || "failed to create point of interest";
    e.hidden = false;
    return;
  }
  el("poi-error").hidden = true;
  const poi = await res.json();
  await fetchWorld();
  refreshImage();
  selectPOI(poi.id);
}

function handleTogglePlacePOI() {
  placingPOI = !placingPOI;
  if (placingPOI) {
    disarmPlaceSettlement();
    disarmTravelMeasure();
    disarmPickEncounter();
  }
  const btn = el("btn-place-poi");
  btn.classList.toggle("armed", placingPOI);
  btn.textContent = placingPOI ? "Click the map to place... (click here to cancel)" : "Place on Map";
}

// ---- Settlements: manual placement ----

async function handleCreateSettlement(x, y) {
  const tier = el("new-settlement-tier").value;
  disarmPlaceSettlement();

  const res = await fetch("/api/settlements", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ x, y, tier, name: `New ${tier}` }),
  });
  if (!res.ok) {
    const e = el("settlement-error");
    e.textContent = (await res.json()).error || "failed to create settlement";
    e.hidden = false;
    return;
  }
  el("settlement-error").hidden = true;
  const settlement = await res.json();
  await fetchWorld();
  refreshImage();
  selectSettlement(settlement.id);
}

function disarmPlaceSettlement() {
  placingSettlement = false;
  el("btn-place-settlement").classList.remove("armed");
  el("btn-place-settlement").textContent = "Place on Map";
}

function handleTogglePlaceSettlement() {
  placingSettlement = !placingSettlement;
  if (placingSettlement) {
    placingPOI = false;
    el("btn-place-poi").classList.remove("armed");
    el("btn-place-poi").textContent = "Place on Map";
    disarmTravelMeasure();
    disarmPickEncounter();
  }
  const btn = el("btn-place-settlement");
  btn.classList.toggle("armed", placingSettlement);
  btn.textContent = placingSettlement ? "Click the map to place... (click here to cancel)" : "Place on Map";
}

// ---- Title ----

async function handleTitleSave() {
  const title = el("map-title").value;
  const res = await fetch("/api/title", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) {
    showError((await res.json()).error || "failed to save title");
    return;
  }
  const data = await res.json();
  el("map-title").value = data.title;
  refreshImage();
}

// ---- World file management (upload/download/save-as/generate/undo/restore) ----

function showFilesError(msg) {
  const e = el("files-error");
  e.textContent = msg;
  e.hidden = false;
  el("files-status").textContent = "";
}

function showFilesStatus(msg) {
  el("files-error").hidden = true;
  el("files-status").textContent = msg;
}

async function handleUpload() {
  const input = el("upload-file-input");
  const file = input.files[0];
  if (!file) {
    showFilesError("choose a .npz file first");
    return;
  }
  showFilesStatus(`Uploading ${file.name}...`);
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch("/api/worlds/upload", { method: "POST", body: formData });
  if (!res.ok) {
    showFilesError((await res.json()).error || "failed to upload world");
    return;
  }
  clearSelection();
  await fetchWorld();
  refreshImage();
  input.value = "";
  showFilesStatus(`Uploaded and loaded ${file.name}.`);
}

function handleDownloadWorld() {
  showFilesStatus("Preparing download...");
  const a = document.createElement("a");
  a.href = "/api/worlds/download";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  showFilesStatus("Download started.");
}

async function handleSaveAs() {
  const filename = el("save-as-filename").value;
  if (!filename.trim()) {
    showFilesError("enter a filename first");
    return;
  }
  showFilesStatus("Saving...");
  const res = await fetch("/api/worlds/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  });
  if (!res.ok) {
    showFilesError((await res.json()).error || "failed to save");
    return;
  }
  const data = await res.json();
  await fetchWorld();
  showFilesStatus(`Saved as ${data.filename}.`);
}

async function handleGenerate() {
  const seed = el("gen-seed").value;
  const size = el("gen-size").value;
  const continentBias = el("gen-continent-bias").value;
  const filename = el("gen-filename").value || "world.npz";

  const btn = el("btn-generate");
  btn.disabled = true;
  showFilesStatus("Generating new world... this can take up to 20-30 seconds.");
  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        seed, size,
        continent_bias: continentBias === "" ? null : continentBias,
        filename,
      }),
    });
    if (!res.ok) {
      showFilesError((await res.json()).error || "failed to generate world");
      return;
    }
    clearSelection();
    await fetchWorld();
    refreshImage();
    showFilesStatus(`Generated and loaded ${filename}.`);
  } finally {
    btn.disabled = false;
  }
}

async function handleUndo() {
  showFilesStatus("Undoing...");
  const res = await fetch("/api/undo", { method: "POST" });
  if (!res.ok) {
    showFilesError((await res.json()).error || "nothing to undo");
    return;
  }
  clearSelection();
  await fetchWorld();
  refreshImage();
  showFilesStatus("Last edit undone.");
}

async function handleRestoreBackup() {
  if (!confirm("Restore the world to its state before the last save? This reverts the whole file.")) return;
  showFilesStatus("Restoring backup...");
  const res = await fetch("/api/restore-backup", { method: "POST" });
  if (!res.ok) {
    showFilesError((await res.json()).error || "no backup available");
    return;
  }
  clearSelection();
  await fetchWorld();
  refreshImage();
  showFilesStatus("Restored from backup.");
}

// ---- GM Tools: travel calculator ----

function disarmTravelMeasure() {
  measuringTravel = false;
  travelPoints = [];
  el("btn-measure-travel").classList.remove("armed");
  el("btn-measure-travel").textContent = "Pick Points on Map";
}

function handleToggleMeasureTravel() {
  measuringTravel = !measuringTravel;
  travelPoints = [];
  if (measuringTravel) {
    disarmPlaceSettlement();
    placingPOI = false;
    el("btn-place-poi").classList.remove("armed");
    el("btn-place-poi").textContent = "Place on Map";
    disarmPickEncounter();
  }
  const btn = el("btn-measure-travel");
  btn.classList.toggle("armed", measuringTravel);
  btn.textContent = measuringTravel ? "Click point A on the map... (click here to cancel)" : "Pick Points on Map";
}

async function handleTravelPoint(x, y) {
  travelPoints.push({ x, y });
  if (travelPoints.length === 1) {
    el("btn-measure-travel").textContent = "Click point B on the map...";
    return;
  }
  const [a, b] = travelPoints;
  disarmTravelMeasure();

  const res = await fetch("/api/tools/travel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ x0: a.x, y0: a.y, x1: b.x, y1: b.y }),
  });
  if (!res.ok) {
    const e = el("travel-error");
    e.textContent = (await res.json()).error || "failed to calculate travel time";
    e.hidden = false;
    return;
  }
  el("travel-error").hidden = true;
  renderTravelResult(await res.json());
}

function renderTravelResult(data) {
  const lines = [`Distance: ${data.distance_miles} mi (${data.distance_km} km)`];
  if (data.mode === "sea") {
    lines.push(`By sea (~2 knots): ${data.days.sea} days`);
  } else {
    lines.push(`Fast pace: ${data.days.fast} days`);
    lines.push(`Normal pace: ${data.days.normal} days`);
    lines.push(`Slow pace: ${data.days.slow} days`);
    lines.push(`Terrain multiplier: &times;${data.terrain_multiplier}`);
  }
  if (data.note) lines.push(data.note);
  el("travel-result").innerHTML = lines.map((l) => `<div>${l}</div>`).join("");
}

function handleClearTravel() {
  disarmTravelMeasure();
  el("travel-result").innerHTML = "";
  el("travel-error").hidden = true;
}

// ---- GM Tools: random NPC ----

async function handleRollNPC() {
  const res = await fetch("/api/tools/npc");
  const npc = await res.json();
  el("npc-result").innerHTML =
    `<div><strong>${npc.name}</strong> &mdash; ${npc.race} ${npc.occupation}</div>` +
    `<div>Trait: ${npc.trait}</div>` +
    `<div>Motivation: ${npc.motivation}</div>`;
}

// ---- GM Tools: random encounter ----

function disarmPickEncounter() {
  pickingEncounter = false;
  el("btn-pick-encounter").classList.remove("armed");
  el("btn-pick-encounter").textContent = "Pick Spot on Map";
}

function handleTogglePickEncounter() {
  pickingEncounter = !pickingEncounter;
  if (pickingEncounter) {
    disarmPlaceSettlement();
    placingPOI = false;
    el("btn-place-poi").classList.remove("armed");
    el("btn-place-poi").textContent = "Place on Map";
    disarmTravelMeasure();
  }
  const btn = el("btn-pick-encounter");
  btn.classList.toggle("armed", pickingEncounter);
  btn.textContent = pickingEncounter ? "Click a spot on the map... (click here to cancel)" : "Pick Spot on Map";
}

async function rollEncounterAt(x, y) {
  const res = await fetch(`/api/tools/encounter?x=${x}&y=${y}`);
  renderEncounterResult(await res.json());
}

async function handleRollEncounterRandom() {
  const res = await fetch("/api/tools/encounter");
  renderEncounterResult(await res.json());
}

function renderEncounterResult(enc) {
  el("encounter-result").innerHTML =
    `<div><strong>${enc.biome}</strong> &mdash; difficulty: ${enc.difficulty}</div>` +
    `<div>${enc.description}</div>`;
}

// ---- GM Tools: share link ----

async function refreshShareStatus() {
  const res = await fetch("/api/share");
  if (!res.ok) return;
  renderShareStatus(await res.json());
}

function renderShareStatus(data) {
  el("share-no-link").hidden = !!data.token;
  el("share-has-link").hidden = !data.token;
  if (data.token) {
    el("share-link-input").value = `${window.location.origin}${data.url}`;
  }
}

async function handleCreateShare() {
  const res = await fetch("/api/share", { method: "POST" });
  if (!res.ok) {
    el("share-status").textContent = (await res.json()).error || "failed to create link";
    return;
  }
  renderShareStatus(await res.json());
  el("share-status").textContent = "Link created.";
}

async function handleCopyShare() {
  await navigator.clipboard.writeText(el("share-link-input").value);
  el("share-status").textContent = "Copied to clipboard.";
}

async function handleRevokeShare() {
  if (!confirm("Revoke this share link? Anyone using it will lose access.")) return;
  await fetch("/api/share", { method: "DELETE" });
  renderShareStatus({ token: null });
  el("share-status").textContent = "Link revoked.";
}

// ---- Dynasty: persistent noble-house genealogies ----

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function personYearsLabel(person) {
  const birth = person.birth_year != null ? person.birth_year : "?";
  if (person.death_year != null) return `${birth}–${person.death_year}`;
  return `b. ${birth}`;
}

function houseMembers(houseId) {
  return Object.values(dynasty.people).filter((p) => p.house_id === houseId);
}

// ---- Family tree: data -> layout -> SVG ----
//
// A "node" pairs one person with their spouse (if any) as a single visual
// unit -- a couple's two boxes plus the marriage line between them -- since
// that's how a genealogy chart reads, not as two independent people. Layout
// is a simple non-overlapping subtree-width algorithm (each node reserves
// enough horizontal "slots" for the wider of itself or its children, then
// centers itself over whatever span it was given): not the tightest packing
// possible, but house sizes here are dozens of people, not thousands, so
// it doesn't need to be.

function buildDynastyTreeData(houseId) {
  const members = houseMembers(houseId);
  if (members.length === 0) return [];

  const rootCandidates = members.filter((p) => {
    const fatherKnown = p.father_id && dynasty.people[p.father_id];
    const motherKnown = p.mother_id && dynasty.people[p.mother_id];
    return !fatherKnown && !motherKnown;
  });

  // A married couple who are both otherwise-rootless members of this same
  // house would each qualify as their own root -- keep only one (the
  // earlier-born, ties broken by uid) as the actual root; the other is
  // drawn as that root's spouse box instead.
  const rootIds = new Set(rootCandidates.map((p) => p.uid));
  const skipAsRoot = new Set();
  for (const p of rootCandidates) {
    if (skipAsRoot.has(p.uid) || !p.spouse_id || !rootIds.has(p.spouse_id) || skipAsRoot.has(p.spouse_id)) continue;
    const spouse = dynasty.people[p.spouse_id];
    const pBirth = p.birth_year ?? Infinity;
    const spouseBirth = spouse.birth_year ?? Infinity;
    const loser = pBirth !== spouseBirth ? (pBirth < spouseBirth ? spouse : p) : (p.uid < spouse.uid ? spouse : p);
    skipAsRoot.add(loser.uid);
  }
  const roots = rootCandidates.filter((p) => !skipAsRoot.has(p.uid));

  // Deliberately NOT seeded with skipAsRoot: a skipped duplicate-root is
  // always the spouse_id of the root that won (marriages are recorded
  // symmetrically), so that root's own buildNode call below naturally
  // claims them as its spouse box and marks them visited. Seeding visited
  // up front would beat buildNode to it and leave the root spouseless.
  const visited = new Set();
  const buildNode = (person) => {
    visited.add(person.uid);
    let spouse = null;
    if (person.spouse_id) {
      const candidate = dynasty.people[person.spouse_id];
      // Only pair them visually if the spouse hasn't already been drawn
      // elsewhere (e.g. another node's own spouse) -- a rare cross-branch
      // marriage still shows the person, just without a duplicate box for
      // a spouse already on the canvas.
      if (candidate && !visited.has(candidate.uid)) {
        spouse = candidate;
        visited.add(candidate.uid);
      }
    }
    const parentIds = new Set([person.uid, ...(spouse ? [spouse.uid] : [])]);
    const children = Object.values(dynasty.people)
      .filter((p) => !visited.has(p.uid) && (parentIds.has(p.father_id) || parentIds.has(p.mother_id)))
      .sort((a, b) => (a.birth_year ?? 0) - (b.birth_year ?? 0))
      .map(buildNode);
    return { person, spouse, children };
  };

  // A plain .map() here would double-render anyone whose only tie to this
  // house is having married into a root's subtree (e.g. Cassia: no parents
  // of her own, so a root candidate by that rule, but also consumed as
  // Aldric's spouse while building his father's subtree) -- `visited` only
  // grows correctly if each root is checked *as we go*, not filtered up
  // front against a snapshot of `visited` before any subtree gets built.
  const rootNodes = [];
  for (const p of [...roots].sort((a, b) => (a.birth_year ?? 0) - (b.birth_year ?? 0))) {
    if (!visited.has(p.uid)) rootNodes.push(buildNode(p));
  }
  // Anyone left over (a reference cycle, or a parent that isn't itself a
  // member of this house) still gets their own tree, so nobody silently
  // disappears from the canvas.
  const leftoverNodes = members.filter((p) => !visited.has(p.uid)).map(buildNode);
  return [...rootNodes, ...leftoverNodes];
}

function computeSubtreeWidth(node) {
  const selfWidth = node.spouse ? 2 : 1;
  node.width = node.children.length === 0
    ? selfWidth
    : Math.max(selfWidth, node.children.reduce((sum, c) => sum + computeSubtreeWidth(c), 0));
  return node.width;
}

function assignTreePositions(node, left, depth) {
  node.left = left;
  node.depth = depth;
  node.centerSlot = left + node.width / 2;
  let cursor = left;
  for (const child of node.children) {
    assignTreePositions(child, cursor, depth + 1);
    cursor += child.width;
  }
}

function layoutForest(roots) {
  let cursor = 0;
  for (const root of roots) {
    computeSubtreeWidth(root);
    assignTreePositions(root, cursor, 0);
    cursor += root.width;
  }
  return cursor;
}

const TREE_SLOT_WIDTH = 140;
const TREE_ROW_HEIGHT = 110;
const TREE_BOX_WIDTH = 120;
const TREE_BOX_HEIGHT = 46;

function personBoxCenterX(node) {
  const spanCenterPx = node.centerSlot * TREE_SLOT_WIDTH;
  return node.spouse ? spanCenterPx - TREE_SLOT_WIDTH / 2 : spanCenterPx;
}

function spouseBoxCenterX(node) {
  return node.centerSlot * TREE_SLOT_WIDTH + TREE_SLOT_WIDTH / 2;
}

function personBoxSVG(person, cx, y) {
  const x = cx - TREE_BOX_WIDTH / 2;
  const deceased = person.death_year != null;
  const parts = [
    `<g>`,
    `<rect class="tree-node-box${deceased ? " deceased" : ""}" x="${x}" y="${y}" width="${TREE_BOX_WIDTH}" height="${TREE_BOX_HEIGHT}" rx="5"></rect>`,
    `<text class="tree-node-name" x="${cx}" y="${y + 17}" text-anchor="middle">${escapeHtml(person.name)}</text>`,
    `<text class="tree-node-years" x="${cx}" y="${y + 32}" text-anchor="middle">${escapeHtml(personYearsLabel(person))}</text>`,
  ];
  if (person.title) {
    parts.push(`<text class="tree-node-title" x="${cx}" y="${y + 43}" text-anchor="middle">${escapeHtml(person.title)}</text>`);
  }
  parts.push(`</g>`);
  return parts.join("");
}

function renderNodeSVG(node, parts) {
  const y = node.depth * TREE_ROW_HEIGHT;
  const personCx = personBoxCenterX(node);
  parts.push(personBoxSVG(node.person, personCx, y));

  if (node.spouse) {
    const spouseCx = spouseBoxCenterX(node);
    parts.push(personBoxSVG(node.spouse, spouseCx, y));
    parts.push(`<line class="tree-line-marriage" x1="${personCx + TREE_BOX_WIDTH / 2}" y1="${y + TREE_BOX_HEIGHT / 2}" ` +
      `x2="${spouseCx - TREE_BOX_WIDTH / 2}" y2="${y + TREE_BOX_HEIGHT / 2}"></line>`);
  }

  if (node.children.length > 0) {
    const connectorX = node.centerSlot * TREE_SLOT_WIDTH;
    const busY = y + TREE_BOX_HEIGHT + (TREE_ROW_HEIGHT - TREE_BOX_HEIGHT) / 2;
    parts.push(`<line class="tree-line-descent" x1="${connectorX}" y1="${y + TREE_BOX_HEIGHT}" x2="${connectorX}" y2="${busY}"></line>`);
    const childXs = node.children.map((c) => c.centerSlot * TREE_SLOT_WIDTH);
    if (node.children.length > 1) {
      parts.push(`<line class="tree-line-descent" x1="${Math.min(...childXs)}" y1="${busY}" x2="${Math.max(...childXs)}" y2="${busY}"></line>`);
    }
    for (const child of node.children) {
      const childX = child.centerSlot * TREE_SLOT_WIDTH;
      parts.push(`<line class="tree-line-descent" x1="${childX}" y1="${busY}" x2="${childX}" y2="${child.depth * TREE_ROW_HEIGHT}"></line>`);
      renderNodeSVG(child, parts);
    }
  }
}

function renderDynastyTree(houseId) {
  const header = el("dynasty-tree-title");
  const svg = el("dynasty-tree-svg");
  const house = houseId ? dynasty.houses[houseId] : null;

  if (!house) {
    header.textContent = "Select a house";
    svg.innerHTML = "";
    svg.setAttribute("width", "0");
    svg.setAttribute("height", "0");
    return;
  }

  header.textContent = house.name;
  const roots = buildDynastyTreeData(houseId);
  if (roots.length === 0) {
    svg.innerHTML = '<text x="10" y="24" fill="#a89b84" font-size="13">No people recorded in this house yet.</text>';
    svg.setAttribute("width", "400");
    svg.setAttribute("height", "40");
    return;
  }

  const totalSlots = layoutForest(roots);
  let maxDepth = 0;
  const walk = (node) => {
    maxDepth = Math.max(maxDepth, node.depth);
    node.children.forEach(walk);
  };
  roots.forEach(walk);

  const width = Math.max(totalSlots * TREE_SLOT_WIDTH, TREE_SLOT_WIDTH) + 20;
  const height = (maxDepth + 1) * TREE_ROW_HEIGHT + 20;

  const parts = [];
  for (const root of roots) renderNodeSVG(root, parts);

  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `-10 -10 ${width} ${height}`);
  svg.innerHTML = parts.join("");
}

function renderEventLog(houseId) {
  const memberIds = new Set(houseMembers(houseId).map((p) => p.uid));
  const list = el("house-event-log");
  list.innerHTML = "";
  const relevant = dynasty.events
    .filter((e) => e.person_ids.some((id) => memberIds.has(id)))
    .sort((a, b) => a.year - b.year);
  for (const e of relevant) {
    const li = document.createElement("li");
    li.textContent = `${e.year}: ${e.description} `;
    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "Delete this event";
    del.className = "event-delete-btn";
    del.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm("Delete this event from the log?")) return;
      await fetch(`/api/dynasty/events/${e.uid}`, { method: "DELETE" });
      await fetchDynasty();
    });
    li.appendChild(del);
    list.appendChild(li);
  }
}

function populateNationSelect(select) {
  select.innerHTML = '<option value="">No nation</option>';
  for (const n of world.nations) {
    const option = document.createElement("option");
    option.value = n.id;
    option.textContent = n.name;
    select.appendChild(option);
  }
}

function populatePersonSelect(select, blankLabel) {
  const previous = select.value;
  select.innerHTML = "";
  if (blankLabel) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = blankLabel;
    select.appendChild(opt);
  }
  const people = Object.values(dynasty.people).sort((a, b) => a.name.localeCompare(b.name));
  for (const p of people) {
    const option = document.createElement("option");
    option.value = p.uid;
    option.textContent = `${p.name} (${personYearsLabel(p)})`;
    select.appendChild(option);
  }
  if ([...select.options].some((o) => o.value === previous)) select.value = previous;
}

function renderHouseList() {
  const list = el("house-list");
  list.innerHTML = "";
  const houses = Object.values(dynasty.houses);
  el("house-count").textContent = houses.length;
  for (const h of houses) {
    const nation = h.nation_id != null ? world.nations.find((n) => n.id === h.nation_id) : null;
    const li = document.createElement("li");
    li.textContent = `${h.name}${nation ? ` — ${nation.name}` : ""}`;
    if (h.uid === selectedHouseId) li.classList.add("active-row");
    li.addEventListener("click", () => selectHouse(h.uid));
    list.appendChild(li);
  }
}

function selectHouse(hid) {
  selectedHouseId = hid;
  renderHouseList();
  renderHouseDetail();
  renderDynastyTree(selectedHouseId);
}

function renderHouseDetail() {
  const panel = el("dynasty-house-detail");
  const house = selectedHouseId ? dynasty.houses[selectedHouseId] : null;
  if (!house) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  el("house-detail-name").textContent = house.name;
  el("house-notes").value = house.notes || "";
  el("btn-house-generate-founder").hidden = !!house.founder_id;

  populatePersonSelect(el("new-person-father"), "Unknown");
  populatePersonSelect(el("new-person-mother"), "Unknown");
  populatePersonSelect(el("marriage-a"), "");
  populatePersonSelect(el("marriage-b"), "");
  populatePersonSelect(el("birth-father"), "Unknown");
  populatePersonSelect(el("birth-mother"), "Unknown");
  populatePersonSelect(el("death-person"), "");

  renderEventLog(selectedHouseId);
  el("house-detail-error").hidden = true;
}

function renderDynasty() {
  el("dynasty-year").value = dynasty.current_year;
  populateNationSelect(el("new-house-nation"));
  renderHouseList();
  renderHouseDetail();
  renderDynastyTree(selectedHouseId);
}

async function fetchDynasty() {
  const res = await fetch("/api/dynasty");
  if (!res.ok) return;
  dynasty = await res.json();
  renderDynasty();
}

function showDynastyError(msg) {
  const e = el("dynasty-error");
  e.textContent = msg;
  e.hidden = false;
}

function showHouseDetailError(msg) {
  const e = el("house-detail-error");
  e.textContent = msg;
  e.hidden = false;
}

async function handleSetDynastyYear() {
  const res = await fetch("/api/dynasty/year", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ year: el("dynasty-year").value }),
  });
  if (!res.ok) {
    showDynastyError((await res.json()).error || "failed to save year");
    return;
  }
  el("dynasty-error").hidden = true;
  dynasty = await res.json();
  renderDynasty();
}

async function handleCreateHouse() {
  const res = await fetch("/api/dynasty/houses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: el("new-house-name").value,
      nation_id: el("new-house-nation").value || null,
    }),
  });
  if (!res.ok) {
    showDynastyError((await res.json()).error || "failed to create house");
    return;
  }
  el("dynasty-error").hidden = true;
  el("new-house-name").value = "";
  dynasty = await res.json();
  renderDynasty();
}

async function handleGenerateHouse() {
  const res = await fetch("/api/dynasty/houses/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nation_id: el("new-house-nation").value || null }),
  });
  if (!res.ok) {
    showDynastyError((await res.json()).error || "failed to generate house");
    return;
  }
  el("dynasty-error").hidden = true;
  dynasty = await res.json();
  renderDynasty();
}

async function handleSaveHouse() {
  if (!selectedHouseId) return;
  const res = await fetch(`/api/dynasty/houses/${selectedHouseId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: el("house-notes").value }),
  });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to save house");
    return;
  }
  dynasty = await res.json();
  renderDynasty();
}

async function handleDeleteHouse() {
  if (!selectedHouseId) return;
  if (!confirm("Delete this house? People already recorded stay in the dynasty but lose their house link.")) return;
  const res = await fetch(`/api/dynasty/houses/${selectedHouseId}`, { method: "DELETE" });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to delete house");
    return;
  }
  selectedHouseId = null;
  dynasty = await res.json();
  renderDynasty();
}

async function handleGenerateFounder() {
  if (!selectedHouseId) return;
  const res = await fetch(`/api/dynasty/houses/${selectedHouseId}/generate-founder`, { method: "POST" });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to generate founder");
    return;
  }
  dynasty = await res.json();
  renderDynasty();
}

async function handleAddPerson() {
  const res = await fetch("/api/dynasty/people", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: el("new-person-name").value,
      sex: el("new-person-sex").value,
      birth_year: el("new-person-birth-year").value || null,
      father_id: el("new-person-father").value || null,
      mother_id: el("new-person-mother").value || null,
      house_id: selectedHouseId,
    }),
  });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to add person");
    return;
  }
  el("new-person-name").value = "";
  el("new-person-sex").value = "";
  el("new-person-birth-year").value = "";
  dynasty = await res.json();
  renderDynasty();
}

async function handleRecordMarriage() {
  const personAId = el("marriage-a").value;
  const personBId = el("marriage-b").value;
  if (!personAId || !personBId) {
    showHouseDetailError("choose both people first");
    return;
  }
  const res = await fetch("/api/dynasty/events/marriage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_a_id: personAId, person_b_id: personBId, year: el("marriage-year").value }),
  });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to record marriage");
    return;
  }
  dynasty = await res.json();
  renderDynasty();
}

async function handleRecordBirth() {
  const res = await fetch("/api/dynasty/events/birth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: el("birth-name").value,
      sex: el("birth-sex").value,
      year: el("birth-year").value,
      father_id: el("birth-father").value || null,
      mother_id: el("birth-mother").value || null,
      house_id: selectedHouseId,
    }),
  });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to record birth");
    return;
  }
  el("birth-name").value = "";
  el("birth-sex").value = "";
  el("birth-year").value = "";
  dynasty = await res.json();
  renderDynasty();
}

async function handleRecordDeath() {
  const personId = el("death-person").value;
  if (!personId) {
    showHouseDetailError("choose a person first");
    return;
  }
  const res = await fetch("/api/dynasty/events/death", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ person_id: personId, year: el("death-year").value }),
  });
  if (!res.ok) {
    showHouseDetailError((await res.json()).error || "failed to record death");
    return;
  }
  dynasty = await res.json();
  renderDynasty();
}

// ---- Wiring ----

el("btn-save").addEventListener("click", handleSave);
el("btn-delete").addEventListener("click", handleDelete);
el("btn-cancel").addEventListener("click", clearSelection);
el("settlement-search").addEventListener("input", (e) => renderSettlementTable(e.target.value));
el("btn-title-save").addEventListener("click", handleTitleSave);
el("style-select").addEventListener("change", (e) => {
  currentStyle = e.target.value;
  refreshImage();
});
el("btn-upload").addEventListener("click", handleUpload);
el("btn-switch-world").addEventListener("click", handleSwitchWorld);
el("btn-delete-world").addEventListener("click", handleDeleteWorld);
el("btn-download-world").addEventListener("click", handleDownloadWorld);
el("btn-save-as").addEventListener("click", handleSaveAs);
el("btn-generate").addEventListener("click", handleGenerate);
el("btn-undo").addEventListener("click", handleUndo);
el("btn-restore-backup").addEventListener("click", handleRestoreBackup);
for (const level of Object.keys(boundaryToggles)) {
  el(`toggle-${level}`).addEventListener("change", (e) => {
    boundaryToggles[level] = e.target.checked;
    refreshImage();
  });
}
el("grid-kind-select").addEventListener("change", (e) => {
  gridSettings.kind = e.target.value;
  refreshImage();
});
el("grid-size-miles").addEventListener("change", (e) => {
  gridSettings.sizeMiles = parseFloat(e.target.value) || 0;
  refreshImage();
});
el("map-wrap").addEventListener("mousedown", handleMapMouseDown);
window.addEventListener("mousemove", handleMapMouseMove);
window.addEventListener("mouseup", handleMapMouseUp);
el("btn-zoom-in").addEventListener("click", handleZoomIn);
el("btn-zoom-clear").addEventListener("click", handleZoomClear);
el("btn-download-map").addEventListener("click", handleDownloadMap);
el("btn-place-poi").addEventListener("click", handleTogglePlacePOI);
el("btn-place-settlement").addEventListener("click", handleTogglePlaceSettlement);
el("btn-measure-travel").addEventListener("click", handleToggleMeasureTravel);
el("btn-clear-travel").addEventListener("click", handleClearTravel);
el("btn-roll-npc").addEventListener("click", handleRollNPC);
el("btn-pick-encounter").addEventListener("click", handleTogglePickEncounter);
el("btn-roll-encounter-random").addEventListener("click", handleRollEncounterRandom);
el("btn-create-share").addEventListener("click", handleCreateShare);
el("btn-copy-share").addEventListener("click", handleCopyShare);
el("btn-revoke-share").addEventListener("click", handleRevokeShare);
el("btn-dynasty-year-save").addEventListener("click", handleSetDynastyYear);
el("btn-create-house").addEventListener("click", handleCreateHouse);
el("btn-generate-house").addEventListener("click", handleGenerateHouse);
el("btn-house-save").addEventListener("click", handleSaveHouse);
el("btn-house-delete").addEventListener("click", handleDeleteHouse);
el("btn-house-generate-founder").addEventListener("click", handleGenerateFounder);
el("btn-add-person").addEventListener("click", handleAddPerson);
el("btn-record-marriage").addEventListener("click", handleRecordMarriage);
el("btn-record-birth").addEventListener("click", handleRecordBirth);
el("btn-record-death").addEventListener("click", handleRecordDeath);

startApp();
