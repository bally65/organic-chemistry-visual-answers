#!/usr/bin/env python3
"""
Render a JS-driven answer-key HTML to a clean, paginated PDF using headless Chromium.

Why this exists: the answer-key HTML draws every chemical structure with SmilesDrawer
(a <canvas>), typesets formulas with MathJax, and builds the layout with JS. A naive
HTML->PDF tool that doesn't execute JS produces a blank document. We use Playwright's
Chromium, WAIT until all canvases + MathJax have finished, then export.

Critical trick: Chromium's page.pdf() silently drops live <canvas> bitmaps. So before
exporting we bake every canvas into a PNG <img> (canvas.toDataURL) — images always print.

Fails loudly: a PDF that is missing its structures/formulas looks fine at a glance, so the
script verifies the render before it keeps the file — libraries actually loaded, no JS
errors, no failed CDN requests, no blank canvases, no .smfail, no raw $\\ce{..}$ left over.
If any check fails it prints why, writes NO pdf, and exits 2.

Usage:
    python make_pdf.py <input.html> <output.pdf> [--lenient] [--chromium PATH]

    --lenient        downgrade failed checks to warnings and write the PDF anyway
    --chromium PATH  use this Chromium/Chrome binary instead of Playwright's own build

Exit codes: 0 ok · 1 setup problem (no playwright / no browser / bad args) · 2 render checks failed

First-time setup (if Playwright isn't installed yet):
    python3 -m pip install --user playwright
    python3 -m playwright install chromium     # skip if a Chromium is already installed
                                               # (e.g. PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers):
                                               # a version mismatch is handled automatically below.
"""
import argparse, atexit, glob, os, pathlib, re, shutil, sys

CANVAS_TIMEOUT_MS = 45000
SCRIPT_TIMEOUT_MS = 10000   # networkidle already waited for the CDN scripts; this only covers init

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

# --- page-side snippets -------------------------------------------------------------------
JS_HAS_PIXELS = """c => { try { const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
    for (let i=3;i<d.length;i+=4) { if (d[i]) return true; } return false; } catch(e) { return null; } }"""

JS_SCRIPT_FLAGS = """() => ({
    smilesScript: !!document.querySelector('script[src*="smiles-drawer"]'),
    mathScript:   !!document.querySelector('script[src*="mathjax"]'),
})"""

# every remaining canvas has pixels (a canvas that failed to parse is replaced by a .smfail div, so it
# leaves this set — .smfail is judged afterwards, not waited on).
JS_CANVASES_DRAWN = "() => [...document.querySelectorAll('canvas')].every(c => (__PX__)(c) !== false)".replace("__PX__", JS_HAS_PIXELS)

JS_MATH_READY = "() => !!(window.MathJax && typeof MathJax.typesetPromise === 'function')"

# Unrendered math: an mhchem \\ce{..} or a $...$ span containing a TeX command, still in the visible text.
RAW_MATH_SRC = r"\\ce\{|\$[^$\n]*\\[a-zA-Z]+[^$\n]*\$"

# The template's MathJax config sets startup.typeset=false and the page typesets itself after building the
# DOM. Give that a few seconds to finish. Do NOT call typesetPromise() unconditionally: re-typesetting an
# already-typeset page duplicates every formula (tested). Only if raw math is still there after the wait
# (the page never typeset) do we typeset once ourselves.
JS_MATH_SETTLE = """async (rawSrc) => {
    await MathJax.startup.promise;
    const raw = () => ((document.body.innerText || '').match(new RegExp(rawSrc, 'g')) || []).length;
    for (let i = 0; i < 40 && raw(); i++) await new Promise(r => setTimeout(r, 200));
    if (raw()) await MathJax.typesetPromise();
    return raw();
}"""

JS_REPORT = """(rawSrc) => {
    const hasPixels = __PX__;
    const canvases = [...document.querySelectorAll('canvas')];
    const text = document.body.innerText || '';
    return {
        canvases: canvases.length,
        blank: canvases.filter(c => hasPixels(c) === false).length,
        smfail: [...document.querySelectorAll('.smfail')].map(e => (e.textContent || '').trim().slice(0, 60)),
        smilesLoaded: typeof SmilesDrawer !== 'undefined',
        mathLoaded: !!(window.MathJax && typeof MathJax.typesetPromise === 'function'),
        mjx: document.querySelectorAll('mjx-container').length,
        mjxErr: document.querySelectorAll('mjx-merror').length,
        rawMath: (text.match(new RegExp(rawSrc, 'g')) || []).slice(0, 5),
    };
}""".replace("__PX__", JS_HAS_PIXELS)

