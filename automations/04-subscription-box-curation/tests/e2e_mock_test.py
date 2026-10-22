"""End to end test for the Monthly Chocolate Subscription Box Curation workflow, against MOCK APIs.

No real Airtable base or Slack workspace is used. The script:
  0. checks the workflow file itself (retries and error outputs, read pagination, pacing before every Airtable call,
     the one-run-at-a-time lock and the failure path, IF operators that exist in n8n, no em or en dashes),
  1. starts the shared strict mocks (tests/mocks) on this machine: a seeded Airtable base with the real rate limit
     (5 requests a second, then 30 seconds of 429) and a Slack incoming webhook. mock.lose_answer() makes the
     failure where Airtable saves a write and the answer is lost,
  2. imports a temporary copy of the workflow into your n8n, pointed at the mocks, plus an ACTIVE copy of
     shared/error-alert-slack.json as its error workflow,
  3. plays the scenarios below through the Run Now webhook and checks every Airtable record, every request and every
     Slack message, then deletes both temporary workflows,
  4. runs allergen_property.js in a small temporary workflow: the planner code taken from the workflow file's
     "Code: Plan Boxes" node, on 1,500 random bases with scarce stock, checked against the true allergens of every
     product, plus a control run against a copy that ignores May Contain Traces (it must find breaches).

Expected boxes come from an independent Python version of the box rules (expected_plan below). The allergen checks
do not use it: they look at each packed product's own record, and they run on every box of every scenario
(allergen_breaches), with the totals checked at the end.

Requirements: Python 3.9+, a running n8n 2.x with the public API enabled.
Environment:
  N8N_URL       default http://localhost:5678
  N8N_API_KEY   your n8n API key (or N8N_ENV_FILE pointing at a .env file that has it, as a Windows path
                such as C:/Users/you/n8n/.env when you run Windows Python from Git Bash)
  MOCK_HOST     how n8n reaches this machine; default host.docker.internal (n8n in Docker). Use 127.0.0.1 if n8n
                runs directly on this machine.
Usage: python automations/04-subscription-box-curation/tests/e2e_mock_test.py
"""
import json
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.mocks import AIRTABLE_TOKEN, MockServer  # noqa: E402
from tests.mocks.n8n import N8n, Report, mock_env, prepare_workflow  # noqa: E402

SLUG = 'subscription-box-curation'
PORT = 8794
WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow' / 'subscription-box-curation.json'
HOOK = 'subscription-box-run-now'
SECRET = 'mock-run-now-secret'
AUTH = {'X-Run-Now-Secret': SECRET}
PACKING = '/services/T000MOCK/B000MOCK/packing'
ALERTS = '/services/T000MOCK/B000MOCK/alerts'
NUT_TRAPS = ('DC-BAR-DARK-RASPBERRY', 'DC-BAR-WHITE-MATCHA', 'DC-BAR-MILK-LAVENDER')
AUTOMATION = '04 Subscription Boxes'


# ---------------------------------------------------------------------------------------------- months and dates

def month_add(ym, k):
    y, m = map(int, ym.split('-'))
    total = y * 12 + m - 1 + k
    return f'{total // 12}-{total % 12 + 1:02d}'


def months_between(a, b):
    ya, ma = map(int, a.split('-'))
    yb, mb = map(int, b.split('-'))
    return (yb * 12 + mb) - (ya * 12 + ma)


TODAY = datetime.now(timezone.utc).date()
THIS_MONTH = TODAY.strftime('%Y-%m')
TARGET = month_add(THIS_MONTH, 1)          # what Run Now plans by default
SEED_TARGET = '2026-11'                    # the seed's last boxes are 2026-10 (box months are text, never shifted)
DELTA = months_between(SEED_TARGET, TARGET)


def fresh(mock):
    """Freshly seeded base. When the test runs after October 2026, move the seed's box months forward so that the
    last boxes are always last month's, as on the seed day (dates are shifted by the mock, box month text is not)."""
    mock.reset()
    at = mock.airtable
    if DELTA:
        for r in at.records('Box History'):
            f = r['fields']
            m = month_add(f['Box Month'], DELTA)
            at.update('Box History', r['id'], {'Box Month': m, 'History ID': m + f['History ID'][7:]})
    at.unblock()


# ---------------------------------------------------------------------------------------------- the box rules, again

def expected_plan(at, month, today, repeat_window=1, buffer_days=14, extra_allergens=None, unverified=(),
                  not_vegan=(), exclude_skus=()):
    """An independent Python version of the curation rules, used to know the exact boxes to expect.
    extra_allergens: {sku: {allergen}} the cross-checks are expected to add; unverified: SKUs without an ingredient
    list (they only go to subscribers without dietary needs); not_vegan: Vegan labelled SKUs the cross-checks rule
    out; exclude_skus: SKUs that must go in no box (for example two records with the same SKU)."""
    extra_allergens = extra_allergens or {}
    prods = []
    for r in at.records('Products'):
        f = r['fields']
        if (f.get('Status') == 'Active' and f.get('Subscription Eligible') is True and f.get('Category') == 'Bar'
                and f.get('SKU') not in exclude_skus):
            prods.append(r)
    prods.sort(key=lambda r: r['fields']['SKU'])
    pid = {r['id']: r for r in prods}

    def allergens(r):
        f = r['fields']
        return set(f.get('Contains Allergens', [])) | set(f.get('May Contain Traces', [])) | extra_allergens.get(f['SKU'], set())

    def suits(r, needs):
        f, a = r['fields'], allergens(r)
        for n in needs:
            if n == 'Nut-Free' and a & {'Tree Nuts', 'Peanuts'}:
                return 'diet'
            if n == 'Dairy-Free' and 'Milk' in a:
                return 'diet'
            if n == 'Gluten-Free' and 'Wheat' in a:
                return 'diet'
            if n == 'Vegan' and ('Vegan' not in f.get('Diet Labels', []) or {'Milk', 'Eggs'} & set(f.get('Contains Allergens', []))
                                 or f['SKU'] in not_vegan):
                return 'diet'
        if needs and f['SKU'] in unverified:
            return 'unverified'
        return None

    month_start = date.fromisoformat(month + '-01')
    ship = max(today, month_start)
    cutoff = ship + timedelta(days=buffer_days)
    batches = {}
    for r in at.records('Production Batches'):
        f = r['fields']
        links = f.get('Product', [])
        if f.get('Status') != 'Released' or len(links) != 1 or links[0] not in pid:
            continue
        if not f.get('Produced On') or date.fromisoformat(f['Produced On']) > today:
            continue
        if not f.get('Best Before') or date.fromisoformat(f['Best Before']) <= cutoff:
            continue
        left = max(f.get('Quantity Made', 0) - f.get('Quantity Reserved', 0) - f.get('Quantity Sold', 0), 0)
        if left > 0:
            batches.setdefault(links[0], []).append({'id': r['id'], 'code': f['Batch Code'], 'bb': f['Best Before'], 'left': left})
    for arr in batches.values():
        arr.sort(key=lambda b: (b['bb'], b['code']))
    stock = {p: sum(b['left'] for b in batches.get(p, [])) for p in pid}
    initial = dict(stock)

    subs = [r for r in at.records('Subscribers') if r['fields'].get('Status') == 'Active']
    subs.sort(key=lambda r: (r['fields'].get('Started On') or '9999-12-31', r['fields'].get('Subscriber ID', ''), r['id']))
    cancelled = {r['id'] for r in at.records('Box Shipments') if r['fields'].get('Status') == 'Cancelled'}
    seen_months = {}
    recorded = set()        # subscribers with bars for this month in Box History (a box recorded outside the workflow)
    for r in at.records('Box History'):
        f = r['fields']
        if set(f.get('Box Shipment', [])) & cancelled:
            continue        # a cancelled box was never received
        if f.get('Box Month') == month:
            recorded.update(f.get('Subscriber', []))
        elif f.get('Box Month', '9999') < month:
            for s in f.get('Subscriber', []):
                seen_months.setdefault(s, {}).setdefault(f['Box Month'], set()).update(f.get('Product', []))
    existing = set()
    for r in at.records('Box Shipments'):
        f = r['fields']
        if f.get('Box Month') == month:
            existing.update(f.get('Subscriber', []))
        elif f.get('Box Month', '9999') < month and f.get('Status') != 'Cancelled':
            for s in f.get('Subscriber', []):
                seen_months.setdefault(s, {}).setdefault(f['Box Month'], set()).update(f.get('Products', []))

    plan_subs = []
    for r in subs:
        f = r['fields']
        size = re.fullmatch(r'(\d+) Bars?', f.get('Plan', ''))
        if (r['id'] in existing or r['id'] in recorded or not size or not 1 <= int(size.group(1)) <= 10
                or set(f.get('Dietary Needs', [])) - {'Nut-Free', 'Dairy-Free', 'Vegan', 'Gluten-Free'}):
            continue
        months = sorted(seen_months.get(r['id'], {}), reverse=True)[:repeat_window] if repeat_window else []
        window = set().union(*[seen_months[r['id']][m] for m in months]) if months else set()
        verdicts = {}
        for p in prods:
            v = suits(p, f.get('Dietary Needs', []))
            if not v and set(p['fields'].get('Flavor Notes', [])) & set(f.get('Flavor Dislikes', [])):
                v = 'dislike'
            if not v and p['id'] in window:
                v = 'repeat'
            verdicts[p['id']] = v or 'ok'
        need = [p['id'] for p in prods if verdicts[p['id']] == 'ok' and initial[p['id']] > 0]
        plan_subs.append({'rec': r, 'sub': f['Subscriber ID'], 'size': int(size.group(1)), 'verdicts': verdicts,
                          'need': need, 'tight': len(need) <= int(size.group(1)), 'window': window})

    out = {}
    for i, s in enumerate(plan_subs):
        later = [t for t in plan_subs[i + 1:] if t['tight']]
        pool = [p for p in prods if s['verdicts'][p['id']] == 'ok' and stock[p['id']] > 0]
        picked, fam, typ = [], {}, {}
        while len(picked) < s['size'] and pool:
            def key(p):
                d = sum(1 for t in later if p['id'] in t['need'])
                left = stock[p['id']]
                return (1 if left <= d else 0, fam.get(p['fields'].get('Flavor Family'), 0),
                        typ.get(p['fields'].get('Chocolate Type'), 0), -(left - d), p['fields']['SKU'])
            best = min(pool, key=key)
            picked.append(best)
            fam[best['fields'].get('Flavor Family')] = fam.get(best['fields'].get('Flavor Family'), 0) + 1
            typ[best['fields'].get('Chocolate Type')] = typ.get(best['fields'].get('Chocolate Type'), 0) + 1
            stock[best['id']] -= 1
            pool.remove(best)
        picked.sort(key=lambda p: p['fields']['SKU'])
        bars = []
        for p in picked:
            b = next(b for b in batches[p['id']] if b['left'] > 0)
            b['left'] -= 1
            bars.append((p, b))
        fill = 'Full' if len(bars) == s['size'] else 'Empty' if not bars else 'Partial'
        out[f'BOX-{month}-{s["sub"]}'] = {
            'subscriber': s['rec']['id'], 'sub': s['sub'], 'size': s['size'], 'fill': fill,
            'products': [p['id'] for p, _ in bars], 'skus': [p['fields']['SKU'] for p, _ in bars],
            'batches': list(dict.fromkeys(b['id'] for _, b in bars)), 'codes': [b['code'] for _, b in bars],
            'window': s['window'], 'needs': s['rec']['fields'].get('Dietary Needs', []),
        }
    return out


# ---------------------------------------------------------------------------------------------- independent checks

NUT_WORDS = re.compile(r'\b(almonds?|hazelnuts?|walnuts?|pecans?|cashews?|pistachios?|macadamias?|peanuts?|tree nuts?|nuts?|praline)\b', re.I)
MILK_WORDS = re.compile(r'\b(whole milk|skim milk|milk powder|milk chocolate|cream|whey|lactose)\b', re.I)
WHEAT_WORDS = re.compile(r'\b(wheat|gluten|barley|rye|spelt)\b', re.I)


