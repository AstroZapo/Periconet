
const API_BASE = "";
let SETUPS = [];
let SETUP_AXIS_RANGE = {};
let SETUP_AXIS_UNIT = {};
let lastMessageTime = Date.now();
let configRevision = null;
const STATES = {};
const RENDER_KEYS = {};
const IMAGE_STATES = {};
let audioContext = null;
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
function changed(id, field, data, render) {
  const key = `${id}:${field}`;
  const value = JSON.stringify(data);
  if (RENDER_KEYS[key] !== value) { RENDER_KEYS[key] = value; render(); }
}
function guideUnit(id) {
  return SETUP_AXIS_UNIT[id] === "px" || !STATES[id]?.pixel_scale ? "px" : "arcsec";
}
function renderGuiding(id) {
  const state = STATES[id];
  if (!state) return;
  const unit = guideUnit(id);
  renderGuideCanvas(id, state.guide_steps || [], SETUP_AXIS_RANGE[id], unit);
  renderRms(id, state.guide_steps || [], unit);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function updateSequenceEndButton(setupId, button) {
  const state = STATES[setupId];
  const running = state?.connected === true && state.sequence_running === true;
  button.disabled = !running || button.dataset.sending === "true";
  button.title = running ? "Saltar al bloque final de la secuencia" : "Requiere una secuencia avanzada en curso";
}

async function skipSequenceToEnd(setup, button, status) {
  if (button.disabled || !window.confirm(`¿Saltar al bloque final de la secuencia de «${setup.name}»?`)) return;
  button.disabled = true;
  button.dataset.sending = "true";
  status.classList.remove("error");
  status.textContent = "Enviando orden a NINA…";
  try {
    const response = await fetch(`${API_BASE}/api/setups/${encodeURIComponent(setup.id)}/sequence/to-end`, {
      method: "POST",
      headers: {"X-Periconet-Action": "sequence-to-end"},
    });
    const result = await response.json();
    if (!response.ok || result.ok !== true) {
      status.classList.add("error");
      status.textContent = result.detail || "No se pudo confirmar la orden.";
      return;
    }
    status.textContent = "NINA ha aceptado el salto al bloque final.";
  } catch {
    status.classList.add("error");
    status.textContent = "No se pudo confirmar la orden. Comprueba la secuencia antes de repetirla.";
  } finally {
    delete button.dataset.sending;
    updateSequenceEndButton(setup.id, button);
  }
}

function buildPanel(setup) {
  const panel = el("div", "panel");
  panel.id = `panel-${setup.id}`;

  const header = el("div", "panel-header");
  const left = el("div");
  left.innerHTML = `<span class="dot disconnected" id="dot-${setup.id}"></span>`;
  left.appendChild(el("h2", null, setup.name));
  const badge = el("span", "badge", "sin datos");
  badge.id = `guiding-badge-${setup.id}`;
  const actions = el("div", "panel-actions");
  const endButton = el("button", "sequence-end-button", "Ir al fin de secuencia");
  endButton.type = "button";
  endButton.id = `sequence-end-${setup.id}`;
  updateSequenceEndButton(setup.id, endButton);
  const endStatus = el("div", "sequence-end-status");
  endStatus.setAttribute("role", "status");
  endButton.addEventListener("click", () => skipSequenceToEnd(setup, endButton, endStatus));
  actions.appendChild(badge);
  actions.appendChild(endButton);
  header.appendChild(left);
  header.appendChild(actions);

  const body = el("div", "panel-body");

  const imageCol = el("div", "image-col");
  const imageCell = el("div", "image-cell");
  imageCell.innerHTML = `
    <span class="placeholder">esperando imagen…</span>
    <span class="image-zoom-badge">🔍 Zoom HD</span>
  `;
  imageCell.id = `image-cell-${setup.id}`;
  imageCell.title = "Haz click para inspeccionar a alta resolución";
  imageCell.addEventListener("click", () => openImageModal(setup.id));

  const exposureBlock = el("div", "exposure-block");
  exposureBlock.id = `exposure-block-${setup.id}`;
  exposureBlock.innerHTML = `
    <div class="exposure-header">
      <span class="exposure-status" id="exposure-status-${setup.id}">En reposo</span>
      <span class="exposure-timer" id="exposure-timer-${setup.id}">—</span>
    </div>
    <div class="exposure-bar-track">
      <div class="exposure-bar-fill" id="exposure-bar-${setup.id}"></div>
    </div>
  `;

  imageCol.appendChild(imageCell);
  imageCol.appendChild(exposureBlock);

  const dataCell = el("div", "data-cell");

  const targetBlock = el("div", "block target-block");
  targetBlock.id = `target-block-${setup.id}`;
  targetBlock.innerHTML = `
    <div class="target-header">
      <div class="target-title-wrap">
        <span class="target-label">Objetivo en curso</span>
        <h3 class="target-name" id="target-name-${setup.id}">—</h3>
      </div>
      <div class="target-coords" id="target-coords-${setup.id}">
        <span class="coord-item"><span class="lbl">RA</span> <span class="val" id="target-ra-${setup.id}">—</span></span>
        <span class="coord-item"><span class="lbl">DEC</span> <span class="val" id="target-dec-${setup.id}">—</span></span>
        <span class="coord-item"><span class="lbl">ALT</span> <span class="val" id="target-alt-${setup.id}">—</span></span>
        <span class="coord-item"><span class="lbl">AZ</span> <span class="val" id="target-az-${setup.id}">—</span></span>
      </div>
    </div>
    <div class="target-chart-wrap">
      <canvas id="target-alt-canvas-${setup.id}" class="target-alt-canvas" height="85"></canvas>
    </div>
    <div class="target-filters-wrap" id="target-filters-${setup.id}"></div>
  `;

  const guideBlock = el("div", "block");
  guideBlock.appendChild(el("div", "block-title", "Guiado en vivo (RA / DEC)"));
  const canvas = el("canvas");
  canvas.id = `guide-canvas-${setup.id}`;
  canvas.height = 190;
  guideBlock.appendChild(canvas);
  const rmsRow = el("div", "rms-row");
  rmsRow.id = `rms-${setup.id}`;
  guideBlock.appendChild(rmsRow);

  const hfrBlock = el("div", "block");
  hfrBlock.appendChild(el("div", "block-title", "Histórico HFR"));
  const hfrList = el("div", "hfr-list");
  hfrList.id = `hfr-list-${setup.id}`;
  hfrBlock.appendChild(hfrList);

  const meridianBlock = el("div", "block meridian");
  meridianBlock.appendChild(el("div", "block-title", "Meridian flip"));
  const meridianVal = el("div", "value", "—");
  meridianVal.id = `meridian-${setup.id}`;
  meridianBlock.appendChild(meridianVal);

  const weatherBlock = el("div", "block");
  weatherBlock.appendChild(el("div", "block-title", "Meteorología"));
  const wg = el("div", "weather-grid");
  wg.id = `weather-${setup.id}`;
  weatherBlock.appendChild(wg);

  dataCell.appendChild(targetBlock);
  dataCell.appendChild(guideBlock);
  dataCell.appendChild(hfrBlock);
  dataCell.appendChild(meridianBlock);
  dataCell.appendChild(weatherBlock);

  body.appendChild(imageCol);
  body.appendChild(dataCell);

  panel.appendChild(header);
  panel.appendChild(endStatus);
  panel.appendChild(body);
  return panel;
}

const GUIDE_WINDOW = 120;
const GUIDE_AXIS_RANGE_DEFAULT = 3;

function renderGuideCanvas(setupId, allSteps, axisRange, unit) {
  const canvas = document.getElementById(`guide-canvas-${setupId}`);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  canvas.title = `Error de guiado RA/DEC (${unit === "px" ? "píxeles" : "arcosegundos"})`;
  const w = canvas.clientWidth || 500;
  const h = canvas.height;
  canvas.width = w;
  ctx.clearRect(0, 0, w, h);

  const leftPad = 26;
  const plotW = w - leftPad;
  const pxPerPoint = plotW / GUIDE_WINDOW;

  const steps = allSteps.slice(-GUIDE_WINDOW);
  if (!steps.length) return;

  const field = unit === "px" ? "_px" : "_arcsec";
  const ra = steps.map(s => s[`ra_distance${field}`] ?? 0);
  const dec = steps.map(s => s[`dec_distance${field}`] ?? 0);

  const range = axisRange || GUIDE_AXIS_RANGE_DEFAULT;
  const min = -range, max = range;
  const yFor = (v) => h - ((v - min) / (max - min)) * h;

  const tickStep = range > 6 ? 2 : 1;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.fillStyle = "rgba(255,255,255,0.35)";
  ctx.font = "10px " + getComputedStyle(document.body).fontFamily;
  for (let v = -range; v <= range; v += tickStep) {
    const y = yFor(v);
    ctx.beginPath();
    ctx.moveTo(leftPad, y);
    ctx.lineTo(w, y);
    ctx.stroke();
    if (v !== 0) ctx.fillText(String(v), 2, y + 3);
  }
  ctx.strokeStyle = "rgba(255,255,255,0.2)";
  ctx.beginPath();
  ctx.moveTo(leftPad, yFor(0));
  ctx.lineTo(w, yFor(0));
  ctx.stroke();

  const drawLine = (values, color) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = leftPad + i * pxPerPoint;
      const y = yFor(Math.max(min, Math.min(max, v)));
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };

  drawLine(ra, "#c9463a");
  drawLine(dec, "#4a9fd8");
}

