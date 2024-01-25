#!/usr/bin/env bash

# To build for Raspberry PI
# ./docker/build.sh --rpi

# To build for testing
# ./docker/build.sh

while [ $# -gt 0 ]; do

   if [[ $1 == *"--"* ]]; then
        v="${1/--/}"
        declare $v="$2"
   fi

  shift
done

if [ -z ${rpi+x} ]; then
    docker build --file ./docker/dockerfile --tag astropi .
else
    # On Raspberry Pi
    docker build --file ./docker/dockerfile --build-arg RPI=true --tag astropi .
fi