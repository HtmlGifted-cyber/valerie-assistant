// Valerie extension options: server address, pairing token, and a connection test.

const DEFAULT_SERVER = "http://127.0.0.1:8790";
const SETTINGS_KEY = "valerieSettings";

const $ = (id) => document.getElementById(id);

function showStatus(text, cls) {
  const s = $("status");
  s.className = cls || "";
  s.textContent = text;
}

function cleanServer(raw) {
  const v = String(raw || "").trim().replace(/\/+$/, "");
  return v || DEFAULT_SERVER;
}

// Only this computer is allowed: 127.0.0.1 or localhost, over plain http.
function isThisComputer(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" && (u.hostname === "127.0.0.1" || u.hostname === "localhost");
  } catch (e) {
    return false;
  }
}

function updatePortWarning() {
  const v = cleanServer($("server").value);
  $("portwarn").style.display = v === DEFAULT_SERVER ? "none" : "block";
}

async function load() {
  const got = await chrome.storage.local.get(SETTINGS_KEY);
  const s = got[SETTINGS_KEY] || {};
  $("server").value = s.serverUrl || DEFAULT_SERVER;
  $("token").value = s.token || "";
  updatePortWarning();
}

async function save() {
  const serverUrl = cleanServer($("server").value);
  if (!isThisComputer(serverUrl)) {
    showStatus("The server address must be this computer: http://127.0.0.1:8790.", "bad");
    return false;
  }
  const token = $("token").value.trim();
  await chrome.storage.local.set({ [SETTINGS_KEY]: { serverUrl: serverUrl, token: token } });
  $("server").value = serverUrl;
  updatePortWarning();
  showStatus("Saved.", "ok");
  return true;
}

async function test() {
  if (!(await save())) return;
  const serverUrl = cleanServer($("server").value);
  const token = $("token").value.trim();
  if (!token) {
    showStatus("Not paired - paste the token from pair-extension first.", "bad");
    return;
  }
  showStatus("Testing...", "");
  let res;
  try {
    res = await fetch(serverUrl + "/ext/ping", { headers: { "X-Valerie-Token": token } });
  } catch (e) {
    showStatus("Valerie isn't running - start valerie-web on this computer.", "bad");
    return;
  }
  if (res.status === 401) {
    showStatus("Not paired - the token doesn't match. Run pair-extension again and paste the new token.", "bad");
    return;
  }
  if (res.status === 403) {
    showStatus("Valerie refused this connection. The server address must be this computer.", "bad");
    return;
  }
  if (!res.ok) {
    showStatus("Valerie answered with an error (" + res.status + ").", "bad");
    return;
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  showStatus("Connected to " + (data.name || "Valerie") + ". Paired and ready.", "ok");
}

$("server").addEventListener("input", updatePortWarning);
$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