const hfrDateFormat = new Intl.DateTimeFormat("es-ES", {
  dateStyle: "short", timeStyle: "medium", hourCycle: "h23",
});

function renderHfrList(setupId, history) {
  const list = document.getElementById(`hfr-list-${setupId}`);
  if (!list) return;
  list.innerHTML = "";
  (history || []).forEach((h, i) => {
    const hfr = h.HFR ?? h.Hfr ?? h.hfr;
    if (!Number.isFinite(hfr)) return;
    const target = typeof h.TargetName === "string" ? h.TargetName.trim() : "";
    const date = typeof h.Date === "string" ? new Date(h.Date) : null;
    const row = el("div", "hfr-row");
    const label = el("span", "idx", `Imagen ${i + 1} - ${target || "Objetivo desconocido"} - `);
    if (date && Number.isFinite(date.getTime())) {
      const time = el("time", null, hfrDateFormat.format(date));
      time.dateTime = date.toISOString();
      label.appendChild(time);
    } else {
      label.appendChild(el("span", null, "Sin fecha y hora"));
    }
    row.appendChild(label);
    row.appendChild(el("span", "val", hfr.toFixed(2)));
    list.appendChild(row);
  });
}

function rms(values) {
  if (!values.length) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const sq = values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length;
  return Math.sqrt(sq);
}

