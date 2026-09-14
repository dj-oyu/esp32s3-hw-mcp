# 再開メモ（コンテナを作り直したあと最初に読む）

作業場所: `/workspace/esp32s3-hw-mcp`（git は `main`、リモート https://github.com/dj-oyu/esp32s3-hw-mcp と同期済み）

## いまの状態

| 段階 | 内容 | 検証 |
|---|---|---|
| M0 | 一次情報のpin（TRM v1.8 1531p / Datasheet v2.2, sha256）＋コーパス（印字ページ=PDFページ） | CI緑 |
| PIE | `data/pie_instructions.json` 220命令、`data/pie_pipeline.json` Table 1.7-2 の217行、`data/pie_hazards.md` | `tools/verify_pie.py` 0 fail / 4 warn |
| レジスタ | `data/registers.json` 1581件、`data/peripheral_map.json` 44件 | `tools/verify_registers.py` 0 fail / 0 warn |
| MCPサーバー | `server/esp32s3_mcp.py` 8ツール＋2リソース（全応答に文書・版・ページ） | `tools/test_mcp_server.py` 21/21 |
| 実機実験 | ソフト一式（ファームはビルド成功、パーサは自己検査付き） | `tools/gen_pie_timing_asm.py --check`、`tools/selftest_pie_timing_parser.py` |

CI（`.github/workflows/verify.yml`）は 取得→コーパス→2種の抽出→2種の検証→生成物の鮮度→パーサ自己検査→MCP E2E→
「ゼロから再生成して `data/` とバイト一致」まで全部通る状態。

## 次にやること: 実機で測る（デバイスが見えるコンテナで）

```bash
cd /workspace/esp32s3-hw-mcp
PORT=/dev/ttyACM0 BACKUP_DIR=/workspace/backups bash tools/device_experiment.sh
```

順序（スクリプトが強制する）:

1. `esptool flash_id` でチップ素性を記録
2. **フラッシュ全8MBを退避** → `/workspace/backups/cardputer-s3-<日時>.bin` ＋ sha256
3. 退避の構造検査（0x1000 ブートローダ / 0x8000 パーティション表 / 0x10000 アプリ のマジック）。
   ここが通らなければ**焼かない**
4. ビルド → 書込 → シリアル取得（`END` まで、最大240秒）
5. `tools/parse_pie_timing.py` で解釈 → `data/pie_timing_measured.json`

### 結果の読み方

- **アンカー5件が全部 ok か**が最重要。外れたら `valid: false`、**派生値は1つも出ない**（そういう作りにしてある）。
  期待値: ACCX M→E = 1、qs M→E = 1、q{E}→q{E} = 0、ネイティブ `l32i`→`add` = 1、`add`→`add` = 0
- ノイズ床（`noise_cycles`）が0.5サイクルを超える回は無効扱い（0ストールを主張できないため）
- 通った場合に入る派生値:
  - `LD.QR` の operand def stage = 1 + stall(d=1)
  - `MV.QR` の operand def stage = 1 + stall(d=1)
  - `ST.QR` の operand use stage = （測った LD.QR の def 段）− stall(d=1)
  - `LD.QR` の アドレス（as）use stage = 1 − stall(d=1)

### 戻し方

```bash
PORT=/dev/ttyACM0 bash tools/restore_flash.sh /workspace/backups/cardputer-s3-<日時>.bin
```

## 未着手（優先度は上から）

1. `data/pie_timing_measured.json` が出たら MCP サーバーに載せる（`provenance.kind = measured_on_hardware` を
   PDF 由来の tier-1 と**区別して**返す）。`instruction_pipeline` が LD.QR/ST.QR/MV.QR に対して
   「一次情報に無い」ではなく実測値を返せるようにする
2. フィールド（ビット範囲）の抽出: 一次情報ではビットマップ図に描かれており、テキスト層には説明文しかない。
   図形座標から復元するか、ESP-IDF `soc/esp32s3/include/soc/*_reg.h` と突き合わせる
3. Datasheet 側（モジュール・クロック・電力）を知識源に追加
4. `analyze_sequence` の実コード検証: Cardputer 側の PIE カーネル（`cardputer-adv-pocketjs/tools/pie/`）に
   当てて「見積り vs 実測」を比較する
5. LD.QR/ST.QR/MV.QR の実測が取れたら `analyze_sequence` の対象に加える（現在は「一次情報なし」で除外）

## 環境メモ（作り直すと消えるもの）

- ESP-IDF: `/opt/esp-idf`（`. /opt/esp-idf/export.sh` で v6.0.1 と `xtensa-esp32s3-elf-*` が入る）
- esptool / pyserial: `/root/.espressif/python_env/idf6.0_py3.11_env/bin/`（`tools/capture_serial.py` はこれを想定）
- プロジェクトの venv: `/workspace/esp32s3-hw-mcp/.venv`（pypdf / pymupdf / mcp）
- 生成物で git 管理外: `sources/*.pdf`、`corpus/`、`experiments/pie-timing/firmware/build_*/`、退避ダンプ
- デバイスは **コンテナ起動時に `--device=/dev/ttyACM0` を渡す**こと（無いと mknod も拒否される）
