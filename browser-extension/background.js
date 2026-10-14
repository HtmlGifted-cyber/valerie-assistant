// Valerie browser extension - background service worker.
// Sets up the right-click menu items and opens the side panel. The side panel does the
// actual work (reading the page when the user clicks, and talking to Valerie).

const MENU_ASK = "valerie-ask";
const MENU_SAVE = "valerie-save";

function setupMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ASK,
      title: "Ask Valerie about this",
      contexts: ["page", "selection"]
    });
    chrome.contextMenus.create({
      id: MENU_SAVE,
      title: "Save to Valerie's memory",
      contexts: ["page", "selection"]
    });
  });
}

chrome.runtime.onInstalled.addListener(() => {
  setupMenus();
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || tab.windowId === undefined) return;
  const kind = info.menuItemId === MENU_SAVE ? "save" : info.menuItemId === MENU_ASK ? "ask" : null;
  if (!kind) return;
  // Open the panel first, while the click is still a user gesture.
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch (e) {
    return;
  }
  // The panel picks this up on load, or straight away if it was already open.
  await chrome.storage.session.set({ pendingAction: { kind: kind, at: Date.now() } });
});
