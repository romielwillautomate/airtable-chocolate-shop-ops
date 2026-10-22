/**
 * A strict stand-in for the Airtable automation scripting environment (the "Run a script" action),
 * used to test the scripts in ../scripts against the repo's base blueprint and seed data.
 *
 * It loads base/schema.json and base/seed/*.json into memory and runs a script the way an Airtable
 * automation does: top level await, the globals base, input, output, console and fetch, and nothing
 * else (no setTimeout, no require, no process). Every rule below follows the Airtable scripting docs
 * (airtable.com/developers/scripting/api) and the "Run a script" limits in Airtable's support article.
 * Where the docs give a rule but no error text, the message here is close to the real one; where the
 * docs are silent, the stricter behavior was chosen, so a passing test means the script also runs in
 * Airtable.
 *
 *   Reading
 *   - selectRecordsAsync({fields, sorts, recordIds}) and selectRecordAsync(id, {fields}). Every field
 *     name is checked ("No field matching ..."). getCellValue on a field that was not loaded throws
 *     "Cell values for field fld... are not loaded". recordIds takes at most 100 ids ("Request exceeds
 *     maximum batch size limit of 100 items"). Leaving out fields loads every field but is recorded as
 *     a warning, because Airtable discourages it. record.name needs the primary field loaded here.
 *   - Cell values come back in the scripting read format: an empty cell is null, a link is
 *     [{id, name}], a single select {id, name, color}, a multiple select an array of those, a checkbox
 *     true or null, dates and date times ISO strings.
 *   - The order of records is arbitrary but stable (sorted by record id), as the docs say, so a script
 *     cannot lean on the order of the seed files.
 *   - At most 30 queries per run ("Exceeded quota of 30 selectRecords queries per script invocation").
 *
 *   Writing
 *   - create/update/deleteRecord(s)Async take at most 50 records per call. Field names, value types,
 *     select options (a script cannot add an option by writing it) and linked record ids are all
 *     checked, links must be written as [{id}], and a call is all or nothing. Writing one side of a
 *     link updates the other side, as in Airtable.
 *   - A number with more decimals than the field's precision is recorded as a warning.
 *   - At most 15 writes may start inside any one second ("Exceeded quota of 15 mutations per second").
 *     Each write takes 80 ms here, so a script that awaits each batch stays under the limit and a
 *     script that fires writes in parallel does not, which is how the limit shows up in Airtable.
 *   - A write still running when the script ends (a missing await) is recorded as a warning.
 *
 *   Input and output
 *   - input.config() returns the input variables. output.set(key, value) only accepts JSON values.
 *     The extension only methods (output.markdown, input.textAsync, ...) do not exist.
 *   - fetch exists in Airtable automations; here it is counted and refused, because the scripts in
 *     this folder must never call an outside service.
 *   - A run fails when the script throws, like an automation run in Airtable, and has 30 seconds.
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import crypto from 'node:crypto';

export const LIMITS = Object.freeze({
  recordsPerWrite: 50,
  recordIdsPerQuery: 100,
  queriesPerRun: 30,
  mutationsPerSecond: 15,
  runSeconds: 30,
  outputBytes: 6 * 1024 * 1024,
});

const COMPUTED_TYPES = new Set([
  'formula', 'rollup', 'lookup', 'multipleLookupValues', 'count', 'autoNumber', 'createdTime',
  'lastModifiedTime', 'createdBy', 'lastModifiedBy', 'button', 'externalSyncSource', 'aiText',
]);
const TEXT_TYPES = new Set(['singleLineText', 'multilineText', 'richText', 'email', 'url', 'phoneNumber']);
const NUMBER_TYPES = new Set(['number', 'currency', 'percent', 'duration', 'rating']);
const RECORD_ID = /^rec[A-Za-z0-9]{14}$/;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})$/;
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
const NOT_EMULATED = Symbol('not emulated');
const DATE_FORMATS = { iso: 'YYYY-MM-DD', us: 'M/D/YYYY', european: 'D/M/YYYY', friendly: 'LL', local: 'l' };
const TIME_FORMATS = { '24hour': 'HH:mm', '12hour': 'h:mma' };

// The scripting globals are built inside the script's own realm, so instanceof Error, Array and Date
// behave in the script exactly as they do in Airtable.
const BOOTSTRAP = `(function (nowMs) {
  'use strict';
  const RealDate = Date;
  function ScriptDate(...args) {
    if (!new.target) return new RealDate(nowMs()).toString();
    return args.length ? new RealDate(...args) : new RealDate(nowMs());
  }
  ScriptDate.prototype = RealDate.prototype;
  ScriptDate.now = function now() { return nowMs(); };
  ScriptDate.UTC = RealDate.UTC;
  ScriptDate.parse = RealDate.parse;
  Object.defineProperty(RealDate.prototype, 'constructor', { value: ScriptDate, writable: true, configurable: true });
  globalThis.Date = ScriptDate;
  return Object.freeze({
    clone: function (json) { return JSON.parse(json); },
    array: function (items) { return Array.from(items); },
    error: function (message) { return new Error(message); },
  });
})`;

export function makeId(prefix, ...parts) {
  const hash = crypto.createHash('sha256').update(parts.join('\u0000')).digest();
  let out = '';
  for (let i = 0; i < 14; i += 1) out += ALPHABET[hash[i] % 62];
  return prefix + out;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && Object.prototype.toString.call(v) === '[object Object]';
}

function isDateObject(v) {
  return Object.prototype.toString.call(v) === '[object Date]';
}

function validDate(s) {
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

function isoMs(ms) {
  return new Date(ms).toISOString();
}

function addDays(dateString, days) {
  const d = new Date(`${dateString}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Why a value is not JSON-serializable, or null when it is. Stricter than JSON.stringify on purpose. */
