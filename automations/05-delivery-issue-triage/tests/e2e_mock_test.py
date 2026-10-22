"""End to end test for 05 Delivery Issue Triage against the shared MOCK APIs.

No real Airtable base, Shopify store, Claude account, Postmark server or Slack workspace is used. The script:
  1. starts the shared mocks (tests/mocks) on port 8795 with the seeded Demo Chocolate Co. base, shifted to today,
     a live clock and the Airtable rate limiter switched on (5 requests per second, then 30 seconds blocked),
  2. swaps in a Shopify mock that also has DraftOrder.ready (see ShopifyWithReady below),
  3. imports a temporary copy of the workflow into your n8n, plus an ACTIVE copy of shared/error-alert-slack.json
     as its error workflow, both named "TEST delivery-issue-triage (temporary)",
  4. posts support messages the way a help form or an email parser would and plays every rule: melted claims that
     are approved (warm forecast without a cold pack, late delivery), every reason a claim goes to a person, damaged
     and wrong item photo requests, tracking for a parcel in transit, allergy and safety questions, repeat messages,
     forged requests and outages of Claude, Shopify, Postmark, Slack and Airtable,
  5. checks the exact records, emails, draft orders and Slack messages, prints PASS or FAIL per check,
     then deletes both temporary workflows.

The mock Claude does not think: it returns scripted tool calls, but it rejects any request the real Messages API
would reject. If ANTHROPIC_API_KEY is available (environment or N8N_ENV_FILE), the captured request is also sent
to Anthropic's free token counting endpoint, which validates its shape without generating anything.

Requirements: Python 3.10+, a running n8n with the public API enabled.
Environment:
  N8N_URL       default http://localhost:5678
  N8N_API_KEY   your n8n API key (or N8N_ENV_FILE pointing at a .env file that has it)
  MOCK_HOST     how n8n reaches this machine; default host.docker.internal (n8n in Docker).
                Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python automations/05-delivery-issue-triage/tests/e2e_mock_test.py   (about 5 minutes)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.mocks import MockServer, AIRTABLE_TOKEN  # noqa: E402
from tests.mocks import graphql as G  # noqa: E402
from tests.mocks import shopify as shopify_mod  # noqa: E402
from tests.mocks.core import parse_iso  # noqa: E402
from tests.mocks.n8n import N8n, Report, load_env_file, mock_env, prepare_workflow  # noqa: E402

SLUG, PORT = 'delivery-issue-triage', 8795
WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow' / 'delivery-issue-triage.json'
SECRET = 'mock-support-form-secret'
PATH = 'support-message'
BASE = 'appDemoChocolate1'
GAP = 1.3          # seconds between messages: back to back runs must stay under Airtable's 5 requests per second
ALL_SERVICES = ('airtable', 'anthropic', 'shopify', 'postmark', 'slack')


# ---------------------------------------------------------------------------------------------- DraftOrder.ready
# The real DraftOrder has `ready` ("Whether the draft order is ready and can be completed. Draft orders might have
# asynchronous operations that can take time to finish."). The shared mock's schema does not have it yet, so this
# subclass adds the field and lets a test make the next drafts "calculating" for a few seconds. Completing a draft
# that is not ready gives a user error; Shopify does not document that message, so the text below is our own.
READY_SDL = shopify_mod.SDL.replace('type DraftOrder implements Node {\n  id: ID! name: String!',
                                    'type DraftOrder implements Node {\n  id: ID! name: String! ready: Boolean!')
assert READY_SDL != shopify_mod.SDL, 'the DraftOrder type in tests/mocks/shopify.py changed; update ShopifyWithReady'
READY_SCHEMA = G.Schema(READY_SDL)


class ShopifyWithReady(shopify_mod.ShopifyMock):
    def reset(self):
        super().reset()
        self.ready_at = {}          # draft gid -> time.monotonic() when it becomes ready
        self._calculating = []      # seconds of calculation for the next drafts created

    def calculate_next(self, seconds, count=1):
        """The next `count` drafts created stay ready: false for `seconds`."""
        self._calculating += [seconds] * count

    def handle(self, req, entry):
        # MockServer.dispatch calls handle() while holding its lock, so requests never overlap here and the module's
        # schema can be swapped for this one request without touching the shared file.
        saved = shopify_mod.SCHEMA
        shopify_mod.SCHEMA = READY_SCHEMA
        try:
            return super().handle(req, entry)
        finally:
            shopify_mod.SCHEMA = saved

    def resolvers(self):
        r = super().resolvers()
        r['DraftOrder.ready'] = lambda p, a, i: self.is_ready(p['id'])
        return r

    def is_ready(self, gid):
        return time.monotonic() >= self.ready_at.get(gid, 0)

    def _draft_create(self, parent, args, info):
        result = super()._draft_create(parent, args, info)
        draft = result.get('draftOrder')
        if draft and self._calculating:
            self.ready_at[draft['id']] = time.monotonic() + self._calculating.pop(0)
        return result

    def _draft_complete(self, parent, args, info):
        if args['id'] in self.draft_orders and not self.is_ready(args['id']):
            return {'draftOrder': None, 'userErrors': [{'field': ['id'], 'message': 'The draft order is still being calculated'}]}
        return super()._draft_complete(parent, args, info)


report = Report()
check = report.check


def days_between(a, b):
    return (date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days


MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']


def month_day(iso):
    d = date.fromisoformat(iso[:10])
    return f'{MONTHS[d.month - 1]} {d.day}'


class Sent:
    """One posted message: the HTTP answer, the finished n8n run and the mock requests it caused."""

    def __init__(self, mock, status, text, ex, mark, answered_at=None):
        self.mock, self.status, self.ex, self.mark, self.answered_at = mock, status, ex, mark, answered_at
        try:
            self.body = json.loads(text) if text else {}
        except ValueError:
            self.body = {'raw': text}

    def log(self, service, **match):
        return self.mock.log(service, since=self.mark, **match)

    def count(self, service, **match):
        return len(self.log(service, **match))

    def touched(self):
        return {s: self.count(s) for s in ALL_SERVICES if self.count(s)}

    def shopify_ops(self):
        return [e.get('operation') for e in self.log('shopify')]


def lines_of(f):
    return [ln.split(' ', 1)[1] for ln in f.get('Action Log', '').split('\n')]


def main():
    mock = MockServer(port=PORT)
    shop = ShopifyWithReady(mock)
    mock.services[mock.services.index(mock.shopify)] = shop
    mock.shopify = shop
    mock.reset()                     # reloads the base and builds the store in the new Shopify mock
    mock.start()
    n8n = N8n()
    n8n.cleanup(SLUG)
    seed = mock.seed_date
    today = mock.clock.today().isoformat()
    T = lambda tid: mock.airtable.by_primary('Tickets', tid)
    ref = lambda table, r: mock.airtable.ref(table, r)
    run_status = []                  # (message id, expected run status, actual run status) for every message sent

    def next_ticket():
        nums = [int(r['fields']['Ticket ID'][2:]) for r in mock.airtable.records('Tickets')]
        return f'T-{max(nums) + 1:04d}'

    def claude(match, issue, order='', sentiment='negative', safety=False, summary='Customer reports a delivery problem.'):
        mock.anthropic.on(match, tool_input={'issue_type': issue, 'order_number': order, 'sentiment': sentiment,
                                             'safety_concern': safety, 'summary': summary}, times=1)

    def message(key, email, text, name='', subject='', order=None, channel=None, photos=None, message_id=None):
        p = {'message_id': message_id or f'<e2e-{key}@help.example.com>', 'email': email, 'name': name, 'message': text}
        if subject:
            p['subject'] = subject
        if order is not None:
            p['order_number'] = order
        if channel:
            p['channel'] = channel
        if photos is not None:
            p['photos'] = photos
        return p

    def make_late(order_ref, delivered_days_ago=1, late_days=2):
        """Test setup: the order was delivered `delivered_days_ago` days ago, `late_days` after Expected Delivery."""
        delivered = (date.fromisoformat(today) - timedelta(days=delivered_days_ago))
        mock.airtable.update('Orders', ref('Orders', order_ref), {
            'Delivered At': delivered.isoformat() + 'T17:45:00.000Z',
            'Expected Delivery': (delivered - timedelta(days=late_days)).isoformat()})
        return delivered.isoformat()

    try:
        wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
        with n8n.temporary(wf, SLUG, mock=mock) as run:

            def send(payload, secret=SECRET, raw=None, expect='success', after_post=None):
                time.sleep(GAP)
                mark = mock.mark()
                headers = {} if secret is None else {'X-Support-Secret': secret}
                status, text = run.post(PATH, payload, headers=headers, raw=raw)
                answered_at = datetime.now(timezone.utc)
                if after_post:
                    after_post()
                ex = run.wait(timeout=180)
                run_status.append(((payload or {}).get('message_id', 'raw body'), expect, ex['status']))
                return Sent(mock, status, text, ex, mark, answered_at)

            tickets_before = mock.airtable.count('Tickets')

            # ---------------------------------------------------------------- 1. security and validation
            s = send(message('nosecret', 'tom.becker@example.com', 'My bars arrived melted, order #1012.'), secret=None)
            check('Forged request, no secret', 'refused with 401, nothing called',
                  s.status == 401 and s.body.get('error') == 'Missing or wrong X-Support-Secret header' and not s.touched(),
                  f'{s.status} {s.body}')
            s = send(message('badsecret', 'tom.becker@example.com', 'My bars arrived melted, order #1012.'), secret='not-the-secret')
            check('Forged request, wrong secret', 'refused with 401, nothing called',
                  s.status == 401 and s.body.get('error') == 'Missing or wrong X-Support-Secret header' and not s.touched(), f'{s.status}')
            s = send({'email': 'not-an-email', 'message': 'Hello'})
            check('Invalid message', 'missing message_id and a bad email give 400 with both reasons, nothing saved',
                  s.status == 400 and s.body.get('error') == 'message_id is required; email is missing or not a valid address'
                  and not s.touched(), f'{s.status} {s.body.get("error")}')
            s = send(message('httpphoto', 'victor.hale@example.com', 'Box was crushed', photos=[{'url': 'http://example.com/box.jpg'}]))
            check('Invalid message', 'a photo that is not https gives 400', s.status == 400 and s.body.get('error') == 'every photo needs an https url'
                  and not s.touched(), f'{s.status} {s.body.get("error")}')
            s = send(None, raw=json.dumps([message('array', 'tom.becker@example.com', 'Melted')]).encode())
            check('Invalid message', 'a JSON list instead of an object gives 400', s.status == 400
                  and s.body.get('error') == 'Send the message as a JSON object' and not s.touched(), f'{s.status} {s.body.get("error")}')
            s = send(message('sms', 'tom.becker@example.com', 'Melted', channel='sms'))
            check('Invalid message', 'an unknown channel gives 400', s.status == 400 and s.body.get('error') == 'channel must be help_form or email'
                  and not s.touched(), f'{s.status} {s.body.get("error")}')
            check('Invalid message', 'no ticket was created by any refused request', mock.airtable.count('Tickets') == tickets_before,
                  f'{mock.airtable.count("Tickets")} tickets')

            # ---------------------------------------------------------------- 2. melted, warm forecast and no cold pack
            claude('my Houston order 1012 arrived completely melted', 'melted', '#1012', summary='Order 1012 arrived melted in Houston.')
            p = message('tom-1', 'Tom.Becker@Example.com ', 'Hi, my Houston order 1012 arrived completely melted. The bars are one puddle.',
                        name='Tom Becker', subject='Melted bars', order='#1012')
            s = send(p)
            t = T('T-0005')
            f = (t or {}).get('fields', {})
            check('Melted, warm and no cold pack (#1012)', 'webhook answers 200 with the new ticket number',
                  s.status == 200 and s.body == {'ok': True, 'status': 'received', 'ticket': 'T-0005'}, f'{s.status} {s.body}')
            first_claude = s.log('anthropic')[:1]
            lead = (parse_iso(first_claude[0]['time']) - s.answered_at).total_seconds() if first_claude else None
            check('Melted, warm and no cold pack (#1012)', 'the sender has its answer before Claude, Shopify and Postmark are called',
                  lead is not None and lead > 0.5, f'Claude called {lead:.1f} s after the answer' if lead is not None else 'no Claude call')
            want = {'Ticket ID': 'T-0005', 'Message ID': '<e2e-tom-1@help.example.com>', 'Channel': 'Help Form', 'Customer Name': 'Tom Becker',
                    'Customer Email': 'tom.becker@example.com', 'Order Number Given': '#1012', 'Email Matches Order': True,
                    'Subject': 'Melted bars', 'Issue Type': 'Melted', 'Sentiment': 'Negative', 'Classification Method': 'Claude',
                    'Status': 'Auto-Approved', 'Priority': 'Normal', 'Resolution': 'Replacement Approved',
                    'Customer': [ref('Customers', 'tom')], 'Order': [ref('Orders', 'o1012')], 'Replacement Order Name': '#1026'}
            diff = {k: (f.get(k), v) for k, v in want.items() if f.get(k) != v}
            check('Melted, warm and no cold pack (#1012)', 'ticket saved with the exact classification, links and outcome', t and not diff, json.dumps(diff))
            check('Melted, warm and no cold pack (#1012)', 'routing reason names the rule that approved it',
                  f.get('Routing Reason') == 'Free replacement approved: warm forecast (30.5 C, limit 27 C) and no cold pack, delivered '
                  + f'{days_between(today, seed("2026-10-18"))} days ago (window 7 days).', f.get('Routing Reason'))
            source = (s.log('shopify', operation='ReplacementSource') or [{}])[0]
            check('Melted, warm and no cold pack (#1012)', 'Shopify calls in order: read the order and look for an earlier replacement, create, complete',
                  s.shopify_ops() == ['ReplacementSource', 'CreateReplacement', 'CompleteReplacement']
                  and source.get('variables') == {'id': 'gid://shopify/Order/5200000001012', 'existing': 'tag:"replacement-for-1012"'}
                  and not [e for e in s.log('shopify') if e.get('graphql_errors')], json.dumps(s.shopify_ops()))
            drafts = list(mock.shopify.draft_orders.values())
            d0 = drafts[0] if len(drafts) == 1 else {}
            inp = d0.get('_input', {})
            want_input = {
                'email': 'tom.becker@example.com', 'note': 'Free replacement for #1012, melted on arrival. Support ticket T-0005.',
                'tags': ['replacement', 'delivery-triage', 'ticket-T-0005', 'replacement-for-1012'],
                'lineItems': [{'variantId': 'gid://shopify/ProductVariant/4510000000103', 'quantity': 2},
                              {'variantId': 'gid://shopify/ProductVariant/4510000000116', 'quantity': 2}],
                'appliedDiscount': {'valueType': 'PERCENTAGE', 'value': 100, 'title': 'Melt replacement', 'description': 'Free replacement for #1012'},
                'shippingAddress': {'firstName': 'Tom', 'lastName': 'Becker', 'address1': '48 Harbor Street', 'city': 'Houston',
                                    'provinceCode': 'TX', 'countryCode': 'US', 'zip': '77002'},
                'shippingLine': {'title': 'Free replacement shipping', 'priceWithCurrency': {'amount': '0.00', 'currencyCode': 'USD'}},
                'purchasingEntity': {'customerId': 'gid://shopify/Customer/7700000000113'}}
            check('Melted, warm and no cold pack (#1012)', 'one Shopify draft order with the same lines, 100 percent off, free shipping, the customer, the address and the replacement tag',
                  len(drafts) == 1 and inp == want_input and d0['totalPriceSet']['shopMoney']['amount'] == '0.0', json.dumps(inp)[:300])
            order = next((o for o in mock.shopify.orders.values() if o.get('_from_draft') == d0.get('id')), None)
            check('Melted, warm and no cold pack (#1012)', 'draft completed into order #1026 at no charge, ids stored on the ticket',
                  order and order['name'] == '#1026' and order['totalPriceSet']['shopMoney']['amount'] == '0.0' and d0['status'] == 'COMPLETED'
                  and f.get('Replacement Draft Order ID') == d0.get('id'), f'{order and order["name"]} {f.get("Replacement Draft Order ID")}')
            sent = mock.postmark.sent
            mail = sent[-1] if len(sent) == 1 else {}
            check('Melted, warm and no cold pack (#1012)', 'one approval email from support with the replacement number and the ticket reference',
                  len(sent) == 1 and mail['From'] == 'Demo Chocolate Support <support@example.com>' and mail['To'] == 'tom.becker@example.com'
                  and mail['Subject'] == 'Your free replacement for order #1012' and 'Your replacement order is #1026.' in mail['TextBody']
                  and 'Your reference is T-0005.' in mail['TextBody'] and mail['MessageStream'] == 'outbound' and mail['Tag'] == 'delivery-triage'
                  and mail['Metadata'] == {'ticket_id': 'T-0005', 'message_id': '<e2e-tom-1@help.example.com>'} and 'Headers' not in mail
                  and f.get('Reply Message ID') == mail['MessageID'] and bool(f.get('Reply Sent At')), mail.get('Subject', 'no email'))
            check('Melted, warm and no cold pack (#1012)', 'no Slack alert for an automatic approval', s.count('slack') == 0, f'{s.count("slack")} Slack posts')
            calls = mock.anthropic.calls
            body = calls[-1] if calls else {}
            tool = (body.get('tools') or [{}])[0]
            check('Melted, warm and no cold pack (#1012)', 'one Claude call, forced strict tool, no sampling parameters, accepted by the strict mock',
                  len(calls) == 1 and body['model'] == 'claude-sonnet-5' and body['tool_choice'] == {'type': 'tool', 'name': 'record_triage'}
                  and tool.get('strict') is True and body['max_tokens'] == 1024 and 'temperature' not in body and 'thinking' not in body
                  and 'arrived completely melted' in body['messages'][0]['content'] and body['messages'][-1]['role'] == 'user'
                  and mock.anthropic.problems == [] and mock.anthropic.schema_problems == [], f'{len(calls)} call(s)')
            lines = lines_of(f)
            check('Melted, warm and no cold pack (#1012)', 'Action Log has every step, in order',
                  lines == ['Received by Help Form as T-0005', 'Classified as Melted (Claude), sentiment Negative',
                            'Linked to order #1012, the sender email matches the order',
                            'Approved replacements already on record for this email: 0 (limit 2)',
                            'Decision Auto-Approved, Normal priority. ' + f.get('Routing Reason', ''),
                            'Replacement draft #D1 created (' + d0.get('id', '') + ')', 'Replacement order #1026 created at no charge',
                            'Reply sent to tom.becker@example.com (replacement confirmation, Postmark ' + mail.get('MessageID', '') + ')'],
                  f'{len(lines)} lines')
            at = s.log('airtable')
            check('Melted, warm and no cold pack (#1012)', 'exactly 7 Airtable requests (3 to save, 3 lookups, 1 update), one record per write, no 429',
                  [(e['operation'], e['table']) for e in at] == [('list', 'Tickets'), ('list', 'Tickets'), ('create', 'Tickets'), ('list', 'Orders'),
                                                                 ('list', 'Customers'), ('list', 'Tickets'), ('update', 'Tickets')]
                  and all(e.get('record_count', 1) == 1 for e in at) and all(e['status'] == 200 for e in at),
                  f'{len(at)} requests')

            # ---------------------------------------------------------------- 3. the same message again
            before = json.dumps(T('T-0005'))
            s = send(p)
            check('Same message delivered twice', 'answered as a duplicate of T-0005', s.status == 200 and s.body == {'ok': True, 'status': 'duplicate', 'ticket': 'T-0005'},
                  f'{s.status} {s.body}')
            check('Same message delivered twice', 'one Airtable lookup only: no new ticket, no Claude, Shopify, email or Slack, T-0005 unchanged',
                  s.touched() == {'airtable': 1} and T('T-0006') is None and json.dumps(T('T-0005')) == before and len(mock.shopify.draft_orders) == 1,
                  json.dumps(s.touched()))

            # ---------------------------------------------------------------- 4. a second message about the same order
            claude('Still waiting to hear about my melted 1012 order', 'melted', '1012', sentiment='negative')
            s = send(message('tom-2', 'tom.becker@example.com', 'Still waiting to hear about my melted 1012 order.', name='Tom Becker',
                             subject='Re: Melted bars', channel='email', message_id='<CAF-tom-2@mail.example.com>'))
            f = T('T-0006')['fields']
            check('Second message about #1012', 'a person decides, no second replacement for the same order',
                  f['Status'] == 'Human Queue' and f['Resolution'] == 'Routed to Human' and f['Channel'] == 'Email'
                  and f['Routing Reason'] == '#1012 already has an approved replacement (T-0005).' and len(mock.shopify.draft_orders) == 1
                  and s.count('postmark') == 0 and s.count('shopify') == 0, f['Routing Reason'])
            slack = [m['text'] for m in mock.slack.messages[-1:] if '/support' in m['_path']]
            check('Second message about #1012', 'Slack alert for the support queue with the ticket and the reason',
                  s.count('slack') == 1 and slack and slack[0].startswith('*Support ticket for a person* (Normal priority)\n*T-0006* from Tom Becker (tom.becker@example.com) via Email')
                  and 'Why: #1012 already has an approved replacement (T-0005).' in slack[0], (slack or ['no alert'])[0].split('\n')[0])

            # ---------------------------------------------------------------- 5. melted, arrived late
            claude('took ages and the bars were soft', 'melted', '#1015')
            s = send(message('sofia-1', 'sofia.marino@example.com', 'Order #1015 took ages and the bars were soft and bent when they came.', name='Sofia Marino'))
            f = T('T-0007')['fields']
            exp, dlv = seed('2026-10-14'), seed('2026-10-17T17:45:00Z')[:10]
            check('Melted, arrived 3 days late (#1015)', 'approved because it arrived late, even with a mild forecast',
                  f['Status'] == 'Auto-Approved' and f['Replacement Order Name'] == '#1027'
                  and f['Routing Reason'] == f'Free replacement approved: arrived 3 days late (expected {exp}, delivered {dlv}), delivered {days_between(today, dlv)} days ago (window 7 days).',
                  f['Routing Reason'])
            d1 = mock.shopify.draft_orders.get(f.get('Replacement Draft Order ID'), {})
            check('Melted, arrived 3 days late (#1015)', 'replacement has both lines of #1015 and the email went out',
                  d1.get('_input', {}).get('lineItems') == [{'variantId': 'gid://shopify/ProductVariant/4510000000101', 'quantity': 1},
                                                           {'variantId': 'gid://shopify/ProductVariant/4510000000117', 'quantity': 1}]
                  and s.count('postmark') == 1 and mock.postmark.sent[-1]['To'] == 'sofia.marino@example.com', json.dumps(d1.get('_input', {}).get('lineItems')))

            # ---------------------------------------------------------------- 6. reasons a melt claim goes to a person
            claude('even with the ice pack', 'melted', '#1009')
            s = send(message('omar-1', 'omar.haddad@example.com', 'Order #1009 was a bit soft even with the ice pack.', name='Omar Haddad'))
            f = T('T-0008')['fields']
            check('Melted, cold pack and on time (#1009)', 'no automatic replacement, a person decides',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'Normal'
                  and f['Routing Reason'] == '#1009 shipped with a cold pack and arrived on time, so there is no automatic replacement.'
                  and s.count('shopify') == 0 and s.count('postmark') == 0 and s.count('slack') == 1, f['Routing Reason'])
            check('Melted, cold pack and on time (#1009)', 'no reply fields on a ticket for a person, the Slack alert is in the Action Log',
                  'Reply Sent At' not in f and 'Reply Message ID' not in f and 'Replacement Order Name' not in f
                  and f['Action Log'].split('\n')[-1].endswith(' Slack alert posted to the support queue'), f['Action Log'].split('\n')[-1][25:])

            claude('Late note about order 1003', 'melted', '1003')
            s = send(message('priya-1', 'priya.raman@example.com', 'Late note about order 1003, the bars had melted.', name='Priya Raman'))
            f = T('T-0009')['fields']
            since = days_between(today, seed('2026-09-30'))
            check('Melted, outside the claim window (#1003)', f'delivered {since} days ago goes to a person',
                  f['Status'] == 'Human Queue' and f['Routing Reason'] == f'#1003 was delivered {since} days ago, outside the 7 day claim window.'
                  and f['Customer'] == [ref('Customers', 'priya')] and s.count('shopify') == 0, f['Routing Reason'])

            claude('melted AGAIN', 'melted', '#1021', sentiment='angry')
            s = send(message('derek-1', 'derek.olsen@example.com', 'Order #1021 melted AGAIN. Third time.', name='Derek Olsen'))
            f = T('T-0010')['fields']
            check('Repeat claimant at the limit (#1021)', 'a customer with 2 approved claims goes to a person, angry gives High priority',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'High' and f['Sentiment'] == 'Angry'
                  and f['Routing Reason'] == 'This customer already has 2 approved replacement claims (T-0001, T-0002), the limit is 2. The customer is angry.'
                  and s.count('shopify') == 0 and s.count('postmark') == 0, f['Routing Reason'])

            # ---------------------------------------------------------------- 7. damaged and wrong item: ask for a photo
            claude('praline boxes were crushed', 'damaged', '#1018')
            s = send(message('victor-1', 'victor.hale@example.com', 'Both praline boxes were crushed in order #1018.', name='Victor Hale'))
            f = T('T-0011')['fields']
            mail = mock.postmark.sent[-1]
            check('Damaged (#1018)', 'Waiting on Customer, Photo Requested, linked to the order, no Slack alert',
                  f['Status'] == 'Waiting on Customer' and f['Resolution'] == 'Photo Requested' and f['Routing Reason'] == 'Damaged items need a photo first.'
                  and f['Order'] == [ref('Orders', 'o1018')] and f['Email Matches Order'] is True
                  and s.count('slack') == 0 and s.count('shopify') == 0, f['Status'])
            check('Damaged (#1018)', 'photo request email to the customer',
                  s.count('postmark') == 1 and mail['To'] == 'victor.hale@example.com' and mail['Subject'] == 'About your order #1018'
                  and 'a photo of the box and the bars as they arrived' in mail['TextBody'] and f['Reply Message ID'] == mail['MessageID'], mail['Subject'])

            claude('cherry bars instead of', 'wrong_item', '#1020', sentiment='neutral')
            s = send(message('felix-1', 'felix.osei@example.com', 'I got two cherry bars instead of the orange one in #1020.', name='Felix Osei'))
            f = T('T-0012')['fields']
            mail = mock.postmark.sent[-1]
            check('Wrong item (#1020)', 'photo of what arrived requested',
                  f['Status'] == 'Waiting on Customer' and f['Resolution'] == 'Photo Requested' and f['Issue Type'] == 'Wrong Item'
                  and s.count('postmark') == 1 and mail['To'] == 'felix.osei@example.com'
                  and 'arrived with the wrong item' in mail['TextBody'] and 'with the label showing' in mail['TextBody'], f['Routing Reason'])

            # ---------------------------------------------------------------- 8. missing parcels
            claude('never showed up', 'missing', '#1022')
            s = send(message('clara-1', 'clara.jensen@example.com', 'Order #1022 never showed up.', name='Clara Jensen'))
            f = T('T-0013')['fields']
            late = days_between(today, seed('2026-10-17'))
            check('Missing and late (#1022)', 'a person opens a carrier trace, High priority, no email',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'High' and f['Issue Type'] == 'Missing'
                  and f['Routing Reason'] == f'#1022 shipped on {seed("2026-10-14")}, was expected by {seed("2026-10-17")} and is {late} days late. Open a carrier trace.'
                  and s.count('postmark') == 0 and s.count('slack') == 1, f['Routing Reason'])

            claude('Where is my order', 'missing', '#1017', sentiment='neutral')
            s = send(message('rosa-1', 'rosa.delgado@example.com', 'Where is my order #1017?', name='Rosa Delgado', subject='Where is my order?',
                             channel='email', message_id='<CAF-rosa-1@mail.example.com>'))
            f = T('T-0014')['fields']
            mail = mock.postmark.sent[-1]
            check('Missing but in transit (#1017)', 'resolved with tracking details, no Slack alert',
                  f['Status'] == 'Resolved' and f['Resolution'] == 'None' and s.count('slack') == 0 and s.count('postmark') == 1
                  and f['Routing Reason'] == f'#1017 is in transit and not late yet (expected by {seed("2026-10-21")}). Tracking details sent.', f['Routing Reason'])
            check('Missing but in transit (#1017)', 'email has the tracking number and stays in the customer email thread',
                  'Tracking number 1Z999AA10000001017' in mail['TextBody'] and mail['Subject'] == 'Re: Where is my order?'
                  and f'the carrier expects to deliver it by {month_day(seed("2026-10-21"))}.' in mail['TextBody']
                  and mail['Headers'] == [{'Name': 'In-Reply-To', 'Value': '<CAF-rosa-1@mail.example.com>'},
                                          {'Name': 'References', 'Value': '<CAF-rosa-1@mail.example.com>'}], mail['Subject'])

            # ---------------------------------------------------------------- 9. allergy questions and safety
            claude('tree nut allergy', 'allergy_question', '', sentiment='neutral', summary='Asks if the raspberry bar is safe with a tree nut allergy.')
            s = send(message('rosa-2', 'rosa.delgado@example.com', 'My son has a tree nut allergy. Is the raspberry bar safe for him? <!channel> <https://evil.example.com|click>',
                             name='Rosa Delgado'))
            f = T('T-0015')['fields']
            slack = mock.slack.messages[-1]['text'] if s.count('slack') else ''
            check('Allergy question', 'never answered automatically: Human Queue, High priority, no email',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'High' and f['Issue Type'] == 'Allergy Question'
                  and f['Routing Reason'] == 'Allergy and ingredient questions are always answered by a person.'
                  and s.count('postmark') == 0 and f['Customer'] == [ref('Customers', 'rosa')] and 'Order' not in f, f['Routing Reason'])
            check('Allergy question', 'customer text in the Slack alert is escaped, so it cannot ping the channel or fake a link',
                  s.count('slack') == 1 and '&lt;!channel&gt;' in slack and '<!channel>' not in slack and '<https://evil' not in slack, slack.split('\n')[-1][:90])

            claude('broke out in hives', 'melted', '#1018', safety=True)
            s = send(message('victor-2', 'victor.hale@example.com', 'The bars from #1018 had melted and my daughter broke out in hives after one bite.', name='Victor Hale'))
            f = T('T-0016')['fields']
            check('Safety concern (#1018)', 'Urgent for a person, no replacement even though the order would qualify',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'Urgent' and s.count('shopify') == 0 and s.count('postmark') == 0
                  and s.count('slack') == 1 and '(Urgent priority)' in mock.slack.messages[-1]['text'], f['Priority'])

            # ---------------------------------------------------------------- 10. order checks
            claude('order 1020 melted', 'melted', '1020')
            s = send(message('jordan-1', 'jordan.blake@example.com', 'Hi, order 1020 melted on the porch.', name='Jordan Blake'))
            f = T('T-0017')['fields']
            check('Sender email does not match the order (#1020)', 'nothing automatic, a person checks it',
                  f['Status'] == 'Human Queue' and 'Email Matches Order' not in f and f['Order'] == [ref('Orders', 'o1020')]
                  and f['Routing Reason'] == 'The sender email does not match the email on #1020, so nothing was sent automatically.'
                  and s.count('shopify') == 0 and s.count('postmark') == 0, f['Routing Reason'])
            check('Sender email does not match the order (#1020)', 'customer matched by email prefers the record with a Shopify id over the duplicate',
                  f['Customer'] == [ref('Customers', 'jordan')],
                  'jordan, Shopify Customer ID 7700000000103' if f.get('Customer') == [ref('Customers', 'jordan')] else str(f.get('Customer')))

            claude('chocolate arrived as a puddle', 'melted', '')
            s = send(message('aisha-1', 'aisha.khan@example.com', 'My chocolate arrived as a puddle, so sad.', name='https://evil.example.com/refund Khan'))
            f = T('T-0018')['fields']
            mail = mock.postmark.sent[-1]
            check('No order number, a link typed as the name', 'customer asked for the order number, Waiting on Customer',
                  f['Status'] == 'Waiting on Customer' and f['Resolution'] == 'None' and 'Order' not in f and f['Customer'] == [ref('Customers', 'aisha')]
                  and s.count('postmark') == 1 and mail['Subject'] == 'Could you send us your order number?' and mail['To'] == 'aisha.khan@example.com', f['Routing Reason'])
            check('No order number, a link typed as the name', 'the link never reaches the email, the greeting falls back to "there"',
                  mail['TextBody'].startswith('Hi there,\n') and 'evil' not in mail['TextBody'], mail['TextBody'].split('\n')[0])

            claude('order #9999', 'damaged', '#9999')
            s = send(message('ben-1', 'ben.carter@example.com', 'My order #9999 came broken.', name='Ben Carter'))
            f = T('T-0019')['fields']
            check('Order number not found', 'a person looks it up', f['Status'] == 'Human Queue'
                  and f['Routing Reason'] == 'Order #9999 was not found in Airtable.' and 'Order' not in f and s.count('postmark') == 0, f['Routing Reason'])

            claude('photos attached', 'damaged', '#1020')
            photos = [{'url': 'https://files.example.com/felix/box.jpg', 'filename': 'box.jpg'}, 'https://files.example.com/felix/bars.jpg']
            s = send(message('felix-2', 'felix.osei@example.com', 'The bars in #1020 were also cracked, photos attached.', name='Felix Osei', photos=photos))
            f = T('T-0020')['fields']
            check('Damaged with photos attached (#1020)', 'photos saved on the ticket and a person reviews them instead of asking again',
                  f['Status'] == 'Human Queue' and f['Routing Reason'] == 'Damage reported with 2 photos attached, ready for a person to review.'
                  and [(a['url'], a['filename']) for a in f.get('Photos', [])] == [('https://files.example.com/felix/box.jpg', 'box.jpg'),
                                                                                  ('https://files.example.com/felix/bars.jpg', 'bars.jpg')]
                  and s.count('postmark') == 0, f['Routing Reason'])

            # ---------------------------------------------------------------- 11. odd message ids are matched exactly
            odd = '<odd "quoted" \\ id@help.example.com>'
            claude('ship to Canada', 'other', '', sentiment='positive', summary='Asks about shipping to Canada.')
            s = send(message('lucia-1', 'lucia.gomez@example.com', 'Love your bars! Do you ship to Canada?', name='Lucia Gomez', message_id=odd))
            f = T('T-0021')['fields']
            check('Other question, message id with quotes and a backslash', 'saved exactly and sent to a person',
                  f['Message ID'] == odd and f['Issue Type'] == 'Other' and f['Status'] == 'Human Queue'
                  and f['Routing Reason'] == 'Not a delivery problem the automatic rules cover.', f['Message ID'])
            s = send(message('lucia-1', 'lucia.gomez@example.com', 'Love your bars! Do you ship to Canada?', name='Lucia Gomez', message_id=odd))
            check('Other question, message id with quotes and a backslash', 'the repeat is found by the escaped formula and skipped',
                  s.body == {'ok': True, 'status': 'duplicate', 'ticket': 'T-0021'} and s.touched() == {'airtable': 1}, json.dumps(s.body))

            # ---------------------------------------------------------------- 12. Claude down or confused
            mock.anthropic.on('bars arrived melted, order', status=529, times=3)
            s = send(message('maya-1', 'maya.chen@example.com', 'The bars arrived melted, order #1001.', name='Maya Chen'))
            f = T('T-0022')['fields']
            check('Claude down (529)', 'retried 3 times, then sorted by keywords', s.count('anthropic') == 3 and f['Classification Method'] == 'Keyword Fallback'
                  and f['Issue Type'] == 'Melted' and f['Order'] == [ref('Orders', 'o1001')], f'{s.count("anthropic")} Claude calls')
            check('Claude down (529)', 'always goes to a person with the reason, never approved by keywords',
                  f['Status'] == 'Human Queue' and s.count('shopify') == 0
                  and f['Routing Reason'] == 'Claude was unavailable (HTTP 529, overloaded_error), so the message was sorted by keywords as Melted. A person must check it.'
                  and 'Issue: Melted (Keyword Fallback)' in mock.slack.messages[-1]['text'], f['Routing Reason'])

            mock.anthropic.on('crushed when it arrived', text='I think this is a damaged parcel.', times=1)
            s = send(message('aisha-2', 'aisha.khan@example.com', 'The box was crushed when it arrived.', name='Aisha Khan', order='1013'))
            f = T('T-0023')['fields']
            check('Claude answers without the tool call', 'keyword fallback, form order number used, a person decides',
                  f['Classification Method'] == 'Keyword Fallback' and f['Issue Type'] == 'Damaged' and f['Order'] == [ref('Orders', 'o1013')]
                  and f['Order Number Given'] == '1013' and f['Status'] == 'Human Queue'
                  and f['Routing Reason'].startswith('Claude was unavailable (Claude answered without the triage tool call)'), f['Routing Reason'])

            # ---------------------------------------------------------------- 13. Shopify and Postmark failures
            claude('bars, order 1018, they arrived late', 'melted', '1018')
            mock.fail('shopify', status=500, times=3, match={'operation': 'CreateReplacement'})
            drafts_before = len(mock.shopify.draft_orders)
            s = send(message('victor-3', 'victor.hale@example.com', 'Soft bars, order 1018, they arrived late too.', name='Victor Hale'))
            f = T('T-0024')['fields']
            check('Shopify down while creating the replacement (#1018)', 'retried 3 times, then a person creates it, High priority, no approval email',
                  s.count('shopify', operation='CreateReplacement') == 3 and len(mock.shopify.draft_orders) == drafts_before
                  and s.count('shopify', operation='CompleteReplacement') == 0
                  and f['Status'] == 'Human Queue' and f['Priority'] == 'High' and f['Resolution'] == 'Routed to Human'
                  and 'But Shopify did not answer the draft order request (HTTP 500' in f['Routing Reason'] and s.count('postmark') == 0,
                  '3 attempts, then: ' + f['Routing Reason'].split(' But ')[-1])
            check('Shopify down while creating the replacement (#1018)', 'the support queue is told in Slack',
                  s.count('slack') == 1 and '*T-0024*' in mock.slack.messages[-1]['text'] and '(High priority)' in mock.slack.messages[-1]['text'], '')

            claude('about the melted bars from 1018', 'melted', '1018')
            mock.postmark.inactive_recipients.add('victor.hale@example.com')
            s = send(message('victor-4', 'victor.hale@example.com', 'Writing again about the melted bars from 1018.', name='Victor Hale'))
            mock.postmark.inactive_recipients.discard('victor.hale@example.com')
            f = T('T-0025')['fields']
            check('Email bounces after the replacement was created (#1018)', 'replacement #1028 stays approved, the ticket goes to a person with High priority',
                  f['Replacement Order Name'] == '#1028' and f['Resolution'] == 'Replacement Approved' and f['Status'] == 'Human Queue'
                  and f['Priority'] == 'High' and 'Reply Message ID' not in f and s.count('postmark') == 3
                  and 'The reply email could not be sent (HTTP 422, Postmark error 406' in f['Routing Reason'],
                  'The reply email ' + f['Routing Reason'].split(' The reply email ')[-1])
            check('Email bounces after the replacement was created (#1018)', 'Slack alert names the replacement so a person can contact the customer',
                  s.count('slack') == 1 and 'Replacement: #1028 created' in mock.slack.messages[-1]['text'], mock.slack.messages[-1]['text'].split('\n')[4])

            # ---------------------------------------------------------------- 14. Shopify still calculating, guards and errors
            # #1023 (ahmed): made 2 days late for this test; Shopify has since refunded the peanut bar line completely.
            dlv = make_late('o1023')
            o1023 = mock.shopify.orders['gid://shopify/Order/5200000001023']
            next(ln for ln in o1023['_lines'] if ln['sku'] == 'DC-BAR-MILK-PEANUT')['currentQuantity'] = 0
            mock.shopify.calculate_next(1.0)
            claude('soft and bent from the heat', 'melted', '#1023')
            tid = next_ticket()
            s = send(message('ahmed-1', 'ahmed.siddiqui@example.com', 'Order #1023 arrived soft and bent from the heat, two days late.', name='Ahmed Siddiqui'))
            f = T(tid)['fields']
            gid = f.get('Replacement Draft Order ID', '')
            dn = mock.shopify.draft_orders.get(gid, {})
            complete_entry = (s.log('shopify', operation='CompleteReplacement') or [{}])[0]
            check('Shopify still calculating the draft (#1023)', 'waits 2 seconds, checks ready once, then completes it',
                  s.shopify_ops() == ['ReplacementSource', 'CreateReplacement', 'DraftReady', 'CompleteReplacement']
                  and f['Status'] == 'Auto-Approved' and f['Resolution'] == 'Replacement Approved' and f.get('Replacement Order Name') == '#1029'
                  and dn.get('status') == 'COMPLETED' and complete_entry.get('status') == 200
                  and not [e for e in s.log('shopify') if e.get('graphql_errors')], json.dumps(s.shopify_ops()))
            cookie = mock.shopify.variant_by_sku('DC-BAR-WHITE-COOKIE')['id']
            check('Shopify still calculating the draft (#1023)', 'only what was not refunded is reshipped: the current quantity, the refunded line left out',
                  dn.get('_input', {}).get('lineItems') == [{'variantId': cookie, 'quantity': 1}]
                  and dn['_input']['tags'] == ['replacement', 'delivery-triage', f'ticket-{tid}', 'replacement-for-1023'],
                  json.dumps(dn.get('_input', {}).get('lineItems')))
            name = dn.get('name', '')
            check('Shopify still calculating the draft (#1023)', 'Action Log shows the skipped line, the wait and the ready check',
                  lines_of(f)[5:9] == ['Not reshipped (refunded, removed or custom lines): DC-BAR-MILK-PEANUT',
                                       f'Replacement draft {name} created ({gid})',
                                       f'Shopify is still calculating draft {name}, checking again in 2 seconds',
                                       f'Draft {name} is ready after 1 check']
                  and f['Routing Reason'] == f'Free replacement approved: arrived 2 days late (expected {(date.fromisoformat(dlv) - timedelta(days=2)).isoformat()}, delivered {dlv}), delivered 1 day ago (window 7 days).',
                  ' | '.join(lines_of(f)[5:9]))

            # #1013 (aisha): made late; the new draft stays "calculating" for 2 minutes.
            make_late('o1013')
            mock.shopify.calculate_next(120)
            claude('melted into the wrapper', 'melted', '#1013')
            tid = next_ticket()
            drafts_before = len(mock.shopify.draft_orders)
            s = send(message('aisha-3', 'aisha.khan@example.com', 'Order #1013 melted into the wrapper.', name='Aisha Khan'))
            f = T(tid)['fields']
            gid = f.get('Replacement Draft Order ID', '')
            name = mock.shopify.draft_orders.get(gid, {}).get('name', '?')
            check('Draft still calculating after 3 checks (#1013)', 'never completed: 3 checks 2 seconds apart, then a person completes it',
                  s.shopify_ops() == ['ReplacementSource', 'CreateReplacement', 'DraftReady', 'DraftReady', 'DraftReady']
                  and len(mock.shopify.draft_orders) == drafts_before + 1 and mock.shopify.draft_orders[gid]['status'] == 'OPEN'
                  and f['Status'] == 'Human Queue' and f['Priority'] == 'High' and f['Resolution'] == 'Routed to Human'
                  and 'Replacement Order Name' not in f and s.count('postmark') == 0 and s.count('slack') == 1
                  and f['Routing Reason'].endswith(f' But Shopify was still calculating draft {name} after 3 checks, so it was not completed. '
                                                   f'Create the replacement by hand (complete draft {name}) and reply to the customer.'),
                  json.dumps(s.shopify_ops()))

            claude('Any news on 1013', 'melted', '1013')
            tid = next_ticket()
            drafts_before = len(mock.shopify.draft_orders)
            s = send(message('aisha-4', 'aisha.khan@example.com', 'Any news on 1013? The bars were melted.', name='Aisha Khan'))
            f = T(tid)['fields']
            check('A replacement already exists in Shopify (#1013)', 'the replacement tag stops a second draft even though Airtable shows no approval',
                  s.shopify_ops() == ['ReplacementSource'] and len(mock.shopify.draft_orders) == drafts_before
                  and f['Status'] == 'Human Queue' and f['Priority'] == 'High' and 'Replacement Draft Order ID' not in f
                  and f['Routing Reason'].endswith(f' But Shopify already has a replacement for #1013 (draft {name}, not completed), so a second one was not created. '
                                                   'Check that replacement before sending another, then reply to the customer.')
                  and s.count('postmark') == 0, f['Routing Reason'].split(' But ')[-1])

            # #1014 (ben): made late; Shopify refuses the draft with a user error, then is throttled.
            make_late('o1014')
            claude('came late and the bars were soft', 'melted', '#1014')
            refusal = {'data': {'draftOrderCreate': {'draftOrder': None, 'userErrors': [
                {'field': ['lineItems', '0', 'variantId'], 'message': 'Product with ID gid://shopify/ProductVariant/4510000000121 is no longer available.'}]}}}
            mock.fail('shopify', status=200, body=refusal, times=1, match={'operation': 'CreateReplacement'})
            tid = next_ticket()
            s = send(message('ben-2', 'ben.carter@example.com', 'Order #1014 came late and the bars were soft.', name='Ben Carter'))
            f = T(tid)['fields']
            check('Shopify user error on the draft order (#1014)', 'no retry on a user error, a person creates it, High priority',
                  s.shopify_ops() == ['ReplacementSource', 'CreateReplacement'] and f['Status'] == 'Human Queue' and f['Priority'] == 'High'
                  and 'Replacement Draft Order ID' not in f and s.count('postmark') == 0
                  and ' But Shopify did not create the draft order (Product with ID gid://shopify/ProductVariant/4510000000121 is no longer available.). '
                      'Create the replacement by hand and reply to the customer.' in f['Routing Reason'], f['Routing Reason'].split(' But ')[-1])

            claude('Still nothing about #1014', 'melted', '#1014')
            mock.shopify.throttle(times=1)
            tid = next_ticket()
            s = send(message('ben-3', 'ben.carter@example.com', 'Still nothing about #1014, the bars were melted.', name='Ben Carter'))
            f = T(tid)['fields']
            check('Shopify throttled (#1014)', 'a throttled order lookup creates nothing and goes to a person with the reason',
                  s.shopify_ops() == ['ReplacementSource'] and s.log('shopify')[0].get('throttled') is True
                  and f['Status'] == 'Human Queue' and f['Priority'] == 'High'
                  and ' But Shopify refused the order lookup (Throttled). Create the replacement by hand and reply to the customer.' in f['Routing Reason'], f['Routing Reason'].split(' But ')[-1])

            # ---------------------------------------------------------------- 15. more rule paths
            claude('tracking says delivered but #1009 never arrived', 'missing', '#1009', sentiment='neutral')
            tid = next_ticket()
            s = send(message('omar-2', 'omar.haddad@example.com', 'The tracking says delivered but #1009 never arrived.', name='Omar Haddad'))
            f = T(tid)['fields']
            check('Missing, but the carrier says delivered (#1009)', 'High priority for a person to check with the carrier, no email',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'High' and s.count('postmark') == 0 and s.count('slack') == 1
                  and f['Routing Reason'] == f'The carrier marked #1009 delivered on {seed("2026-10-19")}, but the customer says it did not arrive. Check with the carrier.',
                  f['Routing Reason'])

            claude('Has my order #1024 shipped', 'missing', '#1024', sentiment='neutral')
            tid = next_ticket()
            s = send(message('mila-1', 'mila.petrova@example.com', 'Has my order #1024 shipped? It has not arrived.', name='Mila\n  Petrova',
                             subject='Where is\r\nmy order'))
            f = T(tid)['fields']
            check('Missing, not shipped yet (#1024)', 'a person checks the order; line breaks in the name and subject become spaces',
                  f['Status'] == 'Human Queue' and f['Priority'] == 'Normal' and f['Routing Reason'] == '#1024 has not shipped yet.'
                  and f['Customer Name'] == 'Mila Petrova' and f['Subject'] == 'Where is my order' and s.count('postmark') == 0, f['Routing Reason'])

            claude('melted already', 'melted', '#1017', sentiment='negative')
            tid = next_ticket()
            s = send(message('rosa-3', 'rosa.delgado@example.com', 'I can tell #1017 will arrive melted already.', name='Rosa Delgado'))
            f = T(tid)['fields']
            check('Melted claim before delivery (#1017)', 'a person decides, nothing is created',
                  f['Status'] == 'Human Queue' and f['Routing Reason'] == '#1017 is not marked delivered yet.'
                  and s.count('shopify') == 0 and s.count('postmark') == 0, f['Routing Reason'])

            claude('cancelled order #1025 melted', 'melted', '#1025')
            mock.fail('slack', status=500, times=3, match={'path_contains': '/support'})
            tid = next_ticket()
            s = send(message('owen-1', 'owen.price@example.com', 'My cancelled order #1025 melted anyway?', name='Owen Price'))
            f = T(tid)['fields']
            check('Cancelled order, Slack down (#1025)', 'nothing to replace on a cancelled order, a person decides',
                  f['Status'] == 'Human Queue' and f['Routing Reason'] == '#1025 is cancelled, so there is nothing to replace automatically.'
                  and s.count('shopify') == 0, f['Routing Reason'])
            check('Cancelled order, Slack down (#1025)', 'Slack tried 3 times, the ticket stays in the Human Queue and the Action Log says the alert failed',
                  s.count('slack') == 3 and s.ex['status'] == 'success'
                  and lines_of(f)[-1] == 'Slack alert failed (HTTP 500, internal_error), the ticket still waits in the Human Queue', lines_of(f)[-1])

            mock.anthropic.on('refund for my bars', tool_input={'issue_type': 'refund', 'order_number': '#1012', 'sentiment': 'negative',
                                                                 'safety_concern': False, 'summary': 'Wants a refund.'}, times=1)
            tid = next_ticket()
            s = send(message('tom-3', 'tom.becker@example.com', 'I want a refund for my bars from #1012, they melted.', name='Tom Becker'))
            f = T(tid)['fields']
            check('Claude answers outside the schema', 'an issue type that is not in the list falls back to keywords and a person',
                  f['Classification Method'] == 'Keyword Fallback' and f['Issue Type'] == 'Melted' and f['Status'] == 'Human Queue'
                  and f['Routing Reason'] == 'Claude was unavailable (Claude returned an incomplete triage), so the message was sorted by keywords as Melted. A person must check it.'
                  and [p['input']['issue_type'] for p in mock.anthropic.schema_problems] == ['refund'] and s.count('shopify') == 0, f['Routing Reason'])

            # ---------------------------------------------------------------- 16. Airtable failures
            mock.fail('airtable', status=429, times=3, match={'table': 'Tickets', 'operation': 'list'})
            time.sleep(GAP)
            mark = mock.mark()
            count_before = mock.airtable.count('Tickets')
            req = urllib.request.Request(f'{n8n.url}/webhook/{SLUG}-e2e-{PATH}', method='POST',
                                         data=json.dumps(message('irene-1', 'irene.novak@example.com', 'Is my order #1019 shipped yet?')).encode(),
                                         headers={'Content-Type': 'application/json', 'X-Support-Secret': SECRET})
            run.started += 1           # Run.post does not return headers, and this check needs Retry-After
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status, retry_after, text = r.status, r.headers.get('Retry-After'), r.read().decode()
            except urllib.error.HTTPError as e:
                status, retry_after, text = e.code, e.headers.get('Retry-After'), e.read().decode()
            ex = run.wait()
            run_status.append(('<e2e-irene-1@help.example.com>', 'error', ex['status']))
            check('Airtable rate limited while saving', 'the sender gets 503 with Retry-After 60, so the form or parser sends it again',
                  status == 503 and retry_after == '60' and json.loads(text).get('ok') is False and mock.airtable.count('Tickets') == count_before
                  and len(mock.log('airtable', since=mark)) == 3, f'{status} Retry-After {retry_after}')
            alert = run.alert_for(ex)
            check('Airtable rate limited while saving', 'run fails and the error alert names the message',
                  ex['status'] == 'error' and alert and 'failed at *Stop: Airtable Unavailable*' in alert
                  and 'Airtable did not answer while saving support message <e2e-irene-1@help.example.com> (429)' in alert,
                  alert.split('\n')[1] if alert else 'no alert')

            claude('order 1019 shipped yet', 'missing', '#1019', sentiment='neutral')
            mock.fail('airtable', status=503, times=3, match={'table': 'Orders'})
            tid = next_ticket()
            s = send(message('irene-2', 'irene.novak@example.com', 'Has order 1019 shipped yet?', name='Irene Novak'), expect='error')
            alert = run.alert_for(s.ex)
            check('Airtable down during the lookups', f'ticket {tid} is kept at New and the alert says to triage it by hand, no cooldown for a 503',
                  s.status == 200 and T(tid)['fields']['Status'] == 'New' and s.ex['status'] == 'error' and alert
                  and s.count('airtable', table='Orders') == 3 and not run.ran(s.ex, 'Wait: Lookup Cooldown')
                  and f'Ticket {tid} was saved but not triaged, because Airtable did not answer the order and claim lookups (503)' in alert,
                  alert.split('\n')[1] if alert else 'no alert')

            claude('gift wrap', 'other', '', sentiment='neutral')
            mock.fail('airtable', status=500, times=3, match={'table': 'Tickets', 'method': 'PATCH'})
            tid = next_ticket()
            s = send(message('irene-3', 'irene.novak@example.com', 'Can you add gift wrap to future orders?', name='Irene Novak'), expect='error')
            alert = run.alert_for(s.ex)
            check('Airtable down when saving the outcome', 'retried 3 times, the alert carries the outcome for manual entry',
                  s.ex['status'] == 'error' and s.count('airtable', method='PATCH') == 3 and alert and 'failed at *Stop: Ticket Not Saved*' in alert
                  and not run.ran(s.ex, 'Wait: Update Cooldown')
                  and f'Ticket {tid} was triaged as Human Queue (Routed to Human, Normal priority), no email was sent, but Airtable did not save the result (500).' in alert,
                  alert.split('\n')[1] if alert else 'no alert')

            # Another automation bursts the base right after the ticket is saved: the real limiter blocks it for 30 seconds.
            def burst():
                for _ in range(6):
                    r = urllib.request.Request(f'{mock.local_url}/v0/{BASE}/Products?maxRecords=1',
                                               headers={'Authorization': f'Bearer {AIRTABLE_TOKEN}', 'X-Test-Burst': '1'})
                    try:
                        urllib.request.urlopen(r, timeout=10).read()
                    except urllib.error.HTTPError as e:
                        e.read()

            claude('Order 1019 status please', 'missing', '#1019', sentiment='neutral')
            tid = next_ticket()
            burst_from = mock.mark()
            s = send(message('irene-4', 'irene.novak@example.com', 'Order 1019 status please, has it shipped?', name='Irene Novak'), after_post=burst)
            burst_to = mock.mark()
            f = T(tid)['fields']
            orders = s.log('airtable', table='Orders')
            check('Airtable rate limited during the lookups', 'waits 35 seconds once, looks again and finishes the triage',
                  s.ex['status'] == 'success' and [e['status'] for e in orders] == [429, 429, 429, 200]
                  and not any(e.get('injected') for e in orders) and run.runs_of(s.ex, 'Wait: Lookup Cooldown') == 1
                  and (parse_iso(orders[3]['time']) - parse_iso(orders[2]['time'])).total_seconds() >= 34
                  and f['Status'] == 'Human Queue' and f['Routing Reason'] == '#1019 has not shipped yet.'
                  and 'Airtable was rate limited during the lookups, so the workflow waited 35 seconds and looked again' in lines_of(f)
                  and run.alert_for(s.ex, timeout=3) is None, json.dumps([e['status'] for e in orders]))

            claude('Thanks for the quick help', 'other', '', sentiment='positive')
            mock.fail('airtable', status=429, times=3, match={'table': 'Tickets', 'method': 'PATCH'})
            tid = next_ticket()
            s = send(message('irene-5', 'irene.novak@example.com', 'Thanks for the quick help with my order!', name='Irene Novak'))
            f = T(tid)['fields']
            patches = s.log('airtable', method='PATCH')
            check('Airtable rate limited when saving the outcome', 'waits 35 seconds once, then the outcome is saved and no alert goes out',
                  s.ex['status'] == 'success' and [e['status'] for e in patches] == [429, 429, 429, 200]
                  and run.runs_of(s.ex, 'Wait: Update Cooldown') == 1 and f['Status'] == 'Human Queue' and f['Issue Type'] == 'Other'
                  and run.alert_for(s.ex, timeout=3) is None, json.dumps([e['status'] for e in patches]))

            # ---------------------------------------------------------------- 17. whole run
            limited = [e for e in mock.log('airtable') if e['status'] == 429 and not e.get('injected') and not (burst_from < e['seq'] <= burst_to)]
            writes = [e for e in mock.log('airtable') if 'record_count' in e]
            check('Whole run', 'Airtable never rate limited the workflow outside the deliberate burst, every write holds 1 record',
                  not limited and writes and all(e['record_count'] == 1 for e in writes), f'{len(limited)} other 429s, {len(writes)} writes')
            check('Whole run', 'every Claude request was valid for the real API rules; the only off-schema answer is the scripted one',
                  mock.anthropic.problems == [] and len(mock.anthropic.schema_problems) == 1, json.dumps(mock.anthropic.problems)[:200])
            bad_gql = [e.get('operation') for e in mock.log('shopify') if e.get('graphql_errors') and not e.get('injected')]
            check('Whole run', 'every Shopify query and mutation was valid for the schema (no GraphQL errors)', not bad_gql, json.dumps(bad_gql))
            stray = [m['_path'] for m in mock.slack.messages if not m['_path'].endswith(('/support', '/errors'))]
            check('Whole run', 'Slack only got support queue alerts and error alerts', not stray, json.dumps(stray))
            wrong = [(mid, want_s, got) for mid, want_s, got in run_status if want_s != got]
            check('Whole run', 'every run ended as expected: success, or failed (with an alert) only where Airtable failed',
                  not wrong, f'{len(run_status)} runs' if not wrong else json.dumps(wrong))

            first_request = mock.anthropic.calls[0] if mock.anthropic.calls else None
            if os.environ.get('E2E_SAMPLES'):
                # optional: save what the workflow really sent, for docs/test-report.md
                gid = (T('T-0005') or {}).get('fields', {}).get('Replacement Draft Order ID')
                samples = {
                    'claude_request': first_request,
                    'ticket_T-0005': (T('T-0005') or {}).get('fields'),
                    'draft_input_T-0005': mock.shopify.draft_orders.get(gid, {}).get('_input'),
                    'emails': [{k: m.get(k) for k in ('From', 'To', 'Subject', 'TextBody', 'Headers', 'Metadata')} for m in mock.postmark.sent],
                    'slack_support': [m.get('text') for m in mock.slack.messages if m['_path'].endswith('/support')],
                    'error_alerts': run.error_alerts(),
                }
                Path(os.environ['E2E_SAMPLES']).write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding='utf-8')

        # ---------------------------------------------------------------- 18. Slack is optional
        wf3 = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, SLACK_SUPPORT_WEBHOOK_URL=''))
        with n8n.temporary(wf3, SLUG, mock=mock) as run3:
            claude('Are the dark bars vegan', 'allergy_question', '', sentiment='neutral')
            tid = next_ticket()
            time.sleep(GAP)
            mark = mock.mark()
            status, text = run3.post(PATH, message('nina-1', 'nina.patel@example.com', 'Are the dark bars vegan?', name='Nina Patel'),
                                     headers={'X-Support-Secret': SECRET})
            ex = run3.wait()
            f = (T(tid) or {}).get('fields', {})
            check('No SLACK_SUPPORT_WEBHOOK_URL set', 'the ticket still lands in the Human Queue and the Action Log says no alert was sent',
                  status == 200 and ex['status'] == 'success' and f.get('Status') == 'Human Queue' and len(mock.log('slack', since=mark)) == 0
                  and f.get('Classification Method') == 'Claude' and f.get('Issue Type') == 'Allergy Question' and mock.anthropic.problems == []
                  and f.get('Action Log', '').endswith('No SLACK_SUPPORT_WEBHOOK_URL set, the ticket waits in the Human Queue without an alert'),
                  f.get('Action Log', '').split('\n')[-1][25:])

        # ---------------------------------------------------------------- 19. secret not configured (fail closed)
        wf2 = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock, SUPPORT_FORM_SECRET=''))
        with n8n.temporary(wf2, SLUG, mock=mock) as run2:
            time.sleep(GAP)
            mark = mock.mark()
            status, text = run2.post(PATH, message('cfg-1', 'tom.becker@example.com', 'Order #1012 melted.'), headers={'X-Support-Secret': ''})
            ex = run2.wait()
            alert = run2.alert_for(ex)
            check('No SUPPORT_FORM_SECRET configured', 'every message refused with 503, even with an empty secret header, nothing called',
                  status == 503 and json.loads(text).get('error') == 'The support webhook is not configured yet'
                  and not [e for e in mock.log(since=mark) if e['service'] in ALL_SERVICES and '/errors' not in e['path']], f'{status} {text}')
            check('No SUPPORT_FORM_SECRET configured', 'the error alert tells you to set it',
                  ex['status'] == 'error' and alert and 'failed at *Stop: Not Configured*' in alert and 'SUPPORT_FORM_SECRET is empty' in alert,
                  (alert or 'no alert').split('\n')[0])

        # ---------------------------------------------------------------- 20. the request shape against the real API (free)
        key = os.environ.get('ANTHROPIC_API_KEY') or (load_env_file(os.environ['N8N_ENV_FILE']).get('ANTHROPIC_API_KEY')
                                                       if os.environ.get('N8N_ENV_FILE') else '')
        if key and first_request:
            body = {k: v for k, v in first_request.items() if k != 'max_tokens'}
            req = urllib.request.Request('https://api.anthropic.com/v1/messages/count_tokens', data=json.dumps(body).encode(),
                                         headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    result = (r.status, json.loads(r.read().decode()).get('input_tokens'))
            except urllib.error.HTTPError as e:
                result = (e.code, e.read().decode()[:200])
            check('Real Claude API (count_tokens, free)', 'the captured request is accepted by Anthropic', result[0] == 200,
                  f'{result[0]}, {result[1]} input tokens' if result[0] == 200 else str(result))
        else:
            print('\n(count_tokens check skipped: no ANTHROPIC_API_KEY)')
    finally:
        mock.stop()

    ok = report.print()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
