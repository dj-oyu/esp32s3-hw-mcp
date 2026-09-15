# 12. uvx 配布と、知識レジストリへのルート

決定: 2026-09-15。実行経路の第一を「クローンして .venv」から
`uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp` に移した。あわせて、
知識への到達経路（どの成果物を、どのツール/リソースで、どの写しから引くか）を1枚の表に集約した。

## 何を配るか（`pyproject.toml` + `esp32s3_hw_mcp/`）

- wheel に**同梱する**: `data/*.json|md`（抽出済み知識）、`tools/asm_toolchain.py`（符号化の橋）、
  `tools/build_corpus.py`（`--fetch-corpus` 用）、`NOTICE.md`。
  hatchling の force-include で `esp32s3_hw_mcp/` 配下に置く。リポジトリ側の配置（`tools/` に
  スクリプト、`data/` に抽出物）は**そのまま**にしてある: 抽出パイプラインと CI の再生成門がそこを見ている。
- **同梱しない**: `sources/*.pdf`（Espressif の著作物。再配布しない方針）、`corpus/`（PDF から作る派生物）、
  兄弟プロジェクトの `docs/pie-simd.md`（引用は `data/pie_measured_costs.json` に同梱済み）。
- 依存は `mcp>=2.0` のみ。`pymupdf` は `--fetch-corpus` の1回だけ要るので extra（`corpus` / `dev`）に置き、
  `uvx --with pymupdf …` でも渡せるようにした。
- entry point は `esp32s3-hw-mcp` と別名 `esp32-hw-mcp` の2つ（同じ `esp32s3_hw_mcp.server:main`）。

## ルートの解決（`esp32s3_hw_mcp/registry.py`）

同じ知識が複数の場所にあり得るため、順序を1か所に固定し、**探索の履歴ごと**返す:

1. `ESP32S3_DATA_DIR`（`ESP32S3_CORPUS_DIR` / `ESP32S3_SOURCES_DIR` / `ESP32S3_CACHE_DIR` /
   `ESP32S3_POCKETJS_DOC`）
2. wheel 同梱の写し（uvx の既定）
3. クローンの `data/`（`python server/esp32s3_mcp.py`）
4. `~/.cache/esp32s3-hw-mcp/`（`--fetch-corpus` が作る PDF とコーパス）

`--paths` と `knowledge_routes`（ツール）/ `esp32s3://registry`（リソース）がこの解決結果を返す。
「どの写しが答えたか」を常に言えるようにしておくと、`corpus_missing` のような返事が
「ルートが無い」のか「インストールが壊れている」のかを切り分けられる。
必須の成果物が欠けていれば**起動を拒否する**（黙って空を返さない）。

## レジストリ（`ARTIFACTS`）

成果物 → 層（①PDF ②ツールチェーン ③自機 ④兄弟プロジェクト）→ 提供するツール/リソース → 生成元 → 門。
`knowledge_routes` は件数を**その場でファイルを開いて数える**（表の数字を信用しない）。
これを門にしている理由: ルートと成果物が食い違うと、ツールは起動し、何も無いところから答える。
配布物でも同じ失敗が起きる（wheel から data が1つ欠ける）ので、同じ表を
`tools/check_wheel.py` が読み、CI の package ジョブ（wheel を組む → 検査 → 素の環境にインストール →
E2E 88項目）が落ちる。

- 今回、**ルートが無かった** `data/pie_review.json`（マニュアル自身が食い違う行）を
  `esp32s3://trm/review` として公開した。
- コーパスは `--fetch-corpus` で uvx からも到達可能にした（PDF を sha256 検証つきで取得し、
  `~/.cache/esp32s3-hw-mcp/` に TRM 1531ページ＋Datasheet 87ページを生成）。

## 検証したこと（2026-09-15）

- `uvx --from /workspace/esp32s3-hw-mcp esp32s3-hw-mcp --paths` → data の解決元が `wheel`。
- `uvx --from git+file:///workspace/esp32s3-hw-mcp esp32s3-hw-mcp --list` → git 経路も同じ。
- `uvx --with pymupdf … --fetch-corpus` → sha256 一致（`4484bf8a…` / `2d5a7cb7…`）、1531 + 87ページ。
- E2E 88項目を3通りで緑: クローン（`server/esp32s3_mcp.py`）、wheel を素の venv に入れた console script、
  `uvx --from /workspace/esp32s3-hw-mcp`（`ESP32S3_SERVER_CMD` で対象を差し替え）。
- `tools/check_wheel.py` 31項目緑（同梱物のバイト一致・件数・entry point・登録ツールの def まで）。
- 実行時間: `--fetch-corpus` は 4 秒（16MB 取得＋1531ページ抽出）。wheel は 172KB。
- クライアント登録も実際に通した: `hermes mcp add esp32s3-hw --command uvx --args --from <path>
  esp32s3-hw-mcp` → 18/18 ツールを発見（確認後に `hermes mcp remove` で元に戻した）。
- CI の package ジョブと同じ手順をローカルで再現（`python -m build --wheel` → `check_wheel.py` →
  素の venv にインストール → E2E）。コーパス無しでも通る（本文検索の2項目は skip になる）。