def unsafe_reasons(product_fields, needs):
    """Why a product must not go to someone with these needs, judged only from the product record itself."""
    f = product_fields
    listed = set(f.get('Contains Allergens', [])) | set(f.get('May Contain Traces', []))
    ing = f.get('Ingredients') or ''
    notes = set(f.get('Flavor Notes', []))
    milk_type = f.get('Chocolate Type') in ('Milk', 'White', 'Ruby')     # ruby chocolate is made with milk powder
    bad = []
    if needs and not ing:
        bad.append('no ingredient list')
    if 'Nut-Free' in needs and (listed & {'Tree Nuts', 'Peanuts'} or NUT_WORDS.search(ing) or NUT_WORDS.search(f.get('Name', ''))
                                or notes & {'Hazelnut', 'Almond', 'Peanut'}):
        bad.append('nuts')
    if 'Dairy-Free' in needs and ('Milk' in listed or MILK_WORDS.search(ing) or milk_type):
        bad.append('milk')
    if 'Gluten-Free' in needs and ('Wheat' in listed or WHEAT_WORDS.search(ing)):
        bad.append('wheat')
    if 'Vegan' in needs and ('Vegan' not in f.get('Diet Labels', []) or {'Milk', 'Eggs'} & set(f.get('Contains Allergens', []))
                             or milk_type or 'Honey' in notes):
        bad.append('not vegan')
    return bad


NEED_LABEL = {'Nut-Free': 'nuts', 'Dairy-Free': 'milk', 'Gluten-Free': 'wheat', 'Vegan': 'not vegan'}
NUT_ALLERGENS = {'Tree Nuts', 'Peanuts'}

# Every box that check_boxes sees, in every scenario, is also judged by unsafe_reasons, so the allergen rule is
# checked in every scenario and not only in the ones about allergens. main() reports the totals.
SAFETY = {'seen': set(), 'scenarios': set(), 'boxes': 0, 'bars': 0, 'nut_free_boxes': 0, 'nut_free_bars': 0,
          'nut_free_short': 0, 'breaches': []}


def allergen_breaches(at, boxes, scenario):
    prods = {r['id']: r['fields'] for r in at.records('Products')}
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    out = []
    SAFETY['scenarios'].add(scenario)
    for sid, r in boxes.items():
        f = r['fields']
        needs = subs.get((f.get('Subscriber') or [None])[0], {}).get('Dietary Needs', [])
        ids = f.get('Products', [])
        for p in ids:
            why = unsafe_reasons(prods.get(p, {}), needs)
            if why:
                out.append(f'{sid} {prods.get(p, {}).get("SKU", p)} {why}')
        key = (scenario, r['id'], tuple(ids))
        if key in SAFETY['seen']:
            continue
        SAFETY['seen'].add(key)
        SAFETY['boxes'] += 1
        SAFETY['bars'] += len(ids)
        if 'Nut-Free' in needs:
            SAFETY['nut_free_boxes'] += 1
            SAFETY['nut_free_bars'] += len(ids)
            SAFETY['nut_free_short'] += f.get('Fill Status') != 'Full'
    SAFETY['breaches'] += [f'{scenario}: {b}' for b in out if f'{scenario}: {b}' not in SAFETY['breaches']]
    return out


# ---------------------------------------------------------------------------------------------- helpers

class Ctx:
    def __init__(self, mock, run, report):
        self.mock, self.run, self.r, self.at = mock, run, report, mock.airtable

    def post(self, body=None, headers=AUTH, path=HOOK, count=True):
        status, text = self.run.post(path, body, headers=headers, count=count)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def boxes(self, month=TARGET):
        return {r['fields']['Shipment ID']: r for r in self.at.records('Box Shipments') if r['fields'].get('Box Month') == month}

    def reserved(self):
        return {r['id']: r['fields'].get('Quantity Reserved', 0) for r in self.at.records('Production Batches')}

    def history(self, month=TARGET):
        return {r['fields']['History ID']: r for r in self.at.records('Box History') if r['fields'].get('Box Month') == month}

    def airtable(self, mark):
        return self.mock.log('airtable', since=mark)

    def writes(self, mark):
        return [e for e in self.airtable(mark) if e['method'] in ('POST', 'PATCH', 'PUT', 'DELETE') and e.get('operation') != 'list']

    def ops(self, mark):
        return [(e.get('table'), e.get('operation')) for e in self.airtable(mark)]

    def packing(self, mark_count):
        return self.mock.slack.texts(PACKING)[mark_count:]

    def run_row(self, run_id):
        rows = [r['fields'] for r in self.at.records('Sync Log') if r['fields'].get('Run ID') == run_id]
        return rows[0] if len(rows) == 1 else {}


def alert_message(alert):
    """The reason line of an error alert (line 2 of 3)."""
    lines = (alert or '').split('\n')
    return lines[1] if len(lines) == 3 else ''


def closed_as_failed(c, resp, alert, changed):
    """The failed run closed its own Sync Log row: Failed, Finished At, 1 error, the boxes it saved, the alert text."""
    f = c.run_row(resp.get('runId') if isinstance(resp, dict) else '')
    msg = alert_message(alert)
    ok = (f.get('Automation') == AUTOMATION and f.get('Status') == 'Failed' and bool(f.get('Finished At'))
          and f.get('Errors') == 1 and f.get('Items Changed') == changed and bool(msg) and f.get('Details') == msg)
    return ok, f"Sync Log {f.get('Status')} changed {f.get('Items Changed')} finished {bool(f.get('Finished At'))}"


def check_boxes(c, scenario, expected, label='every box matches the expected plan exactly'):
    """Every box against the expected plan, and every packed bar against its subscriber's needs (unsafe_reasons)."""
    boxes = c.boxes()
    problems = []
    for sid, e in expected.items():
        r = boxes.get(sid)
        if not r:
            problems.append(f'{sid} missing')
            continue
        f = r['fields']
        want = {'Subscriber': [e['subscriber']], 'Box Month': TARGET, 'Bars Planned': e['size'], 'Bars Packed': len(e['skus']),
                'Fill Status': e['fill'], 'Status': 'Planned'}
        for k, v in want.items():
            if f.get(k) != v:
                problems.append(f'{sid} {k}={f.get(k)!r} want {v!r}')
        if f.get('Products', []) != e['products']:
            problems.append(f'{sid} products {[c.at.primary_name("Products", p) for p in f.get("Products", [])]} want {e["skus"]}')
        if f.get('Batches', []) != e['batches']:
            problems.append(f'{sid} batches differ')
        if bool(f.get('Shortfall Reason')) != (e['fill'] != 'Full'):
            problems.append(f'{sid} shortfall reason presence')
        lines = f.get('Packing List', '')
        if not all(s in lines for s in e['skus']) or not all(code in lines for code in e['codes']):
            problems.append(f'{sid} packing list misses a SKU or batch code')
    extra = set(boxes) - set(expected)
    if extra:
        problems.append(f'unexpected boxes {sorted(extra)}')
    problems += [f'ALLERGEN BREACH {b}' for b in allergen_breaches(c.at, boxes, scenario)]
    return c.r.check(scenario, label + ", no bar breaks its subscriber's needs", not problems,
                     '; '.join(problems[:6]) or f'{len(expected)} boxes')


def reservation_delta(before, after):
    return {k: after.get(k, 0) - v for k, v in before.items() if after.get(k, 0) != v}


def units_by_batch(at, expected, only=None):
    """Units each batch should gain: one per bar, from the batch the plan used for it."""
    ids = {r['fields']['Batch Code']: r['id'] for r in at.records('Production Batches')}
    out = {}
    for sid, e in expected.items():
        if only is None or sid in only:
            for code in e['codes']:
                out[ids[code]] = out.get(ids[code], 0) + 1
    return out


def max_in_one_second(entries):
    times = sorted(datetime.fromisoformat(e['time'].replace('Z', '+00:00')).timestamp() for e in entries)
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] <= t - 1.0:
            j += 1
        best = max(best, i - j + 1)
    return best


def run_ok(c, scenario, body, label='Run Now answers 202 and the run succeeds', timeout=180):
    status, resp = c.post(body)
    ex = c.run.wait(timeout=timeout)
    c.r.check(scenario, label, status == 202 and ex['status'] == 'success',
              f'{status} {str(resp)[:120]} / run {ex["status"]} at {c.run.last_node(ex)} {c.run.error(ex)[:200]}')
    return status, resp, ex


def run_fails(c, body=None, timeout=180):
    status, resp = c.post(body or {'month': TARGET})
    ex = c.run.wait(timeout=timeout)
    alert = c.run.alert_for(ex, timeout=40) or ''
    return status, resp, ex, alert


# ---------------------------------------------------------------------------------------------- static checks

IF_OPERATORS = {
    'string': {'equals', 'notEquals', 'contains', 'notContains', 'startsWith', 'endsWith', 'regex', 'empty', 'notEmpty',
               'exists', 'notExists'},
    'number': {'equals', 'notEquals', 'gt', 'gte', 'lt', 'lte'},
    'boolean': {'true', 'false', 'equals', 'notEquals'},
}
SINGLE_VALUE = {'empty', 'notEmpty', 'exists', 'notExists', 'true', 'false'}
AIRTABLE_URL = re.compile(r'^=https://api\.airtable\.com/v0/\{\{ \$env\.AIRTABLE_BASE_ID \}\}/[A-Za-z%0-9]+(/listRecords)?$')


def wait_seconds(n):
    """The shortest wait a Wait node can make (its amount may be an expression such as 'x ? 31 : 0.5')."""
    a = n['parameters'].get('amount', 0)
    if isinstance(a, (int, float)):
        return float(a)
    nums = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', str(a))]
    return min(nums) if nums else 0.0


def scenario_static(report):
    """Checks on the workflow file itself, before anything runs."""
    s = 'Workflow file'
    text = WORKFLOW.read_text(encoding='utf-8')
    wf = json.loads(text)
    nodes = {n['name']: n for n in wf['nodes'] if n['type'] != 'n8n-nodes-base.stickyNote'}
    conns = wf['connections']
    http = [n for n in nodes.values() if n['type'] == 'n8n-nodes-base.httpRequest']
    bad = [n['name'] for n in http if not (n.get('retryOnFail') is True and n.get('maxTries') == 3 and n.get('waitBetweenTries') == 2000
                                           and n.get('onError') == 'continueErrorOutput'
                                           and len(conns.get(n['name'], {}).get('main', [])) > 1 and conns[n['name']]['main'][1])]
    report.check(s, 'every HTTP Request node retries 3 times, 2 s apart, and its error output is wired to a handler',
                 not bad and not any('continueOnFail' in n for n in nodes.values()), f'{len(http)} HTTP nodes, bad {bad}')
    reads = [n for n in http if n['name'].startswith('Airtable: List ')]
    bad = []
    for n in reads:
        p = n['parameters']
        pg = p.get('options', {}).get('pagination', {}).get('pagination', {})
        body = p.get('jsonBody', '')
        if not (p.get('method') == 'POST' and p['url'].endswith('/listRecords') and pg.get('paginationMode') == 'updateAParameterInEachRequest'
                and pg.get('parameters', {}).get('parameters', [{}])[0] == {'type': 'body', 'name': 'offset', 'value': '={{ $response.body.offset }}'}
                and pg.get('completeExpression') == '={{ !$response.body.offset }}' and pg.get('requestInterval', 0) >= 500
                and 'pageSize: 100' in body and 'fields: [' in body and 'filterByFormula' in body and n.get('executeOnce') is True):
            bad.append(n['name'])
    report.check(s, 'the 6 reads (open runs and 5 tables) use POST listRecords with a formula, named fields, pageSize 100 and '
                    'offset pagination 500 ms apart', len(reads) == 6 and not bad, f'{len(reads)} reads, bad {bad}')
    airtable = [n for n in http if n['name'].startswith('Airtable: ')]
    urls_ok = all(AIRTABLE_URL.match(n['parameters']['url']) and n['parameters']['headerParameters']['parameters']
                  == [{'name': 'Authorization', 'value': '=Bearer {{ $env.AIRTABLE_TOKEN }}'}] for n in airtable)
    feeders = {}
    for src, cn in conns.items():
        for i, outs in enumerate(cn.get('main', [])):
            for t in outs:
                feeders.setdefault(t['node'], set()).add((src, i))
    unpaced = sorted({n['name'] for n in airtable for src, _ in feeders.get(n['name'], ())
                      if not (nodes[src]['type'] == 'n8n-nodes-base.wait' and wait_seconds(nodes[src]) >= 0.5)})
    report.check(s, 'every Airtable call uses the exact base URL and token header and comes right after a Wait of at least '
                    '500 ms (2 calls a second, so two runs at once stay under Airtable\'s 5)',
                 urls_ok and not unpaced and len(airtable) == 14, f'{len(airtable)} Airtable nodes, unpaced {unpaced}')
    bad_if = []
    for n in nodes.values():
        if n['type'] == 'n8n-nodes-base.if':
            for cnd in n['parameters']['conditions']['conditions']:
                op = cnd['operator']
                if op['operation'] not in IF_OPERATORS.get(op['type'], set()) or (op['operation'] in SINGLE_VALUE) != bool(op.get('singleValue')):
                    bad_if.append(f'{n["name"]} {op}')
    report.check(s, 'every IF condition uses an operator that exists in IF v2 (single value operators marked)', not bad_if,
                 f'{sum(1 for n in nodes.values() if n["type"] == "n8n-nodes-base.if")} IF nodes, bad {bad_if}')
    names_ok = all(re.fullmatch(r'[A-Z][A-Za-z]*: [A-Z0-9].*', n) for n in nodes)
    report.check(s, 'no em or en dash anywhere, node names read "Type: Name", the schedule is monthly on the 20th at 08:00',
                 chr(0x2014) not in text and chr(0x2013) not in text and names_ok
                 and nodes['Schedule: Monthly On The 20th']['parameters']['rule']['interval'][0]
                 == {'field': 'months', 'monthsInterval': 1, 'triggerAtDayOfMonth': 20, 'triggerAtHour': 8, 'triggerAtMinute': 0},
                 f'{len(nodes)} nodes')
    # the lock and the failure path
    stops = {n['name'] for n in nodes.values() if n['type'] == 'n8n-nodes-base.stopAndError'}
    fail_in = feeders.get('Code: Failed Run Fields', set())
    lock_ok = (conns['Start: Curation Run']['main'] == [[{'node': 'Wait: Before Claim', 'type': 'main', 'index': 0}]]
               and conns['Wait: Before Claim']['main'][0][0]['node'] == 'Airtable: Claim Run'
               and wait_seconds(nodes['Wait: Lock Settle']) >= 2
               and conns['IF: Clear To Run?']['main'][0][0]['node'] == 'Wait: Pace Read 1')
    report.check(s, 'the run claims its Sync Log row before any read, and every failure after the start closes that row '
                    'before the one Stop node',
                 lock_ok and stops == {'Stop: Bad Settings', 'Stop: Bad Run Now Settings', 'Stop: Run Failed', 'Stop: Slack Failed', 'Stop: Sync Log Failed'}
                 and feeders.get('Stop: Run Failed') == {('Airtable: Log Failed Run', 0), ('Airtable: Log Failed Run', 1)}
                 and fail_in == {('IF: Clear To Run?', 1), ('IF: Wait Out Read Limit?', 1), ('IF: Plan Failed?', 0),
                                 ('Code: Write Failed', 0), ('Code: History Failed', 0)},
                 f'stops {sorted(stops)}, failure path fed by {sorted(fail_in)}')


