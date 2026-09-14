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

## 次にやること: 実機で測る（**ポッド再起動が必要**な状態）

**今はデバイスに触れない。** 2026-09-14 16:42 に USB-Serial-JTAG が再列挙し、コンテナ側の
`/dev/ttyACM0` が再び削除済み inode を指したまま（`mountinfo` が `/ttyACM0//deleted`、`c---------`、
open は EACCES）。復旧は **USB を attach したままポッドを再起動**するだけ（前回と同じ）。

再起動後にやること（この順で）:

```bash
ls -l /dev/ttyACM0 && python3 -c "import os; os.close(os.open('/dev/ttyACM0', os.O_RDWR|os.O_NONBLOCK)); print('openable')"
cd /workspace/esp32s3-hw-mcp && PORT=/dev/ttyACM0 BACKUP_DIR=/workspace/backups bash tools/device_experiment.sh
```

1. `/dev/ttyACM0` が開けることを確認（`c---------` なら再起動が効いていない）。
2. スクリプトは 退避→ゲート→ビルド→書込み→読み戻し照合→シリアル取得→解釈 まで自走する。
   **今回はノイズ床が下がっているはず**（下の「16:10 の修正」を参照）。見るのは
   `ENV code_lo=... in_iram=yes` と、解釈結果が `VALID` になったかどうか。
3. 通ったら `data/pie_timing_measured.json` をコミットし、MCP の `measured_timing` /
   `instruction_pipeline` に実測値を載せる。
4. **最後に退避を書き戻す**。戻すのは **`cardputer-s3-20260914T150410Z.bin`**（15:04 の退避。
   09:35 の退避とはアプリ領域が別物＝ユーザーの現行ビルドなので、必ず 15:04 の方を使う）:

```bash
PORT=/dev/ttyACM0 bash tools/restore_flash.sh /workspace/backups/cardputer-s3-20260914T150410Z.bin
```

### ポッド定義への要望（再起動のついでに）

再発する原因は「USB が再列挙すると、コンテナの `/dev/ttyACM0` がホスト側で作り直された新しい inode を
見られなくなる」こと。`--device=/dev/ttyACM0` は**起動時の inode を bind するだけ**なので追随しない。
`-v /dev:/dev`（ホストの /dev を丸ごと見せる）か、`--privileged` 相当にすれば、再列挙後も
コンテナから新しいノードが見えて、ポッド再起動なしで復帰できる（usbipd の attach し直しでも
`ttyACM1` に変わっても追随できる）。今回は「再起動で復帰」の運用で問題ないが、頻度が高いならこれ。

### 16:10 の修正（ノイズ床 → 有効な測定にするための2点）

15:04 の実測は**書込み・取得まで完走し、アンカー5件が全部一致した**（下記）。落ちたのは
パーサの有効性ゲート1つだけ: 距離1のケースで `noise_cycles > 0.5` だった（`anchor_accx_M_to_E` 0.785、
`native_load_use` 0.842）。原因を特定して直した:

1. **タイマ割込みが計測区間に入っていた**。2000反復のループに FreeRTOS のティック（〜10msごと、
   ハンドラ約7µs ≒ 1700サイクル）が1回入ると、その繰り返しだけ +1684 サイクルになる＝
   0.842 サイクル/反復。→ `portDISABLE_INTERRUPTS()` でタイムド領域だけ割込みをマスク
   （dep/indep 両方に同じ条件なので差では相殺される）
2. **計測ループがフラッシュ実行（XIP）だった**。命令フェッチのキャッシュ当たり外れが直接
   CCOUNT 差に乗る。→ 生成アセンブリを `.iram1`（IRAM）に置く。`ENV ... in_iram=yes` で確認できる
   （`xtensa-esp32s3-elf-nm` でも 0x40377xxx に入っていることを確認済み）

あわせて反復を5→9回、`ENV code_lo/code_hi/in_iram/excm_level` を追加。
両修正とも `tools/gen_pie_timing_asm.py`（IRAM セクション）と
`experiments/pie-timing/firmware/main/main.c`（割込みマスク）に入っていて、
生成物は `--check` で一致、ビルドも通っている。**焼くだけの状態**。

### 16:20 の修正3: レポートの頭が消える問題（ハンドシェイク＋ラウンド）

15:04 のログを精査すると、**先頭50行ほどが欠けていた**（`ENV` / `PROBE` / `BEGIN` が無く、
アンカー系の `repeat=0` が数ケース欠けている）。原因は単純で、ファームは全体を数十ミリ秒で
吐き終わるのに、キャプチャはリセットの約1.5秒後に開く。USB-Serial-JTAG 側のバッファ（≈330行）から
古い方が溢れて消えていた。ENV（＝その回が何を焼いていたかの記録）が消えるのは証跡として致命的なので直した:

- ファームは**ホストからの1バイトを待ってから**レポートを出す（`select()` で最大10秒、来なければ走る）
- ホストは**ポートを開いた直後に1バイト送る**（`tools/capture_serial.py`）。つまり読み手が構えてから
  書き始めるので、頭から入る
- 1バイトごとに**レポートを丸ごと再出力**（最大5ラウンド）。ポートを落として再試行しても、
  次の1バイトで新しい完全なレポートが出る（従来は再試行しても何も出なかった）