function renderRms(setupId, allSteps, unit = "arcsec") {
  const row = document.getElementById(`rms-${setupId}`);
  if (!row) return;
  const steps = allSteps.slice(-GUIDE_WINDOW);
  if (!steps.length) { row.textContent = "Sin datos de guiado"; return; }
  const suffix = unit === "px" ? "_px" : "_arcsec";
  const ra = steps.map(s => s[`ra_distance${suffix}`]);
  const dec = steps.map(s => s[`dec_distance${suffix}`]);
  const raRms = rms(ra);
  const decRms = rms(dec);
  const totalRms = Math.sqrt(raRms ** 2 + decRms ** 2);

  row.innerHTML = "";
  const item = (label, value, cls) => {
    const span = el("span", cls || "");
    span.innerHTML = `${label} <span class="rms-value">${value.toFixed(2)} ${unit === "px" ? "px" : '″'}</span>`;
    return span;
  };
  row.appendChild(item("RA", raRms, "rms-ra"));
  row.appendChild(item("DEC", decRms, "rms-dec"));
  row.appendChild(item("Total", totalRms));
}

function renderMeridian(setupId, mount) {
  const target = document.getElementById(`meridian-${setupId}`);
  if (!target) return;
  if (!mount || mount.Connected === false) {
    target.textContent = "montaje desconectado";
    return;
  }
  const t = mount.TimeToMeridianFlip;
  if (t === undefined || t === null) {
    target.textContent = "sin datos de montaje";
    return;
  }
  const flipTime = new Date(Date.now() + t * 3600 * 1000);
  const hh = String(flipTime.getHours()).padStart(2, "0");
  const mm = String(flipTime.getMinutes()).padStart(2, "0");
  target.textContent = `${hh}:${mm}`;
}

function renderWeather(setupId, weather, safety) {
  const grid = document.getElementById(`weather-${setupId}`);
  if (!grid) return;
  if (weather?.Connected === false) weather = null;
  if (safety?.Connected === false) safety = null;
  const windKmh = typeof weather?.WindSpeed === "number" ? weather.WindSpeed * 3.6 : undefined;
  const rows = [
    ["Temp. cielo", weather?.SkyTemperature, "°C"],
    ["Temp. ambiente", weather?.Temperature, "°C"],
    ["Viento", windKmh !== undefined ? windKmh.toFixed(1) : undefined, "km/h"],
    ["Humedad", weather?.Humidity, "%"],
  ];
  grid.innerHTML = "";
  rows.forEach(([label, value, unit]) => {
    grid.appendChild(el("div", "label", label));
    grid.appendChild(el("div", "value", value !== undefined && value !== null ? `${value} ${unit}` : "—"));
  });
  const safeVal = safety?.IsSafe;
  grid.appendChild(el("div", "label", "Estado"));
  grid.appendChild(el("div", "value", safeVal === true ? "SAFE" : safeVal === false ? "UNSAFE" : "—"));
}

let soundEnabled = localStorage.getItem("periconet_sound") !== "off";
const lostStarTimers = {};

function updateSoundButton() {
  const btn = document.getElementById("sound-toggle");
  if (!btn) return;
  btn.textContent = soundEnabled ? "🔔" : "🔕";
  btn.style.opacity = soundEnabled ? "1" : "0.5";
}

function playAlertBeep() {
  if (!soundEnabled) return;
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    const ctx = audioContext;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const beep = (freq, start, dur) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.2, ctx.currentTime + start);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + dur);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + start);
      osc.stop(ctx.currentTime + start + dur);
    };
    beep(880, 0, 0.18);
    beep(660, 0.2, 0.22);
  } catch (e) {
    console.warn("No se pudo reproducir el aviso sonoro:", e);
  }
}

function handleGuidingStatusSound(setupId, status) {
  const isLost = status === "estrella perdida";
  if (isLost && !lostStarTimers[setupId]) {
    playAlertBeep();
    lostStarTimers[setupId] = setInterval(playAlertBeep, 2500);
  } else if (!isLost && lostStarTimers[setupId]) {
    clearInterval(lostStarTimers[setupId]);
    delete lostStarTimers[setupId];
  }
}

function updateBadge(setupId, status) {
  const badge = document.getElementById(`guiding-badge-${setupId}`);
  if (!badge) return;
  badge.textContent = status;
  badge.className = "badge " + status.replace(/\s+/g, "").toLowerCase();
  handleGuidingStatusSound(setupId, status);
}

function updateConnectionDot(setupId, connected) {
  const dot = document.getElementById(`dot-${setupId}`);
  if (!dot) return;
  dot.className = "dot " + (connected ? "connected" : "disconnected");
}

function updateImage(setupId, url, imageId) {
  const cell = document.getElementById(`image-cell-${setupId}`);
  if (!cell) return;
  const old = IMAGE_STATES[setupId];
  IMAGE_STATES[setupId] = {url, id: imageId};
  const key = imageId || "";
  if (!key || !url) {
    cell.dataset.requestedKey = "";
    cell.dataset.loadedKey = "";
    cell.replaceChildren(el("span", "placeholder", "Esperando una captura del objetivo actual…"));
    if (currentModalSetupId === setupId) closeImageModal();
    return;
  }
  if (cell.dataset.loadedKey === key || cell.dataset.requestedKey === key) return;
  cell.dataset.requestedKey = key;
  const probe = new Image();
  probe.alt = "Última captura del objetivo actual";
  probe.onload = () => {
    if (IMAGE_STATES[setupId]?.id !== key || cell.dataset.requestedKey !== key) return;
    cell.replaceChildren(probe, el("span", "image-zoom-badge", "🔍 Zoom HD"));
    cell.dataset.loadedKey = key;
    cell.dataset.requestedKey = "";
  };
  probe.onerror = () => {
    if (cell.dataset.requestedKey === key) cell.dataset.requestedKey = "";
  };
  probe.src = url;
  if (currentModalSetupId === setupId && old?.id !== key) openImageModal(setupId, true);
}

