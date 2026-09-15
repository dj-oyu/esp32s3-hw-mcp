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

### ここまでで潰した罠（ABI / EXCCAUSE / アドレス歩行）

- **windowed ABI**: 関数ポインタ経由（`call8`）で呼ばれるアセンブラ関数は `entry`/`retw` を対にすること。
  裸の `ret` は命令としては正しいが、呼び出し側のレジスタ窓が回ったままになる。症状は「コンパイルも
  書込みも通り、実機でだけ別の命令が fault する」— 実際は2回目の呼び出しの引数が壊れていた
  （a2 が 3 になり `EE.LD.ACCX.IP a3, 0` がアドレス0を読む）。`tools/gen_pie_timing_asm.py` は
  `entry a1, 32` + `retw.n` を出し、`ret` を出したら生成器が落ちるようにしてある。
- **アドレス歩行**: `EE.LD.ACCX.IP` / `EE.ST.ACCX.IP` / `EE.LD.128.USAR.IP` は `as` を
  ポストインクリメントする。生成側は1反復ごとに `as` を差し直す（`mov a3, a2` / `addi a4, a2, 16`）。
  無いと256バイトのバッファを毎反復16バイトずつ越えていき、2000反復で32KB先を触る。差し直しは
  dep/indep 両方に入るので差分には効かない。ついでに触る範囲が32バイト（1キャッシュライン）に固定され、
  歩き回る場合のようなD-cacheストリーミングが乗らない。
- **ESP32-S3 の EXCCAUSE 28 は LoadProhibited ではない**。S3（LX7）の `core.h` では
  28 = `XCHAL_EXCCAUSE_LOAD_CACHE_ATTRIBUTE`、29 = STORE_CACHE_ATTRIBUTE。ESP-IDF のパニック
  メッセージ（32系の名前が残っている）は「LoadProhibited」と出すので、名前ではなく
  `components/xtensa/esp32s3/include/xtensa/config/core.h` の値で読むこと。
  ちなみにコプロセッサ無効は 32+3 = 35（PIE は `tie.h` の CP3 = `cop_ai`）。

`experiments/pie-timing/cases.json` が測定ケースの唯一の定義で、`tools/gen_pie_timing_asm.py` が
それを実アセンブリ（`experiments/pie-timing/firmware/main/measure.S`）に変換する。
C では「2命令を1サイクル差で並べる」ことを保証できないので、測定対象は手書き生成のアセンブリで固定する。

各ケース・各発行間隔 d について**2つの関数**を生成する:

```
m_<case>_d<d>_dep    : 生産命令 ; (d-1)個の独立フィラー ; 消費命令(生産先を読む)
m_<case>_d<d>_indep  : 同じ命令数 ; (d-1)個の独立フィラー ; 消費命令(無関係な先を読む)
```

（indep の生産側は既定で dep と同じ。`producer_indep` を書いたケースだけ、生産側のアキュムレータを
差し替える — QACC のように「依存しない消費者」が存在しない対のため。issue #1 の節を参照。）

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

## issue #1 の結果: QACC_H/QACC_L の def→use（2026-09-15 実測）

対象: `EE.VMULAS.U16.QACC` / `EE.VMULAS.S16.QACC` の直後に QACC を読む命令を置いたときのストール。
予測（TRM Table 1.7-2 の QACC_H/L def 2(M) / use 1(E) と 1.7.1 の `D = max(SA - SB + 1, 0)`）は **D=2
= 1サイクル**。測定はこの予測に対して **当たり／外れが分かれた**。

| 消費者 | 予測 | 実測（発行間隔1、2000反復あたり） | D 予測→実測 | 判定 |
|---|---|---|---|---|
| `EE.SRCMB.S16.QACC`（U16 の生産者） | 1 | **0** | 2 → 1 | **外れ** |
| `EE.SRCMB.S16.QACC`（S16 の生産者） | 1 | **0** | 2 → 1 | **外れ** |
| `EE.VMULAS.U16.QACC`（対照、def/use とも段2） | 0 | 0 | 1 → 1 | 当たり |
| `EE.ST.QACC_L.L.128.IP`（QACC_L を読む第2の消費者） | 1 | **1** | 2 → 2 | 当たり |

つまり「QACC_H/QACC_L の def→use は必ず1サイクル」ではなく、**消費命令によって変わる**。同じシリコンで
同じ生産者（`VMULAS.U16.QACC`）から、`EE.ST.QACC_L.L.128.IP` は予測どおり1サイクルを払い、
`EE.SRCMB.S16.QACC` は何も払わない。cardputer-adv-pocketjs の 17 か所（`EE.VMULAS.*.QACC` →
`EE.SRCMB.S16.QACC` の隣接）は、この実測の範囲では**予測された1サイクル/ブロックを失っていない**。

### 測定の設計: 双子プロデューサ（`producer_indep`）

