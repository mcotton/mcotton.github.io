#!/bin/sh
flask sync-apps
flask generate-feed
exec "$@"
