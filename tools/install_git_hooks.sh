#!/bin/sh

set -eu

repository_root=$(git rev-parse --show-toplevel)
cd "$repository_root"

chmod +x .githooks/pre-commit
git config --local core.hooksPath .githooks

configured_path=$(git config --local --get core.hooksPath)
if [ "$configured_path" != ".githooks" ]; then
    echo "Failed to configure repository hooks path." >&2
    exit 1
fi

echo "Git hooks path: $configured_path"
echo "Graphify pre-commit hook installed."
