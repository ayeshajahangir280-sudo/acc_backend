#!/bin/sh
# Kept for existing start-command settings; the real logic lives in entrypoint.sh.
exec sh ./entrypoint.sh "$@"
