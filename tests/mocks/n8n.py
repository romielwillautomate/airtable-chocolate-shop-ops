"""Helpers for the end to end tests: talk to the n8n public API, import a temporary copy of a workflow that points
at the mocks (plus a temporary, ACTIVE copy of shared/error-alert-slack.json as its error workflow), wait for runs,
read what each node produced, match error alerts to runs, and print PASS/FAIL like the other portfolio repos.

    from tests.mocks import MockServer
    from tests.mocks.n8n import N8n, prepare_workflow, mock_env, Report

    mock = MockServer(port=8791).start()
    n8n = N8n()                                    # N8N_URL, N8N_API_KEY (or N8N_ENV_FILE) from the environment
    n8n.cleanup('shopify-orders')                  # deletes leftovers named exactly "TEST shopify-orders (temporary)"
    wf = prepare_workflow(PATH, mock, slug='shopify-orders', env=mock_env(mock, MELT_TEMP_C='27'))
    with n8n.temporary(wf, slug='shopify-orders', mock=mock) as run:
        status, body = run.post_shopify('orders', 'orders/create', payload)   # POST /webhook/shopify-orders-e2e-orders
        ex = run.wait()                                                      # the finished execution, with data
        run.ran(ex, 'Airtable: Upsert Orders')
        alert = run.alert_for(ex)                                            # the error workflow's Slack text, or None
"""
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ERROR_WORKFLOW = REPO_ROOT / 'shared' / 'error-alert-slack.json'
ERROR_SLACK_PATH = '/services/T000MOCK/B000MOCK/errors'
ALERT_TIMEOUT = 90       # seconds Run.alert_for waits by default; a busy n8n can start the error workflow late

REAL_HOSTS = ('https://api.airtable.com', 'https://api.anthropic.com', 'https://api.postmarkapp.com',
              'https://geocoding-api.open-meteo.com', 'https://api.open-meteo.com', 'https://hooks.slack.com')
HOST_ENV_VARS = ('SHOPIFY_STORE_DOMAIN',)
# anything still pointing at a real API after prepare_workflow is a bug in the workflow under test
REAL_API_URL = re.compile(r'https?://(?:api\.airtable\.com|[A-Za-z0-9-]+\.myshopify\.com|api\.anthropic\.com|'
                          r'api\.postmarkapp\.com|(?:geocoding-)?api\.open-meteo\.com|hooks\.slack\.com)', re.I)
# a host that is built at run time ('https://' + host, `https://${host}`, https://{{ $json.host }}) cannot be pointed
# at the mock, so a test could reach a real store. Only https://{{ $env.SHOPIFY_STORE_DOMAIN }} is rewritten.
BUILT_HOST = re.compile(r'https?://(?:\s*["\'`]\s*\+|\$\{|\{\{)', re.I)
SETTINGS_KEYS = ('executionOrder', 'timezone', 'saveDataErrorExecution', 'saveDataSuccessExecution',
                 'saveManualExecutions', 'saveExecutionProgress', 'executionTimeout', 'errorWorkflow', 'callerPolicy')


