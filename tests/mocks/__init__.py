"""Shared strict mocks for the Demo Chocolate Co. automations. See tests/mocks/README.md for the contract."""
from .airtable import BASE_ID as AIRTABLE_BASE_ID, TOKEN as AIRTABLE_TOKEN
from .anthropic import API_KEY as ANTHROPIC_API_KEY
from .core import Clock, MockServer
from .postmark import SERVER_TOKEN as POSTMARK_SERVER_TOKEN
from .shopify import (ACCESS_TOKEN as SHOPIFY_ADMIN_TOKEN, DEFAULT_VERSION as SHOPIFY_API_VERSION, SHOP_DOMAIN,
                      WEBHOOK_SECRET as SHOPIFY_WEBHOOK_SECRET, post_webhook, sign_webhook, webhook_request)

__all__ = ['MockServer', 'Clock', 'AIRTABLE_BASE_ID', 'AIRTABLE_TOKEN', 'ANTHROPIC_API_KEY', 'POSTMARK_SERVER_TOKEN',
           'SHOPIFY_ADMIN_TOKEN', 'SHOPIFY_API_VERSION', 'SHOP_DOMAIN', 'SHOPIFY_WEBHOOK_SECRET', 'post_webhook',
           'sign_webhook', 'webhook_request']
