# Valerie browser extension

A Chrome side panel that lets you talk to **your own Valerie**, the local assistant that runs on your computer. It can read the page you are looking at, explain or summarize it, and save it to Valerie's memory.

Everything stays on your computer. The extension talks only to `http://127.0.0.1:8790` (valerie-web on this machine).

## What it does

- **Side panel "Valerie"** with a chat box and these buttons:
  - **Ask about this page**: asks a question about the page you are reading (or a default "what is this about" question if the box is empty).
  - **Summarize page**: a few short bullet points.
  - **Explain selection**: explains the text you highlighted, using the page for context.
  - **Save page to my memory**: saves the page text as a note Valerie can search later.
  - **Open a site**: type a web address to open it, or words to run a Google search in a new tab.
- **Right-click menu** on a page or selection:
  - **Ask Valerie about this** opens the side panel and asks about the page.
  - **Save to Valerie's memory** opens the side panel and saves the page.

## Everyday uses

- Summarize long articles before deciding whether to read them.
- Explain jargon, legal wording, or medical terms in plain words.
- Compare products while shopping: open two pages, ask Valerie to compare the key points.
- Save recipes and how-tos to memory, then ask Valerie for them later.
- Research for school or work: save sources, then ask Valerie what they say.
- Quick questions about the page you are on, without copying and pasting.

## Install

You need Valerie (the desktop app or `valerie-web`) installed on this computer, and Google Chrome (version 116 or newer).

1. Start the web server:
   ```
   valerie-web start
   ```
   (or `venv/bin/python app/valerie_web.py serve` from the Valerie folder)
2. Create a pairing token. From the Valerie folder, run:
   ```
   venv/bin/python app/valerie_web.py pair-extension
   ```
   It prints a 32-character token. Copy it. The token is shown once; only a hash of it is kept on the computer. Running this command again makes a new token and the old one stops working.
3. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose the `browser-extension/` folder from this project.
4. Open the extension's **Options** page (right-click the Valerie icon, or use the Options link in the side panel). Paste the token into **Pairing token**, keep the server address as `http://127.0.0.1:8790`, click **Save**, then **Test connection**. You should see "Connected to Valerie. Paired and ready."
5. Click the Valerie icon in the Chrome toolbar to open the side panel.

## Privacy

- A page is read **only when you click** a button or a menu item. Nothing runs in the background.
- Page text is sent to your own Valerie on this computer, and it is labeled for Valerie as information only, never as instructions. A label helps, but no AI model follows it perfectly, so do not rely on Valerie to ignore instructions hidden in a page.
- The extension does **not** click, type, submit forms, or fill anything in on web pages in this version.
- Only requests from this computer (127.0.0.1 or ::1) are answered. Other devices on your home network are refused, even when they know the token.
- The server does not write page text to its activity log. Saved pages are stored as markdown files in the Valerie data folder under `web-clips/`.
- The extension asks Chrome for access to the active tab and to `http://127.0.0.1:8790` only. It does not ask for access to every website.
- The token is stored in Chrome's extension storage on this computer. Anyone with access to your Chrome profile can read it, so treat it like a password.
- It works only with your own Valerie. It does not connect to any other service.

## Troubleshooting

- **"Valerie isn't running - start valerie-web on this computer."** Start the server (`valerie-web start`), then try again.
- **"Not paired - open Options."** Paste the token from `pair-extension` into Options and click Save. If it still fails, run `pair-extension` again and paste the new token.
- **"Valerie refused this connection."** The server address must be `http://127.0.0.1:8790`. Other addresses are refused.
- **"Valerie can't read this page yet."** Chrome sometimes needs a fresh click on the page first. Click the Valerie icon while the page is open, then try again. Browser pages (chrome://, the Chrome Web Store, and similar) cannot be read.
- **"Select some text on the page first."** Highlight the words you want explained, then click Explain selection.
- **Changed the port?** The extension may only reach port 8790. Keep the default unless you also change `host_permissions` in `manifest.json`, then reload the extension in `chrome://extensions`.
- **Valerie is slow.** The local model can take several minutes on the first answer. The panel shows how many seconds have passed.
- **Saved page does not show up in searches.** Document search needs the local embedding model to be running. The page is still saved in `web-clips/`.

## Files

- `manifest.json`: Manifest V3 settings and permissions
- `background.js`: right-click menus and opening the side panel
- `sidepanel.html`, `sidepanel.js`, `sidepanel.css`: the side panel
- `options.html`, `options.js`: server address, pairing token, connection test
- `icons/`: toolbar icons

The server side lives in `app/valerie_web.py` under the `/ext/` routes.
