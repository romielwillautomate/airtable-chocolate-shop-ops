"""Mock of the Shopify Admin GraphQL API (versions 2026-04 to 2026-10) plus webhook signing helpers.

Endpoint: POST {store}/admin/api/{version}/graphql.json with header X-Shopify-Access-Token.
The schema below is the supported subset. Anything outside it is rejected exactly like Shopify rejects it
(HTTP 200 with "errors" and no "data"), so a passing test means the query would also be valid on Shopify
for the fields used.

Current API rules it enforces (2026-04 and later):
  * inventorySetQuantities needs @idempotent(key: ...) on the mutation field, and every quantities entry must
    pass changeFromQuantity: the current quantity for a compare and swap, or an explicit null to skip it.
    compareQuantity and ignoreCompareQuantity no longer exist and are rejected as undefined input fields.
  * name must be "available" or "on_hand"; reason must be one of Shopify's inventory reasons;
    referenceDocumentUri must be a URI
  * the same idempotency key with the same input returns the first result without applying it again;
    with a different input it fails with IDEMPOTENCY_KEY_PARAMETER_MISMATCH; a key whose first attempt returned
    userErrors is used up (IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED), so a corrected retry needs a new key
  * DraftOrder has note2 (not note); DraftOrderInput.customerId is deprecated in favour of purchasingEntity
  * every response carries extensions.cost with throttleStatus; a query over 1000 points fails with
    MAX_COST_EXCEEDED and an empty bucket gives THROTTLED
"""
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta, timezone
from pathlib import Path

from . import graphql as G
from .core import iso_ms, json_response, parse_iso

ACCESS_TOKEN = 'shpat_mock_demo_chocolate_0000000000000000'
SHOP_DOMAIN = 'demo-chocolate.myshopify.com'
WEBHOOK_SECRET = 'mock-shopify-client-secret'
SUPPORTED_VERSIONS = ('2026-04', '2026-07', '2026-10')
DEFAULT_VERSION = '2026-10'
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'shopify_store.json'
SHOP_TZ = timezone(timedelta(hours=-4))

INVENTORY_REASONS = ('correction', 'cycle_count_available', 'damaged', 'movement_canceled', 'movement_created',
                     'movement_received', 'movement_updated', 'other', 'promotion', 'quality_control', 'received',
                     'reservation_created', 'reservation_deleted', 'reservation_updated', 'restock', 'safety_stock',
                     'shrinkage')
QUANTITY_NAMES = ('available', 'on_hand', 'committed', 'incoming', 'reserved', 'damaged', 'safety_stock', 'quality_control')

