"""End to end test for 02 Batch Inventory Sync against MOCK APIs (no real Airtable, Shopify or Slack).

The script:
  1. starts the shared strict mocks (tests/mocks) on port 8792: a seeded Airtable base, a Shopify store whose stock
     matches the seed, and Slack incoming webhooks,
  2. imports a temporary copy of the workflow into your n8n ("TEST batch-inventory-sync (temporary)", every API host
     pointed at the mocks, schedule disabled) plus an ACTIVE copy of shared/error-alert-slack.json as its error workflow,
  3. runs it in 23 scenarios, through the protected Run Now webhook (and once through the schedule path): security,
     the seeded run, an idempotent rerun, stock that changed in Shopify and in Airtable (and a Scheduled purchasing
     request), a sale that lands during the run, mapping mistakes, a lost Shopify answer, Shopify throttling once and
     twice and a busy idempotency key, a Shopify outage, an Airtable rate limit, a failed Airtable write that heals on
     the next run, Airtable outages, a promo made by hand, more than 10 changes at once, batch data mistakes, Slack down,
     a scheduled run, two Run Now calls at once, bad settings, reads that need several pages, an Airtable read that is
     cut short and a closed Shopify location,
  4. checks every request the workflow made, the records it left behind, the Slack notes and the error alerts, prints
     PASS or FAIL per check, then deletes the temporary workflows.

The mocks reject what the real APIs reject (more than 10 records per write, unknown fields, wrong types, a sixth
Airtable request inside one second, an inventorySetQuantities call without @idempotent or changeFromQuantity...), so a
passing check means the request would also be accepted for real.

Requirements: Python 3.9+, a running n8n 2.x with the public API enabled.
Environment:
  N8N_URL       default http://localhost:5678
  N8N_API_KEY   your n8n API key (or N8N_ENV_FILE pointing at a .env that has it)
  MOCK_HOST     how n8n reaches this machine; default host.docker.internal (n8n in Docker), 127.0.0.1 otherwise
Usage: python automations/02-batch-inventory-sync/tests/e2e_mock_test.py
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # automations/02-batch-inventory-sync/tests/e2e_mock_test.py -> repo root
sys.path.insert(0, str(ROOT))
from tests.mocks import MockServer, AIRTABLE_TOKEN  # noqa: E402
from tests.mocks.n8n import N8n, Report, mock_env, prepare_workflow  # noqa: E402
from tests.mocks.shopify import ACCESS_TOKEN as SHOPIFY_TOKEN  # noqa: E402

SLUG, PORT = 'batch-inventory-sync', 8792
WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow' / 'batch-inventory-sync.json'
HOOK = 'inventory-run-now'
SECRET = {'X-Run-Now-Secret': 'mock-run-now-secret'}
FACTS = json.loads((ROOT / 'base' / 'seed' / '_meta.json').read_text(encoding='utf-8'))['expected']['02-inventory-sync']
CHANGED = {sku: (v['from'], v['to']) for sku, v in FACTS['changedSkus'].items()}       # seed: Shopify value -> sellable
UNCHANGED = [s for s in FACTS['onShopify'] if s not in CHANGED]
PRODUCT_LINKS = {'Production Batches', 'Order Lines', 'Price List', 'Quote Lines', 'Box Shipments', 'Box History',
                 'Promos', 'Purchasing Requests'}
RUN_ID = re.compile(r'inventory-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z-\d+')     # time plus the n8n execution id
IF_OPERATORS = {'string': {'equals', 'notEquals', 'contains', 'notContains', 'startsWith', 'endsWith', 'regex', 'empty',
                           'notEmpty', 'exists', 'notExists'},
                'number': {'equals', 'notEquals', 'gt', 'gte', 'lt', 'lte'},
                'boolean': {'true', 'false', 'equals', 'notEquals'}}
SAMPLES = {}


class InventoryMock(MockServer):
    """The shared mock server plus one hook only this test needs:
    before_shopify(match, action)  run action(entry) just before the Shopify mock handles a matching request, for
                                   example a customer sale that lands between the workflow's read and its write.
    It also keeps the error workflow's alerts across reset(). A lost answer (the change is applied, n8n never hears
    back and retries) comes from the shared mock: mock.lose_answer('shopify', match)."""

    def __init__(self, *args, **kwargs):
        self._before = []
        self.kept_alerts = []      # error workflow alerts survive reset(), so the final check sees every one
        super().__init__(*args, **kwargs)
        original = self.shopify.handle

        def handle(req, entry):
            for rule in self._before:
                if rule['times'] and rule['match'](entry):
                    rule['times'] -= 1
                    rule['action'](entry)
            return original(req, entry)
        self.shopify.handle = handle

    def before_shopify(self, match, action, times=1):
        self._before.append({'match': match, 'action': action, 'times': times})

    def reset(self, *args, **kwargs):
        self._before.clear()
        self.kept_alerts += self.error_alerts()
        super().reset(*args, **kwargs)

    def error_alerts(self):
        return [m.get('text', '') for m in self.slack.messages if m['_path'].endswith(ERROR_PATH)]


ERROR_PATH = '/services/T000MOCK/B000MOCK/errors'      # where the test copy of the error workflow posts
mock = InventoryMock(port=PORT).start()
report = Report()
FAILED_RUNS = []                                        # execution ids of the runs that are meant to fail


# ---------------------------------------------------------------------------------------------- helpers

def when(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def run_now(run, headers=None):
    status, body = run.post(HOOK, {}, headers=SECRET if headers is None else headers)
    ex = run.wait(timeout=180)
    try:
        answer = json.loads(body)
    except ValueError:
        answer = {}
    return status, answer, ex


def calls(since, service=None):
    """Requests the workflow itself made (the error workflow's alert to Slack is not one of them)."""
    return [e for e in mock.log(service, since=since) if e['service'] in ('airtable', 'shopify', 'slack')
            and not e['path'].endswith(ERROR_PATH)]


def expect_alert(run, ex, count=True):
    """The error workflow's alert for a run that is meant to fail (and remember the run for the final count).
    The wait is generous because other test suites may be using the same n8n at the same time."""
    if count:
        FAILED_RUNS.append(str(ex['id']))
    return run.alert_for(ex, timeout=60) or ''


def set_stock_calls(since):
    return [e for e in mock.log('shopify', since=since) if e.get('operation') == 'SetStock']


def sku_of(entry):
    item = entry['variables']['input']['quantities'][0]['inventoryItemId']
    return mock.shopify.items[item]['sku'] if item in mock.shopify.items else item


def by_sku(entries):
    out = {}
    for e in entries:
        out.setdefault(sku_of(e), []).append(e)
    return out


def expected_input(sku, start, target, run_id):
    return {'name': 'available', 'reason': 'correction', 'referenceDocumentUri': f'gid://airtable-inventory-sync/SyncRun/{run_id}',
            'quantities': [{'inventoryItemId': mock.shopify.item_by_sku(sku)['id'], 'locationId': mock.shopify.primary_location,
                            'quantity': target, 'changeFromQuantity': start}]}


def product(sku):
    return mock.airtable.by_primary('Products', sku)


def product_fields(sku):
    return {k: v for k, v in product(sku)['fields'].items() if k not in PRODUCT_LINKS}


def snapshot_products():
    return {r['fields']['SKU']: {k: v for k, v in r['fields'].items() if k not in PRODUCT_LINKS} for r in mock.airtable.records('Products')}


def batch(code):
    return mock.airtable.by_primary('Production Batches', code)


def change_batch(code, **fields):
    mock.airtable.update('Production Batches', batch(code)['id'], {k.replace('_', ' '): v for k, v in fields.items()})


def level(sku):
    return mock.shopify.levels[(mock.shopify.item_by_sku(sku)['id'], mock.shopify.primary_location)]


def sale(sku, units):
    """What Shopify does when an order is placed: available goes down, committed goes up (on hand stays)."""
    lv = level(sku)
    lv['available'] -= units
    lv['committed'] += units


def sync_log(run_id):
    row = mock.airtable.by_primary('Sync Log', run_id)
    return row['fields'] if isinstance(row, dict) else {}


def counts(fields):
    keys = ('Status', 'Items Checked', 'Items Changed', 'Shopify Updates', 'Promos Created', 'Purchasing Requests Created', 'Errors')
    return {k: fields.get(k) for k in keys}


def slack_notes(since):
    return [e['body'].get('text', '') for e in mock.log('slack', since=since)
            if e['path'].endswith('/alerts') and isinstance(e.get('body'), dict)]


def airtable_shape(since):
    return [(e['operation'], e.get('table'), e.get('record_count')) for e in mock.log('airtable', since=since)]


def pacing(since):
    """Most Airtable requests inside any 1 second window, the smallest gap between two of them, and real 429s."""
    entries = mock.log('airtable', since=since)
    t = sorted(when(e['time']).timestamp() for e in entries)
    window = max((sum(1 for u in t[i:] if u - s < 1.0) for i, s in enumerate(t)), default=0)
    gap = min((b - a for a, b in zip(t, t[1:])), default=1.0)
    limited = [e for e in entries if e['status'] == 429 and not e.get('injected')]
    return window, gap, limited


def open_requests(sku):
    return [r for r in mock.airtable.records('Purchasing Requests') if r['fields'].get('SKU') == sku and r['fields'].get('Status') == 'Open']


def node_ran_last(run, ex, name):
    return run.last_node(ex) == name


# ---------------------------------------------------------------------------------------------- scenarios

def static_checks():
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))
    text = WORKFLOW.read_text(encoding='utf-8')
    https = [n for n in wf['nodes'] if n['type'] == 'n8n-nodes-base.httpRequest']
    ok = all(n.get('retryOnFail') is True and n.get('maxTries') == 3 and n.get('waitBetweenTries') == 2000
             and n.get('onError') == 'continueErrorOutput' for n in https)
    report.check('Workflow file', 'every HTTP Request node retries 3 times and routes its error output',
                 ok and len(https) == 11, f'{len(https)} HTTP nodes')
    ifs = [n for n in wf['nodes'] if n['type'] == 'n8n-nodes-base.if']
    conds = [c for n in ifs for c in n['parameters']['conditions']['conditions']]
    bad_ops = [f"{c['operator']['type']}.{c['operator']['operation']}" for c in conds
               if c['operator']['operation'] not in IF_OPERATORS.get(c['operator']['type'], set())
               or (c['operator']['operation'] in ('true', 'false', 'empty', 'notEmpty', 'exists', 'notExists')
                   and c['operator'].get('singleValue') is not True)]
    report.check('Workflow file', 'every IF node (v2) uses an operator n8n 2.19.5 has, single value operators flagged as such',
                 len(ifs) == 11 and all(n['typeVersion'] == 2 for n in ifs) and not bad_ops,
                 f'{len(ifs)} IF nodes, {len(conds)} conditions' + (f', bad: {bad_ops}' if bad_ops else ''))
    lists = [n for n in https if n['parameters']['url'].endswith('/listRecords')]
    report.check('Workflow file', 'the 4 Airtable reads paginate (options.pagination.pagination, offset in the body, 500 ms apart)',
                 len(lists) == 4 and all(n['parameters']['options']['pagination']['pagination']['parameters']['parameters']
                                         == [{'type': 'body', 'name': 'offset', 'value': '={{ $response.body.offset }}'}]
                                         and n['parameters']['options']['pagination']['pagination']['requestInterval'] == 500
                                         for n in lists))
    sched = next(n for n in wf['nodes'] if n['type'] == 'n8n-nodes-base.scheduleTrigger')
    report.check('Workflow file', 'daily at 06:00 UTC, no en or em dashes, no continueOnFail',
                 sched['parameters']['rule']['interval'] == [{'field': 'days', 'triggerAtHour': 6}]
                 and wf['settings'].get('timezone') == 'UTC' and chr(0x2013) not in text and chr(0x2014) not in text
                 and 'continueOnFail' not in text)


