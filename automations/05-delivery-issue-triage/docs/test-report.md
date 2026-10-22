# Test report

**Date:** 2026-10-21, 03:26 UTC
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 81 of 81 checks pass (43 scenarios, plus 5 checks over the whole run)

## How it was tested

No real Airtable base, Shopify store, Claude account, Postmark server or Slack workspace was used. The test script:

1. starts the repo's shared mocks ([tests/mocks](../../../tests/mocks)) on port 8795: the Demo Chocolate Co. base loaded from [base/schema.json](../../../base/schema.json) and [base/seed](../../../base/seed) with every date shifted to today, a Shopify store built from the same seed, Claude, Postmark and Slack, all on a live clock,
2. keeps the Airtable rate limiter on for the whole run, like the real API: a sixth request inside one second gets 429 and blocks the base for 30 seconds. One scenario trips it on purpose right after a ticket is saved, the way a busy second automation would,
3. replaces the shared Shopify mock with a subclass, defined in the test file, that adds `DraftOrder.ready` (the shared mock does not have it yet) and can keep the next draft "calculating" for a set number of seconds; completing a draft that is not ready gives a user error,
4. imports a temporary copy of the workflow into n8n with every API host pointed at the mocks and fake keys, plus an **active** copy of [shared/error-alert-slack.json](../../../shared/error-alert-slack.json) as its error workflow,
5. posts support messages the way a help form or an email parser would, waits for each run to finish, then checks the exact Airtable records, the Shopify calls in order, the draft orders and orders, every email and Slack message, the error alerts, and that every run ended as success or failed only where it should,
6. imports two more temporary copies, one without a support Slack webhook and one without `SUPPORT_FORM_SECRET`,
7. deletes every temporary workflow, whatever happens.

Some scenarios change the seed for the test before they run: #1013, #1014 and #1023 are marked delivered yesterday, two days late, so they qualify for a replacement, and the peanut bar line of #1023 is fully refunded in the Shopify store.

The mocks reject what the real APIs reject: a missing or wrong token, unknown field names, a formula that does not parse, a value of the wrong type, a select option that does not exist, more than 10 records in a write, an unknown GraphQL field or argument, a Postmark sender that is not confirmed or an inactive recipient, and on the Claude side a missing version header, sampling parameters on Claude Sonnet 5, a conversation that ends on an assistant turn, or a forced tool that is not defined. The mock Claude returns scripted tool calls keyed by the customer's message, so the test checks the workflow, not the model, and every scripted answer is checked against the request's tool schema.

### Check against the real Claude API