function jsonProblem(value, seen = new Set(), where = 'value') {
  if (value === undefined) return `${where} is undefined`;
  if (typeof value === 'function') return `${where} is a function`;
  if (typeof value === 'symbol') return `${where} is a symbol`;
  if (typeof value === 'bigint') return `${where} is a BigInt`;
  if (typeof value === 'number' && !Number.isFinite(value)) return `${where} is ${value}`;
  if (value === null || typeof value !== 'object' || isDateObject(value)) return null;
  if (seen.has(value)) return `${where} is a circular reference`;
  seen.add(value);
  const entries = Array.isArray(value) ? value.map((v, i) => [`${where}[${i}]`, v]) : Object.entries(value).map(([k, v]) => [`${where}.${k}`, v]);
  for (const [w, v] of entries) {
    const problem = jsonProblem(v, seen, w);
    if (problem) return problem;
  }
  seen.delete(value);
  return null;
}

class SimTable {
  constructor(id, name, description) {
    this.id = id;
    this.name = name;
    this.description = description || null;
    this.fields = [];
    this.records = new Map();
  }

  get primary() {
    return this.fields[0];
  }

  field(key) {
    return this.fields.find((f) => f.id === key || f.name === key) || null;
  }
}

class SimField {
  constructor(table, spec) {
    this.table = table;
    this.spec = spec;
    this.id = makeId('fld', table.id, spec.name);
    this.name = spec.name;
    this.type = spec.type;
    this.description = spec.description || null;
    this.choices = null;
    this.linkedTable = null;
    this.inverse = null;
    if (spec.type === 'singleSelect' || spec.type === 'multipleSelects') {
      this.choices = (spec.options.choices || []).map((c) => ({ id: makeId('sel', table.id, spec.name, c.name), name: c.name, color: c.color }));
    }
  }

  /** The options object a script sees on field.options. */
  publicOptions() {
    const o = this.spec.options || {};
    switch (this.type) {
      case 'singleSelect':
      case 'multipleSelects':
        return { choices: this.choices.map((c) => ({ ...c })) };
      case 'multipleRecordLinks':
        return {
          linkedTableId: this.linkedTable.id,
          isReversed: false,
          prefersSingleRecordLink: Boolean(o.prefersSingleRecordLink),
          ...(this.inverse ? { inverseLinkFieldId: this.inverse.id } : {}),
        };
      case 'date':
        return { dateFormat: { name: o.dateFormat.name, format: DATE_FORMATS[o.dateFormat.name] } };
      case 'dateTime':
        return {
          timeZone: o.timeZone,
          dateFormat: { name: o.dateFormat.name, format: DATE_FORMATS[o.dateFormat.name] },
          timeFormat: { name: o.timeFormat.name, format: TIME_FORMATS[o.timeFormat.name] },
        };
      default:
        return Object.keys(o).length ? JSON.parse(JSON.stringify(o)) : null;
    }
  }
}

class SimRecord {
  constructor(id, createdTime) {
    this.id = id;
    this.createdTime = createdTime;
    this.cells = new Map();
  }
}

/** Everything one script run did: queries, writes, output, logs and warnings. */
class ScriptRun {
  constructor(label, inputs) {
    this.label = label;
    this.inputs = inputs;
    this.status = 'running';
    this.error = null;
    this.output = {};
    this.logs = [];
    this.warnings = [];
    this.queries = [];
    this.mutations = [];
    this.fetchCalls = 0;
    this.queryCount = 0;
    this.starts = [];
    this.pending = new Set();
    this.h = null;
    this.durationMs = 0;
  }

  warn(message) {
    this.warnings.push(message);
  }

  countQuery() {
    this.queryCount += 1;
    if (this.queryCount > LIMITS.queriesPerRun) {
      throw this.h.error(`Exceeded quota of ${LIMITS.queriesPerRun} selectRecords queries per script invocation`);
    }
  }

  startMutation(method, table, count) {
    const now = Date.now();
    this.starts = this.starts.filter((t) => now - t < 1000);
    if (this.starts.length >= LIMITS.mutationsPerSecond) {
      throw this.h.error(`Exceeded quota of ${LIMITS.mutationsPerSecond} mutations per second`);
    }
    this.starts.push(now);
    this.mutations.push({ method, table, count });
  }

  track(promise) {
    this.pending.add(promise);
    const done = () => this.pending.delete(promise);
    promise.then(done, done);
  }

  async settle() {
    await Promise.allSettled([...this.pending]);
  }

  /** Records written per table, for example { Customers: 6, 'Sync Log': 1 }. */
  writesByTable() {
    const out = {};
    for (const m of this.mutations) out[m.table] = (out[m.table] || 0) + m.count;
    return out;
  }
}

