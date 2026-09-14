# ESP32-S3 ハードウェア知識MCPサーバー

ESP32-S3 の **PIE（Processor Instruction Extensions, `EE.*` 命令）・レジスタ特性・パフォーマンス
チューニング**に関する知識を、MCP（Model Context Protocol）経由でエージェントに提供するための
サーバー。回答は推測ではなく **Espressif 公式PDFのページ番号付き引用**で返すことを設計の前提にする。

公開: https://github.com/dj-oyu/esp32s3-hw-mcp

## 公開・ライセンス方針

- **ライセンスは付与しない（All rights reserved）。** 事実の出所が Espressif Systems の著作物である
  派生データを含むため、明示的な利用許諾は与えない立場を取る。詳細は [NOTICE.md](NOTICE.md)。
- 公式PDFは**同梱しない**（git 管理外）。`tools/fetch_sources.sh` が配布元から取得し sha256 で検証する。
- 抽出データには常に出所（文書・版・ページ）を持たせる。ページを出せない値はデータに入れない。
- CI（`.github/workflows/verify.yml`）が一次情報を再取得して全派生ファイルを再生成し、
  コミット済みの `data/` と**バイト一致**することを要求する。抽出の静かな変化・上流の改版はここで落ちる。

## 一次情報ポリシー

- 収録する事実は **Espressif 公式ドキュメント**のみ。本リポジトリは PDF を再配布せず、
  `tools/fetch_sources.sh` で取得し **sha256 をピン留めして検証**する。
- 一次情報の格は3段階で管理し、回答に必ず出所を添える。
  1. **PDF**（一次）: TRM / Datasheet。ページ番号で引用する。
  2. **公式HTML**（準一次）: errata、チップリビジョン、ESP-IDF の cache/perf 記述。現状未収録。
  3. **SDKソース**（突き合わせ用）: ESP-IDF の `soc/esp32s3/include/soc/*_reg.h`。現状未収録。
- 抽出値には **`source_page` を必ず持たせる**。ページを出せない値はデータに入れない。

## 収録済みの一次情報（M0時点）

| 文書 | 版 | ページ | sha256（先頭16桁） |
|---|---|---|---|
| ESP32-S3 Technical Reference Manual | v1.8 (2026-03-04) | 1531 | `4484bf8a69035ec4` |
| ESP32-S3 Series Datasheet | v2.2 (2026-03-05) | 87 | `2d5a7cb7fd559d8d` |

- どちらも **印字ページ＝PDFページ**（`tools/build_corpus.py` がビルド時に検査）。引用のオフセット計算は不要。
- **Xtensa 基本ISA のリファレンスマニュアルは Espressif 配布のPDFが存在しない**。
  `documentation.espressif.com/*.pdf` の未知URLは HTTP 200 で SPA の HTML（約13KB）を返すため、
  存在確認はステータスコードでは不可。サイズと sha256 で判定すること。
  → 基本ISA（パイプライン段の一般論、命令スケジューリング、hwloop 等）は PDF では裏が取れない。
     TRM 1.7 に書かれている範囲だけを一次情報として扱う。

## 現状の成果物（`data/`）

| ファイル | 内容 | 規模 |
|---|---|---|
| `pie_instructions.json` | TRM 1.8 の命令個別仕様（p76-303）: 命令語エンコード、アセンブラ構文、説明、操作擬似コード＋各フィールドのページ | 220命令（`EE.*` 217 + `LD.QR`/`ST.QR`/`MV.QR`） |
| `pie_pipeline.json` | TRM Table 1.7-2（p66-74）: 命令ごとのオペランド／特殊レジスタの use/def パイプライン段（1=E, 2=M） | 217行 |
| `pie_hazards.md` | TRM 1.7.1〜1.7.3（p65-75）の本文（データハザード／ハードウェア資源ハザード／制御ハザード） | ページマーカー付き原文 |
| `pie_review.json` | マニュアル自身の記述が食い違う行（後述） | 2行 |
| `registers.json` | TRM の "Register Summary" 表（第2〜39章、41節）: レジスタ名・説明・オフセット・アクセス種別・グループ・節・ページ | **1581レジスタ** |
| `peripheral_map.json` | Table 4.3-3（p408-409）: ペリフェラル名と境界アドレス・サイズ。オフセットを絶対アドレスに直す基準 | 44行 |

`pie_pipeline.json` が本プロジェクトの中核データで、これがあると
**「この命令列は何サイクルストールするか」を決定論的に計算できる**（LLMの推定に頼らない）。

### 検証

```bash
.venv/bin/python tools/verify_pie.py        # PIE:  0 failure / 2 warning で緑
.venv/bin/python tools/verify_registers.py  # レジスタ: 0 failure / 0 warning で緑（pypdfとの突き合わせで数分）
```

再現性の門: `tools/extract_pie.py` を流し直した結果が `data/` と一致すること（CI でも検査している）。

```bash
.venv/bin/python tools/extract_pie.py && git diff --exit-code -- data/
```

6つの検査を、抽出器とは独立の情報源（1.8 のアセンブラ構文、Table 1.7-1 の段番号、印字ページの目視、
コーパス統計）で行う。**片方の抽出器だけを信じない**方針で、表のセルは pypdf と PyMuPDF の
両方で取り、両者が一致することを確認済み。

## 既知の限界（レジスタ側）

1. **I2S 章はアドレス列を2本（I2S0 / I2S1）持つ**（p1059 のヘッダは "I2S0 Ad-" + "dress" にハイフネーション
   される）。抽出は左の列を採用しており、両インスタンスでオフセットが一致することを前提にしている。