def scenario_security(run):
    for label, headers in (('no secret header', {}), ('wrong secret', {'X-Run-Now-Secret': 'not-the-secret'})):
        mark = mock.mark()
        status, answer, ex = run_now(run, headers)
        made = calls(mark)
        report.check('Run Now security', f'{label}: 401, nothing read or changed',
                     status == 401 and answer == {'error': 'unauthorized'} and not made and ex['status'] == 'success',
                     f'{status} {answer}, {len(made)} API calls')


def scenario_seeded(run):
    mock.reset()
    before = snapshot_products()
    promos_before = {r['id']: r for r in mock.airtable.records('Promos')}
    requests_before = {r['id'] for r in mock.airtable.records('Purchasing Requests')}
    mint_before = open_requests('DC-BAR-DARK-MINT')
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    mark = mock.mark()
    status, answer, ex = run_now(run)
    run_id = answer.get('runId', '')
    today = run_id[10:20]
    S = 'Seeded run'
    report.check(S, 'answers 202 with the run id right away',
                 status == 202 and answer == {'status': 'started', 'runId': run_id, 'trigger': 'Run Now'} and RUN_ID.fullmatch(run_id),
                 f'{status} {answer}')
    report.check(S, 'run succeeds and ends without an error', ex['status'] == 'success' and node_ran_last(run, ex, 'IF: Run Failed?'),
                 f"{ex['status']}, last node {run.last_node(ex)}")

    sets = set_stock_calls(mark)
    grouped = by_sku(sets)
    exact = set(grouped) == set(CHANGED) and len(sets) == 6 and all(
        grouped[s][0]['variables']['input'] == expected_input(s, start, target, run_id) for s, (start, target) in CHANGED.items())
    report.check(S, 'one inventorySetQuantities per changed SKU (6), exact input with changeFromQuantity', exact,
                 ', '.join(f"{s} {c[0]['variables']['input']['quantities'][0]['changeFromQuantity']} to "
                           f"{c[0]['variables']['input']['quantities'][0]['quantity']}" for s, c in sorted(grouped.items())))
    keys = [e['variables']['key'] for e in sets]
    report.check(S, 'every call uses @idempotent with its own key <run id>-<SKU>-<start value> (the run id holds the execution id)',
                 len(set(keys)) == 6 and all('@idempotent(key: $key)' in e['query'] for e in sets)
                 and run_id.endswith(f"-{ex['id']}")
                 and all(e['variables']['key'] == f'{run_id}-{sku_of(e)}-{CHANGED[sku_of(e)][0]}' for e in sets),
                 keys[0] if keys else 'no keys')
    SAMPLES['set_stock'] = next((e['variables'] for e in sets if sku_of(e) == 'DC-BAR-DARK-SEASALT'), None)

    shop_ok = all(mock.shopify.available(s) == t for s, (_, t) in CHANGED.items()) and all(
        mock.shopify.available(s) == before[s]['Shopify Stock'] for s in UNCHANGED)
    report.check(S, 'Shopify shows the new stock; the 15 unchanged SKUs, PUMPKIN-SPICE (40) and GINGER (25) untouched',
                 shop_ok and len(UNCHANGED) == 15 and mock.shopify.available('DC-BAR-PUMPKIN-SPICE') == 40
                 and mock.shopify.available('DC-BAR-GINGER') == 25 and len(mock.shopify.adjustments) == 6,
                 f'{len(mock.shopify.adjustments)} adjustments')

    after = snapshot_products()
    pushed_ok = all(after[s]['Shopify Stock'] == t and when(after[s]['Stock Synced At']) >= started for s, (_, t) in CHANGED.items())
    rest_ok = all(after[s] == before[s] for s in before if s not in CHANGED)
    report.check(S, 'Products: Shopify Stock and Stock Synced At updated for exactly the 6 SKUs', pushed_ok and rest_ok,
                 'DC-BAR-DARK-SEASALT ' + str(after['DC-BAR-DARK-SEASALT'].get('Shopify Stock')))

    new_promos = {r['fields']['Promo Name']: r['fields'] for r in mock.airtable.records('Promos') if r['id'] not in promos_before}
    want = {}
    for p in FACTS['promoBatches']:
        if 'PROMO-' + p['batch'] == FACTS['existingPromo']:
            continue
        b = batch(p['batch'])
        want['PROMO-' + p['batch']] = {'Promo Name': 'PROMO-' + p['batch'], 'Batch': [b['id']], 'Product': [product(p['sku'])['id']],
                                       'Best Before': b['fields']['Best Before'], 'Units Available': p['units'],
                                       'Discount Percent': 25, 'Status': 'Proposed'}
    promo_ok = set(new_promos) == set(want) and all(
        {k: v for k, v in new_promos[n].items() if k != 'Created At'} == want[n] and when(new_promos[n]['Created At']) >= started for n in want)
    seeded_promo = promos_before[mock.airtable.by_primary('Promos', FACTS['existingPromo'])['id']]
    report.check(S, 'exactly 3 new promos with exact fields; PROMO-B-DSS-260523 untouched; none for B-MHZ-260712 (46 days)',
                 promo_ok and len(want) == FACTS['newPromosExpected'] and mock.airtable.count('Promos') == 4
                 and mock.airtable.by_primary('Promos', FACTS['existingPromo']) == seeded_promo
                 and mock.airtable.by_primary('Promos', 'PROMO-B-MHZ-260712') is None, ', '.join(sorted(new_promos)))

    new_requests = {r['fields']['SKU']: r['fields'] for r in mock.airtable.records('Purchasing Requests') if r['id'] not in requests_before}
    sellable = FACTS['sellableBySku']
    req_ok = set(new_requests) == set(FACTS['newPurchasingRequestsExpected'])
    for sku, f in new_requests.items():
        p = product(sku)['fields']
        want_f = {'Request ID': f'PR-{sku}-{today}', 'Product': [product(sku)['id']], 'SKU': sku, 'Status': 'Open',
                  'Sellable Stock': sellable[sku], 'Reorder Point': p['Reorder Point'], 'Suggested Quantity': p['Reorder Quantity']}
        req_ok = req_ok and all(f.get(k) == v for k, v in want_f.items()) and when(f['Requested At']) >= started and run_id in f['Notes']
    praline_note = new_requests.get('DC-PRALINE-HAZELNUT', {}).get('Notes', '')
    report.check(S, 'exactly 3 new Open purchasing requests with exact fields; DARK-MINT keeps its one open request',
                 req_ok and open_requests('DC-BAR-DARK-MINT') == mint_before and len(mint_before) == 1
                 and f"B-PRL-261023 (1500 units on {mock.seed_date('2026-10-23')})" in praline_note,
                 ', '.join(sorted(new_requests)))
    SAMPLES['request_note'] = praline_note

    log = sync_log(run_id)
    report.check(S, 'one Sync Log row with the exact counts',
                 counts(log) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6, 'Shopify Updates': 6,
                                 'Promos Created': 3, 'Purchasing Requests Created': 3, 'Errors': 0}
                 and log.get('Automation') == '02 Inventory Sync' and log.get('Trigger') == 'Run Now'
                 and when(log['Finished At']) >= when(log['Started At']) and 'B-D85-260403 DC-BAR-DARK-85 30 units' in log.get('Details', '')
                 and 'B-DAL-260314' in log.get('Details', '')
                 and 'Purchasing requests already in progress: DC-BAR-DARK-MINT (Open).' in log.get('Details', '')
                 and mock.airtable.count('Sync Log') == 3, json.dumps(counts(log)))
    SAMPLES['details'] = log.get('Details', '')

    notes = slack_notes(mark)
    note = notes[0] if notes else ''
    report.check(S, 'one Slack note with the 3 promos, the 3 requests and the expired batch still marked Released',
                 len(notes) == 1 and all(n in note for n in want) and all(f'• {s},' in note for s in FACTS['newPurchasingRequestsExpected'])
                 and 'B-DAL-260314 (DC-BAR-DARK-ALMOND) is past its best before date' in note and 'PUMPKIN' not in note,
                 f'{len(notes)} note(s)')
    SAMPLES['slack'] = note

    shape = airtable_shape(mark)
    window, gap, limited = pacing(mark)
    report.check(S, 'Airtable: 4 reads, then 3 writes of at most 10 records and the Sync Log, 500 ms apart (at most 2 in any second)',
                 shape == [('list', 'Products', None), ('list', 'Production Batches', None), ('list', 'Purchasing Requests', None),
                           ('list', 'Promos', None), ('update', 'Products', 6), ('upsert', 'Promos', 3),
                           ('upsert', 'Purchasing Requests', 3), ('upsert', 'Sync Log', 1)]
                 and window <= 2 and gap >= 0.49 and not limited
                 and all(e['headers'].get('authorization') == 'Bearer ' + AIRTABLE_TOKEN for e in mock.log('airtable', since=mark)),
                 f'max {window} per second, smallest gap {gap:.2f} s')
    shop = mock.log('shopify', since=mark)
    report.check(S, 'Shopify: 1 stock read and 6 updates, token sent, no GraphQL errors',
                 [e.get('operation') for e in shop] == ['StockLevels'] + ['SetStock'] * 6
                 and all(e['headers'].get('x-shopify-access-token') == SHOPIFY_TOKEN and not e.get('graphql_errors')
                         and e['status'] == 200 for e in shop))
    return run_id


