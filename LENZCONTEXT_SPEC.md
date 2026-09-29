# LenzContext

Pythonで `LenzContext` というCLIツールを実装してください。

JPEG画像からEXIF情報と画像内容を解析し、複数画像の文脈情報を**1つのYAMLファイル**として出力するツールです。

設計上は以下を優先してください。

- 単純さ
- 保守性
- 外部依存の最小化
- バッチ処理
- LLMに不要な情報を渡さない
- GeoNamesによるReverse Geocodingを完全ローカルで実行する

Python 3.11+ を対象としてください。

---

# 1. CLI

複数ファイルを一度に指定できるようにする。

例:

```bash
lenzcontext IMG_001.jpg IMG_002.jpg IMG_003.jpeg
```

出力ファイルを指定できるようにする。

```bash
lenzcontext IMG_001.jpg IMG_002.jpg -o result.yaml
```

出力ファイルを省略した場合:

```text
lenzcontext.yaml
```

を使用する。

CLI例:

```bash
lenzcontext \
  IMG_001.jpg \
  IMG_002.jpeg \
  screenshot.png \
  IMG_003.jpg \
  -o result.yaml
```

この場合 `screenshot.png` はJPEGではないので、

```text
WARNING: skipping non-JPEG file: screenshot.png
```

のようにstderrへ警告を出してスキップする。

他のJPEGについては処理を継続する。

---

# 2. JPEG判定

対象はJPEGのみ。

以下をJPEGとして受け付ける。

```text
.jpg
.jpeg
```

拡張子比較はcase-insensitiveにする。

例:

```text
.JPG
.JPEG
```

も受け付ける。

JPEG以外が指定された場合はfatal errorにしない。

```text
warn
↓
skip
↓
次のファイルを処理
```

とする。

可能であれば、拡張子だけではなくPillow等で実際のJPEG形式であることも確認する。

拡張子が `.jpg` でもJPEGとして読み込めない場合はそのファイルについてwarning/errorを出し、バッチ全体は継続する。

---

# 3. 全体処理フロー

各JPEGについて以下を行う。

```text
JPEG
 │
 ├─ EXIF解析
 │    ├─ GPS
 │    └─ 撮影日時
 │
 ├─ GPSが存在する場合
 │    ↓
 │  Local GeoNames DB
 │    ├─ 最寄り地名
 │    ├─ 行政区
 │    ├─ English address
 │    └─ Local-language address
 │
 └─ Vision対応OpenAI API互換LLM
      │
      ├─ JPEG画像
      ├─ address
      └─ capture time
             ↓
      ├─ description_en
      ├─ OCR
      └─ screenshot_probability
```

これを指定された全JPEGに対して繰り返し、結果を1つのYAMLファイルへまとめる。

---

# 4. 画像前処理

画像の前処理は行わない。

以下を行わないこと。

- resize
- rotate
- crop
- color conversion
- image enhancement
- OCR用画像変換

元JPEGをそのままLLMへ送信する。

---

# 5. EXIF解析

JPEGから以下を取得する。

## GPS

使用するEXIF:

```text
GPSLatitude
GPSLatitudeRef
GPSLongitude
GPSLongitudeRef
```

decimal degreesへ変換する。

例:

```yaml
latitude: 37.5665
longitude: 126.978
```

以下を正しく扱う。

```text
N
S
E
W
```

GPSが存在しない場合:

```yaml
latitude: null
longitude: null
```

とする。

---

# 6. 撮影日時

以下の優先順位で取得する。

```text
1. DateTimeOriginal
2. DateTimeDigitized
3. DateTime
```

`OffsetTimeOriginal` が存在する場合はtimezone offsetを付加する。

例:

```text
2026-04-12T14:23:11+09:00
```

timezone情報がない場合:

```text
2026-04-12T14:23:11
```

のまま保持する。

GPSからtimezoneを推測しない。

OSのファイル作成日時・更新日時などを撮影日時として使用しない。

EXIF撮影日時が存在しない場合:

```yaml
taken_at: null
```

とする。

---

# 7. Reverse Geocoding

外部Geocoding APIは使用しない。

GeoNamesのデータセットをローカルに保持して使用する。

使用データ:

```text
allCountries.zip
alternateNamesV2.zip
admin1CodesASCII.txt
admin2Codes.txt
countryInfo.txt
```

