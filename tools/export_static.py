"""Bake the dashboard into files GitHub Pages can serve.

The engine shells out to git, so it cannot run on a static host. What it can
do is answer every question once, here, and write the answers down. The page
then reads files instead of an API and behaves the same for everything that
does not need a live repository.

This runs the real server in-process and records its responses rather than
reimplementing the dispatch, so the baked JSON cannot drift from what a
running instance would say.

    python3 tools/export_static.py ~/prophecy-demo

Writes docs/index.html and docs/data/*.json.
"""

import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prophecy.server import Handler  # noqa: E402
from prophecy.mcp import Server as McpServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"

# Everything the page asks for with no arguments. Left out on purpose:
# backfill replays hundreds of merges and would take longer than the whole
# export, and the write endpoints have nothing to say on a read-only host.
PLAIN = [
    "risk", "scan", "sessions", "people", "usage", "sharing", "history",
    "status", "insights", "fleet", "worktree", "setup", "mcp_config",
    "repos", "sample", "messages",
]


def key(cmd, params=None):
    """The filename a request maps to. Mirrored exactly in dashboard.html."""
    parts = []
    for k, v in sorted((params or {}).items()):
        if v in (None, "", []):
            continue
        parts.append(f"{k}-{'_'.join(v) if isinstance(v, list) else v}")
    name = f"{cmd}__{'__'.join(parts)}" if parts else cmd
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def main(repo):
    repo = str(Path(repo).expanduser().resolve())
    DATA.mkdir(parents=True, exist_ok=True)
    for stale in DATA.glob("*.json"):
        stale.unlink()

    # port 0: let the OS pick one, so this never collides with a dashboard
    # the person already has running
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, repo=repo))
    httpd.daemon_threads = True
    httpd.mcp = McpServer(repo)
    httpd.mcp_session = "static-export"
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def grab(cmd, params=None):
        query = f"repo={urllib.parse.quote(repo)}"
        for k, v in (params or {}).items():
            for one in (v if isinstance(v, list) else [v]):
                query += f"&{k}={urllib.parse.quote(str(one))}"
        url = f"http://127.0.0.1:{port}/api/{cmd}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                body = r.read()
        except (urllib.error.URLError, socket.timeout) as exc:
            print(f"  skipped {cmd} ({exc})")
            return None
        (DATA / f"{key(cmd, params)}.json").write_bytes(body)
        return json.loads(body)

    print(f"exporting {repo}")
    for cmd in PLAIN:
        grab(cmd)
        print(f"  {cmd}")

    # One per branch, so clicking a change on the Risk tab still opens.
    risk = json.loads((DATA / "risk.json").read_text())
    for change in risk.get("changes", []):
        label = change.get("label", "")
        target = label.replace("branch ", "")
        if target:
            grab("analyze", {"target": target})
    print(f"  analyze x{len(risk.get('changes', []))}")

    # One per file, so clicking a node on the Map still opens.
    tracked = subprocess.run(
        ["git", "-C", repo, "ls-files"],
        capture_output=True, text=True).stdout.split()
    for path in tracked:
        grab("file", {"path": path})
    print(f"  file x{len(tracked)}")

    httpd.shutdown()

    page = (ROOT / "prophecy" / "dashboard.html").read_text()
    # The page is identical to the live one apart from knowing it is static
    # and which project to open, both of which it reads off window. There is
    # no </head> to hang this on, so it goes before the first script, which
    # is the first thing that could read it.
    boot = (
        "<script>window.PROPHECY_STATIC = true;"
        f"window.PROPHECY_REPO = {json.dumps(repo)};</script>\n"
    )
    # After the doctype, not before the first <script>: the first script tag
    # in this file lives inside a hidden <textarea> holding the MCP demo page
    # as text, so anything injected there is content rather than code.
    doctype = "<!doctype html>\n"
    assert page.startswith(doctype), "dashboard.html does not start with a doctype"
    page = doctype + boot + page[len(doctype):]
    # Pages serves this from /hackMIT/, where an absolute asset path is a 404
    page = page.replace('="/logo.png"', '="logo.png"')
    (DOCS / "index.html").write_text(page)
    for asset in ("logo.png",):
        src = ROOT / "prophecy" / asset
        if src.exists():
            shutil.copy(src, DOCS / asset)

    size = sum(f.stat().st_size for f in DATA.glob("*.json"))
    print(f"\ndocs/index.html + {len(list(DATA.glob('*.json')))} files "
          f"({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