def scenario_rerun(run):
    S = 'Rerun with nothing new'
    promos, requests = mock.airtable.count('Promos'), mock.airtable.count('Purchasing Requests')
    before = snapshot_products()
    mark = mock.mark()
    status, answer, ex = run_now(run)
    run_id = answer.get('runId', '')
    report.check(S, 'Shopify is read once and not changed', [e.get('operation') for e in mock.log('shopify', since=mark)] == ['StockLevels'])
    report.check(S, 'no promo, purchasing request or product write; only the Sync Log row',
                 mock.airtable.count('Promos') == promos and mock.airtable.count('Purchasing Requests') == requests
                 and snapshot_products() == before
                 and [op for op, _, _ in airtable_shape(mark)] == ['list', 'list', 'list', 'list', 'upsert'],
                 str(airtable_shape(mark)[-1]))
    details = sync_log(run_id).get('Details', '')
    report.check(S, 'Sync Log row with zero changes; it names the 4 promos and the 4 requests already there',
                 counts(sync_log(run_id)) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 0, 'Shopify Updates': 0,
                                              'Promos Created': 0, 'Purchasing Requests Created': 0, 'Errors': 0}
                 and 'Promos already there: PROMO-B-DSS-260523, PROMO-B-WMA-260612, PROMO-B-PRB-260930, PROMO-B-MSC-260722.' in details
                 and 'Purchasing requests already in progress: DC-BAR-DARK-85 (Open), DC-BAR-DARK-CHILI (Open), '
                     'DC-BAR-DARK-MINT (Open), DC-PRALINE-HAZELNUT (Open).' in details)
    notes = slack_notes(mark)
    report.check(S, 'Slack note only repeats what still needs fixing (the expired batch)',
                 len(notes) == 1 and 'B-DAL-260314' in notes[0] and 'New promos' not in notes[0]
                 and 'New purchasing requests' not in notes[0] and 'Shopify stock set' not in notes[0], f'{len(notes)} note(s)')


