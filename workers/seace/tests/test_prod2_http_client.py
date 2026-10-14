"""Offline protocol fixtures contain synthetic buyer names, no session secrets."""
from urllib.parse import parse_qs

import httpx
import pytest

from buenapro_worker.prod2.client import (
    Document, FORM, TABLE, SEARCH_URL, Prod2HttpClient, Prod2ProtocolError,
    parse_detail, parse_search,
)

EXPECTED = {'procedure_id': 1255251, 'convocatoria_id': 1046958,
            'entity': 'SYNTHETIC BUYER', 'nomenclature': 'TEST-2026-1', 'description': 'SOFTWARE'}


def search_html(end=1, total=1):
    native = f"addSubmitParam('{FORM}',{{'nidProceso':'1046958','ptoRetorno':'LOCAL'}})"
    process = "PrimeFaces.ab({params:[{name:'nidProceso',value:'1255251'}]})"
    values = ['1', EXPECTED['entity'], '05/10/2026', EXPECTED['nomenclature'], '', 'Servicio',
              EXPECTED['description'], '', '', '100', 'PEN', '3', '']
    columns = ''.join(f'<td>{v}</td>' for v in values)
    # Actions live in the existing first cell; preserve the native 13-column contract.
    columns = columns.replace('<td>1</td>', f'<td>1<a onclick="{native}">detail</a><a onclick="{process}">id</a></td>')
    return (f'<span>Mostrando de 1 a {end} del total {total} - Página: 1/1</span>'
            f'<table><tbody id="{TABLE}_data"><tr data-ri="0">{columns}</tr></tbody></table>'
            + paginator_html(total))


def paginator_html(total, size=15, page=1):
    return (f'<script>PrimeFaces.cw("DataTable","widget_tbBuscador_idFormBuscarProceso_dtProcesos",'
            f'{{id:"{TABLE}",paginator:{{rows:{size},rowCount:{total},page:{page-1},'
            'currentPageTemplate:"[ Mostrando de {startRecord} a {endRecord} ]"},'
            f'behaviors:{{page:function(a){{PrimeFaces.ab({{source:"{TABLE}",event:"page",'
            f'process:"{TABLE}"}},arguments[1])}}}}}})</script>')


def search_rows_html(first, length):
    result = []
    for index in range(first, first + length):
        native = f"addSubmitParam('{FORM}',{{'nidProceso':'{1046958 + index}','ptoRetorno':'LOCAL'}})"
        process = f"PrimeFaces.ab({{params:[{{name:'nidProceso',value:'{1255251 + index}'}}]}})"
        values = [str(index + 1), EXPECTED['entity'], '05/10/2026', f'TEST-2026-{index + 1}', '',
                  'Servicio', EXPECTED['description'], '', '', '100', 'PEN', '3', '']
        columns = ''.join(f'<td>{v}</td>' for v in values)
        columns = columns.replace(f'<td>{index + 1}</td>',
                                  f'<td>{index + 1}<a onclick="{native}">detail</a><a onclick="{process}">id</a></td>', 1)
        result.append(f'<tr data-ri="{index}">{columns}</tr>')
    return ''.join(result)