GeoNamesのTSVを実行時に毎回直接検索しない。

事前にSQLite DBへimportする。

例:

```text
data/
└── geonames.db
```

GeoNames DB生成用コマンドを実装する。

例:

```bash
python -m lenzcontext.geonames.importer /path/to/geonames-data
```

---

# 8. GeoNames DB

SQLiteを利用する。

必要に応じてSQLite RTreeを使って位置検索を高速化する。

概念的には以下のデータを保持する。

```text
geonames

geoname_id
name
ascii_name
latitude
longitude
feature_class
feature_code
country_code
admin1_code
admin2_code
admin3_code
admin4_code
population
```

alternate name:

```text
alternate_names

geoname_id
language
name
preferred
```

country:

```text
countries

country_code
name
languages
...
```

行政区情報もGeoNamesデータから解決できるようにする。

---

# 9. Reverse Geocoding検索

GPS座標から周辺候補を検索する。

まずRTree等で周辺候補を絞り、その後Haversine距離を使用する。

基本的には、

```text
feature_class = P
```

のpopulated placeを主要候補とする。

順位付けでは、

```text
1. distance
2. feature code
3. population
```

を考慮する。

最優先は距離。

populationを強く優先して、近くの町を無視して遠方の大都市を返してはいけない。

---

# 10. address

単純さを優先して、フィールド名は

```text
address
```

とする。

`approximate_location` という名称は使用しない。

ただしGeoNamesなので、番地レベルではなく市区町村・行政区レベルのおおよその住所である。

住所には可能な範囲で以下を含める。

```text
place
district / administrative division
region
country
```

不要な重複は除去する。

---

# 11. addressの例

韓国ソウルの場合、単に

```text
Seoul, South Korea
```

ではなく、利用可能であれば**区まで含める**。

例:

```yaml
address:
  english: "Jung-gu, Seoul, South Korea"
  local: "대한민국 서울특별시 중구"
```

日本の場合:

```yaml
address:
  english: "Shinjuku, Tokyo, Japan"
  local: "日本 東京都 新宿区"
```

GeoNamesに十分な行政区情報が存在しない場合は、取得できる粒度まででよい。

架空の行政区を補完しない。

---

# 12. English / Local name

English addressにはGeoNamesの英語alternate nameを使用する。

Local addressには現地言語の名称を使用する。

名称選択の基本優先順位:

```text
1. requested language + isPreferredName = 1
2. requested language alternate name
3. geoname.name
4. geoname.asciiname
```

英語:

```text
language = en
```

Local languageは `countryInfo.txt` のlanguages情報を利用する。

単純な固定辞書だけに依存しない。

複数公用語国については将来拡張できるよう、言語選択ロジックを分離する。

V1ではcountryInfo上の主要言語1つを採用してよい。

---

# 13. LLM

Vision対応のOpenAI API互換LLMを使用する。

特定OpenAIモデルにはハードコードしない。

以下を環境変数または設定で変更可能にする。

```text
LENZCONTEXT_API_BASE
LENZCONTEXT_API_KEY
LENZCONTEXT_MODEL
```

基本API:

```text
/v1/chat/completions
```

LLM provider依存コードはadapterとして分離する。

---

# 14. LLMへ送信する情報

LLMへ送るのは以下のみ。

```text
1. 元JPEG
2. Reverse Geocoding済みaddress
3. EXIF撮影日時
```

以下はLLMへ送らない。

```text
latitude
longitude
GeoNames ID
country code
feature class
feature code
population
admin code
その他のEXIF
```

緯度経度そのものは絶対にLLMへ送らない。

---

# 15. LLM PromptをYAML設定ファイルへ分離

LLMに与えるpromptをPythonコードへハードコードしない。

例えば:

```text
config/
└── prompts.yaml
```

に保存する。

YAML内でテンプレート変数を使用できるようにする。

テンプレート記法はPython標準ライブラリの:

```python
string.Template
```

互換とする。

したがって以下を使用する。

```text
${address}
${taken_at}
```

この記法はYAMLのblock scalar内で問題なく使用できる。

独自のJinja2等は導入せず、標準ライブラリ `string.Template` を使う。

---

# 16. prompts.yaml

例えば以下の構造とする。

