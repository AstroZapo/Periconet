let count = 0;
let extraImages = [];
function addSetup(values = {}) {
  const card = document.createElement("div");
  card.className = "setup-card";
  card.id = `setup-card-${++count}`;
  card.dataset.setupId = values.id || "";
  card.innerHTML = `
    <button class="remove" type="button">✕</button>
    <label>Nombre del setup</label><input class="f-name" required maxlength="100" placeholder="Telescopio principal">
    <div class="row"><div><label>IP o nombre del equipo NINA</label><input class="f-nina-host" required placeholder="192.168.10.30"></div>
    <div><label>Puerto NINA</label><input class="f-nina-port" type="number" required min="1" max="65535" step="1"></div></div>
    <div class="row"><div><label>IP o nombre del equipo PHD2 (opcional)</label><input class="f-phd2-host" placeholder="192.168.10.30"></div>
    <div><label>Puerto PHD2</label><input class="f-phd2-port" type="number" required min="1" max="65535" step="1"></div></div>
    <label>Unidad del gráfico de guiado</label><select class="f-guide-unit"><option value="arcsec">Arcosegundos (″)</option><option value="px">Píxeles</option></select>
    <label>Rango del eje del gráfico de guiado (±)</label><input class="f-guide-range" type="number" required min="0.01" max="1000" step="any">
    <div style="font-size:0.72rem;color:var(--text-dim);margin-top:4px;">Usa la misma unidad y rango que en PHD2. Si no hay escala de píxel disponible, se mostrarán píxeles.</div>`;
  const fields = {"f-name": values.name || "", "f-nina-host": values.nina_host || "",
    "f-nina-port": values.nina_port ?? 1888, "f-phd2-host": values.phd2_host || "",
    "f-phd2-port": values.phd2_port ?? 4400, "f-guide-unit": values.guide_axis_unit || "arcsec",
    "f-guide-range": values.guide_axis_range ?? 3};
  Object.entries(fields).forEach(([field, value]) => card.querySelector(`.${field}`).value = value);
  card.querySelector(".remove").onclick = () => card.remove();
  document.getElementById("setups").appendChild(card);
}
function showStatus(message, error = false) {
  const status = document.getElementById("status");
  status.style.display = "block";
  status.style.color = error ? "#d64545" : "var(--text)";
  status.textContent = message;
}
async function save() {
  for (const input of document.querySelectorAll(".wrap > input, #setups input, #extra-0 input, #extra-1 input")) {
    if (!input.reportValidity()) return;
  }
  if (cameraSettingsDirty) {
    for (const input of cameraEntries.querySelectorAll("input")) {
      if (!input.reportValidity()) return;
    }
  }
  const cards = [...document.querySelectorAll("#setups .setup-card")];
  const setups = cards.map(card => {
    const value = cls => card.querySelector(`.${cls}`).value.trim();
    return {id: card.dataset.setupId || null, name: value("f-name"), nina_host: value("f-nina-host"),
      nina_port: Number(value("f-nina-port")), phd2_host: value("f-phd2-host"), phd2_port: Number(value("f-phd2-port")),
      guide_axis_range: Number(value("f-guide-range")), guide_axis_unit: value("f-guide-unit")};
  });
  const names = setups.map(s => s.name.toLocaleLowerCase());
  if (new Set(names).size !== names.length) { showStatus("Cada equipo debe tener un nombre diferente.", true); return; }
  const extras = [0, 1].map(i => {
    const card = document.getElementById(`extra-${i}`);
    return {title:card.querySelector(".x-title").value.trim(), url:card.querySelector(".x-url").value.trim(),
      refresh_seconds:Number(card.querySelector(".x-refresh").value || (i === 0 ? 300 : 60))};
  });
  saveButton.disabled = true;
  cameraFields.disabled = true;
  try {
    const settings = {
      observatory_name: document.getElementById("observatory-name").value.trim(),
      setups,
      port: Number(document.getElementById("port").value || 8000),
      log_level: document.getElementById("log-level").value,
      extra_images: [...extras, ...extraImages.slice(2)]
    };
    const headers = {"Content-Type": "application/json"};
    if (cameraSettingsDirty) {
      settings.camera_settings = collectCameraSettings();
      Object.assign(headers, cameraSettingsHeaders);
    }
    const response = await fetch("/api/setup", {method: "POST", headers, body: JSON.stringify(settings)});
    const result = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(result.detail) ? result.detail.map(e => e.msg).join(". ") : result.detail;
      throw new Error(detail || "No se pudo guardar la configuración.");
    }
    result.setups.forEach((setup, i) => cards[i].dataset.setupId = setup.id);
    extraImages = settings.extra_images;
    if (result.camera_settings) displayCameraSettings(result.camera_settings);
    showStatus(result.restart_required
      ? `Configuración guardada. Reinicia Periconet para usar el puerto ${result.port}.`
      : "Configuración aplicada. El panel se actualiza automáticamente.");
  } catch (error) {
    showStatus(error.message || "No se pudo conectar con Periconet.", true);
  } finally {
    saveButton.disabled = false;
    cameraFields.disabled = false;
  }
}
const saveButton = document.getElementById("save-settings");
saveButton.disabled = true;
fetch("/api/config").then(r => { if (!r.ok) throw new Error(); return r.json(); }).then(data => {
  document.getElementById("observatory-name").value = data.observatory_name || "";
  document.getElementById("port").value = data.port || 8000;
  document.getElementById("log-level").value = data.log_level || "INFO";
  extraImages = data.extra_images || [];
  extraImages.slice(0, 2).forEach((item, i) => {
    const card = document.getElementById(`extra-${i}`);
    card.querySelector(".x-title").value = item.title || "";
    card.querySelector(".x-url").value = item.url || "";
    card.querySelector(".x-refresh").value = item.refresh_seconds || 60;
  });
  if (data.setups?.length) data.setups.forEach(addSetup); else addSetup();
  saveButton.disabled = false;
}).catch(() => showStatus("No se pudo leer la configuración. Recarga la página para reintentarlo.", true));

