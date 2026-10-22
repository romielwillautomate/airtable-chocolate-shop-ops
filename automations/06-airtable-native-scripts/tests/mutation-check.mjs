#!/usr/bin/env node
/**
 * Checks the tests themselves: breaks the scripts on purpose, one realistic mistake at a time, and
 * runs run-tests.mjs against each broken copy. Every mistake must make the suite fail. A mistake the
 * suite does not notice is reported as MISSED and the check exits with 1.
 *
 * The broken copies go to a temporary folder; the real scripts are never changed.
 * Usage: node automations/06-airtable-native-scripts/tests/mutation-check.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPTS = path.join(HERE, '..', 'scripts');
const RUNNER = path.join(HERE, 'run-tests.mjs');

const MUTANTS = [
  {
    script: '01-dedupe-customers.js',
    what: 'dedupe keeps whichever record comes first instead of ranking them',
    from: 'const ranked = members.slice().sort(compareKeep);',
    to: 'const ranked = members.slice();',
  },
  {
    script: '01-dedupe-customers.js',
    what: 'dedupe copies marketing consent from the duplicate to the kept record',
    from: "    if (!m.keep.phone && withPhone) keepFields['Phone'] = withPhone.phone;",
    to: "    if (!m.keep.phone && withPhone) keepFields['Phone'] = withPhone.phone;\n    keepFields['Accepts Marketing'] = true;",
  },
  {
    script: '02-customer-stats.js',
    what: 'stats adds money up in floating point dollars instead of whole cents',
    replace: [
      ['let net = toCents(total || 0) - toCents(refunded || 0);', 'let net = (total || 0) - (refunded || 0);'],
      ['return { cents: cents, ltv: cents / 100, count: count, aov: count ? Math.round(cents / count) / 100 : null };',
        'return { cents: Math.round(cents * 100), ltv: cents, count: count, aov: count ? Math.round((cents * 100) / count) / 100 : null };'],
    ],
  },
  {
    script: '02-customer-stats.js',
    what: 'a lifetime value exactly on the Gold threshold stays Silver',
    from: "if (cents >= t.gold) return 'Gold';",
    to: "if (cents > t.gold) return 'Gold';",
  },
  {
    script: '02-customer-stats.js',
    what: 'stats fires its writes in parallel instead of one batch at a time',
    from: '    await table.updateRecordsAsync(batch);',
    to: '    await Promise.all(batch.map(function (u) { return table.updateRecordAsync(u.id, u.fields); }));',
  },
  {
    script: '03-link-order-lines.js',
    what: 'the linker sends 100 records per write call',
    from: '  for (let i = 0; i < updates.length; i += 50) {\n    const batch = updates.slice(i, i + 50);',
    to: '  for (let i = 0; i < updates.length; i += 100) {\n    const batch = updates.slice(i, i + 100);',
  },
  {
    script: '03-link-order-lines.js',
    what: 'the linker compares SKUs without trimming and uppercasing',
    from: "return typeof v === 'string' ? v.replace(/\\s+/g, '').toUpperCase() : '';",
    to: "return typeof v === 'string' ? v : '';",
  },
  {
    script: '04-weekly-data-quality.js',
    what: 'the report forgets to look for an existing open finding, so reruns repeat work',
    from: '    if (activeByKey.has(key)) {',
    to: '    if (false) {',
  },
  {
    script: '04-weekly-data-quality.js',
    what: 'the report writes a single select as plain text',
    from: "'Severity': { name: SEVERITY[p.check] },",
    to: "'Severity': SEVERITY[p.check],",
  },
  {
    script: '04-weekly-data-quality.js',
    what: 'the ISO week is counted from Sunday instead of the Thursday rule',
    from: 'd.setUTCDate(d.getUTCDate() + 4 - day);',
    to: 'd.setUTCDate(d.getUTCDate() - day);',
  },
  // Added in review: each of these was a real flaw in the first version, or guards its fix.
  {
    script: '01-dedupe-customers.js',
    what: 'the notes name the kept record by its old (empty) name after a name was copied to it',
    from: "m.keepLabel = ((m.keep.name || (named ? named.name : '')) || 'Unnamed record') + ' (' + m.keep.id + ')';",
    to: 'm.keepLabel = describe(m.keep);',
  },
  {
    script: '01-dedupe-customers.js',
    what: 'dedupe does not check the Duplicate option before it moves orders',
    from: "  requireOptions(customersTable, 'Record Status', ['Duplicate']);\n",
    to: '',
  },
  {
    script: '02-customer-stats.js',
    what: 'stats finds out that Sync Log is missing only after it has written the customers',
    from: '  checkSyncLog();\n',
    to: '',
  },
  {
    script: '02-customer-stats.js',
    what: 'an empty customerRecordIds list from script 1 recalculates every customer',
    from: '  } else if (customerIdsGiven) {',
    to: '  } else if (customerIds.length) {',
  },
  {
    script: '03-link-order-lines.js',
    what: 'the linker writes the cleaned SKU back to the line, against the Shopify sync',
    from: "        if (status !== 'Matched') fields['SKU Status'] = { name: 'Matched' };",
    to: "        if (status !== 'Matched') fields['SKU Status'] = { name: 'Matched' };\n        if (rawSku !== product.getCellValue('SKU')) fields['SKU'] = product.getCellValue('SKU');",
  },
  {
    script: '03-link-order-lines.js',
    what: 'the linker ticks Needs Review again on every run, overruling a person who unticked it',
    from: "      if (!add.length) continue;\n      const fields = { 'Review Reason': add.reduce(appendLine, reason) };",
    to: "      if (!add.length && order.getCellValue('Needs Review')) continue;\n      const fields = add.length ? { 'Review Reason': add.reduce(appendLine, reason) } : {};",
  },
  {
    script: '03-link-order-lines.js',
    what: 'a one-line run works out the order\'s reasons from its own line only',
    from: '    if (lineId) {\n      const others',
    to: '    if (false) {\n      const others',
  },
  {
    script: '04-weekly-data-quality.js',
    what: 'the report does not check the Subscribers table before it starts',
    from: '  requireFields(subscribersTable, SUBSCRIBER_FIELDS);\n',
    to: '',
  },
];

function runSuite(dir) {
  const result = spawnSync(process.execPath, [RUNNER], { env: { ...process.env, SCRIPTS_DIR: dir }, encoding: 'utf8', timeout: 300000 });
  const out = `${result.stdout || ''}${result.stderr || ''}`;
  const failed = (out.match(/^\s+FAIL\s/gm) || []).length;
  const summary = (out.match(/(\d+)\/(\d+) checks passed/) || [])[0] || 'no summary (the runner crashed)';
  return { code: result.status, failed, summary };
}

function copyScripts(dir) {
  fs.mkdirSync(dir, { recursive: true });
  for (const name of fs.readdirSync(SCRIPTS)) fs.copyFileSync(path.join(SCRIPTS, name), path.join(dir, name));
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'airtable-06-mutants-'));
let missed = 0;
try {
  const baseline = path.join(root, 'baseline');
  copyScripts(baseline);
  const ok = runSuite(baseline);
  console.log(`${ok.code === 0 ? 'OK    ' : 'BROKEN'}  unchanged scripts  (${ok.summary})`);
  if (ok.code !== 0) process.exit(1);

  MUTANTS.forEach((m, i) => {
    const dir = path.join(root, `mutant-${i + 1}`);
    copyScripts(dir);
    const file = path.join(dir, m.script);
    let source = fs.readFileSync(file, 'utf8');
    for (const [from, to] of m.replace || [[m.from, m.to]]) {
      if (source.split(from).length !== 2) throw new Error(`mutant ${i + 1}: the text to replace is not in ${m.script} exactly once`);
      source = source.replace(from, to);
    }
    fs.writeFileSync(file, source);
    const r = runSuite(dir);
    const caught = r.code !== 0;
    if (!caught) missed += 1;
    console.log(`${caught ? 'CAUGHT' : 'MISSED'}  ${m.script}: ${m.what}  (${r.failed} failing checks, ${r.summary})`);
  });
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}
console.log(`\n${MUTANTS.length - missed}/${MUTANTS.length} deliberate mistakes caught`);
process.exit(missed ? 1 : 0);
