"""Public PROD2 JSF HTTP transport: search, native row navigation, and document inventory.

An HTTP 200 alone is never treated as a valid search or detail. No browser state,
CAPTCHA solving, database writes, or model calls are used by this client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit
from uuid import UUID
from xml.etree import ElementTree as ET

import httpx

SEARCH_URL = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"
FORM = "tbBuscador:idFormBuscarProceso"
TABLE = FORM + ":dtProcesos"
_COUNTER = re.compile(r"Mostrando de\s+(\d+)\s+a\s+(\d+)\s+del total\s+(\d+)\s*-\s*Página:\s*(\d+)/(\d+)")


class Prod2ProtocolError(ValueError):
    """Failure without response bodies, cookies, or opaque session values."""


@dataclass
class Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def walk(self, tag: str | None = None):
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(tag)

    def text(self) -> str:
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(" ".join(
            child.text() if isinstance(child, Node) else child for child in self.children
        ).split())

    def by_id(self, element_id: str) -> Node | None:
        return next((n for n in self.walk() if n.attrs.get("id") == element_id), None)


class Document(HTMLParser):
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self._void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def form_fields(form: Node) -> dict[str, str]:
    fields = {}
    for node in form.walk():
        name = node.attrs.get("name")
        if not name or "disabled" in node.attrs:
            continue
        if node.tag == "input":
            kind = node.attrs.get("type", "text")
            if kind in {"submit", "reset", "button", "file"}:
                continue
            if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                continue
            fields[name] = node.attrs.get("value") or ("on" if kind in {"checkbox", "radio"} else "")
        elif node.tag == "select":
            options = list(node.walk("option"))
            selected = next((o for o in options if "selected" in o.attrs), options[0] if options else None)
            fields[name] = (selected.attrs.get("value") or "") if selected else ""
        elif node.tag == "textarea":
            fields[name] = node.text()
    return fields


def cells(row: Node) -> list[Node]:
    return [n for n in row.children if isinstance(n, Node) and n.tag in {"td", "th"}]


def _search_rows(root: Node) -> tuple[list[dict], list[dict[str, str]], list[int]]:
    body = root.by_id(TABLE + "_data")
    if body is None:
        raise Prod2ProtocolError("Native search table missing")
    rows, actions, indices = [], [], []
    for row in (node for node in body.children if isinstance(node, Node) and node.tag == "tr"):
        if row.attrs.get("data-ri") is None:
            if cells(row) and "ui-datatable-empty-message" not in (row.attrs.get("class") or ""):
                raise Prod2ProtocolError("Search row index missing")
            continue
        if not (row.attrs["data-ri"] or "").isdigit():
            raise Prod2ProtocolError("Invalid search row index")
        indices.append(int(row.attrs["data-ri"]))
        values = [n.text() for n in cells(row)]
        if len(values) != 13:
            raise Prod2ProtocolError("Unexpected search column count")
        native = None
        procedure = None
        for link in row.walk("a"):
            onclick = link.attrs.get("onclick") or ""
            if "addSubmitParam" in onclick and "ptoRetorno" in onclick:
                match = re.search(r"addSubmitParam\('([^']+)',\{(.*?)\}\)", onclick)
                if not match or match[1] != FORM:
                    raise Prod2ProtocolError("Unexpected native detail action")
                native = dict(re.findall(r"'([^']+)':'([^']*)'", match[2]))
            elif "PrimeFaces.ab" in onclick and "nidProceso" in onclick:
                match = re.search(r"name:'nidProceso',value:'(\d+)'", onclick)
                if match:
                    procedure = int(match[1])
        if not native or not native.get("nidProceso", "").isdigit() or procedure is None:
            raise Prod2ProtocolError("Procedure and convocatoria identities missing")
        rows.append({"procedure_id": procedure, "convocatoria_id": int(native["nidProceso"]),
                     "entity": values[1], "published_at": values[2], "nomenclature": values[3],
                     "object": values[5], "description": values[6], "amount_text": values[9],
                     "currency": values[10], "seace_version": values[11]})
        actions.append(native)
    if len({r["convocatoria_id"] for r in rows}) != len(rows):
        raise Prod2ProtocolError("Duplicate identities within search page")
    return rows, actions, indices


def _validate_search_counter(counts: tuple[int, ...], rows: list[dict], indices: list[int]):
    start, end, total, page, pages = counts
    if (page < 1 or pages < page
            or (total == 0 and (rows or counts != (0, 0, 0, 1, 1)))
            or (total > 0 and (start < 1 or end < start or end > total or not rows
                              or len(rows) != end - start + 1
                              or indices != list(range(start - 1, end))))):
        raise Prod2ProtocolError("Search counter does not match extracted rows")


def parse_search(html: str) -> tuple[list[dict], list[dict[str, str]], tuple[int, ...]]:
    root = Document(html).root
    counters = list(_COUNTER.finditer(root.text()))
    if len(counters) != 1:
        raise Prod2ProtocolError("Search counter missing or ambiguous; coverage is unverified")
    counts = tuple(map(int, counters[0].groups()))
    rows, actions, indices = _search_rows(root)
    _validate_search_counter(counts, rows, indices)
    return rows, actions, counts


def _search_paginator(html: str, *, required: bool) -> dict | None:
    """Read the public DataTable configuration, never evaluate response JavaScript."""
    start = re.search(r'PrimeFaces\.cw\("DataTable","widget_tbBuscador_idFormBuscarProceso_dtProcesos",\{id:"'
                      + re.escape(TABLE) + r'",paginator:\{', html)
    if not start:
        if required or 'PrimeFaces.cw("DataTable","widget_tbBuscador_idFormBuscarProceso_dtProcesos"' in html:
            raise Prod2ProtocolError("Native search paginator configuration missing")
        return None
    # All observed numeric fields precede currentPageTemplate, whose text contains braces.
    config = html[start.end():].split("currentPageTemplate:", 1)[0].split("},behaviors:", 1)[0]
    numeric = {key: re.search(r"\b" + key + r":(\d+)\b", config) for key in ("rows", "rowCount", "page")}
    if not all(numeric.values()):
        raise Prod2ProtocolError("Native search paginator counts missing")
    result = {key: int(match[1]) for key, match in numeric.items()}
    behavior = re.search(r'behaviors:\{page:function\([^)]*\)\{PrimeFaces\.ab\(\{source:"([^"]+)",'
                         r'event:"([^"]+)",process:"([^"]+)"', html[start.end():])
    if not behavior or behavior.groups() != (TABLE, "page", TABLE):
        raise Prod2ProtocolError("Native search page behavior is unverified")
    if not 1 <= result["rows"] <= 100:
        raise Prod2ProtocolError("Native search page size is outside bounded policy")
    return result


def parse_document_rows(body: Node) -> list[dict]:
    """Read publication rows, not distinct files: one UUID may occur in several stages."""
    documents = []
    for row in body.walk("tr"):
        columns = cells(row)
        row_documents = []
        for link in row.walk("a"):
            onclick = link.attrs.get("onclick") or ""
            match = re.search(r"descargaDocGeneral\('([^']+)','([^']+)','([^']+)'\)", onclick)
            if not match:
                continue
            try:
                document_uuid = str(UUID(match[1]))
            except ValueError:
                raise Prod2ProtocolError("Invalid observed document UUID") from None
            if len(columns) != 6:
                raise Prod2ProtocolError("Unexpected document column count")
            row_documents.append({"uuid": document_uuid, "seace_version": match[2],
                              "filename": match[3], "stage": columns[1].text(),
                              "type": columns[2].text(), "published_at": columns[4].text()})
        if len(row_documents) > 1:
            raise Prod2ProtocolError("Multiple download actions in one document publication row")
        documents.extend(row_documents)
    document_resources(documents)
    return documents


def document_resources(events: list[dict]) -> list[dict]:
    """Keep one download resource per UUID, preserving all publication events separately.

    The first native occurrence remains the legacy metadata representative. A
    reused UUID is valid only when its filename/version agree and its publication
    metadata differ; an exact repeated row still invalidates reconciliation.
    """
    resources, seen = {}, set()
    for event in events:
        identity = tuple(event[key] for key in
                         ("uuid", "seace_version", "filename", "stage", "type", "published_at"))
        if identity in seen:
            raise Prod2ProtocolError("Duplicate UUID publication event in document inventory")
        seen.add(identity)
        previous = resources.get(event["uuid"])
        if previous and any(previous[key] != event[key] for key in ("filename", "seace_version")):
            raise Prod2ProtocolError("Conflicting filename or version for a document UUID")
        resources.setdefault(event["uuid"], dict(event))
    return list(resources.values())


def parse_detail(html: str, expected: dict) -> dict:
    root = Document(html).root
    labels = {"Nomenclatura:": "nomenclature", "N° Convocatoria:": "convocatoria_number",
              "Tipo Compra o Selección:": "selection_type", "Normativa Aplicable:": "regulation",
              "Versión SEACE": "seace_version", "Entidad Convocante:": "entity",
              "Objeto de Contratación:": "object", "Descripción del Objeto:": "description",
              "VR / VE / Cuantía de la contratación": "amount_text",
              "Fecha y Hora Publicación:": "published_at"}
    fields = {}
    for row in root.walk("tr"):
        columns = cells(row)
        if len(columns) == 2 and columns[0].text() in labels:
            fields.setdefault(labels[columns[0].text()], columns[1].text())
    # The visible cell is shortened with "...". The native dialog is already
    # present in the same HTTP response and holds the complete official object.
    description_dialog = root.by_id("tbFicha:dialogDescObj")
    if description_dialog is not None:
        full = description_dialog.text()
        label = "Descripción del Objeto:"
        if full.startswith(label):
            fields["description"] = full[len(label):].strip()
    if (fields.get("nomenclature") != expected["nomenclature"]
            or fields.get("description") != expected["description"]
            or fields.get("entity") != expected["entity"]):
        raise Prod2ProtocolError("Detail does not match selected search result")
    schedule_body = root.by_id("tbFicha:dtCronograma_data")
    if schedule_body is None:
        raise Prod2ProtocolError("Detail schedule missing")
    schedule = []
    for row in schedule_body.walk("tr"):
        columns = cells(row)
        if len(columns) == 3:
            schedule.append({"stage": columns[0].text(), "starts_at_text": columns[1].text(),
                             "ends_at_text": columns[2].text()})
    documents_body = root.by_id("tbFicha:dtDocumentos_data")
    if documents_body is None:
        raise Prod2ProtocolError("Detail document inventory missing")
    document_events = parse_document_rows(documents_body)
    documents = document_resources(document_events)
    inventory = re.search(r'PrimeFaces\.cw\("DataTable","widget_tbFicha_dtDocumentos",\{id:"tbFicha:dtDocumentos",paginator:\{[^}]*\browCount:(\d+)', html)
    inventory_total = int(inventory[1]) if inventory else None
    if inventory_total is not None and len(document_events) > inventory_total:
        raise Prod2ProtocolError("Document count exceeds native inventory total")
    # Read item state only; bidder/person tables are deliberately excluded from exports.
    states = []
    for row in root.walk("tr"):
        columns = cells(row)
        for index, column in enumerate(columns[:-1]):
            if column.text() == "Estado:" and columns[index + 1].text():
                states.append(columns[index + 1].text())
    return {"procedure_id": expected["procedure_id"], "convocatoria_id": expected["convocatoria_id"],
            **fields, "schedule": schedule, "documents": documents, "document_events": document_events,
            "document_inventory_total": inventory_total,
            "document_inventory_complete": inventory_total is not None and inventory_total == len(document_events),
            "item_states": list(dict.fromkeys(states))}


class Prod2HttpClient:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.fields: dict[str, str] = {}
        self.form: Node | None = None
        self.rows: list[dict] = []
        self.actions: list[dict[str, str]] = []
        self.counter: tuple[int, ...] = ()
        self.search_snapshot: dict | None = None
        self._page_rows: dict[int, list[dict]] = {}
        self._seen_convocatorias: dict[int, int] = {}
        self._counter_origin = ""

    def _clear_search(self):
        # Never permit detail navigation with stale row actions after a failed page.
        self.rows, self.actions, self.counter = [], [], ()
        self.search_snapshot = None
        self._page_rows = {}
        self._seen_convocatorias = {}
        self._counter_origin = ""

    def _search_result(self, *, counter_origin: str) -> dict:
        self._counter_origin = counter_origin
        return {"rows": self.rows, "counter": list(self.counter), "search_verified": True,
                "counter_origin": counter_origin, "snapshot": dict(self.search_snapshot),
                # JSF pagination is not an atomic source snapshot. The caller must
                # repeat the initial search to reconcile counts before declaring coverage.
                "coverage_complete": False}

    def _request(self, method: str, url: str, *, data=None, headers=None):
        history = []
        for _ in range(7):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "prod2.seace.gob.pe"
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise Prod2ProtocolError("JSF redirect is outside the official PROD2 HTTPS host")
            try:
                response = self.client.request(method, url, data=data, headers=headers, follow_redirects=False)
            except httpx.HTTPError:
                raise Prod2ProtocolError("PROD2 HTTP transport failed") from None
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise Prod2ProtocolError("JSF redirect lacks location")
                history.append(response)
                url = urljoin(str(response.url), location)
                target = urlsplit(url)
                # The native POST emits an http:// URL even on TLS. Use the same
                # official path over HTTPS; never transmit the JSF session over HTTP.
                if (target.scheme == "http" and target.hostname == "prod2.seace.gob.pe"
                        and not target.username and not target.password and target.port in (None, 80)):
                    url = target._replace(scheme="https", netloc="prod2.seace.gob.pe").geturl()
                if response.status_code == 303 or (method == "POST" and response.status_code in {301, 302}):
                    method, data = "GET", None
                continue
            if response.status_code != 200:
                raise Prod2ProtocolError(f"PROD2 HTTP status {response.status_code}")
            response.history = history
            return response
        raise Prod2ProtocolError("Too many native JSF redirects")

    def _post(self, payload: dict[str, str], *, ajax: bool):
        headers = {"Referer": SEARCH_URL}
        if ajax:
            headers.update({"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"})
        return self._request("POST", SEARCH_URL, data=payload, headers=headers)

    def _updates(self, response: httpx.Response) -> str:
        try:
            xml = ET.fromstring(response.text)
        except ET.ParseError:
            raise Prod2ProtocolError("Expected native JSF XML response") from None
        if xml.tag != "partial-response" or xml.find(".//error") is not None or xml.find(".//redirect") is not None:
            raise Prod2ProtocolError("Unexpected native JSF response")
        states = [n for n in xml.findall(".//update") if "ViewState" in n.attrib.get("id", "")]
        if not states or not all(n.text for n in states):
            raise Prod2ProtocolError("Native JSF ViewState update missing")
        fragments = []
        for node in xml.findall(".//update"):
            element_id = node.attrib.get("id", "")
            if "ViewState" in element_id:
                self.fields["javax.faces.ViewState"] = node.text or ""
            elif element_id.startswith(FORM + ":"):
                fragments.append(node.text or "")
        html = "".join(fragments)
        wrapper = Document(f'<form id="{FORM}">{html}</form>').root.by_id(FORM)
        self.fields.update(form_fields(wrapper))
        # Search-panel controls refresh with each native request. Keep its button/select DOM.
        if wrapper.by_id(FORM + ":btnBuscarSel") is not None:
            self.form = wrapper
        return html

    def bootstrap(self, *, initialize_tab: bool = True):
        self._clear_search()
        response = self._request("GET", SEARCH_URL)
        root = Document(response.text).root
        self.form = root.by_id(FORM)
        if self.form is None:
            raise Prod2ProtocolError("Native procedures form missing")
        self.fields = form_fields(self.form)
        if not self.fields.get("javax.faces.ViewState"):
            raise Prod2ProtocolError("Native JSF ViewState missing")
        if initialize_tab:
            first_form = next(root.walk("form"), None)
            config = re.search(r'behaviors:\{tabChange:[\s\S]*?update:"([^"]+)"', response.text)
            if first_form is None or not config:
                raise Prod2ProtocolError("Native procedures tab configuration missing")
            data = form_fields(first_form)
            data.update({"javax.faces.partial.ajax": "true", "javax.faces.source": "tbBuscador",
                         "javax.faces.partial.execute": "tbBuscador", "javax.faces.partial.render": config[1],
                         "javax.faces.behavior.event": "tabChange", "javax.faces.partial.event": "tabChange",
                         "tbBuscador_activeIndex": "1", "tbBuscador_newTab": "tbBuscador:tab1", "tbBuscador_tabindex": "1"})
            self._updates(self._post(data, ajax=True))

    def _choose(self, text: str, value: str):
        matches = [node for node in self.form.walk("select") if any(
            option.text() == text and option.attrs.get("value") == value for option in node.walk("option"))]
        if len(matches) != 1 or not matches[0].attrs.get("name"):
            raise Prod2ProtocolError("Native selector could not be uniquely resolved")
        self.fields[matches[0].attrs["name"]] = value

    def available_convocation_years(self) -> list[int]:
        """Return only the years advertised by the native convocatoria selector."""
        if self.form is None:
            self.bootstrap()
        selectors = [node for node in self.form.walk("select")
                     if node.attrs.get("name") == FORM + ":anioConvocatoria_input"]
        if len(selectors) != 1:
            raise Prod2ProtocolError("Native convocatoria year selector missing or ambiguous")
        years = []
        placeholders = 0
        for option in selectors[0].walk("option"):
            value = option.attrs.get("value")
            if value == "" and option.text() == "[Seleccione]":
                placeholders += 1
                continue
            if not value or not re.fullmatch(r"\d{4}", value) or option.text() != value:
                raise Prod2ProtocolError("Unexpected native convocatoria year option")
            years.append(int(value))
        if not years or placeholders > 1 or len(years) != len(set(years)):
            raise Prod2ProtocolError("Native convocatoria year options missing or duplicated")
        return years

    def search(self, keyword: str, *, year: int, object_type: str, published_from: str = "",
               published_to: str = "", allow_empty_keyword: bool = False) -> dict:
        if self.form is None:
            self.bootstrap()
        if object_type not in {"Bien", "Servicio"} or (not keyword.strip() and not allow_empty_keyword):
            raise ValueError("Use a nonempty keyword and Bien/Servicio")
        dates = []
        for value in (published_from, published_to):
            if value:
                dates.append(datetime.strptime(value, "%d/%m/%Y"))
        if len(dates) == 2 and dates[0] > dates[1]:
            raise ValueError("Publication date range is reversed")
        if not keyword.strip() and (len(dates) != 2 or not isinstance(year, int) or isinstance(year, bool)):
            raise ValueError("Empty keyword requires explicit year, object and bounded publication dates")
        self._clear_search()
        self._choose(object_type, {"Bien": "62", "Servicio": "65"}[object_type])
        self._choose(str(year), str(year))
        self._choose("Seace 3", "3")
        source = FORM + ":btnBuscarSel"
        button = self.form.by_id(source)
        update = re.search(r"\bupdate:\s*['\"]([^'\"]+)['\"]", button.attrs.get("onclick", "") if button else "")
        if not update:
            raise Prod2ProtocolError("Native search button configuration missing")
        self.fields.update({FORM + ":descripcionObjeto": keyword.strip(), FORM + ":dfechaInicio_input": published_from,
                            FORM + ":dfechaFin_input": published_to, FORM + ":numPositionTabView": "1", "submit": "S"})
        data = self.fields.copy()
        data.update({
                     "javax.faces.partial.ajax": "true", "javax.faces.source": source, source: source,
                     "javax.faces.partial.execute": "@all", "javax.faces.partial.render": update[1]})
        html = self._updates(self._post(data, ajax=True))
        rows, actions, counter = parse_search(html)
        paginator = _search_paginator(html, required=True)
        total, pages = counter[2], counter[4]
        expected_pages = max(1, (total + paginator["rows"] - 1) // paginator["rows"])
        if (paginator["rowCount"] != total or paginator["page"] != 0 or counter[3] != 1
                or pages != expected_pages or len(rows) != min(paginator["rows"], total)
                or counter[:2] != ((1, min(paginator["rows"], total)) if total else (0, 0))):
            raise Prod2ProtocolError("Native search paginator does not reconcile with counter")
        self.rows, self.actions, self.counter = rows, actions, counter
        self.search_snapshot = {"page_size": paginator["rows"], "total_rows": total, "total_pages": pages,
                                "year": year, "year_filter_kind": "convocatoria_year", "object_type": object_type,
                                "keyword": keyword.strip(), "published_from": published_from,
                                "published_to": published_to, "native_atomic_snapshot": False}
        self._page_rows[1] = [dict(row) for row in rows]
        self._seen_convocatorias = {row["convocatoria_id"]: 1 for row in rows}
        return self._search_result(counter_origin="native_search_counter")

    def search_page(self, page_number: int) -> dict:
        """Navigate a one-based native page within the latest validated search.

        The paginator sends tbody rows, not a new textual counter. Its counter
        uses the initial native total and the exact observed data-ri indices.
        Any optional new count metadata must agree; terminal reconciliation is
        the caller's responsibility. Detail(index) always binds to this page.
        """
        if self.search_snapshot is None:
            raise Prod2ProtocolError("A verified search is required before pagination")
        if (not isinstance(page_number, int) or isinstance(page_number, bool)
                or not 1 <= page_number <= self.search_snapshot["total_pages"]):
            raise ValueError("Search page is outside the verified page bounds")
        if self.counter[3] == page_number:
            return self._search_result(counter_origin=self._counter_origin)
        snapshot = self.search_snapshot
        size, total = snapshot["page_size"], snapshot["total_rows"]
        first = (page_number - 1) * size
        payload = self.fields.copy()
        payload.update({"javax.faces.partial.ajax": "true", "javax.faces.source": TABLE,
                        "javax.faces.partial.execute": TABLE, "javax.faces.partial.render": TABLE,
                        "javax.faces.behavior.event": "page", "javax.faces.partial.event": "page",
                        TABLE + "_pagination": "true", TABLE + "_first": str(first),
                        TABLE + "_rows": str(size), TABLE + "_encodeFeature": "true"})
        try:
            response = self._post(payload, ajax=True)
            html = self._updates(response)
            xml = ET.fromstring(response.text)
            table_updates = [n for n in xml.findall(".//update") if n.attrib.get("id") == TABLE]
            states = [n for n in xml.findall(".//update") if "ViewState" in n.attrib.get("id", "")]
            if len(table_updates) != 1 or not states or not all(n.text for n in states):
                raise Prod2ProtocolError("Native search page update or ViewState missing")
            fragment = table_updates[0].text or ""
            root = Document(fragment).root
            if root.by_id(TABLE + "_data") is None:
                root = Document(f'<table><tbody id="{TABLE}_data">{fragment}</tbody></table>').root
            rows, actions, indices = _search_rows(root)
            counter = (first + 1, min(first + size, total), total, page_number, snapshot["total_pages"])
            _validate_search_counter(counter, rows, indices)
            textual = list(_COUNTER.finditer(Document(html).root.text()))
            if len(textual) > 1 or (textual and tuple(map(int, textual[0].groups())) != counter):
                raise Prod2ProtocolError("Search counter changed during pagination")
            paginator = _search_paginator(html, required=False)
            if paginator and (paginator["rows"] != size or paginator["rowCount"] != total
                              or paginator["page"] != page_number - 1):
                raise Prod2ProtocolError("Search paginator counts changed during pagination")
            if page_number in self._page_rows and rows != self._page_rows[page_number]:
                raise Prod2ProtocolError("Search rows changed when revisiting a page")
            for row in rows:
                prior = self._seen_convocatorias.get(row["convocatoria_id"])
                if prior is not None and prior != page_number:
                    raise Prod2ProtocolError("Duplicate convocatoria across search pages")
            self.rows, self.actions, self.counter = rows, actions, counter
            self._page_rows[page_number] = [dict(row) for row in rows]
            self._seen_convocatorias.update({row["convocatoria_id"]: page_number for row in rows})
            return self._search_result(counter_origin="initial_native_total_and_observed_indices")
        except Prod2ProtocolError:
            self._clear_search()
            raise
        except ET.ParseError:
            self._clear_search()
            raise Prod2ProtocolError("Search page did not return valid JSF XML") from None

    def detail(self, index: int, *, all_documents: bool = False) -> dict:
        if index < 0 or index >= len(self.rows):
            raise ValueError("Search row index is outside this session")
        data = self.fields.copy()
        data.update(self.actions[index])
        response = self._post(data, ajax=False)
        path = urlsplit(str(response.url)).path
        if path != "/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml":
            raise Prod2ProtocolError("Native detail navigation did not reach ficha")
        result = parse_detail(response.text, self.rows[index])
        if all_documents and not result["document_inventory_complete"]:
            self._complete_documents(response, result)
        result["http_status"] = response.status_code
        result["redirect_statuses"] = [r.status_code for r in response.history]
        return result

    def _complete_documents(self, response: httpx.Response, result: dict):
        """Use the observed native DataTable paginator, with bounded inventory reconciliation."""
        table = "tbFicha:dtDocumentos"
        total = result["document_inventory_total"]
        if total is None or not 0 <= total <= 100:
            raise Prod2ProtocolError("Document inventory total is unknown or exceeds bounded policy")
        root = Document(response.text).root
        form = next((f for f in root.walk("form") if f.by_id(table + "_data")), None)
        if form is None or not form.attrs.get("id"):
            raise Prod2ProtocolError("Native document paginator form missing")
        fields = form_fields(form)
        if not fields.get("javax.faces.ViewState"):
            raise Prod2ProtocolError("Document paginator ViewState missing")
        collected = []
        for first in range(0, total, 15):
            payload = fields.copy()
            payload.update({"javax.faces.partial.ajax": "true", "javax.faces.source": table,
                "javax.faces.partial.execute": table, "javax.faces.partial.render": table,
                table + "_pagination": "true", table + "_first": str(first),
                table + "_rows": "15", table + "_encodeFeature": "true"})
            page = self._request("POST", str(response.url), data=payload,
                headers={"Referer": str(response.url), "Faces-Request": "partial/ajax",
                         "X-Requested-With": "XMLHttpRequest"})
            try:
                xml = ET.fromstring(page.text)
            except ET.ParseError:
                raise Prod2ProtocolError("Document paginator did not return JSF XML") from None
            if xml.tag != "partial-response" or xml.find(".//error") is not None or xml.find(".//redirect") is not None:
                raise Prod2ProtocolError("Document paginator returned an unexpected response")
            fragments = []
            for node in xml.findall(".//update"):
                if "ViewState" in node.attrib.get("id", ""):
                    fields["javax.faces.ViewState"] = node.text or ""
                elif node.attrib.get("id") == table:
                    fragments.append(node.text or "")
            if len(fragments) != 1:
                raise Prod2ProtocolError("Native document table update missing")
            body = Document('<table><tbody>' + fragments[0] + '</tbody></table>').root
            documents = parse_document_rows(body)
            if len(documents) != min(15, total - first):
                raise Prod2ProtocolError("Document paginator count does not reconcile")
            collected.extend(documents)
            document_resources(collected)
        initial = result.get("document_events", result["documents"])
        if collected[:len(initial)] != initial:
            raise Prod2ProtocolError("Document inventory changed during pagination")
        if len(collected) != total:
            raise Prod2ProtocolError("Incomplete document inventory")
        result["document_events"] = collected
        result["documents"] = document_resources(collected)
        result["document_inventory_complete"] = True
