"""Mock of a Slack incoming webhook (hooks.slack.com/services/T.../B.../...).

Any path under /services/ or /slack/ is accepted, so each workflow can use its own webhook URL, for example
SLACK_WEBHOOK_URL = mock.url + '/services/T000/B000/packing'. Real behaviour reproduced:
  * JSON body (or form encoded payload=...), otherwise 400 invalid_payload
  * text or blocks required, otherwise 400 no_text
  * blocks must be a list of at most 50 blocks of known types; a section text is limited to 3000 characters
    and a header to 150, otherwise 400 invalid_blocks
  * answers 200 with the plain text body "ok"
Posted messages are kept in mock.slack.messages (the parsed JSON plus the path).
"""
import json
from urllib.parse import parse_qs

from .core import Response

BLOCK_TYPES = {'section', 'header', 'divider', 'context', 'actions', 'image', 'rich_text', 'input', 'file', 'video'}


def _text(status, body):
    return Response(status, body, content_type='text/plain; charset=utf-8')


class SlackMock:
    def __init__(self, server):
        self.server = server
        self.reset()

    def reset(self):
        self.messages = []
        self.archived_paths = set()

    def texts(self, path_contains=None):
        """The "text" of every message (optionally only those posted to a path containing the given string)."""
        return [m.get('text', '') for m in self.messages if path_contains is None or path_contains in m['_path']]

    def route(self, path):
        if path.startswith('/services/') or path.startswith('/slack/'):
            return 'slack'
        return None

    def annotate(self, req, entry):
        if isinstance(req.json, dict):
            entry['text'] = req.json.get('text')

    def error_body(self, status):
        return {404: 'no_service', 403: 'invalid_token', 410: 'channel_is_archived', 429: 'rate_limited'}.get(status, 'internal_error')

    def handle(self, req, entry):
        if req.method != 'POST':
            return _text(405, 'invalid_method')
        if req.path in self.archived_paths:
            return _text(410, 'channel_is_archived')
        payload = None
        ctype = req.headers.get('content-type', '')
        if 'application/x-www-form-urlencoded' in ctype:
            form = parse_qs(req.text())
            try:
                payload = json.loads(form.get('payload', [''])[0])
            except ValueError:
                payload = None
        elif req.json_error is None:
            payload = req.json
        if not isinstance(payload, dict):
            return _text(400, 'invalid_payload')
        text = payload.get('text')
        blocks = payload.get('blocks')
        if (text is None or text == '') and not blocks:
            return _text(400, 'no_text')
        if text is not None and not isinstance(text, str):
            return _text(400, 'invalid_payload')
        if blocks is not None:
            if not isinstance(blocks, list) or len(blocks) > 50:
                return _text(400, 'invalid_blocks')
            for b in blocks:
                if not isinstance(b, dict) or b.get('type') not in BLOCK_TYPES:
                    return _text(400, 'invalid_blocks')
                if b['type'] == 'section':
                    t = (b.get('text') or {}).get('text', '')
                    if not b.get('text') and not b.get('fields'):
                        return _text(400, 'invalid_blocks')
                    if len(t) > 3000:
                        return _text(400, 'invalid_blocks')
                if b['type'] == 'header' and len((b.get('text') or {}).get('text', '')) > 150:
                    return _text(400, 'invalid_blocks')
        if text and len(text) > 40000:
            text = text[:40000]
        self.messages.append(dict(payload, _path=req.path))
        return _text(200, 'ok')