def paginated_search_html(total, size=15):
    last = min(total, size)
    pages = max(1, (total + size - 1) // size)
    return (f'<span>Mostrando de {1 if total else 0} a {last} del total {total} - Página: 1/{pages}</span>'
            f'<table><tbody id="{TABLE}_data">{search_rows_html(0, last)}</tbody></table>'
            + paginator_html(total, size))


def ajax_html(fragment, *, state='synthetic-new', update_id=TABLE):
    return (f'<partial-response><changes><update id="{update_id}"><![CDATA[{fragment}]]></update>'
            f'<update id="javax.faces.ViewState">{state}</update></changes></partial-response>')


def prepared_source(client):
    source = FORM + ':btnBuscarSel'
    html = (f'<form id="{FORM}"><input name="javax.faces.ViewState" value="synthetic-old"/>'
            '<select name="object"><option value="65">Servicio</option></select>'
            '<select name="year"><option value="2026">2026</option></select>'
            '<select name="version"><option value="3">Seace 3</option></select>'
            f'<button id="{source}" onclick="PrimeFaces.ab({{update:\'{TABLE}\'}})"></button></form>')
    source_client = Prod2HttpClient(client)
    source_client.form = Document(html).root.by_id(FORM)
    source_client.fields = {'javax.faces.ViewState': 'synthetic-old', FORM: FORM}
    return source_client


def detail_html():
    rows = ''.join(f'<tr><td>{label}</td><td>{EXPECTED[key]}</td></tr>' for label, key in
                   [('Nomenclatura:', 'nomenclature'), ('Entidad Convocante:', 'entity'),
                    ('Descripción del Objeto:', 'description')])
    return (f'<table>{rows}</table><table><tbody id="tbFicha:dtCronograma_data">'
            '<tr><td>Presentación</td><td>05/10/2026</td><td>06/10/2026</td></tr></tbody></table>'
            '<table><tbody id="tbFicha:dtDocumentos_data"></tbody></table>'
            '<script>DO_NOT_EXPORT_SCRIPT</script><style>DO_NOT_EXPORT_STYLE</style>'
            '<table id="bidders"><tr><td>Postor:</td><td>DO_NOT_EXPORT_BIDDER</td></tr></table>')


def test_identity_distinguishes_process_from_convocatoria():
    rows, actions, counts = parse_search(search_html())
    assert rows[0]['procedure_id'] == 1255251
    assert rows[0]['convocatoria_id'] == 1046958
    assert actions[0]['nidProceso'] == '1046958'
    assert counts == (1, 1, 1, 1, 1)


def test_counter_mismatch_is_rejected():
    with pytest.raises(Prod2ProtocolError, match='counter does not match'):
        parse_search(search_html(end=2, total=2))


@pytest.mark.parametrize('html', [search_html(total=0), search_html().replace('de 1 a', 'de 0 a'),
                                 search_html().replace('total 1', 'total 0')])
def test_rows_cannot_exceed_or_contradict_counter(html):
    with pytest.raises(Prod2ProtocolError, match='counter does not match'):
        parse_search(html)


def test_jsf_redirect_to_other_host_is_rejected_before_request():
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={'location': 'https://outside.example/ficha'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Prod2ProtocolError, match='outside the official'):
            Prod2HttpClient(client).bootstrap()
    assert calls == ['prod2.seace.gob.pe']


def test_native_http_redirect_is_requested_over_https():
    calls = []
    def handler(request):
        calls.append((request.method, request.url.scheme, request.url.path))
        if len(calls) == 1:
            return httpx.Response(302, headers={'location':
                'http://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml'})
        return httpx.Response(200, text='ficha')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Prod2HttpClient(client)._request('POST', SEARCH_URL, data={'synthetic': 'state'})
    assert result.status_code == 200
    assert calls[1] == ('GET', 'https', '/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml')


def test_document_pagination_is_reported_and_multicolumn_item_state_read():
    document = ('<tr><td>1</td><td>Convocatoria</td><td>Bases</td><td>'
                '<a onclick="descargaDocGeneral(\'efdf87fb-9e04-4fa7-9c34-5912db454f7f\',\'3\',\'test.pdf\')">PDF</a>'
                '</td><td>05/10/2026</td><td></td></tr>')
    html = detail_html().replace('<tbody id="tbFicha:dtDocumentos_data">',
                                '<tbody id="tbFicha:dtDocumentos_data">' + document)
    html += ('<script>PrimeFaces.cw("DataTable","widget_tbFicha_dtDocumentos",'
             '{id:"tbFicha:dtDocumentos",paginator:{rows:5,rowCount:2}})</script>'
             '<table><tr><td>Amount</td><td>100</td><td>Estado:</td><td>Adjudicado</td></tr></table>')
    result = parse_detail(html, EXPECTED)
    assert result['item_states'] == ['Adjudicado']
    assert result['document_inventory_total'] == 2
    assert result['document_inventory_complete'] is False
    assert parse_detail(html.replace('rowCount:2', 'rowCount:1'), EXPECTED)['document_inventory_complete'] is True


def test_empty_detail_is_rejected_even_with_http_200():
    with pytest.raises(Prod2ProtocolError, match='does not match'):
        parse_detail('<html><body>Empty ficha</body></html>', EXPECTED)


def test_snapshot_excludes_scripts_styles_and_bidder_tables():
    import json
    result = parse_detail(detail_html(), EXPECTED)
    assert 'DO_NOT_EXPORT' not in json.dumps(result)
    assert 'DO_NOT_EXPORT' not in Document(detail_html()).root.text().replace('DO_NOT_EXPORT_BIDDER', '')


def test_truncated_visible_description_uses_complete_native_dialog():
    html = detail_html().replace('<td>SOFTWARE</td>', '<td>SOFT...</td>')
    html += '<div id="tbFicha:dialogDescObj">Descripción del Objeto: SOFTWARE</div>'
    assert parse_detail(html, EXPECTED)['description'] == 'SOFTWARE'
    with pytest.raises(Prod2ProtocolError, match='does not match'):
        parse_detail(html.replace('Objeto: SOFTWARE', 'Objeto: WRONG'), EXPECTED)


@pytest.mark.parametrize('duplicate', [False, True])
def test_native_document_pagination_reconciles_inventory(duplicate):
    from uuid import UUID
    def row(number):
        code = str(UUID(int=number))
        return ('<tr><td>1</td><td>Convocatoria</td><td>Bases</td><td>'
                f'<a onclick="descargaDocGeneral(\'{code}\',\'3\',\'test.pdf\')">PDF</a>'
                '</td><td>05/10/2026</td><td></td></tr>')
    rows = ''.join(row(number) for number in range(1, 7))
    if duplicate:
        rows = rows.replace(str(UUID(int=6)), str(UUID(int=1)))
    payloads = []
    def handler(request):
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        payloads.append(data)
        return httpx.Response(200, text=(f'<partial-response><changes><update id="tbFicha:dtDocumentos">'
            f'<![CDATA[{rows}]]></update><update id="javax.faces.ViewState">new</update></changes></partial-response>'))
    initial = '<form id="tbFicha"><input name="javax.faces.ViewState" value="synthetic"/>'
    initial += '<table><tbody id="tbFicha:dtDocumentos_data">' + row(1) + '</tbody></table></form>'
    response = httpx.Response(200, text=initial, request=httpx.Request('GET',
        'https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml'))
    from buenapro_worker.prod2.client import parse_document_rows
    result = {'documents': parse_document_rows(Document(initial).root), 'document_inventory_total': 6,
              'document_inventory_complete': False}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = Prod2HttpClient(client)
        if duplicate:
            with pytest.raises(Prod2ProtocolError, match='Duplicate UUID'):
                source._complete_documents(response, result)
        else:
            source._complete_documents(response, result)
            assert result['document_inventory_complete'] and len(result['documents']) == 6
            assert payloads[0]['tbFicha:dtDocumentos_first'] == ['0']
            assert payloads[0]['tbFicha:dtDocumentos_rows'] == ['15']


def publication_row(code, *, stage='Convocatoria', kind='Bases', date='05/10/2026 10:00',
                    filename='test.pdf', version='3'):
    return ('<tr><td>1</td>' + f'<td>{stage}</td><td>{kind}</td><td>'
            f'<a onclick="descargaDocGeneral(\'{code}\',\'{version}\',\'{filename}\')">PDF</a>'
            f'</td><td>{date}</td><td></td></tr>')


def test_same_file_in_two_stages_preserves_two_publications_one_download_resource():
    from uuid import UUID
    code = str(UUID(int=1))
    rows = publication_row(code) + publication_row(code, stage='Integración de las Bases',
               kind='Bases Integradas', date='05/10/2026 10:06')
    html = detail_html().replace('<tbody id="tbFicha:dtDocumentos_data">',
                                '<tbody id="tbFicha:dtDocumentos_data">' + rows)
    html += ('<script>PrimeFaces.cw("DataTable","widget_tbFicha_dtDocumentos",'
             '{id:"tbFicha:dtDocumentos",paginator:{rows:5,rowCount:2}})</script>')
    result = parse_detail(html, EXPECTED)
    assert result['document_inventory_complete'] is True
    assert result['document_inventory_total'] == len(result['document_events']) == 2
    assert len(result['documents']) == 1
    assert result['documents'][0] == result['document_events'][0]
    assert [event['stage'] for event in result['document_events']] == ['Convocatoria', 'Integración de las Bases']


@pytest.mark.parametrize('change', [{'filename': 'other.pdf'}, {'version': '2'}])
def test_reused_uuid_conflicting_file_identity_is_rejected(change):
    from buenapro_worker.prod2.client import parse_document_rows
    from uuid import UUID
    code = str(UUID(int=1))
    rows = publication_row(code) + publication_row(code, stage='Integración', **change)
    with pytest.raises(Prod2ProtocolError, match='Conflicting filename or version'):
        parse_document_rows(Document('<table>' + rows + '</table>').root)


def test_multiple_download_actions_inside_one_publication_are_rejected():
    from buenapro_worker.prod2.client import parse_document_rows
    from uuid import UUID
    row = publication_row(str(UUID(int=1)))
    row = row.replace('PDF</a>', 'PDF</a><a onclick="descargaDocGeneral(\''
                      + str(UUID(int=2)) + '\',\'3\',\'other.pdf\')">PDF</a>')
    with pytest.raises(Prod2ProtocolError, match='Multiple download actions'):
        parse_document_rows(Document('<table>' + row + '</table>').root)


@pytest.mark.parametrize('mode', ['valid', 'duplicate', 'conflict', 'changed_initial', 'short_page'])
def test_publications_across_pages_reconcile_before_resource_deduplication(mode):
    from buenapro_worker.prod2.client import parse_document_rows
    from uuid import UUID
    first_rows = ''.join(publication_row(str(UUID(int=number))) for number in range(1, 16))
    final_row = publication_row(str(UUID(int=1)), stage='Integración', kind='Bases Integradas')
    if mode == 'duplicate':
        final_row = publication_row(str(UUID(int=1)))
    elif mode == 'conflict':
        final_row = publication_row(str(UUID(int=1)), stage='Integración', filename='conflict.pdf')
    elif mode == 'short_page':
        final_row = ''
    pages = [first_rows, final_row]
    if mode == 'changed_initial':
        pages[0] = first_rows.replace('05/10/2026 10:00', '05/10/2026 10:01', 1)
    payloads = []
    def handler(request):
        payload = parse_qs(request.content.decode(), keep_blank_values=True)
        payloads.append(payload)
        first = int(payload['tbFicha:dtDocumentos_first'][0])
        fragment = pages[first // 15]
        return httpx.Response(200, text=ajax_html(fragment, update_id='tbFicha:dtDocumentos'))
    initial_rows = ''.join(publication_row(str(UUID(int=number))) for number in range(1, 6))
    initial = ('<form id="tbFicha"><input name="javax.faces.ViewState" value="synthetic"/>'
               '<table><tbody id="tbFicha:dtDocumentos_data">' + initial_rows + '</tbody></table></form>')
    response = httpx.Response(200, text=initial, request=httpx.Request('GET',
        'https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml'))
    events = parse_document_rows(Document(initial).root)
    result = {'documents': events, 'document_events': events, 'document_inventory_total': 16,
              'document_inventory_complete': False}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if mode != 'valid':
            with pytest.raises(Prod2ProtocolError):
                Prod2HttpClient(client)._complete_documents(response, result)
            assert result['document_inventory_complete'] is False
        else:
            Prod2HttpClient(client)._complete_documents(response, result)
            assert result['document_inventory_complete'] is True
            assert len(result['document_events']) == 16
            assert len(result['documents']) == 15
            assert result['document_events'][0]['uuid'] == result['document_events'][-1]['uuid']
            assert [int(p['tbFicha:dtDocumentos_first'][0]) for p in payloads] == [0, 15]


def test_native_button_parameter_and_refreshed_viewstate_before_detail():
    payloads = []
    source = FORM + ':btnBuscarSel'
    form = (f'<form id="{FORM}"><input name="javax.faces.ViewState" value="synthetic-old"/>'
            '<select name="object"><option value="65">Servicio</option></select>'
            '<select name="year"><option value="2026">2026</option></select>'
            '<select name="version"><option value="3">Seace 3</option></select>'
            f'<button id="{source}" onclick="PrimeFaces.ab({{update:\'{TABLE}\'}})"></button></form>')
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, text=detail_html())
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        payloads.append(data)
        if len(payloads) == 1:
            assert data[source] == [source]
            assert data['javax.faces.source'] == [source]
            assert data['javax.faces.ViewState'] == ['synthetic-old']
            return httpx.Response(200, text=(f'<partial-response><changes>'
                f'<update id="{TABLE}"><![CDATA[{search_html()}]]></update>'
                '<update id="javax.faces.ViewState"><![CDATA[synthetic-new]]></update>'
                '</changes></partial-response>'))
        assert data['javax.faces.ViewState'] == ['synthetic-new']
        assert data['nidProceso'] == ['1046958']
        return httpx.Response(302, headers={'location':
            'https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml'})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        source_client = Prod2HttpClient(client)
        source_client.form = Document(form).root.by_id(FORM)
        source_client.fields = {'javax.faces.ViewState': 'synthetic-old'}
        source_client.search('SOFTWARE', year=2026, object_type='Servicio')
        result = source_client.detail(0)
    assert result['procedure_id'] == 1255251
    assert len(payloads) == 2


def test_search_pages_preserve_filters_and_bind_detail_to_current_row():
    payloads = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, text=detail_html().replace('TEST-2026-1', 'TEST-2026-31'))
        data = parse_qs(request.content.decode(), keep_blank_values=True)
        payloads.append(data)
        call = len(payloads)
        if call == 1:
            return httpx.Response(200, text=ajax_html(paginated_search_html(31), state='state-1'))
        if call in {2, 3}:
            assert data['javax.faces.source'] == [TABLE]
            assert data['javax.faces.partial.execute'] == [TABLE]
            assert data['javax.faces.partial.render'] == [TABLE]
            assert data['javax.faces.behavior.event'] == ['page']
            assert data['javax.faces.partial.event'] == ['page']
            assert data[TABLE + '_pagination'] == ['true']
            assert data[TABLE + '_encodeFeature'] == ['true']
            assert data[TABLE + '_rows'] == ['15']
            assert data[TABLE + '_first'] == [str((call - 1) * 15)]
            assert data['javax.faces.ViewState'] == [f'state-{call - 1}']
            assert data[FORM + ':descripcionObjeto'] == ['SOFTWARE']
            assert data[FORM + ':dfechaInicio_input'] == ['05/04/2026']
            assert data[FORM + ':dfechaFin_input'] == ['05/10/2026']
            assert data['year'] == ['2026'] and data['object'] == ['65']
            assert FORM + ':btnBuscarSel' not in data
            return httpx.Response(200, text=ajax_html(
                search_rows_html((call - 1) * 15, 15 if call == 2 else 1), state=f'state-{call}'))
        assert data['nidProceso'] == ['1046988']
        assert data['javax.faces.ViewState'] == ['state-3']
        return httpx.Response(302, headers={'location':
            'https://prod2.seace.gob.pe/seacebus-uiwd-pub/fichaSeleccion/fichaSeleccion.xhtml'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = prepared_source(client)
        first = source.search('SOFTWARE', year=2026, object_type='Servicio',
                              published_from='05/04/2026', published_to='05/10/2026')
        assert first['counter_origin'] == 'native_search_counter'
        assert first['snapshot']['year_filter_kind'] == 'convocatoria_year'
        assert first['snapshot']['page_size'] == 15
        second = source.search_page(2)
        assert second['counter'] == [16, 30, 31, 2, 3]
        assert second['counter_origin'] == 'initial_native_total_and_observed_indices'
        assert source.search_page(2)['counter'] == second['counter']  # No duplicate HTTP request.
        last = source.search_page(3)
        assert last['counter'] == [31, 31, 31, 3, 3]
        assert last['coverage_complete'] is False
        assert last['snapshot']['native_atomic_snapshot'] is False
        assert source.detail(0)['convocatoria_id'] == 1046988
    assert len(payloads) == 4


@pytest.mark.parametrize('kind', ['malformed_xml', 'missing_update', 'missing_state', 'short_page',
                                  'wrong_index', 'duplicate_index', 'duplicate_identity',
                                  'duplicate_previous_page', 'changed_total', 'changed_page_size',
                                  'changed_text_counter', 'duplicate_table_update'])
def test_page_failures_invalidate_prior_row_actions(kind):
    fragment = search_rows_html(15, 15)
    if kind == 'short_page':
        fragment = search_rows_html(15, 14)
    elif kind == 'wrong_index':
        fragment = fragment.replace('data-ri="15"', 'data-ri="wrong"', 1)
    elif kind == 'duplicate_index':
        fragment = fragment.replace('data-ri="16"', 'data-ri="15"', 1)
    elif kind == 'duplicate_identity':
        fragment = fragment.replace("'nidProceso':'1046974'", "'nidProceso':'1046973'", 1)
    elif kind == 'duplicate_previous_page':
        fragment = fragment.replace("'nidProceso':'1046973'", "'nidProceso':'1046958'", 1)
    elif kind == 'changed_total':
        fragment += paginator_html(32, page=2)
    elif kind == 'changed_page_size':
        fragment += paginator_html(31, size=10, page=2)
    elif kind == 'changed_text_counter':
        fragment += '<span>Mostrando de 16 a 30 del total 32 - Página: 2/3</span>'
    response_html = ajax_html(fragment)
    if kind == 'malformed_xml':
        response_html = '<html>Not a JSF response</html>'
    elif kind == 'missing_update':
        response_html = ajax_html(fragment, update_id=FORM + ':unrelated')
    elif kind == 'missing_state':
        response_html = ajax_html(fragment, state='')
    elif kind == 'duplicate_table_update':
        response_html = response_html.replace('</changes>',
            f'<update id="{TABLE}"><![CDATA[{fragment}]]></update></changes>')
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=ajax_html(paginated_search_html(31)) if len(calls) == 1 else response_html)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = prepared_source(client)
        source.search('SOFTWARE', year=2026, object_type='Servicio')
        with pytest.raises(Prod2ProtocolError):
            source.search_page(2)
        assert source.rows == [] and source.actions == [] and source.search_snapshot is None
        with pytest.raises(ValueError, match='outside this session'):
            source.detail(0)
        with pytest.raises(Prod2ProtocolError, match='verified search is required'):
            source.search_page(2)
    assert len(calls) == 2


@pytest.mark.parametrize('number', [0, -1, 4, 1.5, True, '2'])
def test_page_bounds_are_checked_before_transport(number):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=ajax_html(paginated_search_html(31)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = prepared_source(client)
        source.search('SOFTWARE', year=2026, object_type='Servicio')
        with pytest.raises(ValueError, match='verified page bounds'):
            source.search_page(number)
    assert len(calls) == 1


@pytest.mark.parametrize('bad', ['missing_config', 'rowcount', 'pages', 'size', 'behavior'])
def test_initial_search_geometry_must_reconcile(bad):
    html = paginated_search_html(31)
    if bad == 'missing_config':
        html = html[:html.index('<script>')]
    elif bad == 'rowcount':
        html = html.replace('rowCount:31', 'rowCount:32')
    elif bad == 'pages':
        html = html.replace('Página: 1/3', 'Página: 1/2')
    elif bad == 'size':
        html = html.replace('rows:15', 'rows:0')
    elif bad == 'behavior':
        html = html.replace('event:"page"', 'event:"unknown"')
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ajax_html(html)))) as client:
        source = prepared_source(client)
        with pytest.raises(Prod2ProtocolError):
            source.search('SOFTWARE', year=2026, object_type='Servicio')
        assert source.rows == [] and source.search_snapshot is None


