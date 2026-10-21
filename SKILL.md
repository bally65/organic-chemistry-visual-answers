---
name: exam-answer-key
description: >-
  Produce a polished, verified ANSWER KEY (rendered HTML + PDF) from an exam paper, problem
  set, quiz, or homework sheet — especially when answers need real chemical structures,
  reaction products, mechanisms, or math/equations drawn out rather than described in words.
  Use this whenever the user shares a photo/scan/PDF of test questions and wants the answers
  worked out and laid out nicely, says things like "分析這份考卷/題目幫我做出解答",
  "幫我生成解答", "solve this problem set and make an answer key", "出一份解答 HTML/PDF",
  or asks to render organic-chemistry reactions, SMILES structures, pKa/molecular formulas,
  keto-enol/arrow-pushing mechanisms, or synthesis routes. Strongly prefer this skill for
  multi-question chemistry/science sheets even if they don't say "answer key" — it bundles a
  battle-tested structure-rendering engine and PDF pipeline so you don't re-debug blank
  canvases. Not for editing a single Word/Excel/PowerPoint file or for a one-off "what's the
  answer to this one question" with no rendering needed.
---

# Exam answer key

Turn a sheet of questions into a clean, **verified**, rendered answer key — HTML you can read
in a browser plus a paginated PDF — with chemical structures, mechanisms, and formulas drawn
out properly. The hard parts (getting structures to actually render and survive PDF export,
and getting the chemistry *right*) are already solved here; reuse them instead of rediscovering
the bugs.

## When this applies

Any worked-answers deliverable from question images: organic chemistry reaction sheets,
general chemistry, but also math/physics problem sets (MathJax instead of structures). The
standout strength is chemistry: SMILES → skeletal structures, reaction products with
regio/stereochemistry, intermediates (incl. organometallics), synthesis routes, arrow-pushing
mechanisms, and `\ce{}` molecular formulas/pKa/charges.

## Workflow

### 1. Read the source carefully (you own this — subagents can't see the image)
Transcribe every question. For chemistry, write each starting material as a **SMILES** and
note the reagents. Watch for deuterium/`[2H]` labels on reacting carbons — they exist to
expose addition stereochemistry (syn/anti). If a structure is unreadable, flag it rather than
guess. See `references/solving-and-verifying.md`.

### 2. Verify the answers before rendering
For a substantive sheet (many reactions, any stereochemistry, multi-step problems), run a
fan-out **Workflow**: one expert agent per problem derives the answer independently from your
transcription and returns `{product, smiles, regiochem, stereochem, mechanism, agree, note}`.
This catches regio/stereo errors and gives you validated SMILES to render. Give each agent
enough context (esp. when a step depends on a prior intermediate). Details + caveats in
`references/solving-and-verifying.md`. For a tiny sheet you can skip this and reason directly.

### 3. Build the HTML from the template
Copy `assets/template.html` to the output location (in the user's project folder, e.g.
`<name>_answers.html`). It already wires up SmilesDrawer v1.2.0, MathJax+mhchem, and a render
engine with these helpers — **reuse them, don't rebuild**:

