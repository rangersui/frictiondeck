#!/bin/bash
# slow_write.sh — stream a file into an elastik world, chunk by chunk.
# Pure bash + curl. No Python, no urllib, no Windows Defender slowdowns.
#
# Usage:
#   ./slow_write.sh testworld.html
#   ./slow_write.sh page.html my-world tatatata
#   CHUNK=20 DELAY=0.02 ./slow_write.sh testworld.html

set -e

FILE="${1:?usage: slow_write.sh FILE [WORLD] [TOKEN]}"
WORLD="${2:-$(basename "$FILE" .${FILE##*.})}"
TOKEN="${3:-${ELASTIK_APPROVE_TOKEN:-${ELASTIK_TOKEN}}}"
HOST="${HOST:-127.0.0.1:3005}"
CHUNK="${CHUNK:-50}"
DELAY="${DELAY:-0.05}"
EXT="${EXT:-${FILE##*.}}"

[ -f "$FILE" ] || { echo "file not found: $FILE"; exit 1; }
[ -n "$TOKEN" ] || { echo "no token. pass as 3rd arg or set ELASTIK_APPROVE_TOKEN"; exit 1; }

TOTAL=$(wc -c < "$FILE")
BLOCKS=$(( (TOTAL + CHUNK - 1) / CHUNK ))

echo "→ target: http://$HOST/$WORLD (ext=$EXT)"
echo "→ source: $FILE ($TOTAL bytes, $BLOCKS chunks of $CHUNK)"
echo "→ delay:  ${DELAY}s between"
echo ""

# Reset
curl -s -X PUT "http://$HOST/home/$WORLD?ext=$EXT" \
  -H "Authorization: Bearer $TOKEN" \
  --data-binary "" > /dev/null
echo "[reset] world '$WORLD' cleared"

# Stream. dd is binary-safe; read -n strips newlines and dies on nulls.
t0=$(date +%s.%N)
for i in $(seq 0 $((BLOCKS - 1))); do
  dd if="$FILE" bs=$CHUNK skip=$i count=1 2>/dev/null | \
    curl -s -X POST "http://$HOST/home/$WORLD?ext=$EXT" \
      -H "Authorization: Bearer $TOKEN" \
      --data-binary @- > /dev/null
  sent=$(( (i + 1) * CHUNK ))
  [ $sent -gt $TOTAL ] && sent=$TOTAL
  pct=$(( sent * 100 / TOTAL ))
  printf "\r[%3d%%] %6d/%-6d  chunk %d/%d" $pct $sent $TOTAL $((i+1)) $BLOCKS
  sleep "$DELAY"
done
t1=$(date +%s.%N)
dt=$(echo "$t1 - $t0" | bc 2>/dev/null || python -c "print($t1 - $t0)")

echo ""
echo "done in ${dt}s  ($(echo "$TOTAL / $dt" | bc 2>/dev/null || python -c "print(int($TOTAL/$dt))") B/s)"
