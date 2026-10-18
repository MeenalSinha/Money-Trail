.PHONY: install up down check init test-unit test-integration test
install:        ; pip install -e ".[dev]"
up:             ; docker compose up -d
down:           ; docker compose down        # keeps data volume
check:          ; money-trail-check
init:           ; money-trail-init
test-unit:      ; pytest tests/unit
test-integration: ; pytest tests/integration
test:           ; pytest
generate:       ; money-trail generate --config examples/generator.example.json
