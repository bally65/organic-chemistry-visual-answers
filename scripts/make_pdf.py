#!/usr/bin/env python3
"""
Render a JS-driven answer-key HTML to a clean, paginated PDF using headless Chromium.

Why this exists: the answer-key HTML draws every chemical structure with SmilesDrawer
(a <canvas>), typesets formulas with MathJax, and builds the layout with JS. A naive
HTML->PDF tool that doesn't execute JS produces a blank document. We use Playwright's
Chromium, WAIT until all canvases + MathJax have finished, then export.

Canvases are baked into PNG <img>s (canvas.toDataURL) before exporting. Some Chromium builds
drop live <canvas> bitmaps from page.pdf() (Chromium 141 does not), and the print-density CSS
below targets the baked <img>, so baking is kept: it makes the output identical across builds.

Fails loudly: a PDF that is missing its structures/formulas looks fine at a glance, so the
script verifies the render before it keeps the file — libraries actually loaded, no JS errors,
no failed script/stylesheet/image requests, structures actually drawn (and none blank, none
.smfail), every MathJax expression typeset with no undefined macros or MathJax errors, and the
baked images present in the PDF. If any check fails it prints why, writes NO pdf and exits 2.
(It also opens collapsed <details> so hidden answers are printed, and warns when a structure
touches its canvas edge — a clipped label such as 'OH' printed as 'OI'.)

Usage:
    python make_pdf.py <input.html> <output.pdf> [--lenient] [--chromium PATH]

    --lenient        write the PDF even though checks failed (to inspect a broken page); exit 3
    --chromium PATH  use this Chromium/Chrome binary instead of Playwright's own build

Exit codes:
    0  ok
    1  setup problem: bad arguments/paths, no playwright, no usable browser
    2  render checks failed — no PDF written (an older PDF at that path is stale)
    3  --lenient only: PDF written although checks failed — do not deliver it

First-time setup (if Playwright isn't installed yet):
    python3 -m pip install --user playwright pymupdf   # pymupdf: lets the script check the PDF itself
    python3 -m playwright install chromium             # skip if a Chromium is already installed
                                                       # (e.g. PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers):
                                                       # a version mismatch is handled automatically below.
"""
import argparse, atexit, glob, os, pathlib, re, shutil, sys

EXIT_OK, EXIT_SETUP, EXIT_CHECKS, EXIT_LENIENT = 0, 1, 2, 3

CANVAS_TIMEOUT_MS = 20000    # SmilesDrawer draws synchronously: ~160 structures take a few seconds, not 20
SCRIPT_TIMEOUT_MS = 10000    # the 'load' event already waited for the CDN scripts; this only covers init
MATH_SETTLE_MS = 4000        # how long the page gets to typeset its own math before we call it a bug
QUIET_POLL_MS, QUIET_ROUNDS, QUIET_MAX_MS = 250, 3, 5000

# Print-only CSS: shrink the baked structure images a touch so ~2 reaction cards
# fit per A4 page (less wasted whitespace), and tighten card chrome.
DENSITY_CSS = """
  .mol img { max-width: 210px !important; }
  .rxn { padding: 9px 12px 7px !important; }
  .arrow { padding: 2px 0 !important; }
  .arrow .bar { height: 16px !important; }
  .prodname { margin-top: 5px !important; padding-top: 4px !important; }
  .rxns { gap: 11px !important; }
"""

# --- page-side snippets (raw strings: the backslashes are JavaScript's) ----------------------
JS_HAS_PIXELS = r"""c => { try { const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
    for (let i=3;i<d.length;i+=4) { if (d[i]) return true; } return false; } catch(e) { return null; } }"""

# Does any drawn pixel sit on the outermost 1px border of the canvas? (SmilesDrawer scales to fit using
# atom positions + padding; a terminal label such as the H of OH can overhang that and get cut off.)
JS_TOUCHES_EDGE = r"""c => { try { const w=c.width, h=c.height, d=c.getContext('2d').getImageData(0,0,w,h).data;
    for (let x=0;x<w;x++) { if (d[x*4+3] || d[((h-1)*w+x)*4+3]) return true; }
    for (let y=0;y<h;y++) { if (d[y*w*4+3] || d[(y*w+w-1)*4+3]) return true; }
    return false; } catch(e) { return false; } }"""

