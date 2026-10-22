"""End to end test for "Shopify Orders to Airtable, with Melt-Risk Check" against the shared MOCK APIs.

No real Shopify store, Airtable base, Open-Meteo or Slack workspace is used. The script:
  1. starts the shared strict mocks (tests/mocks) on port 8791: a seeded Airtable base with the 5 requests per
     second limit switched on, Open-Meteo geocoding and forecast, and Slack incoming webhooks,
  2. imports a temporary copy of the workflow into your n8n (API hosts pointed at the mocks, fake keys), plus an
     ACTIVE temporary copy of shared/error-alert-slack.json as its error workflow,
  3. delivers signed Shopify webhooks (X-Shopify-Hmac-Sha256, base64 HMAC SHA256 of the raw body) for new orders,
     updates, repeats, forged calls and outages, waits for each n8n run and checks every record and request,
  4. prints PASS or FAIL per check and deletes both temporary workflows.

Requirements: Python 3.9+, a running n8n 2.x with the public API enabled.
Environment:
  N8N_URL       default http://localhost:5678
  N8N_API_KEY   your n8n API key (Settings > n8n API), or N8N_ENV_FILE pointing at a .env that has it
  MOCK_HOST     how n8n reaches this machine; default host.docker.internal (n8n in Docker), 127.0.0.1 otherwise
Usage: python automations/01-shopify-orders-sync/tests/e2e_mock_test.py
It takes 6 to 9 minutes: every orders/updated waits 10 seconds on purpose, and two scenarios wait out the
31 to 60 second Airtable cooldown. Add --only=<scenario>,<scenario> to run some of them, --sent to print
the requests and messages the workflow sent, --alerts to print the error workflow's Slack alerts.
"""
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.mocks import MockServer, AIRTABLE_TOKEN  # noqa: E402
from tests.mocks.n8n import N8n, Report, mock_env, prepare_workflow  # noqa: E402
from tests.mocks.shopify import shop_time  # noqa: E402

SLUG, PORT = 'shopify-orders-sync', 8791
WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow' / 'shopify-orders-sync.json'
HOOK = 'shopify-orders'
WRITE_OPS = ('create', 'update', 'upsert', 'replace', 'delete')
SENT = {}   # requests and messages kept for docs/test-report.md
EXPECTED_REJECTIONS = set()   # request log seq numbers the mock refused on purpose (not injected failures)


def addr(city, region, zip_code, street='1 Test Way', country='US'):
    return {'address1': street, 'city': city, 'province_code': region, 'country_code': country, 'zip': zip_code}


PHOENIX = addr('Phoenix', 'AZ', '85004')
SEATTLE = addr('Seattle', 'WA', '98101', '12 Cedar Lane')


def line_id(number, position):
    return str(14100000000000 + number * 100 + position)


def ts(entry):
    return datetime.fromisoformat(entry['time'].replace('Z', '+00:00'))


class T:
    def __init__(self, mock, run, report):
        self.mock, self.run, self.r = mock, run, report
        self.at = mock.airtable
        self.created = {}   # created_at of each order, reused by its updates like Shopify does

    # ---------------------------------------------------------------- delivery
    def deliver(self, topic, payload, webhook_id=None, **kw):
        mark = self.mock.mark()
        t0 = time.monotonic()
        status, body = self.run.post_shopify(HOOK, topic, payload, webhook_id=webhook_id or str(uuid.uuid4()), **kw)
        elapsed = time.monotonic() - t0
        ex = self.run.wait(timeout=240)
        return mark, status, body, elapsed, ex

    def later(self, minutes):
        return shop_time(self.mock.clock.now() + timedelta(minutes=minutes))

    # ---------------------------------------------------------------- reading the mock
    def calls(self, mark, service='airtable', **match):
        return self.mock.log(service, since=mark, **match)

    def seq(self, mark):
        return [(e.get('table'), e.get('operation')) for e in self.calls(mark)]

    def writes(self, mark, table=None):
        return [e for e in self.calls(mark) if e.get('operation') in WRITE_OPS and (table is None or e.get('table') == table)]

    def order(self, number):
        rec = self.at.by_primary('Orders', number)
        return rec if isinstance(rec, dict) else None

    def lines(self, order_rec):
        return [self.at.record('Order Lines', rid) for rid in order_rec['fields'].get('Order Lines', [])]

    def line(self, number, position):
        rec = self.at.by_primary('Order Lines', line_id(number, position))
        return rec if isinstance(rec, dict) else None

    def webhook_rows(self, webhook_id):
        return self.at.find('Webhook Log', **{'Event ID': webhook_id})

    def run_row(self, webhook_id):
        rows = self.at.find('Sync Log', **{'Run ID': f'orders-{webhook_id}'})
        return rows[0] if len(rows) == 1 else None

    def slack(self, mark, path):
        return [e for e in self.calls(mark, 'slack') if path in e['path']]

    def team_slack(self, mark):
        """Slack posts made by the workflow itself. The error workflow posts to /errors, and its alert can land a
        moment after the failed run has finished, so it is not counted here (run.alert_for checks it)."""
        return [e for e in self.calls(mark, 'slack') if '/errors' not in e['path']]

    def weather(self, mark):
        return self.calls(mark, 'open-meteo-geocoding'), self.calls(mark, 'open-meteo-forecast')

    def pacing(self, mark):
        """(smallest gap in seconds between two Airtable requests, most requests inside any one second)."""
        times = [ts(e) for e in self.calls(mark)]
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        burst = max((sum(1 for u in times if 0 <= (u - t).total_seconds() < 1.0) for t in times), default=0)
        return (min(gaps) if gaps else None), burst


def alert_for(run, ex, timeout=90):
    """The error workflow's Slack alert for this failed run, matched by the execution link inside it.
    n8n starts the error workflow after the failed run has finished, and on a shared, busy n8n that can take a
    while, so this waits up to 90 seconds. When no alert arrives it prints what the error workflow did, so a slow
    alert can be told apart from a missing one."""
    alert = run.alert_for(ex, timeout=timeout)
    if alert is None and run.error_wid:
        page = run.n8n.api('GET', f'/api/v1/executions?workflowId={run.error_wid}&limit=50')
        states = [f"{e['id']} {e.get('status')}" for e in page.get('data', [])]
        print(f"  (no alert for run {ex['id']} after {timeout} s; error workflow runs so far: {', '.join(states) or 'none'})",
              file=sys.stderr)
    return alert


def same_time(a, b):
    """True when two ISO times (Airtable's ...Z, Shopify's with an offset) are the same moment."""
    try:
        return datetime.fromisoformat(str(a).replace('Z', '+00:00')) == datetime.fromisoformat(str(b).replace('Z', '+00:00'))
    except ValueError:
        return False


def subset(actual, expected):
    """Keys of expected that differ in actual, as 'key: actual != expected'."""
    return [f'{k}: {actual.get(k)!r} != {v!r}' for k, v in expected.items() if actual.get(k) != v]


# ====================================================================== scenarios

