.DEFAULT_GOAL := check
PYTHON ?= python3

check:
	@$(PYTHON) -m pytest -q -p no:cacheprovider tests/test_gitchat_channel.py tests/test_gitchat_serve.py tests/test_gitchat_stream_log.py
	@$(PYTHON) tests/test_integrations.py

record:
	@$(PYTHON) scripts/record_session.py

demo:
	@$(PYTHON) scripts/generate_demo.py

assets:
	@$(PYTHON) assets/build.py

asset-check:
	@$(PYTHON) assets/build.py --check

.PHONY: check record demo assets asset-check