`EE.SRCMB.S16.QACC` は定義上必ず QACC_H/QACC_L を読むので、この対には「依存しない消費者」が存在しない。
そこで dep/indep の差を「消費命令のコスト差」から切り離すため、**消費者を両変種で同一に固定し、生産者の
アキュムレータだけを変える**（`EE.VMULAS.U16.QACC` ↔ `EE.VMULAS.U16.ACCX`、命令クラスと命令数は同じ）:

```
dep   : EE.ZERO.QACC ; EE.VMULAS.U16.QACC q0,q1 ; EE.SRCMB.S16.QACC q2,a3,0
indep : EE.ZERO.QACC ; EE.VMULAS.U16.ACCX q0,q1 ; EE.SRCMB.S16.QACC q2,a3,0
```

これが判定を決めた。既存の形（indep 側を `EE.ANDQ` にする）で同じ対を測ると **1.000 サイクル**が出るが、
その数字はインターロックではない:

- `qacc_HL_vmulas_u16_to_srcmb_s16_andq_indep` = 1.000（既存パターン）
- `calib_srcmb_s16_vs_andq_cost` = 1.000 ← **両変種とも QACC 依存が無い**のに 1.000

後者は校正ケースで、生産者を両変種とも `EE.VMULAS.U16.ACCX` にしたもの。依存が存在しないので、この
1.000 は `EE.SRCMB.S16.QACC` と `EE.ANDQ` の**1反復あたりのコスト差**（同じ 2000 反復で 19999 対
17999）でしかない。よって「dep と indep で別命令を使う」従来の形は、消費命令のコスト差を定数として
インターロックに混ぜ込む（アンカーが正しく出たのは `EE.ST.ACCX.IP` / `EE.LD.ACCX.IP` / `EE.ANDQ` の
コストがたまたま揃っていたため）。`cases.json` では、この混入があるケースに `predict_confound` を付け、
`tools/parse_pie_timing.py` が `predictions[].confound` としてそのまま返す。

### 予測が外れた理由（**ここは仮説**、実測ではない）

1. `EE.SRCMB.S16.QACC` は自分の実行のなかで QACC を読む時点が表の「use 1(E)」より遅い、または
   QACC 用のバイパスが存在し、stage 2 の def が stage 1 の use にそのまま間に合っている。
2. Table 1.7-2 の `EE.SRCMB.S16.QACC` の QACC 段が実際のハードウェアと合っていない（use が実質 2 なら
   D=1 でストール0になる）。
3. `EE.ST.QACC_L.L.128.IP` 側は表のとおりに1サイクル払うので、「段の差がストールになる」機構自体は
   存在する。したがって 1. か 2. は「命令ごとの実装差」であって測定系の問題ではない。

切り分けに必要な追加測定（未実施）: `EE.SRCMB.S16.QACC` の直後に別の QACC 消費者を置いた場合、
`EE.MOV.S16.QACC`（QACC を書く側）との WAW、`EE.SRCMB.S8.QACC` 版、複数反復での重畳。

### この回の証跡と限界

- ログ `pie-timing-20260915T011819Z.log`（910行、`END` 到達）。flash 直後にアプリ区間を読み戻し、
  ビルドした `pie_timing.bin`（sha256 `be4f5908…`）と一致を確認済み。
- アンカー5件は全部一致（ノイズ床 最大 0.113 サイクル、ゲートは 0.5）。`valid: true`。
- 限界1: レポートの先頭（`ENV` ブロック）が両回とも取得側で落ちた。`data/pie_timing_measured.json` の
  `provenance.environment` は空。IRAM 実行の確認は `xtensa-esp32s3-elf-nm` で `m_*` が 0x4037xxxx に
  あること（ホスト側の静的確認）と、上記の読み戻し一致で代用している。
- 限界2: 1チップ・1周波数（240MHz）・1つのループ形（1反復＝7命令、ループ先頭で `as` を差し直す）での
  測定。`alone` 系はどの命令でも 7.0 サイクル/反復で、1命令あたりのコストは分解できない（校正ケースが
  その代わり）。
- 限界3: `qacc_HL_vmulas_u16_to_srcmb_s16_andq_indep` の 1.000 は上記のとおり混入値。ケースは残して
  ある（従来パターンの落とし穴を記録するため）が、数字をインターロックとして引用してはいけない。

## 結果の扱い（一次情報との区別）

- 実測値は `data/pie_timing_measured.json` に入り、`provenance.kind = "measured_on_hardware"` を持つ。
  **PDF 由来の `data/pie_pipeline.json` とは別ファイルにし、混ぜない**。
- MCP サーバーは「マニュアルに書かれている（tier-1）」と「このシリコンで測った（tier-2）」を
  区別して返す。`LD.QR`/`ST.QR`/`MV.QR` はマニュアルに段が無いので、実測値が付けば
  `citation` は「measured」となり、根拠はログ行になる。
- 嵌まっていない限界: 資源ハザード（1.7.2）と制御ハザード（1.7.3）、複数命令の重畳、
  CPU周波数・フラッシュキャッシュ状態による変動。1チップ・1周波数の結果であることも明記する。
