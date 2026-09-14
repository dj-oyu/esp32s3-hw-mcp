# 実機で PIE のインターロックを測る（Phase 3 の設計と手順）

対象: Cardputer ADV（ESP32-S3FN8）。目的は2つ。

1. **TRM から作ったモデルを実機で検算する**。`data/pie_pipeline.json`（Table 1.7-2）と
   `analyze_sequence` の規則（1.7.1）が、シリコン上で本当にその通りか。
2. **Table 1.7-2 が載せていない3命令（`LD.QR` / `ST.QR` / `MV.QR`、p301-303）の段を実測で決める**。
   一次情報にはハザード段の記載が無いので、これは推定ではなく測定として記録する。

## 事前に分かったこと（ソフトだけで確認できたもの）

### アセンブラは3命令とも受け付ける

```
EE.ANDQ q2, q0, q1        -> dd3024
LD.QR  q0, a5, 0          -> cd2054
ST.QR  q0, a5, 16         -> cd6154
MV.QR  q2, q0             -> 9f0004
```

- **QR レジスタはアセンブラでは `q0`〜`q7`**。TRM の役割名（`qx`/`qy`/`qa`/`qs`/`qu`/`qs0`…）は
  この binutils では**受け付けられない**（`qs0` は "bad register number: s0" になる）。
  各命令の構文行にある役割名は、その命令のなかでの役割であって、レジスタ番号ではない。
- `ld.qr` / `st.qr` の即値は 16バイト単位（`tie-asm.h` の実使用例でも 0,16,…,112 を使っている）。

### TRM の命令語フィールド表はエンコーダとして使える（検証済み）

Table 1.8 の `Instruction Word` を MSB→LSB の順に連結すると、アセンブラの出力と**ビット単位で一致**する。
例: `EE.ANDQ q2, q0, q1` → TRM の `11 qa[2:1] 1101 qa[0] 011 qy[2:1] 00 qx[2:1] qy[0] qx[0] 0100` に
qa=2, qx=0, qy=1 を入れると `1101 1101 0011 0000 0010 0100` = `dd3024` ✓ 実測と一致。
`LD.QR q0, a5, 0` も `cd2054` で一致 ✓。→ 一次情報だけから命令語を組み立てられる
（アセンブラが対応しない命令に遭遇しても詰まらない）。

### ESP-IDF 側に段の情報は無い

`components/xtensa/esp32s3/include/xtensa/config/tie.h` にはコプロセッサの構成（`XCHAL_CP_NUM=2`,
CP3 = `cop_ai`）と保存領域の記述はあるが、**命令の段・レイテンシは書かれていない**。
`tie-asm.h` は文脈切り替えの `st.qr`/`ld.qr` 実装で、QR 8本 × 16バイト = 128バイトの
保存順（q0@+80, q1@+96, q2@+112, q3@+0, …）が分かるだけ。段の推定材料にはならない。
→ **実測以外に道が無い**（ESP-IDF を突き合わせても段は分からない）。

## 測定の設計

`experiments/pie-timing/cases.json` が測定ケースの唯一の定義で、`tools/gen_pie_timing_asm.py` が
それを実アセンブリ（`experiments/pie-timing/firmware/main/measure.S`）に変換する。
C では「2命令を1サイクル差で並べる」ことを保証できないので、測定対象は手書き生成のアセンブリで固定する。

各ケース・各発行間隔 d について**2つの関数**を生成する:

```
m_<case>_d<d>_dep    : 生産命令 ; (d-1)個の独立フィラー ; 消費命令(生産先を読む)
m_<case>_d<d>_indep  : 同じ命令数・同じ生産命令 ; 消費命令は無関係なレジスタを読む
```

各関数は `<iterations>` 回ループし、`rsr.ccount` の差を返す。1反復あたりのインターロックは

```
stall = (min_cycles_dep - min_cycles_indep) / iterations
```

（反復の最小値をとる = 割り込み等の外乱が最も少ない回を使う）。ループのオーバーヘッドと
WAW/WAR のパターンは dep/indep で同一なので差し引きで消える。

**ゲート（ここが肝）**: TRM が段を明記しているケースを「アンカー」として入れ、測定が
マニュアルどおりに出なければ**その回の結果は無効**として扱う（`valid: false`）。

