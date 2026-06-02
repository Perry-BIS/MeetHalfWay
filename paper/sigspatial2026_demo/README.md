# SIGSPATIAL 2026 Demo Paper Draft

This folder contains an ACM `acmart` draft for the SIGSPATIAL 2026 Demonstration Track.

Official demo-track requirements checked from `https://sigspatial2026.sigspatial.org/demo-submission.html`:

- Page limit: up to 4 pages, including references.
- Submission format: PDF using ACM camera-ready templates.
- Template: ACM Conference Proceedings Primary Article, two-column format.
- Title rule: submitted demo-track titles should end with `[Demo]`.
- Reviewing model: single-blind, so author names and affiliations must appear in the submitted version.
- AI disclosure: generative AI use must be disclosed in the paper.

## Files

- `main.tex`: ACM two-column demo paper draft.
- `references.bib`: BibTeX entries for related work.

## Before submission

1. Replace all `TODO` author and affiliation placeholders as appropriate.
2. Keep `[Demo]` in the submitted title; remove it only for the camera-ready copy if accepted.
3. Compile and verify that the PDF is no more than 4 pages including references.
4. Re-check the official demo page before submission in case EasyChair or template details change.
5. Confirm the AI-use disclosure in the acknowledgements matches your actual writing workflow.

## Compile

Preferred:

```powershell
latexmk -pdf main.tex
```

Fallback:

```powershell
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```
