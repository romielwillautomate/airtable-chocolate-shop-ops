"""Mock of the Postmark email API (api.postmarkapp.com): POST /email and POST /email/batch.

Real behaviour reproduced:
  * X-Postmark-Server-Token header required (401, ErrorCode 10)
  * Content-Type must be JSON (415), the body must parse (422, ErrorCode 402)
  * unknown body fields are refused (422, ErrorCode 403 "Invalid request field(s)")
  * From must be a confirmed sender signature (422, ErrorCode 400)
  * To is required and every address must be valid, at most 50 recipients over To, Cc and Bcc (ErrorCode 300)
  * HtmlBody or TextBody is required (ErrorCode 300)
  * inactive (bounced or unsubscribed) recipients are refused (ErrorCode 406)
  * MessageStream must exist (default "outbound")
  * /email/batch takes up to 500 messages and answers 200 with one result per message
Sent messages are kept in mock.postmark.sent for assertions.
"""
import re
import uuid

from .core import json_response

SERVER_TOKEN = 'mock-postmark-server-token'
FIELDS = {'From', 'To', 'Cc', 'Bcc', 'Subject', 'Tag', 'HtmlBody', 'TextBody', 'ReplyTo', 'Headers', 'TrackOpens',
          'TrackLinks', 'Metadata', 'Attachments', 'MessageStream', 'InlineCss'}
ADDRESS = re.compile(r'^\s*(?:"?([^"<]*)"?\s*<)?([^@\s<>",]+@[^@\s<>",]+\.[A-Za-z]{2,})>?\s*$')


