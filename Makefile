PORT ?= 8875

.PHONY: install test lint format run redteam gate bench noisy migrations cost screenshot check-mobile docker-build clean

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff format .
	uvx ruff check . --fix

run:  ## seeds a demo fleet into ./data on first start
	uv run tenancylab serve --port $(PORT)

redteam:
	uv run tenancylab redteam --backend all

gate:  ## the CI isolation gate: zero leaks on all backends, and the planted bug must be caught
	uv run tenancylab redteam --backend all
	uv run tenancylab redteam --backend pooled --planted-bug --expect-leaks

bench: noisy migrations  ## regenerate everything in results/
	uv run tenancylab redteam --backend all --out isolation.json

noisy:
	uv run tenancylab noisy

migrations:
	uv run tenancylab migrations

cost:
	uv run tenancylab cost

screenshot:  ## needs make run in another terminal and Google Chrome
	uv run python scripts/screenshot.py --port $(PORT)

check-mobile:  ## fails if the page overflows sideways at 390px (needs make run and Chrome)
	cd scripts && uv run python check_mobile.py --port $(PORT)

docker-build:
	docker build -t tenancy-lab .

clean:
	rm -rf .pytest_cache .ruff_cache data