def test_explicit_empty_keyword_preflight_accepts_verified_zero_only():
    payloads = []
    def handler(request):
        payloads.append(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(200, text=ajax_html(paginated_search_html(0)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        source = prepared_source(client)
        with pytest.raises(ValueError, match='nonempty'):
            source.search('', year=2026, object_type='Servicio')
        with pytest.raises(ValueError, match='bounded publication dates'):
            source.search('', year=2026, object_type='Servicio', allow_empty_keyword=True)
        result = source.search('', year=2026, object_type='Servicio', allow_empty_keyword=True,
                               published_from='05/04/2026', published_to='05/10/2026')
        assert result['counter'] == [0, 0, 0, 1, 1] and result['rows'] == []
        assert payloads[0][FORM + ':descripcionObjeto'] == ['']
        assert result['search_verified'] and result['coverage_complete'] is False
        with pytest.raises(ValueError, match='page bounds'):
            source.search_page(2)
    assert len(payloads) == 1


def test_search_rejects_unverified_zero_and_missing_row_indices():
    for html in [paginated_search_html(0).replace('Página: 1/1', 'Página: 1/0'),
                 search_html().replace('data-ri="0"', ''),
                 paginated_search_html(0).replace('rowCount:0', 'rowCount:1')]:
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=ajax_html(html)))) as client:
            with pytest.raises(Prod2ProtocolError):
                prepared_source(client).search('SOFTWARE', year=2026, object_type='Servicio')


