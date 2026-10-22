# Test report

**Date:** 2026-10-22 (UTC)
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 133 of 133 checks pass: 6 static checks of the workflow file, 125 checks across 34 scenarios run through n8n (one of them the random base check), one allergen check over every scenario together, and a final check that every temporary workflow was deleted. The run took 16 minutes, most of it the workflow's real 31 second rate limit waits.

## How it was tested

No real Airtable base or Slack workspace was used. The test script:

1. checks the workflow file itself: every HTTP Request node retries 3 times and has its error output wired, the six reads use `POST .../listRecords` with offset pagination 500 ms apart, every Airtable call comes right after a Wait of at least 500 ms, the run claims its Sync Log row before any read, every failure after the start goes through the node that closes that row, every IF condition uses an operator that exists in the IF node v2, and there are no em or en dashes,
2. starts the repo's shared mocks ([tests/mocks](../../../tests/mocks)) on port 8794: the Demo Chocolate Co. base loaded from `base/seed` with every date moved so the seed's "today" is today, and Slack incoming webhooks. `mock.lose_answer` adds the failure where Airtable saves a write and the answer is lost (the workflow sees HTTP 500). It started as a small subclass in this test and moved into the shared mock during the integration on 2026-10-22, because automation 02 needs the same failure,
3. imports a temporary copy of the workflow into n8n with every API host pointed at the mocks and fake keys (the schedule is disabled, so only the test starts runs), plus an **active** copy of `shared/error-alert-slack.json` as its error workflow,
4. calls the Run Now webhook the way you would, waits for each run to finish, then checks every request the workflow made, every record it left in the mock base, every Slack message and every error alert,
5. deletes both temporary workflows (also when a check fails).

Three more temporary copies run with other settings: `REPEAT_WINDOW=2`, an invalid `REPEAT_WINDOW`, and the Box History read limited to one page.

**Allergen safety, three ways.** Allergen safety is the property that matters most here, so it is not left to the allergen scenarios alone:

1. **Every box of every scenario** is judged from its own product records (Contains Allergens, May Contain Traces, Diet Labels, a scan of the ingredient text and the name, the flavor notes and the chocolate type), whatever the scenario is about: a rate limit, a lost answer, a sold out bar. The last check adds it all up.
2. **Stock that runs short.** Two scenarios take the safe bars away: once every nut-safe bar is sold out (the nut-free boxes stay Empty), once every nut-safe bar keeps exactly 1 unit while the nut and trace bars keep their stock (13 safe units for 22 nut-free bars). The nut-free boxes stay short and say why; the nut bars go to other subscribers and still have stock left, so the workflow had them and did not use them.
3. **Random bases.** [tests/allergen_property.js](../tests/allergen_property.js) runs in its own temporary workflow, on n8n's own JavaScript runtime, with the planner code taken from the workflow file's `Code: Plan Boxes` node. It plans 1,500 random bases (seeds 1 to 1,500): 10 to 28 bars each, nuts common or rare, stock scarce, mixed or plentiful, batches that are not Released, expired or not made yet, recent boxes, boxes already planned with an unsuitable bar, and allergen hints the fields do not list (almonds in the ingredient text, a nut word in the name, a Hazelnut flavor note, Milk, White or Ruby chocolate without Milk, a "shared equipment" sentence). Because the script made the data, it knows the true allergens of every bar and checks every box against them, plus the stock, repeat, dislike and box count rules and that a box which is not full had nothing suitable left in stock. As a control, the same script runs against a copy of the planner that ignores May Contain Traces and skips the final check; it must find nut-free breaches, which shows the check can fail.

The workflow itself has two layers too: the planner, and before any write a final check that reads every chosen bar's allergen fields straight from its Airtable record again.

The mocks are strict, so a pass means the request would be accepted for real:

- **Airtable** allows 5 requests per second per base. The 6th inside one second gets 429 and the base stays blocked for 30 seconds, as Airtable documents. The limiter was on in every scenario. The busiest second of a normal run had 2 requests, and with two runs at once 4. The mock also refuses more than 10 records per write, unknown field names, wrong value types, an unknown select option, a link to a record that does not exist, a bad formula and a merge field of the wrong type. The rate limit scenarios inject 429 answers on purpose; the 31 second waits the workflow does in response are real.
- **Slack** answers the plain text `ok`, and `channel_is_archived` with HTTP 410 for the "Slack is down" scenario.

**Where the expected boxes come from.** The test has its own Python version of the box rules (`expected_plan`), written separately from the planner in the workflow, and every box is compared with it field by field: subscriber, products, batches, Bars Planned, Bars Packed, Fill Status and Status. Before the final run, the JS planner and the Python version were also compared offline on eight data setups (the plain seed, `REPEAT_WINDOW=2`, `REPEAT_WINDOW=0`, the ingredient mismatch, the flavor note, chocolate type and duplicate SKU mismatches, nut-free scarcity, the safe bars with 1 unit each, a sold out batch) and gave the same boxes on all of them. The allergen checks do not use either: they judge each packed bar from its own product record (allergen fields, diet labels, ingredient text, name, flavor notes, chocolate type).

The test changes the seed only through the mock's direct helpers, inside the test: an extra ingredient, a removed ingredient list, a flavor note, a chocolate type, a second product record with the same SKU, a peanut trace, sold out batches, safe bars cut to 1 unit, a subscriber who becomes nut-free, a broken subscriber record, a cancelled box, open Sync Log rows. When the test runs after October 2026 it also moves the seed's box months forward, so the last boxes are always last month's (box months are text, which the mock does not shift). Nothing in `base/` or `tests/mocks/` was changed.

This automation does not call Claude, so there was no token counting check.

**Mutation check.** To show that the checks catch what they claim to, broken copies of the workflow were run through n8n, each through the scenarios meant to notice it. Every one was caught:

```
planner ignores May Contain Traces (the final check still reads it): CAUGHT (6 of 6 checks fail)
     FAIL  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:55:00Z-16481', 'message': 'Planning the sub /
     FAIL  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (BOX-2026-11-SUB-0001 missing; BOX-2026-11-SUB-0002 missing; BOX-2026-11-SUB-0003 miss
     FAIL  the stock is really short: 13 nut-safe bars with 1 unit each against 0 nut-free bars planned  (13 safe units, 0 wanted)
planner AND final check ignore May Contain Traces: CAUGHT (3 of 6 checks fail)
     FAIL  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (BOX-2026-11-SUB-0001 products ['DC-BAR-DARK-RASPBERRY', 'DC-BAR-MILK-CARAMEL', 'DC-BA
     FAIL  no nut-free box has a bar with tree nuts or peanuts, as an ingredient or as a trace, however short the safe ba  (hits [('SUB-0001', 'DC-BAR-DARK-RASPBERRY'), ('SUB-0001'
     FAIL  the nut and trace bars were there all along: other subscribers got them and every one still has stock  (39 nut bars to others, left {'DC-BAR-MILK-LAVENDER': 43, 'DC-BAR-
earlier boxes are not checked again: CAUGHT (2 of 23 checks fail)
     FAIL  the rerun lists exactly the bars that no longer suit (a new peanut trace on DARK-85, SUB-0003 now nut-free), f  (*Subscription boxes for 2026-11* (Run Now, run boxes-202
     FAIL  nothing in the base changes (the packing team may be at work); the Sync Log row is Partial  ({"Run ID": "boxes-2026-11-2026-10-22T07:50:18Z-16463", "Automation": "04 Sub
short boxes of an earlier run are not listed: CAUGHT (1 of 8 checks fail)
     FAIL  Slack says 9 planned this run and 4 before, and still lists all 3 short boxes, SUB-0004 from the failed run to  (*Subscription boxes for 2026-11* (Run Now, run boxes-202
flavor note signals off: CAUGHT (3 of 5 checks fail)
     FAIL  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (BOX-2026-11-SUB-0001 Bars Packed=5 want 4; BOX-2026-11-SUB-0001 products ['DC-BAR-DAR
     FAIL  sea salt (Hazelnut flavor note, no nut allergen) reaches no nut-free box; espresso (Milk chocolate, no Milk li  (sea salt ['SUB-0001', 'SUB-0005', 'SUB-0006', 'SUB-0007'
     FAIL  Slack lists all four problems under data to check  (her 17 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 2 in the last box (DC-BAR-
lock never blocks: CAUGHT (4 of 8 checks fail)
     FAIL  both calls get 202; exactly one run plans, the other stops before reading and names the first  ()
     FAIL  no 429, at most 4 Airtable requests in any second, the second run made only its 3 Sync Log calls, both rows cl  (96 requests, busiest second 4, 429s 0)
     FAIL  both calls get 202; exactly one run plans, the other stops before reading and names the first  ()
box never looked up after a failed save: CAUGHT (4 of 8 checks fail)
     FAIL  Airtable saved the box 3 times but answered 500; the run finds it and succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:52:34Z-
     FAIL  the box was looked up instead of given up: no stock given back, no alert, one box record  (3 lost answers)
     FAIL  all 13 boxes match one uninterrupted run, no bar breaks its subscriber's needs  (BOX-2026-11-SUB-0006 missing; BOX-2026-11-SUB-0007 missing; BOX-2026-11-SUB-0008 missing
undo writes the reserved numbers: CAUGHT (3 of 8 checks fail)
     FAIL  the run stops, and the reservation that went through is set back: only the first 2 boxes hold stock  (:rotating_light: *TEST subscription-box-curation (temporary)* faile
     FAIL  no double reservation: the result equals one uninterrupted run  ()
     FAIL  all 13 boxes match one uninterrupted run, no bar breaks its subscriber's needs  (BOX-2026-11-SUB-0009 Bars Packed=2 want 3; BOX-2026-11-SUB-0009 products ['DC-BAR-DARK-E
```

The random base check was put through the same kind of test, offline with Node.js on 600 bases each: eleven broken copies of the planner (May Contain Traces ignored, peanuts not counted as nuts, no flavor note, name, ingredient or chocolate type cross-check, a bar without an ingredient list allowed, short boxes filled with anything, stock or batch units not counted down, earlier boxes not checked again), each with the workflow's final check on and off. All 22 were caught. With the final check on, no plan that put a bar with a nut allergen or nut trace listed in its fields into a nut-free box got through: the final check stopped it before any write. The final check reads only the allergen fields, so a breach through a hint outside them (a peanut in the ingredient text that the fields do not list) was caught by the random base check alone, which is why the planner's cross-checks matter.

## Found and fixed

Found by the first builder while testing:

| What the test or review showed | Fix |
|---|---|
| Run Now for the current month planned a second box for every subscriber whose box for the month was in Box History without a Box Shipment (the seed's October boxes, like any box packed by hand). On the seed, 13 more October boxes and 62 bars. | Such a subscriber gets no new box, and Slack lists them with the way to plan one anyway. Covered by "Run Now for this month". |
| The Box History rows of a Cancelled Box Shipment still counted as received. | Rows linked to a Cancelled box are ignored for the repeat check. Covered by "A cancelled box does not count as received". |
| A 429 that outlasts n8n's retries stopped the run. n8n 2.19.5 caps retries at 5 tries 5 seconds apart, while Airtable blocks a base for 30 seconds. | After its 3 tries, a read or a box's pass waits 31 seconds and starts again, at most twice per step. Covered by the four rate limit scenarios. |
| A read that stops at its page limit would have been planned from a partial list. | The planner refuses a read whose last page still has an offset, before any write. Covered by "More records than one run reads". |

Found in the review of the finished workflow:

| What the review showed | Fix |
|---|---|
| Two Run Now calls at the same moment (a double click) made both runs fail: together they sent more than 5 requests a second, Airtable blocked the base, and after two 31 second waits both stopped with an alert blaming "another app". Nothing was planned. A second call 1.5 or 6 seconds later failed the same way after about 70 seconds. Measured on the original workflow: 18 answers of 429 each time. The original README also named the worse case: a second run that reads the stock between a box's reservation and its save reserves that box twice. | One run at a time: each run upserts its Sync Log row, waits 2 seconds and stops when an earlier curation run is still open. Pacing went from 250 to 500 ms, so two runs together stay at 4 requests a second. Covered by "Run Now twice at the same moment" and "Run Now again 4 seconds later": one run plans, the other stops after 3 Sync Log calls, no 429. |
| Two runs started in the same second got the same Run ID and shared one Sync Log row. | The Run ID ends with the n8n execution id. |
| A failed run left no Sync Log row, although Sync Log is "one row per automation run". | Every failure after the start goes through one path that closes the row as Failed with the reason, then stops. Checked in every failure scenario (Status, Finished At, Errors, Items Changed and the alert text). |
| When Airtable saved a box but the answer was lost (a timeout, a proxy error), the run gave the box's stock back although the box existed, so the box was promised without stock. | Before giving stock back, the run looks the box up; if it is there, the pass carries on. Covered by "A box is saved but the answer is lost". |
| When a reservation went through but its answer was lost, the run said "nothing was saved" and the stock stayed reserved for a box that did not exist; the next run reserved it again. | A failed reservation sets the box's batches back to their numbers from the start of the run. Covered by "A reservation is saved but the answer is lost": after the rerun the stock equals one clean run. |
| A product whose only nut signal is a flavor note (Hazelnut, Almond, Peanut), or a Milk or White chocolate bar without Milk listed, passed the safety rules. Two product records with the same SKU could make the final safety check stop the whole run. | The name, the flavor notes and the chocolate type are cross-checked like the ingredients (they can only add an allergen). Duplicate or missing SKUs are left out and reported. Covered by "Allergen data that does not add up (flavor note, chocolate type, duplicate SKU)". |
| Nothing proved that a nut-free subscriber gets an empty box rather than an unsafe bar when the safe bars run out. | New scenario "Nut-free subscribers when only nut bars are left": every nut-safe bar sold out except one that now carries only a peanut trace in May Contain Traces. All 5 nut-free boxes are Empty; the others still get bars. |
| The Slack summary stopped listing after 25 boxes per section, so a large shortfall month did not name every subscriber. | Every subscriber is listed; only a message that would pass Slack's 40,000 characters is cut short. |
| A run whose Slack summary failed was logged as Success. | It is logged as Partial with 1 error. |
| After a failure the alert said "run again", and a plain Run Now in the next calendar month would plan the wrong month. | The alert names the month to run again. |
| Some checks could pass with a broken workflow: the no-ingredient bar check passed when no box got the bar, the read count was "at least 5", the shortfall reasons were only partly compared. | Those checks are exact now. |

Found in the second review, which looked at allergen safety above all:

| What the review showed | Fix |
|---|---|
| A box planned earlier was never looked at again. If a product gained a nut trace after the boxes were planned (a supplier change), or a subscriber added Nut-Free, every later run stayed silent, and a nut-free subscriber could receive a nut-trace bar. | Every run checks the bars of this month's boxes (any status but Cancelled) and the bars recorded for the month in Box History against the subscriber's needs as they are now, and lists every bar that does not suit at the top of the Slack message, to take out by hand. The box is not changed (the packing team may be at work); the Sync Log row says Partial. Products is now read for every bar, so a bar set to Discontinued after a recall can still be checked. Covered by "A planned box is checked again after the data changed" and by the random base check. |
| After a failed run, its saved boxes that were not full were never reported. The failed run sends no summary, and the next run listed only the boxes it planned itself, so the packing team never heard why SUB-0004's box was short. | The summary lists every box of the month that is not full, with its reason, and marks the ones planned by an earlier run. Covered by "Box save fails mid-run" and the rerun. |
| The "separate" final safety check reused the planner's own reading of each product, so a bug in that reading would have passed it too. | The final check now also reads each chosen bar's Contains Allergens, May Contain Traces, Diet Labels and Ingredients straight from the record. |
| A Ruby chocolate bar without Milk listed counted as dairy-free and vegan. Ruby chocolate is made with milk powder. | Ruby counts as a milk signal, like Milk and White chocolate. The seed's ruby bar lists Milk, so no box changed. |
| The milk scan ignored a "hazelnut cream" or "almond cream" in the ingredients as a plant cream. A nut cream (a spread) often contains milk. | Only plant milks and drinks (oat milk, almond drink) and coconut cream are ignored. |
| Allergen safety was only checked in the allergen scenarios, no scenario had the safe bars run short (only run out), and nothing showed the rule holds beyond the seed's 19 bars. | Every box of every scenario is judged from its product records, a new scenario leaves every nut-safe bar 1 unit, and the planner code from the workflow runs on 1,500 random bases in n8n with a control that must fail. |
| The README still had unfilled placeholders for the samples and the test table, and docs/test-report.md described an older workflow. | Both are filled from the final run below. |

## Results

The output of the final run:

```
Test month 2026-11 (today 2026-10-22 UTC, box months moved by 0). Mock on http://host.docker.internal:8794.

Workflow file
  PASS  every HTTP Request node retries 3 times, 2 s apart, and its error output is wired to a handler  (15 HTTP nodes, bad [])
  PASS  the 6 reads (open runs and 5 tables) use POST listRecords with a formula, named fields, pageSize 100 and offset pagination 500 ms apart  (6 reads, bad [])
  PASS  every Airtable call uses the exact base URL and token header and comes right after a Wait of at least 500 ms (2 calls a second, so two runs at once stay under Airtable's 5)  (14 Airtable nodes, unpaced [])
  PASS  every IF condition uses an operator that exists in IF v2 (single value operators marked)  (17 IF nodes, bad [])
  PASS  no em or en dash anywhere, node names read "Type: Name", the schedule is monthly on the 20th at 08:00  (76 nodes)
  PASS  the run claims its Sync Log row before any read, and every failure after the start closes that row before the one Stop node  (stops ['Stop: Bad Run Now Settings', 'Stop: Bad Settings', 'Stop: Run Failed', 'Stop: Slack Failed', 'Stop: Sync Log Failed'], failure path fed by [('Code: History Failed', 0), ('Code: Write Failed', 0), ('IF: Clear To Run?', 1), ('IF: Plan Failed?', 0), ('IF: Wait Out Read Limit?', 1)])

Run Now security
  PASS  no secret header and a wrong secret both get 401  (401 {'accepted': False, 'error': 'Missing or wrong X-Run-Now-Secret header.'} / 401)
  PASS  nothing is read or written, and no alert  (0 Airtable calls)

Bad month
  PASS  a malformed month, a past month (in the query string), a month too far ahead and a number get 400  (400 {'accepted': False, 'error': 'month "2026-13" is not in the form YYYY-MM, for example 2026-11'} / 400 {'accepted': False, 'error': 'month 2026-09 cannot be curated now. Use this month (2026-10) or next month (2026-11), in UTC'} / 400 / 400)
  PASS  nothing is read or written, and no alert  (0 Airtable calls)

First run for the month
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:56:50Z-16490', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  202 body names the month and a run id made of the month, the start time and the n8n execution id  ({'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:56:50Z-16490', 'message': 'Planning the subscription boxes for 2026-11. The summary goes to Slack when the run is done.'})
  PASS  one box per Active subscriber (13), none for the paused or cancelled one  (13 boxes)
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  seed facts: 10 full, SUB-0004 1 of 6, SUB-0009 3 of 4, SUB-0011 1 of 4  ([('SUB-0004', 1, 6), ('SUB-0009', 3, 4), ('SUB-0011', 1, 4)])
  PASS  shortfall reasons say exactly why (diet, dislike, last box with the SKUs)  (1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK). | 3 of 4 bars packed. The other 16 subscription bars: 12 not suitable for Vegan and Nut-Free; 4 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-ORANGE, DC-BAR-OAT-MILK). | 1 of 4 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).)
  PASS  allergen safety: no packed bar breaks its subscriber's needs (fields, ingredients, name, notes, type checked)  (63 bars checked)
  PASS  nut trace traps (raspberry, matcha, lavender) never reach a nut-free box  (5 nut-free boxes, 21 bars, hits [])
  PASS  no bar from the subscriber's last box, no bar twice in a box  ([] [])
  PASS  flavor variety: at least 3 flavor families in every full box of 4 or more  (lowest share of distinct families 0.67 (BOX-2026-11-SUB-0001))
  PASS  Quantity Reserved went up by exactly one per packed bar on the batch the packing list names  (63 units on 19 batches)
  PASS  no batch over-reserved; only Released batches with more than 14 days left at ship date were used  ([])
  PASS  scarce stock respected: DARK-MINT 3 of 3 (to SUB-0002, SUB-0003 and SUB-0009), DARK-85 at most 4  ({'DC-BAR-DARK-MINT': 3, 'DC-BAR-DARK-85': 3, 'DC-BAR-DARK-CHILI': 2})
  PASS  one Box History row per bar, linked to its box, subscriber and product  (63 rows for 63 bars)
  PASS  one Slack summary with the counts and every box that is not full  (*Subscription boxes for 2026-11* (Run Now, run boxes-2026-11-2026-10-22T07:56:50Z-16490)
13 active subscribers. Planned this run: 13 boxes (10 full, 3 partial, 0 empty), 63 bars reserved from 19 batches. Planned before: 0 boxes.

*Not fully filled (3)*
• SUB-0004 Derek Olsen, 1 of 6 bars packed. The)
  PASS  one Sync Log row for the run: Run Now, Partial, 13 checked, 13 changed, 0 errors, closed with a Finished At  ({"Run ID": "boxes-2026-11-2026-10-22T07:56:50Z-16490", "Automation": "04 Subscription Boxes", "Trigger": "Run Now", "Started At": "2026-10-22T07:56:50.000Z", "Finished At": "2026-10-22T07:57:17.211Z",)
  PASS  the first request claims the run: a Sync Log upsert with no Status and no Finished At, then the open runs are listed  ({"Run ID": "boxes-2026-11-2026-10-22T07:56:50Z-16490", "Automation": "04 Subscription Boxes", "Trigger": "Run Now", "Started At": "2026-10-22T07:56:50Z", "Detai)
  PASS  Airtable pacing: no 429, never more than 2 requests in any second, every write 10 records or fewer  (48 requests, busiest second 2, largest write 8)
  PASS  exactly 7 reads (all naming their fields) and 41 writes: the claim, one reserve, one box and one history upsert per box, the Sync Log  (7 reads, 41 writes)
  PASS  no secret in any node output (Airtable token, Slack webhook URL)  (752016 characters of run data checked)

Rerun for the same month (no month given)
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:57:17Z-16491', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  without a month, Run Now plans next month (UTC)  ({'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:57:17Z-16491', 'message': 'Planning the subscription boxes for 2026-11. The summary goes to Slack when the run is done.'})
  PASS  no new box, no reservation, no history row: the only writes are the run's Sync Log row (claim and close)  ([('Sync Log', 'upsert'), ('Sync Log', 'upsert')])
  PASS  Slack says nothing new and 13 planned before, and lists the 3 that are not full again with their reasons; the Sync Log row is closed as Partial  (*Subscription boxes for 2026-11* (Run Now, run boxes-2026-11-2026-10-22T07:57:17Z-16491)
13 active subscribers. Nothing new to plan this run, no stock was reserved. Planned before: 13 boxes (3 not full).

*Not fully filled (3)*
• SUB-0004 Derek Olsen, 1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BA)

A planned box is checked again after the data changed
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:57:24Z-16492', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  the rerun lists exactly the bars that no longer suit (a new peanut trace on DARK-85, SUB-0003 now nut-free), first in the message  (• BOX-2026-11-SUB-0003 for SUB-0003 Priya Raman has DC-BAR-DARK-RASPBERRY, which does not suit Nut-Free. Take it out by hand before packing. | • BOX-2026-11-SUB-0005 for SUB-0005 Elena Rossi has DC-BAR-DARK-85, which does not suit Nut-Free. Take it out by hand before packing.)
  PASS  nothing in the base changes (the packing team may be at work); the Sync Log row is Partial  ({"Run ID": "boxes-2026-11-2026-10-22T07:57:24Z-16492", "Automation": "04 Subscription Boxes", "Trigger": "Run Now", "Started At": "2026-10-22T07:57:24.000Z", "F)

Slack is down
  PASS  Slack is tried 3 times, the Sync Log row is still closed with 1 error, then the run fails with an alert  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Slack Failed* | The Slack summary for 2026-11 could not be posted (HTTP 410). The boxes and the Sync Log row are saved. Check SLACK_PACKING_WEBHOOK_URL. | http://localhost:5678/workflow/0c01lgnAnPIEat11/executions/16493)

Sync Log write fails
  PASS  the Slack summary still goes out first, then the run fails with an alert that says the row stays open  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Sync Log Failed* | The Sync Log row boxes-2026-11-2026-10-22T07:57:42Z-16495 could not be closed (HTTP 500). The boxes are saved and the Slack summary was posted. New curation runs wait for this row for up to 60 minutes. )

A Sync Log row left open holds new runs back
  PASS  the next run stops after 3 Sync Log calls and names the open row; nothing else is read or written  ([('Sync Log', 'upsert'), ('Sync Log', 'list'), ('Sync Log', 'upsert')] | :rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Another curation run (boxes-2026-11-2026-10-22T07:57:42Z-16495, started 2026-10-22 07:57 UTC) is still running, so this run stopped before reading )
  PASS  with a Status set on that row by hand, an open row 2 hours old (a run that died) and an open row of another automation, the next run goes ahead  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:57:57Z-16499', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  the old and the other automation's rows are left alone

Run Now twice at the same moment
  PASS  both calls get 202; exactly one run plans, the other stops before reading and names the first  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Another curation run (boxes-2026-11-2026-10-22T07:58:04Z-16500, started 2026-10-22 07:58 UTC) is still running, so this run sto)
  PASS  the boxes are exactly those of one run, no bar breaks its subscriber's needs  (13 boxes)
  PASS  stock reserved once: Quantity Reserved equals one run, 63 history rows
  PASS  no 429, at most 4 Airtable requests in any second, the second run made only its 3 Sync Log calls, both rows closed  (51 requests, busiest second 4, 429s 0)

Run Now again 4 seconds later
  PASS  both calls get 202; exactly one run plans, the other stops before reading and names the first  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Another curation run (boxes-2026-11-2026-10-22T07:58:31Z-16503, started 2026-10-22 07:58 UTC) is still running, so this run sto)
  PASS  the boxes are exactly those of one run, no bar breaks its subscriber's needs  (13 boxes)
  PASS  stock reserved once: Quantity Reserved equals one run, 63 history rows
  PASS  no 429, at most 4 Airtable requests in any second, the second run made only its 3 Sync Log calls, both rows closed  (51 requests, busiest second 4, 429s 0)

Box save fails mid-run, then the next run resumes
  PASS  the run tries the box 3 times, asks Airtable whether it was saved after all, gives the stock back once and stops  (error at Stop: Run Failed, 3 tries)
  PASS  the alert names the box, says the reservation was undone, how many boxes are saved and which month to run  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | BOX-2026-11-SUB-0005 could not be saved (HTTP 500 SERVER_ERROR). Its stock reservation was undone. 4 boxes before it are saved. Run again for 2026-11 to carry on, saved boxes are skipped. | http://localhost:)
  PASS  the run's Sync Log row is closed as Failed with 4 boxes changed and the alert text  (Sync Log Failed changed 4 finished True)
  PASS  only the 4 earlier boxes exist, and the stock reserved is exactly theirs  (['BOX-2026-11-SUB-0001', 'BOX-2026-11-SUB-0002', 'BOX-2026-11-SUB-0003', 'BOX-2026-11-SUB-0004'])
  PASS  after the outage, the next run succeeds (the closed row does not hold it back)  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T07:59:17Z-16508', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  the result is the same as one uninterrupted run (boxes, products, batches), no bar breaks its subscriber's needs  (13 boxes)
  PASS  no double reservation: stock reserved equals one uninterrupted run
  PASS  Slack says 9 planned this run and 4 before, and still lists all 3 short boxes, SUB-0004 from the failed run too (that run never sent its summary)  (*Subscription boxes for 2026-11* (Run Now, run boxes-2026-11-2026-10-22T07:59:17Z-16508)
13 active subscribers. Planned this run: 9 boxes (7 full, 2 partial, 0 empty), 44 bars reserved from 18 batches. Planned before: 4 boxes (1 not full).

*Not fully filled (3)*
• SUB-0009 Lucia Gomez, 3 of 4 bars packed. The other 16 subscription bars: 12 not suitable for Vegan and Nut-Free; 4 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-ORANGE, DC-BAR-OAT-MILK).
• SUB-0011 Aisha Khan, 1 of 4 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked fla)

Stock reservation fails
  PASS  the run stops before saving the third box and the alert says what is saved  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Stock could not be reserved for BOX-2026-11-SUB-0003 (HTTP 503 RETRIABLE_ERROR). Nothing is saved for this box. 2 boxes before it are saved. Run again for 2026-11 to carry on, saved boxes are skipped. | http)
  PASS  only the first two boxes hold stock (the third box's batches were set back once), the Sync Log row is closed as Failed  (Sync Log Failed changed 2 finished True)

Box save and the undo both fail
  PASS  the alert lists the batches to fix by hand  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | BOX-2026-11-SUB-0002 could not be saved (HTTP 500 SERVER_ERROR) and its stock reservation could not be undone (HTTP 500 SERVER_ERROR). Lower Quantity Reserved by 1 on B-DCC-260801, B-DES-260925, B-DMI-260811, B-MHZ-260712, B-MLH-260816, B-OAT-260915, B-RCH-260930, B-WMA-260612 by hand, then run again. 1 bo)
  PASS  the base matches the alert: SUB-0001 saved, SUB-0002 has no box but still holds its stock

Box History fails, then the next run adds the missing rows
  PASS  the run stops and the alert says the box and stock are saved  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Box History rows for BOX-2026-11-SUB-0003 could not be saved (HTTP 500 SERVER_ERROR). The box and its stock reservation are saved, and the next run adds the missing rows. 2 boxes before it are saved. Run aga)
  PASS  the Sync Log row is closed as Failed with 3 boxes changed  (Sync Log Failed changed 3 finished True)
  PASS  the next run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:00:26Z-16516', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  SUB-0003 gets its 4 missing rows, linked to its existing box, and no second reservation  (4 rows)
  PASS  all 13 boxes match one uninterrupted run, no bar breaks its subscriber's needs  (13 boxes)
  PASS  Slack mentions the 4 added rows  (ox (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).

*Used up by this run:* DC-BAR-DARK-MINT

Added 4 missing Box History rows for boxes planned before.)

A box is saved but the answer is lost
  PASS  Airtable saved the box 3 times but answered 500; the run finds it and succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:00:48Z-16517', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  the box was looked up instead of given up: no stock given back, no alert, one box record  (3 lost answers)
  PASS  all 13 boxes match one uninterrupted run, no bar breaks its subscriber's needs  (13 boxes)
  PASS  stock reserved once and SUB-0005's history rows written

A reservation is saved but the answer is lost
  PASS  the run stops, and the reservation that went through is set back: only the first 2 boxes hold stock  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Stock could not be reserved for BOX-2026-11-SUB-0003 (HTTP 500 SERVER_ERROR). Nothing is saved for this box. 2 boxes before it are saved. Run again)
  PASS  the next run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:01:40Z-16520', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  no double reservation: the result equals one uninterrupted run
  PASS  all 13 boxes match one uninterrupted run, no bar breaks its subscriber's needs  (13 boxes)

A field was renamed in Airtable
  PASS  the run stops and the alert names the table and the Airtable error  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Reading Products from Airtable failed with HTTP 422 UNKNOWN_FIELD_NAME. No box or stock was changed. Check AIRTABLE_TOKEN, AIRTABLE_BASE_ID and that the field names match base/schema.json, then run again. | )
  PASS  the only writes are the run's own Sync Log row, closed as Failed; no box  (Sync Log Failed changed 0 finished True)

Rate limit during a read (another app used up the base)
  PASS  n8n retries 3 times, the run waits out the 30 second penalty and succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:02:14Z-16523', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  after a wait of more than 30 seconds the run starts again from its Sync Log row (the same row, updated)  ([('Sync Log', 'upsert', 200), ('Sync Log', 'list', 200), ('Subscribers', 'list', 200), ('Products', 'list', 429), ('Products', 'list', 429), ('Products', 'list', 429), ('Sync Log', 'upsert', 200), ('Sync Log', 'list', 200), ('Subscribers', 'list', 200)], waited 32 s)
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)

Rate limit while saving a box
  PASS  the pass waits 31 seconds, starts again and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:03:20Z-16524', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  the repeated pass sent the same absolute stock numbers again, so nothing is reserved twice  (2 reserve requests for SUB-0003)
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)

Rate limit that does not go away
  PASS  after 2 waits (3 rounds of 3 tries) the run stops, closes its Sync Log row after a last wait, and alerts  (16 requests, Sync Log Failed changed 0 finished True | :rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | Reading Subscribers from Airtable kept hitting the rate limit (HTTP 429 RATE_LIMIT_REACHED), also after waiting out the 30 second penalty twice. No)

Rate limit on a box that does not go away
  PASS  after 2 waits the box is looked up, gives its stock back and the run stops with an alert  (2 waits, boxes ['SUB-0001', 'SUB-0002'] | :rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | BOX-2026-11-SUB-0003 could not be saved (HTTP 429 rate limit). Its stock reservation was undone. 2 boxes before it are saved. Run again for 2026-11 to carry on, saved )

A page token expires in the middle of a read
  PASS  the read is retried from the first page and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:08:20Z-16529', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  Box History was read again in full (2 pages, then 2 pages), so no history row was missed  ([200, 422, 200, 200])
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)

Allergen data that does not add up (ingredients)
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:08:50Z-16530', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  the orange bar (almonds in the text, no nut allergen listed) reaches no nut-free box, but others still get it  ({'SUB-0001': False, 'SUB-0002': False, 'SUB-0003': False, 'SUB-0004': False, 'SUB-0005': False, 'SUB-0006': True, 'SUB-0007': False, 'SUB-0008': False, 'SUB-0009': False, 'SUB-0010': True, 'SUB-0011': False, 'SUB-0012': False, 'SUB-0013': False})
  PASS  the bar with no ingredient list goes only to subscribers without dietary needs (and does go to some)  (['SUB-0006', 'SUB-0012'])
  PASS  Slack lists both products under data to check  ( (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).

*Product data to check (2)*
• DC-BAR-DARK-ORANGE: the ingredients mention "almonds" but Tree Nuts is not listed. It is kept out of boxes for that need until the record is fixed.
• DC-BAR-WHITE-COOKIE: no ingredient list, so it only goes to subscribers without dietary needs.

*Used up by this run:* DC-BAR-DARK-MINT)

Allergen data that does not add up (flavor note, chocolate type, duplicate SKU)
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:09:17Z-16531', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  sea salt (Hazelnut flavor note, no nut allergen) reaches no nut-free box; espresso (Milk chocolate, no Milk listed) no dairy-free or vegan box; both still go to others  (sea salt ['SUB-0006', 'SUB-0007', 'SUB-0010', 'SUB-0011'], espresso ['SUB-0002', 'SUB-0006', 'SUB-0007', 'SUB-0010', 'SUB-0012', 'SUB-0013'])
  PASS  neither of the two records with the SKU DC-BAR-RUBY-CHERRY goes in a box
  PASS  Slack lists all four problems under data to check  (ck (4)*
• DC-BAR-RUBY-CHERRY: 2 product records have this SKU, so none of them goes in a box until the duplicate is removed.
• DC-BAR-DARK-ESPRESSO: it is Milk chocolate but Milk is not listed. It is kept out of boxes for that need until the record is fixed.
• DC-BAR-DARK-ESPRESSO: it is labelled Vegan but it is Milk chocolate. It is kept out of boxes for that need until the record is fixed.
• DC-BAR-DARK-SEASALT: it has the flavor note Hazelnut but Tree Nuts is not listed. It is kept out of boxes for that need until the record is fixed.

*Used up by this run:* DC-BAR-DARK-85, DC-BAR-DARK-MINT)

Nut-free subscribers when only nut bars are left
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:09:45Z-16532', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  all 5 nut-free boxes are Empty and say the safe bars are out of stock; no nut or trace bar is used to fill them  (SUB-0001 Empty 0 of 6 bars packed. The other 19 subscription bars: 7 not su; SUB-0005 Empty 0 of 4 bars packed. The other 19 subscription bars: 10 not s; SUB-0008 Empty 0 of 4 bars packed. The other 19 subscription bars: 7 not su; SUB-0009 Empty 0 of 4 bars packed. The other 19 subscription bars: 13 not s; SUB-0013 Empty 0 of 4 bars packed. The other 19 subscription bars: 7 not su)
  PASS  the other subscribers still get bars, the peanut-trace DARK-85 included; no history or stock for the empty boxes  (7 other boxes with bars)

Nut-free subscribers when the safe bars run short
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:10:05Z-16533', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  the stock is really short: 13 nut-safe bars with 1 unit each against 22 nut-free bars planned  (13 safe units, 22 wanted)
  PASS  no nut-free box has a bar with tree nuts or peanuts, as an ingredient or as a trace, however short the safe bars are  (hits [], 8 bars in 5 nut-free boxes)
  PASS  the nut-free boxes that could not be filled are Partial or Empty and say why; no safe bar is used twice  (SUB-0005 Partial 2/4; SUB-0008 Empty 0/4; SUB-0009 Empty 0/4; SUB-0013 Empty 0/4)
  PASS  the nut and trace bars were there all along: other subscribers got them and every one still has stock  (22 nut bars to others, left {'DC-BAR-WHITE-MATCHA': 51, 'DC-BAR-DARK-ALMOND': 75, 'DC-BAR-MILK-PEANUT': 82, 'DC-BAR-MILK-LAVENDER': 47, 'DC-BAR-MILK-HAZELNUT': 245, 'DC-BAR-DARK-RASPBERRY': 51})

Subscriber records a person must fix
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:10:27Z-16534', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  an unknown dietary need, a missing plan and a 12 bar plan are not guessed: no box, listed in Slack  (iption bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).

*Not planned, the subscriber record needs a person (3)*
• SUB-0010 Tom Becker, the plan "12 Bars" has more than 10 bars, which this workflow does not plan.
• SUB-0012 Ben Carter, the dietary need "Soy-Free" has no safety rule yet, so a person must pick these bars.
• SUB-0013 Sofia Marino, no plan is set.

*Used up by this run:* DC-BAR-DARK-MINT)
  PASS  after the plan is fixed, the next run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:10:49Z-16535', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  only SUB-0013 gets a box, from the stock that is left

Sold out bar
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:10:57Z-16536', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  SUB-0011 (only sea salt was left for them) gets an Empty box that says why  (0 of 4 bars packed. The other 19 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK); 1 out of stock (DC-BAR-DARK-SEASALT).)
  PASS  an Empty box writes the box only: 13 box upserts, but 12 reservations and 12 history upserts  ({'Box Shipments': 13, 'Production Batches': 12, 'Box History': 12})

Run Now for this month (the bars are already in Box History)
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-10', 'runId': 'boxes-2026-10-2026-10-22T08:11:23Z-16537', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every Active subscriber already has this month's bars in Box History (recorded before this workflow), so no box is planned, nothing is reserved, and the only writes are the Sync Log row  (2026-10, 0 boxes, writes [('Sync Log', 'upsert'), ('Sync Log', 'upsert')])
  PASS  Slack lists all 13 with their bar counts and says how to plan one anyway; the Sync Log row is Partial  (*Subscription boxes for 2026-10* (Run Now, run boxes-2026-10-2026-10-22T08:11:23Z-16537)
13 active subscribers. Nothing new to plan this run, no stock was reserved. Planned before: 0 boxes.

*Bars for 2026-10 already in Box History without a Box Shipment, so no new box (13)*
• SUB-0001 Maya Chen (6 bars)
• SUB-0002 Jordan Blake (8 bars)
• SUB-0003 Priya Raman (4 bars)
• SUB-0004 Derek Olsen (4 bars)
• SUB-0005 Elena )

A cancelled box does not count as received
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:11:29Z-16538', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  SUB-0001's last box was cancelled, so the repeat check uses the box before it: none of those bars, and bars from the cancelled box can come back  (packed ['DC-BAR-DARK-85', 'DC-BAR-DARK-CHILI', 'DC-BAR-DARK-ESPRESSO', 'DC-BAR-MILK-CLASSIC', 'DC-BAR-MILK-ESPRESSO', 'DC-BAR-WHITE-STRAWBERRY']; from the cancelled box ['DC-BAR-DARK-85', 'DC-BAR-DARK-CHILI', 'DC-BAR-DARK-ESPRESSO', 'DC-BAR-MILK-CLASSIC', 'DC-BAR-MILK-ESPRESSO', 'DC-BAR-WHITE-STRAWBERRY'])

REPEAT_WINDOW=2
  PASS  Run Now answers 202 and the run succeeds  (202 {'accepted': True, 'month': '2026-11', 'runId': 'boxes-2026-11-2026-10-22T08:11:58Z-16539', 'message': 'Planning the sub / run success at IF: Slack Failed? )
  PASS  every box matches the expected plan exactly, no bar breaks its subscriber's needs  (13 boxes)
  PASS  no bar from either of the last 2 boxes; SUB-0004 had all 5 of its bars in them, so its box is Empty  (0 of 6 bars packed. The other 19 subscription bars: 14 not suitable for Dairy-Free; 5 in the last 2 boxes (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).)

Settings problem
  PASS  Run Now gets 500 with the reason, the run fails with an alert, nothing is read or written  (500 {'accepted': False, 'error': 'Settings problem. REPEAT_WINDOW must be a whole number from 0 to 12.'} | :rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Bad Run Now Settings*
Settings problem. REPEAT_WINDOW must be a whole number from 0 to 12.
http://localhost:5678/workflow)

More records than one run reads
  PASS  with the page limit at 1, the 2 page Box History read stops the run before any box or stock write  (:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed* | The plan was not made, no box or stock was changed. Box History has more records than one run reads. Raise maxRequests on its read node, then run a)

Random bases, the planner code from the workflow run in n8n
  PASS  1500 random bases (seeds 1 to 1500) with scarce stock, nut bars everywhere and hidden allergen hints: no nut-free subscriber ever gets a bar with tree nuts or peanuts, as an ingredient or a trace  ({"runs": 1500, "nutFreeBoxes": 5695, "nutFreeBars": 8409, "nutFreeShort": 5224, "nutFreeEmpty": 2409, "hiddenNutProducts": 4152, "nutBarsToOthers": 8223, "nutFreeBreaches": 0})
  PASS  every other rule holds in every plan: dairy, gluten and vegan needs, no ingredient list, dislikes, repeats, one unit per bar on a usable batch, no batch over-reserved, a short box had nothing suitable left, one box per subscriber, every unsuitable bar in an earlier box reported  ({"boxes": 11278, "bars": 23972, "shortBoxes": 9294, "plannedBefore": 971, "conflictsExpected": 1952, "violationCount": 0} )
  PASS  control: the same check run against a copy of the planner that ignores May Contain Traces finds nut-free breaches  (189 breaches in 300 bases, for example seed 5: NUT-FREE BREACH BOX-2026-11-SUB-5-003 has DC-RND-5-13 (Milk, Soy, Peanuts))

Allergen safety, every scenario together
  PASS  every box of the 17 scenarios that planned boxes was judged from its product records: no bar breaks its subscriber's needs, and no nut-free box has a nut or nut trace bar  (221 boxes with 955 bars, of them 85 nut-free boxes with 302 bars (30 not full); plus 5695 nut-free boxes in 1500 random bases; breaches [])

Cleanup
  PASS  every temporary workflow was deleted  (0 left)

133/133 checks passed

Total time 946 s
```

## Samples from the first run

The Run Now answer, the request that claims the run, the Slack summary, a partial box and a full box as saved in Airtable, the exact upsert and reservation requests, and the alert when a box cannot be saved. Record ids are the mock base's.

```
Run Now response: {"accepted": true, "month": "2026-11", "runId": "boxes-2026-11-2026-10-22T07:56:50Z-16490", "message": "Planning the subscription boxes for 2026-11. The summary goes to Slack when the run is done."}

First request (the run claims its Sync Log row): {"performUpsert": {"fieldsToMergeOn": ["Run ID"]}, "records": [{"fields": {"Run ID": "boxes-2026-11-2026-10-22T07:56:50Z-16490", "Automation": "04 Subscription Boxes", "Trigger": "Run Now", "Started At": "2026-10-22T07:56:50Z", "Details": "Running. Planning the boxes for 2026-11."}}], "typecast": false}

Slack summary:
*Subscription boxes for 2026-11* (Run Now, run boxes-2026-11-2026-10-22T07:56:50Z-16490)
13 active subscribers. Planned this run: 13 boxes (10 full, 3 partial, 0 empty), 63 bars reserved from 19 batches. Planned before: 0 boxes.

*Not fully filled (3)*
• SUB-0004 Derek Olsen, 1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).
• SUB-0009 Lucia Gomez, 3 of 4 bars packed. The other 16 subscription bars: 12 not suitable for Vegan and Nut-Free; 4 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-ORANGE, DC-BAR-OAT-MILK).
• SUB-0011 Aisha Khan, 1 of 4 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK).

*Used up by this run:* DC-BAR-DARK-MINT

Box Shipment BOX-...-SUB-0004 fields: {
 "Shipment ID": "BOX-2026-11-SUB-0004",
 "Subscriber": [
  "recyHKsiT79lGoJK7"
 ],
 "Box Month": "2026-11",
 "Products": [
  "recn8eBVCpnhbiowY"
 ],
 "Batches": [
  "recOodpR15XdWb9QU"
 ],
 "Packing List": "Box 2026-11 for Derek Olsen (SUB-0004), 6 Bars plan.\nDietary needs: Dairy-Free. Dislikes: none.\n1. DC-BAR-DARK-85, Dark 85% Single Origin Bar, batch B-D85-260821 (best before 2027-08-18)\nShort: 1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).\nEvery bar was checked against Contains Allergens, May Contain Traces, its ingredients, name, flavor notes and chocolate type.",
 "Bars Planned": 6,
 "Bars Packed": 1,
 "Fill Status": "Partial",
 "Shortfall Reason": "1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).",
 "Status": "Planned",
 "Box History": [
  "recYfswtsviS5rYEC"
 ]
}

Box Shipment BOX-...-SUB-0001 Packing List:
Box 2026-11 for Maya Chen (SUB-0001), 6 Bars plan.
Dietary needs: Nut-Free. Dislikes: Mint.
1. DC-BAR-DARK-ORANGE, Dark Orange Zest Bar, batch B-DOR-260905 (best before 2027-09-02)
2. DC-BAR-DARK-SEASALT, Dark 70% Sea Salt Bar, batch B-DSS-260910 (best before 2027-09-07)
3. DC-BAR-MILK-CARAMEL, Milk Salted Caramel Bar, batch B-MSC-260722 (best before 2026-12-06)
4. DC-BAR-OAT-MILK, Oat Milk Chocolate Bar, batch B-OAT-260915 (best before 2027-06-09)
5. DC-BAR-RUBY-CHERRY, Ruby Cherry Bar, batch B-RCH-260930 (best before 2027-05-30)
6. DC-BAR-WHITE-COOKIE, White Cookies and Cream Bar, batch B-WCC-260826 (best before 2027-04-20)
Every bar was checked against Contains Allergens, May Contain Traces, its ingredients, name, flavor notes and chocolate type.

Upsert request for SUB-0004: {
 "performUpsert": {
  "fieldsToMergeOn": [
   "Shipment ID"
  ]
 },
 "records": [
  {
   "fields": {
    "Shipment ID": "BOX-2026-11-SUB-0004",
    "Subscriber": [
     "recyHKsiT79lGoJK7"
    ],
    "Box Month": "2026-11",
    "Packing List": "Box 2026-11 for Derek Olsen (SUB-0004), 6 Bars plan.\nDietary needs: Dairy-Free. Dislikes: none.\n1. DC-BAR-DARK-85, Dark 85% Single Origin Bar, batch B-D85-260821 (best before 2027-08-18)\nShort: 1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK).\nEvery bar was checked against Contains Allergens, May Contain Traces, its ingredients, name, flavor notes and chocolate type.",
    "Bars Planned": 6,
    "Bars Packed": 1,
    "Fill Status": "Partial",
    "Status": "Planned",
    "Products": [
     "recn8eBVCpnhbiowY"
    ],
    "Batches": [
     "recOodpR15XdWb9QU"
    ],
    "Shortfall Reason": "1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK)."
   }
  }
 ],
 "typecast": false
}

First reservation request: {"records": [{"id": "recJfIKHSUytnA6r3", "fields": {"Quantity Reserved": 1}}, {"id": "recSg3XPtqnko5dkO", "fields": {"Quantity Reserved": 11}}, {"id": "recIxEi4J05FcRCCX", "fields": {"Quantity Reserved": 1}}, {"id": "recqqN6qQXNmqWvN5", "fields": {"Quantity Reserved": 5}}, {"id": "recT1Vfw06cNAirds", "fields": {"Quantity Reserved": 1}}, {"id": "recXrStRVvQZiasSz", "fields": {"Quantity Reserved": 1}}], "typecast": false}

Alert when a box cannot be saved:
:rotating_light: *TEST subscription-box-curation (temporary)* failed at *Stop: Run Failed*
BOX-2026-11-SUB-0005 could not be saved (HTTP 500 SERVER_ERROR). Its stock reservation was undone. 4 boxes before it are saved. Run again for 2026-11 to carry on, saved boxes are skipped.
http://localhost:5678/workflow/0c01lgnAnPIEat11/executions/16506

Slack summary of the rerun after the data changed:
*Subscription boxes for 2026-11* (Run Now, run boxes-2026-11-2026-10-22T07:57:24Z-16492)
13 active subscribers. Nothing new to plan this run, no stock was reserved. Planned before: 13 boxes (3 not full).

*Bars to take out before packing (2)*
• BOX-2026-11-SUB-0003 for SUB-0003 Priya Raman has DC-BAR-DARK-RASPBERRY, which does not suit Nut-Free. Take it out by hand before packing.
• BOX-2026-11-SUB-0005 for SUB-0005 Elena Rossi has DC-BAR-DARK-85, which does not suit Nut-Free. Take it out by hand before packing.
This run did not change these boxes. A bar taken out still holds its Quantity Reserved, so lower it by 1 on that batch.

*Not fully filled (3)*
• SUB-0004 Derek Olsen, 1 of 6 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 4 in the last box (DC-BAR-DARK-ESPRESSO, DC-BAR-DARK-MINT, DC-BAR-DARK-SEASALT, DC-BAR-OAT-MILK). (planned by an earlier run)
• SUB-0009 Lucia Gomez, 3 of 4 bars packed. The other 16 subscription bars: 12 not suitable for Vegan and Nut-Free; 4 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-ORANGE, DC-BAR-OAT-MILK). (planned by an earlier run)
• SUB-0011 Aisha Khan, 1 of 4 bars packed. The other 18 subscription bars: 14 not suitable for Dairy-Free; 1 with a disliked flavor (Mint); 3 in the last box (DC-BAR-DARK-85, DC-BAR-DARK-ESPRESSO, DC-BAR-OAT-MILK). (planned by an earlier run)
```
