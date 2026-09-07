.PHONY: help install test lint data study check clean

help:
	@echo "CostProof -- the counterfactual layer for cloud and AI spend"
	@echo ""
	@echo "  make install   install the package and dev dependencies"
	@echo "  make test      run the test suite"
	@echo "  make lint      ruff check"
	@echo "  make data      generate the FOCUS estate into data/"
	@echo "  make study     run the full validation study, regenerate every README number"
	@echo "  make check     validate FOCUS 1.2 conformance"
	@echo "  make clean     remove generated data and outputs"

install:
	pip install -e ".[dev,report]"

test:
	pytest

lint:
	ruff check src tests

data:
	python -m costproof.cli data

study:
	python -m costproof.cli study

check:
	python -m costproof.cli check

clean:
	rm -rf data/bronze/* data/silver/* data/gold/* outputs/figures/* outputs/tables/*
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
