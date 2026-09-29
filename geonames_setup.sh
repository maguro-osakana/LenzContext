#!/bin/bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output="${1:-data/geonames.db}"
source_dir="$script_dir/data/geonames-src"
mkdir -p "$source_dir"
cd "$source_dir"

curl --fail --location --continue-at - --remote-name https://download.geonames.org/export/dump/allCountries.zip
curl --fail --location --continue-at - --remote-name https://download.geonames.org/export/dump/alternateNamesV2.zip
curl --fail --location --continue-at - --remote-name https://download.geonames.org/export/dump/admin1CodesASCII.txt
curl --fail --location --continue-at - --remote-name https://download.geonames.org/export/dump/admin2Codes.txt
curl --fail --location --continue-at - --remote-name https://download.geonames.org/export/dump/countryInfo.txt

unzip -o -q allCountries.zip
unzip -o -q alternateNamesV2.zip

cd "$script_dir"
if [[ -x "$script_dir/.venv/bin/python" ]]; then
    python="$script_dir/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    python="$(command -v python)"
else
    python="$(command -v python3)"
fi
"$python" -m lenzcontext.geonames.importer "$source_dir" -o "$output"