const EXPOSURE_TRACKING = {};

function updateExposureState(setupId, exp) {
  if (!exp) return;
  let endTimeMs = null;
  if (exp.end_time) {
    const t = new Date(exp.end_time).getTime();
    if (!isNaN(t)) endTimeMs = t;
  }
  EXPOSURE_TRACKING[setupId] = {
    isExposing: !!exp.is_exposing,
    cameraState: exp.camera_state || "Desconectado",
    endTimeMs: endTimeMs,
    duration: exp.duration || 0,
  };
  renderExposureTick(setupId);
}

function renderExposureTick(setupId) {
  const exp = EXPOSURE_TRACKING[setupId];
  if (!exp) return;

  const statusEl = document.getElementById(`exposure-status-${setupId}`);
  const timerEl = document.getElementById(`exposure-timer-${setupId}`);
  const barEl = document.getElementById(`exposure-bar-${setupId}`);
  if (!statusEl || !timerEl || !barEl) return;

  const now = Date.now();

  if (["Downloading", "Download", "Reading"].includes(exp.cameraState)) {
    statusEl.textContent = "Descargando imagen…";
    statusEl.className = "exposure-status downloading";
    timerEl.textContent = "Lectura del sensor";
    barEl.style.width = "100%";
    barEl.className = "exposure-bar-fill downloading";
  } else if (exp.isExposing && exp.endTimeMs && exp.duration > 0) {
    const remainingSec = Math.max(0, (exp.endTimeMs - now) / 1000);
    const elapsedSec = Math.max(0, exp.duration - remainingSec);
    const pct = Math.min(100, Math.max(0, (elapsedSec / exp.duration) * 100));

    statusEl.textContent = "Exponiendo";
    statusEl.className = "exposure-status exposing";
    timerEl.textContent = `${Math.floor(elapsedSec)}s / ${Math.floor(exp.duration)}s (${Math.round(pct)}%)`;
    barEl.style.width = `${pct.toFixed(1)}%`;
    barEl.className = "exposure-bar-fill";
  } else if (exp.isExposing && exp.endTimeMs) {
    const remainingSec = Math.max(0, (exp.endTimeMs - now) / 1000);
    statusEl.textContent = "Exponiendo";
    statusEl.className = "exposure-status exposing";
    timerEl.textContent = `${Math.ceil(remainingSec)}s restantes`;
    barEl.style.width = "0%";
    barEl.className = "exposure-bar-fill";
  } else {
    statusEl.textContent = exp.cameraState === "Idle" ? "En reposo" : (exp.cameraState || "En espera");
    statusEl.className = "exposure-status";
    timerEl.textContent = "—";
    barEl.style.width = "0%";
    barEl.className = "exposure-bar-fill";
  }
}

// Bucle cliente optimizado: se ejecuta cada 200ms en el navegador
// Proporciona animación continua sin generar peticiones a la red ni sobrecargar NINA/servidor
setInterval(() => {
  for (const [setupId, exp] of Object.entries(EXPOSURE_TRACKING)) {
    if (exp && exp.isExposing) {
      renderExposureTick(setupId);
    }
  }
}, 200);

// Manejo del Objetivo actual y Gráfica de Altura (Target Scheduler)
const LAST_TARGET_DATA = {};
const SIDEREAL_DEG_PER_DAY = 360.98564736629;

function localSiderealDeg(timeMs, lonDeg) {
  const daysSinceJ2000 = timeMs / 86400000 - 10957.5;
  return 280.46061837 + SIDEREAL_DEG_PER_DAY * daysSinceJ2000 + lonDeg;
}

function getAltitudeDeg(raHours, decDeg, latDeg, lonDeg, dateObj) {
  const ha = (localSiderealDeg(dateObj.getTime(), lonDeg) - raHours * 15) % 360;

  const latRad = latDeg * (Math.PI / 180.0);
  const decRad = decDeg * (Math.PI / 180.0);
  const haRad = ha * (Math.PI / 180.0);

  const sinAlt = Math.sin(latRad) * Math.sin(decRad) + Math.cos(latRad) * Math.cos(decRad) * Math.cos(haRad);
  return Math.asin(Math.max(-1.0, Math.min(1.0, sinAlt))) * (180.0 / Math.PI);
}

