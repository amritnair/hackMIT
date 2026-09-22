#!/usr/bin/env bash
# Runs once, when the codespace is created.
set -eu

pip install --quiet -e .

# `prophecy demo` makes commits, and a fresh container has no git identity
git config --global user.email "you@codespace.local"
git config --global user.name "Codespace"
git config --global init.defaultBranch main

# The demo project: five branches in flight, agents already at work. Built
# rather than cloned so the working tree is yours to edit, commit and revert,
# which is the half the published site cannot do.
if [ ! -d /workspaces/demo/.git ]; then
  prophecy demo /workspaces/demo
fi

echo
echo "Prophecy is installed. The dashboard starts on port 8000."
echo "Point it at any other repository from the project picker, or:"
echo "  prophecy -C /path/to/your/repo serve --port 8000"
