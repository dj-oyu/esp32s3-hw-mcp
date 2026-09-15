# ESP32-S3 ハードウェア知識MCPサーバー

ESP32-S3 の **PIE（Processor Instruction Extensions, `EE.*` 命令）・レジスタ特性・パフォーマンス
チューニング**に関する知識を、MCP（Model Context Protocol）経由でエージェントに提供するための
サーバー。回答は推測ではなく **Espressif 公式PDFのページ番号付き引用**で返すことを設計の前提にする。

公開: https://github.com/dj-oyu/esp32s3-hw-mcp

## 動かし方（uvx：クローン不要）

```bash
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp          # MCP（stdio）として起動
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --list   # ツール面を人向けに表示
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --paths  # どの写しの知識が答えたか
```

抽出済みの知識（`data/`）は wheel に同梱されるので、**クローンも PDF も追加設定も無しに 18 ツール中 14 ツールが答える**
（残り4つは符号化を確かめるツールで、Espressif の binutils が要る。無い環境では「無い」と答える）。

マニュアル**本文**のページ検索（`search_manual` / `get_page`）だけは本文を再配布できないため、初回に一度だけ:

```bash
uvx --with pymupdf --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --fetch-corpus
```

sha256 を検証して PDF を取得し（16MB）、`~/.cache/esp32s3-hw-mcp/` にTRM 1531ページ＋Datasheet 87ページの
コーパスを作る。以後は同じ uvx 起動がそこを見つけるので、環境変数は要らない。

MCP クライアントへの登録（クライアントの流儀に従う）:

```bash
claude mcp add esp32s3-hw -- uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp
hermes mcp add esp32s3-hw --command uvx --args --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp
```

```json
{ "mcpServers": { "esp32s3-hw": { "command": "uvx",
  "args": ["--from", "git+https://github.com/dj-oyu/esp32s3-hw-mcp", "esp32s3-hw-mcp"] } } }
```

短い別名 `esp32-hw-mcp` も同じサーバーを起動する（どちらの名前も同じ `main` を指す）。

## 公開・ライセンス方針

- **ライセンスは付与しない（All rights reserved）。** 事実の出所が Espressif Systems の著作物である
  派生データを含むため、明示的な利用許諾は与えない立場を取る。詳細は [NOTICE.md](NOTICE.md)。
- 公式PDFは**同梱しない**（git 管理外）。`tools/fetch_sources.sh` が配布元から取得し sha256 で検証する。
- 抽出データには常に出所（文書・版・ページ）を持たせる。ページを出せない値はデータに入れない。
- CI（`.github/workflows/verify.yml`）が一次情報を再取得して全派生ファイルを再生成し、
  コミット済みの `data/` と**バイト一致**することを要求する。抽出の静かな変化・上流の改版はここで落ちる。
- CI はさらに **wheel を組んで配布物を検査する**（`tools/check_wheel.py`。レジストリが「同梱する」と宣言した
  成果物が実際に入っているか、件数・entry point・登録ツールの実体まで）、素の環境にインストールした同じ
  サーバーを stdio 越しの E2E 88項目に通す。**同梱物が1つ欠けてもツールは起動してしまい、何も無いところから
  答える**ので、そこを門にしている。

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

## 知識レジストリ（`data/` とルート）

集めた知識は「成果物 → 層 → **それを提供するツール/リソース** → 生成元 → 門」の1枚の表で管理している
（実体は `esp32s3_hw_mcp/registry.py` の `ARTIFACTS`。表と実装が食い違えば CI が落ちる）。
規模は表の数字ではなく、サーバーが**その場でファイルを開いて数えた**値を `knowledge_routes` が返す。

