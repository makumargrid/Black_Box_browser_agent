.PHONY: up tools setup-hexstrike

up:  ## Start blackbox-agent + juice-shop (no HexStrike)
	docker compose up --build

tools: setup-hexstrike  ## Start full stack with HexStrike tools (auto-clones repo if needed)
	docker compose --profile tools up --build

setup-hexstrike:  ## Clone HexStrike AI into ./hexstrike if not already present
	@if [ ! -d "hexstrike" ]; then \
	  echo "Cloning HexStrike AI..."; \
	  git clone https://github.com/0x4m4/hexstrike-ai.git hexstrike 2>/dev/null || \
	  git clone --branch v6.0 https://github.com/0x4m4/hexstrike-ai.git hexstrike 2>/dev/null || \
	  { echo "ERROR: Could not clone HexStrike. See docs/hexstrike_integration.md for manual setup."; exit 1; }; \
	else \
	  echo "hexstrike/ already present — skipping clone."; \
	fi