- ラウンドをまたいでも `MEAS` の重複は「反復が増える」だけで、行（ノイズ床の単位）は増えない。
  これは `tools/selftest_pie_timing_parser.py` に3ラウンドの合成ログを足して固定した

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
- 対処: **USB を attach 済みの状態でポッドを再起動する**（これが唯一の復旧手段。外からの `chmod 666`
  も、usbipd で attach し直すのも効かなかった — コンテナが握っているのは再列挙前の inode で、
  `mountinfo` の該当行が `root=/ttyACM0//deleted` になっている。ホスト側は `crw-rw-rw- root dialout`
  なのにコンテナ側は `c---------` のままで、両者は別 inode）。
- 再発防止として、書込みは `esptool ... --after no-reset`（`idf.py flash` ではなく）、読み戻しも
  `--before no-reset --after no-reset` にして、チップのリセットは capture 側（DTR/RTS）に任せる形に
  変更済み。USB の再列挙は書込み→取得の間で起きるとこの事故を招くため、その間は意図的に列挙を動かさない。
  それでもアプリが起動しない場合は、`--after hard-reset` で1回だけリセットして取り直す（フォールバック）。

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

## 2026-09-14 15:04 の実測記録（ファームは動いた。足りないのはノイズ床だけ）

`tools/device_experiment.sh`（当時の `--after hard-reset` 版）が **退避→ゲート→ビルド→書込み→
読み戻し照合→シリアル取得→解釈** まで完走した。書込み後にアプリ区間を読み戻してハッシュ照合も通っている。

- ログ: `/workspace/backups/pie-timing-20260914T150410Z.log`（322行、`END` 到達）
- 測定: 31件（アンカー5種×距離 + LD.QR/ST.QR/MV.QR/LD_QR_reads_as/QR_load_to_QR_op + alone 8件）
- アンカーは**5件すべて期待どおり**: `anchor_accx_M_to_E` 期待1→実測1.056、`anchor_qs_M_to_E` 1.000、
  `anchor_qr_E_to_E` 0→0.000、`native_load_use` 1→1.000、`native_alu_use` 0→0.000
- 落ちたのは有効性ゲートだけ: 距離1の 2件で `noise_cycles` が 0.5 超（0.785 / 0.842）
- 生データを見ると、ノイズは**1回の反復だけが +1684 サイクル**という形（例: 17999 が 4回、
  19681 が 1回）。これは「2000反復のループに 1回だけ割込みが落ちた」形そのもの

### デバイスの中身について（重要）

15:04 の退避（`cardputer-s3-20260914T150410Z.bin`）は **09:35 の退避とは別物**だった
（アプリ領域 0x10000-0x310000 のほぼ全バイトが差分 = 別ビルド。パーティション表は同じで
`skk_dict` / `jp_font` / `storage` 付き = ユーザーの pocketjs 系ビルド）。09:50 に焼いた計測ファームが
走っていた（アプリ版 cedaa9b-dirty、panic ループ）ので、**その後どこかで現行ビルドが焼き直されている**。
書き戻すときは必ず 15:04 の退避を使うこと。

| 退避 | 意味 |
|---|---|
| `cardputer-s3-20260914T093526Z.bin` | 当時の「元の中身」。09:35 時点 |
| `cardputer-s3-20260914T150410Z.bin` | **今回の書き込み直前の中身。戻すならこれ** |

## ホスト側から焼く経路（コンテナが死んでいるときの本線）

コンテナの `/dev/ttyACM0` は起動時の inode を bind するだけなので、USB 再列挙で削除済み inode を
指したままになる（mode 0000・open 不可・内側からは復旧不能）。**WSL ホスト側の /dev はカーネルが
作り直すので常に生きており、再列挙も無害。** そこでホストで焼いて、ログを bind 共有に落とす:

```bash
uv run --no-project --with esptool --with pyserial python -m esptool version   # 依存は uv に任せる
bash /workspace/esp32s3-hw-mcp/tools/host_flash_and_log.sh                     # 以後は uv を自動で使う
```

`tools/host_flash_and_log.sh` は esptool + pyserial だけを要求する（ファームはコンテナ側で
ビルド済み、IDF 不要）。識別 →（任意で全8MB退避＋構造ゲート）→ 書込み（`--after no-reset`）→
アプリ区間の読み戻し＋sha256照合 → アプリ起動 → レポート取得（ハンドシェイク1バイト、ENDが無ければ
1回だけ再リセットして再取得）→ `parse_pie_timing.py` で解釈まで自走し、ログを
`<repo 親>/backups/pie-timing-<stamp>.log`（＝コンテナの `/workspace/backups/...`）に置く。
実行前後で `/dev/ttyACM0` の inode を比べ、「再列挙でノードが作り直されたか」を verdict として出す。

- `--restore <dump>` で書き戻し（`tools/check_flash_dump.py` のゲート付き）
- `--no-flash`（焼かずにログ取りのみ）/ `--backup` / `--dry-run` / `--uv`（uv を強制）/ `--port` / `--out-dir`
- 依存の探し方: ESP_PYTHON → .venv-host → ~/.venv-esp → IDF env → python3 → **uv**（無ければ作り方を表示）

uv しか無い環境では `tools/capture_serial.py` 単体も使える（PEP 723 のヘッダで pyserial を宣言済み）:

```bash
uv run --no-project tools/capture_serial.py --port /dev/ttyACM0 --out /workspace/backups/log.txt
```