export class AirtableSim {
  /**
   * @param {object} options
   * @param {string} options.root repo root (holds base/schema.json and base/seed)
   * @param {string|null} options.now ISO time to freeze the clock at, or null for the real clock
   * @param {number} options.mutationLatencyMs how long every write call takes
   * @param {boolean} options.seed load base/seed (true) or start with empty tables
   */
  constructor({ root, now = null, mutationLatencyMs = 80, seed = true } = {}) {
    this.root = root;
    this.frozenMs = now ? Date.parse(now) : null;
    this.latency = mutationLatencyMs;
    this.tables = [];
    this.counter = 0;
    this.refs = new Map();
    this.baseId = 'appDemoChocolate1';
    this.meta = JSON.parse(fs.readFileSync(path.join(root, 'base', 'seed', '_meta.json'), 'utf8'));
    this.loadSchema(path.join(root, 'base', 'schema.json'));
    this.seedShiftDays = 0;
    if (seed) this.loadSeed(path.join(root, 'base', 'seed'));
  }

  // ---------------------------------------------------------------- clock

  nowMs() {
    return this.frozenMs === null ? Date.now() : this.frozenMs;
  }

  setNow(iso) {
    this.frozenMs = Date.parse(iso);
  }

  advanceDays(days) {
    if (this.frozenMs === null) throw new Error('advanceDays needs a frozen clock');
    this.frozenMs += days * 86400000;
  }

  today() {
    return isoMs(this.nowMs()).slice(0, 10);
  }

  // ---------------------------------------------------------------- schema and seed

  table(key) {
    return this.tables.find((t) => t.name === key || t.id === key) || null;
  }

  mustTable(key) {
    const t = this.table(key);
    if (!t) throw new Error(`no table ${key}`);
    return t;
  }

  loadSchema(file) {
    const spec = JSON.parse(fs.readFileSync(file, 'utf8'));
    this.baseName = spec.baseName;
    for (const t of spec.tables) {
      if (this.table(t.name)) throw new Error(`duplicate table ${t.name}`);
      this.tables.push(new SimTable(makeId('tbl', this.baseId, t.name), t.name, t.description));
    }
    for (const t of spec.tables) {
      const table = this.table(t.name);
      for (const f of t.fields) {
        if (table.fields.some((x) => x.name.toLowerCase() === f.name.toLowerCase())) throw new Error(`${t.name}: duplicate field ${f.name}`);
        table.fields.push(new SimField(table, f));
      }
    }
    for (const table of this.tables) {
      for (const f of table.fields) {
        if (f.type !== 'multipleRecordLinks') continue;
        f.linkedTable = this.mustTable(f.spec.options.linkedTableName);
        const inverseName = f.spec.options.inverseLinkFieldName;
        if (inverseName) {
          f.inverse = f.linkedTable.field(inverseName);
          if (!f.inverse || f.inverse.type !== 'multipleRecordLinks') throw new Error(`${table.name}.${f.name}: inverse ${inverseName} missing`);
        }
      }
    }
    for (const table of this.tables) {
      for (const f of table.fields) {
        if (f.inverse && (f.inverse.inverse !== f || f.inverse.linkedTable !== table)) {
          throw new Error(`${table.name}.${f.name} and its inverse do not point at each other`);
        }
      }
    }
  }

