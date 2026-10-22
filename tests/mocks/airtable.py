"""Strict mock of the Airtable Web API and Metadata API for one base.

Real behaviour it reproduces (each one is covered in test_mocks.py):
  * Authorization: Bearer <token> on every call (401 without it)
  * 5 requests per second per base; the 6th inside one second gets 429
    {"error": {"type": "RATE_LIMIT_REACHED", ...}} and the base stays blocked for 30 seconds
    (configurable with mock.airtable.set_rate_limit)
  * at most 10 records per create, update, upsert or delete (422 INVALID_RECORDS)
  * list records: pageSize (max 100), maxRecords, offset pagination, sort, fields, view, filterByFormula,
    returnFieldsByFieldId, cellFormat; GET with query string or POST .../listRecords with a JSON body
  * empty values ("", [], false) are left out of returned records
  * field names are exact; unknown names give 422 UNKNOWN_FIELD_NAME
  * values are type checked against base/schema.json (422 INVALID_VALUE_FOR_COLUMN), select options must
    exist (422 INVALID_MULTIPLE_CHOICE_OPTIONS) unless typecast is true, linked records must be existing
    record ids (422 ROW_DOES_NOT_EXIST), computed fields cannot be written
  * typecast: numbers from strings, new select options, links by primary field value (creating the linked
    record when no match exists, like Airtable does)
  * upsert with performUpsert.fieldsToMergeOn (1 to 3 fields, only the documented types: number, single line text,
    long text, single select, multiple select, date); several existing matches fail the request with
    422 INVALID_VALUE_FOR_COLUMN "Cannot update more than one record for fields to merge on"
  * a number 0 is a value and is returned; only "", [] and false count as empty
  * every multi record write is all or nothing
  * linked record fields are two way: writing Orders.Customer updates Customers.Orders
  * Metadata API: list bases, get base schema, create table, update table, create field, update field,
    with the auto created inverse field for links and the field types the API cannot create
"""
import copy
import hashlib
import json
import re
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from . import formula as F
from .core import Response, iso_ms, json_response

BASE_ID = 'appDemoChocolate1'
BASE_NAME = 'Demo Chocolate Co. Operations'
TOKEN = 'patMockDemoChoc01.' + 'a1b2c3d4' * 8

TEXT_TYPES = {'singleLineText', 'multilineText', 'richText', 'email', 'url', 'phoneNumber'}
NUMBER_TYPES = {'number', 'currency', 'percent', 'rating', 'duration'}
COMPUTED_TYPES = {'formula', 'rollup', 'count', 'multipleLookupValues', 'autoNumber', 'createdTime', 'lastModifiedTime',
                  'createdBy', 'lastModifiedBy', 'button', 'aiText', 'externalSyncSource'}
WRITABLE_TYPES = TEXT_TYPES | NUMBER_TYPES | {'checkbox', 'singleSelect', 'multipleSelects', 'date', 'dateTime',
                                              'multipleRecordLinks', 'multipleAttachments', 'barcode'}
ALL_TYPES = WRITABLE_TYPES | COMPUTED_TYPES | {'singleCollaborator', 'multipleCollaborators'}
PRIMARY_TYPES = {'singleLineText', 'multilineText', 'email', 'url', 'phoneNumber', 'number', 'currency', 'percent',
                 'duration', 'date', 'dateTime', 'barcode'}
# "must be one of the following types: number, text, long text, single select, multiple select, date"
# (Update multiple records, performUpsert). Email, URL, phone, currency, percent and dateTime are not on that list,
# so the mock refuses them instead of letting a test pass that Airtable may reject.
MERGE_TYPES = {'singleLineText', 'multilineText', 'number', 'singleSelect', 'multipleSelects', 'date'}
# field types the create field endpoint does not take (barcode cells are writable, the field is not creatable)
NOT_CREATABLE = COMPUTED_TYPES | {'barcode'}
UPSERT_MULTI_MATCH = 'Cannot update more than one record for fields to merge on'
URL_LIMIT = 16000
COLORS = {f'{c}{s}' for c in ('blue', 'cyan', 'teal', 'green', 'yellow', 'orange', 'red', 'pink', 'purple', 'gray')
          for s in ('Light2', 'Light1', 'Bright', 'Dark1')}
CHECKBOX_ICONS = {'check', 'xCheckbox', 'star', 'heart', 'thumbsUp', 'flag', 'dot'}
DATE_FORMATS = {'local': 'l', 'friendly': 'LL', 'us': 'M/D/YYYY', 'european': 'D/M/YYYY', 'iso': 'YYYY-MM-DD'}
TIME_FORMATS = {'12hour': 'h:mma', '24hour': 'HH:mm'}
LIST_PARAMS = {'pageSize', 'maxRecords', 'offset', 'view', 'sort', 'filterByFormula', 'cellFormat', 'timeZone',
               'userLocale', 'fields', 'returnFieldsByFieldId', 'recordMetadata', 'includeDateDependencyMetadata'}
REC_ID = re.compile(r'^rec[A-Za-z0-9]{14}$')
_B62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
GENERIC_422 = 'Invalid request: parameter validation failed. Check your request data.'
RATE_LIMIT_MESSAGE = 'Rate limit exceeded. Please try again later'
RATE_LIMIT_BODY = {'error': {'type': 'RATE_LIMIT_REACHED', 'message': RATE_LIMIT_MESSAGE}}


def make_id(prefix, *parts):
    n = int(hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest(), 16)
    out = []
    for _ in range(14):
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return prefix + ''.join(out)


class AirtableError(Exception):
    def __init__(self, status, type_, message=None):
        super().__init__(message or type_)
        self.status, self.type, self.message = status, type_, message

    def body(self):
        if self.message is None:
            return {'error': self.type}
        return {'error': {'type': self.type, 'message': self.message}}


def _err(type_, message, status=422):
    return AirtableError(status, type_, message)


# ---------------------------------------------------------------------------------------------- model

class Field:
    def __init__(self, id_, name, type_, description=None, options=None):
        self.id, self.name, self.type, self.description, self.options = id_, name, type_, description, options or {}

    def model(self):
        out = {'id': self.id, 'name': self.name, 'type': self.type}
        if self.description:
            out['description'] = self.description
        if self.options:
            out['options'] = copy.deepcopy(self.options)
        return out


class Record:
    __slots__ = ('id', 'created', 'cells')

    def __init__(self, id_, created, cells=None):
        self.id, self.created, self.cells = id_, created, cells or {}


class Table:
    def __init__(self, id_, name, description=None):
        self.id, self.name, self.description = id_, name, description
        self.fields = []
        self.records = {}
        self.view_id = make_id('viw', id_, 'Grid view')

    @property
    def primary(self):
        return self.fields[0]

    def field(self, key):
        for f in self.fields:
            if f.name == key or f.id == key:
                return f
        return None

    def field_ci(self, name):
        return next((f for f in self.fields if f.name.lower() == name.lower()), None)

    def model(self, include_visible=False):
        out = {'id': self.id, 'name': self.name, 'primaryFieldId': self.primary.id,
               'fields': [f.model() for f in self.fields],
               'views': [{'id': self.view_id, 'name': 'Grid view', 'type': 'grid'}]}
        if self.description:
            out['description'] = self.description
        if include_visible:
            out['views'][0]['visibleFieldIds'] = [f.id for f in self.fields]
        return out