2. **RNG 章はオフセットでなく絶対アドレス**（`0x6003_507C`）を印字する。`address_is_absolute: true` を
   付けて区別してある。オフセットとして解釈してはならない。
3. **メモリブロック表（20.4, p878）はレジスタ表ではない**ため収録しない（"Starting/Ending Address" を
   持つヘッダを弾いている）。
4. 抽出したのは **Register Summary 表のみ**。各レジスタの**フィールド（ビット範囲）は図版**
   （ビットマップ図）に描かれており、テキスト層には説明文しかない。フィールドの抽出は次の段階で、
   図形の座標からビット範囲を復元するか、ESP-IDF の `soc/*_reg.h` と突き合わせる必要がある。
5. オフセットの一意性検査は「同一節かつ同一レジスタファミリ（先頭トークン）」に限定している。章をまたいで
   同じ番地に別ペリフェラルのレジスタが並ぶのは正常（別ベースアドレス）。

## 既知の限界（PIE側）

1. **`LD.QR` / `ST.QR` / `MV.QR`（p301-303）は Table 1.7-2 に載っていない**。ハザード段の情報が
   一次情報に存在しないので、これら3命令のスケジューリングは「未検証」として扱う。
2. **`EE.BITREV` は表と構文が食い違う**。Table 1.7-2 は use=`ax`、1.8 のアセンブラ構文は `qa, as`
   （p66 と p77、両抽出器で同じ）。どちらが正かは一次情報では決まらない → 手当てで確定する。
3. **`EE.FFT.AMS.S16.ST.INCP` は表が `as0` を挙げるが構文には `as` しかない**。添字付き
   サブレジスタ（`as0`/`as1`, `qz1`, `fu0`〜）の扱いは未整理。
4. `EE.VMULAS.S8.QACC.LD.IP`（p228）に `Description` 節が無い（マニュアル側の欠落）。
5. Table 1.7-2 のセルは **PDFのテキスト層でスペースが落ちる**（`qv2,as01,as1,` = "qv 2, as0 1, as 1"）。
   パーサは貪欲マッチで復元し、復元できなかった残りは `raw` として残す（黙って切り捨てない）。

## ディレクトリ

```
sources/          取得したPDF（git管理外。tools/fetch_sources.sh で再取得）
corpus/           ページ単位JSONL＋ブックマーク（git管理外。tools/build_corpus.py で再生成）
data/             抽出済み知識（git管理。MCPサーバーが読む）
tools/            取得・コーパス・抽出・検証のスクリプト
notes/            調査メモ
```

## 使い方

```bash
bash tools/fetch_sources.sh                                   # PDF取得＋sha256検証
python3 -m venv .venv && .venv/bin/pip install pypdf pymupdf  # 依存
.venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3
.venv/bin/python tools/extract_pie.py                         # data/*.json を生成
.venv/bin/python tools/verify_pie.py                          # 検証（緑になること）
```

コーパスへの問い合わせは skill 付属の `tools/pdf_corpus.py`（`toc` / `find` / `text` / `grep`）が使える。

## 予定している MCP のサーフェス（設計案）

ツール（すべて回答に TRM/Datasheet のページを付ける）:

- `search_manual(query, doc?, chapter?)` / `get_page(page, doc)` / `get_section(ref)`
- `get_instruction(name)` — エンコード・構文・説明・操作・ページ
- `list_instructions(class?)` — 1.6 の分類（Read/Write/DataExchange/Arithmetic/Comparison/…）で絞る
- `instruction_pipeline(name)` — use/def 段とハザード則（`pie_pipeline.json`）
- `analyze_sequence([...])` — **命令列のストール段数を見積もる決定論的ツール**（1.7.1 の D=max(SA-SB+1,0) など）
- `get_register(name)` / `get_field(reg, field)` / `list_registers(chapter)` — 各章の Registers 節から
- `memory_map()` / `clock_tree()` — Ch.4 (p400-409) / Ch.7 (p526-533)
- `perf_checklist(topic)` — チューニング項目の整理（根拠ページ付き）

リソースとして TRM のページとセクションを `trm://page/65` のように公開し、プロンプトで
「PIEで書かれたカーネルのストール解析」を定型化する。

## 決定事項（2026-09-14、ユーザー判断）

1. **実装言語: Python 一本。`uv` / `uvx` で動かせることを必須要件とする。**
   `pyproject.toml` に `[project.scripts]` と依存を宣言し、`uvx --from <path|repo> esp32s3-hw-mcp`
   で起動できる形にする（`uv run` も同じ宣言から動く）。`python3 -m venv` の手順は補助に落とす。
2. **知識の持ち方: 構造化KB＋ページ全文検索**（ベクトルRAG・埋め込みは入れない）。
   値（オフセット・段数・ハザード・集約）は構造化データから、記述はページ全文検索から引く。
   どちらの経路でも回答に文書名・版・ページを付ける。
3. **収録範囲: PDF中核（TRM/Datasheet）＋ SDK突き合わせ。**
   ESP-IDF の `soc/esp32s3/include/soc/*_reg.h` 等を**照合専用データ**として収録し、
   PDF の値との一致・不一致の両方を返せるようにする（PDFを一次、SDKは突き合わせ用と区別する）。
   公式HTML（errata 等）は補助で、PDFと同格には扱わない。
4. **リポジトリ: GitHub public**（`dj-oyu/esp32s3-hw-mcp`、現行のまま）。
   著作権の免責（出所は Espressif 著作物、非公式、PDF非同梱、引用は技術仕様を伝えるのに必要な
   最小限）を `NOTICE.md` に明記する。**MCP の応答にも免責と出所を必ず載せる**。
