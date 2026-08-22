# extract_geiger_standalone.py
# Usage:  python extract_geiger_standalone.py [source.html] [out.html]
# Default: J:\index400.html -> J:\geiger_standalone.html

from pathlib import Path
import re
import sys

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else r"J:\index400.html")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else r"J:\geiger_standalone.html")

if not SRC.exists():
    print(f"ERROR: missing {SRC}")
    sys.exit(1)

text = SRC.read_text(encoding="utf-8", errors="replace")
print(f"Read {SRC} ({len(text):,} chars, {text.count(chr(10)):,} lines)")

# ── helpers ──────────────────────────────────────────────────────────
def grab_between(s, start_pat, end_pat, flags=re.I | re.S):
    m = re.search(start_pat, s, flags)
    if not m:
        return None, f"START not found: {start_pat[:60]}"
    rest = s[m.start():]
    m2 = re.search(end_pat, rest, flags)
    if not m2:
        return rest, f"END not found after start: {end_pat[:60]} (took to EOF)"
    return rest[: m2.start()], None

def grab_id_block(s, elem_id, tag_hint=None):
    """Grab outermost element with id=elem_id (div/section/aside)."""
    # opening tag with this id
    open_re = re.compile(
        rf'<(?P<tag>div|section|aside|main|article)\b[^>]*\bid=["\']{re.escape(elem_id)}["\'][^>]*>',
        re.I,
    )
    m = open_re.search(s)
    if not m:
        return None
    tag = m.group("tag")
    start = m.start()
    i = m.end()
    depth = 1
    # scan forward for matching close (naive but works for well-formed panels)
    token = re.compile(rf'</?{tag}\b[^>]*>', re.I)
    for t in token.finditer(s, i):
        tok = t.group(0)
        if tok.startswith(f"</"):
            depth -= 1
            if depth == 0:
                return s[start : t.end()]
        elif not tok.endswith("/>"):
            depth += 1
    return s[start:]  # fallback

def extract_style_chunks(s):
    """Pull <style> blocks that mention geiger / gp- / ssig / metaldet."""
    chunks = []
    for m in re.finditer(r"<style\b[^>]*>(.*?)</style>", s, re.I | re.S):
        body = m.group(1)
        if re.search(r"geiger|gp-|ssig|metaldet|#gp-|rba-", body, re.I):
            chunks.append(body)
    return chunks

def extract_script_by_markers(s, markers, window_before=0):
    """
    For each marker, find it in a <script> and take a large slice of that script
    from a stable start anchor to a stable end, or the whole script containing it.
    """
    scripts = list(re.finditer(r"<script\b([^>]*)>(.*?)</script>", s, re.I | re.S))
    out = []
    used = set()
    for i, m in enumerate(scripts):
        attrs, body = m.group(1), m.group(2)
        if "src=" in attrs.lower():
            continue  # external
        hit = any(mk in body for mk in markers)
        if not hit:
            continue
        if i in used:
            continue
        used.add(i)
        out.append(body)
    return out

# ── 1) HTML panels ───────────────────────────────────────────────────
PANEL_IDS = [
    "geiger-feed-panel",
    "gp-canvas",           # may be inside panel
    "source-sig-panel",
    "ssig-panel",
    "gp-md-panel",
]

html_parts = []
for pid in PANEL_IDS:
    block = grab_id_block(text, pid)
    if block:
        print(f"  + panel #{pid} ({len(block):,} chars)")
        html_parts.append(f"<!-- extracted #{pid} -->\n{block}")
    else:
        print(f"  - panel #{pid} NOT FOUND")

# Also grab any standalone canvas wrapper near gp-canvas
if not any("geiger-feed-panel" in (p or "") for p in html_parts):
    # fallback: search for known button markup region
    m = re.search(
        r'(<div[^>]*>[\s\S]{0,500}?id=["\']gp-canvas["\'][\s\S]{0,200000}?</div>\s*</div>)',
        text,
        re.I,
    )
    if m:
        html_parts.insert(0, "<!-- fallback geiger region -->\n" + m.group(1))
        print("  + fallback region around #gp-canvas")

# ── 2) CSS ───────────────────────────────────────────────────────────
css_chunks = extract_style_chunks(text)
print(f"  + style chunks: {len(css_chunks)}")