  loadSeed(dir) {
    const shift = Math.round((Date.parse(`${this.today()}T00:00:00Z`) - Date.parse(`${this.meta.seedToday}T00:00:00Z`)) / 86400000);
    this.seedShiftDays = shift;
    const files = this.meta.loadOrder.map((name) => JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8')));
    const created = this.nowMs();
    for (const spec of files) {
      const table = this.mustTable(spec.table);
      spec.records.forEach((rec, i) => {
        const key = `${table.name}\u0000${rec.ref}`;
        if (this.refs.has(key)) throw new Error(`duplicate seed ref ${table.name}/${rec.ref}`);
        const id = makeId('rec', this.baseId, table.name, rec.ref);
        this.refs.set(key, id);
        const record = new SimRecord(id, created + i);
        table.records.set(id, record);
        for (const [name, raw] of Object.entries(rec.fields)) {
          const field = table.field(name);
          if (!field) throw new Error(`seed ${table.name}/${rec.ref}: unknown field ${name}`);
          if (field.type === 'multipleRecordLinks') continue;
          let value = raw;
          if (shift && field.type === 'date' && value) value = addDays(value, shift);
          if (shift && field.type === 'dateTime' && value) value = isoMs(Date.parse(value) + shift * 86400000);
          const canonical = this.fromApi(field, value, null);
          if (canonical !== null) record.cells.set(field.id, canonical);
        }
      });
    }
    for (const spec of files) {
      const table = this.mustTable(spec.table);
      for (const rec of spec.records) {
        const record = table.records.get(this.refs.get(`${table.name}\u0000${rec.ref}`));
        for (const [name, raw] of Object.entries(rec.fields)) {
          const field = table.field(name);
          if (field.type !== 'multipleRecordLinks') continue;
          const ids = this.fromApi(field, raw, (ref) => this.ref(field.linkedTable.name, ref)) || [];
          this.setLinks(record, field, ids);
        }
      }
    }
  }

  // ---------------------------------------------------------------- value conversion

  choiceByName(field, name) {
    const c = field.choices.find((x) => x.name === name);
    if (!c) throw new Error(`${field.table.name}.${field.name}: no option ${JSON.stringify(name)}`);
    return c;
  }

  /** Web API style value (select names, link ids) to the stored form. Used by the seed and test helpers. */
  fromApi(field, value, resolveLink) {
    const bad = (why) => new Error(`${field.table.name}.${field.name}: ${why} (got ${JSON.stringify(value)})`);
    if (value === null || value === undefined) return null;
    const t = field.type;
    if (TEXT_TYPES.has(t)) {
      if (typeof value !== 'string') throw bad('expected a string');
      return value === '' ? null : value;
    }
    if (NUMBER_TYPES.has(t)) {
      if (typeof value !== 'number' || !Number.isFinite(value)) throw bad('expected a number');
      return value;
    }
    if (t === 'checkbox') {
      if (typeof value !== 'boolean') throw bad('expected a boolean');
      return value ? true : null;
    }
    if (t === 'singleSelect') {
      if (typeof value !== 'string') throw bad('expected an option name');
      return this.choiceByName(field, value).id;
    }
    if (t === 'multipleSelects') {
      if (!Array.isArray(value)) throw bad('expected a list of option names');
      const ids = value.map((v) => this.choiceByName(field, v).id);
      return ids.length ? ids : null;
    }
    if (t === 'multipleRecordLinks') {
      if (!Array.isArray(value)) throw bad('expected a list of record ids');
      const ids = value.map((v) => (resolveLink ? resolveLink(v) : v));
      for (const id of ids) if (!field.linkedTable.records.has(id)) throw bad(`linked record ${id} does not exist`);
      return ids.length ? [...new Set(ids)] : null;
    }
    if (t === 'date') {
      if (typeof value !== 'string' || !DATE_ONLY.test(value) || !validDate(value)) throw bad('expected YYYY-MM-DD');
      return value;
    }
    if (t === 'dateTime') {
      if (typeof value !== 'string' || !DATE_TIME.test(value) || Number.isNaN(Date.parse(value))) throw bad('expected an ISO date time');
      return isoMs(Date.parse(value));
    }
    if (t === 'multipleAttachments') {
      if (!Array.isArray(value)) throw bad('expected a list of attachments');
      return value.length ? JSON.parse(JSON.stringify(value)) : null;
    }
    throw bad(`type ${t} is not supported`);
  }

  /** The scripting write format to the stored form. Throws the way Airtable refuses a bad value. */
  fromScript(run, field, value) {
    const bad = (why) => run.h.error(`Field "${field.name}" cannot accept the provided value${why ? ` (${why})` : ''}`);
    if (value === undefined) throw bad('undefined');
    if (value === null) return null;
    const t = field.type;
    if (TEXT_TYPES.has(t)) {
      if (typeof value !== 'string') throw bad(`a ${t} field takes a string`);
      if (t === 'singleLineText' && /[\r\n]/.test(value)) throw bad('a single line text field cannot hold a line break');
      return value === '' ? null : value;
    }
    if (NUMBER_TYPES.has(t)) {
      if (typeof value !== 'number' || !Number.isFinite(value)) throw bad(`a ${t} field takes a number`);
      const precision = field.spec.options && field.spec.options.precision;
      if (typeof precision === 'number') {
        const scaled = value * 10 ** precision;
        if (Math.abs(scaled - Math.round(scaled)) > 1e-6) {
          run.warn(`${field.table.name}.${field.name} was written as ${value}, which has more decimals than its precision of ${precision}`);
        }
      }
      return value;
    }
    if (t === 'checkbox') {
      if (typeof value !== 'boolean') throw bad('a checkbox takes true or false');
      return value ? true : null;
    }
    if (t === 'singleSelect') return this.scriptChoice(field, value, bad).id;
    if (t === 'multipleSelects') {
      if (!Array.isArray(value)) throw bad('a multiple select takes an array of {name} or {id}');
      const ids = value.map((v) => this.scriptChoice(field, v, bad).id);
      if (new Set(ids).size !== ids.length) throw bad('the same option is listed twice');
      return ids.length ? ids : null;
    }
    if (t === 'multipleRecordLinks') {
      if (!Array.isArray(value)) throw bad('a link field takes an array of {id}');
      const ids = [];
      for (const v of value) {
        if (!isPlainObject(v) || Object.keys(v).length !== 1 || typeof v.id !== 'string') throw bad('each linked record must be written as {id: "rec..."}');
        if (!field.linkedTable.records.has(v.id)) throw bad(`record ${v.id} does not exist in ${field.linkedTable.name}`);
        if (ids.includes(v.id)) throw bad(`record ${v.id} is listed twice`);
        ids.push(v.id);
      }
      return ids.length ? ids : null;
    }
    if (t === 'date') {
      if (isDateObject(value)) {
        if (Number.isNaN(value.getTime())) throw bad('invalid Date');
        return value.toISOString().slice(0, 10);
      }
      if (typeof value === 'string' && DATE_ONLY.test(value) && validDate(value)) return value;
      throw bad('a date field takes "YYYY-MM-DD" or a Date');
    }
    if (t === 'dateTime') {
      if (isDateObject(value)) {
        if (Number.isNaN(value.getTime())) throw bad('invalid Date');
        return value.toISOString();
      }
      if (typeof value === 'string' && DATE_TIME.test(value) && !Number.isNaN(Date.parse(value))) return isoMs(Date.parse(value));
      throw bad('a date time field takes an ISO 8601 date time or a Date');
    }
    if (t === 'multipleAttachments') {
      if (!Array.isArray(value) || !value.every((a) => isPlainObject(a) && typeof a.url === 'string')) throw bad('attachments are written as [{url, filename}]');
      return value.length ? JSON.parse(JSON.stringify(value)) : null;
    }
    throw bad(`writing ${t} fields is not emulated`);
  }

  scriptChoice(field, value, bad) {
    if (!isPlainObject(value) || Object.keys(value).length !== 1 || !(typeof value.id === 'string' || typeof value.name === 'string')) {
      throw bad('a select option is written as {name: "..."} or {id: "sel..."}');
    }
    const choice = field.choices.find((c) => (value.id !== undefined ? c.id === value.id : c.name === value.name));
    if (!choice) throw bad(`no option ${JSON.stringify(value)} in this field, and a script cannot add options by writing them`);
    return choice;
  }

  primaryString(table, rec) {
    const v = rec.cells.get(table.primary.id);
    if (v === undefined) return '';
    return typeof v === 'string' ? v : String(v);
  }

  recordName(table, id) {
    const rec = table.records.get(id);
    return (rec && this.primaryString(table, rec)) || 'Unnamed record';
  }

  /** The scripting read format of one cell (null when empty). */
  scriptValue(field, rec) {
    const v = rec.cells.get(field.id);
    if (v === undefined) return null;
    switch (field.type) {
      case 'checkbox':
        return true;
      case 'singleSelect': {
        const c = field.choices.find((x) => x.id === v);
        return { id: c.id, name: c.name, color: c.color };
      }
      case 'multipleSelects':
        return v.map((id) => {
          const c = field.choices.find((x) => x.id === id);
          return { id: c.id, name: c.name, color: c.color };
        });
      case 'multipleRecordLinks':
        return v.map((id) => ({ id, name: this.recordName(field.linkedTable, id) }));
      case 'multipleAttachments':
        return JSON.parse(JSON.stringify(v));
      default:
        return v;
    }
  }

  /** getCellValueAsString, for the field types where Airtable's formatting is certain. */
  stringValue(field, rec) {
    const v = this.scriptValue(field, rec);
    if (TEXT_TYPES.has(field.type)) return v || '';
    if (field.type === 'singleSelect') return v ? v.name : '';
    if (field.type === 'multipleSelects' || field.type === 'multipleRecordLinks') return v ? v.map((x) => x.name).join(', ') : '';
    return NOT_EMULATED;
  }

  /** The Web API style value, for test assertions: select names, link ids, empty cells left out. */
  apiValue(field, rec) {
    const v = rec.cells.get(field.id);
    if (v === undefined) return undefined;
    switch (field.type) {
      case 'singleSelect':
        return field.choices.find((x) => x.id === v).name;
      case 'multipleSelects':
        return v.map((id) => field.choices.find((x) => x.id === id).name);
      case 'multipleRecordLinks':
        return v.slice();
      case 'multipleAttachments':
        return JSON.parse(JSON.stringify(v));
      default:
        return v;
    }
  }

  // ---------------------------------------------------------------- storage

  setLinks(rec, field, ids) {
    const old = rec.cells.get(field.id) || [];
    if (ids.length) rec.cells.set(field.id, ids.slice());
    else rec.cells.delete(field.id);
    const inverse = field.inverse;
    if (!inverse) return;
    const removed = old.filter((x) => !ids.includes(x));
    const added = ids.filter((x) => !old.includes(x));
    for (const targetId of removed) {
      const target = field.linkedTable.records.get(targetId);
      if (!target) continue;
      const rest = (target.cells.get(inverse.id) || []).filter((x) => x !== rec.id);
      if (rest.length) target.cells.set(inverse.id, rest);
      else target.cells.delete(inverse.id);
    }
    for (const targetId of added) {
      const target = field.linkedTable.records.get(targetId);
      const current = target.cells.get(inverse.id) || [];
      if (!current.includes(rec.id)) target.cells.set(inverse.id, [...current, rec.id]);
    }
  }

  applyCells(table, rec, changes) {
    for (const [fieldId, value] of changes) {
      const field = table.fields.find((f) => f.id === fieldId);
      if (field.type === 'multipleRecordLinks') this.setLinks(rec, field, value || []);
      else if (value === null) rec.cells.delete(fieldId);
      else rec.cells.set(fieldId, value);
    }
  }

  deleteRecord(table, id) {
    const rec = table.records.get(id);
    for (const field of table.fields) {
      if (field.type === 'multipleRecordLinks' && rec.cells.has(field.id)) this.setLinks(rec, field, []);
    }
    table.records.delete(id);
    // Links without an inverse (such as Customers.Merged Into) that point at the deleted record.
    for (const other of this.tables) {
      for (const field of other.fields) {
        if (field.type !== 'multipleRecordLinks' || field.linkedTable !== table) continue;
        for (const r of other.records.values()) {
          const ids = r.cells.get(field.id);
          if (ids && ids.includes(id)) {
            const rest = ids.filter((x) => x !== id);
            if (rest.length) r.cells.set(field.id, rest);
            else r.cells.delete(field.id);
          }
        }
      }
    }
  }

  // ---------------------------------------------------------------- test helpers (no limits)

  ref(tableName, ref) {
    const id = this.refs.get(`${tableName}\u0000${ref}`);
    if (!id) throw new Error(`no seed ref ${tableName}/${ref}`);
    return id;
  }

  row(tableName, id) {
    const table = this.mustTable(tableName);
    const rec = table.records.get(id);
    if (!rec) return null;
    const fields = {};
    for (const f of table.fields) {
      const v = this.apiValue(f, rec);
      if (v !== undefined) fields[f.name] = v;
    }
    return { id: rec.id, createdTime: isoMs(rec.createdTime), fields };
  }

  rows(tableName) {
    return [...this.mustTable(tableName).records.keys()].map((id) => this.row(tableName, id));
  }

  fields(tableName, id) {
    const r = this.row(tableName, id);
    return r ? r.fields : null;
  }

  byPrimary(tableName, value) {
    const table = this.mustTable(tableName);
    const found = this.rows(tableName).filter((r) => r.fields[table.primary.name] === value);
    if (!found.length) return null;
    return found.length === 1 ? found[0] : found;
  }

  count(tableName) {
    return this.mustTable(tableName).records.size;
  }

  insert(tableName, fields) {
    const table = this.mustTable(tableName);
    this.counter += 1;
    const rec = new SimRecord(makeId('rec', this.baseId, 'test', String(this.counter)), this.nowMs());
    table.records.set(rec.id, rec);
    this.applyCells(table, rec, this.apiChanges(table, fields));
    return rec.id;
  }

  update(tableName, id, fields) {
    const table = this.mustTable(tableName);
    const rec = table.records.get(id);
    if (!rec) throw new Error(`no record ${id} in ${tableName}`);
    this.applyCells(table, rec, this.apiChanges(table, fields));
  }

  remove(tableName, id) {
    this.deleteRecord(this.mustTable(tableName), id);
  }

  /** Make the base differ from the blueprint, as a user's renamed field or table would. */
  renameField(tableName, oldName, newName) {
    const field = this.mustTable(tableName).field(oldName);
    if (!field) throw new Error(`no field ${tableName}.${oldName}`);
    field.name = newName;
  }

  renameTable(oldName, newName) {
    this.mustTable(oldName).name = newName;
  }

  /** Rename a select option, as a user would in the field settings. The option keeps its id. */
  renameChoice(tableName, fieldName, oldName, newName) {
    const field = this.mustTable(tableName).field(fieldName);
    if (!field || !field.choices) throw new Error(`no select field ${tableName}.${fieldName}`);
    const choice = field.choices.find((c) => c.name === oldName);
    if (!choice) throw new Error(`no option ${oldName} in ${tableName}.${fieldName}`);
    choice.name = newName;
  }

  apiChanges(table, fields) {
    const changes = new Map();
    for (const [name, value] of Object.entries(fields)) {
      const field = table.field(name);
      if (!field) throw new Error(`${table.name}: unknown field ${name}`);
      changes.set(field.id, this.fromApi(field, value, null));
    }
    return changes;
  }

  /** The whole base as JSON, to check that a run changed nothing. */
  snapshot() {
    return JSON.stringify(this.tables.map((t) => [t.name, this.rows(t.name)]));
  }

  // ---------------------------------------------------------------- the scripting API

  resolveField(run, table, key) {
    let field = null;
    if (typeof key === 'string') field = table.fields.find((f) => f.id === key || f.name === key) || null;
    else if (key && typeof key === 'object' && typeof key.id === 'string') field = table.fields.find((f) => f.id === key.id) || null;
    if (!field) {
      const label = typeof key === 'string' ? key : key && key.id;
      throw run.h.error(`No field matching "${label}" found in table "${table.name}"`);
    }
    return field;
  }

  fieldApi(run, field) {
    return Object.freeze(run.h.clone(JSON.stringify({
      id: field.id,
      name: field.name,
      type: field.type,
      description: field.description,
      options: field.publicOptions(),
      isComputed: COMPUTED_TYPES.has(field.type),
    })));
  }

  snapshotRecord(run, table, rec, loaded) {
    const sim = this;
    const values = new Map();
    const strings = new Map();
    for (const f of table.fields) {
      if (!loaded.has(f.id)) continue;
      values.set(f.id, JSON.stringify(this.scriptValue(f, rec)));
      strings.set(f.id, this.stringValue(f, rec));
    }
    const primaryLoaded = loaded.has(table.primary.id);
    const name = primaryLoaded ? (this.primaryString(table, rec) || 'Unnamed record') : null;
    return Object.freeze({
      id: rec.id,
      get name() {
        if (!primaryLoaded) throw run.h.error(`Cell values for field ${table.primary.id} are not loaded`);
        return name;
      },
      getCellValue(key) {
        const field = sim.resolveField(run, table, key);
        if (!values.has(field.id)) throw run.h.error(`Cell values for field ${field.id} are not loaded`);
        return run.h.clone(values.get(field.id));
      },
      getCellValueAsString(key) {
        const field = sim.resolveField(run, table, key);
        if (!strings.has(field.id)) throw run.h.error(`Cell values for field ${field.id} are not loaded`);
        const s = strings.get(field.id);
        if (s === NOT_EMULATED) throw run.h.error(`getCellValueAsString is not emulated for ${field.type} fields in these tests; use getCellValue`);
        return s;
      },
    });
  }

  query(run, table, method, options, singleId) {
    run.countQuery();
    const opts = options === undefined || options === null ? {} : options;
    if (!isPlainObject(opts)) throw run.h.error(`${method}: the options must be an object`);
    const allowed = method === 'selectRecordAsync' ? ['fields'] : ['fields', 'sorts', 'recordIds'];
    for (const k of Object.keys(opts)) if (!allowed.includes(k)) throw run.h.error(`${method}: unknown option "${k}"`);
    let fields;
    if (opts.fields === undefined) {
      fields = table.fields.slice();
      run.warn(`${table.name}: ${method} without "fields" loads every field, which Airtable discourages and plans to remove`);
    } else {
      if (!Array.isArray(opts.fields)) throw run.h.error(`${method}: fields must be an array`);
      fields = opts.fields.map((k) => this.resolveField(run, table, k));
    }
    let records;
    if (singleId !== undefined) {
      if (typeof singleId !== 'string' || !RECORD_ID.test(singleId)) throw run.h.error(`selectRecordAsync: "${singleId}" is not a record id`);
      const r = table.records.get(singleId);
      records = r ? [r] : [];
    } else if (opts.recordIds !== undefined) {
      if (!Array.isArray(opts.recordIds)) throw run.h.error(`${method}: recordIds must be an array`);
      if (opts.recordIds.length > LIMITS.recordIdsPerQuery) throw run.h.error(`Request exceeds maximum batch size limit of ${LIMITS.recordIdsPerQuery} items`);
      for (const id of opts.recordIds) {
        if (typeof id !== 'string' || !RECORD_ID.test(id)) throw run.h.error(`${method}: "${id}" is not a record id`);
      }
      const wanted = new Set(opts.recordIds);
      records = [...table.records.values()].filter((r) => wanted.has(r.id));
    } else {
      records = [...table.records.values()];
    }
    records.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    if (opts.sorts !== undefined) {
      if (!Array.isArray(opts.sorts)) throw run.h.error(`${method}: sorts must be an array`);
      const keys = opts.sorts.map((s) => {
        if (!isPlainObject(s)) throw run.h.error(`${method}: each sort is {field, direction}`);
        const direction = s.direction === undefined ? 'asc' : s.direction;
        if (direction !== 'asc' && direction !== 'desc') throw run.h.error(`${method}: direction must be "asc" or "desc"`);
        return { field: this.resolveField(run, table, s.field), sign: direction === 'asc' ? 1 : -1 };
      });
      const sortValue = (field, rec) => {
        const v = this.scriptValue(field, rec);
        if (v === null) return null;
        if (Array.isArray(v)) return v.map((x) => (x && x.name) || '').join(', ');
        if (typeof v === 'object') return v.name;
        return v;
      };
      records = records
        .map((r, i) => ({ r, i }))
        .sort((x, y) => {
          for (const k of keys) {
            const a = sortValue(k.field, x.r);
            const b = sortValue(k.field, y.r);
            if (a === b) continue;
            if (a === null) return 1;
            if (b === null) return -1;
            const c = typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b));
            if (c) return c * k.sign;
          }
          return x.i - y.i;
        })
        .map((x) => x.r);
    }
    run.queries.push({
      table: table.name,
      method,
      fields: fields.map((f) => f.name),
      recordIds: singleId !== undefined ? 1 : opts.recordIds ? opts.recordIds.length : null,
      returned: records.length,
    });
    const loaded = new Set(fields.map((f) => f.id));
    const snaps = records.map((r) => this.snapshotRecord(run, table, r, loaded));
    if (singleId !== undefined) return snaps[0] || null;
    const byId = new Map(snaps.map((s) => [s.id, s]));
    return Object.freeze({
      get records() { return run.h.array(snaps); },
      get recordIds() { return run.h.array(snaps.map((s) => s.id)); },
      getRecord(id) {
        const s = byId.get(id);
        if (!s) throw run.h.error(`No record with id ${id} in this query result`);
        return s;
      },
    });
  }

  validateFields(run, table, fields) {
    if (!isPlainObject(fields)) throw run.h.error('The fields of a record must be an object');
    const changes = new Map();
    for (const [key, value] of Object.entries(fields)) {
      const field = this.resolveField(run, table, key);
      if (changes.has(field.id)) throw run.h.error(`Field "${field.name}" is given twice`);
      if (COMPUTED_TYPES.has(field.type)) throw run.h.error(`Field "${field.name}" is computed and cannot be written`);
      changes.set(field.id, this.fromScript(run, field, value));
    }
    return changes;
  }

  checkRecordId(run, table, id, seen, method) {
    if (typeof id !== 'string' || !RECORD_ID.test(id)) throw run.h.error(`${method}: "${id}" is not a record id`);
    if (!table.records.has(id)) throw run.h.error(`${method}: record ${id} does not exist in table "${table.name}"`);
    if (seen.has(id)) throw run.h.error(`${method}: record ${id} is listed twice in one call`);
    seen.add(id);
  }

  async write(run, table, op, items, method) {
    if (!Array.isArray(items)) throw run.h.error(`${method} expects an array`);
    if (items.length > LIMITS.recordsPerWrite) throw run.h.error(`Request exceeds maximum batch size limit of ${LIMITS.recordsPerWrite} items`);
    const plan = [];
    const seen = new Set();
    for (const item of items) {
      if (op === 'create') {
        if (!isPlainObject(item) || Object.keys(item).some((k) => k !== 'fields')) throw run.h.error(`${method}: each record is written as {fields: {...}}`);
        plan.push({ changes: this.validateFields(run, table, item.fields === undefined ? {} : item.fields) });
      } else if (op === 'update') {
        if (!isPlainObject(item) || Object.keys(item).some((k) => k !== 'id' && k !== 'fields')) throw run.h.error(`${method}: each record is written as {id, fields}`);
        this.checkRecordId(run, table, item.id, seen, method);
        plan.push({ id: item.id, changes: this.validateFields(run, table, item.fields) });
      } else {
        const id = typeof item === 'string' ? item : item && typeof item.id === 'string' ? item.id : item;
        this.checkRecordId(run, table, id, seen, method);
        plan.push({ id });
      }
    }
    run.startMutation(method, table.name, items.length);
    const work = (async () => {
      await sleep(this.latency);
      const ids = [];
      for (const step of plan) {
        if (op === 'create') {
          this.counter += 1;
          const rec = new SimRecord(makeId('rec', this.baseId, 'script', String(this.counter)), this.nowMs());
          table.records.set(rec.id, rec);
          this.applyCells(table, rec, step.changes);
          ids.push(rec.id);
        } else if (op === 'update') {
          this.applyCells(table, table.records.get(step.id), step.changes);
        } else {
          this.deleteRecord(table, step.id);
        }
      }
      return ids;
    })();
    run.track(work);
    return work;
  }

  tableApi(run, table) {
    const sim = this;
    const idOf = (recordOrId) => (recordOrId && typeof recordOrId === 'object' ? recordOrId.id : recordOrId);
    return Object.freeze({
      get id() { return table.id; },
      get name() { return table.name; },
      get description() { return table.description; },
      get fields() { return run.h.array(table.fields.map((f) => sim.fieldApi(run, f))); },
      getField(idOrName) { return sim.fieldApi(run, sim.resolveField(run, table, idOrName)); },
      async selectRecordsAsync(options) { return sim.query(run, table, 'selectRecordsAsync', options); },
      async selectRecordAsync(recordId, options) { return sim.query(run, table, 'selectRecordAsync', options, recordId); },
      async createRecordAsync(fields) {
        const ids = await sim.write(run, table, 'create', [{ fields }], 'createRecordAsync');
        return ids[0];
      },
      async createRecordsAsync(records) {
        const ids = await sim.write(run, table, 'create', records, 'createRecordsAsync');
        return run.h.array(ids);
      },
      async updateRecordAsync(recordOrId, fields) {
        await sim.write(run, table, 'update', [{ id: idOf(recordOrId), fields }], 'updateRecordAsync');
      },
      async updateRecordsAsync(records) {
        await sim.write(run, table, 'update', records, 'updateRecordsAsync');
      },
      async deleteRecordAsync(recordOrId) {
        await sim.write(run, table, 'delete', [idOf(recordOrId)], 'deleteRecordAsync');
      },
      async deleteRecordsAsync(list) {
        await sim.write(run, table, 'delete', Array.isArray(list) ? list.map(idOf) : list, 'deleteRecordsAsync');
      },
    });
  }

  baseApi(run) {
    const sim = this;
    const apis = new Map();
    const apiFor = (t) => {
      if (!apis.has(t.id)) apis.set(t.id, sim.tableApi(run, t));
      return apis.get(t.id);
    };
    return Object.freeze({
      get id() { return sim.baseId; },
      get name() { return sim.baseName; },
      get tables() { return run.h.array(sim.tables.map(apiFor)); },
      getTable(idOrName) {
        const t = typeof idOrName === 'string' ? sim.table(idOrName) : null;
        if (!t) throw run.h.error(`No table matching "${idOrName}" found in base "${sim.baseName}"`);
        return apiFor(t);
      },
    });
  }

  /**
   * Run one automation script.
   * @param {string|{code: string}} script a file path, or {code} for an inline script
   * @param {{inputs?: object, label?: string}} options inputs are what input.config() returns
   */
  async runScript(script, { inputs = {}, label } = {}) {
    const inline = typeof script === 'object' && script !== null;
    const code = inline ? script.code : fs.readFileSync(script, 'utf8');
    const filename = inline ? label || 'inline-script.js' : path.basename(script);
    const problem = jsonProblem(inputs, new Set(), 'inputs');
    if (problem) throw new Error(`test setup: ${problem}`);
    const run = new ScriptRun(label || filename, inputs);
    const context = vm.createContext({});
    run.h = vm.runInContext(BOOTSTRAP, context)(() => this.nowMs());
    context.base = this.baseApi(run);
    context.input = Object.freeze({ config: () => run.h.clone(JSON.stringify(inputs)) });
    context.output = Object.freeze({
      set: (key, value) => {
        if (typeof key !== 'string' || key === '') throw run.h.error('output.set needs a non-empty string key');
        const why = jsonProblem(value);
        if (why) throw run.h.error(`output.set("${key}"): the value is not JSON-serializable (${why})`);
        run.output[key] = JSON.parse(JSON.stringify(value));
        if (Buffer.byteLength(JSON.stringify(run.output)) > LIMITS.outputBytes) throw run.h.error('The output is larger than 6 MB');
      },
    });
    const format = (args) => args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    const log = (level) => (...args) => run.logs.push(`${level}: ${format(args)}`);
    context.console = Object.freeze({ log: log('log'), info: log('info'), warn: log('warn'), error: log('error'), debug: log('debug') });
    context.fetch = async () => {
      run.fetchCalls += 1;
      throw run.h.error('fetch is blocked in these tests: the scripts must not call outside services');
    };
    const started = Date.now();
    let timer;
    try {
      const compiled = new vm.Script(`(async () => {\n${code}\n})()`, { filename });
      const promise = compiled.runInContext(context, { timeout: LIMITS.runSeconds * 1000 });
      await Promise.race([
        promise,
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`Script exceeded execution time limit of ${LIMITS.runSeconds} seconds`)), LIMITS.runSeconds * 1000);
        }),
      ]);
      run.status = 'success';
    } catch (error) {
      run.status = 'failed';
      run.error = error && error.message !== undefined ? String(error.message) : String(error);
    } finally {
      clearTimeout(timer);
    }
    if (run.pending.size) {
      run.warn(`${run.pending.size} write call(s) were still running when the script finished (a missing await)`);
      await run.settle();
    }
    run.durationMs = Date.now() - started;
    return run;
  }
}