function culminationTime(startMs, endMs, raHours, lonDeg) {
  // En el tránsito superior la hora sidérea local coincide con la RA.
  const middle = (startMs + endMs) / 2;
  const angle = (localSiderealDeg(middle, lonDeg) - raHours * 15) % 360;
  const nearestTransit = angle - 360 * Math.round(angle / 360);
  const time = middle - nearestTransit * 86400000 / SIDEREAL_DEG_PER_DAY;
  return time >= startMs && time <= endMs ? time : null;
}
function renderTargetAltCanvas(setupId, target) {
  const canvas = document.getElementById(`target-alt-canvas-${setupId}`);
  if (!canvas) return;

  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = rect.width || canvas.clientWidth || 300;
  const h = 85;

  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }

  const ctx = canvas.getContext("2d");
  ctx.save();
  ctx.scale(dpr, dpr);

  // Fondo cielo nocturno
  ctx.fillStyle = "#08090d";
  ctx.fillRect(0, 0, w, h);

  const now = new Date();
  const night = STATES[setupId]?.night;
  const site = STATES[setupId]?.site;
  if (!night || !site) {
    ctx.fillStyle = "#71717a";
    ctx.font = "11px monospace";
    ctx.fillText("Ubicación de NINA no disponible", 8, 40);
    ctx.restore();
    return;
  }
  const start = new Date(night.start), end = new Date(night.end);
  const totalMs = end - start;
  const colors = {day:"#343b4e", civil:"#242a40", nautical:"#171b30", astronomical:"#101321", night:"#08090d"};
  const xTime = time => (new Date(time) - start) / totalMs * w;
  let phase = night.initial_phase, left = 0;
  for (const event of night.events) {
    const right = xTime(event.time);
    ctx.fillStyle = colors[phase];
    ctx.fillRect(left, 0, right - left, h);
    left = right;
    phase = event.phase_after;
  }
  ctx.fillStyle = colors[phase];
  ctx.fillRect(left, 0, w - left, h);
  canvas.title = "Horas del navegador · " + night.events.map(e =>
    `${e.direction === "setting" ? "Anochecer" : "Amanecer"} ${e.altitude}°: ${new Date(e.time).toLocaleTimeString("es-ES", {hour:"2-digit", minute:"2-digit"})}`).join(" · ");

  // Dimensiones y márgenes
  const padBottom = 16;
  const padTop = 10;
  const chartH = h - padBottom - padTop;
  const altToY = (alt) => padTop + chartH * (1.0 - (alt - (-5)) / 95.0);

  // Línea de horizonte (0°)
  const yHorizon = altToY(0);
  ctx.strokeStyle = "#27272a";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, yHorizon);
  ctx.lineTo(w, yHorizon);
  ctx.stroke();

  // Línea de referencia de altitud 30°
  const y30 = altToY(30);
  ctx.strokeStyle = "rgba(113, 113, 122, 0.25)";
  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(0, y30);
  ctx.lineTo(w, y30);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = "#52525b";
  ctx.font = "8px monospace";
  ctx.textAlign = "right";
  ctx.fillText("30°", w - 4, y30 - 2);

  const ra = target.ra;
  const dec = target.dec;
  const lat = site.latitude;
  const lon = site.longitude;

  if (Number.isFinite(ra) && Number.isFinite(dec)) {
    const points = [];

    const steps = 72;
    for (let i = 0; i <= steps; i++) {
      const frac = i / steps;
      const tMs = start.getTime() + frac * totalMs;
      const tDate = new Date(tMs);
      const alt = getAltitudeDeg(ra, dec, lat, lon, tDate);
      const px = frac * w;
      const py = altToY(alt);
      points.push({ x: px, y: py });
    }

    const transitMs = culminationTime(start.getTime(), end.getTime(), ra, lon);
    const maxAlt = 90 - Math.abs(lat - dec);
    const hasCulmination = transitMs !== null && maxAlt > 0;
    const maxX = hasCulmination ? xTime(transitMs) : 0;
    const maxY = altToY(maxAlt);
    if (hasCulmination) {
      points.splice(Math.ceil(maxX / w * steps), 0, { x: maxX, y: maxY });
    }

    // Relleno bajo la curva
    ctx.beginPath();
    ctx.moveTo(points[0].x, yHorizon);
    for (const pt of points) {
      ctx.lineTo(pt.x, Math.min(yHorizon, pt.y));
    }
    ctx.lineTo(points[points.length - 1].x, yHorizon);
    ctx.closePath();
    const fillGrad = ctx.createLinearGradient(0, padTop, 0, yHorizon);
    fillGrad.addColorStop(0, "rgba(244, 63, 94, 0.25)");
    fillGrad.addColorStop(1, "rgba(244, 63, 94, 0.0)");
    ctx.fillStyle = fillGrad;
    ctx.fill();

    // Trazo de la curva de altura (color idéntico a NINA Target Scheduler)
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, padTop, w, chartH);
    ctx.clip();
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(points[i].x, points[i].y);
    }
    ctx.strokeStyle = "#f43f5e";
    ctx.lineWidth = 1.8;
    ctx.stroke();
    ctx.restore();

    // Punto de culminación / Tránsito
    if (hasCulmination) {
      ctx.beginPath();
      ctx.arc(maxX, maxY, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = "#f43f5e";
      ctx.fill();

      ctx.fillStyle = "#fb7185";
      ctx.font = "9px monospace";
      ctx.textAlign = "center";
      const time = new Date(transitMs).toLocaleTimeString("es-ES", {hour:"2-digit", minute:"2-digit"});
      const label = `Culm. ${time} · ${maxAlt.toFixed(1)}°`;
      const halfWidth = ctx.measureText(label).width / 2 + 3;
      const labelX = Math.max(halfWidth, Math.min(w - halfWidth, maxX));
      ctx.fillText(label, labelX, maxY < 23 ? maxY + 13 : maxY - 5);
      canvas.title += ` · ${label}`;
    }
  }

  // Indicador de hora actual ("Ahora")
  const nowMs = now.getTime();
  if (nowMs >= start.getTime() && nowMs <= end.getTime()) {
    const fracNow = (nowMs - start.getTime()) / totalMs;
    const xNow = fracNow * w;

    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(xNow, padTop);
    ctx.lineTo(xNow, h - padBottom);
    ctx.stroke();
    ctx.setLineDash([]);

    let currentAlt = target.altitude;
    if (currentAlt == null && ra != null && dec != null) {
      currentAlt = getAltitudeDeg(ra, dec, lat, lon, now);
    }

    if (currentAlt != null) {
      const yNow = altToY(currentAlt);
      ctx.beginPath();
      ctx.arc(xNow, yNow, 5, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(56, 189, 248, 0.35)";
      ctx.fill();

      ctx.beginPath();
      ctx.arc(xNow, yNow, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = "#38bdf8";
      ctx.fill();
    }

    ctx.fillStyle = "#38bdf8";
    ctx.font = "bold 8px monospace";
    ctx.textAlign = xNow > w - 30 ? "right" : (xNow < 30 ? "left" : "center");
    ctx.fillText("Ahora", xNow, padTop - 2);
  }

  // Marcas de horas en el eje inferior
  ctx.fillStyle = "#71717a";
  ctx.font = "8px monospace";
  ctx.textAlign = "center";
  const hours = [12, 15, 18, 21, 0, 3, 6, 9, 12];
  for (let i = 0; i < hours.length; i++) {
    const xHour = (i / (hours.length - 1)) * w;
    const hrStr = new Date(start.getTime() + totalMs * i / (hours.length - 1)).toLocaleTimeString("es-ES", {hour:"2-digit", minute:"2-digit"});
    ctx.textAlign = i === 0 ? "left" : i === hours.length - 1 ? "right" : "center";
    ctx.fillText(hrStr, xHour, h - 3);

    ctx.strokeStyle = "#27272a";
    ctx.beginPath();
    ctx.moveTo(xHour, h - padBottom);
    ctx.lineTo(xHour, h - padBottom + 3);
    ctx.stroke();
  }

  ctx.restore();
}

function renderTargetInfo(setupId, target) {
  if (!target) return;
  const site = STATES[setupId]?.site;
  if (site && target.ra != null && target.dec != null) {
    const now = new Date();
    target = {...target, altitude: getAltitudeDeg(target.ra, target.dec, site.latitude, site.longitude, now)};
    const ha = ((localSiderealDeg(now.getTime(), site.longitude) - target.ra * 15) % 360) * Math.PI / 180;
    const lat = site.latitude * Math.PI / 180, dec = target.dec * Math.PI / 180;
    target.azimuth = (Math.atan2(Math.sin(ha), Math.cos(ha) * Math.sin(lat) - Math.tan(dec) * Math.cos(lat)) * 180 / Math.PI + 180) % 360;
  }
  LAST_TARGET_DATA[setupId] = target;

  const nameEl = document.getElementById(`target-name-${setupId}`);
  const raEl = document.getElementById(`target-ra-${setupId}`);
  const decEl = document.getElementById(`target-dec-${setupId}`);
  const altEl = document.getElementById(`target-alt-${setupId}`);
  const azEl = document.getElementById(`target-az-${setupId}`);
  const filtersEl = document.getElementById(`target-filters-${setupId}`);

  if (nameEl) nameEl.textContent = target.name || "—";
  if (raEl) raEl.textContent = target.ra_string || "—";
  if (decEl) decEl.textContent = target.dec_string || "—";
  if (altEl) altEl.textContent = target.altitude != null ? `${Number(target.altitude).toFixed(1)}°` : "—";
  if (azEl) azEl.textContent = target.azimuth != null ? `${Number(target.azimuth).toFixed(1)}°` : "—";

  if (filtersEl && target.filter_summary) {
    const pills = Object.entries(target.filter_summary).map(([filter, info]) => {
      const mins = Math.round(info.total_seconds / 60);
      const timeStr = mins > 0 ? `(${mins}m)` : '';
      return `<span class="filter-pill"><span class="f-name">${escapeHtml(filter)}</span> <span class="f-count">${info.count}</span> <span class="f-time">${timeStr}</span></span>`;
    });
    filtersEl.innerHTML = pills.length ? `<span style="color:var(--text-dim);font-size:0.68rem;text-transform:uppercase;letter-spacing:0.05em;margin-right:2px;">Tomas objeto:</span> ${pills.join("")}` : "";
  }

  renderTargetAltCanvas(setupId, target);
}

// Redibujar automáticamente las gráficas de altitud al redimensionar la ventana o rotar el móvil
window.addEventListener("resize", () => {
  for (const [setupId, target] of Object.entries(LAST_TARGET_DATA)) {
    renderTargetAltCanvas(setupId, target);
  }
});

function applyFullState(setupId, update) {
  const previous = STATES[setupId];
  const state = {...previous, ...update};
  STATES[setupId] = state;
  if (previous?.target?.key !== state.target?.key) updateImage(setupId, null, null);
  updateConnectionDot(setupId, state.connected);
  const endButton = document.getElementById(`sequence-end-${setupId}`);
  if (endButton) updateSequenceEndButton(setupId, endButton);
  updateBadge(setupId, state.guiding_status || "sin datos");
  renderTargetInfo(setupId, state.target || {});
  updateImage(setupId, state.latest_image_url, state.latest_image_id);
  changed(setupId, "exposure", state.exposure, () => updateExposureState(setupId, state.exposure));
  changed(setupId, "hfr", state.hfr_history, () => renderHfrList(setupId, state.hfr_history));
  if (update.guide_steps) renderGuiding(setupId);
  changed(setupId, "weather", [state.weather, state.safety], () => renderWeather(setupId, state.weather, state.safety));
  renderMeridian(setupId, state.mount);
}

// Gestión del Visor Lightbox con Zoom & Pan de Alta Resolución
let currentModalSetupId = null;
let modalScale = 1.0;
let modalTranslateX = 0;
let modalTranslateY = 0;
let isPanning = false;
let startPointerX = 0;
let startPointerY = 0;

function applyModalTransform() {
  const img = document.getElementById("modal-img");
  if (!img) return;
  img.style.transform = `translate(${modalTranslateX}px, ${modalTranslateY}px) scale(${modalScale})`;
}

function resetModalZoom() {
  modalScale = 1.0;
  modalTranslateX = 0;
  modalTranslateY = 0;
  applyModalTransform();
}

function openImageModal(setupId, preserveZoom = false) {
  const capture = IMAGE_STATES[setupId];
  if (!capture?.id) return;
  const token = capture.id;
  currentModalSetupId = setupId;
  const modal = document.getElementById("image-modal");
  const modalImg = document.getElementById("modal-img");
  const modalTitle = document.getElementById("modal-title");
  const modalSub = document.getElementById("modal-sub");
  const modalLoader = document.getElementById("modal-loader");

  const targetData = LAST_TARGET_DATA[setupId];
  const targetName = targetData ? targetData.name : "";

  modalTitle.textContent = `${targetName ? targetName + ' — ' : ''}Inspección de alta resolución`;
  modalSub.textContent = "Cargando imagen completa (Full Res comprimida)…";

  if (!preserveZoom) resetModalZoom();

  // Mostramos primero el thumbnail actual para dar feedback instantáneo
  if (capture.url) {
    modalImg.src = capture.url;
  } else {
    modalImg.src = "";
  }

  modalLoader.style.display = "flex";
  modal.style.display = "flex";

  // Carga bajo demanda de la versión HD
  const hiresUrl = `${API_BASE}/api/image/${setupId}/hires?v=${encodeURIComponent(token)}`;
  const hiresProbe = new Image();
  hiresProbe.onload = () => {
    if (currentModalSetupId === setupId && IMAGE_STATES[setupId]?.id === token) {
      modalImg.src = hiresUrl;
      modalSub.textContent = `${hiresProbe.naturalWidth}×${hiresProbe.naturalHeight} px • Detalle completo`;
      modalLoader.style.display = "none";
    }
  };
  hiresProbe.onerror = () => {
    if (currentModalSetupId === setupId && IMAGE_STATES[setupId]?.id === token) {
      modalSub.textContent = "Resolución estándar";
      modalLoader.style.display = "none";
    }
  };
  hiresProbe.src = hiresUrl;
}

function closeImageModal() {
  const modal = document.getElementById("image-modal");
  modal.style.display = "none";
  currentModalSetupId = null;
  document.getElementById("modal-img").removeAttribute("src");
}

function initModalEvents() {
  const viewport = document.getElementById("modal-viewport");
  const btnClose = document.getElementById("btn-modal-close");
  const btnIn = document.getElementById("btn-zoom-in");
  const btnOut = document.getElementById("btn-zoom-out");
  const btnFit = document.getElementById("btn-zoom-fit");

  btnClose.addEventListener("click", closeImageModal);

  btnIn.addEventListener("click", () => {
    modalScale = Math.min(7.0, modalScale * 1.35);
    applyModalTransform();
  });

  btnOut.addEventListener("click", () => {
    modalScale = Math.max(0.4, modalScale / 1.35);
    applyModalTransform();
  });

  btnFit.addEventListener("click", resetModalZoom);

  // Cerrar con Escape
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeImageModal();
  });

  // Zoom con rueda de ratón centrado
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.2 : 0.82;
    modalScale = Math.min(7.0, Math.max(0.4, modalScale * factor));
    applyModalTransform();
  }, { passive: false });

  // Doble click para alternar zoom rápido
  viewport.addEventListener("dblclick", () => {
    if (modalScale > 1.2) {
      resetModalZoom();
    } else {
      modalScale = 2.5;
      applyModalTransform();
    }
  });

  // Arrastre con ratón
  viewport.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    isPanning = true;
    startPointerX = e.clientX - modalTranslateX;
    startPointerY = e.clientY - modalTranslateY;
  });

  window.addEventListener("mousemove", (e) => {
    if (!isPanning) return;
    modalTranslateX = e.clientX - startPointerX;
    modalTranslateY = e.clientY - startPointerY;
    applyModalTransform();
  });

  window.addEventListener("mouseup", () => {
    isPanning = false;
  });

  // Gestos táctiles en móvil (arrastre y pellizco / pinch-to-zoom)
  let initialPinchDistance = 0;
  let initialPinchScale = 1.0;

  viewport.addEventListener("touchstart", (e) => {
    if (e.touches.length === 1) {
      isPanning = true;
      startPointerX = e.touches[0].clientX - modalTranslateX;
      startPointerY = e.touches[0].clientY - modalTranslateY;
    } else if (e.touches.length === 2) {
      isPanning = false;
      initialPinchDistance = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      initialPinchScale = modalScale;
    }
  }, { passive: false });

  viewport.addEventListener("touchmove", (e) => {
    e.preventDefault();
    if (e.touches.length === 1 && isPanning) {
      modalTranslateX = e.touches[0].clientX - startPointerX;
      modalTranslateY = e.touches[0].clientY - startPointerY;
      applyModalTransform();
    } else if (e.touches.length === 2 && initialPinchDistance > 0) {
      const currentDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      const ratio = currentDist / initialPinchDistance;
      modalScale = Math.min(7.0, Math.max(0.4, initialPinchScale * ratio));
      applyModalTransform();
    }
  }, { passive: false });

  viewport.addEventListener("touchend", (e) => {
    if (e.touches.length < 2) {
      initialPinchDistance = 0;
    }
    if (e.touches.length === 0) {
      isPanning = false;
    }
  });
}

