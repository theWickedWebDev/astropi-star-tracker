#!/usr/bin/env bash
# --privileged for gpio access
docker rm astropi
docker run --detach --privileged --name astropi --net=host astropi