@pytest.mark.parametrize('options,expected', [
    ('<option value="">[Seleccione]</option><option value="2026">2026</option>'
     '<option value="2025">2025</option><option value="2004">2004</option>', [2026, 2025, 2004]),
    ('<option value="2024">2024</option><option value="2022">2022</option>', [2024, 2022]),
])
def test_available_years_use_observed_options_without_filling_gaps(options, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No transport expected'))) as client:
        source = Prod2HttpClient(client)
        source.form = Document(f'<form><select name="{FORM}:anioConvocatoria_input">{options}</select></form>').root
        assert source.available_convocation_years() == expected


@pytest.mark.parametrize('selects', [
    '',
    f'<select name="{FORM}:anioConvocatoria_input"><option value="">[Seleccione]</option></select>',
    f'<select name="{FORM}:anioConvocatoria_input"><option value="2026">2026</option>'
    '<option value="2026">2026</option></select>',
    f'<select name="{FORM}:anioConvocatoria_input"><option value="all">Todos</option></select>',
    f'<select name="{FORM}:anioConvocatoria_input"><option value="2026">2025</option></select>',
    f'<select name="{FORM}:anioConvocatoria_input"><option>2026</option></select>',
    f'<select name="{FORM}:anioConvocatoria_input"><option value="2026">2026</option></select>' * 2,
])
def test_available_years_reject_missing_duplicate_or_unexpected_options(selects):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No transport expected'))) as client:
        source = Prod2HttpClient(client)
        source.form = Document(f'<form>{selects}</form>').root
        with pytest.raises(Prod2ProtocolError, match='year'):
            source.available_convocation_years()


def test_nested_native_row_controls_do_not_become_search_results():
    html = search_html().replace('<td>100</td>',
                                '<td>100<table><tr><td>nested control</td></tr></table></td>')
    rows, actions, counter = parse_search(html)
    assert len(rows) == len(actions) == 1 and counter == (1, 1, 1, 1, 1)


def test_ambiguous_counter_and_missing_initial_viewstate_are_rejected():
    with pytest.raises(Prod2ProtocolError, match='counter missing or ambiguous'):
        parse_search(search_html() + '<span>Mostrando de 0 a 0 del total 0 - Página: 1/1</span>')
    html = f'<partial-response><changes><update id="{TABLE}"><![CDATA[{search_html()}]]></update></changes></partial-response>'
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
        with pytest.raises(Prod2ProtocolError, match='ViewState update missing'):
            prepared_source(client).search('SOFTWARE', year=2026, object_type='Servicio')
