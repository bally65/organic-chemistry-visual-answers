# Solving the problems & verifying answers

The rendering is only half the job — the answers have to be *right*. For anything where a
wrong answer is plausible-looking (organic mechanisms, stereochemistry, multi-step math),
verify before you render.

## Read the source carefully first

- Transcribe every structure/equation yourself from the image. For chemistry, convert each
  starting material to a **SMILES** string and name the reagents. This transcription is the
  part only you (with vision) can do — own it; subagents can't see the image.
- Watch for labels that exist **to expose stereochemistry**: a deuterium (`[2H]`) on an
  alkene carbon is there so syn/anti addition becomes visible (the carbon becomes a real
  stereocenter after addition). If you see D/²H on a reacting position, the question is
  testing addition stereochemistry — answer with syn/anti + which diastereomer/racemate/meso.
- If you genuinely can't read a structure, say so and flag it in the output (`⚠ 結構依印卷確認`)
  rather than guessing silently.

## Verify with a fan-out workflow (for substantive problem sets)

For a sheet with many reactions or any stereochemistry, run a verification Workflow: one
expert agent per problem derives the answer **independently** from your transcription, then
compares to your proposed answer. This catches regio/stereo slips and yields validated SMILES
you can render directly. Have each agent return structured data:

```
{ id, product, smiles, regiochem, stereochem, mechanism, agree, note, confidence }
```

Caveats to keep in mind when reading the results:
- Agents only see the **one** item you give them. If a step depends on a prior intermediate
  ("(product 8) then H₂O₂"), spell out what product 8 is, or the agent will guess the wrong
  reaction (this produced a real false-positive disagreement). Give each item enough context.
- The agents are right about chemistry far more often than not, but YOU still own the
  image-reading. A disagreement might be the agent missing context, not a wrong answer.

## Communicating stereochemistry in the answer

Because ring stereo doesn't render reliably, the **text tag** under each card carries it.
Be explicit and specific:
- regiochemistry: Markovnikov / anti-Markovnikov, and why.
- addition mode: **syn** or **anti**.
- product stereo: cis/trans, E/Z, R/S where defined.
- mixture vs single: **single diastereomer (racemic)**, **meso (achiral)**, or **mixture of
  diastereomers** (the last happens via planar carbocations and radical demercuration —
  non-stereospecific steps).

## Other subjects

The engine isn't chemistry-only. Use it for any subject:
- **Math/physics**: skip SmilesDrawer; use MathJax for all equations/derivations
  (`$...$` inline, `$$...$$` display), and tables for structured answers. Show worked steps.
- **Mixed sheets**: render the chemistry parts with structures, everything else with
  MathJax + tables + prose. One HTML, one PDF.

## Optional: save the answer key as a deliverable

The user usually wants the PDF. Deliver it (and mention the HTML lives alongside it). Keep
both files in the user's project folder, not /tmp.
