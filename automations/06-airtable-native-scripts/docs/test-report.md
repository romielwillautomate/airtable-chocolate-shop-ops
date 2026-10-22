# Test report

**Date:** 2026-10-21
**Runtime:** Node 24.15.0, no packages, no network
**Test scripts:** [tests/run-tests.mjs](../tests/run-tests.mjs) and [tests/mutation-check.mjs](../tests/mutation-check.mjs) (both also run by [tests/e2e_mock_test.py](../tests/e2e_mock_test.py))
**Result:** 94 of 94 checks pass across 26 scenarios; 18 of 18 deliberate mistakes caught by the mutation check

## How it was tested

The four scripts are Airtable automation scripts, so they only run inside Airtable, and no real Airtable base was used. The test script:

1. loads [base/schema.json](../../../base/schema.json) and every file in [base/seed](../../../base/seed) into [tests/airtable-scripting-sim.mjs](../tests/airtable-scripting-sim.mjs), a strict stand-in for the automation scripting environment,
2. runs each script file unchanged, the way a "Run a script" action does: top level `await`, the globals `base`, `input`, `output`, `console` and `fetch`, and nothing else,
3. checks every record the scripts wrote, every query and write call they made, their output values and the Sync Log rows, against the facts in [base/seed/_meta.json](../../../base/seed/_meta.json),
4. starts every scenario from a fresh copy of the seed, with the clock frozen at 2026-10-20 07:00 UTC (the seed's "today") except in the real clock scenario.

### What the stand-in enforces

Everything below follows the Airtable scripting reference (airtable.com/developers/scripting/api: table, record, cell values, field, input, output) and the limits in Airtable's "Run a script" support article. Both were checked again during the review. The first scenario of the test proves each rule is enforced, so a script that breaks one fails the test.

| Rule | Source | What the stand-in does |
|---|---|---|
| At most 50 records per create, update or delete call | Table reference ("You may only update up to 50 records in one call") | Refuses the call, writes nothing |
| At most 100 `recordIds` per query | Table reference ("A maximum of 100 records can be requested at a time") | "Request exceeds maximum batch size limit of 100 items" |
| At most 30 selectRecords queries per run | Run a script limits ("30 selectRecords queries") | Fails the 31st query |
| At most 15 mutations per second | Run a script limits ("up to 15 mutations a second") | "Exceeded quota of 15 mutations per second". Each write takes 80 ms, so awaited batches pass and parallel writes fail |
| At most 6 MB of output | Run a script limits | Fails `output.set` past 6 MB |
| A field must be loaded before it is read | Airtable's "not loaded" error | "Cell values for field fld... are not loaded" |
| Field names must exist | Table and record reference | "No field matching ..." on reads, writes, `fields` and `getField` |
| Cell read formats | Cell values reference | Empty is `null`, links `[{id, name}]`, selects `{id, name, color}`, checkbox `true` or `null` |
| Cell write formats | Cell values reference | Links only as `[{id}]` to records that exist, selects only as `{name}` or `{id}` of an existing option, numbers as numbers, date times as ISO date times |
| A script cannot add a select option | Field reference (`updateOptionsAsync` is "extensions only") | Writing an option that does not exist fails; `field.options.choices` lists the options |
| Linked records stay in sync | Airtable links are two sided | Writing one side updates the other |
| Query order is arbitrary but stable | Table reference | Records sorted by record id, not by seed order |
| Only `input.config()` and `output.set()` in automations | Input and output reference | No `output.markdown`, `input.textAsync`; `output.set` takes JSON values only |
| No `setTimeout` in automation scripts | Airtable community answers | Not defined |
| A run fails when the script throws | Run a script article | The run is marked failed with the message |
| Runs can overlap | Trigger articles ("The trigger runs once per record, so pasting into 40 rows produces 40 runs") | Two `runScript` calls can run at the same time on one base, as two automation runs do |

Where Airtable documents a rule but not its error text (the 50 record limit, the 30 query limit, an unknown table, a record id that does not exist), the stand-in's message is close to Airtable's but may differ. Where the docs are silent it chose the stricter behavior: `record.name` needs the primary field loaded, `{id, name}` is not accepted as a link value, and `recordIds` that do not exist are skipped. The scripts are written to work either way, for example they always load the primary field and accept a `getField` that throws or returns nothing. The run time limit is 30 seconds here; Airtable has raised it to 120 seconds for now, so the stand-in is the stricter of the two.

## What testing changed

Building the tests found these problems, all fixed before the run below:

- A list of products sharing one SKU showed the SKU twice instead of the product names, which told a person nothing. It now shows each product's name and record id.
- None of the seed amounts happen to show floating point noise when added as dollars (28.45 + 21.45 is exactly 49.9 in JavaScript), so a script that summed dollars instead of cents would have passed. A test now uses 1.10 + 2.20 + (10.00 - 9.95), which is 3.350000000000001 in floating point dollars and must be 3.35.
- Two deliberate mistakes in the mutation check made the test runner crash instead of reporting failures. A scenario that throws is now recorded as a failed check and the other scenarios still run.
- The scripts relied on `table.getField` throwing for an unknown name. The docs do not say whether it throws or returns nothing, so every script now handles both, and the same for `base.getTable`.
- The tier thresholds come from input variables (Airtable takes a typed, fixed value there). Script 2 also has an optional `FALLBACK_THRESHOLDS` constant; a set input variable still wins. Both paths are tested.

## What the review changed

An adversarial review then went through the scripts, the stand-in and the docs against the Airtable docs and the other automations on the same base. It found seven problems. Each one is fixed, has a check of its own and a deliberate mistake in the mutation check. Run against the scripts as they were before the review, the new suite fails 11 checks.

| Problem in the first version | What it caused | Fix |
|---|---|---|
| Script 3 wrote the cleaned SKU back to the line (` dc-bar-milk-caramel` became `DC-BAR-MILK-CARAMEL`) | The Shopify orders sync (automation 01) writes the SKU as Shopify sent it on every order update, so the two kept changing the field back, and every change started script 3 again | The SKU text is never written; the line is linked and marked Matched |
| Script 3 ticked Needs Review on every run while the line's SKU was unknown | A person who unticked it after dealing with the order had it ticked again by the next sweep, every week | Needs Review is ticked only when a new reason line is added |
| Script 3 built the order's Review Reason from the one line that started the run | Airtable starts one run per created line; two lines with unknown SKUs created on one order together (an import, say) gave two runs that each wrote their own line, and the second wiped out the first | Each run works out the reasons from all of the order's lines, read after its own write, so both runs write the same text |
| Sync Log and the select options were only used at the end of a run | With Sync Log missing, script 2 wrote all 31 customers and only then failed; a renamed option such as Duplicate failed half way, after the orders had moved | Every script checks the Sync Log table, its fields and options, and every option it writes, before its first read or write |
| Script 4 read Subscribers without checking the table first | A renamed Subscribers field gave Airtable's raw "No field matching" error instead of the plain message | Subscribers is checked with the other tables |
| Script 1 described the kept record before copying a missing name to it | The notes and output said "duplicate of Unnamed record (rec...)" although the record was named Maya Chen in the same run. The kept email cleanup was not documented or tested | The final name is used; the email cleanup is documented and tested |
| Script 2 treated an empty `customerRecordIds` as "no input" | On a day with nothing to merge, the second action of the merge automation recalculated every customer and logged a full run, three hours after the nightly run | Set but empty means nothing to do: no read, no write, a note in the output |

## Results

```
The stand-in refuses what Airtable refuses
  PASS  51 records in one write call fail with the batch limit, nothing written  (failed: Request exceeds maximum batch size limit of 50 items)
  PASS  unloaded field, unknown field and 124 recordIds in one query are refused with Airtable's messages  ("Cell values for field fldzt9Yi0bD2zAxRo are not loaded")
  PASS  values in the wrong write format are refused (select as text, unknown option, link as text or to a missing record, number as text, date into a date time)  (9/9 refused)
  PASS  cells read in the scripting format: link [{id, name}], select {id, name, color}, empty link and unticked checkbox null  (success)
  PASS  20 writes fired in parallel trip the 15 per second limit; the same 20 awaited one by one pass  (failed: Exceeded quota of 15 mutations per second / success)
  PASS  30 queries in one run pass, the 31st fails  (failed: Exceeded quota of 30 selectRecords queries per script invocation)
  PASS  automation only APIs: no output.markdown, input.textAsync or setTimeout; output.set takes JSON only; input.config() returns the inputs  (success)
  PASS  a query without fields and a write without await are both recorded as warnings  (Customers: selectRecordsAsync without "fields" loads every field, which Airtable discourages and plans to remove | 1 write call(s) were still running when the script finished (a missing await))

Weekly data quality report on the seed (week 2026-W43)
  PASS  exactly the 10 findings the seed holds: 2 missing fields, 3 invalid emails, 1 orphan line, 3 duplicates, 1 unknown SKU  (10 rows, success)
  PASS  a finding row has every field: week, check, table, record id and name, details, severity, status, time  (Email "sam.ortiz@example" has no domain ending.)
  PASS  details say what is wrong and how to fix it (grace gets her subscription email, kai is named by email)  (7 rows compared)
  PASS  last week's hand written finding without a Record ID is left exactly as it was  (legacyWithoutRecordId 1)
  PASS  output for the next step: counts per check and a plain text report  (10 open problems: 10 new, 0 already listed, 0 opened again. 0 fixed since the last check.)
  PASS  one Sync Log row with the counts  (129 checked, 10 changed)
  PASS  one batched create for the findings, 6 queries, every query names its fields  (6 queries: Customers, Orders, Order Lines, Products, Data Quality, Subscribers)

Weekly data quality report, run again the same week
  PASS  no new or changed findings, only the Sync Log row  (new 0, still open 10)
  PASS  a dry run reports the same 10 findings and writes nothing at all  (success)

Merge duplicate customers on the seed
  PASS  a dry run plans the 3 merges and writes nothing, not even Sync Log  (Dry run, nothing written. Merged 3 duplicate customers into 3 records (moved 2 orders, 0 tickets, 0 subscriptions). 0 earlier duplicates had new links. 0 groups left for a person because of different Shopify Customer IDs. 0 problems.)
  PASS  keeps the record with a Shopify Customer ID in each group (maya, jordan, priya)  (success)
  PASS  each duplicate is marked Duplicate, points at the kept record and says what moved  (2026-10-20: duplicate of Maya Chen (rec0kKPgfSt7SmcnH), merged by the dedupe script. Moved: #1004.)
  PASS  orders #1004 and #1011 now belong to the kept records, and both sides of the link agree  (maya #1001 #1004, jordan #1002 #1011)
  PASS  writes only the 2 moved orders and the 6 customers, in one call each  ({"Orders":2,"Customers":6,"Sync Log":1})
  PASS  kept records only gain a Notes line (no field overwritten, nothing to fill in on the seed)  (2026-10-20: merged 1 duplicate record with the same email: Maya Chen (rechh5z9mNyFBCzQH).)
  PASS  output names the 6 affected customers for the stats script, and the summary reads well  (Merged 3 duplicate customers into 3 records (moved 2 orders, 0 tickets, 0 subscriptions). 0 earlier duplicates had new links. 0 groups left for a person because of different Shopify Customer IDs. 0 problems.)
  PASS  one Sync Log row: 31 checked, 8 changed, Success  (Success, 8 changed)

Merge duplicate customers, run again
  PASS  nothing left to merge, only the Sync Log row  (Merged 0 duplicate customers into 0 records (moved 0 orders, 0 tickets, 0 subscriptions). 0 earlier duplicates had new links. 0 groups left for a person because of different Shopify Customer IDs. 0 problems.)

Merge duplicate customers: links that reach a merged record later
  PASS  an order linked to a merged duplicate, and a ticket on a duplicate of a duplicate, both move to the kept record  (#1026 -> rec0kKPgfSt7SmcnH, T-0099 -> rec0kKPgfSt7SmcnH)
  PASS  a duplicate with no Merged Into keeps its order and is reported as a problem, Sync Log says Partial  (Lost Duplicate (recekko3Om0O9umnh) is marked Duplicate, but Merged Into does not lead to an Active customer, so its links were left where they are.)
  PASS  only the moved order and ticket are written, no customer record is touched again  ({"Orders":1,"Tickets":1,"Sync Log":1})

Merge duplicate customers: tickets, subscriptions, blanks and conflicts
  PASS  a ticket and a subscription on duplicates move to the kept records  (success)
  PASS  a missing phone is copied to the kept record, marketing consent is not  (phone +15550101001, consent undefined)
  PASS  three records with one email: both extras merge into jordan, the one with an order ranked first  (recAvbm0xumP3LmYI, rec2TsUBW5SBs0sde)
  PASS  two records with different Shopify Customer IDs are left Active and listed as a conflict  (["sam.lee@example.com"])

Customer stats for every customer on the seed
  PASS  Lifetime Value and Order Count match the seed facts for all 22 customers with orders  (22 of 22)
  PASS  refunds are subtracted and cancelled orders left out (ahmed 28.95 - 7.25 = 21.70, owen only cancelled = 0)  (ahmed 21.7, owen 0)
  PASS  the 9 customers without a counted order get a stored 0 and 0, an empty average and Standard  (9 of 9)
  PASS  Average Order Value is rounded to the cent (derek 80.35 / 3 = 26.78)  (derek 26.78)
  PASS  VIP tiers from the thresholds 40 / 75 / 150: derek Gold, 5 Silver, 25 Standard  ({"Standard":25,"Silver":5,"Gold":1,"Platinum":0})
  PASS  every customer gets Stats Updated At, all 31 in one write call, from 2 queries  (2 queries)
  PASS  one Sync Log row for the full run: 31 checked, 31 changed  (Checked 31 customers (all), updated 31. Tiers: Standard 25, Silver 5, Gold 1, Platinum 0.)

Customer stats, run again
  PASS  nothing changed, nothing written but the Sync Log row  (Checked 31 customers (all), updated 0. Tiers: Standard 25, Silver 5, Gold 1, Platinum 0.)

Customer stats after the merge (second action, customerRecordIds from script 1)
  PASS  merged lifetime values: maya 28.45 + 21.45 = 49.90 Silver, jordan 43.95 + 30.40 = 74.35 Silver (just under Gold)  (maya 49.9, jordan 74.35)
  PASS  averages round half a cent up (jordan 74.35 / 2 = 37.175 -> 37.18), duplicates drop to 0 with an empty average  (jordan 37.18)
  PASS  only the 6 listed customers are read and the 4 that changed are written; no Sync Log row for a partial run  (6 checked, 4 updated, 2 queries)
  PASS  on a day with nothing to merge, script 1 outputs an empty list and script 2 recalculates nobody (no query, no write, a note says why)  (customers, 0 queries, 0 writes)

Customer stats for one changed order (trigger on Orders)
  PASS  cancelling #1016 drops derek to 59.40 over 2 orders, Silver; only derek is written, 3 queries, no Sync Log row  (59.4 Silver, 3 queries)
  PASS  a 9.95 refund on #1021 gives 49.45, average 24.73 (24.725 rounded up)  (49.45 / 24.73)
  PASS  an order moved to elena updates elena (60.40, Silver); derek is fixed by the nightly full run, as documented  (derek 49.45 after the order run, 20 after the nightly run)

Customer stats: input variables
  PASS  no thresholds: the run fails with a clear message, writes no customer and logs a Failed row  (Set the input variable silverThreshold to a lifetime value in dollars (for example 40, 75 and 150 for silver, gold and platinum). It is "" now.)
  PASS  text in a threshold, thresholds that go down and a malformed record id all fail before anything is written  (The thresholds must go up: silverThreshold < goldThreshold < platinumThreshold. They are 75, 40, 150 now.)
  PASS  a value equal to a threshold reaches the tier (marcus 44.20 Silver, derek 80.35 Gold), "$1,000" is read as 1000  (marcus Silver, derek Gold, jordan Standard)
  PASS  money is added in whole cents: 1.10 + 2.20 + (10.00 - 9.95) is exactly 3.35, average 1.12 (in floating point dollars it is 3.350000000000001)  (3.35 / 1.12, one id given as text)
  PASS  an order that no longer exists falls back to every customer; an order without a customer writes nothing  (Order #1030 has no customer linked, so there was nothing to recalculate.)

Link order lines to products on the seed
  PASS  the imported #1004 lines are linked: exact SKU, and " dc-bar-milk-caramel" (matched without spaces and in capitals; the SKU text stays as imported)  (success)
  PASS  the products see the new lines too (the other side of the link)  (7 lines on DC-BAR-DARK-SEASALT)
  PASS  the unknown SKU DC-BAR-GINGER stays unlinked and flags order #1011 for review  (Unknown SKU DC-BAR-GINGER on line 14100000101103.)
  PASS  the gift wrap line (no SKU) and the orphan line are left exactly as they were  (missingSku 1)
  PASS  writes 2 lines and 1 order; Sync Log says Partial because an unknown SKU is left  (Checked 50 order lines, updated 2. Matched 48, unknown SKU 1, no SKU 1, SKU shared by several products 0. Orders flagged for review: #1011.)

Link order lines, run again
  PASS  nothing written but the Sync Log row, and #1011 does not get the reason twice  (Checked 50 order lines, updated 0. Matched 48, unknown SKU 1, no SKU 1, SKU shared by several products 0. Orders flagged for review: none.)

Link order lines: one new or changed line (trigger on Order Lines)
  PASS  a new line with "dc-bar-dark-85 " is linked to DC-BAR-DARK-85, from 2 queries and 1 write, no Sync Log row  (2 queries, {"Order Lines":1})
  PASS  a line whose SKU was changed moves from the old product to the new one, on both sides of the link  (now rec5RG79fLX1LXBZq)
  PASS  an unknown SKU keeps the link a person made and adds a line to the order's existing Review Reason  (Unknown SKU DC-BAR-NOPE on line 14100000101901.)
  PASS  an unknown SKU on a line without an order is marked and noted; no order is written  (Line LEGACY-0002 has the unknown SKU DC-BAR-NOPE-2 and no order, so no order was flagged.)

Link order lines: a SKU shared by two products
  PASS  the 3 lines with that SKU are left alone and listed, the rest is linked as usual  (3 ambiguous)

Data quality after the cleanup scripts ran
  PASS  the 3 duplicate findings are set to Fixed with a dated note; nothing new is created  (2026-W43: 7 problems found, 0 new, 7 already open, 0 opened again, 3 fixed since the last check.)

Data quality a week later (2026-W44)
  PASS  only the new problem (zoe) and the one closed by hand that is still broken (lena) get 2026-W44 rows  (2026-W44, 2026-W44)
  PASS  sam's fixed email closes his finding; kai's Ignored finding and the hand written 2026-W42 row are not touched  (sam Fixed, kai Ignored)
  PASS  problems still listed from last week (grace, kai, tom-n, the orphan line, the unknown SKU) get no second row  (still open 5 (grace, kai ignored, tom-n, orphan, unknown SKU))

Scale: more records than one batch or one query
  PASS  310 findings are created in 7 calls of at most 50  (310 in [50,50,50,50,50,50,10])
  PASS  123 merges move 122 orders: 122 ids read in 2 queries (100 + 22), written in 3 calls, 246 customers in 5 calls  (123 merged, order queries [100,22])
  PASS  331 customers recalculated in 7 calls; every kept bulk buyer has both orders (20.00, 2 orders)  (331 updated, 120 kept buyers)
  PASS  the 120 unlinked bulk lines are linked in 3 calls  (122 lines updated)
  PASS  no run comes near the limits: at most 30 queries and well under 30 seconds  (04 6q 751ms, 01 3q 841ms, 02 2q 746ms, 03 3q 465ms)

With the real clock (seed dates moved to today)
  PASS  the report week is today's ISO week and Found At is the time of the run  (2026-W43, seed moved 1 days)
  PASS  the seed's dates moved with the clock (last week's finding is still a week old)  (2026-10-14T07:00:00.000Z)
  PASS  ISO week at the turn of the year: Friday 2027-01-01 is 2026-W53  (2026-W53)

A base that differs from the blueprint
  PASS  a renamed field stops the script before it writes anything, with a message that names the field, and a Failed Sync Log row  (The Customers table has no field called "Merged Into". Rename the field in Airtable or change the name in this script.)
  PASS  a renamed table is reported by name  (This base has no table called "Data Quality". Rename the table in Airtable or change the name in this script.)
  PASS  with no Sync Log table the run still fails with the real reason, and the log problem goes to the console  (log: Could not add the Failed row to Sync Log: This base has no table called "Sync Log". Rename the table in Airtable or change the name in this script.)

Customer stats: thresholds kept in the script
  PASS  FALLBACK_THRESHOLDS works without input variables, and an input variable that is set wins over it  ({"Standard":25,"Silver":5,"Gold":1,"Platinum":0} then {"Standard":25,"Silver":4,"Gold":2,"Platinum":0})

Merge duplicate customers: a kept record with a messy email and no name
  PASS  the Shopify record is still kept, gets the clean email and the duplicate's name, and the notes and output call it "Maya Chen", not "Unnamed record"  (Maya Chen (rec0kKPgfSt7SmcnH))

Link order lines: the order flag, a person, and runs at the same moment
  PASS  a person who unticks Needs Review on #1011 is not overruled: the next sweep and a run for the same line leave the order alone  (Needs Review undefined, sweep wrote {"Sync Log":1})
  PASS  a second unknown SKU on that order flags it again and adds only the new line  ("Unknown SKU DC-BAR-GINGER on line 14100000101103.\nUnknown SKU DC-BAR-NOPE-C on line 14100000101104.")
  PASS  two new lines of one order checked at the same moment (Airtable starts one run per line): #1024 ends with both reasons, and a later sweep adds nothing  ("Unknown SKU DC-BAR-NOPE-A on line 14100000102491.\nUnknown SKU DC-BAR-NOPE-B on line 14100000102492.")
  PASS  once the product exists the line is linked and Matched; the order keeps its flag and reason for the person to clear  ({"Order Lines":1})
  PASS  the SKU text is never rewritten, so the Shopify sync and this script never undo each other: a run for the linked " dc-bar-milk-caramel" line writes nothing  (0 writes)

Checks before the first write
  PASS  without a Sync Log table all four scripts stop before their first read or write and name the table (before this check, script 2 wrote 31 customers first)  (dedupe ok, stats ok, link ok, dq ok)
  PASS  a renamed select option (a script cannot add one) stops the run before it writes, naming the field and option; the Failed row still goes to Sync Log  (dedupe true, link true, data quality true)
  PASS  script 4 checks Subscribers up front too: a renamed Subscribers field gives the plain message before any read, and no finding is written  (The Subscribers table has no field called "Email". Rename the field in Airtable or change the name in this script.)

Every script run above
  PASS  no write call above 50 records  (largest 50, 70 runs)
  PASS  no warnings: every query names its fields, every write is awaited, money is rounded to the cent  (166 queries)
  PASS  each script writes only to its own tables  (checked)
  PASS  no script calls fetch, so no data leaves Airtable  (fetch calls 0)
  PASS  the only failed runs are the 15 that test bad input variables or a base that differs from the blueprint, each with its intended message  (15 failed runs)

94/94 checks passed
(13.8 s)
```

## Mutation check

`tests/mutation-check.mjs` copies the scripts to a temporary folder, breaks one thing in the copy, runs the whole suite against it, and repeats for each mistake. The real scripts are never changed. The last eight were added in the review.

```
OK      unchanged scripts  (94/94 checks passed)
CAUGHT  01-dedupe-customers.js: dedupe keeps whichever record comes first instead of ranking them  (11 failing checks, 83/94 checks passed)
CAUGHT  01-dedupe-customers.js: dedupe copies marketing consent from the duplicate to the kept record  (1 failing checks, 93/94 checks passed)
CAUGHT  02-customer-stats.js: stats adds money up in floating point dollars instead of whole cents  (2 failing checks, 92/94 checks passed)
CAUGHT  02-customer-stats.js: a lifetime value exactly on the Gold threshold stays Silver  (1 failing checks, 93/94 checks passed)
CAUGHT  02-customer-stats.js: stats fires its writes in parallel instead of one batch at a time  (15 failing checks, 79/94 checks passed)
CAUGHT  03-link-order-lines.js: the linker sends 100 records per write call  (2 failing checks, 92/94 checks passed)
CAUGHT  03-link-order-lines.js: the linker compares SKUs without trimming and uppercasing  (7 failing checks, 87/94 checks passed)
CAUGHT  04-weekly-data-quality.js: the report forgets to look for an existing open finding, so reruns repeat work  (4 failing checks, 90/94 checks passed)
CAUGHT  04-weekly-data-quality.js: the report writes a single select as plain text  (9 failing checks, 78/87 checks passed)
CAUGHT  04-weekly-data-quality.js: the ISO week is counted from Sunday instead of the Thursday rule  (7 failing checks, 84/91 checks passed)
CAUGHT  01-dedupe-customers.js: the notes name the kept record by its old (empty) name after a name was copied to it  (1 failing checks, 93/94 checks passed)
CAUGHT  01-dedupe-customers.js: dedupe does not check the Duplicate option before it moves orders  (2 failing checks, 92/94 checks passed)
CAUGHT  02-customer-stats.js: stats finds out that Sync Log is missing only after it has written the customers  (1 failing checks, 93/94 checks passed)
CAUGHT  02-customer-stats.js: an empty customerRecordIds list from script 1 recalculates every customer  (1 failing checks, 93/94 checks passed)
CAUGHT  03-link-order-lines.js: the linker writes the cleaned SKU back to the line, against the Shopify sync  (3 failing checks, 91/94 checks passed)
CAUGHT  03-link-order-lines.js: the linker ticks Needs Review again on every run, overruling a person who unticked it  (1 failing checks, 93/94 checks passed)
CAUGHT  03-link-order-lines.js: a one-line run works out the order's reasons from its own line only  (2 failing checks, 92/94 checks passed)
CAUGHT  04-weekly-data-quality.js: the report does not check the Subscribers table before it starts  (2 failing checks, 92/94 checks passed)

18/18 deliberate mistakes caught
```

## What the scripts wrote (seed, frozen clock)

**Script 4, first weekly report** (`reportText`, also the Sync Log Details):

```
Data quality report 2026-W43 (2026-10-20)
10 open problems: 10 new, 0 already listed, 0 opened again. 0 fixed since the last check.
- Missing Required Field: 2 (Grace Kim, Unnamed record (kai.morgan@example.com))
- Invalid Email: 3 (Lena Fischer, Sam Ortiz, Tom Nguyen)
- Orphan Order Line: 1 (LEGACY-0001)
- Duplicate Customer: 3 (Jordan Blake, Maya Chen, P. Raman)
- Unknown SKU: 1 (14100000101103)
- Order lines with a known SKU but no product link: 2 (script 3 links them)
- Older open findings without a Record ID, left as they are: 1
```

**One Data Quality row** (the second Maya Chen record, after script 1 merged it and script 4 ran again):

```json
{
  "Finding ID": "2026-W43 / Duplicate Customer / rechh5z9mNyFBCzQH",
  "Report Week": "2026-W43",
  "Check": "Duplicate Customer",
  "Table": "Customers",
  "Record ID": "rechh5z9mNyFBCzQH",
  "Record Name": "Maya Chen",
  "Details": "Same email as Maya Chen (rec0kKPgfSt7SmcnH). The dedupe script (script 1) merges it into that record.\nNot found in the 2026-W43 check on 2026-10-20, so it was marked Fixed.",
  "Severity": "Medium",
  "Status": "Fixed",
  "Found At": "2026-10-20T07:00:00.000Z"
}
```

**Script 1, a merged duplicate** (excerpt: the fields script 1 reads or writes on the imported `Maya.Chen@Example.com ` record):

```json
{
  "Name": "Maya Chen",
  "Email": "Maya.Chen@Example.com ",
  "Phone": "+15550101001",
  "Created Via": "Import",
  "Record Status": "Duplicate",
  "Merged Into": ["rec0kKPgfSt7SmcnH"],
  "Notes": "2026-10-20: duplicate of Maya Chen (rec0kKPgfSt7SmcnH), merged by the dedupe script. Moved: #1004."
}
```

**Script 2, run as the second action with `affectedCustomerIds` from script 1** (`updated` output, record ids left out):

```json
[
  { "name": "Maya Chen", "lifetimeValue": 49.9, "orderCount": 2, "averageOrderValue": 24.95, "vipTier": "Silver", "previousTier": "Standard" },
  { "name": "Jordan Blake", "lifetimeValue": 0, "orderCount": 0, "averageOrderValue": null, "vipTier": "Standard", "previousTier": "Standard" },
  { "name": "Jordan Blake", "lifetimeValue": 74.35, "orderCount": 2, "averageOrderValue": 37.18, "vipTier": "Silver", "previousTier": "Silver" },
  { "name": "Maya Chen", "lifetimeValue": 0, "orderCount": 0, "averageOrderValue": null, "vipTier": "Standard", "previousTier": "Standard" }
]
```

**Script 3, the imported line ` dc-bar-milk-caramel` after the run** (linked and Matched; the SKU text is kept as imported):

```json
{
  "Line Item ID": "IMPORT-1004-2",
  "Product": ["rec6WOYlvJaIZTgYO"],
  "SKU": " dc-bar-milk-caramel",
  "SKU Status": "Matched"
}
```

**Script 3, order #1024 after two lines with unknown SKUs were checked by two runs at the same moment** (`Review Reason`):

```
Unknown SKU DC-BAR-NOPE-A on line 14100000102491.
Unknown SKU DC-BAR-NOPE-B on line 14100000102492.
```

**Sync Log rows from one pass of all four scripts:**

| Run ID | Status | Items Checked | Items Changed | Details (first line) |
|---|---|---|---|---|
| data-quality-2026-10-20T07:00:00Z | Success | 129 | 10 | Data quality report 2026-W43 (2026-10-20) |
| customer-stats-2026-10-20T07:00:00Z | Success | 31 | 31 | Checked 31 customers (all), updated 31. Tiers: Standard 25, Silver 5, Gold 1, Platinum 0. |
| customer-dedupe-2026-10-20T07:00:00Z | Success | 31 | 8 | Merged 3 duplicate customers into 3 records (moved 2 orders, 0 tickets, 0 subscriptions). ... |
| order-line-skus-2026-10-20T07:00:00Z | Partial | 50 | 3 | Checked 50 order lines, updated 2. Matched 48, unknown SKU 1, no SKU 1, ... Orders flagged for review: #1011. |
| data-quality-2026-10-20T07:00:00Z | Success | 129 | 3 | 7 open problems: 0 new, 7 already listed, 0 opened again. 3 fixed since the last check. |

Record ids are generated by the stand-in from the seed refs, so they are stable between runs but differ from a real base. All names, emails and phone numbers are made up.
