"use strict";
/* PiPair web UI — plain JS, no build step. Talks to the REST API in docs/API.md. */

const $ = (sel, root = document) => root.querySelector(sel);
const S = { status: null, playlists: [], media: [], settings: null, system: null,
            selected: null, draft: null, dirty: false, cueTransition: null,
            gpi: null, gpiDraft: null, gpiDirty: false };

// ---------------------------------------------------------------- helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
// "just now" / "14 min ago" / "2 days ago" — enough to tell a live problem
// from one logged days ago.
const ago = (epochSeconds) => {
  if (!epochSeconds) return "";
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return "just now";
  const units = [[86400, "day"], [3600, "hour"], [60, "min"]];
  for (const [size, name] of units) {
    if (s >= size) {
      const n = Math.floor(s / size);
      return `${n} ${name}${n > 1 && name !== "min" ? "s" : ""} ago`;
    }
  }
  return "just now";
};

const fmt = (s) => {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  const hh = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
};
const bytes = (n) => n > 1e9 ? (n / 1e9).toFixed(2) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.ceil(n / 1e3) + " kB";
const enc = encodeURIComponent;
const clone = (o) => JSON.parse(JSON.stringify(o));

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), err ? 5000 : 2200);
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error(data?.error || r.statusText); e.data = data; e.status = r.status; throw e; }
  return data;
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}

// ------------------------------------------------------------- live data
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  ws.onopen = () => { $("#conn").classList.add("ok"); $("#conn").title = "Connected"; loadAll(); };
  ws.onclose = () => { $("#conn").classList.remove("ok"); $("#conn").title = "Disconnected"; setTimeout(connect, 2000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "status") { S.status = m.status; renderNow(); renderLink(); }
    else if (m.type === "media") loadMedia();
    else if (m.type === "playlist") loadPlaylists();
    else if (m.type === "settings") { loadSettings(); loadGpi(); }
    else if (m.type === "gpi") onGpiEvent(m);
  };
}
async function loadAll() { await Promise.all([loadPlaylists(), loadMedia(), loadSettings(), loadSystem(), loadGpi()]); renderSettings(); }
async function loadPlaylists() {
  S.playlists = await api("GET", "/api/playlists");
  renderQuick(); renderPlaylistList(); renderNow();
  if (S.selected && !S.dirty) {
    const pl = S.playlists.find((p) => p.id === S.selected);
    if (pl) { S.draft = clone(pl); renderEditor(); } else { S.selected = null; S.draft = null; renderEditor(); }
  }
}
async function loadMedia() { S.media = await api("GET", "/api/media"); renderMedia(); if (S.draft) renderEditor(); }
async function loadSettings() { S.settings = await api("GET", "/api/settings"); renderSettings(); renderPlaylistList(); }
async function loadSystem() { S.system = await api("GET", "/api/system"); renderSys(); }
setInterval(() => { if (!document.hidden) loadSystem().catch(() => {}); }, 10000);