| ファイル | 層 | 内容 | 規模 | ルート（ツール / リソース） |
|---|---|---|---|---|
| `registers.json` | ① | TRM の "Register Summary" 表（第2〜39章、41節）: 名前・説明・オフセット・アクセス種別・グループ・節・ページ | **1581レジスタ** | `get_register` / `list_registers` |
| `peripheral_map.json` | ① | Table 4.3-3（p408-409）: ペリフェラル名と境界アドレス・サイズ。オフセットを絶対アドレスに直す基準 | 44行 | `list_peripherals` / `get_register`（`include_base_guess`） |
| `pie_instructions.json` | ① | TRM 1.8 の命令個別仕様（p76-303）: 命令語エンコード、アセンブラ構文、説明、操作擬似コード＋各フィールドのページ | 220命令 | `get_instruction` / `instruction_encoding` |
| `pie_pipeline.json` | ① | Table 1.7-2（p66-74）: 命令ごとのオペランド／特殊レジスタの use/def パイプライン段（1=E, 2=M） | 217行 | `instruction_pipeline` / `analyze_sequence` |
| `pie_hazards.md` | ① | TRM 1.7.1〜1.7.3（p65-75）の本文（データハザード／ハードウェア資源ハザード／制御ハザード） | ページマーカー付き原文 | `esp32s3://trm/pie-hazards` |
| `pie_review.json` | ① | マニュアル自身の記述が食い違う行（後述） | 2行 | `esp32s3://trm/review` |
| `pie_timing_measured.json` | ③ | **実機（Cardputer / ESP32-S3）で測った** PIE 命令のインターロック。アンカー（マニュアルが段を明記している5ケース）が全部一致したときだけ `valid: true` になり派生値を出す | 45測定 / 5アンカー / 派生4件 / 予測対5 | `measured_timing` / `instruction_pipeline` / `analyze_sequence` |
| `pie_examples_measured.json` | ③ | **実機で測った命令の意味**（`examples/` の手書きカーネル。各カーネルは装置上で C 参照と、ホスト側で Python 参照と三重に照合） | 18知見＋解釈保留1件 | `example_measured_semantics` |
| `pie_encoding_errata.json` | ② | マニュアルの命令語図と Espressif アセンブラが**食い違う命令だけ**（下記「アセンブラ照合」） | 220命令中6件 | `manual_errata` |
| `pie_measured_costs.json` | ④ | 兄弟プロジェクト cardputer-adv-pocketjs の実機で較正されたコストモデルと落とし穴（全項目に出典行の逐字引用と `provenance`） | 46項目 / 6セクション | `measured_costs` / `pie_cost_estimate` / `esp32s3://pocketjs/pie-costs` |
| `sources/*.pdf` | ① | 引用元そのもの（TRM v1.8 1531p / Datasheet v2.2 87p）。sha256 でピン留めし、再配布はしない | 2 PDF | `esp32s3://docs/sources`（`--fetch-corpus` で取得） |
| `corpus/trm-s3/pages.jsonl` | ① | TRM のページ全文（1531ページ）＋ブックマーク。**同梱しない**（`--fetch-corpus` が作る） | 1531ページ | `search_manual` / `get_page` |
| 兄弟プロジェクトの `docs/pie-simd.md` | ④ | ④層のデータが引用している文書。実行時には読まない（引用は同梱済み）が、④層の門がここに突き合わせる | — | （`tools/verify_measured_costs.py`） |
| Espressif の binutils | ② | ファームをビルドするのと同じ `xtensa-esp32s3-elf-as` / `objdump`。`esp32s3_hw_mcp/asm_toolchain.py` が橋渡し | — | `check_asm` / `decode_instruction` / `instruction_encoding` / `toolchain_status` |

`pie_pipeline.json` が本プロジェクトの中核データで、これがあると
**「この命令列は何サイクルストールするか」を決定論的に計算できる**（LLMの推定に頼らない）。

### ルートの解決（どの写しが答えるか）

同じ知識が「wheel の中」「クローンの `data/`」「環境変数が指す先」にあり得るので、順番と根拠を1か所に決めている
（`esp32s3_hw_mcp/registry.py`）。最初に見つかったものが勝ち、**探索の履歴ごと**報告する:

1. `ESP32S3_DATA_DIR`（`ESP32S3_CORPUS_DIR` / `ESP32S3_SOURCES_DIR` / `ESP32S3_POCKETJS_DOC` も同様）— 明示指定が最優先。
2. wheel 同梱の写し（`uvx --from git+…` の既定）。
3. クローンの `data/`（`python server/esp32s3_mcp.py` のとき）。
4. `~/.cache/esp32s3-hw-mcp/`（`--fetch-corpus` が作った PDF とコーパス。`ESP32S3_CACHE_DIR` で変更可）。

