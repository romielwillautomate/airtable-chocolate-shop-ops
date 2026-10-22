# Changelog

## v1 (2026-10-22)

First release: one Airtable base and six automations for Demo Chocolate Co., each built, tested end to end against strict mocks of the real APIs, then reviewed by a second pass that tried to break it.

### Added

- **The base.** [base/schema.json](base/schema.json) describes 17 tables and 258 fields with real Airtable field types and options, [base/seed](base/seed) holds 362 fake records with deliberate mess (duplicate customers, bad emails, an expired batch still marked Released, nut trace traps), and [base/README.md](base/README.md) explains the tables, the keys and the traps.
- **[setup/create_base.py](setup/create_base.py)** builds the base through the Metadata API and loads the seed through the Web API. Safe to run again, with `--dry-run`, `--no-seed` and `--shift-dates-to-today`.
- **[shared/error-alert-slack.json](shared/error-alert-slack.json)**, the error workflow every n8n workflow uses. It must be imported and activated.
- **[01 Shopify orders sync](automations/01-shopify-orders-sync):** signed order webhooks into Customers, Orders, Order Lines and Products, with a melt-risk check from the Open-Meteo forecast and Slack alerts for the packing team.
- **[02 Batch inventory sync](automations/02-batch-inventory-sync):** daily sellable stock per SKU from the production batches, set in Shopify with `changeFromQuantity` and an idempotency key, promos for short dated batches and purchasing requests below the reorder point.
- **[03 Wholesale quote to draft order](automations/03-wholesale-quote-to-draft-order):** free text inquiries read by Claude, priced from the price list, checked against stock and capacity, emailed through Postmark, and approved quotes turned into Shopify draft orders and invoices exactly once.
- **[04 Subscription box curation](automations/04-subscription-box-curation):** monthly boxes that are allergen and diet safe, never repeat a bar, share scarce stock fairly and reserve it on the batches, with one run at a time.
- **[05 Delivery issue triage](automations/05-delivery-issue-triage):** support messages as tickets, classified by Claude, free Shopify replacements when the order's own data backs a melt claim, photo and tracking emails, and a human queue in Slack.
- **[06 Native Airtable scripts](automations/06-airtable-native-scripts):** four scripts for Airtable's "Run a script" action, used by seven automations in the base, that merge duplicate customers, keep lifetime value and VIP tier current, link order lines to products by SKU and write a weekly data quality report.
- **[tests/mocks](tests/mocks/README.md):** strict mocks of Airtable (with a filterByFormula engine and the 5 requests per second limit), Shopify Admin GraphQL and webhooks, Open-Meteo, Postmark, Slack and Claude, plus the n8n test helpers, with 83 unit tests of their own.
- **Tests for every automation**, 562 checks in total, and a test report per automation from its real final run. Summary: [docs/test-report.md](docs/test-report.md).
- **Docs:** the root [README](README.md), [docs/setup.md](docs/setup.md) for the real services and [.env.example](.env.example) with every variable.

### Fixed: found by testing and review before release

Each of these was caught by a failing check or a probe, fixed in the workflow (never in the test), and is now covered by a test.

- **Mocks.** A stored 0 was dropped from Airtable answers like an empty cell; upsert keys of types Airtable does not allow (email, URL, currency) were accepted; a search field the Shopify mock did not support returned an empty list instead of an error. All of these could have let a wrong workflow pass.
- **01.** A run that restarted after an Airtable cooldown could write an older order over a newer one and send a second packing alert. A gift note with `<!channel>` pinged the packing channel, and forged webhook headers could post Slack markup into the ops channel.
- **02.** Pagination was nested one level too shallow, which n8n ignores without a warning, so only the first page of batches was read. Only an Open purchasing request blocked a new one, so once the kitchen set it to Scheduled a duplicate appeared every day. Two Run Now calls at once both hit Airtable's rate limit and stopped; a Shopify THROTTLED answer (HTTP 200) was never retried.
- **03.** The same pagination bug, so a catalog over 100 records would have been quoted from page 1. The rule-based reader read "1,200" as 200. Quote lines could be lost for good, because n8n retries a multi-item HTTP node only when its first item fails. A cooldown loop skipped every write on its second pass. An approved quote with a Total of 0 became a 100 percent discount.
- **04.** Subscribers whose box for the month was already recorded by hand got a second box: on the seed, 13 extra boxes and 62 reserved bars. A cancelled box counted toward the repeat window. Boxes planned before an allergen change were never checked again. Ruby chocolate now counts as dairy.
- **05.** Replacements were completed before Shopify had finished calculating the draft order (`DraftOrder.ready`). If the final Airtable update failed after a replacement, the next message about the order could be approved again. An Airtable 429 after the ticket was saved left it stuck at New.
- **06.** The SKU linker wrote the cleaned SKU back, so it and automation 01 flipped the field back and forth and kept starting each other. It also re-flagged orders a person had already cleared, and two runs for one order could lose a review reason. All four scripts now check the base, Sync Log and select options before the first write.

### Changed during the integration

- **Shared mocks:** `mock.lose_answer(service, match, times, status)` (the request is handled, then its answer is lost) moved into the shared mock server from the tests of 02 and 04, which had their own copies. `run.alert_for()` now waits up to 90 seconds by default and says what the error workflow did when no alert arrives. `N8n()` gives a clear message when `N8N_ENV_FILE` points at a file that does not exist. Every automation's test passed again after these changes.
- **Base docs:** the Customers description now says how customers are matched (Shopify Customer ID, then trimmed lowercase email, never an upsert on Email), and the descriptions of Shopify Stock, Stats Updated At and the Sync Log Run ID, the keys table and the trap table in base/README.md match what the automations do.
- **.env.example:** the Shopify scope list is the one the workflows need (`read_products` was not needed), with a note that `SHOPIFY_ADMIN_TOKEN` must not expire and which Claude models refuse a forced tool call.