// ------------------------------------------------------------------ tabs
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  if (name === "api" && !$("#api-doc").childElementCount) loadDoc();
  try { localStorage.setItem("pipair.tab", name); } catch {}
}
$("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });

// ---------------------------------------------------- transition widget
function transitionEditor(value, { inherit = null, onchange = () => {} } = {}) {
  const wrap = h("span", { class: "trans" });
  const type = h("select", {},
    inherit ? h("option", { value: "" }, inherit) : null,
    h("option", { value: "cut" }, "Cut"),
    h("option", { value: "dissolve" }, "Dissolve"),
    h("option", { value: "dip" }, "Dip to color"));
  const dur = h("input", { type: "number", min: 0, max: 30, step: 0.1, title: "Duration (seconds)" });
  const unit = h("span", { class: "unit" }, "s");
  const color = h("input", { type: "color", title: "Dip color" });
  const set = (v) => {
    type.value = v ? v.type : (inherit ? "" : "cut");
    dur.value = v?.duration ?? 1; color.value = v?.color ?? "#000000"; sync();
  };
  const sync = () => {
    const t = type.value;
    dur.hidden = unit.hidden = !t || t === "cut";
    color.hidden = t !== "dip";
  };
  const get = () => type.value ? { type: type.value, duration: Number(dur.value) || 0, color: color.value } : null;
  for (const el of [type, dur, color]) el.addEventListener("input", () => { sync(); onchange(get()); });
  wrap.append(type, dur, unit, color);
  set(value);
  return { el: wrap, get, set };
}

// ------------------------------------------------------------ now playing
const cue = transitionEditor(null, { inherit: "Playlist default", onchange: (v) => {
  S.cueTransition = v; try { localStorage.setItem("pipair.cue", JSON.stringify(v)); } catch {}
} });
try { S.cueTransition = JSON.parse(localStorage.getItem("pipair.cue")); cue.set(S.cueTransition); } catch {}
$("#cue-transition").append(cue.el);
const cueBody = (extra = {}) => ({ ...extra, ...(S.cueTransition ? { transition: S.cueTransition } : {}) });

$("#t-play").onclick = () => act(() => api("POST", S.status?.state === "playing" ? "/api/pause" : "/api/play", {}));
$("#t-stop").onclick = () => act(() => api("POST", "/api/stop", cueBody()));
$("#t-next").onclick = () => act(() => api("POST", "/api/next", cueBody()));
$("#t-prev").onclick = () => act(() => api("POST", "/api/previous", cueBody()));
$("#t-loop").onclick = () => act(() => api("POST", "/api/loop-item", { enabled: !S.status?.loop_item }));
const playPlaylist = (id, index = 0) => act(() => api("POST", `/api/playlists/${enc(id)}/play`, cueBody({ index })));

function renderNow() {
  const st = S.status; if (!st) return;
  const badge = $("#st-state");
  badge.textContent = st.loading ? "loading" : st.state; badge.className = "badge " + st.state;
  $("#st-playlist").textContent = st.playlist ? st.playlist.name : "";
  $("#st-item").textContent = st.item ? st.item.media
    : st.state === "dormant" ? "Waiting for a display"
    : st.state === "stopped" ? "Stopped" : "—";
  $("#st-pos").textContent = fmt(st.position);
  $("#st-dur").textContent = fmt(st.duration);
  $("#st-bar").style.width = st.duration ? Math.min(100, (100 * st.position) / st.duration) + "%" : "0";
  const n = st.next;
  $("#st-next").textContent = !n ? "" : n.action ? `then: ${n.action}` :
    `next: ${n.media}${n.playlist !== st.playlist?.id ? ` (${n.playlist})` : ""} · ${n.transition.type}`;
  $("#t-play").textContent = st.state === "playing" ? "⏸" : "▶";
  $("#t-loop").classList.toggle("on", !!st.loop_item);
  if (st.next && st.loop_item) $("#st-next").textContent = "looping this item";
  renderPositionerState();
  renderLimitsNotice();
  const dormant = st.state === "dormant";
  const faulted = st.state === "error";
  const el = $("#st-error");
  if (dormant) {
    el.textContent = "No display connected. Playback resumes automatically when one is plugged in.";
    el.className = "error-line waiting";
  } else if (st.last_error) {
    // Stamp it: an error from startup should not read like a live fault hours
    // later. Red only while the player is actually in an error state.
    const when = ago(st.last_error_at);
    el.textContent = `Last error${when ? " (" + when + ")" : ""}: ${st.last_error}`;
    el.className = faulted ? "error-line" : "error-line stale";
  } else {
    el.textContent = "";
    el.className = "error-line";
  }
  document.querySelectorAll("#quick button").forEach((b) => b.classList.toggle("current", b.dataset.id === st.playlist?.id));
  renderCurrentItems();
}

function renderQuick() {
  const q = $("#quick"); q.replaceChildren();
  for (const pl of S.playlists) {
    q.append(h("button", { "data-id": pl.id, onclick: () => playPlaylist(pl.id), title: `Play ${pl.name}` },
      h("span", { class: "qn" }, pl.name), h("span", { class: "qm" }, `${pl.items.length} item${pl.items.length === 1 ? "" : "s"}`)));
  }
  if (!S.playlists.length) q.append(h("p", { class: "muted" }, "No playlists yet."));
}

function renderCurrentItems() {
  const st = S.status, ol = $("#cur-items");
  const pl = S.playlists.find((p) => p.id === st?.playlist?.id);
  $("#cur-title").textContent = pl ? pl.name : "Current playlist";
  ol.replaceChildren();
  if (!pl) { ol.append(h("li", { class: "muted" }, "Nothing playing")); return; }
  pl.items.forEach((it, i) => ol.append(h("li", {
    class: it.uid === st.item?.uid ? "current" : "", onclick: () => playPlaylist(pl.id, i), title: "Jump to this item" },
    h("span", { class: "n" }, i + 1), h("span", { class: "nm" }, it.media),
    h("span", { class: "muted small" }, it.kind === "image" ? fmt(it.duration ?? S.settings?.default_image_duration) : fmt(it.media_duration)))));
}

// The Bluetooth link to the head (absent when one device does everything).
function linkState(l) {
  if (!l) return null;
  if (l.ready) return ["ok", "Head connected"];
  if (l.connected) return ["wait", l.head && l.head.ok === false ? (l.head.no_display ? "Head has no display" : "Head renderer down") : "Head syncing…"];
  return ["", l.state === "error" ? "Bluetooth error" : "Searching for head…"];
}
function renderLink() {
  const l = S.status?.output?.link || S.system?.link, el = $("#link"), st = linkState(l);
  el.hidden = !st; if (!st) return;
  el.className = "linkpill " + st[0]; el.textContent = st[1];
  el.title = l.ready ? `${l.head?.name || l.device || "head"} · round trip ${l.rtt_ms ?? "?"} ms` : (l.last_error || "");
}

function renderSys() {
  const s = S.system; if (!s) return;
  renderLink();
  const r = s.renderer || {}, hl = s.health || {};
  const l = s.link, hh = l?.head?.health || {};
  const linkRows = l ? [
    ["Head link", (linkState(l) || ["", "–"])[1] + (l.device ? ` (${l.head?.name || l.device})` : "")],
    ["Round trip", l.rtt_ms != null ? `${l.rtt_ms} ms best, ${l.rtt_median_ms} ms typical` : "–"],
    ["Picture offset", `${r.av_offset_ms ?? 0} ms`],
    ["Head temperature", hh.temperature_c != null ? hh.temperature_c + " °C" : "–"],
    ["Head power", hh.throttled ? (hh.throttled.under_voltage_now ? "UNDER-VOLTAGE" : hh.throttled.under_voltage_since_boot ? "under-voltage since boot" : "ok") : "–"],
  ] : [];
  const rows = [
    ["Device", s.hardware?.model || "not a Raspberry Pi"],
    ...linkRows,
    ["Renderer", r.backend + (r.sink ? ` (${r.sink})` : "")],
    ["Display", r.display ? `${r.display.connector} ${r.display.mode || ""}${r.display.connected ? "" : " (disconnected)"}` : "–"],
    ["Output", r.render_size ? `${r.render_size.join("×")} @ ${r.fps} ${r.backend === "kms" ? "Hz (hardware planes)" : "fps"}` : "–"],
    ["Dropped frames", r.frames_dropped != null ? `${r.frames_dropped} of ${r.frames_rendered + r.frames_dropped}` : "–"],
    ["Audio", r.audio_device || "off"],
    ["Temperature", hl.temperature_c != null ? hl.temperature_c + " °C" : "–"],
    ["Throttling", hl.throttled ? (hl.throttled.under_voltage_now ? "UNDER-VOLTAGE" : hl.throttled.throttled_now ? "throttled" : hl.throttled.under_voltage_since_boot ? "under-voltage since boot" : "none") : "–"],
    ["Version", s.version],
  ];
  $("#sys").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

// live preview
let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  const on = $("#preview-toggle").checked;
  $(".preview-wrap").classList.toggle("off", !on);
  if (!on) return;
  previewTimer = setTimeout(() => {
    if (document.hidden || !$("#tab-now").classList.contains("active")) return schedulePreview();
    const img = new Image();
    img.onload = () => { $("#preview").src = img.src; schedulePreview(); };
    img.onerror = () => schedulePreview();
    img.src = "/api/preview.jpg?t=" + Date.now();
  }, 1500);
}
$("#preview-toggle").onchange = () => { try { localStorage.setItem("pipair.preview", $("#preview-toggle").checked ? "1" : "0"); } catch {} schedulePreview(); };

// --------------------------------------------------------------- playlists
function renderPlaylistList() {
  const ul = $("#pl-list"); ul.replaceChildren();
  for (const pl of S.playlists) {
    ul.append(h("li", { class: pl.id === S.selected ? "sel" : "", onclick: () => selectPlaylist(pl.id) },
      h("span", {}, pl.name), pl.id === S.settings?.default_playlist ? h("span", { class: "tag" }, "default") : null));
  }
}
function selectPlaylist(id) {
  if (S.dirty && id !== S.selected && !confirm("Discard unsaved changes?")) return;
  S.selected = id; S.dirty = false;
  S.draft = id ? clone(S.playlists.find((p) => p.id === id)) : null;
  renderPlaylistList(); renderEditor();
}
$("#pl-new").onclick = async () => {
  const name = prompt("Playlist name:"); if (!name) return;
  let id = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "playlist";
  let n = 2; const base = id; while (S.playlists.some((p) => p.id === id)) id = `${base}-${n++}`;
  await act(() => api("POST", "/api/playlists", { id, name }), "Playlist created");
  await loadPlaylists(); selectPlaylist(id);
};

function markDirty() { S.dirty = true; const n = $("#dirty-note"); if (n) n.textContent = "Unsaved changes"; }

function renderEditor() {
  const box = $("#pl-editor");
  const d = S.draft;
  if (!d) { box.replaceChildren(h("p", { class: "muted" }, "Select or create a playlist.")); return; }
  const name = h("input", { type: "text", value: d.name, oninput: (e) => { d.name = e.target.value; markDirty(); }, style: { width: "260px" } });
  const id = h("input", { type: "text", value: d.id, oninput: (e) => { d.id = e.target.value.trim(); markDirty(); }, style: { width: "180px" }, title: "Short URL-friendly id used by the API" });
  const trans = transitionEditor(d.transition, { inherit: "Global default", onchange: (v) => { d.transition = v; markDirty(); } });

  const endType = h("select", { onchange: (e) => { d.end_action.type = e.target.value; markDirty(); renderEditor(); } },
    ...[["loop", "Loop"], ["stop", "Stop (fade to background)"], ["hold", "Hold last frame"], ["goto", "Play another playlist"]]
      .map(([v, l]) => h("option", { value: v, selected: d.end_action.type === v }, l)));
  const endExtra = [];
  if (d.end_action.type === "goto") {
    endExtra.push(h("select", { onchange: (e) => { d.end_action.playlist = e.target.value; markDirty(); } },
      h("option", { value: "" }, "— choose —"),
      ...S.playlists.filter((p) => p.id !== S.selected).map((p) => h("option", { value: p.id, selected: d.end_action.playlist === p.id }, p.name))));
    endExtra.push(transitionEditor(d.end_action.transition, { inherit: "Its own transition", onchange: (v) => { d.end_action.transition = v; markDirty(); } }).el);
  }

  box.replaceChildren(
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h("label", { class: "field" }, h("span", {}, "ID"), id)),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("span", {}, "Transition between items"), trans.el)),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("span", {}, "At the end"), h("span", { class: "trans" }, endType, ...endExtra))),
    h("div", { class: "pane-head" }, h("h3", {}, `Items (${d.items.length})`),
      h("button", { class: "btn small", onclick: openPicker }, "+ Add media")),
    renderItems(d),
    h("div", { class: "editor-actions" },
      h("div", {},
        h("button", { class: "btn primary", onclick: savePlaylist }, "Save"),
        h("button", { class: "btn", onclick: () => { S.dirty = false; selectPlaylist(S.selected); } }, "Revert"),
        h("span", { id: "dirty-note", class: "dirty-note" }, S.dirty ? "Unsaved changes" : "")),
      h("div", {},
        h("button", { class: "btn", onclick: async () => { if (S.dirty) await savePlaylist(); playPlaylist(S.selected); } }, "▶ Play"),
        h("button", { class: "btn", disabled: S.settings?.default_playlist === S.selected,
          onclick: () => act(() => api("PATCH", "/api/settings", { default_playlist: S.selected }), "Default playlist set") }, "Make default"),
        h("button", { class: "btn danger", onclick: deletePlaylist }, "Delete"))));
}