# Judge by behaviour, not file name: a vendored copy may not have 'mathjax' / 'smiles-drawer' in its URL.
JS_SCRIPT_FLAGS = r"""() => {
    const src = [...document.scripts].map(s => s.src || '');
    return {
        smilesScript: src.some(s => /smiles-?drawer/i.test(s)),
        mathScript: !!window.MathJax || src.some(s => /mathjax|tex-(mml|chtml|svg)|mml-(chtml|svg)/i.test(s)),
    };
}"""

# every remaining canvas has pixels (a canvas that failed to parse is replaced by a .smfail div, so it
# leaves this set — .smfail is judged afterwards, not waited on).
JS_CANVASES_DRAWN = r"() => [...document.querySelectorAll('canvas')].every(c => (__PX__)(c) !== false)" \
    .replace("__PX__", JS_HAS_PIXELS)

JS_COUNTS = r"""() => [document.querySelectorAll('canvas').length, document.querySelectorAll('.smfail').length,
    document.querySelectorAll('mjx-container').length, document.images.length].join(',')"""

JS_MATH_READY = r"() => !!(window.MathJax && typeof MathJax.typesetPromise === 'function')"

# Math that MathJax was configured to process but that is still sitting in the page as text. Uses the
# page's own MathJax config (delimiters, escapes, skipped tags), so currency '$' on a page that does not
# use '$' as a delimiter, or TeX shown inside <code>/<pre>, is not flagged.
JS_MATH_RAW = r"""() => {
    const M = window.MathJax;
    if (!M) return [];
    const cfg = (M.config && M.config.tex) || {};
    // the page's configured delimiters, plus the ones an author is likely to have written anyway: if MathJax
    // is not configured for them (e.g. \( \) on a '$'-only page) they stay as literal text — a wrong-delimiter bug
    const pairs = (cfg.displayMath || [['$$', '$$'], ['\\[', '\\]']]).concat(cfg.inlineMath || [['\\(', '\\)']],
        [['$$', '$$'], ['\\[', '\\]'], ['\\(', '\\)']]);
    const skip = (((M.config && M.config.options && M.config.options.skipHtmlTags) ||
        ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'annotation', 'annotation-xml']).join(',')) +
        ',mjx-container,.tex2jax_ignore';
    const out = [], tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n; (n = tw.nextNode()); ) {
        const p = n.parentElement;
        if (!p || p.closest(skip)) continue;
        let t = n.nodeValue;
        if (cfg.processEscapes !== false) t = t.replace(/\\\$/g, '  ');          // \$ is a literal dollar
        for (const [o, c] of pairs) {
            const i = t.indexOf(o);
            if (i >= 0 && t.indexOf(c, i + o.length) > i) { out.push(t.trim().slice(0, 60)); break; }
        }
    }
    return out;
}"""

# The template's MathJax config sets startup.typeset=false and the page typesets itself after building the
# DOM; give that a few seconds. We never typeset on the page's behalf: re-typesetting an already-typeset
# page duplicates/nests every formula, and a page that forgets would show raw TeX in the HTML too.
JS_MATH_SETTLE = r"""async (limit) => {
    await MathJax.startup.promise;
    const raw = (__RAW__);
    for (let i = 0; i < limit && raw().length; i++) await new Promise(r => setTimeout(r, 200));
}""".replace("__RAW__", JS_MATH_RAW)

JS_OPEN_DETAILS = r"""() => { const d = [...document.querySelectorAll('details:not([open])')]; d.forEach(e => e.open = true); return d.length; }"""

