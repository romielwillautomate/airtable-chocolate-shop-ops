# Connect Shopify to the workflow

The workflow needs two Shopify webhooks, **orders/create** and **orders/updated**, both pointing at the same n8n URL, and the key Shopify signs them with. It never calls the Shopify API, so it needs no Shopify access token: every webhook carries the whole order.

## Before you start

- The workflow is imported and **active** in n8n, and n8n is reachable from the internet at a public HTTPS URL. Shopify delivers webhooks to an HTTPS address, so `localhost` or a private network address will never receive them.
- Your webhook URL is `https://YOUR-N8N-DOMAIN/webhook/shopify-orders` (the production URL shown in the **Webhook: Shopify Orders** node).
- `shared/error-alert-slack.json` is imported, **activated** and chosen as this workflow's Error Workflow, so a forged webhook or a failed save reaches Slack.

## Option A: webhooks in the Shopify admin (simplest)

1. In Shopify, open **Settings > Notifications > Webhooks** and click **Create webhook**.
2. Event **Order creation**, format **JSON**, URL as above, webhook API version **2026-10**. Save.
3. Create a second webhook the same way with the event **Order update**.
4. The same page says that your webhooks are signed with a key (a string of letters and numbers unique to your store). Copy it into `SHOPIFY_WEBHOOK_SECRET` in the n8n environment and restart n8n.

The event can't be changed after a webhook is created; delete it and make a new one instead.

## Option B: webhooks owned by an app

If the store already has a custom app for your integrations, subscribe from the app instead (in `shopify.app.toml`, or with the GraphQL `webhookSubscriptionCreate` mutation, topics `ORDERS_CREATE` and `ORDERS_UPDATED`, the URL in `uri`). Then:

- `SHOPIFY_WEBHOOK_SECRET` is the app's **client secret** (Dev Dashboard > Apps > your app > Settings > Credentials). When you rotate it, Shopify keeps signing webhooks with the oldest secret that is not revoked, so switch n8n to the new secret at the moment you revoke the old one. The workflow checks a single secret, so a delivery that arrives signed with the other one in that moment gets a 401, and Shopify retries it later.
- The app needs the `read_orders` scope for both topics. A public app also needs approved access to protected customer data (names, emails, phone numbers and addresses), otherwise Shopify redacts them and the order arrives without a customer name or a shipping address.

## Check it

1. Place a test order (a draft order marked as paid works well), shipping to a city you know.
2. In Airtable, the order appears in **Orders** within a few seconds, with **Cold Pack**, **Forecast Max C** and **Melt Check Note** filled in, its lines in **Order Lines**, and one row in **Webhook Log** (Status Processed) and **Sync Log**.
3. Mark the order as fulfilled in Shopify. About 10 seconds later the same Orders record shows Fulfillment Status Fulfilled, and no second record appears.

The **Send test** button on Shopify's webhook page posts a made-up sample order. The workflow treats it like a real order, so it creates a sample order, a customer and Draft products for the sample SKUs. Delete those rows afterwards, or test with a real test order instead.

## What Shopify does when n8n is down

Shopify allows 1 second to connect and 5 seconds for the whole request. The workflow answers before it does any work, so that is never a problem while n8n is up. If n8n is down or answers with an error, Shopify retries the delivery 8 times over the next 4 hours. After 8 failures in a row, Shopify deletes a subscription that was created through the Admin API (option B), so if you see a run of "Bad Signature" or "Not Configured" alerts, fix `SHOPIFY_WEBHOOK_SECRET` quickly and check that both webhooks still exist.
