"""MCP Apps widgets: one self-contained HTML page per widget, served by the
MCP server itself as a ``ui://`` resource.

The plugin review wants embedded pages from the server's own domain, a CSP
that names exactly what the page fetches, and pages that work on desktop and
mobile. A widget here fetches nothing: data arrives as the tool result and
writes go back through ``tools/call``, so its CSP is empty by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MIME = "text/html;profile=mcp-app"

# Put ``<!--nal-bridge-->`` in a widget's <head>: it becomes ``window.nalBridge``,
# one API over both hosts. ChatGPT exposes ``window.openai``; other MCP Apps
# hosts speak JSON-RPC over postMessage (ui/initialize, tools/call,
# ui/message, ui/notifications/tool-result).
#   nalBridge.onData(cb)        cb(structuredContent) now and on every result
#   nalBridge.callTool(n, args) -> Promise<result>
#   nalBridge.say(text)         a follow-up message from the person
#   nalBridge.theme()           "light" | "dark"
BRIDGE_MARKER = "<!--nal-bridge-->"
BRIDGE_JS = """<script>
(function () {
  var oa = window.openai, listeners = [], last = null, seq = 0, pending = {};
  function emit(d) { if (d) { last = d; listeners.forEach(function (cb) { try { cb(d); } catch (e) {} }); } }
  function rpc(method, params) {
    var id = ++seq;
    window.parent.postMessage({ jsonrpc: "2.0", id: id, method: method, params: params || {} }, "*");
    return new Promise(function (res, rej) { pending[id] = { res: res, rej: rej }; });
  }
  window.addEventListener("message", function (ev) {
    var m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id && pending[m.id]) {
      var p = pending[m.id]; delete pending[m.id];
      if (m.error) p.rej(m.error); else p.res(m.result);
    } else if (m.method === "ui/notifications/tool-result") {
      emit((m.params || {}).structuredContent);
    }
  });
  window.addEventListener("openai:set_globals", function () { emit(window.openai && window.openai.toolOutput); });
  if (!oa) rpc("ui/initialize", { appInfo: { name: "nal-widget", version: "1" }, capabilities: {} }).catch(function () {});
  window.nalBridge = {
    onData: function (cb) { listeners.push(cb); var d = (window.openai && window.openai.toolOutput) || last; if (d) cb(d); },
    callTool: function (name, args) {
      if (window.openai && window.openai.callTool) return window.openai.callTool(name, args || {});
      return rpc("tools/call", { name: name, arguments: args || {} });
    },
    say: function (text) {
      if (window.openai && window.openai.sendFollowUpMessage) return window.openai.sendFollowUpMessage({ prompt: text });
      return rpc("ui/message", { role: "user", content: [{ type: "text", text: text }] });
    },
    theme: function () {
      var t = window.openai && window.openai.theme;
      if (t) return t;
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
  };
})();
</script>"""


def csp(
    *,
    connect: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
    frames: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    return {
        "connectDomains": list(connect),
        "resourceDomains": list(resources),
        "frameDomains": list(frames),
    }


def register_widget(
    mcp: Any,
    *,
    uri: str,
    html: Path,
    name: str,
    description: str,
    policy: dict[str, list[str]] | None = None,
    prefers_border: bool = True,
) -> None:
    """Serve ``html`` at ``uri`` with the MCP Apps MIME type and its CSP.

    The page is read on every request, so a content fix at the same URI ships
    without a new plugin version (the review allows backward-compatible
    changes at the same resource URI).
    """
    if not uri.startswith("ui://"):
        raise ValueError("a widget URI starts with ui://")
    if not html.is_file():
        raise FileNotFoundError(html)
    policy = policy or csp()
    meta = {
        "ui": {"csp": policy, "prefersBorder": prefers_border},
        # ChatGPT's spelling of the same policy.
        "openai/widgetCSP": {
            "connect_domains": policy["connectDomains"],
            "resource_domains": policy["resourceDomains"],
        },
        "openai/widgetPrefersBorder": prefers_border,
    }

    @mcp.resource(uri, name=name, description=description, mime_type=MIME, meta=meta)
    def _widget() -> str:
        return render(html)


def render(html: Path) -> str:
    """The page as served: the bridge in place of its marker."""
    page = html.read_text(encoding="utf-8")
    if BRIDGE_MARKER in page:
        page = page.replace(BRIDGE_MARKER, BRIDGE_JS, 1)
    return page
