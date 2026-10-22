"""Unit tests for the shared mocks and for setup/create_base.py (which runs against the mock Airtable).

Run from the repo root:  python -m unittest tests.mocks.test_mocks -v
Uses port 8790 (MOCK_TEST_PORT to change it). No real API is called.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'setup'))

from tests.mocks import (AIRTABLE_BASE_ID as BASE, AIRTABLE_TOKEN as TOKEN, ANTHROPIC_API_KEY, MockServer,
                         POSTMARK_SERVER_TOKEN, SHOPIFY_ADMIN_TOKEN, SHOPIFY_WEBHOOK_SECRET, sign_webhook, webhook_request)
from tests.mocks import formula as F
from tests.mocks.n8n import mock_env, prepare_workflow

PORT = int(os.environ.get('MOCK_TEST_PORT', '8790'))
MOCK = None
URL = f'http://127.0.0.1:{PORT}'
META = json.loads((ROOT / 'base' / 'seed' / '_meta.json').read_text(encoding='utf-8'))
ERROR_WF = ROOT / 'shared' / 'error-alert-slack.json'


def setUpModule():
    global MOCK
    MOCK = MockServer(port=PORT).start()


def tearDownModule():
    MOCK.stop()


def call(method, path, body=None, headers=None, raw=None, timeout=15):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {'Content-Type': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode(), e.code
        e.close()
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def at(method, path, body=None, token=TOKEN, **kw):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return call(method, f'/v0/{BASE}/{path}' if not path.startswith('/') else path, body, headers, **kw)


def gql(query, variables=None, token=SHOPIFY_ADMIN_TOKEN, version='2026-10'):
    return call('POST', f'/admin/api/{version}/graphql.json', {'query': query, 'variables': variables or {}},
                {'X-Shopify-Access-Token': token} if token else {})


class Base(unittest.TestCase):
    def setUp(self):
        MOCK.clock.live()
        MOCK.reset()                      # seed shifted to today (seed_today='auto')
        MOCK.airtable.set_rate_limit(enabled=False)

    def freeze(self, now):
        """Freeze the clock and reload the seed so it is shifted to that day."""
        MOCK.clock.set(now)
        MOCK.reset()
        MOCK.airtable.set_rate_limit(enabled=False)


# ================================================================================ formula engine

class FormulaTests(unittest.TestCase):
    def ev(self, text, fields=None, now='2026-10-20T12:00:00Z'):
        from tests.mocks.core import parse_iso
        fields = fields or {}
        node = F.parse(text, set(fields))
        return F.evaluate(node, F.Context(lambda n: fields[n], parse_iso(now), 'recX'))

    def test_text_equality_is_case_sensitive(self):
        self.assertTrue(self.ev("{S} = 'Approved'", {'S': 'Approved'}))
        self.assertFalse(self.ev("{S} = 'approved'", {'S': 'Approved'}))
        self.assertTrue(self.ev("LOWER({S}) = 'approved'", {'S': 'Approved'}))

    def test_blank_equals_empty_zero_and_unchecked(self):
        self.assertTrue(self.ev('{A} = BLANK()', {'A': F.BLANK}))
        self.assertTrue(self.ev("{A} = ''", {'A': F.BLANK}))
        self.assertTrue(self.ev('{C} = BLANK()', {'C': False}))
        self.assertTrue(self.ev('{N} = 0', {'N': F.BLANK}))
        self.assertFalse(self.ev('{A} = BLANK()', {'A': 'x'}))

    def test_logic_and_not_on_checkbox(self):
        self.assertTrue(self.ev("AND({S} = 'Approved', NOT({P}))", {'S': 'Approved', 'P': False}))
        self.assertFalse(self.ev("AND({S} = 'Approved', NOT({P}))", {'S': 'Approved', 'P': True}))
        self.assertTrue(self.ev("OR({S} = 'X', {P} = TRUE())", {'S': 'Approved', 'P': True}))

    def test_find_and_search_are_case_sensitive(self):
        self.assertEqual(self.ev("FIND('Nuts', {A})", {'A': 'Tree Nuts, Milk'}), 6)
        self.assertEqual(self.ev("FIND('nuts', {A})", {'A': 'Tree Nuts, Milk'}), 0)
        self.assertEqual(self.ev("SEARCH('Nuts', {A})", {'A': 'Tree Nuts, Milk'}), 6)
        self.assertIs(self.ev("SEARCH('nuts', {A})", {'A': 'Tree Nuts, Milk'}), F.BLANK)
        self.assertEqual(self.ev("SEARCH('nuts', LOWER({A}))", {'A': 'Tree Nuts, Milk'}), 6)
        self.assertIs(self.ev("SEARCH('Soy', {A})", {'A': 'Tree Nuts, Milk'}), F.BLANK)

    def test_arrayjoin_and_arrays_as_text(self):
        arr = F.Arr(['Tree Nuts', 'Milk'])
        self.assertEqual(self.ev('ARRAYJOIN({A})', {'A': arr}), 'Tree Nuts, Milk')
        self.assertEqual(self.ev("ARRAYJOIN({A}, '|')", {'A': arr}), 'Tree Nuts|Milk')
        self.assertTrue(self.ev("FIND('Milk', {A}) > 0", {'A': arr}))

    def test_dates(self):
        bb = F.parse_datetime('2026-11-03')
        self.assertTrue(self.ev('IS_AFTER({B}, TODAY())', {'B': bb}))
        self.assertEqual(self.ev("DATETIME_DIFF({B}, TODAY(), 'days')", {'B': bb}), 14)
        self.assertEqual(self.ev("DATETIME_DIFF(TODAY(), {B}, 'days')", {'B': bb}), -14)
        self.assertTrue(self.ev("IS_SAME({B}, '2026-11-03T18:00:00Z', 'day')", {'B': bb}))
        self.assertFalse(self.ev("IS_SAME({B}, '2026-11-04', 'day')", {'B': bb}))
        self.assertTrue(self.ev("IS_BEFORE(DATEADD(TODAY(), 14, 'days'), {B}) = 0", {'B': bb}))
        self.assertEqual(F.to_str(self.ev("DATETIME_FORMAT({B}, 'YYYY-MM')", {'B': bb})), '2026-11')
        self.assertEqual(self.ev("DATETIME_DIFF('2026-12-15', '2026-10-20', 'months')"), 1)

    def test_datetime_format_tokens_like_moment(self):
        d = {'D': F.parse_datetime('2026-10-20T09:05:03.000Z')}
        fmt = lambda f: F.to_str(self.ev(f"DATETIME_FORMAT({{D}}, '{f}')", d))
        self.assertEqual(fmt('GGGG-[W]WW'), '2026-W43')            # ISO week, as in Data Quality "Report Week"
        self.assertEqual(fmt('YYYY-MM-DD HH:mm:ss'), '2026-10-20 09:05:03')
        self.assertEqual(fmt('Do MMM YYYY, h:mm a'), '20th Oct 2026, 9:05 am')
        self.assertEqual(fmt('Q / DDDD / E / d'), '4 / 293 / 2 / 2')
        self.assertEqual(fmt('[Week] w'), 'Week 43')
        self.assertEqual(F.to_str(self.ev("DATETIME_FORMAT('2027-01-01', 'GGGG-[W]WW')")), '2026-W53')
        self.assertEqual(self.ev("WEEKDAY('2026-10-18')"), 0)                # a Sunday
        self.assertEqual(self.ev("WEEKDAY('2026-10-18', 'Monday')"), 6)
        self.assertEqual(self.ev("WEEKNUM('2026-01-03')"), 1)
        self.assertEqual(self.ev("WEEKNUM('2026-01-04')"), 2)                # first Sunday starts week 2
        self.assertEqual(self.ev("WEEKNUM('2026-01-04', 'Monday')"), 1)
        self.assertEqual(self.ev("WEEKNUM('2026-10-20')"), 43)

    def test_datetime_diff_truncates_partial_days(self):
        # delivered two calendar days ago at 17:45 UTC is only 1 whole day before TODAY() (midnight UTC)
        d = {'D': F.parse_datetime('2026-10-18T17:45:00.000Z')}
        self.assertEqual(self.ev("DATETIME_DIFF(TODAY(), {D}, 'days')", d), 1)
        self.assertEqual(self.ev("DATETIME_DIFF(TODAY(), DATETIME_PARSE(DATETIME_FORMAT({D}, 'YYYY-MM-DD')), 'days')", d), 2)
        self.assertTrue(self.ev("IS_SAME({D}, DATEADD(TODAY(), -2, 'days'), 'day')", d))

    def test_blank_dates_never_match_comparisons(self):
        self.assertFalse(self.ev('IS_BEFORE({B}, TODAY())', {'B': F.BLANK}))
        self.assertFalse(self.ev('{B} < TODAY()', {'B': F.BLANK}))

    def test_syntax_and_unknown_fields(self):
        with self.assertRaises(F.FormulaSyntaxError):
            F.parse("AND({A} = 'x'", {'A'})
        with self.assertRaises(F.FormulaSyntaxError):
            F.parse("EQUALS({A}, 1)", {'A'})
        with self.assertRaises(F.UnknownFieldsError) as ctx:
            F.parse("{Stauts} = 'Open'", {'Status'})
        self.assertEqual(ctx.exception.names, ['Stauts'])

    def test_evaluation_error_excludes_record(self):
        node = F.parse("{A} * 2 > 1", {'A'})
        self.assertFalse(F.matches(node, F.Context(lambda n: 'abc', None)))


# ================================================================================ Airtable Web API

class AirtableWebTests(Base):
    def test_auth_base_and_table_errors(self):
        self.assertEqual(at('GET', 'Products', token=None)[0], 401)
        self.assertEqual(at('GET', 'Products', token='patWrong.x')[0], 401)
        s, b = at('GET', f'/v0/appWrongBase000001/Products')
        self.assertEqual((s, b['error']['type']), (403, 'INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND'))
        s, b = at('GET', 'Prodcts')
        self.assertEqual(s, 403)

    def test_list_pagination_covers_every_record_once(self):
        seen, offset, pages = [], None, 0
        while True:
            s, b = at('GET', 'Box%20History' + (f'?offset={offset}' if offset else ''))
            self.assertEqual(s, 200)
            pages += 1
            self.assertLessEqual(len(b['records']), 100)
            seen += [r['id'] for r in b['records']]
            offset = b.get('offset')
            if not offset:
                break
        self.assertEqual(pages, 2)
        self.assertEqual(len(seen), MOCK.airtable.count('Box History'))
        self.assertEqual(len(set(seen)), len(seen))

    def test_page_size_limits_and_unknown_params(self):
        self.assertEqual(at('GET', 'Products?pageSize=101')[0], 422)
        s, b = at('GET', 'Products?pageSize=5')
        self.assertEqual((s, len(b['records']), 'offset' in b), (200, 5, True))
        s, b = at('GET', 'Products?filterbyformula=1')
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_REQUEST_UNKNOWN'))
        s, b = at('GET', 'Products?offset=itrNOPE/recNOPE')
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_OFFSET_VALUE'))
        offset = at('GET', 'Products?pageSize=5')[1]['offset']
        MOCK.airtable.expire_iterators()
        s, b = at('GET', 'Products?pageSize=5&offset=' + urllib.request.quote(offset))
        self.assertEqual((s, b['error']['type']), (422, 'LIST_RECORDS_ITERATOR_NOT_AVAILABLE'))
        s, b = at('GET', 'Products/recAAAAAAAAAAAAAA')
        self.assertEqual((s, b), (404, {'error': {'type': 'MODEL_ID_NOT_FOUND', 'message': 'Record not found'}}))

    def test_filter_by_formula_on_links_uses_primary_names(self):
        f = urllib.request.quote("FIND('Derek Olsen', ARRAYJOIN({Customer}))")
        s, b = at('GET', f'Orders?filterByFormula={f}')
        self.assertEqual(s, 200)
        self.assertEqual(sorted(r['fields']['Order Number'] for r in b['records']), ['#1005', '#1016', '#1021'])
        rid = MOCK.airtable.ref('Customers', 'derek')
        f = urllib.request.quote(f"FIND('{rid}', ARRAYJOIN({{Customer}}))")
        self.assertEqual(at('GET', f'Orders?filterByFormula={f}')[1]['records'], [])

    def test_bad_formula_and_unknown_field_in_formula(self):
        s, b = at('GET', 'Quotes?filterByFormula=' + urllib.request.quote("AND({Status}='Approved'"))
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_FILTER_BY_FORMULA'))
        s, b = at('GET', 'Quotes?filterByFormula=' + urllib.request.quote("{Stauts}='Approved'"))
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_FILTER_BY_FORMULA'))
        self.assertIn('Unknown field names: stauts', b['error']['message'])

    def test_approved_unprocessed_quote_poll_formula(self):
        f = urllib.request.quote("AND({Status} = 'Approved', NOT({Processed}))")
        s, b = at('GET', f'Quotes?filterByFormula={f}')
        self.assertEqual([r['fields']['Quote Number'] for r in b['records']], ['Q-2026-0042'])

    def test_fields_sort_and_field_ids(self):
        s, b = at('GET', 'Products?fields%5B%5D=SKU&fields%5B%5D=Price&sort%5B0%5D%5Bfield%5D=Price&sort%5B0%5D%5Bdirection%5D=desc&pageSize=3')
        self.assertEqual(s, 200)
        self.assertEqual(set().union(*[r['fields'].keys() for r in b['records']]), {'SKU', 'Price'})
        self.assertEqual(b['records'][0]['fields']['SKU'], 'DC-BOX-PRALINE-12')
        self.assertEqual(at('GET', 'Products?fields%5B%5D=Nope')[1]['error']['type'], 'UNKNOWN_FIELD_NAME')
        s, b = at('GET', 'Products?returnFieldsByFieldId=true&pageSize=1')
        self.assertTrue(all(k.startswith('fld') for k in b['records'][0]['fields']))

    def test_empty_values_are_left_out(self):
        rec = MOCK.airtable.record('Quotes', MOCK.airtable.ref('Quotes', 'q42'))
        self.assertNotIn('Processed', rec['fields'])           # unchecked checkbox
        self.assertNotIn('Draft Order ID', rec['fields'])      # empty text
        self.assertEqual(rec['fields']['Status'], 'Approved')

    def test_post_list_records_body(self):
        s, b = at('POST', 'Production%20Batches/listRecords', {
            'filterByFormula': "AND({Status} = 'Released', DATETIME_DIFF({Best Before}, TODAY(), 'days') <= 45)",
            'fields': ['Batch Code', 'Best Before'], 'sort': [{'field': 'Best Before', 'direction': 'asc'}]})
        self.assertEqual(s, 200)
        self.assertEqual(b['records'][0]['fields']['Batch Code'], 'B-DAL-260314')   # expired but still Released
        self.assertEqual(b['records'][0]['fields']['Best Before'], (MOCK.clock.today() - timedelta(days=5)).isoformat())
        self.assertEqual(at('POST', 'Products/listRecords', {'fields': 'SKU'})[0], 422)
        self.assertEqual(at('POST', 'Products/listRecords', {'filter': 'x'})[0], 422)

    def test_create_limits_and_validation(self):
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': f'r{i}'}} for i in range(11)]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_RECORDS'))
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'r1', 'Nope': 1}}]})
        self.assertEqual((s, b['error']['type'], b['error']['message']), (422, 'UNKNOWN_FIELD_NAME', 'Unknown field name: "Nope"'))
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'r1', 'Errors': '2'}}]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_VALUE_FOR_COLUMN'))
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'r1', 'Errors': '2'}}], 'typecast': True})
        self.assertEqual((s, b['records'][0]['fields']['Errors']), (200, 2))
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 7}}]})
        self.assertEqual(s, 422)
        before = MOCK.airtable.count('Sync Log')
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'ok'}}, {'fields': {'Run ID': 'bad', 'Status': 'Weird'}}]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_MULTIPLE_CHOICE_OPTIONS'))
        self.assertEqual(MOCK.airtable.count('Sync Log'), before)   # all or nothing

    def test_select_typecast_adds_option(self):
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'x', 'Status': 'Weird'}}], 'typecast': True})
        self.assertEqual(s, 200)
        names = [c['name'] for c in MOCK.airtable.table('Sync Log').field('Status').options['choices']]
        self.assertIn('Weird', names)

    def test_date_and_datetime_formats(self):
        s, b = at('POST', 'Promos', {'records': [{'fields': {'Promo Name': 'P1', 'Best Before': '2026-11-01T00:00:00.000Z'}}]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_VALUE_FOR_COLUMN'))
        s, b = at('POST', 'Promos', {'records': [{'fields': {'Promo Name': 'P1', 'Best Before': '2026-11-01',
                                                             'Created At': '2026-10-20T06:00:00-04:00'}}]})
        self.assertEqual(b['records'][0]['fields']['Created At'], '2026-10-20T10:00:00.000Z')
        s, b = at('POST', 'Promos', {'records': [{'fields': {'Promo Name': 'P2', 'Created At': '2026-10-20'}}]})
        self.assertEqual(s, 422)

    def test_links_need_existing_record_ids(self):
        s, b = at('POST', 'Order%20Lines', {'records': [{'fields': {'Line Item ID': 'X1', 'Order': ['recAAAAAAAAAAAAAA']}}]})
        self.assertEqual((s, b['error']['type']), (422, 'ROW_DOES_NOT_EXIST'))
        s, b = at('POST', 'Order%20Lines', {'records': [{'fields': {'Line Item ID': 'X1', 'Order': ['#1001']}}]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_VALUE_FOR_COLUMN'))
        s, b = at('POST', 'Order%20Lines', {'records': [{'fields': {'Line Item ID': 'X1', 'Order': ['#1001'],
                                                                   'Product': ['DC-BAR-NEW-ONE']}}], 'typecast': True})
        self.assertEqual(s, 200)
        self.assertEqual(b['records'][0]['fields']['Order'], [MOCK.airtable.ref('Orders', 'o1001')])
        self.assertEqual(MOCK.airtable.by_primary('Products', 'DC-BAR-NEW-ONE')['fields']['SKU'], 'DC-BAR-NEW-ONE')

    def test_inverse_links_stay_in_sync(self):
        order = MOCK.airtable.ref('Orders', 'o1001')
        s, b = at('POST', 'Order%20Lines', {'records': [{'fields': {'Line Item ID': 'X9', 'Order': [order]}}]})
        line = b['records'][0]['id']
        self.assertIn(line, MOCK.airtable.record('Orders', order)['fields']['Order Lines'])
        self.assertEqual(at('DELETE', f'Order%20Lines?records%5B%5D={line}')[0], 200)
        self.assertNotIn(line, MOCK.airtable.record('Orders', order)['fields']['Order Lines'])

    def test_upsert_creates_then_updates_without_duplicates(self):
        body = {'performUpsert': {'fieldsToMergeOn': ['Shopify Order ID']},
                'records': [{'fields': {'Order Number': '#1026', 'Shopify Order ID': '5200000001026', 'Total': 21.95}}]}
        s, b = at('PATCH', 'Orders', body)
        self.assertEqual((s, len(b['createdRecords']), len(b['updatedRecords'])), (200, 1, 0))
        body['records'][0]['fields']['Total'] = 14.45
        s, b2 = at('PATCH', 'Orders', body)
        self.assertEqual((len(b2['createdRecords']), b2['updatedRecords']), (0, b['createdRecords']))
        self.assertEqual(len(MOCK.airtable.find('Orders', **{'Order Number': '#1026'})), 1)
        self.assertEqual(MOCK.airtable.find('Orders', **{'Order Number': '#1026'})[0]['fields']['Total'], 14.45)

    def test_upsert_rules(self):
        # two existing records match the key: Airtable's own error body
        before = MOCK.airtable.count('Customers')
        s, b = at('PATCH', 'Customers', {'performUpsert': {'fieldsToMergeOn': ['Name']},
                                         'records': [{'fields': {'Name': 'Jordan Blake', 'Notes': 'x'}}]})
        self.assertEqual((s, b), (422, {'error': {'type': 'INVALID_VALUE_FOR_COLUMN',
                                                  'message': 'Cannot update more than one record for fields to merge on'}}))
        self.assertEqual(MOCK.airtable.count('Customers'), before)
        s, b = at('PATCH', 'Sync%20Log', {'performUpsert': {'fieldsToMergeOn': ['Run ID']},
                                          'records': [{'fields': {'Run ID': 'a'}}, {'fields': {'Run ID': 'a'}}]})
        self.assertEqual(s, 422)                                    # the same key twice in one request
        s, b = at('PATCH', 'Customers', {'performUpsert': {'fieldsToMergeOn': ['Orders']},
                                         'records': [{'fields': {'Name': 'a'}}]})
        self.assertEqual(s, 422)                                    # links cannot be merge keys
        s, b = at('PATCH', 'Sync%20Log', {'performUpsert': {'fieldsToMergeOn': ['Run ID']},
                                          'records': [{'fields': {'Details': 'No Run ID'}}]})
        self.assertEqual(s, 422)                                    # every record needs the key
        s, b = at('PATCH', 'Customers', {'records': [{'fields': {'Name': 'No id'}}]})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_RECORDS'))
        s, b = at('PATCH', 'Sync%20Log', {'performUpsert': {'fieldsToMergeOn': ['Run ID', 'Automation', 'Status', 'Trigger']},
                                          'records': [{'fields': {'Run ID': 'a'}}]})
        self.assertEqual(s, 422)                                    # at most 3 merge fields

    def test_upsert_merge_field_types_are_the_documented_ones(self):
        # number, single line text, long text, single select, multiple select, date: allowed
        for table, field, value in (('Sync Log', 'Run ID', 'run-x'), ('Sync Log', 'Errors', 7), ('Sync Log', 'Details', 'long'),
                                    ('Sync Log', 'Status', 'Partial'), ('Promos', 'Best Before', '2027-01-02'),
                                    ('Products', 'Flavor Notes', ['Mint'])):
            body = {'performUpsert': {'fieldsToMergeOn': [field]}, 'records': [{'fields': {field: value}}]}
            s, b = at('PATCH', urllib.request.quote(table), body)
            self.assertEqual(s, 200, (table, field, b))
        # email, currency, dateTime, checkbox, link: refused, so a test can never pass on a key Airtable may reject
        for table, field, value in (('Customers', 'Email', 'new.person@example.com'), ('Orders', 'Total', 10.0),
                                    ('Orders', 'Order Date', '2026-10-20T10:00:00.000Z'), ('Quotes', 'Processed', True),
                                    ('Quotes', 'Invoice URL', 'https://example.com/i'), ('Customers', 'Phone', '+15550100000')):
            body = {'performUpsert': {'fieldsToMergeOn': [field]}, 'records': [{'fields': {field: value}}]}
            s, b = at('PATCH', urllib.request.quote(table), body)
            self.assertEqual((s, b['error']['type']), (422, 'INVALID_REQUEST_UNKNOWN'), (table, field))
            self.assertIn('cannot be used in fieldsToMergeOn', b['error']['message'])
        self.assertEqual(MOCK.airtable.find('Customers', Email='new.person@example.com'), [])

    def test_zero_is_a_value_and_is_returned(self):
        rid = MOCK.airtable.ref('Production Batches', 'b-dss-260523')
        s, b = at('GET', f'Production%20Batches/{rid}')
        self.assertEqual((b['fields']['Quantity Reserved'], b['fields']['Quantity Sold']), (0, 170))
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'zero', 'Errors': 0, 'Items Changed': 0.0}}]})
        self.assertEqual((b['records'][0]['fields']['Errors'], b['records'][0]['fields']['Items Changed']), (0, 0))
        gift = MOCK.airtable.by_primary('Order Lines', '14100000100809')['fields']
        self.assertEqual((gift['Unit Price'], gift['Line Total']), (0, 0))
        f = urllib.request.quote("{Quantity Reserved} = 0")
        n = len(at('GET', f'Production%20Batches?filterByFormula={f}')[1]['records'])
        self.assertEqual(n, 28)                                     # 31 batches, 3 with units reserved
        s, b = at('PATCH', 'Sync%20Log', {'records': [{'id': MOCK.airtable.by_primary('Sync Log', 'zero')['id'],
                                                       'fields': {'Errors': None}}]})
        self.assertNotIn('Errors', b['records'][0]['fields'])       # null clears it, and an empty cell is left out
        s, b = at('GET', 'Products?fields%5B%5D=SKU&fields%5B%5D=Needs%20Review&pageSize=3')
        self.assertTrue(all('Needs Review' not in r['fields'] for r in b['records']))   # unticked is left out

    def test_long_get_url_answers_414_and_post_list_records_works(self):
        formula = 'OR(' + ', '.join(f"{{SKU}} = 'DC-NOPE-{i:04d}'" for i in range(700)) + ", {SKU} = 'DC-BAR-DARK-85')"
        s, b = at('GET', 'Products?filterByFormula=' + urllib.request.quote(formula))
        self.assertEqual((s, b['error']['type']), (414, 'URL_TOO_LONG'))
        s, b = at('POST', 'Products/listRecords', {'filterByFormula': formula, 'fields': ['SKU']})
        self.assertEqual((s, [r['fields'] for r in b['records']]), (200, [{'SKU': 'DC-BAR-DARK-85'}]))
        short = "OR({SKU} = 'DC-BAR-DARK-85', {SKU} = 'DC-BAR-DARK-MINT')"
        self.assertEqual(len(at('GET', 'Products?filterByFormula=' + urllib.request.quote(short))[1]['records']), 2)
        # injected failures carry Airtable's shapes too
        MOCK.fail('airtable', status=404)
        self.assertEqual(at('GET', 'Products?pageSize=1'), (404, {'error': 'NOT_FOUND'}))
        MOCK.fail('airtable', status=503)
        self.assertEqual(at('GET', 'Products?pageSize=1')[1]['error']['type'], 'RETRIABLE_ERROR')

    def test_batch_limit_messages_match_airtable(self):
        s, b = at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': f'r{i}'}} for i in range(11)]})
        self.assertEqual(b['error'], {'type': 'INVALID_RECORDS',
                                      'message': 'A maximum of 10 records can be created per request but you have provided 11.'})
        rid = MOCK.airtable.ref('Quotes', 'q42')
        s, b = at('PATCH', 'Quotes', {'records': [{'id': rid, 'fields': {}}] * 12})
        self.assertEqual(b['error']['message'], 'A maximum of 10 records can be updated per request but you have provided 12.')
        ids = '&'.join(f'records%5B%5D=rec{i:014d}' for i in range(11))
        self.assertEqual(at('DELETE', f'Quotes?{ids}')[1]['error']['message'],
                         'A maximum of 10 records can be deleted per request but you have provided 11.')
        s, b = at('PATCH', 'Quotes', {'records': [{'id': rid, 'fields': {}}] * 10})
        self.assertEqual((s, len(b['records'])), (200, 10))

    def test_update_put_and_unknown_ids(self):
        rid = MOCK.airtable.ref('Quotes', 'q42')
        s, b = at('PATCH', 'Quotes', {'records': [{'id': rid, 'fields': {'Processed': True}}]})
        self.assertEqual((s, b['records'][0]['fields']['Processed'], b['records'][0]['fields']['Status']), (200, True, 'Approved'))
        lines = MOCK.airtable.record('Quotes', rid)['fields']['Quote Lines']
        s, b = at('PUT', f'Quotes/{rid}', {'fields': {'Quote Number': 'Q-2026-0042'}})
        self.assertEqual(set(b['fields']), {'Quote Number'})   # PUT clears every other cell, links included
        self.assertTrue(all('Quote' not in MOCK.airtable.record('Quote Lines', l)['fields'] for l in lines))
        self.assertEqual(at('PATCH', 'Quotes', {'records': [{'id': 'recAAAAAAAAAAAAAA', 'fields': {}}]})[0], 404)
        self.assertEqual(at('GET', 'Quotes/recAAAAAAAAAAAAAA')[1]['error']['type'], 'MODEL_ID_NOT_FOUND')
        ids = '&'.join(f'records%5B%5D=rec{i:014d}' for i in range(11))
        self.assertEqual(at('DELETE', f'Quotes?{ids}')[1]['error']['type'], 'INVALID_RECORDS')

    def test_today_follows_the_mock_clock(self):
        formula = urllib.request.quote("IS_BEFORE({Best Before}, TODAY())")
        self.freeze(META['seedToday'] + 'T12:00:00Z')
        self.assertEqual(MOCK.seed_shift_days, 0)
        n_today = len(at('GET', f'Production%20Batches?filterByFormula={formula}')[1]['records'])
        MOCK.clock.advance(days=16)
        n_later = len(at('GET', f'Production%20Batches?filterByFormula={formula}')[1]['records'])
        self.assertEqual(n_today, 1)            # only the expired almond batch
        self.assertEqual(n_later, 3)            # plus the sea salt (15 days) and dark 85 (10 days) batches

    def test_seed_is_shifted_to_the_live_today_by_default(self):
        today = MOCK.clock.today()
        self.assertEqual(MOCK.seed_shift_days, (today - date.fromisoformat(META['seedToday'])).days)
        rec = MOCK.airtable.by_primary('Production Batches', 'B-DSS-260523')
        self.assertEqual(rec['fields']['Best Before'], (today + timedelta(days=15)).isoformat())
        self.assertEqual(MOCK.seed_date('2026-11-04'), (date(2026, 11, 4) + timedelta(days=MOCK.seed_shift_days)).isoformat())
        order = MOCK.airtable.by_primary('Orders', '#1012')['fields']
        delivered = date.fromisoformat(order['Delivered At'][:10])
        self.assertEqual((today - delivered).days, 2)
        gid = 'gid://shopify/Order/' + order['Shopify Order ID']
        self.assertEqual(MOCK.shopify.orders[gid]['createdAt'], order['Order Date'])

    def test_rate_limit_429_then_30_second_block(self):
        MOCK.airtable.set_rate_limit(per_second=5, penalty=30)
        codes = [at('GET', 'Products?pageSize=1')[0] for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])
        time.sleep(1.1)
        s, b = at('GET', 'Products?pageSize=1')
        self.assertEqual((s, b), (429, {'error': {'type': 'RATE_LIMIT_REACHED',
                                                  'message': 'Rate limit exceeded. Please try again later'}}))
        MOCK.airtable.unblock()
        self.assertEqual(at('GET', 'Products?pageSize=1')[0], 200)
        MOCK.fail('airtable', status=429)
        self.assertEqual(at('GET', 'Products?pageSize=1')[1]['error']['type'], 'RATE_LIMIT_REACHED')

    def test_paced_requests_pass(self):
        MOCK.airtable.set_rate_limit(per_second=5, penalty=30)
        codes = []
        for _ in range(12):
            codes.append(at('GET', 'Products?pageSize=1')[0])
            time.sleep(0.25)
        self.assertEqual(set(codes), {200})

    def test_seed_dates_can_shift_to_any_today(self):
        target = date.fromisoformat(META['seedToday']) + timedelta(days=30)
        MOCK.reset(seed_today=target.isoformat())
        try:
            rec = MOCK.airtable.by_primary('Production Batches', 'B-DSS-260523')
            self.assertEqual(rec['fields']['Best Before'], (target + timedelta(days=15)).isoformat())
            MOCK.reset(seed_today=None)
            rec = MOCK.airtable.by_primary('Production Batches', 'B-DSS-260523')
            self.assertEqual(rec['fields']['Best Before'], (date.fromisoformat(META['seedToday']) + timedelta(days=15)).isoformat())
        finally:
            MOCK.reset(seed_today='auto')


# ================================================================================ seed facts, recomputed

class SeedFactTests(Base):
    """base/seed/_meta.json "expected" recomputed from the loaded (shifted) base, independently of the generator."""

    def sku(self, rec, field='Product'):
        return [MOCK.airtable.primary_name('Products', i) for i in rec['fields'].get(field, [])]

    def sellable(self):
        today, out = MOCK.clock.today(), {}
        for b in MOCK.airtable.records('Production Batches'):
            f = b['fields']
            sku = self.sku(b)[0]
            out.setdefault(sku, 0)
            if f.get('Status') == 'Released' and date.fromisoformat(f['Produced On']) <= today \
                    and (date.fromisoformat(f['Best Before']) - today).days > 14:
                out[sku] += max(f.get('Quantity Made', 0) - f.get('Quantity Reserved', 0) - f.get('Quantity Sold', 0), 0)
        return out

    def test_inventory_facts(self):
        e = META['expected']['02-inventory-sync']
        sellable = self.sellable()
        self.assertEqual({k: sellable.get(k, 0) for k in e['sellableBySku']}, e['sellableBySku'])
        products = {p['fields']['SKU']: p['fields'] for p in MOCK.airtable.records('Products')}
        changed = {s: {'from': f['Shopify Stock'], 'to': sellable.get(s, 0)} for s, f in products.items()
                   if f.get('Shopify Inventory Item ID') and f.get('Shopify Stock') != sellable.get(s, 0)}
        self.assertEqual(changed, e['changedSkus'])
        for s, f in products.items():
            if f.get('Shopify Inventory Item ID'):
                self.assertEqual(MOCK.shopify.available(s), f['Shopify Stock'], s)   # both sides agree before a sync
        self.assertEqual(sorted(s for s, f in products.items() if not f.get('Shopify Inventory Item ID')), e['notOnShopify'])
        below = sorted(s for s, f in products.items() if f.get('Status') == 'Active' and f.get('Reorder Point') is not None
                       and sellable.get(s, 0) < f['Reorder Point'])
        self.assertEqual(below, e['belowReorderPoint'])
        formula = ("AND({Status} = 'Released', NOT(IS_AFTER({Produced On}, TODAY())), "
                   "DATETIME_DIFF({Best Before}, TODAY(), 'days') > 14, DATETIME_DIFF({Best Before}, TODAY(), 'days') <= 45)")
        promo = sorted(r['fields']['Batch Code'] for r in MOCK.airtable.where('Production Batches', formula))
        self.assertEqual(promo, sorted(p['batch'] for p in e['promoBatches']))

    def test_subscriber_allowed_bars(self):
        e = META['expected']['04-subscription-boxes']
        products = {p['fields']['SKU']: p['fields'] for p in MOCK.airtable.records('Products')}
        bars = sorted(s for s, f in products.items() if f.get('Status') == 'Active' and f.get('Subscription Eligible') and f.get('Category') == 'Bar')
        self.assertEqual(bars, sorted(e['subscriptionBars']))
        rules = {'Nut-Free': {'Tree Nuts', 'Peanuts'}, 'Dairy-Free': {'Milk'}, 'Gluten-Free': {'Wheat'}}
        for s in MOCK.airtable.records('Subscribers'):
            f = s['fields']
            allowed = []
            for sku in bars:
                p = products[sku]
                risky = set(p.get('Contains Allergens', [])) | set(p.get('May Contain Traces', []))
                bad = any(('Vegan' not in p.get('Diet Labels', [])) if n == 'Vegan' else bool(rules[n] & risky)
                          for n in f.get('Dietary Needs', []))
                if not bad and not set(f.get('Flavor Dislikes', [])) & set(p.get('Flavor Notes', [])):
                    allowed.append(sku)
            self.assertEqual(sorted(allowed), sorted(e['subscribers'][f['Subscriber ID']]['allowedSkus']), f['Subscriber ID'])
        for sku in e['nutTraceTraps']:                 # a trace only, never an ingredient
            self.assertIn('Tree Nuts', products[sku]['May Contain Traces'])
            self.assertFalse({'Tree Nuts', 'Peanuts'} & set(products[sku].get('Contains Allergens', [])))
        for sku, f in products.items():                # labels never contradict ingredients
            contains = set(f.get('Contains Allergens', []))
            if 'Vegan' in f.get('Diet Labels', []) or 'Dairy-Free' in f.get('Diet Labels', []):
                self.assertNotIn('Milk', contains, sku)
            if 'Gluten-Free' in f.get('Diet Labels', []):
                self.assertNotIn('Wheat', contains, sku)
            if f.get('Chocolate Type') in ('Milk', 'White', 'Ruby') and f.get('Status') == 'Active':
                self.assertIn('Milk', contains, sku)

    def test_customer_stats_and_quality_traps(self):
        e = META['expected']['06-airtable-scripts']
        ltv, count = {}, {}
        for o in MOCK.airtable.records('Orders'):
            f = o['fields']
            if f.get('Order Status') == 'Cancelled':
                continue
            for c in f.get('Customer', []):
                ltv[c] = round(ltv.get(c, 0) + f.get('Total', 0) - f.get('Refunded Amount', 0), 2)
                count[c] = count.get(c, 0) + 1
        ref = MOCK.airtable.ref
        self.assertEqual(ltv, {ref('Customers', r): v for r, v in e['lifetimeValueByCustomerRef'].items()})
        self.assertEqual(count, {ref('Customers', r): v for r, v in e['orderCountByCustomerRef'].items()})
        everyone = {c['id'] for c in MOCK.airtable.records('Customers')}
        self.assertEqual(everyone - set(ltv), {ref('Customers', r) for r in e['customersWithoutCountedOrders']})
        for email, refs in e['duplicateGroups'].items():
            found = MOCK.airtable.where('Customers', f"LOWER(TRIM({{Email}})) = '{email}'")
            self.assertEqual(sorted(r['id'] for r in found), sorted(ref('Customers', x) for x in refs), email)
        self.assertEqual(len(MOCK.airtable.where('Customers', "{Email} = 'jordan.blake@example.com'")), 2)

    def test_delivery_dates_are_relative_to_today(self):
        today = MOCK.clock.today()
        o = {r['fields']['Order Number']: r['fields'] for r in MOCK.airtable.records('Orders')}
        ago = lambda v: (today - date.fromisoformat(v[:10])).days
        self.assertEqual((ago(o['#1012']['Delivered At']), o['#1012']['Cold Pack'], o['#1012'].get('Cold Pack Shipped')), (2, 'Required', None))
        self.assertEqual((date.fromisoformat(o['#1015']['Delivered At'][:10]) - date.fromisoformat(o['#1015']['Expected Delivery'])).days, 3)
        self.assertEqual((ago(o['#1009']['Delivered At']), o['#1009']['Cold Pack Shipped']), (1, True))
        self.assertEqual(ago(o['#1003']['Delivered At']), 20)
        self.assertEqual((o['#1022'].get('Delivered At'), (today - date.fromisoformat(o['#1022']['Expected Delivery'])).days), (None, 3))
        claims = MOCK.airtable.where('Tickets', "AND(LOWER({Customer Email}) = 'derek.olsen@example.com', {Resolution} = 'Replacement Approved')")
        self.assertEqual(len(claims), 2)


# ================================================================================ Airtable Metadata API

class AirtableMetaTests(Base):
    def test_schema_matches_blueprint(self):
        s, b = at('GET', f'/v0/meta/bases/{BASE}/tables')
        spec = json.loads((ROOT / 'base' / 'schema.json').read_text(encoding='utf-8'))
        self.assertEqual([t['name'] for t in b['tables']], [t['name'] for t in spec['tables']])
        orders = next(t for t in b['tables'] if t['name'] == 'Orders')
        self.assertEqual(orders['primaryFieldId'], orders['fields'][0]['id'])
        cust = next(f for f in orders['fields'] if f['name'] == 'Customer')
        self.assertTrue(cust['options']['inverseLinkFieldId'].startswith('fld'))

    def test_create_table_and_field_rules(self):
        path = f'/v0/meta/bases/{BASE}/tables'
        self.assertEqual(at('POST', path, {'name': 'Orders', 'fields': [{'name': 'A', 'type': 'singleLineText'}]})[1]['error']['type'],
                         'DUPLICATE_TABLE_NAME')
        products = MOCK.airtable.table('Products').id
        s, b = at('POST', path, {'name': 'Bad', 'fields': [{'name': 'L', 'type': 'multipleRecordLinks', 'options': {'linkedTableId': products}}]})
        self.assertEqual(s, 422)
        s, t = at('POST', path, {'name': 'Reviews', 'fields': [{'name': 'Title', 'type': 'singleLineText'}]})
        self.assertEqual(s, 200)
        fpath = f'{path}/{t["id"]}/fields'
        s, b = at('POST', fpath, {'name': 'Score', 'type': 'formula', 'options': {'formula': '1+1'}})
        self.assertEqual((s, b['error']['type']), (422, 'UNSUPPORTED_FIELD_TYPE_FOR_CREATE'))
        s, b = at('POST', fpath, {'name': 'Stars', 'type': 'number'})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_FIELD_TYPE_OPTIONS_FOR_CREATE'))
        s, b = at('POST', fpath, {'name': 'title', 'type': 'singleLineText'})
        self.assertEqual(b['error']['type'], 'DUPLICATE_OR_EMPTY_FIELD_NAME')
        s, f = at('POST', fpath, {'name': 'Product', 'type': 'multipleRecordLinks', 'options': {'linkedTableId': products}})
        self.assertEqual(s, 200)
        inverse = MOCK.airtable.table('Products').field(f['options']['inverseLinkFieldId'])
        self.assertEqual(inverse.name, 'Reviews')
        s, f2 = at('PATCH', f'{path}/{products}/fields/{inverse.id}', {'name': 'Product Reviews'})
        self.assertEqual((s, f2['name']), (200, 'Product Reviews'))
        # attachments take no write options; prefersSingleRecordLink cannot be set through the API
        s, b = at('POST', fpath, {'name': 'Photos', 'type': 'multipleAttachments', 'options': {'isReversed': True}})
        self.assertEqual((s, b['error']['type']), (422, 'INVALID_FIELD_TYPE_OPTIONS_FOR_CREATE'))
        s, b = at('POST', fpath, {'name': 'Photos', 'type': 'multipleAttachments'})
        self.assertEqual((s, b['options']), (200, {'isReversed': False}))
        s, b = at('POST', fpath, {'name': 'Order', 'type': 'multipleRecordLinks',
                                  'options': {'linkedTableId': MOCK.airtable.table('Orders').id, 'prefersSingleRecordLink': True}})
        self.assertEqual(s, 422)
        s, b = at('POST', fpath, {'name': 'Done', 'type': 'checkbox'})
        self.assertEqual(s, 422)                                    # checkbox needs icon and color
        s, b = at('POST', fpath, {'name': 'Done', 'type': 'checkbox', 'options': {'icon': 'check', 'color': 'greenBright'}})
        self.assertEqual(s, 200)
        # barcode cells can be written, but the field type cannot be created through the API
        s, b = at('POST', fpath, {'name': 'Code', 'type': 'barcode'})
        self.assertEqual((s, b['error']['type']), (422, 'UNSUPPORTED_FIELD_TYPE_FOR_CREATE'))
        # select choices may carry an optional id (field model, write format)
        s, b = at('POST', fpath, {'name': 'Mood', 'type': 'singleSelect',
                                  'options': {'choices': [{'id': 'selAAAAAAAAAAAAAA', 'name': 'Happy', 'color': 'greenLight2'}, {'name': 'Sad'}]}})
        self.assertEqual((s, [c['name'] for c in b['options']['choices']]), (200, ['Happy', 'Sad']))
        s, b = at('POST', fpath, {'name': 'Mood 2', 'type': 'singleSelect', 'options': {'choices': [{'name': 'A', 'color': 'neon'}]}})
        self.assertEqual(s, 422)


# ================================================================================ setup/create_base.py

class CreateBaseTests(unittest.TestCase):
    def test_creates_the_base_then_changes_nothing_on_rerun(self):
        import create_base
        MOCK.reset(empty_base=True)
        MOCK.airtable.set_rate_limit(per_second=5, penalty=30)
        os.environ['AIRTABLE_TOKEN'] = TOKEN
        state = Path(tempfile.mkdtemp()) / 'state.json'
        args = ['--api-url', URL, '--base-id', BASE, '--state-file', str(state)]
        try:
            dry = create_base.main(args + ['--dry-run'], log=lambda s: None)
            self.assertEqual(len(MOCK.airtable.tables), 0)
            first = create_base.main(args, log=lambda s: None)
            self.assertEqual(MOCK.log('airtable', status=429), [])
            self.assertEqual(first['schema']['warnings'], [])
            self.assertEqual(first['seed']['created'], sum(len(json.loads((ROOT / 'base' / 'seed' / n).read_text(encoding='utf-8'))['records'])
                                                           for n in META['loadOrder']))
            self.assertEqual(dry['writes'], first['writes'])
            reference = MockServer(port=0)          # provisioned straight from schema.json, never started
            for t in reference.airtable.tables:
                built = MOCK.airtable.table(t.name)
                self.assertIsNotNone(built, t.name)
                self.assertEqual(built.primary.name, t.primary.name)
                want = {f.name: (f.type, _comparable(reference.airtable, f)) for f in t.fields}
                got = {f.name: (f.type, _comparable(MOCK.airtable, f)) for f in built.fields}
                self.assertEqual(got, want, t.name)
                self.assertEqual(len(built.records), len(t.records), t.name)
            derek = MOCK.airtable.find('Customers', Name='Derek Olsen')[0]
            orders = sorted(MOCK.airtable.primary_name('Orders', r) for r in derek['fields']['Orders'])
            self.assertEqual(orders, ['#1005', '#1016', '#1021'])
            second = create_base.main(args, log=lambda s: None)
            self.assertEqual((second['writes'], second['seed']['created']), (0, 0))
            again = create_base.main(args + ['--dry-run'], log=lambda s: None)
            self.assertEqual(again['writes'], 0)                   # a dry run on the finished base plans nothing
        finally:
            MOCK.reset(empty_base=False)

    def test_existing_records_without_state_are_kept_and_matched(self):
        import create_base
        MOCK.reset(empty_base=True)
        MOCK.airtable.set_rate_limit(enabled=False)
        os.environ['AIRTABLE_TOKEN'] = TOKEN
        state = Path(tempfile.mkdtemp()) / 'state.json'
        args = ['--api-url', URL, '--base-id', BASE, '--state-file', str(state), '--pace', '0']
        try:
            create_base.main(args + ['--no-seed'], log=lambda s: None)
            MOCK.airtable.insert('Products', {'SKU': 'DC-BAR-DARK-SEASALT', 'Name': 'Added by hand'})
            MOCK.airtable.insert('Products', {'SKU': 'DC-BAR-DARK-85', 'Name': 'Added by hand'})
            out = create_base.main(args, log=lambda s: None)
            self.assertEqual(MOCK.airtable.count('Products'), 2)                    # left alone, no duplicates
            self.assertEqual(len(out['seed']['skipped_tables']), 1)
            self.assertIn('2 of 23 seed records matched by SKU', out['seed']['skipped_tables'][0])
            dss = MOCK.airtable.by_primary('Products', 'DC-BAR-DARK-SEASALT')
            self.assertEqual(sorted(MOCK.airtable.primary_name('Production Batches', r) for r in dss['fields']['Production Batches']),
                             ['B-DSS-260523', 'B-DSS-260910', 'B-DSS-261026'])
            self.assertTrue(out['seed']['warnings'])                               # links to the missing products
            self.assertTrue(all('not found in the existing records' in w for w in out['seed']['warnings']))
        finally:
            MOCK.reset(empty_base=False)


def _comparable(airtable, f):
    o = dict(f.options)
    if f.type in ('singleSelect', 'multipleSelects'):
        return [(c['name'], c.get('color')) for c in o.get('choices', [])]
    if f.type == 'multipleRecordLinks':
        target = airtable.table(o['linkedTableId'])
        inv = target.field(o['inverseLinkFieldId']).name if o.get('inverseLinkFieldId') else None
        return (target.name, inv)
    return json.dumps(o, sort_keys=True)


# ================================================================================ Shopify

INVENTORY_QUERY = '''query($q: String!, $loc: ID!) {
  productVariants(first: 5, query: $q) { nodes { sku inventoryItem { id inventoryLevel(locationId: $loc) {
    quantities(names: ["available", "on_hand"]) { name quantity } } } } }
}'''
SET_MUTATION = '''mutation Set($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { reason referenceDocumentUri changes { name delta quantityAfterChange } }
    userErrors { code field message }
  }
}'''


class ShopifyTests(Base):
    def setUp(self):
        super().setUp()
        self.loc = MOCK.shopify.primary_location
        self.item = MOCK.shopify.item_by_sku('DC-BAR-DARK-SEASALT')['id']

    def set_input(self, **q):
        entry = {'inventoryItemId': self.item, 'locationId': self.loc, 'quantity': 200}
        entry.update(q)
        return {'name': 'available', 'reason': 'correction', 'referenceDocumentUri': 'gid://demo-chocolate/SyncRun/1',
                'quantities': [entry]}

    def test_auth_and_versions(self):
        self.assertEqual(gql('{ shop { name } }', token=None)[0], 401)
        self.assertEqual(gql('{ shop { name } }', version='2025-01')[0], 404)
        s, b = gql('{ shop { name myshopifyDomain } }')
        self.assertEqual(b['data']['shop'], {'name': 'Demo Chocolate Co.', 'myshopifyDomain': 'demo-chocolate.myshopify.com'})
        self.assertEqual(set(b['extensions']['cost']['throttleStatus']), {'maximumAvailable', 'currentlyAvailable', 'restoreRate'})

    def test_inventory_query_selects_only_requested_fields(self):
        s, b = gql(INVENTORY_QUERY, {'q': 'sku:DC-BAR-DARK-SEASALT', 'loc': self.loc})
        node = b['data']['productVariants']['nodes'][0]
        self.assertEqual(set(node), {'sku', 'inventoryItem'})
        self.assertEqual(node['inventoryItem']['inventoryLevel']['quantities'],
                         [{'name': 'available', 'quantity': 180}, {'name': 'on_hand', 'quantity': 180}])
        s, b = gql('{ productVariants(first: 250, query: "sku:DC-BAR-DARK-SEASALT OR sku:DC-BAR-DARK-85") { nodes { sku } } }')
        self.assertEqual(sorted(n['sku'] for n in b['data']['productVariants']['nodes']), ['DC-BAR-DARK-85', 'DC-BAR-DARK-SEASALT'])

    def test_validation_errors_like_shopify(self):
        s, b = gql('{ shop { name nope } }')
        self.assertEqual((s, 'data' in b, b['errors'][0]['extensions']['code']), (200, False, 'undefinedField'))
        self.assertEqual(b['errors'][0]['message'], "Field 'nope' doesn't exist on type 'Shop'")
        s, b = gql('{ productVariants(sku: "x") { nodes { sku } } }')
        self.assertEqual(b['errors'][0]['extensions']['code'], 'argumentNotAccepted')
        s, b = gql('{ productVariants { nodes { sku } } }')
        self.assertEqual((s, b['data'], len(b['errors'])), (200, None, 1))     # non-null root field: data is null
        self.assertEqual((b['errors'][0]['message'], b['errors'][0]['path']), ('you must provide one of first or last', ['productVariants']))
        self.assertNotIn('mock_crash', MOCK.log('shopify')[-1])
        s, b = gql('{ shop { name } productVariants(first: 300) { nodes { sku } } }')
        self.assertEqual((b['data'], b['errors'][0]['message']), (None, 'The first and last arguments must be less than or equal to 250'))
        s, b = gql('{ shop { name }')
        self.assertIn('Parse error', b['errors'][0]['message'])
        s, b = gql('query($x: Int) { shop { name } }')
        self.assertEqual(b['errors'][0]['extensions']['code'], 'variableNotUsed')

    def test_inventory_set_needs_idempotent_directive(self):
        q = SET_MUTATION.replace(' @idempotent(key: $key)', '').replace(', $key: String!', '')
        s, b = gql(q, {'input': self.set_input(changeFromQuantity=None)})
        self.assertIsNone(b['data']['inventorySetQuantities'])
        self.assertIn('@idempotent', b['errors'][0]['message'])
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 180)

    def test_inventory_set_applies_and_records(self):
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=None), 'key': 'k1'})
        res = b['data']['inventorySetQuantities']
        self.assertEqual(res['userErrors'], [])
        self.assertEqual(res['inventoryAdjustmentGroup']['changes'],
                         [{'name': 'available', 'delta': 20, 'quantityAfterChange': 200},
                          {'name': 'on_hand', 'delta': 20, 'quantityAfterChange': 200}])
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 200)
        self.assertEqual(len(MOCK.shopify.adjustments), 1)

    def test_inventory_set_removed_and_missing_fields(self):
        inp = self.set_input(changeFromQuantity=None)
        inp['ignoreCompareQuantity'] = True
        s, b = gql(SET_MUTATION, {'input': inp, 'key': 'k2'})
        self.assertNotIn('data', b)
        self.assertIn('ignoreCompareQuantity (Field is not defined on InventorySetQuantitiesInput)', b['errors'][0]['message'])
        s, b = gql(SET_MUTATION, {'input': self.set_input(compareQuantity=180), 'key': 'k3'})
        self.assertIn('Field is not defined on InventoryQuantityInput', b['errors'][0]['message'])
        s, b = gql(SET_MUTATION, {'input': self.set_input(), 'key': 'k4'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'CHANGE_FROM_QUANTITY_STALE')
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=150), 'key': 'k5'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'CHANGE_FROM_QUANTITY_STALE')
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=180), 'key': 'k6'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'], [])
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 200)

    def test_inventory_set_user_errors(self):
        inp = self.set_input(changeFromQuantity=None)
        inp['reason'] = 'restock_please'
        inp['name'] = 'committed'
        inp['quantities'][0]['locationId'] = 'gid://shopify/Location/71000000002'
        s, b = gql(SET_MUTATION, {'input': inp, 'key': 'k7'})
        codes = [e['code'] for e in b['data']['inventorySetQuantities']['userErrors']]
        self.assertEqual(codes, ['INVALID_NAME', 'INVALID_REASON', 'INVALID_LOCATION'])
        inp = self.set_input(changeFromQuantity=None, quantity=-1)
        inp['referenceDocumentUri'] = 'not a uri'
        s, b = gql(SET_MUTATION, {'input': inp, 'key': 'k8'})
        codes = [e['code'] for e in b['data']['inventorySetQuantities']['userErrors']]
        self.assertEqual(codes, ['INVALID_REFERENCE_DOCUMENT', 'INVALID_QUANTITY_NEGATIVE'])

    def test_idempotency_key_replay_and_mismatch(self):
        gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=None), 'key': 'same'})
        MOCK.shopify.set_available('DC-BAR-DARK-SEASALT', 50)
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=None), 'key': 'same'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'], [])
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 50)       # not applied again
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=None, quantity=10), 'key': 'same'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'IDEMPOTENCY_KEY_PARAMETER_MISMATCH')

    def test_failed_attempt_uses_up_the_idempotency_key(self):
        # the first attempt fails the compare and swap: the key is spent, as Shopify documents with
        # IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED, so a corrected retry needs a new key
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=150), 'key': 'sync-1-dss'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'CHANGE_FROM_QUANTITY_STALE')
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=150), 'key': 'sync-1-dss'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED')
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=180), 'key': 'sync-1-dss'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'][0]['code'], 'IDEMPOTENCY_KEY_PARAMETER_MISMATCH')
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 180)
        s, b = gql(SET_MUTATION, {'input': self.set_input(changeFromQuantity=180), 'key': 'sync-1-dss-retry'})
        self.assertEqual(b['data']['inventorySetQuantities']['userErrors'], [])
        self.assertEqual((MOCK.shopify.available('DC-BAR-DARK-SEASALT'), len(MOCK.shopify.adjustments)), (200, 1))

    def test_search_fields_and_sort_keys(self):
        # a search field the mock cannot apply fails loudly instead of returning nothing
        s, b = gql('{ productVariants(first: 5, query: "sku:DC-BAR-DARK-85 AND inventory_quantity:>0") { nodes { sku } } }')
        self.assertIsNone(b['data'])
        self.assertIn("does not support the search field 'inventory_quantity'", b['errors'][0]['message'])
        s, b = gql('{ productVariants(first: 3, sortKey: SKU) { nodes { sku } } }')
        self.assertEqual([n['sku'] for n in b['data']['productVariants']['nodes']],
                         ['DC-BAR-DARK-85', 'DC-BAR-DARK-ALMOND', 'DC-BAR-DARK-CHILI'])
        s, b = gql('{ productVariants(first: 1, sortKey: SKU, reverse: true) { nodes { sku } } }')
        self.assertEqual(b['data']['productVariants']['nodes'][0]['sku'], 'DC-HOT-COCOA-250')
        s, b = gql('{ productVariants(first: 1, sortKey: COST) { nodes { sku } } }')
        self.assertIn('Expected type', b['errors'][0]['message'])                 # not a ProductVariantSortKeys value
        since = (MOCK.clock.today() - timedelta(days=3)).isoformat()
        s, b = gql('query($q: String!) { orders(first: 50, query: $q, sortKey: ORDER_NUMBER) { nodes { name } } }',
                   {'q': f'created_at:>={since}'})
        names = [n['name'] for n in b['data']['orders']['nodes']]
        want = sorted((o['fields']['Order Number'] for o in MOCK.airtable.records('Orders') if o['fields'].get('Shopify Order ID')
                       and o['fields']['Order Date'][:10] >= since), key=lambda n: int(n[1:]))
        self.assertEqual(names, want)
        self.assertTrue(names)
        s, b = gql('{ orders(first: 5, query: "status:cancelled") { nodes { name } } }')
        self.assertEqual([n['name'] for n in b['data']['orders']['nodes']], ['#1025'])

    def test_fragment_rules_and_idempotent_key_type(self):
        s, b = gql('query { shop { ...F } } fragment F on Product { title }')
        self.assertEqual((b.get('data'), b['errors'][0]['extensions']['code']), (None, 'cannotSpreadFragment'))
        s, b = gql('query { shop { name } } fragment F on Shop { name }')
        self.assertEqual(b['errors'][0]['message'], 'Fragment F was defined, but not used')
        s, b = gql('query { productVariants(first: 1) { nodes { ...V } } } fragment V on Node { id }')
        self.assertEqual(len(b['data']['productVariants']['nodes'][0]['id']) > 0, True)   # an interface fragment fits
        s, b = gql(SET_MUTATION.replace('@idempotent(key: $key)', '@idempotent(key: 5)').replace(', $key: String!', ''),
                   {'input': self.set_input(changeFromQuantity=None)})
        self.assertIsNone(b['data']['inventorySetQuantities'])
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 180)

    def test_nodes_by_ids_and_invoice_email_body(self):
        ids = [MOCK.shopify.item_by_sku(s)['id'] for s in ('DC-BAR-DARK-85', 'DC-BAR-DARK-MINT')] + ['gid://shopify/InventoryItem/1']
        s, b = gql('query($ids: [ID!]!, $loc: ID!) { nodes(ids: $ids) { id ... on InventoryItem { sku '
                   'inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { quantity } } } } }',
                   {'ids': ids, 'loc': self.loc})
        nodes = b['data']['nodes']
        self.assertEqual([n and n['sku'] for n in nodes], ['DC-BAR-DARK-85', 'DC-BAR-DARK-MINT', None])
        self.assertEqual(nodes[1]['inventoryLevel']['quantities'], [{'quantity': 3}])
        variant = MOCK.shopify.variant_by_sku('DC-BAR-DARK-SEASALT')['id']
        s, b = gql('mutation($input: DraftOrderInput!) { draftOrderCreate(input: $input) { draftOrder { id } userErrors { message } } }',
                   {'input': {'email': 'juniper.market@example.com', 'lineItems': [{'variantId': variant, 'quantity': 1}]}})
        did = b['data']['draftOrderCreate']['draftOrder']['id']
        s, b = gql('mutation($id: ID!, $e: EmailInput) { draftOrderInvoiceSend(id: $id, email: $e) { draftOrder { status } userErrors { message } } }',
                   {'id': did, 'e': {'to': 'juniper.market@example.com', 'subject': 'Your invoice', 'body': 'Thanks', 'customMessage': 'Hi'}})
        self.assertEqual(b['data']['draftOrderInvoiceSend'], {'draftOrder': {'status': 'INVOICE_SENT'}, 'userErrors': []})

    def test_pumpkin_spice_is_in_shopify_but_a_draft_without_ids_in_airtable(self):
        self.assertEqual(MOCK.shopify.available('DC-BAR-PUMPKIN-SPICE'), 40)
        f = MOCK.airtable.by_primary('Products', 'DC-BAR-PUMPKIN-SPICE')['fields']
        self.assertEqual((f['Status'], f.get('Shopify Inventory Item ID'), f.get('Production Batches')), ('Draft', None, None))
        self.assertIsNone(MOCK.airtable.by_primary('Products', 'DC-BAR-GINGER'))

    def test_draft_order_create_invoice_complete(self):
        variant = MOCK.shopify.variant_by_sku('DC-BAR-DARK-SEASALT')['id']
        create = '''mutation($input: DraftOrderInput!) { draftOrderCreate(input: $input) {
            draftOrder { id name status note2 invoiceUrl totalPriceSet { shopMoney { amount currencyCode } }
                         lineItems(first: 10) { nodes { sku quantity custom originalUnitPriceSet { shopMoney { amount } } } } }
            userErrors { field message } } }'''
        inp = {'email': 'juniper.market@example.com', 'note': 'Quote Q-2026-0042', 'tags': ['wholesale', 'quote-Q-2026-0042'],
               'lineItems': [{'variantId': variant, 'quantity': 100, 'priceOverride': {'amount': '4.20', 'currencyCode': 'USD'}},
                             {'title': 'Hazelnut Praline, single piece', 'sku': 'DC-PRALINE-HAZELNUT', 'quantity': 60,
                              'originalUnitPriceWithCurrency': {'amount': '1.10', 'currencyCode': 'USD'}}]}
        s, b = gql(create, {'input': inp})
        d = b['data']['draftOrderCreate']['draftOrder']
        self.assertEqual((d['status'], d['note2'], d['totalPriceSet']['shopMoney']['amount']), ('OPEN', 'Quote Q-2026-0042', '486.0'))
        self.assertEqual([n['custom'] for n in d['lineItems']['nodes']], [False, True])
        s, b = gql('{ draftOrder(id: "%s") { note } }' % d['id'])
        self.assertEqual(b['errors'][0]['message'], "Field 'note' doesn't exist on type 'DraftOrder'")
        send = 'mutation($id: ID!) { draftOrderInvoiceSend(id: $id) { draftOrder { status invoiceSentAt } userErrors { message } } }'
        s, b = gql(send, {'id': d['id']})
        self.assertEqual(b['data']['draftOrderInvoiceSend']['draftOrder']['status'], 'INVOICE_SENT')
        self.assertEqual(MOCK.shopify.invoices[0]['to'], 'juniper.market@example.com')
        bad = 'mutation($id: ID!, $e: EmailInput) { draftOrderInvoiceSend(id: $id, email: $e) { userErrors { message } } }'
        s, b = gql(bad, {'id': d['id'], 'e': {'to': 'not-an-email'}})
        self.assertEqual(b['data']['draftOrderInvoiceSend']['userErrors'][0]['message'], 'To is invalid')
        done = 'mutation($id: ID!) { draftOrderComplete(id: $id) { draftOrder { status order { name displayFinancialStatus } } userErrors { message } } }'
        s, b = gql(done, {'id': d['id']})
        self.assertEqual(b['data']['draftOrderComplete']['draftOrder']['order']['name'], '#1026')
        self.assertEqual(MOCK.shopify.available('DC-BAR-DARK-SEASALT'), 80)

    def test_seed_orders_and_customers(self):
        q = '''query($q: String!) { orders(first: 2, query: $q) { nodes { name email displayFulfillmentStatus
                  customer { email displayName } shippingAddress { city provinceCode }
                  lineItems(first: 5) { nodes { sku quantity currentQuantity } } } } }'''
        s, b = gql(q, {'q': 'name:#1009'})
        o = b['data']['orders']['nodes']
        self.assertEqual(len(o), 1)
        self.assertEqual((o[0]['email'], o[0]['customer'], o[0]['shippingAddress']['city']),
                         ('omar.haddad@example.com', {'email': 'omar.haddad@example.com', 'displayName': 'Omar Haddad'}, 'Miami'))
        self.assertEqual(o[0]['lineItems']['nodes'], [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'currentQuantity': 2},
                                                      {'sku': 'DC-BAR-DARK-CHILI', 'quantity': 2, 'currentQuantity': 2}])
        s, b = gql(q, {'q': 'name:#1004'})                     # imported order, never in Shopify
        self.assertEqual(b['data']['orders']['nodes'], [])
        s, b = gql('{ customers(first: 5, query: "email:jordan.blake@example.com") { nodes { id email } } }')
        self.assertEqual([c['id'] for c in b['data']['customers']['nodes']], ['gid://shopify/Customer/7700000000103'])

    def test_draft_order_input_matches_current_schema(self):
        variant = MOCK.shopify.variant_by_sku('DC-HOT-COCOA-250')['id']
        create = 'mutation($input: DraftOrderInput!) { draftOrderCreate(input: $input) { draftOrder { id email } userErrors { field message } } }'
        s, b = gql(create, {'input': {'email': 'juniper.market@example.com', 'phone': '+15550102042', 'visibleToCustomer': True,
                                      'purchasingEntity': {'customerId': 'gid://shopify/Customer/7700000000108'},
                                      'lineItems': [{'variantId': variant, 'quantity': 12}]}})
        self.assertEqual(b['data']['draftOrderCreate']['userErrors'], [])
        s, b = gql(create, {'input': {'lineItems': [{'variantId': variant, 'quantity': 1}], 'customer': {'id': 'x'}}})
        self.assertIn('customer (Field is not defined on DraftOrderInput)', b['errors'][0]['message'])
        s, b = gql(create, {'input': {'lineItems': [{'variantId': variant, 'quantity': 1}],
                                      'purchasingEntity': {'customerId': 'gid://shopify/Customer/1'}}})
        self.assertEqual(b['data']['draftOrderCreate']['userErrors'][0]['message'], 'Customer does not exist')

    def test_replacement_draft_at_100_percent(self):
        variant = MOCK.shopify.variant_by_sku('DC-BAR-MILK-CARAMEL')['id']
        create = '''mutation($input: DraftOrderInput!) { draftOrderCreate(input: $input) {
            draftOrder { id totalPriceSet { shopMoney { amount } } appliedDiscount { valueType value } } userErrors { field message } } }'''
        s, b = gql(create, {'input': {'email': 'tom.becker@example.com', 'lineItems': [{'variantId': variant, 'quantity': 2}],
                                      'appliedDiscount': {'valueType': 'PERCENTAGE', 'value': 100.0, 'title': 'Melted replacement'}}})
        d = b['data']['draftOrderCreate']['draftOrder']
        self.assertEqual((d['totalPriceSet']['shopMoney']['amount'], d['appliedDiscount']['value']), ('0.0', 100.0))
        s, b = gql(create, {'input': {'lineItems': [{'variantId': 'gid://shopify/ProductVariant/1', 'quantity': 1}]}})
        self.assertEqual(b['data']['draftOrderCreate']['userErrors'][0]['field'], ['lineItems', '0', 'variantId'])
        s, b = gql(create, {'input': {'lineItems': [{'variantId': variant, 'quantity': 1}],
                                      'appliedDiscount': {'valueType': 'PERCENT', 'value': 100}}})
        self.assertIn('was provided invalid value for appliedDiscount.valueType', b['errors'][0]['message'])

    def test_throttle_and_max_cost(self):
        MOCK.shopify.throttle(1)
        s, b = gql('{ shop { name } }')
        self.assertEqual((s, b['errors'][0]['extensions']['code']), (200, 'THROTTLED'))
        s, b = gql('{ productVariants(first: 250) { nodes { inventoryItem { inventoryLevels(first: 250) { nodes { id } } } } } }')
        self.assertEqual(b['errors'][0]['extensions']['code'], 'MAX_COST_EXCEEDED')

    def test_webhook_signing_matches_reference(self):
        payload = MOCK.shopify.order_payload(1026, 'new.buyer@example.com', [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5}],
                                             shipping_address={'city': 'Phoenix', 'province_code': 'AZ', 'country_code': 'US', 'zip': '85004',
                                                               'address1': '1 Test Way'},
                                             customer={'name': 'New Buyer'}, refunds=None)
        raw, headers = webhook_request('orders/create', payload, webhook_id='abc-123')
        ref = base64.b64encode(hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()).decode()
        self.assertEqual(headers['X-Shopify-Hmac-Sha256'], ref)
        self.assertEqual(sign_webhook(raw), ref)
        self.assertEqual((headers['X-Shopify-Topic'], headers['X-Shopify-Webhook-Id']), ('orders/create', 'abc-123'))
        body = json.loads(raw)
        self.assertEqual((body['name'], body['line_items'][0]['variant_id'], body['total_price']), ('#1026', 4510000000101, '21.95'))
        raw2, headers2 = webhook_request('orders/create', payload, signature='')
        self.assertNotIn('X-Shopify-Hmac-Sha256', headers2)

    def test_refund_payload_updates_current_quantity(self):
        p = MOCK.shopify.order_payload(1030, 'r@example.com', [{'sku': 'DC-BAR-MILK-CARAMEL', 'quantity': 3, 'price': 7.0, 'id': 555}],
                                       refunds=[{'line_item_id': 555, 'quantity': 1}], financial_status='partially_refunded')
        self.assertEqual((p['line_items'][0]['current_quantity'], p['current_total_price'], len(p['refunds'])), (2, '20.95', 1))
        self.assertEqual(p['refunds'][0]['refund_line_items'][0]['line_item_id'], 555)


# ================================================================================ Open-Meteo

class OpenMeteoTests(Base):
    def test_geocoding_matches_names_only(self):
        s, b = call('GET', '/v1/search?name=Phoenix&count=1&language=en&format=json')
        self.assertEqual((b['results'][0]['latitude'], b['results'][0]['country_code']), (33.44838, 'US'))
        s, b = call('GET', '/v1/search?name=Phoenix%2C%20Arizona&count=1')
        self.assertEqual(b['results'][0]['admin1'], 'Arizona')
        s, b = call('GET', '/v1/search?name=Phoenix%2C%20AZ&count=1')                # admin1 abbreviation
        self.assertEqual(b['results'][0]['admin1'], 'Arizona')
        s, b = call('GET', '/v1/search?name=Phoenix%2C%20Ariz&count=1')              # no prefix matching
        self.assertNotIn('results', b)
        s, b = call('GET', '/v1/search?name=Portland&count=5')
        self.assertEqual([r['admin1'] for r in b['results']], ['Oregon', 'Maine'])
        s, b = call('GET', '/v1/search?name=Portland%2C%20ME&count=5')
        self.assertEqual([r['admin1'] for r in b['results']], ['Maine'])
        s, b = call('GET', '/v1/search?name=Portland%2C%20US&count=5')               # country code
        self.assertEqual(len(b['results']), 2)
        s, b = call('GET', '/v1/search?name=San%20Diego%2C%20CA&count=5')            # CA is California, not Canada
        self.assertEqual(b['results'][0]['admin1'], 'California')
        self.assertNotIn('admin1_code', b['results'][0])
        s, b = call('GET', '/v1/search?name=Nowhereville')
        self.assertEqual((s, 'results' in b), (200, False))
        s, b = call('GET', '/v1/search?name=P')
        self.assertNotIn('results', b)
        self.assertEqual(call('GET', '/v1/search?count=1')[0], 400)

    def test_forecast_daily_max(self):
        MOCK.clock.set('2026-10-20T16:00:00Z')
        s, b = call('GET', '/v1/forecast?latitude=33.44838&longitude=-112.07404&daily=temperature_2m_max&timezone=auto&forecast_days=3')
        self.assertEqual(b['daily']['time'], ['2026-10-20', '2026-10-21', '2026-10-22'])
        self.assertEqual(b['daily']['temperature_2m_max'], [36.4, 37.1, 35.8])
        self.assertEqual((b['timezone'], b['daily_units']['temperature_2m_max']), ('America/Phoenix', '°C'))
        MOCK.meteo.set_forecast('Seattle', [30.0, 12.0])
        s, b = call('GET', '/v1/forecast?latitude=47.6&longitude=-122.33&daily=temperature_2m_max&timezone=auto&forecast_days=2')
        self.assertEqual(b['daily']['temperature_2m_max'], [30.0, 12.0])

    def test_forecast_errors(self):
        for q in ('latitude=95&longitude=0&daily=temperature_2m_max&timezone=auto',
                  'latitude=33&longitude=-112&daily=temperature_max&timezone=auto',
                  'latitude=33&longitude=-112&daily=temperature_2m_max&timezone=auto&forecast_days=17',
                  'latitude=33&longitude=-112&daily=temperature_2m_max',
                  'longitude=-112&daily=temperature_2m_max&timezone=auto'):
            s, b = call('GET', '/v1/forecast?' + q)
            self.assertEqual((s, b['error']), (400, True), q)

    def test_injected_outage(self):
        MOCK.fail('open-meteo-forecast', status=503, times=2)
        self.assertEqual(call('GET', '/v1/forecast?latitude=33&longitude=-112')[0], 503)
        self.assertEqual(call('GET', '/v1/forecast?latitude=33&longitude=-112')[0], 503)
        self.assertEqual(call('GET', '/v1/forecast?latitude=33&longitude=-112')[0], 200)


# ================================================================================ Postmark, Slack

class PostmarkTests(Base):
    H = {'X-Postmark-Server-Token': POSTMARK_SERVER_TOKEN, 'Accept': 'application/json'}

    def send(self, body, headers=None):
        return call('POST', '/email', body, headers if headers is not None else self.H)

    def test_send_and_errors(self):
        good = {'From': 'Demo Chocolate Co. <wholesale@example.com>', 'To': 'juniper.market@example.com', 'Subject': 'Quote',
                'HtmlBody': '<p>Hi</p>', 'MessageStream': 'outbound', 'Metadata': {'quote': 'Q-2026-0046'}}
        s, b = self.send(good)
        self.assertEqual((s, b['ErrorCode'], b['Message']), (200, 0, 'OK'))
        self.assertEqual(MOCK.postmark.sent[0]['MessageID'], b['MessageID'])
        self.assertEqual(self.send(good, headers={})[1]['ErrorCode'], 10)
        self.assertEqual(self.send(dict(good, From='not.a.signature@example.com'))[1]['ErrorCode'], 400)
        self.assertEqual(self.send({k: v for k, v in good.items() if k != 'HtmlBody'})[1]['ErrorCode'], 300)
        self.assertEqual(self.send(dict(good, To='not-an-email'))[1]['ErrorCode'], 300)
        self.assertEqual(self.send(dict(good, Htmlbody='x'))[1]['ErrorCode'], 403)
        self.assertEqual(self.send(dict(good, MessageStream='nope'))[1]['ErrorCode'], 1235)
        self.assertEqual(self.send(good, headers={'X-Postmark-Server-Token': POSTMARK_SERVER_TOKEN, 'Content-Type': 'text/plain'})[0], 415)
        MOCK.postmark.inactive_recipients.add('juniper.market@example.com')
        self.assertEqual(self.send(good)[1]['ErrorCode'], 406)
        self.assertEqual(len(MOCK.postmark.sent), 1)


class SlackTests(Base):
    def test_webhook(self):
        s, b = call('POST', '/services/T1/B1/x', {'text': 'Packing alert'})
        self.assertEqual((s, b), (200, 'ok'))
        self.assertEqual(MOCK.slack.texts(), ['Packing alert'])
        self.assertEqual(call('POST', '/services/T1/B1/x', {'username': 'x'}), (400, 'no_text'))
        self.assertEqual(call('POST', '/services/T1/B1/x', {'text': 'x', 'blocks': [{'type': 'bogus'}]}), (400, 'invalid_blocks'))
        self.assertEqual(call('POST', '/services/T1/B1/x', raw=b'not json')[1], 'invalid_payload')
        form = 'payload=' + urllib.request.quote(json.dumps({'text': 'form post'}))
        s, b = call('POST', '/services/T1/B1/x', raw=form.encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual((s, MOCK.slack.texts()[-1]), (200, 'form post'))


# ================================================================================ Anthropic

def claude_body(**over):
    body = {'model': 'claude-sonnet-5', 'max_tokens': 1024, 'system': 'Extract the quote.',
            'messages': [{'role': 'user', 'content': '120 dark sea salt bars by March 3'}],
            'tools': [{'name': 'extract_quote', 'description': 'x', 'input_schema': {
                'type': 'object', 'additionalProperties': False, 'required': ['business_name', 'items'],
                'properties': {'business_name': {'type': 'string'},
                               'items': {'type': 'array', 'items': {'type': 'object', 'required': ['product', 'quantity'],
                                                                    'properties': {'product': {'type': 'string'}, 'quantity': {'type': 'integer'}}}}}}}],
            'tool_choice': {'type': 'tool', 'name': 'extract_quote'}}
    body.update(over)
    return body


class AnthropicTests(Base):
    H = {'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'}

    def msg(self, body, headers=None):
        return call('POST', '/v1/messages', body, headers if headers is not None else self.H)

    def test_scripted_tool_call(self):
        MOCK.anthropic.on('dark sea salt', tool_input={'business_name': 'Bluebird Cafe', 'items': [{'product': 'dark sea salt bar', 'quantity': 120}]})
        s, b = self.msg(claude_body())
        self.assertEqual((s, b['stop_reason'], b['content'][0]['name']), (200, 'tool_use', 'extract_quote'))
        self.assertEqual(b['content'][0]['input']['items'][0]['quantity'], 120)
        self.assertEqual((MOCK.anthropic.problems, MOCK.anthropic.schema_problems), ([], []))

    def test_rejects_what_the_real_api_rejects(self):
        cases = [
            ({}, {'x-api-key': ANTHROPIC_API_KEY}, 400),                                    # no anthropic-version
            ({}, {'anthropic-version': '2023-06-01'}, 401),                                  # no key
            ({'temperature': 0.2}, None, 400),                                               # sampling removed on Sonnet 5
            ({'model': 'claude-sonnet-9'}, None, 404),
            ({'max_tokens': 200000}, None, 400),
            ({'messages': [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': '{'}]}, None, 400),   # prefill
            ({'messages': [{'role': 'user', 'content': 'a'}, {'role': 'user', 'content': 'b'}]}, None, 400),
            ({'tool_choice': {'type': 'tool', 'name': 'other'}}, None, 400),
            ({'model': 'claude-opus-5-5'}, None, 400),                                       # forced tool not allowed
            ({'thinking': {'type': 'adaptive'}}, None, 400),                                  # thinking with forced tool
            ({'thinking': {'type': 'enabled', 'budget_tokens': 2000}, 'tool_choice': {'type': 'auto'}}, None, 400),
            ({'model': 'claude-opus-5-5', 'thinking': {'type': 'disabled'}, 'tool_choice': {'type': 'auto'}}, None, 400),
            ({'stream': True}, None, 400),
            ({'extra': 1}, None, 400),
        ]
        for over, headers, status in cases:
            s, b = self.msg(claude_body(**over), headers)
            self.assertEqual(s, status, over)
        self.assertEqual(len(MOCK.anthropic.problems), len(cases))

    def test_outage_unscripted_and_schema_drift(self):
        MOCK.anthropic.on(None, status=529, times=1)
        s, b = self.msg(claude_body())
        self.assertEqual((s, b['error']['type']), (529, 'overloaded_error'))
        s, b = self.msg(claude_body())
        self.assertEqual(s, 500)
        self.assertEqual(MOCK.anthropic.problems[-1]['message'], 'no scripted answer')
        MOCK.anthropic.queue(tool_input={'business_name': 'X', 'items': [{'product': 'bar'}], 'extra': True})
        s, b = self.msg(claude_body())
        self.assertEqual(s, 200)
        self.assertEqual(sorted(MOCK.anthropic.schema_problems[0]['issues']),
                         ['$.extra: not allowed (additionalProperties is false)', '$.items[0].quantity: required'])

    def test_count_tokens(self):
        body = claude_body()
        s, b = call('POST', '/v1/messages/count_tokens', body, self.H)
        self.assertEqual(s, 400)
        body.pop('max_tokens')
        s, b = call('POST', '/v1/messages/count_tokens', body, self.H)
        self.assertEqual((s, isinstance(b['input_tokens'], int)), (200, True))
        # like the real count_tokens (checked in October 2026): model and tool_choice rules apply, generation rules do not
        self.assertEqual(call('POST', '/v1/messages/count_tokens', dict(body, model='claude-opus-5-5'), self.H)[0], 400)
        self.assertEqual(call('POST', '/v1/messages/count_tokens', dict(body, thinking={'type': 'adaptive'}), self.H)[0], 200)
        prefill = dict(body, messages=[{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': '{'}])
        self.assertEqual(call('POST', '/v1/messages/count_tokens', prefill, self.H)[0], 200)
        self.assertEqual(self.msg(dict(prefill, max_tokens=100))[0], 400)          # still a 400 on /v1/messages


# ================================================================================ core: log, failures, clock, n8n helper

class CoreTests(Base):
    def test_request_log_and_match_filters(self):
        mark = MOCK.mark()
        at('GET', 'Orders?pageSize=1')
        at('PATCH', 'Orders', {'performUpsert': {'fieldsToMergeOn': ['Shopify Order ID']},
                               'records': [{'fields': {'Order Number': '#2000', 'Shopify Order ID': '1'}}]})
        entries = MOCK.log('airtable', since=mark)
        self.assertEqual([(e['method'], e['table'], e['operation']) for e in entries],
                         [('GET', 'Orders', 'list'), ('PATCH', 'Orders', 'upsert')])
        self.assertEqual(entries[1]['body']['records'][0]['fields']['Order Number'], '#2000')
        self.assertEqual(MOCK.log('airtable', since=mark, operation='upsert')[0]['record_count'], 1)

    def test_failure_injection_by_match_times_and_clear(self):
        MOCK.fail('airtable', status=503, times=-1, match={'table': 'Orders', 'method': 'PATCH'})
        self.assertEqual(at('GET', 'Orders?pageSize=1')[0], 200)
        body = {'records': [{'id': MOCK.airtable.ref('Orders', 'o1001'), 'fields': {}}]}
        self.assertEqual([at('PATCH', 'Orders', body)[0] for _ in range(3)], [503, 503, 503])
        self.assertEqual(at('PATCH', 'Orders', body)[1]['error']['type'], 'RETRIABLE_ERROR')
        MOCK.clear_failures()
        self.assertEqual(at('PATCH', 'Orders', body)[0], 200)

    def test_timeout_and_dropped_connection(self):
        MOCK.fail('postmark', status=None, delay=2.0)
        t = time.time()
        with self.assertRaises((socket.timeout, TimeoutError, urllib.error.URLError)):
            call('POST', '/email', {}, {'X-Postmark-Server-Token': POSTMARK_SERVER_TOKEN}, timeout=0.5)
        self.assertLess(time.time() - t, 1.5)
        time.sleep(2)
        MOCK.fail('slack', drop=True)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            call('POST', '/services/T/B/x', {'text': 'x'})
        self.assertEqual(MOCK.log('slack')[-1]['response'], 'dropped')

    def test_lose_answer_applies_the_change_then_drops_or_replaces_the_answer(self):
        # dropped: the record is created, the client gets no answer at all
        MOCK.lose_answer('airtable', {'table': 'Sync Log', 'method': 'POST'})
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'lost-1'}}]})
        self.assertEqual(len(MOCK.airtable.find('Sync Log', **{'Run ID': 'lost-1'})), 1)
        entry = MOCK.log('airtable', table='Sync Log')[-1]
        self.assertEqual((entry['answer_lost'], entry['status'], entry['handled_status']), (True, None, 200))
        # used up: the next request is answered normally
        self.assertEqual(at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'lost-2'}}]})[0], 200)
        # replaced: the change is applied, the client sees the service's real 500 body
        seen = []
        MOCK.lose_answer('airtable', lambda e: seen.append(e['seq']) or e.get('operation') == 'create', status=500, times=2)
        body = {'records': [{'fields': {'Run ID': 'lost-3'}}]}
        s, b = at('POST', 'Sync%20Log', body)
        self.assertEqual((s, b['error']['type']), (500, 'SERVER_ERROR'))
        self.assertEqual(len(MOCK.airtable.find('Sync Log', **{'Run ID': 'lost-3'})), 1)
        # a request the service refuses changed nothing, so it is never matched and does not use up the rule
        bad = {'records': [{'fields': {'Run ID': 'lost-4', 'No Such Field': 1}}]}
        self.assertEqual(at('POST', 'Sync%20Log', bad)[0], 422)
        self.assertEqual(len(seen), 1)
        self.assertEqual(at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'lost-5'}}]})[0], 500)
        self.assertEqual(at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'lost-6'}}]})[0], 200)
        # other services and injected failures are left alone; reset() clears the rules
        MOCK.lose_answer('shopify', times=-1)
        self.assertEqual(at('GET', 'Orders?pageSize=1')[0], 200)
        MOCK.fail('shopify', status=503)
        self.assertEqual(gql('{ shop { name } }')[0], 503)
        MOCK.reset()
        self.assertEqual(MOCK.lost_answers, [])
        self.assertEqual(gql('{ shop { name } }')[0], 200)

    def test_n8n_env_file_that_does_not_exist_is_a_clear_error(self):
        from tests.mocks.n8n import N8n
        saved = {k: os.environ.get(k) for k in ('N8N_API_KEY', 'N8N_ENV_FILE')}
        try:
            os.environ.pop('N8N_API_KEY', None)
            os.environ['N8N_ENV_FILE'] = '/c/no/such/folder/.env'
            with self.assertRaises(SystemExit) as ctx:
                N8n()
            self.assertIn('N8N_ENV_FILE points at', str(ctx.exception))
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '.env')
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write('# test\nN8N_API_KEY="abc"\n')
                os.environ['N8N_ENV_FILE'] = path
                self.assertEqual(N8n().key, 'abc')
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_reset_restores_seed(self):
        at('POST', 'Sync%20Log', {'records': [{'fields': {'Run ID': 'temp'}}]})
        self.assertEqual(MOCK.airtable.count('Sync Log'), 3)
        MOCK.reset()
        self.assertEqual(MOCK.airtable.count('Sync Log'), 2)
        self.assertEqual(MOCK.log(), [])

    def test_unknown_path_is_404_and_logged(self):
        s, b = call('GET', '/v2/whatever')
        self.assertEqual((s, b['error']), (404, 'NOT_MOCKED'))
        self.assertEqual(MOCK.log('unknown')[0]['path'], '/v2/whatever')

    def test_prepare_workflow_for_mocks(self):
        wf = {'nodes': [
            {'name': 'Hook', 'type': 'n8n-nodes-base.webhook', 'parameters': {'path': 'orders'}, 'webhookId': 'x'},
            {'name': 'Daily', 'type': 'n8n-nodes-base.scheduleTrigger', 'parameters': {}},
            {'name': 'Airtable: List', 'type': 'n8n-nodes-base.httpRequest', 'parameters': {
                'url': '=https://api.airtable.com/v0/{{ $env.AIRTABLE_BASE_ID }}/Orders',
                'headerParameters': {'parameters': [{'name': 'Authorization', 'value': '=Bearer {{ $env.AIRTABLE_TOKEN }}'}]}}},
            {'name': 'Shopify: Set', 'type': 'n8n-nodes-base.httpRequest', 'parameters': {
                'url': '=https://{{ $env.SHOPIFY_STORE_DOMAIN }}/admin/api/{{ $env.SHOPIFY_API_VERSION }}/graphql.json'}},
        ], 'connections': {}}
        out = prepare_workflow(wf, MOCK, 'inventory-sync', mock_env(MOCK))
        self.assertEqual(out['nodes'][0]['parameters']['path'], 'inventory-sync-e2e-orders')
        self.assertTrue(out['nodes'][1]['disabled'])
        self.assertEqual(out['nodes'][2]['parameters']['url'], f'={MOCK.url}/v0/{{{{ "{BASE}" }}}}/Orders')
        self.assertEqual(out['nodes'][3]['parameters']['url'], f'={MOCK.url}/admin/api/{{{{ "2026-10" }}}}/graphql.json')
        self.assertIn(TOKEN, out['nodes'][2]['parameters']['headerParameters']['parameters'][0]['value'])
        # a URL the rewrite cannot reach would hit a real API: refuse to import it
        leak = {'nodes': [{'name': 'Code: Build URL', 'type': 'n8n-nodes-base.code', 'parameters': {
            'jsCode': "const u = 'https://demo-chocolate.myshopify.com/admin/api/2026-10/graphql.json';"}}], 'connections': {}}
        with self.assertRaises(ValueError) as ctx:
            prepare_workflow(leak, MOCK, 'x', mock_env(MOCK))
        self.assertIn('Code: Build URL', str(ctx.exception))
        note = {'nodes': [{'name': 'Note', 'type': 'n8n-nodes-base.stickyNote', 'parameters': {
            'content': 'Shopify admin: https://demo-chocolate.myshopify.com/admin'}}], 'connections': {}}
        self.assertEqual(len(prepare_workflow(note, MOCK, 'x', mock_env(MOCK))['nodes']), 1)
        # a host built at run time cannot be pointed at the mock, so it is refused too
        for code in ("const u = 'https://' + $env.SHOPIFY_STORE_DOMAIN + '/admin/api/2026-10/graphql.json';",
                     "const u = `https://${$env.SHOPIFY_STORE_DOMAIN}/admin/api/graphql.json`;"):
            built = {'nodes': [{'name': 'Code: Build URL', 'type': 'n8n-nodes-base.code', 'parameters': {'jsCode': code}}],
                     'connections': {}}
            with self.assertRaises(ValueError, msg=code) as ctx:
                prepare_workflow(built, MOCK, 'x', mock_env(MOCK))
            self.assertIn('builds the host at run time', str(ctx.exception))
        dyn = {'nodes': [{'name': 'Shopify: Query', 'type': 'n8n-nodes-base.httpRequest', 'parameters': {
            'url': '=https://{{ $json.shop }}/admin/api/2026-10/graphql.json'}}], 'connections': {}}
        with self.assertRaises(ValueError):
            prepare_workflow(dyn, MOCK, 'x', mock_env(MOCK))
        other = {'nodes': [{'name': 'HTTP: Elsewhere', 'type': 'n8n-nodes-base.httpRequest', 'parameters': {
            'url': 'https://api.example.com/v1/thing'}}], 'connections': {}}
        with self.assertRaises(ValueError) as ctx:
            prepare_workflow(other, MOCK, 'x', mock_env(MOCK))
        self.assertIn('is not a mocked API', str(ctx.exception))
        ok = {'nodes': [{'name': 'Slack: Alert', 'type': 'n8n-nodes-base.httpRequest', 'parameters': {
            'url': '={{ $env.SLACK_PACKING_WEBHOOK_URL }}'}},
            {'name': 'Code: Check Shop', 'type': 'n8n-nodes-base.code', 'parameters': {
                'jsCode': "if ($json.headers['x-shopify-shop-domain'] !== $env.SHOPIFY_STORE_DOMAIN) throw new Error('Wrong shop');"}}],
            'connections': {}}
        out = prepare_workflow(ok, MOCK, 'x', mock_env(MOCK))
        self.assertEqual(out['nodes'][0]['parameters']['url'], '={{ "' + MOCK.url + '/services/T000MOCK/B000MOCK/packing" }}')
        self.assertIn('"demo-chocolate.myshopify.com"', out['nodes'][1]['parameters']['jsCode'])
        self.assertEqual(len(prepare_workflow(ERROR_WF, MOCK, 'x', {'SLACK_WEBHOOK_URL': MOCK.url + '/services/T/B/errors'})['nodes']), 3)
        env = mock_env(MOCK, MELT_TEMP_C=30)
        self.assertEqual((env['MELT_TEMP_C'], env['CLAIM_LIMIT'], env['SLACK_PACKING_WEBHOOK_URL']),
                         ('30', '2', MOCK.url + '/services/T000MOCK/B000MOCK/packing'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
