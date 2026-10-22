#!/usr/bin/env node
/**
 * Tests for the four Airtable automation scripts in ../scripts.
 *
 * Airtable cannot run these scripts outside Airtable, and this repo never touches a real base, so
 * the scripts run here inside airtable-scripting-sim.mjs: a strict stand-in for the automation
 * scripting environment, loaded with base/schema.json and the seed data in base/seed. The scripts
 * run unchanged, exactly as they would be pasted into a "Run a script" action.
 *
 * The first scenario proves the stand-in refuses what Airtable refuses (51 records in one call,
 * fields that were not loaded, wrong value formats, parallel writes, too many queries, extension
 * only APIs), so the other scenarios mean something. Expected numbers come from base/seed/_meta.json.
 *
 * Requirements: Node 18 or later. No packages, no network.
 * Usage: node automations/06-airtable-native-scripts/tests/run-tests.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { AirtableSim, LIMITS } from './airtable-scripting-sim.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..', '..');
// SCRIPTS_DIR lets mutation-check.mjs run this suite against deliberately broken copies.
const SCRIPT_DIR = process.env.SCRIPTS_DIR ? path.resolve(process.env.SCRIPTS_DIR) : path.join(HERE, '..', 'scripts');
const SCRIPTS = {
  dedupe: path.join(SCRIPT_DIR, '01-dedupe-customers.js'),
  stats: path.join(SCRIPT_DIR, '02-customer-stats.js'),
  link: path.join(SCRIPT_DIR, '03-link-order-lines.js'),
  dq: path.join(SCRIPT_DIR, '04-weekly-data-quality.js'),
};
const FROZEN = '2026-10-20T07:00:00Z';
const FROZEN_ISO = '2026-10-20T07:00:00.000Z';
const THRESHOLDS = { silverThreshold: '40', goldThreshold: '75', platinumThreshold: '150' };
const META = JSON.parse(fs.readFileSync(path.join(ROOT, 'base', 'seed', '_meta.json'), 'utf8')).expected['06-airtable-scripts'];
const ALLOWED_WRITES = {
  '01-dedupe-customers.js': ['Orders', 'Tickets', 'Subscribers', 'Customers', 'Sync Log'],
  '02-customer-stats.js': ['Customers', 'Sync Log'],
  '03-link-order-lines.js': ['Order Lines', 'Orders', 'Sync Log'],
  '04-weekly-data-quality.js': ['Data Quality', 'Sync Log'],
};

class Report {
  constructor() {
    this.results = [];
  }

  check(scenario, label, ok, detail = '') {
    this.results.push([scenario, label, Boolean(ok), detail]);
    return Boolean(ok);
  }

  print() {
    let last = null;
    for (const [scenario, label, ok, detail] of this.results) {
      if (scenario !== last) {
        console.log(`\n${scenario}`);
        last = scenario;
      }
      console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? `  (${detail})` : ''}`);
    }
    const passed = this.results.filter((r) => r[2]).length;
    console.log(`\n${passed}/${this.results.length} checks passed`);
    return passed === this.results.length;
  }
}

const report = new Report();
const SCRIPT_RUNS = [];

function fresh(now = FROZEN) {
  return new AirtableSim({ root: ROOT, now });
}

async function run(sim, key, inputs = {}) {
  const r = await sim.runScript(SCRIPTS[key], { inputs });
  SCRIPT_RUNS.push(r);
  return r;
}

function status(r) {
  return r.status + (r.error ? `: ${r.error}` : '');
}

function writes(r) {
  return r.writesByTable();
}

function state(sim, except = []) {
  return JSON.stringify(sim.tables.filter((t) => !except.includes(t.name)).map((t) => [t.name, sim.rows(t.name)]));
}

function omit(obj, keys) {
  return Object.fromEntries(Object.entries(obj).filter(([k]) => !keys.includes(k)));
}

function sorted(list) {
  return [...(list || [])].sort();
}

function syncRow(sim, prefix) {
  return sim.rows('Sync Log').filter((r) => r.fields['Run ID'].startsWith(prefix)).map((r) => r.fields);
}

function isoWeekOf(ms) {
  const d = new Date(ms);
  const day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const mondayBased = (new Date(day).getUTCDay() + 6) % 7;
  const thursday = new Date(day + (3 - mondayBased) * 86400000);
  const year = thursday.getUTCFullYear();
  const week = 1 + Math.floor((thursday.getTime() - Date.UTC(year, 0, 1)) / (7 * 86400000));
  return `${year}-W${String(week).padStart(2, '0')}`;
}

// ====================================================================== 0. the stand-in itself

async function emulatorStrictness() {
  const S = 'The stand-in refuses what Airtable refuses';
  let sim = fresh();
  const before = sim.count('Data Quality');
  let r = await sim.runScript({
    code: `const t = base.getTable('Data Quality');
const records = [];
for (let i = 0; i < 51; i++) records.push({ fields: { 'Finding ID': 'x' + i } });
await t.createRecordsAsync(records);`,
  });
  report.check(S, '51 records in one write call fail with the batch limit, nothing written',
    r.status === 'failed' && r.error === 'Request exceeds maximum batch size limit of 50 items' && sim.count('Data Quality') === before, status(r));

  const emailId = sim.mustTable('Customers').field('Email').id;
  r = await sim.runScript({
    code: `const t = base.getTable('Customers');
const q = await t.selectRecordsAsync({ fields: ['Name'] });
const out = [];
try { q.records[0].getCellValue('Email'); } catch (e) { out.push(e.message); }
try { q.records[0].getCellValue('Emial'); } catch (e) { out.push(e.message); }
try { await t.selectRecordsAsync({ fields: ['Emial'] }); } catch (e) { out.push(e.message); }
try { await t.updateRecordAsync(q.records[0].id, { 'Emial': 'a@example.com' }); } catch (e) { out.push(e.message); }
try { await t.selectRecordsAsync({ fields: ['Name'], recordIds: q.records.concat(q.records, q.records, q.records).map(x => x.id) }); } catch (e) { out.push(e.message); }
output.set('errors', out);`,
  });
  const unknown = 'No field matching "Emial" found in table "Customers"';
  report.check(S, 'unloaded field, unknown field and 124 recordIds in one query are refused with Airtable\'s messages',
    isDeepStrictEqual(r.output.errors, [`Cell values for field ${emailId} are not loaded`, unknown, unknown, unknown, 'Request exceeds maximum batch size limit of 100 items']),
    JSON.stringify(r.output.errors && r.output.errors[0]));

  sim = fresh();
  const snap = state(sim);
  r = await sim.runScript({
    code: `const t = base.getTable('Customers');
const q = await t.selectRecordsAsync({ fields: ['Name'] });
const id = q.records[0].id;
const attempts = [
  { 'Record Status': 'Duplicate' },
  { 'Record Status': { name: 'Merged' } },
  { 'Merged Into': [id] },
  { 'Merged Into': [{ id: 'recAAAAAAAAAAAAAA' }] },
  { 'Merged Into': [{ id: id, name: 'Maya Chen' }] },
  { 'Order Count': '3' },
  { 'Stats Updated At': '2026-10-20' },
  { 'Accepts Marketing': 'yes' },
  { 'Shopify Customer ID': 'line one\\nline two' },
];
const out = [];
for (const a of attempts) { try { await t.updateRecordAsync(id, a); out.push('accepted'); } catch (e) { out.push(e.message); } }
output.set('results', out);`,
  });
  const results = r.output.results || [];
  report.check(S, 'values in the wrong write format are refused (select as text, unknown option, link as text or to a missing record, number as text, date into a date time)',
    results.length === 9 && results.every((m) => m.startsWith('Field "') && m.includes('cannot accept the provided value')) && state(sim) === snap,
    `${results.filter((m) => m !== 'accepted').length}/9 refused`);

  sim = fresh();
  r = await sim.runScript({
    code: `const q = await base.getTable('Orders').selectRecordsAsync({ fields: ['Order Number', 'Customer', 'Cold Pack', 'Cold Pack Shipped', 'Tickets', 'Total', 'Delivered At'] });
const r = q.records.filter(x => x.name === '#1001')[0];
output.set('values', { customer: r.getCellValue('Customer'), coldPack: r.getCellValue('Cold Pack'), shipped: r.getCellValue('Cold Pack Shipped'),
  tickets: r.getCellValue('Tickets'), total: r.getCellValue('Total'), delivered: r.getCellValue('Delivered At'), customerText: r.getCellValueAsString('Customer'),
  isArray: r.getCellValue('Customer') instanceof Array });`,
  });
  const coldPack = sim.mustTable('Orders').field('Cold Pack').choices.find((c) => c.name === 'Not Needed');
  report.check(S, 'cells read in the scripting format: link [{id, name}], select {id, name, color}, empty link and unticked checkbox null',
    isDeepStrictEqual(r.output.values, {
      customer: [{ id: sim.ref('Customers', 'maya'), name: 'Maya Chen' }],
      coldPack: { id: coldPack.id, name: 'Not Needed', color: 'greenLight2' },
      shipped: null, tickets: null, total: 28.45, delivered: '2026-08-29T17:45:00.000Z', customerText: 'Maya Chen', isArray: true,
    }), status(r));

  sim = fresh();
  const parallel = await sim.runScript({
    code: `const t = base.getTable('Customers');
const q = await t.selectRecordsAsync({ fields: ['Notes'] });
await Promise.all(q.records.slice(0, 20).map(r => t.updateRecordAsync(r, { 'Notes': 'parallel' })));`,
  });
  const sequential = await sim.runScript({
    code: `const t = base.getTable('Customers');
const q = await t.selectRecordsAsync({ fields: ['Notes'] });
for (const r of q.records.slice(0, 20)) await t.updateRecordAsync(r, { 'Notes': 'one by one' });`,
  });
  report.check(S, '20 writes fired in parallel trip the 15 per second limit; the same 20 awaited one by one pass',
    parallel.status === 'failed' && parallel.error === 'Exceeded quota of 15 mutations per second' && sequential.status === 'success' && sequential.mutations.length === 20,
    `${status(parallel)} / ${status(sequential)}`);

  const queries = (n) => `const t = base.getTable('Products');
for (let i = 0; i < ${n}; i++) await t.selectRecordsAsync({ fields: ['SKU'] });`;
  const thirty = await sim.runScript({ code: queries(30) });
  const thirtyOne = await sim.runScript({ code: queries(31) });
  report.check(S, '30 queries in one run pass, the 31st fails',
    thirty.status === 'success' && thirtyOne.status === 'failed' && thirtyOne.error === 'Exceeded quota of 30 selectRecords queries per script invocation', status(thirtyOne));

  const apis = await sim.runScript({
    code: `const out = [];
try { output.markdown('# hi'); } catch (e) { out.push(e.constructor.name + ' ' + e.message); }
try { await input.textAsync('name?'); } catch (e) { out.push(e.constructor.name + ' ' + e.message); }
try { setTimeout(function () {}, 10); } catch (e) { out.push(e.constructor.name + ' ' + e.message); }
try { output.set('bad', undefined); } catch (e) { out.push(e.message); }
try { output.set('fn', { f: function () {} }); } catch (e) { out.push(e.message); }
output.set('config', input.config());
output.set('out', out);`,
  }, { inputs: { dryRun: 'true', ids: ['recAAAAAAAAAAAAAA'] } });
  report.check(S, 'automation only APIs: no output.markdown, input.textAsync or setTimeout; output.set takes JSON only; input.config() returns the inputs',
    apis.status === 'success' && isDeepStrictEqual(apis.output.out, [
      'TypeError output.markdown is not a function', 'TypeError input.textAsync is not a function', 'ReferenceError setTimeout is not defined',
      'output.set("bad"): the value is not JSON-serializable (value is undefined)', 'output.set("fn"): the value is not JSON-serializable (value.f is a function)',
    ]) && isDeepStrictEqual(apis.output.config, { dryRun: 'true', ids: ['recAAAAAAAAAAAAAA'] }), status(apis));

  const sloppy = await sim.runScript({
    code: `const t = base.getTable('Customers');
const q = await t.selectRecordsAsync();
t.updateRecordAsync(q.records[0], { 'Notes': 'not awaited' });`,
  });
  report.check(S, 'a query without fields and a write without await are both recorded as warnings',
    sloppy.warnings.length === 2 && sloppy.warnings[0].includes('without "fields"') && sloppy.warnings[1].includes('missing await'), sloppy.warnings.join(' | '));
}

// ====================================================================== 1. weekly data quality

function expectedFirstFindings(sim) {
  const list = [];
  for (const ref of Object.keys(META.missingRequired)) list.push(['Missing Required Field', 'Customers', ref]);
  for (const ref of Object.keys(META.invalidEmails)) list.push(['Invalid Email', 'Customers', ref]);
  for (const ref of META.orphanOrderLines) list.push(['Orphan Order Line', 'Order Lines', ref]);
  for (const refs of Object.values(META.duplicateGroups)) list.push(['Duplicate Customer', 'Customers', refs[1]]);
  for (const [ref, note] of Object.entries(META.unlinkedOrderLines)) if (note.includes('unknown SKU')) list.push(['Unknown SKU', 'Order Lines', ref]);
  return list.map(([check, table, ref]) => `2026-W43 / ${check} / ${sim.ref(table, ref)}`).sort();
}

async function dataQualityFirstRun() {
  const S = 'Weekly data quality report on the seed (week 2026-W43)';
  const sim = fresh();
  const legacy = sim.byPrimary('Data Quality', '2026-W42 / Invalid Email / sam');
  const r = await run(sim, 'dq');
  const rows = sim.rows('Data Quality').filter((x) => x.fields['Report Week'] === '2026-W43');
  const ids = rows.map((x) => x.fields['Finding ID']).sort();
  report.check(S, 'exactly the 10 findings the seed holds: 2 missing fields, 3 invalid emails, 1 orphan line, 3 duplicates, 1 unknown SKU',
    r.status === 'success' && isDeepStrictEqual(ids, expectedFirstFindings(sim)), `${rows.length} rows, ${status(r)}`);

  const sam = sim.ref('Customers', 'sam');
  const samRow = sim.byPrimary('Data Quality', `2026-W43 / Invalid Email / ${sam}`);
  report.check(S, 'a finding row has every field: week, check, table, record id and name, details, severity, status, time',
    samRow && isDeepStrictEqual(samRow.fields, {
      'Finding ID': `2026-W43 / Invalid Email / ${sam}`, 'Report Week': '2026-W43', 'Check': 'Invalid Email', 'Table': 'Customers',
      'Record ID': sam, 'Record Name': 'Sam Ortiz', 'Details': 'Email "sam.ortiz@example" has no domain ending.', 'Severity': 'Medium',
      'Status': 'Open', 'Found At': FROZEN_ISO,
    }), samRow ? samRow.fields.Details : 'missing');

  const detail = (table, ref, check) => {
    const row = sim.byPrimary('Data Quality', `2026-W43 / ${check} / ${sim.ref(table, ref)}`);
    return row ? [row.fields['Record Name'], row.fields.Details, row.fields.Severity] : null;
  };
  const got = {
    lena: detail('Customers', 'lena', 'Invalid Email'),
    tomN: detail('Customers', 'tom-n', 'Invalid Email'),
    grace: detail('Customers', 'grace', 'Missing Required Field'),
    kai: detail('Customers', 'kai', 'Missing Required Field'),
    orphan: detail('Order Lines', 'l-orphan', 'Orphan Order Line'),
    ginger: detail('Order Lines', 'l1011-3', 'Unknown SKU'),
    mayaDup: detail('Customers', 'maya-dup', 'Duplicate Customer'),
  };
  const maya = sim.ref('Customers', 'maya');
  report.check(S, 'details say what is wrong and how to fix it (grace gets her subscription email, kai is named by email)',
    isDeepStrictEqual(got, {
      lena: ['Lena Fischer', 'Email "lena.fischer.example.com" has no @.', 'Medium'],
      tomN: ['Tom Nguyen', 'Email "tom.nguyen@@example.com" has more than one @.', 'Medium'],
      grace: ['Grace Kim', 'Missing Email. The linked subscription has the email grace.kim@example.com.', 'Medium'],
      kai: ['Unnamed record (kai.morgan@example.com)', 'Missing Name.', 'Medium'],
      orphan: ['LEGACY-0001', 'Order line LEGACY-0001 is not linked to an order (SKU DC-BAR-DARK-SEASALT, quantity 1, unit price 7.50).', 'High'],
      ginger: ['14100000101103', 'SKU "DC-BAR-GINGER" on line 14100000101103 (order #1011) matches no product.', 'High'],
      mayaDup: ['Maya Chen', `Same email as Maya Chen (${maya}). The dedupe script (script 1) merges it into that record.`, 'Medium'],
    }), '7 rows compared');

  const legacyAfter = sim.byPrimary('Data Quality', '2026-W42 / Invalid Email / sam');
  report.check(S, 'last week\'s hand written finding without a Record ID is left exactly as it was',
    legacy && legacyAfter && isDeepStrictEqual(legacy, legacyAfter) && r.output.legacyWithoutRecordId === 1, `legacyWithoutRecordId ${r.output.legacyWithoutRecordId}`);

  report.check(S, 'output for the next step: counts per check and a plain text report',
    isDeepStrictEqual(r.output.byCheck, { 'Missing Required Field': 2, 'Invalid Email': 3, 'Orphan Order Line': 1, 'Duplicate Customer': 3, 'Unknown SKU': 1 }) &&
    r.output.newFindings === 10 && r.output.reportWeek === '2026-W43' &&
    r.output.reportText.split('\n')[0] === 'Data quality report 2026-W43 (2026-10-20)' &&
    r.output.reportText.includes('\n- Invalid Email: 3 (Lena Fischer, Sam Ortiz, Tom Nguyen)\n') &&
    r.output.reportText.includes('\n- Order lines with a known SKU but no product link: 2 (script 3 links them)'),
    r.output.reportText.split('\n')[1]);

  const log = syncRow(sim, 'data-quality-');
  report.check(S, 'one Sync Log row with the counts',
    log.length === 1 && isDeepStrictEqual(omit(log[0], ['Details']), {
      'Run ID': 'data-quality-2026-10-20T07:00:00Z', 'Automation': '06 Airtable Scripts', 'Trigger': 'Airtable Automation',
      'Started At': FROZEN_ISO, 'Finished At': FROZEN_ISO, 'Status': 'Success', 'Items Checked': 129, 'Items Changed': 10, 'Errors': 0,
    }) && log[0].Details === r.output.reportText, log.length ? `${log[0]['Items Checked']} checked, ${log[0]['Items Changed']} changed` : 'no row');

  report.check(S, 'one batched create for the findings, 6 queries, every query names its fields',
    isDeepStrictEqual(r.mutations, [{ method: 'createRecordsAsync', table: 'Data Quality', count: 10 }, { method: 'createRecordAsync', table: 'Sync Log', count: 1 }]) &&
    r.queries.length === 6 && r.warnings.length === 0, `${r.queries.length} queries: ${r.queries.map((q) => q.table).join(', ')}`);

  const snap = state(sim, ['Sync Log']);
  const again = await run(sim, 'dq');
  report.check('Weekly data quality report, run again the same week', 'no new or changed findings, only the Sync Log row',
    again.status === 'success' && state(sim, ['Sync Log']) === snap && again.output.newFindings === 0 && again.output.stillOpen === 10 &&
    isDeepStrictEqual(writes(again), { 'Sync Log': 1 }), `new ${again.output.newFindings}, still open ${again.output.stillOpen}`);

  const dry = fresh();
  const drySnap = state(dry);
  const dr = await run(dry, 'dq', { dryRun: 'true' });
  report.check('Weekly data quality report, run again the same week', 'a dry run reports the same 10 findings and writes nothing at all',
    dr.status === 'success' && state(dry) === drySnap && dr.mutations.length === 0 && dr.output.newFindings === 10 && dr.output.dryRun === true, status(dr));
}

// ====================================================================== 2. dedupe customers

async function dedupe() {
  const S = 'Merge duplicate customers on the seed';
  let sim = fresh();
  const ref = (r) => sim.ref('Customers', r);
  const snap = state(sim);
  const dry = await run(sim, 'dedupe', { dryRun: true });
  report.check(S, 'a dry run plans the 3 merges and writes nothing, not even Sync Log',
    dry.status === 'success' && state(sim) === snap && dry.mutations.length === 0 && dry.output.duplicatesMerged === 3, dry.output.summary);

  const keepBefore = Object.fromEntries(['maya', 'jordan', 'priya'].map((k) => [k, sim.fields('Customers', ref(k))]));
  const r = await run(sim, 'dedupe');
  report.check(S, 'keeps the record with a Shopify Customer ID in each group (maya, jordan, priya)',
    r.status === 'success' && isDeepStrictEqual(r.output.merges, [
      { email: 'jordan.blake@example.com', kept: `Jordan Blake (${ref('jordan')})`, keptId: ref('jordan'), duplicateIds: [ref('jordan-dup')] },
      { email: 'maya.chen@example.com', kept: `Maya Chen (${ref('maya')})`, keptId: ref('maya'), duplicateIds: [ref('maya-dup')] },
      { email: 'priya.raman@example.com', kept: `Priya Raman (${ref('priya')})`, keptId: ref('priya'), duplicateIds: [ref('priya-dup')] },
    ]), status(r));

  const dupState = (dup, keep, moved) => {
    const f = sim.fields('Customers', ref(dup));
    const name = keep === 'priya' ? 'Priya Raman' : keep === 'maya' ? 'Maya Chen' : 'Jordan Blake';
    return f['Record Status'] === 'Duplicate' && isDeepStrictEqual(f['Merged Into'], [ref(keep)]) && f.Orders === undefined &&
      f.Notes === `2026-10-20: duplicate of ${name} (${ref(keep)}), merged by the dedupe script. Moved: ${moved}.`;
  };
  report.check(S, 'each duplicate is marked Duplicate, points at the kept record and says what moved',
    dupState('maya-dup', 'maya', '#1004') && dupState('jordan-dup', 'jordan', '#1011') && dupState('priya-dup', 'priya', 'nothing'),
    sim.fields('Customers', ref('maya-dup')).Notes);

  const orderCustomer = (n) => sim.byPrimary('Orders', n).fields.Customer;
  const orderNames = (k) => sorted((sim.fields('Customers', ref(k)).Orders || []).map((id) => sim.fields('Orders', id)['Order Number']));
  report.check(S, 'orders #1004 and #1011 now belong to the kept records, and both sides of the link agree',
    isDeepStrictEqual(orderCustomer('#1004'), [ref('maya')]) && isDeepStrictEqual(orderCustomer('#1011'), [ref('jordan')]) &&
    isDeepStrictEqual(orderNames('maya'), ['#1001', '#1004']) && isDeepStrictEqual(orderNames('jordan'), ['#1002', '#1011']) && r.output.ordersMoved === 2,
    `maya ${orderNames('maya').join(' ')}, jordan ${orderNames('jordan').join(' ')}`);

  report.check(S, 'writes only the 2 moved orders and the 6 customers, in one call each',
    isDeepStrictEqual(r.mutations, [
      { method: 'updateRecordsAsync', table: 'Orders', count: 2 }, { method: 'updateRecordsAsync', table: 'Customers', count: 6 },
      { method: 'createRecordAsync', table: 'Sync Log', count: 1 },
    ]), JSON.stringify(writes(r)));

  const keepOk = ['maya', 'jordan', 'priya'].every((k) => {
    const after = sim.fields('Customers', ref(k));
    const dup = sim.fields('Customers', ref(`${k}-dup`));
    return isDeepStrictEqual(omit(after, ['Orders', 'Notes']), omit(keepBefore[k], ['Orders', 'Notes'])) &&
      after.Notes === `2026-10-20: merged 1 duplicate record with the same email: ${dup.Name} (${ref(`${k}-dup`)}).`;
  });
  report.check(S, 'kept records only gain a Notes line (no field overwritten, nothing to fill in on the seed)', keepOk,
    sim.fields('Customers', ref('maya')).Notes);

  report.check(S, 'output names the 6 affected customers for the stats script, and the summary reads well',
    isDeepStrictEqual(sorted(r.output.affectedCustomerIds), sorted(['maya', 'maya-dup', 'jordan', 'jordan-dup', 'priya', 'priya-dup'].map(ref))) &&
    r.output.summary === 'Merged 3 duplicate customers into 3 records (moved 2 orders, 0 tickets, 0 subscriptions). 0 earlier duplicates had new links. 0 groups left for a person because of different Shopify Customer IDs. 0 problems.',
    r.output.summary);

  const log = syncRow(sim, 'customer-dedupe-');
  report.check(S, 'one Sync Log row: 31 checked, 8 changed, Success',
    log.length === 1 && log[0].Status === 'Success' && log[0]['Items Checked'] === 31 && log[0]['Items Changed'] === 8 && log[0].Errors === 0 &&
    log[0].Details === r.output.summary, log.length ? `${log[0].Status}, ${log[0]['Items Changed']} changed` : 'no row');

  const snap2 = state(sim, ['Sync Log']);
  const again = await run(sim, 'dedupe');
  report.check('Merge duplicate customers, run again', 'nothing left to merge, only the Sync Log row',
    again.status === 'success' && state(sim, ['Sync Log']) === snap2 && again.output.duplicateGroups === 0 && isDeepStrictEqual(writes(again), { 'Sync Log': 1 }),
    again.output.summary);

  // Links that reach a merged duplicate after the merge.
  const S2 = 'Merge duplicate customers: links that reach a merged record later';
  const late = sim.insert('Orders', { 'Order Number': '#1026', 'Customer': [ref('maya-dup')], 'Email': 'maya.chen@example.com', 'Order Status': 'Open', 'Total': 12.5 });
  const chained = sim.insert('Customers', { 'Name': 'Maya Chen (old import)', 'Email': 'maya.chen@example.com', 'Record Status': 'Duplicate', 'Merged Into': [ref('maya-dup')] });
  const ticket = sim.insert('Tickets', { 'Ticket ID': 'T-0099', 'Customer': [chained] });
  const lost = sim.insert('Customers', { 'Name': 'Lost Duplicate', 'Email': 'lost.duplicate@example.com', 'Record Status': 'Duplicate' });
  const lostOrder = sim.insert('Orders', { 'Order Number': '#1027', 'Customer': [lost], 'Order Status': 'Open', 'Total': 9.5 });
  const s = await run(sim, 'dedupe');
  report.check(S2, 'an order linked to a merged duplicate, and a ticket on a duplicate of a duplicate, both move to the kept record',
    s.status === 'success' && isDeepStrictEqual(sim.fields('Orders', late).Customer, [ref('maya')]) &&
    isDeepStrictEqual(sim.fields('Tickets', ticket).Customer, [ref('maya')]) && s.output.ordersMoved === 1 && s.output.ticketsMoved === 1,
    `#1026 -> ${sim.fields('Orders', late).Customer}, T-0099 -> ${sim.fields('Tickets', ticket).Customer}`);
  report.check(S2, 'a duplicate with no Merged Into keeps its order and is reported as a problem, Sync Log says Partial',
    isDeepStrictEqual(sim.fields('Orders', lostOrder).Customer, [lost]) && s.output.problems.length === 1 && s.output.problems[0].startsWith(`Lost Duplicate (${lost}) is marked Duplicate`) &&
    syncRow(sim, 'customer-dedupe-').slice(-1)[0].Status === 'Partial', s.output.problems[0]);
  report.check(S2, 'only the moved order and ticket are written, no customer record is touched again',
    isDeepStrictEqual(writes(s), { Orders: 1, Tickets: 1, 'Sync Log': 1 }), JSON.stringify(writes(s)));

  // Tickets, subscriptions, blank fields, three in a group and Shopify ID conflicts.
  const S3 = 'Merge duplicate customers: tickets, subscriptions, blanks and conflicts';
  sim = fresh();
  const id = (r) => sim.ref('Customers', r);
  const t98 = sim.insert('Tickets', { 'Ticket ID': 'T-0098', 'Customer': [id('jordan-dup')] });
  const sub3 = sim.ref('Subscribers', 's03');
  sim.update('Subscribers', sub3, { 'Customer': [id('priya-dup')] });
  sim.update('Customers', id('maya'), { 'Phone': null, 'Accepts Marketing': false });
  sim.update('Customers', id('maya-dup'), { 'Accepts Marketing': true });
  const third = sim.insert('Customers', { 'Name': 'J. Blake', 'Email': ' JORDAN.BLAKE@EXAMPLE.COM', 'Created Via': 'Import', 'Record Status': 'Active' });
  const lee1 = sim.insert('Customers', { 'Name': 'Sam Lee', 'Email': 'sam.lee@example.com', 'Shopify Customer ID': '7700000000201', 'Created Via': 'Shopify', 'Record Status': 'Active' });
  const lee2 = sim.insert('Customers', { 'Name': 'Samuel Lee', 'Email': 'Sam.Lee@example.com', 'Shopify Customer ID': '7700000000202', 'Created Via': 'Shopify', 'Record Status': 'Active' });
  const m = await run(sim, 'dedupe');
  report.check(S3, 'a ticket and a subscription on duplicates move to the kept records',
    m.status === 'success' && isDeepStrictEqual(sim.fields('Tickets', t98).Customer, [id('jordan')]) &&
    isDeepStrictEqual(sim.fields('Subscribers', sub3).Customer, [id('priya')]) && m.output.ticketsMoved === 1 && m.output.subscriptionsMoved === 1,
    status(m));
  const mayaNow = sim.fields('Customers', id('maya'));
  report.check(S3, 'a missing phone is copied to the kept record, marketing consent is not',
    mayaNow.Phone === '+15550101001' && mayaNow['Accepts Marketing'] === undefined, `phone ${mayaNow.Phone}, consent ${mayaNow['Accepts Marketing']}`);
  const jordanMerge = m.output.merges.find((x) => x.email === 'jordan.blake@example.com');
  report.check(S3, 'three records with one email: both extras merge into jordan, the one with an order ranked first',
    jordanMerge && isDeepStrictEqual(jordanMerge.duplicateIds, [id('jordan-dup'), third]) &&
    isDeepStrictEqual(sim.fields('Customers', third)['Merged Into'], [id('jordan')]) &&
    sim.fields('Customers', id('jordan')).Notes === `2026-10-20: merged 2 duplicate records with the same email: Jordan Blake (${id('jordan-dup')}), J. Blake (${third}).`,
    jordanMerge ? jordanMerge.duplicateIds.join(', ') : 'no merge');
  report.check(S3, 'two records with different Shopify Customer IDs are left Active and listed as a conflict',
    isDeepStrictEqual(m.output.conflicts, [{ email: 'sam.lee@example.com', shopifyCustomerIds: ['7700000000201', '7700000000202'], customers: [`Sam Lee (${lee1})`, `Samuel Lee (${lee2})`].sort() }]) &&
    sim.fields('Customers', lee1)['Record Status'] === 'Active' && sim.fields('Customers', lee2)['Record Status'] === 'Active' &&
    syncRow(sim, 'customer-dedupe-')[0].Status === 'Partial', JSON.stringify(m.output.conflicts.map((c) => c.email)));
}

// ====================================================================== 3. customer stats

function expectedTier(ltv) {
  if (ltv >= 150) return 'Platinum';
  if (ltv >= 75) return 'Gold';
  if (ltv >= 40) return 'Silver';
  return 'Standard';
}

async function customerStats() {
  const S = 'Customer stats for every customer on the seed';
  let sim = fresh();
  const id = (r) => sim.ref('Customers', r);
  const r = await run(sim, 'stats', THRESHOLDS);
  const f = (ref) => sim.fields('Customers', id(ref));
  const ltvWrong = Object.entries(META.lifetimeValueByCustomerRef).filter(([ref, v]) => f(ref)['Lifetime Value'] !== v);
  const countWrong = Object.entries(META.orderCountByCustomerRef).filter(([ref, v]) => f(ref)['Order Count'] !== v);
  report.check(S, 'Lifetime Value and Order Count match the seed facts for all 22 customers with orders',
    r.status === 'success' && ltvWrong.length === 0 && countWrong.length === 0, ltvWrong.length || countWrong.length ? `wrong: ${ltvWrong.concat(countWrong).map((x) => x[0]).join(', ')}` : '22 of 22');
  report.check(S, 'refunds are subtracted and cancelled orders left out (ahmed 28.95 - 7.25 = 21.70, owen only cancelled = 0)',
    f('ahmed')['Lifetime Value'] === 21.7 && f('owen')['Lifetime Value'] === 0 && f('owen')['Order Count'] === 0 && f('derek')['Lifetime Value'] === 80.35,
    `ahmed ${f('ahmed')['Lifetime Value']}, owen ${f('owen')['Lifetime Value']}`);
  const zeros = META.customersWithoutCountedOrders.filter((ref) => {
    const c = f(ref);
    return c['Lifetime Value'] === 0 && c['Order Count'] === 0 && c['Average Order Value'] === undefined && c['VIP Tier'] === 'Standard';
  });
  report.check(S, 'the 9 customers without a counted order get a stored 0 and 0, an empty average and Standard',
    zeros.length === 9 && META.customersWithoutCountedOrders.length === 9, `${zeros.length} of 9`);
  const aovWrong = Object.keys(META.lifetimeValueByCustomerRef).filter((ref) => {
    const c = f(ref);
    return c['Average Order Value'] !== Math.round((Math.round(c['Lifetime Value'] * 100)) / c['Order Count']) / 100;
  });
  report.check(S, 'Average Order Value is rounded to the cent (derek 80.35 / 3 = 26.78)',
    aovWrong.length === 0 && f('derek')['Average Order Value'] === 26.78, `derek ${f('derek')['Average Order Value']}`);
  const tierWrong = Object.entries(META.lifetimeValueByCustomerRef).filter(([ref, v]) => f(ref)['VIP Tier'] !== expectedTier(v));
  report.check(S, 'VIP tiers from the thresholds 40 / 75 / 150: derek Gold, 5 Silver, 25 Standard',
    tierWrong.length === 0 && isDeepStrictEqual(r.output.tiers, { Standard: 25, Silver: 5, Gold: 1, Platinum: 0 }) && f('derek')['VIP Tier'] === 'Gold' &&
    isDeepStrictEqual(sorted(sim.rows('Customers').filter((c) => c.fields['VIP Tier'] === 'Silver').map((c) => c.fields.Name)), ['Hana Sato', 'Jordan Blake', 'Marcus Webb', 'Mila Petrova', 'Victor Hale']),
    JSON.stringify(r.output.tiers));
  report.check(S, 'every customer gets Stats Updated At, all 31 in one write call, from 2 queries',
    sim.rows('Customers').every((c) => c.fields['Stats Updated At'] === FROZEN_ISO) &&
    isDeepStrictEqual(r.mutations, [{ method: 'updateRecordsAsync', table: 'Customers', count: 31 }, { method: 'createRecordAsync', table: 'Sync Log', count: 1 }]) &&
    r.queries.length === 2 && r.warnings.length === 0, `${r.queries.length} queries`);
  const log = syncRow(sim, 'customer-stats-');
  report.check(S, 'one Sync Log row for the full run: 31 checked, 31 changed',
    log.length === 1 && log[0].Status === 'Success' && log[0]['Items Checked'] === 31 && log[0]['Items Changed'] === 31 &&
    log[0].Details === 'Checked 31 customers (all), updated 31. Tiers: Standard 25, Silver 5, Gold 1, Platinum 0.', log.length ? log[0].Details : 'no row');

  const snap = state(sim, ['Sync Log']);
  const again = await run(sim, 'stats', THRESHOLDS);
  report.check('Customer stats, run again', 'nothing changed, nothing written but the Sync Log row',
    again.status === 'success' && state(sim, ['Sync Log']) === snap && again.output.customersUpdated === 0 && isDeepStrictEqual(writes(again), { 'Sync Log': 1 }),
    again.output.summary);

  // Chained after the dedupe, with the list the dedupe script outputs.
  const S2 = 'Customer stats after the merge (second action, customerRecordIds from script 1)';
  const d = await run(sim, 'dedupe');
  const logsBefore = sim.count('Sync Log');
  const chained = await run(sim, 'stats', { ...THRESHOLDS, customerRecordIds: d.output.affectedCustomerIds });
  const c = (ref) => sim.fields('Customers', id(ref));
  report.check(S2, 'merged lifetime values: maya 28.45 + 21.45 = 49.90 Silver, jordan 43.95 + 30.40 = 74.35 Silver (just under Gold)',
    chained.status === 'success' && c('maya')['Lifetime Value'] === 49.9 && c('maya')['Order Count'] === 2 && c('maya')['VIP Tier'] === 'Silver' &&
    c('jordan')['Lifetime Value'] === 74.35 && c('jordan')['Order Count'] === 2 && c('jordan')['VIP Tier'] === 'Silver' && c('priya')['Lifetime Value'] === 36.95,
    `maya ${c('maya')['Lifetime Value']}, jordan ${c('jordan')['Lifetime Value']}`);
  report.check(S2, 'averages round half a cent up (jordan 74.35 / 2 = 37.175 -> 37.18), duplicates drop to 0 with an empty average',
    c('jordan')['Average Order Value'] === 37.18 && c('maya')['Average Order Value'] === 24.95 &&
    ['maya-dup', 'jordan-dup', 'priya-dup'].every((ref) => c(ref)['Lifetime Value'] === 0 && c(ref)['Order Count'] === 0 && c(ref)['Average Order Value'] === undefined),
    `jordan ${c('jordan')['Average Order Value']}`);
  report.check(S2, 'only the 6 listed customers are read and the 4 that changed are written; no Sync Log row for a partial run',
    chained.output.mode === 'customers' && chained.output.customersChecked === 6 && chained.output.customersUpdated === 4 &&
    isDeepStrictEqual(writes(chained), { Customers: 4 }) && sim.count('Sync Log') === logsBefore && chained.queries.length === 2,
    `${chained.output.customersChecked} checked, ${chained.output.customersUpdated} updated, ${chained.queries.length} queries`);
  const quietDay = await run(sim, 'dedupe');
  const noneGiven = await run(sim, 'stats', { ...THRESHOLDS, customerRecordIds: quietDay.output.affectedCustomerIds });
  const blankText = await run(sim, 'stats', { ...THRESHOLDS, customerRecordIds: '' });
  report.check(S2, 'on a day with nothing to merge, script 1 outputs an empty list and script 2 recalculates nobody (no query, no write, a note says why)',
    isDeepStrictEqual(quietDay.output.affectedCustomerIds, []) && [noneGiven, blankText].every((x) => x.status === 'success' && x.output.mode === 'customers' &&
      x.output.customersChecked === 0 && x.queries.length === 0 && x.mutations.length === 0 &&
      isDeepStrictEqual(x.output.notes, ['customerRecordIds is empty (no customer changed in the step before), so there was nothing to recalculate.'])),
    `${noneGiven.output.mode}, ${noneGiven.queries.length} queries, ${noneGiven.mutations.length} writes`);

  // One changed order (automation trigger A).
  const S3 = 'Customer stats for one changed order (trigger on Orders)';
  sim = fresh();
  const cid = (r) => sim.ref('Customers', r);
  const oid = (r) => sim.ref('Orders', r);
  const derek = () => sim.fields('Customers', cid('derek'));
  await run(sim, 'stats', THRESHOLDS);
  sim.update('Orders', oid('o1016'), { 'Order Status': 'Cancelled' });
  const one = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: oid('o1016') });
  report.check(S3, 'cancelling #1016 drops derek to 59.40 over 2 orders, Silver; only derek is written, 3 queries, no Sync Log row',
    one.status === 'success' && derek()['Lifetime Value'] === 59.4 && derek()['Order Count'] === 2 && derek()['Average Order Value'] === 29.7 &&
    derek()['VIP Tier'] === 'Silver' && isDeepStrictEqual(writes(one), { Customers: 1 }) && one.queries.length === 3 && one.output.mode === 'order',
    `${derek()['Lifetime Value']} ${derek()['VIP Tier']}, ${one.queries.length} queries`);
  sim.update('Orders', oid('o1021'), { 'Refunded Amount': 9.95 });
  const refund = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: oid('o1021') });
  report.check(S3, 'a 9.95 refund on #1021 gives 49.45, average 24.73 (24.725 rounded up)',
    refund.status === 'success' && derek()['Lifetime Value'] === 49.45 && derek()['Average Order Value'] === 24.73, `${derek()['Lifetime Value']} / ${derek()['Average Order Value']}`);
  sim.update('Orders', oid('o1005'), { 'Customer': [cid('elena')] });
  const moved = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: oid('o1005') });
  const derekStale = derek()['Lifetime Value'];
  const nightly = await run(sim, 'stats', THRESHOLDS);
  report.check(S3, 'an order moved to elena updates elena (60.40, Silver); derek is fixed by the nightly full run, as documented',
    moved.status === 'success' && sim.fields('Customers', cid('elena'))['Lifetime Value'] === 60.4 && sim.fields('Customers', cid('elena'))['VIP Tier'] === 'Silver' &&
    derekStale === 49.45 && nightly.status === 'success' && derek()['Lifetime Value'] === 20 && derek()['Order Count'] === 1 && nightly.output.customersUpdated === 1,
    `derek ${derekStale} after the order run, ${derek()['Lifetime Value']} after the nightly run`);

  // Bad input variables.
  const S4 = 'Customer stats: input variables';
  sim = fresh();
  const snap2 = state(sim, ['Sync Log']);
  const none = await run(sim, 'stats', {});
  const flog = syncRow(sim, 'customer-stats-');
  report.check(S4, 'no thresholds: the run fails with a clear message, writes no customer and logs a Failed row',
    none.status === 'failed' && none.error.startsWith('Set the input variable silverThreshold to a lifetime value in dollars') && state(sim, ['Sync Log']) === snap2 &&
    flog.length === 1 && flog[0].Status === 'Failed' && flog[0].Errors === 1 && flog[0].Details === `Customer stats failed: ${none.error}`, none.error);
  const letters = await run(sim, 'stats', { ...THRESHOLDS, goldThreshold: 'lots' });
  const down = await run(sim, 'stats', { silverThreshold: '75', goldThreshold: '40', platinumThreshold: '150' });
  const badId = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: 'order 1016' });
  report.check(S4, 'text in a threshold, thresholds that go down and a malformed record id all fail before anything is written',
    letters.status === 'failed' && letters.error.includes('goldThreshold') && down.status === 'failed' && down.error.startsWith('The thresholds must go up') &&
    badId.status === 'failed' && badId.error.startsWith('The input variable orderRecordId must be an Airtable record id') && state(sim, ['Sync Log']) === snap2,
    down.error);
  const edge = await run(sim, 'stats', { silverThreshold: '44.20', goldThreshold: '80.35', platinumThreshold: '$1,000' });
  const e = (ref) => sim.fields('Customers', sim.ref('Customers', ref))['VIP Tier'];
  report.check(S4, 'a value equal to a threshold reaches the tier (marcus 44.20 Silver, derek 80.35 Gold), "$1,000" is read as 1000',
    edge.status === 'success' && e('marcus') === 'Silver' && e('derek') === 'Gold' && e('jordan') === 'Standard' && edge.output.tiers.Platinum === 0,
    `marcus ${e('marcus')}, derek ${e('derek')}, jordan ${e('jordan')}`);

  sim = fresh();
  const penny = sim.insert('Customers', { 'Name': 'Penny Counter', 'Email': 'penny.counter@example.com', 'Created Via': 'Manual', 'Record Status': 'Active' });
  sim.insert('Orders', { 'Order Number': '#1031', 'Customer': [penny], 'Email': 'penny.counter@example.com', 'Order Status': 'Closed', 'Total': 1.1 });
  sim.insert('Orders', { 'Order Number': '#1032', 'Customer': [penny], 'Email': 'penny.counter@example.com', 'Order Status': 'Closed', 'Total': 2.2 });
  sim.insert('Orders', { 'Order Number': '#1033', 'Customer': [penny], 'Email': 'penny.counter@example.com', 'Order Status': 'Closed', 'Total': 10, 'Refunded Amount': 9.95 });
  const cents = await run(sim, 'stats', { ...THRESHOLDS, customerRecordIds: penny });
  const p = sim.fields('Customers', penny);
  report.check(S4, 'money is added in whole cents: 1.10 + 2.20 + (10.00 - 9.95) is exactly 3.35, average 1.12 (in floating point dollars it is 3.350000000000001)',
    cents.status === 'success' && p['Lifetime Value'] === 3.35 && p['Order Count'] === 3 && p['Average Order Value'] === 1.12 && cents.warnings.length === 0 &&
    1.1 + 2.2 + (10 - 9.95) !== 3.35, `${p['Lifetime Value']} / ${p['Average Order Value']}, one id given as text`);

  sim = fresh();
  const missing = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: 'recAAAAAAAAAAAAAA' });
  const noCustomer = sim.insert('Orders', { 'Order Number': '#1030', 'Order Status': 'Open', 'Total': 15 });
  const lonely = await run(sim, 'stats', { ...THRESHOLDS, orderRecordId: noCustomer });
  report.check(S4, 'an order that no longer exists falls back to every customer; an order without a customer writes nothing',
    missing.status === 'success' && missing.output.mode === 'all' && missing.output.customersUpdated === 31 &&
    missing.output.notes[0] === 'Order recAAAAAAAAAAAAAA was not found (it may have been deleted), so every customer was recalculated.' &&
    lonely.status === 'success' && lonely.mutations.length === 0 && lonely.output.notes[0] === 'Order #1030 has no customer linked, so there was nothing to recalculate.',
    lonely.output.notes[0]);
}

// ====================================================================== 4. order line linker

async function linkOrderLines() {
  const S = 'Link order lines to products on the seed';
  let sim = fresh();
  const line = (ref) => sim.fields('Order Lines', sim.ref('Order Lines', ref));
  const prod = (ref) => sim.ref('Products', ref);
  const giftBefore = line('l1008-gift');
  const orphanBefore = line('l-orphan');
  const skusBefore = sim.rows('Order Lines').map((x) => [x.id, x.fields.SKU]);
  const r = await run(sim, 'link');
  report.check(S, 'the imported #1004 lines are linked: exact SKU, and " dc-bar-milk-caramel" (matched without spaces and in capitals; the SKU text stays as imported)',
    r.status === 'success' && isDeepStrictEqual(line('l1004-1').Product, [prod('dss')]) && line('l1004-1')['SKU Status'] === 'Matched' &&
    isDeepStrictEqual(line('l1004-2').Product, [prod('msc')]) && line('l1004-2')['SKU Status'] === 'Matched' && line('l1004-2').SKU === ' dc-bar-milk-caramel' &&
    isDeepStrictEqual(sim.rows('Order Lines').map((x) => [x.id, x.fields.SKU]), skusBefore),
    status(r));
  const productLines = (ref) => sim.fields('Products', prod(ref))['Order Lines'] || [];
  report.check(S, 'the products see the new lines too (the other side of the link)',
    productLines('dss').includes(sim.ref('Order Lines', 'l1004-1')) && productLines('msc').includes(sim.ref('Order Lines', 'l1004-2')),
    `${productLines('dss').length} lines on DC-BAR-DARK-SEASALT`);
  const o1011 = sim.byPrimary('Orders', '#1011').fields;
  report.check(S, 'the unknown SKU DC-BAR-GINGER stays unlinked and flags order #1011 for review',
    line('l1011-3').Product === undefined && line('l1011-3')['SKU Status'] === 'Unknown SKU' && o1011['Needs Review'] === true &&
    o1011['Review Reason'] === 'Unknown SKU DC-BAR-GINGER on line 14100000101103.' &&
    isDeepStrictEqual(r.output.unknownSkus, [{ line: '14100000101103', lineId: sim.ref('Order Lines', 'l1011-3'), sku: 'DC-BAR-GINGER', orders: ['#1011'] }]),
    o1011['Review Reason']);
  report.check(S, 'the gift wrap line (no SKU) and the orphan line are left exactly as they were',
    isDeepStrictEqual(line('l1008-gift'), giftBefore) && isDeepStrictEqual(line('l-orphan'), orphanBefore) && r.output.missingSku === 1, `missingSku ${r.output.missingSku}`);
  report.check(S, 'writes 2 lines and 1 order; Sync Log says Partial because an unknown SKU is left',
    isDeepStrictEqual(r.mutations, [
      { method: 'updateRecordsAsync', table: 'Order Lines', count: 2 }, { method: 'updateRecordsAsync', table: 'Orders', count: 1 },
      { method: 'createRecordAsync', table: 'Sync Log', count: 1 },
    ]) && r.output.matched === 48 && syncRow(sim, 'order-line-skus-')[0].Status === 'Partial' && syncRow(sim, 'order-line-skus-')[0]['Items Changed'] === 3,
    r.output.summary);

  const snap = state(sim, ['Sync Log']);
  const again = await run(sim, 'link');
  report.check('Link order lines, run again', 'nothing written but the Sync Log row, and #1011 does not get the reason twice',
    again.status === 'success' && state(sim, ['Sync Log']) === snap && isDeepStrictEqual(writes(again), { 'Sync Log': 1 }), again.output.summary);

  const S2 = 'Link order lines: one new or changed line (trigger on Order Lines)';
  const logs = sim.count('Sync Log');
  const added = sim.insert('Order Lines', { 'Line Item ID': '14100000102601', 'Order': [sim.ref('Orders', 'o1024')], 'SKU': 'dc-bar-dark-85 ', 'Quantity': 1, 'Unit Price': 8 });
  const one = await run(sim, 'link', { orderLineRecordId: added });
  report.check(S2, 'a new line with "dc-bar-dark-85 " is linked to DC-BAR-DARK-85, from 2 queries and 1 write, no Sync Log row',
    one.status === 'success' && isDeepStrictEqual(sim.fields('Order Lines', added).Product, [prod('d85')]) && sim.fields('Order Lines', added).SKU === 'dc-bar-dark-85 ' &&
    isDeepStrictEqual(sim.fields('Order Lines', added), { 'Line Item ID': '14100000102601', 'Order': [sim.ref('Orders', 'o1024')], 'Product': [prod('d85')],
      'SKU': 'dc-bar-dark-85 ', 'Quantity': 1, 'Unit Price': 8, 'SKU Status': 'Matched' }) &&
    sim.fields('Order Lines', added)['SKU Status'] === 'Matched' && one.queries.length === 2 && isDeepStrictEqual(writes(one), { 'Order Lines': 1 }) && sim.count('Sync Log') === logs,
    `${one.queries.length} queries, ${JSON.stringify(writes(one))}`);
  const l1001 = sim.ref('Order Lines', 'l1001-1');
  sim.update('Order Lines', l1001, { 'SKU': 'DC-BAR-MILK-CLASSIC' });
  const relink = await run(sim, 'link', { orderLineRecordId: l1001 });
  report.check(S2, 'a line whose SKU was changed moves from the old product to the new one, on both sides of the link',
    relink.status === 'success' && isDeepStrictEqual(sim.fields('Order Lines', l1001).Product, [prod('mcl')]) &&
    !productLines('dss').includes(l1001) && productLines('mcl').includes(l1001), `now ${sim.fields('Order Lines', l1001).Product}`);
  const l1019 = sim.ref('Order Lines', 'l1019-1');
  const reasonBefore = sim.byPrimary('Orders', '#1019').fields['Review Reason'];
  sim.update('Order Lines', l1019, { 'SKU': 'DC-BAR-NOPE' });
  const unknownOne = await run(sim, 'link', { orderLineRecordId: l1019 });
  const o1019 = sim.byPrimary('Orders', '#1019').fields;
  report.check(S2, 'an unknown SKU keeps the link a person made and adds a line to the order\'s existing Review Reason',
    unknownOne.status === 'success' && isDeepStrictEqual(sim.fields('Order Lines', l1019).Product, [prod('psb')]) && sim.fields('Order Lines', l1019)['SKU Status'] === 'Unknown SKU' &&
    o1019['Review Reason'] === `${reasonBefore}\nUnknown SKU DC-BAR-NOPE on line 14100000101901.` && o1019['Needs Review'] === true,
    o1019['Review Reason'].split('\n').slice(-1)[0]);
  const orphan = sim.insert('Order Lines', { 'Line Item ID': 'LEGACY-0002', 'SKU': 'DC-BAR-NOPE-2', 'Quantity': 1 });
  const noOrder = await run(sim, 'link', { orderLineRecordId: orphan });
  report.check(S2, 'an unknown SKU on a line without an order is marked and noted; no order is written',
    noOrder.status === 'success' && sim.fields('Order Lines', orphan)['SKU Status'] === 'Unknown SKU' && isDeepStrictEqual(writes(noOrder), { 'Order Lines': 1 }) &&
    noOrder.output.notes[0] === 'Line LEGACY-0002 has the unknown SKU DC-BAR-NOPE-2 and no order, so no order was flagged.', noOrder.output.notes[0]);

  const S3 = 'Link order lines: a SKU shared by two products';
  sim = fresh();
  const copy = sim.insert('Products', { 'SKU': 'DC-BAR-DARK-85', 'Name': 'Dark 85% Single Origin Bar (copy)', 'Status': 'Draft', 'Category': 'Bar' });
  const before85 = ['l1002-2', 'l1013-1', 'l1021-2'].map((ref) => sim.fields('Order Lines', sim.ref('Order Lines', ref)));
  const amb = await run(sim, 'link');
  report.check(S3, 'the 3 lines with that SKU are left alone and listed, the rest is linked as usual',
    amb.status === 'success' && isDeepStrictEqual(sorted(amb.output.ambiguous.map((a) => a.line)), ['14100000100202', '14100000101301', '14100000102102']) &&
    isDeepStrictEqual(sorted(amb.output.ambiguous[0].products), sorted([`Dark 85% Single Origin Bar (copy) (${copy})`, `Dark 85% Single Origin Bar (${sim.ref('Products', 'd85')})`])) &&
    isDeepStrictEqual(['l1002-2', 'l1013-1', 'l1021-2'].map((ref) => sim.fields('Order Lines', sim.ref('Order Lines', ref))), before85) &&
    isDeepStrictEqual(writes(amb), { 'Order Lines': 2, Orders: 1, 'Sync Log': 1 }), `${amb.output.ambiguous.length} ambiguous`);
}

// ====================================================================== 5. data quality over time

async function dataQualityOverTime() {
  const S = 'Data quality after the cleanup scripts ran';
  const sim = fresh();
  const cid = (r) => sim.ref('Customers', r);
  await run(sim, 'dq');
  await run(sim, 'dedupe');
  await run(sim, 'link');
  const r = await run(sim, 'dq');
  const dupRows = ['maya-dup', 'jordan-dup', 'priya-dup'].map((ref) => sim.byPrimary('Data Quality', `2026-W43 / Duplicate Customer / ${cid(ref)}`).fields);
  report.check(S, 'the 3 duplicate findings are set to Fixed with a dated note; nothing new is created',
    r.status === 'success' && dupRows.every((f) => f.Status === 'Fixed' && f.Details.endsWith('\nNot found in the 2026-W43 check on 2026-10-20, so it was marked Fixed.')) &&
    r.output.fixed === 3 && r.output.newFindings === 0 && r.output.stillOpen === 7 &&
    isDeepStrictEqual(r.mutations.map((m) => [m.method, m.table, m.count]), [['updateRecordsAsync', 'Data Quality', 3], ['createRecordAsync', 'Sync Log', 1]]),
    r.output.summary);

  const S2 = 'Data quality a week later (2026-W44)';
  sim.advanceDays(7);
  sim.update('Customers', cid('sam'), { 'Email': 'sam.ortiz@example.com' });
  const zoe = sim.insert('Customers', { 'Name': 'Zoe Park', 'Email': 'zoe.park@example', 'Created Via': 'Manual', 'Record Status': 'Active' });
  const row = (ref, check) => sim.byPrimary('Data Quality', `2026-W43 / ${check} / ${cid(ref)}`);
  sim.update('Data Quality', row('kai', 'Missing Required Field').id, { 'Status': 'Ignored' });
  sim.update('Data Quality', row('lena', 'Invalid Email').id, { 'Status': 'Fixed' });
  const kaiBefore = row('kai', 'Missing Required Field').fields;
  const legacyBefore = sim.byPrimary('Data Quality', '2026-W42 / Invalid Email / sam').fields;
  const w44 = await run(sim, 'dq');
  const newIds = sim.rows('Data Quality').filter((x) => x.fields['Report Week'] === '2026-W44').map((x) => x.fields['Finding ID']).sort();
  report.check(S2, 'only the new problem (zoe) and the one closed by hand that is still broken (lena) get 2026-W44 rows',
    w44.status === 'success' && w44.output.reportWeek === '2026-W44' && isDeepStrictEqual(newIds, [`2026-W44 / Invalid Email / ${cid('lena')}`, `2026-W44 / Invalid Email / ${zoe}`].sort()) &&
    w44.output.newFindings === 2, newIds.map((x) => x.split(' / ')[0]).join(', '));
  report.check(S2, 'sam\'s fixed email closes his finding; kai\'s Ignored finding and the hand written 2026-W42 row are not touched',
    row('sam', 'Invalid Email').fields.Status === 'Fixed' && w44.output.fixed === 1 && isDeepStrictEqual(row('kai', 'Missing Required Field').fields, kaiBefore) &&
    isDeepStrictEqual(sim.byPrimary('Data Quality', '2026-W42 / Invalid Email / sam').fields, legacyBefore),
    `sam ${row('sam', 'Invalid Email').fields.Status}, kai ${row('kai', 'Missing Required Field').fields.Status}`);
  report.check(S2, 'problems still listed from last week (grace, kai, tom-n, the orphan line, the unknown SKU) get no second row',
    w44.output.stillOpen === 5 && sim.rows('Data Quality').filter((x) => x.fields['Report Week'] === '2026-W44').length === 2,
    `still open ${w44.output.stillOpen} (grace, kai ignored, tom-n, orphan, unknown SKU)`);
}

// ====================================================================== 6. scale

async function scale() {
  const S = 'Scale: more records than one batch or one query';
  const sim = fresh();
  for (let i = 1; i <= 120; i += 1) {
    const n = String(i).padStart(3, '0');
    const email = `bulk.${n}@example.com`;
    const a = sim.insert('Customers', { 'Name': `Bulk Buyer ${n}`, 'Email': email, 'Shopify Customer ID': `7700000${String(10000 + i)}`, 'Created Via': 'Shopify', 'Record Status': 'Active' });
    const b = sim.insert('Customers', { 'Name': `Bulk Buyer ${n}`, 'Email': email.toUpperCase(), 'Created Via': 'Import', 'Record Status': 'Active' });
    sim.insert('Orders', { 'Order Number': `#B${n}A`, 'Customer': [a], 'Email': email, 'Order Status': 'Closed', 'Total': 10 });
    sim.insert('Orders', { 'Order Number': `#B${n}B`, 'Customer': [b], 'Email': email, 'Order Status': 'Closed', 'Total': 10 });
    sim.insert('Order Lines', { 'Line Item ID': `BULK-LINE-${n}`, 'SKU': 'DC-BAR-DARK-SEASALT', 'Quantity': 1, 'Unit Price': 7.5 });
  }
  for (let i = 1; i <= 60; i += 1) sim.insert('Customers', { 'Name': `Broken Email ${i}`, 'Email': `broken.${i}@example`, 'Created Via': 'Import', 'Record Status': 'Active' });

  const dq = await run(sim, 'dq');
  const dqCreates = dq.mutations.filter((m) => m.table === 'Data Quality').map((m) => m.count);
  report.check(S, '310 findings are created in 7 calls of at most 50',
    dq.status === 'success' && dq.output.newFindings === 310 && isDeepStrictEqual(dqCreates, [50, 50, 50, 50, 50, 50, 10]), `${dq.output.newFindings} in [${dqCreates}]`);

  const dd = await run(sim, 'dedupe');
  const orderQueries = dd.queries.filter((q) => q.table === 'Orders').map((q) => q.recordIds);
  report.check(S, '123 merges move 122 orders: 122 ids read in 2 queries (100 + 22), written in 3 calls, 246 customers in 5 calls',
    dd.status === 'success' && dd.output.duplicatesMerged === 123 && dd.output.ordersMoved === 122 && isDeepStrictEqual(orderQueries, [100, 22]) &&
    isDeepStrictEqual(dd.mutations.filter((m) => m.table === 'Orders').map((m) => m.count), [50, 50, 22]) &&
    isDeepStrictEqual(dd.mutations.filter((m) => m.table === 'Customers').map((m) => m.count), [50, 50, 50, 50, 46]),
    `${dd.output.duplicatesMerged} merged, order queries [${orderQueries}]`);

  const st = await run(sim, 'stats', THRESHOLDS);
  const kept = sim.rows('Customers').filter((c) => c.fields.Name && c.fields.Name.startsWith('Bulk Buyer') && c.fields['Record Status'] === 'Active');
  report.check(S, '331 customers recalculated in 7 calls; every kept bulk buyer has both orders (20.00, 2 orders)',
    st.status === 'success' && st.output.customersUpdated === 331 && st.mutations.filter((m) => m.table === 'Customers').length === 7 &&
    kept.length === 120 && kept.every((c) => c.fields['Lifetime Value'] === 20 && c.fields['Order Count'] === 2 && c.fields['Average Order Value'] === 10),
    `${st.output.customersUpdated} updated, ${kept.length} kept buyers`);

  const ln = await run(sim, 'link');
  report.check(S, 'the 120 unlinked bulk lines are linked in 3 calls',
    ln.status === 'success' && ln.output.linesUpdated === 122 && isDeepStrictEqual(ln.mutations.filter((m) => m.table === 'Order Lines').map((m) => m.count), [50, 50, 22]),
    `${ln.output.linesUpdated} lines updated`);

  const runs = [dq, dd, st, ln];
  report.check(S, 'no run comes near the limits: at most 30 queries and well under 30 seconds',
    runs.every((x) => x.queries.length <= LIMITS.queriesPerRun && x.durationMs < 10000),
    runs.map((x) => `${x.label.slice(0, 2)} ${x.queries.length}q ${x.durationMs}ms`).join(', '));
}

// ====================================================================== 7. the real clock

async function liveClock() {
  const S = 'With the real clock (seed dates moved to today)';
  const sim = fresh(null);
  const legacy = sim.byPrimary('Data Quality', '2026-W42 / Invalid Email / sam').fields;
  const before = Date.now();
  const r = await run(sim, 'dq');
  const after = Date.now();
  const sam = sim.byPrimary('Data Quality', `${isoWeekOf(before)} / Invalid Email / ${sim.ref('Customers', 'sam')}`);
  const found = sam ? Date.parse(sam.fields['Found At']) : NaN;
  report.check(S, 'the report week is today\'s ISO week and Found At is the time of the run',
    r.status === 'success' && r.output.reportWeek === isoWeekOf(before) && r.output.newFindings === 10 && found >= before - 1000 && found <= after,
    `${r.output.reportWeek}, seed moved ${sim.seedShiftDays} days`);
  const shifted = Date.parse('2026-10-13T07:00:00.000Z') + sim.seedShiftDays * 86400000;
  report.check(S, 'the seed\'s dates moved with the clock (last week\'s finding is still a week old)',
    Date.parse(legacy['Found At']) === shifted, legacy['Found At']);

  const edge = fresh('2027-01-01T07:00:00Z');
  const y = await run(edge, 'dq', { dryRun: 'true' });
  report.check(S, 'ISO week at the turn of the year: Friday 2027-01-01 is 2026-W53',
    y.status === 'success' && y.output.reportWeek === '2026-W53' && isoWeekOf(Date.parse('2027-01-01T07:00:00Z')) === '2026-W53', y.output.reportWeek);
}

// ====================================================================== 8. a base that differs

async function differentBase() {
  const S = 'A base that differs from the blueprint';
  let sim = fresh();
  sim.renameField('Customers', 'Merged Into', 'Merged To');
  const snap = state(sim, ['Sync Log']);
  const r = await run(sim, 'dedupe');
  const log = syncRow(sim, 'customer-dedupe-');
  report.check(S, 'a renamed field stops the script before it writes anything, with a message that names the field, and a Failed Sync Log row',
    r.status === 'failed' && r.error === 'The Customers table has no field called "Merged Into". Rename the field in Airtable or change the name in this script.' &&
    state(sim, ['Sync Log']) === snap && log.length === 1 && log[0].Status === 'Failed' && log[0].Details === `Customer dedupe failed: ${r.error}`, r.error);

  sim = fresh();
  sim.renameTable('Data Quality', 'Findings');
  const t = await run(sim, 'dq');
  report.check(S, 'a renamed table is reported by name',
    t.status === 'failed' && t.error === 'This base has no table called "Data Quality". Rename the table in Airtable or change the name in this script.', t.error);

  sim = fresh();
  sim.renameTable('Sync Log', 'Run Log');
  const n = await run(sim, 'stats', {});
  report.check(S, 'with no Sync Log table the run still fails with the real reason, and the log problem goes to the console',
    n.status === 'failed' && n.error.startsWith('Set the input variable silverThreshold') &&
    n.logs.length === 1 && n.logs[0] === 'log: Could not add the Failed row to Sync Log: This base has no table called "Sync Log". Rename the table in Airtable or change the name in this script.',
    n.logs[0]);

  const S2 = 'Customer stats: thresholds kept in the script';
  sim = fresh();
  const source = fs.readFileSync(SCRIPTS.stats, 'utf8');
  const code = source.replace('const FALLBACK_THRESHOLDS = null;', 'const FALLBACK_THRESHOLDS = { silver: 40, gold: 75, platinum: 150 };');
  const a = await sim.runScript({ code }, { label: '02-customer-stats.js', inputs: {} });
  const b = await sim.runScript({ code }, { label: '02-customer-stats.js', inputs: { goldThreshold: '50' } });
  SCRIPT_RUNS.push(a, b);
  report.check(S2, 'FALLBACK_THRESHOLDS works without input variables, and an input variable that is set wins over it',
    code !== source && a.status === 'success' && isDeepStrictEqual(a.output.tiers, { Standard: 25, Silver: 5, Gold: 1, Platinum: 0 }) &&
    b.status === 'success' && isDeepStrictEqual(b.output.tiers, { Standard: 25, Silver: 4, Gold: 2, Platinum: 0 }) &&
    sim.fields('Customers', sim.ref('Customers', 'victor'))['VIP Tier'] === 'Gold', `${JSON.stringify(a.output.tiers)} then ${JSON.stringify(b.output.tiers)}`);
}

// ====================================================================== 9. found in review

async function dedupeMessyKeptRecord() {
  const S = 'Merge duplicate customers: a kept record with a messy email and no name';
  const sim = fresh();
  const id = (r) => sim.ref('Customers', r);
  const dupBefore = sim.fields('Customers', id('maya-dup'));
  sim.update('Customers', id('maya'), { 'Email': ' Maya.Chen@EXAMPLE.com', 'Name': null });
  const r = await run(sim, 'dedupe');
  const maya = sim.fields('Customers', id('maya'));
  const dup = sim.fields('Customers', id('maya-dup'));
  const merge = (r.output.merges || []).find((m) => m.email === 'maya.chen@example.com');
  report.check(S, 'the Shopify record is still kept, gets the clean email and the duplicate\'s name, and the notes and output call it "Maya Chen", not "Unnamed record"',
    r.status === 'success' && maya.Email === 'maya.chen@example.com' && maya.Name === 'Maya Chen' && merge && merge.kept === `Maya Chen (${id('maya')})` &&
    maya.Notes === `2026-10-20: merged 1 duplicate record with the same email: Maya Chen (${id('maya-dup')}).` &&
    dup.Notes === `2026-10-20: duplicate of Maya Chen (${id('maya')}), merged by the dedupe script. Moved: #1004.` && dup.Email === dupBefore.Email,
    merge ? merge.kept : status(r));
}

async function orderFlag() {
  const S = 'Link order lines: the order flag, a person, and runs at the same moment';
  let sim = fresh();
  const o1011 = () => sim.byPrimary('Orders', '#1011');
  await run(sim, 'link');
  sim.update('Orders', o1011().id, { 'Needs Review': false });
  const reasonKept = o1011().fields['Review Reason'];
  const sweep = await run(sim, 'link');
  const lineRun = await run(sim, 'link', { orderLineRecordId: sim.ref('Order Lines', 'l1011-3') });
  report.check(S, 'a person who unticks Needs Review on #1011 is not overruled: the next sweep and a run for the same line leave the order alone',
    sweep.status === 'success' && lineRun.status === 'success' && o1011().fields['Needs Review'] === undefined && o1011().fields['Review Reason'] === reasonKept &&
    isDeepStrictEqual(writes(sweep), { 'Sync Log': 1 }) && isDeepStrictEqual(sweep.output.ordersFlagged, []) && lineRun.mutations.length === 0 && lineRun.queries.length === 4,
    `Needs Review ${o1011().fields['Needs Review']}, sweep wrote ${JSON.stringify(writes(sweep))}`);

  const extra = sim.insert('Order Lines', { 'Line Item ID': '14100000101104', 'Order': [o1011().id], 'SKU': 'DC-BAR-NOPE-C', 'Quantity': 1 });
  const newLine = await run(sim, 'link', { orderLineRecordId: extra });
  report.check(S, 'a second unknown SKU on that order flags it again and adds only the new line',
    newLine.status === 'success' && o1011().fields['Needs Review'] === true &&
    o1011().fields['Review Reason'] === `${reasonKept}\nUnknown SKU DC-BAR-NOPE-C on line 14100000101104.` &&
    isDeepStrictEqual(newLine.mutations.map((m) => [m.table, m.count]), [['Order Lines', 1], ['Orders', 1]]),
    JSON.stringify(o1011().fields['Review Reason']));

  sim = fresh();
  const o24 = sim.ref('Orders', 'o1024');
  const before24 = sim.fields('Orders', o24);
  const a = sim.insert('Order Lines', { 'Line Item ID': '14100000102491', 'Order': [o24], 'SKU': 'DC-BAR-NOPE-A', 'Quantity': 1 });
  const b = sim.insert('Order Lines', { 'Line Item ID': '14100000102492', 'Order': [o24], 'SKU': 'DC-BAR-NOPE-B', 'Quantity': 1 });
  const [ra, rb] = await Promise.all([run(sim, 'link', { orderLineRecordId: a }), run(sim, 'link', { orderLineRecordId: b })]);
  const both = 'Unknown SKU DC-BAR-NOPE-A on line 14100000102491.\nUnknown SKU DC-BAR-NOPE-B on line 14100000102492.';
  const after24 = sim.fields('Orders', o24);
  const sweep24 = await run(sim, 'link');
  report.check(S, 'two new lines of one order checked at the same moment (Airtable starts one run per line): #1024 ends with both reasons, and a later sweep adds nothing',
    before24['Review Reason'] === undefined && ra.status === 'success' && rb.status === 'success' && after24['Review Reason'] === both && after24['Needs Review'] === true &&
    ra.queries.length === 4 && rb.queries.length === 4 && sweep24.status === 'success' && !sweep24.output.ordersFlagged.includes('#1024') &&
    sim.fields('Orders', o24)['Review Reason'] === both,
    JSON.stringify(after24['Review Reason']));

  sim = fresh();
  await run(sim, 'link');
  const reason1011 = o1011().fields['Review Reason'];
  const ginger = sim.insert('Products', { 'SKU': 'DC-BAR-GINGER', 'Name': 'Dark Ginger Bar', 'Status': 'Draft', 'Category': 'Bar' });
  const gl = sim.ref('Order Lines', 'l1011-3');
  const known = await run(sim, 'link', { orderLineRecordId: gl });
  report.check(S, 'once the product exists the line is linked and Matched; the order keeps its flag and reason for the person to clear',
    known.status === 'success' && isDeepStrictEqual(sim.fields('Order Lines', gl).Product, [ginger]) && sim.fields('Order Lines', gl)['SKU Status'] === 'Matched' &&
    o1011().fields['Needs Review'] === true && o1011().fields['Review Reason'] === reason1011 && isDeepStrictEqual(writes(known), { 'Order Lines': 1 }),
    JSON.stringify(writes(known)));

  const messy = sim.ref('Order Lines', 'l1004-2');
  const messyRun = await run(sim, 'link', { orderLineRecordId: messy });
  report.check(S, 'the SKU text is never rewritten, so the Shopify sync and this script never undo each other: a run for the linked " dc-bar-milk-caramel" line writes nothing',
    messyRun.status === 'success' && messyRun.mutations.length === 0 && sim.fields('Order Lines', messy).SKU === ' dc-bar-milk-caramel' &&
    sim.fields('Order Lines', messy)['SKU Status'] === 'Matched', `${messyRun.mutations.length} writes`);
}

async function checkedBeforeWriting() {
  const S = 'Checks before the first write';
  const noSyncLog = 'This base has no table called "Sync Log". Rename the table in Airtable or change the name in this script.';
  const outcomes = [];
  for (const [key, inputs] of [['dedupe', {}], ['stats', THRESHOLDS], ['link', {}], ['dq', {}]]) {
    const sim = fresh();
    sim.renameTable('Sync Log', 'Run Log');
    const snap = state(sim);
    const r = await run(sim, key, inputs);
    outcomes.push([key, r.status === 'failed' && r.error === noSyncLog && state(sim) === snap && r.mutations.length === 0 && r.queries.length === 0]);
  }
  report.check(S, 'without a Sync Log table all four scripts stop before their first read or write and name the table (before this check, script 2 wrote 31 customers first)',
    outcomes.every((o) => o[1]), outcomes.map((o) => `${o[0]} ${o[1] ? 'ok' : 'WROTE OR WRONG'}`).join(', '));

  const option = (field, table, name) => `The ${field} field in the ${table} table has no option "${name}". Add the option in Airtable or change the name in this script.`;
  let sim = fresh();
  sim.renameChoice('Customers', 'Record Status', 'Duplicate', 'Merged');
  let snap = state(sim, ['Sync Log']);
  const d = await run(sim, 'dedupe');
  const dOk = d.status === 'failed' && d.error === option('Record Status', 'Customers', 'Duplicate') && state(sim, ['Sync Log']) === snap &&
    syncRow(sim, 'customer-dedupe-').length === 1 && syncRow(sim, 'customer-dedupe-')[0].Status === 'Failed';
  sim = fresh();
  sim.renameChoice('Sync Log', 'Status', 'Partial', 'Warning');
  snap = state(sim, ['Sync Log']);
  const l = await run(sim, 'link');
  const lOk = l.status === 'failed' && l.error === option('Status', 'Sync Log', 'Partial') && state(sim, ['Sync Log']) === snap &&
    syncRow(sim, 'order-line-skus-').length === 1 && syncRow(sim, 'order-line-skus-')[0].Status === 'Failed';
  sim = fresh();
  sim.renameChoice('Data Quality', 'Check', 'Unknown SKU', 'SKU Not Found');
  const findingsBefore = sim.count('Data Quality');
  const q = await run(sim, 'dq');
  const qOk = q.status === 'failed' && q.error === option('Check', 'Data Quality', 'Unknown SKU') && sim.count('Data Quality') === findingsBefore && q.queries.length === 0;
  report.check(S, 'a renamed select option (a script cannot add one) stops the run before it writes, naming the field and option; the Failed row still goes to Sync Log',
    dOk && lOk && qOk, `dedupe ${dOk}, link ${lOk}, data quality ${qOk}`);

  sim = fresh();
  sim.renameField('Subscribers', 'Email', 'E-mail');
  const s = await run(sim, 'dq');
  report.check(S, 'script 4 checks Subscribers up front too: a renamed Subscribers field gives the plain message before any read, and no finding is written',
    s.status === 'failed' && s.error === 'The Subscribers table has no field called "Email". Rename the field in Airtable or change the name in this script.' &&
    s.queries.length === 0 && isDeepStrictEqual(writes(s), { 'Sync Log': 1 }), s.error);
}

// ====================================================================== every run

function everyRun() {
  const S = 'Every script run above';
  const realRuns = SCRIPT_RUNS;
  const biggest = Math.max(...realRuns.flatMap((r) => r.mutations.map((m) => m.count)));
  report.check(S, `no write call above ${LIMITS.recordsPerWrite} records`, biggest <= LIMITS.recordsPerWrite, `largest ${biggest}, ${realRuns.length} runs`);
  const warned = realRuns.filter((r) => r.warnings.length);
  report.check(S, 'no warnings: every query names its fields, every write is awaited, money is rounded to the cent',
    warned.length === 0, warned.length ? warned[0].warnings[0] : `${realRuns.reduce((n, r) => n + r.queries.length, 0)} queries`);
  const outside = realRuns.filter((r) => Object.keys(r.writesByTable()).some((t) => !ALLOWED_WRITES[r.label].includes(t)));
  report.check(S, 'each script writes only to its own tables', outside.length === 0, outside.length ? `${outside[0].label} wrote ${Object.keys(outside[0].writesByTable())}` : 'checked');
  report.check(S, 'no script calls fetch, so no data leaves Airtable', realRuns.every((r) => r.fetchCalls === 0), 'fetch calls 0');
  const failedOnPurpose = realRuns.filter((r) => r.status === 'failed').map((r) => r.error);
  const intended = ['Set the input variable silverThreshold', 'Set the input variable goldThreshold', 'The thresholds must go up', 'The input variable orderRecordId must be',
    'The Customers table has no field called "Merged Into"', 'This base has no table called', 'The Record Status field in the Customers table',
    'The Status field in the Sync Log table', 'The Check field in the Data Quality table', 'The Subscribers table has no field called "Email"'];
  const unexpected = failedOnPurpose.filter((e) => !intended.some((p) => e.startsWith(p)));
  report.check(S, 'the only failed runs are the 15 that test bad input variables or a base that differs from the blueprint, each with its intended message',
    failedOnPurpose.length === 15 && unexpected.length === 0, unexpected.length ? `unexpected: ${unexpected[0]}` : `${failedOnPurpose.length} failed runs`);
}

async function main() {
  const started = Date.now();
  const scenarios = [emulatorStrictness, dataQualityFirstRun, dedupe, customerStats, linkOrderLines, dataQualityOverTime, scale, liveClock, differentBase,
    dedupeMessyKeptRecord, orderFlag, checkedBeforeWriting];
  for (const scenario of scenarios) {
    try {
      await scenario();
    } catch (error) {
      // A scenario that throws (for example because a script wrote nothing it could find) is a failure, not a crash.
      report.check(`Scenario ${scenario.name}`, 'ran to the end', false, error && error.message);
    }
  }
  everyRun();
  const ok = report.print();
  console.log(`(${((Date.now() - started) / 1000).toFixed(1)} s)`);
  process.exit(ok ? 0 : 1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