async function init() {
  initModalEvents();
  updateSoundButton();
  document.getElementById("sound-toggle").addEventListener("click", () => {
    soundEnabled = !soundEnabled;
    localStorage.setItem("periconet_sound", soundEnabled ? "on" : "off");
    updateSoundButton();
    if (soundEnabled) playAlertBeep();
  });

  const [config, setups] = await Promise.all([
    fetch(`${API_BASE}/api/config`).then(r => r.json()),
    fetch(`${API_BASE}/api/setups`).then(r => r.json()),
  ]);
  configRevision = config.revision;
  SETUPS = setups;
  SETUP_AXIS_RANGE = Object.fromEntries(setups.map(s => [s.id, s.guide_axis_range || GUIDE_AXIS_RANGE_DEFAULT]));
  SETUP_AXIS_UNIT = Object.fromEntries(setups.map(s => [s.id, s.guide_axis_unit || "arcsec"]));

  if (config.observatory_name) {
    document.getElementById("observatory-title").textContent = config.observatory_name;
    document.getElementById("brand-tag").style.display = "block";
  }

  const container = document.getElementById("panels");
  SETUPS.forEach(s => container.appendChild(buildPanel(s)));

  const extras = document.getElementById("extras");
  (config.extra_images || []).forEach((img, i) => {
    if (img.url) extras.appendChild(buildExtraCard(img, i));
  });

  connectWebSocket();
  startWatchdog();

  setInterval(() => {
    document.getElementById("clock").textContent = new Date().toLocaleTimeString("es-ES");
  }, 1000);
}

