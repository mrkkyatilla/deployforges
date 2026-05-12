#!/bin/bash
set -e

echo "=== DeployForge: Initial Migration ==="
echo ""

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Run 'make setup' first."
    exit 1
fi

echo "1. Generating initial migration..."
alembic revision --autogenerate -m "initial_schema"

echo ""
echo "2. Applying migration..."
alembic upgrade head

echo ""
echo "=== Migration complete! ==="
echo ""
echo "Tables created:"
echo "  - users"
echo "  - api_keys"
echo "  - projects"
echo "  - builds"
echo "  - deployments"
echo "  - ai_interactions"
echo "  - credit_transactions"
echo "  - webhooks"
echo "  - webhook_deliveries"
echo "  - inbound_webhook_configs"
echo "  - pipeline_runs"
