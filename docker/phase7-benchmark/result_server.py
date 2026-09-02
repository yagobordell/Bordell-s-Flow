from __future__ import annotations

import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


PUBLIC_ROOT = Path(
    os.environ.get("BENCHMARK_PUBLIC_ROOT", "/workspace/public")
).resolve()
PORT = int(os.environ.get("PORT", "8080"))


class ResultHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path

        if request_path == "/health":
            document = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)
            return

        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"result-server client={self.client_address[0]} "
            f"message={format % args}",
            flush=True,
        )


class ResultHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)

    handler = partial(
        ResultHandler,
        directory=str(PUBLIC_ROOT),
    )
    server = ResultHTTPServer(("0.0.0.0", PORT), handler)
    print(
        f"result-server listening=0.0.0.0:{PORT} root={PUBLIC_ROOT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