```yaml
vision:
  system: |-
    You analyze JPEG images and return structured metadata.

    Follow the requested output schema exactly.

    Do not translate OCR text.

    The supplied address and capture time are contextual metadata only.
    Do not claim that an object, landmark, place, event, or condition is
    visible unless it is supported by the image itself.

  user: |-
    Analyze this image.

    Context:
    ${address}
    ${taken_at}

    Tasks:

    1. Write a concise English description of the visible image content.
       Target approximately 120-180 English characters.

    2. Extract readable text from the image.
       Preserve the original language and script.
       Do not translate or correct the text.
       Do not invent unreadable characters.

    3. Estimate the probability that this image is a screenshot.
       Return a value between 0.0 and 1.0.
```

---

# 17. Template変数の生成

`${address}` にはlabelを含めた完成済みcontextを渡す。

住所が存在する場合:

```text
Address:
English: Jung-gu, Seoul, South Korea
Local: 대한민국 서울특별시 중구
```

つまり:

```python
address_context = """Address:
English: Jung-gu, Seoul, South Korea
Local: 대한민국 서울특별시 중구"""
```

を `${address}` に代入する。

住所が存在しない場合:

```python
address_context = ""
```

とする。

同様に `${taken_at}` は、撮影日時が存在する場合:

```text
Capture time: 2026-04-12T14:23:11+09:00
```

とする。

存在しない場合:

```python
taken_at_context = ""
```

とする。

これにより、存在しないmetadataについて余計な:

```text
Address: unknown
Capture time: unknown
```

などをLLMへ渡さない。

テンプレート展開は例えば:

```python
from string import Template

prompt = Template(config["vision"]["user"]).substitute(
    address=address_context,
    taken_at=taken_at_context,
)
```

のように実装する。

---

# 18. 英文画像説明

LLMは画像内容について簡潔な英語説明を生成する。

目安:

```text
120-180 English characters
```

約150文字を目標とする。

位置情報や撮影日時は文脈理解の補助情報としてのみ使用する。

住所だけを根拠に画像内容を生成してはいけない。

例えばaddressが:

```text
Jung-gu, Seoul, South Korea
```

でも、画像から確認できない場合に:

```text
Seoul City Hall
Myeongdong
N Seoul Tower
```

などを勝手に記述してはいけない。

---

# 19. OCR

画像内の文字を抽出する。

ルール:

```text
元言語を維持する
翻訳しない
スペル修正しない
文字体系を維持する
可能な限り改行を維持する
読めない文字を推測しない
```

例:

画像:

```text
서울역
Welcome to Seoul
出口
```

出力:

```text
서울역
Welcome to Seoul
出口
```

とする。

OCR対象がない場合:

```yaml
detected: false
text: ""
```

とする。

---

# 20. Screenshot probability

画像がスクリーンショットである可能性を:

```text
0.0 - 1.0
```

で返す。

意味:

```text
0.0 = almost certainly a camera/photo image
1.0 = almost certainly a screenshot
```

視覚的特徴を主に判断材料とする。

例:

```text
OS status bar
navigation bar
browser chrome
application UI
buttons
menus
uniform digital typography
screen layout
```

EXIFが存在しないことだけを根拠にスクリーンショット判定してはいけない。

---

# 21. LLM出力schema

LLMから以下を取得する。

```yaml
description_en: "A busy urban intersection with pedestrians, cars and illuminated storefronts."
ocr:
  detected: true
  text: |-
    서울특별시
    CITY HALL
screenshot_probability: 0.02
```

Pydantic等でvalidationする。

`screenshot_probability`:

```text
0.0 <= value <= 1.0
```

を保証する。

---

# 22. Structured Output

OpenAI互換LLMがStructured Output / JSON Schemaをサポートしている場合は利用可能な設計にする。

ただしOpenAI以外の互換サーバーでも動作することを優先する。

fallback:

```text
JSON output instruction
↓
parse
↓
Pydantic validation
↓
失敗した場合は最大1回retry
```

とする。

LLMからJSONを取得し、それを最終的にLenzContext側でYAMLへ変換する。

LLM自身にYAMLを生成させる必要はない。

---

# 23. 最終出力

最終出力は**YAMLファイル**とする。

複数JPEGの解析結果を1つのYAMLにまとめる。

例:

```yaml
version: 1

images:
  - file:
      name: "IMG_001.jpg"

    exif:
      taken_at: "2026-04-12T14:23:11+09:00"
      latitude: 37.5665
      longitude: 126.978

    address:
      english: "Jung-gu, Seoul, South Korea"
      local: "대한민국 서울특별시 중구"

    analysis:
      description_en: >-
        A busy urban intersection with pedestrians, cars,
        illuminated storefronts and tall office buildings.

      ocr:
        detected: true
        text: |-
          서울특별시
          CITY HALL

      screenshot_probability: 0.02

  - file:
      name: "IMG_002.jpg"

    exif:
      taken_at: null
      latitude: null
      longitude: null

    address: null

    analysis:
      description_en: >-
        A close-up photograph of food served on a white plate
        beside a drink on a wooden table.

      ocr:
        detected: false
        text: ""

      screenshot_probability: 0.01
```

YAML生成はPyYAML等を使用してよい。

手動でYAML文字列を組み立てない。

Unicode文字をescapeせず、人間が読める形で出力する。

例えば:

```yaml
local: "대한민국 서울특별시 중구"
```

を:

```text
"\ub300\ud55c..."
```

のように不要にescapeしない。

---

# 24. Batch処理

入力ファイルは指定された順番で処理する。

出力YAMLの `images` も入力順を維持する。

例:

```bash
lenzcontext C.jpg A.jpg B.jpg
```

なら:

```yaml
images:
  - C.jpg
  - A.jpg
  - B.jpg
```

の順番とする。

---

# 25. Batch時のエラー処理

1ファイルの問題でバッチ全体を停止させないことを基本とする。

JPEG以外:

```text
warning
skip
continue
```

JPEGとして読めない:

```text
warning/error
skip
continue
```

EXIFなし:

```text
正常処理
```

GPSなし:

```text
正常処理
address = null
LLM解析を継続
```

撮影日時なし:

```text
正常処理
LLM解析を継続
```

Reverse Geocodingで住所が見つからない:

```text
address = null
LLM解析を継続
```

LLM API failureについてはエラーを明確に出す。

可能な限り他ファイルの処理は継続する。

最低でも1つ正常処理できた場合は、正常処理分のYAMLを書き出す。

正常処理できるJPEGが1つもなかった場合はnon-zero exit codeとする。

---

# 26. ログ

通常結果はYAMLファイルへ書く。

warning/error/logはstderrへ出力する。

API keyをログへ出力してはいけない。

例:

```text
INFO: processing IMG_001.jpg
WARNING: skipping non-JPEG file: screenshot.png
INFO: processing IMG_002.jpg
INFO: wrote 2 image records to result.yaml
```

過剰なログは不要。

---

# 27. プロジェクト構成

以下を目安とする。

```text
lenzcontext/
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
├── models.py
├── exif.py
├── pipeline.py
├── output.py
│
├── geonames/
│   ├── __init__.py
│   ├── importer.py
│   ├── database.py
│   ├── reverse.py
│   └── language.py
│
└── llm/
    ├── __init__.py
    ├── base.py
    └── openai_compatible.py

config/
└── prompts.yaml

data/
└── geonames.db

tests/
├── test_exif.py
├── test_geonames.py
├── test_prompt.py
├── test_models.py
├── test_pipeline.py
└── test_output.py
```

---

# 28. モジュール責務

## exif.py

```text
JPEG validation
GPS取得
decimal degrees変換
撮影日時取得
```

## geonames/importer.py

```text
GeoNames dump
↓
SQLite DB
```

## geonames/database.py

```text
SQLite connection
RTree
query
```

## geonames/reverse.py

```text
GPS
↓
最寄り地名
↓
行政区解決
↓
Address
```

## geonames/language.py

```text
English name
Local-language name
country language
alternateNames選択
```

## llm/openai_compatible.py

```text
OpenAI compatible Vision API
prompt template展開
image送信
structured response parse
validation
retry
```

## output.py

```text
複数ImageResult
↓
単一YAML
```

## pipeline.py

```text
JPEG
↓
EXIF
↓
GeoNames
↓
LLM
↓
ImageResult
```

## cli.py

```text
複数input
batch control
warning
output
exit code
```

---

# 29. データモデル

概念的には以下のPydantic modelを使用する。