# Bake every canvas -> <img> so the structures actually appear in the PDF.
JS_BAKE = """() => {
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

JS_BAKED_LOADED = "() => Array.from(document.querySelectorAll('img[data-baked]')).every(i => i.complete && i.naturalWidth > 0)"


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
    Used when Playwright's own expected build is missing — typically a newer pip `playwright`
    than the pre-installed browser in PLAYWRIGHT_BROWSERS_PATH."""
    roots = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0": roots.append(env)
    roots += ["/opt/pw-browsers", os.path.expanduser("~/.cache/ms-playwright")]
    patterns = ["chromium_headless_shell-*/chrome-linux*/headless_shell",
                "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                "chromium-*/chrome-linux*/chrome"]
    found = []
    for root in roots:
        for pat in patterns:
            hits = glob.glob(os.path.join(root, pat))
            # newest build first within a pattern
            hits.sort(key=lambda h: int((re.search(r"-(\d+)/", h) or [0, 0])[1] or 0), reverse=True)
            found += hits
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
    for path in discover_chromium():
        try:
            return p.chromium.launch(executable_path=path), \
                   f"Playwright's own Chromium build is missing ({first}); using installed browser {path}"
        except Exception:
            continue
    raise RuntimeError(
        f"{first}\nNo usable Chromium found. Either run `python3 -m playwright install chromium`, "
        "or pass --chromium /path/to/chrome.")


def hint_for(messages):
    blob = " ".join(messages)
    if "ERR_CERT" in blob:
        return ("Chromium does not trust the certificate it was served. In the Claude Code cloud sandbox "
                "keep HTTPS_PROXY set (Chromium picks up the proxy + CA from the environment); don't unset it.")
    if re.search(r"ERR_(NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|TUNNEL|CONNECTION|TIMED_OUT|PROXY)", blob):
        return ("The page loads SmilesDrawer/MathJax from cdn.jsdelivr.net and Chromium couldn't reach it. "
                "Check outbound network access to that host (or vendor the two .js files next to the HTML).")
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


def main():
    ap = argparse.ArgumentParser(description="Render an answer-key HTML to a verified PDF.")
    ap.add_argument("input"); ap.add_argument("output")
    ap.add_argument("--lenient", action="store_true", help="warn instead of failing when render checks fail")
    ap.add_argument("--chromium", metavar="PATH", help="Chromium/Chrome binary to use")
    args = ap.parse_args()

    src = pathlib.Path(args.input).resolve()
    if not src.is_file():
        print(f"error: input not found: {src}", file=sys.stderr); return 1
    out = pathlib.Path(args.output).resolve()
    partial = out.with_name(out.name + ".partial")
    atexit.register(lambda: partial.unlink(missing_ok=True))   # never leave a half-written PDF behind

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: Python Playwright is not installed. Run:\n"
              "    python3 -m pip install --user playwright", file=sys.stderr)
        return 1

    ck = Checks()
    net_msgs = []

    def classify(kind, url, detail):
        # Missing scripts/styles/documents/XHR break the page; a missing font/image degrades it.
        msg = f"{kind}: {url} ({detail})"
        net_msgs.append(msg)
        if kind in ("script", "stylesheet", "document", "xhr", "fetch") and "ERR_ABORTED" not in detail:
            ck.error(f"request failed — {msg}")
        else:
            ck.warn(f"request failed — {msg}")

    with sync_playwright() as p:
        try:
            browser, note = launch_browser(p, args.chromium)
        except Exception as e:
            print(f"error: could not start Chromium:\n{e}", file=sys.stderr); return 1
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
            page.goto(src.as_uri(), wait_until="networkidle", timeout=60000)
        except Exception as e:
            ck.error(f"page did not finish loading: {str(e).splitlines()[0]}")

        flags = page.evaluate(JS_SCRIPT_FLAGS)

        # --- MathJax: wait for the library, then for the typeset to complete -----------------
        if flags["mathScript"]:
            try:
                page.wait_for_function(JS_MATH_READY, timeout=SCRIPT_TIMEOUT_MS)
                page.evaluate(JS_MATH_SETTLE, RAW_MATH_SRC)
            except Exception as e:
                ck.error(f"MathJax did not load/typeset: {str(e).splitlines()[0]}")

        # --- SmilesDrawer: wait until every canvas has pixels --------------------------------
        try:
            page.wait_for_function(JS_CANVASES_DRAWN, timeout=CANVAS_TIMEOUT_MS)
        except Exception:
            ck.error(f"some structure canvases were still blank after {CANVAS_TIMEOUT_MS // 1000}s")
        page.wait_for_timeout(500)

        # --- verify what actually rendered ---------------------------------------------------
        rep = page.evaluate(JS_REPORT, RAW_MATH_SRC)
        if flags["smilesScript"] and not rep["smilesLoaded"]:
            ck.error("SmilesDrawer script tag is present but the library did not load (SmilesDrawer is undefined)")
        if flags["mathScript"] and not rep["mathLoaded"]:
            ck.error("MathJax script tag is present but the library did not load")
        if rep["blank"]:
            ck.error(f"{rep['blank']} of {rep['canvases']} structure canvases are blank")
        if rep["smfail"]:
            ck.error(f"{len(rep['smfail'])} SMILES failed to parse/draw (.smfail): {rep['smfail'][:5]}")
        if rep["mjxErr"]:
            ck.error(f"{rep['mjxErr']} MathJax error element(s) (mjx-merror) — malformed TeX")
        if rep["rawMath"]:
            ck.error(f"un-typeset math left in the page text: {rep['rawMath']}")
        if flags["smilesScript"] and rep["smilesLoaded"] and rep["canvases"] == 0 and not rep["smfail"]:
            ck.warn("SmilesDrawer is loaded but the page drew no structures (0 canvases)")
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

        out.parent.mkdir(parents=True, exist_ok=True)
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
    hint = hint_for(net_msgs + ck.errors)
    if hint and ck.errors: print("hint:", hint, file=sys.stderr)

    if failed:
        partial.unlink(missing_ok=True)
        print(f"FAILED: {len(ck.errors)} render check(s) failed — no PDF written."
              + (f" (An existing {out} from an earlier run was left untouched and is stale.)" if out.exists() else ""),
              file=sys.stderr)
        return 2

    os.replace(partial, out)
    size_kb = out.stat().st_size // 1024
    extra = f", {images} image(s)" if images is not None else ""
    print(f"PDF written: {out}  ({size_kb} KB, {pages} pages{extra})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
