#!/usr/bin/env python3
import http.server
import json
import os
import time

TOKEN_DIR = "/var/run/dcv-tokens"
PORT = 8445


class TokenHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
        token = params.get("authenticationToken", "").strip()
        if not token:
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(b'<auth result="no"/>')
            return
        token_file = os.path.join(TOKEN_DIR, token)
        if os.path.isfile(token_file):
            try:
                with open(token_file) as f:
                    data = json.load(f)
                if data.get("expires", 0) > time.time():
                    user = data["user"]
                    response = f'<auth result="yes"><username>{user}</username></auth>'
                    os.remove(token_file)
                else:
                    os.remove(token_file)
                    response = '<auth result="no"/>'
            except Exception:
                response = '<auth result="no"/>'
        else:
            response = '<auth result="no"/>'
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(response.encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    os.makedirs(TOKEN_DIR, mode=0o700, exist_ok=True)
    server = http.server.HTTPServer(("127.0.0.1", PORT), TokenHandler)
    server.serve_forever()
