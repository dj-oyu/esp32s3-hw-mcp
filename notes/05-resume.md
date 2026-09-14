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

### 2026-09-14 の到達点（ここから再開）

退避までは完走する。**書込み後の実測で2つ詰まりを潰した**（両方ともリポジトリ側の修正済み）:

1. `tools/check_flash_dump.py` が ESP32 クラシックのブートローダオフセット（0x1000）を見ていて、
   自分の退避を拒否していた（S3 は 0x0）。→ チップ別オフセット表＋イメージのチップID照合＋
   パーティション表が指すアプリ区間の実在確認＋イメージ自身の SHA-256 再検証に書き直し、
   11ケースのセルフテスト（`tools/selftest_check_flash_dump.py`）を CI に追加。
2. `tools/gen_pie_timing_asm.py` が生成する関数が **windowed ABI 違反**（`ret` で戻っていた）。
   call8 で呼ばれる関数は `entry`/`retw` を対にしないと*呼び出し側*のレジスタ窓が回りっぱなしになり、
   2回目の呼び出しで引数が壊れる（実機では a2=3 で `EE.LD.ACCX.IP` がアドレス0を読み、LoadProhibited）。
   → 生成側を `entry a1, 32` + `retw.n` に修正し、`ret` を出したら生成器が落ちる不変条件を追加。

この2件の後は **書込みまでは通っている**（書込み後にアプリ区間を読み戻してハッシュ照合する検証も追加済み）。
止まっているのは最後のシリアル取得だけ:

- 書込み直後のチップリセットで USB-Serial-JTAG が再列挙し、ホスト側の `vhci_hcd` が
  デバイスを切り離して再アタッチする（`dmesg` に `USB disconnect` → `cdc_acm ttyACM0` が出る）。
- その結果コンテナ内の `/dev/ttyACM0` は **mode 0000・所有者は未マップ uid** になり、
  コンテナの中からは `chmod` も `mknod` もできない（CAP_MKNOD / CAP_SYS_ADMIN が無い）→ EACCES。
- 対処: WSL 側（ポッドの外）で `sudo chmod 666 /dev/ttyACM0`。効かなければ
  **USB を attach 済みの状態でポッドを再起動**（`--device=/dev/ttyACM0` は起動時バインド）。

そのときの状態: デバイスには **プローブ入りの計測ファームが焼かれている**（`main/probe.S` + `main.c` の
PROBE ブロック。CPENABLE と PIE 命令の実行可否を1命令ずつ確かめる一時的な仕掛け）。
計測が通ったら PROBE を外して計測ファームだけを焼き直し、最後に退避を書き戻す:

```bash
PORT=/dev/ttyACM0 bash tools/restore_flash.sh /workspace/backups/cardputer-s3-20260914T093526Z.bin
```

（退避の中身は `cardputer_pocketjs 1-55-g97bd403-dirty`／ESP-IDF v6.0.1。ブートローダとアプリの
自己 SHA-256 まで一致を確認済み。）

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
