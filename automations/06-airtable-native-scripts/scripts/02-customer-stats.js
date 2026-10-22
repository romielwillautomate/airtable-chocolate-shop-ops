/*
 * Demo Chocolate Co. | Airtable automation script 2 of 4: customer lifetime value and VIP tier
 *
 * Paste this into an Airtable automation as a "Run a script" action. Use it in up to four
 * automations, all with the same script:
 *   A. Trigger "When a record is updated" on Orders, watching Customer, Total, Refunded Amount and
 *      Order Status. Input orderRecordId = "Airtable record ID" of the trigger record.
 *   B. Trigger "When a record is created" on Orders, same input. Orders from the Shopify webhook
 *      arrive this way.
 *   C. As the second action after script 1 (dedupe), with customerRecordIds = affectedCustomerIds
 *      from script 1.
 *   D. Trigger "At a scheduled time" (nightly) with no record input. It recalculates every customer
 *      and also fixes a customer whose order was moved to someone else, which A cannot see.
 *
 * Input variables (left panel of the script editor):
 *   silverThreshold, goldThreshold, platinumThreshold   required. Lifetime value in dollars at which
 *       a customer reaches the tier, for example 40, 75 and 150. Each must be higher than the one before.
 *       (Or fill in FALLBACK_THRESHOLDS below to keep them in the script instead.)
 *   orderRecordId       optional. Recalculate the customer(s) linked to this order.
 *   customerRecordIds   optional. A list, or a comma separated text, of customer record ids. When the
 *                       variable is set but empty (script 1 merged nothing today), nothing is
 *                       recalculated: the nightly run covers everyone.
 *   dryRun              optional. true shows the result in the output and writes nothing.
 * With neither orderRecordId nor a customerRecordIds variable, every customer is recalculated.
 *
 * The rules (one place to change them):
 *   Lifetime Value       sum of Total minus Refunded Amount over the customer's orders, cancelled
 *                        orders left out (Order Status "Cancelled")
 *   Order Count          orders that were not cancelled
 *   Average Order Value  Lifetime Value / Order Count, left empty when the count is 0
 *   VIP Tier             Platinum, Gold or Silver from the thresholds above, otherwise Standard
 * A customer with no counted order gets Lifetime Value 0 and Order Count 0, written as real zeros.
 * Money is added up in whole cents, so the totals never pick up floating point noise.
 *
 * Only customers whose numbers changed are written, and Stats Updated At is set on those. A rerun
 * with nothing new writes nothing. A run for every customer adds one row to Sync Log; a run for one
 * order or a few customers does not, so the log is not flooded. A failed run always adds a Failed row.
 * The tables, fields and select options it needs (Sync Log included) are checked before any write.
 *
 * Output: mode, customersChecked, customersUpdated, ordersRead, tiers, updated, notes, warnings, summary.
 */

const settings = input.config();
const DRY_RUN = isTrue(settings.dryRun);
const STARTED = new Date();
const RUN_ID = 'customer-stats-' + STARTED.toISOString().replace(/\.\d{3}Z$/, 'Z');
const progress = { checked: 0, changed: 0, fullRun: false };

// Leave this null and set the thresholds as input variables. Fill it in, for example
// { silver: 40, gold: 75, platinum: 150 }, only if you would rather keep them in the script.
const FALLBACK_THRESHOLDS = null;

const CUSTOMER_FIELDS = ['Name', 'Orders', 'Lifetime Value', 'Order Count', 'Average Order Value', 'VIP Tier'];
const ORDER_FIELDS = ['Order Number', 'Customer', 'Total', 'Refunded Amount', 'Order Status'];
const TIERS = ['Standard', 'Silver', 'Gold', 'Platinum'];