function renderItems(d) {
  const ul = h("ul", { class: "items" });
  if (!d.items.length) return h("div", { class: "items items-empty" }, "No items. Add videos or images from the media library.");
  let dragFrom = null;
  d.items.forEach((it, i) => {
    const media = S.media.find((m) => m.name === it.media);
    const kind = it.kind || media?.kind;
    const thumb = h("div", { class: "thumb" }, kind === "image" ? "" : "VIDEO");
    if (kind === "image" && media) thumb.style.backgroundImage = `url(/api/media/${enc(it.media)}/thumb)`;
    const meta = h("div", { class: "imeta" });
    if (kind === "image") {
      meta.append("Show for", h("input", { type: "number", min: 0.1, step: 0.5, value: it.duration ?? "",
        placeholder: S.settings?.default_image_duration, oninput: (e) => { it.duration = e.target.value === "" ? null : Number(e.target.value); markDirty(); } }), "s");
    } else {
      meta.append(media?.info?.duration ? fmt(media.info.duration) : "video");
    }
    meta.append(h("select", { title: "Scaling", onchange: (e) => { it.fit = e.target.value || null; markDirty(); } },
      ...[["", "Fit: default"], ["contain", "Fit"], ["cover", "Fill (crop)"], ["stretch", "Stretch"]]
        .map(([v, l]) => h("option", { value: v, selected: (it.fit || "") === v }, l))));
    if (it.offset_x || it.offset_y) meta.append(h("span", { class: "offset-tag", title: "Position offset" }, `⌖ ${it.offset_x || 0}, ${it.offset_y || 0}`));
    meta.append(h("span", {}, "In:"), transitionEditor(it.transition, { inherit: "Playlist", onchange: (v) => { it.transition = v; markDirty(); } }).el);
    const move = (to) => { const [x] = d.items.splice(i, 1); d.items.splice(to, 0, x); markDirty(); renderEditor(); };
    const li = h("li", { class: media ? "" : "missing", draggable: true,
      ondragstart: (e) => { dragFrom = i; e.dataTransfer.effectAllowed = "move"; },
      ondragover: (e) => { e.preventDefault(); li.classList.add("drag-over"); },
      ondragleave: () => li.classList.remove("drag-over"),
      ondrop: (e) => { e.preventDefault(); li.classList.remove("drag-over"); if (dragFrom != null && dragFrom !== i) { const [x] = d.items.splice(dragFrom, 1); d.items.splice(i, 0, x); markDirty(); renderEditor(); } } },
      h("span", { class: "handle", title: "Drag to reorder" }, "⋮⋮"),
      thumb,
      h("div", {}, h("div", { class: "iname", title: it.media }, `${i + 1}. ${it.media}${media ? "" : " (missing)"}${media && it.head_warning ? " (not on the head)" : ""}`), meta),
      h("div", { class: "iactions" },
        h("button", { class: "icon-btn", title: "Position on screen", onclick: () => openPositioner(it) }, "⌖"),
        h("button", { class: "icon-btn", title: "Move up", disabled: i === 0, onclick: () => move(i - 1) }, "↑"),
        h("button", { class: "icon-btn", title: "Move down", disabled: i === d.items.length - 1, onclick: () => move(i + 1) }, "↓"),
        h("button", { class: "icon-btn", title: "Remove", onclick: () => { d.items.splice(i, 1); markDirty(); renderEditor(); } }, "✕")));
    ul.append(li);
  });
  return ul;
}