# ---------------------------------------------------------------------------------------------- scenarios

def scenario_security(c):
    s = 'Run Now security'
    mark = c.mock.mark()
    st1, b1 = c.post({'month': TARGET}, headers={})
    st2, b2 = c.post({'month': TARGET}, headers={'X-Run-Now-Secret': 'wrong-secret'})
    runs = c.run.wait_all()
    c.r.check(s, 'no secret header and a wrong secret both get 401', st1 == 401 and st2 == 401, f'{st1} {b1} / {st2}')
    c.r.check(s, 'nothing is read or written, and no alert', not c.airtable(mark) and len(runs) == 2
              and all(e['status'] == 'success' for e in runs) and not c.run.error_alerts(), f'{len(c.airtable(mark))} Airtable calls')


def scenario_bad_month(c):
    s = 'Bad month'
    mark = c.mock.mark()
    st1, b1 = c.post({'month': '2026-13'})
    st2, b2 = c.post(None, path=HOOK + '?month=' + month_add(THIS_MONTH, -1))
    st3, b3 = c.post({'month': month_add(THIS_MONTH, 6)})
    st4, b4 = c.post({'month': 202611})
    c.run.wait_all()
    c.r.check(s, 'a malformed month, a past month (in the query string), a month too far ahead and a number get 400',
              (st1, st2, st3, st4) == (400, 400, 400, 400) and 'YYYY-MM' in b1['error'] and 'cannot be curated now' in b2['error']
              and 'cannot be curated now' in b3['error'] and 'YYYY-MM' in b4['error'], f'{st1} {b1} / {st2} {b2} / {st3} / {st4}')
    c.r.check(s, 'nothing is read or written, and no alert', not c.airtable(mark) and not c.run.error_alerts(),
              f'{len(c.airtable(mark))} Airtable calls')


def scenario_happy(c, samples):
    s = 'First run for the month'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    slack_n = len(c.mock.slack.texts(PACKING))
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    c.r.check(s, '202 body names the month and a run id made of the month, the start time and the n8n execution id',
              resp.get('month') == TARGET
              and re.fullmatch(rf'boxes-{TARGET}-\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ-{ex["id"]}', resp.get('runId', '')), str(resp))
    boxes = c.boxes()
    active = {r['id'] for r in c.at.records('Subscribers') if r['fields'].get('Status') == 'Active'}
    c.r.check(s, 'one box per Active subscriber (13), none for the paused or cancelled one',
              len(boxes) == 13 and {r['fields']['Subscriber'][0] for r in boxes.values()} == active, f'{len(boxes)} boxes')
    check_boxes(c, s, expected)
    fills = sorted((sid[-8:], r['fields']['Bars Packed'], r['fields']['Bars Planned']) for sid, r in boxes.items() if r['fields']['Fill Status'] != 'Full')
    c.r.check(s, 'seed facts: 10 full, SUB-0004 1 of 6, SUB-0009 3 of 4, SUB-0011 1 of 4',
              fills == [('SUB-0004', 1, 6), ('SUB-0009', 3, 4), ('SUB-0011', 1, 4)]
              and sum(1 for r in boxes.values() if r['fields']['Fill Status'] == 'Full') == 10, str(fills))
    r4 = boxes[f'BOX-{TARGET}-SUB-0004']['fields'].get('Shortfall Reason', '')
    r9 = boxes[f'BOX-{TARGET}-SUB-0009']['fields'].get('Shortfall Reason', '')
    r11 = boxes[f'BOX-{TARGET}-SUB-0011']['fields'].get('Shortfall Reason', '')
    c.r.check(s, 'shortfall reasons say exactly why (diet, dislike, last box with the SKUs)',
              r4 == '1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box '
                    '(DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).'
              and r9 == '3 of 4 bars packed. The other 16 subscription bars: 12 not suitable for Vegan and Nut-Free; 4 in the last box '
                        '(DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-ORANGE, DC-BAR-OAT-MILK).'
              and r11 == '1 of 4 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); '
                         '3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).', r4 + ' | ' + r9 + ' | ' + r11)

    # allergen safety, judged from each packed product's own record
    prods = {r['id']: r['fields'] for r in c.at.records('Products')}
    subs = {r['id']: r['fields'] for r in c.at.records('Subscribers')}
    unsafe = []
    nut_free_boxes, nut_free_bars = 0, 0
    for sid, r in boxes.items():
        needs = subs[r['fields']['Subscriber'][0]].get('Dietary Needs', [])
        for p in r['fields'].get('Products', []):
            why = unsafe_reasons(prods[p], needs)
            if why:
                unsafe.append(f'{sid} {prods[p]["SKU"]} {why}')
        if 'Nut-Free' in needs:
            nut_free_boxes += 1
            nut_free_bars += len(r['fields'].get('Products', []))
    c.r.check(s, 'allergen safety: no packed bar breaks its subscriber\'s needs (fields, ingredients, name, notes, type checked)',
              not unsafe and sum(len(r['fields'].get('Products', [])) for r in boxes.values()) == 63,
              '; '.join(unsafe) or f'{sum(len(r["fields"].get("Products", [])) for r in boxes.values())} bars checked')
    trap_hits = [(sid, prods[p]['SKU']) for sid, r in boxes.items() for p in r['fields'].get('Products', [])
                 if 'Nut-Free' in subs[r['fields']['Subscriber'][0]].get('Dietary Needs', []) and prods[p]['SKU'] in NUT_TRAPS]
    c.r.check(s, 'nut trace traps (raspberry, matcha, lavender) never reach a nut-free box', not trap_hits and nut_free_boxes == 5
              and nut_free_bars == 21, f'{nut_free_boxes} nut-free boxes, {nut_free_bars} bars, hits {trap_hits}')

    # repeats, duplicates, variety
    repeats = [sid for sid, e in expected.items() if set(boxes[sid]['fields'].get('Products', [])) & e['window']]
    dupes = [sid for sid, r in boxes.items() if len(set(r['fields'].get('Products', []))) != len(r['fields'].get('Products', []))]
    c.r.check(s, 'no bar from the subscriber\'s last box, no bar twice in a box', not repeats and not dupes, f'{repeats} {dupes}')
    fam = {r['id']: r['fields'].get('Flavor Family') for r in c.at.records('Products')}
    worst = min((len({fam[p] for p in r['fields']['Products']}) / len(r['fields']['Products']), sid) for sid, r in boxes.items() if r['fields'].get('Products'))
    c.r.check(s, 'flavor variety: at least 3 flavor families in every full box of 4 or more',
              all(len({fam[p] for p in r['fields']['Products']}) >= 3 for r in boxes.values() if r['fields']['Fill Status'] == 'Full'),
              f'lowest share of distinct families {worst[0]:.2f} ({worst[1]})')

    # stock
    after = c.reserved()
    want = units_by_batch(c.at, expected)
    got = reservation_delta(before, after)
    c.r.check(s, 'Quantity Reserved went up by exactly one per packed bar on the batch the packing list names',
              got == want and sum(got.values()) == 63, f'{sum(got.values())} units on {len(got)} batches')
    bad_batches = []
    for r in c.at.records('Production Batches'):
        f = r['fields']
        if r['id'] in got and (f['Status'] != 'Released' or date.fromisoformat(f['Best Before']) <= max(TODAY, date.fromisoformat(TARGET + '-01')) + timedelta(days=14)
                               or f['Quantity Made'] - f.get('Quantity Reserved', 0) - f.get('Quantity Sold', 0) < 0):
            bad_batches.append(f['Batch Code'])
    c.r.check(s, 'no batch over-reserved; only Released batches with more than 14 days left at ship date were used',
              not bad_batches, str(bad_batches))
    scarce = {sku: sum(1 for r in boxes.values() for p in r['fields'].get('Products', []) if prods[p]['SKU'] == sku)
              for sku in ('DC-BAR-DARK-MINT', 'DC-BAR-DARK-85', 'DC-BAR-DARK-CHILI')}
    c.r.check(s, 'scarce stock respected: DARK-MINT 3 of 3 (to SUB-0002, SUB-0003 and SUB-0009), DARK-85 at most 4',
              scarce['DC-BAR-DARK-MINT'] == 3 and scarce['DC-BAR-DARK-85'] <= 4
              and sorted(sid[-8:] for sid, r in boxes.items() if any(prods[p]['SKU'] == 'DC-BAR-DARK-MINT' for p in r['fields'].get('Products', [])))
              == ['SUB-0002', 'SUB-0003', 'SUB-0009'], str(scarce))

    # history, Slack, Sync Log
    hist = c.history()
    total_bars = sum(len(e['skus']) for e in expected.values())
    rows_ok = len(hist) == total_bars and all(
        h['fields'].get('Box Shipment') == [boxes[f'BOX-{TARGET}-{h["fields"]["History ID"].split(" / ")[1]}']['id']]
        and h['fields'].get('Product') and h['fields'].get('Subscriber') for h in hist.values())
    c.r.check(s, 'one Box History row per bar, linked to its box, subscriber and product', rows_ok, f'{len(hist)} rows for {total_bars} bars')
    texts = c.packing(slack_n)
    t = texts[0] if texts else ''
    batches_used = len({b for e in expected.values() for b in e['batches']})
    c.r.check(s, 'one Slack summary with the counts and every box that is not full',
              len(texts) == 1 and f'Planned this run: 13 boxes (10 full, 3 partial, 0 empty), {total_bars} bars reserved from {batches_used} batches.' in t
              and '• SUB-0004 Derek Olsen, 1 of 6 bars packed.' in t and '• SUB-0009 Lucia Gomez, 3 of 4 bars packed.' in t
              and '• SUB-0011 Aisha Khan, 1 of 4 bars packed.' in t and '*Not fully filled (3)*' in t
              and 'Used up by this run:* DC-BAR-DARK-MINT' in t and not c.mock.slack.texts(ALERTS), t[:300])
    lf = c.run_row(resp.get('runId'))
    c.r.check(s, 'one Sync Log row for the run: Run Now, Partial, 13 checked, 13 changed, 0 errors, closed with a Finished At',
              len([r for r in c.at.records('Sync Log') if r['fields'].get('Automation') == AUTOMATION]) == 1
              and lf.get('Trigger') == 'Run Now' and lf.get('Status') == 'Partial' and lf.get('Items Checked') == 13
              and lf.get('Items Changed') == 13 and lf.get('Errors') == 0 and lf.get('Finished At') and 'SUB-0004' in lf.get('Details', ''),
              json.dumps(lf)[:200])

    # request discipline
    calls = c.airtable(mark)
    w = c.writes(mark)
    reads = [e for e in calls if e.get('operation') == 'list']
    first = calls[0] if calls else {}
    claim = ((first.get('body') or {}).get('records') or [{}])[0].get('fields', {}) if isinstance(first.get('body'), dict) else {}
    c.r.check(s, 'the first request claims the run: a Sync Log upsert with no Status and no Finished At, then the open runs are listed',
              first.get('table') == 'Sync Log' and first.get('operation') == 'upsert' and claim.get('Run ID') == resp.get('runId')
              and claim.get('Automation') == AUTOMATION and 'Status' not in claim and 'Finished At' not in claim
              and (calls[1].get('table'), calls[1].get('operation')) == ('Sync Log', 'list'), json.dumps(claim)[:160])
    c.r.check(s, 'Airtable pacing: no 429, never more than 2 requests in any second, every write 10 records or fewer',
              all(e['status'] == 200 for e in calls) and max_in_one_second(calls) <= 2 and all(e.get('record_count', 0) <= 10 for e in w),
              f'{len(calls)} requests, busiest second {max_in_one_second(calls)}, largest write {max((e.get("record_count", 0) for e in w), default=0)}')
    c.r.check(s, 'exactly 7 reads (all naming their fields) and 41 writes: the claim, one reserve, one box and one history upsert per box, the Sync Log',
              len(reads) == 7 and all(isinstance(e['body'], dict) and e['body'].get('fields') for e in reads)
              and sum(1 for e in w if (e['table'], e['operation']) == ('Box Shipments', 'upsert')) == 13
              and sum(1 for e in w if (e['table'], e['operation']) == ('Production Batches', 'update')) == 13
              and sum(1 for e in w if (e['table'], e['operation']) == ('Box History', 'upsert')) == 13
              and sum(1 for e in w if (e['table'], e['operation']) == ('Sync Log', 'upsert')) == 2 and len(w) == 41,
              f'{len(reads)} reads, {len(w)} writes')
    run_data = json.dumps(c.run.result(ex).get('runData'))
    c.r.check(s, 'no secret in any node output (Airtable token, Slack webhook URL)',
              AIRTABLE_TOKEN not in run_data and PACKING not in run_data and 'T000MOCK' not in run_data, f'{len(run_data)} characters of run data checked')
    samples['slack'] = t
    samples['box'] = boxes[f'BOX-{TARGET}-SUB-0004']['fields']
    samples['box_full'] = boxes[f'BOX-{TARGET}-SUB-0001']['fields']
    samples['upsert'] = next(e['body'] for e in w if e['table'] == 'Box Shipments' and 'SUB-0004' in json.dumps(e['body']))
    samples['reserve'] = next(e['body'] for e in w if e['table'] == 'Production Batches')
    samples['claim'] = first.get('body')
    samples['response'] = resp
    return expected


