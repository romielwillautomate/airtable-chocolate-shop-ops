/*
 * Demo Chocolate Co. | Airtable automation script 4 of 4: weekly data quality report
 *
 * Paste this into an Airtable automation as a "Run a script" action.
 * Trigger: "At a scheduled time", every Monday at 07:00.
 * Optional next action: "Send an email" or "Slack: Send a message" with the reportText output.
 *
 * Input variables (left panel of the script editor, all optional):
 *   dryRun   true shows the report in the output and writes nothing
 *
 * The checks (records marked as Duplicate customers are history and are skipped):
 *   Missing Required Field  Customers without Name or Email, Orders without Customer or Email,
 *                           Products without Name, Status or Category
 *   Invalid Email           Customers and Orders whose Email, once trimmed, is not a usable address
 *   Orphan Order Line       Order Lines that are not linked to an order
 *   Duplicate Customer      Active customers that share an email (script 1 merges them)
 *   Unknown SKU             Order Lines whose SKU matches no product (script 3 flags the order)
 *
 * How findings are kept (the Data Quality table works as an issue list):
 *   - A new problem gets a row with Finding ID "<ISO week> / <check> / <record id>", for example
 *     "2026-W43 / Invalid Email / recXXXXXXXXXXXXXX", Status Open. The week is the week it was
 *     first found.
 *   - A problem that already has an Open or Ignored row gets no new row, so a rerun in the same
 *     week, or next week, never makes duplicates.
 *   - An Open row whose problem is gone is set to Fixed, with a line added to Details.
 *   - A problem that comes back after its row was set to Fixed gets a new row for this week.
 *   - Rows without a Record ID (older findings written by hand) are left as they are.
 *
 * Output: reportWeek, newFindings, stillOpen, fixed, reopened, legacyWithoutRecordId, byCheck,
 * findings, reportText (plain text, ready for an email or Slack step), summary.
 * A real run adds one row to Sync Log; a failed run adds a Failed row and stops with the error.
 * The tables, fields and select options it needs (Sync Log included) are checked before anything
 * is read or written.
 */

const settings = input.config();
const DRY_RUN = isTrue(settings.dryRun);
const STARTED = new Date();
const NOW = STARTED.toISOString();
const TODAY = NOW.slice(0, 10);
const WEEK = isoWeek(STARTED);
const RUN_ID = 'data-quality-' + NOW.replace(/\.\d{3}Z$/, 'Z');
const progress = { checked: 0, changed: 0 };

const CHECKS = ['Missing Required Field', 'Invalid Email', 'Orphan Order Line', 'Duplicate Customer', 'Unknown SKU'];
const SEVERITY = {
  'Missing Required Field': 'Medium',
  'Invalid Email': 'Medium',
  'Orphan Order Line': 'High',
  'Duplicate Customer': 'Medium',
  'Unknown SKU': 'High',
};
const REQUIRED = {
  Customers: ['Name', 'Email'],
  Orders: ['Customer', 'Email'],
  Products: ['Name', 'Status', 'Category'],
};
const FINDING_FIELDS = ['Finding ID', 'Report Week', 'Check', 'Table', 'Record ID', 'Record Name', 'Details', 'Severity', 'Status', 'Found At'];
const CUSTOMER_FIELDS = ['Name', 'Email', 'Phone', 'Shopify Customer ID', 'Created Via', 'Record Status', 'Orders', 'Subscribers'];
const ORDER_FIELDS = ['Order Number', 'Customer', 'Email'];
const LINE_FIELDS = ['Line Item ID', 'Order', 'Product', 'SKU', 'Quantity', 'Unit Price'];
const PRODUCT_FIELDS = ['SKU', 'Name', 'Status', 'Category'];
const SUBSCRIBER_FIELDS = ['Subscriber ID', 'Email'];
const SOURCE_ORDER = ['Shopify', 'Wholesale', 'Manual', 'Import'];

