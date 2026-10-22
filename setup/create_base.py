"""Create the Demo Chocolate Co. tables and fields in an Airtable base, then load the seed data.

Uses only the official Airtable APIs:
  Metadata API  GET  /v0/meta/bases/{baseId}/tables                      read what exists
                POST /v0/meta/bases/{baseId}/tables                      create a table (non link fields)
                POST /v0/meta/bases/{baseId}/tables/{tableId}/fields     create a field (links need the target table id)
                PATCH /v0/meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}   name the inverse link field
  Web API       POST /v0/{baseId}/{tableId}                              create records, 10 per request
                PATCH /v0/{baseId}/{tableId}                             set links, 10 per request

Safe to run again:
  * tables and fields that already exist (same name) are skipped; a type mismatch is reported, never changed
  * seed records are remembered in setup/.seed-state.local.json (git ignored), so a second run creates nothing new;
    a table that already has records and no saved state is left alone instead of filling it with duplicates, and
    its records are matched to the seed by primary field value so the other tables can still link to them
  * requests are paced to 4 per second (the limit is 5 per second per base); a 429 waits 30 seconds and retries
  * --dry-run writes nothing but still reads the base when a token is set, so the plan it prints is the real one

Links marked prefersSingleRecordLink in schema.json are created as normal links: the Metadata API cannot set that
option. Switch "Allow linking to multiple records" off in the Airtable UI if you want it enforced there.

Before you run it: create an empty base in Airtable, copy its id (appXXXXXXXXXXXXXX from the URL) and create a
personal access token with the scopes schema.bases:read, schema.bases:write, data.records:read and data.records:write
for that base. Airtable puts a default "Table 1" in a new base; delete it by hand afterwards.

Usage:
  AIRTABLE_TOKEN=pat... AIRTABLE_BASE_ID=app... python setup/create_base.py [--dry-run] [--no-seed]
        [--shift-dates-to-today] [--api-url URL] [--schema PATH] [--seed-dir PATH] [--pace SECONDS] [--force-seed]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = Path(__file__).resolve().parent / '.seed-state.local.json'


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f'{status} {body}')
        self.status, self.body = status, body


class Airtable:
    def __init__(self, api_url, token, base_id, pace=0.25, dry_run=False, log=print):
        self.api = api_url.rstrip('/')
        self.token, self.base_id, self.pace, self.dry_run, self.log = token, base_id, pace, dry_run, log
        self.calls = 0
        self.writes = 0
        self._last = 0.0

    def request(self, method, path, body=None, write=False):
        if write and self.dry_run:
            self.writes += 1
            return None
        attempt = 0
        while True:
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            data = json.dumps(body).encode('utf-8') if body is not None else None
            req = urllib.request.Request(self.api + path, data=data, method=method,
                                         headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode('utf-8')
                    if write:
                        self.writes += 1
                    return json.loads(text) if text else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode('utf-8', 'replace')
                attempt += 1
                if e.code == 429 and attempt <= 3:
                    self.log('  rate limited (429), waiting 30 seconds as Airtable asks')
                    time.sleep(30)
                    continue
                if e.code >= 500 and attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    raise ApiError(e.code, json.loads(text))
                except ValueError:
                    raise ApiError(e.code, text)
            except urllib.error.URLError as e:
                attempt += 1
                if attempt <= 3:
                    time.sleep(2 ** attempt)
                    continue
                raise

    # metadata
    def tables(self):
        return self.request('GET', f'/v0/meta/bases/{self.base_id}/tables')['tables']

    def create_table(self, name, description, fields):
        body = {'name': name, 'fields': fields}
        if description:
            body['description'] = description
        return self.request('POST', f'/v0/meta/bases/{self.base_id}/tables', body, write=True)

    def create_field(self, table_id, field):
        return self.request('POST', f'/v0/meta/bases/{self.base_id}/tables/{table_id}/fields', field, write=True)

    def update_field(self, table_id, field_id, changes):
        return self.request('PATCH', f'/v0/meta/bases/{self.base_id}/tables/{table_id}/fields/{field_id}', changes, write=True)

    # records
    def list_ids(self, table_id):
        ids, offset = set(), None
        while True:
            body = {'pageSize': 100, 'fields': []}
            if offset:
                body['offset'] = offset
            page = self.request('POST', f'/v0/{self.base_id}/{quote(table_id)}/listRecords', body)
            ids.update(r['id'] for r in page['records'])
            offset = page.get('offset')
            if not offset:
                return ids

    def list_fields(self, table_id, field_names):
        out, offset = {}, None
        while True:
            body = {'pageSize': 100, 'fields': field_names}
            if offset:
                body['offset'] = offset
            page = self.request('POST', f'/v0/{self.base_id}/{quote(table_id)}/listRecords', body)
            out.update({r['id']: r['fields'] for r in page['records']})
            offset = page.get('offset')
            if not offset:
                return out

    def create_records(self, table_id, records):
        return self.request('POST', f'/v0/{self.base_id}/{quote(table_id)}', {'records': records}, write=True)

    def update_records(self, table_id, records):
        return self.request('PATCH', f'/v0/{self.base_id}/{quote(table_id)}', {'records': records}, write=True)


def field_body(f):
    body = {'name': f['name'], 'type': f['type']}
    if f.get('description'):
        body['description'] = f['description']
    if f['type'] != 'multipleRecordLinks' and f.get('options'):
        body['options'] = f['options']
    return body


def build_schema(at, spec, log):
    """Create missing tables and fields. Returns {table name: table model} after the changes."""
    existing = {} if at.dry_run and not at.token else {t['name']: t for t in at.tables()}
    created_tables = created_fields = renamed = 0
    warnings = []
    # 1. tables with their non link fields
    for t in spec['tables']:
        plain = [f for f in t['fields'] if f['type'] != 'multipleRecordLinks']
        if t['name'] not in existing:
            log(f'create table {t["name"]} with {len(plain)} fields')
            created_tables += 1
            model = at.create_table(t['name'], t.get('description'), [field_body(f) for f in plain])
            existing[t['name']] = model or {'id': f'(new {t["name"]})', 'name': t['name'], 'fields': [
                {'id': f'(new {f["name"]})', 'name': f['name'], 'type': f['type']} for f in plain]}
            continue
        have = {f['name']: f for f in existing[t['name']]['fields']}
        if plain and plain[0]['name'] not in have:
            warnings.append(f'{t["name"]}: primary field "{plain[0]["name"]}" is missing; rename the first column by hand')
        for f in plain:
            if f['name'] in have:
                if have[f['name']]['type'] != f['type']:
                    warnings.append(f'{t["name"]}.{f["name"]}: is {have[f["name"]]["type"]}, expected {f["type"]} (left as is)')
                continue
            log(f'create field {t["name"]}.{f["name"]} ({f["type"]})')
            created_fields += 1
            res = at.create_field(existing[t['name']]['id'], field_body(f))
            existing[t['name']]['fields'].append(res or {'id': f'(new {f["name"]})', 'name': f['name'], 'type': f['type']})
    # 2. link fields (owning side), then name the inverse Airtable created
    for t in spec['tables']:
        for f in t['fields']:
            if f['type'] != 'multipleRecordLinks' or f.get('createdAutomatically'):
                continue
            o = f['options']
            src, dst = existing[t['name']], existing[o['linkedTableName']]
            have = {x['name']: x for x in src['fields']}
            field = have.get(f['name'])
            if field is None:
                log(f'create link {t["name"]}.{f["name"]} -> {dst["name"]}')
                created_fields += 1
                body = field_body(f)
                body['options'] = {'linkedTableId': dst['id']}
                field = at.create_field(src['id'], body)
                if field is None:  # dry run: Airtable will name the new inverse field after this table
                    src['fields'].append({'id': f'(new {f["name"]})', 'name': f['name'], 'type': 'multipleRecordLinks'})
                    inverse_name = o.get('inverseLinkFieldName')
                    if inverse_name and src is not dst:
                        if inverse_name != t['name']:
                            log(f'  name the inverse field {dst["name"]}.{t["name"]} -> {inverse_name}')
                            renamed += 1
                        at.update_field(dst['id'], f'(inverse of {f["name"]})', {'name': inverse_name})
                    continue
                src['fields'].append(field)
                if src is not dst and field.get('options', {}).get('inverseLinkFieldId'):
                    fresh = {x['name']: x for x in at.tables()}
                    existing[dst['name']] = fresh[dst['name']]
                    dst = existing[dst['name']]
            elif field['type'] != 'multipleRecordLinks':
                warnings.append(f'{t["name"]}.{f["name"]}: exists but is {field["type"]}, not a link (left as is)')
                continue
            inverse_name = o.get('inverseLinkFieldName')
            inv_id = (field.get('options') or {}).get('inverseLinkFieldId')
            if not inverse_name or not inv_id:
                continue
            inv = next((x for x in dst['fields'] if x['id'] == inv_id), None)
            spec_inv = next((x for x in next(tt for tt in spec['tables'] if tt['name'] == dst['name'])['fields']
                             if x['name'] == inverse_name), {})
            if inv is None:
                continue
            changes = {}
            if inv['name'] != inverse_name:
                if any(x['name'].lower() == inverse_name.lower() and x['id'] != inv_id for x in dst['fields']):
                    warnings.append(f'{dst["name"]}: cannot rename inverse "{inv["name"]}" to "{inverse_name}", the name is taken')
                else:
                    changes['name'] = inverse_name
            if spec_inv.get('description') and inv.get('description') != spec_inv['description']:
                changes['description'] = spec_inv['description']
            if changes:
                if 'name' in changes:
                    log(f'  name the inverse field {dst["name"]}.{inv["name"]} -> {inverse_name}')
                    renamed += 1
                at.update_field(dst['id'], inv_id, changes)
                inv.update(changes)
    if not at.dry_run:
        existing = {t['name']: t for t in at.tables()}
    return existing, {'tables': created_tables, 'fields': created_fields, 'inverse_renamed': renamed, 'warnings': warnings}


def shift_fields(fields, schema_fields, days):
    if not days:
        return fields
    out = dict(fields)
    for k, v in fields.items():
        f = schema_fields.get(k)
        if not f or not v:
            continue
        if f['type'] == 'date':
            out[k] = (date.fromisoformat(v) + timedelta(days=days)).isoformat()
        elif f['type'] == 'dateTime':
            dt = datetime.fromisoformat(v.replace('Z', '+00:00')) + timedelta(days=days)
            out[k] = dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return out


def load_seed(at, spec, tables, seed_dir, shift_days, force, log, state_file=STATE_FILE):
    meta = json.loads((seed_dir / '_meta.json').read_text(encoding='utf-8'))
    files = [json.loads((seed_dir / n).read_text(encoding='utf-8')) for n in meta['loadOrder']]
    spec_fields = {t['name']: {f['name']: f for f in t['fields']} for t in spec['tables']}
    state = {}
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding='utf-8')).get(at.base_id, {})
    ids = {}            # (table, ref) -> record id
    created = linked = 0
    skipped_tables, skipped, warnings = [], set(), []

    def readable(table):
        # a dry run still reads what already exists, so its plan matches what a real run would do
        return bool(at.token) and not str(table['id']).startswith('(new')

    # pass 1: records without links
    for spec_file in files:
        tname = spec_file['table']
        table = tables[tname]
        links = {n for n, f in spec_fields[tname].items() if f['type'] == 'multipleRecordLinks'}
        known = state.get(tname, {})
        live = set()
        if spec_file['records'] and readable(table):
            live = at.list_ids(table['id'])
        alive = {ref: rid for ref, rid in known.items() if rid in live}
        if not alive and live and not force:
            # leave the table alone, but match its records to seed refs by primary field value so other
            # tables can still link to them
            primary = next(tt for tt in spec['tables'] if tt['name'] == tname)['fields'][0]['name']
            by_value = {}
            for rid, f in at.list_fields(table['id'], [primary]).items():
                by_value.setdefault(f.get(primary), []).append(rid)
            matched = 0
            for r in spec_file['records']:
                hits = by_value.get(r['fields'].get(primary), [])
                if len(hits) == 1:
                    ids[(tname, r['ref'])] = hits[0]
                    matched += 1
            skipped.add(tname)
            skipped_tables.append(f'{tname} ({len(live)} records already there and no saved seed state; '
                                  f'{matched} of {len(spec_file["records"])} seed records matched by {primary})')
            continue
        todo = [r for r in spec_file['records'] if r['ref'] not in alive]
        for ref, rid in alive.items():
            ids[(tname, ref)] = rid
        for i in range(0, len(todo), 10):
            chunk = todo[i:i + 10]
            body = [{'fields': shift_fields({k: v for k, v in r['fields'].items() if k not in links}, spec_fields[tname], shift_days)}
                    for r in chunk]
            res = at.create_records(table['id'], body)
            created += len(chunk)
            for r, rec in zip(chunk, (res or {}).get('records', [{'id': f'(new {r["ref"]})'} for r in chunk])):
                ids[(tname, r['ref'])] = rec['id']
        if todo:
            log(f'seed {tname}: {len(todo)} records created' + (f', {len(alive)} already there' if alive else ''))
        elif spec_file['records']:
            log(f'seed {tname}: all {len(alive)} records already there')
        state[tname] = {ref: ids[(tname, ref)] for (t, ref) in ids if t == tname}
        if not at.dry_run:
            save_state(state_file, at.base_id, state)
    # pass 2: links, by record id (setting the same links again changes nothing)
    for spec_file in files:
        tname = spec_file['table']
        if tname in skipped:
            continue
        table = tables[tname]
        links = {n: f for n, f in spec_fields[tname].items() if f['type'] == 'multipleRecordLinks'}
        updates = []
        for r in spec_file['records']:
            lf = {}
            for k, v in r['fields'].items():
                if k in links:
                    target = links[k]['options']['linkedTableName']
                    missing = [x for x in v if (target, x) not in ids]
                    if missing and target not in skipped:
                        raise SystemExit(f'seed {tname}/{r["ref"]}.{k}: unknown {target} refs {missing}')
                    if missing:
                        warnings.append(f'{tname}/{r["ref"]}.{k}: {target} {missing} not found in the existing records, link left out')
                    lf[k] = [ids[(target, x)] for x in v if (target, x) in ids]
                    if not lf[k]:
                        del lf[k]
            if lf:
                updates.append({'id': ids[(tname, r['ref'])], 'fields': lf})
        if updates and readable(table):
            current = at.list_fields(table['id'], sorted({k for u in updates for k in u['fields']}))
            updates = [u for u in updates
                       if any(sorted(current.get(u['id'], {}).get(k, [])) != sorted(v) for k, v in u['fields'].items())]
        for i in range(0, len(updates), 10):
            at.update_records(table['id'], updates[i:i + 10])
        if updates:
            linked += len(updates)
            log(f'link {tname}: {len(updates)} records')
    return {'created': created, 'linked': linked, 'skipped_tables': skipped_tables, 'warnings': warnings}


def save_state(state_file, base_id, table_state):
    all_state = {}
    if state_file.exists():
        all_state = json.loads(state_file.read_text(encoding='utf-8'))
    all_state[base_id] = table_state
    state_file.write_text(json.dumps(all_state, indent=2), encoding='utf-8')


def main(argv=None, log=print):
    p = argparse.ArgumentParser(description='Create the Demo Chocolate Co. Airtable base from base/schema.json and base/seed.')
    p.add_argument('--base-id', default=os.environ.get('AIRTABLE_BASE_ID'))
    p.add_argument('--api-url', default=os.environ.get('AIRTABLE_API_URL', 'https://api.airtable.com'))
    p.add_argument('--schema', default=str(ROOT / 'base' / 'schema.json'))
    p.add_argument('--seed-dir', default=str(ROOT / 'base' / 'seed'))
    p.add_argument('--no-seed', action='store_true', help='create tables and fields only')
    p.add_argument('--shift-dates-to-today', action='store_true',
                   help='move every seed date so that the seed "today" becomes the real today')
    p.add_argument('--dry-run', action='store_true', help='print what would change, write nothing')
    p.add_argument('--force-seed', action='store_true', help='seed tables even if they already have records')
    p.add_argument('--pace', type=float, default=0.25, help='seconds between requests (default 0.25, 4 per second)')
    p.add_argument('--state-file', default=str(STATE_FILE), help='where seeded record ids are remembered')
    args = p.parse_args(argv)
    token = os.environ.get('AIRTABLE_TOKEN', '')
    if not args.dry_run and (not token or not args.base_id):
        raise SystemExit('Set AIRTABLE_TOKEN and AIRTABLE_BASE_ID (or pass --base-id), or use --dry-run.')
    spec = json.loads(Path(args.schema).read_text(encoding='utf-8'))
    at = Airtable(args.api_url, token, args.base_id or 'appXXXXXXXXXXXXXX', pace=args.pace, dry_run=args.dry_run, log=log)
    if args.dry_run:
        log('DRY RUN: nothing will be written' + ('' if token else ' (no token, planning for an empty base)'))
    tables, summary = build_schema(at, spec, log)
    for w in summary['warnings']:
        log('WARNING ' + w)
    log(f"schema: {summary['tables']} tables and {summary['fields']} fields to create, {summary['inverse_renamed']} inverse fields named")
    seed = None
    if not args.no_seed:
        shift = 0
        if args.shift_dates_to_today:
            meta = json.loads((Path(args.seed_dir) / '_meta.json').read_text(encoding='utf-8'))
            # UTC, like Airtable's TODAY() and the test mocks
            shift = (datetime.now(timezone.utc).date() - date.fromisoformat(meta['seedToday'])).days
            log(f'dates shifted by {shift} days')
        seed = load_seed(at, spec, tables, Path(args.seed_dir), shift, args.force_seed, log, Path(args.state_file))
        for s in seed['skipped_tables']:
            log('WARNING seed skipped for ' + s)
        for w in seed['warnings']:
            log('WARNING ' + w)
        log(f"seed: {seed['created']} records created, {seed['linked']} records linked")
    log(f'{at.calls} API calls, {at.writes} writes' + (' planned' if args.dry_run else ''))
    return {'schema': summary, 'seed': seed, 'calls': at.calls, 'writes': at.writes}


if __name__ == '__main__':
    try:
        main()
    except ApiError as e:
        print(f'Airtable said {e.status}: {json.dumps(e.body)}', file=sys.stderr)
        sys.exit(1)
