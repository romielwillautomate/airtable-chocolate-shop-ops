# Test report

**Date:** 2026-10-21 (UTC)
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 71 of 71 checks pass: 23 scenarios run through n8n, plus static checks of the workflow file and a final count of the error alerts

## How it was tested

No real Airtable base, Shopify store or Slack workspace was used. The test script:

1. starts the repo's shared mocks ([tests/mocks](../../../tests/mocks)) on port 8792: the Demo Chocolate Co. base loaded from `base/seed` with every date moved so the seed's "today" is today, a Shopify store whose stock matches the seed, and Slack incoming webhooks,
2. imports a temporary copy of the workflow into n8n with every API host pointed at the mocks and fake keys (the schedule is disabled, so only the test starts runs), plus an active copy of `shared/error-alert-slack.json` as its error workflow,
3. calls the Run Now webhook the way you would, waits for each run to finish, then checks every request the workflow made, the records it left in the mock base, the Shopify stock, the Slack notes and the error alerts,
4. deletes both temporary workflows.

A few scenarios use their own temporary copy: bad settings and a closed location (other variables), reads over several pages (10 records per page), a scheduled run (the schedule node swapped for a webhook with the same name, because the test disables schedules) and two Run Now calls at once (with the Airtable rate limiter on, as everywhere else).

The mocks are strict, so a pass means the request would be accepted for real:

- **Airtable** allows 5 requests per second per base. The 6th inside one second gets 429 and the base stays blocked for 30 seconds, as Airtable documents. The limiter was on for every scenario. It also refuses more than 10 records per write, unknown fields, wrong types, an unknown select option, a bad formula and a merge field of the wrong type.
- **Shopify Admin GraphQL 2026-10** runs a real GraphQL validator over the subset of the schema the workflow uses. `inventorySetQuantities` needs `@idempotent(key:)` and a `changeFromQuantity` on every quantity; `compareQuantity` and `ignoreCompareQuantity` are refused as unknown input fields, as in 2026-04 and later. A stale `changeFromQuantity` gets `CHANGE_FROM_QUANTITY_STALE`, an inactive location `INVALID_LOCATION`, and the same key with the same input replays the first answer without applying it again.

