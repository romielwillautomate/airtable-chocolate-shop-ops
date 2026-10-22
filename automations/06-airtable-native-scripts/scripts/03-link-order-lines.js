/*
 * Demo Chocolate Co. | Airtable automation script 3 of 4: link order lines to products by SKU
 *
 * Paste this into an Airtable automation as a "Run a script" action. Use it in up to three
 * automations, all with the same script:
 *   A. Trigger "When a record is created" on Order Lines.
 *      Input orderLineRecordId = "Airtable record ID" of the trigger record.
 *   B. Trigger "When a record is updated" on Order Lines, watching only the SKU field, same input.
 *   C. Trigger "At a scheduled time" (weekly) with no input, to sweep every line, for example
 *      after an import.
 *
 * Input variables (left panel of the script editor, all optional):
 *   orderLineRecordId   the line to check. Leave it out to check every line.
 *   dryRun              true shows the result in the output and writes nothing
 *
 * What it does with each line:
 *   - Compares the line's SKU with every product SKU with all spaces removed and in capitals,
 *     so " dc-bar-milk-caramel" finds DC-BAR-MILK-CARAMEL.
 *   - One product matches: links the line to it (replacing a link to any other product) and sets
 *     SKU Status "Matched". The SKU text itself is never changed: it is the SKU as Shopify (or the
 *     import) sent it, and the Shopify orders sync writes that field on every order update, so a
 *     cleaned copy here would only be written back and forth.
 *   - No product matches: sets SKU Status "Unknown SKU" and keeps any link a person made. The order
 *     gets the line "Unknown SKU <sku> on line <id>." in its Review Reason, once per line, and Needs
 *     Review is ticked when such a line is added. A person who has dealt with it can untick Needs
 *     Review and leave the text: the order is not flagged again for that line. Unknown SKUs are
 *     listed in the output.
 *   - The line has no SKU (a gift wrap line, for example): sets SKU Status "Missing SKU".
 *   - Two products share the SKU: changes nothing and lists it in the output, because picking one
 *     would be a guess.
 * Only lines that need a change are written, so a rerun writes nothing.
 *
 * The reasons for an order are worked out from every line of that order, not only the line that
 * started the run. Airtable starts one run per created line, so two new lines of one order can be
 * checked at the same moment; both runs then write the same complete Review Reason instead of one
 * overwriting the other.
 *
 * A run over every line adds one row to Sync Log; a run for one line does not, so the log is not
 * flooded. A failed run always adds a Failed row and then stops with the error. The tables, fields
 * and select options it needs (Sync Log included) are checked before anything is written.
 *
 * Output: linesChecked, linesUpdated, matched, unknownSkus, missingSku, ambiguous, ordersFlagged, notes, summary.
 */

const settings = input.config();
const DRY_RUN = isTrue(settings.dryRun);
const STARTED = new Date();
const RUN_ID = 'order-line-skus-' + STARTED.toISOString().replace(/\.\d{3}Z$/, 'Z');
const progress = { checked: 0, changed: 0, fullRun: false };

const LINE_FIELDS = ['Line Item ID', 'Order', 'Product', 'SKU', 'SKU Status'];
const PRODUCT_FIELDS = ['SKU', 'Name', 'Status'];
const ORDER_FIELDS = ['Order Number', 'Needs Review', 'Review Reason', 'Order Lines'];

