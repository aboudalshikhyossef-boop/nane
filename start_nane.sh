#!/usr/bin/env bash
# Starts NANE locally and publishes it through a temporary Cloudflare Quick Tunnel.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
HOST="127.0.0.1"
PORT="$(printenv NANE_PORT || printf 8765)"
CLOUDFLARED_BIN="$(printenv CLOUDFLARED_BIN || printf cloudflared)"
fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "NANE_PORT must be numeric."
(( PORT >= 1024 && PORT <= 65535 )) || fail "NANE_PORT must be between 1024 and 65535."
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required."
command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1 || {
  echo "cloudflared is missing. Install it from https://developers.cloudflare.com/tunnel/downloads/" >&2
  exit 1
}
TMP_BASE="$(printenv TMPDIR || printf /tmp)"
RUN_DIR="$(mktemp -d "$TMP_BASE/nane-session.XXXXXX")"
chmod 700 "$RUN_DIR"
SERVER_LOG="$RUN_DIR/server.log"
TUNNEL_LOG="$RUN_DIR/tunnel.log"
SERVER_PID=""
TUNNEL_PID=""
stop_child(){
  local child="$1"
  [[ -n "$child" ]] || return 0
  if kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$child" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL "$child" 2>/dev/null || true
  fi
  wait "$child" 2>/dev/null || true
}
cleanup(){
  local code=$?
  trap - EXIT INT TERM
  echo
  echo "Stopping NANE server and Cloudflare tunnel..."
  stop_child "$TUNNEL_PID"
  stop_child "$SERVER_PID"
  rm -rf "$RUN_DIR"
  echo "NANE session stopped."
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Starting NANE server on $HOST:$PORT ..."
PYTHONUNBUFFERED=1 python3 -m server.app --host "$HOST" --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
SERVER_READY=0
for _ in $(seq 1 20); do
  if grep -Fq "NANE application server is running." "$SERVER_LOG"; then SERVER_READY=1; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Python server stopped unexpectedly:" >&2; cat "$SERVER_LOG" >&2 || true; exit 1
  fi
  sleep 0.5
done
[[ "$SERVER_READY" -eq 1 ]] || { echo "Python server did not become ready:" >&2; cat "$SERVER_LOG" >&2 || true; exit 1; }

echo "Starting Cloudflare Quick Tunnel without changing VPN or firewall settings..."
"$CLOUDFLARED_BIN" tunnel --url "http://$HOST:$PORT" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
PUBLIC_URL=""
for _ in $(seq 1 90); do
  PUBLIC_URL="$(grep -Eo 'https://[[:alnum:]-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"
  [[ -n "$PUBLIC_URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "cloudflared exited before creating the URL:" >&2; cat "$TUNNEL_LOG" >&2 || true; exit 1
  fi
  sleep 1
done
if [[ -z "$PUBLIC_URL" ]]; then
  echo "No Quick Tunnel URL appeared within 90 seconds." >&2
  tail -n 50 "$TUNNEL_LOG" >&2 || true
  echo "Check internet/VPN reachability. The launcher never disables VPN protections." >&2
  exit 1
fi
echo
echo "============================================================"
echo " NANE — SERVER READY"
echo " Share this URL with all players:"
echo " $PUBLIC_URL"
echo " Open the same URL on the laptop and every phone."
echo " Keep this terminal open; press Ctrl+C to stop."
echo "============================================================"
echo
echo "SQLite remains on this laptop, outside the repository."
echo "Quick Tunnel URLs are public while active."
echo "Open /compatibility for a temporary device/network check."
echo
wait "$TUNNEL_PID"
