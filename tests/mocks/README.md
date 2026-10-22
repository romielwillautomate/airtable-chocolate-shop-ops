# Shared mocks: the contract for every automation's tests

Every end to end test in this repo runs the real workflow in n8n against these mock APIs, never against Airtable, Shopify, Open-Meteo, Postmark, Slack or Claude. The mocks are strict: they reject requests the real APIs reject, with the real error shapes, so a passing test means the request would also work for real. Each rule below was checked against the official API docs in October 2026 (Shopify Admin GraphQL 2026-10). Where the docs state a rule but not the exact error body, the mock enforces the rule and says so below; where the docs are silent, the mock picks the stricter behavior.

## Changes after the October 2026 review (read these if you started before)

| Change | What to do |
|---|---|
| A stored number 0 is now returned (it was dropped like an empty cell). | Read `fields['Quantity Reserved']` as 0, not missing. Write 0 when you mean zero. |
| `fieldsToMergeOn` takes only the documented types: number, single line text, long text, single select, multiple select, date. Email, URL, phone, currency, percent, dateTime, checkbox and links answer 422. | Never upsert Customers on `Email`. Look the customer up (see the 01 seed facts) and update by record id or create. Every other upsert key in this base is single line text. |
| Two existing records matching the merge key now answer Airtable's real body: 422 `INVALID_VALUE_FOR_COLUMN` `Cannot update more than one record for fields to merge on`. | Match on that type and message, not on `INVALID_REQUEST_UNKNOWN`. |
| More than 10 records: the message is now Airtable's (`A maximum of 10 records can be created per request but you have provided 11.`). | Nothing, unless you matched the old text. |
| A GET list URL of 16,000 characters or more answers 414. | Use `POST /v0/{base}/{table}/listRecords` with the parameters in the body for long formulas. |
| Shopify: a key whose first `inventorySetQuantities` attempt returned userErrors is used up: the same key again gives `IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED` (same input) or `IDEMPOTENCY_KEY_PARAMETER_MISMATCH` (new input). | Make a new key for every new attempt, for example `<run id>-<sku>-<changeFromQuantity>`. |
| Shopify: a search field the mock does not implement is now an error instead of an empty result, and `sortKey` works on products, variants and orders. | If you see `does not support the search field`, filter in a Code node or ask for the field to be added. |
| Shopify: `nodes(ids:)` and `EmailInput.body` exist now. A fragment spread where its type cannot appear, a fragment that is never used, and a non string `@idempotent` key are now errors, as in graphql-ruby. | Remove unused fragments from your queries. |
| Orders `Fulfillment Status` gained `Not Eligible` (REST `fulfillment_status` can be `not_eligible`) and `Financial Status` gained `Expired` (the GraphQL `EXPIRED`). | Map every value Shopify documents (financial: pending, authorized, partially_paid, paid, partially_refunded, refunded, voided; fulfillment: null, fulfilled, partial, not_eligible, restocked) without `typecast`. |
| `DC-BAR-PUMPKIN-SPICE` is in the Shopify store (available 40) and in Airtable as a Draft with no Shopify ids. The old text said "Shopify only". | The inventory sync must skip it and must not set its stock to 0. |
| `prepare_workflow` also refuses a host built at run time (`'https://' + ...`, `` `https://${...}` ``, `https://{{ $json.x }}`) and an HTTP Request URL that is not a mocked API. | Write URLs exactly as in rule 4. |
| `DATETIME_FORMAT` knows the moment tokens `GGGG`, `WW`, `W`, `ww`, `w`, `Q`, `Do`, `DDDD`, `E`, `m`, `s`; `WEEKDAY` and `WEEKNUM` take the "Monday" argument. | `DATETIME_FORMAT(TODAY(), 'GGGG-[W]WW')` gives `2026-W43`. |
| Anthropic `count_tokens` no longer applies generation-only rules (prefill, `budget_tokens`, thinking with a forced tool), like the real endpoint. `/v1/messages` still does. | Nothing. |
| `base/seed/_meta.json` gained `notOnShopifyNote` (02), `customerMatching` (01), `customersWithoutCountedOrders` and `existingFindingNote` (06). `SeedFactTests` in `test_mocks.py` recomputes the expected facts from the loaded base. | Read them; they are facts, not new rules. |

## Changes during the integration (2026-10-22)

All three are backward compatible: every automation's test passed again after them.

| Change | What to do |
|---|---|
| `mock.lose_answer(service, match=None, times=1, status=None)` is part of the shared mock now. 02 and 04 each had their own copy in a subclass; both use the shared one. | Use it to prove that a write is safe to replay: the request is handled for real, then the answer is dropped (`status=None`) or replaced with that status and the service's error body. See [Failure injection](#failure-injection). |
| `run.alert_for(ex)` waits up to 90 seconds by default (it was 30), because a busy n8n can start the error workflow late. With the default, a miss prints the error workflow's runs to stderr. | Pass a short timeout, for example `run.alert_for(ex, timeout=5)`, when you expect no alert. Nothing is printed then. |
| `N8n()` stops with a clear message when `N8N_ENV_FILE` is set but is not a file this Python can open. Before, it said "Set N8N_API_KEY", which hid the real problem (a Git Bash path such as `/c/Users/...` handed to Windows Python). | Write the path as `C:/Users/...`. |

## Modules

| Module | Mocks |
|---|---|
| `core.py` | `MockServer`: one HTTP server for every mock, the request log, failure injection and the clock |
| `airtable.py`, `formula.py` | Airtable Web API and Metadata API for one base, loaded from `base/schema.json` and `base/seed`, with a filterByFormula engine |
| `shopify.py`, `graphql.py` | Shopify Admin GraphQL (a strict GraphQL engine with a subset of the real schema), webhook signing helpers |
| `open_meteo.py` | Open-Meteo geocoding and daily forecast |
| `postmark.py` | Postmark single and batch email |
| `slack.py` | Slack incoming webhooks |
| `anthropic.py` | Claude Messages API and token counting, with scripted tool calls |
| `n8n.py` | Test helpers: prepare a workflow for the mocks, run temporary copies in n8n, wait for runs, match error alerts |
| `server.py` | Run every mock from the command line |
| `test_mocks.py` | Unit tests for all of the above: `python -m unittest tests.mocks.test_mocks -v` |

## Quick start

```python
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]          # automations/NN-slug/tests/e2e_mock_test.py -> repo root
sys.path.insert(0, str(ROOT))
from tests.mocks import MockServer, AIRTABLE_TOKEN
from tests.mocks.n8n import N8n, Report, mock_env, prepare_workflow

SLUG, PORT = 'shopify-orders', 8791                  # your slug and your own port
WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow' / 'shopify-orders.json'

mock = MockServer(port=PORT).start()                 # seeded base, seed dates shifted to today, live clock
n8n = N8n()                                          # N8N_URL (default http://localhost:5678), N8N_API_KEY
n8n.cleanup(SLUG)                                    # only deletes workflows named exactly "TEST shopify-orders (temporary)"
report = Report()
try:
    wf = prepare_workflow(WORKFLOW, mock, SLUG, mock_env(mock))
    with n8n.temporary(wf, SLUG, mock=mock) as run:  # also creates and ACTIVATES the temporary error workflow
        payload = mock.shopify.order_payload(1026, 'new.buyer@example.com',
                                             [{'sku': 'DC-BAR-DARK-SEASALT', 'quantity': 2, 'price': 7.5}],
                                             shipping_address={'city': 'Phoenix', 'province_code': 'AZ',
                                                               'country_code': 'US', 'zip': '85004', 'address1': '1 Test Way'},
                                             customer={'name': 'New Buyer'})
        mark = mock.mark()
        status, body = run.post_shopify('orders', 'orders/create', payload, webhook_id='wh-1')
        ex = run.wait()
        order = mock.airtable.by_primary('Orders', '#1026')
        report.check('New order', 'one order upserted', order and order['fields']['Cold Pack'] == 'Required')
        report.check('New order', 'batched writes', all(e['record_count'] <= 10 for e in mock.log('airtable', since=mark) if 'record_count' in e))
        mock.fail('airtable', status=500, times=3, match={'table': 'Orders', 'method': 'PATCH'})
        ...
        alert = run.alert_for(ex_failed)             # the error workflow's Slack text for that run, or None
finally:
    mock.stop()
ok = report.print()
sys.exit(0 if ok else 1)
```

