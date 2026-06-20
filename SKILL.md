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
  multi-question chemistry/science sheets even if the user doesn't say the word "skill" or
  "answer key" — it bundles a battle-tested structure-rendering engine and PDF pipeline so
  you don't re-debug blank canvases. Not for editing a single Word/Excel/PowerPoint file or
  for one-off "what's the answer to this one question" with no rendering needed.
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
canvas count, `.smfail` count (must be 0 = no SMILES failed to parse), `mjx-container`
count (MathJax rendered), and a sample canvas's fill fraction (~0.8+). Screenshots are flaky
at large scroll offsets — prefer the numeric checks. Fix anything off, citing the cookbook.

### 5. Make the PDF
```
python3 -m pip install --user playwright      # first time only
python3 -m playwright install chromium        # first time only (~90MB)
python scripts/make_pdf.py <input.html> <output.pdf>
```
The script executes the page in headless Chromium, waits for structures+MathJax, **bakes every
canvas to a PNG `<img>`** (Chromium's `page.pdf()` drops live canvases — this is essential),
and exports A4 with ~2 reactions/page. Verify the PDF by rasterizing a few pages with PyMuPDF
(`fitz`) and looking — confirm structures aren't blank.

### 6. Deliver
Give the user the PDF (and note the HTML lives beside it). Keep both in their project folder.
Surface any flagged uncertainties (unreadable structures, debatable stereochemistry) and the
key corrections if you fixed errors in a draft they provided.

## Bundled resources
- `assets/template.html` — starter HTML with the full render engine + working examples. Copy & edit.
- `scripts/make_pdf.py` — HTML → PDF via Playwright (bakes canvases; the core of step 5).
- `scripts/serve.py` — sandbox-safe static server for previewing (step 4).
- `references/rendering-cookbook.md` — why every rendering default is what it is. Read before touching the engine.
- `references/solving-and-verifying.md` — how to read the source, run the verification workflow, and phrase stereochemistry.