def new_order_hot_city(t):
    S = 'New order to a hot city (Phoenix), new customer'
    customers_before = t.at.count('Customers')
    p = t.mock.shopify.order_payload(1026, 'new.buyer@example.com',
                                     [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5},
                                      {'sku': 'DC-BAR-MILK-CARAMEL', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=PHOENIX, customer={'name': 'New Buyer', 'phone': '+15550109026'})
    t.created[1026] = p['created_at']
    mark, status, body, elapsed, ex = t.deliver('orders/create', p, webhook_id='wh-1026-create')
    t.r.check(S, 'Shopify gets 200 within 5 seconds, before the slow work', status == 200 and body == 'ok' and elapsed < 5,
              f'{status} {body!r} in {elapsed:.2f} s')
    t.r.check(S, 'the run succeeds and ends at Done: Order Synced', ex['status'] == 'success' and t.run.last_node(ex) == 'Done: Order Synced',
              f"{ex['status']}, {t.run.last_node(ex)}")
    o = t.order('#1026')
    cust = t.at.find('Customers', **{'Shopify Customer ID': '7700000001026'})
    t.r.check(S, 'one new customer with the Shopify details',
              t.at.count('Customers') == customers_before + 1 and len(cust) == 1 and not subset(cust[0]['fields'], {
                  'Name': 'New Buyer', 'Email': 'new.buyer@example.com', 'Phone': '+15550109026',
                  'Created Via': 'Shopify', 'Record Status': 'Active'}),
              f"{t.at.count('Customers') - customers_before} new, {cust[0]['fields'] if cust else 'none'}")
    expected = {'Order Number': '#1026', 'Shopify Order ID': '5200000001026', 'Email': 'new.buyer@example.com',
                'Financial Status': 'Paid', 'Fulfillment Status': 'Unfulfilled', 'Order Status': 'Open',
                'Subtotal': 22.0, 'Shipping': 6.95, 'Tax': 0, 'Total': 28.95, 'Refunded Amount': 0, 'Currency': 'USD',
                'Shipping Name': 'New Buyer', 'Shipping Address': '1 Test Way', 'Shipping City': 'Phoenix',
                'Shipping Region': 'AZ', 'Shipping Country': 'US', 'Shipping Postal Code': '85004',
                'Cold Pack': 'Required', 'Forecast Max C': 37.1,
                'Melt Check Note': 'Forecast max 37.1 C over the next 3 days in Phoenix, Arizona (cold pack from 27 C).',
                'Customer': [cust[0]['id']] if cust else None}
    diff = subset(o['fields'], expected) if o else ['no order']
    extra = [k for k in ('Needs Review', 'Is Gift', 'Gift Note', 'Cancelled At') if o and k in o['fields']]
    t.r.check(S, 'one Orders record with the exact values, Cold Pack Required at 37.1 C', o and not diff and not extra
              and 'Melt Checked At' in o['fields'] and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001026'})) == 1,
              '; '.join(diff + extra) or 'all fields match')
    l1, l2 = t.line(1026, 1), t.line(1026, 2)
    t.r.check(S, 'two Order Lines linked to the order and to their products', l1 and l2 and not subset(l1['fields'], {
        'Order': [o['id']], 'Product': [t.at.ref('Products', 'dss')], 'SKU': 'DC-BAR-DARK-SEASALT', 'Quantity': 2,
        'Current Quantity': 2, 'Refunded Quantity': 0, 'Unit Price': 7.5, 'Line Total': 15.0, 'SKU Status': 'Matched',
        'Product Title': 'Dark 70% Sea Salt Bar'}) and not subset(l2['fields'], {
        'Order': [o['id']], 'Product': [t.at.ref('Products', 'msc')], 'Quantity': 1, 'Line Total': 7.0, 'SKU Status': 'Matched'})
        and sorted(o['fields'].get('Order Lines', [])) == sorted([l1['id'], l2['id']]),
        f"{len(o['fields'].get('Order Lines', [])) if o else 0} lines")
    geo, fc = t.weather(mark)
    gp = geo[0]['params'] if geo else {}
    fp = fc[0]['params'] if fc else {}
    t.r.check(S, 'geocoding asks for "Phoenix, AZ" in the US, the forecast for 3 days of temperature_2m_max',
              len(geo) == 1 and len(fc) == 1 and gp.get('name') == 'Phoenix, AZ' and gp.get('countryCode') == 'US'
              and gp.get('count') == '10' and fp.get('daily') == 'temperature_2m_max' and fp.get('timezone') == 'auto'
              and fp.get('forecast_days') == '3' and fp.get('latitude') == '33.44838',
              f'geocode {gp}, forecast {fp}')
    packing = t.slack(mark, '/packing')
    text = packing[0]['body'].get('text', '') if packing else ''
    t.r.check(S, 'one packing alert with the forecast, the address and the items',
              len(packing) == 1 and text.startswith(':snowflake: *Cold pack needed* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001026|#1026>')
              and 'Forecast max 37.1 C' in text and 'Ship to: New Buyer, Phoenix, AZ, 85004' in text
              and 'Items: 2 x Dark 70% Sea Salt Bar, 1 x Milk Salted Caramel Bar' in text and not t.slack(mark, '/alerts'),
              text.splitlines()[0] if text else 'no alert')
    expected_seq = [('Webhook Log', 'list'), ('Orders', 'list'), ('Customers', 'list'), ('Customers', 'upsert'),
                    ('Products', 'list'), ('Orders', 'upsert'), ('Order Lines', 'upsert'), ('Webhook Log', 'upsert'),
                    ('Sync Log', 'upsert')]
    calls = t.calls(mark)
    t.r.check(S, 'exactly 9 Airtable requests in this order, all 200, every one authorized',
              t.seq(mark) == expected_seq and all(e['status'] == 200 for e in calls)
              and all(e['headers'].get('authorization') == 'Bearer ' + AIRTABLE_TOKEN for e in calls),
              ', '.join(f'{a} {b}' for a, b in t.seq(mark)))
    cu = next((e for e in calls if e.get('table') == 'Customers' and e.get('operation') == 'upsert'), None)
    t.r.check(S, 'the new customer is an upsert on Shopify Customer ID, never on Email',
              cu and cu['body']['performUpsert'] == {'fieldsToMergeOn': ['Shopify Customer ID']},
              json.dumps(cu['body']['performUpsert']) if cu else 'no customer write')
    gap, burst = t.pacing(mark)
    t.r.check(S, 'Airtable calls are paced: at least 500 ms apart, never more than 2 in one second',
              gap is not None and gap >= 0.49 and burst <= 2, f'smallest gap {gap:.3f} s, busiest second {burst} requests')
    wl = t.webhook_rows('wh-1026-create')
    sl = t.run_row('wh-1026-create')
    t.r.check(S, 'Webhook Log row Processed and linked, Sync Log row Success with counts',
              len(wl) == 1 and not subset(wl[0]['fields'], {'Status': 'Processed', 'Topic': 'orders/create', 'Source': 'Shopify',
                                                            'Order': [o['id']]})
              and sl and not subset(sl['fields'], {'Automation': '01 Shopify Orders', 'Trigger': 'Webhook', 'Status': 'Success',
                                                   'Items Checked': 2, 'Items Changed': 4, 'Shopify Updates': 0, 'Errors': 0}),
              (wl[0]['fields'].get('Notes', '') if wl else 'no Webhook Log row'))
    SENT['orders_upsert'] = next(e['body'] for e in calls if e.get('table') == 'Orders' and e.get('operation') == 'upsert')
    SENT['lines_upsert'] = next(e['body'] for e in calls if e.get('table') == 'Order Lines')
    SENT['customer_upsert'] = cu['body'] if cu else None
    SENT['geocode'] = gp
    SENT['forecast'] = fp
    SENT['packing_alert'] = text
    SENT['webhook_notes'] = wl[0]['fields'].get('Notes') if wl else ''


def repeat_delivery(t):
    S = 'Same webhook delivered twice'
    p = t.mock.shopify.order_payload(1026, 'new.buyer@example.com',
                                     [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5},
                                      {'sku': 'DC-BAR-MILK-CARAMEL', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=PHOENIX, customer={'name': 'New Buyer'})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1026-create')
    geo, fc = t.weather(mark)
    t.r.check(S, 'second copy skipped: one lookup, no writes, no weather call, no Slack',
              status == 200 and t.run.last_node(ex) == 'Done: Repeat Skipped' and t.seq(mark) == [('Webhook Log', 'list')]
              and not geo and not fc and not t.team_slack(mark),
              f'ended at {t.run.last_node(ex)}, {len(t.calls(mark))} Airtable request(s)')
    S = 'Webhook id already in the seeded Webhook Log'
    p = t.mock.shopify.order_payload(1024, 'mila.petrova@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Tucson', 'AZ', '85701', '77 Mill Road'),
                                     customer={'name': 'Mila Petrova', 'id': 7700000000129})
    logs_before = t.at.count('Webhook Log')
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='b54557e4-bdd9-4b37-8a5f-bf7d70bcd043')
    geo, fc = t.weather(mark)
    t.r.check(S, 'b54557e4-... is recognised as processed: one lookup, no writes, no weather call, no Slack',
              status == 200 and t.run.last_node(ex) == 'Done: Repeat Skipped' and t.seq(mark) == [('Webhook Log', 'list')]
              and not geo and not fc and not t.team_slack(mark) and t.at.count('Webhook Log') == logs_before,
              f'ended at {t.run.last_node(ex)}, {len(t.calls(mark))} Airtable request(s)')