Run it with `N8N_API_KEY` set (or `N8N_ENV_FILE` pointing at a `.env` that has it) and, when n8n does not run in Docker, `MOCK_HOST=127.0.0.1`. The mocks use only the Python standard library. On Windows, `zoneinfo` has no time zone data of its own, so install `tzdata` once (`pip install tzdata`); without it every Open-Meteo forecast request gets 400 "Invalid timezone".

## Rules for every builder

1. **Your own port.** Each automation's test starts its own `MockServer(port=...)`. Never share a port with a test that may run at the same time.
2. **Your own workflows only.** Temporary workflows are named exactly `TEST <slug> (temporary)` (the error workflow copy gets the same name) and webhook paths start with `<slug>-e2e-`. `n8n.cleanup(slug)` before you start, and `n8n.temporary()` deletes both workflows in `finally`. Never touch any other workflow.
3. **Keep the clock live in end to end tests.** n8n uses the real time, so the mock must too. The seed is already shifted so its "today" is today (UTC). Freeze the clock (`mock.clock.set(...)` then `mock.reset()`) only in tests that never go through n8n. Compute dates in UTC in workflows: a test run near midnight can flake otherwise.
4. **Write API URLs exactly like this**, so `prepare_workflow` can point them at the mock:

   | Service | URL in the workflow |
   |---|---|
   | Airtable | `https://api.airtable.com/v0/{{ $env.AIRTABLE_BASE_ID }}/Production%20Batches` (table names, URL encoded) |
   | Airtable Metadata | `https://api.airtable.com/v0/meta/bases/{{ $env.AIRTABLE_BASE_ID }}/tables` |
   | Shopify | `https://{{ $env.SHOPIFY_STORE_DOMAIN }}/admin/api/{{ $env.SHOPIFY_API_VERSION }}/graphql.json` |
   | Open-Meteo | `https://geocoding-api.open-meteo.com/v1/search` and `https://api.open-meteo.com/v1/forecast` |
   | Postmark | `https://api.postmarkapp.com/email` (or `/email/batch`) |
   | Slack | `{{ $env.SLACK_WEBHOOK_URL }}`, `{{ $env.SLACK_PACKING_WEBHOOK_URL }}`, `{{ $env.SLACK_SUPPORT_WEBHOOK_URL }}` |
   | Claude | `https://api.anthropic.com/v1/messages` |

   Make API calls from HTTP Request nodes, not by building URLs in Code nodes. `prepare_workflow` raises `ValueError` if any node (sticky notes excepted) still holds a real API URL after the rewrite, if a host is built at run time (`'https://' + host`, `` `https://${host}` ``, `https://{{ $json.host }}`), or if an HTTP Request node's literal URL is not one of the mocked APIs, so a forgotten host can never reach a real service. A URL that comes whole from an expression (`={{ $env.SLACK_WEBHOOK_URL }}`, `={{ $json.url }}`) is allowed.
5. **Read config with `$env.NAME`.** `prepare_workflow` replaces every `$env.NAME` (and `$env['NAME']`) with the JSON quoted test value from the `env` you pass, because the n8n container does not have these variables. Values are strings, as in n8n.
6. **Use these variable names** (they are also in `.env.example`). `mock_env(mock, **overrides)` returns all of them:

   | Variable | Mock value | Used by |
   |---|---|---|
   | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID` | `AIRTABLE_TOKEN`, `appDemoChocolate1` | all |
   | `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_API_VERSION` | `demo-chocolate.myshopify.com`, `shpat_mock_demo_chocolate_0000000000000000`, `2026-10` | 01, 02, 03, 05 |
   | `SHOPIFY_WEBHOOK_SECRET` | `mock-shopify-client-secret` | 01 |
   | `SHOPIFY_LOCATION_ID` | `gid://shopify/Location/71000000001` | 02 |
   | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | `mock-anthropic-key`, `claude-sonnet-5` | 03, 05 |
   | `POSTMARK_SERVER_TOKEN`, `POSTMARK_MESSAGE_STREAM` | `mock-postmark-server-token`, `outbound` | 03, 05 |
   | `EMAIL_FROM_WHOLESALE`, `EMAIL_FROM_SUPPORT` | `Demo Chocolate Wholesale <wholesale@example.com>`, `Demo Chocolate Support <support@example.com>` | 03, 05 |
   | `SLACK_WEBHOOK_URL` | mock path `/services/T000MOCK/B000MOCK/alerts` (ops alerts) | all |
   | `SLACK_PACKING_WEBHOOK_URL` | mock path `.../packing` (melt alerts, box summary) | 01, 04 |
   | `SLACK_SUPPORT_WEBHOOK_URL` | mock path `.../support` (human queue) | 05 |
   | `RUN_NOW_SECRET` | `mock-run-now-secret` (header `X-Run-Now-Secret` on Run Now webhooks) | 02, 03, 04 |
   | `WHOLESALE_FORM_SECRET`, `SUPPORT_FORM_SECRET` | `mock-wholesale-form-secret`, `mock-support-form-secret` | 03, 05 |
   | `MELT_TEMP_C`, `MELT_FORECAST_DAYS` | `27`, `3` | 01, 05 |
   | `SELL_BY_BUFFER_DAYS`, `PROMO_WINDOW_DAYS`, `PROMO_DISCOUNT_PERCENT` | `14`, `45`, `25` | 02 |
   | `QUOTE_VALID_DAYS` | `14` | 03 |
   | `REPEAT_WINDOW` | `1` | 04 |
   | `CLAIM_WINDOW_DAYS`, `CLAIM_LIMIT` | `7`, `2` | 05 |

   The error workflow (`shared/error-alert-slack.json`) posts to `SLACK_WEBHOOK_URL`; in tests it goes to the mock path `/services/T000MOCK/B000MOCK/errors` instead, so error alerts never mix with your workflow's own alerts.
7. **Pace Airtable.** The limiter is on by default: the 6th request inside any one second gets 429 and the base is then blocked for 30 seconds, like the real API. Use 10 records per write and a 250 ms gap between Airtable calls (4 per second). When two runs of the same workflow can overlap (a webhook, or Run Now during a scheduled run), use 500 ms: 02 measured that two runs at 250 ms both hit 429. Switch it off only in a scenario that is not about Airtable, with `mock.airtable.set_rate_limit(enabled=False)`, and use `set_rate_limit(per_second=5, penalty=2)` to test the 429 path quickly.
8. **Assert strictly.** Check exact records and exact request counts through `mock.log(...)` and the direct helpers, and check `mock.anthropic.problems == []` after every Claude scenario.

