#!/bin/bash
# elastik.sh — elastik in bash. Same five rules, different stdlib.
# Deps: bash 4+, nc, openssl, python3 (json escape only — the honest cheat).
# Run:  ./elastik.sh [PORT]

PORT=${1:-3006}; DIR=${WORLDS_DIR:-worlds}; KEY=${ELASTIK_KEY:-dev-key}
mkdir -p "$DIR"
# python3 on Unix, python on Windows (Windows Store python3 is a failing stub)
PY=; for p in python3 python; do command -v $p >/dev/null 2>&1 && $p -c '' 2>/dev/null && { PY=$p; break; }; done
[ -z "$PY" ] && { echo "error: need python3 or python for json escape" >&2; exit 1; }

jesc(){ printf '%s' "$1" | $PY -c 'import sys,json;print(json.dumps(sys.stdin.read()),end="")'; }
hmac(){ printf '%s' "$1" | openssl dgst -sha256 -hmac "$KEY" -r | cut -d' ' -f1; }
meta(){ grep -oE "\"$1\":[^,}]*" "$2" 2>/dev/null | cut -d: -f2- | tr -d '"'; }
respond(){ printf 'HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' "$1" "${#2}" "$2"; }

handle(){
  read -r m p _
  local cl=0 h b
  while IFS= read -r h; do h=${h%$'\r'}; [ -z "$h" ] && break; [[ "${h,,}" == content-length:* ]] && cl=${h#*: }; done
  (( cl > 0 )) && read -r -N "$cl" b
  p=${p%%\?*}; p=${p#/}; IFS=/ read -r n a <<< "$p"
  local d="$DIR/$n" pv=0 ph="" c=""
  [ -f "$d/meta.json" ] && { pv=$(meta version "$d/meta.json"); ph=$(meta hmac "$d/meta.json"); }
  [ -f "$d/content" ] && c=$(cat "$d/content")

  if [ "$m $n" = "GET stages" ]; then
    local o='[' f=1
    for x in "$DIR"/*/; do [ -f "$x/meta.json" ] || continue; (( f )) || o+=','; f=0
      o+="{\"name\":\"$(basename "$x")\",\"version\":$(meta version "$x/meta.json")}"; done
    respond "200 OK" "$o]"
  elif [ "$m $a" = "GET read" ]; then
    respond "200 OK" "{\"stage_html\":$(jesc "$c"),\"version\":$pv,\"hmac\":\"$ph\"}"
  elif [ "$m" = POST ] && [[ $a =~ ^(write|append)$ ]]; then
    mkdir -p "$d"
    local new; [ "$a" = append ] && new="$c$b" || new="$b"
    local nh; nh=$(hmac "$ph$new")
    printf '%s' "$new" > "$d/content.tmp" && mv "$d/content.tmp" "$d/content"
    printf '{"version":%d,"hmac":"%s"}' $((pv+1)) "$nh" > "$d/meta.tmp" && mv "$d/meta.tmp" "$d/meta.json"
    respond "200 OK" "{\"version\":$((pv+1)),\"hmac\":\"$nh\"}"
  else
    respond "404 Not Found" '{"error":"not found"}'
  fi
}

echo "elastik.sh → http://127.0.0.1:$PORT  (worlds/ = $(cd "$DIR" && pwd))"
while true; do
  coproc nc -l -p "$PORT" -q1 2>/dev/null || coproc nc -l "$PORT" 2>/dev/null
  handle <&"${COPROC[0]}" >&"${COPROC[1]}"
  wait "$COPROC_PID" 2>/dev/null
done