def scenario_drift(run):
    S = 'Stock changed in Shopify and in Airtable'
    mock.shopify.set_available('DC-BAR-MILK-CARAMEL', 300)           # someone typed 300 in the Shopify admin
    sale('DC-BAR-DARK-ORANGE', 5)                                   # a paid order for 5, not shipped yet
    change_batch('B-MES-260821', Status='On Hold')                  # quality hold: MILK-ESPRESSO has nothing to sell
    change_batch('B-WCC-260826', Quantity_Sold=104)                 # 12 more WHITE-COOKIE bars shipped
    chili = open_requests('DC-BAR-DARK-CHILI')[0]                   # the kitchen scheduled the CHILI batch
    mock.airtable.update('Purchasing Requests', chili['id'], {'Status': 'Scheduled'})
    orange_synced = product_fields('DC-BAR-DARK-ORANGE')['Stock Synced At']
    mark = mock.mark()
    status, answer, ex = run_now(run)
    run_id = answer.get('runId', '')
    got = {s: (c[0]['variables']['input']['quantities'][0]['changeFromQuantity'], c[0]['variables']['input']['quantities'][0]['quantity'])
           for s, c in by_sku(set_stock_calls(mark)).items()}
    report.check(S, 'Airtable wins: CARAMEL 300 to 290, MILK-ESPRESSO 50 to 0, WHITE-COOKIE 48 to 36, nothing else',
                 got == {'DC-BAR-MILK-CARAMEL': (300, 290), 'DC-BAR-MILK-ESPRESSO': (50, 0), 'DC-BAR-WHITE-COOKIE': (48, 36)}
                 and mock.shopify.available('DC-BAR-MILK-ESPRESSO') == 0, json.dumps(got))
    orange = product_fields('DC-BAR-DARK-ORANGE')
    report.check(S, 'units in an unshipped order are not put back on sale (ORANGE stays 57, Airtable corrected to 57)',
                 'DC-BAR-DARK-ORANGE' not in got and level('DC-BAR-DARK-ORANGE')['available'] == 57
                 and orange['Shopify Stock'] == 57 and orange['Stock Synced At'] == orange_synced,
                 f"Shopify {level('DC-BAR-DARK-ORANGE')['available']} available, {level('DC-BAR-DARK-ORANGE')['committed']} committed")
    writes = [e for e in mock.log('airtable', since=mark) if e['operation'] == 'update' and e['table'] == 'Products']
    report.check(S, 'Products updated in one call with 4 records (3 pushed, 1 corrected)',
                 len(writes) == 1 and writes[0]['record_count'] == 4)
    esp = open_requests('DC-BAR-MILK-ESPRESSO')
    chili_rows = [r for r in mock.airtable.records('Purchasing Requests') if r['fields'].get('SKU') == 'DC-BAR-DARK-CHILI']
    report.check(S, 'one new request for MILK-ESPRESSO (a stored 0 is kept); none for the 3 open ones or the Scheduled CHILI one',
                 len(esp) == 1 and esp[0]['fields']['Sellable Stock'] == 0
                 and all(len(open_requests(s)) == 1 for s in ('DC-BAR-DARK-85', 'DC-PRALINE-HAZELNUT', 'DC-BAR-DARK-MINT'))
                 and [r['fields']['Status'] for r in chili_rows] == ['Scheduled'] and mock.airtable.count('Purchasing Requests') == 6
                 and 'DC-BAR-DARK-CHILI (Scheduled)' in sync_log(run_id).get('Details', ''),
                 f"{mock.airtable.count('Purchasing Requests')} requests")
    report.check(S, 'Sync Log counts',
                 counts(sync_log(run_id)) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 3, 'Shopify Updates': 3,
                                              'Promos Created': 0, 'Purchasing Requests Created': 1, 'Errors': 0},
                 json.dumps(counts(sync_log(run_id))))


def scenario_sale_during_run(run):
    S = 'Sale during the run'
    seasalt = mock.shopify.item_by_sku('DC-BAR-DARK-SEASALT')['id']
    change_batch('B-DSS-260910', Quantity_Sold=125)                 # SEASALT sellable 200 to 195
    change_batch('B-DRB-260831', Quantity_Sold=100)                 # RASPBERRY sellable 55 to 50
    mock.before_shopify(lambda e: e.get('operation') == 'SetStock'
                        and e['variables']['input']['quantities'][0]['inventoryItemId'] == seasalt,
                        lambda e: sale('DC-BAR-DARK-SEASALT', 1))
    mark = mock.mark()
    status, answer, ex = run_now(run)
    run_id = answer.get('runId', '')
    alert = expect_alert(run, ex)
    report.check(S, 'Shopify refuses the stale change; the run ends at Stop: Sync Failed and the alert names the SKU',
                 ex['status'] == 'error' and node_ran_last(run, ex, 'Stop: Sync Failed') and '*Stop: Sync Failed*' in alert
                 and 'DC-BAR-DARK-SEASALT' in alert and 'CHANGE_FROM_QUANTITY_STALE' in alert, alert.splitlines()[1] if alert else 'no alert')
    SAMPLES['alert_stale'] = alert
    report.check(S, 'the sale is kept (SEASALT 199 in Shopify, 200 in Airtable); RASPBERRY still set to 50',
                 level('DC-BAR-DARK-SEASALT')['available'] == 199 and product_fields('DC-BAR-DARK-SEASALT')['Shopify Stock'] == 200
                 and mock.shopify.available('DC-BAR-DARK-RASPBERRY') == 50 and product_fields('DC-BAR-DARK-RASPBERRY')['Shopify Stock'] == 50)
    notes = slack_notes(mark)
    report.check(S, 'Sync Log says Partial with 1 error, and the Slack note lists it',
                 counts(sync_log(run_id)) == {'Status': 'Partial', 'Items Checked': 21, 'Items Changed': 1, 'Shopify Updates': 1,
                                              'Promos Created': 0, 'Purchasing Requests Created': 0, 'Errors': 1}
                 and len(notes) == 1 and '*Errors (1)*' in notes[0] and 'DC-BAR-DARK-SEASALT was not updated in Shopify' in notes[0],
                 json.dumps(counts(sync_log(run_id))))
    failed_key = by_sku(set_stock_calls(mark))['DC-BAR-DARK-SEASALT'][0]['variables']['key']

    mark = mock.mark()
    status, answer, ex = run_now(run)
    calls_ = by_sku(set_stock_calls(mark))
    seasalt_call = calls_.get('DC-BAR-DARK-SEASALT', [{}])[0]
    q = seasalt_call.get('variables', {}).get('input', {}).get('quantities', [{}])[0]
    report.check(S, 'next run sets SEASALT 199 to 194 (195 sellable minus 1 unshipped) with a new key',
                 ex['status'] == 'success' and list(calls_) == ['DC-BAR-DARK-SEASALT'] and q.get('changeFromQuantity') == 199
                 and q.get('quantity') == 194 and seasalt_call['variables']['key'] != failed_key
                 and product_fields('DC-BAR-DARK-SEASALT')['Shopify Stock'] == 194
                 and '195 sellable minus 1 in unshipped Shopify orders' in sync_log(answer.get('runId', '')).get('Details', ''),
                 f"{q.get('changeFromQuantity')} to {q.get('quantity')}")


def scenario_mapping(run):
    S = 'Mapping mistakes are skipped'
    strawberry_item = mock.shopify.item_by_sku('DC-BAR-WHITE-STRAWBERRY')['id']
    mock.airtable.update('Products', product('DC-BAR-RUBY-CHERRY')['id'], {'Shopify Inventory Item ID': strawberry_item})
    mock.shopify.item_by_sku('DC-BAR-DARK-ESPRESSO')['tracked'] = False
    mock.airtable.update('Products', product('DC-HOT-COCOA-250')['id'], {'Shopify Inventory Item ID': 'gid://shopify/InventoryItem/4720000009999'})
    mock.airtable.update('Products', product('DC-BOX-PRALINE-12')['id'], {'Shopify Inventory Item ID': 'shopify-item-120'})
    for code, sold in (('B-RCH-260930', 50), ('B-DES-260925', 55), ('B-HOT-260622', 64), ('B-PRB-260930', 40)):
        change_batch(code, Quantity_Sold=sold)                       # each SKU would otherwise need a push
    stock = {s: mock.shopify.available(s) for s in ('DC-BAR-WHITE-STRAWBERRY', 'DC-BAR-RUBY-CHERRY', 'DC-BAR-DARK-ESPRESSO',
                                                    'DC-HOT-COCOA-250', 'DC-BOX-PRALINE-12')}
    mark = mock.mark()
    status, answer, ex = run_now(run)
    report.check(S, 'no inventorySetQuantities call; WHITE-STRAWBERRY keeps 62 (not overwritten with RUBY-CHERRY numbers)',
                 not set_stock_calls(mark) and {s: mock.shopify.available(s) for s in stock} == stock and stock['DC-BAR-WHITE-STRAWBERRY'] == 62)
    notes = slack_notes(mark)
    note = notes[0] if notes else ''
    reasons = ['DC-BAR-RUBY-CHERRY was not synced because its Shopify Inventory Item ID belongs to SKU DC-BAR-WHITE-STRAWBERRY in Shopify',
               'DC-BAR-DARK-ESPRESSO was not synced because Shopify does not track inventory for it',
               'DC-HOT-COCOA-250 was not synced because its Shopify Inventory Item ID gid://shopify/InventoryItem/4720000009999 was not found in Shopify',
               'DC-BOX-PRALINE-12 has a Shopify Inventory Item ID that is not a Shopify inventory item id (shopify-item-120), so it was not synced']
    report.check(S, 'the Slack note names each problem and its reason', len(notes) == 1 and all(r in note for r in reasons)
                 and '*Needs attention (5)*' in note, f'{sum(r in note for r in reasons)} of 4 reasons')
    report.check(S, 'skips are not errors: Sync Log Success, 20 SKUs checked',
                 counts(sync_log(answer.get('runId', ''))) == {'Status': 'Success', 'Items Checked': 20, 'Items Changed': 0,
                                                               'Shopify Updates': 0, 'Promos Created': 0,
                                                               'Purchasing Requests Created': 0, 'Errors': 0},
                 json.dumps(counts(sync_log(answer.get('runId', '')))))