Two hooks go beyond plain failure injection. One runs an action just before Shopify handles a request (a customer sale that lands between the workflow's read and its write, or a throttle); it lives in a small subclass of the shared mock server inside the test. The other lets Shopify apply a change and then drops the connection, so n8n never hears back and retries. It started in that subclass too and has been part of the shared mock (`mock.lose_answer`) since the integration on 2026-10-22, because automation 04 needs the same failure. A busy idempotency key (`IDEMPOTENCY_CONCURRENT_REQUEST`, which the Shopify idempotency guide says to retry with the same key) is injected with the shared `mock.fail`.

This automation does not call Claude, so there was no token counting check.

## Found and fixed while testing

| What the test showed | Fix |
|---|---|
| The pagination option of the four Airtable reads was nested one level too shallow (`options.pagination` instead of `options.pagination.pagination`). n8n ignored it without a warning and read only the first page. Every seeded table fits in one page, so the normal scenarios passed; with more than 100 batches the sync would have pushed too little stock to Shopify. The new "Airtable read cut short" scenario showed it. | Fixed in the workflow. A static check and the "Reads that need several pages" scenario (10 records per page) now cover it. |
| A paginated read that reaches its page limit while records are left would have been used half read. | The Code node after each read refuses a read whose last page still has an offset, before anything is changed. |
| With pagination on, n8n puts the Airtable error body in `error.error` instead of `error.message`, so the outage alert only said "503 no details". | The error text helper reads both shapes. The alert now says `503 RETRIABLE_ERROR Server encountered an error...`. |
| In the test itself: `mock.reset()` empties the Slack mock, so the final alert count missed the alerts of earlier scenarios, and the error workflow's own Slack post was counted as a workflow call. | The test keeps alerts across resets, matches them to runs by execution id, and leaves the error alert path out of the call counts. |

A review after the first build found these, and each one now has a scenario:

| What the review found | Fix |
|---|---|
| Only `Open` purchasing requests were treated as in progress. Once the kitchen set a request to `Scheduled` (the batch is planned, the stock is still low), the next run opened a second request, and so on every day until the batch was made. | Open and Scheduled both block a new request. The drift scenario sets one to Scheduled and checks that no new one appears. |
| The Run ID had one second precision, so two runs started in the same second (a double click on Run Now) wrote one Sync Log row: the second run overwrote the first one's numbers. | The Run ID is the start time plus the n8n execution id (`inventory-2026-10-21T06:00:00Z-48213`), and the Shopify keys are built from it. A new scenario runs two Run Now calls at once. |
| Two runs at once, with 250 ms between Airtable calls, made about 8 calls per second together. A probe with the rate limiter on showed both runs hitting 429 on a read and stopping, twice out of twice, so a double click meant two alerts and no sync that day. | Every Airtable call now waits 500 ms after the previous one (about 3 seconds more per run), so two runs stay at 4 per second. The two-runs scenario keeps the limiter on and checks there is no 429. |
| With two runs at once, both upserts of the same promo or request succeed, and both runs counted it as created, so Slack and the Sync Log announced it twice. | Only records the upsert answer lists in `createdRecords` count as created; a matched one is listed as already there. |
| A Shopify call that came back `THROTTLED` (HTTP 200, so n8n's retry does not fire) failed the SKU or the whole run, and `IDEMPOTENCY_CONCURRENT_REQUEST` was not handled, although Shopify's idempotency guide says to retry it with the same key. | One more try after 2 seconds with the same key and input, through a separate node so a second throttle cannot loop. Scenarios cover one throttle (recovered, no alert) and two (Partial or Failed, alert). |
| A failed Airtable read left no Sync Log row, so the run did not show in Airtable at all. | The failed read path writes a Failed row first when Airtable takes it, and the alert says when it could not. |
| A promo typed by hand as `promo-b-wma-260612 ` (other case, a trailing space) was not found, so the sync created a second promo for the batch. | The promo lookup compares `LOWER(TRIM({Promo Name}))`. |
| A Planned batch whose Produced On date had passed was dropped without a word, and a batch with more units reserved and sold than made silently counted as 0. | Both are flagged in Slack like the other batch mistakes. |
| Untested claims: the schedule path, Slack being down, and the attention lines for batch mistakes. | A scenario for each. A mutation check (the workflow broken on purpose in 7 ways, one at a time) confirmed that each new check fails when its fix is removed. |

## Results

The output of the final run:

```
Workflow file
  PASS  every HTTP Request node retries 3 times and routes its error output  (11 HTTP nodes)
  PASS  every IF node (v2) uses an operator n8n 2.19.5 has, single value operators flagged as such  (11 IF nodes, 12 conditions)
  PASS  the 4 Airtable reads paginate (options.pagination.pagination, offset in the body, 500 ms apart)
  PASS  daily at 06:00 UTC, no en or em dashes, no continueOnFail

Run Now security
  PASS  no secret header: 401, nothing read or changed  (401 {'error': 'unauthorized'}, 0 API calls)
  PASS  wrong secret: 401, nothing read or changed  (401 {'error': 'unauthorized'}, 0 API calls)

Seeded run
  PASS  answers 202 with the run id right away  (202 {'status': 'started', 'runId': 'inventory-2026-10-21T03:16:29Z-14064', 'trigger': 'Run Now'})
  PASS  run succeeds and ends without an error  (success, last node IF: Run Failed?)
  PASS  one inventorySetQuantities per changed SKU (6), exact input with changeFromQuantity  (DC-BAR-DARK-85 30 to 4, DC-BAR-DARK-ALMOND 110 to 80, DC-BAR-DARK-SEASALT 180 to 200, DC-BAR-MILK-CLASSIC 200 to 190, DC-BAR-OAT-MILK 80 to 75, DC-BAR-WHITE-MATCHA 20 to 54)
  PASS  every call uses @idempotent with its own key <run id>-<SKU>-<start value> (the run id holds the execution id)  (inventory-2026-10-21T03:16:29Z-14064-DC-BAR-DARK-85-30)
  PASS  Shopify shows the new stock; the 15 unchanged SKUs, PUMPKIN-SPICE (40) and GINGER (25) untouched  (6 adjustments)
  PASS  Products: Shopify Stock and Stock Synced At updated for exactly the 6 SKUs  (DC-BAR-DARK-SEASALT 200)
  PASS  exactly 3 new promos with exact fields; PROMO-B-DSS-260523 untouched; none for B-MHZ-260712 (46 days)  (PROMO-B-MSC-260722, PROMO-B-PRB-260930, PROMO-B-WMA-260612)
  PASS  exactly 3 new Open purchasing requests with exact fields; DARK-MINT keeps its one open request  (DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-PRALINE-HAZELNUT)
  PASS  one Sync Log row with the exact counts  ({"Status": "Success", "Items Checked": 21, "Items Changed": 6, "Shopify Updates": 6, "Promos Created": 3, "Purchasing Requests Created": 3, "Errors": 0})
  PASS  one Slack note with the 3 promos, the 3 requests and the expired batch still marked Released  (1 note(s))
  PASS  Airtable: 4 reads, then 3 writes of at most 10 records and the Sync Log, 500 ms apart (at most 2 in any second)  (max 2 per second, smallest gap 0.51 s)
  PASS  Shopify: 1 stock read and 6 updates, token sent, no GraphQL errors

Rerun with nothing new
  PASS  Shopify is read once and not changed
  PASS  no promo, purchasing request or product write; only the Sync Log row  (('upsert', 'Sync Log', 1))
  PASS  Sync Log row with zero changes; it names the 4 promos and the 4 requests already there
  PASS  Slack note only repeats what still needs fixing (the expired batch)  (1 note(s))

Stock changed in Shopify and in Airtable
  PASS  Airtable wins: CARAMEL 300 to 290, MILK-ESPRESSO 50 to 0, WHITE-COOKIE 48 to 36, nothing else  ({"DC-BAR-MILK-CARAMEL": [300, 290], "DC-BAR-MILK-ESPRESSO": [50, 0], "DC-BAR-WHITE-COOKIE": [48, 36]})
  PASS  units in an unshipped order are not put back on sale (ORANGE stays 57, Airtable corrected to 57)  (Shopify 57 available, 5 committed)
  PASS  Products updated in one call with 4 records (3 pushed, 1 corrected)
  PASS  one new request for MILK-ESPRESSO (a stored 0 is kept); none for the 3 open ones or the Scheduled CHILI one  (6 requests)
  PASS  Sync Log counts  ({"Status": "Success", "Items Checked": 21, "Items Changed": 3, "Shopify Updates": 3, "Promos Created": 0, "Purchasing Requests Created": 1, "Errors": 0})

Sale during the run
  PASS  Shopify refuses the stale change; the run ends at Stop: Sync Failed and the alert names the SKU  (Inventory sync inventory-2026-10-21T03:16:40Z-14075 finished with 1 error. DC-BAR-DARK-SEASALT was not updated in Shopify (CHANGE_FROM_QUANTITY_STALE, the stock changed in Shopify during the run (a sale or an edit), the next run sets it again). Details are in the Sync Log.)
  PASS  the sale is kept (SEASALT 199 in Shopify, 200 in Airtable); RASPBERRY still set to 50
  PASS  Sync Log says Partial with 1 error, and the Slack note lists it  ({"Status": "Partial", "Items Checked": 21, "Items Changed": 1, "Shopify Updates": 1, "Promos Created": 0, "Purchasing Requests Created": 0, "Errors": 1})
  PASS  next run sets SEASALT 199 to 194 (195 sellable minus 1 unshipped) with a new key  (199 to 194)

Mapping mistakes are skipped
  PASS  no inventorySetQuantities call; WHITE-STRAWBERRY keeps 62 (not overwritten with RUBY-CHERRY numbers)
  PASS  the Slack note names each problem and its reason  (4 of 4 reasons)
  PASS  skips are not errors: Sync Log Success, 20 SKUs checked  ({"Status": "Success", "Items Checked": 20, "Items Changed": 0, "Shopify Updates": 0, "Promos Created": 0, "Purchasing Requests Created": 0, "Errors": 0})

Lost Shopify answer
  PASS  n8n retries with the same key and input; Shopify replays, the change is applied once  (2 requests, 1 adjustment)
  PASS  the run still succeeds with all 6 SKUs synced

Shopify throttles or is still busy
  PASS  a throttled stock read is sent once more after 2 seconds  (2 reads, 2.0 s apart)
  PASS  a throttled update and a busy key are sent once more with the same key and input, other SKUs once  (2 CLASSIC and 2 ALMOND calls)
  PASS  each change is applied once, the run succeeds with no alert-worthy error

Shopify throttles twice
  PASS  after the second THROTTLED answer the SKU keeps its old stock in Shopify and Airtable, the other 5 are set  (2 CLASSIC calls)
  PASS  Sync Log says Partial; the alert names the SKU and THROTTLED  (Inventory sync inventory-2026-10-21T03:17:07Z-14118 finished with 1 error. DC-BAR-MILK-CLASSIC was not updated in Shopify (Shopify refused the update )
  PASS  a stock read throttled twice: 2 reads, nothing set, Sync Log Failed, the alert says THROTTLED  (Inventory sync inventory-2026-10-21T03:17:14Z-14125 finished with 1 error. Shopify refused the stock query (THROTTLED Throttled). Details are in the S)

Shopify is down
  PASS  the stock read is tried 3 times, nothing is set in Shopify or in Products
  PASS  promos and purchasing requests are still created (they only need Airtable)
  PASS  Sync Log says Failed, the alert explains why  (Inventory sync inventory-2026-10-21T03:17:20Z-14133 finished with 1 error. Shopify stock could not be read (500 Internal)

Airtable rate limit on a write
  PASS  3 tries get 429, the workflow waits 30 seconds, the 4th try saves the promos  (4 tries, waited 30.0 s)
  PASS  no duplicate promos, the run succeeds

Airtable write fails, next run heals
  PASS  Shopify is updated, Products is not; promos and requests are still saved
  PASS  Sync Log says Partial; the alert names the table and the Airtable error  (Inventory sync inventory-2026-10-21T03:18:06Z-14170 finished with 1 error. Airtable did not save Shopify Stock in Products for DC-BAR-DARK-8)
  PASS  next run: no Shopify change, Products corrected to what Shopify shows

Airtable is down at the start
  PASS  stops at Stop: Airtable Read Failed before anything changes; only a Failed Sync Log row is written  (5 API calls)
  PASS  the Sync Log row and the alert say which read failed, with the Airtable error, and that nothing changed  (Airtable read failed at List Batches (503 RETRIABLE_ERROR Server encountered an error while processing your request, and it is safe to retry)
  PASS  all of Airtable down: 3 reads, 3 tries to log, nothing else; the alert says the row could not be saved  (Airtable read failed at List Products (503 RETRIABLE_ERROR Server encountered an error while processing your request, and it is safe to retry the request). Noth)

Promo made by hand
  PASS  found by name (case and spaces ignored): no second promo, the Active one keeps its 30% and is not linked

More than 10 changes
  PASS  18 SKUs set in Shopify, Products written in 2 calls (10 + 8), still paced  (writes [10, 8], max 2 per second)

Batch data mistakes
  PASS  each mistake is named in Slack (6 attention lines) and none of those units is counted  (6 of 6 lines)
  PASS  the stock result is the seeded one: the same 6 changes, Success

Slack is down
  PASS  the note is tried 3 times; the work is saved (Sync Log Success) but the run ends at Stop: Sync Failed  (Inventory sync inventory-2026-10-21T03:18:54Z-14224 finished, but the Slack note could not be sent.)

Error alerts
  PASS  exactly one alert per failed run (8), none for the 14 runs that succeeded  (8 alerts for runs 14075, 14118, 14125, 14133, 14170, 14185, 14205, 14224)

Scheduled run
  PASS  runs as a Schedule run: no Run Now node, no webhook answer node, Trigger Schedule in the Sync Log  (success, inventory-2026-10-21T03:19:04Z-14229)
  PASS  same result as the Run Now path: the 6 changes, 3 promos, 3 requests

Two Run Now calls at once
  PASS  both answer 202 with their own run id, each run writes its own Sync Log row, no 429 together  (inventory-2026-10-21T03:19:10Z-14231, inventory-2026-10-21T03:19:10Z-14232, max 4 Airtable calls per second)
  PASS  every change is applied exactly once in Shopify, Products ends right, Shopify Updates add up to 6  (10 SetStock calls, updates [6, 0], errors [0, 4])
  PASS  no duplicate promo or purchasing request, and each new one is counted as created by one run only  (promos created [3, 0], requests created [3, 0])
  PASS  a run that lost a SKU to the other one says so (Partial and one alert); a clean run sends none  (success, error, 1 alert(s))

Bad settings
  PASS  Run Now answers 500 with the exact problems, no API call  (500)
  PASS  the run fails at Stop: Bad Config and the alert lists the problems  (Inventory sync settings are missing or wrong. SHOPIFY_LOCATION_ID is empty. PROMO_WINDOW_DAYS must be larger than SELL_B)

Reads that need several pages
  PASS  22 products and 30 batches read in 3 pages each (the offset goes back in the body), still paced  ({'Products': 3, 'Production Batches': 3}, max 2 per second)
  PASS  same result as one page: the 6 changes, 3 promos, 3 requests

Airtable read cut short
  PASS  a read stopped by its page limit is refused: nothing is written, Shopify is not called  (The Production Batches read stopped after 2 pages with more records left, so the run stopped before changing anything. Raise maxRequests in Airtable L)

Closed Shopify location
  PASS  no stock is set, Products untouched, Sync Log Failed, the alert says the location is not active  (Inventory sync inventory-2026-10-21T03:19:31Z-14250 finished with 1 error. The Shopify location Old Warehouse (closed) i)

71/71 checks passed
```

The text in brackets is cut at about 150 characters by the test; the full alerts are below.

## What the workflow sent (seeded run)

**Shopify: `inventorySetQuantities`** (one call per changed SKU; this is DC-BAR-DARK-SEASALT)

```graphql
mutation SetStock($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    inventoryAdjustmentGroup { id reason referenceDocumentUri changes { name delta quantityAfterChange } }
    userErrors { code field message }
  }
}
```

```json
{
  "key": "inventory-2026-10-21T03:16:29Z-14064-DC-BAR-DARK-SEASALT-180",
  "input": {
    "name": "available",
    "reason": "correction",
    "referenceDocumentUri": "gid://airtable-inventory-sync/SyncRun/inventory-2026-10-21T03:16:29Z-14064",
    "quantities": [
      {
        "inventoryItemId": "gid://shopify/InventoryItem/4720000000101",
        "locationId": "gid://shopify/Location/71000000001",
        "quantity": 200,
        "changeFromQuantity": 180
      }
    ]
  }
}
```

**Slack: ops note**

```
*Inventory sync* inventory-2026-10-21T03:16:29Z-14064 (Run Now), Success
Shopify stock set for 6 SKUs at Demo Kitchen: DC-BAR-DARK-85 30 to 4, DC-BAR-DARK-ALMOND 110 to 80, DC-BAR-DARK-SEASALT 180 to 200, DC-BAR-MILK-CLASSIC 200 to 190, DC-BAR-OAT-MILK 80 to 75, DC-BAR-WHITE-MATCHA 20 to 54

*New promos (3)*, 25% off suggested, Status Proposed in Airtable:
• PROMO-B-WMA-260612 for DC-BAR-WHITE-MATCHA, 40 units, best before 2026-11-20 (30 days left)
• PROMO-B-PRB-260930 for DC-BOX-PRALINE-12, 38 units, best before 2026-11-30 (40 days left)
• PROMO-B-MSC-260722 for DC-BAR-MILK-CARAMEL, 50 units, best before 2026-12-05 (45 days left)

*New purchasing requests (3)*:
• DC-BAR-DARK-85, 4 sellable, reorder point 40, suggested batch 200
• DC-BAR-DARK-CHILI, 19 sellable, reorder point 30, suggested batch 150
• DC-PRALINE-HAZELNUT, 40 sellable, reorder point 200, suggested batch 1000

*Needs attention (1)*:
• Batch B-DAL-260314 (DC-BAR-DARK-ALMOND) is past its best before date (2026-10-16) but still marked Released, 20 units
```

**Airtable: Sync Log Details**

```
Run Now run, started 2026-10-21T03:16:29.042Z.
Checked 21 Shopify SKUs at Demo Kitchen. 6 changed, 15 already correct.
Set in Shopify (FIFO batches in brackets): DC-BAR-DARK-85 30 to 4 [B-D85-260821 4]; DC-BAR-DARK-ALMOND 110 to 80 [B-DAL-260920 80]; DC-BAR-DARK-SEASALT 180 to 200 [B-DSS-260523 30, B-DSS-260910 170]; DC-BAR-MILK-CLASSIC 200 to 190 [B-MCL-260920 190]; DC-BAR-OAT-MILK 80 to 75 [B-OAT-260915 75]; DC-BAR-WHITE-MATCHA 20 to 54 [B-WMA-260612 40, B-WMA-261010 14].
Not on Shopify, Airtable only: DC-PRALINE-HAZELNUT.
Held back, inside the 14 day sell-by buffer: B-D85-260403 DC-BAR-DARK-85 30 units (10 days left).
Promos created (25% suggested): PROMO-B-WMA-260612, PROMO-B-PRB-260930, PROMO-B-MSC-260722.
Promos already there: PROMO-B-DSS-260523.
Purchasing requests created: DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-PRALINE-HAZELNUT.
Purchasing requests already in progress: DC-BAR-DARK-MINT (Open).
Needs attention: Batch B-DAL-260314 (DC-BAR-DARK-ALMOND) is past its best before date (2026-10-16) but still marked Released, 20 units.
```

**Airtable: Purchasing Request note** (DC-PRALINE-HAZELNUT)

```
Raised by the inventory sync (inventory-2026-10-21T03:16:29Z-14064). Sellable 40, reorder point 200. Already planned B-PRL-261023 (1500 units on 2026-10-24).
```

## Error alerts (from the error workflow)

A sale lands during the run:

```
:rotating_light: *TEST batch-inventory-sync (temporary)* failed at *Stop: Sync Failed*
Inventory sync inventory-2026-10-21T03:16:40Z-14075 finished with 1 error. DC-BAR-DARK-SEASALT was not updated in Shopify (CHANGE_FROM_QUANTITY_STALE, the stock changed in Shopify during the run (a sale or an edit), the next run sets it again). Details are in the Sync Log.
http://localhost:5678/workflow/1woNbAoImfvDlxH5/executions/14075
```

Shopify is down:

```
:rotating_light: *TEST batch-inventory-sync (temporary)* failed at *Stop: Sync Failed*
Inventory sync inventory-2026-10-21T03:17:20Z-14133 finished with 1 error. Shopify stock could not be read (500 Internal Server Error). Details are in the Sync Log.
http://localhost:5678/workflow/1woNbAoImfvDlxH5/executions/14133
```

An Airtable write fails:

```
:rotating_light: *TEST batch-inventory-sync (temporary)* failed at *Stop: Sync Failed*
Inventory sync inventory-2026-10-21T03:18:06Z-14170 finished with 1 error. Airtable did not save Shopify Stock in Products for DC-BAR-DARK-85, DC-BAR-DARK-ALMOND, DC-BAR-DARK-SEASALT, DC-BAR-MILK-CLASSIC, DC-BAR-OAT-MILK, DC-BAR-WHITE-MATCHA (422 INVALID_VALUE_FOR_COLUMN Field "Shopify Stock" cannot accept the provided value). Details are in the Sync Log.
http://localhost:5678/workflow/1woNbAoImfvDlxH5/executions/14170
```

Airtable is down at the start:

```
:rotating_light: *TEST batch-inventory-sync (temporary)* failed at *Stop: Airtable Read Failed*
Airtable read failed at List Batches (503 RETRIABLE_ERROR Server encountered an error while processing your request, and it is safe to retry the request). Nothing was changed, run the sync again once Airtable answers.
http://localhost:5678/workflow/1woNbAoImfvDlxH5/executions/14185
```

Bad settings:

```
:rotating_light: *TEST batch-inventory-sync (temporary)* failed at *Stop: Bad Config*
Inventory sync settings are missing or wrong. SHOPIFY_LOCATION_ID is empty. PROMO_WINDOW_DAYS must be larger than SELL_BY_BUFFER_DAYS (or 0 to switch promos off), otherwise no sellable batch can get a promo.
http://localhost:5678/workflow/gwAWFKR0gfYmDGWI/executions/14236
```

All products, batches, customers and numbers above are made up. "Demo Chocolate Co." is a fictional shop.
