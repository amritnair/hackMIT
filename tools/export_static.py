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


CODESPACE = "https://codespaces.new/amritnair/hackMIT?quickstart=1"

# Appended to the published page only. The local dashboard has an engine
# behind it and needs none of this.
FULL_VERSION = """
<div id="fullVersion">
  <strong>You are reading recorded results.</strong>
  Editing, commits, your own repositories and MCP need Prophecy running on a
  machine with a checkout.
  Run one and the MCP tab here will connect your own agent to it.
  <code>docker run -p 8000:8000 ghcr.io/amritnair/prophecy</code>
  <a href="%s">Or open it in a browser</a>, free, on GitHub's hours.
  <button type="button" onclick="this.parentNode.remove()"
          aria-label="Dismiss">&times;</button>
</div>
<style>
  #fullVersion {
    position: fixed; right: 16px; bottom: 16px; z-index: 40; max-width: 370px;
    padding: 13px 34px 13px 15px; border: 1px solid var(--line, #22272f);
    border-radius: 10px; background: var(--raised, #11151b);
    color: var(--text-2, #9aa4b2);
    font: 12.5px/1.6 var(--ui, ui-sans-serif, system-ui, sans-serif);
    box-shadow: 0 10px 30px rgba(0, 0, 0, .45);
  }
  #fullVersion strong { color: var(--text, #e8ecf2); font-weight: 600; }
  #fullVersion a { color: var(--accent, #7DBBFF); }
  #fullVersion code {
    display: block; margin-top: 7px; font-size: 11.5px;
    color: var(--text-3, #6f747c); word-break: break-all;
  }
  #fullVersion button {
    position: absolute; top: 7px; right: 9px; border: 0; background: none;
    color: var(--text-3, #6f747c); font-size: 16px; cursor: pointer;
    line-height: 1;
  }
  @media (max-width: 640px) { #fullVersion { display: none; } }
</style>
""" % CODESPACE


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

    # The export ran on an ephemeral port, and handing a reader
    # 127.0.0.1:41337 as their MCP endpoint is worse than saying nothing.
    # What they would actually get from `prophecy serve` is port 8000.
    local = "http://127.0.0.1:8000/mcp"
    (DATA / "mcp_config.json").write_text(json.dumps({
        "config": {"mcpServers": {"prophecy": {"type": "http", "url": local}}},
        "url": local,
        "repo": repo,
    }))

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
    # Half the product needs a checkout and a process. Saying so once, in a
    # corner, beats a reader concluding the editor is broken.
    page += FULL_VERSION
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