async function main() {
  const thresholds = readThresholds(settings);
  const orderRecordId = readRecordId(settings.orderRecordId, 'orderRecordId');
  const customerIds = readRecordIdList(settings.customerRecordIds, 'customerRecordIds');
  const customerIdsGiven = Object.prototype.hasOwnProperty.call(settings, 'customerRecordIds');

  const customersTable = getTable('Customers');
  const ordersTable = getTable('Orders');
  requireFields(customersTable, CUSTOMER_FIELDS.concat(['Stats Updated At']));
  requireOptions(customersTable, 'VIP Tier', TIERS);
  requireFields(ordersTable, ORDER_FIELDS);
  checkSyncLog();

  const notes = [];
  const warnings = [];
  let mode = 'all';
  let targetIds = null;
  if (orderRecordId) {
    const order = await ordersTable.selectRecordAsync(orderRecordId, { fields: ['Order Number', 'Customer'] });
    if (!order) {
      notes.push('Order ' + orderRecordId + ' was not found (it may have been deleted), so every customer was recalculated.');
    } else {
      mode = 'order';
      targetIds = linkIds(order, 'Customer');
      if (!targetIds.length) notes.push('Order ' + order.name + ' has no customer linked, so there was nothing to recalculate.');
    }
  } else if (customerIdsGiven) {
    mode = 'customers';
    targetIds = customerIds;
    if (!customerIds.length) notes.push('customerRecordIds is empty (no customer changed in the step before), so there was nothing to recalculate.');
  }
  progress.fullRun = targetIds === null;

  let customers;
  let orders;
  if (targetIds === null) {
    customers = (await customersTable.selectRecordsAsync({ fields: CUSTOMER_FIELDS })).records;
    orders = (await ordersTable.selectRecordsAsync({ fields: ORDER_FIELDS })).records;
  } else {
    customers = await selectByIds(customersTable, targetIds, CUSTOMER_FIELDS);
    const found = new Set(customers.map(function (c) { return c.id; }));
    const missing = targetIds.filter(function (id) { return !found.has(id); });
    if (missing.length) notes.push('Not found (deleted?): ' + missing.join(', ') + '.');
    orders = await selectByIds(ordersTable, flatten(customers.map(function (c) { return linkIds(c, 'Orders'); })), ORDER_FIELDS);
  }
  const orderById = new Map(orders.map(function (o) { return [o.id, o]; }));
  progress.checked = customers.length;

  const tiers = { Standard: 0, Silver: 0, Gold: 0, Platinum: 0 };
  const updates = [];
  const changed = [];
  const now = new Date().toISOString();
  for (const customer of customers) {
    const stats = computeStats(linkIds(customer, 'Orders'), orderById, warnings);
    const tier = tierFor(stats.cents, thresholds);
    tiers[tier] += 1;
    const current = {
      ltv: customer.getCellValue('Lifetime Value'),
      count: customer.getCellValue('Order Count'),
      aov: customer.getCellValue('Average Order Value'),
      tier: selectName(customer.getCellValue('VIP Tier')) || null,
    };
    if (current.ltv === stats.ltv && current.count === stats.count && current.aov === stats.aov && current.tier === tier) continue;
    updates.push({
      id: customer.id,
      fields: {
        'Lifetime Value': stats.ltv,
        'Order Count': stats.count,
        'Average Order Value': stats.aov,
        'VIP Tier': { name: tier },
        'Stats Updated At': now,
      },
    });
    changed.push({ id: customer.id, name: customer.name, lifetimeValue: stats.ltv, orderCount: stats.count, averageOrderValue: stats.aov, vipTier: tier, previousTier: current.tier });
  }

  if (!DRY_RUN) await updateInBatches(customersTable, updates);

  const summary = (DRY_RUN ? 'Dry run, nothing written. ' : '') + 'Checked ' + customers.length + ' customer' +
    (customers.length === 1 ? '' : 's') + ' (' + mode + '), updated ' + updates.length + '. Tiers: ' +
    TIERS.map(function (t) { return t + ' ' + tiers[t]; }).join(', ') + '.' + (warnings.length ? ' ' + warnings.length + ' warning(s).' : '');

  output.set('dryRun', DRY_RUN);
  output.set('mode', mode);
  output.set('customersChecked', customers.length);
  output.set('customersUpdated', updates.length);
  output.set('ordersRead', orders.length);
  output.set('tiers', tiers);
  output.set('updated', changed.slice(0, 200));
  output.set('notes', notes);
  output.set('warnings', warnings);
  output.set('summary', summary);

  if (!DRY_RUN && progress.fullRun) {
    await writeSyncLog(warnings.length ? 'Partial' : 'Success', progress.checked, progress.changed, 0, summary + (warnings.length ? '\n' + warnings.join('\n') : ''));
  }
}

