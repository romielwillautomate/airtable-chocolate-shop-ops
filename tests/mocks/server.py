"""Run every mock on one port from the command line, for manual exploration or out of process tests.

  python -m tests.mocks.server --port 8790                 seeded base (base/schema.json + base/seed)
  python -m tests.mocks.server --port 8790 --empty-base    no tables at all (to try setup/create_base.py)
  python -m tests.mocks.server --port 8790 --now 2026-10-20T09:00:00Z --seed-today none

Control endpoints while it runs (JSON):
  GET  /__mock/health                     {"ok": true}
  GET  /__mock/log?service=airtable&since=0
  GET  /__mock/airtable/<table name>      records as the API returns them
  POST /__mock/reset   {"empty_base": false}
  POST /__mock/fail    {"service": "airtable", "status": 500, "times": 1, "match": {"table": "Orders"}}
  POST /__mock/clock   {"now": "2026-10-20T09:00:00Z"}   or {"live": true}
"""
import argparse
import signal
import sys
import threading
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.mocks.core import MockServer
    from tests.mocks import airtable, anthropic, postmark, shopify
else:
    from .core import MockServer
    from . import airtable, anthropic, postmark, shopify


def main(argv=None):
    p = argparse.ArgumentParser(description='Run the Demo Chocolate Co. mock APIs.')
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--empty-base', action='store_true', help='start with no Airtable tables')
    p.add_argument('--now', help='freeze the mock clock at this ISO time')
    p.add_argument('--seed-today', default='auto',
                   help="shift seed dates so seedToday becomes this date (default 'auto' = the mock clock's today, "
                        "'none' = load the dates as written)")
    p.add_argument('--no-rate-limit', action='store_true', help='switch the Airtable 5 per second limiter off')
    args = p.parse_args(argv)
    seed_today = None if str(args.seed_today).lower() == 'none' else args.seed_today
    mock = MockServer(args.port, host=args.host, empty_base=args.empty_base, now=args.now, seed_today=seed_today)
    if args.no_rate_limit:
        mock.airtable.set_rate_limit(enabled=False)
    mock.start()
    print(f'Mock APIs on {mock.local_url} (n8n in Docker: {mock.url})')
    print(f'  Airtable base {airtable.BASE_ID}, token {airtable.TOKEN[:22]}...')
    print(f'  Shopify token {shopify.ACCESS_TOKEN}, webhook secret {shopify.WEBHOOK_SECRET}')
    print(f'  Postmark token {postmark.SERVER_TOKEN}, Anthropic key {anthropic.API_KEY}')
    print('Ctrl+C to stop.')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    try:
        while not stop.wait(0.5):
            pass
    finally:
        mock.stop()


if __name__ == '__main__':
    main()
