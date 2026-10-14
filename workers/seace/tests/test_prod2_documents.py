import json
import stat

import httpx
import pytest

from buenapro_worker.prod2.documents import DocumentDownloadError, download_document

CODE = 'efdf87fb-9e04-4fa7-9c34-5912db454f7f'


@pytest.mark.parametrize('jsonp', [True, False])
def test_download_context_and_private_manifest(tmp_path, jsonp):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            data = json.dumps({'result': '200', 'downloadUrl': '/service/content?alf_ticket=PRIVATE'})
            return httpx.Response(200, text=f'buenaproDocument({data});' if jsonp else data)
        assert request.url.path == '/alfresco/service/content'
        return httpx.Response(200, content=b'%PDF-test', headers={'content-type': 'application/pdf'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        destination = tmp_path / 'document.pdf'
        manifest = download_document(client, CODE, destination)
    assert manifest['bytes'] == 9
    assert 'PRIVATE' not in json.dumps(manifest)
    assert 'alf_ticket' not in json.dumps(manifest)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize('reference', ['https://evil.example/file', 'http://alfprod.seace.gob.pe/file', '//evil.example/file'])
def test_rejects_untrusted_download_host(tmp_path, reference):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'result': '200', 'downloadUrl': reference})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, CODE, tmp_path / 'file.pdf')
    assert len(calls) == 1
    assert not (tmp_path / 'file.pdf').exists()


def test_rejects_redirect_before_request(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={'downloadUrl': '/service/content'})
        return httpx.Response(302, headers={'location': 'https://evil.example/?alf_ticket=PRIVATE'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DocumentDownloadError) as error:
            download_document(client, CODE, tmp_path / 'file.pdf')
    assert 'PRIVATE' not in str(error.value)
    assert len(calls) == 2


@pytest.mark.parametrize('content,limit', [(b'<html>failure</html>', 100), (b'%PDF-' + b'x' * 100, 20)])
def test_rejects_invalid_or_oversized_without_saving(tmp_path, content, limit):
    def handler(request):
        if request.url.path.endswith('downloadDoc'):
            return httpx.Response(200, json={'downloadUrl': '/service/content'})
        return httpx.Response(200, content=content)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, CODE, tmp_path / 'file.pdf', max_bytes=limit)
    assert not (tmp_path / 'file.pdf').exists()


@pytest.mark.parametrize('expected_format,content', [('zip', b'PK\x03\x04test'), ('pdf', b'%PDF-test'),
                                                    ('7z', b'7z\xbc\xaf\x27\x1ctest')])
def test_expected_format_magic(tmp_path, expected_format, content):
    def handler(request):
        if request.url.path.endswith('downloadDoc'):
            return httpx.Response(200, json={'downloadUrl': '/service/content'})
        return httpx.Response(200, content=content)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manifest = download_document(client, CODE, tmp_path / 'file', expected_format=expected_format)
    assert manifest['format'] == expected_format
    assert (tmp_path / 'file').read_bytes() == content


@pytest.mark.parametrize('expected_format', ['zip', '7z'])
def test_zip_expected_rejects_pdf(tmp_path, expected_format):
    def handler(request):
        if request.url.path.endswith('downloadDoc'):
            return httpx.Response(200, json={'downloadUrl': '/service/content'})
        return httpx.Response(200, content=b'%PDF-test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, CODE, tmp_path / 'file', expected_format=expected_format)
    assert not (tmp_path / 'file').exists()
