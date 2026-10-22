# Test report

**Date:** 2026-10-21 (UTC)
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 83 of 83 checks pass: 34 scenarios run through n8n, plus a check of every request in the run and a cleanup check. The run took 411 seconds.

## How it was tested

No real Shopify store, Airtable base, Open-Meteo account or Slack workspace was used. The test script:

1. starts the repo's shared mocks ([tests/mocks](../../../tests/mocks/README.md)) on port 8791: the Demo Chocolate Co. base loaded from `base/seed` with every date moved so the seed's "today" is today, Open-Meteo geocoding and forecast, and Slack incoming webhooks,
2. imports a temporary copy of the workflow into n8n with every API host pointed at the mocks and fake keys, plus an **active** copy of `shared/error-alert-slack.json` as its error workflow (n8n 2.x skips an error workflow that is not active),
3. delivers webhooks signed exactly like Shopify signs them (`X-Shopify-Hmac-Sha256`, base64 of the HMAC SHA256 of the raw body, with the Shopify headers `X-Shopify-Topic`, `X-Shopify-Webhook-Id`, `X-Shopify-Event-Id` and the rest), waits for each n8n run to finish, then checks the records left in the mock base, every request the workflow made, the Slack messages and the error alerts,
4. deletes both temporary workflows.

The mocks are strict, so a pass means the request would be accepted for real:

- **Airtable** allows 5 requests per second per base. The 6th inside one second gets 429 and the base stays blocked for 30 seconds, as Airtable documents. The limiter was on in every scenario. The mock also refuses more than 10 records per write, unknown field names, wrong types, a select option that does not exist, a link that is not a record id, a bad formula, an upsert key of a type Airtable does not take (an Email field, for example) and an upsert that matches two existing records, each with Airtable's real error body.
- **Open-Meteo** geocoding matches like the real service: a qualifier after the comma (`Austin, TX`) must match a country or a state exactly, and a place it does not know returns no results. The forecast refuses daily data without `timezone`.
- **Slack** answers `ok`, or the real error texts.

Failures were injected with the mock's failure switch: 500 and 503 from Open-Meteo, a forecast with a null day, 429 and 422 from Airtable, 500 from the packing Slack webhook. Three scenarios start their own copy of the workflow with other settings: no `SHOPIFY_WEBHOOK_SECRET`, `MELT_TEMP_C=35` with `MELT_FORECAST_DAYS=2` and no packing webhook, and a wrong `AIRTABLE_TOKEN`.

This workflow does not call Claude or the Shopify API, so there was no token counting check and no Shopify GraphQL request (the whole run check confirms that no request reached the Shopify mock).

## Checked against the official docs

Every external call was compared with the provider's current documentation in October 2026.

| Rule | Source | How the workflow follows it |
|---|---|---|
| Shopify signs the raw body with HMAC SHA256 and sends it base64 encoded in `X-Shopify-Hmac-Sha256`; verify before parsing | Shopify, Deliver webhooks through HTTPS | The webhook keeps the raw body, the Crypto node signs it, the Code node compares in constant time |
| Deduplicate on `X-Shopify-Webhook-Id`; `X-Shopify-Event-Id` groups deliveries of one merchant action | same | Webhook Log is keyed on the webhook id |
| 1 second to connect, 5 seconds for the whole request; 8 retries over 4 hours | same | 200 first, all work after the answer (0.10 s in the test) |
| Order fields: `line_items[].current_quantity`, `sku`, `variant_id`, `total_shipping_price_set`, `note_attributes`, `buyer_accepts_marketing`, `financial_status`, `fulfillment_status`, `cancelled_at`, `closed_at`, shipping address codes | Shopify REST Admin, Order resource | Read as documented; money strings are turned into numbers |
| 5 requests per second per base, then 429 and a 30 second wait | Airtable, Rate limits | 500 ms between calls; a 31 to 60 second cooldown after 3 failed tries |
| At most 10 records per write; `fieldsToMergeOn` takes 1 to 3 fields of type number, text, long text, single select, multiple select or date; the upsert fails when several records match; the answer has `createdRecords` and `updatedRecords` | Airtable, Update multiple records | Batches of 10; every upsert key is single line text; Customers are never upserted on Email |
| `POST /v0/{baseId}/{table}/listRecords` takes the list parameters in the body, for URLs that would pass 16,000 characters; empty fields are left out of records | Airtable, List records | Every lookup uses the POST form; a missing field is read as empty |
| A qualifier after a comma ("a country or first-level administrative area") must match exactly; country names, 2-letter codes, admin1 names and abbreviations are accepted; `countryCode` filters by country | Open-Meteo, Geocoding API | `City, ST` plus `countryCode=US` and `count=10`, first result used. The docs do not say what a search with no match returns, so a missing `results` key and an empty list both mean Review |
| Daily variables need `timezone` (`auto` takes it from the coordinates); `forecast_days` is 0 to 16, default 7; `temperature_2m_max` is the daily maximum at 2 m | Open-Meteo, Weather Forecast API | `daily=temperature_2m_max&timezone=auto&forecast_days=MELT_FORECAST_DAYS`; a missing value means Review |
| `&`, `<` and `>` are control characters in message text and must be sent as `&amp;`, `&lt;` and `&gt;` when they are not markup | Slack, Formatting message text | Gift notes, names, addresses and titles are escaped in every packing and SKU alert; header values in alert texts keep only letters, digits and `._/@-` |

