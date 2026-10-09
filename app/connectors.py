"""
connectors.py - plug-in connectors for Valerie, defined as JSON files.

Drop a .json file in ~/.local/share/valerie-assistant/connectors/ and
Valerie can use it through one tool (use_connector) - no code changes.

Two types:

  "sheet_board"  A Google Sheet used as a message/task board (a shared task
                 board), reached through the Apps Script web
                 app connector in Settings. Actions: inbox, list, submit,
                 update.

  "http"         Any JSON web endpoint. Each action names a method, url and
                 a body template whose {placeholders} are filled from the
                 fields Valerie supplies. Secrets come from config.json
                 ("connector_secrets": {"<name>": "..."}) via {secret}.

Anything that writes (submit/update/http POST) asks the owner first when
"confirm_writes" is true (the default).
"""

import datetime
import json
import os
import time

import requests

import config as cfg_mod
from connector import WebAppConnector

CONNECTORS_DIR = os.path.join(cfg_mod.DATA_DIR, "connectors")


class ConnectorPluginError(Exception):
    pass


def load_all():
    out = {}
    if not os.path.isdir(CONNECTORS_DIR):
        return out
    for f in sorted(os.listdir(CONNECTORS_DIR)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONNECTORS_DIR, f), encoding="utf-8") as fh:
                c = json.load(fh)
            if c.get("enabled", True):
                out[c.get("name") or f[:-5]] = c
        except (OSError, ValueError):
            continue
    return out


def describe():
    """One line per connector, used in the tool description."""
    parts = []
    for name, c in load_all().items():
        acts = {"http": ", ".join(c.get("actions", {}).keys()), "php_board": "inbox, list, submit"}.get(
            c.get("type"), "inbox, list, submit, update")
        parts.append(f"'{name}' ({c.get('description', '')[:90]}; actions: {acts})")
    return "; ".join(parts) or "none installed"


def _approve(c, confirm, title, text):
    if not c.get("confirm_writes", True):
        return
    if confirm is None:
        raise ConnectorPluginError("This needs the owner's approval in the Valerie window.")
    if not confirm(title, text):
        raise ConnectorPluginError("Declined - nothing was sent.")


# ------------------------------------------------------------ sheet_board

def _wa(cfg):
    wa = cfg.get("web_app", {})
    return WebAppConnector(wa.get("url", ""), wa.get("token", ""))


def _board_rows(c, cfg):
    values = _wa(cfg).read_sheet(c["spreadsheet_id"], c.get("sheet", "Sheet1"), None) or []
    if not values:
        return [], []
    header = [str(h).strip() for h in values[0]]
    rows = []
    for i, r in enumerate(values[1:], start=2):  # sheet row numbers
        rows.append((i, {header[j]: ("" if j >= len(r) else str(r[j])) for j in range(len(header))}))
    return header, rows


def _fmt(rownum, row, cols):
    return f"[row {rownum}] " + " | ".join(f"{k}: {row.get(k, '')[:300]}" for k in cols if row.get(k, ""))


def _board(c, cfg, action, fields, confirm):
    cols = c.get("columns") or ["Timestamp", "From", "To", "Message", "Priority", "Status", "Completed Y/N"]
    done_col = c.get("completed_column", "Completed Y/N")
    if action in ("inbox", "list"):
        header, rows = _board_rows(c, cfg)
        limit = int(fields.get("limit") or 10)
        if action == "inbox":
            who = [w.lower() for w in c.get("inbox_to", ["Valerie"])]
            rows = [(n, r) for n, r in rows
                    if r.get("To", "").strip().lower() in who and not r.get(done_col, "").strip().upper().startswith("Y")]
        for k, v in fields.items():
            if k in header and v:
                rows = [(n, r) for n, r in rows if str(v).lower() in r.get(k, "").lower()]
        rows = rows[-limit:][::-1]
        title = c.get("title", c["name"])
        if not rows:
            return f"{title}: nothing {'open for ' + '/'.join(c.get('inbox_to', ['Valerie'])) if action == 'inbox' else 'matching'}."
        return f"{title} ({len(rows)} shown, newest first):\n" + "\n".join(_fmt(n, r, header) for n, r in rows)

    if action == "submit":
        row = dict(c.get("defaults", {}))
        row.update({k: str(v) for k, v in fields.items() if v not in (None, "")})
        if not row.get("Message"):
            raise ConnectorPluginError("submit needs a Message.")
        if not row.get("To"):
            raise ConnectorPluginError("submit needs To (who the item is for, e.g. a person or team name).")
        ts_col = c.get("timestamp_column", "Timestamp")
        row[ts_col] = datetime.datetime.now().strftime(c.get("timestamp_format", "%Y-%m-%d %H:%M ")) + time.strftime("%Z")
        values = [row.get(col, "") for col in cols]
        _approve(c, confirm, f"Submit to {c.get('title', c['name'])}?",
                 "\n".join(f"{col}: {row.get(col, '')}" for col in cols))
        _wa(cfg).append_sheet(c["spreadsheet_id"], c.get("sheet", "Sheet1"), values)
        return f"Submitted to {c.get('title', c['name'])} (To: {row['To']})."

    if action == "update":
        rownum = int(fields.pop("row", 0) or 0)
        if rownum < 2:
            raise ConnectorPluginError("update needs the row number (from inbox/list).")
        header, rows = _board_rows(c, cfg)
        current = dict(rows).get(rownum)
        if current is None:
            raise ConnectorPluginError(f"Row {rownum} doesn't exist.")
        changes = {k: str(v) for k, v in fields.items() if k in header}
        if not changes:
            raise ConnectorPluginError(f"Nothing to change - use column names: {', '.join(header)}.")
        new = dict(current, **changes)
        _approve(c, confirm, f"Update row {rownum} on {c.get('title', c['name'])}?",
                 "\n".join(f"{k}: {current.get(k, '')} -> {v}" for k, v in changes.items()))
        _wa(cfg).update_sheet_row(c["spreadsheet_id"], c.get("sheet", "Sheet1"), rownum, [new.get(h, "") for h in header])
        return f"Row {rownum} updated ({', '.join(f'{k}={v}' for k, v in changes.items())})."

    raise ConnectorPluginError("Board actions: inbox, list, submit, update.")