def scenario_lost_answer(run):
    S = 'Lost Shopify answer'
    mock.reset()
    d85 = mock.shopify.item_by_sku('DC-BAR-DARK-85')['id']
    mock.lose_answer('shopify', lambda e: e.get('operation') == 'SetStock'
                     and e['variables']['input']['quantities'][0]['inventoryItemId'] == d85)
    mark = mock.mark()
    status, answer, ex = run_now(run)
    tries = by_sku(set_stock_calls(mark)).get('DC-BAR-DARK-85', [])
    applied = [a for a in mock.shopify.adjustments if any(c['sku'] == 'DC-BAR-DARK-85' for c in a['changes'])]
    report.check(S, 'n8n retries with the same key and input; Shopify replays, the change is applied once',
                 len(tries) == 2 and tries[0].get('answer_lost') and tries[1].get('idempotent_replay')
                 and tries[0]['variables'] == tries[1]['variables'] and len(applied) == 1
                 and mock.shopify.available('DC-BAR-DARK-85') == 4,
                 f'{len(tries)} requests, {len(applied)} adjustment')
    report.check(S, 'the run still succeeds with all 6 SKUs synced',
                 ex['status'] == 'success' and counts(sync_log(answer.get('runId', '')))['Items Changed'] == 6
                 and product_fields('DC-BAR-DARK-85')['Shopify Stock'] == 4)


def is_set_stock(sku):
    item = mock.shopify.item_by_sku(sku)['id']
    return lambda e: (e.get('service') == 'shopify' and e.get('operation') == 'SetStock'
                      and e['variables']['input']['quantities'][0]['inventoryItemId'] == item)


def is_stock_read(e):
    return e.get('service') == 'shopify' and e.get('operation') == 'StockLevels'


def scenario_throttled(run):
    S = 'Shopify throttles or is still busy'
    mock.reset()
    # the stock read and the CLASSIC update are throttled once (HTTP 200 with errors THROTTLED, nothing run); the ALMOND
    # update finds its key still in progress (userError IDEMPOTENCY_CONCURRENT_REQUEST, nothing applied)
    mock.before_shopify(is_stock_read, lambda e: mock.shopify.throttle(1))
    mock.before_shopify(is_set_stock('DC-BAR-MILK-CLASSIC'), lambda e: mock.shopify.throttle(1))
    busy = {'data': {'inventorySetQuantities': {'inventoryAdjustmentGroup': None, 'userErrors': [{
        'code': 'IDEMPOTENCY_CONCURRENT_REQUEST', 'field': None,
        'message': 'Another request with the same idempotency key is currently being processed.'}]}}}
    mock.fail('shopify', status=200, body=busy, times=1, match=is_set_stock('DC-BAR-DARK-ALMOND'))
    mark = mock.mark()
    status, answer, ex = run_now(run)
    reads = [e for e in mock.log('shopify', since=mark) if e.get('operation') == 'StockLevels']
    read_gap = (when(reads[1]['time']) - when(reads[0]['time'])).total_seconds() if len(reads) == 2 else 0
    per_sku = by_sku(set_stock_calls(mark))
    classic, almond = per_sku.get('DC-BAR-MILK-CLASSIC', []), per_sku.get('DC-BAR-DARK-ALMOND', [])
    report.check(S, 'a throttled stock read is sent once more after 2 seconds',
                 len(reads) == 2 and reads[0].get('throttled') and not reads[1].get('throttled') and read_gap >= 2.0,
                 f'{len(reads)} reads, {read_gap:.1f} s apart')
    report.check(S, 'a throttled update and a busy key are sent once more with the same key and input, other SKUs once',
                 len(classic) == 2 and classic[0].get('throttled') and classic[0]['variables'] == classic[1]['variables']
                 and len(almond) == 2 and almond[0].get('injected') and almond[0]['variables'] == almond[1]['variables']
                 and all(len(per_sku.get(s, [])) == 1 for s in CHANGED if s not in ('DC-BAR-MILK-CLASSIC', 'DC-BAR-DARK-ALMOND'))
                 and (when(classic[1]['time']) - when(classic[0]['time'])).total_seconds() >= 2.0,
                 f'{len(classic)} CLASSIC and {len(almond)} ALMOND calls')
    report.check(S, 'each change is applied once, the run succeeds with no alert-worthy error',
                 ex['status'] == 'success' and len(mock.shopify.adjustments) == 6
                 and all(mock.shopify.available(s) == t and product_fields(s)['Shopify Stock'] == t for s, (_, t) in CHANGED.items())
                 and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6,
                                                                   'Shopify Updates': 6, 'Promos Created': 3,
                                                                   'Purchasing Requests Created': 3, 'Errors': 0})

    S = 'Shopify throttles twice'
    mock.reset()
    mock.before_shopify(is_set_stock('DC-BAR-MILK-CLASSIC'), lambda e: mock.shopify.throttle(1), times=2)
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    others = [s for s in CHANGED if s != 'DC-BAR-MILK-CLASSIC']
    classic = by_sku(set_stock_calls(mark)).get('DC-BAR-MILK-CLASSIC', [])
    report.check(S, 'after the second THROTTLED answer the SKU keeps its old stock in Shopify and Airtable, the other 5 are set',
                 len(classic) == 2 and all(e.get('throttled') for e in classic)
                 and mock.shopify.available('DC-BAR-MILK-CLASSIC') == 200 and product_fields('DC-BAR-MILK-CLASSIC')['Shopify Stock'] == 200
                 and all(mock.shopify.available(s) == CHANGED[s][1] and product_fields(s)['Shopify Stock'] == CHANGED[s][1] for s in others),
                 f'{len(classic)} CLASSIC calls')
    report.check(S, 'Sync Log says Partial; the alert names the SKU and THROTTLED',
                 counts(sync_log(answer.get('runId', ''))) == {'Status': 'Partial', 'Items Checked': 21, 'Items Changed': 5,
                                                               'Shopify Updates': 5, 'Promos Created': 3,
                                                               'Purchasing Requests Created': 3, 'Errors': 1}
                 and ex['status'] == 'error'
                 and 'DC-BAR-MILK-CLASSIC was not updated in Shopify (Shopify refused the update (THROTTLED' in alert,
                 alert.splitlines()[1][:150] if alert else 'no alert')

    mock.reset()
    before = snapshot_products()
    mock.before_shopify(is_stock_read, lambda e: mock.shopify.throttle(1), times=2)
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    report.check(S, 'a stock read throttled twice: 2 reads, nothing set, Sync Log Failed, the alert says THROTTLED',
                 [e.get('operation') for e in mock.log('shopify', since=mark)] == ['StockLevels'] * 2
                 and snapshot_products() == before and not mock.shopify.adjustments
                 and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Failed', 'Items Checked': 0, 'Items Changed': 0,
                                                                   'Shopify Updates': 0, 'Promos Created': 3,
                                                                   'Purchasing Requests Created': 3, 'Errors': 1}
                 and 'Shopify refused the stock query (THROTTLED Throttled)' in alert,
                 alert.splitlines()[1][:150] if alert else 'no alert')


def scenario_shopify_down(run):
    S = 'Shopify is down'
    mock.reset()
    before = snapshot_products()
    mock.fail('shopify', status=500, times=3, match={'operation': 'StockLevels'})
    mark = mock.mark()
    status, answer, ex = run_now(run)
    run_id = answer.get('runId', '')
    report.check(S, 'the stock read is tried 3 times, nothing is set in Shopify or in Products',
                 [e.get('operation') for e in mock.log('shopify', since=mark)] == ['StockLevels'] * 3 and snapshot_products() == before)
    report.check(S, 'promos and purchasing requests are still created (they only need Airtable)',
                 mock.airtable.count('Promos') == 4 and mock.airtable.count('Purchasing Requests') == 5)
    alert = expect_alert(run, ex)
    report.check(S, 'Sync Log says Failed, the alert explains why',
                 counts(sync_log(run_id)) == {'Status': 'Failed', 'Items Checked': 0, 'Items Changed': 0, 'Shopify Updates': 0,
                                              'Promos Created': 3, 'Purchasing Requests Created': 3, 'Errors': 1}
                 and ex['status'] == 'error' and 'Shopify stock could not be read (500' in alert,
                 alert.splitlines()[1][:120] if alert else 'no alert')
    SAMPLES['alert_shopify_down'] = alert