const cameraSettingsUrl = "/api/setup/cameras";
const cameraSettingsHeaders = {"X-Periconet-Action": "camera-setup"};
const cameraEntries = document.getElementById("camera-entries");
const cameraFields = document.getElementById("camera-fields");
const cameraTemplate = document.getElementById("camera-template");
const addCameraButton = document.getElementById("add-camera");
let cameraSettingsRevision = "";
let cameraSettingsDirty = false;

function showCameraSettingsStatus(message, error = false) {
  const status = document.getElementById("camera-status");
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle("error", error);
}

function addCamera(values = {}) {
  const card = cameraTemplate.content.firstElementChild.cloneNode(true);
  card.dataset.cameraId = values.id || "";
  const name = card.querySelector(".camera-name");
  const url = card.querySelector(".camera-url");
  const urlPlaceholder = url.placeholder;
  const protectedUrl = card.querySelector(".camera-protected-url");
  const credentialFields = card.querySelector(".camera-options");
  const username = card.querySelector(".camera-user");
  const password = card.querySelector(".camera-password");
  const credentialStatus = card.querySelector(".camera-credential-status");
  const removalLabel = card.querySelector(".camera-credential-remove");
  const removeCredentials = removalLabel.querySelector("input");
  const savedSeparateCredentials = !values.protected_url && (values.has_credentials || values.credentials_missing);
  name.value = values.name || "";
  url.value = values.url || "";
  protectedUrl.checked = Boolean(values.protected_url);
  card.querySelector(".camera-interval").value = values.refresh_seconds ?? 10;

  function updateCameraAccess() {
    const isProtected = protectedUrl.checked;
    if (!isProtected && url.type === "password") url.value = values.url || "";
    url.type = isProtected ? "password" : "url";
    url.autocomplete = isProtected ? "new-password" : "off";
    url.required = !isProtected || !values.protected_url;
    url.placeholder = isProtected ? "URL de captura completa" : urlPlaceholder;
    credentialFields.hidden = isProtected;
    removalLabel.hidden = isProtected || !savedSeparateCredentials;
    username.disabled = password.disabled = isProtected || removeCredentials.checked;

    let message = "";
    if (isProtected && values.protected_url) {
      message = values.credentials_missing
        ? "La URL protegida no está disponible en esta cuenta. Vuelve a introducirla."
        : "URL protegida guardada en Windows. Deja el campo vacío para conservarla.";
    } else if (!isProtected && savedSeparateCredentials) {
      message = values.credentials_missing
        ? "Las credenciales no están disponibles en esta cuenta. Vuelve a introducirlas."
        : "Credenciales guardadas en Windows. Deja los campos vacíos para conservarlas.";
    }
    credentialStatus.textContent = message;
    credentialStatus.hidden = !message;
  }

  protectedUrl.addEventListener("change", updateCameraAccess);
  removeCredentials.addEventListener("change", updateCameraAccess);
  updateCameraAccess();
  card.querySelector("button.remove").addEventListener("click", () => {
    card.remove();
    cameraSettingsDirty = true;
  });
  cameraEntries.appendChild(card);
  return name;
}

function displayCameraSettings(settings) {
  cameraSettingsRevision = settings.revision;
  cameraEntries.replaceChildren();
  settings.cameras.forEach(addCamera);
  cameraSettingsDirty = false;
}

function collectCameraSettings() {
  const cameras = [...cameraEntries.children].map(card => {
    const protectedUrl = card.querySelector(".camera-protected-url").checked;
    const removeCredentials = !protectedUrl && card.querySelector(".camera-credential-remove input").checked;
    const useSeparateCredentials = !protectedUrl && !removeCredentials;
    return {
      id: card.dataset.cameraId || null,
      name: card.querySelector(".camera-name").value.trim(),
      url: card.querySelector(".camera-url").value.trim(),
      refresh_seconds: Number(card.querySelector(".camera-interval").value),
      username: useSeparateCredentials ? card.querySelector(".camera-user").value.trim() : "",
      password: useSeparateCredentials ? card.querySelector(".camera-password").value : "",
      protected_url: protectedUrl,
      remove_credentials: removeCredentials
    };
  });
  return {revision: cameraSettingsRevision, cameras};
}

async function loadCameraSettings() {
  const loadError = "No se pudo cargar la configuración de cámaras. Recarga la página para reintentar.";
  if (!["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
    showCameraSettingsStatus("Abre esta página desde localhost en el PC de Periconet para configurar las cámaras.");
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(cameraSettingsUrl, {
      headers: cameraSettingsHeaders, cache: "no-store", signal: controller.signal
    });
    const result = await response.json();
    if (!response.ok || !Array.isArray(result?.cameras) || typeof result.revision !== "string") {
      showCameraSettingsStatus(typeof result?.detail === "string" ? result.detail : loadError, true);
      return;
    }
    displayCameraSettings(result);
    addCameraButton.disabled = false;
    showCameraSettingsStatus("");
  } catch {
    showCameraSettingsStatus(loadError, true);
  } finally {
    clearTimeout(timer);
  }
}

addCameraButton.addEventListener("click", () => {
  addCamera().focus();
  cameraSettingsDirty = true;
});
cameraEntries.addEventListener("input", () => {
  cameraSettingsDirty = true;
});
loadCameraSettings();
