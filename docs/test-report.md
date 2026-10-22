# Test report: the whole system

**Date:** 2026-10-22 (UTC), the integration run after the last shared changes
**n8n:** 2.19.5, self-hosted in Docker
**Runtimes:** Python 3.14.4, Node 24.15.0
**Result:** every suite passes. 562 of 562 automation checks, 18 of 18 deliberate mistakes caught by 06's mutation check, and 83 of 83 unit tests of the shared mocks.

The suites ran one after the other, each on its own mock port, against the same n8n. No real Airtable base, Shopify store, Postmark server, Slack workspace or Claude model was used. The only real API call was Anthropic's free token counting endpoint, which 05 uses to check its Claude request (200, 1,342 input tokens); it validates a request without generating anything. No `TEST <slug> (temporary)` workflow was left in n8n afterwards.

## Summary

| Suite | Result | What it covers | Time | Full report |
|---|---|---|---|---|
| Shared mocks and `setup/create_base.py` | 83/83 unit tests | Every mock rule (rate limit, record limits, field types, upsert keys, formula engine, GraphQL validation, idempotency keys, Claude request rules), the n8n helpers, and `create_base.py` building the base twice with no change on the rerun | 74 s | [tests/mocks/README.md](../tests/mocks/README.md) |
| [01 Shopify orders sync](../automations/01-shopify-orders-sync) | 83/83 | 34 scenarios through n8n, a check of every request in the run and a cleanup check | 409 s | [report](../automations/01-shopify-orders-sync/docs/test-report.md) |
| [02 Batch inventory sync](../automations/02-batch-inventory-sync) | 71/71 | 23 scenarios through n8n, static checks of the workflow file and a count of the error alerts | 188 s | [report](../automations/02-batch-inventory-sync/docs/test-report.md) |
| [03 Wholesale quote to draft order](../automations/03-wholesale-quote-to-draft-order) | 100/100 | 35 scenarios through n8n, 5 static checks and 4 checks over the whole run | 291 s | [report](../automations/03-wholesale-quote-to-draft-order/docs/test-report.md) |
| [04 Subscription box curation](../automations/04-subscription-box-curation) | 133/133 | 6 static checks, 34 scenarios through n8n (one is a 1,500 base random allergen check), an allergen check over every scenario and a cleanup check | 1,015 s | [report](../automations/04-subscription-box-curation/docs/test-report.md) |
| [05 Delivery issue triage](../automations/05-delivery-issue-triage) | 81/81 | 43 scenarios through n8n and 5 checks over the whole run | 238 s | [report](../automations/05-delivery-issue-triage/docs/test-report.md) |
| [06 Native Airtable scripts](../automations/06-airtable-native-scripts) | 94/94, mutations 18/18 | 26 scenarios in a strict stand-in for Airtable's scripting environment, then the same suite against 18 broken copies of the scripts | 280 s | [report](../automations/06-airtable-native-scripts/docs/test-report.md) |

Most of the time goes into waiting on purpose: several scenarios wait out Airtable's real 30 second block after a 429, and 01 lets every `orders/updated` wait 10 seconds, as the workflow does in production. The time column is this run; each automation's own report has the output of its builder's or reviewer's final run, with the same checks.

## The final lines of each suite, as printed

```
python -m unittest tests.mocks.test_mocks                              Ran 83 tests in 73.445s   OK
python automations/01-shopify-orders-sync/tests/e2e_mock_test.py       83/83 checks passed
python automations/02-batch-inventory-sync/tests/e2e_mock_test.py      71/71 checks passed
python automations/03-wholesale-quote-to-draft-order/tests/e2e_mock_test.py   100/100 checks passed
python automations/04-subscription-box-curation/tests/e2e_mock_test.py 133/133 checks passed
python automations/05-delivery-issue-triage/tests/e2e_mock_test.py     81/81 checks passed
python automations/06-airtable-native-scripts/tests/e2e_mock_test.py   94/94 checks passed
                                                                       18/18 deliberate mistakes caught
                                                                       All passed.
```

## What a passing check means

The mocks in [tests/mocks](../tests/mocks/README.md) reject what the real APIs reject, with the real error bodies, and each rule was checked against the official docs in October 2026. So a workflow that sends 11 records in one Airtable write, an upsert keyed on an email field, a sixth request inside one second, an unknown GraphQL field, an `inventorySetQuantities` call without `@idempotent`, a Claude request with `temperature`, or an email from an unconfirmed Postmark sender fails its test the way it would fail for real.

The checks are strict: exact records and cell values, exact request counts and order, the Slack and email texts, and the error alert of every failed run matched to that run by its execution link. Every suite covers the happy path, a rerun that must change nothing, every failure path the README describes (outages, rate limits, timeouts, refusals, lost answers), edge cases from the seed's deliberate mess, and security (forged signatures and secrets, Slack markup in customer text, config that is missing).

Several suites also prove that they can fail: 06's mutation check breaks the scripts in 18 ways and the suite catches each one, and the builders and reviewers of 01 to 04 ran their new checks against deliberately broken or earlier versions of their workflows and saw them fail (see each report).

## Changes made during the integration, and the reruns

These shared files changed before this run, and every suite above ran after the last of them:

- `tests/mocks/core.py`: `mock.lose_answer()` (a request is handled, then its answer is lost) is part of the shared mock now. 02 and 04 had their own copies in subclasses and use the shared one; their lost answer scenarios pass as before.
- `tests/mocks/n8n.py`: `run.alert_for()` waits up to 90 seconds by default and reports the error workflow's runs on a miss; `N8n()` stops with a clear message when `N8N_ENV_FILE` points at a missing file.
- `tests/mocks/test_mocks.py`: 2 new unit tests for the above (81 became 83).
- `base/schema.json` and `base/README.md`: descriptions and docs only, no field or option changed.
- `.env.example`: comments, and the Shopify scope list; 03's static check that every variable it reads is documented there passed against the new file.

## What the tests cannot prove

- That the real services behave exactly like the mocks. Where the docs give a rule but not the error text, or say nothing, the mocks pick the stricter behavior, and real timing (Shopify calculating a draft order, Airtable answering under load) can differ.
- That two requests at the very same moment are handled atomically by Airtable: the mock handles requests one at a time.
- 06's scripts have not run in a real Airtable base; they run unchanged in a stand-in that follows Airtable's scripting docs.
- The schedule triggers themselves: the tests switch them off and start the same nodes through the protected Run Now webhooks.
