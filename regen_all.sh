#!/bin/bash
# Regenerates every flowgraphs/*.grc into its .py, then runs preflight.py.
# Run this after editing any .grc, or after add_satellite.py generates a
# new one - "forgot to grcc" has been a real, repeated source of confusion.
#
# With radios.yaml (multi-station mode) this acts on ONE station:
#     ./regen_all.sh --radio mini        (or be asked)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v grcc &> /dev/null; then
    echo "grcc not found - install GNU Radio Companion first." >&2
    exit 1
fi

# Pick the station and cd into it. Prints nothing at all in a classic
# single-folder setup. Assigned on its own line, then eval'd, on purpose:
# under set -e a failure inside eval "$(...)" is silently swallowed, and
# we'd carry on compiling in the wrong folder.
station_env="$(python3 "$SCRIPT_DIR/station.py" --shell "$@")" || exit 1
eval "$station_env"

shopt -s nullglob
grcs=(flowgraphs/*.grc)
if [ ${#grcs[@]} -eq 0 ]; then
    echo "No .grc files in $(pwd)/flowgraphs - nothing to generate."
    exit 0
fi

for grc in "${grcs[@]}"; do
    outdir="$(dirname "$grc")"
    echo "Generating ${grc%.grc}.py ..."
    grcc -o "$outdir" "$grc"
done

echo
echo "Running preflight checks..."
python3 "$SCRIPT_DIR/preflight.py"