def scenario_rate_limit(run):
    S = 'Airtable rate limit on a write'
    mock.reset()
    mock.fail('airtable', status=429, times=3, match={'table': 'Promos', 'method': 'PATCH'})
    mark = mock.mark()
    status, answer, ex = run_now(run, None)
    tries = [e for e in mock.log('airtable', since=mark) if e['table'] == 'Promos' and e['method'] == 'PATCH']
    wait = when(tries[3]['time']) - when(tries[2]['time']) if len(tries) == 4 else timedelta(0)
    report.check(S, '3 tries get 429, the workflow waits 30 seconds, the 4th try saves the promos',
                 len(tries) == 4 and [e['status'] for e in tries] == [429, 429, 429, 200] and wait >= timedelta(seconds=30),
                 f'{len(tries)} tries, waited {wait.total_seconds():.1f} s')
    names = [r['fields']['Promo Name'] for r in mock.airtable.records('Promos')]
    report.check(S, 'no duplicate promos, the run succeeds',
                 len(names) == 4 and len(set(names)) == 4 and ex['status'] == 'success'
                 and counts(sync_log(answer.get('runId', '')))['Promos Created'] == 3)


def scenario_write_fails(run):
    S = 'Airtable write fails, next run heals'
    mock.reset()
    body = {'error': {'type': 'INVALID_VALUE_FOR_COLUMN', 'message': 'Field "Shopify Stock" cannot accept the provided value'}}
    mock.fail('airtable', status=422, times=-1, body=body, match={'table': 'Products', 'method': 'PATCH'})
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    report.check(S, 'Shopify is updated, Products is not; promos and requests are still saved',
                 all(mock.shopify.available(s) == t for s, (_, t) in CHANGED.items())
                 and all(product_fields(s)['Shopify Stock'] == f for s, (f, _) in CHANGED.items())
                 and mock.airtable.count('Promos') == 4 and mock.airtable.count('Purchasing Requests') == 5)
    report.check(S, 'Sync Log says Partial; the alert names the table and the Airtable error',
                 counts(sync_log(answer.get('runId', ''))) == {'Status': 'Partial', 'Items Checked': 21, 'Items Changed': 6,
                                                               'Shopify Updates': 6, 'Promos Created': 3,
                                                               'Purchasing Requests Created': 3, 'Errors': 1}
                 and 'Airtable did not save Shopify Stock in Products' in alert and '422 INVALID_VALUE_FOR_COLUMN' in alert,
                 alert.splitlines()[1][:140] if alert else 'no alert')
    SAMPLES['alert_write'] = alert
    mock.clear_failures()
    mark = mock.mark()
    status, answer, ex = run_now(run)
    writes = [e for e in mock.log('airtable', since=mark) if e['operation'] == 'update' and e['table'] == 'Products']
    report.check(S, 'next run: no Shopify change, Products corrected to what Shopify shows',
                 ex['status'] == 'success' and not set_stock_calls(mark) and len(writes) == 1 and writes[0]['record_count'] == 6
                 and all(product_fields(s)['Shopify Stock'] == t for s, (_, t) in CHANGED.items()))


def scenario_airtable_down(run):
    S = 'Airtable is down at the start'
    mock.reset()
    mock.fail('airtable', status=503, times=3, match={'table': 'Production Batches'})
    before = snapshot_products()
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    made = calls(mark)
    report.check(S, 'stops at Stop: Airtable Read Failed before anything changes; only a Failed Sync Log row is written',
                 ex['status'] == 'error' and node_ran_last(run, ex, 'Stop: Airtable Read Failed')
                 and [(e['service'], e.get('table'), e.get('operation'), e['status']) for e in made]
                 == [('airtable', 'Products', 'list', 200)] + [('airtable', 'Production Batches', 'list', 503)] * 3
                 + [('airtable', 'Sync Log', 'upsert', 200)]
                 and snapshot_products() == before and mock.airtable.count('Promos') == 1
                 and mock.airtable.count('Purchasing Requests') == 2,
                 f'{len(made)} API calls')
    log = sync_log(answer.get('runId', ''))
    report.check(S, 'the Sync Log row and the alert say which read failed, with the Airtable error, and that nothing changed',
                 counts(log) == {'Status': 'Failed', 'Items Checked': 0, 'Items Changed': 0, 'Shopify Updates': 0,
                                 'Promos Created': 0, 'Purchasing Requests Created': 0, 'Errors': 1}
                 and log.get('Trigger') == 'Run Now'
                 and 'Airtable read failed at List Batches (503 RETRIABLE_ERROR Server encountered an error' in log.get('Details', '')
                 and 'Airtable read failed at List Batches (503 RETRIABLE_ERROR Server encountered an error' in alert
                 and 'Nothing was changed' in alert and 'could not be saved' not in alert,
                 alert.splitlines()[1][:140] if alert else 'no alert')
    SAMPLES['alert_airtable_down'] = alert

    mock.reset()
    mock.fail('airtable', status=503, times=-1)                      # all of Airtable is down
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    made = calls(mark)
    mock.clear_failures()
    report.check(S, 'all of Airtable down: 3 reads, 3 tries to log, nothing else; the alert says the row could not be saved',
                 [(e.get('table'), e.get('operation')) for e in made] == [('Products', 'list')] * 3 + [('Sync Log', 'upsert')] * 3
                 and sync_log(answer.get('runId', '')) == {}
                 and 'Airtable read failed at List Products (503' in alert and 'The Sync Log row could not be saved either.' in alert,
                 alert.splitlines()[1][:160] if alert else 'no alert')


def scenario_manual_promo(run):
    S = 'Promo made by hand'
    mock.reset()
    # typed by hand, without the batch link, in lower case and with a trailing space
    manual = mock.airtable.insert('Promos', {'Promo Name': 'promo-b-wma-260612 ', 'Status': 'Active', 'Discount Percent': 30})
    status, answer, ex = run_now(run)
    same = [r for r in mock.airtable.records('Promos') if r['fields']['Promo Name'].strip().upper() == 'PROMO-B-WMA-260612']
    report.check(S, 'found by name (case and spaces ignored): no second promo, the Active one keeps its 30% and is not linked',
                 len(same) == 1 and same[0]['fields'] == manual['fields'] and mock.airtable.count('Promos') == 4
                 and ex['status'] == 'success'
                 and counts(sync_log(answer.get('runId', '')))['Promos Created'] == 2
                 and 'Promos already there: PROMO-B-DSS-260523, PROMO-B-WMA-260612' in sync_log(answer.get('runId', '')).get('Details', ''))


def scenario_many_changes(run):
    S = 'More than 10 changes'
    mock.reset()
    drifted = UNCHANGED[:12]
    for s in drifted:
        mock.shopify.set_available(s, mock.shopify.available(s) + 1)
    mark = mock.mark()
    status, answer, ex = run_now(run)
    writes = [e['record_count'] for e in mock.log('airtable', since=mark) if e['operation'] == 'update' and e['table'] == 'Products']
    window, gap, limited = pacing(mark)
    report.check(S, '18 SKUs set in Shopify, Products written in 2 calls (10 + 8), still paced',
                 len(set_stock_calls(mark)) == 18 and writes == [10, 8] and window <= 2 and not limited
                 and all(mock.shopify.available(s) == product_fields(s)['Shopify Stock'] for s in FACTS['onShopify']),
                 f'writes {writes}, max {window} per second')


