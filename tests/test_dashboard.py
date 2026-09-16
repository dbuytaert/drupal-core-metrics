"""The dashboard renders in a real browser: every section gets its cards and no error
replaces them. Charts have no per-chart error isolation, so one runtime error anywhere
blanks every chart; a leftover variable reference once did exactly that while every other
test passed. Chrome ships on GitHub's Ubuntu runners, so this needs nothing installed.
"""
import functools
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).parent.parent
CHROME = next((path for path in (os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
                                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
               if path and Path(path).exists()), None)
SECTIONS = ["activity", "people", "security", "codebase", "api", "quality", "performance"]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *arguments):
        pass


def rendered(directory: Path) -> str:
    """The page's DOM once its data has loaded and every chart is built, without the page's
    own scripts, whose source text would otherwise pass for rendered markup."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(directory)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dom = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-sandbox", "--virtual-time-budget=20000",
             "--dump-dom", f"http://127.0.0.1:{server.server_address[1]}/index.html"],
            capture_output=True, text=True, timeout=120, check=True).stdout
    finally:
        server.shutdown()
        server.server_close()
    return re.sub(r"<script\b[^>]*>.*?</script>", "", dom, flags=re.DOTALL)


# Skipped on a machine without a browser, never in CI, where a silent skip would pass
# without testing anything.
@unittest.skipUnless(CHROME or os.environ.get("CI"), "needs Chrome or Chromium")
class DashboardRendersTest(unittest.TestCase):

    def setUp(self):
        self.assertTrue(CHROME, "CI needs Chrome or Chromium to render the dashboard")

    def test_every_section_renders_its_cards(self):
        dom = rendered(REPOSITORY)
        self.assertNotIn('class="error"', dom)
        sections = dom.split('<div class="section-header" id="')[1:]
        self.assertEqual([section.split('"', 1)[0] for section in sections], SECTIONS)
        for section in sections:
            self.assertIn('class="card"', section, section.split('"', 1)[0])

    def test_a_runtime_error_in_one_chart_blanks_the_whole_dashboard(self):
        # The check above has to be able to fail: break one chart and the page must show
        # its error instead of the dashboard.
        with tempfile.TemporaryDirectory() as directory:
            for name in ("index.html", "chart-rules.js", "data.json"):
                shutil.copy(REPOSITORY / name, directory)
            page = Path(directory, "index.html")
            chart = "function createLcom4Chart(container, data) {"
            source = page.read_text()
            self.assertEqual(source.count(chart), 1)
            page.write_text(source.replace(chart, chart + " undefinedReference;"))
            dom = rendered(Path(directory))
        self.assertIn('class="error"', dom)
        self.assertNotIn('class="section-header"', dom)


if __name__ == "__main__":
    unittest.main()
