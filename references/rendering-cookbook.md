# Rendering cookbook — why the engine is built the way it is

Every rule here was learned by hitting the bug. Follow them and structures render crisp,
fill their boxes, and survive PDF export. Ignore them and you'll re-debug blank canvases.

## SmilesDrawer

- **Use v1.2.0, not v2.x.** v2's `draw()` throws `Cannot read properties of null (reading 'determineDimensions')`. v1.2.0's stable canvas-ID API works:
  ```js
  SmilesDrawer.parse(smiles, tree => drawer.draw(tree, canvasElement.id, 'light', false), onError);
  ```
  Note: the draw target is the canvas **id string**, not the element.
- **It draws at a fixed bond length and does NOT scale small molecules up.** A 2-atom molecule in a big canvas looks tiny with huge margins. Fix by tuning the drawer so a *typical* molecule fills the box: ring/general structures `mkDrawer(230,168,pad 6,bondLength 22)` → ~85% fill; linear route/mechanism molecules need a **wide-short** canvas `mkDrawer(175,88,pad 4,bondLength 22)` (~2:1) because they're wide and short.
- **It DOES render Hg, B, Os, and other metals** — so draw organometallic intermediates (organomercurial, organoborane, osmate ester) as real structures, don't fall back to text. They make "intermediate" answer boxes look complete instead of cut-in-half.
- **Stereochemistry on rings is unreliable** to render from `@`/`@@`. Draw the flat constitution and put cis/trans/syn/anti/R-S/racemic/meso/diastereomer in a **text tag** under the card — that's the channel that's always correct. (E/Z on acyclic double bonds via `/\` renders fine.)
- The first `draw()` call can race library/font init and fail → the engine **retries** (setTimeout) up to 3× before showing the SMILES as fallback text. If you ever see raw SMILES in a `.cap` div, that molecule failed.

## Canvas sizing (the "tiny structures / huge gaps" bug)

- Give every structure canvas a **fixed display box + `object-fit:contain`**: `.mol canvas{width:100%;max-width:235px;height:170px;object-fit:contain}`. This means: scale the bitmap to fit, no distortion, and the canvas can **never balloon tall** (some browsers auto-size canvas height huge, which created enormous vertical gaps). `object-fit` is honored on `<canvas>` (it's a replaced element).
- Don't rely on `height:auto` — an inline height SmilesDrawer writes can override your stylesheet, and a too-tall canvas wrecks the layout.
- Backing store is automatically `displaySize × devicePixelRatio` (Retina-crisp). Don't multiply size yourself — that double-scales.

## MathJax / mhchem

- Config with `startup: { typeset: false }`, then call `typesetMath()` **after** building the DOM. MathJax's auto-typeset runs once on load; if you inject `$\ce{}$` nodes later it's a race and you get raw `$\ce{C9H8O4}$` text. The engine polls until MathJax is ready then typesets manually.
- Formulas: `$\ce{C8H10N4O2}$` → C₈H₁₀N₄O₂. Charges: `$\ce{C=O+}$`, arrows `$\ce{->}$`, `$\ce{<=>}$`.

## Layout

- **Vertical reaction cards** (reactant on top → reagents+down-arrow → product below). Horizontal SM→arrow→product crams structures to ~50px on phones. Vertical gives full-width structures at every width.
- 2-column grid on wide screens (`@media min-width:760px`) keeps the page from being absurdly long.
- Wrap wide tables in `.tablewrap` (overflow-x:auto) so they scroll on phones instead of overflowing the page.

## PDF export (scripts/make_pdf.py)

- Needs a real JS-executing browser. `python -m playwright install chromium` once (~90MB). `wkhtmltopdf`/weasyprint won't run the JS → blank structures.
- **Chromium's `page.pdf()` silently drops live `<canvas>` content.** The script bakes every canvas to a PNG `<img>` (`canvas.toDataURL`) before exporting — images always print. This is the single most important step.
- It waits for canvases to have pixels + MathJax to finish before exporting.
- `print_background=True` + `device_scale_factor=2` for styled, crisp output. Per-card `break-inside:avoid` keeps cards whole; a small print-only CSS shrinks structures so ~2 reactions fit per A4 page.

## Verifying your output

- Preview: `python scripts/serve.py /tmp 8731` (custom server — stock `http.server` crashes where `os.getcwd()` is sandboxed), copy the HTML to `/tmp`, open `http://127.0.0.1:8731/<file>.html` in the preview tool.
- Programmatic check (more reliable than screenshots at large scroll offsets): eval in the page — count canvases, count `.smfail` (a SMILES that failed to parse — **must be 0**; note step labels use `.cap`, so check `.smfail` specifically), count `mjx-container` (MathJax rendered), and measure a canvas's drawn-pixel bounding box vs its size (fill fraction should be ~0.8+ for compact molecules).
- Inspect the PDF without a browser: `pip install pymupdf`, then `fitz.open(pdf)[page].get_pixmap(dpi=105).save(png)` and look at the png. Don't trust that "it'll be fine" — the canvas-in-PDF bug is invisible until you look.