def _addresses(value):
    if not value:
        return []
    return [a for a in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', value) if a.strip()]


class PostmarkMock:
    def __init__(self, server, token=SERVER_TOKEN):
        self.server = server
        self.token = token
        self.reset()

    def reset(self):
        self.sender_signatures = {'orders@example.com', 'support@example.com', 'wholesale@example.com', 'hello@example.com'}
        self.inactive_recipients = set()
        self.streams = {'outbound', 'broadcast'}
        self.sent = []

    def route(self, path):
        if path in ('/email', '/email/batch'):
            return 'postmark'
        return None

    def annotate(self, req, entry):
        if isinstance(req.json, dict):
            entry['to'] = req.json.get('To')
            entry['subject'] = req.json.get('Subject')

    def error_body(self, status):
        if status == 401:
            return {'ErrorCode': 10, 'Message': 'Request does not contain a valid Server token.'}
        if status == 503:
            return {'ErrorCode': 100, 'Message': 'Maintenance'}
        if status == 429:
            return {'ErrorCode': 429, 'Message': 'Rate limit exceeded'}
        return {'ErrorCode': 101, 'Message': 'An unexpected error occurred. Please try again later.'}

    def handle(self, req, entry):
        if req.method != 'POST':
            return json_response(404, {'ErrorCode': 404, 'Message': 'Not found'})
        if req.headers.get('x-postmark-server-token') != self.token:
            return json_response(401, {'ErrorCode': 10, 'Message': 'Request does not contain a valid Server token.'})
        if 'json' not in req.headers.get('content-type', ''):
            return json_response(415, {'ErrorCode': 415, 'Message': 'Unsupported media type. Use Content-Type: application/json'})
        accept = req.headers.get('accept', '*/*')
        if 'json' not in accept and '*/*' not in accept:
            return json_response(406, {'ErrorCode': 406, 'Message': 'Accept header must allow application/json'})
        if req.json_error is not None or req.json is None:
            return json_response(422, {'ErrorCode': 402, 'Message': 'Received invalid JSON input.'})
        if req.path == '/email/batch':
            if not isinstance(req.json, list):
                return json_response(422, {'ErrorCode': 402, 'Message': 'Received invalid JSON input.'})
            if len(req.json) > 500:
                return json_response(422, {'ErrorCode': 410, 'Message': 'You may only send up to 500 messages in a single batched request.'})
            results = []
            for msg in req.json:
                status, body = self._send(msg)
                results.append(body)
            return json_response(200, results)
        if not isinstance(req.json, dict):
            return json_response(422, {'ErrorCode': 402, 'Message': 'Received invalid JSON input.'})
        status, body = self._send(req.json)
        entry['message_id'] = body.get('MessageID')
        return json_response(status, body)

    def _send(self, msg):
        if not isinstance(msg, dict):
            return 422, {'ErrorCode': 402, 'Message': 'Received invalid JSON input.'}
        unknown = sorted(set(msg) - FIELDS)
        if unknown:
            return 422, {'ErrorCode': 403, 'Message': 'Invalid request field(s): ' + ', '.join(f"'{u}'" for u in unknown) + '.'}
        sender = msg.get('From')
        m = ADDRESS.match(sender or '')
        if not m:
            return 422, {'ErrorCode': 300, 'Message': f"Invalid 'From' address: '{sender}'."}
        if m.group(2).lower() not in self.sender_signatures:
            return 422, {'ErrorCode': 400, 'Message': f"The 'From' address you supplied ({m.group(2)}) is not a Sender "
                                                       "Signature on your account. Please add and confirm this address in "
                                                       "order to be able to use it in the 'From' field of your messages."}
        recipients = []
        for field in ('To', 'Cc', 'Bcc'):
            value = msg.get(field)
            if value is not None and not isinstance(value, str):
                return 422, {'ErrorCode': 300, 'Message': f"Invalid '{field}' address."}
            for a in _addresses(value):
                am = ADDRESS.match(a)
                if not am:
                    return 422, {'ErrorCode': 300, 'Message': f"Invalid '{field}' address: '{a.strip()}'."}
                recipients.append(am.group(2).lower())
        if not _addresses(msg.get('To')):
            return 422, {'ErrorCode': 300, 'Message': 'Zero recipients specified'}
        if len(recipients) > 50:
            return 422, {'ErrorCode': 300, 'Message': 'Too many recipients. A message can have at most 50 recipients.'}
        if not msg.get('HtmlBody') and not msg.get('TextBody'):
            return 422, {'ErrorCode': 300, 'Message': 'Provide either email TextBody or HtmlBody or both.'}
        if msg.get('ReplyTo') and not all(ADDRESS.match(a) for a in _addresses(msg['ReplyTo'])):
            return 422, {'ErrorCode': 300, 'Message': f"Invalid 'ReplyTo' address: '{msg['ReplyTo']}'."}
        stream = msg.get('MessageStream', 'outbound')
        if stream not in self.streams:
            return 422, {'ErrorCode': 1235, 'Message': 'The stream provided does not exist on this server.'}
        if any(r in self.inactive_recipients for r in recipients):
            return 422, {'ErrorCode': 406, 'Message': 'You tried to send to recipient(s) that have been marked as inactive. '
                                                      'Found inactive addresses: ' + ', '.join(r for r in recipients if r in self.inactive_recipients)
                                                      + '. Inactive recipients are ones that have generated a hard bounce, a spam complaint, or a manual suppression.'}
        if 'Metadata' in msg and not (isinstance(msg['Metadata'], dict) and all(isinstance(v, str) for v in msg['Metadata'].values())):
            return 422, {'ErrorCode': 300, 'Message': 'Metadata values must be strings.'}
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'postmark-{len(self.sent)}-{msg.get("To")}-{msg.get("Subject")}'))
        submitted = self.server.clock.now().strftime('%Y-%m-%dT%H:%M:%S.%f0+00:00')
        self.sent.append(dict(msg, MessageID=message_id, SubmittedAt=submitted))
        return 200, {'To': msg['To'], 'SubmittedAt': submitted, 'MessageID': message_id, 'ErrorCode': 0, 'Message': 'OK'}
