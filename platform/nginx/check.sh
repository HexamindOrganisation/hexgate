#!/usr/bin/env bash
# Checks the nginx files the box includes, before the box does:
#   1. `nginx -t` on each stage's file, included from a stub server block
#      shaped like the box's (a catch-all `location /` to the api), with the
#      stage's zones file at http level, so a typo, a location that clashes
#      with the catch-all or a zone that is not declared fails here, not on
#      reload.
#   2. No file names the other stage's ports (70xx prod, 72xx staging) or
#      zones (`_prod`, `_staging`), so a copy left on the other stage fails,
#      and each staging file carries the same directives as prod's, ports and
#      zone names aside, so a route added to one stage only fails too.
#   3. One `nginx -t` shaped like the box: both stages' zones and server
#      files in one http context, through the box's own wildcard includes, so
#      a zone name the two stages share, or a file the server wildcard would
#      pull in at the wrong level, fails here too.
#   4. Every location proxies to the port its route table names (api x000,
#      collector x001, MCP server x002), and /v1/traces has a body cap equal
#      to the collector's own max_request_body_size: if the proxy allows less,
#      large exports get a 413 from nginx; if more, nginx buffers bodies the
#      collector then refuses.
# Needs `nginx` on PATH; without it the check is skipped with a notice, unless
# NGINX_CHECK_REQUIRED is set (the CI job sets it, so a broken install step
# fails instead of passing). Writes nothing outside a temp directory.
set -euo pipefail

if ! command -v nginx >/dev/null; then
    if [[ -n "${NGINX_CHECK_REQUIRED:-}" ]]; then
        echo "nginx not found, and NGINX_CHECK_REQUIRED is set" >&2
        exit 1
    fi
    echo "nginx not found, skipping nginx-check (CI runs it)"
    exit 0
fi

here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The top of a stub config named $1: pid, log and temp paths under the temp
# dir, so the check does not depend on the runner's own nginx directories.
stub_head() {
    cat <<EOF
pid "$tmp/$1.pid";
error_log "$tmp/$1-error.log";
events {}
http {
    access_log off;
    client_body_temp_path "$tmp/body";
    proxy_temp_path "$tmp/proxy";
    fastcgi_temp_path "$tmp/fastcgi";
    uwsgi_temp_path "$tmp/uwsgi";
    scgi_temp_path "$tmp/scgi";
EOF
}

check_stage() {
    local stage="$1" api_port="$2"
    local conf="$tmp/$stage.conf"
    cat >"$conf" <<EOF
$(stub_head "$stage")
    include "$here/hexgate-zones-$stage.conf";
    server {
        listen 127.0.0.1:8080;
        server_name $stage.hexgate.test;
        # The exact path, not the box's wildcard form: here a missing file
        # must fail the check rather than include nothing.
        include "$here/hexgate-$stage.conf";
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
    sed -E -e 's/(^|[[:space:]])#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/[[:space:]]+/ /g' -e '/^$/d' "$1"
}

# A port (on any host) or a zone name of the other stage in a file's
# directives.
wrong_stage() {
    local file="$1" other_port="$2" other_name="$3"
    if directives "$file" | grep -E ":${other_port}[0-9]{2}([^0-9]|$)|_${other_name}([^a-z0-9_]|$)"; then
        echo "$(basename "$file") names the other stage's port (${other_port}xx) or zone (_${other_name})" >&2
        return 0
    fi
    return 1
}

# Staging's directives written as prod's: 72xx ports as 70xx, _staging as _prod.
as_prod() {
    directives "$1" | sed -E -e 's/:72([0-9]{2})([^0-9]|$)/:70\1\2/g' -e 's/_staging([^a-z0-9_]|$)/_prod\1/g'
}

# These four files, and only these, may match the box's wildcards: anything
# else would reach the box without the checks below.
for name in hexgate-prod hexgate-staging hexgate-zones-prod hexgate-zones-staging; do
    if [[ ! -f "$here/$name.conf" ]]; then
        echo "$name.conf is missing" >&2
        exit 1
    fi
done
for f in "$here"/hexgate-prod*.conf "$here"/hexgate-staging*.conf "$here"/hexgate-zones-prod*.conf "$here"/hexgate-zones-staging*.conf; do
    case "$(basename "$f")" in
        hexgate-prod.conf | hexgate-staging.conf | hexgate-zones-prod.conf | hexgate-zones-staging.conf) ;;
        *) echo "$(basename "$f") matches a box include but is not one of the checked files" >&2; exit 1 ;;
    esac
