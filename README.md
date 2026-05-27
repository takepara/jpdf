# PDFTranslate

英語PDFを日本語PDFへ翻訳し、元レイアウトをできるだけ維持して出力するスクリプト群です。

入力PDFは固定せず、任意のPDFを引数で指定して処理します。

## ファイル構成と役割

- `extract.py`
  - PyMuPDFでPDFのテキストブロックを抽出。
  - 各ブロックについて `page`, `block_idx`, `bbox`, `text`, `size`, `color`, `font` を保存。
  - さらに `x0`, `y0`, `x1`, `y1`, `width`, `height`, `line_count`, `reading_order` も保存し、段落判定に利用。

- `auto_translate.py`
  - 抽出JSONを読み込み、近接 block を同一ページ内の文脈 segment に束ねてから英日翻訳。
  - 目次や短い連続行、段落途中で切れた本文を優先的にまとめる。
  - 翻訳時は複数 block をタグ付きでまとめて送り、翻訳後に block 単位へ戻す。
  - 並列実行（`ThreadPoolExecutor`）で segment 単位に翻訳。
  - 数字のみ・空文字・極短文字は翻訳をスキップ。
  - segment の再分割に失敗した場合は、その segment だけ block 単位翻訳へフォールバック。
  - 結果を `<source_stem>_translated_text.json` に保存（`original_text` も保持）。

- `generate.py`
  - 元PDFを開き、各テキスト領域をredactionで消去して日本語テキストを再配置。
  - `segment_kind=paragraph` のブロックは強制改行を潰してから配置し、テキストボックス幅を使って自然に折り返す。
  - フォントは macOS の `Hiragino Sans GB.ttc` を優先、無ければPyMuPDF内蔵CJKフォントにフォールバック。
  - バウンディングボックス内に収まるフォントサイズを二分探索で調整。
  - 出力先はCLI引数で指定。

## 実行フロー

1. テキスト抽出
2. block を文脈 segment に再構成
3. segment 単位で機械翻訳し、block 単位へ戻す
4. レイアウト維持でPDF再生成

## 実行方法

### 1) パイプライン一括実行（推奨）

第一引数に英語PDF、第二引数に出力する日本語PDF名を指定します。
第二引数を省略した場合は、入力ファイル名の末尾に `_JA.pdf` を付与して出力します。

```bash
python3 auto_translate.py <source_pdf> [output_pdf]
```

例:

```bash
python3 auto_translate.py input.pdf
# -> input_JA.pdf が生成される

python3 auto_translate.py input.pdf translated.pdf
# -> translated.pdf が生成される
```

### 2) 抽出のみ実行

```bash
python3 extract.py <source_pdf> <output_json>
```

例:

```bash
python3 extract.py input.pdf extracted_text.json
```

### 3) PDF再生成のみ実行

```bash
python3 generate.py <source_pdf> <translated_json> <output_pdf>
```

例:

```bash
python3 generate.py input.pdf translated_text.json output.pdf
```

## 必要環境

- macOS（現状はmacOSシステムフォント前提の分岐あり）
- Python 3.x
- 主要ライブラリ:
  - `PyMuPDF`
  - `deep-translator`

インストール例:

```bash
pip install pymupdf deep-translator
```

## 現状の制約・注意点

- 文脈 segment 化は現在「同一ページ内」の連続 block が中心です。ページまたぎ段落の結合は未実装です。
- まとめ翻訳時に block 境界マーカーが崩れた segment は、自動で block 単位翻訳へフォールバックします。
- レイアウト維持は `bbox` ベースのため、長文化した日本語はフォント縮小で対応しています。可読性が下がるケースがあります。
- `extract.py` では画像/非テキストブロックは対象外です。

## 次にやると良さそうな改善

- ページまたぎ段落の結合判定追加
- 段落セグメントの再配分ロジック改善
- 失敗セグメントのみ自動再試行するリカバリ強化
- 用語集や表組みの専用判定を追加