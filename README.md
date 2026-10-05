# LenzContext

LenzContext analyzes JPEGs and writes their context in input order to **one YAML
file**. It extracts EXIF capture time and GPS locally, resolves approximate
addresses with a local GeoNames SQLite database, and asks a configurable vision
LLM for an English description, a description in the requested language,
original-language OCR, and screenshot probability.

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

`curl`, `unzip`, and the project environment are required to download and prepare
the GeoNames dataset. Run the setup script to download the data and create the
SQLite database at `data/geonames.db`:

```bash
bash geonames_setup.sh
```

Downloaded files are kept in `data/geonames-src/`.

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
For options with a CLI flag, precedence is **CLI flag > environment variable >
`.env` > built-in default**. Only the selected value is validated, so a CLI flag
can override an invalid environment value.

| Variable | Meaning | Default |
| --- | --- | --- |
| `LENZCONTEXT_API_BASE` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `LENZCONTEXT_API_KEY` | Bearer token; may be empty for local servers | Empty |
| `LENZCONTEXT_MODEL` | Vision-capable model identifier | Required |
| `LENZCONTEXT_GEONAMES_DB` | Default for `--geonames-db` | `data/geonames.db` |
| `LENZCONTEXT_PROMPT_CONFIG` | Default for `--prompt-config` | Automatic prompt discovery |
| `LENZCONTEXT_DESCRIPTION_LANGUAGE` | Default for `--description-language` | `English` |
| `LENZCONTEXT_TIMEOUT` | Default for `--timeout`, in seconds | `120` |
| `LENZCONTEXT_RETRIES` | Default for `--retries`, maximum requests per image | `5` |
| `LENZCONTEXT_JOBS` | Default for `-j/--jobs`, maximum simultaneous LLM analyses | `1` |

Paths are relative to the working directory. Leave `LENZCONTEXT_PROMPT_CONFIG`
unset to retain automatic prompt discovery. Empty path or language values are
configuration errors. Timeout must be positive and finite; retries must be a
non-negative integer (`0` retries indefinitely).
Jobs must be a positive integer; `1` processes images sequentially.

For example, add these defaults to `.env`:

```dotenv
LENZCONTEXT_GEONAMES_DB=data/geonames.db
LENZCONTEXT_PROMPT_CONFIG=config/prompts.yaml
LENZCONTEXT_DESCRIPTION_LANGUAGE=Japanese
LENZCONTEXT_TIMEOUT=90
LENZCONTEXT_RETRIES=2
```

`lenzcontext IMG_001.jpg --description-language English --timeout 120` overrides
the language and timeout while using the other configured defaults.

The adapter appends `/chat/completions` to the base URL. A host-only URL gets
`/v1` added; an explicit path is preserved. Requests contain a base64 JPEG data URL,
the configured prompts, and the available address and capture time. HTTP redirects
are rejected so the request stays at the configured endpoint.

By default, JSON output instructions provide compatibility with servers that do
not support `response_format`. Use `--structured-output` to request strict JSON
Schema. When reasoning controls are omitted, an HTTP 400/404/415/422 rejection
can use a retry without `response_format`. When reasoning controls
are configured, these statuses instead report a compatibility error without retrying
or removing any controls, since the rejection may concern the reasoning settings.
Malformed JSON/schema violations and transient connection,
429, or 5xx errors share a limit of **five requests per image by default**
(the initial request plus up to four retries). `--retries N` sets the maximum
total request count: `1` sends only the initial request, `5` sends up to five,
and `0` retries eligible failures indefinitely. All failure types share the
same limit. Authentication errors do not retry.
All responses undergo strict Pydantic validation, including
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