JS_REPORT = r"""() => {
    const hasPixels = (__PX__), touchesEdge = (__EDGE__), mathRaw = (__RAW__);
    const canvases = [...document.querySelectorAll('canvas')];
    return {
        canvases: canvases.length,
        blank: canvases.filter(c => hasPixels(c) === false).length,
        edge: canvases.filter(c => touchesEdge(c)).length,
        smfail: [...document.querySelectorAll('.smfail')].map(e => (e.textContent || '').trim().slice(0, 60)),
        smilesLoaded: typeof SmilesDrawer !== 'undefined',
        mathLoaded: !!(window.MathJax && typeof MathJax.typesetPromise === 'function'),
        mjx: document.querySelectorAll('mjx-container').length,
        mjxErr: document.querySelectorAll('mjx-merror').length,
        // MathJax's noundefined extension renders an unknown macro as red literal text, not as an error node
        badMacro: [...document.querySelectorAll('mjx-container')].filter(c => /\\[a-zA-Z]+/.test(c.innerText || '')).map(c => (c.innerText || '').trim().slice(0, 40)),
        rawMath: mathRaw().slice(0, 5),
        textLen: (document.body.innerText || '').trim().length,
        images: document.images.length,
    };
}""".replace("__PX__", JS_HAS_PIXELS).replace("__EDGE__", JS_TOUCHES_EDGE).replace("__RAW__", JS_MATH_RAW)

# Bake every canvas -> <img> so the structures print identically on every Chromium build.
JS_BAKE = r"""() => {
    let total=0, drawn=0, conv=0;
    document.querySelectorAll('canvas').forEach(cv => {
        total++;
        try { const d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
              for (let i=3;i<d.length;i+=4) { if (d[i]) { drawn++; break; } } } catch(e) {}
        try { const u=cv.toDataURL('image/png');
              const im=document.createElement('img'); im.src=u; im.className=cv.className;
              im.style.width=(cv.clientWidth||200)+'px'; im.style.maxWidth='100%'; im.style.height='auto';
              im.setAttribute('data-baked','1'); cv.replaceWith(im); conv++; } catch(e) {}
    });
    return {total, drawn, conv};
}"""

JS_BAKED_LOADED = r"() => Array.from(document.querySelectorAll('img[data-baked]')).every(i => i.complete && i.naturalWidth > 0)"


class Parser(argparse.ArgumentParser):
    def error(self, message):            # argparse exits 2 by default, which here means "render checks failed"
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(EXIT_SETUP)


class Checks:
    """Collects problems; errors fail the run (unless --lenient), warnings never do."""
    def __init__(self):
        self.errors, self.warnings = [], []
    def error(self, msg):
        if msg not in self.errors: self.errors.append(msg)
    def warn(self, msg):
        if msg not in self.warnings: self.warnings.append(msg)