- `buildRxn(container, list)` — vertical reaction cards. Each item:
  `{ n, sm:'<SMILES>', cond:'<reagents>', prod:'<SMILES|null>', name:'<product>', mech, tag:'<stereo note>', tagClass:'|bad|warn', inter:true|false }`.
  Put cis/trans/syn/anti/racemic/meso/diastereomer in `tag` (ring stereo doesn't render reliably).
  Draw intermediates (organomercurial, organoborane, osmate…) as real SMILES — SmilesDrawer
  handles Hg/B/Os; set `inter:true` for the dashed "intermediate" style.
- `buildRoute(container, title, steps, arrows, stepsHtml?)` — synthesis routes / mechanisms.
  `steps=[{smiles,label}]`, `arrows=['reagents', …]`. Append an `<ol>` of arrow-pushing steps
  via `stepsHtml` for mechanisms (formal charges in `<span class="fc">`).
- `buildFormulas(container, [[name,'\\ce{C8H10N4O2}'], …])` — molecular formulas.
- Generic tables for classification/ranking questions (wrap in `.tablewrap`).

Replace the demo DATA block at the bottom with your real content, then keep the final
`flushQueue(); typesetMath();` calls. **Read `references/rendering-cookbook.md` before
editing the engine** — every default (drawer sizes, `object-fit` boxes, synchronous flush,
manual typeset) fixes a specific rendering bug.

For non-chemistry sheets: skip SmilesDrawer entirely, use MathJax (`$...$`, `$$...$$`) for
equations and tables/prose for the rest. Same template, same PDF pipeline.

### 4. Verify it actually renders (don't assume)
The canvas/MathJax bugs are invisible until you look. Copy the HTML to `/tmp`, run
`python scripts/serve.py /tmp 8731`, open it in the preview tool, and check programmatically:
canvas count (**> 0**, and matching the number of structures you wrote), `typeof SmilesDrawer`
(must not be `'undefined'`), `.smfail` count (must be 0 = no SMILES failed to parse),
`mjx-container` count (MathJax rendered, no raw `$\ce{…}$` left in the text), and a sample
canvas's fill fraction (~0.8+ — the bounding box of the drawn pixels vs. the canvas, not ink
coverage). **`.smfail = 0` alone proves nothing:** if a CDN script failed to load, the page
script throws before it builds any DOM, so you get 0 canvases *and* 0 `.smfail` — a vacuous
pass. Screenshots are flaky at large scroll offsets — prefer the numeric checks. Fix anything
off, citing the cookbook. (`make_pdf.py` re-runs these checks itself — step 5 — but a quick look
here catches problems before you spend a PDF export on them.)

### 5. Make the PDF
```
python3 -m pip install --user playwright pymupdf   # first time only
python3 scripts/make_pdf.py <input.html> <output.pdf>
```
**Browser.** The script needs a Chromium. If Playwright's own build is missing it falls back
automatically to one already on the machine (`$PLAYWRIGHT_BROWSERS_PATH`, `/opt/pw-browsers`,
`~/.cache/ms-playwright`, or a `chromium`/`google-chrome` on `PATH`) and says which it used;
`--chromium /path/to/chrome` forces one. Only when there is no Chromium at all, run
`python3 -m playwright install chromium` (a few hundred MB — roughly 200–300 MB depending on
the Playwright version). In sandboxes that ship a pre-installed browser, don't install one: a
`pip` Playwright newer than the pre-installed build is expected, and the fallback handles it.

The script executes the page in headless Chromium, waits for structures+MathJax, **checks the
render**, **bakes every canvas to a PNG `<img>`** (Chromium's `page.pdf()` drops live canvases —
this is essential), and exports A4 with ~2 reactions/page.

**It fails loudly.** Checks: SmilesDrawer/MathJax actually loaded, no JS errors, no failed
script/stylesheet requests (e.g. the CDN), no blank canvases, no `.smfail`, no MathJax errors or
un-typeset `$…$`, and (with `pymupdf` installed) that the PDF really contains the baked images.
On any failure it prints the reason, **writes no PDF and exits 2** — don't deliver anything
after a non-zero exit, and fix the cause rather than reaching for `--lenient` (which only turns
errors into warnings, for inspecting a broken page). Exit 1 = setup problem (no Playwright / no
browser). In the Claude Code cloud sandbox Chromium reaches the CDN via the `HTTPS_PROXY` in the
environment — keep it set; without it the CDN requests die with `ERR_CERT_AUTHORITY_INVALID`
and the script exits 2 with a hint.

Then look at the result anyway: rasterize a few pages with PyMuPDF
(`import pymupdf; pymupdf.open(pdf)[i].get_pixmap(dpi=105).save(png)`) and confirm the
structures and formulas are really there.

### 6. Deliver
Give the user the PDF (and note the HTML lives beside it). Keep both in their project folder.
Surface any flagged uncertainties (unreadable structures, debatable stereochemistry) and the
key corrections if you fixed errors in a draft they provided.

## Bundled resources
- `assets/template.html` — starter HTML with the full render engine + working examples. Copy & edit.
- `scripts/make_pdf.py` — HTML → verified PDF via Playwright (bakes canvases, falls back to an installed Chromium, exits non-zero without a PDF if the render is wrong; the core of step 5).
- `scripts/serve.py` — sandbox-safe static server for previewing (step 4).
- `references/rendering-cookbook.md` — why every rendering default is what it is. Read before touching the engine.
- `references/solving-and-verifying.md` — how to read the source, run the verification workflow, and phrase stereochemistry.
