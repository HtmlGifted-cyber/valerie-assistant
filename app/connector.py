"""
connector.py - client for the generic Google Apps Script web app connector.

This talks to a Web App that YOU deploy from appscript/Code.gs into your
own Google account (see docs/README.md). Valerie never talks to Google
directly or holds Google credentials - it only ever calls the single
HTTPS endpoint you deployed, authenticated with a shared-secret token
you chose at deploy time. You control what that script is allowed to
touch, because you wrote the spreadsheetId/docId into the request, not
Valerie.
"""

import requests

DEFAULT_TIMEOUT = 20


class ConnectorError(Exception):
    pass


class WebAppConnector:
    def __init__(self, url, token):
        self.url = (url or "").strip()
        self.token = token or ""

    def _post(self, action, params=None):
        if not self.url:
            raise ConnectorError(
                "No web app URL configured yet. Deploy appscript/Code.gs "
                "as a Web App and paste its URL into Valerie's Settings."
            )
        payload = {"action": action, "token": self.token}
        payload.update(params or {})
        try:
            # requests follows the script.google.com -> script.googleusercontent.com
            # redirect automatically, so no curl workaround is needed here.
            resp = requests.post(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            raise ConnectorError(f"Could not reach the web app: {e}")

        if resp.status_code != 200:
            raise ConnectorError(f"Web app returned HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError:
            raise ConnectorError(f"Web app did not return JSON: {resp.text[:300]}")

        if not data.get("ok", False):
            raise ConnectorError(data.get("error", "Web app reported an unknown error."))
        return data.get("result")

    def health_check(self):
        return self._post("health")

    def read_sheet(self, spreadsheet_id, sheet_name="Sheet1", a1_range=None):
        return self._post("readSheet", {
            "spreadsheetId": spreadsheet_id,
            "sheetName": sheet_name,
            "range": a1_range,
        })

    def write_sheet(self, spreadsheet_id, sheet_name, a1_range, values):
        return self._post("writeSheet", {
            "spreadsheetId": spreadsheet_id,
            "sheetName": sheet_name,
            "range": a1_range,
            "values": values,
        })

    def append_sheet(self, spreadsheet_id, sheet_name, row):
        return self._post("appendSheet", {
            "spreadsheetId": spreadsheet_id,
            "sheetName": sheet_name,
            "row": row,
        })

    def update_sheet_row(self, spreadsheet_id, sheet_name, row_number, row):
        return self._post("updateSheetRow", {
            "spreadsheetId": spreadsheet_id,
            "sheetName": sheet_name,
            "rowNumber": row_number,
            "row": row,
        })

    def read_doc(self, doc_id):
        return self._post("readDoc", {"docId": doc_id})

    def write_doc(self, doc_id, text, mode="append"):
        return self._post("writeDoc", {"docId": doc_id, "text": text, "mode": mode})

    def create_doc(self, title, text="", parent_folder_id=None):
        return self._post("createDoc", {
            "title": title,
            "text": text,
            "parentFolderId": parent_folder_id,
        })