async function main() {
  const lineId = readRecordId(settings.orderLineRecordId, 'orderLineRecordId');
  const linesTable = getTable('Order Lines');
  const productsTable = getTable('Products');
  const ordersTable = getTable('Orders');
  requireFields(linesTable, LINE_FIELDS);
  requireOptions(linesTable, 'SKU Status', ['Matched', 'Unknown SKU', 'Missing SKU']);
  requireFields(productsTable, PRODUCT_FIELDS);
  requireFields(ordersTable, ORDER_FIELDS);
  checkSyncLog();
  progress.fullRun = !lineId;

  const products = (await productsTable.selectRecordsAsync({ fields: PRODUCT_FIELDS })).records;
  const bySku = new Map();
  for (const p of products) {
    const key = skuKey(p.getCellValue('SKU'));
    if (!key) continue;
    if (!bySku.has(key)) bySku.set(key, []);
    bySku.get(key).push(p);
  }

  const notes = [];
  let lines;
  if (lineId) {
    const one = await linesTable.selectRecordAsync(lineId, { fields: LINE_FIELDS });
    lines = one ? [one] : [];
    if (!one) notes.push('Order line ' + lineId + ' was not found (it may have been deleted), so there was nothing to do.');
  } else {
    lines = (await linesTable.selectRecordsAsync({ fields: LINE_FIELDS })).records;
  }
  progress.checked = lines.length;

  const lineUpdates = [];
  const unknown = [];
  const ambiguous = [];
  let matched = 0;
  let missingSku = 0;
  for (const line of lines) {
    const rawSku = line.getCellValue('SKU') || '';
    const key = skuKey(rawSku);
    const status = selectName(line.getCellValue('SKU Status'));
    const productIds = linkIds(line, 'Product');
    const fields = {};
    if (!key) {
      missingSku += 1;
      if (status !== 'Missing SKU') fields['SKU Status'] = { name: 'Missing SKU' };
    } else {
      const candidates = bySku.get(key) || [];
      if (candidates.length > 1) {
        ambiguous.push({ line: line.name, lineId: line.id, sku: rawSku.trim(), products: candidates.map(function (p) { return (p.getCellValue('Name') || p.name) + ' (' + p.id + ')'; }) });
        continue;
      }
      if (candidates.length === 1) {
        matched += 1;
        const product = candidates[0];
        if (!(productIds.length === 1 && productIds[0] === product.id)) fields['Product'] = [{ id: product.id }];
        if (status !== 'Matched') fields['SKU Status'] = { name: 'Matched' };
      } else {
        unknown.push({ line: line.name, lineId: line.id, sku: rawSku.trim(), orderIds: linkIds(line, 'Order'), orders: linkNames(line, 'Order') });
        if (status !== 'Unknown SKU') fields['SKU Status'] = { name: 'Unknown SKU' };
      }
    }
    if (Object.keys(fields).length) lineUpdates.push({ id: line.id, fields: fields });
  }

  if (!DRY_RUN) await updateInBatches(linesTable, lineUpdates);

  // Flag the orders that hold an unknown SKU, once per line. The orders are read after the line
  // writes, and their reasons come from every line of the order (a one-line run reads the order's
  // other lines too), so two runs for two lines of one order end with the same full text.
  for (const u of unknown) {
    if (!u.orderIds.length) notes.push('Line ' + u.line + ' has the unknown SKU ' + u.sku + ' and no order, so no order was flagged.');
  }
  const orderUpdates = [];
  const ordersFlagged = [];
  const flagIds = unique(flatten(unknown.map(function (u) { return u.orderIds; })));
  if (flagIds.length) {
    const orders = await selectByIds(ordersTable, flagIds, ORDER_FIELDS);
    const lineById = new Map(lines.map(function (l) { return [l.id, l]; }));
    if (lineId) {
      const others = flatten(orders.map(function (o) { return linkIds(o, 'Order Lines'); })).filter(function (id) { return !lineById.has(id); });
      for (const l of await selectByIds(linesTable, others, LINE_FIELDS)) lineById.set(l.id, l);
    }
    for (const order of orders) {
      const wanted = [];
      for (const id of linkIds(order, 'Order Lines')) {
        const l = lineById.get(id);
        if (!l) continue;
        const sku = (l.getCellValue('SKU') || '').trim();
        const key = skuKey(sku);
        if (key && !bySku.has(key)) wanted.push({ line: l.name, text: 'Unknown SKU ' + sku + ' on line ' + l.name + '.' });
      }
      wanted.sort(function (a, b) { return a.line < b.line ? -1 : a.line > b.line ? 1 : 0; });
      const reason = order.getCellValue('Review Reason') || '';
      const add = wanted.map(function (w) { return w.text; }).filter(function (text) { return reason.indexOf(text) === -1; });
      // Nothing new for this order: leave Needs Review as it is, even if a person unticked it.
      if (!add.length) continue;
      const fields = { 'Review Reason': add.reduce(appendLine, reason) };
      if (!order.getCellValue('Needs Review')) fields['Needs Review'] = true;
      orderUpdates.push({ id: order.id, fields: fields });
      ordersFlagged.push(order.name);
    }
  }

  if (!DRY_RUN) await updateInBatches(ordersTable, orderUpdates);

  const summary = (DRY_RUN ? 'Dry run, nothing written. ' : '') + 'Checked ' + lines.length + ' order line' + (lines.length === 1 ? '' : 's') +
    ', updated ' + lineUpdates.length + '. Matched ' + matched + ', unknown SKU ' + unknown.length + ', no SKU ' + missingSku +
    ', SKU shared by several products ' + ambiguous.length + '. Orders flagged for review: ' + (ordersFlagged.length ? ordersFlagged.join(', ') : 'none') + '.';

  output.set('dryRun', DRY_RUN);
  output.set('linesChecked', lines.length);
  output.set('linesUpdated', lineUpdates.length);
  output.set('matched', matched);
  output.set('unknownSkus', unknown.map(function (u) { return { line: u.line, lineId: u.lineId, sku: u.sku, orders: u.orders }; }));
  output.set('missingSku', missingSku);
  output.set('ambiguous', ambiguous);
  output.set('ordersFlagged', ordersFlagged);
  output.set('notes', notes);
  output.set('summary', summary);

  if (!DRY_RUN && progress.fullRun) {
    await writeSyncLog(unknown.length || ambiguous.length ? 'Partial' : 'Success', progress.checked, progress.changed, 0, summary);
  }
}

// ------------------------------------------------------------------ helpers

function skuKey(v) {
  return typeof v === 'string' ? v.replace(/\s+/g, '').toUpperCase() : '';
}

function readRecordId(value, name) {
  let v = value;
  if (Array.isArray(v) && v.length === 1) v = v[0];
  if (v === undefined || v === null || (typeof v === 'string' && v.trim() === '')) return null;
  if (typeof v !== 'string' || !/^rec[A-Za-z0-9]{14}$/.test(v.trim())) {
    throw new Error('The input variable ' + name + ' must be an Airtable record id (rec followed by 14 letters or digits). It is "' + v + '" now.');
  }
  return v.trim();
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
      await writeSyncLog('Failed', progress.checked, progress.changed, 1, 'Order line SKU check failed: ' + message);
    } catch (logError) {
      console.log('Could not add the Failed row to Sync Log: ' + (logError && logError.message));
    }
  }
  throw error;
}
