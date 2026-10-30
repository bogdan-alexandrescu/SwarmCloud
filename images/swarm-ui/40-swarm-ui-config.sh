#!/bin/sh
# Writes /tmp/swarm-ui/config.json, which nginx.conf serves at /config.json,
# from the container's ENVIRONMENT (DAYONE-UIENV-1). nginx's entrypoint runs it
# as uid 101 before nginx starts; images/swarm-ui/Dockerfile copies it in, and
# says why it is a file and not a heredoc. The value is never echoed.
set -eu
dir=/tmp/swarm-ui
name=$(printf '%s' "${ENVIRONMENT:-}" | tr '[:upper:]' '[:lower:]')
case "$name" in
  "") ;;
  [!a-z0-9]* | *[!a-z0-9._-]*)
    echo "swarm-ui: ENVIRONMENT is not a plain environment name; /config.json declares none" >&2
    name="" ;;
esac
if [ "${#name}" -gt 63 ]; then
  echo "swarm-ui: ENVIRONMENT is longer than 63 characters; /config.json declares none" >&2
  name=""
fi
mkdir -p "$dir"
if [ -n "$name" ]; then
  printf '{"environment":"%s"}\n' "$name" > "$dir/config.json.tmp"
else
  printf '{}\n' > "$dir/config.json.tmp"
fi
mv "$dir/config.json.tmp" "$dir/config.json"
