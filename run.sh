#!/usr/bin/env bash

# poetry shell
# poetry run -- python -m src.main

# poetry shell
poetry_path="$(which poetry)"
sudo -E chrt -f 90 sudo -E -u pi "$poetry_path" run -- python -m src.main

# poetry shell
# find . -name '*.py' | entr -cr python -m src.main
