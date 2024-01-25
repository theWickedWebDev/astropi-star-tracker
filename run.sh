#!/usr/bin/env bash
poetry_path="$(which poetry)"
/usr/bin/sudo -E /usr/bin/chrt -f 90 /usr/bin/sudo -E -u pi "$poetry_path" run -- python -m src.main