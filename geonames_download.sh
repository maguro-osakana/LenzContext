#!/bin/bash
set -x

mkdir -p data/geonames-src
cd data/geonames-src

curl -O https://download.geonames.org/export/dump/allCountries.zip
curl -O https://download.geonames.org/export/dump/alternateNamesV2.zip
curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
curl -O https://download.geonames.org/export/dump/admin2Codes.txt
curl -O https://download.geonames.org/export/dump/countryInfo.txt

unzip allCountries.zip
unzip alternateNamesV2.zip
