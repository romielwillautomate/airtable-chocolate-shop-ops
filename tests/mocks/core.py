"""The mock HTTP server: one port, every mocked service, a shared request log, failure injection and a clock.

Routing is by path, so a workflow under test only needs its API hosts swapped for the mock URL:

  /v0/meta/...                          Airtable Metadata API
  /v0/{baseId}/...                      Airtable Web API
  /admin/api/{version}/graphql.json     Shopify Admin GraphQL
  /v1/search                            Open-Meteo geocoding (geocoding-api.open-meteo.com)
  /v1/forecast                          Open-Meteo forecast (api.open-meteo.com)
  /email, /email/batch                  Postmark
  /services/..., /slack/...             Slack incoming webhooks
  /v1/messages, /v1/messages/count_tokens   Anthropic Messages
  /__mock/...                           control endpoints (log, reset, fail, clock) for out of process use
"""
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = REPO_ROOT / 'base' / 'schema.json'
DEFAULT_SEED = REPO_ROOT / 'base' / 'seed'


# ---------------------------------------------------------------------------------------------- clock

def parse_iso(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if len(text) == 10:
        text += 'T00:00:00'
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_ms(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


class Clock:
    """The time the mocks believe it is. Live (real UTC time) unless set.

    clock.set('2026-10-20T09:00:00Z') freezes it, clock.advance(days=1) moves it, clock.live() goes back to real time.
    Airtable TODAY() and NOW(), record createdTime, Open-Meteo forecast dates and Shopify timestamps all use it.
    """

    def __init__(self, now=None):
        self._fixed = parse_iso(now) if now else None
        self._offset = timedelta(0)

    def now(self):
        if self._fixed is not None:
            return self._fixed
        return datetime.now(timezone.utc) + self._offset

    def set(self, value):
        self._fixed = parse_iso(value)
        return self._fixed

    def advance(self, **delta):
        if self._fixed is not None:
            self._fixed += timedelta(**delta)
        else:
            self._offset += timedelta(**delta)
        return self.now()

    def live(self):
        self._fixed, self._offset = None, timedelta(0)

    def today(self):
        return self.now().date()

    def iso(self):
        return iso_ms(self.now())


# ---------------------------------------------------------------------------------------------- request / response

class Request:
    def __init__(self, method, target, headers, raw):
        parts = urlsplit(target)
        self.method = method
        self.target = target
        self.path = parts.path
        self.query_string = parts.query
        self.query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        self.query = {}
        for k, v in self.query_pairs:
            self.query.setdefault(k, []).append(v)
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.raw = raw
        self.json = None
        self.json_error = None
        if raw:
            try:
                self.json = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as e:
                self.json_error = str(e)

    def text(self):
        try:
            return self.raw.decode('utf-8')
        except UnicodeDecodeError:
            return ''

    def q(self, name, default=None):
        values = self.query.get(name)
        return values[-1] if values else default


class Response:
    def __init__(self, status, body=None, headers=None, content_type=None):
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        if content_type:
            self.content_type = content_type
        elif isinstance(body, (dict, list)):
            self.content_type = 'application/json; charset=utf-8'
        else:
            self.content_type = 'text/plain; charset=utf-8'

    def payload(self):
        if self.body is None:
            return b''
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, (dict, list)):
            return json.dumps(self.body).encode('utf-8')
        return str(self.body).encode('utf-8')


def json_response(status, body, headers=None):
    return Response(status, body, headers)


# ---------------------------------------------------------------------------------------------- failure injection

class Failure:
    def __init__(self, service, status=500, body=None, times=1, delay=None, drop=False, match=None, headers=None):
        self.service = service
        self.status = status
        self.body = body
        self.remaining = times
        self.delay = delay
        self.drop = drop
        self.match = match
        self.headers = headers or {}
        self.hits = 0

    def applies(self, service, entry):
        if self.remaining == 0 or service != self.service:
            return False
        if self.match is None:
            return True
        if callable(self.match):
            return bool(self.match(entry))
        for key, want in self.match.items():
            if key == 'path_contains':
                if want not in entry['path']:
                    return False
            elif key == 'body_contains':
                if want not in json.dumps(entry.get('body')):
                    return False
            elif entry.get(key) != want:
                return False
        return True


# ---------------------------------------------------------------------------------------------- server

class MockServer:
    """All mocks on one port.

    mock = MockServer(port=8790)        # loads base/schema.json and base/seed, clock live
    mock.start()
    ... point n8n at mock.url (http://host.docker.internal:8790 by default) ...
    mock.stop()

    seed_today='auto' (the default) shifts every seed date so the seed's "today" (base/seed/_meta.json seedToday)
    becomes the mock clock's UTC date. Relative facts ("best before in 15 days", "delivered 2 days ago") then hold
    on any day, and they agree with n8n's own clock. Pass seed_today=None to load the dates exactly as written.
    """

    def __init__(self, port, host='127.0.0.1', public_host=None, schema=DEFAULT_SCHEMA, seed=DEFAULT_SEED,
                 seed_today='auto', empty_base=False, now=None):
        from .airtable import AirtableMock
        from .anthropic import AnthropicMock
        from .open_meteo import OpenMeteoMock
        from .postmark import PostmarkMock
        from .shopify import ShopifyMock
        from .slack import SlackMock

        self.port = port
        self.host = host
        self.public_host = public_host or os.environ.get('MOCK_HOST', 'host.docker.internal')
        self.clock = Clock(now)
        self.lock = threading.RLock()
        self.entries = []
        self.failures = []
        self.lost_answers = []
        self._seq = 0
        self._httpd = None
        self._thread = None
        self._schema_path, self._seed_path, self._seed_today, self._empty = schema, seed, seed_today, empty_base

        self.airtable = AirtableMock(self)
        self.shopify = ShopifyMock(self)
        self.meteo = OpenMeteoMock(self)
        self.postmark = PostmarkMock(self)
        self.slack = SlackMock(self)
        self.anthropic = AnthropicMock(self)
        self.services = [self.airtable, self.shopify, self.meteo, self.postmark, self.slack, self.anthropic]
        self._load()

    # ------------------------------------------------------------------ lifecycle

    def _load(self):
        if not self._empty and self._schema_path:
            self.airtable.load_schema(self._schema_path)
            if self._seed_path:
                self.airtable.load_seed(self._seed_path, today=self._seed_today)
        self.shopify.load_store(self._seed_path, today=self._seed_today)

    def reset(self, empty_base=None, seed_today=False):
        """Back to the freshly seeded state: data, logs, failures, scripts. The clock is kept, and with the default
        seed_today='auto' the seed is shifted to the clock's current date again. seed_today: a date string, 'auto'
        or None (no shift) to change the setting; leave it out to keep the current one."""
        with self.lock:
            if empty_base is not None:
                self._empty = empty_base
            if seed_today is not False:
                self._seed_today = seed_today
            self.entries.clear()
            self.failures.clear()
            self.lost_answers.clear()
            for s in self.services:
                s.reset()
            self._load()

    @property
    def seed_shift_days(self):
        """How many days the seed dates were moved when they were loaded (0 when seed_today=None)."""
        return getattr(self.airtable, 'seed_shift_days', 0)

    def seed_date(self, value):
        """A date or datetime string from base/seed or _meta.json, moved by the same number of days as the loaded seed.
        seed_date('2026-11-04') -> '2026-11-05' when the tests run one day after seedToday."""
        if len(str(value)) == 10:
            from datetime import date as _date
            return (_date.fromisoformat(str(value)) + timedelta(days=self.seed_shift_days)).isoformat()
        return iso_ms(parse_iso(value) + timedelta(days=self.seed_shift_days))

    @property
    def url(self):
        """The URL n8n (in Docker) uses to reach the mock."""
        return f'http://{self.public_host}:{self.port}'

    @property
    def local_url(self):
        """The URL this machine uses (tests, setup/create_base.py)."""
        return f'http://127.0.0.1:{self.port}'

    def start(self):
        server = self

        class Handler(_Handler):
            mock = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ request log

    def mark(self):
        """Position in the request log. Pass it to log(since=...) to see only newer requests."""
        with self.lock:
            return len(self.entries)

    def log(self, service=None, since=0, **match):
        """Logged requests, oldest first. Filters: service, since (a mark), and any entry key, e.g.
        log('airtable', method='PATCH', table='Orders', operation='upsert')."""
        with self.lock:
            out = []
            for e in self.entries[since:]:
                if service and e['service'] != service:
                    continue
                if all(e.get(k) == v for k, v in match.items()):
                    out.append(e)
            return out

    # ------------------------------------------------------------------ failures

    def fail(self, service, status=500, times=1, body=None, delay=None, drop=False, match=None, headers=None):
        """Make the next `times` matching requests to `service` fail.

        status   HTTP status to return (the body defaults to that service's real error shape)
        delay    seconds to wait before answering (use with a node timeout to simulate a timeout);
                 with status=None the normal answer is sent after the delay
        drop     close the connection without any answer
        match    dict of log entry keys (method, path, table, operation, path_contains, body_contains)
                 or a callable(entry) -> bool
        times    how many requests to fail; -1 means every matching request until clear_failures()
        """
        f = Failure(service, status, body, times, delay, drop, match, headers)
        with self.lock:
            self.failures.append(f)
        return f

    def clear_failures(self):
        with self.lock:
            self.failures.clear()
            self.lost_answers.clear()

    def lose_answer(self, service, match=None, times=1, status=None):
        """Let the next `times` matching requests to `service` be handled for real (the change is applied), then
        lose the answer, as after a timeout or a proxy error. The workflow never learns that its write worked.

        status   None closes the connection with no answer; a number sends that status with the service's
                 real error body instead of the real answer (for example 500)
        match    like fail(): a dict of log entry keys or a callable(entry) -> bool. It is only asked about
                 requests the service answered with a 2xx, because only those changed anything
        times    how many answers to lose; -1 means every matching one until clear_failures() or reset()

        The log entry gets answer_lost: True, status (what the client got: None when dropped) and
        handled_status (what the service answered)."""
        rule = {'service': service, 'match': match, 'left': times, 'status': status, 'hits': 0}
        with self.lock:
            self.lost_answers.append(rule)
        return rule

    def _take_lost_answer(self, service, entry, resp):
        if resp is None or not 200 <= resp.status < 300:
            return None
        with self.lock:
            for rule in self.lost_answers:
                if rule['left'] == 0 or rule['service'] != service:
                    continue
                if rule['match'] is not None and not Failure(service, match=rule['match']).applies(service, entry):
                    continue
                if rule['left'] > 0:
                    rule['left'] -= 1
                rule['hits'] += 1
                return rule
        return None

    def _take_failure(self, service, entry):
        with self.lock:
            for f in self.failures:
                if f.applies(service, entry):
                    if f.remaining > 0:
                        f.remaining -= 1
                    f.hits += 1
                    return f
        return None

    # ------------------------------------------------------------------ dispatch

    def _service_for(self, path):
        if path.startswith('/__mock/'):
            return 'control'
        for s in self.services:
            name = s.route(path)
            if name:
                return name, s
        return None

    def dispatch(self, req):
        """Handle one request. Returns (Response or None to drop, entry)."""
        with self.lock:
            self._seq += 1
            seq = self._seq
        entry = {'seq': seq, 'time': self.clock.iso(), 'method': req.method, 'path': req.path, 'query': req.query,
                 'headers': req.headers, 'body': req.json if req.json is not None else req.text(), 'raw': req.raw}
        routed = self._service_for(req.path)
        if routed == 'control':
            entry['service'] = 'control'
            return self._control(req), None
        if routed is None:
            entry['service'] = 'unknown'
            resp = json_response(404, {'error': 'NOT_MOCKED', 'message': f'No mock for {req.method} {req.path}'})
            self._record(entry, resp)
            return resp, entry
        name, svc = routed
        entry['service'] = name
        try:
            svc.annotate(req, entry)
        except Exception:  # annotation is best effort
            pass
        failure = self._take_failure(name, entry)
        if failure is not None:
            entry['injected'] = True
            if failure.delay:
                time.sleep(failure.delay)
            if failure.drop:
                entry['status'] = None
                entry['response'] = 'dropped'
                with self.lock:
                    self.entries.append(entry)
                return None, entry
            if failure.status is not None:
                body = failure.body if failure.body is not None else svc.error_body(failure.status)
                resp = Response(failure.status, body, failure.headers)
                self._record(entry, resp)
                return resp, entry
        with self.lock:
            try:
                resp = svc.handle(req, entry)
            except Exception as e:  # a bug in a mock must be loud, not a silent 200
                import traceback
                traceback.print_exc()
                resp = json_response(500, {'mock_error': repr(e)})
                entry['mock_crash'] = repr(e)
            self._record(entry, resp)
            lost = self._take_lost_answer(name, entry, resp)
            if lost is not None:
                entry['answer_lost'] = True
                entry['handled_status'] = resp.status
                entry['status'] = lost['status']
                if lost['status'] is None:
                    return None, entry
                return Response(lost['status'], svc.error_body(lost['status'])), entry
        return resp, entry

    def _record(self, entry, resp):
        entry['status'] = resp.status
        entry['response'] = resp.body
        with self.lock:
            self.entries.append(entry)

    # ------------------------------------------------------------------ control endpoints (out of process use)

    def _control(self, req):
        path = req.path[len('/__mock'):]
        body = req.json or {}
        if path == '/health':
            return json_response(200, {'ok': True, 'port': self.port})
        if path == '/log':
            since = int(req.q('since', 0))
            entries = self.log(req.q('service'), since=since)
            return json_response(200, {'entries': [_jsonable(e) for e in entries]})
        if path == '/reset' and req.method == 'POST':
            self.reset(empty_base=body.get('empty_base'), seed_today=body.get('seed_today', False))
            return json_response(200, {'ok': True})
        if path == '/fail' and req.method == 'POST':
            self.fail(body['service'], status=body.get('status', 500), times=body.get('times', 1), body=body.get('body'),
                      delay=body.get('delay'), drop=body.get('drop', False), match=body.get('match'))
            return json_response(200, {'ok': True})
        if path == '/clock' and req.method == 'POST':
            if body.get('live'):
                self.clock.live()
            else:
                self.clock.set(body['now'])
            return json_response(200, {'now': self.clock.iso()})
        if path.startswith('/airtable/'):
            table = path[len('/airtable/'):]
            from urllib.parse import unquote
            return json_response(200, {'records': self.airtable.records(unquote(table))})
        return json_response(404, {'error': 'unknown control endpoint'})


def _jsonable(entry):
    e = dict(entry)
    raw = e.pop('raw', b'')
    e['raw_length'] = len(raw or b'')
    return e


class _Handler(BaseHTTPRequestHandler):
    mock = None
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass

    def _read_body(self):
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            chunks = []
            while True:
                size = int(self.rfile.readline().strip() or b'0', 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b''.join(chunks)
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''

    def _handle(self):
        raw = self._read_body()
        req = Request(self.command, self.path, dict(self.headers.items()), raw)
        resp, _ = self.mock.dispatch(req)
        if resp is None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.close_connection = True
            return
        data = resp.payload()
        try:
            self.send_response(resp.status)
            self.send_header('Content-Type', resp.content_type)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
        except (ConnectionError, OSError):
            # the client gave up first (a timeout test, or n8n cancelling a request); nothing left to answer
            self.close_connection = True

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = _handle