def scenario_batch_mistakes(run):
    S = 'Batch data mistakes'
    mock.reset()
    today = mock.clock.today()
    day = lambda n: (today + timedelta(days=n)).isoformat()
    pid = lambda sku: [product(sku)['id']]
    mock.airtable.insert('Production Batches', {'Batch Code': 'B-DSS-261019', 'Product': pid('DC-BAR-DARK-SEASALT'), 'Status': 'Planned',
                                                'Produced On': day(-1), 'Best Before': day(360), 'Quantity Made': 200})
    mock.airtable.insert('Production Batches', {'Batch Code': 'B-DOR-261019', 'Product': pid('DC-BAR-DARK-ORANGE'), 'Status': 'Released',
                                                'Produced On': day(-30), 'Best Before': day(200), 'Quantity Made': 10,
                                                'Quantity Reserved': 4, 'Quantity Sold': 8})
    mock.airtable.insert('Production Batches', {'Batch Code': 'B-DES-261030', 'Product': pid('DC-BAR-DARK-ESPRESSO'), 'Status': 'Released',
                                                'Produced On': day(1), 'Best Before': day(365), 'Quantity Made': 50})
    mock.airtable.insert('Production Batches', {'Batch Code': 'B-NOP-261001', 'Status': 'Released', 'Produced On': day(-20),
                                                'Best Before': day(300), 'Quantity Made': 30})
    mock.airtable.insert('Production Batches', {'Batch Code': 'B-MPB-261001', 'Product': pid('DC-BAR-MILK-PEANUT'), 'Status': 'Released',
                                                'Produced On': day(-20), 'Best Before': day(300)})
    mark = mock.mark()
    status, answer, ex = run_now(run)
    notes = slack_notes(mark)
    note = notes[0] if notes else ''
    lines = ['• Batch B-DAL-260314 (DC-BAR-DARK-ALMOND) is past its best before date',
             f'• Batch B-DSS-261019 (DC-BAR-DARK-SEASALT) is still Planned although its Produced On date ({day(-1)}) has passed. '
             'Release it or move the date',
             '• Batch B-DOR-261019 (DC-BAR-DARK-ORANGE) has more units reserved and sold (12) than made (10), so it counts as 0',
             f'• Batch B-DES-261030 (DC-BAR-DARK-ESPRESSO) is Released but its Produced On date ({day(1)}) is in the future, '
             'so its 50 units are not counted',
             '• Batch B-NOP-261001 is not linked to a product',
             '• Batch B-MPB-261001 (DC-BAR-MILK-PEANUT) has no Quantity Made, so it is not counted']
    report.check(S, 'each mistake is named in Slack (6 attention lines) and none of those units is counted',
                 len(notes) == 1 and '*Needs attention (6)*' in note and all(l in note for l in lines),
                 f'{sum(l in note for l in lines)} of {len(lines)} lines')
    report.check(S, 'the stock result is the seeded one: the same 6 changes, Success',
                 ex['status'] == 'success' and set(by_sku(set_stock_calls(mark))) == set(CHANGED)
                 and all(mock.shopify.available(s) == t for s, (_, t) in CHANGED.items())
                 and mock.shopify.available('DC-BAR-DARK-ORANGE') == 62 and mock.shopify.available('DC-BAR-DARK-ESPRESSO') == 68
                 and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6,
                                                                   'Shopify Updates': 6, 'Promos Created': 3,
                                                                   'Purchasing Requests Created': 3, 'Errors': 0})


def scenario_slack_down(run):
    S = 'Slack is down'
    mock.reset()
    mock.fail('slack', status=500, times=3, match={'path_contains': '/alerts'})
    mark = mock.mark()
    status, answer, ex = run_now(run)
    alert = expect_alert(run, ex)
    tries = [e for e in mock.log('slack', since=mark) if e['path'].endswith('/alerts')]
    report.check(S, 'the note is tried 3 times; the work is saved (Sync Log Success) but the run ends at Stop: Sync Failed',
                 [e['status'] for e in tries] == [500] * 3 and ex['status'] == 'error'
                 and node_ran_last(run, ex, 'Stop: Sync Failed') and len(mock.shopify.adjustments) == 6
                 and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6,
                                                                   'Shopify Updates': 6, 'Promos Created': 3,
                                                                   'Purchasing Requests Created': 3, 'Errors': 0}
                 and f"Inventory sync {answer.get('runId')} finished, but the Slack note could not be sent." in alert,
                 alert.splitlines()[1][:150] if alert else 'no alert')


def scenario_schedule(n8n):
    """The daily schedule path. prepare_workflow disables schedule triggers, so this copy swaps the schedule node for a
    webhook with the same name and connections. Nothing else changes: the run must see itself as a Schedule run."""
    S = 'Scheduled run'
    mock.reset()
    src = json.loads(WORKFLOW.read_text(encoding='utf-8'))
    sched = next(n for n in src['nodes'] if n['type'] == 'n8n-nodes-base.scheduleTrigger')
    sched.update({'type': 'n8n-nodes-base.webhook', 'typeVersion': 2,
                  'parameters': {'httpMethod': 'POST', 'path': 'schedule-stand-in', 'responseMode': 'responseNode', 'options': {}}})
    wf = prepare_workflow(src, mock, SLUG, mock_env(mock))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        mark = mock.mark()
        run.post('schedule-stand-in', {}, timeout=180)
        ex = run.wait(timeout=180)
        rows = [r['fields'] for r in mock.airtable.records('Sync Log') if r['fields']['Run ID'].endswith(f"-{ex['id']}")]
        log = rows[0] if len(rows) == 1 else {}
        notes = slack_notes(mark)
        report.check(S, 'runs as a Schedule run: no Run Now node, no webhook answer node, Trigger Schedule in the Sync Log',
                     ex['status'] == 'success' and not run.ran(ex, 'Webhook: Run Now')
                     and not any(run.ran(ex, n) for n in ('Respond: 202 Started', 'Respond: 401 Unauthorized', 'Respond: 500 Config Error'))
                     and log.get('Trigger') == 'Schedule' and RUN_ID.fullmatch(log.get('Run ID', ''))
                     and len(notes) == 1 and notes[0].startswith(f"*Inventory sync* {log.get('Run ID')} (Schedule), Success"),
                     f"{ex['status']}, {log.get('Run ID')}")
        report.check(S, 'same result as the Run Now path: the 6 changes, 3 promos, 3 requests',
                     set(by_sku(set_stock_calls(mark))) == set(CHANGED)
                     and counts(log) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6, 'Shopify Updates': 6,
                                         'Promos Created': 3, 'Purchasing Requests Created': 3, 'Errors': 0})


def scenario_two_at_once(n8n):
    """Two Run Now calls right after each other (a double click). The runs overlap, so which one sets which SKU is up to
    timing; the checks only use what must hold either way. The Airtable rate limiter stays on: with 500 ms between
    calls, two runs together stay at 4 calls per second."""
    S = 'Two Run Now calls at once'
    mock.reset()
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        mark = mock.mark()
        answers = []
        for _ in range(2):
            status, body = run.post(HOOK, {}, headers=SECRET)
            answers.append((status, json.loads(body or '{}')))
        runs = run.wait_all(timeout=240)
        ids = [a.get('runId', '') for _, a in answers]
        rows = [sync_log(i) for i in ids]
        window, gap, limited = pacing(mark)
        report.check(S, 'both answer 202 with their own run id, each run writes its own Sync Log row, no 429 together',
                     [s for s, _ in answers] == [202, 202] and len(set(ids)) == 2 and all(RUN_ID.fullmatch(i) for i in ids)
                     and len(runs) == 2 and all(r.get('Run ID') for r in rows) and mock.airtable.count('Sync Log') == 4
                     and window <= 4 and not limited,
                     ', '.join(ids) + f', max {window} Airtable calls per second')
        sets = set_stock_calls(mark)
        applied = sorted(c['sku'] for a in mock.shopify.adjustments for c in a['changes'] if c['name'] == 'available')
        report.check(S, 'every change is applied exactly once in Shopify, Products ends right, Shopify Updates add up to 6',
                     applied == sorted(CHANGED) and len(mock.shopify.adjustments) == 6
                     and all(mock.shopify.available(s) == t and product_fields(s)['Shopify Stock'] == t for s, (_, t) in CHANGED.items())
                     and sum(r.get('Shopify Updates', 0) for r in rows) == 6
                     and all((r.get('Status') == 'Partial') == (r.get('Errors', 0) > 0) for r in rows),
                     f'{len(sets)} SetStock calls, updates {[r.get("Shopify Updates") for r in rows]}, '
                     f'errors {[r.get("Errors") for r in rows]}')
        promo_names = [r['fields']['Promo Name'] for r in mock.airtable.records('Promos')]
        report.check(S, 'no duplicate promo or purchasing request, and each new one is counted as created by one run only',
                     len(promo_names) == 4 and len(set(promo_names)) == 4 and mock.airtable.count('Purchasing Requests') == 5
                     and all(len(open_requests(s)) == 1 for s in FACTS['newPurchasingRequestsExpected'] + ['DC-BAR-DARK-MINT'])
                     and sum(r.get('Promos Created', 0) for r in rows) == 3
                     and sum(r.get('Purchasing Requests Created', 0) for r in rows) == 3,
                     f"promos created {[r.get('Promos Created') for r in rows]}, "
                     f"requests created {[r.get('Purchasing Requests Created') for r in rows]}")
        failed = [ex for ex in runs if ex['status'] == 'error']
        got = {str(ex['id']): run.alert_for(ex, timeout=60) for ex in failed}
        texts = run.error_alerts()
        row_of = {str(ex['id']): next((r for r in rows if r.get('Run ID', '').endswith(f"-{ex['id']}")), {}) for ex in runs}
        report.check(S, 'a run that lost a SKU to the other one says so (Partial and one alert); a clean run sends none',
                     all(got.values()) and len(texts) == len(failed)
                     and all((ex['status'] == 'error') == (row_of[str(ex['id'])].get('Status') == 'Partial') for ex in runs),
                     ', '.join(f"{ex['status']}" for ex in runs) + f', {len(texts)} alert(s)')


