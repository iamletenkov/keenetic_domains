#!/bin/sh
# Снимает running-config с роутера Keenetic в .local/ (каталог не версионируется).
#
#   sh tools/fetch-config.sh
#   HOST=192.168.2.1 PORT=222 sh tools/fetch-config.sh
#
# Конфиг содержит приватные ключи WireGuard и пароли — в git он попасть не должен,
# поэтому кладётся только в .local/, который перечислен в .gitignore.
set -eu

HOST=${HOST:-192.168.2.1}
PORT=${PORT:-222}
LOGIN=${LOGIN:-root}

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:-"$root_dir/.local/running-config.txt"}
mkdir -p "$(dirname -- "$out")"

ssh -p "$PORT" -o BatchMode=yes "$LOGIN@$HOST" "ndmc -c 'show running-config'" \
	| tr -d '\r' > "$out"

printf 'снято строк: %s -> %s\n' "$(wc -l < "$out")" "$out"
