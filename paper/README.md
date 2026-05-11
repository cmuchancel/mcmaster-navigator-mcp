# ASME IDETC-CIE Paper Draft

Primary files:

- `asme_idetc_mcmaster_navigator.tex`: ASME-style LaTeX manuscript.
- `asme_idetc_mcmaster_navigator.bib`: references.
- `asme_idetc_mcmaster_navigator.pdf`: compiled PDF.
- `mcmaster_navigator_paper_draft.md`: source prose draft used to build the LaTeX paper.

The manuscript uses the current CTAN `asmeconf` class files vendored in this
directory because Tectonic uses an engine that `asmeconf` rejects; the class
requires pdfLaTeX or LuaLaTeX.

Build from the repository root:

```bash
mkdir -p paper/build
TEXINPUTS=paper//: BIBINPUTS=paper//: BSTINPUTS=paper//: \
  pdflatex -interaction=nonstopmode -halt-on-error -output-directory=paper/build \
  paper/asme_idetc_mcmaster_navigator.tex
BIBINPUTS=paper//: BSTINPUTS=paper//: \
  bibtex paper/build/asme_idetc_mcmaster_navigator
TEXINPUTS=paper//: BIBINPUTS=paper//: BSTINPUTS=paper//: \
  pdflatex -interaction=nonstopmode -halt-on-error -output-directory=paper/build \
  paper/asme_idetc_mcmaster_navigator.tex
TEXINPUTS=paper//: BIBINPUTS=paper//: BSTINPUTS=paper//: \
  pdflatex -interaction=nonstopmode -halt-on-error -output-directory=paper/build \
  paper/asme_idetc_mcmaster_navigator.tex
cp paper/build/asme_idetc_mcmaster_navigator.pdf paper/asme_idetc_mcmaster_navigator.pdf
```