def load_env_file(path):
    """Read KEY=VALUE lines (for N8N_API_KEY in an n8n project .env). Values are never printed."""
    out = {}
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def mock_env(mock, **overrides):
    """Environment values that point a workflow at the mock server (fake keys, mock URLs).
    Every value is a string, the way n8n's $env delivers it."""
    from .airtable import BASE_ID, TOKEN
    from .anthropic import API_KEY
    from .postmark import SERVER_TOKEN
    from .shopify import ACCESS_TOKEN, DEFAULT_VERSION, SHOP_DOMAIN, WEBHOOK_SECRET
    env = {
        'AIRTABLE_TOKEN': TOKEN, 'AIRTABLE_BASE_ID': BASE_ID,
        'SHOPIFY_STORE_DOMAIN': SHOP_DOMAIN, 'SHOPIFY_ADMIN_TOKEN': ACCESS_TOKEN, 'SHOPIFY_API_VERSION': DEFAULT_VERSION,
        'SHOPIFY_WEBHOOK_SECRET': WEBHOOK_SECRET, 'SHOPIFY_LOCATION_ID': mock.shopify.primary_location,
        'ANTHROPIC_API_KEY': API_KEY, 'ANTHROPIC_MODEL': 'claude-sonnet-5',
        'POSTMARK_SERVER_TOKEN': SERVER_TOKEN, 'POSTMARK_MESSAGE_STREAM': 'outbound',
        'EMAIL_FROM_SUPPORT': 'Demo Chocolate Support <support@example.com>',
        'EMAIL_FROM_WHOLESALE': 'Demo Chocolate Wholesale <wholesale@example.com>',
        'SLACK_WEBHOOK_URL': mock.url + '/services/T000MOCK/B000MOCK/alerts',
        'SLACK_PACKING_WEBHOOK_URL': mock.url + '/services/T000MOCK/B000MOCK/packing',
        'SLACK_SUPPORT_WEBHOOK_URL': mock.url + '/services/T000MOCK/B000MOCK/support',
        'RUN_NOW_SECRET': 'mock-run-now-secret', 'WHOLESALE_FORM_SECRET': 'mock-wholesale-form-secret',
        'SUPPORT_FORM_SECRET': 'mock-support-form-secret',
        'MELT_TEMP_C': '27', 'MELT_FORECAST_DAYS': '3', 'SELL_BY_BUFFER_DAYS': '14', 'PROMO_WINDOW_DAYS': '45',
        'PROMO_DISCOUNT_PERCENT': '25', 'QUOTE_VALID_DAYS': '14', 'REPEAT_WINDOW': '1', 'CLAIM_WINDOW_DAYS': '7',
        'CLAIM_LIMIT': '2',
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def prepare_workflow(path_or_dict, mock, slug, env, disable_schedules=True):
    """A copy of the workflow ready for the mocks:
      * https://{{ $env.SHOPIFY_STORE_DOMAIN }} becomes the mock URL (write the Shopify URL exactly like that:
        https://{{ $env.SHOPIFY_STORE_DOMAIN }}/admin/api/{{ $env.SHOPIFY_API_VERSION }}/graphql.json)
      * every $env.NAME becomes the JSON quoted test value (missing names become an empty string)
      * real API hosts (Airtable, Anthropic, Postmark, Open-Meteo, Slack) become the mock URL
      * webhook paths get the prefix "<slug>-e2e-" and fresh webhook ids
      * schedule triggers are disabled so only the test decides when the workflow runs
    Raises ValueError when a real API URL is still left anywhere outside sticky notes, when a host is built at run
    time ('https://' + host, `https://${host}`, https://{{ $json.host }}), or when an HTTP Request node's literal URL
    is not a mocked API.
    """
    if isinstance(path_or_dict, dict):
        wf = copy.deepcopy(path_or_dict)
    else:
        with open(path_or_dict, encoding='utf-8') as fh:
            wf = json.load(fh)

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        for var in HOST_ENV_VARS:
            value = re.sub(r'https://\{\{\s*\$env\.' + var + r'\s*\}\}', mock.url, value)
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(str(env.get(m.group(1), ''))), value)
        value = re.sub(r'\$env\[[\'"]([A-Z0-9_]+)[\'"]\]', lambda m: json.dumps(str(env.get(m.group(1), ''))), value)
        for real in REAL_HOSTS:
            value = value.replace(real, mock.url)
        return value

    leftovers = []
    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = f'{slug}-e2e-' + node['parameters'].get('path', 'hook').lstrip('/')
            node['webhookId'] = str(uuid.uuid4())
        if disable_schedules and node['type'] in ('n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.cron'):
            node['disabled'] = True
        if node['type'] != 'n8n-nodes-base.stickyNote':
            text = json.dumps(node['parameters'])
            leftovers += [f'{node["name"]}: {m.group(0)}' for m in REAL_API_URL.finditer(text)]
            leftovers += [f'{node["name"]}: builds the host at run time ({m.group(0)}...); write the URL as '
                          f'https://{{{{ $env.SHOPIFY_STORE_DOMAIN }}}}/... or a literal API host'
                          for m in BUILT_HOST.finditer(_strings(node['parameters']))]
            if node['type'] == 'n8n-nodes-base.httpRequest':
                url = str(node['parameters'].get('url', '')).lstrip('=').strip()
                if url.lower().startswith(('http://', 'https://')) and not url.startswith(mock.url):
                    leftovers.append(f'{node["name"]}: {url[:80]} is not a mocked API')
    if leftovers:
        raise ValueError('these nodes would call a real API in the test: ' + '; '.join(leftovers))
    return wf


def _strings(value):
    """Every string inside node parameters, joined by newlines (unescaped, unlike json.dumps)."""
    if isinstance(value, dict):
        return '\n'.join(_strings(v) for v in value.values())
    if isinstance(value, list):
        return '\n'.join(_strings(v) for v in value)
    return value if isinstance(value, str) else ''


def _settings(wf, **extra):
    out = {k: v for k, v in (wf.get('settings') or {}).items() if k in SETTINGS_KEYS}
    out.setdefault('executionOrder', 'v1')
    out.update(extra)
    return out


