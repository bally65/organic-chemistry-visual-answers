#!/usr/bin/env python3
"""
Render a JS-driven answer-key HTML to a clean, paginated PDF using headless Chromium.

Why this exists: the answer-key HTML draws every chemical structure with SmilesDrawer
(a <canvas>), typesets formulas with MathJax, and builds the layout with JS. A naive
HTML->PDF tool that doesn't execute JS produces a blank document. We use Playwright's
Chromium, WAIT until all canvases + MathJax have finished, then export.

Critical trick: Chromium's page.pdf() silently drops live <canvas> bitmaps. So before
exporting we bake every canvas into a PNG <img> (canvas.toDataURL) — images always print.

Usage:
    python make_pdf.py <input.html> <output.pdf>

First-time setup (if Chromium isn't installed yet):
    python3 -m pip install --user playwright
    python3 -m playwright install chromium
"""
import sys, pathlib, re

def main():
    if len(sys.argv) < 3:
        print("usage: python make_pdf.py <input.html> <output.pdf>"); sys.exit(1)
    src = pathlib.Path(sys.argv[1]).resolve()
    out = str(pathlib.Path(sys.argv[2]).resolve())
    from playwright.sync_api import sync_playwright

    # Print-only CSS: shrink the baked structure images a touch so ~2 reaction cards
    # fit per A4 page (less wasted whitespace), and tighten card chrome.
    density = """
      .mol img { max-width: 210px !important; }
      .rxn { padding: 9px 12px 7px !important; }
      .arrow { padding: 2px 0 !important; }
      .arrow .bar { height: 16px !important; }
      .prodname { margin-top: 5px !important; padding-top: 4px !important; }
      .rxns { gap: 11px !important; }
    """

    with sync_playwright() as p:
        b = p.chromium.launch()
        # device_scale_factor=2 => crisp (Retina-resolution) structure bitmaps in the PDF
        ctx = b.new_context(device_scale_factor=2, viewport={"width": 840, "height": 1180})
        page = ctx.new_page()
        page.goto(src.as_uri(), wait_until="networkidle", timeout=60000)

        # Wait until structures are drawn (no fallback .cap text) and MathJax has typeset.
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('canvas').length===0 "
                "|| (document.querySelectorAll('.smfail').length===0 "
                "    && [...document.querySelectorAll('canvas')].every(c=>{try{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;for(let i=3;i<d.length;i+=4){if(d[i])return true;}return false;}catch(e){return true;}}))",
                timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # Bake every canvas -> <img> so the structures actually appear in the PDF.
        stats = page.evaluate("""() => {
            let drawn=0, conv=0;
            document.querySelectorAll('canvas').forEach(cv => {
                try { const d=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
                      for(let i=3;i<d.length;i+=4){ if(d[i]){ drawn++; break; } } } catch(e){}
                try { const u=cv.toDataURL('image/png');
                      const im=document.createElement('img'); im.src=u; im.className=cv.className;
                      im.style.width=(cv.clientWidth||200)+'px'; im.style.maxWidth='100%'; im.style.height='auto';
                      im.setAttribute('data-baked','1'); cv.replaceWith(im); conv++; } catch(e){}
            });
            return {drawn, conv};
        }""")
        print("canvases drawn/converted:", stats)

        page.add_style_tag(content=density)
        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('img[data-baked]')).every(i=>i.complete&&i.naturalWidth>0)",
                timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(400)

        page.pdf(path=out, format="A4", print_background=True,
                 margin={"top": "11mm", "bottom": "11mm", "left": "10mm", "right": "10mm"})
        b.close()

    data = open(out, "rb").read()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    print(f"PDF written: {out}  ({len(data)//1024} KB, ~{pages} pages)")

if __name__ == "__main__":
    main()
