# 03 — 実装方針の決定（2026-09-14）

ユーザー判断により、M1 以降の方針を次のとおり固定する。README の「決定事項」と同じ内容を、
実装が従う形（パッケージ構成・スキーマ・検証）に落としたもの。

## 決定

| # | 論点 | 決定 |
|---|---|---|
| 1 | 実装言語 | Python 一本。**`uv` / `uvx` で起動できることを必須要件**とする |
| 2 | 知識の持ち方 | 構造化KB＋**SQLite FTS5 によるページ全文検索**（ベクトルRAGは入れない） |
| 3 | 収録範囲 | PDF（TRM/Datasheet）中核＋**ESP-IDF SDK との突き合わせ** |
| 4 | リポジトリ | GitHub public（`dj-oyu/esp32s3-hw-mcp`）＋著作権免責を明記 |

## 1. パッケージ構成（uv/uvx）

```
pyproject.toml            # [project] / [project.scripts] esp32s3-hw-mcp = "esp32s3_hw_mcp.server:main"
src/esp32s3_hw_mcp/
  server.py               # MCP サーバー（stdio）。ツールの登録だけを行う薄い層
  kb.py                   # SQLite(FTS5) のKBを開く／検索する
  data.py                 # data/*.json（PIE・レジスタ・周辺マップ）の読み込みと索引
  sdk.py                  # ESP-IDF ヘッダとの突き合わせ
  cite.py                 # 出所表記（文書名・版・ページ）と免責文言の一元化
tools/build_kb.py         # corpus/ + data/ → kb.sqlite（FTS5）を再生成
```

- `uvx --from . esp32s3-hw-mcp` と `uv run esp32s3-hw-mcp` の**両方**を README に書く。
- 依存は `mcp`（Python SDK）＋標準ライブラリのみ。KB 生成だけ `pymupdf`/`pypdf` を使うので、
  生成依存は `[project.optional-dependencies] build` に分離する（サーバー実行にPDFは不要）。
- `kb.sqlite` は**生成物**として git 管理外（`corpus/` と同じ扱い）。CI が再生成して検証する。

## 2. KB（構造化KB＋ページ全文検索）

- `pages` テーブル: `doc` / `page` / `text`（`corpus/*/pages.jsonl`）＋ FTS5 仮想テーブル
  `pages_fts(text)`、`bm25()` で順位付け。
- `sections` テーブル: `outline.json` の節番号・タイトル・ページ範囲（`get_section` 用）。
- 構造化側は既存の `data/*.json` をそのまま読み、`source_page` を回答に必ず付ける。
- **回答の形**: 値は構造化データから、説明文は「そのページの原文を検索して該当箇所を引用」から。
  引用は `doc` 名＋版＋ページを付けて返す。ページを出せない値は返さない（README の一次情報ポリシー）。

## 3. SDK 突き合わせ（3-c）

- 取得元: `https://raw.githubusercontent.com/espressif/esp-idf/master/components/soc/esp32s3/include/soc/*_reg.h`
  （未認証で取得可。GitHub code search は認証必須なので使わない）。リビジョンはコミットSHAでピン留め。
- 生成物 `data/sdk_registers.json`: `{name, offset, field, bit, doc}`。`tools/fetch_sdk.sh` + 抽出器で作る。
- 検証 `tools/verify_sdk.py`: PDF由来の `registers.json` と**名前で突き合わせ**、
  一致件数・PDFのみ・SDKのみ・オフセット不一致をそれぞれ報告する（不一致は隠さず出す）。
- ツール `cross_check_register(name)` は「PDFの値」「SDKの値」「一致/不一致」を返す。
  SDK のみに存在する値は**SDK由来と明示**して返す（PDFを一次、SDKは突き合わせ用という格付けを守る）。

## 4. 免責（4-c）

- `NOTICE.md` に方針（ライセンスを付与しない／Espressif 著作物／PDF非同梱／引用は必要最小限）を置く。
- サーバー応答には `cite.py` の定型で**出所と免責を必ず含める**（例:
  `source: ESP32-S3 TRM v1.8 p49 (© Espressif Systems) / 非公式・引用は技術仕様に必要な範囲`）。
- README/NOTICE の文面は公開物なので、内部の作業メモは置かない。

## 5. 検証（この方針での合格条件）

1. `uv run esp32s3-hw-mcp` と `uvx --from . esp32s3-hw-mcp` の両方で stdio サーバーが起動する。
2. MCP クライアント（`initialize` → `tools/list` → `tools/call`）で各ツールを実際に呼ぶ。
   代表: `get_instruction("EE.VADDS.S32")` / `instruction_pipeline` / `get_register` / `search_manual`。
3. 回答に必ず `source_page` が付き、印刷ページ＝PDFページの前提が守られている。
4. `tools/verify_*.py` が緑（PDF由来データ・SDK突き合わせ・KB）で、CI が再生成バイト一致で落ちる。
