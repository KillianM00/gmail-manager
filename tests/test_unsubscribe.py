import base64
import socket

import pytest

from gmail_mgr.unsubscribe import extract_body, find_body_unsubscribe_links, is_safe_unsub_url, parse_list_unsubscribe


def test_list_unsubscribe_retains_both_supported_methods():
    assert parse_list_unsubscribe('<mailto:leave@example.com?subject=stop>, <https://example.com/unsubscribe>') == {
        'mailto': ['leave@example.com?subject=stop'], 'http': ['https://example.com/unsubscribe']}
    assert parse_list_unsubscribe('') == {'mailto': [], 'http': []}


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://localhost/unsub', 'http://127.0.0.1', 'http://169.254.169.254', 'http://[::1]', 'https://192.168.1.1', 'not a URL'])
def test_unsubscribe_rejects_local_and_non_http_destinations(url):
    assert not is_safe_unsub_url(url)


def test_unsubscribe_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))])
    assert not is_safe_unsub_url('https://example.com/unsubscribe')


def test_unsubscribe_accepts_public_destination(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 0))])
    assert is_safe_unsub_url('https://example.com/unsubscribe')


def test_nested_body_extracts_unpadded_gmail_parts():
    def body(text): return {'data': base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}
    assert extract_body({'parts': [{'mimeType': 'text/plain', 'body': body('plain')},
        {'parts': [{'mimeType': 'text/html', 'body': body('<p>html</p>')}]}]}) == ('plain', '<p>html</p>')


def test_body_links_are_deduplicated_and_navigation_is_ignored():
    html = '<a href="https://example.com">Home</a><a href="https://example.com/leave">Unsubscribe</a>'
    assert find_body_unsubscribe_links('https://example.com/unsubscribe', html) == [
        'https://example.com/leave', 'https://example.com/unsubscribe']
