/*
 * Demo Chocolate Co. | Airtable automation script 1 of 4: merge duplicate customers
 *
 * Paste this into an Airtable automation as a "Run a script" action.
 * Trigger: "At a scheduled time", for example every day at 05:00.
 *
 * Input variables (left panel of the script editor, all optional):
 *   dryRun   true lists what would change in the output and writes nothing
 *
 * What it does:
 *   1. Groups Active customers by email after trimming spaces and lowercasing, so
 *      "Maya.Chen@Example.com " and "maya.chen@example.com" are the same person.
 *   2. Keeps one record per group: the one with a Shopify Customer ID, then the one with the
 *      most orders, then the one with the most contact details, then the one that came from
 *      Shopify, then the lowest record id (so the choice never changes between runs).
 *   3. Moves the other records' orders, tickets and subscriptions to the kept record by
 *      rewriting the Customer link on each moved order, ticket or subscriber. Nothing else is
 *      written, so an order that another automation links at the same moment is never unlinked.
 *   4. Marks each other record as Record Status "Duplicate", points Merged Into at the kept
 *      record, copies a missing name or phone number over to the kept record, writes the kept
 *      record's email in its clean form (trimmed, lowercase) when it was stored with capitals or
 *      spaces, and adds a dated line to Notes on both. Accepts Marketing is never copied: consent
 *      stays with the record it was given on.
 *   5. Leaves alone any group in which two records have different Shopify Customer IDs. Those
 *      are listed in the output under "conflicts" for a person to check.
 *   6. Moves links that reached an already merged duplicate later (for example an order that an
 *      import linked to the old record) on to the record it was merged into.
 *
 * Output (usable in later steps): duplicateGroups, duplicatesMerged, ordersMoved, ticketsMoved,
 * subscriptionsMoved, affectedCustomerIds, merges, conflicts, problems, summary, dryRun.
 * Add script 2 (customer stats) as the next action with customerRecordIds set to
 * affectedCustomerIds, so lifetime values are recalculated in the same automation run.
 *
 * A real run (not a dry run) adds one row to the Sync Log table. If the script fails it adds a
 * Failed row and then stops with the error, so the automation run shows as failed. The tables,
 * fields and select options it needs (Sync Log included) are checked before anything is written.
 */

const settings = input.config();
const DRY_RUN = isTrue(settings.dryRun);
const STARTED = new Date();
const TODAY = STARTED.toISOString().slice(0, 10);
const RUN_ID = 'customer-dedupe-' + STARTED.toISOString().replace(/\.\d{3}Z$/, 'Z');
const progress = { checked: 0, changed: 0 };

const CUSTOMER_FIELDS = ['Name', 'Email', 'Phone', 'Shopify Customer ID', 'Created Via', 'Record Status',
  'Merged Into', 'Notes', 'Orders', 'Tickets', 'Subscribers'];
// Where a customer record came from, best first. Used only to break a tie.
const SOURCE_ORDER = ['Shopify', 'Wholesale', 'Manual', 'Import'];
// Tables that link to Customers. Their Customer link moves from a duplicate to the kept record.
const LINKED = [
  { table: 'Orders', backLink: 'Orders', link: 'Customer', label: 'Order Number', key: 'orders' },
  { table: 'Tickets', backLink: 'Tickets', link: 'Customer', label: 'Ticket ID', key: 'tickets' },
  { table: 'Subscribers', backLink: 'Subscribers', link: 'Customer', label: 'Subscriber ID', key: 'subscriptions' },
];

