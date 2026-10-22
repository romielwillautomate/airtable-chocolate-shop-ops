# Setting up the whole system

This guide takes you from nothing to all six automations running against real services: Airtable, Shopify, Slack, Postmark, Claude and n8n. You can stop after any automation; each one works on its own as long as the base and the error workflow are in place. Plan for about two hours for everything, most of it in the Shopify and Postmark settings.

Service menus change now and then. If a label below is slightly different, look for the closest match.

## 1. What you need

| Service | For | Notes |
|---|---|---|
| Airtable | the base (all six) | A plan with automations and the "Run a script" action for 06. Creator or Owner access to the base |
| n8n 2.x, self-hosted | 01 to 05 | Reachable from the internet over HTTPS, because Shopify and your forms post to it |
| Shopify | 01, 02, 03, 05 | A store where you can create an app and webhooks |
| Slack | 01 to 05, and the error alerts | One workspace, up to three channels: ops, packing, support |
| Postmark | 03, 05 | A server with confirmed sender signatures for the wholesale and support addresses |
| Anthropic | 03, 05 | An API key with credit |
| Open-Meteo | 01 | No account. The free API is for non-commercial use; a real shop needs a paid plan (see step 9) |

## 2. n8n

The workflows read every setting with `$env`, so they need **self-hosted n8n** with this setting in its environment:

```
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
```

Put the variables from [.env.example](../.env.example) in the same environment (for Docker Compose, an `env_file:` entry pointing at your `.env`), fill them in as you go through the steps below, and restart n8n after each change. Set `GENERIC_TIMEZONE` too if you want automation 04's monthly run at 08:00 in your own time zone rather than n8n's default.

On **n8n Cloud**, `$env` is not available. There, replace `$env.` with `$vars.` in each workflow file before you import it, and create the same names under Variables.

n8n must be reachable at a public HTTPS address (`https://YOUR-N8N-DOMAIN`). A hosted n8n works as it is; a local one needs a tunnel such as Cloudflare Tunnel or ngrok.

## 3. Airtable: build the base

1. In Airtable, create an empty base. Copy its id from the URL: the `appXXXXXXXXXXXXXX` part. That is `AIRTABLE_BASE_ID`.
2. Create a personal access token at airtable.com/create/tokens with access to this base only and these scopes:
   - `data.records:read` and `data.records:write`: what the automations need. This is `AIRTABLE_TOKEN`.
   - `schema.bases:read` and `schema.bases:write`: only for the setup script. Use a second token for setup if you want the automations' token to stay minimal.
3. From the repo root, look at the plan first, then build the base:

   ```
   AIRTABLE_TOKEN=pat... AIRTABLE_BASE_ID=app... python setup/create_base.py --dry-run
   AIRTABLE_TOKEN=pat... AIRTABLE_BASE_ID=app... python setup/create_base.py --shift-dates-to-today
   ```

   `--shift-dates-to-today` loads the fake Demo Chocolate Co. data with every date moved so that relative facts hold today, which is the easiest way to try the automations. For a real shop, use `--no-seed` to create the tables and fields only, then import your own products and batches. The script is safe to run again: it skips what already exists and remembers the records it loaded in `setup/.seed-state.local.json`.
4. Delete the default "Table 1" that Airtable puts in a new base.
5. Optional: for the link fields that hold one record (an order line's Order, a batch's Product; they are marked `prefersSingleRecordLink` in [base/schema.json](../base/schema.json)), switch "Allow linking to multiple records" off in Airtable. The Metadata API cannot set that option.

What each table is for, and which automation writes it: [base/README.md](../base/README.md).

## 4. Slack

Create a Slack app for your workspace (api.slack.com/apps, From scratch), open **Incoming Webhooks**, switch it on and add a webhook for each channel:

| Variable | Channel | Gets |
|---|---|---|
| `SLACK_WEBHOOK_URL` | ops | Error alerts from the error workflow (all automations), new SKU alerts (01), the inventory note (02), quote review alerts and run summaries (03). Required for 03 |
| `SLACK_PACKING_WEBHOOK_URL` | packing | Cold pack alerts (01, falls back to `SLACK_WEBHOOK_URL`) and the monthly box summary (04, required) |
| `SLACK_SUPPORT_WEBHOOK_URL` | support | Tickets that need a person (05). Use a private channel: the alerts include the customer's name and email |