```bash
esp32s3-hw-mcp --paths       # どのルートがどこに解決したか（探索履歴つき）＋成果物の有無
esp32s3-hw-mcp --registry    # レジストリ全体を JSON で（MCP の knowledge_routes と同じ内容）
```

MCP 経由では **`knowledge_routes`**（ツール）と **`esp32s3://registry`**（リソース）が同じ索引を返す。
索引には「どの写しから答えたか」「何件あったか」が入るので、`corpus_missing` や `no_measurements` のような
返事が出たときに、ルートとインストールのどちらが悪いのかを切り分けられる。必須の成果物が1つでも欠けていれば、
サーバーは黙って空を返さず起動を拒否する（`--paths` が原因を出す）。

### アセンブラ照合（マニュアルの読み取りは信用しない）

TRM の命令語は**ビットフィールド図**で書かれており、これを目で読むと1ビットの取り違えが
「それらしい別の命令」として通ってしまう。そこでファームを実際にビルドするのと同じ
Espressif binutils に答えさせ、図から再構成した命令語と突き合わせる（`tools/asm_toolchain.py`）。

```bash
.venv/bin/python tools/asm_toolchain.py --check-asm "ld.qr q0, a3, 0"   # 符号化をアセンブラに答えさせる
.venv/bin/python tools/asm_toolchain.py --decode cd2034                 # 命令語 → ニーモニック
.venv/bin/python tools/asm_toolchain.py --check-instruction EE.ANDQ     # 図 vs アセンブラ
.venv/bin/python tools/asm_toolchain.py --check-all                     # 全220命令
.venv/bin/python tools/asm_toolchain.py --errata data/pie_encoding_errata.json
.venv/bin/python tools/selftest_asm_toolchain.py                        # 門の自己検査（ツールチェーンが無ければ skip）
```

結果は **220命令中 214命令がビット単位で一致**。残り6件が不一致で、それが成果物
（`data/pie_encoding_errata.json`、詳細と解釈は `notes/06-encoding-verification.md`）:

| 命令 | 何が食い違うか |
|---|---|
| `MV.QR` | 印刷された図は合計23ビット（命令は24ビット）。`qs[2:1]` と `qs[0]` の間の定数は実際には `0000` |
| `EE.VLDBC.32.IP` | 構文行の即値 `-256..252`（刻み2）に対し、アセンブラは `-512..508`（刻み4） |
| `EE.ST.ACCX.IP` | 構文行の `-512..508`（刻み4）に対し、アセンブラは8の倍数のみ・`-1024..1016` |
| `ST.QR` | 構文行のニーモニックが `LD.QR` と印字（誤植） |
| `EE.SRC.Q.LD.IP` / `EE.VMULAS.S8.QACC.LD.IP` | 抽出した図がフィールド列になっていない（抽出器の要修正） |

