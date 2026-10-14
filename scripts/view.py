"""Open the trace viewer in your web browser.

Usage (from the repository folder, on Windows, macOS or Linux):

    python scripts/view.py            # serve docs/ and open the browser
    python scripts/view.py --port 8080

The viewer loads JSON trace files with fetch(), which browsers block when a
page is opened straight from disk (file://). This script starts Python's
built-in web server on your own computer (localhost only) and opens the page.
Press Ctrl+C in the terminal to stop it.

Author: Farzana Akhter
"""

import argparse
import functools
import http.server
import threading
import webbrowser
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


def make_server(handler, preferred: int) -> http.server.ThreadingHTTPServer:
    """Bind to the preferred port on localhost; if it is busy, let the OS pick a free one."""
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", preferred), handler)
    except OSError:
        print(f"Port {preferred} is busy; using a free port instead.")
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve docs/ and open the viewer.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    if not (DOCS / "traces" / "index.json").exists():
        raise SystemExit("docs/traces/index.json is missing. Run the run-all command first.")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DOCS))
    server = make_server(handler, args.port)
    port = server.server_address[1]
    url = f"http://localhost:{port}/"
    print(f"Viewer running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