function buildExtraCard(img, index) {
  const card = el("div", "extra-card");
  const header = el("div", "panel-header");
  header.appendChild(el("h2", null, img.title || `Imagen ${index + 1}`));
  const upd = el("span", "upd", "");
  header.appendChild(upd);
  const body = el("div", "extra-body");
  body.appendChild(el("span", "placeholder", "cargando…"));
  card.appendChild(header);
  card.appendChild(body);

  const refresh = () => {
    const probe = new Image();
    const targetSrc = `${API_BASE}/api/extra-image/${index}?t=${Date.now()}`;
    probe.onload = () => {
      probe.alt = img.title || "";
      body.replaceChildren(probe);
      upd.textContent = new Date().toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
    };
    probe.onerror = () => {
      // Si ya había una imagen la dejamos (mejor una vieja que nada)
      if (!body.querySelector("img")) {
        body.innerHTML = "";
        body.appendChild(el("span", "placeholder", "imagen no disponible"));
      }
      upd.textContent = "sin conexión";
    };
    probe.src = targetSrc;
  };
  refresh();
  setInterval(refresh, Math.max(10, img.refresh_seconds || 60) * 1000);
  return card;
}

let ws = null;

function markDisconnected() {
  for (const setup of SETUPS) {
    const id = setup.id;
    applyFullState(id, {connected:false, sequence_running:false, camera:null, mount:null, weather:null, safety:null,
      target:{name:"Sin conexión", key:null}, latest_image_id:null, latest_image_url:null,
      exposure:{camera_state:"Desconectado", is_exposing:false}, guiding_status:"desconectado"});
  }
}
function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  lastMessageTime = Date.now();
  ws.onmessage = (msg) => {
    lastMessageTime = Date.now();
    const data = JSON.parse(msg.data);
    if ((data.type === "config_changed" || data.type === "initial_state") && data.revision !== configRevision) {
      location.reload(); return;
    }
    if (data.type === "initial_state") {
      Object.entries(data.state).forEach(([id, state]) => { delete STATES[id]; applyFullState(id, state); });
    } else if (data.type === "nina_update") {
      applyFullState(data.setup_id, data.state);
    } else if (data.type === "guide_step") {
      const state = STATES[data.setup_id];
      if (!state || data.point.seq <= (state.guide_seq || 0)) return;
      state.guide_seq = data.point.seq;
      state.pixel_scale = data.pixel_scale;
      state.guide_steps = [...(state.guide_steps || []), data.point].slice(-300);
      state.guiding_status = "guiando";
      updateBadge(data.setup_id, "guiando");
      renderGuiding(data.setup_id);
    } else if (data.type === "guiding_status") {
      const state = STATES[data.setup_id];
      if (!state) return;
      state.guiding_status = data.status;
      state.pixel_scale = data.pixel_scale;
      if (data.reset) state.guide_steps = [];
      updateBadge(data.setup_id, data.status);
      renderGuiding(data.setup_id);
    }
  };
  ws.onclose = () => { markDisconnected(); setTimeout(connectWebSocket, 3000); };
}

function startWatchdog() {
  setInterval(() => {
    const staleMs = Date.now() - lastMessageTime;
    if (staleMs > 15000 && ws) {
      console.warn("WebSocket sin datos hace", staleMs, "ms — forzando reconexión");
      ws.close();
      lastMessageTime = Date.now();
    }
  }, 10000);
}

init().catch(error => {
  document.getElementById("panels").textContent = "No se pudo conectar con Periconet. Reintentando…";
  setTimeout(() => location.reload(), 5000);
});
