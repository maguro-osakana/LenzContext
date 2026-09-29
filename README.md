# LenzContext

LenzContext analyzes JPEGs in input order and writes their context to **one YAML
file**. It extracts EXIF capture time and GPS locally, resolves approximate
addresses with a local GeoNames SQLite database, and asks a configurable vision
LLM for an English description, original-language OCR, and screenshot probability.

## Requirements and installation

Python **3.11+**, SQLite with RTree support (included in typical Python builds),
and a vision-capable OpenAI-compatible Chat Completions endpoint are required.
Runtime dependencies are **Pillow, Pydantic 2, PyYAML, and python-dotenv**. HTTP, SQLite, CLI
parsing, and prompt templates use the standard library.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Both `lenzcontext` and `python -m lenzcontext` run the CLI. There is no hardcoded
model choice and no real API call is needed to install or run tests.

## GeoNames dataset setup

Download these files from the [GeoNames dump directory](https://download.geonames.org/export/dump/)
into `data/geonames-src/`:

- `allCountries.zip`
- `alternateNamesV2.zip`
- `admin1CodesASCII.txt`
- `admin2Codes.txt`
- `countryInfo.txt`

For example, the existing `bash geonames_download.sh` helper downloads these files
and extracts the ZIPs (requires `curl` and `unzip`). The importer also reads ZIP
members directly, so extraction is optional. When both are present it uses the
extracted `.txt`; keep extracted files in sync when refreshing the downloads.

```bash
python -m lenzcontext.geonames.importer data/geonames-src
# Optional destination or explicit replacement:
python -m lenzcontext.geonames.importer data/geonames-src -o data/custom.db
python -m lenzcontext.geonames.importer data/geonames-src --overwrite
```

The default database is `data/geonames.db`. A full import needs several GB of disk
space and can take several minutes; the downloaded files remain separate. Import
streams records in bounded batches, builds indexes, then atomically publishes the
database. A failed import does not replace an existing database. Re-import after
updating the source dumps. Generated databases and source dumps are excluded from
Git; they are not bundled with the Python package.

At runtime the database is read-only. RTree bounding boxes narrow candidates,
then Haversine distance ranks populated places. Distance takes precedence over
feature-code and population tie breakers. Historical, abandoned, and destroyed
populated places (`PPLH`, `PPLQ`, `PPLW`) are excluded. Searches expand to 100 km;
no result within this range yields `address: null`. Dateline and polar queries
are supported.

Addresses include the selected place, its available administrative levels 1–4,
and country, with duplicate names removed. Parent relationships come from the
place's administrative codes, **not guessed containment from nearby centroids**.
GeoNames is a point gazetteer, not an address or administrative-boundary database:
it cannot guarantee district membership or street-level accuracy. For example,
a Seoul record lacking an admin2 code may yield only Seoul and country, while a
neighborhood with a Jung-gu parent includes that district.

English names use `en` alternates; local names use the first language in
`countryInfo.txt`, then its base language (e.g. `ko-KR` → `ko`). Preferred
alternates win, then other alternates, then the GeoNames name and ASCII name.
Historical and colloquial alternate names are omitted during import. Missing
local names may therefore fall back to Latin script. Japanese, Korean, and Chinese
local addresses use broad-to-narrow order; other languages use place-to-country.

## LLM configuration

```bash
cp .env.sample .env
```

Edit `.env` with your endpoint, model, and API key:

```dotenv
LENZCONTEXT_API_BASE=http://localhost:8000/v1
LENZCONTEXT_MODEL=your-vision-model
LENZCONTEXT_API_KEY=your-provider-key
```

The CLI automatically reads `.env` from the **current working directory**; no
`export` or `source .env` is needed. Existing environment variables take precedence.
The file is optional, so environment-only configuration still works. Quoted values
and comments are supported; `${...}` expansion is disabled to preserve literal
API key contents. `.env` is ignored by Git; `.env.sample` contains only examples.

| Variable | Meaning | Default |
| --- | --- | --- |
| `LENZCONTEXT_API_BASE` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `LENZCONTEXT_API_KEY` | Bearer token; may be empty for local servers | Empty |
| `LENZCONTEXT_MODEL` | Vision-capable model identifier | Required |

The adapter appends `/chat/completions` to the base URL. A host-only URL gets
`/v1` added; an explicit path is preserved. Requests contain a base64 JPEG data URL,
the configured prompts, and the available address and capture time. HTTP redirects
are rejected so the request stays at the configured endpoint.

By default, JSON output instructions provide compatibility with servers that do
not support `response_format`. Use `--structured-output` to request strict JSON
Schema. If the server rejects it with HTTP 400/404/415/422, the one allowed retry
omits `response_format`. Malformed JSON/schema violations and transient connection,
429, or 5xx errors also receive at most **one retry total per image**. Authentication
errors do not retry. All responses undergo strict Pydantic validation, including
finite screenshot probability in `[0, 1]` and consistent OCR detection/text.

The request formats follow the official OpenAI documentation for
[vision inputs](https://developers.openai.com/api/docs/guides/images-vision) and
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Provider support for these features varies.

## Usage

```bash
# Single file, default output lenzcontext.yaml:
lenzcontext IMG_001.jpg

# Batch, preserving the exact argument order; PNG is warned about and skipped:
lenzcontext C.jpg A.JPEG screenshot.png B.jpg -o result.yaml

# Custom database, prompts, structured output, and timeout:
lenzcontext *.jpg --geonames-db data/custom.db \
  --prompt-config config/prompts.yaml --structured-output --timeout 90 -o result.yaml
```

Shell globs are expanded by the shell; explicit arguments control ordering.
JPEG extensions are case-insensitive. Pillow verifies that the content actually
decodes as JPEG, including rejecting PNG content renamed to `.jpg`. Images are
never resized, rotated, cropped, re-encoded, enhanced, or converted before sending.

Capture time priority is `DateTimeOriginal`, `DateTimeDigitized`, then `DateTime`.
Invalid dates are skipped. A valid `OffsetTimeOriginal` is appended when present;
otherwise the time remains naive. No timezone is inferred from GPS and no filesystem
timestamp is used. Missing or invalid GPS produces null coordinates.

## Prompts

Edit **`config/prompts.yaml`**, or pass `--prompt-config my-prompts.yaml`. Both
`vision.system` and `vision.user` must be nonempty strings. With no override, the
CLI looks in the working directory, the source checkout (editable installs), then
the installed `share/lenzcontext/config/` directory. Explicit overrides must exist.

Templates use standard-library `string.Template`:

- `${address}` becomes `Address:` followed by `English:` and `Local:` lines.
- `${taken_at}` becomes `Capture time: ...`.
- Missing metadata becomes an empty string; no `unknown` labels are added.
- Unknown placeholders and malformed `$` syntax are configuration errors.
- Use `$$` for a literal dollar sign.

Keep JSON output instructions in custom prompts, especially when using the default
compatibility mode. The default prompts ask for a 120–180 character English
description (a target, not a truncation rule), preserve OCR spelling/script/line
breaks, and prevent unsupported location claims based only on supplied context.
Screenshot estimates primarily use visible UI features, not missing EXIF.

## Output

```yaml
version: 1
images:
  - file:
      name: IMG_001.jpg
    exif:
      taken_at: '2026-04-12T14:23:11+09:00'
      latitude: 37.5665
      longitude: 126.978
    address:
      english: Jung-gu, Seoul, South Korea
      local: 대한민국 서울특별시 중구
    analysis:
      description_en: A busy street with pedestrians and illuminated storefronts.
      ocr:
        detected: true
        text: |-
          서울역
          Welcome to Seoul
      screenshot_probability: 0.02
  - file:
      name: IMG_002.jpg
    exif:
      taken_at: null
      latitude: null
      longitude: null
    address: null
    analysis:
      description_en: Food on a white plate beside a drink on a wooden table.
      ocr:
        detected: false
        text: ''
      screenshot_probability: 0.01
```

PyYAML writes Unicode directly and preserves multiline OCR. File names are base
names; paths are not included. Output is replaced atomically after processing.
The output directory must already exist.

## Errors and logging

Logs go to stderr; results go only to the YAML file. Non-JPEG and unreadable inputs
are warned about and skipped. Missing EXIF, GPS, capture time, or an address is
normal. An unavailable database produces a warning and analysis continues without
addresses. API failures are reported per image and remaining inputs continue.
Provider response bodies and API keys are never logged.

- Exit `0`: at least one successful record was written, even if others failed.
- Exit `1`: no successful JPEGs, or writing output failed.
- Exit `2`: invalid arguments/configuration or output colliding with an input/DB.

When nothing succeeds, an existing output file is left unchanged. Successful
records preserve input order; failed inputs are omitted.

## Privacy

GPS coordinates are used locally for GeoNames reverse geocoding. Raw latitude and
longitude are not sent to the LLM **as separate fields or prompt context**. Only
the generated address, capture time, and JPEG image are sent to the configured
LLM endpoint, together with the configured analysis instructions.

**The JPEG is sent byte-for-byte unchanged. Embedded EXIF (including GPS and other
metadata) therefore travels inside the image file.** Preserving original bytes
and guaranteeing that no embedded GPS leaves the machine are incompatible. If
embedded metadata must not leave the machine, prepare a sanitized copy before
using this tool, or use a trusted local LLM endpoint. LenzContext itself does not
strip EXIF or preprocess the JPEG. The final local YAML intentionally contains GPS.

## Tests

```bash
python -m pytest -q
```

Tests generate small JPEGs and GeoNames fixtures, mock LLM responses, and require
neither real API keys nor the full GeoNames dataset. They cover metadata priority,
hemispheres, JPEG validation, administrative/language resolution, spatial edges,
prompt validation, JSON validation/retry/fallback, batch recovery, and YAML output.

## Project layout

```text
lenzcontext/
├── __init__.py, __main__.py, cli.py
├── config.py, models.py, exif.py, pipeline.py, output.py
├── geonames/
│   └── __init__.py, importer.py, database.py, reverse.py, language.py
└── llm/
    └── __init__.py, base.py, openai_compatible.py
config/prompts.yaml
tests/
data/geonames.db       # generated, ignored
pyproject.toml
README.md
```

## GeoNames attribution

Geographic data is provided by [GeoNames](https://www.geonames.org/), licensed under
[Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
See [GeoNames about/licensing](https://www.geonames.org/about.html) and the
[dump documentation](https://download.geonames.org/export/dump/readme.txt).
GeoNames data is supplied without warranties of accuracy or completeness.

When sharing or distributing the database or derived geographic content, credit
GeoNames, link to the license, and indicate modifications. LenzContext converts
the dumps to SQLite, filters non-address alternate names, and assembles approximate
addresses from selected place and administrative records. Source datasets and
generated databases are maintained separately from the application repository.
