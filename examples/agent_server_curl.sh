#!/bin/sh
# Talk to `a11y-agent serve` with curl. Not run by tests or CI.
# Start the server first:
#   a11y-agent serve --host 127.0.0.1 --port 8765 --token "$TOKEN"
set -eu

BASE="${BASE:-http://127.0.0.1:8765}"
TOKEN="${TOKEN:-}"
GOAL="${GOAL:-Save the note}"
MODEL="${MODEL:-scripted:turns.json}"
DISPLAY="${DISPLAY_ARG:-}"

auth() {
  if [ -n "$TOKEN" ]; then
    printf 'Authorization: Bearer %s\n' "$TOKEN"
  fi
}

body=$(printf '{"goal":"%s","model":"%s"' "$GOAL" "$MODEL")
if [ -n "$DISPLAY" ]; then
  body=$(printf '%s,"display":"%s"' "$body" "$DISPLAY")
fi
body="$body}"

echo "POST $BASE/runs"
if [ -n "$TOKEN" ]; then
  created=$(curl -sS -H "$(auth)" -H 'Content-Type: application/json' -d "$body" "$BASE/runs")
else
  created=$(curl -sS -H 'Content-Type: application/json' -d "$body" "$BASE/runs")
fi
echo "$created"
id=$(printf '%s' "$created" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
if [ -z "$id" ]; then
  echo "no run id in the response" >&2
  exit 1
fi

echo "GET $BASE/runs/$id/events"
if [ -n "$TOKEN" ]; then
  curl -N -sS -H "$(auth)" "$BASE/runs/$id/events"
else
  curl -N -sS "$BASE/runs/$id/events"
fi
echo
echo "GET $BASE/runs/$id"
if [ -n "$TOKEN" ]; then
  curl -sS -H "$(auth)" "$BASE/runs/$id"
else
  curl -sS "$BASE/runs/$id"
fi
echo
