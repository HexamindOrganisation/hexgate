#!/usr/bin/env bash
# Checks the nginx files the box includes, before the box does:
#   1. `nginx -t` on each stage's file, included from a stub server block
#      shaped like the box's (a catch-all `location /` to the api), so a typo
#      or a location that clashes with the catch-all fails here, not on reload.
#   2. Neither file names the other stage's ports (70xx prod, 72xx staging),
#      so a copy left on the other stage's port fails, and the staging file
#      carries the same directives as prod's, ports aside, so a route added
#      to one stage only fails too.
#   3. The /v1/traces block points at the collector (port x001) with a body
#      cap equal to the collector's own max_request_body_size: if the proxy
#      allows less, large exports get a 413 from nginx; if more, nginx buffers
#      bodies the collector then refuses.
# Needs `nginx` on PATH; writes nothing outside a temp directory.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

check_stage() {
    local stage="$1" api_port="$2"
    local conf="$tmp/$stage.conf"
    cat >"$conf" <<EOF
pid $tmp/$stage.pid;
error_log $tmp/$stage-error.log;
events {}
http {
    access_log off;
    # Under the temp dir, so the check does not depend on the runner having
    # the build's own temp directories.
    client_body_temp_path $tmp/body;
    proxy_temp_path $tmp/proxy;
    fastcgi_temp_path $tmp/fastcgi;
    uwsgi_temp_path $tmp/uwsgi;
    scgi_temp_path $tmp/scgi;
    server {
        listen 127.0.0.1:8080;
        server_name $stage.hexgate.test;
        # The exact path, not the box's wildcard form: here a missing file
        # must fail the check rather than include nothing.
        include $here/hexgate-$stage.conf;
        location / {
            proxy_pass http://127.0.0.1:$api_port;
        }
    }
}
EOF
    nginx -t -q -p "$tmp" -e "$tmp/$stage-error.log" -c "$conf"
    echo "nginx -t: hexgate-$stage.conf ok"
}

# Directives only: comments (a `#` at the start of a token, not one inside a
# value), blank lines and surrounding whitespace dropped.
directives() {
    sed -E -e 's/(^|[[:space:]])#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' "$1"
}

# A port of the other stage in a file's directives, on any host.
wrong_stage_port() {
    local file="$1" other="$2"
    if directives "$file" | grep -E ":${other}[0-9]{2}([^0-9]|$)"; then
        echo "$(basename "$file") points at the other stage's port (${other}xx)" >&2
        return 0
    fi
    return 1
}

check_stage prod 7000
check_stage staging 7200

if wrong_stage_port "$here/hexgate-prod.conf" 72 || wrong_stage_port "$here/hexgate-staging.conf" 70; then
    exit 1
fi

if ! diff <(directives "$here/hexgate-prod.conf") \
          <(directives "$here/hexgate-staging.conf" | sed -E 's/:72([0-9]{2})([^0-9]|$)/:70\1\2/g'); then
    echo "hexgate-prod.conf and hexgate-staging.conf differ beyond their ports" >&2
    exit 1
fi
echo "prod and staging match, ports aside"

# The directives inside prod's `location = /v1/traces { … }`.
traces_block="$(directives "$here/hexgate-prod.conf" \
    | awk '/^location = \/v1\/traces \{/{inside=1; next} inside && /^}/{inside=0} inside')"

if ! grep -qE '^proxy_pass http://127\.0\.0\.1:7001;$' <<<"$traces_block"; then
    echo "location = /v1/traces does not proxy_pass to the collector (127.0.0.1:7001 on prod)" >&2
    exit 1
fi

# Bytes in the collector's config; the cap is written in MiB here (`32m`).
collector_bytes="$(sed -nE 's/^[[:space:]]*max_request_body_size:[[:space:]]*([0-9]+).*/\1/p' "$here/../collector/config.yaml")"
proxy_mib="$(sed -nE 's/^client_max_body_size ([0-9]+)[mM];$/\1/p' <<<"$traces_block")"
if ! [[ "$collector_bytes" =~ ^[0-9]+$ && "$proxy_mib" =~ ^[0-9]+$ ]] \
    || (( collector_bytes != proxy_mib * 1024 * 1024 )); then
    echo "the /v1/traces client_max_body_size (want one value in MiB, e.g. 32m; read '${proxy_mib}')" \
         "is not the collector's max_request_body_size (one value in bytes; read '${collector_bytes}')" >&2
    exit 1
fi
echo "/v1/traces goes to the collector, body cap ${proxy_mib}m matches it"