# Minimal always-on shell CSS
SHELL_CSS = """
html, body {
  margin: 0; padding: 0; height: 100%;
  background: #000; color: #ccc;
  font-family: Consolas, ui-monospace, monospace;
}
#geiger-root { display: flex; flex-direction: column; height: 100vh; }
#geiger-feed-panel, #geiger-root > .panel {
  display: flex !important; flex-direction: column; flex: 1; min-height: 0;
}
#gp-canvas { width: 100%; flex: 1; min-height: 240px; display: block; background: #080808; }
.hidden-panel, .hidden { display: none !important; }
button { font-family: inherit; }
"""

# ── 3) JS: scripts that own geiger logic ─────────────────────────────
MARKERS = [
    "const _gp",
    "function runDetectors",
    "function _startGeigerSSE",
    "function toggleGeigerPanel",
    "function nsToUtcStr",
    "_gp.openReplay",
    "_gp._fastProcessFile",
    "_gp.openFastProcess",
    "function onRecord",
    "METALDET",
    "const SSIG",
    "const RBA",
    "const DS=",
    "const C={",
]

js_bodies = extract_script_by_markers(text, MARKERS)
print(f"  + script bodies: {len(js_bodies)} (total {sum(len(b) for b in js_bodies):,} chars)")

# Deduplicate near-identical
uniq = []
for b in js_bodies:
    key = b[:200] + str(len(b))
    if key not in {u[:200] + str(len(u)) for u in uniq}:
        uniq.append(b)
js_bodies = uniq

# ── 4) Standalone shell ──────────────────────────────────────────────
# Prefer relative WS to same host; allow override via ?ws=
BOOT = r"""
(function () {
  // Standalone boot: show geiger panel, start WS to live server
  function $(id) { return document.getElementById(id); }
  window.addEventListener('DOMContentLoaded', function () {
    var panel = $('geiger-feed-panel');
    if (panel) {
      panel.style.display = 'flex';
      panel.classList.remove('hidden-panel', 'hidden');
    }
    // Optional WS override: geiger_standalone.html?ws=wss://host/api/geiger/ws
    var q = new URLSearchParams(location.search);
    if (q.get('ws')) {
      window.__GEIGER_WS_URL = q.get('ws');
    }
    try {
      if (typeof _gp !== 'undefined' && _gp.init) _gp.init();
    } catch (e) { console.warn('init', e); }
    try {
      if (typeof _startGeigerSSE === 'function') _startGeigerSSE();
      else if (typeof toggleGeigerPanel === 'function') toggleGeigerPanel();
    } catch (e) { console.warn('sse', e); }
    console.info('[standalone] geiger ready — expects geiger_live_server.py');
  });
})();
"""

# Patch common WS builder to honor __GEIGER_WS_URL if present
WS_PATCH = r"""
// Standalone WS URL patch (injected)
(function(){
  var _orig = window._startGeigerSSE;
  if (typeof _orig !== 'function') return;
  // leave original; user server is same-origin or set ?ws=
})();
"""

html_body = "\n\n".join(html_parts) if html_parts else """
<div id="geiger-root">
  <p style="color:#f66;padding:1rem">No geiger panel IDs found in source.
  Check that #geiger-feed-panel exists in the input HTML.</p>
  <canvas id="gp-canvas"></canvas>
</div>
"""

# If panel exists but was hidden in social app, force visible wrapper
if "geiger-feed-panel" in html_body and "geiger-root" not in html_body:
    html_body = f'<div id="geiger-root">\n{html_body}\n</div>'

out_doc = f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#000000"/>
<title>CTW Geiger Scope — Standalone</title>
<style>
{SHELL_CSS}
{"\n\n".join(css_chunks)}
</style>
</head>
<body>
{html_body}

<!-- Extracted application scripts -->
"""

for i, body in enumerate(js_bodies):
    out_doc += f"\n<script>\n/* ---- extracted script block {i+1} ---- */\n{body}\n</script>\n"

out_doc += f"""
<script>
/* ---- standalone boot ---- */
{BOOT}
{WS_PATCH}
</script>
</body>
</html>
"""

OUT.write_text(out_doc, encoding="utf-8")
print(f"\nWrote {OUT}")
print(f"  size: {OUT.stat().st_size:,} bytes")
print(f"  scripts: {len(js_bodies)}")
print(f"  panels:  {len(html_parts)}")
print("""
Next:
  1) Run fs5000_serial.py + geiger_live_server.py as usual
  2) Open geiger_standalone.html via the same origin as the live server
     (or file:// with ?ws=ws://127.0.0.1:PORT/api/geiger/ws if CORS/WS allows)
  3) If panel missing, open extract log: which IDs were NOT FOUND
""")