# Up to four simultaneous LLM analyses, writing results in input order:
lenzcontext sample/*.jpg --jobs 4 -o result.yaml

# Recursively process JPEGs in directories with four simultaneous analyses:
lenzcontext -R sample --jobs 4 -o result.yaml

# Describe images in Japanese as well as English:
lenzcontext IMG_001.jpg --description-language Japanese -o result.yaml

# Add another batch to an existing output file (or create it if absent):
lenzcontext more/*.jpg -a result.yaml

# Skip images already in an output and append only new analyses:
lenzcontext -R sample --resume result.yaml --jobs 4

# Custom database, prompts, structured output, timeout, and retries:
lenzcontext *.jpg --geonames-db data/custom.db \
  --prompt-config config/prompts.yaml --structured-output --timeout 90 \
  --retries 2 -o result.yaml

# Print the version (-v):
lenzcontext -v

# DEBUG diagnostics on stdout (-V):
lenzcontext IMG_001.jpg -V
```

The version and verbosity flags differ only by case: `-v/--version` prints the
version and exits, while `-V/--verbose` enables DEBUG logging for the run.
`-o FILE`, `-a FILE`, and `--resume FILE` are mutually exclusive. Without them, the output is
`lenzcontext.yaml`.
`--timeout` applies to each request attempt. A series of timeouts can therefore
take much longer than one timeout period, especially with `--retries 0`.
`--description-language` accepts a language name and defaults to `English`.
When English is selected, the prompt asks the model to make `description`
identical to `description_en`.

Each successful image is written to the YAML file as soon as all earlier inputs
have finished or been skipped. To
watch an existing output while the batch runs, use `tail -f lenzcontext.yaml`;
`tail -F lenzcontext.yaml` also waits for the file to appear. When using `-o`
or the default output, the first success replaces the existing file. With `-a`,
the first success creates the file if needed or adds a record to an existing
LenzContext YAML file. Append mode checks the file's format before any image is
processed; manually edited or malformed files are rejected. Repeated file names
are appended as separate records.

`--resume FILE` reads the existing generated YAML and skips inputs whose
`file.name` is already recorded, then appends successful new analyses. A missing
or empty file is created on the first success. If every input is already recorded,
the command exits successfully without modifying the file or calling the LLM.
Both recorded names and inputs are normalized to absolute paths relative to the
current working directory (not the YAML file's directory). Thus `sample/A.jpg`,
`./sample/A.jpg`, and an equivalent absolute path match; `..` and repeated path
separators are normalized. Symbolic links are not resolved. Output names keep
the original input spelling. The name set is fixed at startup: repeated new
inputs are still analyzed and appended separately, just as with `-a`.
Existing YAML is validated one record at a time, retaining only the name set;
startup time scales with the size of the existing YAML. Malformed or manually
edited files are rejected before analysis.

`-j N` / `--jobs N` controls simultaneous LLM analyses, including retries; it
defaults to `1`. All job counts use the same worker-pool implementation, including
a single worker for `--jobs 1`. Image preparation, address lookup, and YAML writes
run on the caller thread; LLM analysis runs on workers.
The INFO log `processing FILE (Ns)` reports approximate LLM analysis time,
including retries, measured inside the worker on success or failure. It excludes
image preparation, address lookup, output writes, and result collection delays.
Timing logs appear as workers finish; YAML records remain in input order.
Set `LENZCONTEXT_JOBS=4` in `.env` for a persistent default,
or override it per run with `--jobs`. JPEG validation, EXIF extraction, local
address lookup, and YAML writes run on the calling thread. Images are prepared
as workers become available, with at most `2 × jobs` inputs running or waiting
for ordered output. An earlier slow image may delay writing later results;
when this limit is reached, preparation waits for ordered output to advance.
Completed records retained in the returned batch still use memory proportional
to the number of successful images.

Four workers improved batch throughput in the local sample measurements;
larger values can increase individual-image latency without improving throughput.
Choose the value for your endpoint. A write failure or Ctrl-C stops new work
and further retries and cancels tasks that have not started. Already-running
HTTP requests must finish or time out before shutdown completes. Ctrl-C exits
with code `130`; records already written remain in the file. Each request
attempt retains the configured timeout and each image retains its own retry limit.

Shell globs are expanded by the shell; explicit arguments control ordering.
With `-R` / `--recursive`, arguments are directories. All `.jpg` and `.jpeg`
files inside them are collected recursively, including through symbolic links,
in directory traversal order. Multiple directories can be supplied; file and
directory arguments are not mixed in this mode.
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
- `${description_language}` becomes the language named by `--description-language`.
- Missing metadata becomes an empty string; no `unknown` labels are added.
- Unknown placeholders and malformed `$` syntax are configuration errors.
- Use `$$` for a literal dollar sign.

Keep JSON output instructions in custom prompts, especially when using the default
compatibility mode. Custom prompts must request both `description_en` and
`description` in the JSON response. The default prompts ask for a 120–180
character English description (a target, not a truncation rule), preserve OCR
spelling/script with single-space separators, and prevent unsupported location
claims based only on supplied context.
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
      description: 歩行者と明かりのついた店が並ぶにぎやかな通り。
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
      description: 白い皿に盛られた料理と、木製のテーブルに置かれた飲み物。
      ocr:
        detected: false
        text: ''
      screenshot_probability: 0.01
```

The example shows output with `--description-language Japanese`.

OCR text is normalized after string type validation: consecutive whitespace
(including LF/CRLF, tabs, full-width spaces, and nonbreaking spaces) becomes one
ASCII space (`0x20`), and leading/trailing whitespace is removed. Other characters
are preserved. Whitespace-only text becomes empty; `detected: true` with empty
text remains a validation error. This normalization applies to the stored OCR
value, not YAML formatting or the raw model response shown in verbose logs.

PyYAML writes Unicode directly. `file.name` preserves the input path exactly as
passed on the command line, including directory components. Each complete record is rendered in memory and written to the same
output file, then closed so it appears in `tail`. If the process is forcibly
stopped during a write, the last record may be incomplete. The output directory
must already exist.

## Errors and logging

All console messages, including logs and argument errors, go to stdout; results go only to the YAML file. Non-JPEG and unreadable inputs
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

- Exit `0`: at least one successful record was written, even if others failed,
  or every input was skipped as already recorded with `--resume`.
- Exit `1`: no successful JPEGs, or writing output failed.
- Exit `2`: invalid arguments/configuration, invalid append/resume target, or output
  colliding with an input/DB.
- Exit `130`: processing interrupted with Ctrl-C.

When nothing succeeds, an existing output file is left unchanged and a new one
is not created. Successful records preserve input order; failed inputs are
omitted. A write error stops the batch; records already written remain in the
output file.

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
prompt validation, JSON validation/retry/fallback, bounded parallel execution,
ordered output, cancellation, batch recovery, and YAML output.

## Project layout

```text
lenzcontext/
├── __init__.py, __main__.py, cli.py
├── config.py, models.py, exif.py, pipeline.py, batch.py, output.py
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

The LenzContext application source code is licensed under the MIT License; see
[`LICENSE`](LICENSE). This license applies to the application code and its
documentation. GeoNames datasets are separate, are not included in this repository,
and remain subject to their own attribution terms.

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