def scenario_bad_config(n8n):
    S = 'Bad settings'
    mock.reset()
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, SHOPIFY_LOCATION_ID='', PROMO_WINDOW_DAYS='10'))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        mark = mock.mark()
        status, answer, ex = run_now(run)
        problems = ['SHOPIFY_LOCATION_ID is empty',
                    'PROMO_WINDOW_DAYS must be larger than SELL_BY_BUFFER_DAYS (or 0 to switch promos off), otherwise no sellable batch can get a promo']
        report.check(S, 'Run Now answers 500 with the exact problems, no API call',
                     status == 500 and answer == {'error': 'The inventory sync settings are missing or wrong', 'problems': problems}
                     and not calls(mark), f'{status}')
        alert = expect_alert(run, ex, count=False)
        report.check(S, 'the run fails at Stop: Bad Config and the alert lists the problems',
                     ex['status'] == 'error' and '*Stop: Bad Config*' in alert and 'SHOPIFY_LOCATION_ID is empty' in alert,
                     alert.splitlines()[1][:120] if alert else 'no alert')
        SAMPLES['alert_config'] = alert


def small_pages(max_batch_pages=None):
    """A test copy of the workflow that reads every Airtable table 10 records per page (Products has 22 active
    records, Production Batches 30 Released or Planned), optionally stopping the batch read after a few pages."""
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
    changed = 0
    for node in wf['nodes']:
        if node['type'] == 'n8n-nodes-base.code' and 'pageSize: 100,' in node['parameters']['jsCode']:
            node['parameters']['jsCode'] = node['parameters']['jsCode'].replace('pageSize: 100,', 'pageSize: 10,')
            changed += 1
        if node['name'] == 'Airtable: List Batches' and max_batch_pages:
            node['parameters']['options']['pagination']['pagination']['maxRequests'] = max_batch_pages
    assert changed == 4, changed
    return wf


def scenario_small_pages(n8n):
    S = 'Reads that need several pages'
    mock.reset()
    with n8n.temporary(small_pages(), SLUG, mock=mock) as run:
        mark = mock.mark()
        status, answer, ex = run_now(run)
        lists = [e for e in mock.log('airtable', since=mark) if e['operation'] == 'list']
        pages = {t: sum(1 for e in lists if e['table'] == t) for t in ('Products', 'Production Batches')}
        offsets = [e for e in lists if e['table'] == 'Production Batches' and isinstance(e['body'], dict) and e['body'].get('offset')]
        window, gap, limited = pacing(mark)
        report.check(S, '22 products and 30 batches read in 3 pages each (the offset goes back in the body), still paced',
                     pages == {'Products': 3, 'Production Batches': 3} and len(offsets) == 2 and window <= 2 and not limited,
                     f'{pages}, max {window} per second')
        report.check(S, 'same result as one page: the 6 changes, 3 promos, 3 requests',
                     ex['status'] == 'success' and set(by_sku(set_stock_calls(mark))) == set(CHANGED)
                     and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Success', 'Items Checked': 21, 'Items Changed': 6,
                                                                       'Shopify Updates': 6, 'Promos Created': 3,
                                                                       'Purchasing Requests Created': 3, 'Errors': 0})


def scenario_read_cut_short(n8n):
    S = 'Airtable read cut short'
    mock.reset()
    with n8n.temporary(small_pages(max_batch_pages=2), SLUG, mock=mock) as run:
        mark = mock.mark()
        status, answer, ex = run_now(run)
        alert = expect_alert(run, ex, count=False)
        made = calls(mark)
        report.check(S, 'a read stopped by its page limit is refused: nothing is written, Shopify is not called',
                     ex['status'] == 'error' and run.last_node(ex) == 'Code: Compute Sellable'
                     and [(e['service'], e.get('table')) for e in made]
                     == [('airtable', 'Products')] * 3 + [('airtable', 'Production Batches')] * 2
                     and 'The Production Batches read stopped after 2 pages with more records left' in alert,
                     alert.splitlines()[1][:150] if alert else 'no alert')


def scenario_closed_location(n8n):
    S = 'Closed Shopify location'
    mock.reset()
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, SHOPIFY_LOCATION_ID='gid://shopify/Location/71000000002'))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        mark = mock.mark()
        status, answer, ex = run_now(run)
        alert = expect_alert(run, ex, count=False)
        report.check(S, 'no stock is set, Products untouched, Sync Log Failed, the alert says the location is not active',
                     not set_stock_calls(mark) and not mock.shopify.adjustments and ex['status'] == 'error'
                     and all(product_fields(s)['Shopify Stock'] == f for s, (f, _) in CHANGED.items())
                     and counts(sync_log(answer.get('runId', ''))) == {'Status': 'Failed', 'Items Checked': 0, 'Items Changed': 0,
                                                                       'Shopify Updates': 0, 'Promos Created': 3,
                                                                       'Purchasing Requests Created': 3, 'Errors': 1}
                     and 'The Shopify location Old Warehouse (closed) is not active' in alert,
                     alert.splitlines()[1][:120] if alert else 'no alert')


# ---------------------------------------------------------------------------------------------- main

n8n = N8n()
n8n.cleanup(SLUG)
try:
    static_checks()
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        scenario_security(run)
        scenario_seeded(run)
        scenario_rerun(run)
        scenario_drift(run)
        scenario_sale_during_run(run)
        scenario_mapping(run)
        scenario_lost_answer(run)
        scenario_throttled(run)
        scenario_shopify_down(run)
        scenario_rate_limit(run)
        scenario_write_fails(run)
        scenario_airtable_down(run)
        scenario_manual_promo(run)
        scenario_many_changes(run)
        scenario_batch_mistakes(run)
        scenario_slack_down(run)
        alerts = mock.kept_alerts + mock.error_alerts()
        alerted = [m.group(1) for m in (re.search(r'/executions/(\d+)', a) for a in alerts) if m]
        report.check('Error alerts', f'exactly one alert per failed run ({len(FAILED_RUNS)}), none for the '
                                     f'{run.started - len(FAILED_RUNS)} runs that succeeded',
                     sorted(alerted) == sorted(FAILED_RUNS) and len(FAILED_RUNS) == 8 and len(alerts) == 8,
                     f'{len(alerts)} alerts for runs {", ".join(alerted)}')
    scenario_schedule(n8n)
    scenario_two_at_once(n8n)
    scenario_bad_config(n8n)
    scenario_small_pages(n8n)
    scenario_read_cut_short(n8n)
    scenario_closed_location(n8n)
finally:
    mock.stop()

ok = report.print()
if SAMPLES:
    print('\n---- what the workflow sent (seeded run) ----')
    print('\ninventorySetQuantities variables (DC-BAR-DARK-SEASALT):\n' + json.dumps(SAMPLES.get('set_stock'), indent=2))
    print('\nSlack note:\n' + SAMPLES.get('slack', ''))
    print('\nSync Log Details:\n' + SAMPLES.get('details', ''))
    print('\nPurchasing Request note (DC-PRALINE-HAZELNUT):\n' + SAMPLES.get('request_note', ''))
    for key in ('alert_stale', 'alert_shopify_down', 'alert_write', 'alert_airtable_down', 'alert_config'):
        print(f'\nError alert ({key[6:].replace("_", " ")}):\n' + SAMPLES.get(key, ''))
sys.exit(0 if ok else 1)
