"""Explicitly synthetic upstream for local integration tests; never started by the product."""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        payload = {"data": [{"id": "quartz-mini"}, {"id": "quartz-reasoning"}]} if self.path == "/v1/models" else {"error": "not found"}
        self.respond(200 if self.path == "/v1/models" else 404, payload)

    def respond(self, status, payload):
        value = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.respond(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        text = "Connection verified. This is a synthetic test endpoint, not a real language model. Your request passed through Pliqra; open its request details to inspect the selected model, routing decision, latency, and reported test usage."
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for word in text.split(" "):
                    chunk = {"id": "synthetic", "model": body["model"], "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(.015)
                self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":18,"completion_tokens":42}}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
        else:
            self.respond(200, {"id": "synthetic", "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 18, "completion_tokens": 42}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