# -------------------------------------------------------------- php_board
# A board reached through a PHP bridge (e.g. https://example.com/bridge.php):
# GET ?apiKey=... returns {"gas_response": "<JSON rows>"}, POST {apiKey, from,
# to, message, priority, status} adds a row. The key lives in config.json
# under connector_secrets, never in the connector file.

def _php_rows(c, cfg):
    key = cfg.get("connector_secrets", {}).get(c["name"], "")
    r = requests.get(c["url"], params={"apiKey": key}, timeout=30)
    try:
        data = r.json()
    except ValueError:
        raise ConnectorPluginError(f"Bridge returned HTTP {r.status_code}, not JSON: {r.text[:200]}")
    raw = data.get("gas_response", "")
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise ConnectorPluginError(
            f"The bridge cut the board off at {len(raw)} characters, so I can't see the newer rows. "
            "The PHP needs to return the full gas_response (remove its length limit).")
    if not isinstance(values, list):
        info = values if isinstance(values, dict) else {}
        raise ConnectorPluginError(
            f"This bridge's read only returns a status check ({info.get('result', '?')}, "
            f"{info.get('spreadsheet', '')}, tabs: {', '.join(info.get('sheets', []))}), not the items. "
            "I can still submit to it.")
    header = [str(h).strip() for h in values[0]] if values else []
    rows = [(i, {header[j]: ("" if j >= len(v) else str(v[j])) for j in range(len(header))})
            for i, v in enumerate(values[1:], start=2)]
    return header, rows


def _php_board(c, cfg, action, fields, confirm):
    if action in ("inbox", "list"):
        global _board_rows
        orig = _board_rows
        try:
            _board_rows = lambda c_, cfg_: _php_rows(c_, cfg_)  # reuse the sheet_board filtering/formatting
            return _board(c, cfg, action, fields, confirm)
        finally:
            _board_rows = orig
    if action == "submit":
        row = dict(c.get("defaults", {}))
        row.update({k: str(v) for k, v in fields.items() if v not in (None, "")})
        if not row.get("Message") or not row.get("To"):
            raise ConnectorPluginError("submit needs To and Message.")
        key = cfg.get("connector_secrets", {}).get(c["name"], "")
        if not key:
            raise ConnectorPluginError(f"No API key saved for {c['name']} (connector_secrets in config.json).")
        payload = {"from": row.get("From", "Valerie"), "to": row["To"], "message": row["Message"],
                   "priority": row.get("Priority", "Normal"), "status": row.get("Status", "Unread")}
        _approve(c, confirm, f"Submit to {c.get('title', c['name'])}?",
                 "\n".join(f"{k.title()}: {v}" for k, v in payload.items()))
        r = requests.post(c["url"], json=dict(payload, apiKey=key), timeout=30)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code != 200 or str(data.get("result", "")).lower().startswith(("error", "unauthor")):
            raise ConnectorPluginError(f"Bridge said HTTP {r.status_code}: {json.dumps(data)[:300]}")
        return f"Submitted to {c.get('title', c['name'])} (To: {payload['to']}). Bridge: {data.get('message') or data.get('result')}"
    raise ConnectorPluginError("This bridge supports: inbox, list, submit.")


# ------------------------------------------------------------------- http

def _fill(template, fields, secret):
    if isinstance(template, dict):
        return {k: _fill(v, fields, secret) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, fields, secret) for v in template]
    if isinstance(template, str):
        out = template.replace("{secret}", secret or "")
        for k, v in fields.items():
            out = out.replace("{" + k + "}", str(v))
        return out
    return template


def _http(c, cfg, action, fields, confirm):
    spec = c.get("actions", {}).get(action)
    if not spec:
        raise ConnectorPluginError(f"Actions for {c['name']}: {', '.join(c.get('actions', {}))}.")
    secret = cfg.get("connector_secrets", {}).get(c["name"], "")
    method = spec.get("method", "GET").upper()
    url = _fill(spec["url"], fields, secret)
    body = _fill(spec.get("body"), fields, secret) if spec.get("body") is not None else None
    headers = _fill(spec.get("headers", {}), fields, secret)
    if method != "GET":
        _approve(c, confirm, f"{c.get('title', c['name'])}: {action}?", json.dumps(body, indent=1)[:1500] if body else url)
    r = requests.request(method, url, json=body, headers=headers, timeout=30)
    text = r.text[:3000]
    return f"HTTP {r.status_code}: {text}"


def run(name, action, fields=None, confirm=None, cfg=None):
    cfg = cfg or cfg_mod.load_config()
    conns = load_all()
    if name == "list" or not name:
        return "Connectors: " + describe()
    c = conns.get(name)
    if not c:
        raise ConnectorPluginError(f"No connector '{name}'. Installed: {', '.join(conns) or 'none'}.")
    c.setdefault("name", name)
    fields = dict(fields or {})
    if c.get("type") == "sheet_board":
        return _board(c, cfg, action, fields, confirm)
    if c.get("type") == "http":
        return _http(c, cfg, action, fields, confirm)
    if c.get("type") == "php_board":
        return _php_board(c, cfg, action, fields, confirm)
    raise ConnectorPluginError(f"Unknown connector type '{c.get('type')}'.")
