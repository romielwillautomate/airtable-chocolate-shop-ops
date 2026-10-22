// Randomized safety check of the box planner, used by e2e_mock_test.py.
//
// The test puts this file after the exact planner code of the workflow's "Code: Plan Boxes" node into one n8n
// Code node, so it checks the code that ships, on n8n's own JavaScript runtime. It builds many random bases
// (scarce stock, nut bars everywhere, allergen data that does not add up, recent boxes), plans every one of
// them and checks each plan against the TRUE allergens of every product. The true allergens are known here
// because this file made the data: the allergen fields plus every hidden hint it planted (almonds in the
// ingredient text, a Hazelnut flavor note, Milk chocolate with no Milk listed, an unlisted trace sentence).
//
// Checked for every box of every plan:
//   - a Nut-Free subscriber never gets a bar with tree nuts or peanuts, as an ingredient or as a trace,
//   - the same for Dairy-Free (milk), Gluten-Free (wheat) and Vegan (animal ingredients, the Vegan label),
//   - a subscriber with any dietary need never gets a bar without an ingredient list,
//   - no flavor the subscriber dislikes, no bar from the last REPEAT_WINDOW boxes, no bar twice,
//   - stock: each bar is reserved on a usable batch of that product, and no batch gives more units than it has,
//   - a box that is not full could not have taken any suitable bar that is still in stock at the end,
//   - one box per subscriber who needs one, none for a subscriber who already has one this month,
//   - a box planned before with a bar that no longer suits its subscriber is reported in conflicts.

