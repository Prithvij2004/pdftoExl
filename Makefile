.PHONY: test lint eval eval-update-goldens install

install:
	pip install -e '.[dev]'

lint:
	ruff check src tests

test:
	pytest

eval:
	python -m pdftoxl.evals run --eval B --all
	python -m pdftoxl.evals run --eval C --all

eval-update-goldens:
	@echo "Stub: regeneration of goldens is not implemented."
	@echo "Usage (future): make eval-update-goldens FX=<fixture_id>"
	@false
