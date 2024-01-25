#!/usr/bin/env bash

# Made this script so that it was easier
# to run poetry install from the Dockerfile
# with conditional logic - maybe there is an easier way?

while [ $# -gt 0 ]; do
   if [[ $1 == *"--"* ]]; then
        v="${1/--/}"
        declare $v="$2"
   fi
  shift
done

cmd=(poetry install --no-interaction --no-ansi)
if [[ "$rpi" == "true" ]]; then
    cmd+=(--with rpi)
fi

${cmd[@]}