## Starting, resetting and the clock

```python
mock = MockServer(port, host='127.0.0.1', public_host=None, schema=..., seed=..., seed_today='auto', empty_base=False, now=None)
mock.start(); mock.stop()            # also usable as a context manager
mock.url                             # http://host.docker.internal:<port>  (what n8n uses; MOCK_HOST overrides the host)
mock.local_url                       # http://127.0.0.1:<port>             (what your test uses)
mock.reset()                         # fresh seed, empty request log, failures and scripts cleared; the clock is kept
mock.reset(seed_today=None)          # reload with the dates exactly as written in base/seed
mock.reset(empty_base=True)          # no tables at all (to try setup/create_base.py)
mock.clock.set('2026-10-20T09:00:00Z'); mock.clock.advance(days=1); mock.clock.live(); mock.clock.today()
mock.seed_shift_days                 # how far the seed dates were moved (today minus 2026-10-20)
mock.seed_date('2026-11-04')         # a date from base/seed or _meta.json, moved the same way
```

With `seed_today='auto'` (the default) every `date` and `dateTime` in the seed, and the seeded Shopify orders, move by `mock.seed_shift_days`, so every relative fact in `base/seed/_meta.json` holds today. Text that contains a date (batch codes such as `B-DSS-260523`, box months such as `2026-10`, Run IDs) does not move. Airtable `TODAY()`, `NOW()`, record `createdTime`, Open-Meteo forecast days and Shopify timestamps all follow `mock.clock`.

## Failure injection

```python
mock.fail(service, status=500, times=1, body=None, delay=None, drop=False, match=None, headers=None)
mock.clear_failures()
```

| Argument | Meaning |
|---|---|
| `service` | `airtable`, `shopify`, `open-meteo-geocoding`, `open-meteo-forecast`, `postmark`, `slack`, `anthropic` |
| `status` | HTTP status to answer. The body defaults to that service's real error shape for the status (Airtable 429 is `{"error": {"type": "RATE_LIMIT_REACHED", "message": "Rate limit exceeded. Please try again later"}}`, Anthropic 529 is `overloaded_error`, Postmark 401 is `ErrorCode 10`, Slack answers plain text). |
| `times` | How many matching requests fail. `-1` means every one until `clear_failures()`. |
| `delay` | Seconds to wait before answering. With `status=None` the normal answer is sent after the delay, so set a node timeout shorter than the delay to test a timeout. |
| `drop` | Close the connection with no answer. |
| `match` | A dict of log entry keys (`method`, `path`, `table`, `operation`, `path_contains`, `body_contains`, or any key in the table below), or a callable `entry -> bool`. |

Remember that `retryOnFail` with `maxTries: 3` sends 3 requests, so `times=3` exhausts the retries and `times=1` tests recovery.

`mock.fail(service, status=200, body={...})` answers with that body as it is, without the mock's request checks for that one call. Use it for a raw success answer the scripts do not cover, for example a Claude refusal (`stop_reason` `refusal`), as 03 does.

A lost answer is the opposite of an injected failure: the change is applied, only the answer goes missing.

```python
mock.lose_answer(service, match=None, times=1, status=None)
```

| Argument | Meaning |
|---|---|
| `service` | As for `fail` |
| `match` | As for `fail` (a dict of log entry keys or a callable). It is only asked about requests the service answered with a 2xx, because only those changed anything, so a stateful callable counts successful requests only |
| `times` | How many answers to lose. `-1` means every matching one until `clear_failures()` or `reset()` |
| `status` | `None` closes the connection with no answer (n8n sees a network error and retries). A number sends that status with the service's real error body instead of the real answer |

The log entry gets `answer_lost: True`, `status` (what the client got, `None` when dropped) and `handled_status` (what the service answered). 02 uses it to prove that a Shopify stock update is applied once when n8n retries with the same idempotency key; 04 uses it to prove that a box saved with its answer lost is found again instead of saved twice or undone.

## The request log

`mark = mock.mark()` before an action, then `mock.log(service=None, since=mark, **match)` returns the requests after it, oldest first. Every entry has `seq, time, service, method, path, query (dict of lists), headers (lowercase keys), body (parsed JSON or text), raw (bytes), status, response`, plus `injected: True` when a failure answered it, plus:

| Service | Extra keys |
|---|---|
| airtable | `api` (`web` or `meta`), `table` (name, even when the URL used the id), `operation` (`list`, `get`, `create`, `update`, `replace`, `upsert`, `delete`, `meta.get_schema`, `meta.create_table`, `meta.create_field`, `meta.update_field`, ...), `record_count` on writes, `error_type` when it failed |
| shopify | `api_version`, `query`, `variables`, `operation_type` (`query` or `mutation`), `operation` (operation name), `root_fields`, `graphql_errors`, `throttled`, `idempotent_replay` |
| open-meteo-geocoding, open-meteo-forecast | `params` (query parameters, last value of each) |
| postmark | `to`, `subject`, `message_id` |
| slack | `text` |
| anthropic | `model`, `tool` (the forced tool name), `user_text` (last user message text), `operation` (`messages` or `count_tokens`), `problem` when rejected, `schema_problems` |

Unknown paths answer 404 `{"error": "NOT_MOCKED"}` and are logged with service `unknown`.

## Airtable

Base id `appDemoChocolate1`, token `AIRTABLE_TOKEN` (import it from `tests.mocks`), header `Authorization: Bearer <token>`. Tables and fields are exactly those in `base/schema.json`; refer to them by name.

What it enforces, with the real responses:

| Situation | Answer |
|---|---|
| No or wrong token | 401 `AUTHENTICATION_REQUIRED` |
| Wrong base id, unknown table | 403 `INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND` |
| More than 5 requests in one second | 429 `RATE_LIMIT_REACHED`, then 429 for 30 seconds |
| More than 10 records in a create, update, upsert or delete; empty `records` | 422 `INVALID_RECORDS` `A maximum of 10 records can be created per request but you have provided 11.` (`updated`, `deleted`) |
| A GET list URL of 16,000 characters or more (Airtable's documented limit; the real status is not documented) | 414 `URL_TOO_LONG`. Use `POST .../listRecords`. |
| Unknown field name (writes, `fields[]`, `sort`) | 422 `UNKNOWN_FIELD_NAME` `Unknown field name: "X"` |
| Wrong type (text into a number, a date into a dateTime, a number into a checkbox ...) | 422 `INVALID_VALUE_FOR_COLUMN` |
| A select option that does not exist (without `typecast`) | 422 `INVALID_MULTIPLE_CHOICE_OPTIONS` |
| A link value that is not a record id (without `typecast`) / a record id that does not exist | 422 `INVALID_VALUE_FOR_COLUMN` / 422 `ROW_DOES_NOT_EXIST` |
| Updating, reading or deleting a record id that does not exist | 404 `MODEL_ID_NOT_FOUND` `Record not found` |
| Bad formula / unknown field in a formula | 422 `INVALID_FILTER_BY_FORMULA` (`Unknown field names: x`) |
| Unknown query or body parameter, `pageSize` over 100 | 422 `INVALID_REQUEST_UNKNOWN` |
| Bad offset / expired offset (`mock.airtable.expire_iterators()`) | 422 `INVALID_OFFSET_VALUE` / 422 `LIST_RECORDS_ITERATOR_NOT_AVAILABLE` |
| Upsert where two existing records match `fieldsToMergeOn` (the body Airtable really sends) | 422 `INVALID_VALUE_FOR_COLUMN` `Cannot update more than one record for fields to merge on` |
| Upsert where two records in the same request share a key (not documented; the mock refuses) | 422 `INVALID_REQUEST_UNKNOWN` |
| `fieldsToMergeOn` with a field type outside the documented list (email, URL, phone, currency, percent, dateTime, checkbox, link...) or more than 3 fields (the rule is documented, the body is not) | 422 `INVALID_REQUEST_UNKNOWN` `Field "Email" of type email cannot be used in fieldsToMergeOn...` |
| Writing a computed field type | 422 `INVALID_VALUE_FOR_COLUMN` |

Behaviour:

- **List**: `GET /v0/{base}/{table}` with `pageSize`, `maxRecords`, `offset`, `sort[0][field]`/`sort[0][direction]`, `fields[]` (also `fields[0]` or repeated `fields`), `view`, `filterByFormula`, `cellFormat`, `timeZone`, `userLocale`, `returnFieldsByFieldId`, `recordMetadata`; or `POST /v0/{base}/{table}/listRecords` with the same names in a JSON body. Pages hold at most 100 records; `offset` is present until the last page. This n8n HTTP Request pagination setup is tested against the mock:
  ```json
  "options": { "pagination": { "pagination": {
    "paginationMode": "updateAParameterInEachRequest",
    "parameters": { "parameters": [ { "type": "qs", "name": "offset", "value": "={{ $response.body.offset }}" } ] },
    "paginationCompleteWhen": "other", "completeExpression": "={{ !$response.body.offset }}",
    "limitPagesFetched": true, "maxRequests": 20, "requestInterval": 250 } } }
  ```
  It returns one item per page (each with `records`), so read every page with `$('Node').all()`.
- **Empty values are left out** of returned records (unticked checkbox, empty text, empty link). **A number 0 is a value** and is returned as `0`; `null` clears a cell.
- **Create** `POST` with `{"records": [{"fields": {...}}], "typecast": false}` (or a single `{"fields": {...}}`). **Update** `PATCH` (only the given fields) or `PUT` (clears every other cell, links included) with `{"records": [{"id", "fields"}]}`. **Upsert** `PATCH` with `{"performUpsert": {"fieldsToMergeOn": ["Shopify Order ID"]}, "records": [{"fields": {...}}]}`: 1 to 3 merge fields of the documented types (number, single line text, long text, single select, multiple select, date), each record must have a value for them, matching is exact (case and spaces count), the answer adds `createdRecords` and `updatedRecords` (record ids). **Delete** `DELETE ?records[]=rec...` (at most 10). Every multi record write is all or nothing.
- **What n8n shows for an Airtable error** (checked on n8n 2.19.5, HTTP Request 4.2 with `onError: continueErrorOutput`, no pagination): the error output item is the node's INPUT item plus `error: {message, name: "AxiosError", code, status, stack}`. `error.status` is the HTTP status, and `error.message` is `422 - "<the response body as JSON text>"`, so the Airtable type is `JSON.parse(JSON.parse(m.slice(m.indexOf(' - ') + 3))).error.type` (wrap it in try/catch). Because the input item's fields are still there, never read `$json.records` on an error branch as if it were this call's answer. With pagination on, the status is in `error.statusCode` or `error.response.status` instead (see the lessons below).
- **Links** are arrays of record ids. Writing one side updates the other side (`Orders.Customer` and `Customers.Orders`). With `typecast: true`, a link value may be the linked table's primary field value; a missing one is created, like Airtable does.
- **typecast** also turns `"2"` into a number, `"true"` into a checkbox, creates missing select options, and turns a date string into a `date`.
- **Dates**: `date` fields take `YYYY-MM-DD`. `dateTime` fields take a full ISO time (`2026-10-20T14:05:00Z` or with an offset) and come back as `2026-10-20T14:05:00.000Z`. A bare date into a `dateTime` field is refused without `typecast`.
- **Metadata API**: `GET /v0/meta/bases`, `GET /v0/meta/bases/{base}/tables`, `POST .../tables` (first field must be a primary field type), `PATCH .../tables/{id}`, `POST .../tables/{id}/fields`, `PATCH .../tables/{id}/fields/{id}` (name and description only). Field options are validated like the real API: checkbox needs `icon` and a `...Bright` color, number needs `precision`, currency needs `precision` and `symbol`, date needs `dateFormat.name`, dateTime needs `timeZone`, `dateFormat` and `timeFormat`, selects need `choices` (each `name`, optional `color` and `id`), links take only `linkedTableId` (and `viewIdForRecordSelection`), attachments take no options. Barcode fields cannot be created (their cells can be written). Formula, rollup, lookup, count, autonumber and the other computed types answer 422 `UNSUPPORTED_FIELD_TYPE_FOR_CREATE`: the current Create field reference does show request bodies for formula, rollup and lookup fields, but this base uses none and the mock cannot compute them. A new link creates the inverse field in the other table, named after the source table.

**filterByFormula** supports `{Field Name}` (and bare names without spaces), `'single'` or `"double"` quoted strings with backslash escapes, numbers, `= != < > <= >= & + - * /`, and these functions: `IF SWITCH AND OR NOT XOR BLANK TRUE FALSE ERROR ISERROR LOWER UPPER TRIM LEN LEFT RIGHT MID FIND SEARCH SUBSTITUTE REPLACE CONCATENATE REPT T VALUE REGEX_MATCH REGEX_EXTRACT REGEX_REPLACE ENCODE_URL_COMPONENT ABS ROUND ROUNDUP ROUNDDOWN INT CEILING FLOOR MOD MAX MIN SUM AVERAGE COUNT COUNTA COUNTALL TODAY NOW IS_BEFORE IS_AFTER IS_SAME DATETIME_DIFF DATEADD DATETIME_PARSE DATETIME_FORMAT DATESTR YEAR MONTH DAY HOUR MINUTE WEEKDAY WEEKNUM RECORD_ID CREATED_TIME LAST_MODIFIED_TIME ARRAYJOIN ARRAYUNIQUE ARRAYCOMPACT ARRAYFLATTEN`. An unknown function is a syntax error (422). Value rules that catch people out, all as in Airtable:

- Text comparison with `=` is **case sensitive**. FIND and SEARCH are both treated as case sensitive (Airtable does not document SEARCH as case insensitive), so compare `LOWER(TRIM({Email})) = 'x@example.com'`.
- A **link field evaluates to the primary field values** of the linked records, never their ids: `ARRAYJOIN({Subscriber}) = 'SUB-0001'` works, `FIND('rec...', ARRAYJOIN({Customer}))` never matches. To filter by a linked record, use its primary value or a plain text copy (`Order Lines.SKU`, `Purchasing Requests.SKU`, `Box History.Box Month`).
- `BLANK()` equals an empty value, `''`, `0` and an unticked checkbox; a multiple select or link used as text is joined with `, `.
- `DATETIME_DIFF(a, b, 'days')` is `a - b` truncated to a whole number; `TODAY()` is the mock clock's UTC date at midnight. So for a `dateTime` such as `Delivered At` two calendar days ago at 17:45 UTC, `DATETIME_DIFF(TODAY(), {Delivered At}, 'days')` is **1**, not 2. Compare calendar days with `DATETIME_DIFF(TODAY(), DATETIME_PARSE(DATETIME_FORMAT({Delivered At}, 'YYYY-MM-DD')), 'days')`, with `IS_SAME(..., 'day')`, or in a Code node on the `YYYY-MM-DD` part.
- `DATETIME_FORMAT` uses moment.js tokens in UTC: `YYYY YY GGGG MMMM MMM MM M Q Do DDDD DDD DD D dddd ddd dd d E e WW W ww w HH H hh h mm m ss s SSS A a ZZ Z x X` and `[literal text]`; other letters are copied as they are. `DATETIME_FORMAT(TODAY(), 'GGGG-[W]WW')` is the ISO week (`2026-W43`). `WEEKDAY(d, 'Monday')` and `WEEKNUM(d, 'Monday')` start weeks on Monday; the default is Sunday, and week 1 of `WEEKNUM` contains January 1.
- An evaluation error on one record (text times a number) just leaves that record out.

Direct helpers for assertions and setup (no HTTP, no rate limit, not logged):

```python
mock.airtable.records('Orders')                 # every record, shaped like the API: {'id', 'createdTime', 'fields'}
mock.airtable.record('Orders', rec_id)
mock.airtable.by_primary('Orders', '#1012')     # the record, a list if several share the value, None if none
mock.airtable.find('Customers', Email='derek.olsen@example.com')     # fields equal to the given values
mock.airtable.where('Products', "FIND('Tree Nuts', {May Contain Traces})")
mock.airtable.count('Box Shipments')
mock.airtable.ref('Customers', 'derek')         # record id of a seed record by its seed ref
mock.airtable.primary_name('Orders', rec_id)
mock.airtable.insert('Tickets', {...}); mock.airtable.update('Quotes', rec_id, {'Status': 'Approved'})
mock.airtable.set_rate_limit(per_second=5, penalty=30.0, enabled=True); mock.airtable.unblock(); mock.airtable.expire_iterators()
```

## Shopify Admin GraphQL

`POST /admin/api/{version}/graphql.json` with `X-Shopify-Access-Token: shpat_mock_demo_chocolate_0000000000000000` and `{"query", "variables", "operationName"}`. Versions `2026-04`, `2026-07`, `2026-10` (default); others answer 404. Missing token: 401 `{"errors": "[API] Invalid API key or access token ..."}`.

Like the real API, a query that uses an unknown field or argument, misses a required argument or passes a variable of the wrong shape gets HTTP 200 with `errors` (graphql-ruby messages and `extensions.code` such as `undefinedField`, `argumentNotAccepted`, `missingRequiredArguments`, `cannotSpreadFragment`, `useAndDefineFragment` for a fragment that is defined but never used, `variableNotUsed`) and no `data`. Every answer carries `extensions.cost` (`requestedQueryCost`, `actualQueryCost`, `throttleStatus` with `maximumAvailable` 1000, `currentlyAvailable`, `restoreRate` 50). A query over 1000 points gets `MAX_COST_EXCEEDED`; an empty bucket or `mock.shopify.throttle(times)` gets `{"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}`.

Supported fields:

- Queries: `shop`, `node`, `nodes(ids:)` (at most 250, `null` for an unknown id, use `... on InventoryItem { }`), `location`, `locations`, `product(s)`, `productVariant(s)`, `inventoryItem(s)` (with `inventoryLevel(locationId:)` and `quantities(names: [...])`), `order(s)` (with `lineItems`, `customer`, `shippingAddress`), `draftOrder(s)`, `customer(s)`. Connections need `first` or `last` (at most 250) and support `after`, `reverse`, `nodes`, `edges`, `pageInfo`; `products`, `productVariants` and `orders` take the documented `sortKey` enums (default `ID`; the mock ranks SKU, TITLE, INVENTORY_QUANTITY, ORDER_NUMBER, CREATED_AT, UPDATED_AT, TOTAL_PRICE and the like, and keeps ID order for keys it cannot rank such as POPULAR or RELEVANCE). Search `query` supports, per connection: variants `sku:`, `product_id:`, `id:`, `title:`; products `sku:`, `status:`, `id:`, `title:`; inventory items `sku:`, `id:`; orders `name:`, `email:`, `id:`, `tag:`, `status:` (open, closed, cancelled), `created_at:` and `updated_at:` with `>`, `>=`, `<`, `<=`; draft orders `status:`, `email:`, `tag:`, `name:`; customers `email:`, `id:`; plus free text, `OR`, `AND` and `-negation`. **Any other search field is an error** (`The mock does not support the search field 'x'`), never a silent empty result, so a test cannot pass or fail because of a filter the mock does not apply.
- `inventorySetQuantities(input:) @idempotent(key: $key)`: as of 2026-04 the `@idempotent` directive with a non-empty key is **required**, and every quantity **must include `changeFromQuantity`**: the current quantity for a compare and swap, or an explicit `null` to skip the check. `compareQuantity` and `ignoreCompareQuantity` no longer exist and are rejected as undefined input fields. `name` must be `available` or `on_hand`, `reason` one of Shopify's reasons (`correction`, `cycle_count_available`, `damaged`, `movement_created`, `received`, `restock`, `shrinkage`, ...), `referenceDocumentUri` a URI such as `gid://demo-chocolate/InventorySync/2026-10-20`. User errors use the real codes (`CHANGE_FROM_QUANTITY_STALE`, `INVALID_NAME`, `INVALID_REASON`, `INVALID_LOCATION`, `INVALID_INVENTORY_ITEM`, `INVALID_QUANTITY_NEGATIVE`, `INVALID_REFERENCE_DOCUMENT`, `NO_DUPLICATE_INVENTORY_ITEM_ID_GROUP_ID_PAIR`, `IDEMPOTENCY_KEY_PARAMETER_MISMATCH`, `IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED`). The docs give no code for a missing `changeFromQuantity` ("an error at runtime"); the mock answers `CHANGE_FROM_QUANTITY_STALE`. The same key with the same input replays the first result without applying it again (`idempotent_replay` in the log), so an n8n retry after a lost answer is safe. A key whose first attempt returned userErrors is used up: the same input again gives `IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED`, a corrected input gives `IDEMPOTENCY_KEY_PARAMETER_MISMATCH`. Build the key from the run and the input (for example `inventory-<run id>-<sku>-<changeFromQuantity>`), so a rerun with fresh numbers gets a fresh key. Several aliased `inventorySetQuantities` fields in one mutation, each with its own key, are fine.
  ```graphql
  mutation SetStock($input: InventorySetQuantitiesInput!, $key: String!) {
    inventorySetQuantities(input: $input) @idempotent(key: $key) {
      inventoryAdjustmentGroup { reason referenceDocumentUri changes { name delta quantityAfterChange } }
      userErrors { code field message }
    }
  }
  ```
- `draftOrderCreate(input: DraftOrderInput!)`: `email`, `note` (read back as `DraftOrder.note2`, there is no `DraftOrder.note`), `tags`, `lineItems` (`variantId` + `quantity` with optional `priceOverride: {amount, currencyCode}`, or a custom line with `title`, `sku`, `originalUnitPriceWithCurrency`), `appliedDiscount` (`valueType` `PERCENTAGE` or `FIXED_AMOUNT`, `value`, `title`), `purchasingEntity: {customerId}` (`customerId` alone is deprecated), `shippingAddress`, `shippingLine`, `poNumber`, `reserveInventoryUntil`, `phone`, `visibleToCustomer` and the other documented scalar fields. User errors: no line items, unknown variant, bad email, unknown customer, discount out of range.
- `draftOrderInvoiceSend(id:, email: EmailInput)` (`to`, `from`, `subject`, `customMessage`, `body`, `bcc`) sets status `INVOICE_SENT` and `invoiceSentAt`, and records the email in `mock.shopify.invoices`. `draftOrderComplete(id:, paymentPending:)` creates an order (next number after #1025) and takes the stock. `tagsAdd(id:, tags:)`.

Store facts: shop `Demo Chocolate Co.`, `demo-chocolate.myshopify.com`. Location `gid://shopify/Location/71000000001` (Demo Kitchen, active) and `...71000000002` (closed, so setting stock there gives `INVALID_LOCATION`). Every product in `base/seed/products.json` that has Shopify ids exists with one variant, with `available` equal to its `Shopify Stock`; ids follow the seed (`gid://shopify/ProductVariant/4510000000101` for DC-BAR-DARK-SEASALT and so on). Two more products exist in the store: `DC-BAR-GINGER` (available 25) is not in Airtable at all, and `DC-BAR-PUMPKIN-SPICE` (available 40) is in Airtable only as a Draft product with no Shopify ids and no batches, so the inventory sync must leave it alone. Seeded orders #1001 to #1025 (except the imported #1004) exist with their line items and customers, as `gid://shopify/Order/5200000001001` and so on.

Helpers:

```python
mock.shopify.available('DC-BAR-DARK-SEASALT'); mock.shopify.set_available(sku, 50)
mock.shopify.item_by_sku(sku); mock.shopify.variant_by_sku(sku); mock.shopify.primary_location
mock.shopify.adjustments      # every applied inventory change: name, reason, referenceDocumentUri, idempotencyKey, changes [{sku, delta, ...}]
mock.shopify.draft_orders     # by gid, including '_input' (the exact input sent)
mock.shopify.invoices; mock.shopify.orders; mock.shopify.customers
mock.shopify.throttle(times=1)
mock.shopify.order_payload(number, email, line_items, shipping_address=None, customer=None, note=None,
                           note_attributes=None, financial_status='paid', fulfillment_status=None, refunds=None,
                           cancelled_at=None, cancel_reason=None, created_at=None, updated_at=None, tags='',
                           shipping_price=6.95, order_id=None, phone=None, currency='USD')
```

`order_payload` builds the REST style JSON Shopify sends for `orders/create` and `orders/updated`. `customer` takes `name`, `id` (pass the seeded Shopify Customer ID, for example `7700000000103` for jordan, to test matching by id; the default is a new id `7700000000000 + number`), `phone` and `accepts_marketing`. Order id is `5200000000000 + number`, line item ids are `14100000000000 + number * 100 + position` unless given, `variant_id` and `product_id` are filled in when the SKU exists in Shopify (null for a custom line), refunds lower `current_quantity` and add `refunds[].refund_line_items`.

Webhooks: Shopify signs the raw body with the app's client secret, `X-Shopify-Hmac-Sha256` = base64 of HMAC SHA256. Deduplicate on `X-Shopify-Webhook-Id` (one delivery; `X-Shopify-Event-Id` groups deliveries of one merchant action). Shopify expects a 200 within 5 seconds (1 second to connect) and retries 8 times over 4 hours, so answer the webhook first and do the slow work (geocoding, forecast, paced Airtable writes) after the response.

```python
raw, headers = webhook_request(topic, payload, secret=SHOPIFY_WEBHOOK_SECRET, webhook_id=None, event_id=None,
                               shop_domain=SHOP_DOMAIN, api_version='2026-10', triggered_at=None, signature=None)
# headers: Content-Type, X-Shopify-Topic, X-Shopify-Shop-Domain, X-Shopify-API-Version, X-Shopify-Webhook-Id,
#          X-Shopify-Event-Id, X-Shopify-Triggered-At, User-Agent, X-Shopify-Hmac-Sha256
# signature='' leaves the HMAC header out, signature='abc' sends a wrong one, secret='other' signs with another secret
run.post_shopify('orders', 'orders/updated', payload, webhook_id='same-id-again')
sign_webhook(raw_bytes)          # the expected header value
```

## Open-Meteo

`GET /v1/search?name=...&count=...&language=en&format=json&countryCode=US` and `GET /v1/forecast?latitude=...&longitude=...&daily=temperature_2m_max&timezone=auto&forecast_days=3`. No key. As documented:

- Geocoding matches the place name only: 1 character finds nothing, 2 must match exactly, 3 or more do a case and accent insensitive prefix match. After a comma, a qualifier must match exactly: a country name, a 2-letter country code, a state name or a state abbreviation (`Austin, TX` and `Austin, Texas` work, `Austin, Tex` does not; `CA` means California, not Canada). No match is HTTP 200 with no `results` key at all. Results are ordered by population (`Portland` gives Oregon, then Maine).
- Forecast needs `latitude` and `longitude` in range; `forecast_days` 0 to 16 (default 7); daily variables must be real names (`temperature_2m_max`, `temperature_2m_min`, `apparent_temperature_max`, `precipitation_sum`, `weather_code`, ...); daily data needs `timezone` (`auto` is the usual choice). Errors are 400 `{"error": true, "reason": "..."}`. The first day is today in the location's time zone.

Daily maximum over the first 3 days, so with `MELT_TEMP_C=27` the cold pack result is:

| Required (max C) | Not needed (max C) |
|---|---|
| Phoenix 37.1, Tucson 34.6, Houston 31.2, Miami 31.0, Las Vegas 31.0, Austin 30.1, Honolulu 30.0, Orlando 29.6 | Atlanta 26.1 (closest to the line), San Diego 25.0, Nashville 23.1, Denver 18.4, Salt Lake City 16.8, Brooklyn 16.1, Boston 14.6, Portland OR 14.1, Chicago 13.4, Seattle 13.2, Portland ME 11.0, Madison 10.8, Burlington 9.4, Minneapolis 9.4 |

Any other place is not found. Helpers: `mock.meteo.set_forecast('Seattle', [30.0, 12.0])` (city name or `(lat, lon)`; the last value repeats), `mock.meteo.add_city(name, lat, lon, temps, admin1=..., timezone=...)`.

## Postmark

`POST /email` (or `/email/batch`, up to 500 messages) with headers `X-Postmark-Server-Token: mock-postmark-server-token`, `Content-Type: application/json`, `Accept: application/json`. Body fields as documented: `From` (must be a confirmed sender signature: `wholesale@example.com`, `support@example.com`, `orders@example.com` or `hello@example.com`, with or without a display name), `To` (comma separated, at most 50 recipients over To, Cc and Bcc), `Cc`, `Bcc`, `Subject`, `HtmlBody` and/or `TextBody` (one is required), `ReplyTo`, `Tag`, `Headers`, `TrackOpens`, `TrackLinks`, `Metadata` (string values), `Attachments`, `MessageStream` (`outbound` or `broadcast`), `InlineCss`. Success: 200 `{"To", "SubmittedAt", "MessageID", "ErrorCode": 0, "Message": "OK"}`. Errors: 401 `ErrorCode 10`, 415 without the JSON content type, 422 with `ErrorCode` 300 (invalid or missing recipients or body), 400 (sender signature), 402 (invalid JSON), 403 (unknown field, for example `Htmlbody`), 406 (inactive recipient), 1235 (unknown stream). Helpers: `mock.postmark.sent` (each message plus `MessageID`), `mock.postmark.inactive_recipients.add(email)`, `mock.postmark.sender_signatures`.

## Slack

Any `POST` under `/services/...` (or `/slack/...`). JSON `{"text": ...}` and/or `{"blocks": [...]}` (or form encoded `payload=`). Answers 200 with the plain text `ok`; 400 `invalid_payload`, `no_text` or `invalid_blocks` (unknown block type, more than 50 blocks, section text over 3000 characters, header over 150); 410 `channel_is_archived` for a path in `mock.slack.archived_paths`. Helpers: `mock.slack.messages` (each payload plus `_path`), `mock.slack.texts(path_contains='/packing')`.

## Claude (Anthropic Messages)

`POST /v1/messages` and `POST /v1/messages/count_tokens`, headers `x-api-key: mock-anthropic-key` and `anthropic-version: 2023-06-01`. The mock does not think; it answers with scripted tool calls, but rejects what the real API rejects (checked against the current model docs):

- 401 without a valid key, 400 without `anthropic-version`, 404 `not_found_error` for an unknown model (current ids include `claude-sonnet-5`, `claude-opus-5`, `claude-opus-5-5`, `claude-haiku-4-5`).
- `max_tokens` required, a positive integer within the model's limit; not allowed on `count_tokens`. `stream: true` is refused (send non streaming requests).
- `messages` non empty, first role `user`, roles alternating, no empty text; ending on an `assistant` turn (prefill) is a 400 on current models.
- `temperature`, `top_p` and `top_k` are a 400 on `claude-sonnet-5`, Opus 5, Opus 5.5, Opus 4.7/4.8 and Fable 5/5.1. `thinking` with `budget_tokens` is a 400 on those models; thinking cannot be combined with a forced tool; `thinking: {"type": "disabled"}` is a 400 on Opus 5.5 and Fable 5/5.1.
- Tools: names match `^[a-zA-Z0-9_-]{1,128}$`, `input_schema` has `type: "object"`, names are unique. `tool_choice: {"type": "tool", "name": "..."}` must name a defined tool, and forced tool choice is a 400 on `claude-opus-5-5`, `claude-fable-5-1` and `claude-mythos-5-1`, so keep the default `claude-sonnet-5` for forced tool calls.
- Unknown top level fields: 400 `Extra inputs are not permitted`.

A successful answer is a real message shape: `{"id", "type": "message", "role": "assistant", "model", "content": [{"type": "tool_use", "id", "name", "input"}], "stop_reason": "tool_use", "usage": {...}}`.

```python
mock.anthropic.on('dark sea salt', tool_input={...})      # match: substring of the last user message, a callable(body), or None
mock.anthropic.on(None, tool_input=lambda body: {...})    # compute the answer from the request
mock.anthropic.on('melted', status=529, times=3)          # an outage for the next 3 matching calls
mock.anthropic.on(None, text='no tool call')              # a plain text answer (stop_reason end_turn)
mock.anthropic.queue(tool_input={...})                    # answers the very next request, whatever it says
mock.anthropic.calls          # every accepted request body
# on() rules are tried in the order they were added and match by substring, so a later message that repeats
# earlier wording can hit an earlier rule. queue() is the safe way to script exactly one answer.
mock.anthropic.problems       # every rejected request (assert it is empty), plus unscripted calls (answered 500)
mock.anthropic.schema_problems  # scripted inputs that do not match the request's input_schema (still sent, on purpose)
```

To check the request shape against the real API for free, send the same body without `max_tokens` to the real `POST https://api.anthropic.com/v1/messages/count_tokens` (allowed by the repo rules); never call the real `/v1/messages`. The real `count_tokens` checks the model and `tool_choice` (forced tool on `claude-opus-5-5` and `claude-fable-5-1` is a 400 there too, checked in October 2026) but not the generation rules (prefill, `budget_tokens`, thinking with a forced tool), and the mock's `count_tokens` now behaves the same. So a 200 from the real `count_tokens` does not prove those generation rules are met; the mock's `/v1/messages` checks them.

## n8n helpers (`tests/mocks/n8n.py`)

```python
prepare_workflow(path_or_dict, mock, slug, env, disable_schedules=True)   # see rule 4 and 5; schedule triggers are disabled
mock_env(mock, **overrides)                                              # the variable table above, as strings
n8n = N8n(url=None, api_key=None)       # N8N_URL, N8N_API_KEY or N8N_ENV_FILE
n8n.cleanup(slug)                       # delete leftovers named exactly "TEST <slug> (temporary)"
with n8n.temporary(wf, slug, mock=mock, error_workflow=True, activate=True) as run:
    run.post(path, payload=None, headers=None, raw=None, method='POST', timeout=60, count=True)   # /webhook/<slug>-e2e-<path>
    run.post_shopify(path, topic, payload, **webhook_request_kwargs)
    ex = run.wait(timeout=120)          # newest finished run, with data, once every run started so far is done
    runs = run.wait_all()               # every finished run not returned before, oldest first
    run.ran(ex, 'Node'); run.runs_of(ex, 'Node'); run.output(ex, 'Node', run=0, branch=0)
    run.node_error(ex, 'Node'); run.last_node(ex); run.error(ex); ex['status']   # 'success' or 'error'
    run.alert_for(ex, timeout=None)     # the error workflow's alert for this run (matched by its execution link), or None;
                                        # waits up to 90 s by default, pass e.g. timeout=5 when no alert is expected
    run.error_alerts()                  # every error alert so far
```

Pass `count=False` to `run.post` for a call that should not start a run (for example a webhook that answers 404 because the path is wrong). An error alert looks like this (from the smoke test against n8n 2.19.5):

```
:rotating_light: *TEST foundation (temporary)* failed at *Code: Fail On Purpose*
Smoke test failure on purpose [line 1]
http://localhost:5678/workflow/SS5jLioIcTAtDBeP/executions/5407
```

Lessons from running against n8n 2.19.5, all still true here:

- n8n 2.x only runs an error workflow that is **active**. `n8n.temporary()` activates the copy for you; the README and docs/setup.md of each automation must tell the user to import and activate `shared/error-alert-slack.json`.
- The Code node keeps only the text after the last `: ` of a thrown message and appends ` [line N]`. Write thrown messages without `: ` and without URLs.
- `retryOnFail` on an HTTP node that holds several items looks at item 0 and re-runs all items, so a multi item write can duplicate records on a retry. Send writes one batch per pass (Loop Over Items) when duplicates matter.
- With pagination on, the HTTP error item has the status in `error.statusCode` or `error.response.status`, not `error.status`.
- Error alerts arrive after the failed run finishes: use `run.alert_for(ex)`.
- If a Code node throws before a `responseMode: responseNode` webhook has answered, n8n answers 200 with an empty body. Route config problems to an explicit Respond to Webhook with a 4xx or 5xx.

More lessons, found by the builders and reviewers of 01 to 05 on the same n8n 2.19.5:

- HTTP Request pagination must be nested as `options.pagination.pagination`. One level less is ignored without any warning and only the first page is read (02 and 03 both had this bug until their tests caught it). Assert the nesting in a static check.
- With pagination on, the error output item has no `message`. The response body is in `error.error` as JSON text (encoded once), and the status is in `error.statusCode` and `error.response.status`.
- A Shopify `THROTTLED` answer is HTTP 200, so `retryOnFail` never fires on it. Check for it in an IF node and wait before one more try (02 does).
- `retryOnFail` is capped at 5 tries 5 seconds apart, whatever you ask for (04 probed it). Wait out Airtable's 30 second block with a separate Wait node, not with a long retry wait.
- Loop Over Items (Split In Batches v3) keeps its state after its done output. If a cooldown loop sends items back into the same loop, set `options.reset` (03 uses `{{ $prevNode.name === 'Number Free?' }}`), or the second pass skips every item.

## Seed facts to rely on

Everything below is computed from `base/seed` and listed in full in `base/seed/_meta.json` under `expected`. Dates are relative to today because the seed is shifted.

- **01 Shopify orders**: highest order #1025; webhook id `b54557e4-bdd9-4b37-8a5f-bf7d70bcd043` is already in Webhook Log (a repeat delivery must be skipped); customers are matched by lookup, never by an upsert on `Email` (an email field is not a documented merge key type, so the mock refuses it): first `{Shopify Customer ID} = '<customer.id>'`, then `LOWER(TRIM({Email})) = '<lowercased email>'`; when several records come back (`jordan.blake@example.com` twice exactly, `maya.chen@example.com` and `priya.raman@example.com` once clean and once with other case and spaces), prefer the one with a Shopify Customer ID (jordan 7700000000103, maya 7700000000101, priya 7700000000105), then update it by record id; create a customer only when nothing matches. `DC-BAR-GINGER` exists in Shopify but not in Airtable (unknown SKU, create a Draft product); `DC-BAR-PUMPKIN-SPICE` is already a Draft product (match it, do not create it again). 21 SKUs exist in both with Shopify ids. Shopify wants the webhook answered within 5 seconds.
- **02 Inventory**: sellable = batches with Status `Released`, `Produced On` on or before today and more than `SELL_BY_BUFFER_DAYS` (14) days to best before; units = `max(Made - Reserved - Sold, 0)`. Six SKUs changed since the last sync (Airtable `Shopify Stock` and Shopify `available` agree on the old value): DC-BAR-DARK-SEASALT 180 to 200, DC-BAR-DARK-85 30 to 4, DC-BAR-DARK-ALMOND 110 to 80, DC-BAR-WHITE-MATCHA 20 to 54, DC-BAR-OAT-MILK 80 to 75, DC-BAR-MILK-CLASSIC 200 to 190; 15 Shopify SKUs are unchanged; DC-PRALINE-HAZELNUT and DC-BAR-PUMPKIN-SPICE have no Shopify ids in Airtable (`notOnShopify`). DC-PRALINE-HAZELNUT is wholesale only; DC-BAR-PUMPKIN-SPICE is a Draft that does exist in the store with `available` 40, so sync only Active products that have a `Shopify Inventory Item ID`, and assert its Shopify stock is still 40 after a run. Promo candidates (within 45 days, units left): B-DSS-260523 (15 days, 30 units, promo already exists), B-WMA-260612 (30, 40), B-PRB-260930 (40, 38), B-MSC-260722 (45, 50, on the edge); B-MHZ-260712 at 46 days is out. Below reorder point: DC-BAR-DARK-85, DC-BAR-DARK-CHILI, DC-BAR-DARK-MINT (open request exists), DC-PRALINE-HAZELNUT, so 3 new Purchasing Requests.
- **03 Wholesale**: next quote number Q-2026-0046. The example inquiry "120 dark sea salt bars and 60 hazelnut pralines" prices as DC-BAR-DARK-SEASALT 120 x 4.20 = 504.00 (tier 100-299, 200 in stock) and DC-PRALINE-HAZELNUT 60 x 1.10 = 66.00 (tier 50-199, only 40 in stock, a planned batch of 1500 in 3 days; not on Shopify, so it becomes a custom draft order line), subtotal 570.00. The old tier `DC-BAR-DARK-SEASALT 300+ (old)` is inactive. `AND({Status} = 'Approved', NOT({Processed}))` finds only Q-2026-0042 (DC-BAR-DARK-SEASALT 100 at 4.20 and DC-HOT-COCOA-250 12 at 9.00, Juniper Market).
- **04 Subscription boxes**: 13 Active, 1 Paused, 1 Cancelled subscribers; last boxes were 2026-10, so test with month `2026-11` and `REPEAT_WINDOW=1`. Rules: Nut-Free excludes Tree Nuts and Peanuts in Contains Allergens or May Contain Traces; Dairy-Free excludes Milk in either; Vegan needs the Vegan diet label; Gluten-Free excludes Wheat in either; dislikes exclude matching Flavor Notes. Eligible bars: Status Active, Subscription Eligible, Category Bar (19 SKUs). Nut trace traps: DC-BAR-DARK-RASPBERRY, DC-BAR-WHITE-MATCHA, DC-BAR-MILK-LAVENDER. SUB-0004 and SUB-0011 (dairy-free) each have only 1 allowed bar they did not get last month; SUB-0009 has 3 for a 4 bar plan. Scarce stock: DC-BAR-DARK-MINT 3 units, DC-BAR-DARK-85 4, DC-BAR-DARK-CHILI 19. Every subscriber's allowed list is in `_meta.json`.
- **05 Delivery triage**: next ticket T-0005; Derek Olsen already has 2 approved melt replacements (`CLAIM_LIMIT=2`). A warm forecast means `Forecast Max C >= MELT_TEMP_C`; no cold pack means `Cold Pack Shipped` is unticked; late means delivered after `Expected Delivery`. Scenarios: #1012 (tom.becker, delivered 2 days ago, Cold Pack Required but not shipped, forecast 30.5: approve), #1015 (sofia.marino, 3 days late: approve), #1009 (omar.haddad, shipped with a cold pack, on time: no automatic approval), #1003 (priya.raman, delivered 20 days ago: outside the window), #1021 (derek.olsen, 1 day late, but he already has `CLAIM_LIMIT` (2) approved claims: human), #1018 (victor.hale, damaged), #1020 (felix.osei, wrong item), #1022 (clara.jensen, not delivered: missing), #1017 (rosa.delgado, in transit).
- **06 Airtable scripts**: duplicate groups by normalized email (maya, jordan, priya), invalid emails (sam, lena, tom-n), missing Email (grace) and missing Name (kai), order lines without a product link (l1004-1, l1004-2 with a lowercase SKU and a leading space, l1011-3 unknown SKU, l1008-gift with no SKU), the orphan line LEGACY-0001, and the expected lifetime value and order count per customer. Nine customers have no order that counts (`customersWithoutCountedOrders`: bluebird, grace, kai, lena, nina, owen with only a cancelled order, priya-dup, sam, tom-n): write Lifetime Value 0 and Order Count 0 for them, since a stored 0 is returned as 0. The gift wrap line has Unit Price 0 and Line Total 0. The seeded finding `2026-W42 / Invalid Email / sam` is last week's and has no Record ID (seed record ids only exist after loading); new findings use `<ISO week> / <check> / <Airtable record id>`.

## Running the mocks by hand

```
python -m tests.mocks.server --port 8790                       # seeded base, dates shifted to today
python -m tests.mocks.server --port 8790 --empty-base          # no tables (try setup/create_base.py against it)
python -m tests.mocks.server --port 8790 --now 2026-10-20T09:00:00Z --seed-today none --no-rate-limit
```

Control endpoints for out of process use: `GET /__mock/health`, `GET /__mock/log?service=airtable&since=0`, `GET /__mock/airtable/<table name>`, `POST /__mock/reset {"empty_base": false, "seed_today": "auto"}`, `POST /__mock/fail {"service": "airtable", "status": 500, "times": 1, "match": {"table": "Orders"}}`, `POST /__mock/clock {"now": "..."}` or `{"live": true}`.
