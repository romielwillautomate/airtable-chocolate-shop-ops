# Setting up the four scripts in Airtable

This guide builds the seven automations from the main [README](../README.md) in an Airtable base made from [base/schema.json](../../../base/schema.json). It takes about 30 minutes. You need Creator or Owner access to the base. Airtable renames buttons now and then; if a label below is slightly different in your base, look for the closest match.

## Before you start

1. Build the base with [setup/create_base.py](../../../setup/create_base.py) (see [docs/setup.md](../../../docs/setup.md)), or check that your base has the tables Customers, Orders, Order Lines, Products, Subscribers, Tickets, Data Quality and Sync Log with the field names and select options in the blueprint. Each script checks this when it starts, before it reads or writes anything, and stops with a clear message if something is missing. A script cannot add a select option itself, so if you rename an option (for example Sync Log Status "Partial"), change the name in the script too.
2. Keep [scripts/](../scripts) open in a text editor. You will paste each file whole, comments included.
3. Decide your VIP thresholds. The examples use 40, 75 and 150 dollars of lifetime value for Silver, Gold and Platinum.

## The steps that are the same for every automation

1. Open the base and click **Automations**, then **+ Create automation**. Give it the name from the table in the README, for example "Customer stats: new order".
2. Click **+ Add trigger** and choose the trigger, then set its table and, for "When record updated", the fields to watch. Use the trigger's test to load a sample record.
3. Add an action (in the current builder: **+ Add advanced logic or action**) and choose **Run a script**.
4. Open the code editor, delete the sample code and paste the script.
5. In the panel on the left of the editor, add each input variable (**+ Add input variable**). The name must match exactly, including capitals. For a record id, click the value box and pick **Airtable record ID** from the trigger. For a fixed value such as a threshold, type it.
6. Add one more input variable, `dryRun`, with the value `true`, and click **Test**. A test runs the script for real against your base; with `dryRun` it only reports. Read the output on the right.
7. Delete the `dryRun` variable (or set it to `false`), click **Finish editing**, and switch the automation **On**.

If a run fails, Airtable marks it as failed in **Automation history** and emails the person who turned the automation on. The script also adds a row with Status Failed and the reason to the Sync Log table.

## 1. Customers: merge duplicates

| Setting | Value |
|---|---|
| Trigger | **At scheduled time**, every day at 05:00 |
| Action 1 | Run a script: [01-dedupe-customers.js](../scripts/01-dedupe-customers.js). No input variables (only `dryRun` while testing) |
| Action 2 | Run a script: [02-customer-stats.js](../scripts/02-customer-stats.js) with `customerRecordIds` = the **affectedCustomerIds** output of action 1, plus `silverThreshold` `40`, `goldThreshold` `75`, `platinumThreshold` `150` |

Before you switch it on, test action 1 with `dryRun` and read `merges` (which record is kept in each group) and `conflicts` (groups with two Shopify Customer IDs, which are never merged automatically). A merge is not undone automatically, so this is the one to check.

Action 2 recalculates the kept records and the merged duplicates straight away. Without it, the nightly run (automation 4) does the same a few hours later. On a day with nothing to merge, **affectedCustomerIds** is an empty list and action 2 does nothing at all (no read, no write). Each moved order also starts automation 3 for that order, which is harmless: it works out the same numbers for the kept record.

## 2. Customer stats: new order

| Setting | Value |
|---|---|
| Trigger | **When record created**, table Orders |
| Action | Run a script: [02-customer-stats.js](../scripts/02-customer-stats.js) |
| Input variables | `orderRecordId` = Airtable record ID (from the trigger), `silverThreshold` `40`, `goldThreshold` `75`, `platinumThreshold` `150` |

## 3. Customer stats: order changed

| Setting | Value |
|---|---|
| Trigger | **When record updated**, table Orders, fields to watch: Customer, Total, Refunded Amount, Order Status |
| Action | Run a script: [02-customer-stats.js](../scripts/02-customer-stats.js) |
| Input variables | the same as automation 2 |

"When record updated" does not run for a record that was just created, which is why automation 2 exists. Watch only the four fields: the script writes to Customers, never to Orders, so it cannot start itself again.

## 4. Customer stats: nightly

| Setting | Value |
|---|---|
| Trigger | **At scheduled time**, every day at 02:00 |
| Action | Run a script: [02-customer-stats.js](../scripts/02-customer-stats.js) |
| Input variables | `silverThreshold` `40`, `goldThreshold` `75`, `platinumThreshold` `150` (no record id: every customer) |

This run catches what automation 3 cannot see: when an order moves from one customer to another, automation 3 updates the new customer, and this run corrects the old one. It writes only customers whose numbers changed and adds one row to Sync Log.

## 5. Order lines: new line

| Setting | Value |
|---|---|
| Trigger | **When record created**, table Order Lines |
| Action | Run a script: [03-link-order-lines.js](../scripts/03-link-order-lines.js) |
| Input variables | `orderLineRecordId` = Airtable record ID (from the trigger) |

Airtable starts one run per created line, so an order that arrives with several lines starts several runs at once. That is expected: each run looks at all of the order's lines, so the order ends up with one Review Reason line per unknown SKU, whichever run finishes last.

When an order is flagged for an unknown SKU, either add the product (or fix the SKU on the line, which starts automation 6 and links it), or decide it is fine. In both cases untick **Needs Review** on the order when you are done and leave the reason text as a record: the order is not flagged again for that line.

## 6. Order lines: SKU edited

| Setting | Value |
|---|---|
| Trigger | **When record updated**, table Order Lines, fields to watch: SKU only |
| Action | Run a script: [03-link-order-lines.js](../scripts/03-link-order-lines.js) |
| Input variables | `orderLineRecordId` = Airtable record ID (from the trigger) |

Watch the SKU field only. The script writes Product and SKU Status on the line but never the SKU text (it stays as Shopify or the import sent it, for example ` dc-bar-milk-caramel`), so its own writes never start this automation again, and the Shopify orders sync can keep writing the SKU as Shopify sends it without the two undoing each other.

Recommended: a copy of this automation on **At scheduled time** (weekly) with no input variables checks every line. It catches lines from an import, and it puts back an unknown SKU reason on an order in the rare case two runs at the same moment missed one.

## 7. Data quality: weekly report

| Setting | Value |
|---|---|
| Trigger | **At scheduled time**, every week on Monday at 07:00 |
| Action 1 | Run a script: [04-weekly-data-quality.js](../scripts/04-weekly-data-quality.js). No input variables |
| Action 2 | **Send email** (or a Slack message) to the person who looks after the data. Subject: `Data quality` and the **reportWeek** output. Body: the **reportText** output |

The findings land in the Data Quality table. A useful view there: Status is Open, grouped by Check, sorted by Severity. Set a finding to **Ignored** when it is fine as it is (a wholesale customer without a phone, say) and the report will not raise it again. Set it to **Fixed** only once the data is fixed; if the problem is still there next week, the report opens a new finding.

## Changing the rules

| To change | Where |
|---|---|
| VIP thresholds | the three input variables of script 2 (or `FALLBACK_THRESHOLDS` at the top of the script) |
| What counts towards lifetime value | `computeStats` in script 2 |
| Which record a merge keeps | `compareKeep` in script 1 (and the same function in script 4, so the report names the same record) |
| Required fields, severities | `REQUIRED` and `SEVERITY` at the top of script 4 |
| Table or field names | the lists at the top of each script; each script checks them when it starts |

After any change, run the tests in [tests/](../tests) (see the README) before you paste the new version into Airtable.