## Found and fixed while testing

| What the test showed | Fix |
|---|---|
| The first full run paced Airtable calls 250 ms apart, which is 4 per second for one run. Two webhooks processed at the same moment would then reach 8 per second, over Airtable's 5. | 500 ms between calls. "Two new orders processed at the same moment" now runs both with the real limiter on: busiest second 4 requests, no 429. |
| Shopify often sends orders/updated a moment after orders/create. Run together, both would see a new order, check the weather twice and post two packing alerts. | An orders/updated waits 10 seconds before it starts. The scenario that delivers both at once now ends with one order, one customer, one melt check and one packing alert. |
| The first cooldown after a 429 was a fixed 31 seconds, so two runs blocked together would come back together and trip the limit again. | 31 to 60 seconds, with a random part. |
| In one full run, the alert for the duplicated Orders row did not arrive within the test's 45 second wait, although the same scenario passed on its own. n8n starts the error workflow only after the failed run has finished, and other test suites were using the same n8n at that time, which is the likely cause. | The test now waits up to 90 seconds for an error alert and, when none arrives, prints the error workflow's runs, so a slow alert can be told apart from a missing one. In the final run every expected alert arrived. |

### Adversarial review (2026-10-21)

A second pass looked for mistakes the first build and its tests could not see. Each new check below was run first against the earlier workflow, where it failed (47 of 52 in that partial run), then against the fixed one.

| What the review found | Fix |
|---|---|
| A run that restarts after an Airtable cooldown kept the stale decision from its first attempt. If another webhook for the same order saved a newer version during the 31 to 60 seconds, the restarted run wrote the older order over it (Financial Status went back from Paid to Pending in the test) and sent a second packing alert. | Check Order compares with the stored Shopify Updated At on every attempt and only keeps the weather decision. The late run is logged as Ignored with a note on what its first attempt already did. New scenario: "A newer orders/updated is saved while the create waits out a cooldown". |
| Gift notes, names, addresses and product titles went into Slack as they were, so a gift note with `<!channel>` pinged the whole packing channel and `<https://...|text>` posted a disguised link. | Escaped as Slack documents. New check "Customer text in a Slack alert"; Airtable still keeps the note as typed. |
| The bad signature alert quoted the `X-Shopify-Topic` and `X-Shopify-Webhook-Id` headers, which are not signed. Anyone who knows the URL could post Slack markup to the ops channel through the error workflow. | Header values in alert texts keep only letters, digits and `._/@-`. New check in "Forged and unsigned webhooks". |
| Geocoding asked for 1 result. The docs say the comma qualifier narrows the results, but not whether that happens before the count is applied, so a qualified place could be cut off. | Up to 10 results, the first one is used. |
| Paths that had never run in a test: a guest checkout without a customer id (the plain create), an order with no email and no customer, a guest matched by email when two records share it, more than 10 unknown SKUs (the second Draft product batch), a forecast with a missing day. The repeat and stale checks also allowed extra requests. | New checks for each path (they passed on the earlier workflow too, so they guard against regressions), and the repeat and stale checks now compare the exact request sequence, the order's cells and the Sync Log counts. |

## Results

The output of the final run:

```
New order to a hot city (Phoenix), new customer
  PASS  Shopify gets 200 within 5 seconds, before the slow work  (200 'ok' in 0.08 s)
  PASS  the run succeeds and ends at Done: Order Synced  (success, Done: Order Synced)
  PASS  one new customer with the Shopify details  (1 new, {'Name': 'New Buyer', 'Email': 'new.buyer@example.com', 'Phone': '+15550109026', 'Shopify Customer ID': '7700000001026', 'Created Via': 'Shopify', 'Record Status': 'Active', 'Orders': ['recp0pHPvSxur0VkQ']})
  PASS  one Orders record with the exact values, Cold Pack Required at 37.1 C  (all fields match)
  PASS  two Order Lines linked to the order and to their products  (2 lines)
  PASS  geocoding asks for "Phoenix, AZ" in the US, the forecast for 3 days of temperature_2m_max  (geocode {'name': 'Phoenix, AZ', 'count': '10', 'language': 'en', 'format': 'json', 'countryCode': 'US'}, forecast {'latitude': '33.44838', 'longitude': '-112.07404', 'daily': 'temperature_2m_max', 'timezone': 'auto', 'forecast_days': '3'})
  PASS  one packing alert with the forecast, the address and the items  (:snowflake: *Cold pack needed* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001026|#1026>)
  PASS  exactly 9 Airtable requests in this order, all 200, every one authorized  (Webhook Log list, Orders list, Customers list, Customers upsert, Products list, Orders upsert, Order Lines upsert, Webhook Log upsert, Sync Log upsert)
  PASS  the new customer is an upsert on Shopify Customer ID, never on Email  ({"fieldsToMergeOn": ["Shopify Customer ID"]})
  PASS  Airtable calls are paced: at least 500 ms apart, never more than 2 in one second  (smallest gap 0.517 s, busiest second 2 requests)
  PASS  Webhook Log row Processed and linked, Sync Log row Success with counts  (Order #1026 created. Customer created (New Buyer). 2 lines (2 new, 0 updated). Cold Pack Required. Forecast max 37.1 C over the next 3 days in Phoenix, Arizona (cold pack from 27 C). Packing alert sent.)

Same webhook delivered twice
  PASS  second copy skipped: one lookup, no writes, no weather call, no Slack  (ended at Done: Repeat Skipped, 1 Airtable request(s))

Webhook id already in the seeded Webhook Log
  PASS  b54557e4-... is recognised as processed: one lookup, no writes, no weather call, no Slack  (ended at Done: Repeat Skipped, 1 Airtable request(s))

orders/updated: partial refund and fulfilment
  PASS  the same Orders record is updated in place  (Partially Refunded, Fulfilled, 7.50 refunded)
  PASS  lines updated, none added: the refunded unit shows on line 1  (line 1 1 of 2, refunded 1)
  PASS  no second melt check, no Slack, no customer write  (0 geocoding calls, 0 Slack posts)

Older orders/updated arrives after a newer one
  PASS  nothing overwritten, the webhook is logged as Ignored and linked to the order  (Skipped. This orders/updated webhook has updated_at 2026-10-21T03:35:21.000Z, older than the version already in Airtable)

Customer matching
  PASS  jordan (two records share the email) is matched by Shopify Customer ID, not the duplicate  (linked to Jordan Blake)
  PASS  messy email and an unknown id: matched by LOWER(TRIM(Email)) to the record that has a Shopify id  (Email stored as maya.chen@example.com)
  PASS  email match without an id: the record is updated by record id (Name, id, phone filled in)  ({"records": [{"id": "rec8hFhav93r7aVAx", "fields": {"Shopify Customer ID": "7700000000132", "Name": "Kai Morgan", "Phone": "+15550101032"}}]})
  PASS  guest checkout by jordan (no customer id, two records share the email): the record with a Shopify id wins  (OR(LOWER(TRIM({Email})) = 'jordan.blake@example.com'))
  PASS  a new guest without a customer id is created with a plain POST (an upsert needs a key), email in lower case  ({"records": [{"fields": {"Created Via": "Shopify", "Record Status": "Active", "Accepts Marketing": false, "Name": "Guest Buyer", "Email": "guest.buyer@example.com", "Phone": "+15550109054"}}]})
  PASS  the next webhook for that guest finds the record by email: no second customer  (1 new customer(s))
  PASS  no email and no customer id: the order is saved without a customer and flagged for review  (No customer email or Shopify customer id on the order, so no customer is linked.)

Unknown SKU, existing Draft, messy SKU, line without SKU
  PASS  DC-BAR-GINGER becomes one Draft product with Needs Review and the reason  (Created automatically from Shopify order #1030: unknown SKU (Shopify variant 4510000099001). Add allergens, ingredients and stock rules before selling.)
  PASS  one product upsert on SKU with exactly 1 record; the Pumpkin Spice Draft is matched, not created again  (1 product write(s), 1 record(s))
  PASS  lines: new Draft linked, Draft matched, " dc-bar-milk-caramel" matched (kept as sent), gift wrap Missing SKU at 0  (DC-BAR-GINGER Matched, DC-BAR-PUMPKIN-SPICE Matched, dc-bar-milk-caramel Matched, (none) Missing SKU)
  PASS  the order is flagged for review with the reason  (Unknown SKU DC-BAR-GINGER on line 1. A draft product was created.)
  PASS  one ops alert about the new SKU, no packing alert (Minneapolis is cold)  (:package: *New SKU from Shopify* on order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001030|#1030>)

orders/updated for the same order
  PASS  no new product, no new lines, no second alert, the review reason is not repeated  (4 lines updated)

Twelve unknown SKUs on one order
  PASS  Draft products created in two upserts on SKU, 10 and 2 records, 500 ms apart  ([10, 2], 12 new products)
  PASS  all 12 lines are linked to their new Draft product  (Matched)
  PASS  one ops alert names all 12 SKUs, and the order lists all 12 reasons  (:package: *New SKU from Shopify* on order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001056|#1056>)

Gift orders
  PASS  a "Gift Message" cart attribute: Is Gift and the message for the packing slip (not the delivery note)  ('Happy birthday, Sam!', Cold Pack Not Needed)
  PASS  tag "gift": the order note is the gift note, and the packing alert carries it  (Gift note for the packing slip: "For Grandma, with love")

Customer text in a Slack alert
  PASS  a gift note with <!channel> and a link cannot ping the channel or post a link; Airtable keeps the note as typed  (Gift note for the packing slip: "Love you &lt;3 &amp; see you soon &lt;!channel&gt; &lt;https://evil.example/login|claim)

Melt threshold edges (MELT_TEMP_C 27, 3 days)
  PASS  Atlanta 26.1 C, the closest to the line: Not Needed  (Not Needed, max 26.1, 0 packing alert(s))
  PASS  exactly 27.0 C on day 2 counts (at or above): Required  (Required, max 27, 1 packing alert(s))
  PASS  35 C on day 4 is outside the 3 day window: Not Needed  (Not Needed, max 12, 0 packing alert(s))
  PASS  "Austin, TX" with a state code qualifier: Required  (Required, max 30.1, 1 packing alert(s))

Geocoding service down (500 three times)
  PASS  the order is still saved, Cold Pack Review, and the note says why  (Melt check failed: the Open-Meteo geocoding service answered HTTP 500 after 3 tries. Cold Pack set to Review, check the forecast for Houston, TX, US by hand.)
  PASS  packing team told to check by hand, Sync Log Partial with 1 error  (:warning: *Check the cold pack by hand* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001037|#1037>)

Weather back: the next orders/updated checks again
  PASS  Review becomes Required (Houston 31.2 C) and the packing team is told  (Required, 1 alert(s))

Forecast service down (503 three times)
  PASS  Cold Pack Review after 1 geocoding call and 3 forecast tries  (Melt check failed: the Open-Meteo forecast service answered HTTP 503 after 3 tries. Cold Pack set to Review, check the forecast for Miami, FL, US by hand.)

Forecast with a missing day (null)
  PASS  a day without a value means Review, not a guess from the other days  (Melt check could not finish: the Open-Meteo forecast for Honolulu, Hawaii did not include 3 days of maximum te)

City the geocoder does not know
  PASS  no results means Review (no guessing), no forecast call  (Melt check could not run: Open-Meteo geocoding found no place called "Springfield, IL" in US. Cold Pack set to)

Pickup order without a shipping address
  PASS  Cold Pack Not Needed with a note, no weather calls, no address fields  (No shipping address (pickup or digital order), no melt check.)

Large order: 23 lines
  PASS  three line upserts of 10, 10 and 3 records; no request over 10 records  ([10, 10, 3])
  PASS  all 23 lines saved and linked, no 429 from the rate limiter  (23 lines, busiest second 2 requests, smallest gap 0.519 s)

orders/create and orders/updated for one new order, delivered together
  PASS  both answered 200 at once; the update waited 10 s, so the create was saved first  (['success', 'success'], update waited: True)
  PASS  one order with the newer state, one customer, one melt check, one packing alert  (Paid, 1 melt check(s), 1 packing alert(s))

Two new orders processed at the same moment
  PASS  with the real limiter on, the two runs together stay under 5 requests per second (no 429)  (18 Airtable requests, busiest second 4)

Airtable rate limit (429) while saving the order
  PASS  after 3 tries the run waits 31 to 60 s (the base is blocked for 30), then starts the webhook again  (3 x 429, paused 34.0 s, Start Processing ran 2 times)
  PASS  one order, one customer, one packing alert and one melt check in total  (1 packing alert(s), 1 geocoding call(s))
  PASS  logged as Processed, and the run log still counts the customer it created on the first attempt  (Order #1042 created. Customer created (Vegas Buyer). 1 line (1 new, 0 updated). Cold Pack Required. )

A newer orders/updated is saved while the create waits out a cooldown
  PASS  the create restarts after its cooldown and finds the newer version: it writes nothing over it  ([429, 429, 429, 200], order Paid)
  PASS  the late create is logged as Ignored (with what its first attempt did), the update as Processed  (Skipped. This orders/create webhook has updated_at 2026-10-21T03:34:03.000Z, older than the version already in Airtable (2026-10-21T03:35:03)
  PASS  one customer, one line, one packing alert (the late create sends none)  (1 packing alert(s), 2 geocoding call(s), 1 line(s))

Airtable rejects the lines (422) after the order is saved
  PASS  no cooldown for a 422: the run stops after 3 tries and the webhook is marked Failed  (ended at Stop: Airtable Failed, Webhook Log Failed)
  PASS  the error workflow posts the reason to Slack  (Order #1043 from orders/create was not fully saved after 1 attempt. Airtable answered 422 INVALID_VALUE_FOR_COLUMN (Field "Quantity" cannot accept the provided value) at step Upsert Order Lines (batch 1 of 1). Webhook wh-1043-create is not marked Processed, so the next orders/updated for this order (or a redelivery) writes it again.)
  PASS  the packing alert still went out (alerts are sent before the lines)  (1 packing alert(s))

The next orders/updated repairs it
  PASS  lines written, no second packing alert, no second melt check  (1 line(s))

Someone duplicated an Orders row in Airtable
  PASS  Airtable's 422 for two matching rows stops the run, and the alert says which rows to merge  (Order #1050 from orders/updated was not fully saved after 1 attempt. Airtable answered 422 INVALID_VALUE_FOR_COLUMN (Cannot update more than one record for fiel)

Packing Slack webhook down (500 three times)
  PASS  order and lines saved, Webhook Log Processed, Sync Log Partial  (3 tries, Sync Log Partial)
  PASS  the run ends failed so the error workflow tells ops  (Order #1044 is saved in Airtable, but the packing alert (Slack answered HTTP 500 after 3 tries) could not be posted to Slack. Cold Pack is Required. Tell the packing team by hand.)

Cancelled order
  PASS  Order Status Cancelled, Cancelled At and Voided are written, nothing else happens  (Cancelled, Voided, cancelled 2026-10-21T04:00:34.000Z)

Shipping address changed to a cold city
  PASS  the melt check runs again: Not Needed, and the packing team hears the cold pack is no longer needed  (*Cold pack no longer needed* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001032|#1032> (the shipping address changed))

Forged and unsigned webhooks
  PASS  wrong signature: 401, nothing read or written, run failed with an alert  (401 'invalid signature', ended at Stop: Bad Signature)
  PASS  no signature: 401, nothing read or written, run failed with an alert  (401 'invalid signature', ended at Stop: Bad Signature)
  PASS  signed with another secret: 401, nothing read or written, run failed with an alert  (401 'invalid signature', ended at Stop: Bad Signature)
  PASS  the forged order never reached Airtable  (0 new orders)
  PASS  Slack markup in forged headers is neutralised before the alert: no ping, no link  (Rejected a Shopify webhook with a wrong X-Shopify-Hmac-Sha256 signature (topic orders/create???channel???https?//evil.example/login?Shopify?log, webho)
  PASS  an empty POST: 401, nothing read  (401 'invalid signature')
  PASS  a body that is not JSON is refused by n8n itself (422) before the workflow runs  (422 '{"code":422,"message":"Failed to parse request bod')

Other topics and bodies
  PASS  a signed orders/paid webhook: 200 "ignored", no Airtable call  (200 'ignored')
  PASS  a signed body that is not an order: 200 so Shopify does not retry, run failed with an alert  (200, ended at Stop: Not An Order)

Whole run
  PASS  every other Airtable request was accepted: no 4xx, no 429 (injected failures and the duplicate row 422s aside)  (348 requests, 9 injected failures, 3 expected 422s)
  PASS  no request over 10 records, no request to Shopify or to an unmocked URL  (largest write 10 records)

SHOPIFY_WEBHOOK_SECRET not set
  PASS  fail closed: even a correctly signed webhook gets 500 and nothing is read or written  (500 'webhook not configured', ended at Stop: Not Configured)

Settings from $env: MELT_TEMP_C 35, MELT_FORECAST_DAYS 2, no packing webhook
  PASS  Tucson at 34.6 C is under the new 35 C limit, and only 2 days are asked for  (Forecast max 34.6 C over the next 2 days in Tucson, Arizona (cold pack from 35 C).)
  PASS  Phoenix at 37.1 C is over it, and the packing alert falls back to SLACK_WEBHOOK_URL  (Required, alerts 1, packing 0)

Wrong AIRTABLE_TOKEN
  PASS  Shopify still gets its 200; the run stops at the first lookup (401, no cooldown) and the alert names the cause  (Order #1051 from orders/create was not fully saved after 1 attempt. Airtable answered 401 AUTHENTICATION_REQUIRED (Invalid authentication token) at st)

Cleanup
  PASS  every temporary workflow was deleted  (0 left, 0 leftovers removed before the run)

83/83 checks passed
```