## 5. The error workflow (do this before any automation)

1. Import [shared/error-alert-slack.json](../shared/error-alert-slack.json) into n8n.
2. **Activate it.** n8n 2.x only runs an error workflow that is active. An inactive one is skipped and no alert arrives, without any warning.
3. Every time you import an automation below, open its **Settings** and choose "Error Alert: Slack notification for failed workflows" as the **Error Workflow**.

It posts one message per failed run to `SLACK_WEBHOOK_URL`: the workflow, the node where it stopped, the reason and a link to the run.

## 6. Shopify

### The app and its token (02, 03, 05)

The workflows call the Admin GraphQL API with a fixed token in `SHOPIFY_ADMIN_TOKEN` (header `X-Shopify-Access-Token`), so they need a token that **does not expire**:

- **A custom app created in the Shopify admin before 2026-01-01** (Settings, Apps and sales channels, Develop apps) has an Admin API access token (`shpat_...`) that does not expire. Use it, after adding the scopes below.
- **A new app made in the Dev Dashboard**: install it on your store with the authorization code grant and exchange the code at `POST https://your-store.myshopify.com/admin/oauth/access_token` without `expiring` (or with `expiring=0`), which gives a non-expiring offline token. Custom apps are exempt from Shopify's move to expiring tokens for public apps. Do not use the client credentials grant for these workflows: its tokens expire after 24 hours, and the workflows do not refresh a token.

Admin API access scopes, all on one app:

| Scope | For |
|---|---|
| `write_inventory` (includes reading inventory) and `read_locations` | 02 sets stock at one location |
| `write_draft_orders` (includes reading draft orders) and `read_customers` | 03 creates the draft order and invoice, and attaches an existing customer; 05 creates and completes the replacement |
| `read_orders` | 05 reads the order behind a complaint (orders older than 60 days would need `read_all_orders`, which the 7 day claim window never reaches) |

05's replacement carries the customer's name, email and shipping address, which Shopify treats as protected customer data. Custom apps have it available, but select those fields in the app's protected customer data settings if Shopify asks.

Then set:

| Variable | Value |
|---|---|
| `SHOPIFY_STORE_DOMAIN` | `your-store.myshopify.com`, without `https://` |
| `SHOPIFY_ADMIN_TOKEN` | the token above |
| `SHOPIFY_API_VERSION` | optional, default `2026-10`. 02 needs 2026-04 or newer (`changeFromQuantity` and `@idempotent`) |
| `SHOPIFY_LOCATION_ID` | 02 only: the location whose stock it sets, `gid://shopify/Location/<number>` (Settings, Locations, the number at the end of the URL) |

### The order webhooks (01)