SDL = '''
scalar DateTime
scalar Decimal
scalar Money
scalar URL
scalar UnsignedInt64
scalar JSON

enum CurrencyCode { USD CAD EUR GBP AUD }
enum CountryCode { US CA GB AU DE FR NL IE NZ MX }
enum ProductStatus { ACTIVE ARCHIVED DRAFT UNLISTED }
enum ProductVariantSortKeys { FULL_TITLE ID INVENTORY_LEVELS_AVAILABLE INVENTORY_MANAGEMENT INVENTORY_POLICY INVENTORY_QUANTITY NAME POPULAR POSITION RELEVANCE SKU TITLE }
enum ProductSortKeys { CREATED_AT ID INVENTORY_TOTAL PRODUCT_TYPE PUBLISHED_AT RELEVANCE TITLE UPDATED_AT VENDOR }
enum OrderSortKeys { CREATED_AT CURRENT_TOTAL_PRICE CUSTOMER_NAME DESTINATION FINANCIAL_STATUS FULFILLMENT_STATUS ID ORDER_NUMBER PO_NUMBER PROCESSED_AT RELEVANCE TOTAL_ITEMS_QUANTITY TOTAL_PRICE UPDATED_AT }
enum DraftOrderStatus { COMPLETED INVOICE_SENT OPEN }
enum DraftOrderAppliedDiscountType { FIXED_AMOUNT PERCENTAGE }
enum WeightUnit { GRAMS KILOGRAMS OUNCES POUNDS }
enum OrderDisplayFinancialStatus { AUTHORIZED EXPIRED PAID PARTIALLY_PAID PARTIALLY_REFUNDED PENDING REFUNDED VOIDED }
enum OrderDisplayFulfillmentStatus { FULFILLED IN_PROGRESS ON_HOLD OPEN PARTIALLY_FULFILLED PENDING_FULFILLMENT RESTOCKED SCHEDULED UNFULFILLED }
enum InventorySetQuantitiesUserErrorCode {
  CHANGE_FROM_QUANTITY_STALE COMPARE_QUANTITY_REQUIRED COMPARE_QUANTITY_STALE IDEMPOTENCY_CONCURRENT_REQUEST
  IDEMPOTENCY_KEY_PARAMETER_MISMATCH IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED INVALID_INVENTORY_ITEM INVALID_LOCATION
  INVALID_NAME INVALID_QUANTITY_NEGATIVE INVALID_QUANTITY_TOO_HIGH INVALID_QUANTITY_TOO_LOW INVALID_REASON
  INVALID_REFERENCE_DOCUMENT NO_DUPLICATE_INVENTORY_ITEM_ID_GROUP_ID_PAIR NON_MUTABLE_INVENTORY_ITEM
}

interface Node { id: ID! }

type PageInfo { hasNextPage: Boolean! hasPreviousPage: Boolean! startCursor: String endCursor: String }

type QueryRoot {
  shop: Shop!
  node(id: ID!): Node
  nodes(ids: [ID!]!): [Node]!
  location(id: ID): Location
  locations(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean, includeInactive: Boolean): LocationConnection!
  product(id: ID!): Product
  products(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean, sortKey: ProductSortKeys, savedSearchId: ID): ProductConnection!
  productVariant(id: ID!): ProductVariant
  productVariants(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean, sortKey: ProductVariantSortKeys, savedSearchId: ID): ProductVariantConnection!
  inventoryItem(id: ID!): InventoryItem
  inventoryItems(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean): InventoryItemConnection!
  order(id: ID!): Order
  orders(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean, sortKey: OrderSortKeys): OrderConnection!
  draftOrder(id: ID!): DraftOrder
  draftOrders(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean): DraftOrderConnection!
  customer(id: ID!): Customer
  customers(first: Int, after: String, last: Int, before: String, query: String, reverse: Boolean): CustomerConnection!
}

type Mutation {
  inventorySetQuantities(input: InventorySetQuantitiesInput!): InventorySetQuantitiesPayload
  draftOrderCreate(input: DraftOrderInput!): DraftOrderCreatePayload
  draftOrderInvoiceSend(id: ID!, email: EmailInput): DraftOrderInvoiceSendPayload
  draftOrderComplete(id: ID!, paymentGatewayId: ID, sourceName: String, paymentPending: Boolean): DraftOrderCompletePayload
  tagsAdd(id: ID!, tags: [String!]!): TagsAddPayload
}

type Shop implements Node { id: ID! name: String! email: String! myshopifyDomain: String! currencyCode: CurrencyCode! ianaTimezone: String! }
type Location implements Node { id: ID! name: String! isActive: Boolean! legacyResourceId: UnsignedInt64! }
type LocationConnection { edges: [LocationEdge!]! nodes: [Location!]! pageInfo: PageInfo! }
type LocationEdge { cursor: String! node: Location! }

type Product implements Node {
  id: ID! title: String! handle: String! status: ProductStatus! vendor: String! productType: String!
  tags: [String!]! legacyResourceId: UnsignedInt64! totalInventory: Int!
  variants(first: Int, after: String, last: Int, before: String, reverse: Boolean): ProductVariantConnection!
}
type ProductConnection { edges: [ProductEdge!]! nodes: [Product!]! pageInfo: PageInfo! }
type ProductEdge { cursor: String! node: Product! }

type ProductVariant implements Node {
  id: ID! sku: String title: String! displayName: String! price: Money! inventoryQuantity: Int
  legacyResourceId: UnsignedInt64! product: Product! inventoryItem: InventoryItem!
}
type ProductVariantConnection { edges: [ProductVariantEdge!]! nodes: [ProductVariant!]! pageInfo: PageInfo! }
type ProductVariantEdge { cursor: String! node: ProductVariant! }

type InventoryItem implements Node {
  id: ID! sku: String tracked: Boolean! legacyResourceId: UnsignedInt64! variant: ProductVariant!
  inventoryLevel(locationId: ID!): InventoryLevel
  inventoryLevels(first: Int, after: String, last: Int, before: String, reverse: Boolean): InventoryLevelConnection!
}
type InventoryItemConnection { edges: [InventoryItemEdge!]! nodes: [InventoryItem!]! pageInfo: PageInfo! }
type InventoryItemEdge { cursor: String! node: InventoryItem! }
type InventoryLevel implements Node { id: ID! location: Location! item: InventoryItem! updatedAt: DateTime! quantities(names: [String!]!): [InventoryQuantity!]! }
type InventoryLevelConnection { edges: [InventoryLevelEdge!]! nodes: [InventoryLevel!]! pageInfo: PageInfo! }
type InventoryLevelEdge { cursor: String! node: InventoryLevel! }
type InventoryQuantity { id: ID! name: String! quantity: Int! updatedAt: DateTime }

type InventorySetQuantitiesPayload { inventoryAdjustmentGroup: InventoryAdjustmentGroup userErrors: [InventorySetQuantitiesUserError!]! }
type InventoryAdjustmentGroup implements Node {
  id: ID! createdAt: DateTime! reason: String! referenceDocumentUri: String
  changes(inventoryItemIds: [ID!], locationIds: [ID!], quantityNames: [String!]): [InventoryChange!]!
}
type InventoryChange { name: String! delta: Int! quantityAfterChange: Int item: InventoryItem location: Location }
type InventorySetQuantitiesUserError { code: InventorySetQuantitiesUserErrorCode field: [String!] message: String! }

type UserError { field: [String!] message: String! }
type MoneyV2 { amount: Decimal! currencyCode: CurrencyCode! }
type MoneyBag { shopMoney: MoneyV2! presentmentMoney: MoneyV2! }
type MailingAddress {
  address1: String address2: String city: String company: String countryCodeV2: CountryCode firstName: String
  lastName: String name: String phone: String provinceCode: String zip: String latitude: Float longitude: Float
}

type Customer implements Node {
  id: ID! email: String firstName: String lastName: String displayName: String! phone: String
  numberOfOrders: UnsignedInt64! tags: [String!]! legacyResourceId: UnsignedInt64!
}
type CustomerConnection { edges: [CustomerEdge!]! nodes: [Customer!]! pageInfo: PageInfo! }
type CustomerEdge { cursor: String! node: Customer! }

type Order implements Node {
  id: ID! name: String! email: String createdAt: DateTime! updatedAt: DateTime! cancelledAt: DateTime closedAt: DateTime
  displayFinancialStatus: OrderDisplayFinancialStatus displayFulfillmentStatus: OrderDisplayFulfillmentStatus!
  note: String tags: [String!]! customer: Customer shippingAddress: MailingAddress legacyResourceId: UnsignedInt64!
  totalPriceSet: MoneyBag! subtotalPriceSet: MoneyBag
  lineItems(first: Int, after: String, last: Int, before: String, reverse: Boolean): LineItemConnection!
}
type OrderConnection { edges: [OrderEdge!]! nodes: [Order!]! pageInfo: PageInfo! }
type OrderEdge { cursor: String! node: Order! }
type LineItem implements Node {
  id: ID! name: String! title: String! sku: String quantity: Int! currentQuantity: Int! variant: ProductVariant
  originalUnitPriceSet: MoneyBag!
}
type LineItemConnection { edges: [LineItemEdge!]! nodes: [LineItem!]! pageInfo: PageInfo! }
type LineItemEdge { cursor: String! node: LineItem! }

type DraftOrder implements Node {
  id: ID! name: String! status: DraftOrderStatus! email: String note2: String tags: [String!]! invoiceUrl: URL
  invoiceSentAt: DateTime createdAt: DateTime! updatedAt: DateTime! completedAt: DateTime legacyResourceId: UnsignedInt64!
  totalPriceSet: MoneyBag! subtotalPriceSet: MoneyBag! totalDiscountsSet: MoneyBag! totalShippingPriceSet: MoneyBag!
  totalTaxSet: MoneyBag! appliedDiscount: DraftOrderAppliedDiscount customer: Customer order: Order
  shippingAddress: MailingAddress poNumber: String reserveInventoryUntil: DateTime
  lineItems(first: Int, after: String, last: Int, before: String, reverse: Boolean): DraftOrderLineItemConnection!
}
type DraftOrderConnection { edges: [DraftOrderEdge!]! nodes: [DraftOrder!]! pageInfo: PageInfo! }
type DraftOrderEdge { cursor: String! node: DraftOrder! }
type DraftOrderAppliedDiscount { title: String description: String! value: Float! valueType: DraftOrderAppliedDiscountType! amountSet: MoneyBag! }
type DraftOrderLineItem implements Node {
  id: ID! name: String! title: String! sku: String quantity: Int! custom: Boolean! variant: ProductVariant
  originalUnitPriceSet: MoneyBag! discountedTotalSet: MoneyBag! appliedDiscount: DraftOrderAppliedDiscount
}
type DraftOrderLineItemConnection { edges: [DraftOrderLineItemEdge!]! nodes: [DraftOrderLineItem!]! pageInfo: PageInfo! }
type DraftOrderLineItemEdge { cursor: String! node: DraftOrderLineItem! }

type DraftOrderCreatePayload { draftOrder: DraftOrder userErrors: [UserError!]! }
type DraftOrderInvoiceSendPayload { draftOrder: DraftOrder userErrors: [UserError!]! }
type DraftOrderCompletePayload { draftOrder: DraftOrder userErrors: [UserError!]! }
type TagsAddPayload { node: Node userErrors: [UserError!]! }

input InventorySetQuantitiesInput { name: String! reason: String! referenceDocumentUri: String quantities: [InventoryQuantityInput!]! }
input InventoryQuantityInput { inventoryItemId: ID! locationId: ID! quantity: Int! changeFromQuantity: Int }
input MoneyInput { amount: Decimal! currencyCode: CurrencyCode! }
input WeightInput { value: Float! unit: WeightUnit! }
input AttributeInput { key: String! value: String! }
input MetafieldInput { namespace: String key: String value: String type: String }
input MailingAddressInput {
  address1: String address2: String city: String company: String countryCode: CountryCode firstName: String
  lastName: String phone: String provinceCode: String zip: String
}
input PurchasingEntityInput { customerId: ID }
input ShippingLineInput { title: String price: Money priceWithCurrency: MoneyInput shippingRateHandle: String }
input DraftOrderAppliedDiscountInput { amount: Money amountWithCurrency: MoneyInput description: String title: String value: Float! valueType: DraftOrderAppliedDiscountType! }
input DraftOrderLineItemInput {
  variantId: ID quantity: Int! title: String originalUnitPrice: Money originalUnitPriceWithCurrency: MoneyInput sku: String
  appliedDiscount: DraftOrderAppliedDiscountInput requiresShipping: Boolean taxable: Boolean weight: WeightInput
  customAttributes: [AttributeInput!] uuid: String priceOverride: MoneyInput generatePriceOverride: Boolean
}
input DraftOrderInput {
  email: String note: String tags: [String!] lineItems: [DraftOrderLineItemInput!] appliedDiscount: DraftOrderAppliedDiscountInput
  shippingAddress: MailingAddressInput billingAddress: MailingAddressInput purchasingEntity: PurchasingEntityInput
  shippingLine: ShippingLineInput useCustomerDefaultAddress: Boolean taxExempt: Boolean reserveInventoryUntil: DateTime
  presentmentCurrencyCode: CurrencyCode customAttributes: [AttributeInput!] sourceName: String poNumber: String
  metafields: [MetafieldInput!] customerId: ID phone: String discountCodes: [String!] acceptAutomaticDiscounts: Boolean
  allowDiscountCodesInCheckout: Boolean visibleToCustomer: Boolean
}
input EmailInput { to: String from: String subject: String customMessage: String body: String bcc: [String!] }
'''

SCHEMA = G.Schema(SDL)
# search fields each mock connection understands (anything else raises, see ShopifyMock._search)
SEARCH_FIELDS = {
    '_variant_terms': ['sku', 'product_id', 'id', 'title', 'free text'],
    '_product_terms': ['sku', 'status', 'id', 'title', 'free text'],
    '_item_terms': ['sku', 'id'],
    '_order_terms': ['name', 'email', 'id', 'tag', 'status', 'created_at', 'updated_at (with > >= < <=)', 'free text'],
    '_draft_terms': ['status', 'email', 'tag', 'name'],
    '_customer_terms': ['email', 'id', 'free text'],
}
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
URI_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://\S+$')


def money(amount):
    """Shopify Decimal strings: 7.5 -> "7.5", 12 -> "12.0", 3.95 -> "3.95"."""
    v = round(float(amount) + 0.0, 2)
    text = f'{v:.2f}'.rstrip('0')
    return text + '0' if text.endswith('.') else text


def money_bag(amount, currency='USD'):
    m = {'amount': money(amount), 'currencyCode': currency}
    return {'shopMoney': dict(m), 'presentmentMoney': dict(m)}


def gid_num(gid):
    return gid.rsplit('/', 1)[-1]


def shop_time(dt):
    return dt.astimezone(SHOP_TZ).isoformat(timespec='seconds')


# ---------------------------------------------------------------------------------------------- webhooks

