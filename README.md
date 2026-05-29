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

## 翻訳API呼び出し粒度ルール（実装準拠）

このプロジェクトでは、翻訳API呼び出しの粒度は「固定文字数」ではなく「文脈segment単位」です。
1回の翻訳API呼び出しは、1つ以上の block から構成される segment に対して行います。

### 1) 最小単位（抽出単位）

- `extract.py` がPyMuPDFのテキスト block を抽出し、これが最小の処理単位になります。
- block 内では行情報を連結して `text` を作成します（改行は保持）。
- 各 block には位置・サイズ情報（`x0/y0/x1/y1`, `width`, `line_count`, `reading_order`）を保持し、後段のsegment化で利用します。

### 2) segment 化ルール（まとめ翻訳の境界）

`auto_translate.py` は block をソートしたうえで、隣接block同士を条件付きで結合します。

- 同一ページ内のみ結合（ページまたぎは結合しない）
- 同一カラムとみなせる横位置であること
- 縦方向ギャップが大きすぎないこと
- 種別（`paragraph` / `list` / `toc` / `heading`）が整合していること
- 本文はフォント近似に加えて、文中継続らしさ（文末記号なし・次行先頭が継続語など）を使って結合
- 短文同士（80文字以下）の連続は結合を優先

### 3) 種別ごとの翻訳戦略

- `paragraph` segment:
  - segment全体を連結して1回翻訳
  - 翻訳後、日本語文を分割して各 block へ再配分
- `toc` / `list` / `heading` を含む非 paragraph segment:
  - block境界にマーカー（`__PDFTRANSLATE_BLOCK_n__`）を埋め込んで1回翻訳
  - 翻訳結果をマーカーで block 単位へ復元
- 非 paragraph かつ block が1つだけの場合:
  - その block を単体翻訳

### 4) フォールバック規則

- segmentまとめ翻訳後の再分割に失敗した場合は、そのsegmentだけ block 単位翻訳へ自動フォールバックします。
- 数字のみ・空文字・極短文字は翻訳スキップ対象です。

### 5) 並列実行

- 翻訳は segment 単位で並列実行します（`ThreadPoolExecutor`, `max_workers = 8`）。
- つまり「同時実行数」は8ですが、「1リクエストに含まれるテキスト量」はsegment化結果に依存する可変です。

### 6) 現状の粒度に関する設計意図

- 段落・目次・箇条書きの連続性を保ちながら翻訳品質を上げる
- block単位の機械的翻訳で起きやすい文脈欠落を減らす
- ただしページまたぎ結合は未実装のため、長い段落がページをまたぐケースは分断されます

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