The mock cannot prove the real API accepts the request, so the first captured request (the #1012 melt claim) was also sent to Anthropic's token counting endpoint. It validates the system prompt, the messages, the strict tool schema, the forced `tool_choice` and the model name, without generating a reply or using credits.

| Model | Result | Input tokens |
|---|---|---|
| `claude-sonnet-5` (default) | 200 OK | 1,342 |

A separate short request with the same forced `tool_choice` on `claude-opus-5-5` was refused by the same endpoint (400, `tool_choice: type "tool" and "any" are not supported for this model.`), which is why the README tells you to keep a model that allows forced tool use.

## Results

```
Forged request, no secret
  PASS  refused with 401, nothing called  (401 {'ok': False, 'error': 'Missing or wrong X-Support-Secret header'})

Forged request, wrong secret
  PASS  refused with 401, nothing called  (401)

Invalid message
  PASS  missing message_id and a bad email give 400 with both reasons, nothing saved  (400 message_id is required; email is missing or not a valid address)
  PASS  a photo that is not https gives 400  (400 every photo needs an https url)
  PASS  a JSON list instead of an object gives 400  (400 Send the message as a JSON object)
  PASS  an unknown channel gives 400  (400 channel must be help_form or email)
  PASS  no ticket was created by any refused request  (4 tickets)

Melted, warm and no cold pack (#1012)
  PASS  webhook answers 200 with the new ticket number  (200 {'ok': True, 'status': 'received', 'ticket': 'T-0005'})
  PASS  the sender has its answer before Claude, Shopify and Postmark are called  (Claude called 1.0 s after the answer)
  PASS  ticket saved with the exact classification, links and outcome  ({})
  PASS  routing reason names the rule that approved it  (Free replacement approved: warm forecast (30.5 C, limit 27 C) and no cold pack, delivered 2 days ago (window 7 days).)
  PASS  Shopify calls in order: read the order and look for an earlier replacement, create, complete  (["ReplacementSource", "CreateReplacement", "CompleteReplacement"])
  PASS  one Shopify draft order with the same lines, 100 percent off, free shipping, the customer, the address and the replacement tag  ({"email": "tom.becker@example.com", "note": "Free replacement for #1012, melted on arrival. Support ticket T-0005.", "tags": ["replacement", "delivery-triage", "ticket-T-0005", "replacement-for-1012"], "lineItems": [{"variantId": "gid://shopify/ProductVariant/4510000000103", "quantity": 2}, {"varian)
  PASS  draft completed into order #1026 at no charge, ids stored on the ticket  (#1026 gid://shopify/DraftOrder/1100000000001)
  PASS  one approval email from support with the replacement number and the ticket reference  (Your free replacement for order #1012)
  PASS  no Slack alert for an automatic approval  (0 Slack posts)
  PASS  one Claude call, forced strict tool, no sampling parameters, accepted by the strict mock  (1 call(s))
  PASS  Action Log has every step, in order  (8 lines)
  PASS  exactly 7 Airtable requests (3 to save, 3 lookups, 1 update), one record per write, no 429  (7 requests)

Same message delivered twice
  PASS  answered as a duplicate of T-0005  (200 {'ok': True, 'status': 'duplicate', 'ticket': 'T-0005'})
  PASS  one Airtable lookup only: no new ticket, no Claude, Shopify, email or Slack, T-0005 unchanged  ({"airtable": 1})

Second message about #1012
  PASS  a person decides, no second replacement for the same order  (#1012 already has an approved replacement (T-0005).)
  PASS  Slack alert for the support queue with the ticket and the reason  (*Support ticket for a person* (Normal priority))

Melted, arrived 3 days late (#1015)
  PASS  approved because it arrived late, even with a mild forecast  (Free replacement approved: arrived 3 days late (expected 2026-10-15, delivered 2026-10-18), delivered 3 days ago (window 7 days).)
  PASS  replacement has both lines of #1015 and the email went out  ([{"variantId": "gid://shopify/ProductVariant/4510000000101", "quantity": 1}, {"variantId": "gid://shopify/ProductVariant/4510000000117", "quantity": 1}])

Melted, cold pack and on time (#1009)
  PASS  no automatic replacement, a person decides  (#1009 shipped with a cold pack and arrived on time, so there is no automatic replacement.)
  PASS  no reply fields on a ticket for a person, the Slack alert is in the Action Log  (Slack alert posted to the support queue)

Melted, outside the claim window (#1003)
  PASS  delivered 20 days ago goes to a person  (#1003 was delivered 20 days ago, outside the 7 day claim window.)

Repeat claimant at the limit (#1021)
  PASS  a customer with 2 approved claims goes to a person, angry gives High priority  (This customer already has 2 approved replacement claims (T-0001, T-0002), the limit is 2. The customer is angry.)

Damaged (#1018)
  PASS  Waiting on Customer, Photo Requested, linked to the order, no Slack alert  (Waiting on Customer)
  PASS  photo request email to the customer  (About your order #1018)

Wrong item (#1020)
  PASS  photo of what arrived requested  (Wrong items need a photo of what arrived first.)

Missing and late (#1022)
  PASS  a person opens a carrier trace, High priority, no email  (#1022 shipped on 2026-10-15, was expected by 2026-10-18 and is 3 days late. Open a carrier trace.)

Missing but in transit (#1017)
  PASS  resolved with tracking details, no Slack alert  (#1017 is in transit and not late yet (expected by 2026-10-22). Tracking details sent.)
  PASS  email has the tracking number and stays in the customer email thread  (Re: Where is my order?)

Allergy question
  PASS  never answered automatically: Human Queue, High priority, no email  (Allergy and ingredient questions are always answered by a person.)
  PASS  customer text in the Slack alert is escaped, so it cannot ping the channel or fake a link  (> My son has a tree nut allergy. Is the raspberry bar safe for him? &lt;!channel&gt; &lt;h)

Safety concern (#1018)
  PASS  Urgent for a person, no replacement even though the order would qualify  (Urgent)

Sender email does not match the order (#1020)
  PASS  nothing automatic, a person checks it  (The sender email does not match the email on #1020, so nothing was sent automatically.)
  PASS  customer matched by email prefers the record with a Shopify id over the duplicate  (jordan, Shopify Customer ID 7700000000103)

No order number, a link typed as the name
  PASS  customer asked for the order number, Waiting on Customer  (No order number in the message, so the customer was asked for it.)
  PASS  the link never reaches the email, the greeting falls back to "there"  (Hi there,)

Order number not found
  PASS  a person looks it up  (Order #9999 was not found in Airtable.)

Damaged with photos attached (#1020)
  PASS  photos saved on the ticket and a person reviews them instead of asking again  (Damage reported with 2 photos attached, ready for a person to review.)

Other question, message id with quotes and a backslash
  PASS  saved exactly and sent to a person  (<odd "quoted" \ id@help.example.com>)
  PASS  the repeat is found by the escaped formula and skipped  ({"ok": true, "status": "duplicate", "ticket": "T-0021"})

Claude down (529)
  PASS  retried 3 times, then sorted by keywords  (3 Claude calls)
  PASS  always goes to a person with the reason, never approved by keywords  (Claude was unavailable (HTTP 529, overloaded_error), so the message was sorted by keywords as Melted. A person must check it.)

Claude answers without the tool call
  PASS  keyword fallback, form order number used, a person decides  (Claude was unavailable (Claude answered without the triage tool call), so the message was sorted by keywords as Damaged. A person must check it.)

Shopify down while creating the replacement (#1018)
  PASS  retried 3 times, then a person creates it, High priority, no approval email  (3 attempts, then: Shopify did not answer the draft order request (HTTP 500, Internal Server Error). Create the replacement by hand and reply to the customer.)
  PASS  the support queue is told in Slack

Email bounces after the replacement was created (#1018)
  PASS  replacement #1028 stays approved, the ticket goes to a person with High priority  (The reply email could not be sent (HTTP 422, Postmark error 406, You tried to send to recipient(s) that have been marked as inactive), so a person must contact the customer.)
  PASS  Slack alert names the replacement so a person can contact the customer  (Replacement: #1028 created)

Shopify still calculating the draft (#1023)
  PASS  waits 2 seconds, checks ready once, then completes it  (["ReplacementSource", "CreateReplacement", "DraftReady", "CompleteReplacement"])
  PASS  only what was not refunded is reshipped: the current quantity, the refunded line left out  ([{"variantId": "gid://shopify/ProductVariant/4510000000114", "quantity": 1}])
  PASS  Action Log shows the skipped line, the wait and the ready check  (Not reshipped (refunded, removed or custom lines): DC-BAR-MILK-PEANUT | Replacement draft #D4 created (gid://shopify/DraftOrder/1100000000004) | Shopify is still calculating draft #D4, checking again in 2 seconds | Draft #D4 is ready after 1 check)

Draft still calculating after 3 checks (#1013)
  PASS  never completed: 3 checks 2 seconds apart, then a person completes it  (["ReplacementSource", "CreateReplacement", "DraftReady", "DraftReady", "DraftReady"])

A replacement already exists in Shopify (#1013)
  PASS  the replacement tag stops a second draft even though Airtable shows no approval  (Shopify already has a replacement for #1013 (draft #D5, not completed), so a second one was not created. Check that replacement before sending another, then reply to the customer.)

Shopify user error on the draft order (#1014)
  PASS  no retry on a user error, a person creates it, High priority  (Shopify did not create the draft order (Product with ID gid://shopify/ProductVariant/4510000000121 is no longer available.). Create the replacement by hand and reply to the customer.)

Shopify throttled (#1014)
  PASS  a throttled order lookup creates nothing and goes to a person with the reason  (Shopify refused the order lookup (Throttled). Create the replacement by hand and reply to the customer.)

Missing, but the carrier says delivered (#1009)
  PASS  High priority for a person to check with the carrier, no email  (The carrier marked #1009 delivered on 2026-10-20, but the customer says it did not arrive. Check with the carrier.)

Missing, not shipped yet (#1024)
  PASS  a person checks the order; line breaks in the name and subject become spaces  (#1024 has not shipped yet.)

Melted claim before delivery (#1017)
  PASS  a person decides, nothing is created  (#1017 is not marked delivered yet.)

Cancelled order, Slack down (#1025)
  PASS  nothing to replace on a cancelled order, a person decides  (#1025 is cancelled, so there is nothing to replace automatically.)
  PASS  Slack tried 3 times, the ticket stays in the Human Queue and the Action Log says the alert failed  (Slack alert failed (HTTP 500, internal_error), the ticket still waits in the Human Queue)

Claude answers outside the schema
  PASS  an issue type that is not in the list falls back to keywords and a person  (Claude was unavailable (Claude returned an incomplete triage), so the message was sorted by keywords as Melted. A person must check it.)

Airtable rate limited while saving
  PASS  the sender gets 503 with Retry-After 60, so the form or parser sends it again  (503 Retry-After 60)
  PASS  run fails and the error alert names the message  (Airtable did not answer while saving support message <e2e-irene-1@help.example.com> (429). The sender got 503 and should send it again. Check AIRTABLE_TOKEN, AIRTABLE_BASE_ID and the Airtable status page.)

Airtable down during the lookups
  PASS  ticket T-0036 is kept at New and the alert says to triage it by hand, no cooldown for a 503  (Ticket T-0036 was saved but not triaged, because Airtable did not answer the order and claim lookups (503). It is still at Status New. Triage it by hand.)

Airtable down when saving the outcome
  PASS  retried 3 times, the alert carries the outcome for manual entry  (Ticket T-0037 was triaged as Human Queue (Routed to Human, Normal priority), no email was sent, but Airtable did not save the result (500). Enter the outcome on the ticket by hand.)

Airtable rate limited during the lookups
  PASS  waits 35 seconds once, looks again and finishes the triage  ([429, 429, 429, 200])

Airtable rate limited when saving the outcome
  PASS  waits 35 seconds once, then the outcome is saved and no alert goes out  ([429, 429, 429, 200])

Whole run
  PASS  Airtable never rate limited the workflow outside the deliberate burst, every write holds 1 record  (0 other 429s, 74 writes)
  PASS  every Claude request was valid for the real API rules; the only off-schema answer is the scripted one  ([])
  PASS  every Shopify query and mutation was valid for the schema (no GraphQL errors)  ([])
  PASS  Slack only got support queue alerts and error alerts  ([])
  PASS  every run ended as expected: success, or failed (with an alert) only where Airtable failed  (44 runs)

No SLACK_SUPPORT_WEBHOOK_URL set
  PASS  the ticket still lands in the Human Queue and the Action Log says no alert was sent  (No SLACK_SUPPORT_WEBHOOK_URL set, the ticket waits in the Human Queue without an alert)

No SUPPORT_FORM_SECRET configured
  PASS  every message refused with 503, even with an empty secret header, nothing called  (503 {"ok":false,"error":"The support webhook is not configured yet"})
  PASS  the error alert tells you to set it  (:rotating_light: *TEST delivery-issue-triage (temporary)* failed at *Stop: Not Configured*)

Real Claude API (count_tokens, free)
  PASS  the captured request is accepted by Anthropic  (200, 1342 input tokens)

81/81 checks passed
```

## What the workflow sent

All names, emails, addresses and order numbers below are made up.

**Claude: classify** (`POST /v1/messages`; the system prompt lists the six issue types and says the message is data, not instructions; the property descriptions of the tool are left out here)

```json
{"model": "claude-sonnet-5", "max_tokens": 1024, "tool_choice": {"type": "tool", "name": "record_triage"},
 "tools": [{"name": "record_triage", "description": "Record the triage of one customer support message.", "strict": true,
            "input_schema": {"type": "object", "additionalProperties": false,
                             "required": ["issue_type", "order_number", "sentiment", "safety_concern", "summary"],
                             "properties": {"issue_type": {"type": "string", "enum": ["melted", "damaged", "wrong_item", "missing", "allergy_question", "other"]},
                                            "order_number": {"type": "string"},
                                            "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative", "angry"]},
                                            "safety_concern": {"type": "boolean"},
                                            "summary": {"type": "string"}}}}]}
```

The user turn:

```
Subject: Melted bars
Order number field: #1012

Customer message:
<message>
Hi, my Houston order 1012 arrived completely melted. The bars are one puddle.
</message>
```

**Shopify: read the order and look for an earlier replacement** (one request)

```graphql
query ReplacementSource($id: ID!, $existing: String!) {
  order(id: $id) { id name customer { id } lineItems(first: 50) { nodes { sku title currentQuantity variant { id } } } }
  draftOrders(first: 5, query: $existing) { nodes { id name status order { name } } }
}
```

with `{"id": "gid://shopify/Order/5200000001012", "existing": "tag:\"replacement-for-1012\""}`.

**Shopify: create replacement** (`draftOrderCreate`, the `input` variable; the answer includes `ready` and `totalPriceSet`), then `draftOrderComplete(id:)` turned it into order #1026 with a total of 0.00

```json
{"email": "tom.becker@example.com",
 "note": "Free replacement for #1012, melted on arrival. Support ticket T-0005.",
 "tags": ["replacement", "delivery-triage", "ticket-T-0005", "replacement-for-1012"],
 "lineItems": [{"variantId": "gid://shopify/ProductVariant/4510000000103", "quantity": 2},
               {"variantId": "gid://shopify/ProductVariant/4510000000116", "quantity": 2}],
 "appliedDiscount": {"valueType": "PERCENTAGE", "value": 100.0, "title": "Melt replacement", "description": "Free replacement for #1012"},
 "shippingAddress": {"firstName": "Tom", "lastName": "Becker", "address1": "48 Harbor Street", "city": "Houston",
                     "provinceCode": "TX", "countryCode": "US", "zip": "77002"},
 "shippingLine": {"title": "Free replacement shipping", "priceWithCurrency": {"amount": "0.00", "currencyCode": "USD"}},
 "purchasingEntity": {"customerId": "gid://shopify/Customer/7700000000113"}}
```

**Airtable: the ticket after triage** (T-0005, links shown as record ids)

```json
{"Ticket ID": "T-0005", "Message ID": "<e2e-tom-1@help.example.com>", "Channel": "Help Form",
 "Customer Name": "Tom Becker", "Customer Email": "tom.becker@example.com",
 "Customer": ["recKnPMk431EATQ5k"], "Order": ["recXhv6n6Ql2CMf0v"], "Order Number Given": "#1012", "Email Matches Order": true,
 "Subject": "Melted bars", "Issue Type": "Melted", "Sentiment": "Negative", "Classification Method": "Claude",
 "Status": "Auto-Approved", "Priority": "Normal", "Resolution": "Replacement Approved",
 "Routing Reason": "Free replacement approved: warm forecast (30.5 C, limit 27 C) and no cold pack, delivered 2 days ago (window 7 days).",
 "Replacement Draft Order ID": "gid://shopify/DraftOrder/1100000000001", "Replacement Order Name": "#1026",
 "Reply Sent At": "2026-10-21T03:21:19.763Z", "Reply Message ID": "a7b391bd-f5f0-5620-ab70-4ad9a978364e"}
```

**Postmark: tracking details** for a parcel in transit, sent into the customer's own email thread

```
From: Demo Chocolate Support <support@example.com>
To: rosa.delgado@example.com
Subject: Re: Where is my order?
Headers: In-Reply-To <CAF-rosa-1@mail.example.com>, References <CAF-rosa-1@mail.example.com>

Hi Rosa,

Good news, your order #1017 is on its way. It left us on October 19 and the carrier expects to deliver it by October 22.

Tracking number 1Z999AA10000001017

If it has not arrived by then, reply to this email and we will look into it straight away. Your reference is T-0014.

Demo Chocolate Co. Support
```

**Postmark: photo request** for a damaged parcel

```
Subject: About your order #1018

Hi Victor,

I am sorry your order #1018 arrived damaged. Could you reply to this email with a photo of the box and the bars as they arrived? As soon as we have it, we will sort out a replacement or a refund for you.

Your reference is T-0011.

Demo Chocolate Co. Support
```

**Slack: allergy question**, with the customer's `<!channel>` and fake link escaped

```
*Support ticket for a person* (High priority)
*T-0015* from Rosa Delgado (rosa.delgado@example.com) via Help Form
Issue: Allergy Question (Claude), sentiment Neutral
Order: none given
Why: Allergy and ingredient questions are always answered by a person.
Summary: Asks if the raspberry bar is safe with a tree nut allergy.
> My son has a tree nut allergy. Is the raspberry bar safe for him? &lt;!channel&gt; &lt;https://evil.example.com|click&gt;
```

**Slack: replacement created but the email bounced**

```
*Support ticket for a person* (High priority)
*T-0025* from Victor Hale (victor.hale@example.com) via Help Form
Issue: Melted (Claude), sentiment Negative
Order: #1018, email matches
Replacement: #1028 created
Why: Free replacement approved: arrived 1 day late (expected 2026-10-16, delivered 2026-10-17), delivered 4 days ago (window 7 days). The reply email could not be sent (HTTP 422, Postmark error 406, You tried to send to recipient(s) that have been marked as inactive), so a person must contact the customer.
Summary: Customer reports a delivery problem.
> Writing again about the melted bars from 1018.
```

**Error alerts** from the shared error workflow (posted to the ops channel)

```
:rotating_light: *TEST delivery-issue-triage (temporary)* failed at *Stop: Airtable Unavailable*
Airtable did not answer while saving support message <e2e-irene-1@help.example.com> (429). The sender got 503 and should send it again. Check AIRTABLE_TOKEN, AIRTABLE_BASE_ID and the Airtable status page.
http://localhost:5678/workflow/9j2YiBBsYOtYG8yq/executions/14501

:rotating_light: *TEST delivery-issue-triage (temporary)* failed at *Stop: Lookup Failed*
Ticket T-0036 was saved but not triaged, because Airtable did not answer the order and claim lookups (503). It is still at Status New. Triage it by hand.
http://localhost:5678/workflow/9j2YiBBsYOtYG8yq/executions/14507

:rotating_light: *TEST delivery-issue-triage (temporary)* failed at *Stop: Ticket Not Saved*
Ticket T-0037 was triaged as Human Queue (Routed to Human, Normal priority), no email was sent, but Airtable did not save the result (500). Enter the outcome on the ticket by hand.
http://localhost:5678/workflow/9j2YiBBsYOtYG8yq/executions/14512
```

## What these tests do not cover

- Real model behavior. The mock Claude answers from a script, so the quality of the classification is not measured here. The rules never let a classification approve anything on its own.
- How long real Shopify takes to calculate a draft, and the exact error Shopify gives when a draft that is not ready is completed (Shopify does not document it). The test subclass makes a draft calculate for 1 second or 2 minutes; the workflow never completes a draft before `ready` is true, so that error text does not matter to it.
- Shopify's search lag. The mock finds a tagged draft the moment it exists; real Shopify search can trail a new draft by a few seconds.
- Two copies of one message arriving at the same moment, and two new messages saved in the same moment (see the limitations in the README). With the Airtable limiter on, two runs in the same second would also trip the 5 requests per second limit.
- Runs that cross midnight UTC. Day counts use UTC calendar days in both the workflow and the mocks, so start the test a few minutes away from 00:00 UTC.
