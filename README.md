# PDFTranslate

英語PDFを日本語PDFへ翻訳し、元レイアウトをできるだけ維持して出力するスクリプト群です。

デフォルト翻訳エンジンは Google 翻訳です。
LM Studio のローカルLLMはオプションで、`--engine llm` 指定時のみ使用します。

## ファイル構成

- `extract.py`
  - PyMuPDFでPDFのテキストブロックを抽出
  - 各ブロックの位置/サイズ/フォント情報をJSON化

- `auto_translate.py`
  - 抽出JSONを読み込み、近接ブロックをsegment化
  - 既定で Google 翻訳、必要時に LM Studio (`/v1/chat/completions`) を利用
  - LLM時は既定で「1ページ単位の構造化JSONリクエスト」で翻訳
  - `--llm-translate-mode segment` で従来のsegment/block経路を使用可能
  - 結果を `<source_stem>_translated_text.json` に保存

- `generate.py`
  - 元PDFの同じ領域へ日本語テキストを再配置
  - macOSでは `Hiragino Sans GB.ttc` を優先利用

## 実行フロー

1. テキスト抽出
2. segment化
3. 翻訳エンジンで翻訳（デフォルト: Google）
4. レイアウト維持でPDF再生成

## 必要環境

- macOS
- Python 3.x
- Pythonライブラリ:
  - `PyMuPDF`

インストール例:

```bash
pip install pymupdf
```

## 事前準備

LLM翻訳を使う場合のみ、以下を準備してください。

1. LM Studioでモデルをロード
2. Local Serverを起動
3. OpenAI互換エンドポイントを確認（通常 `http://127.0.0.1:1234/v1`）

`.env` を使う場合:

```bash
cp .env.example .env
```

`.env` 例:

```dotenv
TRANSLATION_ENGINE=google
GOOGLE_TIMEOUT=30
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_MODEL=translategemma-4b-it
LMSTUDIO_TIMEOUT=0
LMSTUDIO_MAX_TOKENS=4096
LMSTUDIO_TEMPERATURE=0.2
LMSTUDIO_MAX_WORKERS=8
LLM_TRANSLATE_MODE=page
LLM_PAGE_MAX_CHARS=70000
LLM_PAGE_RETRIES=1
```

## 実行方法

### パイプライン一括実行（推奨）

```bash
python3 auto_translate.py <source_pdf> [output_pdf]
```

例:

```bash
python3 auto_translate.py input.pdf
# -> input_G.pdf

python3 auto_translate.py input.pdf translated.pdf
# -> translated.pdf

python3 auto_translate.py input.pdf --engine llm
# -> input_LLM.pdf
```

### 主なオプション

```bash
python3 auto_translate.py <source_pdf> [output_pdf] \
  --engine google \
  --google-timeout 30 \
  --lmstudio-base-url http://127.0.0.1:1234/v1 \
  --lmstudio-model translategemma-4b-it \
  --lmstudio-timeout 0 \
  --lmstudio-max-tokens 4096 \
  --lmstudio-temperature 0.2 \
  --lmstudio-max-workers 8

# pageモードの安定化オプション
python3 auto_translate.py <source_pdf> [output_pdf] --engine llm \
  --llm-translate-mode page \
  --llm-page-max-chars 12000 \
  --llm-page-retries 1

# LLM翻訳を使う場合
python3 auto_translate.py <source_pdf> [output_pdf] --engine llm
# デフォルト: 1ページ単位の構造化JSON翻訳

# 従来のsegment/block経路を使う場合
python3 auto_translate.py <source_pdf> [output_pdf] --engine llm --llm-translate-mode segment
```

### 抽出のみ

```bash
python3 extract.py <source_pdf> <output_json>
```

### PDF再生成のみ

```bash
python3 generate.py <source_pdf> <translated_json> <output_pdf>
```

## 注意点

- Google翻訳は外部APIの応答状況に影響されます
- LLM翻訳品質はLM Studioのモデルに依存します
- `--engine llm` では、既定で1ページ単位の構造化JSON翻訳を実行し、失敗時は従来のsegment/block翻訳へフォールバックします
- pageモードでは、失敗/警告時にリクエストを段階的に分割して再試行します（1ページ -> 1/2ページ -> 1/4ページ）。1/4ページでも失敗するブロックは英文のまま出力します
- LLM呼び出しごとに開始/終了ログを表示し、バッチ分割の発生理由も出力します
- `--lmstudio-timeout` は `0` 以下で強制タイムアウトを無効化できます
- `--lmstudio-max-workers` を上げると速度は上がりますが、モデル/マシンによっては失敗率が上がります
- 数値や固有名詞の保持はプロンプトで指示していますが、完全保証はできません
- レイアウト維持はbboxベースのため、長文化した文はフォント縮小されることがあります