01 needs no token at all: every webhook carries the whole order. It needs two webhooks, `orders/create` and `orders/updated`, both sent to `https://YOUR-N8N-DOMAIN/webhook/shopify-orders`, and the key Shopify signs them with in `SHOPIFY_WEBHOOK_SECRET`. Create them in Settings, Notifications, Webhooks (the signing key is shown on that page), or subscribe from an app (then the key is the app's client secret). Step by step, with what to check afterwards: [automations/01-shopify-orders-sync/docs/shopify-setup.md](../automations/01-shopify-orders-sync/docs/shopify-setup.md).

## 7. Postmark (03, 05)

1. In Postmark, create a server for the shop. Its **Default Transactional Stream** is `outbound`, which is the default of `POSTMARK_MESSAGE_STREAM`.
2. Under **Sender Signatures**, add and confirm the two From addresses (or verify the whole domain with its DKIM and Return-Path records, which also helps delivery).
3. Copy the **Server API token** (the server's API Tokens tab) into `POSTMARK_SERVER_TOKEN`.
4. Set the From addresses exactly as they should appear:

   ```
   EMAIL_FROM_WHOLESALE=Demo Chocolate Wholesale <wholesale@example.com>
   EMAIL_FROM_SUPPORT=Demo Chocolate Support <support@example.com>
   ```

Until Postmark approves a new account, it can only send to the domains you have added and verified in it, so request approval before you go live.

## 8. Claude (03, 05)

1. Create an API key at console.anthropic.com (API keys) and put it in `ANTHROPIC_API_KEY`. Setting a monthly spend limit on the workspace is a good idea.
2. Leave `ANTHROPIC_MODEL` at its default, `claude-sonnet-5`, unless you have a reason. Both workflows use a forced tool call, which `claude-opus-5-5` and `claude-fable-5-1` refuse with a 400: on those models every message would go to the rule-based fallback and to a person. `claude-haiku-4-5` also accepts the request and costs less.

Measured with Anthropic's token counter: 03 uses about 2,900 input tokens per new inquiry with the seed catalog of 22 products (about 40 more per product), and 05 about 1,350 per new message. Both are well under one US cent per message on `claude-sonnet-5`. Repeats, approvals and fallbacks make no Claude call.

## 9. Open-Meteo (01)

01 calls `https://geocoding-api.open-meteo.com/v1/search` and `https://api.open-meteo.com/v1/forecast` with no key. That free API is for non-commercial use. For a real shop, take an Open-Meteo plan: it uses `customer-` host names and an `apikey` parameter, so change the two URLs in 01 (nodes **Open-Meteo: Geocode City** and **Open-Meteo: Get Forecast**) and add the key.

## 10. The automations

Import each workflow, choose the error workflow in its settings (step 5) and activate it. Every row below assumes the variables from the earlier steps are set.

### 01 Shopify orders sync

| | |
|---|---|
| File | [automations/01-shopify-orders-sync/workflow/shopify-orders-sync.json](../automations/01-shopify-orders-sync/workflow/shopify-orders-sync.json) |
| Trigger | `POST https://YOUR-N8N-DOMAIN/webhook/shopify-orders`, from the two Shopify webhooks (step 6) |
| Variables | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `SHOPIFY_WEBHOOK_SECRET` (required; without it every webhook gets 500), optional `SHOPIFY_STORE_DOMAIN` (for the admin link in Slack), `SLACK_PACKING_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `MELT_TEMP_C` (default 27), `MELT_FORECAST_DAYS` (default 3) |
| Check it | Place a test order. Within seconds it is in Orders with Cold Pack filled in, its lines in Order Lines, and one row each in Webhook Log and Sync Log |

### 02 Batch inventory sync

| | |
|---|---|
| File | [automations/02-batch-inventory-sync/workflow/batch-inventory-sync.json](../automations/02-batch-inventory-sync/workflow/batch-inventory-sync.json) |
| Trigger | Every day at 06:00 UTC (the workflow's time zone is set to UTC; change the hour in **Schedule: Daily 06:00 UTC**). Run Now: `POST https://YOUR-N8N-DOMAIN/webhook/inventory-run-now` with the header `X-Run-Now-Secret` |
| Variables | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_LOCATION_ID`, optional `SHOPIFY_API_VERSION`, `SLACK_WEBHOOK_URL`, `RUN_NOW_SECRET` (without it Run Now always answers 401), `SELL_BY_BUFFER_DAYS` (14), `PROMO_WINDOW_DAYS` (45, 0 switches promos off), `PROMO_DISCOUNT_PERCENT` (25) |
| Shopify scopes | `write_inventory`, `read_locations` |
| Check it | `curl -X POST https://YOUR-N8N-DOMAIN/webhook/inventory-run-now -H "X-Run-Now-Secret: $RUN_NOW_SECRET"` answers 202 with a run id, and a Sync Log row with that Run ID appears with the counts |

Before the first run, make sure every Active product that should sync has its `Shopify Inventory Item ID` filled in. 02 sets Shopify's available quantity to sellable stock minus units in unshipped Shopify orders, so the first run can change many numbers at once.

### 03 Wholesale quote to draft order

| | |
|---|---|
| File | [automations/03-wholesale-quote-to-draft-order/workflow/wholesale-quote-to-draft-order.json](../automations/03-wholesale-quote-to-draft-order/workflow/wholesale-quote-to-draft-order.json) |
| Triggers | Your website's server posts each form submission to `https://YOUR-N8N-DOMAIN/webhook/wholesale-inquiry` with the header `X-Form-Secret` (never from the browser, so the secret stays private). Approved quotes are converted every 15 minutes; Run Now: `POST https://YOUR-N8N-DOMAIN/webhook/wholesale-run-now` with `X-Run-Now-Secret` |
| Variables | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `ANTHROPIC_API_KEY`, `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_WHOLESALE` (a confirmed sender signature), `SLACK_WEBHOOK_URL` (required), `WHOLESALE_FORM_SECRET` (required; without it every post gets 500), `RUN_NOW_SECRET`, optional `SHOPIFY_API_VERSION`, `ANTHROPIC_MODEL`, `POSTMARK_MESSAGE_STREAM`, `SELL_BY_BUFFER_DAYS` (14), `QUOTE_VALID_DAYS` (14) |
| Shopify scopes | `write_draft_orders`, `read_customers` |
| Body | JSON with `submissionId`, `email` and `message` required, plus `name`, `company`, `phone`. Send the same `submissionId` again when you retry, and post again on a 503. The exact format is in the [03 README](../automations/03-wholesale-quote-to-draft-order/README.md#setup) |
| Check it | Post the example inquiry; the buyer gets a quote email with a quote number, and the quote is in Quotes with Status Sent. Set it to Approved: within 15 minutes it has a Shopify draft order and invoice link, Processed is ticked and Status is Converted |

### 04 Subscription box curation

| | |
|---|---|
| File | [automations/04-subscription-box-curation/workflow/subscription-box-curation.json](../automations/04-subscription-box-curation/workflow/subscription-box-curation.json) |
| Trigger | 08:00 on the 20th of each month, in n8n's time zone (`GENERIC_TIMEZONE`), for next month's boxes. Run Now: `POST https://YOUR-N8N-DOMAIN/webhook/subscription-box-run-now` with `X-Run-Now-Secret` and an optional body `{"month": "2026-11"}` (this month or next) |
| Variables | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `SLACK_PACKING_WEBHOOK_URL` (required), `RUN_NOW_SECRET`, optional `REPEAT_WINDOW` (1, from 0 to 12), `SELL_BY_BUFFER_DAYS` (14). `SLACK_WEBHOOK_URL` is used by the error workflow |
| Check it | Run Now for next month: one Box Shipment per Active subscriber with a packing list, Box History rows, Quantity Reserved raised on the batches used, and one Slack summary for the packing team |

After any change to a product's allergens or a subscriber's needs, call Run Now for this month (and next month, if it is already planned): every run checks the boxes already planned against today's data and lists any bar to take out at the top of the Slack message.

### 05 Delivery issue triage

| | |
|---|---|
| File | [automations/05-delivery-issue-triage/workflow/delivery-issue-triage.json](../automations/05-delivery-issue-triage/workflow/delivery-issue-triage.json) |
| Trigger | The help form or email parser posts each message to `https://YOUR-N8N-DOMAIN/webhook/support-message` with the header `X-Support-Secret`, added on the server side (the form tool's webhook feature or your backend), never by JavaScript in the visitor's browser |
| Variables | `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `ANTHROPIC_API_KEY`, `POSTMARK_SERVER_TOKEN`, `EMAIL_FROM_SUPPORT` (a confirmed sender signature), `SUPPORT_FORM_SECRET` (required; without it every message gets 503), optional `SHOPIFY_API_VERSION`, `ANTHROPIC_MODEL`, `POSTMARK_MESSAGE_STREAM`, `SLACK_SUPPORT_WEBHOOK_URL`, `MELT_TEMP_C` (27), `CLAIM_WINDOW_DAYS` (7), `CLAIM_LIMIT` (2) |
| Shopify scopes | `read_orders`, `read_customers`, `write_draft_orders` |
| Body | JSON with `message_id`, `email` and `message` required, plus `name`, `subject`, `order_number`, `channel`, `received_at`, `photos`. Every field and answer is in the [05 README](../automations/05-delivery-issue-triage/README.md#the-webhook) |
| Check it | Post a complaint about a real order number: the sender gets `200` with a ticket number, and the ticket appears in Tickets with its Action Log |

05 decides melt claims from the order's own data (Forecast Max C, Cold Pack, Cold Pack Shipped, Expected Delivery, Delivered At). 01 fills the forecast and cold pack fields; the packing team ticks Cold Pack Shipped and the delivery dates come from your carrier or by hand. A completed replacement is a normal Shopify order, so it reaches 01 as `orders/create` (total 0, tagged `replacement`). In Airtable, a Tickets view filtered on Status "Human Queue" and sorted by Priority makes a simple queue for the team.

### 06 Native Airtable scripts

06 has no n8n workflow and no environment variables. Its four scripts run inside seven Airtable automations in the base itself, with the base's own permissions. Build them click by click with [automations/06-airtable-native-scripts/docs/airtable-setup.md](../automations/06-airtable-native-scripts/docs/airtable-setup.md), and test each one with the `dryRun` input variable set to `true` before you switch it on. A failed run is marked failed in Airtable's automation history, emails the person who turned the automation on, and adds a Failed row to Sync Log.

## 11. Before you rely on it

- **Stagger the schedules.** Every automation shares the base's 5 requests per second. The defaults already avoid each other: 06 at 02:00 and 05:00 daily and Monday 07:00 (in the time zone set on each Airtable trigger), 02 at 06:00 UTC, 04 on the 20th at 08:00, and 03 every 15 minutes with only a few calls when nothing is approved. Webhooks (01, 03, 05) arrive whenever they arrive; each workflow paces itself and waits out a 429.
- **Move the keys into n8n credentials** for production (Header Auth for Airtable, Shopify, Postmark and Anthropic), so they never appear in run data, and set an execution data retention period: runs contain customer names, emails and messages.
- **Archive old rows** in Webhook Log and Sync Log now and then; they get one row per webhook or run.
- **Watch the ops channel** for the first week. Every failed run posts there with a link to the run.

## 12. When something does not work

| You see | Likely cause |
|---|---|
| A run failed in n8n but no Slack alert | The error workflow is not active, or not chosen as the Error Workflow in that workflow's settings, or `SLACK_WEBHOOK_URL` is empty |
| `$env` errors, or every value empty | `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` is missing, the variables are not in n8n's environment, or n8n was not restarted. On n8n Cloud, use `$vars.` (step 2) |
| 01: every webhook gets 401, "Bad Signature" alerts | `SHOPIFY_WEBHOOK_SECRET` is not the key that signs the webhooks (admin webhooks and app webhooks use different keys) |
| 01: every webhook gets 500 | `SHOPIFY_WEBHOOK_SECRET` is not set |
| An alert naming 401 AUTHENTICATION_REQUIRED or 403 from Airtable | `AIRTABLE_TOKEN` is wrong, lacks a scope, or has no access to this base; or `AIRTABLE_BASE_ID` is wrong |
| An alert naming UNKNOWN_FIELD_NAME or INVALID_MULTIPLE_CHOICE_OPTIONS | A field or select option was renamed in Airtable. The names must match [base/schema.json](../base/schema.json) |
| Shopify calls answer 401, or worked for a day and then stopped | The token is wrong or expired: use a token that does not expire (step 6) |
| Shopify user errors about access or scopes | A scope from step 6 is missing; add it to the app and reinstall it if Shopify asks |
| 03 or 05: every message goes to a person, read by the rule-based reader (03) or the keyword fallback (05) | Claude refused the request: check `ANTHROPIC_API_KEY`, the credit balance and `ANTHROPIC_MODEL` (step 8) |
| Emails are not sent, alerts name Postmark ErrorCode 400 or 406 | The From address is not a confirmed sender signature (400), or the recipient bounced before and is inactive (406) |
| Run Now answers 401 | The `X-Run-Now-Secret` header does not match `RUN_NOW_SECRET`, or `RUN_NOW_SECRET` is not set |