async function main() {
  const customersTable = getTable('Customers');
  requireFields(customersTable, CUSTOMER_FIELDS);
  requireOptions(customersTable, 'Record Status', ['Duplicate']);
  for (const spec of LINKED) requireFields(getTable(spec.table), [spec.label, spec.link]);
  checkSyncLog();

  const query = await customersTable.selectRecordsAsync({ fields: CUSTOMER_FIELDS });
  const customers = query.records.map(readCustomer);
  const byId = new Map(customers.map(function (c) { return [c.id, c]; }));
  progress.checked = customers.length;

  const moves = new Map(); // duplicate id -> id of the record it is merged into
  const problems = [];
  const stragglers = [];

  // Records merged in an earlier run that have picked up new links since then.
  for (const c of customers) {
    if (c.status !== 'Duplicate' || !hasLinks(c)) continue;
    const target = followMergedInto(c, byId);
    if (!target) {
      problems.push(describe(c) + ' is marked Duplicate, but Merged Into does not lead to an Active customer, so its links were left where they are.');
      continue;
    }
    moves.set(c.id, target.id);
    stragglers.push({ duplicate: c, target: target });
  }

  // New duplicate groups among the Active customers.
  const groups = new Map();
  for (const c of customers) {
    if (c.status === 'Duplicate' || !c.emailKey) continue;
    if (!groups.has(c.emailKey)) groups.set(c.emailKey, []);
    groups.get(c.emailKey).push(c);
  }
  const merges = [];
  const conflicts = [];
  for (const [email, members] of groups) {
    if (members.length < 2) continue;
    const shopifyIds = unique(members.map(function (m) { return m.shopifyId; }).filter(Boolean)).sort();
    if (shopifyIds.length > 1) {
      conflicts.push({ email: email, shopifyCustomerIds: shopifyIds, customers: members.map(describe).sort() });
      continue;
    }
    const ranked = members.slice().sort(compareKeep);
    merges.push({ email: email, keep: ranked[0], duplicates: ranked.slice(1) });
    for (const d of ranked.slice(1)) moves.set(d.id, ranked[0].id);
  }
  merges.sort(function (a, b) { return a.email < b.email ? -1 : a.email > b.email ? 1 : 0; });
  conflicts.sort(function (a, b) { return a.email < b.email ? -1 : a.email > b.email ? 1 : 0; });

  // A straggler's target may itself be merged in this run, so always follow moves to the end.
  function finalTarget(id) {
    let current = id;
    const seen = new Set();
    while (moves.has(current) && !seen.has(current)) {
      seen.add(current);
      current = moves.get(current);
    }
    return current;
  }

  // Plan the link moves, table by table. Only records that link to a moved customer are read.
  const movedLabels = new Map(); // duplicate id -> names of the orders, tickets and subscriptions moved
  const moved = { orders: 0, tickets: 0, subscriptions: 0 };
  const linkWrites = [];
  for (const spec of LINKED) {
    const ids = unique(flatten(Array.from(moves.keys()).map(function (id) { return byId.get(id).links[spec.backLink]; })));
    if (!ids.length) continue;
    const table = getTable(spec.table);
    const records = await selectByIds(table, ids, [spec.label, spec.link]);
    const updates = [];
    for (const r of records) {
      const current = linkIds(r, spec.link);
      const next = unique(current.map(finalTarget));
      if (sameList(current, next)) continue;
      updates.push({ id: r.id, fields: { [spec.link]: next.map(function (id) { return { id: id }; }) } });
      moved[spec.key] += 1;
      for (const from of current) {
        if (!moves.has(from)) continue;
        if (!movedLabels.has(from)) movedLabels.set(from, []);
        movedLabels.get(from).push(r.name);
      }
    }
    linkWrites.push({ table: table, updates: updates });
  }

  // Plan the customer updates: the kept record and every merged duplicate.
  const customerUpdates = [];
  for (const m of merges) {
    const keepFields = {};
    const named = m.duplicates.filter(function (d) { return d.name; })[0];
    if (!m.keep.name && named) keepFields['Name'] = named.name;
    // Name the kept record as it will read after this run, so a copied name shows up in the notes.
    m.keepLabel = ((m.keep.name || (named ? named.name : '')) || 'Unnamed record') + ' (' + m.keep.id + ')';
    const withPhone = m.duplicates.filter(function (d) { return d.phone; })[0];
    if (!m.keep.phone && withPhone) keepFields['Phone'] = withPhone.phone;
    if (m.keep.emailRaw !== m.keep.emailKey && looksLikeEmail(m.keep.emailKey)) keepFields['Email'] = m.keep.emailKey;
    const count = m.duplicates.length;
    keepFields['Notes'] = appendLine(m.keep.notes, TODAY + ': merged ' + count + ' duplicate record' + (count === 1 ? '' : 's') +
      ' with the same email: ' + m.duplicates.map(describe).join(', ') + '.');
    customerUpdates.push({ id: m.keep.id, fields: keepFields });
    for (const d of m.duplicates) {
      const labels = movedLabels.get(d.id) || [];
      customerUpdates.push({
        id: d.id,
        fields: {
          'Record Status': { name: 'Duplicate' },
          'Merged Into': [{ id: m.keep.id }],
          'Notes': appendLine(d.notes, TODAY + ': duplicate of ' + m.keepLabel + ', merged by the dedupe script. Moved: ' +
            (labels.length ? labels.join(', ') : 'nothing') + '.'),
        },
      });
    }
  }

  if (!DRY_RUN) {
    for (const w of linkWrites) await updateInBatches(w.table, w.updates);
    await updateInBatches(customersTable, customerUpdates);
  }

  const affected = [];
  for (const m of merges) {
    affected.push(m.keep.id);
    for (const d of m.duplicates) affected.push(d.id);
  }
  for (const s of stragglers) {
    affected.push(s.duplicate.id);
    affected.push(finalTarget(s.target.id));
  }
  const duplicatesMerged = merges.reduce(function (n, m) { return n + m.duplicates.length; }, 0);
  const summary = (DRY_RUN ? 'Dry run, nothing written. ' : '') +
    'Merged ' + duplicatesMerged + ' duplicate customer' + (duplicatesMerged === 1 ? '' : 's') + ' into ' + merges.length +
    ' record' + (merges.length === 1 ? '' : 's') + ' (moved ' + moved.orders + ' order' + (moved.orders === 1 ? '' : 's') + ', ' +
    moved.tickets + ' ticket' + (moved.tickets === 1 ? '' : 's') + ', ' + moved.subscriptions + ' subscription' +
    (moved.subscriptions === 1 ? '' : 's') + '). ' + stragglers.length + ' earlier duplicate' + (stragglers.length === 1 ? '' : 's') +
    ' had new links. ' + conflicts.length + ' group' + (conflicts.length === 1 ? '' : 's') +
    ' left for a person because of different Shopify Customer IDs. ' + problems.length + ' problem' + (problems.length === 1 ? '' : 's') + '.';

  output.set('dryRun', DRY_RUN);
  output.set('duplicateGroups', merges.length);
  output.set('duplicatesMerged', duplicatesMerged);
  output.set('ordersMoved', moved.orders);
  output.set('ticketsMoved', moved.tickets);
  output.set('subscriptionsMoved', moved.subscriptions);
  output.set('affectedCustomerIds', unique(affected));
  output.set('merges', merges.map(function (m) {
    return { email: m.email, kept: m.keepLabel, keptId: m.keep.id, duplicateIds: m.duplicates.map(function (d) { return d.id; }) };
  }));
  output.set('conflicts', conflicts);
  output.set('problems', problems);
  output.set('summary', summary);

  if (!DRY_RUN) {
    await writeSyncLog(conflicts.length || problems.length ? 'Partial' : 'Success', progress.checked, progress.changed, 0, summary);
  }
}

