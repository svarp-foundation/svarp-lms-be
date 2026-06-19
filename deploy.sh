#!/bin/bash

# Exit immediately if any command exits with a non-zero status
set -e

echo "=== Starting Backend Deployment ==="

# Navigate to the backend directory (where this script is located)
cd "$(dirname "$0")"

# 1. Pull latest changes from dev branch
echo "Pulling latest changes from origin dev..."
git pull origin dev

# 2. Apply database migrations
if [ -f "./scripts/migrate.sh" ]; then
    echo "Running database migrations..."
    ./scripts/migrate.sh apply
else
    echo "Warning: migrate.sh not found, skipping database migrations."
fi

# 3. Restart the backend systemd service
echo "Restarting the backend service..."
sudo systemctl restart svarp-lms-be

echo "=== Backend Deployment Completed Successfully ==="