# ---------------------------------------------------------------------------------------------- the mock

class AirtableMock:
    def __init__(self, server, base_id=BASE_ID, token=TOKEN):
        self.server = server
        self.base_id = base_id
        self.token = token
        self.base_name = BASE_NAME
        self.tables = []
        self.refs = {}
        self.rate = {'per_second': 5, 'penalty': 30.0, 'enabled': True}
        self._hits = deque()
        self._blocked_until = 0.0
        self._iterators = {}
        self._expired = set()
        self._counter = 0

    # ------------------------------------------------------------------ setup

    def reset(self):
        self.tables = []
        self.refs = {}
        self._hits.clear()
        self._blocked_until = 0.0
        self._iterators = {}
        self._expired = set()
        self._counter = 0
        self.rate = {'per_second': 5, 'penalty': 30.0, 'enabled': True}

    def set_rate_limit(self, per_second=5, penalty=30.0, enabled=True):
        """Change the limiter. penalty is how long the base stays blocked after a burst (Airtable: 30 s)."""
        self.rate = {'per_second': per_second, 'penalty': penalty, 'enabled': enabled}
        self._hits.clear()
        self._blocked_until = 0.0

    def unblock(self):
        """End a rate limit penalty early (for tests that check the 429 path and then continue)."""
        self._hits.clear()
        self._blocked_until = 0.0

    def expire_iterators(self):
        """Every offset handed out so far stops working (422 LIST_RECORDS_ITERATOR_NOT_AVAILABLE), as when a
        paginated read takes too long between pages."""
        self._expired.update(self._iterators)
        self._iterators = {}

    def table(self, key):
        """A table by name or id (exact)."""
        for t in self.tables:
            if t.name == key or t.id == key:
                return t
        return None

    def _table_or_raise(self, key):
        t = self.table(key)
        if t is None:
            raise KeyError(f'no table {key!r}')
        return t

    def load_schema(self, path):
        """Provision every table and field from base/schema.json with stable ids (no API calls)."""
        spec = json.loads(Path(path).read_text(encoding='utf-8'))
        self.base_name = spec.get('baseName', BASE_NAME)
        for t in spec['tables']:
            if self.table(t['name']):
                raise ValueError(f'duplicate table {t["name"]}')
            self.tables.append(Table(make_id('tbl', self.base_id, t['name']), t['name'], t.get('description')))
        for t in spec['tables']:
            table = self.table(t['name'])
            for i, f in enumerate(t['fields']):
                ftype = f['type']
                if ftype not in ALL_TYPES:
                    raise ValueError(f'{t["name"]}.{f["name"]}: unknown type {ftype}')
                if i == 0 and ftype not in PRIMARY_TYPES:
                    raise ValueError(f'{t["name"]}: primary field type {ftype} is not allowed')
                if table.field_ci(f['name']):
                    raise ValueError(f'{t["name"]}: duplicate field {f["name"]}')
                fid = make_id('fld', table.id, f['name'])
                table.fields.append(Field(fid, f['name'], ftype, f.get('description'), self._schema_options(table, f)))
        # second pass: wire link fields to their inverse
        for t in spec['tables']:
            table = self.table(t['name'])
            for f in t['fields']:
                if f['type'] != 'multipleRecordLinks':
                    continue
                o = f['options']
                target = self.table(o['linkedTableName'])
                if target is None:
                    raise ValueError(f'{t["name"]}.{f["name"]}: unknown linked table {o["linkedTableName"]}')
                field = table.field(f['name'])
                field.options = {'linkedTableId': target.id, 'isReversed': False,
                                 'prefersSingleRecordLink': bool(o.get('prefersSingleRecordLink', False))}
                inverse = o.get('inverseLinkFieldName')
                if inverse:
                    inv = target.field(inverse)
                    if inv is None or inv.type != 'multipleRecordLinks':
                        raise ValueError(f'{t["name"]}.{f["name"]}: inverse {target.name}.{inverse} missing')
                    field.options['inverseLinkFieldId'] = inv.id
        for t in self.tables:
            for f in t.fields:
                if f.type == 'multipleRecordLinks' and 'inverseLinkFieldId' in f.options:
                    target = self.table(f.options['linkedTableId'])
                    inv = target.field(f.options['inverseLinkFieldId'])
                    if inv.options.get('inverseLinkFieldId') != f.id or inv.options.get('linkedTableId') != t.id:
                        raise ValueError(f'{t.name}.{f.name} and {target.name}.{inv.name} do not point at each other')

    def _schema_options(self, table, f):
        o = copy.deepcopy(f.get('options') or {})
        if f['type'] in ('singleSelect', 'multipleSelects'):
            o['choices'] = [{'id': make_id('sel', table.id, f['name'], c['name']), 'name': c['name'],
                             **({'color': c['color']} if c.get('color') else {})} for c in o.get('choices', [])]
        if f['type'] == 'date':
            name = o['dateFormat']['name']
            o['dateFormat'] = {'name': name, 'format': DATE_FORMATS[name]}
        if f['type'] == 'dateTime':
            dname, tname = o['dateFormat']['name'], o['timeFormat']['name']
            o['dateFormat'] = {'name': dname, 'format': DATE_FORMATS[dname]}
            o['timeFormat'] = {'name': tname, 'format': TIME_FORMATS[tname]}
        if f['type'] == 'multipleRecordLinks':
            return {}
        if f['type'] == 'multipleAttachments':
            return {'isReversed': False}
        return o

    def load_seed(self, seed_dir, today=None):
        """Insert base/seed/*.json directly (no rate limit). today: shift every date so the seed's
        seedToday becomes this date (a date, 'YYYY-MM-DD' or 'auto' for the clock's today)."""
        seed_dir = Path(seed_dir)
        meta = json.loads((seed_dir / '_meta.json').read_text(encoding='utf-8'))
        shift = 0
        if today:
            target = self.server.clock.today() if today == 'auto' else (
                today if isinstance(today, date) else date.fromisoformat(str(today)))
            shift = (target - date.fromisoformat(meta['seedToday'])).days
        self.seed_shift_days = shift
        files = [json.loads((seed_dir / name).read_text(encoding='utf-8')) for name in meta['loadOrder']]
        created = self.server.clock.now()
        # pass 1: records without links
        for spec in files:
            table = self._table_or_raise(spec['table'])
            for i, rec in enumerate(spec['records']):
                ref = rec['ref']
                key = (table.name, ref)
                if key in self.refs:
                    raise ValueError(f'duplicate seed ref {table.name}/{ref}')
                rid = make_id('rec', self.base_id, table.name, ref)
                self.refs[key] = rid
                record = Record(rid, created + timedelta(milliseconds=i))
                table.records[rid] = record
                plain = {k: v for k, v in rec['fields'].items()
                         if table.field(k) is None or table.field(k).type != 'multipleRecordLinks'}
                plain = self._shift(table, plain, shift)
                self._apply_cells(table, record, self._normalize_fields(table, plain, typecast=False, pending=[]))
        # pass 2: links (by seed ref), with inverse sync
        for spec in files:
            table = self.table(spec['table'])
            for rec in spec['records']:
                links = {}
                for k, v in rec['fields'].items():
                    field = table.field(k)
                    if field is None:
                        raise ValueError(f'seed {table.name}/{rec["ref"]}: unknown field {k}')
                    if field.type != 'multipleRecordLinks':
                        continue
                    target = self.table(field.options['linkedTableId'])
                    ids = []
                    for r in v:
                        if (target.name, r) not in self.refs:
                            raise ValueError(f'seed {table.name}/{rec["ref"]}.{k}: unknown ref {target.name}/{r}')
                        ids.append(self.refs[(target.name, r)])
                    links[field.id] = ids
                if links:
                    self._apply_cells(table, table.records[self.refs[(table.name, rec['ref'])]], links)

    @staticmethod
    def _shift(table, fields, days):
        if not days:
            return fields
        out = dict(fields)
        for k, v in fields.items():
            f = table.field(k)
            if f is None or v in (None, ''):
                continue
            if f.type == 'date':
                out[k] = (date.fromisoformat(v) + timedelta(days=days)).isoformat()
            elif f.type == 'dateTime':
                dt = F.parse_datetime(v).dt + timedelta(days=days)
                out[k] = iso_ms(dt)
        return out

    # ------------------------------------------------------------------ direct access for tests

    def ref(self, table, ref):
        """Record id of a seed record, by its seed ref (see base/seed)."""
        return self.refs[(table, ref)]

    def records(self, table, by_id=False):
        """Every record of a table as the API would return it: [{'id', 'createdTime', 'fields'}]."""
        t = self._table_or_raise(table)
        return [self._record_json(t, r, by_id=by_id) for r in t.records.values()]

    def record(self, table, rec_id):
        t = self._table_or_raise(table)
        r = t.records.get(rec_id)
        return self._record_json(t, r) if r else None

    def where(self, table, formula):
        """Records matching an Airtable formula, e.g. where('Orders', "{Cold Pack} = 'Required'")."""
        t = self._table_or_raise(table)
        node = F.parse(formula, {f.name for f in t.fields} | {f.id for f in t.fields})
        return [self._record_json(t, r) for r in t.records.values() if F.matches(node, self._ctx(t, r))]

    def find(self, table, **equals):
        """Records whose fields equal the given values (names with spaces: pass a dict via **{'Order Number': '#1001'})."""
        out = []
        for rec in self.records(table):
            if all(rec['fields'].get(k) == v for k, v in equals.items()):
                out.append(rec)
        return out

    def by_primary(self, table, value):
        t = self._table_or_raise(table)
        found = [r for r in self.records(table) if r['fields'].get(t.primary.name) == value]
        return found[0] if len(found) == 1 else (found or None)

    def count(self, table):
        return len(self._table_or_raise(table).records)

    def insert(self, table, fields, typecast=False):
        """Create a record directly (no HTTP, no rate limit). Returns the record json."""
        t = self._table_or_raise(table)
        pending = []
        cells = self._normalize_fields(t, fields, typecast, pending)
        self._create_pending(pending)
        rec = self._new_record(t)
        self._apply_cells(t, rec, cells)
        return self._record_json(t, rec)

    def update(self, table, rec_id, fields, typecast=False):
        t = self._table_or_raise(table)
        pending = []
        cells = self._normalize_fields(t, fields, typecast, pending)
        self._create_pending(pending)
        self._apply_cells(t, t.records[rec_id], cells)
        return self._record_json(t, t.records[rec_id])

    def primary_name(self, table, rec_id):
        t = self._table_or_raise(table)
        return F.to_str(self._formula_value(t, t.records[rec_id], t.primary))

    # ------------------------------------------------------------------ routing

    def route(self, path):
        if path.startswith('/v0/'):
            return 'airtable'
        return None

    def annotate(self, req, entry):
        parts = [unquote(p) for p in req.path.split('/')[2:]]
        if parts and parts[0] == 'meta':
            entry['api'] = 'meta'
            if len(parts) == 2:
                entry['operation'] = 'meta.list_bases'
            elif len(parts) >= 4 and parts[3] == 'tables':
                t = self.table(parts[4]) if len(parts) > 4 else None
                if t:
                    entry['table'] = t.name
                if len(parts) == 4:
                    entry['operation'] = 'meta.get_schema' if req.method == 'GET' else 'meta.create_table'
                elif len(parts) == 5:
                    entry['operation'] = 'meta.update_table'
                elif len(parts) == 6:
                    entry['operation'] = 'meta.create_field'
                else:
                    entry['operation'] = 'meta.update_field'
            return
        entry['api'] = 'web'
        if len(parts) >= 2:
            t = self.table(parts[1])
            entry['table'] = t.name if t else parts[1]
        body = req.json if isinstance(req.json, dict) else {}
        if len(parts) == 3 and parts[2] == 'listRecords':
            entry['operation'] = 'list'
        elif len(parts) == 3:
            entry['operation'] = {'GET': 'get', 'PATCH': 'update', 'PUT': 'replace', 'DELETE': 'delete'}.get(req.method)
            entry['record_count'] = 1
        elif len(parts) == 2:
            if req.method == 'GET':
                entry['operation'] = 'list'
            elif req.method == 'POST':
                entry['operation'] = 'create'
                entry['record_count'] = len(body.get('records') or []) or (1 if 'fields' in body else 0)
            elif req.method in ('PATCH', 'PUT'):
                entry['operation'] = 'upsert' if body.get('performUpsert') else ('update' if req.method == 'PATCH' else 'replace')
                entry['record_count'] = len(body.get('records') or [])
            elif req.method == 'DELETE':
                entry['operation'] = 'delete'
                entry['record_count'] = len(_array_param(req.query, 'records'))

    def error_body(self, status):
        if status == 429:
            return RATE_LIMIT_BODY
        if status == 503:
            return {'error': {'type': 'RETRIABLE_ERROR', 'message': 'Server encountered an error while processing your request, and it is safe to retry the request'}}
        if status == 502:
            return {'error': {'type': 'BAD_GATEWAY', 'message': 'Bad gateway'}}
        if status == 401:
            return {'error': {'type': 'AUTHENTICATION_REQUIRED', 'message': 'Authentication required'}}
        if status == 422:
            return {'error': {'type': 'INVALID_REQUEST_UNKNOWN', 'message': GENERIC_422}}
        if status == 404:
            return {'error': 'NOT_FOUND'}
        if status == 403:
            return {'error': {'type': 'INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND',
                              'message': 'Invalid permissions, or the requested model was not found. Check that both your '
                                         'user and your token have the required permissions, and that the model names '
                                         'and/or ids are correct.'}}
        if status == 413:
            return {'error': {'type': 'REQUEST_TOO_LARGE', 'message': 'Request body is too large'}}
        return {'error': {'type': 'SERVER_ERROR', 'message': 'Unexpected server error'}}

    # ------------------------------------------------------------------ request handling

    def handle(self, req, entry):
        try:
            return self._handle(req, entry)
        except AirtableError as e:
            entry['error_type'] = e.type
            return json_response(e.status, e.body())

    def _handle(self, req, entry):
        auth = req.headers.get('authorization')
        if not auth:
            raise AirtableError(401, 'AUTHENTICATION_REQUIRED', 'Authentication required')
        if auth != 'Bearer ' + self.token:
            raise AirtableError(401, 'AUTHENTICATION_REQUIRED', 'Invalid authentication token')
        parts = [unquote(p) for p in req.path.split('/')[2:]]
        if parts and parts[0] == 'meta':
            if len(parts) >= 3:
                self._check_base(parts[2])
                self._rate_limit()
            return self._meta(req, parts)
        if not parts or not parts[0]:
            raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
        self._check_base(parts[0])
        self._rate_limit()
        if len(parts) < 2 or not parts[1]:
            raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
        table = self.table(parts[1])
        if table is None:
            raise _err('INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND',
                       'Invalid permissions, or the requested model was not found. Check that both your user and your '
                       'token have the required permissions, and that the model names and/or ids are correct.', 403)
        if req.method in ('POST', 'PATCH', 'PUT') and req.raw and req.json_error:
            raise AirtableError(400, 'INVALID_REQUEST_BODY', 'Could not parse request body')
        if len(parts) == 3 and parts[2] == 'listRecords':
            if req.method != 'POST':
                raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
            return self._list(table, self._list_params_from_body(req.json or {}))
        if len(parts) == 3:
            return self._single(req, table, parts[2])
        if len(parts) != 2:
            raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
        if req.method == 'GET':
            # "Airtable's API only accepts request with a URL shorter than 16,000 characters" (List records). The
            # status and body for a longer URL are not documented; the mock answers 414. Use POST .../listRecords.
            if len('https://api.airtable.com') + len(req.target) >= URL_LIMIT:
                raise AirtableError(414, 'URL_TOO_LONG', 'The request URL is 16,000 characters or longer. Send the '
                                                         'parameters in the body of POST /v0/{baseId}/{table}/listRecords.')
            return self._list(table, self._list_params_from_query(req))
        if req.method == 'POST':
            return self._create(table, req.json)
        if req.method in ('PATCH', 'PUT'):
            return self._update(table, req.json, destructive=req.method == 'PUT')
        if req.method == 'DELETE':
            return self._delete(table, req)
        raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')

    def _check_base(self, base_id):
        if base_id != self.base_id:
            raise _err('INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND',
                       'Invalid permissions, or the requested model was not found. Check that both your user and your '
                       'token have the required permissions, and that the model names and/or ids are correct.', 403)

    def _rate_limit(self):
        if not self.rate['enabled']:
            return
        now = time.monotonic()
        if now < self._blocked_until:
            raise AirtableError(429, 'RATE_LIMIT_REACHED', RATE_LIMIT_MESSAGE)
        while self._hits and now - self._hits[0] >= 1.0:
            self._hits.popleft()
        if len(self._hits) >= self.rate['per_second']:
            self._blocked_until = now + self.rate['penalty']
            raise AirtableError(429, 'RATE_LIMIT_REACHED', RATE_LIMIT_MESSAGE)
        self._hits.append(now)

    # ------------------------------------------------------------------ list

    def _list_params_from_query(self, req):
        params = {}
        for key in req.query:
            base = re.sub(r'(\[[^\]]*\])+$', '', key)
            if base not in LIST_PARAMS:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        params['fields'] = _array_param(req.query, 'fields') or None
        sort = _sort_param(req.query)
        params['sort'] = sort or None
        for name in ('pageSize', 'maxRecords', 'offset', 'view', 'filterByFormula', 'cellFormat', 'timeZone', 'userLocale',
                     'returnFieldsByFieldId'):
            if name in req.query:
                params[name] = req.query[name][-1]
        return params

    def _list_params_from_body(self, body):
        if not isinstance(body, dict):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        for key in body:
            if key not in LIST_PARAMS:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        params = dict(body)
        if 'fields' in params and not (isinstance(params['fields'], list) and all(isinstance(x, str) for x in params['fields'])):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        if 'sort' in params and not (isinstance(params['sort'], list) and all(isinstance(x, dict) for x in params['sort'])):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        return params

    def _list(self, table, p):
        page_size = _int_param(p.get('pageSize'), 100)
        if page_size < 1 or page_size > 100:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        max_records = _int_param(p.get('maxRecords'), None)
        if max_records is not None and max_records < 1:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        by_id = _bool_param(p.get('returnFieldsByFieldId'), False)
        cell_format = p.get('cellFormat', 'json')
        if cell_format not in ('json', 'string'):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        if cell_format == 'string' and not (p.get('timeZone') and p.get('userLocale')):
            raise _err('INVALID_REQUEST_UNKNOWN', 'The cellFormat "string" requires the timeZone and userLocale parameters')
        view = p.get('view')
        if view and view not in ('Grid view', table.view_id):
            raise _err('VIEW_NAME_NOT_FOUND', f'Could not find view {view} in table {table.name}')
        fields = p.get('fields')
        if fields:
            for name in fields:
                if table.field(name) is None:
                    raise _err('UNKNOWN_FIELD_NAME', f'Unknown field name: "{name}"')
        sort = p.get('sort') or []
        for s in sort:
            if set(s) - {'field', 'direction'} or 'field' not in s:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
            if table.field(s['field']) is None:
                raise _err('UNKNOWN_FIELD_NAME', f'Unknown field name: "{s["field"]}"')
            if s.get('direction', 'asc') not in ('asc', 'desc'):
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)

        offset = p.get('offset')
        if offset:
            itr_id, _, start_id = str(offset).partition('/')
            if itr_id in self._expired:
                raise _err('LIST_RECORDS_ITERATOR_NOT_AVAILABLE',
                           'The list records iterator is not available. Restart the request from the first page.')
            itr = self._iterators.get(itr_id)
            if not itr or start_id not in itr:
                raise _err('INVALID_OFFSET_VALUE', f'The value of offset {offset} is invalid')
            ids = itr
            start = ids.index(start_id)
        else:
            formula = (p.get('filterByFormula') or '').strip()
            records = list(table.records.values())
            if formula:
                try:
                    node = F.parse(formula, {f.name for f in table.fields} | {f.id for f in table.fields})
                except F.UnknownFieldsError as e:
                    raise _err('INVALID_FILTER_BY_FORMULA',
                               f'The formula for filtering records is invalid: Unknown field names: {", ".join(e.names).lower()}')
                except F.FormulaSyntaxError:
                    raise _err('INVALID_FILTER_BY_FORMULA',
                               'The formula for filtering records is invalid: Invalid formula. Please check your formula text.')
                records = [r for r in records if F.matches(node, self._ctx(table, r))]
            for s in reversed(sort):
                field = table.field(s['field'])
                desc = s.get('direction', 'asc') == 'desc'
                records.sort(key=lambda r: self._sort_key(table, r, field, desc), reverse=desc)
            ids = [r.id for r in records]
            if max_records is not None:
                ids = ids[:max_records]
            start = 0
        page = ids[start:start + page_size]
        out = {'records': [self._record_json(table, table.records[i], fields, by_id, cell_format, p.get('timeZone'),
                                             p.get('userLocale')) for i in page if i in table.records]}
        if start + page_size < len(ids):
            self._counter += 1
            itr_id = make_id('itr', self.base_id, 'iterator', self._counter)
            self._iterators[itr_id] = ids
            out['offset'] = f'{itr_id}/{ids[start + page_size]}'
        return json_response(200, out)

    def _sort_key(self, table, record, field, desc):
        v = self._formula_value(table, record, field)
        blank = F._is_blankish(v) and not (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0)
        if blank:
            # empty values first in ascending order, last in descending order
            return (0, '') if not desc else (-1, '')
        if isinstance(v, F.DT):
            return (1, v.dt.timestamp())
        if isinstance(v, bool):
            return (1, int(v))
        if isinstance(v, (int, float)):
            return (1, v)
        return (2, F.to_str(v).lower())

    # ------------------------------------------------------------------ single record

    def _single(self, req, table, rec_id):
        record = table.records.get(rec_id)
        if req.method == 'GET':
            if record is None:
                raise AirtableError(404, 'MODEL_ID_NOT_FOUND', 'Record not found')
            by_id = _bool_param(req.q('returnFieldsByFieldId'), False)
            return json_response(200, self._record_json(table, record, by_id=by_id))
        if req.method in ('PATCH', 'PUT'):
            body = req.json
            if not isinstance(body, dict) or set(body) - {'fields', 'typecast', 'returnFieldsByFieldId'} or 'fields' not in body:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
            if record is None:
                raise AirtableError(404, 'MODEL_ID_NOT_FOUND', 'Record not found')
            typecast = _bool_body(body, 'typecast')
            pending = []
            cells = self._normalize_fields(table, body['fields'], typecast, pending)
            if req.method == 'PUT':
                cells = self._replace_cells(table, cells)
            self._create_pending(pending)
            self._apply_cells(table, record, cells)
            return json_response(200, self._record_json(table, record, by_id=_bool_body(body, 'returnFieldsByFieldId')))
        if req.method == 'DELETE':
            if record is None:
                raise AirtableError(404, 'MODEL_ID_NOT_FOUND', 'Record not found')
            self._delete_record(table, rec_id)
            return json_response(200, {'id': rec_id, 'deleted': True})
        raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')

    # ------------------------------------------------------------------ create

    def _create(self, table, body):
        if not isinstance(body, dict):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        if set(body) - {'records', 'fields', 'typecast', 'returnFieldsByFieldId'}:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        typecast = _bool_body(body, 'typecast')
        by_id = _bool_body(body, 'returnFieldsByFieldId')
        single = 'fields' in body and 'records' not in body
        records = [{'fields': body['fields']}] if single else body.get('records')
        _check_batch(records, 'create')
        pending, staged = [], []
        for i, r in enumerate(records):
            if not isinstance(r, dict) or set(r) - {'fields'} or not isinstance(r.get('fields', {}), dict):
                raise _err('INVALID_RECORDS', f'Invalid record at index {i}: only "fields" is allowed when creating')
            staged.append(self._normalize_fields(table, r.get('fields', {}), typecast, pending))
        self._create_pending(pending)
        created = []
        for cells in staged:
            rec = self._new_record(table)
            self._apply_cells(table, rec, cells)
            created.append(rec)
        if single:
            return json_response(200, self._record_json(table, created[0], by_id=by_id))
        return json_response(200, {'records': [self._record_json(table, r, by_id=by_id) for r in created]})

    # ------------------------------------------------------------------ update and upsert

    def _update(self, table, body, destructive):
        if not isinstance(body, dict) or set(body) - {'records', 'performUpsert', 'typecast', 'returnFieldsByFieldId'}:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        records = body.get('records')
        _check_batch(records, 'update')
        typecast = _bool_body(body, 'typecast')
        by_id = _bool_body(body, 'returnFieldsByFieldId')
        upsert = body.get('performUpsert')
        merge_fields = None
        if upsert is not None:
            if not isinstance(upsert, dict) or set(upsert) != {'fieldsToMergeOn'}:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
            names = upsert['fieldsToMergeOn']
            if not isinstance(names, list) or not 1 <= len(names) <= 3:
                raise _err('INVALID_REQUEST_UNKNOWN', 'performUpsert.fieldsToMergeOn must have between 1 and 3 fields')
            merge_fields = []
            for n in names:
                f = table.field(n) if isinstance(n, str) else None
                if f is None:
                    raise _err('UNKNOWN_FIELD_NAME', f'Unknown field name: "{n}"')
                if f.type not in MERGE_TYPES:
                    # the real error body for this case is not documented; the rule itself is
                    raise _err('INVALID_REQUEST_UNKNOWN',
                               f'Field "{f.name}" of type {f.type} cannot be used in fieldsToMergeOn. Use a number, '
                               'single line text, long text, single select, multiple select or date field.')
                merge_fields.append(f)
        pending, plan = [], []
        seen_keys = {}
        for i, r in enumerate(records):
            if not isinstance(r, dict) or set(r) - {'id', 'fields'} or not isinstance(r.get('fields'), dict):
                raise _err('INVALID_RECORDS', f'Invalid record at index {i}')
            cells = self._normalize_fields(table, r['fields'], typecast, pending)
            rid = r.get('id')
            if rid is not None:
                if not isinstance(rid, str) or rid not in table.records:
                    raise AirtableError(404, 'MODEL_ID_NOT_FOUND', 'Record not found')
                plan.append(('update', rid, cells))
                continue
            if merge_fields is None:
                raise _err('INVALID_RECORDS', f'Record at index {i} is missing an id')
            key = []
            for f in merge_fields:
                if f.name not in r['fields'] and f.id not in r['fields'] or cells.get(f.id) in (None, '', []):
                    raise _err('INVALID_REQUEST_UNKNOWN',
                               f'Record at index {i} is missing a value for the fieldsToMergeOn field "{f.name}"')
                key.append(json.dumps(cells[f.id], sort_keys=True))
            key = tuple(key)
            if key in seen_keys:
                raise _err('INVALID_REQUEST_UNKNOWN',
                           f'Records at indexes {seen_keys[key]} and {i} have the same values for fieldsToMergeOn')
            seen_keys[key] = i
            matches = [rec.id for rec in table.records.values()
                       if all(json.dumps(rec.cells.get(f.id), sort_keys=True) == k for f, k in zip(merge_fields, key))]
            if len(matches) > 1:
                # the body Airtable returns when the merge key matches several records
                raise _err('INVALID_VALUE_FOR_COLUMN', UPSERT_MULTI_MATCH)
            plan.append(('update', matches[0], cells) if matches else ('create', None, cells))
        self._create_pending(pending)
        out, created_ids, updated_ids = [], [], []
        for action, rid, cells in plan:
            if action == 'create':
                rec = self._new_record(table)
                self._apply_cells(table, rec, cells)
                created_ids.append(rec.id)
            else:
                rec = table.records[rid]
                self._apply_cells(table, rec, self._replace_cells(table, cells) if destructive else cells)
                updated_ids.append(rec.id)
            out.append(self._record_json(table, rec, by_id=by_id))
        resp = {'records': out}
        if merge_fields is not None:
            resp['createdRecords'] = created_ids
            resp['updatedRecords'] = updated_ids
        return json_response(200, resp)

    def _replace_cells(self, table, cells):
        full = {f.id: None for f in table.fields if f.type not in COMPUTED_TYPES}
        full.update(cells)
        return full

    # ------------------------------------------------------------------ delete

    def _delete(self, table, req):
        ids = _array_param(req.query, 'records')
        if set(req.query) - {k for k in req.query if re.sub(r'(\[[^\]]*\])+$', '', k) == 'records'}:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        _check_batch(ids, 'delete')
        for rid in ids:
            if rid not in table.records:
                raise AirtableError(404, 'MODEL_ID_NOT_FOUND', 'Record not found')
        for rid in ids:
            self._delete_record(table, rid)
        return json_response(200, {'records': [{'id': rid, 'deleted': True} for rid in ids]})

    def _delete_record(self, table, rid):
        table.records.pop(rid)
        for t in self.tables:
            for f in t.fields:
                if f.type == 'multipleRecordLinks' and f.options.get('linkedTableId') == table.id:
                    for rec in t.records.values():
                        if rid in (rec.cells.get(f.id) or []):
                            rec.cells[f.id] = [x for x in rec.cells[f.id] if x != rid]
                            if not rec.cells[f.id]:
                                del rec.cells[f.id]

    # ------------------------------------------------------------------ values

    def _new_record(self, table):
        self._counter += 1
        rid = make_id('rec', self.base_id, table.name, 'api', self._counter)
        rec = Record(rid, self.server.clock.now())
        table.records[rid] = rec
        return rec

    def _normalize_fields(self, table, fields, typecast, pending):
        if not isinstance(fields, dict):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        out = {}
        for key, value in fields.items():
            field = table.field(key)
            if field is None:
                raise _err('UNKNOWN_FIELD_NAME', f'Unknown field name: "{key}"')
            if field.id in out:
                raise _err('INVALID_REQUEST_UNKNOWN', f'Field "{field.name}" was given more than once')
            out[field.id] = self._coerce(table, field, value, typecast, pending)
        return out

    def _coerce(self, table, field, value, typecast, pending):
        t = field.type
        bad = _err('INVALID_VALUE_FOR_COLUMN', f'Field "{field.name}" cannot accept the provided value')
        if t in COMPUTED_TYPES:
            raise _err('INVALID_VALUE_FOR_COLUMN', f'Field "{field.name}" cannot accept a value because the field is computed')
        if value is None:
            return None
        if t in TEXT_TYPES:
            if isinstance(value, str):
                return value
            if typecast and isinstance(value, (int, float)) and not isinstance(value, bool):
                return F._fmt_num(value)
            raise bad
        if t in NUMBER_TYPES:
            if isinstance(value, bool):
                raise bad
            if isinstance(value, str) and typecast:
                try:
                    value = float(value) if any(c in value for c in '.eE') else int(value)
                except ValueError:
                    raise bad
            if not isinstance(value, (int, float)):
                raise bad
            if t == 'rating':
                if not float(value).is_integer() or not 1 <= value <= field.options.get('max', 5):
                    raise bad
                value = int(value)
            if t == 'number' and field.options.get('precision', 0) == 0 and isinstance(value, float) and value.is_integer():
                value = int(value)
            return value
        if t == 'checkbox':
            if isinstance(value, bool):
                return True if value else None
            if typecast and isinstance(value, str) and value.lower() in ('true', 'false', 'checked', 'yes', 'no', '1', '0', ''):
                return True if value.lower() in ('true', 'checked', 'yes', '1') else None
            raise bad
        if t == 'singleSelect':
            if not isinstance(value, str):
                if typecast and isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = F._fmt_num(value)
                else:
                    raise bad
            if value == '':
                return None
            return self._choice(table, field, value, typecast)
        if t == 'multipleSelects':
            if isinstance(value, str) and typecast:
                value = [v.strip() for v in value.split(',') if v.strip()]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise bad
            out = []
            for v in value:
                c = self._choice(table, field, v, typecast)
                if c not in out:
                    out.append(c)
            return out or None
        if t == 'date':
            if not isinstance(value, str):
                raise bad
            if value == '':
                return None
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                try:
                    date.fromisoformat(value)
                    return value
                except ValueError:
                    raise bad
            parsed = F.parse_datetime(value) if typecast else None
            if parsed:
                return parsed.dt.strftime('%Y-%m-%d')
            raise bad
        if t == 'dateTime':
            if not isinstance(value, str):
                raise bad
            if value == '':
                return None
            parsed = F.parse_datetime(value)
            if parsed is None or (parsed.date_only and not typecast):
                raise bad
            return iso_ms(parsed.dt)
        if t == 'multipleRecordLinks':
            target = self.table(field.options['linkedTableId'])
            if isinstance(value, str) and typecast:
                value = [value]
            if not isinstance(value, list):
                raise bad
            out = []
            for v in value:
                if not isinstance(v, str):
                    raise _err('INVALID_VALUE_FOR_COLUMN', f'Value "{json.dumps(v)}" is not a valid record ID.')
                if REC_ID.match(v):
                    if v not in target.records:
                        raise _err('ROW_DOES_NOT_EXIST', f'Record ID {v} does not exist in this table')
                    rid = v
                elif typecast:
                    rid = self._match_or_queue(target, v, pending)
                else:
                    raise _err('INVALID_VALUE_FOR_COLUMN', f'Value "{v}" is not a valid record ID.')
                if rid not in out:
                    out.append(rid)
            return out or None
        if t == 'multipleAttachments':
            if not isinstance(value, list):
                raise bad
            out = []
            for a in value:
                if not isinstance(a, dict) or not isinstance(a.get('url'), str) and 'id' not in a:
                    raise bad
                if set(a) - {'url', 'filename', 'id'}:
                    raise bad
                url = a.get('url', '')
                out.append({'id': a.get('id') or make_id('att', url, len(out)), 'url': url,
                            'filename': a.get('filename') or url.rsplit('/', 1)[-1] or 'file',
                            'size': 1024, 'type': 'image/jpeg' if url.lower().endswith(('.jpg', '.jpeg')) else 'application/octet-stream'})
            return out or None
        if t == 'barcode':
            if isinstance(value, dict) and isinstance(value.get('text'), str):
                return {'text': value['text'], 'type': value.get('type')}
            raise bad
        raise bad

    def _choice(self, table, field, value, typecast):
        for c in field.options.get('choices', []):
            if c['name'] == value:
                return value
        if typecast:
            field.options.setdefault('choices', []).append(
                {'id': make_id('sel', table.id, field.name, value), 'name': value, 'color': 'grayLight2'})
            return value
        raise _err('INVALID_MULTIPLE_CHOICE_OPTIONS', f'Insufficient permissions to create new select option ""{value}""')

    def _match_or_queue(self, target, text, pending):
        for rec in target.records.values():
            if F.to_str(self._formula_value(target, rec, target.primary)) == text:
                return rec.id
        for p in pending:
            if p['table'] is target and p['value'] == text:
                return p['id']
        self._counter += 1
        rid = make_id('rec', self.base_id, target.name, 'typecast', self._counter)
        pending.append({'table': target, 'value': text, 'id': rid})
        return rid

    def _create_pending(self, pending):
        for p in pending:
            t = p['table']
            rec = Record(p['id'], self.server.clock.now())
            t.records[rec.id] = rec
            self._apply_cells(t, rec, {t.primary.id: self._coerce(t, t.primary, p['value'], True, [])})

    def _apply_cells(self, table, record, cells):
        for fid, value in cells.items():
            field = table.field(fid)
            old = record.cells.get(fid)
            if value in (None, '', []):
                record.cells.pop(fid, None)
            else:
                record.cells[fid] = value
            if field.type == 'multipleRecordLinks' and field.options.get('inverseLinkFieldId'):
                target = self.table(field.options['linkedTableId'])
                inv_id = field.options['inverseLinkFieldId']
                before, after = set(old or []), set(value or [])
                for rid in before - after:
                    other = target.records.get(rid)
                    if other and record.id in (other.cells.get(inv_id) or []):
                        other.cells[inv_id] = [x for x in other.cells[inv_id] if x != record.id]
                        if not other.cells[inv_id]:
                            del other.cells[inv_id]
                for rid in value or []:
                    if rid in after - before:
                        other = target.records[rid]
                        links = other.cells.setdefault(inv_id, [])
                        if record.id not in links:
                            links.append(record.id)

    # ------------------------------------------------------------------ reading

    def _record_json(self, table, record, fields=None, by_id=False, cell_format='json', tz=None, locale=None):
        wanted = None
        if fields:
            wanted = {table.field(n).id for n in fields}
        out = {}
        for f in table.fields:
            if wanted is not None and f.id not in wanted:
                continue
            v = record.cells.get(f.id)
            # "empty" means "", [] or false; a number 0 is a value and is returned (0 == False in Python, so test
            # the type, not membership in a tuple)
            if v is None or v is False or (isinstance(v, (str, list)) and len(v) == 0):
                continue
            key = f.id if by_id else f.name
            out[key] = self._string_cell(table, f, v, locale) if cell_format == 'string' else copy.deepcopy(v)
        return {'id': record.id, 'createdTime': iso_ms(record.created), 'fields': out}

    def _string_cell(self, table, f, v, locale):
        if f.type == 'multipleRecordLinks':
            target = self.table(f.options['linkedTableId'])
            return ', '.join(self.primary_name(target.name, rid) for rid in v if rid in target.records)
        if f.type == 'multipleSelects':
            return ', '.join(v)
        if f.type == 'checkbox':
            return 'checked'
        if f.type == 'currency':
            return f"{f.options.get('symbol', '$')}{v:,.{f.options.get('precision', 2)}f}"
        if f.type == 'date':
            d = date.fromisoformat(v)
            return f'{d.month}/{d.day}/{d.year}' if (locale or '').lower().startswith('en-us') else v
        if f.type == 'multipleAttachments':
            return ', '.join(f"{a['filename']} ({a['url']})" for a in v)
        return F.to_str(v) if not isinstance(v, str) else v

    def _formula_value(self, table, record, field):
        v = record.cells.get(field.id)
        t = field.type
        if t == 'checkbox':
            return bool(v)
        if v is None:
            if t in ('multipleSelects', 'multipleRecordLinks', 'multipleAttachments'):
                return F.Arr()
            return F.BLANK
        if t in ('date', 'dateTime', 'createdTime', 'lastModifiedTime'):
            return F.parse_datetime(v)
        if t == 'multipleSelects':
            return F.Arr(v)
        if t == 'multipleRecordLinks':
            target = self.table(field.options['linkedTableId'])
            return F.Arr(F.to_str(self._formula_value(target, target.records[r], target.primary))
                         for r in v if r in target.records)
        if t == 'multipleAttachments':
            return F.Arr(f"{a['filename']} ({a['url']})" for a in v)
        if t == 'barcode':
            return v.get('text', '')
        return v

    def _ctx(self, table, record):
        def get(name):
            f = table.field(name)
            return self._formula_value(table, record, f)
        return F.Context(get, self.server.clock.now(), record.id, record.created)

    # ------------------------------------------------------------------ metadata API

    def _meta(self, req, parts):
        if len(parts) == 2 and parts[1] == 'bases':
            if req.method != 'GET':
                raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
            return json_response(200, {'bases': [{'id': self.base_id, 'name': self.base_name, 'permissionLevel': 'create'}]})
        if len(parts) < 4 or parts[1] != 'bases' or parts[3] != 'tables':
            raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
        body = req.json if isinstance(req.json, dict) else {}
        if req.method in ('POST', 'PATCH') and (req.json_error or not isinstance(req.json, dict)):
            raise AirtableError(400, 'INVALID_REQUEST_BODY', 'Could not parse request body')
        if len(parts) == 4:
            if req.method == 'GET':
                include = _array_param(req.query, 'include')
                return json_response(200, {'tables': [t.model('visibleFieldIds' in include) for t in self.tables]})
            if req.method == 'POST':
                return json_response(200, self._meta_create_table(body))
            raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')
        table = self.table(parts[4])
        if table is None:
            raise _err('INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND',
                       'Invalid permissions, or the requested model was not found. Check that both your user and your '
                       'token have the required permissions, and that the model names and/or ids are correct.', 403)
        if len(parts) == 5 and req.method == 'PATCH':
            if set(body) - {'name', 'description'} or not body:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
            if 'name' in body:
                other = next((t for t in self.tables if t.name.lower() == str(body['name']).lower() and t is not table), None)
                if other or not str(body['name']).strip():
                    raise _err('DUPLICATE_TABLE_NAME', f'A table named "{body["name"]}" already exists')
                table.name = body['name']
            if 'description' in body:
                table.description = body['description']
            return json_response(200, {'id': table.id, 'name': table.name, 'description': table.description})
        if len(parts) == 6 and parts[5] == 'fields' and req.method == 'POST':
            field = self._meta_create_field(table, body)
            return json_response(200, field.model())
        if len(parts) == 7 and parts[5] == 'fields' and req.method == 'PATCH':
            field = table.field(parts[6])
            if field is None:
                raise _err('INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND', 'Could not find field', 403)
            if set(body) - {'name', 'description'} or not body:
                raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
            if 'name' in body:
                clash = table.field_ci(str(body['name']))
                if not str(body['name']).strip() or (clash and clash is not field):
                    raise _err('DUPLICATE_OR_EMPTY_FIELD_NAME', f'Field name "{body["name"]}" is empty or already used')
                field.name = body['name']
            if 'description' in body:
                field.description = body['description']
            return json_response(200, field.model())
        raise AirtableError(404, 'NOT_FOUND', 'Could not find what you are looking for')

    def _meta_create_table(self, body):
        if set(body) - {'name', 'description', 'fields'} or not isinstance(body.get('name'), str) or not body['name'].strip():
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        if any(t.name.lower() == body['name'].lower() for t in self.tables):
            raise _err('DUPLICATE_TABLE_NAME', f'A table named "{body["name"]}" already exists')
        fields = body.get('fields')
        if not isinstance(fields, list) or not fields:
            raise _err('INVALID_REQUEST_UNKNOWN', 'A table needs at least one field')
        if not isinstance(fields[0], dict) or fields[0].get('type') not in PRIMARY_TYPES:
            raise _err('INVALID_FIELD_TYPE_FOR_PRIMARY_FIELD',
                       f'Field type {fields[0].get("type") if isinstance(fields[0], dict) else None} cannot be the primary field')
        table = Table(make_id('tbl', self.base_id, body['name'], 'api', len(self.tables)), body['name'], body.get('description'))
        names = set()
        for f in fields:
            n = (f.get('name') or '').lower() if isinstance(f, dict) else ''
            if not n.strip() or n in names:
                raise _err('DUPLICATE_OR_EMPTY_FIELD_NAME', f'Field name "{f.get("name") if isinstance(f, dict) else f}" is empty or already used')
            names.add(n)
        self.tables.append(table)
        try:
            for f in fields:
                self._meta_create_field(table, f)
        except AirtableError:
            self.tables.remove(table)
            raise
        return table.model()

    def _meta_create_field(self, table, spec):
        if not isinstance(spec, dict) or set(spec) - {'name', 'type', 'description', 'options'}:
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
        name, ftype = spec.get('name'), spec.get('type')
        if not isinstance(name, str) or not name.strip() or table.field_ci(name):
            raise _err('DUPLICATE_OR_EMPTY_FIELD_NAME', f'Field name "{name}" is empty or already used')
        if ftype in NOT_CREATABLE:
            # Note: the current Create field reference also shows request bodies for formula, rollup and lookup
            # fields. The base does not use computed fields and the mock cannot compute them, so it refuses them.
            raise _err('UNSUPPORTED_FIELD_TYPE_FOR_CREATE', f'Creating {ftype} fields is not supported by this mock')
        if ftype not in WRITABLE_TYPES:
            raise _err('INVALID_FIELD_TYPE', f'Unknown or unsupported field type: {ftype}')
        options = self._validate_options(table, name, ftype, spec.get('options'))
        fid = make_id('fld', table.id, name, 'api', len(table.fields))
        field = Field(fid, name, ftype, spec.get('description'), options)
        table.fields.append(field)
        if ftype == 'multipleRecordLinks':
            target = self.table(options['linkedTableId'])
            if target is not table:
                inv_name, n = table.name, 2
                while target.field_ci(inv_name):
                    inv_name = f'{table.name} {n}'
                    n += 1
                inv = Field(make_id('fld', target.id, inv_name, 'api', len(target.fields)), inv_name, 'multipleRecordLinks',
                            None, {'linkedTableId': table.id, 'isReversed': False, 'prefersSingleRecordLink': False,
                                   'inverseLinkFieldId': fid})
                target.fields.append(inv)
                field.options['inverseLinkFieldId'] = inv.id
        return field

    def _validate_options(self, table, name, ftype, options):
        bad = _err('INVALID_FIELD_TYPE_OPTIONS_FOR_CREATE', f'Invalid options for field "{name}" of type {ftype}')
        o = options
        if ftype in TEXT_TYPES or ftype == 'barcode':
            if o not in (None, {}):
                raise bad
            return {}
        if ftype == 'multipleAttachments':
            # the field model lists no write options for attachments: send the type only
            if o not in (None, {}):
                raise bad
            return {'isReversed': False}
        if not isinstance(o, dict):
            raise bad
        if ftype in ('number', 'percent'):
            if set(o) != {'precision'} or not isinstance(o['precision'], int) or not 0 <= o['precision'] <= 8:
                raise bad
            return dict(o)
        if ftype == 'currency':
            if set(o) != {'precision', 'symbol'} or not isinstance(o['precision'], int) or not 0 <= o['precision'] <= 7 \
                    or not isinstance(o['symbol'], str):
                raise bad
            return dict(o)
        if ftype == 'rating':
            if set(o) != {'icon', 'color', 'max'} or o['icon'] not in {'star', 'heart', 'thumbsUp', 'flag', 'dot'} \
                    or not isinstance(o['max'], int) or not 1 <= o['max'] <= 10 or o['color'] not in COLORS:
                raise bad
            return dict(o)
        if ftype == 'duration':
            if set(o) != {'durationFormat'}:
                raise bad
            return dict(o)
        if ftype == 'checkbox':
            if set(o) != {'icon', 'color'} or o['icon'] not in CHECKBOX_ICONS or not str(o['color']).endswith('Bright') \
                    or o['color'] not in COLORS:
                raise bad
            return dict(o)
        if ftype in ('singleSelect', 'multipleSelects'):
            if set(o) != {'choices'} or not isinstance(o['choices'], list):
                raise bad
            seen, choices = set(), []
            for c in o['choices']:
                if not isinstance(c, dict) or set(c) - {'name', 'color', 'id'} or not isinstance(c.get('name'), str) \
                        or not c['name'].strip() or c['name'] in seen or ('color' in c and c['color'] not in COLORS):
                    raise bad
                seen.add(c['name'])
                choices.append({'id': make_id('sel', table.id, name, c['name']), 'name': c['name'],
                                **({'color': c['color']} if 'color' in c else {})})
            return {'choices': choices}
        if ftype == 'date':
            if set(o) != {'dateFormat'} or not isinstance(o['dateFormat'], dict) or o['dateFormat'].get('name') not in DATE_FORMATS:
                raise bad
            n = o['dateFormat']['name']
            if 'format' in o['dateFormat'] and o['dateFormat']['format'] != DATE_FORMATS[n]:
                raise bad
            return {'dateFormat': {'name': n, 'format': DATE_FORMATS[n]}}
        if ftype == 'dateTime':
            if set(o) != {'timeZone', 'dateFormat', 'timeFormat'} or not isinstance(o['timeZone'], str) \
                    or not isinstance(o['dateFormat'], dict) or o['dateFormat'].get('name') not in DATE_FORMATS \
                    or not isinstance(o['timeFormat'], dict) or o['timeFormat'].get('name') not in TIME_FORMATS:
                raise bad
            d, tf = o['dateFormat']['name'], o['timeFormat']['name']
            return {'timeZone': o['timeZone'], 'dateFormat': {'name': d, 'format': DATE_FORMATS[d]},
                    'timeFormat': {'name': tf, 'format': TIME_FORMATS[tf]}}
        if ftype == 'multipleRecordLinks':
            if set(o) - {'linkedTableId', 'viewIdForRecordSelection'} or 'linkedTableId' not in o:
                raise bad
            target = self.table(o['linkedTableId'])
            if target is None or target.id != o['linkedTableId']:
                raise _err('INVALID_FIELD_TYPE_OPTIONS_FOR_CREATE', f'Linked table {o["linkedTableId"]} was not found')
            return {'linkedTableId': target.id, 'isReversed': False, 'prefersSingleRecordLink': False}
        raise bad