// ------------------------------------------------------------------ helpers

function readCustomer(r) {
  const email = r.getCellValue('Email') || '';
  return {
    id: r.id,
    name: (r.getCellValue('Name') || '').trim(),
    emailRaw: email,
    emailKey: email.trim().toLowerCase(),
    phone: r.getCellValue('Phone') || '',
    shopifyId: (r.getCellValue('Shopify Customer ID') || '').trim(),
    createdVia: selectName(r.getCellValue('Created Via')),
    status: selectName(r.getCellValue('Record Status')),
    mergedInto: linkIds(r, 'Merged Into'),
    notes: r.getCellValue('Notes') || '',
    links: { Orders: linkIds(r, 'Orders'), Tickets: linkIds(r, 'Tickets'), Subscribers: linkIds(r, 'Subscribers') },
  };
}

function hasLinks(c) {
  return c.links.Orders.length + c.links.Tickets.length + c.links.Subscribers.length > 0;
}

// Follows Merged Into until it reaches a customer that is not a duplicate. Null on a dead end or a loop.
function followMergedInto(c, byId) {
  let current = c;
  const seen = new Set([c.id]);
  while (current && current.status === 'Duplicate') {
    const next = byId.get(current.mergedInto[0]);
    if (!next || seen.has(next.id)) return null;
    seen.add(next.id);
    current = next;
  }
  return current;
}

function compareKeep(a, b) {
  const pairs = [
    [a.shopifyId ? 0 : 1, b.shopifyId ? 0 : 1],
    [-a.links.Orders.length, -b.links.Orders.length],
    [-filledCount(a), -filledCount(b)],
    [sourceRank(a), sourceRank(b)],
  ];
  for (const pair of pairs) if (pair[0] !== pair[1]) return pair[0] - pair[1];
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

function filledCount(c) {
  return [c.name, c.emailRaw, c.phone, c.shopifyId].filter(Boolean).length;
}

function sourceRank(c) {
  const i = SOURCE_ORDER.indexOf(c.createdVia);
  return i === -1 ? SOURCE_ORDER.length : i;
}

function describe(c) {
  return (c.name || 'Unnamed record') + ' (' + c.id + ')';
}

function looksLikeEmail(v) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
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

function unique(list) {
  return Array.from(new Set(list));
}

function flatten(lists) {
  return lists.reduce(function (all, list) { return all.concat(list); }, []);
}

function sameList(a, b) {
  return a.length === b.length && a.every(function (x, i) { return x === b[i]; });
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
  requireOptions(log, 'Status', ['Success', 'Partial', 'Failed']);
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

// One write call per 50 records (the scripting limit), awaited one after the other so the
// automation stays under 15 writes per second.
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
      await writeSyncLog('Failed', progress.checked, progress.changed, 1, 'Customer dedupe failed: ' + message);
    } catch (logError) {
      console.log('Could not add the Failed row to Sync Log: ' + (logError && logError.message));
    }
  }
  throw error;
}