async function main() {
  const customersTable = getTable('Customers');
  const ordersTable = getTable('Orders');
  const linesTable = getTable('Order Lines');
  const productsTable = getTable('Products');
  const findingsTable = getTable('Data Quality');
  const subscribersTable = getTable('Subscribers');
  requireFields(customersTable, CUSTOMER_FIELDS);
  requireFields(ordersTable, ORDER_FIELDS);
  requireFields(linesTable, LINE_FIELDS);
  requireFields(productsTable, PRODUCT_FIELDS);
  requireFields(subscribersTable, SUBSCRIBER_FIELDS);
  requireFields(findingsTable, FINDING_FIELDS);
  requireOptions(findingsTable, 'Check', CHECKS);
  requireOptions(findingsTable, 'Table', ['Customers', 'Orders', 'Order Lines', 'Products']);
  requireOptions(findingsTable, 'Severity', unique(CHECKS.map(function (c) { return SEVERITY[c]; })));
  requireOptions(findingsTable, 'Status', ['Open', 'Fixed']);
  checkSyncLog();

  const customers = (await customersTable.selectRecordsAsync({ fields: CUSTOMER_FIELDS })).records;
  const orders = (await ordersTable.selectRecordsAsync({ fields: ORDER_FIELDS })).records;
  const lines = (await linesTable.selectRecordsAsync({ fields: LINE_FIELDS })).records;
  const products = (await productsTable.selectRecordsAsync({ fields: PRODUCT_FIELDS })).records;
  const existing = (await findingsTable.selectRecordsAsync({ fields: FINDING_FIELDS })).records.map(readFinding);
  progress.checked = customers.length + orders.length + lines.length + products.length;

  const problems = [];
  const active = customers.filter(function (c) { return selectName(c.getCellValue('Record Status')) !== 'Duplicate'; });

  // Missing required fields. A customer without an email may still have one on a subscription.
  const subscriberIds = flatten(active.filter(function (c) { return isBlank(c.getCellValue('Email')); })
    .map(function (c) { return linkIds(c, 'Subscribers'); }));
  const subscribers = await selectByIds(subscribersTable, subscriberIds, SUBSCRIBER_FIELDS);
  const subscriberEmail = new Map(subscribers.map(function (s) { return [s.id, (s.getCellValue('Email') || '').trim()]; }));
  for (const c of active) {
    const missing = REQUIRED.Customers.filter(function (f) { return isBlank(c.getCellValue(f)); });
    if (!missing.length) continue;
    let details = 'Missing ' + joinAnd(missing) + '.';
    if (missing.indexOf('Email') !== -1) {
      const hints = linkIds(c, 'Subscribers').map(function (id) { return subscriberEmail.get(id); }).filter(Boolean);
      if (hints.length) details += ' The linked subscription has the email ' + hints[0] + '.';
    }
    problems.push(problem('Missing Required Field', 'Customers', c.id, customerLabel(c), details));
  }
  for (const o of orders) {
    const missing = REQUIRED.Orders.filter(function (f) { return isBlank(o.getCellValue(f)); });
    if (missing.length) problems.push(problem('Missing Required Field', 'Orders', o.id, o.name, 'Missing ' + joinAnd(missing) + '.'));
  }
  for (const p of products) {
    const missing = REQUIRED.Products.filter(function (f) { return isBlank(p.getCellValue(f)); });
    if (missing.length) problems.push(problem('Missing Required Field', 'Products', p.id, p.name, 'Missing ' + joinAnd(missing) + '.'));
  }

  // Invalid emails.
  for (const c of active) {
    const why = emailProblem(c.getCellValue('Email'));
    if (why) problems.push(problem('Invalid Email', 'Customers', c.id, customerLabel(c), 'Email "' + c.getCellValue('Email').trim() + '" ' + why + '.'));
  }
  for (const o of orders) {
    const why = emailProblem(o.getCellValue('Email'));
    if (why) problems.push(problem('Invalid Email', 'Orders', o.id, o.name, 'Email "' + o.getCellValue('Email').trim() + '" ' + why + '.'));
  }

  // Orphan order lines and unknown SKUs.
  const productSkus = new Set(products.map(function (p) { return skuKey(p.getCellValue('SKU')); }).filter(Boolean));
  let notLinkedYet = 0;
  for (const line of lines) {
    const sku = (line.getCellValue('SKU') || '').trim();
    if (!linkIds(line, 'Order').length) {
      const parts = [];
      if (sku) parts.push('SKU ' + sku);
      if (line.getCellValue('Quantity') !== null) parts.push('quantity ' + line.getCellValue('Quantity'));
      if (line.getCellValue('Unit Price') !== null) parts.push('unit price ' + line.getCellValue('Unit Price').toFixed(2));
      problems.push(problem('Orphan Order Line', 'Order Lines', line.id, line.name,
        'Order line ' + line.name + ' is not linked to an order' + (parts.length ? ' (' + parts.join(', ') + ')' : '') + '.'));
    }
    if (sku && !productSkus.has(skuKey(sku))) {
      const orderNames = linkNames(line, 'Order');
      problems.push(problem('Unknown SKU', 'Order Lines', line.id, line.name,
        'SKU "' + sku + '" on line ' + line.name + (orderNames.length ? ' (order ' + orderNames.join(', ') + ')' : '') + ' matches no product.'));
    } else if (sku && !linkIds(line, 'Product').length) {
      notLinkedYet += 1;
    }
  }

  // Duplicate customers: same email after trimming and lowercasing. The record script 1 would keep
  // is not reported, the others are.
  const groups = new Map();
  for (const c of active) {
    const key = (c.getCellValue('Email') || '').trim().toLowerCase();
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  for (const members of groups.values()) {
    if (members.length < 2) continue;
    const shopifyIds = unique(members.map(function (m) { return (m.getCellValue('Shopify Customer ID') || '').trim(); }).filter(Boolean));
    const ranked = members.slice().sort(compareKeep);
    for (const d of ranked.slice(1)) {
      const details = 'Same email as ' + customerLabel(ranked[0]) + ' (' + ranked[0].id + '). ' + (shopifyIds.length > 1
        ? 'The records have different Shopify Customer IDs, so the dedupe script leaves them for a person to merge.'
        : 'The dedupe script (script 1) merges it into that record.');
      problems.push(problem('Duplicate Customer', 'Customers', d.id, customerLabel(d), details));
    }
  }

  problems.sort(function (a, b) {
    return CHECKS.indexOf(a.check) - CHECKS.indexOf(b.check) || compareText(a.recordName, b.recordName) || compareText(a.recordId, b.recordId);
  });

  // Compare with the findings that are already in the table.
  const activeByKey = new Map();
  const byFindingId = new Map();
  for (const f of existing) {
    byFindingId.set(f.findingId, f);
    if (f.recordId && (f.status === 'Open' || f.status === 'Ignored')) activeByKey.set(f.check + '|' + f.recordId, f);
  }
  const currentKeys = new Set();
  const creates = [];
  const updates = [];
  const created = [];
  const stillOpen = [];
  const reopened = [];
  for (const p of problems) {
    const key = p.check + '|' + p.recordId;
    currentKeys.add(key);
    if (activeByKey.has(key)) {
      stillOpen.push(p);
      continue;
    }
    const findingId = WEEK + ' / ' + p.check + ' / ' + p.recordId;
    const sameWeek = byFindingId.get(findingId);
    if (sameWeek) {
      updates.push({ id: sameWeek.id, fields: { 'Status': { name: 'Open' }, 'Details': appendLine(p.details, 'Found again on ' + TODAY + ', so the row was opened again.'), 'Found At': NOW } });
      reopened.push(p);
      continue;
    }
    creates.push({
      fields: {
        'Finding ID': findingId,
        'Report Week': WEEK,
        'Check': { name: p.check },
        'Table': { name: p.table },
        'Record ID': p.recordId,
        'Record Name': p.recordName,
        'Details': p.details,
        'Severity': { name: SEVERITY[p.check] },
        'Status': { name: 'Open' },
        'Found At': NOW,
      },
    });
    created.push(p);
  }
  const fixed = [];
  let legacy = 0;
  for (const f of existing) {
    if (CHECKS.indexOf(f.check) === -1 || f.status !== 'Open') continue;
    if (!f.recordId) {
      legacy += 1;
      continue;
    }
    if (currentKeys.has(f.check + '|' + f.recordId)) continue;
    updates.push({ id: f.id, fields: { 'Status': { name: 'Fixed' }, 'Details': appendLine(f.details, 'Not found in the ' + WEEK + ' check on ' + TODAY + ', so it was marked Fixed.') } });
    fixed.push(f);
  }

  if (!DRY_RUN) {
    await createInBatches(findingsTable, creates);
    await updateInBatches(findingsTable, updates);
  }

  const byCheck = {};
  for (const check of CHECKS) byCheck[check] = problems.filter(function (p) { return p.check === check; }).length;
  const reportText = buildReport(problems, created.length, stillOpen.length, fixed.length, reopened.length, legacy, notLinkedYet);
  const summary = (DRY_RUN ? 'Dry run, nothing written. ' : '') + WEEK + ': ' + problems.length + ' problem' + (problems.length === 1 ? '' : 's') +
    ' found, ' + created.length + ' new, ' + stillOpen.length + ' already open, ' + reopened.length + ' opened again, ' + fixed.length + ' fixed since the last check.';

  output.set('dryRun', DRY_RUN);
  output.set('reportWeek', WEEK);
  output.set('newFindings', created.length);
  output.set('stillOpen', stillOpen.length);
  output.set('fixed', fixed.length);
  output.set('reopened', reopened.length);
  output.set('legacyWithoutRecordId', legacy);
  output.set('linesWithoutProductLink', notLinkedYet);
  output.set('byCheck', byCheck);
  output.set('findings', problems.slice(0, 200).map(function (p) {
    return { check: p.check, table: p.table, recordId: p.recordId, recordName: p.recordName, details: p.details };
  }));
  output.set('reportText', reportText);
  output.set('summary', summary);

  if (!DRY_RUN) await writeSyncLog('Success', progress.checked, progress.changed, 0, reportText);
}

// ------------------------------------------------------------------ the checks

// Why an email is not usable, or null when it is fine or empty (empty is a missing field).
function emailProblem(raw) {
  if (isBlank(raw)) return null;
  const v = raw.trim();
  if (/\s/.test(v)) return 'contains a space';
  const at = v.split('@').length - 1;
  if (at === 0) return 'has no @';
  if (at > 1) return 'has more than one @';
  const local = v.split('@')[0];
  const domain = v.split('@')[1];
  if (!local) return 'has nothing before the @';
  if (!domain) return 'has nothing after the @';
  if (domain.indexOf('.') === -1) return 'has no domain ending';
  if (!/^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$/.test(domain)) return 'has an invalid domain';
  if (/^\.|\.$|\.\./.test(local) || !/^[A-Za-z0-9.!#$%&'*+\/=?^_`{|}~-]+$/.test(local)) return 'has invalid characters before the @';
  return null;
}

function compareKeep(a, b) {
  const pairs = [
    [hasText(a, 'Shopify Customer ID') ? 0 : 1, hasText(b, 'Shopify Customer ID') ? 0 : 1],
    [-linkIds(a, 'Orders').length, -linkIds(b, 'Orders').length],
    [-filledCount(a), -filledCount(b)],
    [sourceRank(a), sourceRank(b)],
  ];
  for (const pair of pairs) if (pair[0] !== pair[1]) return pair[0] - pair[1];
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function filledCount(c) {
  return ['Name', 'Email', 'Phone', 'Shopify Customer ID'].filter(function (f) { return hasText(c, f); }).length;
}

function sourceRank(c) {
  const i = SOURCE_ORDER.indexOf(selectName(c.getCellValue('Created Via')));
  return i === -1 ? SOURCE_ORDER.length : i;
}

function buildReport(problems, created, stillOpen, fixed, reopened, legacy, notLinkedYet) {
  const lines = ['Data quality report ' + WEEK + ' (' + TODAY + ')',
    problems.length + ' open problem' + (problems.length === 1 ? '' : 's') + ': ' + created + ' new, ' + stillOpen + ' already listed, ' +
    reopened + ' opened again. ' + fixed + ' fixed since the last check.'];
  for (const check of CHECKS) {
    const found = problems.filter(function (p) { return p.check === check; });
    if (!found.length) continue;
    const names = found.slice(0, 8).map(function (p) { return p.recordName; });
    lines.push('- ' + check + ': ' + found.length + ' (' + names.join(', ') + (found.length > 8 ? ' and ' + (found.length - 8) + ' more' : '') + ')');
  }
  if (notLinkedYet) lines.push('- Order lines with a known SKU but no product link: ' + notLinkedYet + ' (script 3 links them)');
  if (legacy) lines.push('- Older open findings without a Record ID, left as they are: ' + legacy);
  return lines.join('\n');
}

// ------------------------------------------------------------------ helpers

function problem(check, table, recordId, recordName, details) {
  return { check: check, table: table, recordId: recordId, recordName: recordName, details: details };
}

function readFinding(r) {
  return {
    id: r.id,
    findingId: r.getCellValue('Finding ID') || '',
    check: selectName(r.getCellValue('Check')),
    recordId: (r.getCellValue('Record ID') || '').trim(),
    status: selectName(r.getCellValue('Status')),
    details: r.getCellValue('Details') || '',
  };
}

function customerLabel(c) {
  const name = (c.getCellValue('Name') || '').trim();
  if (name) return name;
  const email = (c.getCellValue('Email') || '').trim();
  return 'Unnamed record' + (email ? ' (' + email + ')' : '');
}

// ISO 8601 week in UTC, for example 2026-W43. Weeks start on Monday; week 1 holds the first Thursday.
function isoWeek(date) {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return d.getUTCFullYear() + '-W' + (week < 10 ? '0' : '') + week;
}

function skuKey(v) {
  return typeof v === 'string' ? v.replace(/\s+/g, '').toUpperCase() : '';
}

function isBlank(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

function hasText(record, field) {
  const v = record.getCellValue(field);
  return typeof v === 'string' && v.trim() !== '';
}

function joinAnd(list) {
  return list.length < 2 ? list.join('') : list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1];
}

function compareText(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

function isTrue(v) {
  if (v === true) return true;
  return typeof v === 'string' && ['true', 'yes', '1'].indexOf(v.trim().toLowerCase()) !== -1;
}

function selectName(v) {
  return v && v.name ? v.name : '';
}

function linkIds(record, field) {
  const v = record.getCellValue(field);
  return v ? v.map(function (link) { return link.id; }) : [];
}

function linkNames(record, field) {
  const v = record.getCellValue(field);
  return v ? v.map(function (link) { return link.name; }) : [];
}

function unique(list) {
  return Array.from(new Set(list));
}

function flatten(lists) {
  return lists.reduce(function (all, list) { return all.concat(list); }, []);
}

function appendLine(text, line) {
  return text ? text.replace(/\s+$/, '') + '\n' + line : line;
}

// Stops with a clear message when the base differs from the blueprint, whether Airtable throws or
// returns nothing for a name it does not know.
function getTable(name) {
  let table = null;
  try {
    table = base.getTable(name);
  } catch (e) {
    table = null;
  }
  if (!table) throw new Error('This base has no table called "' + name + '". Rename the table in Airtable or change the name in this script.');
  return table;
}

function requireFields(table, names) {
  for (const name of names) {
    let field = null;
    try {
      field = table.getField(name);
    } catch (e) {
      field = null;
    }
    if (!field) throw new Error('The ' + table.name + ' table has no field called "' + name + '". Rename the field in Airtable or change the name in this script.');
  }
}

// An automation script cannot add a select option (only extensions can), so every option this
// script writes is checked before anything is written.
function requireOptions(table, fieldName, names) {
  const field = table.getField(fieldName);
  const choices = field && field.options && Array.isArray(field.options.choices) ? field.options.choices : [];
  const have = choices.map(function (c) { return c.name; });
  for (const name of names) {
    if (have.indexOf(name) === -1) {
      throw new Error('The ' + fieldName + ' field in the ' + table.name + ' table has no option "' + name + '". Add the option in Airtable or change the name in this script.');
    }
  }
}

// Sync Log is checked first as well, so a run never writes its changes and then fails on the log row.
function checkSyncLog() {
  const log = getTable('Sync Log');
  requireFields(log, ['Run ID', 'Automation', 'Trigger', 'Started At', 'Finished At', 'Status', 'Items Checked', 'Items Changed', 'Errors', 'Details']);
  requireOptions(log, 'Automation', ['06 Airtable Scripts']);
  requireOptions(log, 'Trigger', ['Airtable Automation']);
  requireOptions(log, 'Status', ['Success', 'Failed']);
}

// Reads records by id, 100 ids per query (the scripting limit). Over 500 ids one full read is cheaper.
async function selectByIds(table, ids, fields) {
  const wanted = unique(ids);
  if (!wanted.length) return [];
  if (wanted.length > 500) {
    const all = await table.selectRecordsAsync({ fields: fields });
    const set = new Set(wanted);
    return all.records.filter(function (r) { return set.has(r.id); });
  }
  let out = [];
  for (let i = 0; i < wanted.length; i += 100) {
    const result = await table.selectRecordsAsync({ fields: fields, recordIds: wanted.slice(i, i + 100) });
    out = out.concat(result.records);
  }
  return out;
}

async function createInBatches(table, creates) {
  for (let i = 0; i < creates.length; i += 50) {
    const batch = creates.slice(i, i + 50);
    await table.createRecordsAsync(batch);
    progress.changed += batch.length;
  }
}

async function updateInBatches(table, updates) {
  for (let i = 0; i < updates.length; i += 50) {
    const batch = updates.slice(i, i + 50);
    await table.updateRecordsAsync(batch);
    progress.changed += batch.length;
  }
}

async function writeSyncLog(status, checked, changed, errors, details) {
  await getTable('Sync Log').createRecordAsync({
    'Run ID': RUN_ID,
    'Automation': { name: '06 Airtable Scripts' },
    'Trigger': { name: 'Airtable Automation' },
    'Started At': STARTED.toISOString(),
    'Finished At': new Date().toISOString(),
    'Status': { name: status },
    'Items Checked': checked,
    'Items Changed': changed,
    'Errors': errors,
    'Details': details,
  });
}

try {
  await main();
} catch (error) {
  const message = error && error.message ? error.message : String(error);
  if (!DRY_RUN) {
    try {
      await writeSyncLog('Failed', progress.checked, progress.changed, 1, 'Data quality report failed: ' + message);
    } catch (logError) {
      console.log('Could not add the Failed row to Sync Log: ' + (logError && logError.message));
    }
  }
  throw error;
}