```python
class ExifInfo(BaseModel):
    taken_at: str | None
    latitude: float | None
    longitude: float | None


class Address(BaseModel):
    english: str
    local: str


class OCRResult(BaseModel):
    detected: bool
    text: str


class AnalysisResult(BaseModel):
    description_en: str
    ocr: OCRResult
    screenshot_probability: float


class ImageResult(BaseModel):
    file: FileInfo
    exif: ExifInfo
    address: Address | None
    analysis: AnalysisResult


class BatchResult(BaseModel):
    version: int = 1
    images: list[ImageResult]
```

必要に応じて調整してよい。

---

# 30. Prompt設定ファイルの場所

promptファイルパスはデフォルト:

```text
config/prompts.yaml
```

とする。

将来変更できるようにCLIまたは設定でoverride可能な設計にしてよい。

例えば:

```bash
lenzcontext *.jpg --prompt-config my-prompts.yaml
```

をサポートしてよい。

ただし実装を不必要に複雑にしない。

---

# 31. Prompt template validation

起動時またはLLM呼び出し前に、少なくとも以下のplaceholderが正しく処理できることを確認する。

```text
${address}
${taken_at}
```

未知のplaceholderが存在した場合は、黙って無視せず設定エラーとする。

そのため `safe_substitute()` より原則:

```python
Template(...).substitute(...)
```

を使用する。

---

# 32. GeoNames attribution

READMEにGeoNamesの利用について明記する。

GeoNamesデータのライセンス・attribution要件をREADMEに記載する。

GeoNamesデータ自体をrepositoryへ直接含めるかどうかは別途判断できるようにする。

少なくとも以下をREADMEに説明する。

```text
GeoNamesデータの取得
必要ファイル
DB import方法
DB保存場所
ライセンス / attribution
```

---

# 33. README

最低限以下を記載する。

```text
LenzContextとは何か
requirements
installation
GeoNames dataset setup
GeoNames DB import
LLM configuration
environment variables
prompts.yaml
prompt placeholders
single-file example
batch example
YAML output example
error / warning behavior
privacy
GeoNames attribution
```

特にprivacyについて:

```text
GPS coordinates are used locally for GeoNames reverse geocoding.

Raw latitude and longitude are not sent to the LLM.

Only the generated address, capture time, and JPEG image are sent
to the configured LLM endpoint.
```

と明記する。

---

# 34. テスト

最低限以下をテストする。

```text
JPEG判定
.jpg
.jpeg
.JPG
.JPEG

非JPEG skip

GPS DMS → decimal degrees
N
S
E
W

DateTimeOriginal
DateTimeDigitized fallback
DateTime fallback
OffsetTimeOriginal

EXIFなし
GPSなし

GeoNames nearest place
admin region resolution
English alternate name
local alternate name

Jung-gu / Seoulのような行政区を含むaddress生成

${address} substitution
${taken_at} substitution
metadataなし時の空文字substitution
未知placeholderエラー

LLM JSON parse
LLM Pydantic validation
screenshot_probability validation

複数JPEG batch
入力順維持
非JPEG混在batch

YAML serialization
Unicode保持
複数画像を単一YAMLへ出力
```

LLM APIはmock可能にし、通常のunit testで実APIを必要としない設計にする。

---

# 35. 実装原則

以下を守る。

```text
Simple is better than complex.
```

特に:

- 不要なframeworkを導入しない
- DI frameworkを導入しない
- Jinja2をprompt templateのためだけに導入しない
- `string.Template` を使う
- GeoNamesはSQLiteで完結させる
- EXIF解析をLLMに任せない
- Reverse GeocodingをLLMに任せない
- GPS座標をLLMへ送信しない
- YAML出力をLLMに生成させない
- provider固有コードをpipelineへ混ぜない
- 1ファイルの失敗でbatch全体を必要以上に停止しない
- API keyをsource codeやlogへ出さない
- 型ヒントを使用する
- コードを小さい責務へ分離する

まず短い設計方針と採用予定ライブラリを示し、その後に実装してください。

実装後は、

```text
1. project tree
2.主要な設計判断
3. setup方法
4. GeoNames DB生成方法
5. CLI実行例
6. prompts.yaml変更方法
7. test実行方法
```

を簡潔に説明してください。