def sign_webhook(raw, secret=WEBHOOK_SECRET):
    """X-Shopify-Hmac-Sha256: base64 of HMAC SHA256 over the raw body, keyed with the app client secret."""
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return base64.b64encode(hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).digest()).decode('ascii')


def webhook_request(topic, payload, secret=WEBHOOK_SECRET, webhook_id=None, event_id=None, shop_domain=SHOP_DOMAIN,
                    api_version=DEFAULT_VERSION, triggered_at=None, signature=None):
    """(raw_body_bytes, headers) for a Shopify webhook delivery. signature overrides the computed HMAC
    (pass '' to leave the header out)."""
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'X-Shopify-Topic': topic,
        'X-Shopify-Shop-Domain': shop_domain,
        'X-Shopify-API-Version': api_version,
        'X-Shopify-Webhook-Id': webhook_id or str(uuid.uuid4()),
        'X-Shopify-Event-Id': event_id or str(uuid.uuid4()),
        'X-Shopify-Triggered-At': triggered_at or '2026-10-20T13:00:00.000000000Z',
        'User-Agent': 'Shopify-Captain-Hook',
    }
    sig = sign_webhook(raw, secret) if signature is None else signature
    if sig:
        headers['X-Shopify-Hmac-Sha256'] = sig
    return raw, headers


