"""Tiny Pliqra decision-webhook reference. Replace this demonstration phrase rule with your validator."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        status, result = 200, {}
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.path != '/check' or not 0 < size <= 1_000_000:
                raise ValueError()
            request = json.loads(self.rfile.read(size))
            if request['protocol'] != 'pliqra/guardrail-check/1' or request['stage'] not in ('input', 'output'):
                raise ValueError()
            content = json.dumps(request['messages'], ensure_ascii=False).casefold()
            terms = [v.strip().casefold() for v in os.environ.get('BLOCKED_PHRASES', 'synthetic restricted phrase').split(',') if v.strip()]
            result = {'allow': not any(term in content for term in terms)}
        except (ValueError, KeyError, TypeError):
            status, result = 400, {'error': 'Invalid check request'}
        payload = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8100)
    args = parser.parse_args()
    ThreadingHTTPServer(('0.0.0.0', args.port), Handler).serve_forever()
