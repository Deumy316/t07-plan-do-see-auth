"""Existing functional tests authenticate through real routes; CSRF stays enabled."""
import re
import secrets
from auth import COOKIE

_sessions = {}

def csrf(client, path='/'):
    response = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    if not match:
        raise AssertionError('CSRF form not found')
    return match.group(1).decode()

def authenticated_client(app):
    client = app.test_client()
    key = app.config['DATABASE']
    if key not in _sessions:
        password = secrets.token_urlsafe(24)
        token = csrf(client, '/signup')
        response = client.post('/signup', data=dict(username='test_user', password=password, csrf_token=token))
        assert response.status_code == 303
        response = client.post('/login', data=dict(username='test_user', password=password, csrf_token=token))
        assert response.status_code == 303
        _sessions[key] = client.get_cookie(COOKIE).value
    else:
        client.set_cookie(COOKIE, _sessions[key])
    post = client.post
    def protected_post(*args, **kwargs):
        data = dict(kwargs.pop('data', {}) or {})
        data.setdefault('csrf_token', csrf(client))
        return post(*args, data=data, **kwargs)
    client.post = protected_post
    return client
