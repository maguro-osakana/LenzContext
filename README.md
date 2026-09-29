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
Schema. When reasoning controls are omitted, an HTTP 400/404/415/422 rejection
can use the one allowed retry without `response_format`. When reasoning controls
are configured, these statuses instead report a compatibility error without retrying
or removing any controls, since the rejection may concern the reasoning settings.
Malformed JSON/schema violations and transient connection,
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

# Print the version (-v):
lenzcontext -v

# DEBUG diagnostics on stderr (-V):
lenzcontext IMG_001.jpg -V
```

The version and verbosity flags differ only by case: `-v/--version` prints the
version and exits, while `-V/--verbose` enables DEBUG logging for the run.

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
description (a target, not a truncation rule), preserve OCR spelling/script with
single-space separators, and prevent unsupported location claims based only on supplied context.
Screenshot estimates primarily use visible UI features, not missing EXIF.

### Reasoning controls

The supplied configuration disables reasoning. Set `enabled: true` to enable
reasoning with the configured token budget:

```yaml
vision:
  reasoning:
    enabled: false
    token_budget: 512
  system: |-
    # Keep your existing system prompt here.
  user: |-
    # Keep your existing user prompt here.
```

Set `enabled: false` to disable reasoning; the token budget is then not applied.
Remove the entire `reasoning` section to preserve server defaults and send no
reasoning controls. Existing prompt files without this section remain supported.
`enabled` must be a YAML boolean (`true`/`false`). `token_budget` defaults to 512
and must be a positive integer, even when reasoning is disabled. Invalid settings
fail before any API request. The former `effort` and `token_budgets` fields are
no longer accepted; replace them with a single `token_budget` value.

The budget is a **reasoning token limit**, not a fixed token count or time limit.
With reasoning enabled, the adapter sends `reasoning_effort: low` and
`thinking_token_budget` with the configured limit. With reasoning disabled it
sends `reasoning_effort: none` and omits the budget. The use of `low` is deliberate:
it enables the thinking mode verified on the local DeepSeek deployment, while
the budget controls the token limit. Normal answer tokens are separate from the
reasoning budget.

This requires compatible server/model support. vLLM must support
[`thinking_token_budget` and reasoning mode control](https://docs.vllm.ai/en/latest/features/reasoning_outputs/)
with an appropriate reasoning parser/template. An HTTP success alone does not
guarantee a different server honored the settings; inspect verbose usage when
switching models. Rejected controls are never silently removed. Smaller budgets
can reduce latency but may affect OCR and description quality. The default
120-second request timeout still applies; use `--timeout` if needed for larger
budgets.

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
        text: 서울역 Welcome to Seoul
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

OCR text is normalized after string type validation: consecutive whitespace
(including LF/CRLF, tabs, full-width spaces, and nonbreaking spaces) becomes one
ASCII space (`0x20`), and leading/trailing whitespace is removed. Other characters
are preserved. Whitespace-only text becomes empty; `detected: true` with empty
text remains a validation error. This normalization applies to the stored OCR
value, not YAML formatting or the raw model response shown in verbose logs.

PyYAML writes Unicode directly. File names are base
names; paths are not included. Output is replaced atomically after processing.
The output directory must already exist.

## Errors and logging

Logs go to stderr; results go only to the YAML file. Non-JPEG and unreadable inputs
are warned about and skipped. Missing EXIF, GPS, capture time, or an address is
normal. An unavailable database produces a warning and analysis continues without
addresses. API failures are reported per image and remaining inputs continue.
API keys, `Authorization` headers, raw provider HTTP bodies, and image bytes are
never logged.

`-V/--verbose` raises the application log level to DEBUG (Pillow stays at WARNING,
because it dumps raw EXIF tags) and adds EXIF capture time and coordinates,
reverse-geocoding candidates and the selected place, rendered prompts, the LLM
request payload with the base64 image replaced by its byte length and SHA-256
prefix, and the assistant's returned text and reasoning (`reasoning`, with
`reasoning_content` as a compatibility fallback). Each request attempt logs its
reasoning settings and elapsed time, including failed attempts. Response usage
logs input (`prompt`), generated (`completion`), reasoning, and total tokens.
Completion tokens include reasoning tokens, so do not add those counts together.
Missing counts are `unknown`, not zero. Diagnostics are printed after the
non-streaming response completes; they are not live generation progress.
Reasoning and usage are also logged for received responses that fail validation.
Verbose logs can therefore contain GPS
coordinates and OCR text from the image; redact them before sharing.

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
strip EXIF or preprocess the JPEG. Even with `-V`, the JPEG bytes are never written
to the log; only the image size and a SHA-256 prefix are recorded. The final local
YAML intentionally contains GPS.

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
