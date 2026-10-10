"""A deliberately altered sign-in page, for §10's layout-adaptation test.

WHY THIS EXISTS

§10 requires: "Test with deliberately altered layouts to assess whether the selected browser agent
adapts without rewriting fixed scripts."

`tools/test_portal.py` renders a tidy, accessible sign-in form:

    <label for="email">Email address</label>
    <input id="email" name="email" type="email" required>

That is the easy case. An agent that reads `label[for]` associations finds the fields; one that
hardcodes `#email` happens to work too, and the test would pass without distinguishing them.

THIS PAGE KEEPS THE SEMANTICS AND REMOVES THE HANDLES:

  * NO `<label for=...>` association - the label is a `<span>`, visually adjacent but structurally
    unrelated to the input
  * NO ids and NO names that describe the field - `f1`, `f2`
  * the visual order is PASSWORD then EMAIL, the reverse of the accessible order
  * the labels are ABOVE a wrapper div rather than the input
  * a decorative `<canvas>` and a repeated header add noise the DOM tree reports but a naive reader
    might act on

An agent that finds these fields is reading the page. One that carries selectors from anywhere else
will fail, and the failure is the measurement.

WHAT WOULD MAKE THIS TEST VACUOUS

If the altered page kept `id="email"`, both a reader and a hardcoded selector would succeed and the
test would prove nothing. The ids are deliberately meaningless so that success can only come from
understanding the page.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Account access</title></head>
<body>
<header class="site-header">
  <canvas id="decoration" width="600" height="40"></canvas>
  <h1>Account access</h1>
</header>
<p class="lede">Sign in to continue your application.</p>

<!-- The visual order is password first, email second. The labels are spans, not <label for>. -->
<form method="post" action="/login" class="stacked">
  <div class="row">
    <span class="caption">Password</span>
    <div class="control"><input id="f2" name="f2" type="password" autocomplete="off"></div>
  </div>
  <div class="row">
    <span class="caption">Email address</span>
    <div class="control"><input id="f1" name="f1" type="text" autocomplete="off"></div>
  </div>
  <div class="row">
    <div class="control"><button type="submit" id="go">Continue</button></div>
  </div>
</form>

<footer><hr><p>Simulated portal. Fictional data only. Not a real funder.</p></footer>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"altered portal on http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
