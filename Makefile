.PHONY: test test-api test-e2e test-fast install-test-deps

# Run all tests
test: test-api test-e2e

# API tests only (fast, no browser)
test-api:
	python3 -m pytest tests/test_api.py -v

# E2E browser tests (requires Playwright + Chromium)
test-e2e:
	python3 -m pytest tests/test_e2e.py -v

# Fast path: API tests only
test-fast:
	python3 -m pytest tests/test_api.py -v

# Install test dependencies
install-test-deps:
	pip3 install pytest pytest-playwright requests
	python3 -m playwright install chromium