def scenario_rerun(c, expected):
    s = 'Rerun for the same month (no month given)'
    before_r, before_h, before_b = c.reserved(), c.history(), c.boxes()
    slack_n = len(c.mock.slack.texts(PACKING))
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, None)
    c.r.check(s, 'without a month, Run Now plans next month (UTC)', resp.get('month') == TARGET, str(resp))
    w = c.writes(mark)
    c.r.check(s, 'no new box, no reservation, no history row: the only writes are the run\'s Sync Log row (claim and close)',
              c.boxes() == before_b and c.reserved() == before_r and c.history() == before_h
              and [(e['table'], e['operation']) for e in w] == [('Sync Log', 'upsert')] * 2, str([(e['table'], e['operation']) for e in w]))
    t = (c.packing(slack_n) or [''])[0]
    lf = c.run_row(resp.get('runId'))
    earlier = [(sub, name, before_b[f'BOX-{TARGET}-{sub}']['fields'].get('Shortfall Reason', '?'))
               for sub, name in (('SUB-0004', 'Derek Olsen'), ('SUB-0009', 'Lucia Gomez'), ('SUB-0011', 'Aisha Khan'))]
    c.r.check(s, 'Slack says nothing new and 13 planned before, and lists the 3 that are not full again with their reasons; '
                 'the Sync Log row is closed as Partial',
              'Nothing new to plan this run' in t and 'Planned before: 13 boxes (3 not full).' in t and '*Not fully filled (3)*' in t
              and all(f'• {sub} {name}, {why} (planned by an earlier run)' in t for sub, name, why in earlier)
              and lf.get('Finished At') and lf.get('Items Changed') == 0 and lf.get('Status') == 'Partial', t[:400])


def scenario_box_fails(c, samples):
    s = 'Box save fails mid-run, then the next run resumes'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    c.mock.fail('airtable', status=500, times=3, match={'table': 'Box Shipments', 'operation': 'upsert', 'body_contains': f'BOX-{TARGET}-SUB-0005'})
    status, resp, ex, alert = run_fails(c)
    first4 = {f'BOX-{TARGET}-SUB-000{i}' for i in (1, 2, 3, 4)}
    tries = [e for e in c.mock.log('airtable') if e.get('table') == 'Box Shipments' and e.get('injected')]
    c.r.check(s, 'the run tries the box 3 times, asks Airtable whether it was saved after all, gives the stock back once and stops',
              ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Run Failed' and len(tries) == 3
              and c.run.runs_of(ex, 'Airtable: Find Box') == 1 and c.run.runs_of(ex, 'Airtable: Release Stock') == 1,
              f'{ex["status"]} at {c.run.last_node(ex)}, {len(tries)} tries')
    c.r.check(s, 'the alert names the box, says the reservation was undone, how many boxes are saved and which month to run',
              alert_message(alert) == f'BOX-{TARGET}-SUB-0005 could not be saved (HTTP 500 SERVER_ERROR). Its stock reservation was undone. '
                                      f'4 boxes before it are saved. Run again for {TARGET} to carry on, saved boxes are skipped.',
              alert.replace('\n', ' | ')[:300])
    ok, detail = closed_as_failed(c, resp, alert, 4)
    c.r.check(s, 'the run\'s Sync Log row is closed as Failed with 4 boxes changed and the alert text', ok, detail)
    c.r.check(s, 'only the 4 earlier boxes exist, and the stock reserved is exactly theirs',
              set(c.boxes()) == first4 and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected, first4),
              f'{sorted(c.boxes())}')
    samples['alert_box'] = alert
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'after the outage, the next run succeeds (the closed row does not hold it back)')
    check_boxes(c, s, expected, 'the result is the same as one uninterrupted run (boxes, products, batches)')
    c.r.check(s, 'no double reservation: stock reserved equals one uninterrupted run',
              reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected), '')
    t = c.mock.slack.texts(PACKING)[-1]
    r4 = c.boxes()[f'BOX-{TARGET}-SUB-0004']['fields'].get('Shortfall Reason', '?')
    c.r.check(s, 'Slack says 9 planned this run and 4 before, and still lists all 3 short boxes, SUB-0004 from the failed run too '
                 '(that run never sent its summary)',
              'Planned this run: 9 boxes' in t and 'Planned before: 4 boxes (1 not full).' in t and '*Not fully filled (3)*' in t
              and f'• SUB-0004 Derek Olsen, {r4} (planned by an earlier run)' in t
              and '• SUB-0009 Lucia Gomez, 3 of 4 bars packed.' in t and '• SUB-0011 Aisha Khan, 1 of 4 bars packed.' in t, t[:600])


