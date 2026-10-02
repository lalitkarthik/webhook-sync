#!/usr/bin/env bash
# SessionStart hook. Does nothing on a local machine; in a Claude Code cloud
# session it installs what the session needs and reports what is available.
# What it prints is added to the session's context. It never prints a secret's
# value, only whether the variable is set. See docs/agents/cloud.md.

[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

echo "Cloud session: read docs/agents/cloud.md before working."

# Project dependencies, once there is a project to install.
if [ -f pyproject.toml ]; then
  if uv sync >/tmp/uv-sync.log 2>&1; then
    echo "uv sync: ok"
  else
    echo "uv sync: FAILED (see /tmp/uv-sync.log)"
  fi
fi

# The Firecrawl CLI, only when a key was supplied for it.
if [ -n "$FIRECRAWL_API_KEY" ] && ! command -v firecrawl >/dev/null 2>&1; then
  if npm install -g firecrawl-cli >/tmp/firecrawl-install.log 2>&1; then
    echo "firecrawl CLI: installed"
  else
    echo "firecrawl CLI: install FAILED (see /tmp/firecrawl-install.log)"
  fi
fi

# Which secrets this environment was given: names only.
for name in RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET RAZORPAY_WEBHOOK_SECRET \
            GOOGLE_SERVICE_ACCOUNT_B64 SHEET_ID FIRECRAWL_API_KEY; do
  if [ -n "${!name}" ]; then echo "env $name: set"; else echo "env $name: not set"; fi
done

# A live key must never reach a cloud session.
case "$RAZORPAY_KEY_ID" in
  rzp_live_*) echo "STOP: RAZORPAY_KEY_ID is a LIVE key. Use test keys only; tell the user." ;;
esac

exit 0