# ---------------------------------------------------------------------------------------------- param helpers

def _array_param(query, name):
    """fields[]=a&fields[]=b, fields[0]=a, or fields=a&fields=b"""
    out = []
    keys = [k for k in query if re.sub(r'\[\d*\]$', '', k) == name]
    keys.sort(key=lambda k: int(re.search(r'\[(\d+)\]$', k).group(1)) if re.search(r'\[(\d+)\]$', k) else -1)
    for k in keys:
        out.extend(query[k])
    return out


def _sort_param(query):
    entries = {}
    for k, values in query.items():
        m = re.fullmatch(r'sort\[(\d*)\]\[(field|direction)\]', k)
        if m:
            idx = int(m.group(1) or 0)
            entries.setdefault(idx, {})[m.group(2)] = values[-1]
        elif k.startswith('sort'):
            raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
    return [entries[i] for i in sorted(entries)]


def _int_param(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)


def _bool_param(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if str(value).lower() in ('true', '1'):
        return True
    if str(value).lower() in ('false', '0'):
        return False
    raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)


def _bool_body(body, key):
    if key not in body:
        return False
    if not isinstance(body[key], bool):
        raise _err('INVALID_REQUEST_UNKNOWN', GENERIC_422)
    return body[key]


_PAST = {'create': 'created', 'update': 'updated', 'delete': 'deleted'}


def _check_batch(items, verb):
    if not isinstance(items, list) or not items:
        raise _err('INVALID_RECORDS', f'"records" must be a non-empty array of records to {verb}')
    if len(items) > 10:
        # same wording as Airtable: "A maximum of 10 records can be created per request but you have provided 32."
        raise _err('INVALID_RECORDS', f'A maximum of 10 records can be {_PAST.get(verb, verb)} per request but you have '
                                      f'provided {len(items)}.')