async function savePlaylist() {
  const d = S.draft;
  const body = { id: d.id, name: d.name, transition: d.transition, end_action: d.end_action,
    items: d.items.map(({ uid, media, duration, transition, fit, offset_x, offset_y }) =>
      ({ uid, media, duration, transition, fit, offset_x, offset_y })) };
  const saved = await act(() => api("PUT", `/api/playlists/${enc(S.selected)}`, body), "Saved");
  S.selected = saved.id; S.dirty = false; S.draft = clone(saved);
  await loadPlaylists();
}
async function deletePlaylist() {
  if (!confirm(`Delete playlist "${S.draft.name}"?`)) return;
  await act(() => api("DELETE", `/api/playlists/${enc(S.selected)}`), "Deleted");
  S.selected = null; S.draft = null; S.dirty = false; await loadPlaylists(); renderEditor();
}

// media picker
let pickOrder = [];
function openPicker() {
  pickOrder = [];
  const grid = $("#picker-grid"); grid.replaceChildren();
  if (!S.media.length) grid.append(h("p", { class: "muted" }, "The library is empty — upload files on the Media tab."));
  for (const m of S.media) {
    const card = mediaCard(m, false);
    card.onclick = () => {
      const k = pickOrder.indexOf(m.name);
      k >= 0 ? pickOrder.splice(k, 1) : pickOrder.push(m.name);
      grid.querySelectorAll(".mcard").forEach((c) => {
        const idx = pickOrder.indexOf(c.dataset.name);
        c.classList.toggle("sel", idx >= 0);
        c.querySelector(".order")?.remove();
        if (idx >= 0) c.append(h("span", { class: "order" }, idx + 1));
      });
    };
    grid.append(card);
  }
  $("#picker").showModal();
}
$("#picker").addEventListener("close", () => {
  if ($("#picker").returnValue !== "ok" || !pickOrder.length || !S.draft) return;
  for (const name of pickOrder) S.draft.items.push({ media: name, kind: S.media.find((m) => m.name === name)?.kind, duration: null, transition: null, fit: null });
  markDirty(); renderEditor();
});

// ------------------------------------------------------------- positioner
const P = { item: null, playlist: null, prevLoop: false, timer: null, saveTimer: null, drag: null };