def update_refund(t):
    S = 'orders/updated: partial refund and fulfilment'
    o_before = t.order('#1026')
    lines_before = t.at.count('Order Lines')
    customers_before = t.at.count('Customers')
    p = t.mock.shopify.order_payload(1026, 'new.buyer@example.com',
                                     [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5},
                                      {'sku': 'DC-BAR-MILK-CARAMEL', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=PHOENIX, customer={'name': 'New Buyer', 'phone': '+15550109026'},
                                     financial_status='partially_refunded', fulfillment_status='fulfilled',
                                     refunds=[{'line_item_id': int(line_id(1026, 1)), 'quantity': 1}],
                                     created_at=t.created[1026], updated_at=t.later(10))
    mark, status, _, _, ex = t.deliver('orders/updated', p, webhook_id='wh-1026-update')
    o = t.order('#1026')
    t.r.check(S, 'the same Orders record is updated in place',
              o and o_before and o['id'] == o_before['id'] and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001026'})) == 1
              and not subset(o['fields'], {'Financial Status': 'Partially Refunded', 'Fulfillment Status': 'Fulfilled',
                                           'Refunded Amount': 7.5, 'Total': 28.95, 'Cold Pack': 'Required', 'Forecast Max C': 37.1}),
              '; '.join(subset(o['fields'], {'Financial Status': 'Partially Refunded', 'Fulfillment Status': 'Fulfilled',
                                              'Refunded Amount': 7.5})) or 'Partially Refunded, Fulfilled, 7.50 refunded')
    l1 = t.line(1026, 1)
    lw = t.writes(mark, 'Order Lines')
    t.r.check(S, 'lines updated, none added: the refunded unit shows on line 1',
              t.at.count('Order Lines') == lines_before and l1 and not subset(l1['fields'], {
                  'Quantity': 2, 'Current Quantity': 1, 'Refunded Quantity': 1, 'Line Total': 7.5})
              and len(lw) == 1 and lw[0]['response'].get('createdRecords') == [] and len(lw[0]['response'].get('updatedRecords', [])) == 2,
              f"line 1 {l1['fields'].get('Current Quantity')} of {l1['fields'].get('Quantity')}, refunded {l1['fields'].get('Refunded Quantity')}" if l1 else 'no line')
    geo, fc = t.weather(mark)
    t.r.check(S, 'no second melt check, no Slack, no customer write',
              not geo and not fc and not t.team_slack(mark) and not t.writes(mark, 'Customers')
              and t.at.count('Customers') == customers_before, f'{len(geo)} geocoding calls, {len(t.team_slack(mark))} Slack posts')


def stale_update(t):
    S = 'Older orders/updated arrives after a newer one'
    p = t.mock.shopify.order_payload(1026, 'new.buyer@example.com',
                                     [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5},
                                      {'sku': 'DC-BAR-MILK-CARAMEL', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=PHOENIX, customer={'name': 'New Buyer'},
                                     financial_status='paid', created_at=t.created[1026], updated_at=t.later(5))
    def own(fields):   # the order's own cells, without the inverse link the new Webhook Log row adds
        return {k: v for k, v in fields.items() if k != 'Webhook Log'}
    before = own(t.order('#1026')['fields'])
    mark, status, _, _, ex = t.deliver('orders/updated', p, webhook_id='wh-1026-stale')
    o = t.order('#1026')
    wl = t.webhook_rows('wh-1026-stale')
    sl = t.run_row('wh-1026-stale')
    geo, fc = t.weather(mark)
    t.r.check(S, 'nothing overwritten, the webhook is logged as Ignored and linked to the order',
              own(o['fields']) == before
              and t.seq(mark) == [('Webhook Log', 'list'), ('Orders', 'list'), ('Webhook Log', 'upsert'), ('Sync Log', 'upsert')]
              and not geo and not fc and not t.team_slack(mark)
              and len(wl) == 1 and not subset(wl[0]['fields'], {'Status': 'Ignored', 'Order': [o['id']]})
              and 'older than the version already in Airtable' in wl[0]['fields'].get('Notes', '')
              and sl and not subset(sl['fields'], {'Status': 'Success', 'Items Checked': 0, 'Items Changed': 0, 'Errors': 0}),
              wl[0]['fields'].get('Notes', '')[:120] if wl else 'no row')


def customer_matching(t):
    S = 'Customer matching'
    before = t.at.count('Customers')
    p = t.mock.shopify.order_payload(1027, 'jordan.blake@example.com', [{'sku': 'DC-BAR-DARK-85', 'quantity': 1, 'price': 8.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Jordan Blake', 'id': 7700000000103, 'accepts_marketing': True})
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1027')
    t.r.check(S, 'jordan (two records share the email) is matched by Shopify Customer ID, not the duplicate',
              o and o['fields'].get('Customer') == [t.at.ref('Customers', 'jordan')] and not t.writes(mark, 'Customers')
              and t.at.count('Customers') == before, f"linked to {t.at.primary_name('Customers', o['fields']['Customer'][0]) if o and o['fields'].get('Customer') else 'nobody'}")
    p = t.mock.shopify.order_payload(1028, ' Maya.Chen@Example.COM', [{'sku': 'DC-BAR-WHITE-MATCHA', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=SEATTLE, customer={'name': 'Maya Chen', 'id': 7700000000999})
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1028')
    maya = t.at.record('Customers', t.at.ref('Customers', 'maya'))
    t.r.check(S, 'messy email and an unknown id: matched by LOWER(TRIM(Email)) to the record that has a Shopify id',
              o and o['fields'].get('Customer') == [maya['id']] and o['fields'].get('Email') == 'maya.chen@example.com'
              and maya['fields'].get('Shopify Customer ID') == '7700000000101' and not t.writes(mark, 'Customers')
              and t.at.count('Customers') == before, f"Email stored as {o['fields'].get('Email') if o else None}")
    p = t.mock.shopify.order_payload(1029, 'kai.morgan@example.com', [{'sku': 'DC-BAR-OAT-MILK', 'quantity': 3, 'price': 7.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Kai Morgan', 'id': 7700000000132, 'phone': '+15550101032'})
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1029')
    kai = t.at.record('Customers', t.at.ref('Customers', 'kai'))
    cw = t.writes(mark, 'Customers')
    t.r.check(S, 'email match without an id: the record is updated by record id (Name, id, phone filled in)',
              o and o['fields'].get('Customer') == [kai['id']] and len(cw) == 1 and cw[0]['operation'] == 'update'
              and cw[0]['body'] == {'records': [{'id': kai['id'], 'fields': {'Shopify Customer ID': '7700000000132', 'Name': 'Kai Morgan', 'Phone': '+15550101032'}}]}
              and t.at.count('Customers') == before, json.dumps(cw[0]['body']) if cw else 'no write')

    # A guest checkout carries no Shopify customer id: only the email is looked up.
    p = t.mock.shopify.order_payload(1053, 'jordan.blake@example.com', [{'sku': 'DC-BAR-DARK-85', 'quantity': 1, 'price': 8.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Jordan Blake', 'id': None})
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1053')
    lookups = t.calls(mark, table='Customers', operation='list')
    t.r.check(S, 'guest checkout by jordan (no customer id, two records share the email): the record with a Shopify id wins',
              o and o['fields'].get('Customer') == [t.at.ref('Customers', 'jordan')] and not t.writes(mark, 'Customers')
              and len(lookups) == 1 and lookups[0]['body'].get('filterByFormula') == "OR(LOWER(TRIM({Email})) = 'jordan.blake@example.com')"
              and t.at.count('Customers') == before,
              lookups[0]['body'].get('filterByFormula') if lookups else 'no lookup')

    p = t.mock.shopify.order_payload(1054, 'Guest.Buyer@example.com', [{'sku': 'DC-BAR-OAT-MILK', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Guest Buyer', 'id': None, 'phone': '+15550109054'})
    t.created[1054] = p['created_at']
    mark, _, _, _, ex = t.deliver('orders/create', p)
    o = t.order('#1054')
    cw = t.writes(mark, 'Customers')
    guest = t.at.find('Customers', Email='guest.buyer@example.com')
    t.r.check(S, 'a new guest without a customer id is created with a plain POST (an upsert needs a key), email in lower case',
              ex['status'] == 'success' and len(cw) == 1 and cw[0]['method'] == 'POST' and cw[0]['operation'] == 'create'
              and cw[0]['body'] == {'records': [{'fields': {'Created Via': 'Shopify', 'Record Status': 'Active', 'Accepts Marketing': False,
                                                            'Name': 'Guest Buyer', 'Email': 'guest.buyer@example.com', 'Phone': '+15550109054'}}]}
              and len(guest) == 1 and o and o['fields'].get('Customer') == [guest[0]['id']] and t.at.count('Customers') == before + 1,
              json.dumps(cw[0]['body']) if cw else 'no write')
    mark, *_ = t.deliver('orders/updated', dict(p, updated_at=t.later(12), financial_status='paid'))
    t.r.check(S, 'the next webhook for that guest finds the record by email: no second customer',
              not t.writes(mark, 'Customers') and t.at.count('Customers') == before + 1
              and t.order('#1054')['fields'].get('Customer') == [guest[0]['id']] if guest else False,
              f"{t.at.count('Customers') - before} new customer(s)")

    p = t.mock.shopify.order_payload(1055, '', [{'sku': 'DC-BAR-MILK-CLASSIC', 'quantity': 1, 'price': 6.5}],
                                     shipping_address=SEATTLE, customer={'id': None})
    mark, _, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1055-create')
    o = t.order('#1055')
    wl = t.webhook_rows('wh-1055-create')
    t.r.check(S, 'no email and no customer id: the order is saved without a customer and flagged for review',
              ex['status'] == 'success' and o and 'Customer' not in o['fields'] and 'Email' not in o['fields']
              and o['fields'].get('Needs Review') is True
              and o['fields'].get('Review Reason') == 'No customer email or Shopify customer id on the order, so no customer is linked.'
              and not t.writes(mark, 'Customers') and t.at.count('Customers') == before + 1
              and len(wl) == 1 and 'No customer linked.' in wl[0]['fields'].get('Notes', ''),
              o['fields'].get('Review Reason') if o else 'no order')


def unknown_sku(t):
    S = 'Unknown SKU, existing Draft, messy SKU, line without SKU'
    products_before = t.at.count('Products')
    pumpkin = t.at.ref('Products', 'psb')
    p = t.mock.shopify.order_payload(1030, 'hana.sato@example.com', [
        {'sku': 'DC-BAR-GINGER', 'quantity': 1, 'price': 7.95},
        {'sku': 'DC-BAR-PUMPKIN-SPICE', 'quantity': 2, 'price': 7.5},
        {'sku': ' dc-bar-milk-caramel', 'quantity': 1, 'price': 7.0, 'title': 'Milk Salted Caramel Bar'},
        {'sku': None, 'quantity': 1, 'price': 0, 'title': 'Gift wrap'}],
        shipping_address=addr('Minneapolis', 'MN', '55401'), customer={'name': 'Hana Sato', 'id': 7700000000110})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1030-create')
    ginger = t.at.find('Products', SKU='DC-BAR-GINGER')
    g = ginger[0]['fields'] if len(ginger) == 1 else {}
    t.r.check(S, 'DC-BAR-GINGER becomes one Draft product with Needs Review and the reason',
              t.at.count('Products') == products_before + 1 and len(ginger) == 1 and not subset(g, {
                  'Name': 'Dark Ginger Bar', 'Status': 'Draft', 'Needs Review': True, 'Price': 7.95,
                  'Review Reason': 'Created automatically from Shopify order #1030: unknown SKU (Shopify variant 4510000099001). '
                                   'Add allergens, ingredients and stock rules before selling.'}),
              '; '.join(subset(g, {'Status': 'Draft', 'Needs Review': True})) or g.get('Review Reason', 'no product'))
    pw = t.writes(mark, 'Products')
    t.r.check(S, 'one product upsert on SKU with exactly 1 record; the Pumpkin Spice Draft is matched, not created again',
              len(pw) == 1 and pw[0]['body']['performUpsert'] == {'fieldsToMergeOn': ['SKU']} and pw[0]['record_count'] == 1
              and len(t.at.find('Products', SKU='DC-BAR-PUMPKIN-SPICE')) == 1,
              f"{len(pw)} product write(s), {pw[0]['record_count'] if pw else 0} record(s)")
    l1, l2, l3, l4 = (t.line(1030, i) for i in range(1, 5))
    ok = (l1 and l2 and l3 and l4
          and l1['fields'].get('Product') == [ginger[0]['id']] and l1['fields'].get('SKU Status') == 'Matched'
          and l2['fields'].get('Product') == [pumpkin] and l2['fields'].get('SKU Status') == 'Matched'
          and l3['fields'].get('Product') == [t.at.ref('Products', 'msc')] and l3['fields'].get('SKU') == ' dc-bar-milk-caramel'
          and l3['fields'].get('SKU Status') == 'Matched'
          and 'Product' not in l4['fields'] and 'SKU' not in l4['fields'] and l4['fields'].get('SKU Status') == 'Missing SKU'
          and l4['fields'].get('Unit Price') == 0 and l4['fields'].get('Line Total') == 0)
    t.r.check(S, 'lines: new Draft linked, Draft matched, " dc-bar-milk-caramel" matched (kept as sent), gift wrap Missing SKU at 0',
              ok, ', '.join(f"{(x['fields'].get('SKU') or '(none)').strip()} {x['fields'].get('SKU Status')}" for x in (l1, l2, l3, l4) if x))
    o = t.order('#1030')
    t.r.check(S, 'the order is flagged for review with the reason',
              o and o['fields'].get('Needs Review') is True
              and o['fields'].get('Review Reason') == 'Unknown SKU DC-BAR-GINGER on line 1. A draft product was created.',
              o['fields'].get('Review Reason') if o else 'no order')
    ops = t.slack(mark, '/alerts')
    text = ops[0]['body'].get('text', '') if ops else ''
    t.r.check(S, 'one ops alert about the new SKU, no packing alert (Minneapolis is cold)',
              len(ops) == 1 and text.startswith(':package: *New SKU from Shopify* on order') and 'DC-BAR-GINGER (Dark Ginger Bar)' in text
              and not t.slack(mark, '/packing') and o['fields'].get('Cold Pack') == 'Not Needed', text.splitlines()[0] if text else 'no alert')
    SENT['sku_alert'] = text
    SENT['draft_upsert'] = pw[0]['body'] if pw else None

    S = 'orders/updated for the same order'
    mark, *_ = t.deliver('orders/updated', dict(p, updated_at=t.later(15)), webhook_id='wh-1030-update')
    lw = t.writes(mark, 'Order Lines')
    o = t.order('#1030')
    t.r.check(S, 'no new product, no new lines, no second alert, the review reason is not repeated',
              t.at.count('Products') == products_before + 1 and not t.writes(mark, 'Products') and len(lw) == 1
              and lw[0]['response'].get('createdRecords') == [] and len(lw[0]['response'].get('updatedRecords', [])) == 4
              and not t.team_slack(mark)
              and o['fields'].get('Review Reason') == 'Unknown SKU DC-BAR-GINGER on line 1. A draft product was created.'
              and t.line(1030, 1)['fields'].get('SKU Status') == 'Matched', f"{len(lw[0]['response'].get('updatedRecords', [])) if lw else 0} lines updated")


def many_unknown_skus(t):
    S = 'Twelve unknown SKUs on one order'
    products_before = t.at.count('Products')
    skus = [f'DC-TEST-NEW-{i:02d}' for i in range(1, 13)]
    p = t.mock.shopify.order_payload(1056, 'hana.sato@example.com',
                                     [{'sku': s, 'quantity': 1, 'price': 5.0 + i, 'title': f'Test Bar {i:02d}'} for i, s in enumerate(skus, 1)],
                                     shipping_address=addr('Minneapolis', 'MN', '55401'), customer={'name': 'Hana Sato', 'id': 7700000000110})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1056-create')
    pw = t.writes(mark, 'Products')
    created = [t.at.find('Products', SKU=s) for s in skus]
    ok_products = all(len(c) == 1 and not subset(c[0]['fields'], {'Status': 'Draft', 'Needs Review': True, 'Name': f'Test Bar {i:02d}', 'Price': 5.0 + i})
                      for i, c in enumerate(created, 1))
    t.r.check(S, 'Draft products created in two upserts on SKU, 10 and 2 records, 500 ms apart',
              ex['status'] == 'success' and [e['record_count'] for e in pw] == [10, 2]
              and all(e['body']['performUpsert'] == {'fieldsToMergeOn': ['SKU']} for e in pw)
              and t.at.count('Products') == products_before + 12 and ok_products and t.pacing(mark)[0] >= 0.49,
              f"{[e['record_count'] for e in pw]}, {t.at.count('Products') - products_before} new products")
    lines = [t.line(1056, i) for i in range(1, 13)]
    t.r.check(S, 'all 12 lines are linked to their new Draft product',
              all(c and l and l['fields'].get('Product') == [c[0]['id']] and l['fields'].get('SKU Status') == 'Matched'
                  for l, c in zip(lines, created)),
              ', '.join(sorted({(l['fields'].get('SKU Status') or 'none') for l in lines if l})))
    o = t.order('#1056')
    ops = t.slack(mark, '/alerts')
    text = ops[0]['body'].get('text', '') if ops else ''
    t.r.check(S, 'one ops alert names all 12 SKUs, and the order lists all 12 reasons',
              len(ops) == 1 and all(f'{s} (Test Bar {i:02d})' in text for i, s in enumerate(skus, 1))
              and 'are not in Airtable, so Draft products were created' in text
              and o and o['fields'].get('Review Reason') == '\n'.join(
                  f'Unknown SKU {s} on line {i}. A draft product was created.' for i, s in enumerate(skus, 1)),
              text.splitlines()[0] if text else 'no alert')


def gift_orders(t):
    S = 'Gift orders'
    p = t.mock.shopify.order_payload(1031, 'elena.rossi@example.com', [{'sku': 'DC-BOX-PRALINE-12', 'quantity': 1, 'price': 24.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Elena Rossi', 'id': 7700000000108},
                                     note='Leave at the door', note_attributes=[{'name': 'Gift Message', 'value': 'Happy birthday, Sam!'}])
    t.created[1031] = p['created_at']
    mark, *_ = t.deliver('orders/create', p, webhook_id='wh-1031-create')
    o = t.order('#1031')
    t.r.check(S, 'a "Gift Message" cart attribute: Is Gift and the message for the packing slip (not the delivery note)',
              o and not subset(o['fields'], {'Is Gift': True, 'Gift Note': 'Happy birthday, Sam!', 'Cold Pack': 'Not Needed', 'Forecast Max C': 13.2})
              and not t.slack(mark, '/packing'), f"{o['fields'].get('Gift Note')!r}, Cold Pack {o['fields'].get('Cold Pack')}" if o else 'no order')
    p = t.mock.shopify.order_payload(1032, 'aisha.khan@example.com', [{'sku': 'DC-BAR-RUBY-CHERRY', 'quantity': 2, 'price': 7.5}],
                                     shipping_address=PHOENIX, customer={'name': 'Aisha Khan', 'id': 7700000000114},
                                     note='For Grandma, with love', tags='gift, vip')
    t.created[1032] = p['created_at']
    mark, *_ = t.deliver('orders/create', p, webhook_id='wh-1032-create')
    o = t.order('#1032')
    packing = t.slack(mark, '/packing')
    text = packing[0]['body'].get('text', '') if packing else ''
    t.r.check(S, 'tag "gift": the order note is the gift note, and the packing alert carries it',
              o and not subset(o['fields'], {'Is Gift': True, 'Gift Note': 'For Grandma, with love', 'Shopify Tags': 'gift, vip', 'Cold Pack': 'Required'})
              and len(packing) == 1 and 'Gift note for the packing slip: "For Grandma, with love"' in text, text.splitlines()[-1] if text else 'no alert')

    S = 'Customer text in a Slack alert'
    note = 'Love you <3 & see you soon <!channel> <https://evil.example/login|claim your prize>'
    p = t.mock.shopify.order_payload(1058, 'ana.leo@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=PHOENIX, customer={'name': 'Ana & Leo <Test>', 'id': 7700000001058},
                                     note_attributes=[{'name': 'Gift Message', 'value': note}])
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1058')
    packing = t.slack(mark, '/packing')
    text = packing[0]['body'].get('text', '') if packing else ''
    t.r.check(S, 'a gift note with <!channel> and a link cannot ping the channel or post a link; Airtable keeps the note as typed',
              o and o['fields'].get('Gift Note') == note and len(packing) == 1
              and 'Gift note for the packing slip: "Love you &lt;3 &amp; see you soon &lt;!channel&gt; '
                  '&lt;https://evil.example/login|claim your prize&gt;"' in text
              and 'Ship to: Ana &amp; Leo &lt;Test&gt;, Phoenix, AZ, 85004' in text
              and '<!channel>' not in text and '<https://evil.example' not in text
              and text.startswith(':snowflake: *Cold pack needed* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001058|#1058>'),
              text.splitlines()[-1][:120] if text else 'no alert')


def thresholds(t):
    S = 'Melt threshold edges (MELT_TEMP_C 27, 3 days)'
    t.mock.meteo.set_forecast('Denver', [20.0, 27.0, 18.0])
    t.mock.meteo.set_forecast('Boston', [10.0, 11.0, 12.0, 35.0])
    cases = [(1033, addr('Atlanta', 'GA', '30303'), 'Not Needed', 26.1, 'Atlanta 26.1 C, the closest to the line'),
             (1034, addr('Denver', 'CO', '80202'), 'Required', 27.0, 'exactly 27.0 C on day 2 counts (at or above)'),
             (1035, addr('Boston', 'MA', '02108'), 'Not Needed', 12.0, '35 C on day 4 is outside the 3 day window'),
             (1036, addr('Austin', 'TX', '78701'), 'Required', 30.1, '"Austin, TX" with a state code qualifier')]
    for number, ship, want, temp, label in cases:
        p = t.mock.shopify.order_payload(number, f'buyer{number}@example.com', [{'sku': 'DC-BAR-MILK-CLASSIC', 'quantity': 1, 'price': 6.5}],
                                         shipping_address=ship, customer={'name': f'Buyer {number}'})
        mark, *_ = t.deliver('orders/create', p)
        o = t.order(f'#{number}')
        alerts = t.slack(mark, '/packing')
        t.r.check(S, f'{label}: {want}', o and o['fields'].get('Cold Pack') == want and o['fields'].get('Forecast Max C') == temp
                  and len(alerts) == (1 if want == 'Required' else 0),
                  f"{o['fields'].get('Cold Pack')}, max {o['fields'].get('Forecast Max C')}, {len(alerts)} packing alert(s)" if o else 'no order')


def weather_failures(t):
    S = 'Geocoding service down (500 three times)'
    t.mock.fail('open-meteo-geocoding', status=500, times=3)
    p = t.mock.shopify.order_payload(1037, 'tom.becker@example.com', [{'sku': 'DC-BAR-DARK-CHILI', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Houston', 'TX', '77002'), customer={'name': 'Tom Becker', 'id': 7700000000113})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1037-create')
    o = t.order('#1037')
    geo, fc = t.weather(mark)
    note = o['fields'].get('Melt Check Note', '') if o else ''
    t.r.check(S, 'the order is still saved, Cold Pack Review, and the note says why',
              ex['status'] == 'success' and o and o['fields'].get('Cold Pack') == 'Review' and 'Forecast Max C' not in o['fields']
              and note == 'Melt check failed: the Open-Meteo geocoding service answered HTTP 500 after 3 tries. Cold Pack set to Review, '
                          'check the forecast for Houston, TX, US by hand.' and len(geo) == 3 and not fc, note)
    packing = t.slack(mark, '/packing')
    text = packing[0]['body'].get('text', '') if packing else ''
    sl = t.run_row('wh-1037-create')
    t.r.check(S, 'packing team told to check by hand, Sync Log Partial with 1 error',
              len(packing) == 1 and text.startswith(':warning: *Check the cold pack by hand*') and sl
              and sl['fields'].get('Status') == 'Partial' and sl['fields'].get('Errors') == 1, text.splitlines()[0] if text else 'no alert')
    SENT['review_alert'] = text
    S = 'Weather back: the next orders/updated checks again'
    p2 = dict(p, updated_at=t.later(20), financial_status='paid')
    mark, *_ = t.deliver('orders/updated', p2, webhook_id='wh-1037-update')
    o = t.order('#1037')
    packing = t.slack(mark, '/packing')
    t.r.check(S, 'Review becomes Required (Houston 31.2 C) and the packing team is told',
              o and o['fields'].get('Cold Pack') == 'Required' and o['fields'].get('Forecast Max C') == 31.2 and len(packing) == 1
              and 'Cold pack needed' in packing[0]['body'].get('text', ''), f"{o['fields'].get('Cold Pack')}, {len(packing)} alert(s)" if o else 'no order')

    S = 'Forecast service down (503 three times)'
    t.mock.fail('open-meteo-forecast', status=503, times=3)
    p = t.mock.shopify.order_payload(1038, 'omar.haddad@example.com', [{'sku': 'DC-BAR-DARK-ORANGE', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Miami', 'FL', '33130'), customer={'name': 'Omar Haddad', 'id': 7700000000111})
    mark, status, _, _, ex = t.deliver('orders/create', p)
    o = t.order('#1038')
    geo, fc = t.weather(mark)
    t.r.check(S, 'Cold Pack Review after 1 geocoding call and 3 forecast tries',
              ex['status'] == 'success' and o and o['fields'].get('Cold Pack') == 'Review' and len(geo) == 1 and len(fc) == 3
              and 'forecast service answered HTTP 503' in o['fields'].get('Melt Check Note', ''), o['fields'].get('Melt Check Note') if o else '')

    S = 'Forecast with a missing day (null)'
    today = t.mock.clock.now().date()
    body = {'latitude': 21.3069, 'longitude': -157.8583, 'generationtime_ms': 0.2, 'utc_offset_seconds': -36000,
            'timezone': 'Pacific/Honolulu', 'timezone_abbreviation': 'HST', 'elevation': 5.0,
            'daily_units': {'time': 'iso8601', 'temperature_2m_max': '°C'},
            'daily': {'time': [(today + timedelta(days=i)).isoformat() for i in range(3)], 'temperature_2m_max': [26.0, None, 25.0]}}
    t.mock.fail('open-meteo-forecast', status=200, times=1, body=body)
    p = t.mock.shopify.order_payload(1057, 'sofia.marino@example.com', [{'sku': 'DC-BAR-DARK-ALMOND', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Honolulu', 'HI', '96813'), customer={'name': 'Sofia Marino', 'id': 7700000000116})
    mark, _, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1057-create')
    o = t.order('#1057')
    geo, fc = t.weather(mark)
    packing = t.slack(mark, '/packing')
    sl = t.run_row('wh-1057-create')
    t.r.check(S, 'a day without a value means Review, not a guess from the other days',
              ex['status'] == 'success' and o and o['fields'].get('Cold Pack') == 'Review' and 'Forecast Max C' not in o['fields']
              and o['fields'].get('Melt Check Note') == 'Melt check could not finish: the Open-Meteo forecast for Honolulu, Hawaii did not '
                                                       'include 3 days of maximum temperatures. Cold Pack set to Review, check the forecast by hand.'
              and len(geo) == 1 and len(fc) == 1 and len(packing) == 1
              and packing[0]['body'].get('text', '').startswith(':warning: *Check the cold pack by hand*')
              and sl and not subset(sl['fields'], {'Status': 'Partial', 'Errors': 1}),
              o['fields'].get('Melt Check Note', '')[:110] if o else 'no order')

    S = 'City the geocoder does not know'
    p = t.mock.shopify.order_payload(1039, 'ben.carter@example.com', [{'sku': 'DC-BAR-MILK-PEANUT', 'quantity': 1, 'price': 7.0}],
                                     shipping_address=addr('Springfield', 'IL', '62701'), customer={'name': 'Ben Carter', 'id': 7700000000115})
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1039')
    geo, fc = t.weather(mark)
    t.r.check(S, 'no results means Review (no guessing), no forecast call',
              o and o['fields'].get('Cold Pack') == 'Review' and len(geo) == 1 and not fc
              and 'found no place called "Springfield, IL" in US' in o['fields'].get('Melt Check Note', ''),
              o['fields'].get('Melt Check Note', '')[:110] if o else '')

    S = 'Pickup order without a shipping address'
    p = t.mock.shopify.order_payload(1040, 'irene.novak@example.com', [{'sku': 'DC-HOT-COCOA-250', 'quantity': 1, 'price': 9.0}],
                                     customer={'name': 'Irene Novak', 'id': 7700000000125})
    p.pop('shipping_address', None)
    mark, *_ = t.deliver('orders/create', p)
    o = t.order('#1040')
    geo, fc = t.weather(mark)
    t.r.check(S, 'Cold Pack Not Needed with a note, no weather calls, no address fields',
              o and o['fields'].get('Cold Pack') == 'Not Needed' and not geo and not fc and 'Shipping City' not in o['fields']
              and o['fields'].get('Melt Check Note') == 'No shipping address (pickup or digital order), no melt check.',
              o['fields'].get('Melt Check Note') if o else '')


def large_order(t):
    S = 'Large order: 23 lines'
    skus = json.loads((ROOT / 'base' / 'seed' / '_meta.json').read_text(encoding='utf-8'))['expected']['01-shopify-orders']['knownSkusOnShopify']
    items = [{'sku': skus[i % len(skus)], 'quantity': 1 + i % 3, 'price': 7.0} for i in range(23)]
    lines_before = t.at.count('Order Lines')
    p = t.mock.shopify.order_payload(1041, 'lucia.gomez@example.com', items, shipping_address=SEATTLE,
                                     customer={'name': 'Lucia Gomez', 'id': 7700000000112})
    mark, status, _, _, ex = t.deliver('orders/create', p)
    lw = t.writes(mark, 'Order Lines')
    o = t.order('#1041')
    gap, burst = t.pacing(mark)
    t.r.check(S, 'three line upserts of 10, 10 and 3 records; no request over 10 records',
              [e['record_count'] for e in lw] == [10, 10, 3] and all(e['record_count'] <= 10 for e in t.writes(mark)),
              str([e['record_count'] for e in lw]))
    t.r.check(S, 'all 23 lines saved and linked, no 429 from the rate limiter',
              o and len(o['fields'].get('Order Lines', [])) == 23 and t.at.count('Order Lines') == lines_before + 23
              and not [e for e in t.calls(mark) if e['status'] == 429] and burst <= 2,
              f"{len(o['fields'].get('Order Lines', [])) if o else 0} lines, busiest second {burst} requests, smallest gap {gap:.3f} s")


def concurrent(t):
    S = 'orders/create and orders/updated for one new order, delivered together'
    customers_before = t.at.count('Customers')
    items = [{'sku': 'DC-BAR-MILK-ESPRESSO', 'quantity': 2, 'price': 7.0}]
    p = t.mock.shopify.order_payload(1045, 'quick.buyer@example.com', items, shipping_address=addr('Tucson', 'AZ', '85701'),
                                     customer={'name': 'Quick Buyer'}, financial_status='pending')
    p2 = t.mock.shopify.order_payload(1045, 'quick.buyer@example.com', items, shipping_address=addr('Tucson', 'AZ', '85701'),
                                      customer={'name': 'Quick Buyer'}, financial_status='paid', tags='app-tagged',
                                      created_at=p['created_at'], updated_at=t.later(1))
    mark = t.mock.mark()
    s1, _ = t.run.post_shopify(HOOK, 'orders/create', p, webhook_id='wh-1045-create')
    s2, _ = t.run.post_shopify(HOOK, 'orders/updated', p2, webhook_id='wh-1045-update')
    runs = t.run.wait_all(timeout=240)
    o = t.order('#1045')
    upd = next((ex for ex in runs if t.run.ran(ex, 'Wait: Let Create Finish')), None)
    geo, _ = t.weather(mark)
    t.r.check(S, 'both answered 200 at once; the update waited 10 s, so the create was saved first',
              s1 == 200 and s2 == 200 and len(runs) == 2 and all(ex['status'] == 'success' for ex in runs) and upd is not None,
              f"{[ex['status'] for ex in runs]}, update waited: {upd is not None}")
    t.r.check(S, 'one order with the newer state, one customer, one melt check, one packing alert',
              o and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001045'})) == 1
              and not subset(o['fields'], {'Financial Status': 'Paid', 'Shopify Tags': 'app-tagged', 'Cold Pack': 'Required'})
              and t.at.count('Customers') == customers_before + 1 and len(geo) == 1 and len(t.slack(mark, '/packing')) == 1,
              f"{o['fields'].get('Financial Status')}, {len(geo)} melt check(s), {len(t.slack(mark, '/packing'))} packing alert(s)" if o else 'no order')

    S = 'Two new orders processed at the same moment'
    mark = t.mock.mark()
    for number, ship in ((1046, SEATTLE), (1047, addr('Boston', 'MA', '02108'))):
        p = t.mock.shopify.order_payload(number, f'buyer{number}@example.com', [{'sku': 'DC-BAR-DARK-RASPBERRY', 'quantity': 1, 'price': 7.5}],
                                         shipping_address=ship, customer={'name': f'Buyer {number}'})
        t.run.post_shopify(HOOK, 'orders/create', p)
    runs = t.run.wait_all(timeout=240)
    gap, burst = t.pacing(mark)
    calls = t.calls(mark)
    t.r.check(S, 'with the real limiter on, the two runs together stay under 5 requests per second (no 429)',
              len(runs) == 2 and all(ex['status'] == 'success' for ex in runs) and not [e for e in calls if e['status'] == 429]
              and burst <= 4 and t.order('#1046') and t.order('#1047'),
              f'{len(calls)} Airtable requests, busiest second {burst}')


def rate_limit(t):
    S = 'Airtable rate limit (429) while saving the order'
    customers_before = t.at.count('Customers')
    t.mock.fail('airtable', status=429, times=3, match={'table': 'Orders', 'operation': 'upsert'})
    p = t.mock.shopify.order_payload(1042, 'new.vegas@example.com', [{'sku': 'DC-BAR-DARK-MINT', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Las Vegas', 'NV', '89101'), customer={'name': 'Vegas Buyer'})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1042-create')
    calls = t.calls(mark)
    limited = [e for e in calls if e['status'] == 429]
    after = calls[calls.index(limited[-1]) + 1] if limited and calls.index(limited[-1]) + 1 < len(calls) else None
    pause = (ts(after) - ts(limited[-1])).total_seconds() if after else 0
    t.r.check(S, 'after 3 tries the run waits 31 to 60 s (the base is blocked for 30), then starts the webhook again',
              ex['status'] == 'success' and len(limited) == 3 and all(e.get('injected') for e in limited)
              and t.run.runs_of(ex, 'Code: Start Processing') == 2 and t.run.runs_of(ex, 'Wait: Airtable Cooldown') == 1
              and 30.5 <= pause <= 62, f'{len(limited)} x 429, paused {pause:.1f} s, Start Processing ran {t.run.runs_of(ex, "Code: Start Processing")} times')
    geo, _ = t.weather(mark)
    o = t.order('#1042')
    t.r.check(S, 'one order, one customer, one packing alert and one melt check in total',
              o and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001042'})) == 1
              and t.at.count('Customers') == customers_before + 1 and len(t.slack(mark, '/packing')) == 1 and len(geo) == 1
              and o['fields'].get('Cold Pack') == 'Required',
              f"{len(t.slack(mark, '/packing'))} packing alert(s), {len(geo)} geocoding call(s)")
    wl = t.webhook_rows('wh-1042-create')
    sl = t.run_row('wh-1042-create')
    t.r.check(S, 'logged as Processed, and the run log still counts the customer it created on the first attempt',
              len(wl) == 1 and wl[0]['fields'].get('Status') == 'Processed' and 'Customer created (Vegas Buyer)' in wl[0]['fields'].get('Notes', '')
              and sl and sl['fields'].get('Status') == 'Success' and sl['fields'].get('Items Changed') == 3,
              wl[0]['fields'].get('Notes', '')[:100] if wl else 'no row')


def cooldown_race(t):
    S = 'A newer orders/updated is saved while the create waits out a cooldown'
    customers_before = t.at.count('Customers')
    items = [{'sku': 'DC-BAR-DARK-ORANGE', 'quantity': 1, 'price': 7.5}]
    p = t.mock.shopify.order_payload(1059, 'race.buyer@example.com', items, shipping_address=addr('Phoenix', 'AZ', '85004'),
                                     customer={'name': 'Race Buyer'}, financial_status='pending')
    p2 = t.mock.shopify.order_payload(1059, 'race.buyer@example.com', items, shipping_address=addr('Phoenix', 'AZ', '85004'),
                                      customer={'name': 'Race Buyer'}, financial_status='paid', tags='rush',
                                      created_at=p['created_at'], updated_at=t.later(1))
    t.mock.fail('airtable', status=429, times=3, match={'table': 'Orders', 'operation': 'upsert'})
    mark = t.mock.mark()
    s1, _ = t.run.post_shopify(HOOK, 'orders/create', p, webhook_id='wh-1059-create')
    end = time.time() + 90
    while time.time() < end and len([e for e in t.calls(mark, table='Orders', operation='upsert') if e['status'] == 429]) < 3:
        time.sleep(0.3)
    s2, _ = t.run.post_shopify(HOOK, 'orders/updated', p2, webhook_id='wh-1059-update')   # the create is in its cooldown now
    runs = t.run.wait_all(timeout=240)
    create = next((ex for ex in runs if t.run.ran(ex, 'Wait: Airtable Cooldown')), None)
    o = t.order('#1059')
    ups = t.calls(mark, table='Orders', operation='upsert')
    t.r.check(S, 'the create restarts after its cooldown and finds the newer version: it writes nothing over it',
              s1 == 200 and s2 == 200 and len(runs) == 2 and all(ex['status'] == 'success' for ex in runs) and create is not None
              and t.run.runs_of(create, 'Code: Start Processing') == 2 and t.run.runs_of(create, 'Code: Build Order') == 1
              and [e['status'] for e in ups] == [429, 429, 429, 200]
              and o is not None and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001059'})) == 1
              and not subset(o['fields'], {'Financial Status': 'Paid', 'Shopify Tags': 'rush', 'Cold Pack': 'Required'})
              and same_time(o['fields'].get('Shopify Updated At'), p2['updated_at']),
              f"{[e['status'] for e in ups]}, order {o['fields'].get('Financial Status') if o else None}")
    wl_create, wl_update = t.webhook_rows('wh-1059-create'), t.webhook_rows('wh-1059-update')
    notes = wl_create[0]['fields'].get('Notes', '') if wl_create else ''
    t.r.check(S, 'the late create is logged as Ignored (with what its first attempt did), the update as Processed',
              o is not None and len(wl_create) == 1 and wl_create[0]['fields'].get('Status') == 'Ignored'
              and 'older than the version already in Airtable' in notes
              and 'Before that, this webhook created the customer Race Buyer.' in notes
              and wl_create[0]['fields'].get('Order') == [o['id']]
              and len(wl_update) == 1 and wl_update[0]['fields'].get('Status') == 'Processed',
              notes[:140] or 'no row')
    geo, _ = t.weather(mark)
    lines = t.at.find('Order Lines', **{'Line Item ID': line_id(1059, 1)})
    t.r.check(S, 'one customer, one line, one packing alert (the late create sends none)',
              t.at.count('Customers') == customers_before + 1 and len(lines) == 1 and len(t.slack(mark, '/packing')) == 1
              and len(geo) == 2, f"{len(t.slack(mark, '/packing'))} packing alert(s), {len(geo)} geocoding call(s), {len(lines)} line(s)")


def airtable_rejects(t):
    S = 'Airtable rejects the lines (422) after the order is saved'
    body = {'error': {'type': 'INVALID_VALUE_FOR_COLUMN', 'message': 'Field "Quantity" cannot accept the provided value'}}
    t.mock.fail('airtable', status=422, times=3, body=body, match={'table': 'Order Lines'})
    p = t.mock.shopify.order_payload(1043, 'rosa.delgado@example.com', [{'sku': 'DC-BAR-WHITE-COOKIE', 'quantity': 2, 'price': 7.0}],
                                     shipping_address=addr('Tucson', 'AZ', '85701'), customer={'name': 'Rosa Delgado', 'id': 7700000000123})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1043-create')
    wl = t.webhook_rows('wh-1043-create')
    lw = t.calls(mark, table='Order Lines')
    t.r.check(S, 'no cooldown for a 422: the run stops after 3 tries and the webhook is marked Failed',
              ex['status'] == 'error' and t.run.last_node(ex) == 'Stop: Airtable Failed' and len(lw) == 3
              and t.run.runs_of(ex, 'Wait: Airtable Cooldown') == 0 and len(wl) == 1 and wl[0]['fields'].get('Status') == 'Failed'
              and 'INVALID_VALUE_FOR_COLUMN' in wl[0]['fields'].get('Notes', ''),
              f"ended at {t.run.last_node(ex)}, Webhook Log {wl[0]['fields'].get('Status') if wl else 'none'}")
    alert = alert_for(t.run, ex)
    t.r.check(S, 'the error workflow posts the reason to Slack',
              bool(alert) and '*Stop: Airtable Failed*' in alert and 'Order #1043 from orders/create was not fully saved after 1 attempt' in alert
              and 'Airtable answered 422 INVALID_VALUE_FOR_COLUMN (Field "Quantity" cannot accept the provided value) at step Upsert Order Lines (batch 1 of 1)' in alert,
              (alert or 'no alert').splitlines()[1] if alert else 'no alert')
    SENT['airtable_alert'] = alert
    packing = t.slack(mark, '/packing')
    t.r.check(S, 'the packing alert still went out (alerts are sent before the lines)',
              len(packing) == 1 and t.order('#1043')['fields'].get('Cold Pack') == 'Required', f'{len(packing)} packing alert(s)')
    S = 'The next orders/updated repairs it'
    mark, status, _, _, ex = t.deliver('orders/updated', dict(p, updated_at=t.later(30)), webhook_id='wh-1043-update')
    o = t.order('#1043')
    t.r.check(S, 'lines written, no second packing alert, no second melt check',
              ex['status'] == 'success' and o and len(o['fields'].get('Order Lines', [])) == 1 and not t.slack(mark, '/packing')
              and not t.weather(mark)[0], f"{len(o['fields'].get('Order Lines', [])) if o else 0} line(s)")


def duplicate_rows(t):
    S = 'Someone duplicated an Orders row in Airtable'
    for _ in range(2):
        t.at.insert('Orders', {'Order Number': '#1050', 'Shopify Order ID': '5200000001050', 'Cold Pack': 'Not Needed',
                               'Shipping City': 'Seattle', 'Shipping Region': 'WA', 'Shipping Country': 'US'})
    p = t.mock.shopify.order_payload(1050, 'mila.petrova@example.com', [{'sku': 'DC-BAR-MILK-LAVENDER', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=SEATTLE, customer={'name': 'Mila Petrova', 'id': 7700000000129})
    mark, status, _, _, ex = t.deliver('orders/updated', p)
    up = t.calls(mark, table='Orders', operation='upsert')
    EXPECTED_REJECTIONS.update(e['seq'] for e in up)
    alert = alert_for(t.run, ex)
    t.r.check(S, "Airtable's 422 for two matching rows stops the run, and the alert says which rows to merge",
              ex['status'] == 'error' and len(up) == 3 and all(e.get('error_type') == 'INVALID_VALUE_FOR_COLUMN' for e in up)
              and bool(alert) and 'Cannot update more than one record for fields to merge on' in alert
              and 'Two Orders records have Shopify Order ID 5200000001050, merge them in Airtable.' in alert
              and len(t.at.find('Orders', **{'Shopify Order ID': '5200000001050'})) == 2,
              (alert or 'no alert').splitlines()[1][:160] if alert else 'no alert')


def slack_down(t):
    S = 'Packing Slack webhook down (500 three times)'
    t.mock.fail('slack', status=500, times=3, match={'path_contains': '/packing'})
    p = t.mock.shopify.order_payload(1044, 'felix.osei@example.com', [{'sku': 'DC-BAR-WHITE-STRAWBERRY', 'quantity': 1, 'price': 7.5}],
                                     shipping_address=addr('Orlando', 'FL', '32801'), customer={'name': 'Felix Osei', 'id': 7700000000126})
    mark, status, _, _, ex = t.deliver('orders/create', p, webhook_id='wh-1044-create')
    o = t.order('#1044')
    wl = t.webhook_rows('wh-1044-create')
    sl = t.run_row('wh-1044-create')
    t.r.check(S, 'order and lines saved, Webhook Log Processed, Sync Log Partial',
              o and o['fields'].get('Cold Pack') == 'Required' and len(o['fields'].get('Order Lines', [])) == 1
              and len(wl) == 1 and wl[0]['fields'].get('Status') == 'Processed' and sl and sl['fields'].get('Status') == 'Partial'
              and len(t.slack(mark, '/packing')) == 3, f"{len(t.slack(mark, '/packing'))} tries, Sync Log {sl['fields'].get('Status') if sl else None}")
    alert = alert_for(t.run, ex)
    t.r.check(S, 'the run ends failed so the error workflow tells ops',
              ex['status'] == 'error' and t.run.last_node(ex) == 'Stop: Slack Alert Failed' and bool(alert)
              and 'Order #1044 is saved in Airtable, but the packing alert (Slack answered HTTP 500 after 3 tries) could not be posted to Slack' in alert,
              (alert or 'no alert').splitlines()[1] if alert else 'no alert')
    SENT['slack_alert'] = alert


def later_changes(t):
    S = 'Cancelled order'
    p = t.mock.shopify.order_payload(1031, 'elena.rossi@example.com', [{'sku': 'DC-BOX-PRALINE-12', 'quantity': 1, 'price': 24.0}],
                                     shipping_address=SEATTLE, customer={'name': 'Elena Rossi', 'id': 7700000000108},
                                     note='Leave at the door', note_attributes=[{'name': 'Gift Message', 'value': 'Happy birthday, Sam!'}],
                                     financial_status='voided', cancelled_at=t.later(25), cancel_reason='customer',
                                     created_at=t.created[1031], updated_at=t.later(25))
    mark, *_ = t.deliver('orders/updated', p)
    o = t.order('#1031')
    t.r.check(S, 'Order Status Cancelled, Cancelled At and Voided are written, nothing else happens',
              o and not subset(o['fields'], {'Order Status': 'Cancelled', 'Financial Status': 'Voided', 'Cold Pack': 'Not Needed'})
              and o['fields'].get('Cancelled At') and not t.weather(mark)[0] and not t.team_slack(mark),
              f"{o['fields'].get('Order Status')}, {o['fields'].get('Financial Status')}, cancelled {o['fields'].get('Cancelled At')}" if o else '')
    S = 'Shipping address changed to a cold city'
    p = t.mock.shopify.order_payload(1032, 'aisha.khan@example.com', [{'sku': 'DC-BAR-RUBY-CHERRY', 'quantity': 2, 'price': 7.5}],
                                     shipping_address=SEATTLE, customer={'name': 'Aisha Khan', 'id': 7700000000114},
                                     note='For Grandma, with love', tags='gift, vip', created_at=t.created[1032], updated_at=t.later(25))
    mark, *_ = t.deliver('orders/updated', p)
    o = t.order('#1032')
    packing = t.slack(mark, '/packing')
    t.r.check(S, 'the melt check runs again: Not Needed, and the packing team hears the cold pack is no longer needed',
              o and o['fields'].get('Shipping City') == 'Seattle' and o['fields'].get('Cold Pack') == 'Not Needed' and len(packing) == 1
              and packing[0]['body'].get('text', '').startswith('*Cold pack no longer needed* for order'),
              packing[0]['body'].get('text', '').splitlines()[0] if packing else 'no alert')


def security(t):
    S = 'Forged and unsigned webhooks'
    orders_before = t.at.count('Orders')
    for label, kw in (('wrong signature', {'signature': 'bad'}), ('no signature', {'signature': ''}),
                      ('signed with another secret', {'secret': 'not-the-shop-secret'})):
        p = t.mock.shopify.order_payload(1099, 'forged@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 50, 'price': 0.01}],
                                         shipping_address=PHOENIX)
        mark, status, body, _, ex = t.deliver('orders/create', p, **kw)
        nothing = not t.calls(mark) and not t.calls(mark, 'open-meteo-geocoding') and not t.team_slack(mark)
        alert = alert_for(t.run, ex)
        t.r.check(S, f'{label}: 401, nothing read or written, run failed with an alert',
                  status == 401 and nothing and ex['status'] == 'error' and t.run.last_node(ex) == 'Stop: Bad Signature'
                  and bool(alert) and 'X-Shopify-Hmac-Sha256 signature' in alert, f'{status} {body!r}, ended at {t.run.last_node(ex)}')
    t.r.check(S, 'the forged order never reached Airtable', t.at.count('Orders') == orders_before and t.order('#1099') is None,
              f"{t.at.count('Orders') - orders_before} new orders")
    # Headers are not signed. A forged call puts Slack markup in them to reach the ops channel through the alert.
    p = t.mock.shopify.order_payload(1099, 'forged@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}])
    mark, status, _, _, ex = t.deliver('orders/create <!channel> <https://evil.example/login|Shopify login>', p,
                                       webhook_id='<!here> & <https://evil.example|x>', signature='bad')
    alert = alert_for(t.run, ex)
    t.r.check(S, 'Slack markup in forged headers is neutralised before the alert: no ping, no link',
              status == 401 and bool(alert) and not any(ch in alert for ch in '<>&')
              and 'signature (topic orders/create???channel???https?//evil.example/login?Shopify?log, webhook id ??here?????https?//evil.example?x?)' in alert,
              (alert or 'no alert').splitlines()[1][:150] if alert else 'no alert')
    mark = t.mock.mark()
    status, body = t.run.post(HOOK, raw=b'', headers={'Content-Type': 'application/json'})
    ex = t.run.wait(timeout=120)
    t.r.check(S, 'an empty POST: 401, nothing read', status == 401 and not t.calls(mark) and t.run.last_node(ex) == 'Stop: Bad Signature',
              f'{status} {body!r}')
    runs_before = len(t.run.executions())
    status, body = t.run.post(HOOK, raw=b'not json at all', headers={'Content-Type': 'application/json'}, count=False)
    time.sleep(3)
    t.r.check(S, 'a body that is not JSON is refused by n8n itself (422) before the workflow runs',
              status == 422 and len(t.run.executions()) == runs_before and not t.calls(mark), f'{status} {body[:50]!r}')
    S = 'Other topics and bodies'
    p = t.mock.shopify.order_payload(1098, 'paid@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}])
    mark, status, body, _, ex = t.deliver('orders/paid', p)
    t.r.check(S, 'a signed orders/paid webhook: 200 "ignored", no Airtable call',
              status == 200 and body == 'ignored' and not t.calls(mark) and ex['status'] == 'success', f'{status} {body!r}')
    mark, status, body, _, ex = t.deliver('orders/create', {'id': 5200000009999})
    alert = alert_for(t.run, ex)
    t.r.check(S, 'a signed body that is not an order: 200 so Shopify does not retry, run failed with an alert',
              status == 200 and not t.calls(mark) and t.run.last_node(ex) == 'Stop: Not An Order' and bool(alert)
              and 'did not contain an order with line items' in alert, f'{status}, ended at {t.run.last_node(ex)}')


def mock_hygiene(t, mark0):
    S = 'Whole run'
    calls = t.calls(mark0)
    rejected = [e for e in calls if e['status'] != 200 and not e.get('injected') and e['seq'] not in EXPECTED_REJECTIONS]
    t.r.check(S, 'every other Airtable request was accepted: no 4xx, no 429 (injected failures and the duplicate row 422s aside)',
              not rejected, ', '.join(f"{e['method']} {e.get('table')} {e['status']} {e.get('error_type')}" for e in rejected[:5])
              or f'{len(calls)} requests, {sum(1 for e in calls if e.get("injected"))} injected failures, {len(EXPECTED_REJECTIONS)} expected 422s')
    t.r.check(S, 'no request over 10 records, no request to Shopify or to an unmocked URL',
              all(e.get('record_count', 0) <= 10 for e in calls) and not t.calls(mark0, 'shopify') and not t.calls(mark0, 'unknown'),
              f"largest write {max((e.get('record_count', 0) for e in calls), default=0)} records")


def no_secret(mock, n8n, r):
    S = 'SHOPIFY_WEBHOOK_SECRET not set'
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, SHOPIFY_WEBHOOK_SECRET=''))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        p = mock.shopify.order_payload(1097, 'nosecret@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}])
        mark = mock.mark()
        status, body = run.post_shopify(HOOK, 'orders/create', p)
        ex = run.wait(timeout=120)
        r.check(S, 'fail closed: even a correctly signed webhook gets 500 and nothing is read or written',
                status == 500 and not mock.log('airtable', since=mark) and t_last(run, ex) == 'Stop: Not Configured',
                f'{status} {body!r}, ended at {t_last(run, ex)}')


def custom_settings(mock, n8n, r):
    S = 'Settings from $env: MELT_TEMP_C 35, MELT_FORECAST_DAYS 2, no packing webhook'
    env = mock_env(mock, MELT_TEMP_C='35', MELT_FORECAST_DAYS='2', SLACK_PACKING_WEBHOOK_URL='')
    wf = prepare_workflow(WORKFLOW, mock, SLUG, env)
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        t = T(mock, run, r)
        p = mock.shopify.order_payload(1048, 'tucson.buyer@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}],
                                       shipping_address=addr('Tucson', 'AZ', '85701'), customer={'name': 'Tucson Buyer'})
        mark, *_ = t.deliver('orders/create', p)
        o = t.order('#1048')
        geo, fc = t.weather(mark)
        t.r.check(S, 'Tucson at 34.6 C is under the new 35 C limit, and only 2 days are asked for',
                  o and o['fields'].get('Cold Pack') == 'Not Needed' and o['fields'].get('Forecast Max C') == 34.6
                  and fc and fc[0]['params'].get('forecast_days') == '2'
                  and o['fields'].get('Melt Check Note') == 'Forecast max 34.6 C over the next 2 days in Tucson, Arizona (cold pack from 35 C).',
                  o['fields'].get('Melt Check Note') if o else 'no order')
        p = mock.shopify.order_payload(1049, 'phoenix.buyer@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}],
                                       shipping_address=PHOENIX, customer={'name': 'Phoenix Buyer'})
        mark, *_ = t.deliver('orders/create', p)
        o = t.order('#1049')
        t.r.check(S, 'Phoenix at 37.1 C is over it, and the packing alert falls back to SLACK_WEBHOOK_URL',
                  o and o['fields'].get('Cold Pack') == 'Required' and len(t.slack(mark, '/alerts')) == 1 and not t.slack(mark, '/packing')
                  and 'Cold pack needed' in t.slack(mark, '/alerts')[0]['body'].get('text', ''),
                  f"{o['fields'].get('Cold Pack') if o else None}, alerts {len(t.slack(mark, '/alerts'))}, packing {len(t.slack(mark, '/packing'))}")


def wrong_airtable_token(mock, n8n, r):
    S = 'Wrong AIRTABLE_TOKEN'
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, AIRTABLE_TOKEN='patWrongToken.0000'))
    with n8n.temporary(wf, SLUG, mock=mock) as run:
        t = T(mock, run, r)
        p = mock.shopify.order_payload(1051, 'token.test@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 1, 'price': 7.5}],
                                       shipping_address=SEATTLE, customer={'name': 'Token Test'})
        mark, status, _, _, ex = t.deliver('orders/create', p)
        calls = t.calls(mark)
        alert = alert_for(run, ex)
        r.check(S, 'Shopify still gets its 200; the run stops at the first lookup (401, no cooldown) and the alert names the cause',
                status == 200 and ex['status'] == 'error' and run.last_node(ex) == 'Stop: Airtable Failed'
                and [e['status'] for e in calls] == [401] * 6 and run.runs_of(ex, 'Wait: Airtable Cooldown') == 0
                and bool(alert) and 'Airtable answered 401 AUTHENTICATION_REQUIRED' in alert and 'at step Find Webhook' in alert,
                (alert or 'no alert').splitlines()[1][:150] if alert else 'no alert')


def t_last(run, ex):
    return run.last_node(ex)


def print_sent():
    print('\n--- What the workflow sent (first scenario and the alerts) ---')
    for key in ('customer_upsert', 'orders_upsert', 'lines_upsert', 'draft_upsert'):
        if SENT.get(key):
            print(f'\n{key}:\n' + json.dumps(SENT[key], indent=1))
    for key in ('geocode', 'forecast'):
        print(f'\n{key} query: {SENT.get(key)}')
    for key in ('packing_alert', 'review_alert', 'sku_alert', 'airtable_alert', 'slack_alert', 'webhook_notes'):
        print(f'\n{key}:\n{SENT.get(key)}')


def main():
    mock = MockServer(port=PORT).start()
    n8n = N8n()
    left = n8n.cleanup(SLUG)
    r = Report()
    try:
        wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
        with n8n.temporary(wf, SLUG, mock=mock) as run:
            t = T(mock, run, r)
            mark0 = mock.mark()
            scenarios = (new_order_hot_city, repeat_delivery, update_refund, stale_update, customer_matching, unknown_sku,
                         many_unknown_skus, gift_orders, thresholds, weather_failures, large_order, concurrent, rate_limit,
                         cooldown_race, airtable_rejects, duplicate_rows, slack_down, later_changes, security)
            only = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--only=')]
            for scenario in scenarios:
                if only and scenario.__name__ not in only[0].split(','):
                    continue
                try:
                    scenario(t)
                except Exception as e:  # a crash is a failed check, and the other scenarios still run
                    import traceback
                    traceback.print_exc()
                    r.check(scenario.__name__, 'scenario ran to the end', False, repr(e)[:300])
                mock.clear_failures()
            mock_hygiene(t, mark0)
            if '--alerts' in sys.argv:
                print('\n\n'.join(run.error_alerts()))
        if not only:
            no_secret(mock, n8n, r)
            custom_settings(mock, n8n, r)
            wrong_airtable_token(mock, n8n, r)
        after = n8n.workflows_named(n8n.test_name(SLUG))
        r.check('Cleanup', 'every temporary workflow was deleted', after == [], f'{len(after)} left, {left} leftovers removed before the run')
    finally:
        mock.stop()
    ok = r.print()
    if '--sent' in sys.argv:
        print_sent()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
