.PHONY: all figures clean

all: memcommit.pdf

figures:
	uv run --with matplotlib python figures.py

# Uses pdflatex and bibtex when installed, otherwise tectonic.
memcommit.pdf: memcommit.tex commands.tex sec-*.tex references.bib fig/*.pdf
	@if command -v pdflatex >/dev/null; then \
	  pdflatex -interaction=nonstopmode memcommit && bibtex memcommit && \
	  pdflatex -interaction=nonstopmode memcommit && pdflatex -interaction=nonstopmode memcommit; \
	else tectonic memcommit.tex; fi

clean:
	rm -f *.aux *.bbl *.blg *.log *.out
