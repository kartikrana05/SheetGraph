#!/usr/bin/env bash
# Superseded by deploy.sh.
#
# This script assumed the Zerops project already existed. It did not — which is
# why it failed with "Service [graph] not found" while scoped to an unrelated
# project. deploy.sh creates the project first.
printf '\033[33m\n  deploy-api.sh has been replaced by deploy.sh, which also creates\n'
printf '  the Zerops project and pushes to GitHub.\n\n  Running ./deploy.sh instead...\n\033[0m\n'
exec "$(dirname "$0")/deploy.sh" "$@"