class N8n:
    def __init__(self, url=None, api_key=None):
        self.url = (url or os.environ.get('N8N_URL', 'http://localhost:5678')).rstrip('/')
        self.key = api_key or os.environ.get('N8N_API_KEY', '')
        env_file = os.environ.get('N8N_ENV_FILE')
        if not self.key and env_file:
            if not os.path.isfile(env_file):
                raise SystemExit(f'N8N_ENV_FILE points at {env_file!r}, which is not a file this Python can open. '
                                 'With Windows Python from Git Bash, write it as C:/Users/... instead of /c/Users/...')
            self.key = load_env_file(env_file).get('N8N_API_KEY', '')
        if not self.key:
            raise SystemExit('Set N8N_API_KEY (or N8N_ENV_FILE pointing at a .env that has it) first.')

    def api(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'X-N8N-API-KEY': self.key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'n8n API {method} {path} answered {e.code}: {e.read().decode("utf-8", "replace")[:400]}')

    @staticmethod
    def test_name(slug):
        return f'TEST {slug} (temporary)'

    def workflows_named(self, name):
        out, cursor = [], None
        while True:
            q = {'limit': 250, 'name': name}
            if cursor:
                q['cursor'] = cursor
            page = self.api('GET', '/api/v1/workflows?' + urllib.parse.urlencode(q))
            out += [w for w in page.get('data', []) if w.get('name') == name]
            cursor = page.get('nextCursor')
            if not cursor:
                return out

    def cleanup(self, slug):
        """Delete leftovers from an interrupted run: only workflows named exactly "TEST <slug> (temporary)"."""
        removed = 0
        for w in self.workflows_named(self.test_name(slug)):
            self._remove(w['id'], w.get('active'))
            removed += 1
        return removed

    def _remove(self, wid, active=True):
        if active:
            try:
                self.api('POST', f'/api/v1/workflows/{wid}/deactivate')
            except Exception:
                pass
        try:
            self.api('DELETE', f'/api/v1/workflows/{wid}')
        except Exception:
            pass

    def create(self, wf, slug, activate=True, **settings):
        created = self.api('POST', '/api/v1/workflows', {'name': self.test_name(slug), 'nodes': wf['nodes'],
                                                         'connections': wf['connections'],
                                                         'settings': _settings(wf, **settings)})
        if activate:
            self.api('POST', f'/api/v1/workflows/{created["id"]}/activate')
        return created['id']

    @contextmanager
    def temporary(self, wf, slug, mock=None, error_workflow=True, activate=True):
        """Create "TEST <slug> (temporary)" and activate it, yield a Run, then deactivate and delete it.

        With error_workflow=True (the default) a second workflow with the same name is created first from
        shared/error-alert-slack.json, pointed at the mock Slack path ERROR_SLACK_PATH, ACTIVATED (n8n 2.x skips an
        inactive error workflow) and set as the Error Workflow of the workflow under test."""
        ids = []
        try:
            extra = {}
            if error_workflow:
                if mock is None:
                    raise ValueError('pass mock=... so the error workflow can post to the mock Slack')
                ew = prepare_workflow(ERROR_WORKFLOW, mock, slug, {'SLACK_WEBHOOK_URL': mock.url + ERROR_SLACK_PATH})
                ids.append(self.create(ew, slug, activate=True))
                extra['errorWorkflow'] = ids[-1]
            ids.append(self.create(wf, slug, activate=activate, **extra))
            if activate:
                time.sleep(1.5)
            yield Run(self, ids[-1], slug, mock, ids[0] if error_workflow else None)
        finally:
            for wid in reversed(ids):
                self._remove(wid)


