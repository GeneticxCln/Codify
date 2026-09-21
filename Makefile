.PHONY: help test test-engine build-ui dev-ui check-tauri build-tauri run-engine check clean

help:
	@echo "Codify Development Commands:"
	@echo "  make test         - Run full Python test suite (27 tests)"
	@echo "  make build-ui     - Typecheck and build React frontend (Vite)"
	@echo "  make dev-ui       - Start Vite dev server"
	@echo "  make check-tauri  - Cargo check Tauri Rust backend"
	@echo "  make build-tauri  - Build Tauri desktop application"
	@echo "  make run-engine   - Start Codify Python engine standalone"
	@echo "  make check        - Run all verifications (Python tests + UI build + Tauri check)"
	@echo "  make clean        - Remove caches and build artifacts"

test: test-engine

test-engine:
	python3 -m unittest discover -s tests -p "test_*.py" -v

build-ui:
	cd ui && npm run build

dev-ui:
	cd ui && npm run dev

check-tauri:
	cd src-tauri && cargo check

build-tauri:
	cd src-tauri && cargo build

run-engine:
	python3 -m engine

check: test build-ui check-tauri
	@echo "All verifications passed successfully!"

clean:
	rm -rf ui/dist src-tauri/target __pycache__ engine/__pycache__ tests/__pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