## What the workflow sent

The requests and messages from the final run, as the mocks received them. All names, emails, phone numbers and addresses are made up.

```
customer_upsert:
{
 "performUpsert": {
  "fieldsToMergeOn": [
   "Shopify Customer ID"
  ]
 },
 "records": [
  {
   "fields": {
    "Created Via": "Shopify",
    "Record Status": "Active",
    "Accepts Marketing": false,
    "Name": "New Buyer",
    "Email": "new.buyer@example.com",
    "Phone": "+15550109026",
    "Shopify Customer ID": "7700000001026"
   }
  }
 ]
}

orders_upsert:
{
 "performUpsert": {
  "fieldsToMergeOn": [
   "Shopify Order ID"
  ]
 },
 "records": [
  {
   "fields": {
    "Order Number": "#1026",
    "Shopify Order ID": "5200000001026",
    "Shopify Updated At": "2026-10-21T03:30:01.000Z",
    "Order Status": "Open",
    "Cancelled At": null,
    "Subtotal": 22,
    "Shipping": 6.95,
    "Tax": 0,
    "Total": 28.95,
    "Refunded Amount": 0,
    "Currency": "USD",
    "Shopify Tags": "",
    "Is Gift": false,
    "Order Date": "2026-10-21T03:30:01.000Z",
    "Email": "new.buyer@example.com",
    "Financial Status": "Paid",
    "Fulfillment Status": "Unfulfilled",
    "Customer": [
     "recdphCA8boRA6mE7"
    ],
    "Shipping Name": "New Buyer",
    "Shipping Address": "1 Test Way",
    "Shipping City": "Phoenix",
    "Shipping Region": "AZ",
    "Shipping Country": "US",
    "Shipping Postal Code": "85004",
    "Cold Pack": "Required",
    "Forecast Max C": 37.1,
    "Melt Checked At": "2026-10-21T03:30:03.739Z",
    "Melt Check Note": "Forecast max 37.1 C over the next 3 days in Phoenix, Arizona (cold pack from 27 C)."
   }
  }
 ]
}

lines_upsert:
{
 "performUpsert": {
  "fieldsToMergeOn": [
   "Line Item ID"
  ]
 },
 "records": [
  {
   "fields": {
    "Line Item ID": "14100000102601",
    "Order": [
     "recp0pHPvSxur0VkQ"
    ],
    "Product Title": "Dark 70% Sea Salt Bar",
    "Quantity": 2,
    "Current Quantity": 2,
    "Refunded Quantity": 0,
    "Unit Price": 7.5,
    "Line Total": 15,
    "SKU": "DC-BAR-DARK-SEASALT",
    "Product": [
     "recgRpyU1uAQkRvbZ"
    ],
    "SKU Status": "Matched"
   }
  },
  {
   "fields": {
    "Line Item ID": "14100000102602",
    "Order": [
     "recp0pHPvSxur0VkQ"
    ],
    "Product Title": "Milk Salted Caramel Bar",
    "Quantity": 1,
    "Current Quantity": 1,
    "Refunded Quantity": 0,
    "Unit Price": 7,
    "Line Total": 7,
    "SKU": "DC-BAR-MILK-CARAMEL",
    "Product": [
     "reclFFuR1FDIoqrPW"
    ],
    "SKU Status": "Matched"
   }
  }
 ]
}

draft_upsert:
{
 "performUpsert": {
  "fieldsToMergeOn": [
   "SKU"
  ]
 },
 "records": [
  {
   "fields": {
    "SKU": "DC-BAR-GINGER",
    "Name": "Dark Ginger Bar",
    "Status": "Draft",
    "Needs Review": true,
    "Review Reason": "Created automatically from Shopify order #1030: unknown SKU (Shopify variant 4510000099001). Add allergens, ingredients and stock rules before selling.",
    "Price": 7.95
   }
  }
 ]
}

geocode query: {'name': 'Phoenix, AZ', 'count': '10', 'language': 'en', 'format': 'json', 'countryCode': 'US'}

forecast query: {'latitude': '33.44838', 'longitude': '-112.07404', 'daily': 'temperature_2m_max', 'timezone': 'auto', 'forecast_days': '3'}

packing_alert:
:snowflake: *Cold pack needed* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001026|#1026>
Forecast max 37.1 C over the next 3 days in Phoenix, Arizona (cold pack from 27 C).
Ship to: New Buyer, Phoenix, AZ, 85004
Items: 2 x Dark 70% Sea Salt Bar, 1 x Milk Salted Caramel Bar

review_alert:
:warning: *Check the cold pack by hand* for order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001037|#1037>
Melt check failed: the Open-Meteo geocoding service answered HTTP 500 after 3 tries. Cold Pack set to Review, check the forecast for Houston, TX, US by hand.
Ship to: Tom Becker, Houston, TX, 77002
Items: 1 x Dark Chili Cinnamon Bar

sku_alert:
:package: *New SKU from Shopify* on order <https://admin.shopify.com/store/demo-chocolate/orders/5200000001030|#1030>
DC-BAR-GINGER (Dark Ginger Bar) is not in Airtable, so a Draft product was created with Needs Review ticked. Add allergens, ingredients and stock rules, then set it to Active.

airtable_alert:
:rotating_light: *TEST shopify-orders-sync (temporary)* failed at *Stop: Airtable Failed*
Order #1043 from orders/create was not fully saved after 1 attempt. Airtable answered 422 INVALID_VALUE_FOR_COLUMN (Field "Quantity" cannot accept the provided value) at step Upsert Order Lines (batch 1 of 1). Webhook wh-1043-create is not marked Processed, so the next orders/updated for this order (or a redelivery) writes it again.
http://localhost:5678/workflow/JwW820HKVPRbCqc7/executions/15113

slack_alert:
:rotating_light: *TEST shopify-orders-sync (temporary)* failed at *Stop: Slack Alert Failed*
Order #1044 is saved in Airtable, but the packing alert (Slack answered HTTP 500 after 3 tries) could not be posted to Slack. Cold Pack is Required. Tell the packing team by hand.
http://localhost:5678/workflow/JwW820HKVPRbCqc7/executions/15147

webhook_notes:
Order #1026 created. Customer created (New Buyer). 2 lines (2 new, 0 updated). Cold Pack Required. Forecast max 37.1 C over the next 3 days in Phoenix, Arizona (cold pack from 27 C). Packing alert sent.
```