class Run:
    def __init__(self, n8n, wid, slug, mock=None, error_wid=None):
        self.n8n, self.wid, self.slug, self.mock, self.error_wid = n8n, wid, slug, mock, error_wid
        self.started = 0
        self.returned = set()

    def executions(self):
        out, cursor = [], None
        while True:
            q = {'workflowId': self.wid, 'limit': 250}
            if cursor:
                q['cursor'] = cursor
            page = self.n8n.api('GET', '/api/v1/executions?' + urllib.parse.urlencode(q))
            out += page.get('data', [])
            cursor = page.get('nextCursor')
            if not cursor:
                return out

    def post(self, path, payload=None, headers=None, raw=None, method='POST', timeout=60, count=True):
        """Call the workflow's webhook /webhook/<slug>-e2e-<path>. Returns (status, body_text).
        count=False when the call is not expected to start a run (for example a 404 on a wrong path)."""
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        req = urllib.request.Request(f'{self.n8n.url}/webhook/{self.slug}-e2e-{path}', data=data, headers=h, method=method)
        if count:
            self.started += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')

    def post_shopify(self, path, topic, payload, **kw):
        """Deliver a signed Shopify webhook (see shopify.webhook_request for webhook_id, signature, secret...)."""
        from .shopify import webhook_request
        timeout = kw.pop('timeout', 60)
        raw, headers = webhook_request(topic, payload, **kw)
        return self.post(path, raw=raw, headers=headers, timeout=timeout)

    def _finished(self):
        return [e for e in self.executions() if e.get('stoppedAt') or e.get('status') in ('success', 'error', 'crashed')]

    def wait(self, timeout=120):
        """Wait until every run started so far has finished. Returns the newest one with its data."""
        runs = self.wait_all(timeout)
        if runs:
            return runs[-1]
        done = self._finished()
        if not done:
            raise TimeoutError('no execution found')
        return self.execution(max(done, key=lambda e: int(e['id']))['id'])

    def wait_all(self, timeout=120):
        """Wait until every run started so far has finished. Returns the runs not returned before, oldest first."""
        end = time.time() + timeout
        while time.time() < end:
            done = self._finished()
            if len(done) >= self.started:
                fresh = sorted((e for e in done if e['id'] not in self.returned), key=lambda e: int(e['id']))
                self.returned.update(e['id'] for e in fresh)
                return [self.execution(e['id']) for e in fresh]
            time.sleep(0.4)
        raise TimeoutError(f'the workflow did not finish in time ({len(self._finished())} of {self.started} runs done)')

    def execution(self, eid):
        return self.n8n.api('GET', f'/api/v1/executions/{eid}?includeData=true')

    def alert_for(self, ex, timeout=None):
        """The error workflow's Slack message for this failed run (matched by the execution link inside it),
        or None when no alert arrives within timeout seconds. Alerts arrive after the failed run has finished, and
        on a busy n8n that can take a while, so the default wait is ALERT_TIMEOUT (90 s). With the default, a miss
        also prints the error workflow's runs to stderr, so a slow alert can be told apart from a missing one.
        Pass a short timeout (for example 5) when you expect no alert; nothing is printed then."""
        explain = timeout is None
        timeout = ALERT_TIMEOUT if timeout is None else timeout
        eid = str(ex['id'] if isinstance(ex, dict) else ex)
        end = time.time() + timeout
        pattern = re.compile(r'executions/' + re.escape(eid) + r'(?!\d)')
        while time.time() < end:
            for m in (self.mock.slack.messages if self.mock else []):
                if ERROR_SLACK_PATH in m['_path'] and pattern.search(m.get('text', '')):
                    return m['text']
            time.sleep(0.5)
        if explain and self.error_wid:
            try:
                page = self.n8n.api('GET', f'/api/v1/executions?workflowId={self.error_wid}&limit=50')
                states = ', '.join(f"{e['id']} {e.get('status')}" for e in page.get('data', [])) or 'none'
            except Exception as e:  # the diagnosis must never hide the real result
                states = f'could not be read ({e})'
            print(f'  (no error alert for run {eid} after {timeout} s; error workflow runs so far: {states})',
                  file=sys.stderr)
        return None

    def error_alerts(self):
        """Every text the error workflow posted so far."""
        return [m.get('text', '') for m in (self.mock.slack.messages if self.mock else []) if ERROR_SLACK_PATH in m['_path']]

    @staticmethod
    def result(ex):
        return ((ex.get('data') or {}).get('resultData') or {})

    def last_node(self, ex):
        return self.result(ex).get('lastNodeExecuted')

    def error(self, ex):
        return (self.result(ex).get('error') or {}).get('message', '')

    def ran(self, ex, node):
        return node in (self.result(ex).get('runData') or {})

    def runs_of(self, ex, node):
        """How many times a node ran in this execution (Loop Over Items runs a node once per pass)."""
        return len((self.result(ex).get('runData') or {}).get(node) or [])

    def output(self, ex, node, run=0, branch=0):
        """The json items a node produced on one output branch."""
        runs = (self.result(ex).get('runData') or {}).get(node) or []
        if run >= len(runs):
            return []
        main = ((runs[run].get('data') or {}).get('main') or [])
        if branch >= len(main) or main[branch] is None:
            return []
        return [i.get('json') for i in main[branch]]

    def node_error(self, ex, node, run=0):
        runs = (self.result(ex).get('runData') or {}).get(node) or []
        if run >= len(runs):
            return None
        err = runs[run].get('error')
        return (err or {}).get('message') if err else None


class Report:
    """Collects checks and prints them grouped by scenario, in the same format as docs/test-report.md."""

    def __init__(self):
        self.results = []

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))
        return bool(ok)

    def print(self):
        last = None
        for scenario, label, ok, detail in self.results:
            if scenario != last:
                print(f'\n{scenario}')
                last = scenario
            print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
        passed = sum(ok for _, _, ok, _ in self.results)
        print(f'\n{passed}/{len(self.results)} checks passed')
        return passed == len(self.results)
