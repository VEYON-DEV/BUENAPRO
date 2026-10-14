"""Bounded public HTTP probe; no browser, CAPTCHA solving, DB or model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

import httpx

URL = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"
FORM = "tbBuscador:idFormBuscarProceso"


class FormReader(HTMLParser):
    def __init__(self, form_id: str = FORM) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.in_form = False
        self.fields: dict[str, str] = {}
        self.selects: dict[str, list[tuple[str, str]]] = {}
        self.buttons: dict[str, dict[str, str]] = {}
        self._select: str | None = None
        self._option: dict[str, str] | None = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.in_form = a.get("id") == self.form_id
        if not self.in_form:
            return
        name = a.get("name")
        if tag == "input" and name and "disabled" not in a:
            kind = a.get("type", "text").lower()
            if kind not in {"button", "submit", "reset", "file"} and (
                kind not in {"checkbox", "radio"} or "checked" in a
            ):
                self.fields[name] = a.get("value", "on" if kind in {"checkbox", "radio"} else "")
        elif tag == "button" and name:
            self.buttons[name] = a
        elif tag == "select" and name:
            self._select = name
            self.selects[name] = []
            self.fields[name] = ""
        elif tag == "option" and self._select:
            self._option, self._text = a, ""

    def handle_data(self, text):
        if self._option is not None:
            self._text += text

    def handle_endtag(self, tag):
        if tag == "option" and self._select and self._option is not None:
            value = self._option.get("value", self._text)
            self.selects[self._select].append((self._text.strip(), value))
            if "selected" in self._option:
                self.fields[self._select] = value
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "form":
            self.in_form = False

    def choose(self, label: str, value: str) -> None:
        matches = [name for name, options in self.selects.items()
                   if any(text == label and native == value for text, native in options)]
        if len(matches) != 1:
            raise ValueError("Could not resolve a unique native selector")
        self.fields[matches[0]] = value


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def initialize_procedures(client: httpx.Client, html: str, parser: FormReader) -> dict:
    """Replay the documented native tabChange event, not a search/token bypass."""
    first_form = re.search(r'<form[^>]+id="([^"]+)"', html)
    config = re.search(r'behaviors:\{tabChange:[\s\S]*?update:"([^"]+)"', html)
    if not first_form or not config:
        raise ValueError("Native tabChange/form configuration not found")
    form_id = first_form.group(1)
    first = FormReader(form_id)
    first.feed(html)
    payload = first.fields.copy()
    payload.update({"javax.faces.partial.ajax": "true", "javax.faces.source": "tbBuscador",
                    "javax.faces.partial.execute": "tbBuscador",
                    "javax.faces.partial.render": config.group(1),
                    "javax.faces.behavior.event": "tabChange",
                    "javax.faces.partial.event": "tabChange",
                    "tbBuscador_activeIndex": "1", "tbBuscador_newTab": "tbBuscador:tab1",
                    "tbBuscador_tabindex": "1"})
    response = client.post(URL, data=payload, headers={"Faces-Request": "partial/ajax",
                           "X-Requested-With": "XMLHttpRequest", "Referer": URL})
    response.raise_for_status()
    xml = ElementTree.fromstring(response.text)
    fragments = []
    rendered = []
    for node in xml.findall(".//update"):
        node_id = node.attrib.get("id", "")
        if "ViewState" in node_id:
            parser.fields["javax.faces.ViewState"] = node.text or ""
        elif node_id.startswith(FORM + ":"):
            fragments.append(node.text or "")
            rendered.append(node_id)
    refreshed = FormReader()
    refreshed.feed(f'<form id="{FORM}">' + "".join(fragments) + '</form>')
    parser.fields.update(refreshed.fields)
    parser.selects.update(refreshed.selects)
    parser.buttons.update(refreshed.buttons)
    return {"status": response.status_code, "search_form_updated": bool(fragments),
            "updated_ids": rendered}


def probe(*, initialize_tab: bool = False) -> dict:
    report = {"at": datetime.now(timezone.utc).isoformat(), "transport": "httpx",
              "browser_used": False, "captcha_token_generated_or_replayed": False,
              "query": {"keyword": "SOFTWARE", "object": "Servicio", "year": 2026,
                        "published_from": "01/10/2026", "published_to": "05/10/2026"},
              "db_writes": False, "model_calls": False}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.get(URL)
        report["get_status"] = response.status_code
        if response.status_code != 200:
            report["search_verified"] = False
            return report
        parser = FormReader()
        parser.feed(response.text)
        report["viewstate_received"] = bool(parser.fields.get("javax.faces.ViewState"))
        report["session_cookie_received"] = bool(client.cookies)
        report["recaptcha_script_present"] = "recaptcha/api.js" in response.text
        if not report["viewstate_received"]:
            report["search_verified"] = False
            return report
        if initialize_tab:
            report["tab_initialization"] = initialize_procedures(client, response.text, parser)
        parser.choose("Servicio", "65")
        parser.choose("2026", "2026")
        parser.choose("Seace 3", "3")
        source = FORM + ":btnBuscarSel"
        if source not in parser.buttons:
            raise ValueError("Observed search command missing")
        # Preserve only state from this HTTP session. Never copy browser tokens.
        payload = parser.fields.copy()
        payload.update({FORM + ":descripcionObjeto": "SOFTWARE",
                        FORM + ":dfechaInicio_input": "01/10/2026",
                        FORM + ":dfechaFin_input": "05/10/2026",
                        FORM + ":numPositionTabView": "1", "submit": "S",
                        "javax.faces.partial.ajax": "true", "javax.faces.source": source,
                        source: source,
                        "javax.faces.partial.execute": "@all"})
        update = re.search(r"\b(?:update|u):\s*['\"]([^'\"]+)['\"]", parser.buttons[source].get("onclick", ""))
        if update:
            payload["javax.faces.partial.render"] = update.group(1)
        report["native_render_target_resolved"] = bool(update)
        report["captcha_token_in_initial_form"] = bool(payload.get(FORM + ":tokenBusProSel"))
        response = client.post(URL, data=payload, headers={"Faces-Request": "partial/ajax",
                              "X-Requested-With": "XMLHttpRequest", "Referer": URL})
        report["post_status"] = response.status_code
        report["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        report["response_bytes"] = len(response.content)
        report["content_type"] = response.headers.get("content-type", "")
        report["search_verified"] = False
        if response.status_code == 200:
            try:
                xml = ElementTree.fromstring(response.text)
                text = TextReader()
                for node in xml.findall(".//update"):
                    if "ViewState" not in node.attrib.get("id", ""):
                        text.feed(node.text or "")
                    if node.attrib.get("id") == "frmMesajes:gPrincipal":
                        messages = TextReader()
                        messages.feed(node.text or "")
                        report["server_messages"] = " ".join(" ".join(messages.parts).split())[:1000]
                visible = " ".join(text.parts)
                match = re.search(r"Mostrando de\s+(\d+)\s+a\s+(\d+)\s+del total\s+(\d+)\s*-\s*Página:\s*(\d+)/(\d+)", visible)
                report["partial_response"] = xml.tag == "partial-response"
                report["rendered_update_ids"] = [node.attrib.get("id") for node in xml.findall(".//update")
                                                  if "ViewState" not in node.attrib.get("id", "")]
                report["counter"] = list(map(int, match.groups())) if match else None
                report["no_data_message"] = "No se encontraron Datos" in visible
                report["partial_error"] = xml.find(".//error") is not None
                # A zero from a token-free request cannot establish empty coverage.
                report["positive_result_received"] = bool(match and int(match[3]) > 0)
            except ElementTree.ParseError:
                report["partial_response"] = False
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--initialize-tab", action="store_true",
                        help="Initialize the native procedures tab before search")
    args = parser.parse_args()
    result = probe(initialize_tab=args.initialize_tab)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