// ------------------------------------------------------------------ the rules

function computeStats(orderIds, orderById, warnings) {
  let cents = 0;
  let count = 0;
  for (const id of orderIds) {
    const order = orderById.get(id);
    if (!order) continue;
    if (selectName(order.getCellValue('Order Status')) === 'Cancelled') continue;
    const total = order.getCellValue('Total');
    const refunded = order.getCellValue('Refunded Amount');
    if (total === null) warnings.push('Order ' + order.name + ' has no Total, so it counts as 0.');
    let net = toCents(total || 0) - toCents(refunded || 0);
    if (net < 0) {
      warnings.push('Order ' + order.name + ' has a Refunded Amount above its Total, so it counts as 0.');
      net = 0;
    }
    cents += net;
    count += 1;
  }
  return { cents: cents, ltv: cents / 100, count: count, aov: count ? Math.round(cents / count) / 100 : null };
}

function tierFor(cents, t) {
  if (cents >= t.platinum) return 'Platinum';
  if (cents >= t.gold) return 'Gold';
  if (cents >= t.silver) return 'Silver';
  return 'Standard';
}

function toCents(amount) {
  return Math.round(Number(amount) * 100);
}

// Thresholds arrive as text when typed into the input panel. "1,000" and "$150" are fine too.
// An input variable that is set always wins over FALLBACK_THRESHOLDS.
function readThresholds(cfg) {
  const names = [['silverThreshold', 'silver'], ['goldThreshold', 'gold'], ['platinumThreshold', 'platinum']];
  const values = names.map(function (pair) {
    const name = pair[0];
    let raw = cfg[name];
    if ((raw === undefined || raw === null || String(raw).trim() === '') && FALLBACK_THRESHOLDS) raw = FALLBACK_THRESHOLDS[pair[1]];
    const text = raw === undefined || raw === null ? '' : String(raw).replace(/[$,\s]/g, '');
    const value = text === '' ? NaN : Number(text);
    if (!isFinite(value) || value < 0) {
      throw new Error('Set the input variable ' + name + ' to a lifetime value in dollars (for example 40, 75 and 150 for silver, gold and platinum). It is "' + (raw === undefined || raw === null ? '' : raw) + '" now.');
    }
    return toCents(value);
  });
  if (!(values[0] < values[1] && values[1] < values[2])) {
    throw new Error('The thresholds must go up: silverThreshold < goldThreshold < platinumThreshold. They are ' +
      values.map(function (v) { return v / 100; }).join(', ') + ' now.');
  }
  return { silver: values[0], gold: values[1], platinum: values[2] };
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

function readRecordIdList(value, name) {
  if (value === undefined || value === null || value === '') return [];
  const items = Array.isArray(value) ? value : String(value).split(/[\s,]+/);
  const ids = [];
  for (const item of items) {
    if (item === '' || item === null) continue;
    const id = readRecordId(item, name);
    if (id) ids.push(id);
  }
  return unique(ids);
}

// ------------------------------------------------------------------ helpers

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
      await writeSyncLog('Failed', progress.checked, progress.changed, 1, 'Customer stats failed: ' + message);
    } catch (logError) {
      console.log('Could not add the Failed row to Sync Log: ' + (logError && logError.message));
    }
  }
  throw error;
}