async function openPositioner(item) {
  if (S.dirty) await savePlaylist();
  const pl = S.playlists.find((p) => p.id === S.selected);
  const index = pl.items.findIndex((it) => it.uid === item.uid);
  if (index < 0) return toast("Save the playlist first", true);
  P.item = S.draft.items.find((it) => it.uid === item.uid);
  P.playlist = pl.id;
  P.prevLoop = !!S.status?.loop_item;
  $("#pos-title").textContent = "Position · " + item.media;
  syncPosFields();
  // put the item on screen and keep it there while adjusting
  await act(() => api("POST", "/api/loop-item", { enabled: true }));
  if (!(S.status?.playlist?.id === pl.id && S.status?.item?.uid === item.uid))
    await act(() => api("POST", `/api/playlists/${enc(pl.id)}/play`, { index, transition: { type: "cut" } }));
  $("#positioner").showModal();
  refreshPosPreview();
}
function syncPosFields() { $("#pos-x").value = P.item.offset_x || 0; $("#pos-y").value = P.item.offset_y || 0; }
function refreshPosPreview() {
  clearTimeout(P.timer);
  if (!$("#positioner").open) return;
  const img = new Image();
  const next = () => { P.timer = setTimeout(refreshPosPreview, 400); };
  img.onload = () => { $("#pos-img").src = img.src; next(); };
  img.onerror = next;
  img.src = "/api/preview.jpg?t=" + Date.now();
}
function renderPositionerState() {
  if (!$("#positioner").open) return;
  const st = S.status;
  const onScreen = st?.item?.uid === P.item?.uid;
  $("#pos-state").textContent = !onScreen ? "not on screen" : st.state === "paused" ? "on screen · paused" : "on screen · looping";
  $("#pos-pause").textContent = st?.state === "paused" ? "▶ Resume" : "⏸ Pause";
}
function setOffset(x, y) {
  P.item.offset_x = Math.round(x); P.item.offset_y = Math.round(y);
  syncPosFields();
  clearTimeout(P.saveTimer);
  P.saveTimer = setTimeout(() => act(() => api("PATCH",
    `/api/playlists/${enc(P.playlist)}/items/${enc(P.item.uid)}`,
    { offset_x: P.item.offset_x, offset_y: P.item.offset_y })), 120);
}
document.querySelectorAll("#positioner [data-d]").forEach((b) => b.addEventListener("click", (e) => {
  const [dx, dy] = b.dataset.d.split(",").map(Number); const k = e.shiftKey ? 10 : 1;
  setOffset((P.item.offset_x || 0) + dx * k, (P.item.offset_y || 0) + dy * k);
}));
$("#pos-center").onclick = () => setOffset(0, 0);
$("#pos-x").oninput = () => setOffset(Number($("#pos-x").value) || 0, P.item.offset_y || 0);
$("#pos-y").oninput = () => setOffset(P.item.offset_x || 0, Number($("#pos-y").value) || 0);
$("#pos-pause").onclick = () => act(() => api("POST", "/api/toggle"));
$("#pos-done").onclick = () => $("#positioner").close();
$("#positioner").addEventListener("keydown", (e) => {
  const d = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }[e.key];
  if (!d || e.target.tagName === "INPUT") return;
  e.preventDefault(); const k = e.shiftKey ? 10 : 1;
  setOffset((P.item.offset_x || 0) + d[0] * k, (P.item.offset_y || 0) + d[1] * k);
});
$("#positioner").addEventListener("close", () => {
  clearTimeout(P.timer);
  if (!P.prevLoop) api("POST", "/api/loop-item", { enabled: false }).catch(() => {});
  renderEditor();
});
const dragArea = $("#pos-drag");
dragArea.addEventListener("pointerdown", (e) => {
  dragArea.setPointerCapture(e.pointerId);
  P.drag = { x: e.clientX, y: e.clientY, ox: P.item.offset_x || 0, oy: P.item.offset_y || 0 };
});
dragArea.addEventListener("pointermove", (e) => {
  if (!P.drag) return;
  // screen px -> output px: the preview shows the whole output letterboxed in this box
  const [rw, rh] = S.status?.output?.render_size || [1920, 1080];
  const box = dragArea.getBoundingClientRect();
  const scale = Math.max(rw / box.width, rh / box.height);
  setOffset(P.drag.ox + (e.clientX - P.drag.x) * scale, P.drag.oy + (e.clientY - P.drag.y) * scale);
});
dragArea.addEventListener("pointerup", () => { P.drag = null; });
dragArea.addEventListener("pointercancel", () => { P.drag = null; });

// ------------------------------------------------------------------ media
function mediaCard(m, withDelete = true) {
  const i = m.info || {};
  const det = [m.kind === "video" ? fmt(i.duration) : null, i.width ? `${i.width}×${i.height}` : null,
    i.fps ? `${i.fps} fps` : null, i.video_codec, bytes(m.size)].filter(Boolean).join(" · ");
  const th = h("div", { class: "mthumb" }, m.kind === "video" ? "VIDEO" : "");
  if (m.kind === "image") th.style.backgroundImage = `url(/api/media/${enc(m.name)}/thumb?v=${m.mtime})`;
  return h("div", { class: "mcard", "data-name": m.name },
    th,
    h("div", { class: "mbody" },
      h("div", { class: "mname", title: m.name }, m.name),
      h("div", { class: "mdet" }, det || (m.info ? "" : "reading…")),
      m.used_by?.length ? h("div", { class: "mdet" }, "In: " + m.used_by.join(", ")) : null,
      ...(m.warnings || []).map((w) => h("div", { class: "mwarn" }, "⚠ " + w))),
    withDelete ? h("button", { class: "icon-btn mdel", title: "Delete", onclick: (e) => { e.stopPropagation(); deleteMedia(m); } }, "✕") : null);
}
function renderMedia() {
  $("#media-count").textContent = `${S.media.length} file${S.media.length === 1 ? "" : "s"}`;
  const g = $("#media-grid"); g.replaceChildren(...S.media.map((m) => mediaCard(m)));
  if (!S.media.length) g.append(h("p", { class: "muted" }, "No media yet."));
}
async function deleteMedia(m) {
  const used = m.used_by?.length ? `\n\nIt will also be removed from: ${m.used_by.join(", ")}` : "";
  if (!confirm(`Delete ${m.name}?${used}`)) return;
  await act(() => api("DELETE", `/api/media/${enc(m.name)}?force=1`), "Deleted");
}
function upload(files) {
  for (const file of files) {
    const bar = h("div"); const row = h("div", { class: "up" }, h("span", {}, file.name), h("div", { class: "bar" }, bar), h("span", { class: "pct" }, "0%"));
    $("#uploads").append(row);
    const fd = new FormData(); fd.append("file", file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/media");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const p = Math.round((100 * e.loaded) / e.total); bar.style.width = p + "%"; row.querySelector(".pct").textContent = p + "%"; } };
    xhr.onload = () => {
      if (xhr.status < 300) { row.remove(); toast(`Uploaded ${file.name}`); loadMedia(); }
      else { row.querySelector(".pct").textContent = "failed"; toast(`${file.name}: ${JSON.parse(xhr.responseText || "{}").error || xhr.statusText}`, true); }
    };
    xhr.onerror = () => { row.querySelector(".pct").textContent = "failed"; };
    xhr.send(fd);
  }
}
$("#file-input").onchange = (e) => { upload(e.target.files); e.target.value = ""; };
const drop = $("#drop");
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };

// --------------------------------------------------------------- settings
function renderSettings() {
  const s = S.settings; if (!s) return;
  const f = $("#settings-form");
  if (f.contains(document.activeElement)) return; // don't clobber while editing
  const sel = (name, opts, val) => h("select", { name }, ...opts.map(([v, l]) => h("option", { value: v, selected: String(val) === String(v) }, l)));
  const trans = transitionEditor(s.default_transition);
  const vol = h("input", { type: "range", min: 0, max: 100, name: "volume", value: s.audio.volume,
    oninput: (e) => { volLabel.textContent = e.target.value + "%"; },
    onchange: (e) => act(() => api("PATCH", "/api/settings", { audio: { volume: Number(e.target.value) } })) });
  const volLabel = h("span", { class: "small muted" }, s.audio.volume + "%");
  const outputs = S.system?.outputs || [];
  const modes = [...new Set(outputs.flatMap((o) => o.modes || []))].filter((m) => /^\d+x\d+$/.test(m));
  const sizes = [["auto", "Auto (match display, max 1080p)"], ...["1920x1080", "1280x720", "1080x1920", ...modes].filter((v, i, a) => a.indexOf(v) === i).map((m) => [m, m])];
  const renderer = S.system?.renderer || {};
  const kmsMode = renderer.backend === "kms";
  const paired = !!S.system?.link;
  const dispModes = [["auto", "Auto (display's resolution; 30 Hz on a Pi 3)"], ...(renderer.display?.modes || []).map((m) => [m, m.replace("@", " @ ") + " Hz"]),
    ...(renderer.display?.forceable || []).map((m) => [m, m.replace("@", " @ ") + " Hz — forced (the display doesn't list it)"])];
  if (s.output.mode && !dispModes.some(([v]) => v === s.output.mode)) dispModes.push([s.output.mode, s.output.mode]);
  f.replaceChildren(
    h("fieldset", {}, h("legend", {}, "Playback"),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Default playlist (plays at startup)"),
          sel("default_playlist", [["", "— none —"], ...S.playlists.map((p) => [p.id, p.name])], s.default_playlist || "")),
        h("label", { class: "field" }, h("span", {}, "Default image duration (s)"),
          h("input", { type: "number", name: "default_image_duration", min: 0.1, step: 0.5, value: s.default_image_duration })),
        h("label", { class: "field" }, h("span", {}, "Default scaling"),
          sel("default_fit", [["contain", "Fit (letterbox)"], ["cover", "Fill (crop)"], ["stretch", "Stretch"]], s.default_fit))),
      h("div", { class: "row" },
        h("div", { class: "field" }, h("span", {}, "Default transition"), trans.el),
        h("label", { class: "field" }, h("span", {}, "Background color"),
          h("input", { type: "color", name: "background_color", value: s.background_color })))),
    h("fieldset", {}, h("legend", {}, "Output"),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Display mode"), sel("mode", dispModes, s.output.mode || "auto")),
        kmsMode ? null : h("label", { class: "field" }, h("span", {}, "Compositing resolution"), sel("render_size", sizes, s.output.render_size)),
        kmsMode ? null : h("label", { class: "field" }, h("span", {}, "Frame rate"), sel("fps", [[24, "24"], [25, "25"], [30, "30"], [50, "50"], [60, "60"]], s.output.fps)),
        kmsMode ? null : h("label", { class: "field" }, h("span", {}, "Rotation"), sel("rotation", [[0, "0°"], [90, "90° (portrait)"], [180, "180°"], [270, "270° (portrait)"]], s.output.rotation))),
      renderer.limits ? h("p", { class: "hint", style: { color: "var(--warn)" } }, limitsText(renderer)) : null,
      h("p", { class: "hint" }, kmsMode
        ? "Video goes straight to the display hardware at the display's resolution. On a Pi 3, Auto picks 30 Hz when the display supports it: at 60 Hz the Pi 3 can't show two 1080p videos at once, so dissolves between videos would become cuts. Changing the mode briefly restarts playback."
        : "Video is composited at this resolution and scaled to the display's preferred mode. Changing output settings briefly restarts playback.")),
    paired ? h("fieldset", {}, h("legend", {}, "Head"),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Picture offset (ms)"),
          h("input", { type: "number", name: "av_offset_ms", min: -2000, max: 2000, step: 5, value: s.link.av_offset_ms, style: { width: "110px" } })),
        h("label", { class: "field" }, h("span", {}, "Head to connect to"),
          h("input", { type: "text", name: "link_peer", value: s.link.peer, placeholder: "any PiPair head", style: { width: "220px" } }))),
      h("p", { class: "hint" },
        "Picture offset moves the picture against the sound: a positive number shows it later, a negative one earlier. Use it to line up lips and voice once the projector and speakers are in place. Saving a new offset restarts the current item."),
      h("p", { class: "hint" },
        "Leave the head blank to use whichever PiPair head is in range, or enter one head's Bluetooth name (its hostname) or address when several are nearby.")) : null,
    h("fieldset", {}, h("legend", {}, "Audio"),
      h("div", { class: "row" },
        h("label", { class: "check" }, h("input", { type: "checkbox", name: "audio_enabled", checked: s.audio.enabled }), "Play audio"),
        h("label", { class: "field" }, h("span", {}, "ALSA device"), h("input", { type: "text", name: "audio_device", value: s.audio.device, style: { width: "220px" } })),
        h("div", { class: "field" }, h("span", {}, "Volume"), h("span", {}, vol, " ", volLabel))),
      h("p", { class: "hint" },
        paired ? "On auto, sound goes to a USB or HAT sound card if one is fitted, otherwise the 3.5 mm jack. To choose an output yourself: "
          : "Leave the device on auto to play through HDMI. To choose an output yourself: ",
        h("code", {}, "default:CARD=vc4hdmi"), " is the HDMI port, and ",
        h("code", {}, "default:CARD=Headphones"), " is the 3.5 mm analog jack. On a Pi 4 or 5, which has two HDMI ports, use ",
        h("code", {}, "default:CARD=vc4hdmi0"), " or ", h("code", {}, "default:CARD=vc4hdmi1"), "."),
      h("p", { class: "hint" },
        "The analog jack also needs ", h("code", {}, "dtparam=audio=on"), " in ",
        h("code", {}, "/boot/firmware/config.txt"), " — Raspberry Pi OS sets that by default. ",
        "Turning audio on or off, or changing the device, briefly restarts playback; volume changes apply straight away.")),
    h("div", { class: "editor-actions" },
      h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Save settings")),
      h("div", {}, h("button", { class: "btn", type: "button", onclick: () => act(() => api("POST", "/api/system/restart-renderer"), "Renderer restarted") }, "Restart renderer"))));
  f.onsubmit = async (e) => {
    e.preventDefault();
    const v = (n) => f.elements[n].value;
    await act(() => api("PATCH", "/api/settings", {
      default_playlist: v("default_playlist") || null,
      default_image_duration: Number(v("default_image_duration")),
      default_fit: v("default_fit"),
      default_transition: trans.get(),
      background_color: v("background_color"),
      output: kmsMode ? { mode: v("mode") }
        : { mode: v("mode"), render_size: v("render_size"), fps: Number(v("fps")), rotation: Number(v("rotation")) },
      audio: { enabled: f.elements.audio_enabled.checked, device: v("audio_device") || "auto", volume: Number(v("volume")) },
      ...(paired ? { link: { peer: v("link_peer").trim(), av_offset_ms: Number(v("av_offset_ms")) || 0 } } : {}),
    }), "Settings saved");
    document.activeElement?.blur(); await loadSettings();
  };
}