def discover_chromium():
    """Chromium/Chrome binaries already on this machine, best candidate first (Linux layouts).
    Used when Playwright's own build is missing — typically a newer pip `playwright`
    than the pre-installed browser in PLAYWRIGHT_BROWSERS_PATH."""
    roots = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0": roots.append(env)
    roots += ["/opt/pw-browsers", os.path.expanduser("~/.cache/ms-playwright")]
    patterns = ["chromium_headless_shell-*/chrome-linux*/headless_shell",
                "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                "chromium-*/chrome-linux*/chrome"]

    def build(path):   # newest build first, judged by the build directory only (not by anything above it)
        m = re.search(r"/chromium(?:_headless_shell)?-(\d+)/", path)
        return int(m.group(1)) if m else 0

    found = []
    for root in roots:
        for pat in patterns:
            found += sorted(glob.glob(os.path.join(root, pat)), key=build, reverse=True)
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        w = shutil.which(name)
        if w: found.append(w)
    seen, out = set(), []
    for f in found:
        r = os.path.realpath(f)
        if r not in seen and os.access(r, os.X_OK):
            seen.add(r); out.append(f)
    return out


def launch_browser(p, explicit):
    """Return (browser, note). Prefers Playwright's own build; falls back to any installed Chromium."""
    if explicit:
        return p.chromium.launch(executable_path=explicit), f"using --chromium {explicit}"
    try:
        return p.chromium.launch(), None
    except Exception as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
        if "Executable doesn't exist" not in str(e):
            raise
    candidates, failures = discover_chromium(), []
    for path in candidates:
        try:
            return p.chromium.launch(executable_path=path), \
                   f"Playwright's own Chromium build is missing ({first}); using installed browser {path}"
        except Exception as e:
            failures.append(f"  {path}: {str(e).strip().splitlines()[0] if str(e).strip() else repr(e)}")
    if candidates:
        raise RuntimeError(f"{first}\nInstalled browsers were found but none would start:\n" + "\n".join(failures))
    raise RuntimeError(f"{first}\nNo Chromium found. Run `python3 -m playwright install chromium`, "
                       "or pass --chromium /path/to/chrome.")


def hint_for(messages):
    # only script/stylesheet failures say anything about the CDN; a failed fetch() or <img> is the page's own
    blob = " ".join(m for m in messages if not m.startswith(("fetch:", "xhr:", "image:", "document:")))
    if "ERR_CERT" in blob:
        return ("Chromium does not trust the certificate it was served. In the Claude Code cloud sandbox "
                "keep HTTPS_PROXY set (Chromium picks up the proxy + CA from the environment); don't unset it.")
    if re.search(r"ERR_BLOCKED_BY_ORB|HTTP 404", blob):
        return "A script/stylesheet URL answered with an error page (HTTP 404): check the URL and the version in it."
    if re.search(r"ERR_(NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|TUNNEL|CONNECTION|TIMED_OUT|PROXY)|HTTP 40[37]", blob):
        proxy = "" if (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")) else " HTTPS_PROXY is not set here."
        return ("The page loads SmilesDrawer/MathJax from cdn.jsdelivr.net and Chromium couldn't reach it. "
                "Check outbound network access to that host (or vendor the two .js files next to the HTML)." + proxy)
    return None


def count_pdf(path):
    """(pages, images) via PyMuPDF if installed, else (regex page count, None)."""
    try:
        try:
            import pymupdf as fitz          # current name; `import fitz` prints a deprecation notice
        except ImportError:
            import fitz
        doc = fitz.open(path)
        return len(doc), sum(len(pg.get_images(full=True)) for pg in doc)
    except ImportError:
        data = open(path, "rb").read()
        return len(re.findall(rb"/Type\s*/Page[^s]", data)), None


def wait_quiet(page):
    """Wait until the number of canvases / .smfail / formulas / images stops changing, so a sheet that is
    still building (async data, timers) isn't judged half-built."""
    last, stable, waited = None, 0, 0
    while waited < QUIET_MAX_MS and stable < QUIET_ROUNDS:
        cur = page.evaluate(JS_COUNTS)
        stable = stable + 1 if cur == last else 0
        last = cur
        page.wait_for_timeout(QUIET_POLL_MS)
        waited += QUIET_POLL_MS


def main():
    for s in (sys.stdout, sys.stderr):      # CJK file names must not crash the final print on a non-UTF-8 pipe
        if hasattr(s, "reconfigure"): s.reconfigure(errors="backslashreplace")
    ap = Parser(description="Render an answer-key HTML to a verified PDF.")
    ap.add_argument("input"); ap.add_argument("output")
    ap.add_argument("--lenient", action="store_true", help="write the PDF even though render checks failed (exit 3)")
    ap.add_argument("--chromium", metavar="PATH", help="Chromium/Chrome binary to use")
    args = ap.parse_args()

    src = pathlib.Path(args.input).resolve()
    if not src.is_file():
        print(f"error: input not found: {src}", file=sys.stderr); return EXIT_SETUP
    out = pathlib.Path(args.output).resolve()
    if out.is_dir():
        print(f"error: output must be a file name, not a directory: {out}", file=sys.stderr); return EXIT_SETUP
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create output folder {out.parent}: {e}", file=sys.stderr); return EXIT_SETUP
    partial = out.with_name(out.name + ".partial")

    def drop_partial():
        try: partial.unlink()
        except OSError: pass
    atexit.register(drop_partial)           # never leave a half-written PDF behind

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: Python Playwright is not installed. Run:\n"
              "    python3 -m pip install --user playwright", file=sys.stderr)
        return EXIT_SETUP

    ck = Checks()
    net_msgs = []

    def classify(kind, url, detail):
        # A missing script/style/document/XHR/image breaks the page; a missing web font or an optional
        # asset (favicon, ...) only degrades it. Google-Fonts stylesheets fall back to the system fonts.
        msg = f"{kind}: {url} ({detail})"
        net_msgs.append(msg)
        cosmetic = kind == "stylesheet" and re.search(r"fonts\.(googleapis|gstatic)\.com", url)
        if kind in ("script", "stylesheet", "document", "xhr", "fetch", "image") \
                and "ERR_ABORTED" not in detail and not cosmetic:
            ck.error(f"request failed — {msg}")
        else:
            ck.warn(f"request failed — {msg}")

    with sync_playwright() as p:
        try:
            browser, note = launch_browser(p, args.chromium)
        except Exception as e:
            print(f"error: could not start Chromium:\n{e}", file=sys.stderr); return EXIT_SETUP
        if note: print("note:", note)

        # device_scale_factor=2 => crisp (Retina-resolution) structure bitmaps in the PDF
        ctx = browser.new_context(device_scale_factor=2, viewport={"width": 840, "height": 1180})
        page = ctx.new_page()

        page.on("pageerror", lambda err: ck.error(f"JS error in page: {err}"))
        def on_requestfailed(req):
            f = req.failure
            classify(req.resource_type, req.url, f if isinstance(f, str) else (f or {}).get("errorText", "failed"))
        page.on("requestfailed", on_requestfailed)
        def on_response(resp):
            if resp.url.startswith("http") and resp.status >= 400:
                classify(resp.request.resource_type, resp.url, f"HTTP {resp.status}")
        page.on("response", on_response)

        try:
            page.goto(src.as_uri(), wait_until="load", timeout=60000)
        except Exception as e:
            ck.error(f"page did not finish loading: {str(e).splitlines()[0]}")
        try:   # best effort only: a heartbeat / long-poll request must not fail an otherwise fine page
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            ck.warn("network never went idle within 15s (a page that keeps a connection open?); continuing")

        flags = page.evaluate(JS_SCRIPT_FLAGS)

        # --- MathJax: library ready -> DOM stops changing -> the page typesets its own math --------
        math_ok = True
        if flags["mathScript"]:
            try:
                page.wait_for_function(JS_MATH_READY, timeout=SCRIPT_TIMEOUT_MS)
            except Exception as e:
                math_ok = False
                ck.error(f"MathJax did not load: {str(e).splitlines()[0]}")
        wait_quiet(page)
        if flags["mathScript"] and math_ok:
            try:
                page.evaluate(JS_MATH_SETTLE, MATH_SETTLE_MS // 200)
            except Exception as e:
                math_ok = False
                ck.error(f"MathJax failed to start up: {str(e).splitlines()[0]}")

        # --- SmilesDrawer: wait until every canvas has pixels -------------------------------------
        # a page that already threw will not finish drawing: don't sit out the full timeout
        draw_ms = CANVAS_TIMEOUT_MS if not any(e.startswith("JS error") for e in ck.errors) else 5000
        try:
            page.wait_for_function(JS_CANVASES_DRAWN, timeout=draw_ms)
        except Exception:
            ck.error(f"some structure canvases were still blank after {draw_ms // 1000}s")

        opened = page.evaluate(JS_OPEN_DETAILS)    # collapsed <details> would be missing from the print
        if opened: print(f"note: opened {opened} collapsed <details> block(s) so their content is printed")
        page.wait_for_timeout(300)

        # --- verify what actually rendered ---------------------------------------------------
        rep = page.evaluate(JS_REPORT)
        smiles_expected = flags["smilesScript"] or rep["canvases"] > 0 or bool(rep["smfail"])
        if smiles_expected and not rep["smilesLoaded"]:
            ck.error("page uses SmilesDrawer but the library did not load (SmilesDrawer is undefined)")
        if flags["smilesScript"] and rep["smilesLoaded"] and rep["canvases"] == 0 and not rep["smfail"]:
            ck.error("SmilesDrawer is loaded but the page drew no structures (0 canvases) — built too late, or "
                     "this sheet has none: then remove the SmilesDrawer <script> and the engine calls that need it")
        if rep["blank"]:
            ck.error(f"{rep['blank']} of {rep['canvases']} structure canvases are blank")
        if rep["smfail"]:
            ck.error(f"{len(rep['smfail'])} SMILES failed to parse/draw (.smfail): {rep['smfail'][:5]}")
        if rep["edge"]:
            ck.warn(f"{rep['edge']} of {rep['canvases']} structure canvas(es) touch the canvas edge — a label may be "
                    "clipped (e.g. the H of 'OH' printed as 'OI'); raise the drawer padding in the template")
        if rep["mjxErr"]:
            ck.error(f"{rep['mjxErr']} MathJax error element(s) (mjx-merror) — malformed TeX")
        if rep["badMacro"]:
            ck.error(f"MathJax rendered an undefined macro as literal text: {rep['badMacro'][:5]}")
        if rep["rawMath"]:
            ck.error(f"math was never typeset, e.g. {rep['rawMath']} — the delimiters must match the page's MathJax "
                     "config (template: $…$ and $$…$$, not \\(…\\)), and with startup.typeset=false the page must call "
                     "typesetMath() / MathJax.typesetPromise() after building the DOM "
                     "(text inside <code>/<pre> is never typeset)")
        if rep["textLen"] == 0 and rep["canvases"] == 0 and rep["images"] == 0:
            ck.error("the page rendered no visible content")
        print(f"render check: canvases={rep['canvases']} blank={rep['blank']} smfail={len(rep['smfail'])} "
              f"mjx-containers={rep['mjx']} smilesdrawer={'ok' if rep['smilesLoaded'] else 'absent'} "
              f"mathjax={'ok' if rep['mathLoaded'] else 'absent'}")

        # --- bake canvases -> <img>, add print density, export ------------------------------
        stats = page.evaluate(JS_BAKE)
        print(f"canvases drawn/converted: {stats['drawn']}/{stats['conv']} (of {stats['total']})")
        if stats["conv"] < stats["total"]:
            ck.error(f"{stats['total'] - stats['conv']} canvas(es) could not be baked to an image (tainted canvas?)")

        page.add_style_tag(content=DENSITY_CSS)
        try:
            page.wait_for_function(JS_BAKED_LOADED, timeout=20000)
        except Exception:
            ck.error("baked structure images did not finish loading")
        page.wait_for_timeout(400)

        page.pdf(path=str(partial), format="A4", print_background=True,
                 margin={"top": "11mm", "bottom": "11mm", "left": "10mm", "right": "10mm"})
        browser.close()

    # --- verify the PDF itself ---------------------------------------------------------------
    pages, images = count_pdf(partial)
    if images is not None and stats["conv"] > 0 and images == 0:
        ck.error("the PDF contains no images although structures were baked — structures are missing from the PDF")
    if images is None:
        ck.warn("PyMuPDF not installed (pip install --user pymupdf): could not check images inside the PDF")

    for w in ck.warnings: print("warning:", w, file=sys.stderr)
    failed = bool(ck.errors) and not args.lenient
    for e in ck.errors: print(("ERROR: " if failed else "warning (--lenient): ") + e, file=sys.stderr)
    hint = hint_for(net_msgs)
    if hint and ck.errors: print("hint:", hint, file=sys.stderr)

    if failed:
        drop_partial()
        print(f"FAILED: {len(ck.errors)} render check(s) failed — no PDF written."
              + (f" An existing {out} from an earlier run was left untouched: it is STALE, do not deliver it."
                 if out.exists() else ""), file=sys.stderr)
        return EXIT_CHECKS

    os.replace(partial, out)
    size_kb = out.stat().st_size // 1024
    extra = f", {images} image(s)" if images is not None else ""
    print(f"PDF written: {out}  ({size_kb} KB, {pages} pages{extra})")
    if ck.errors:   # only reachable with --lenient
        print(f"WARNING: written although {len(ck.errors)} render check(s) failed — do not deliver this PDF.",
              file=sys.stderr)
        return EXIT_LENIENT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