| アンカー | 期待 | 根拠 |
|---|---|---|
| `EE.LD.ACCX.IP` → `EE.ST.ACCX.IP` | 1ストール | ACCX def@2(M) / use@1(E)（Table 1.7-2） |
| `EE.VRELU.S16` → `EE.MOV.S16.QACC` | 1ストール | qs def@2(M) / use@1(E)（同） |
| `EE.ANDQ` → `EE.ANDQ`（同じ q を読む） | 0ストール | qa def@1(E) / use@1(E)（同） |
| `l32i` → `add`（ネイティブ） | 1サイクル | 基本ISAのロード使用（校正用） |
| `add` → `add`（ネイティブ） | 0サイクル | 校正用 |
| ノイズ床 | 1サイクル未満 | 0ストールを主張するための前提 |

アンカーが通ったときだけ、QR 3命令の段を次で導出する（消費側の段は Table 1.7-2 から既知）:

- `LD.QR` の def 段 = 1 + stall(d=1)（消費は `EE.ANDQ` の use@1）
- `MV.QR` の def 段 = 1 + stall(d=1)（同上）
- `ST.QR` の use 段 = (実測した `LD.QR` の def 段) − stall(d=1)
- `LD.QR` のアドレス（`as`）use 段 = 1 − stall(d=1)（生産側は `EE.LD.128.USAR.IP` の as def@1）

## 手順

```bash
# デバイスが見える場所（コンテナなら --device=/dev/ttyACM0 で起動した中）で:
PORT=/dev/ttyACM0 BACKUP_DIR=/workspace/backups bash tools/device_experiment.sh
```

順序は固定で、途中で止まる条件も入れてある。

1. `esptool flash_id` でチップ素性を記録
2. **フラッシュ全8MBを退避**（`/workspace/backups/cardputer-s3-<日時>.bin`）＋ sha256
3. 退避ファイルの構造検査（`tools/check_flash_dump.py`）。ここが通らなければ**焼かない**
4. ビルド → 書き込み → シリアル取得（`END` まで）
5. `tools/parse_pie_timing.py` で解釈し `data/pie_timing_measured.json` を作る（アンカーが外れたら exit 1）

戻し方:

```bash
PORT=/dev/ttyACM0 bash tools/restore_flash.sh /workspace/backups/cardputer-s3-<日時>.bin
```

### 退避の検査が実際に見ているもの（2026-09-14 に修正）

初回実行でこのゲートが**自分の退避を拒否した**（0x1000 に `0xE9` が無い）。原因は退避ではなく検査側で、
ブートローダのオフセットに **ESP32 クラシックの 0x1000** を書いていた（ESP32-S3 は ROM が 0x0 から
第2段ブートローダを読む。esptool の `targets/esp32s3.py: BOOTLOADER_FLASH_OFFSET = 0x0`）。
0x1000 はブートローダ本体の途中（文字列 `/bootloader_support/` が居る場所）だった。

いまのゲートは次を検証する（`tools/selftest_check_flash_dump.py` が11ケースで検査自体を検査する）:

- サイズが期待どおり（既定 8MB）
- チップごとの正しいオフセット（`esp32s3` = 0x0）にある**ブートローダ**のマジック 0xE9 とチップID = 9
- 0x8000 の**パーティション表**（マジック 0xAA50、ラベル重複なし、8MB 内に収まる）
- パーティション表が指す**アプリ区間**に、同じチップ向けのイメージが実在すること
- イメージが末尾に持つ**自身の SHA-256**（body を再ハッシュして一致すること）。ここが通るという事は、
  退避したバイト列がそのファーム自身のハッシュと一致している ＝ 読み出しが欠けてもずれてもいない、という事。

実測（この端末）: `bootloader @0x0 digest True / app @0x10000 digest True`、アプリは
`cardputer_pocketjs 1-55-g97bd403-dirty`（ESP-IDF v6.0.1、2026-09-14 13:52:53 ビルド）で、
`skk_dict` / `jp_font` / `storage` のデータ区間を持つ。**この退避は上書き前の唯一のコピー**。

## 結果の扱い（一次情報との区別）

- 実測値は `data/pie_timing_measured.json` に入り、`provenance.kind = "measured_on_hardware"` を持つ。
  **PDF 由来の `data/pie_pipeline.json` とは別ファイルにし、混ぜない**。
- MCP サーバーは「マニュアルに書かれている（tier-1）」と「このシリコンで測った（tier-2）」を
  区別して返す。`LD.QR`/`ST.QR`/`MV.QR` はマニュアルに段が無いので、実測値が付けば
  `citation` は「measured」となり、根拠はログ行になる。
- 嵌まっていない限界: 資源ハザード（1.7.2）と制御ハザード（1.7.3）、複数命令の重畳、
  CPU周波数・フラッシュキャッシュ状態による変動。1チップ・1周波数の結果であることも明記する。