// Shown when the display hardware can't show two videos at once (Pi 3 at 50/60 Hz).
function limitsText(o) {
  const mode = (o?.display?.mode || "").replace("@", " @ ") + " Hz";
  return `At ${mode}, this Raspberry Pi 3 can't show two videos on screen at once. When one video dissolves into another, the outgoing clip freezes on its current frame and the new clip fades in over it, starting about ¼ second after the freeze. Cuts, dips to color, and dissolves to or from images aren't affected.`;
}
function renderLimitsNotice() {
  const o = S.status?.output, box = $("#pl-notice");
  const on = !!o?.limits;
  box.hidden = !on;
  if (!on) return;
  const has30 = (o.display?.modes || []).some((m) => /@30$/.test(m));
  box.replaceChildren(h("strong", {}, "Video-to-video dissolves freeze on this display"),
    h("p", {}, limitsText(o)),
    h("p", { class: "small muted" }, has30
      ? "This display lists a 30 Hz mode; choose it in Settings → Display mode to get full-motion dissolves."
      : "For full-motion dissolves, use a display that accepts 1080p at 30 Hz (most TVs and projectors do; you can also try forcing it in Settings → Display mode)."));
}

// ---------------------------------------------------------------- triggers
const GPI_ACTIONS = [["play", "Play playlist"], ["next", "Next item"], ["previous", "Previous item"], ["stop", "Stop"],
  ["pause", "Pause"], ["resume", "Resume"], ["toggle", "Pause / resume"], ["loop_item", "Loop item"]];
const GPI_FIELDS = ["id", "name", "pin", "fire_on", "pull", "active_low", "debounce_ms", "holdoff_ms", "action"];

async function loadGpi() {
  try { S.gpi = await api("GET", "/api/gpi"); } catch { return; }
  if (!S.gpiDirty) S.gpiDraft = { enabled: S.gpi.enabled, inputs: S.gpi.inputs.map((i) => Object.fromEntries(GPI_FIELDS.map((k) => [k, clone(i[k])]))) };
  renderGpi();
}
let gpiReload = null;
function onGpiEvent(m) {
  clearTimeout(gpiReload); gpiReload = setTimeout(loadGpi, 150);
  if (m.fired) S.gpiFlash = m.input;  // highlighted by the re-render below
  if (m.error) toast(`Trigger: ${m.error}`, true);
}
function gpiDirty() { S.gpiDirty = true; $("#gpi-dirty").textContent = "Unsaved changes"; }