function allergenPropertyCheck(planBoxes, options) {
  const opts = options || {};
  const runs = Math.max(1, Math.min(Number(opts.runs) || 1500, 20000));
  const firstSeed = Number(opts.seed) || 1;
  const today = String(opts.today || new Date().toISOString().slice(0, 10));

  const ALLERGENS = ['Milk', 'Soy', 'Tree Nuts', 'Peanuts', 'Wheat', 'Eggs', 'Sesame'];
  const NOTES = ['Sea Salt', 'Red Fruit', 'Caramel', 'Hazelnut', 'Almond', 'Raspberry', 'Orange', 'Chili', 'Cinnamon',
    'Matcha', 'Vanilla', 'Coffee', 'Mint', 'Peanut', 'Cookie', 'Lavender', 'Honey', 'Strawberry', 'Cherry', 'Pumpkin'];
  const FAMILIES = ['Classic', 'Salty', 'Caramel', 'Nutty', 'Fruity', 'Spiced', 'Earthy', 'Coffee', 'Fresh', 'Floral'];
  const TYPES = ['Dark', 'Dark', 'Milk', 'White', 'Ruby', 'Oat Milk'];
  // Ingredient words for an allergen that IS an ingredient (all of them words the planner documents).
  const WORDS = {
    'Milk': ['whole milk powder', 'skim milk powder', 'cream', 'butter', 'whey', 'Milk Powder'],
    'Tree Nuts': ['roasted hazelnuts', 'toasted almonds', 'walnuts', 'pecans', 'cashews', 'pistachios',
      'macadamia nuts', 'hazelnut praline', 'marzipan', 'nougat', 'Almond pieces', 'HAZELNUT paste'],
    'Peanuts': ['peanuts', 'peanut butter', 'roasted peanuts', 'Peanut pieces'],
    'Wheat': ['wheat flour', 'barley malt', 'spelt flakes', 'malted wheat'],
    'Soy': ['soy lecithin'],
    'Eggs': ['egg white', 'eggs'],
    'Sesame': ['sesame seeds'],
  };
  const TRACE_WORDS = { 'Milk': 'milk', 'Tree Nuts': 'tree nuts', 'Peanuts': 'peanuts', 'Wheat': 'wheat', 'Soy': 'soy',
    'Eggs': 'eggs', 'Sesame': 'sesame' };
  const NOTE_ALLERGEN = { Hazelnut: 'Tree Nuts', Almond: 'Tree Nuts', Peanut: 'Peanuts' };
  const PLAIN_NOTES = NOTES.filter(n => !NOTE_ALLERGEN[n]);
  const NUT_NAMES = ['Almond Crunch', 'Hazelnut Bliss', 'Pecan Praline', 'Peanut Brittle', 'Pistachio Dream'];

  let state = 0;
  const rand = () => {   // mulberry32, so every seed gives the same base again
    state = (state + 0x6D2B79F5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const chance = p => rand() < p;
  const int = (lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));
  const pick = a => a[Math.floor(rand() * a.length)];
  const some = (a, p) => a.filter(() => chance(p));
  const day = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const shiftMonth = (ym, by) => {
    const [y, m] = ym.split('-').map(Number);
    const t = y * 12 + (m - 1) + by;
    return `${Math.floor(t / 12)}-${String((t % 12) + 1).padStart(2, '0')}`;
  };
  const put = (fields, key, value) => {   // Airtable leaves empty values out
    if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length) || value === false) return;
    fields[key] = value;
  };

  const month = shiftMonth(today.slice(0, 7), 1);
  const shipDate = `${month}-01`;
  const cutoff = day(shipDate, 14);
  const stats = { runs: 0, boxes: 0, bars: 0, nutFreeBoxes: 0, nutFreeBars: 0, nutFreeShort: 0, nutFreeEmpty: 0,
    nutBarsToOthers: 0, hiddenNutProducts: 0, shortBoxes: 0, plannedBefore: 0, conflictsExpected: 0, plansStopped: 0,
    nutFreeBreaches: 0 };
  const violations = [];
  stats.violationCount = 0;
  const fail = (seed, msg) => { if (violations.length < 25) violations.push(`seed ${seed}: ${msg}`); stats.violationCount += 1; };

  for (let r = 0; r < runs; r++) {
    const seed = firstSeed + r;
    state = seed * 7919;
    // ---------------------------------------------------------------- products, with their true allergens
    const products = [];
    const truth = new Map();
    // each base has its own mix: how common nuts are, and how much stock there is (scarce, mixed or plenty)
    const nutRate = pick([0.04, 0.12, 0.25]);
    const stockMode = int(0, 2);
    const rate = a => (a === 'Tree Nuts' || a === 'Peanuts' ? nutRate : 0.12);
    const nProducts = int(10, 28);
    for (let i = 0; i < nProducts; i++) {
      const id = `recP${seed}x${i}`;
      const type = pick(TYPES);
      const contains = new Set(ALLERGENS.filter(a => chance(rate(a))));
      if ((type === 'Milk' || type === 'White' || type === 'Ruby') && chance(0.85)) contains.add('Milk');
      const traces = new Set(ALLERGENS.filter(a => !contains.has(a) && chance(rate(a))));
      const tIn = new Set(contains);       // true ingredient allergens
      const tTr = new Set(traces);         // true trace allergens
      if (type === 'Milk' || type === 'White' || type === 'Ruby') tIn.add('Milk');
      // flavor notes: a nut note mostly on a bar that has nuts, now and then as the only hint
      const notes = [...new Set([pick(PLAIN_NOTES), ...(chance(0.4) ? [pick(PLAIN_NOTES)] : [])])];
      if (contains.has('Tree Nuts') && chance(0.5)) notes.push(pick(['Hazelnut', 'Almond']));
      if (contains.has('Peanuts') && chance(0.5)) notes.push('Peanut');
      if (chance(0.03)) notes.push(pick(['Hazelnut', 'Almond', 'Peanut']));
      for (const n of notes) if (NOTE_ALLERGEN[n]) tIn.add(NOTE_ALLERGEN[n]);
      let name = `Bar ${seed}-${i}`;
      if (chance(0.06)) { name = `${pick(NUT_NAMES)} Bar ${i}`; tIn.add(/peanut/i.test(name) ? 'Peanuts' : 'Tree Nuts'); }
      const parts = ['Cocoa mass', 'cane sugar', 'cocoa butter'];
      for (const a of contains) parts.push(pick(WORDS[a]));
      const sentences = [];
      for (const a of traces) if (chance(0.5)) sentences.push(`Made on shared equipment with ${TRACE_WORDS[a]}.`);
      // hidden hints: the allergen is in the text but not in the fields
      if (chance(0.08)) { const a = pick(['Tree Nuts', 'Tree Nuts', 'Peanuts']); parts.push(pick(WORDS[a])); tIn.add(a); }
      if (chance(0.06)) { const a = pick(['Tree Nuts', 'Peanuts', 'Milk', 'Wheat']); sentences.push(`Made on shared equipment with ${TRACE_WORDS[a]}.`); tTr.add(a); }
      if (chance(0.05)) { parts.push(pick(WORDS.Milk)); tIn.add('Milk'); }
      if (chance(0.04)) { parts.push(pick(WORDS.Wheat)); tIn.add('Wheat'); }
      let honey = notes.includes('Honey');
      if (chance(0.04)) { parts.push('honey'); honey = true; }
      const noIngredients = chance(0.05);
      const ingredients = noIngredients ? '' : `${parts.join(', ')}.${sentences.length ? ' ' + sentences.join(' ') : ''}`;
      const veganLabel = chance(0.45);
      const fields = {};
      put(fields, 'SKU', `DC-RND-${seed}-${i}`);
      put(fields, 'Name', name);
      put(fields, 'Status', chance(0.92) ? 'Active' : pick(['Draft', 'Discontinued']));
      put(fields, 'Category', chance(0.95) ? 'Bar' : 'Praline');
      put(fields, 'Chocolate Type', type);
      put(fields, 'Flavor Family', pick(FAMILIES));
      put(fields, 'Flavor Notes', notes);
      put(fields, 'Contains Allergens', [...contains]);
      put(fields, 'May Contain Traces', [...traces]);
      put(fields, 'Diet Labels', veganLabel ? ['Vegan'] : []);
      put(fields, 'Ingredients', noIngredients ? '' : ingredients);
      put(fields, 'Subscription Eligible', chance(0.93));
      products.push({ id, createdTime: '2026-01-01T00:00:00.000Z', fields });
      const animal = tIn.has('Milk') || tIn.has('Eggs') || honey || type === 'Milk' || type === 'White';
      truth.set(id, {
        sku: fields.SKU, all: new Set([...tIn, ...tTr]), vegan: veganLabel && !animal, noIngredients, notes,
        eligible: fields.Status === 'Active' && fields['Subscription Eligible'] === true && fields.Category === 'Bar',
        hiddenNut: [...tIn, ...tTr].some(a => (a === 'Tree Nuts' || a === 'Peanuts') && !contains.has(a) && !traces.has(a)),
      });
      if (truth.get(id).hiddenNut) stats.hiddenNutProducts += 1;
    }
    // ---------------------------------------------------------------- batches, mostly scarce
    const batches = [];
    const usable = new Map();   // batch id -> units the planner may use
    const batchProduct = new Map();
    for (const p of products) {
      for (let k = chance(0.85) ? int(1, 3) : 0; k > 0; k--) {
        const id = `recB${seed}x${batches.length}`;
        const status = chance(0.8) ? 'Released' : pick(['Planned', 'On Hold', 'Expired', 'Depleted']);
        const produced = day(today, int(-200, 5));
        const best = day(produced, int(40, 500));
        const made = stockMode === 0 ? int(0, 3) : stockMode === 1 ? (chance(0.6) ? int(0, 4) : int(5, 40)) : int(10, 60);
        const reserved = chance(0.5) ? int(0, 2) : null;
        const sold = int(0, 3);
        const fields = {};
        put(fields, 'Batch Code', `B-RND-${seed}-${batches.length}`);
        put(fields, 'Product', [p.id]);
        put(fields, 'Status', status);
        put(fields, 'Produced On', produced);
        put(fields, 'Best Before', best);
        if (made !== null) fields['Quantity Made'] = made;
        if (reserved !== null) fields['Quantity Reserved'] = reserved;
        fields['Quantity Sold'] = sold;
        batches.push({ id, createdTime: '2026-01-01T00:00:00.000Z', fields });
        batchProduct.set(id, p.id);
        const ok = status === 'Released' && produced <= today && best > cutoff;
        usable.set(id, { start: reserved || 0, units: ok ? Math.max(made - (reserved || 0) - sold, 0) : 0 });
      }
    }
    // ---------------------------------------------------------------- subscribers, history, existing boxes
    const subscribers = [];
    const subTruth = new Map();
    const nSubs = int(3, 16);
    for (let i = 0; i < nSubs; i++) {
      const id = `recS${seed}x${i}`;
      const needs = [];
      if (chance(0.5)) needs.push('Nut-Free');
      if (chance(0.22)) needs.push('Dairy-Free');
      if (chance(0.15)) needs.push('Vegan');
      if (chance(0.2)) needs.push('Gluten-Free');
      const fields = {};
      const valid = chance(0.95);
      put(fields, 'Subscriber ID', `SUB-${seed}-${String(i).padStart(3, '0')}`);
      put(fields, 'Name', `Subscriber ${i}`);
      put(fields, 'Status', chance(0.9) ? 'Active' : pick(['Paused', 'Cancelled']));
      put(fields, 'Plan', valid ? pick(['4 Bars', '6 Bars', '8 Bars']) : '');
      put(fields, 'Dietary Needs', needs);
      put(fields, 'Flavor Dislikes', some(NOTES, 0.08));
      put(fields, 'Started On', day('2024-01-01', int(0, 900)));
      subscribers.push({ id, createdTime: '2026-01-01T00:00:00.000Z', fields });
      subTruth.set(id, { needs, dislikes: fields['Flavor Dislikes'] || [], size: valid ? Number(fields.Plan[0]) : 0,
        active: fields.Status === 'Active', valid, months: new Map() });
    }
    const repeatWindow = int(0, 2);
    const history = [];
    for (const s of subscribers) {
      const t = subTruth.get(s.id);
      for (let back = 1; back <= 3; back++) {
        if (!chance(0.6)) continue;
        const m = shiftMonth(month, -back);
        const got = some(products, 0.2).slice(0, 6);
        if (!got.length) continue;
        t.months.set(m, new Set(got.map(p => p.id)));
        for (const p of got) {
          history.push({ id: `recH${seed}x${history.length}`, createdTime: '2026-01-01T00:00:00.000Z',
            fields: { 'History ID': `${m} / ${s.fields['Subscriber ID']} / ${p.fields.SKU}`, 'Subscriber': [s.id], 'Product': [p.id], 'Box Month': m } });
        }
      }
    }
    // Now and then a subscriber already has this month's box, sometimes with a bar that does not suit them.
    const shipments = [];
    const hasBox = new Set();
    let expectedConflicts = 0;
    for (const s of subscribers) {
      const t = subTruth.get(s.id);
      if (!t.active || !t.valid || !chance(0.08)) continue;
      const inBox = some(products, 0.25).slice(0, t.size);
      shipments.push({ id: `recX${seed}x${shipments.length}`, createdTime: '2026-01-01T00:00:00.000Z', fields: {
        'Shipment ID': `BOX-${month}-${s.fields['Subscriber ID']}`, 'Subscriber': [s.id], 'Box Month': month,
        'Status': 'Planned', 'Fill Status': inBox.length === t.size ? 'Full' : 'Partial', 'Bars Planned': t.size,
        'Bars Packed': inBox.length, ...(inBox.length ? { 'Products': inBox.map(p => p.id) } : {}) } });
      hasBox.add(s.id);
      stats.plannedBefore += 1;
      for (const p of inBox) if (!suits(truth.get(p.id), t)) expectedConflicts += 1;
    }
    stats.conflictsExpected += expectedConflicts;

    // ---------------------------------------------------------------- plan
    const ctx = { month, today, shipDate, shelfLifeCutoff: cutoff, repeatWindow, runId: `property-${seed}`,
      trigger: 'Run Now', startedAt: `${today}T00:00:00Z` };
    let plan;
    try {
      plan = planBoxes({ ctx, subscribers, products, batches, history, shipments });
    } catch (e) {
      stats.plansStopped += 1;
      fail(seed, `the planner stopped with "${String((e && e.message) || e)}" on a valid base`);
      continue;
    }
    stats.runs += 1;

    // ---------------------------------------------------------------- check the plan against the truth
    const current = new Map([...usable].map(([id, u]) => [id, u.start]));
    const used = new Map();
    const newFor = new Map();
    for (const w of plan.writes) {
      if (w.kind !== 'new') continue;
      const subId = (w.box.Subscriber || [])[0];
      const t = subTruth.get(subId);
      if (!t) { fail(seed, `${w.shipmentId} is for an unknown subscriber`); continue; }
      if (newFor.has(subId)) fail(seed, `${w.shipmentId} is a second new box for one subscriber`);
      newFor.set(subId, w);
      const ids = w.box.Products || [];
      stats.boxes += 1;
      stats.bars += ids.length;
      const nutFree = t.needs.includes('Nut-Free');
      if (nutFree) { stats.nutFreeBoxes += 1; stats.nutFreeBars += ids.length; }
      if (ids.length < t.size) { stats.shortBoxes += 1; if (nutFree) stats.nutFreeShort += 1; }
      if (nutFree && !ids.length) stats.nutFreeEmpty += 1;
      if (new Set(ids).size !== ids.length) fail(seed, `${w.shipmentId} has a bar twice`);
      if (ids.length > t.size) fail(seed, `${w.shipmentId} has more bars than the plan`);
      if (w.box['Bars Packed'] !== ids.length || w.box['Bars Planned'] !== t.size) fail(seed, `${w.shipmentId} counts are wrong`);
      const window = recent(t, repeatWindow);
      for (const pid of ids) {
        const p = truth.get(pid);
        if (!p) { fail(seed, `${w.shipmentId} has an unknown product`); continue; }
        if (!p.eligible) fail(seed, `${w.shipmentId} has ${p.sku}, which is not an Active subscription bar`);
        if (nutFree && (p.all.has('Tree Nuts') || p.all.has('Peanuts'))) {
          stats.nutFreeBreaches += 1;
          fail(seed, `NUT-FREE BREACH ${w.shipmentId} has ${p.sku} (${[...p.all].join(', ')})`);
        }
        if (!suits(p, t)) fail(seed, `${w.shipmentId} (${t.needs.join(', ')}) has ${p.sku}, which does not suit (${[...p.all].join(', ')}${p.noIngredients ? ', no ingredient list' : ''})`);
        if (p.notes.some(n => t.dislikes.includes(n))) fail(seed, `${w.shipmentId} has a disliked flavor in ${p.sku}`);
        if (window.has(pid)) fail(seed, `${w.shipmentId} repeats ${p.sku} from a recent box`);
        if (!nutFree && (p.all.has('Tree Nuts') || p.all.has('Peanuts'))) stats.nutBarsToOthers += 1;
      }
      // stock: absolute Quantity Reserved numbers, one unit per bar on a batch of that product
      const perProduct = new Map();
      let units = 0;
      for (const rec of w.reserve) {
        const before = current.get(rec.id);
        const after = rec.fields['Quantity Reserved'];
        const back = (w.release.find(x => x.id === rec.id) || { fields: {} }).fields['Quantity Reserved'];
        if (before === undefined) { fail(seed, `${w.shipmentId} reserves on an unknown batch`); continue; }
        if (back !== before) fail(seed, `${w.shipmentId} would undo ${rec.id} to ${back}, not ${before}`);
        if (!(after > before)) fail(seed, `${w.shipmentId} does not raise ${rec.id}`);
        current.set(rec.id, after);
        units += after - before;
        used.set(rec.id, (used.get(rec.id) || 0) + after - before);
        const pid = batchProduct.get(rec.id);
        perProduct.set(pid, (perProduct.get(pid) || 0) + after - before);
      }
      if (units !== ids.length || ids.some(pid => perProduct.get(pid) !== 1) || perProduct.size !== ids.length) {
        fail(seed, `${w.shipmentId} reserves ${units} units for ${ids.length} bars, or on the wrong product`);
      }
    }
    for (const [bid, n] of used) if (n > usable.get(bid).units) fail(seed, `batch ${bid} gives ${n} units but has ${usable.get(bid).units}`);
    // a box that is not full could not have taken any suitable bar that is still in stock
    const left = new Map();
    for (const [bid, u] of usable) {
      const pid = batchProduct.get(bid);
      left.set(pid, (left.get(pid) || 0) + u.units - (used.get(bid) || 0));
    }
    for (const s of subscribers) {
      const t = subTruth.get(s.id);
      const wants = t.active && t.valid && !hasBox.has(s.id);
      const w = newFor.get(s.id);
      if (wants && !w) fail(seed, `${s.fields['Subscriber ID']} needs a box but got none`);
      if (!wants && w) fail(seed, `${s.fields['Subscriber ID']} got a box it should not get`);
      if (!w) continue;
      const ids = new Set(w.box.Products || []);
      if (ids.size >= t.size) continue;
      const window = recent(t, repeatWindow);
      for (const p of products) {
        const pt = truth.get(p.id);
        if (!pt.eligible || ids.has(p.id) || !suits(pt, t) || window.has(p.id) || pt.notes.some(n => t.dislikes.includes(n))) continue;
        if ((left.get(p.id) || 0) > 0) fail(seed, `${w.shipmentId} is short but ${pt.sku} suits and still has ${left.get(p.id)} left`);
      }
    }
    if ((plan.conflicts || []).length !== expectedConflicts) {
      fail(seed, `${expectedConflicts} unsuitable bars in boxes planned before, but ${(plan.conflicts || []).length} reported`);
    }
  }
  return { ok: violations.length === 0 && stats.runs === runs, stats, violations, firstSeed, month, today };

  function suits(p, t) {
    for (const need of t.needs) {
      if (need === 'Nut-Free' && (p.all.has('Tree Nuts') || p.all.has('Peanuts'))) return false;
      if (need === 'Dairy-Free' && p.all.has('Milk')) return false;
      if (need === 'Gluten-Free' && p.all.has('Wheat')) return false;
      if (need === 'Vegan' && !p.vegan) return false;
    }
    return !(t.needs.length && p.noIngredients);
  }
  function recent(t, windowSize) {
    const out = new Set();
    if (windowSize < 1) return out;
    const months = [...t.months.keys()].sort().reverse().slice(0, windowSize);
    for (const m of months) for (const pid of t.months.get(m)) out.add(pid);
    return out;
  }
}

if (typeof module !== 'undefined') module.exports = { allergenPropertyCheck };