done

check_stage prod 7000
check_stage staging 7200

for name in hexgate hexgate-zones; do
    # Both stages' zones share one http context on the box, so each name
    # carries its own stage's suffix.
    for stage in prod staging; do
        if directives "$here/$name-$stage.conf" | grep -oE 'zone=[A-Za-z0-9_]+' | grep -vE "_${stage}\$"; then
            echo "$name-$stage.conf: every zone name must end in _$stage" >&2
            exit 1
        fi
    done
    if wrong_stage "$here/$name-prod.conf" 72 staging || wrong_stage "$here/$name-staging.conf" 70 prod; then
        exit 1
    fi
    if ! diff <(directives "$here/$name-prod.conf") <(as_prod "$here/$name-staging.conf"); then
        echo "$name-prod.conf and $name-staging.conf differ beyond their ports and zone names" >&2
        exit 1
    fi
done
echo "prod and staging match, ports and zone names aside"

# The box: one http context, conf.d's zones wildcards for both stages, and
# each stage's server block with its wildcard include.
box="$tmp/box.conf"
cat >"$box" <<EOF
$(stub_head box)
    include "$here/hexgate-zones-prod*.conf";
    include "$here/hexgate-zones-staging*.conf";
    server {
        listen 127.0.0.1:8080;
        server_name prod.hexgate.test;
        include "$here/hexgate-prod*.conf";
        location / { proxy_pass http://127.0.0.1:7000; }
    }
    server {
        listen 127.0.0.1:8080;
        server_name staging.hexgate.test;
        include "$here/hexgate-staging*.conf";
        location / { proxy_pass http://127.0.0.1:7200; }
    }
}
EOF
nginx -t -q -p "$tmp" -e "$tmp/box-error.log" -c "$box"
echo "nginx -t: both stages together, as the box includes them ok"

# The directives inside one of prod's location blocks, by its exact header.
location_block() {
    directives "$here/hexgate-prod.conf" \
        | awk -v head="location $1 {" '$0 == head {inside=1; next} inside && /^}/{inside=0} inside'
}

# Each route's upstream on prod (staging follows through the drift check):
# 7000 api, 7001 collector, 7002 MCP server. A location missing from this
# table fails, so a new route has to say where it goes.
routes=("= /v1/traces 7001" "^~ /mcp 7002" "= /.well-known/oauth-protected-resource/mcp 7002"
        "= /.well-known/oauth-authorization-server 7000" "= /.well-known/jwks.json 7000" "^~ /v1/oauth/ 7000")
while IFS= read -r header; do
    known=""
    for route in "${routes[@]}"; do
        [[ "location ${route% *} {" == "$header" ]] && known=1
    done
    if [[ -z "$known" ]]; then
        echo "hexgate-prod.conf: '$header' is not in check.sh's route table" >&2
        exit 1
    fi
done < <(directives "$here/hexgate-prod.conf" | grep -E '^location ')
for route in "${routes[@]}"; do
    port="${route##* }"
    match="${route% *}"
    if ! location_block "$match" | grep -qxF "proxy_pass http://127.0.0.1:$port;"; then
        echo "location $match does not proxy_pass to 127.0.0.1:$port on prod" >&2
        exit 1
    fi
done

traces_block="$(location_block "= /v1/traces")"

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