function renderGpi() {
  const g = S.gpi, d = S.gpiDraft; if (!g || !d) return;
  $("#gpi-status").textContent = g.error ? g.error : g.available ? `Watching ${g.chip}` : d.enabled ? "Idle (no inputs)" : "Disabled";
  $("#gpi-status").style.color = g.error ? "var(--err)" : "";
  $("#gpi-enabled").checked = d.enabled;
  $("#gpi-dirty").textContent = S.gpiDirty ? "Unsaved changes" : "";
  const live = Object.fromEntries(g.inputs.map((i) => [i.id, i]));
  const pinOpts = g.pins.map((p) => [p.gpio, `GPIO ${p.gpio} — pin ${p.header_pin}${p.note ? ` (${p.note})` : ""}`]);
  const list = $("#gpi-list"); list.replaceChildren();
  setTimeout(() => { S.gpiFlash = null; }, 0);
  if (!d.inputs.length) list.append(h("div", { class: "card muted" }, "No inputs yet. Add one to fire a playlist from a button or contact closure."));
  d.inputs.forEach((inp, idx) => {
    const L = live[inp.id] || {};
    const sel = (opts, val, on) => h("select", { onchange: (e) => { on(e.target.value); gpiDirty(); } },
      ...opts.map(([v, l]) => h("option", { value: v, selected: String(val) === String(v) }, l)));
    const num = (val, on, attrs = {}) => h("input", { type: "number", value: val, ...attrs, oninput: (e) => { on(Number(e.target.value)); gpiDirty(); } });
    const a = inp.action;
    const setType = (t) => { inp.action = { type: t, ...(t === "play" ? { playlist: S.playlists[0]?.id, index: 0 } : {}), ...(t === "loop_item" ? { mode: "toggle" } : {}) }; renderGpi(); };
    const actionBits = [];
    if (a.type === "play") {
      const pl = S.playlists.find((p) => p.id === a.playlist);
      actionBits.push(sel([["", "— playlist —"], ...S.playlists.map((p) => [p.id, p.name])], a.playlist || "", (v) => { a.playlist = v; a.index = 0; renderGpi(); }));
      actionBits.push(h("span", { class: "small muted" }, "from item"),
        sel((pl?.items || [{ media: "1" }]).map((it, i) => [i, `${i + 1}. ${it.media}`]), a.index || 0, (v) => { a.index = Number(v); }));
    }
    if (["play", "next", "previous", "stop"].includes(a.type))
      actionBits.push(h("span", { class: "small muted" }, "transition"), transitionEditor(a.transition, { inherit: "Default", onchange: (v) => { a.transition = v; gpiDirty(); } }).el);
    if (a.type === "loop_item") actionBits.push(sel([["toggle", "Toggle"], ["on", "On"], ["off", "Off"]], a.mode, (v) => { a.mode = v; }));
    const stateTxt = L.error ? "error" : L.state || "—";
    list.append(h("div", { class: "card gpi-row" + (S.gpiFlash === inp.id ? " flash" : ""), "data-gpi": inp.id },
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Name"), h("input", { type: "text", value: inp.name, style: { width: "180px" }, oninput: (e) => { inp.name = e.target.value; gpiDirty(); } })),
        h("label", { class: "field" }, h("span", {}, "Input"), sel(pinOpts, inp.pin, (v) => { inp.pin = Number(v); })),
        h("label", { class: "field" }, h("span", {}, "Fires when contact"), sel([["close", "closes"], ["open", "opens"], ["both", "closes or opens"]], inp.fire_on, (v) => { inp.fire_on = v; })),
        h("div", { class: "gpi-meta" },
          h("span", { class: "pill " + (L.error ? "err" : L.state === "closed" ? "closed" : ""), title: L.error || "Live input state" }, stateTxt),
          h("span", { title: L.last ? new Date(L.last * 1000).toLocaleString() : "" }, L.count ? `fired ${L.count}×${L.last ? " · " + new Date(L.last * 1000).toLocaleTimeString() : ""}` : "not fired"),
          h("button", { class: "btn small", title: "Run the action now", disabled: S.gpiDirty, onclick: () => act(() => api("POST", `/api/gpi/${enc(inp.id)}/fire`), "Fired") }, "Test"),
          h("button", { class: "icon-btn", title: "Remove", onclick: () => { d.inputs.splice(idx, 1); gpiDirty(); renderGpi(); } }, "✕"))),
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", {}, "Action"), sel(GPI_ACTIONS, a.type, setType)),
        h("span", { class: "trans" }, ...actionBits)),
      L.last_error ? h("div", { class: "error-line" }, "Last run failed: " + L.last_error) : null,
      L.error ? h("div", { class: "error-line" }, `GPIO ${inp.pin}: ${L.error}`) : null,
      h("details", {},
        h("summary", {}, "Advanced"),
        h("div", { class: "row" },
          h("label", { class: "field" }, h("span", {}, "Pull resistor"), sel([["up", "Pull-up (contact to GND)"], ["down", "Pull-down"], ["none", "None (external)"]], inp.pull, (v) => { inp.pull = v; inp.active_low = v !== "down"; renderGpi(); })),
          h("label", { class: "field" }, h("span", {}, "Closed means"), sel([["true", "pin low"], ["false", "pin high"]], String(inp.active_low), (v) => { inp.active_low = v === "true"; })),
          h("label", { class: "field" }, h("span", {}, "Debounce (ms)"), num(inp.debounce_ms, (v) => (inp.debounce_ms = v), { min: 0, max: 1000 })),
          h("label", { class: "field" }, h("span", {}, "Hold-off (ms)"), num(inp.holdoff_ms, (v) => (inp.holdoff_ms = v), { min: 0, max: 60000, title: "Ignore re-triggers for this long" }))))));
  });
}
$("#gpi-enabled").onchange = (e) => { S.gpiDraft.enabled = e.target.checked; gpiDirty(); };
$("#gpi-add").onclick = () => {
  const used = new Set(S.gpiDraft.inputs.map((i) => i.pin));
  const pin = [17, 27, 22, 23, 24, 25, 5, 6, 16, 26].find((p) => !used.has(p)) ?? 17;
  S.gpiDraft.inputs.push({ id: Math.random().toString(16).slice(2, 8), name: `Button ${S.gpiDraft.inputs.length + 1}`, pin, fire_on: "close", pull: "up",
    active_low: true, debounce_ms: 20, holdoff_ms: 300, action: { type: "play", playlist: S.playlists[0]?.id, index: 0 } });
  gpiDirty(); renderGpi();
};
$("#gpi-revert").onclick = () => { S.gpiDirty = false; loadGpi(); };
$("#gpi-save").onclick = async () => {
  S.gpi = await act(() => api("PUT", "/api/gpi", S.gpiDraft), "Triggers saved");
  S.gpiDirty = false; S.gpiDraft = null; await loadGpi();
};

// -------------------------------------------------------------------- docs
async function loadDoc() {
  const md = await api("GET", "/api/docs");
  $("#api-doc").innerHTML = markdown(md);
}
function markdown(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, u) => `<a href="${u.startsWith("#") || u.startsWith("http") ? u : "#"}">${t}</a>`);
  const out = []; const lines = src.split("\n"); let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.startsWith("```")) { const buf = []; i++; while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]); i++; out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); continue; }
    const hm = l.match(/^(#{1,4})\s+(.*)/);
    if (hm) { const id = hm[2].toLowerCase().replace(/[^a-z0-9]+/g, "-"); out.push(`<h${hm[1].length} id="${id}">${inline(hm[2])}</h${hm[1].length}>`); i++; continue; }
    if (l.startsWith("|")) {
      const rows = []; while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      out.push("<table><thead><tr>" + cells(rows[0]).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>" +
        rows.slice(2).map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }
    if (/^\s*[-*]\s/.test(l)) { const buf = []; while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) buf.push(lines[i++].replace(/^\s*[-*]\s/, "")); out.push("<ul>" + buf.map((b) => `<li>${inline(b)}</li>`).join("") + "</ul>"); continue; }
    if (!l.trim()) { i++; continue; }
    const buf = []; while (i < lines.length && lines[i].trim() && !/^(#|```|\||\s*[-*]\s)/.test(lines[i])) buf.push(lines[i++]);
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }
  return out.join("\n");
}

// ------------------------------------------------------------------- boot
try { $("#preview-toggle").checked = localStorage.getItem("pipair.preview") !== "0"; } catch {}
try { const t = localStorage.getItem("pipair.tab"); if (t && $("#tab-" + t)) showTab(t); } catch {}
api("GET", "/api/status").then((st) => { S.status = st; renderNow(); }).catch(() => {});
connect();
schedulePreview();
window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