即値の「刻み」はマニュアルの範囲幅とフィールド幅から出る（`LD.QR` の `-128..112` ÷ 4ビット = 16刻み、
`-128` は −8 の2の補数で `1000`）。この規則もアセンブラ出力と突き合わせて検証している。

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
data/             抽出済み知識（git管理。wheel に同梱され、MCPサーバーが読む）
esp32s3_hw_mcp/   配布するパッケージ（レジストリ＝ルート索引、wheel に同梱されるサーバー/橋/生成器）
examples/         PIE実用サンプル（手書きアセンブリ＋自己採点。examples/README.md）
experiments/      実機実験（段・ハザードの測定、flash退避）
server/           サーバー本体（wheel では esp32s3_hw_mcp/server.py として入る）
tools/            取得・コーパス・抽出・検証・ビルド・実機実行のスクリプト
notes/            調査メモ（08 に Cardputer ADV のメディア/3D 性能の物差しと量産リスト）
pyproject.toml    配布の宣言（依存・entry point・wheel に入れるもの）。tools/check_wheel.py が検査する
```

## PIE 実用サンプル（`examples/`）

`data/` の知識だけを使って書いた手書き PIE アセンブリの例題集。どの例も「手書き asm / ファーム内 C 参照 /
ホスト側 Python 参照」の三重で答え合わせをし、マニュアルとアセンブラが食い違う箇所は**どちらの文書に
シリコンが従ったかを判定**して出す。詳細と例題一覧は `examples/README.md`。

```bash
bash tools/build_examples.sh                                        # ビルド
bash tools/host_flash_and_log.sh --examples                         # 焼く＋ログ＋自動判定（ホスト側）
.venv/bin/python tools/check_examples_log.py /workspace/backups/pie-examples-<stamp>.log
.venv/bin/python tools/selftest_examples_checker.py                 # チェッカー自身の検査
```

| 例 | 内容 |
|---|---|
| ex01 | メモリ系命令のアドレス後置インクリメントの刻み（文書が 3 通りに割れている 2 件の決着）、`EE.SRS.ACCX`、`EE.BITREV` |
| ex02 | 16×16 int16 行列積（`EE.VMULAS.S16.ACCX` は 8 レーンを合算） |
| ex03 | 16tap Q15 FIR。16バイト境界に乗らない窓は PIE の 128bit アクセスが下位ビットを落とすため、境界に揃えた窓を渡す（丸めの可視化プローブと、コピー不要の `EE.SRC.Q` 経路のプローブ付き） |
| ex04 | `LD.QR`/`ST.QR`/`MV.QR` と、実測段に基づく `LD.QR` インターロックの追試 |
| ex05 | 40bit ACCX と `EE.SRS.ACCX` の飽和（数学的和との比較） |
| ex06 | `EE.FFT.R2BF.S16` / `EE.CMUL.S16` のレーン対応（多段 FFT の前段） |
| ex07 | 4×4 頂点変換を 8 頂点並列で（`EE.VSMULAS.S16.QACC` + `EE.SRCMB.S16.QACC`）。頂点あたりのサイクル数を C(-O2) と比較 |
| ex08 | フレームバッファ効果（RGB565 ハーフブレンド、飽和グロー、クランプ、ティント）。ピクセルあたりのサイクル数を C と比較 |

第1回ラン（`pie-examples-20260914T174138Z.log`）で ex01/ex02/ex04/ex05 と ex06 の測定済みレーンは一致し、
失敗した2件はどちらも**こちら側の思い違い**が原因と判明した（ex03 は 2 バイトずつ滑らせた窓が
8サンプル連続で同じ16バイトを読んでいた＝TRM p49 の丸めを実機が実演、ex06 は sel2=1 のフィールド並びが
MSB 先だった）。詳細は `examples/README.md` と `data/pie_examples_measured.json`。

## 使い方（クローンして開発・抽出・実機検証する場合）

上の uvx 経路が利用者の既定で、こちらは**抽出を作り直す／検証する／実機で測る**ための経路。

```bash
uv sync --extra dev                                           # 依存（pypdf / pymupdf / mcp）
bash tools/fetch_sources.sh                                   # PDF取得＋sha256検証
.venv/bin/python tools/build_corpus.py sources/esp32-s3_technical_reference_manual_en.pdf --out corpus/trm-s3
.venv/bin/python tools/extract_pie.py                         # data/*.json を生成
.venv/bin/python tools/verify_pie.py                          # 検証（緑になること）
```

`uv` を使わない場合は `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` で同じ。

コーパスへの問い合わせは skill 付属の `tools/pdf_corpus.py`（`toc` / `find` / `text` / `grep`）が使える。

## MCPサーバー（`server/esp32s3_mcp.py` / パッケージ名 `esp32s3_hw_mcp`）

```bash
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp              # 利用者（uvx）
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --list       # ツール面
uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --paths      # ルートの解決結果
uvx --with pymupdf --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp --fetch-corpus