def scenario_reserve_fails(c):
    s = 'Stock reservation fails'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    seen = {'n': 0}

    def third_reserve(e):
        if e.get('table') == 'Production Batches' and e['method'] == 'PATCH':
            seen['n'] += 1
            return seen['n'] >= 3
        return False
    c.mock.fail('airtable', status=503, times=3, match=third_reserve)
    status, resp, ex, alert = run_fails(c)
    c.r.check(s, 'the run stops before saving the third box and the alert says what is saved',
              ex['status'] == 'error' and set(c.boxes()) == {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'}
              and alert_message(alert) == f'Stock could not be reserved for BOX-{TARGET}-SUB-0003 (HTTP 503 RETRIABLE_ERROR). Nothing is saved '
                                          f'for this box. 2 boxes before it are saved. Run again for {TARGET} to carry on, saved boxes are skipped.',
              alert.replace('\n', ' | ')[:300])
    ok, detail = closed_as_failed(c, resp, alert, 2)
    c.r.check(s, 'only the first two boxes hold stock (the third box\'s batches were set back once), the Sync Log row is closed as Failed',
              reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected, {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'})
              and c.run.runs_of(ex, 'Airtable: Release Stock') == 1 and ok, detail)


def scenario_release_fails(c):
    s = 'Box save and the undo both fail'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    state = {'box_failed': 0}

    def box(e):
        hit = e.get('table') == 'Box Shipments' and e.get('operation') == 'upsert' and f'BOX-{TARGET}-SUB-0002' in json.dumps(e.get('body'))
        if hit:
            state['box_failed'] += 1
        return hit

    def release(e):
        return state['box_failed'] >= 3 and e.get('table') == 'Production Batches' and e['method'] == 'PATCH'
    c.mock.fail('airtable', status=500, times=3, match=box)
    c.mock.fail('airtable', status=500, times=3, match=release)
    status, resp, ex, alert = run_fails(c)
    codes = ', '.join(dict.fromkeys(expected[f'BOX-{TARGET}-SUB-0002']['codes']))
    c.r.check(s, 'the alert lists the batches to fix by hand',
              ex['status'] == 'error' and alert_message(alert) == f'BOX-{TARGET}-SUB-0002 could not be saved (HTTP 500 SERVER_ERROR) and its stock '
              f'reservation could not be undone (HTTP 500 SERVER_ERROR). Lower Quantity Reserved by 1 on {codes} by hand, then run again. '
              '1 box before it is saved.', alert.replace('\n', ' | ')[:400])
    c.r.check(s, 'the base matches the alert: SUB-0001 saved, SUB-0002 has no box but still holds its stock',
              set(c.boxes()) == {f'BOX-{TARGET}-SUB-0001'}
              and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected, {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'}), '')


def scenario_history_fails(c):
    s = 'Box History fails, then the next run adds the missing rows'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    c.mock.fail('airtable', status=500, times=3, match={'table': 'Box History', 'body_contains': f'{TARGET} / SUB-0003 /'})
    status, resp, ex, alert = run_fails(c)
    c.r.check(s, 'the run stops and the alert says the box and stock are saved',
              ex['status'] == 'error' and alert_message(alert) == f'Box History rows for BOX-{TARGET}-SUB-0003 could not be saved (HTTP 500 '
              f'SERVER_ERROR). The box and its stock reservation are saved, and the next run adds the missing rows. 2 boxes before it are saved. '
              f'Run again for {TARGET} to carry on, saved boxes are skipped.'
              and f'BOX-{TARGET}-SUB-0003' in c.boxes() and not [h for h in c.history() if '/ SUB-0003 /' in h], alert.replace('\n', ' | ')[:300])
    ok, detail = closed_as_failed(c, resp, alert, 3)
    c.r.check(s, 'the Sync Log row is closed as Failed with 3 boxes changed', ok, detail)
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'the next run succeeds')
    hw = [e for e in c.writes(mark) if e['table'] == 'Box History' and '/ SUB-0003 /' in json.dumps(e['body'])]
    rows = [h for h in c.history().values() if '/ SUB-0003 /' in h['fields']['History ID']]
    c.r.check(s, 'SUB-0003 gets its 4 missing rows, linked to its existing box, and no second reservation',
              len(hw) == 1 and len(rows) == 4 and all(h['fields']['Box Shipment'] == [c.boxes()[f'BOX-{TARGET}-SUB-0003']['id']] for h in rows)
              and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected), f'{len(rows)} rows')
    check_boxes(c, s, expected, 'all 13 boxes match one uninterrupted run')
    t = c.mock.slack.texts(PACKING)[-1]
    c.r.check(s, 'Slack mentions the 4 added rows', 'Added 4 missing Box History rows for boxes planned before.' in t, t[-160:])


def scenario_read_fails(c):
    s = 'A field was renamed in Airtable'
    fresh(c.mock)
    body = {'error': {'type': 'UNKNOWN_FIELD_NAME', 'message': 'Unknown field name: "Contains Allergens"'}}
    c.mock.fail('airtable', status=422, times=3, body=body, match={'table': 'Products', 'operation': 'list'})
    mark = c.mock.mark()
    status, resp, ex, alert = run_fails(c)
    c.r.check(s, 'the run stops and the alert names the table and the Airtable error',
              ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Run Failed'
              and alert_message(alert) == 'Reading Products from Airtable failed with HTTP 422 UNKNOWN_FIELD_NAME. No box or stock was changed. '
                                          'Check AIRTABLE_TOKEN, AIRTABLE_BASE_ID and that the field names match base/schema.json, then run again.',
              alert.replace('\n', ' | ')[:300])
    ok, detail = closed_as_failed(c, resp, alert, 0)
    c.r.check(s, 'the only writes are the run\'s own Sync Log row, closed as Failed; no box', ok and not c.boxes()
              and [(e['table'], e['operation']) for e in c.writes(mark)] == [('Sync Log', 'upsert')] * 2, detail)


def scenario_rate_limits(c):
    s = 'Rate limit during a read (another app used up the base)'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    c.mock.fail('airtable', status=429, times=3, match={'table': 'Products', 'operation': 'list'})
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'n8n retries 3 times, the run waits out the 30 second penalty and succeeds')
    calls = c.airtable(mark)
    seq = [(e['table'], e.get('operation'), e['status']) for e in calls[:10]]
    # calls[5] is the third 429 on Products, calls[6] the run claiming its Sync Log row again after the penalty wait
    gap = (datetime.fromisoformat(calls[6]['time'].replace('Z', '+00:00')) - datetime.fromisoformat(calls[5]['time'].replace('Z', '+00:00'))).total_seconds() if len(calls) > 6 else 0
    c.r.check(s, 'after a wait of more than 30 seconds the run starts again from its Sync Log row (the same row, updated)',
              seq[:9] == [('Sync Log', 'upsert', 200), ('Sync Log', 'list', 200), ('Subscribers', 'list', 200),
                          ('Products', 'list', 429), ('Products', 'list', 429), ('Products', 'list', 429),
                          ('Sync Log', 'upsert', 200), ('Sync Log', 'list', 200), ('Subscribers', 'list', 200)] and gap > 30
              and len([r for r in c.at.records('Sync Log') if r['fields'].get('Automation') == AUTOMATION]) == 1,
              f'{seq[:9]}, waited {gap:.0f} s')
    check_boxes(c, s, expected)

    s = 'Rate limit while saving a box'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    c.mock.fail('airtable', status=429, times=3, match={'table': 'Box Shipments', 'operation': 'upsert', 'body_contains': f'BOX-{TARGET}-SUB-0003'})
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'the pass waits 31 seconds, starts again and the run succeeds')
    third = expected[f'BOX-{TARGET}-SUB-0003']
    reserves = [e['body'] for e in c.writes(mark) if e['table'] == 'Production Batches'
                and {r['id'] for r in e['body']['records']} == set(third['batches']) and len(e['body']['records']) == len(third['batches'])]
    c.r.check(s, 'the repeated pass sent the same absolute stock numbers again, so nothing is reserved twice',
              len(reserves) == 2 and reserves[0] == reserves[1] and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected)
              and c.run.runs_of(ex, 'Wait: Write Penalty') == 1 and not c.run.ran(ex, 'Airtable: Find Box'), f'{len(reserves)} reserve requests for SUB-0003')
    check_boxes(c, s, expected)

    s = 'Rate limit that does not go away'
    fresh(c.mock)
    c.mock.fail('airtable', status=429, times=-1, match={'table': 'Subscribers'})
    mark = c.mock.mark()
    status, resp, ex, alert = run_fails(c, timeout=300)
    c.mock.clear_failures()
    ops = c.ops(mark)
    round_ = [('Sync Log', 'upsert'), ('Sync Log', 'list')] + [('Subscribers', 'list')] * 3
    ok, detail = closed_as_failed(c, resp, alert, 0)
    c.r.check(s, 'after 2 waits (3 rounds of 3 tries) the run stops, closes its Sync Log row after a last wait, and alerts',
              ex['status'] == 'error' and ops == round_ * 3 + [('Sync Log', 'upsert')] and ok
              and alert_message(alert).startswith('Reading Subscribers from Airtable kept hitting the rate limit (HTTP 429 RATE_LIMIT_REACHED), '
                                                  'also after waiting out the 30 second penalty twice. No box or stock was changed.'),
              f'{len(ops)} requests, {detail} | ' + alert.replace('\n', ' | ')[:240])

    s = 'Rate limit on a box that does not go away'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    c.mock.fail('airtable', status=429, times=-1, match={'table': 'Box Shipments', 'operation': 'upsert', 'body_contains': f'BOX-{TARGET}-SUB-0003'})
    status, resp, ex, alert = run_fails(c, timeout=300)
    c.mock.clear_failures()
    c.r.check(s, 'after 2 waits the box is looked up, gives its stock back and the run stops with an alert',
              ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Run Failed' and c.run.runs_of(ex, 'Wait: Write Penalty') == 2
              and c.run.runs_of(ex, 'Airtable: Find Box') == 1
              and alert_message(alert) == f'BOX-{TARGET}-SUB-0003 could not be saved (HTTP 429 rate limit). Its stock reservation was undone. '
                                          f'2 boxes before it are saved. Run again for {TARGET} to carry on, saved boxes are skipped.'
              and set(c.boxes()) == {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'}
              and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected, {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'}),
              f'{c.run.runs_of(ex, "Wait: Write Penalty")} waits, boxes {sorted(b[-8:] for b in c.boxes())} | ' + alert.replace('\n', ' | ')[:260])


def scenario_page_token_expires(c):
    s = 'A page token expires in the middle of a read'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    body = {'error': {'type': 'LIST_RECORDS_ITERATOR_NOT_AVAILABLE', 'message': 'The list records iterator is not available.'}}
    c.mock.fail('airtable', status=422, times=1, body=body, match={'table': 'Box History', 'operation': 'list', 'body_contains': '"offset"'})
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'the read is retried from the first page and the run succeeds')
    pages = [e for e in c.airtable(mark) if e.get('table') == 'Box History' and e.get('operation') == 'list']
    c.r.check(s, 'Box History was read again in full (2 pages, then 2 pages), so no history row was missed',
              [bool(isinstance(e['body'], dict) and e['body'].get('offset')) for e in pages] == [False, True, False, True]
              and [e['status'] for e in pages] == [200, 422, 200, 200], str([e['status'] for e in pages]))
    check_boxes(c, s, expected)


def scenario_page_limit(c):
    s = 'More records than one run reads'
    fresh(c.mock)
    mark = c.mock.mark()
    status, resp, ex, alert = run_fails(c, timeout=120)
    ok, detail = closed_as_failed(c, resp, alert, 0)
    c.r.check(s, 'with the page limit at 1, the 2 page Box History read stops the run before any box or stock write',
              ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Run Failed'
              and alert_message(alert) == 'The plan was not made, no box or stock was changed. Box History has more records than one run reads. '
                                          'Raise maxRequests on its read node, then run again.'
              and [(e['table'], e['operation']) for e in c.writes(mark)] == [('Sync Log', 'upsert')] * 2 and ok,
              alert.replace('\n', ' | ')[:240])


def scenario_data_mismatch(c):
    s = 'Allergen data that does not add up (ingredients)'
    fresh(c.mock)
    at = c.at
    orange = at.by_primary('Products', 'DC-BAR-DARK-ORANGE')
    at.update('Products', orange['id'], {'Ingredients': orange['fields']['Ingredients'] + ' Topped with toasted almonds.'})
    cookie = at.by_primary('Products', 'DC-BAR-WHITE-COOKIE')
    at.update('Products', cookie['id'], {'Ingredients': None})
    expected = expected_plan(at, TARGET, TODAY, extra_allergens={'DC-BAR-DARK-ORANGE': {'Tree Nuts'}}, unverified={'DC-BAR-WHITE-COOKIE'})
    slack_n = len(c.mock.slack.texts(PACKING))
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    got = {sid: [at.primary_name('Products', p) for p in r['fields'].get('Products', [])] for sid, r in c.boxes().items()}
    needs = {sid: subs[r['fields']['Subscriber'][0]].get('Dietary Needs', []) for sid, r in c.boxes().items()}
    c.r.check(s, 'the orange bar (almonds in the text, no nut allergen listed) reaches no nut-free box, but others still get it',
              not any('DC-BAR-DARK-ORANGE' in got[k] for k in got if 'Nut-Free' in needs[k]) and any('DC-BAR-DARK-ORANGE' in got[k] for k in got),
              str({k[-8:]: 'DC-BAR-DARK-ORANGE' in v for k, v in got.items()}))
    with_cookie = [k for k in got if 'DC-BAR-WHITE-COOKIE' in got[k]]
    c.r.check(s, 'the bar with no ingredient list goes only to subscribers without dietary needs (and does go to some)',
              with_cookie and all(not needs[k] for k in with_cookie), str([k[-8:] for k in with_cookie]))
    t = (c.packing(slack_n) or [''])[0]
    c.r.check(s, 'Slack lists both products under data to check',
              '*Product data to check (2)*' in t and 'DC-BAR-DARK-ORANGE: the ingredients mention "almonds" but Tree Nuts is not listed' in t
              and 'DC-BAR-WHITE-COOKIE: no ingredient list' in t, t[-400:])


def scenario_data_signals(c):
    s = 'Allergen data that does not add up (flavor note, chocolate type, duplicate SKU)'
    fresh(c.mock)
    at = c.at
    salt = at.by_primary('Products', 'DC-BAR-DARK-SEASALT')
    at.update('Products', salt['id'], {'Flavor Notes': ['Sea Salt', 'Hazelnut']})
    esp = at.by_primary('Products', 'DC-BAR-DARK-ESPRESSO')
    at.update('Products', esp['id'], {'Chocolate Type': 'Milk'})
    cherry = at.by_primary('Products', 'DC-BAR-RUBY-CHERRY')
    copy = {k: v for k, v in cherry['fields'].items() if k in ('SKU', 'Status', 'Category', 'Chocolate Type', 'Flavor Family', 'Flavor Notes',
                                                                 'Contains Allergens', 'Diet Labels', 'Ingredients', 'Subscription Eligible')}
    copy['Name'] = 'Ruby Cherry Bar (second record)'
    at.insert('Products', copy)
    expected = expected_plan(at, TARGET, TODAY, extra_allergens={'DC-BAR-DARK-SEASALT': {'Tree Nuts'}, 'DC-BAR-DARK-ESPRESSO': {'Milk'}},
                             not_vegan={'DC-BAR-DARK-ESPRESSO'}, exclude_skus={'DC-BAR-RUBY-CHERRY'})
    slack_n = len(c.mock.slack.texts(PACKING))
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    got = {sid: [at.primary_name('Products', p) for p in r['fields'].get('Products', [])] for sid, r in c.boxes().items()}
    needs = {sid: subs[r['fields']['Subscriber'][0]].get('Dietary Needs', []) for sid, r in c.boxes().items()}
    salt_in = [k for k in got if 'DC-BAR-DARK-SEASALT' in got[k]]
    esp_in = [k for k in got if 'DC-BAR-DARK-ESPRESSO' in got[k]]
    c.r.check(s, 'sea salt (Hazelnut flavor note, no nut allergen) reaches no nut-free box; espresso (Milk chocolate, no Milk listed) '
                 'no dairy-free or vegan box; both still go to others',
              salt_in and esp_in and not any('Nut-Free' in needs[k] for k in salt_in)
              and not any({'Dairy-Free', 'Vegan'} & set(needs[k]) for k in esp_in), f'sea salt {[k[-8:] for k in salt_in]}, espresso {[k[-8:] for k in esp_in]}')
    c.r.check(s, 'neither of the two records with the SKU DC-BAR-RUBY-CHERRY goes in a box',
              not any('DC-BAR-RUBY-CHERRY' in v for v in got.values()), '')
    t = (c.packing(slack_n) or [''])[0]
    c.r.check(s, 'Slack lists all four problems under data to check',
              '*Product data to check (4)*' in t
              and '• DC-BAR-RUBY-CHERRY: 2 product records have this SKU, so none of them goes in a box until the duplicate is removed.' in t
              and '• DC-BAR-DARK-ESPRESSO: it is Milk chocolate but Milk is not listed.' in t
              and '• DC-BAR-DARK-ESPRESSO: it is labelled Vegan but it is Milk chocolate.' in t
              and '• DC-BAR-DARK-SEASALT: it has the flavor note Hazelnut but Tree Nuts is not listed.' in t, t[-600:])


def scenario_nut_free_scarcity(c):
    s = 'Nut-free subscribers when only nut bars are left'
    fresh(c.mock)
    at = c.at
    prods = {r['id']: r['fields'] for r in at.records('Products')}
    safe = {pid for pid, f in prods.items() if not ({'Tree Nuts', 'Peanuts'} & (set(f.get('Contains Allergens', [])) | set(f.get('May Contain Traces', []))))}
    d85 = at.by_primary('Products', 'DC-BAR-DARK-85')
    at.update('Products', d85['id'], {'May Contain Traces': ['Peanuts']})          # in stock, but now with a peanut trace
    for b in at.records('Production Batches'):
        f = b['fields']
        if f.get('Product', [None])[0] in safe and f['Product'][0] != d85['id'] and f.get('Status') == 'Released':
            at.update('Production Batches', b['id'], {'Quantity Reserved': f['Quantity Made'] - f.get('Quantity Sold', 0)})
    expected = expected_plan(at, TARGET, TODAY)
    before = c.reserved()
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    boxes = c.boxes()
    nut_free = {sid: r['fields'] for sid, r in boxes.items() if 'Nut-Free' in subs[r['fields']['Subscriber'][0]].get('Dietary Needs', [])}
    others = {sid: r['fields'] for sid, r in boxes.items() if sid not in nut_free}
    c.r.check(s, 'all 5 nut-free boxes are Empty and say the safe bars are out of stock; no nut or trace bar is used to fill them',
              len(nut_free) == 5 and all(f['Fill Status'] == 'Empty' and f['Bars Packed'] == 0 and 'Products' not in f
                                         and 'out of stock' in f.get('Shortfall Reason', '') and 'not suitable for' in f.get('Shortfall Reason', '')
                                         for f in nut_free.values()),
              '; '.join(f'{k[-8:]} {f["Fill Status"]} {f.get("Shortfall Reason", "")[:60]}' for k, f in nut_free.items()))
    c.r.check(s, 'the other subscribers still get bars, the peanut-trace DARK-85 included; no history or stock for the empty boxes',
              sum(1 for f in others.values() if f.get('Products')) >= 6
              and any(d85['id'] in f.get('Products', []) for f in others.values())
              and not [h for h in c.history().values() if h['fields']['History ID'].split(' / ')[1] in {k[-8:] for k in nut_free}]
              and reservation_delta(before, c.reserved()) == units_by_batch(at, expected),
              f'{sum(1 for f in others.values() if f.get("Products"))} other boxes with bars')


def usable_left(at, pid, month=TARGET, buffer_days=14):
    """Units of a product the planner may still use: Released batches made by today with enough shelf life."""
    cutoff = max(TODAY, date.fromisoformat(month + '-01')) + timedelta(days=buffer_days)
    total = 0
    for b in at.records('Production Batches'):
        f = b['fields']
        if (f.get('Product') == [pid] and f.get('Status') == 'Released' and f.get('Produced On')
                and date.fromisoformat(f['Produced On']) <= TODAY and f.get('Best Before') and date.fromisoformat(f['Best Before']) > cutoff):
            total += max(f.get('Quantity Made', 0) - f.get('Quantity Reserved', 0) - f.get('Quantity Sold', 0), 0)
    return total


def scenario_nut_free_short_stock(c):
    s = 'Nut-free subscribers when the safe bars run short'
    fresh(c.mock)
    at = c.at
    prods = {r['id']: r['fields'] for r in at.records('Products')}
    listed = lambda f: set(f.get('Contains Allergens', [])) | set(f.get('May Contain Traces', []))  # noqa: E731
    nutty = {pid for pid, f in prods.items() if NUT_ALLERGENS & listed(f)}
    eligible = {pid for pid, f in prods.items() if f.get('Status') == 'Active' and f.get('Subscription Eligible') is True
                and f.get('Category') == 'Bar'}
    safe = eligible - nutty
    # The three trace traps also say "Made on shared equipment with tree nuts." in their ingredients. Take that
    # sentence out, so May Contain Traces is the only sign of the nuts and the field itself is what is tested.
    for sku in NUT_TRAPS:
        r = at.by_primary('Products', sku)
        at.update('Products', r['id'], {'Ingredients': r['fields']['Ingredients'].replace(' Made on shared equipment with tree nuts.', '')})
        assert 'nut' not in at.record('Products', r['id'])['fields']['Ingredients'].lower(), sku
    # every nut-safe bar keeps exactly 1 unit (on its earliest usable batch); the nut and trace bars keep their stock
    cutoff = max(TODAY, date.fromisoformat(TARGET + '-01')) + timedelta(days=14)
    usable = {}
    for b in at.records('Production Batches'):
        f = b['fields']
        pid = (f.get('Product') or [None])[0]
        if pid in safe and f.get('Status') == 'Released' and date.fromisoformat(f['Produced On']) <= TODAY \
                and date.fromisoformat(f['Best Before']) > cutoff:
            usable.setdefault(pid, []).append(b)
    for pid, bs in usable.items():
        bs.sort(key=lambda b: (b['fields']['Best Before'], b['fields']['Batch Code']))
        for i, b in enumerate(bs):
            f = b['fields']
            at.update('Production Batches', b['id'], {'Quantity Reserved': max(f['Quantity Made'] - f.get('Quantity Sold', 0) - (1 if i == 0 else 0), 0)})
    safe_units = sum(usable_left(at, pid) for pid in safe)
    expected = expected_plan(at, TARGET, TODAY)
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    boxes = c.boxes()
    needs = {sid: subs[r['fields']['Subscriber'][0]].get('Dietary Needs', []) for sid, r in boxes.items()}
    nut_free = {sid: r['fields'] for sid, r in boxes.items() if 'Nut-Free' in needs[sid]}
    used = {}
    for r in boxes.values():
        for p in r['fields'].get('Products', []):
            used[p] = used.get(p, 0) + 1
    hits = [(sid[-8:], prods[p]['SKU']) for sid, f in nut_free.items() for p in f.get('Products', [])
            if p in nutty or prods[p]['SKU'] in NUT_TRAPS or unsafe_reasons(prods[p], ['Nut-Free'])]
    demand = sum(f['Bars Planned'] for f in nut_free.values())
    c.r.check(s, f'the stock is really short: {len(safe)} nut-safe bars with 1 unit each against {demand} nut-free bars planned',
              len(nut_free) == 5 and safe_units <= len(safe) and safe_units < demand, f'{safe_units} safe units, {demand} wanted')
    c.r.check(s, 'no nut-free box has a bar with tree nuts or peanuts, as an ingredient or as a trace, however short the safe bars are',
              not hits and sum(len(f.get('Products', [])) for f in nut_free.values()) > 0, f'hits {hits}, '
              f'{sum(len(f.get("Products", [])) for f in nut_free.values())} bars in {len(nut_free)} nut-free boxes')
    short = {sid: f for sid, f in nut_free.items() if f['Fill Status'] != 'Full'}
    c.r.check(s, 'the nut-free boxes that could not be filled are Partial or Empty and say why; no safe bar is used twice',
              short and all(f['Bars Packed'] == len(f.get('Products', [])) and 'not suitable for' in f.get('Shortfall Reason', '')
                            and ('used up by longer-standing subscribers' in f.get('Shortfall Reason', '') or 'out of stock' in f.get('Shortfall Reason', ''))
                            for f in short.values())
              and all(n <= 1 for p, n in used.items() if p in safe),
              '; '.join(f'{k[-8:]} {f["Fill Status"]} {f["Bars Packed"]}/{f["Bars Planned"]}' for k, f in sorted(short.items())))
    nut_left = {prods[p]['SKU']: usable_left(at, p) for p in eligible & nutty}
    c.r.check(s, 'the nut and trace bars were there all along: other subscribers got them and every one still has stock',
              all(v > 0 for v in nut_left.values()) and sum(n for p, n in used.items() if p in nutty) > 0
              and all(not (needs[sid] and 'Nut-Free' in needs[sid]) for sid, r in boxes.items() for p in r['fields'].get('Products', []) if p in nutty),
              f'{sum(n for p, n in used.items() if p in nutty)} nut bars to others, left {nut_left}')


def scenario_recheck(c, samples=None):
    s = 'A planned box is checked again after the data changed'
    at = c.at
    d85 = at.by_primary('Products', 'DC-BAR-DARK-85')
    old_traces = d85['fields'].get('May Contain Traces')
    s3 = at.by_primary('Subscribers', 'SUB-0003')
    old_needs = s3['fields'].get('Dietary Needs')
    at.update('Products', d85['id'], {'May Contain Traces': ['Peanuts']})        # the supplier now warns about peanuts
    at.update('Subscribers', s3['id'], {'Dietary Needs': ['Vegan', 'Nut-Free']})  # the subscriber added Nut-Free
    prods = {r['id']: r['fields'] for r in at.records('Products')}
    subs = {r['id']: r['fields'] for r in at.records('Subscribers')}
    want = []
    for sid, r in sorted(c.boxes().items()):
        sub = subs[r['fields']['Subscriber'][0]]
        for p in r['fields'].get('Products', []):
            why = unsafe_reasons(prods[p], sub.get('Dietary Needs', []))
            need = next((n for n in sub.get('Dietary Needs', []) if NEED_LABEL[n] in why), None)
            if need:
                want.append(f'• {sid} for {sub["Subscriber ID"]} {sub["Name"]} has {prods[p]["SKU"]}, which does not suit {need}. '
                            'Take it out by hand before packing.')
    before_b, before_r, before_h = c.boxes(), c.reserved(), c.history()
    slack_n = len(c.mock.slack.texts(PACKING))
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    t = (c.packing(slack_n) or [''])[0]
    got = [line for line in t.split('\n') if line.startswith('• ') and ('Take it out by hand' in line or 'Check it by hand' in line)]
    c.r.check(s, 'the rerun lists exactly the bars that no longer suit (a new peanut trace on DARK-85, SUB-0003 now nut-free), first in the message',
              len(want) == 2 and sorted(got) == sorted(want) and any('SUB-0005' in w and 'DC-BAR-DARK-85' in w for w in want)
              and any('SUB-0003' in w and 'DC-BAR-DARK-RASPBERRY' in w for w in want)
              and '*Bars to take out before packing (2)*' in t and t.index('*Bars to take out before packing') < t.index('*Not fully filled'),
              ' | '.join(got) or t[:300])
    lf = c.run_row(resp.get('runId'))
    c.r.check(s, 'nothing in the base changes (the packing team may be at work); the Sync Log row is Partial',
              c.boxes() == before_b and c.reserved() == before_r and c.history() == before_h
              and [(e['table'], e['operation']) for e in c.writes(mark)] == [('Sync Log', 'upsert')] * 2
              and lf.get('Status') == 'Partial' and 'Bars to take out before packing (2)' in lf.get('Details', ''), json.dumps(lf)[:160])
    at.update('Products', d85['id'], {'May Contain Traces': old_traces})
    at.update('Subscribers', s3['id'], {'Dietary Needs': old_needs})
    if samples is not None:
        samples['slack_recheck'] = t


PROPERTY_JS = Path(__file__).resolve().with_name('allergen_property.js')
TRACES_LINE = 'const listed = new Set([...contains, ...traces]);'
VERIFY_CALL = 'verifyPlan(boxes, writes, products, toPlan, batchById, rawById);'


def property_workflow():
    """A two node workflow that runs allergen_property.js against the exact planner code of "Code: Plan Boxes", and,
    as a control, against a copy that ignores May Contain Traces and skips the final safety check."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))
    plan_js = next(n for n in wf['nodes'] if n['name'] == 'Code: Plan Boxes')['parameters']['jsCode']
    planner = plan_js.split('\n// ---- n8n glue')[0]
    if TRACES_LINE not in planner or VERIFY_CALL not in planner:
        raise ValueError('the planner changed: update TRACES_LINE and VERIFY_CALL for the control copy')
    broken = planner.replace(TRACES_LINE, 'const listed = new Set([...contains]);').replace(VERIFY_CALL, '')
    prop = re.sub(r"\nif \(typeof module !== 'undefined'\).*", '', PROPERTY_JS.read_text(encoding='utf-8'))
    code = ('const realPlanBoxes = (function () {\n' + planner + '\nreturn planBoxes;\n})();\n'
            'const brokenPlanBoxes = (function () {\n' + broken + '\nreturn planBoxes;\n})();\n' + prop + '\n'
            'const body = $input.first().json.body || {};\n'
            'const today = new Date().toISOString().slice(0, 10);\n'
            'const result = allergenPropertyCheck(realPlanBoxes, { runs: body.runs, seed: body.seed, today });\n'
            'const control = allergenPropertyCheck(brokenPlanBoxes, { runs: 300, seed: body.seed, today });\n'
            "return [{ json: { result, control: control.stats, controlExample: control.violations[0] || '' } }];\n")
    hook, node = 'Webhook: Property Check', 'Code: Allergen Property Check'
    return {'name': 'property check', 'settings': {'executionOrder': 'v1'},
            'nodes': [{'id': str(uuid.uuid4()), 'name': hook, 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2, 'position': [0, 0],
                       'webhookId': str(uuid.uuid4()),
                       'parameters': {'httpMethod': 'POST', 'path': 'allergen-property', 'responseMode': 'lastNode',
                                      'responseData': 'firstEntryJson', 'options': {}}},
                      {'id': str(uuid.uuid4()), 'name': node, 'type': 'n8n-nodes-base.code', 'typeVersion': 2, 'position': [240, 0],
                       'parameters': {'jsCode': code}}],
            'connections': {hook: {'main': [[{'node': node, 'type': 'main', 'index': 0}]]}}}


def scenario_property(c, runs=1500):
    s = 'Random bases, the planner code from the workflow run in n8n'
    status, text = c.run.post('allergen-property', {'runs': runs, 'seed': 1}, timeout=240)
    ex = c.run.wait(timeout=240)
    try:
        out = json.loads(text)
    except ValueError:
        out = {}
    res, ctl = out.get('result') or {}, out.get('control') or {}
    st = res.get('stats') or {}
    c.r.check(s, f'{runs} random bases (seeds 1 to {runs}) with scarce stock, nut bars everywhere and hidden allergen hints: '
                 'no nut-free subscriber ever gets a bar with tree nuts or peanuts, as an ingredient or a trace',
              status == 200 and ex['status'] == 'success' and st.get('runs') == runs and st.get('nutFreeBreaches') == 0
              and st.get('nutFreeBoxes', 0) >= 2000 and st.get('nutFreeBars', 0) >= 2000 and st.get('nutFreeShort', 0) >= 1000
              and st.get('nutFreeEmpty', 0) >= 100 and st.get('hiddenNutProducts', 0) >= 1000 and st.get('nutBarsToOthers', 0) >= 1000,
              json.dumps({k: st.get(k) for k in ('runs', 'nutFreeBoxes', 'nutFreeBars', 'nutFreeShort', 'nutFreeEmpty', 'hiddenNutProducts',
                                                 'nutBarsToOthers', 'nutFreeBreaches')}) if st else f'{status} {text[:200]}')
    c.r.check(s, 'every other rule holds in every plan: dairy, gluten and vegan needs, no ingredient list, dislikes, repeats, '
                 'one unit per bar on a usable batch, no batch over-reserved, a short box had nothing suitable left, '
                 'one box per subscriber, every unsuitable bar in an earlier box reported',
              res.get('ok') is True and st.get('violationCount') == 0 and st.get('plansStopped') == 0 and st.get('boxes', 0) >= 5000
              and st.get('conflictsExpected', 0) >= 100,
              json.dumps({k: st.get(k) for k in ('boxes', 'bars', 'shortBoxes', 'plannedBefore', 'conflictsExpected', 'violationCount')})
              + ' ' + '; '.join((res.get('violations') or [])[:3]))
    c.r.check(s, 'control: the same check run against a copy of the planner that ignores May Contain Traces finds nut-free breaches',
              ctl.get('nutFreeBreaches', 0) > 0, f'{ctl.get("nutFreeBreaches")} breaches in {ctl.get("runs")} bases, for example '
              f'{str(out.get("controlExample", ""))[:160]}')
    return st


def scenario_subscriber_problems(c):
    s = 'Subscriber records a person must fix'
    fresh(c.mock)
    at = c.at
    s12 = at.by_primary('Subscribers', 'SUB-0012')
    at.update('Subscribers', s12['id'], {'Dietary Needs': ['Soy-Free']}, typecast=True)
    s13 = at.by_primary('Subscribers', 'SUB-0013')
    at.update('Subscribers', s13['id'], {'Plan': None})
    s10 = at.by_primary('Subscribers', 'SUB-0010')
    at.update('Subscribers', s10['id'], {'Plan': '12 Bars'}, typecast=True)
    slack_n = len(c.mock.slack.texts(PACKING))
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    t = (c.packing(slack_n) or [''])[0]
    c.r.check(s, 'an unknown dietary need, a missing plan and a 12 bar plan are not guessed: no box, listed in Slack',
              not {f'BOX-{TARGET}-SUB-0010', f'BOX-{TARGET}-SUB-0012', f'BOX-{TARGET}-SUB-0013'} & set(c.boxes()) and len(c.boxes()) == 10
              and '*Not planned, the subscriber record needs a person (3)*' in t
              and 'SUB-0012 Ben Carter, the dietary need "Soy-Free" has no safety rule yet' in t and 'SUB-0013 Sofia Marino, no plan is set.' in t
              and 'SUB-0010 Tom Becker, the plan "12 Bars" has more than 10 bars, which this workflow does not plan.' in t, t[-500:])
    at.update('Subscribers', s13['id'], {'Plan': '4 Bars'})
    expected13 = expected_plan(at, TARGET, TODAY)
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'after the plan is fixed, the next run succeeds')
    new = [e for e in c.writes(mark) if e['table'] == 'Box Shipments']
    c.r.check(s, 'only SUB-0013 gets a box, from the stock that is left',
              len(new) == 1 and f'BOX-{TARGET}-SUB-0013' in c.boxes() and len(c.boxes()) == 11 and list(expected13) == [f'BOX-{TARGET}-SUB-0013']
              and c.boxes()[f'BOX-{TARGET}-SUB-0013']['fields'].get('Products') == expected13[f'BOX-{TARGET}-SUB-0013']['products'], '')


def scenario_out_of_stock(c):
    s = 'Sold out bar'
    fresh(c.mock)
    at = c.at
    b = at.by_primary('Production Batches', 'B-DSS-260910')
    at.update('Production Batches', b['id'], {'Quantity Reserved': 180})
    expected = expected_plan(at, TARGET, TODAY)
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    box = c.boxes()[f'BOX-{TARGET}-SUB-0011']['fields']
    c.r.check(s, 'SUB-0011 (only sea salt was left for them) gets an Empty box that says why',
              box['Fill Status'] == 'Empty' and box['Bars Packed'] == 0 and 'Products' not in box
              and '1 out of stock (DC-BAR-DARK-SEASALT)' in box.get('Shortfall Reason', ''), box.get('Shortfall Reason', ''))
    w = c.writes(mark)
    per_table = {t: sum(1 for e in w if e['table'] == t) for t in ('Box Shipments', 'Production Batches', 'Box History')}
    c.r.check(s, 'an Empty box writes the box only: 13 box upserts, but 12 reservations and 12 history upserts',
              per_table == {'Box Shipments': 13, 'Production Batches': 12, 'Box History': 12}, str(per_table))


def scenario_this_month(c):
    s = 'Run Now for this month (the bars are already in Box History)'
    fresh(c.mock)
    before_r = c.reserved()
    history_before = len(c.at.records('Box History'))
    slack_n = len(c.mock.slack.texts(PACKING))
    mark = c.mock.mark()
    status, resp, ex = run_ok(c, s, {'month': THIS_MONTH})
    w = c.writes(mark)
    c.r.check(s, 'every Active subscriber already has this month\'s bars in Box History (recorded before this workflow), '
              'so no box is planned, nothing is reserved, and the only writes are the Sync Log row',
              resp.get('month') == THIS_MONTH and not c.boxes(THIS_MONTH) and c.reserved() == before_r
              and len(c.at.records('Box History')) == history_before and [(e['table'], e['operation']) for e in w] == [('Sync Log', 'upsert')] * 2,
              f'{resp.get("month")}, {len(c.boxes(THIS_MONTH))} boxes, writes {[(e["table"], e["operation"]) for e in w]}')
    t = (c.packing(slack_n) or [''])[0]
    lf = c.run_row(resp.get('runId'))
    c.r.check(s, 'Slack lists all 13 with their bar counts and says how to plan one anyway; the Sync Log row is Partial',
              f'*Bars for {THIS_MONTH} already in Box History without a Box Shipment, so no new box (13)*' in t
              and '• SUB-0001 Maya Chen (6 bars)' in t and '• SUB-0011 Aisha Khan (3 bars)' in t
              and 'Nothing new to plan this run, no stock was reserved.' in t
              and f"delete that subscriber's Box History rows for {THIS_MONTH} and run again." in t
              and lf.get('Status') == 'Partial' and lf.get('Items Changed') == 0, t[:420])


def scenario_cancelled_box(c):
    s = 'A cancelled box does not count as received'
    fresh(c.mock)
    at = c.at
    sub = at.by_primary('Subscribers', 'SUB-0001')
    mine = [h for h in at.records('Box History') if h['fields'].get('Subscriber') == [sub['id']]]
    last = [h for h in mine if h['fields']['Box Month'] == THIS_MONTH]
    before = [h for h in mine if h['fields']['Box Month'] == month_add(THIS_MONTH, -1)]
    last_ids = {h['fields']['Product'][0] for h in last}
    before_ids = {h['fields']['Product'][0] for h in before}
    box = at.insert('Box Shipments', {'Shipment ID': f'BOX-{THIS_MONTH}-SUB-0001', 'Subscriber': [sub['id']], 'Box Month': THIS_MONTH,
                                      'Products': sorted(last_ids), 'Bars Planned': 6, 'Bars Packed': len(last_ids),
                                      'Fill Status': 'Full', 'Status': 'Cancelled'})
    for h in last:
        at.update('Box History', h['id'], {'Box Shipment': [box['id']]})
    expected = expected_plan(at, TARGET, TODAY)
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    got = set(c.boxes()[f'BOX-{TARGET}-SUB-0001']['fields'].get('Products', []))
    names = lambda ids: sorted(at.primary_name('Products', p) for p in ids)  # noqa: E731
    c.r.check(s, 'SUB-0001\'s last box was cancelled, so the repeat check uses the box before it: none of those bars, '
              'and bars from the cancelled box can come back',
              len(last) == 6 and len(before) == 6 and not got & before_ids and got & last_ids,
              f'packed {names(got)}; from the cancelled box {names(got & last_ids)}')


def scenario_slack_fails(c):
    s = 'Slack is down'
    c.mock.slack.archived_paths.add(PACKING)
    mark = c.mock.mark()
    status, resp, ex, alert = run_fails(c, timeout=120)
    c.mock.slack.archived_paths.discard(PACKING)
    tries = [e for e in c.mock.log('slack', since=mark) if PACKING in e['path']]
    lf = c.run_row(resp.get('runId'))
    c.r.check(s, 'Slack is tried 3 times, the Sync Log row is still closed with 1 error, then the run fails with an alert',
              len(tries) == 3 and lf.get('Errors') == 1 and lf.get('Finished At') and lf.get('Status') == 'Partial'
              and 'The Slack summary could not be posted.' in lf.get('Details', '')
              and ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Slack Failed'
              and alert_message(alert) == f'The Slack summary for {TARGET} could not be posted (HTTP 410). The boxes and the Sync Log row are saved. '
                                          'Check SLACK_PACKING_WEBHOOK_URL.', alert.replace('\n', ' | ')[:300])


def scenario_sync_log_fails(c):
    s = 'Sync Log write fails'

    def closing_write(e):
        body = json.dumps(e.get('body'))
        return e.get('table') == 'Sync Log' and e.get('operation') == 'upsert' and '"Finished At"' in body and '"Failed"' not in body
    c.mock.fail('airtable', status=500, times=3, match=closing_write)
    slack_n = len(c.mock.slack.texts(PACKING))
    status, resp, ex, alert = run_fails(c, timeout=120)
    lf = c.run_row(resp.get('runId'))
    c.r.check(s, 'the Slack summary still goes out first, then the run fails with an alert that says the row stays open',
              len(c.packing(slack_n)) == 1 and ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Sync Log Failed'
              and alert_message(alert) == f'The Sync Log row {resp.get("runId")} could not be closed (HTTP 500). The boxes are saved and the Slack '
                                          'summary was posted. New curation runs wait for this row for up to 60 minutes. Set its Status by hand to run sooner.'
              and lf and not lf.get('Finished At') and not lf.get('Status'), alert.replace('\n', ' | ')[:300])
    return resp.get('runId')


def scenario_open_rows(c, left_open):
    s = 'A Sync Log row left open holds new runs back'
    mark = c.mock.mark()
    status, resp, ex, alert = run_fails(c, timeout=120)
    ok, detail = closed_as_failed(c, resp, alert, 0)
    c.r.check(s, 'the next run stops after 3 Sync Log calls and names the open row; nothing else is read or written',
              status == 202 and ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Run Failed'
              and alert_message(alert).startswith(f'Another curation run ({left_open}, started ')
              and 'is still running, so this run stopped before reading anything.' in alert
              and c.ops(mark) == [('Sync Log', 'upsert'), ('Sync Log', 'list'), ('Sync Log', 'upsert')] and ok,
              f'{c.ops(mark)} | ' + alert.replace('\n', ' | ')[:240])
    row = next(r for r in c.at.records('Sync Log') if r['fields'].get('Run ID') == left_open)
    c.at.update('Sync Log', row['id'], {'Status': 'Failed'})
    real_now = c.mock.clock.now()
    c.mock.clock.set(real_now - timedelta(hours=2))
    stale = c.at.insert('Sync Log', {'Run ID': f'boxes-{TARGET}-crashed-run', 'Automation': AUTOMATION, 'Trigger': 'Schedule',
                                     'Started At': (real_now - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')})
    c.mock.clock.live()
    other = c.at.insert('Sync Log', {'Run ID': 'inventory-open-row', 'Automation': '02 Inventory Sync', 'Trigger': 'Schedule',
                                     'Started At': real_now.strftime('%Y-%m-%dT%H:%M:%SZ')})
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'with a Status set on that row by hand, an open row 2 hours old (a run that died) '
                                                       'and an open row of another automation, the next run goes ahead')
    c.r.check(s, 'the old and the other automation\'s rows are left alone', c.at.record('Sync Log', stale['id'])['fields'] == stale['fields']
              and c.at.record('Sync Log', other['id'])['fields'] == other['fields'] and c.run_row(resp.get('runId')).get('Finished At'), '')


def scenario_double_click(c):
    for gap, s in ((0.0, 'Run Now twice at the same moment'), (4.0, 'Run Now again 4 seconds later')):
        fresh(c.mock)
        expected = expected_plan(c.at, TARGET, TODAY)
        before = c.reserved()
        mark = c.mock.mark()
        res = []
        t1 = threading.Thread(target=lambda: res.append(c.post({'month': TARGET})))
        t1.start()
        time.sleep(gap)
        res.append(c.post({'month': TARGET}))
        t1.join()
        runs = c.run.wait_all(timeout=240)
        ok_runs = [e for e in runs if e['status'] == 'success']
        bad_runs = [e for e in runs if e['status'] == 'error']
        winner = next((b['runId'] for st, b in res if isinstance(b, dict) and ok_runs and b.get('runId', '').endswith('-' + str(ok_runs[0]['id']))), '')
        loser = next((b['runId'] for st, b in res if isinstance(b, dict) and b.get('runId') != winner), '')
        alert = c.run.alert_for(bad_runs[0], timeout=40) if bad_runs else ''
        c.r.check(s, 'both calls get 202; exactly one run plans, the other stops before reading and names the first',
                  [st for st, _ in res] == [202, 202] and len(ok_runs) == 1 and len(bad_runs) == 1 and winner and loser
                  and c.run.last_node(bad_runs[0]) == 'Stop: Run Failed'
                  and alert_message(alert or '').startswith(f'Another curation run ({winner}, started '), (alert or '').replace('\n', ' | ')[:220])
        check_boxes(c, s, expected, 'the boxes are exactly those of one run')
        calls = c.airtable(mark)
        c.r.check(s, 'stock reserved once: Quantity Reserved equals one run, 63 history rows',
                  reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected) and len(c.history()) == 63, '')
        lw, ll = c.run_row(winner), c.run_row(loser)
        c.r.check(s, 'no 429, at most 4 Airtable requests in any second, the second run made only its 3 Sync Log calls, both rows closed',
                  all(e['status'] == 200 for e in calls) and max_in_one_second(calls) <= 4
                  and sum(1 for e in calls if e['table'] == 'Subscribers') == 1 and sum(1 for e in calls if e['table'] == 'Sync Log') == 6
                  and lw.get('Status') == 'Partial' and lw.get('Finished At') and ll.get('Status') == 'Failed' and ll.get('Finished At')
                  and ll.get('Details') == alert_message(alert or ''),
                  f'{len(calls)} requests, busiest second {max_in_one_second(calls)}, 429s {sum(1 for e in calls if e["status"] == 429)}')


def scenario_lost_answers(c):
    s = 'A box is saved but the answer is lost'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    c.mock.lose_answer('airtable', lambda e: e.get('table') == 'Box Shipments' and e.get('operation') == 'upsert'
                       and f'BOX-{TARGET}-SUB-0005' in json.dumps(e.get('body')), status=500, times=3)
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'Airtable saved the box 3 times but answered 500; the run finds it and succeeds')
    lost = [e for e in c.mock.log('airtable') if e.get('answer_lost')]
    c.r.check(s, 'the box was looked up instead of given up: no stock given back, no alert, one box record',
              len(lost) == 3 and c.run.runs_of(ex, 'Airtable: Find Box') == 1 and not c.run.ran(ex, 'Airtable: Release Stock')
              and c.run.alert_for(ex, timeout=5) is None
              and len([r for r in c.at.records('Box Shipments') if r['fields'].get('Shipment ID') == f'BOX-{TARGET}-SUB-0005']) == 1,
              f'{len(lost)} lost answers')
    check_boxes(c, s, expected, 'all 13 boxes match one uninterrupted run')
    c.r.check(s, 'stock reserved once and SUB-0005\'s history rows written',
              reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected) and len(c.history()) == 63
              and len([h for h in c.history() if '/ SUB-0005 /' in h]) == len(expected[f'BOX-{TARGET}-SUB-0005']['skus']), '')

    s = 'A reservation is saved but the answer is lost'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY)
    before = c.reserved()
    seen = {'n': 0}

    def third_reserve(e):
        if e.get('table') == 'Production Batches' and e['method'] == 'PATCH':
            seen['n'] += 1
            return seen['n'] >= 3
        return False
    c.mock.lose_answer('airtable', third_reserve, status=500, times=3)
    status, resp, ex, alert = run_fails(c)
    first2 = {f'BOX-{TARGET}-SUB-0001', f'BOX-{TARGET}-SUB-0002'}
    c.r.check(s, 'the run stops, and the reservation that went through is set back: only the first 2 boxes hold stock',
              ex['status'] == 'error' and set(c.boxes()) == first2
              and alert_message(alert).startswith(f'Stock could not be reserved for BOX-{TARGET}-SUB-0003 (HTTP 500 SERVER_ERROR). Nothing is saved for this box.')
              and reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected, first2), alert.replace('\n', ' | ')[:240])
    status, resp, ex = run_ok(c, s, {'month': TARGET}, 'the next run succeeds')
    c.r.check(s, 'no double reservation: the result equals one uninterrupted run',
              reservation_delta(before, c.reserved()) == units_by_batch(c.at, expected), '')
    check_boxes(c, s, expected, 'all 13 boxes match one uninterrupted run')


def scenario_repeat_window_2(c):
    s = 'REPEAT_WINDOW=2'
    fresh(c.mock)
    expected = expected_plan(c.at, TARGET, TODAY, repeat_window=2)
    status, resp, ex = run_ok(c, s, {'month': TARGET})
    check_boxes(c, s, expected)
    repeats = [sid for sid, e in expected.items() if set(c.boxes()[sid]['fields'].get('Products', [])) & e['window']]
    box4 = c.boxes()[f'BOX-{TARGET}-SUB-0004']['fields']
    c.r.check(s, 'no bar from either of the last 2 boxes; SUB-0004 had all 5 of its bars in them, so its box is Empty',
              not repeats and box4['Fill Status'] == 'Empty' and 'in the last 2 boxes (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK)' in box4.get('Shortfall Reason', ''),
              box4.get('Shortfall Reason', ''))


def scenario_bad_settings(c):
    s = 'Settings problem'
    mark = c.mock.mark()
    status, resp = c.post({'month': TARGET})
    ex = c.run.wait(timeout=60)
    alert = c.run.alert_for(ex, timeout=40) or ''
    c.r.check(s, 'Run Now gets 500 with the reason, the run fails with an alert, nothing is read or written',
              status == 500 and resp.get('error') == 'Settings problem. REPEAT_WINDOW must be a whole number from 0 to 12.'
              and ex['status'] == 'error' and c.run.last_node(ex) == 'Stop: Bad Run Now Settings'
              and alert_message(alert) == 'Settings problem. REPEAT_WINDOW must be a whole number from 0 to 12.'
              and not c.mock.log('airtable', since=mark), f'{status} {resp} | {alert[:200]}')


# ---------------------------------------------------------------------------------------------- main

def main():
    report = Report()
    samples = {}
    scenario_static(report)
    mock = MockServer(port=PORT).start()
    n8n = N8n()
    n8n.cleanup(SLUG)
    started = time.time()
    try:
        fresh(mock)
        print(f'Test month {TARGET} (today {TODAY} UTC, box months moved by {DELTA}). Mock on {mock.url}.')
        wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
        with n8n.temporary(wf, SLUG, mock=mock) as run:
            c = Ctx(mock, run, report)
            scenario_security(c)
            scenario_bad_month(c)
            expected = scenario_happy(c, samples)
            scenario_rerun(c, expected)
            scenario_recheck(c, samples)
            scenario_slack_fails(c)
            left_open = scenario_sync_log_fails(c)
            scenario_open_rows(c, left_open)
            scenario_double_click(c)
            scenario_box_fails(c, samples)
            scenario_reserve_fails(c)
            scenario_release_fails(c)
            scenario_history_fails(c)
            scenario_lost_answers(c)
            scenario_read_fails(c)
            scenario_rate_limits(c)
            scenario_page_token_expires(c)
            scenario_data_mismatch(c)
            scenario_data_signals(c)
            scenario_nut_free_scarcity(c)
            scenario_nut_free_short_stock(c)
            scenario_subscriber_problems(c)
            scenario_out_of_stock(c)
            scenario_this_month(c)
            scenario_cancelled_box(c)
        wf2 = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, REPEAT_WINDOW='2'))
        with n8n.temporary(wf2, SLUG, mock=mock) as run:
            scenario_repeat_window_2(Ctx(mock, run, report))
        wf3 = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, REPEAT_WINDOW='two'))
        with n8n.temporary(wf3, SLUG, mock=mock) as run:
            scenario_bad_settings(Ctx(mock, run, report))
        # the same workflow with the Box History page limit lowered to 1, to prove the guard
        wf4 = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
        for node in wf4['nodes']:
            if node['name'] == 'Airtable: List Box History':
                node['parameters']['options']['pagination']['pagination']['maxRequests'] = 1
        with n8n.temporary(wf4, SLUG, mock=mock) as run:
            scenario_page_limit(Ctx(mock, run, report))
        # the planner code from the workflow file, on many random bases, inside n8n
        with n8n.temporary(prepare_workflow(property_workflow(), mock, SLUG, mock_env(mock)), SLUG, mock=mock) as run:
            prop = scenario_property(Ctx(mock, run, report))
        s = 'Allergen safety, every scenario together'
        report.check(s, f'every box of the {len(SAFETY["scenarios"])} scenarios that planned boxes was judged from its product records: '
                        'no bar breaks its subscriber\'s needs, and no nut-free box has a nut or nut trace bar',
                     not SAFETY['breaches'] and len(SAFETY['scenarios']) >= 15 and SAFETY['nut_free_boxes'] >= 50
                     and SAFETY['nut_free_short'] >= 5,
                     f'{SAFETY["boxes"]} boxes with {SAFETY["bars"]} bars, of them {SAFETY["nut_free_boxes"]} nut-free boxes with '
                     f'{SAFETY["nut_free_bars"]} bars ({SAFETY["nut_free_short"]} not full); plus {prop.get("nutFreeBoxes", 0)} nut-free boxes '
                     f'in {prop.get("runs", 0)} random bases; breaches {SAFETY["breaches"][:3]}')
        left = n8n.workflows_named(n8n.test_name(SLUG))
        report.check('Cleanup', 'every temporary workflow was deleted', not left, f'{len(left)} left')
    finally:
        mock.stop()
    ok = report.print()
    print(f'\nTotal time {time.time() - started:.0f} s')
    if samples:
        print('\n--- Samples from the first run ---')
        print('Run Now response:', json.dumps(samples.get('response')))
        print('\nFirst request (the run claims its Sync Log row):', json.dumps(samples.get('claim')))
        print('\nSlack summary:\n' + samples.get('slack', ''))
        print('\nBox Shipment BOX-...-SUB-0004 fields:', json.dumps(samples.get('box'), indent=1))
        print('\nBox Shipment BOX-...-SUB-0001 Packing List:\n' + samples.get('box_full', {}).get('Packing List', ''))
        print('\nUpsert request for SUB-0004:', json.dumps(samples.get('upsert'), indent=1))
        print('\nFirst reservation request:', json.dumps(samples.get('reserve')))
        print('\nAlert when a box cannot be saved:\n' + samples.get('alert_box', ''))
        print('\nSlack summary of the rerun after the data changed:\n' + samples.get('slack_recheck', ''))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
