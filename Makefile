.PHONY: test lint install

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

install:
	python3 -m pip install .

lint:
	python3 -m compileall -q src tests
