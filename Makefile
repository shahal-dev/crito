.PHONY: help install backend web infra

help:
	@echo "CRITO — targets:"
	@echo "  make install   install python deps (-e .) and web deps"
	@echo "  make backend    run the FastAPI core   -> http://localhost:8000"
	@echo "  make web        run the web console     -> http://localhost:5173"
	@echo "  make infra      start postgres/redis/nats/minio (later phases)"
	@echo ""
	@echo "  The INDI server runs on the observatory edge node (real drivers)."
	@echo "  Point CRITO_INDI_HOST/PORT at it, or set it from the console."

install:
	pip install -e .
	cd web && npm install

backend:
	# --reload-dir crito: watch only source, so runtime writes to data/ (bindings.json,
	# the SQLite archive) don't restart the server and drop the INDI connection.
	uvicorn crito.core.app:app --reload --reload-dir crito --host 0.0.0.0 --port 8000

web:
	cd web && npm run dev

infra:
	docker compose -f deploy/docker-compose.yml --profile infra up -d