.venv/bin/python server/esp32s3_mcp.py                                              # 同じサーバー（クローンから）
.venv/bin/python tools/test_mcp_server.py                                            # stdio越しに88項目のE2E
.venv/bin/python tools/verify_measured_costs.py                                      # ④層のデータと出典文書の突き合わせ
uv build --wheel && .venv/bin/python tools/check_wheel.py dist/*.whl                 # 配布物の門（中身・件数・entry point・ツール面）
```

実装済みツール（18ツール）。**4つの層を混ぜない**: ①一次情報（PDF）は必ず文書・版・印字ページを添える、
②ツールチェーン（アセンブラ）は「実際に何に符号化されるか」を答える、③実機の計測（段・ストール）は測った値と
その有効性検査の結果を返す、④実機の**意味とコスト**は測定条件と一緒に返す。④には2種類あり、
**このリポジトリで測ったもの**（`data/pie_examples_measured.json`、`data/pie_timing_measured.json`）と
**兄弟プロジェクト cardputer-adv-pocketjs で測られたもの**（`data/pie_measured_costs.json`）を
`provenance.measured_in_this_repository` で必ず区別する。両者を混ぜたり平均したりしない。

| ツール | 層 | 内容 |
|---|---|---|
| `knowledge_routes(include_counts)` | 索引 | 上のレジストリ表を実行時に返す（成果物・層・ルート・生成元・門・どの写しが答えたか・**その場で数えた件数**）。何が届くかを先に知りたいとき、返事がおかしいときに最初に呼ぶ |
| `get_register(name, include_base_guess)` | ① | レジスタ名で引く（`_REG` 省略可・部分一致）。`include_base_guess` で Table 4.3-3 からのベースアドレス推定（**推定であることを明示**して返す） |
| `list_registers(prefix/chapter/section/group)` | ① | 前置き・章・節・グループで一覧 |
| `get_instruction(name)` | ① | PIE命令のエンコード・構文・説明・操作擬似コード |
| `instruction_pipeline(name)` | ①③ | Table 1.7-2 の use/def 段（原文セルも併記）。LD.QR/ST.QR/MV.QR は「一次情報に無い（found=false）」と返しつつ、**有効性ゲートを通った実測**があれば `measured` として別枠で添える（表の値と混ぜない） |
| `list_peripherals(target)` | ① | Table 4.3-3 のペリフェラル境界アドレス |
| `search_manual(query)` / `get_page(page)` | ① | TRM本文の検索・ページ取得（**ローカルにコーパスが要る**。無ければ `--fetch-corpus` か作り方を返す） |
| `analyze_sequence([...])` | ① | 命令列のストール段数を見積もる。TRM 1.7.1 の `D=max(SA-SB+1,0)`／ストール `=max(SA-SB,0)` を Table 1.7-2 の段に適用（根拠と限界は `notes/03-interlock-model.md`）。資源・制御ハザードは「未モデル」として明示して返す |
| `check_asm(snippet, expected_words)` | ② | アセンブルして符号化を返す。`expected_words` を渡せば**主張を検査**する（不一致は不一致として返る） |
| `instruction_encoding(name)` | ② | マニュアルの図にオペランドを代入した語と、アセンブラが出した語を比較。不一致なら最初に違うビット位置を返す |
| `decode_instruction(word)` | ② | 命令語 → ニーモニック（逆方向の照合） |
| `toolchain_status()` | ② | どの as/objdump を使っているか（版つき）。無ければ「無い」と言う |
| `measured_timing(instruction)` | ③ | 実機で測ったストール。`valid`（有効性ゲート通過）とアンカー、限界を併せて返す。`interlocks` / `predictions` に「予測 vs 実測」の対ごとの表（`confound` 付き）を含む |
| `manual_errata(instruction)` | ②③ | マニュアルとアセンブラが食い違う命令の一覧（`data/pie_encoding_errata.json`） |
| `example_measured_semantics(instruction)` | ④ | `examples/` のカーネルが実機で確かめた**意味**（`data/pie_examples_measured.json`）。TRM の疑似コードと実機の一致／不一致、レーンの実値、どの読みが反証されたか、解釈保留の生データ |
| `measured_costs(section, query)` | ④ | **兄弟プロジェクトの実機で較正されたコストモデルと落とし穴**（`data/pie_measured_costs.json`）。1命令1サイクル・`VST.128.IP` のみ +0.6・下限の式・実動作 1.3〜1.4倍・索引ロードの値段・スカラー除算/数学関数の値段・計測作法など。全項目に出典行の逐字引用と `provenance` が付く |
| `pie_cost_estimate(blocks, instructions_per_block, stores_per_block, stalls_per_block, runs, cycles_outside_loop_per_run, divisions_in_row_setup)` | ④ | 同じデータの定数で下限を計算する（命令数＋0.6×ストア数＋ストール数、×ブロック数、＋run の足場、×1.3〜1.4 のレンジ）。定数は必ず出典付きで返し、較正の場所以外への外挿には警告を付ける |

リソース（5つ）: `esp32s3://registry`（上記レジストリの索引そのもの）、`esp32s3://trm/pie-hazards`（1.7 の原文）、
`esp32s3://trm/review`（マニュアル自身が食い違う行）、`esp32s3://docs/sources`（出所とsha256。**兄弟プロジェクトの
文書は一次情報と別枠**で digest 付きに列挙）、`esp32s3://pocketjs/pie-costs`（④層のデータ本体）。

設計上の約束:

- マニュアルに書かれていないことは**「無い」と言う**。例: LD.QR のハザード段は Table 1.7-2 に無いので
  推定せず absent を返す。フィールドのビット範囲も未抽出なので返さない。
- 推定値（レジスタのベースアドレス）は `confidence: heuristic` と候補列を付けて返す。
- 応答に必ず `citation`（文書名・版・ページ・sha256）を付ける。
- **マニュアルの図と実際のアセンブラ出力が違うときは、両方をそのまま返す**（どちらが正かを勝手に決めない）。
  その一覧が `manual_errata`。

## ストール見積りの中身（`analyze_sequence`）

- 規則: `D = max(SA - SB + 1, 0)`（SA=書く段, SB=読む段）／インターロックは `D - 1 = max(SA - SB, 0)`。
- **マニュアル自身の矛盾を記録**: p65 の計算例は `SA=W` を **2** として `D=max(2-1+1,0)=2` と書くが、
  Table 1.7-1 は **W=3**。ただし Table 1.7-2 は use/def とも 1(E)/2(M) しか使わず W は現れないため、
  表から駆動する計算には波及しない（応答の `rule.manual_inconsistency` にも明記して返す）。
- 表から導かれる「1ストールを生みうるレジスタ」は **ACCX / QACC_H / QACC_L / UA_STATE / as0 / qs** の6つ。
- ハードウェア資源ハザード（1.7.2）と制御ハザード（1.7.3）は表から計算できないので**計算せず**、根拠ページ付きで
  「未モデル」として返す。

## 予定している MCP のサーフェス（未実装分の設計案）

上の表にあるものは実装済み。ここに残るのは未実装の設計案だけ（すべて回答に TRM/Datasheet のページを付ける）:

- `list_instructions(class?)` — 1.6 の分類（Read/Write/DataExchange/Arithmetic/Comparison/…）で絞る
- `get_section(ref)` — 節単位の取得（現状は `search_manual` + `get_page`）
- `get_field(reg, field)` — レジスタのビット範囲。**一次情報では図版に描かれている**ため、図形座標からの
  復元か ESP-IDF `soc/*_reg.h` との突き合わせが要る（未実装）
- `memory_map()` / `clock_tree()` — Ch.4 (p400-409) / Ch.7 (p526-533)
- `perf_checklist(topic)` — チューニング項目の整理（根拠ページ付き）

リソースとして TRM のページとセクションを `trm://page/65` のように公開し、プロンプトで
「PIEで書かれたカーネルのストール解析」を定型化する。

## 決定事項（2026-09-14、ユーザー判断）

1. **実装言語: Python 一本。`uv` / `uvx` で動かせることを必須要件とする。**
   `pyproject.toml` に `[project.scripts]` と依存を宣言し、`uvx --from <path|repo> esp32s3-hw-mcp`
   で起動できる形にする（`uv run` も同じ宣言から動く）。`python3 -m venv` の手順は補助に落とす。
   → **実装済み（2026-09-15）**: `pyproject.toml`（hatchling）＋ `esp32s3_hw_mcp/` パッケージ。
   `uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp` が最短経路で、知識は wheel に
   同梱。`uvx` からは取れない成果物（PDF・コーパス）は `--fetch-corpus` が `~/.cache/esp32s3-hw-mcp` に作る。
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
