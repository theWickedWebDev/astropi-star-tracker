curl -X PUT http://10.0.0.200:8765/settings \
    -H 'Content-Type: application/json' \
    -d '{"'$1'":"'$2'"}' \
    | python -m json.tool \
    | pygmentize -l javascript