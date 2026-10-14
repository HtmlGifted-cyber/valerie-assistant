// Valerie side panel: chat with your local Valerie about the page you are on.
// A page is read only when the user clicks a button. Page text is sent to Valerie as
// information only; nothing is clicked, typed or filled in on the page.

const DEFAULT_SERVER = "http://127.0.0.1:8790";
const SETTINGS_KEY = "valerieSettings";
const MSG_NOT_RUNNING = "Valerie isn't running - start valerie-web on this computer.";
const MSG_NOT_PAIRED = "Not paired - open Options and paste your Valerie token.";

const $ = (id) => document.getElementById(id);
const ACTION_BUTTONS = ["btn-ask", "btn-summarize", "btn-explain", "btn-save", "btn-open", "send", "sitego"];
let busy = false;
let thinkTimer = null;

function lockButtons(on) {
  busy = on;
  ACTION_BUTTONS.forEach((id) => { $(id).disabled = on; });
}

function addMsg(text, cls) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  $("log").appendChild(d);
  d.scrollIntoView({ block: "end" });
  return d;
}

function startThinking(label) {
  const s = $("status");
  const t0 = Date.now();
  const tick = () => {
    s.textContent = label + " (" + Math.round((Date.now() - t0) / 1000) + "s)";
  };
  s.classList.remove("hide");
  tick();
  thinkTimer = setInterval(tick, 1000);
}

function stopThinking() {
  clearInterval(thinkTimer);
  thinkTimer = null;
  $("status").classList.add("hide");
}

function fileName(p) {
  return String(p).split(/[\\/]/).pop();
}

async function getSettings() {
  const got = await chrome.storage.local.get(SETTINGS_KEY);
  const s = got[SETTINGS_KEY] || {};
  return {
    serverUrl: String(s.serverUrl || DEFAULT_SERVER).replace(/\/+$/, ""),
    token: String(s.token || "").trim()
  };
}

// Calls the local Valerie server. Errors come back as plain sentences for the user.
async function callValerie(path, body) {
  const s = await getSettings();
  if (!s.token) throw new Error(MSG_NOT_PAIRED);
  let res;
  try {
    res = await fetch(s.serverUrl + path, {
      method: body ? "POST" : "GET",
      headers: { "X-Valerie-Token": s.token, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
  } catch (e) {
    throw new Error(MSG_NOT_RUNNING);
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (res.status === 401) throw new Error(MSG_NOT_PAIRED);
  if (res.status === 403) throw new Error("Valerie refused this connection. The server address must be this computer (127.0.0.1).");
  if (!res.ok) throw new Error(data.error || "Valerie could not answer. Try again in a moment.");
  return data;
}

// Runs inside the web page when the user clicks a button.
function pageGrab() {
  const sel = window.getSelection ? String(window.getSelection()) : "";
  const body = document.body ? (document.body.innerText || "") : "";
  return {
    url: location.href,
    title: document.title || "",
    text: body.trim().slice(0, 20000),
    selection: sel.trim()
  };
}

async function grabPage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id === undefined) throw new Error("No web page is open in this window.");
  if (!/^https?:\/\//i.test(tab.url || "")) {
    throw new Error("Valerie can only read normal web pages (http or https), not browser pages.");
  }
  let results;
  try {
    results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: pageGrab });
  } catch (e) {
    throw new Error("Valerie can't read this page yet. Click the Valerie icon while on the page, then try again.");
  }
  const page = results && results[0] && results[0].result;
  if (!page) throw new Error("Valerie couldn't read this page.");
  return page;
}

// Sends one question to Valerie. shown = what appears in the chat (null to show nothing).
async function sendToValerie(shown, message, options) {
  if (busy) return;
  lockButtons(true);
  if (shown) addMsg(shown, "me");
  startThinking("Valerie is thinking...");
  try {
    const body = { message: message };
    if (options.withPage) {
      const page = await grabPage();
      if (options.needSelection && !page.selection) {
        throw new Error("Select some text on the page first, then click Explain selection.");
      }
      body.page = { url: page.url, title: page.title, text: page.text };
      if (page.selection) body.selection = page.selection;
    }
    const data = await callValerie("/ext/chat", body);
    addMsg(String(data.reply || "(no reply)"), "her");
  } catch (e) {
    addMsg(e.message || "Something went wrong.", "err");
  } finally {
    stopThinking();
    lockButtons(false);
  }
}

async function savePage() {
  if (busy) return;
  lockButtons(true);
  startThinking("Saving to Valerie's memory...");
  try {
    const page = await grabPage();
    const data = await callValerie("/ext/save", {
      url: page.url, title: page.title, text: page.text, note: ""
    });
    addMsg("Saved to Valerie's memory: " + fileName(data.saved), "her");
  } catch (e) {
    addMsg(e.message || "Could not save the page.", "err");
  } finally {
    stopThinking();
    lockButtons(false);
  }
}

function openSite(raw) {
  const text = String(raw || "").trim();
  if (!text) return;
  let url;
  let shown;
  if (/^https?:\/\/\S+$/i.test(text)) {
    url = text;
    shown = "Opened " + text;
  } else if (/^[^\s/]+\.[a-z]{2,}(\/\S*)?$/i.test(text)) {
    url = "https://" + text;
    shown = "Opened " + url;
  } else {
    url = "https://www.google.com/search?q=" + encodeURIComponent(text);
    shown = "Searched Google for: " + text;
  }
  chrome.tabs.create({ url: url });
  addMsg(shown, "her");
  $("sitetxt").value = "";
  $("openrow").classList.add("hide");
}

// Context menu items set a pendingAction in session storage (see background.js).
async function takePending() {
  const got = await chrome.storage.session.get("pendingAction");
  const p = got.pendingAction;
  if (!p || busy) return;
  await chrome.storage.session.remove("pendingAction");
  if (p.kind === "ask") {
    sendToValerie("Ask about this page", "Tell me what this page is about and what matters most in it. If there is selected text, explain that first.", { withPage: true });
  } else if (p.kind === "save") {
    savePage();
  }
}

$("btn-ask").addEventListener("click", () => {
  const typed = $("txt").value.trim();
  $("txt").value = "";
  sendToValerie(typed || "Ask about this page", typed || "What is this page about, and what should I know about it?", { withPage: true });
});
$("btn-summarize").addEventListener("click", () => {
  sendToValerie("Summarize page", "Summarize this web page for me in a few short bullet points. Say what it is and what matters most.", { withPage: true });
});
$("btn-explain").addEventListener("click", () => {
  sendToValerie("Explain selection", "Explain the selected text in plain words, using the page for context. Keep it short.", { withPage: true, needSelection: true });
});
$("btn-save").addEventListener("click", savePage);
$("btn-open").addEventListener("click", () => {
  $("openrow").classList.toggle("hide");
  if (!$("openrow").classList.contains("hide")) $("sitetxt").focus();
});
$("sitego").addEventListener("click", () => openSite($("sitetxt").value));
$("sitetxt").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); openSite($("sitetxt").value); }
});
$("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("txt").value.trim();
  if (!t) return;
  $("txt").value = "";
  sendToValerie(t, t, { withPage: false });
});
$("txt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("form").requestSubmit();
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "session" && changes.pendingAction && changes.pendingAction.newValue) {
    takePending();
  }
});

(async function start() {
  const s = await getSettings();
  if (!s.token) addMsg(MSG_NOT_PAIRED, "err");
  else addMsg("Hi. Use the buttons above, or type a question. Valerie only reads a page when you click a button.", "her");
  await takePending();
})();