def post_webhook(url, topic, payload, secret=WEBHOOK_SECRET, timeout=30, **kw):
    """Deliver a signed webhook to url (for example the n8n test webhook). Returns (status, body_text)."""
    raw, headers = webhook_request(topic, payload, secret, **kw)
    req = urllib.request.Request(url, data=raw, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


# ---------------------------------------------------------------------------------------------- the mock

class ShopifyMock:
    def __init__(self, server, token=ACCESS_TOKEN, shop_domain=SHOP_DOMAIN):
        self.server = server
        self.token = token
        self.shop_domain = shop_domain
        self.bucket = {'maximumAvailable': 1000.0, 'restoreRate': 50.0}
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self):
        self.shop = {}
        self.locations = {}
        self.products = {}
        self.variants = {}
        self.items = {}
        self.levels = {}          # (item_id, location_id) -> {quantity name: int}
        self.customers = {}
        self.orders = {}
        self.draft_orders = {}
        self.adjustments = []     # every applied inventory adjustment group
        self.invoices = []        # every invoice email sent by draftOrderInvoiceSend
        self.idempotency = {}
        self._available = self.bucket['maximumAvailable']
        self._last_refill = time.monotonic()
        self._throttle_next = 0
        self._counter = 0
        self.next_order_number = 1001
        self.next_draft_number = 1
        self._customer_refs = {}

    def load_store(self, seed_dir=None, today=None):
        """Build the store: shop and locations from fixtures/shopify_store.json, products and stock from the
        Airtable seed (Products with Shopify ids), customers and orders from the seed as well."""
        fx = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.shop = dict(fx['shop'])
        for loc in fx['locations']:
            self.locations[loc['id']] = dict(loc)
        self.primary_location = fx['locations'][0]['id']
        products = list(fx.get('extraProducts', []))
        if seed_dir:
            seed = Path(seed_dir)
            for p in json.loads((seed / 'products.json').read_text(encoding='utf-8'))['records']:
                f = p['fields']
                if f.get('Shopify Variant ID'):
                    products.append({'productId': f['Shopify Product ID'], 'variantId': f['Shopify Variant ID'],
                                     'inventoryItemId': f['Shopify Inventory Item ID'], 'sku': f['SKU'], 'title': f['Name'],
                                     'price': f.get('Price', 0), 'available': f.get('Shopify Stock', 0),
                                     'status': 'DRAFT' if f.get('Status') == 'Draft' else 'ACTIVE',
                                     'productType': f.get('Category', '')})
            self._customer_refs = {}
            for c in json.loads((seed / 'customers.json').read_text(encoding='utf-8'))['records']:
                f = c['fields']
                if f.get('Shopify Customer ID'):
                    gid = f'gid://shopify/Customer/{f["Shopify Customer ID"]}'
                    self._customer_refs[c['ref']] = gid
                    first, _, last = (f.get('Name') or '').partition(' ')
                    self.customers[gid] = {'id': gid, 'email': (f.get('Email') or '').strip().lower() or None,
                                           'firstName': first or None, 'lastName': last or None,
                                           'displayName': f.get('Name') or f.get('Email') or 'Customer',
                                           'phone': f.get('Phone'), 'tags': [], 'legacyResourceId': f['Shopify Customer ID']}
            self._load_orders(seed, today)
        for p in products:
            self.add_product(**p)

    def _load_orders(self, seed, today=None):
        try:
            orders = json.loads((seed / 'orders.json').read_text(encoding='utf-8'))['records']
            lines = json.loads((seed / 'order_lines.json').read_text(encoding='utf-8'))['records']
            meta = json.loads((seed / '_meta.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        shift = 0
        if today:
            target = self.server.clock.today() if today == 'auto' else (
                today if isinstance(today, date) else date.fromisoformat(str(today)))
            shift = (target - date.fromisoformat(meta['seedToday'])).days

        def moved(value):
            if not value or not shift:
                return value
            return iso_ms(parse_iso(value) + timedelta(days=shift))
        by_order = {}
        for ln in lines:
            for ref in ln['fields'].get('Order', []):
                by_order.setdefault(ref, []).append(ln['fields'])
        for o in orders:
            f = o['fields']
            if not f.get('Shopify Order ID'):
                continue
            gid = f'gid://shopify/Order/{f["Shopify Order ID"]}'
            number = int(f['Order Number'].lstrip('#'))
            self.next_order_number = max(self.next_order_number, number + 1)
            self.orders[gid] = {
                'id': gid, 'name': f['Order Number'], 'email': f.get('Email'), 'createdAt': moved(f.get('Order Date')),
                'updatedAt': moved(f.get('Shopify Updated At') or f.get('Order Date')), 'cancelledAt': moved(f.get('Cancelled At')),
                'closedAt': None,
                'displayFinancialStatus': (f.get('Financial Status') or 'Paid').upper().replace(' ', '_'),
                'displayFulfillmentStatus': {'Fulfilled': 'FULFILLED', 'Partial': 'PARTIALLY_FULFILLED'}.get(
                    f.get('Fulfillment Status'), 'UNFULFILLED'),
                'note': f.get('Gift Note'), 'tags': [t.strip() for t in (f.get('Shopify Tags') or '').split(',') if t.strip()],
                '_customer': self._customer_refs.get((f.get('Customer') or [None])[0]), 'legacyResourceId': f['Shopify Order ID'],
                'shippingAddress': {'city': f.get('Shipping City'), 'provinceCode': f.get('Shipping Region'),
                                    'countryCodeV2': f.get('Shipping Country'), 'zip': f.get('Shipping Postal Code'),
                                    'name': f.get('Shipping Name')},
                'totalPriceSet': money_bag(f.get('Total', 0)), 'subtotalPriceSet': money_bag(f.get('Subtotal', 0)),
                '_lines': [{'id': f'gid://shopify/LineItem/{ln["Line Item ID"]}', 'name': ln.get('Product Title', ''),
                            'title': ln.get('Product Title', ''), 'sku': ln.get('SKU'), 'quantity': ln.get('Quantity', 1),
                            'currentQuantity': ln.get('Current Quantity', ln.get('Quantity', 1)),
                            'originalUnitPriceSet': money_bag(ln.get('Unit Price', 0)), '_sku': ln.get('SKU')}
                           for ln in by_order.get(o['ref'], [])],
            }

    def add_product(self, productId, variantId, inventoryItemId, sku, title, price=0, available=0, status='ACTIVE',
                    productType='', tracked=True, **_):
        """Add a product with one variant and stock at the primary location."""
        self.products[productId] = {'id': productId, 'title': title, 'handle': re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-'),
                                    'status': status, 'vendor': self.shop.get('name', 'Demo Chocolate Co.'),
                                    'productType': productType, 'tags': [], 'legacyResourceId': gid_num(productId),
                                    '_variants': [variantId]}
        self.variants[variantId] = {'id': variantId, 'sku': sku, 'title': 'Default Title', 'displayName': f'{title} - Default Title',
                                    'price': money(price), 'legacyResourceId': gid_num(variantId), '_product': productId,
                                    '_item': inventoryItemId}
        self.items[inventoryItemId] = {'id': inventoryItemId, 'sku': sku, 'tracked': tracked,
                                       'legacyResourceId': gid_num(inventoryItemId), '_variant': variantId}
        for loc in self.locations:
            q = {n: 0 for n in QUANTITY_NAMES}
            if loc == self.primary_location:
                q['available'] = int(available)
                q['on_hand'] = int(available)
            self.levels[(inventoryItemId, loc)] = q

    # ------------------------------------------------------------------ test helpers

    def available(self, sku, location_id=None):
        """Current "available" quantity for a SKU at a location (default: the primary location)."""
        item = self.item_by_sku(sku)
        return self.levels[(item['id'], location_id or self.primary_location)]['available']

    def set_available(self, sku, quantity, location_id=None):
        item = self.item_by_sku(sku)
        q = self.levels[(item['id'], location_id or self.primary_location)]
        q['on_hand'] += int(quantity) - q['available']
        q['available'] = int(quantity)

    def item_by_sku(self, sku):
        for item in self.items.values():
            if item['sku'] == sku:
                return item
        raise KeyError(sku)

    def variant_by_sku(self, sku):
        for v in self.variants.values():
            if v['sku'] == sku:
                return v
        return None

    def throttle(self, times=1):
        """The next `times` GraphQL calls answer THROTTLED (HTTP 200, errors[0].extensions.code)."""
        self._throttle_next = times

    def order_payload(self, number, email, line_items, shipping_address=None, customer=None, note=None,
                      note_attributes=None, financial_status='paid', fulfillment_status=None, refunds=None,
                      cancelled_at=None, cancel_reason=None, created_at=None, updated_at=None, tags='',
                      shipping_price=6.95, order_id=None, phone=None, currency='USD'):
        """A REST style order payload, as Shopify sends it for orders/create and orders/updated.

        line_items: [{'sku', 'quantity', 'price', 'title'?, 'id'?}] (variant_id and product_id are filled in
        from the store when the SKU exists in Shopify, otherwise left null like a custom item)
        refunds: [{'line_item_id', 'quantity', 'amount'?, 'restock_type'?}]
        """
        now = self.server.clock.now()
        created = created_at or shop_time(now)
        updated = updated_at or created
        oid = order_id or 5200000000000 + number
        lines, total_lines = [], 0.0
        for i, li in enumerate(line_items, 1):
            variant = self.variant_by_sku(li.get('sku')) if li.get('sku') else None
            product = self.products.get(variant['_product']) if variant else None
            lid = li.get('id') or int(f'{14100000000000 + number * 100 + i}')
            price = float(li['price'])
            qty = int(li['quantity'])
            total_lines += price * qty
            title = li.get('title') or (product['title'] if product else li.get('sku') or 'Custom item')
            lines.append({
                'id': lid, 'admin_graphql_api_id': f'gid://shopify/LineItem/{lid}',
                'variant_id': int(gid_num(variant['id'])) if variant else None,
                'product_id': int(gid_num(product['id'])) if product else None,
                'sku': li.get('sku'), 'title': title, 'variant_title': None, 'name': title,
                'vendor': self.shop.get('name', 'Demo Chocolate Co.'), 'quantity': qty, 'current_quantity': qty,
                'fulfillable_quantity': 0 if fulfillment_status == 'fulfilled' else qty,
                'fulfillment_status': 'fulfilled' if fulfillment_status == 'fulfilled' else None,
                'fulfillment_service': 'manual', 'price': f'{price:.2f}',
                'price_set': {'shop_money': {'amount': f'{price:.2f}', 'currency_code': currency},
                              'presentment_money': {'amount': f'{price:.2f}', 'currency_code': currency}},
                'total_discount': '0.00', 'gift_card': False, 'requires_shipping': True, 'taxable': True,
                'grams': int(li.get('grams', 90)), 'properties': li.get('properties', []), 'product_exists': bool(product),
                'tax_lines': [], 'discount_allocations': [], 'duties': [],
            })
        refund_objs, refunded_total = [], 0.0
        for j, rf in enumerate(refunds or [], 1):
            line = next(l for l in lines if l['id'] == rf['line_item_id'])
            qty = int(rf['quantity'])
            line['current_quantity'] -= qty
            line['fulfillable_quantity'] = max(line['fulfillable_quantity'] - qty, 0)
            amount = float(rf.get('amount', float(line['price']) * qty))
            refunded_total += amount
            rid = 9300000000000 + number * 10 + j
            refund_objs.append({
                'id': rid, 'admin_graphql_api_id': f'gid://shopify/Refund/{rid}', 'order_id': oid,
                'created_at': rf.get('created_at', updated), 'processed_at': rf.get('created_at', updated),
                'note': rf.get('note'), 'restock': rf.get('restock_type', 'no_restock') != 'no_restock', 'user_id': None,
                'refund_line_items': [{'id': rid + 1, 'line_item_id': line['id'], 'location_id': None, 'quantity': qty,
                                       'restock_type': rf.get('restock_type', 'no_restock'), 'subtotal': round(amount, 2),
                                       'subtotal_set': {'shop_money': {'amount': f'{amount:.2f}', 'currency_code': currency},
                                                        'presentment_money': {'amount': f'{amount:.2f}', 'currency_code': currency}},
                                       'total_tax': 0.0, 'line_item': dict(line)}],
                'transactions': [{'id': rid + 2, 'kind': 'refund', 'status': 'success', 'amount': f'{amount:.2f}',
                                  'currency': currency, 'gateway': 'shopify_payments'}],
                'order_adjustments': [],
            })
        cust = customer or {}
        first, _, last = (cust.get('name') or '').partition(' ')
        cust_id = cust.get('id', 7700000000000 + number)
        ship = dict(shipping_address or {})
        ship.setdefault('first_name', first or None)
        ship.setdefault('last_name', last or None)
        ship.setdefault('name', cust.get('name'))
        ship.setdefault('address2', None)
        ship.setdefault('company', None)
        ship.setdefault('phone', phone)
        ship.setdefault('country', {'US': 'United States', 'CA': 'Canada'}.get(ship.get('country_code'), ship.get('country_code')))
        ship.setdefault('latitude', None)
        ship.setdefault('longitude', None)
        subtotal = total_lines
        total = subtotal + shipping_price
        current_total = total - refunded_total
        return {
            'id': oid, 'admin_graphql_api_id': f'gid://shopify/Order/{oid}', 'app_id': 580111, 'name': f'#{number}',
            'number': number - 1000, 'order_number': number, 'confirmation_number': f'DC{number}X', 'email': email,
            'contact_email': email, 'phone': phone, 'created_at': created, 'updated_at': updated, 'processed_at': created,
            'cancelled_at': cancelled_at, 'cancel_reason': cancel_reason, 'closed_at': None, 'confirmed': True, 'test': False,
            'currency': currency, 'presentment_currency': currency, 'buyer_accepts_marketing': bool(cust.get('accepts_marketing')),
            'financial_status': financial_status, 'fulfillment_status': fulfillment_status, 'source_name': 'web',
            'note': note, 'note_attributes': note_attributes or [], 'tags': tags,
            'subtotal_price': f'{subtotal:.2f}', 'current_subtotal_price': f'{subtotal - refunded_total:.2f}',
            'total_line_items_price': f'{total_lines:.2f}', 'total_discounts': '0.00', 'current_total_discounts': '0.00',
            'total_tax': '0.00', 'current_total_tax': '0.00', 'taxes_included': False, 'estimated_taxes': False,
            'total_price': f'{total:.2f}', 'current_total_price': f'{current_total:.2f}',
            'total_outstanding': '0.00', 'total_weight': sum(l['grams'] * l['quantity'] for l in lines),
            'total_shipping_price_set': {'shop_money': {'amount': f'{shipping_price:.2f}', 'currency_code': currency},
                                         'presentment_money': {'amount': f'{shipping_price:.2f}', 'currency_code': currency}},
            'payment_gateway_names': ['shopify_payments'],
            'order_status_url': f'https://{self.shop_domain}/70000001/orders/{uuid.uuid5(uuid.NAMESPACE_URL, str(oid)).hex}/authenticate',
            'customer': {'id': cust_id, 'email': email, 'first_name': first or None, 'last_name': last or None,
                         'phone': cust.get('phone', phone), 'state': 'enabled', 'verified_email': True, 'tags': '',
                         'currency': currency, 'note': None, 'created_at': created, 'updated_at': updated,
                         'admin_graphql_api_id': f'gid://shopify/Customer/{cust_id}',
                         'default_address': {'city': ship.get('city'), 'province_code': ship.get('province_code'),
                                             'country_code': ship.get('country_code'), 'zip': ship.get('zip'), 'default': True}},
            'shipping_address': ship, 'billing_address': dict(ship),
            'line_items': lines,
            'shipping_lines': [{'id': 4400000000000 + number, 'title': 'Standard Shipping', 'code': 'Standard',
                                'price': f'{shipping_price:.2f}', 'source': 'shopify', 'discounted_price': f'{shipping_price:.2f}'}],
            'refunds': refund_objs, 'fulfillments': [], 'discount_codes': [], 'tax_lines': [],
        }

    # ------------------------------------------------------------------ routing

    def route(self, path):
        if re.match(r'^/admin/api/[^/]+/graphql\.json$', path):
            return 'shopify'
        return None

    def annotate(self, req, entry):
        entry['api_version'] = req.path.split('/')[3]
        body = req.json if isinstance(req.json, dict) else {}
        query = body.get('query') if body else req.text()
        entry['query'] = query
        entry['variables'] = body.get('variables') if body else None
        try:
            doc = G.parse_document(query or '')
            op = doc.operations[0]
            entry['operation_type'] = op.kind
            entry['operation'] = op.name
            entry['root_fields'] = [s.name for s in op.selections if isinstance(s, G.Field)]
        except Exception:
            pass

    def error_body(self, status):
        if status == 429:
            return {'errors': 'Exceeded 2 calls per second for api client. Reduce request rates to resume uninterrupted service.'}
        if status == 401:
            return {'errors': '[API] Invalid API key or access token (unrecognized login or wrong password)'}
        if status == 503:
            return {'errors': 'Unavailable Shop'}
        return {'errors': 'Internal Server Error'}

    # ------------------------------------------------------------------ handling

    def handle(self, req, entry):
        if req.method != 'POST':
            return json_response(404, {'errors': 'Not Found'})
        if req.headers.get('x-shopify-access-token') != self.token:
            return json_response(401, {'errors': '[API] Invalid API key or access token (unrecognized login or wrong password)'})
        version = req.path.split('/')[3]
        if version not in SUPPORTED_VERSIONS:
            return json_response(404, {'errors': 'Not Found'})
        ctype = req.headers.get('content-type', '')
        if 'application/graphql' in ctype:
            query, variables, op_name = req.text(), {}, None
        else:
            if req.json_error or not isinstance(req.json, dict) or not isinstance(req.json.get('query'), str):
                return json_response(400, {'errors': {'query': 'Required parameter missing or invalid'}})
            query, variables, op_name = req.json['query'], req.json.get('variables') or {}, req.json.get('operationName')
        try:
            doc = G.parse_document(query)
        except G.GraphQLSyntaxError as e:
            return json_response(200, {'errors': [{'message': str(e), 'locations': [{'line': e.line, 'column': e.col}]}]})
        op = doc.operations[0] if len(doc.operations) == 1 or not op_name else next(
            (o for o in doc.operations if o.name == op_name), doc.operations[0])
        try:
            cost = G.requested_cost(SCHEMA, doc, op, variables if isinstance(variables, dict) else {})
        except Exception:
            cost = 1
        self._refill()
        if cost > 1000:
            return json_response(200, {'errors': [{
                'message': f'Query cost is {cost}, which exceeds the single query max cost limit (1000).\n\n'
                           'See https://shopify.dev/concepts/about-apis/rate-limits for more information on how the\n'
                           'cost of a query is calculated.\n\nTo query larger amounts of data with fewer limits, bulk '
                           'operations should be used instead.\nSee https://shopify.dev/tutorials/perform-bulk-operations-with-admin-api for usage details.\n',
                'extensions': {'code': 'MAX_COST_EXCEEDED', 'cost': cost, 'maxCost': 1000}}]})
        if self._throttle_next > 0 or cost > self._available:
            if self._throttle_next > 0:
                self._throttle_next -= 1
            entry['throttled'] = True
            return json_response(200, {'errors': [{'message': 'Throttled', 'extensions': {
                'code': 'THROTTLED', 'documentation': 'https://shopify.dev/api/usage/limits#rate-limits'}}],
                'extensions': {'cost': self._cost_ext(cost, 0)}})
        self._available -= cost
        ctx = {'version': version, 'raw_variables': variables, 'entry': entry}
        result, _ = G.execute(SCHEMA, doc, self.resolvers(), variables if isinstance(variables, dict) else None, op_name, ctx,
                              type_resolver=lambda v: v.get('__typename'))
        if 'data' not in result:
            self._available += cost
            entry['graphql_errors'] = result['errors']
            return json_response(200, result)
        if result.get('errors'):
            entry['graphql_errors'] = result['errors']
        result['extensions'] = {'cost': self._cost_ext(cost, cost)}
        return json_response(200, result)

    def _refill(self):
        now = time.monotonic()
        self._available = min(self.bucket['maximumAvailable'],
                              self._available + (now - self._last_refill) * self.bucket['restoreRate'])
        self._last_refill = now

    def _cost_ext(self, requested, actual):
        return {'requestedQueryCost': requested, 'actualQueryCost': actual if actual else None,
                'throttleStatus': {'maximumAvailable': self.bucket['maximumAvailable'],
                                   'currentlyAvailable': int(self._available),
                                   'restoreRate': self.bucket['restoreRate']}}

    # ------------------------------------------------------------------ resolvers

    def resolvers(self):
        return {
            'QueryRoot.shop': lambda p, a, i: {**self.shop, '__typename': 'Shop'},
            'QueryRoot.node': lambda p, a, i: self._node(a['id']),
            'QueryRoot.nodes': self._nodes,
            'QueryRoot.location': lambda p, a, i: self._typed('Location', self.locations.get(a.get('id') or self.primary_location)),
            'QueryRoot.locations': lambda p, a, i: self._connection(
                [self._typed('Location', l) for l in self.locations.values() if l['isActive'] or a.get('includeInactive')], a, i),
            'QueryRoot.product': lambda p, a, i: self._typed('Product', self.products.get(a['id'])),
            'QueryRoot.products': lambda p, a, i: self._connection(self._sorted(
                [self._typed('Product', x) for x in self._search(self.products.values(), a.get('query'), self._product_terms)],
                a.get('sortKey'), 'product'), a, i),
            'QueryRoot.productVariant': lambda p, a, i: self._typed('ProductVariant', self.variants.get(a['id'])),
            'QueryRoot.productVariants': lambda p, a, i: self._connection(self._sorted(
                [self._typed('ProductVariant', x) for x in self._search(self.variants.values(), a.get('query'), self._variant_terms)],
                a.get('sortKey'), 'variant'), a, i),
            'QueryRoot.inventoryItem': lambda p, a, i: self._typed('InventoryItem', self.items.get(a['id'])),
            'QueryRoot.inventoryItems': lambda p, a, i: self._connection(
                [self._typed('InventoryItem', x) for x in self._search(self.items.values(), a.get('query'), self._item_terms)], a, i),
            'QueryRoot.order': lambda p, a, i: self._typed('Order', self.orders.get(a['id'])),
            'QueryRoot.orders': lambda p, a, i: self._connection(self._sorted(
                [self._typed('Order', x) for x in self._search(self.orders.values(), a.get('query'), self._order_terms)],
                a.get('sortKey'), 'order'), a, i),
            'QueryRoot.draftOrder': lambda p, a, i: self._typed('DraftOrder', self.draft_orders.get(a['id'])),
            'QueryRoot.draftOrders': lambda p, a, i: self._connection(
                [self._typed('DraftOrder', x) for x in self._search(self.draft_orders.values(), a.get('query'), self._draft_terms)], a, i),
            'QueryRoot.customer': lambda p, a, i: self._typed('Customer', self.customers.get(a['id'])),
            'QueryRoot.customers': lambda p, a, i: self._connection(
                [self._typed('Customer', x) for x in self._search(self.customers.values(), a.get('query'), self._customer_terms)], a, i),
            'Product.variants': lambda p, a, i: self._connection([self._typed('ProductVariant', self.variants[v]) for v in p['_variants']], a, i),
            'Product.totalInventory': lambda p, a, i: sum(self.levels[(self.variants[v]['_item'], loc)]['available']
                                                          for v in p['_variants'] for loc in self.locations),
            'ProductVariant.product': lambda p, a, i: self._typed('Product', self.products[p['_product']]),
            'ProductVariant.inventoryItem': lambda p, a, i: self._typed('InventoryItem', self.items[p['_item']]),
            'ProductVariant.inventoryQuantity': lambda p, a, i: sum(self.levels[(p['_item'], loc)]['available'] for loc in self.locations),
            'InventoryItem.variant': lambda p, a, i: self._typed('ProductVariant', self.variants[p['_variant']]),
            'InventoryItem.inventoryLevel': lambda p, a, i: self._level(p['id'], a['locationId']),
            'InventoryItem.inventoryLevels': lambda p, a, i: self._connection(
                [self._level(p['id'], loc) for loc in self.locations], a, i),
            'InventoryLevel.quantities': self._quantities,
            'InventoryAdjustmentGroup.changes': self._changes,
            'Order.lineItems': lambda p, a, i: self._connection([self._order_line(l) for l in p['_lines']], a, i),
            'Order.customer': lambda p, a, i: self._typed('Customer', self.customers.get(p.get('_customer'))) if p.get('_customer') else None,
            'DraftOrder.lineItems': lambda p, a, i: self._connection([self._typed('DraftOrderLineItem', l) for l in p['_lines']], a, i),
            'DraftOrder.customer': lambda p, a, i: self._typed('Customer', self.customers.get(p.get('_customer'))) if p.get('_customer') else None,
            'DraftOrder.order': lambda p, a, i: self._typed('Order', self.orders.get(p.get('_order'))) if p.get('_order') else None,
            'DraftOrderLineItem.variant': lambda p, a, i: self._typed('ProductVariant', self.variants.get(p.get('_variant'))) if p.get('_variant') else None,
            'LineItem.variant': lambda p, a, i: self._typed('ProductVariant', self.variant_by_sku(p.get('_sku'))) if p.get('_sku') and self.variant_by_sku(p.get('_sku')) else None,
            'Mutation.inventorySetQuantities': self._inventory_set,
            'Mutation.draftOrderCreate': self._draft_create,
            'Mutation.draftOrderInvoiceSend': self._draft_invoice,
            'Mutation.draftOrderComplete': self._draft_complete,
            'Mutation.tagsAdd': self._tags_add,
        }

    @staticmethod
    def _typed(name, obj):
        if obj is None:
            return None
        o = dict(obj)
        o['__typename'] = name
        return o

    def _node(self, gid):
        for name, store in (('Order', self.orders), ('DraftOrder', self.draft_orders), ('Customer', self.customers),
                            ('Product', self.products), ('ProductVariant', self.variants), ('InventoryItem', self.items),
                            ('Location', self.locations)):
            if gid in store:
                return self._typed(name, store[gid])
        return None

    def _sorted(self, items, key, kind):
        """sortKey, default ID (numeric part of the gid). Keys the mock cannot rank (POPULAR, RELEVANCE...) keep ID order."""
        by_id = lambda x: int(gid_num(x['id'])) if gid_num(x['id']).isdigit() else 0
        items = sorted(items, key=by_id)
        stock = lambda item_id: sum(self.levels[(item_id, loc)]['available'] for loc in self.locations)
        rank = {
            'variant': {'SKU': lambda v: (v.get('sku') or '').lower(),
                        'TITLE': lambda v: self.products[v['_product']]['title'].lower(),
                        'FULL_TITLE': lambda v: self.products[v['_product']]['title'].lower(),
                        'NAME': lambda v: self.products[v['_product']]['title'].lower(),
                        'INVENTORY_QUANTITY': lambda v: stock(v['_item']),
                        'INVENTORY_LEVELS_AVAILABLE': lambda v: stock(v['_item'])},
            'product': {'TITLE': lambda p: p['title'].lower(), 'PRODUCT_TYPE': lambda p: p['productType'].lower(),
                        'VENDOR': lambda p: p['vendor'].lower(),
                        'INVENTORY_TOTAL': lambda p: sum(stock(self.variants[v]['_item']) for v in p['_variants'])},
            'order': {'ORDER_NUMBER': lambda o: int(o['name'].lstrip('#')), 'CREATED_AT': lambda o: o.get('createdAt') or '',
                      'PROCESSED_AT': lambda o: o.get('createdAt') or '', 'UPDATED_AT': lambda o: o.get('updatedAt') or '',
                      'TOTAL_PRICE': lambda o: float(o['totalPriceSet']['shopMoney']['amount']),
                      'CURRENT_TOTAL_PRICE': lambda o: float(o['totalPriceSet']['shopMoney']['amount'])},
        }[kind]
        if key in rank:
            items = sorted(items, key=rank[key])
        return items

    def _nodes(self, parent, args, info):
        if len(args['ids']) > 250:
            raise G.GraphQLFieldError('The ids argument can contain at most 250 ids')
        return [self._node(gid) for gid in args['ids']]

    def _level(self, item_id, location_id):
        if (item_id, location_id) not in self.levels:
            return None
        return {'__typename': 'InventoryLevel', 'id': f'gid://shopify/InventoryLevel/{gid_num(item_id)}?inventory_item_id={gid_num(item_id)}',
                'location': self._typed('Location', self.locations[location_id]), 'item': self._typed('InventoryItem', self.items[item_id]),
                'updatedAt': self.server.clock.iso(), '_key': (item_id, location_id)}

    def _quantities(self, parent, args, info):
        q = self.levels[parent['_key']]
        out = []
        for n in args['names']:
            if n not in QUANTITY_NAMES:
                raise G.GraphQLFieldError(f'The quantity name {n} is not valid. Valid names are: {", ".join(QUANTITY_NAMES)}')
            out.append({'id': f'gid://shopify/InventoryQuantity/{gid_num(parent["_key"][0])}?name={n}', 'name': n,
                        'quantity': q[n], 'updatedAt': self.server.clock.iso()})
        return out

    def _changes(self, parent, args, info):
        out = []
        for c in parent['_changes']:
            if args.get('inventoryItemIds') and c['_item'] not in args['inventoryItemIds']:
                continue
            if args.get('locationIds') and c['_location'] not in args['locationIds']:
                continue
            if args.get('quantityNames') and c['name'] not in args['quantityNames']:
                continue
            out.append({'name': c['name'], 'delta': c['delta'], 'quantityAfterChange': c['quantityAfterChange'],
                        'item': self._typed('InventoryItem', self.items[c['_item']]),
                        'location': self._typed('Location', self.locations[c['_location']])})
        return out

    def _order_line(self, line):
        return self._typed('LineItem', line)

    def _connection(self, items, args, info):
        first, last = args.get('first'), args.get('last')
        if first is None and last is None:
            raise G.GraphQLFieldError('you must provide one of first or last')
        if (first is not None and first > 250) or (last is not None and last > 250):
            raise G.GraphQLFieldError('The first and last arguments must be less than or equal to 250')
        if args.get('reverse'):
            items = list(reversed(items))
        ids = [x.get('id') for x in items]
        start = 0
        if args.get('after'):
            try:
                after = json.loads(base64.b64decode(args['after']).decode())['last_id']
                start = ids.index(after) + 1
            except Exception:
                raise G.GraphQLFieldError('Invalid cursor')
        window = items[start:]
        if first is not None:
            page = window[:first]
            has_next = len(window) > first
        else:
            page = window[-last:] if last else []
            has_next = False
        cursor = lambda x: base64.b64encode(json.dumps({'last_id': x.get('id')}).encode()).decode()
        return {'edges': [{'cursor': cursor(x), 'node': x} for x in page], 'nodes': page,
                'pageInfo': {'hasNextPage': has_next, 'hasPreviousPage': start > 0,
                             'startCursor': cursor(page[0]) if page else None, 'endCursor': cursor(page[-1]) if page else None}}

    # ------------------------------------------------------------------ search syntax (subset)

    @staticmethod
    def _search(items, query, terms):
        items = list(items)
        if not query:
            return items
        groups = [g.strip() for g in re.split(r'\s+OR\s+', query) if g.strip()]
        out = []
        for item in items:
            for g in groups:
                ok = True
                for tok in re.findall(r'(-?\w+:(?:"[^"]*"|\S+)|"[^"]*"|\S+)', g):
                    if tok.upper() == 'AND':
                        continue
                    neg = tok.startswith('-')
                    tok = tok[1:] if neg else tok
                    if ':' in tok:
                        key, _, val = tok.partition(':')
                        val = val.strip('"')
                    else:
                        key, val = 'default', tok.strip('"')
                    hit = terms(item, key.lower(), val)
                    if hit is None:
                        # Shopify supports many more search fields than the mock; fail loudly instead of quietly
                        # returning nothing, so a test never passes or fails on a filter the mock cannot apply
                        raise G.GraphQLFieldError(f"The mock does not support the search field '{key}'. Supported: "
                                                  f"{', '.join(SEARCH_FIELDS.get(terms.__name__, []))}")
                    if hit == neg:
                        ok = False
                        break
                if ok:
                    out.append(item)
                    break
        return out

    def _variant_terms(self, v, key, val):
        if key == 'sku':
            return (v['sku'] or '').lower() == val.lower()
        if key == 'product_id':
            return gid_num(v['_product']) == val
        if key == 'id':
            return gid_num(v['id']) == val
        if key in ('title', 'default'):
            return val.lower() in self.products[v['_product']]['title'].lower()
        return None

    def _product_terms(self, p, key, val):
        if key == 'sku':
            return any((self.variants[x]['sku'] or '').lower() == val.lower() for x in p['_variants'])
        if key == 'status':
            return p['status'].lower() == val.lower()
        if key == 'id':
            return gid_num(p['id']) == val
        if key in ('title', 'default'):
            return val.lower() in p['title'].lower()
        return None

    def _item_terms(self, item, key, val):
        if key == 'sku':
            return (item['sku'] or '').lower() == val.lower()
        if key == 'id':
            return gid_num(item['id']) == val
        return None

    def _order_terms(self, o, key, val):
        if key in ('created_at', 'updated_at'):
            stamp = o.get('createdAt' if key == 'created_at' else 'updatedAt')
            m = re.fullmatch(r'(>=|<=|>|<)?(.+)', val)
            op, when = m.group(1) or '=', m.group(2).strip("'\"")
            if not stamp:
                return False
            try:
                a, b = parse_iso(stamp), parse_iso(when)
            except ValueError:
                raise G.GraphQLFieldError(f"The mock cannot read the date '{when}' in the search query")
            return {'=': a == b, '>': a > b, '>=': a >= b, '<': a < b, '<=': a <= b}[op]
        if key == 'tag':
            return val in o.get('tags', [])
        if key == 'status':
            state = 'cancelled' if o.get('cancelledAt') else ('closed' if o.get('closedAt') else 'open')
            return val.lower() == state
        if key == 'name':
            return o['name'].lstrip('#') == val.lstrip('#')
        if key == 'email':
            return (o.get('email') or '').lower() == val.lower()
        if key == 'id':
            return gid_num(o['id']) == val
        if key == 'default':
            return val.lstrip('#') == o['name'].lstrip('#') or val.lower() == (o.get('email') or '').lower()
        return None

    def _draft_terms(self, d, key, val):
        if key == 'status':
            return d['status'].lower() == val.lower()
        if key == 'email':
            return (d.get('email') or '').lower() == val.lower()
        if key == 'tag':
            return val in d['tags']
        if key == 'name':
            return d['name'].lstrip('#') == val.lstrip('#')
        return None

    def _customer_terms(self, c, key, val):
        if key in ('email', 'default'):
            return (c.get('email') or '').lower() == val.lower()
        if key == 'id':
            return gid_num(c['id']) == val
        return None

    # ------------------------------------------------------------------ mutations

    def _inventory_set(self, parent, args, info):
        inp = args['input']
        version = info.context['version']
        directive = info.directives.get('idempotent')
        if directive is not None and (set(directive) - {'key'} or not isinstance(directive.get('key', ''), str)):
            raise G.GraphQLFieldError("Directive 'idempotent' takes one argument, key: String!", {'code': 'argumentLiteralsIncompatible'})
        if version >= '2026-04' and (directive is None or not directive.get('key')):
            raise G.GraphQLFieldError('The @idempotent directive with a non-empty key is required for inventorySetQuantities '
                                      'as of API version 2026-04.', {'code': 'BAD_REQUEST'})
        key = directive.get('key') if directive else None
        fingerprint = json.dumps(inp, sort_keys=True)
        if key and key in self.idempotency:
            prev_fp, prev_result = self.idempotency[key]
            if prev_fp != fingerprint:
                return {'inventoryAdjustmentGroup': None, 'userErrors': [{
                    'code': 'IDEMPOTENCY_KEY_PARAMETER_MISMATCH', 'field': None,
                    'message': 'The same idempotency key cannot be used with different operation parameters.'}]}
            if prev_result.get('userErrors'):
                # a key whose first attempt failed is used up: a new attempt needs a new key
                return {'inventoryAdjustmentGroup': None, 'userErrors': [{
                    'code': 'IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED', 'field': None,
                    'message': 'A previous request with this idempotency key failed. Retry with a new idempotency key.'}]}
            info.context['entry']['idempotent_replay'] = True
            return prev_result
        errors = []
        if inp['name'] not in ('available', 'on_hand'):
            errors.append(('INVALID_NAME', ['input', 'name'], "The quantity name must be either 'available' or 'on_hand'."))
        if inp['reason'] not in INVENTORY_REASONS:
            errors.append(('INVALID_REASON', ['input', 'reason'], 'The specified reason is invalid.'))
        uri = inp.get('referenceDocumentUri')
        if uri is not None and not URI_RE.match(uri):
            errors.append(('INVALID_REFERENCE_DOCUMENT', ['input', 'referenceDocumentUri'], 'The specified reference document is invalid.'))
        seen = set()
        for idx, q in enumerate(inp['quantities']):
            pair = (q['inventoryItemId'], q['locationId'])
            f = ['input', 'quantities', str(idx)]
            if pair in seen:
                errors.append(('NO_DUPLICATE_INVENTORY_ITEM_ID_GROUP_ID_PAIR', f, 'The combination of inventoryItemId and locationId must be unique.'))
            seen.add(pair)
            if q['inventoryItemId'] not in self.items:
                errors.append(('INVALID_INVENTORY_ITEM', f + ['inventoryItemId'], 'The specified inventory item could not be found.'))
                continue
            if q['locationId'] not in self.locations or not self.locations[q['locationId']]['isActive']:
                errors.append(('INVALID_LOCATION', f + ['locationId'], 'The specified location could not be found.'))
                continue
            if q['quantity'] < 0:
                errors.append(('INVALID_QUANTITY_NEGATIVE', f + ['quantity'], "The quantity can't be negative."))
            if q['quantity'] > 1_000_000_000:
                errors.append(('INVALID_QUANTITY_TOO_HIGH', f + ['quantity'], "The total quantity can't be higher than 1,000,000,000."))
            if 'changeFromQuantity' not in q:
                errors.append(('CHANGE_FROM_QUANTITY_STALE', f + ['changeFromQuantity'],
                               'changeFromQuantity is required as of API version 2026-04. Pass the current quantity, or null '
                               'to skip the check.'))
            elif q['changeFromQuantity'] is not None:
                current = self.levels[pair][inp['name']]
                if q['changeFromQuantity'] != current:
                    errors.append(('CHANGE_FROM_QUANTITY_STALE', f + ['changeFromQuantity'],
                                   'The changeFromQuantity value does not match persisted value.'))
        if errors:
            failed = {'inventoryAdjustmentGroup': None,
                      'userErrors': [{'code': c, 'field': fld, 'message': m} for c, fld, m in errors]}
            if key:
                self.idempotency[key] = (fingerprint, failed)
            return failed
        self._counter += 1
        changes = []
        for q in inp['quantities']:
            pair = (q['inventoryItemId'], q['locationId'])
            level = self.levels[pair]
            delta = q['quantity'] - level[inp['name']]
            level[inp['name']] = q['quantity']
            changes.append({'name': inp['name'], 'delta': delta, 'quantityAfterChange': level[inp['name']],
                            '_item': pair[0], '_location': pair[1]})
            if inp['name'] == 'available':
                level['on_hand'] += delta
                changes.append({'name': 'on_hand', 'delta': delta, 'quantityAfterChange': level['on_hand'],
                                '_item': pair[0], '_location': pair[1]})
        group = {'__typename': 'InventoryAdjustmentGroup', 'id': f'gid://shopify/InventoryAdjustmentGroup/{60000000000 + self._counter}',
                 'createdAt': self.server.clock.now().strftime('%Y-%m-%dT%H:%M:%SZ'), 'reason': inp['reason'],
                 'referenceDocumentUri': uri, '_changes': changes}
        self.adjustments.append({'id': group['id'], 'name': inp['name'], 'reason': inp['reason'], 'referenceDocumentUri': uri,
                                 'idempotencyKey': key,
                                 'changes': [{'sku': self.items[c['_item']]['sku'], 'name': c['name'], 'delta': c['delta'],
                                              'quantityAfterChange': c['quantityAfterChange'], 'locationId': c['_location']}
                                             for c in changes]})
        result = {'inventoryAdjustmentGroup': group, 'userErrors': []}
        if key:
            self.idempotency[key] = (fingerprint, result)
        return result

    def _draft_create(self, parent, args, info):
        inp = args['input']
        errors = []
        lines_in = inp.get('lineItems') or []
        if not lines_in:
            errors.append({'field': ['lineItems'], 'message': 'Add at least 1 product'})
        if inp.get('email') and not EMAIL_RE.match(inp['email']):
            errors.append({'field': ['email'], 'message': 'Email is invalid'})
        cust_id = (inp.get('purchasingEntity') or {}).get('customerId') or inp.get('customerId')
        if cust_id and cust_id not in self.customers:
            errors.append({'field': ['purchasingEntity', 'customerId'], 'message': 'Customer does not exist'})
        lines, subtotal = [], 0.0
        for idx, li in enumerate(lines_in):
            f = ['lineItems', str(idx)]
            if li['quantity'] < 1:
                errors.append({'field': f + ['quantity'], 'message': 'Quantity must be greater than or equal to 1'})
            variant = None
            if li.get('variantId'):
                variant = self.variants.get(li['variantId'])
                if variant is None:
                    errors.append({'field': f + ['variantId'], 'message': f'Product with ID {li["variantId"]} is no longer available.'})
                    continue
                unit = float(li['priceOverride']['amount']) if li.get('priceOverride') else float(variant['price'])
                title = self.products[variant['_product']]['title']
                sku = variant['sku']
            else:
                if not li.get('title'):
                    errors.append({'field': f + ['title'], 'message': "Title can't be blank"})
                price = li.get('originalUnitPriceWithCurrency') or ({'amount': li['originalUnitPrice']} if li.get('originalUnitPrice') else None)
                if price is None:
                    errors.append({'field': f + ['originalUnitPriceWithCurrency'], 'message': "Original unit price can't be blank"})
                    continue
                unit = float(price['amount'])
                title, sku = li.get('title') or '', li.get('sku')
            line_total = unit * li['quantity']
            disc = self._discount(li.get('appliedDiscount'), line_total, f + ['appliedDiscount'], errors)
            subtotal += line_total - (disc['amount'] if disc else 0)
            self._counter += 1
            lines.append({'id': f'gid://shopify/DraftOrderLineItem/{80000000000 + self._counter}', 'name': title, 'title': title,
                          'sku': sku, 'quantity': li['quantity'], 'custom': variant is None,
                          'originalUnitPriceSet': money_bag(unit),
                          'discountedTotalSet': money_bag(line_total - (disc['amount'] if disc else 0)),
                          'appliedDiscount': disc['obj'] if disc else None, '_variant': variant['id'] if variant else None})
        order_disc = self._discount(inp.get('appliedDiscount'), subtotal, ['appliedDiscount'], errors)
        if errors:
            return {'draftOrder': None, 'userErrors': errors}
        shipping = 0.0
        sl = inp.get('shippingLine')
        if sl:
            shipping = float((sl.get('priceWithCurrency') or {}).get('amount') or sl.get('price') or 0)
        discount_total = order_disc['amount'] if order_disc else 0.0
        total = max(subtotal - discount_total, 0) + shipping
        number = self.next_draft_number
        self.next_draft_number += 1
        gid = f'gid://shopify/DraftOrder/{1100000000000 + number}'
        now = self.server.clock.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        token = uuid.uuid5(uuid.NAMESPACE_URL, gid).hex
        if not cust_id and inp.get('email'):
            cust_id = next((c['id'] for c in self.customers.values() if (c.get('email') or '') == inp['email'].lower()), None)
        draft = {'id': gid, 'name': f'#D{number}', 'status': 'OPEN', 'email': inp.get('email'), 'note2': inp.get('note'),
                 'tags': list(inp.get('tags') or []), 'invoiceUrl': f'https://{self.shop_domain}/70000001/invoices/{token}',
                 'invoiceSentAt': None, 'createdAt': now, 'updatedAt': now, 'completedAt': None,
                 'legacyResourceId': str(1100000000000 + number), 'totalPriceSet': money_bag(total),
                 'subtotalPriceSet': money_bag(subtotal), 'totalDiscountsSet': money_bag(discount_total + sum(
                     float(l['originalUnitPriceSet']['shopMoney']['amount']) * l['quantity'] - float(l['discountedTotalSet']['shopMoney']['amount'])
                     for l in lines)),
                 'totalShippingPriceSet': money_bag(shipping), 'totalTaxSet': money_bag(0),
                 'appliedDiscount': order_disc['obj'] if order_disc else None, 'poNumber': inp.get('poNumber'),
                 'reserveInventoryUntil': inp.get('reserveInventoryUntil'),
                 'shippingAddress': _address_out(inp.get('shippingAddress')), '_lines': lines, '_customer': cust_id,
                 '_order': None, '_input': inp}
        self.draft_orders[gid] = draft
        return {'draftOrder': self._typed('DraftOrder', draft), 'userErrors': []}

    @staticmethod
    def _discount(spec, base, field, errors):
        if not spec:
            return None
        value = spec['value']
        if spec['valueType'] == 'PERCENTAGE':
            if value < 0 or value > 100:
                errors.append({'field': field + ['value'], 'message': 'Value must be between 0 and 100'})
                return None
            amount = round(base * value / 100.0, 2)
        else:
            if value < 0:
                errors.append({'field': field + ['value'], 'message': 'Value must be greater than or equal to 0'})
                return None
            amount = min(value, base)
        return {'amount': amount, 'obj': {'title': spec.get('title'), 'description': spec.get('description') or '',
                                          'value': float(value), 'valueType': spec['valueType'], 'amountSet': money_bag(amount)}}

    def _draft_invoice(self, parent, args, info):
        draft = self.draft_orders.get(args['id'])
        if draft is None:
            return {'draftOrder': None, 'userErrors': [{'field': ['id'], 'message': 'Draft order does not exist'}]}
        if draft['status'] == 'COMPLETED':
            return {'draftOrder': None, 'userErrors': [{'field': ['id'], 'message': 'Draft order has already been completed'}]}
        email = args.get('email') or {}
        to = email.get('to') or draft.get('email')
        if not to:
            return {'draftOrder': None, 'userErrors': [{'field': ['email', 'to'], 'message': "To can't be blank"}]}
        if not EMAIL_RE.match(to):
            return {'draftOrder': None, 'userErrors': [{'field': ['email', 'to'], 'message': 'To is invalid'}]}
        now = self.server.clock.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        draft['invoiceSentAt'] = now
        draft['updatedAt'] = now
        draft['status'] = 'INVOICE_SENT'
        self.invoices.append({'draftOrderId': draft['id'], 'to': to, 'subject': email.get('subject'),
                              'customMessage': email.get('customMessage'), 'sentAt': now, 'invoiceUrl': draft['invoiceUrl']})
        return {'draftOrder': self._typed('DraftOrder', draft), 'userErrors': []}

    def _draft_complete(self, parent, args, info):
        draft = self.draft_orders.get(args['id'])
        if draft is None:
            return {'draftOrder': None, 'userErrors': [{'field': ['id'], 'message': 'Draft order does not exist'}]}
        if draft['status'] == 'COMPLETED':
            return {'draftOrder': None, 'userErrors': [{'field': ['id'], 'message': 'This order has already been paid'}]}
        number = self.next_order_number
        self.next_order_number += 1
        oid = 5200000000000 + number
        gid = f'gid://shopify/Order/{oid}'
        now = self.server.clock.now().strftime('%Y-%m-%dT%H:%M:%SZ')
        total = float(draft['totalPriceSet']['shopMoney']['amount'])
        pending = bool(args.get('paymentPending')) and total > 0
        order_lines = []
        for l in draft['_lines']:
            order_lines.append({'id': f'gid://shopify/LineItem/{14100000000000 + number * 100 + len(order_lines) + 1}',
                                'name': l['name'], 'title': l['title'], 'sku': l['sku'], 'quantity': l['quantity'],
                                'currentQuantity': l['quantity'], 'originalUnitPriceSet': l['originalUnitPriceSet'],
                                '_sku': l['sku']})
            if l.get('_variant'):
                level = self.levels[(self.variants[l['_variant']]['_item'], self.primary_location)]
                level['available'] -= l['quantity']
                level['committed'] += l['quantity']
        self.orders[gid] = {'id': gid, 'name': f'#{number}', 'email': draft.get('email'), 'createdAt': now, 'updatedAt': now,
                            'cancelledAt': None, 'closedAt': None,
                            'displayFinancialStatus': 'PENDING' if pending else 'PAID',
                            'displayFulfillmentStatus': 'UNFULFILLED', 'note': draft.get('note2'), 'tags': list(draft['tags']),
                            'legacyResourceId': str(oid), 'shippingAddress': draft.get('shippingAddress'),
                            'totalPriceSet': draft['totalPriceSet'], 'subtotalPriceSet': draft['subtotalPriceSet'],
                            '_lines': order_lines, '_customer': draft.get('_customer'), '_from_draft': draft['id']}
        draft['status'] = 'COMPLETED'
        draft['completedAt'] = now
        draft['updatedAt'] = now
        draft['_order'] = gid
        return {'draftOrder': self._typed('DraftOrder', draft), 'userErrors': []}

    def _tags_add(self, parent, args, info):
        node = self._node(args['id'])
        if node is None or node['__typename'] not in ('Order', 'DraftOrder', 'Customer', 'Product'):
            return {'node': None, 'userErrors': [{'field': ['id'], 'message': 'Node does not exist'}]}
        store = {'Order': self.orders, 'DraftOrder': self.draft_orders, 'Customer': self.customers, 'Product': self.products}[node['__typename']]
        obj = store[args['id']]
        for t in args['tags']:
            for part in [p.strip() for p in t.split(',') if p.strip()]:
                if part not in obj['tags']:
                    obj['tags'].append(part)
        return {'node': self._typed(node['__typename'], obj), 'userErrors': []}


def _address_out(a):
    if not a:
        return None
    name = ' '.join(x for x in (a.get('firstName'), a.get('lastName')) if x) or None
    return {'address1': a.get('address1'), 'address2': a.get('address2'), 'city': a.get('city'), 'company': a.get('company'),
            'countryCodeV2': a.get('countryCode'), 'firstName': a.get('firstName'), 'lastName': a.get('lastName'),
            'name': name, 'phone': a.get('phone'), 'provinceCode': a.get('provinceCode'), 'zip': a.get('zip'),
            'latitude': None, 'longitude': None}
