# 08 — Cardputer ADV でメディア表現と 3D を回すための物差し

`examples/` のカーネルを「動く」から「速い」に進めるための段取り。対象は Cardputer-Adv（Stamp-S3A /
ESP32-S3FN8、8 MB flash、**PSRAM なし**、1.14" LCD、ES8311 コーデック＋NS4150B アンプ＋1W スピーカ、
BMI270 IMU、56 キー）。PSRAM が無いので、フレームバッファも作業バッファも内部 SRAM から出すが、
**「512 KB ある」ではなく「実際に空いている量」で数える**（cardputer-adv-pocketjs の実測、
`CLAUDE.md:84`）:

- この機体で使える **DRAM は約 334 KiB**（`docs/vm-L2-design.md:717` も同値）。ホーム画面の空きヒープは
  **実測 274 KiB**（`idle_free=280,932`、Wi-Fi リンク後）。内訳は静的 DIRAM を 197,847 → 111,383 B まで
  削った結果で、**削減のたびに動く数字**（測り直しは同プロジェクトの `tools/memlog.py --check`）。
- **Wi-Fi をリンクするだけで空きヒープが約 37 KiB 減る**（`.bss` だけでなく `.data` と IRAM 常駐コード。
  S3 では DRAM と同じプール）。`esp_netif_deinit()` は IDF v6.0.1 で `ESP_ERR_NOT_SUPPORTED` なので、
  一度起動すると約 4.8 KiB は戻らない（`CLAUDE.md:91`）。
- JS ゲストの上限は **144 KiB**（`CLAUDE.md:84`）。ただし `docs/vm-L2-design.md:717` は 160 KiB と書いて
  おり、**この 2 つは一致していない**（どちらが現在かをここで断定しない）。

## フレーム予算の考え方

**バスが先に飽和する。** 画面 135×240（Cardputer 系のパネル。ADV の型番と SPI 実効速度は実機で
測って確定させる）として、1 フレームは 64800 px × 2 B = **129.6 KB**。転送時間は推定をやめて実測を引く
（cardputer-adv-pocketjs `docs/pie-simd.md:686-687`、ホーム画面の内訳行）:

```
LEVEL WAVE     mode=0 fps=30.3 draw=21.11 | prep=0.99 loop=1.75 (kernel=1.67) hud=2.63 send=15.68
OCEAN + STARS  mode=1 fps=30.3 draw=19.93 | prep=1.06 loop=0.89 (kernel=0.69) hud=2.44 send=15.47
```

ただし**この表の絶対値は数割高い**（同 :702。計測スクリプトが押す Enter が効果音を鳴らし、その合成が
フレーム時間に混ざっていた）。修正後・SPI 80 MHz の実測は **`turn 1.62 / render 7.92 / send 7.67` ms**
（同 :702）で、**転送の値は SPI 設定とセットで引くこと**（`board.c` が起動時に `LCD SPI %d Hz` を
出しているのはこのため）。つまり:

| 目標 | 使える時間 | CPU（PIE）側に許される量 |
|---|---|---|
| 30 fps | 33.3 ms/フレーム | 転送 7.7〜15.7 ms を引いて ~18〜26 ms。64800 px なら **約 67〜95 サイクル/px**（240 MHz）。**この予算には実動作係数 1.3〜1.4 を先に掛けておく**（`docs/pie-simd.md:145,175`。タスク切替時のコプロセッサ3の退避・復元ぶん）|
| 20 fps | 50 ms/フレーム | ~35 ms。全画面で 129 サイクル/px |
| 60 fps（部分更新前提） | 16.7 ms/フレーム | 全画面転送だけで埋まる → **差分更新か、解像度を落とすか、二重バッファをやめるか**の設計判断になる |

なので最初に測るべきは「1 フレームを何 ms で送れるか」で、そのあと各段の cycles/px を足し上げて
予算に収まるかを判定する。PIE のカーネルはこの表の「CPU 側」に入る数字を作る仕事。

## 測り方（今回のサンプルで確立した形式）

- `BENCH <kernel> <要素>=<個数> cycles_pie=<N> cycles_c=<N>` をファームが出す。C 側は **-O2** で
  ビルドする（`examples/firmware/main/CMakeLists.txt`。ESP-IDF の既定 -Og のままだと C を不当に
  弱く見せる）。
- サイクルは `ex07_ccount()`（`rsr.ccount`）で挟んで差分。要素は「頂点」「ピクセル」など意味のある
  単位に正規化して出す。
- ホスト側の `tools/check_examples_log.py` が `cycles/要素` と C との比をレポートに出す。
- 正しさは C 参照と Python 参照の二重チェック（数値が合わないカーネルの速さは意味がない）。

## パイプラインの各段と担当命令

| 段 | 命令 | 状況 |
|---|---|---|
| 頂点変換（4×4） | `EE.VSMULAS.S16.QACC`（8 頂点並列、係数を 1 レーン broadcast）+ `EE.SRCMB.S16.QACC`（飽和読み出し） | **ex07** |
| 透視除算 | PIE に除算は無い → 逆数表 or 近似。**表引きは `EE.LDXQ.32`（1命令1レーン）で、`EE.LD.QR` は索引ロードではない**（`LD.QR` は `load128(as + imm)` = `docs/pie-simd.md:19,44`、`LDXQ.32` は `as + qs[lane]*4`） | 草案 ex13（proposed/）|
| クリップ／範囲制限 | `EE.VMIN/VMAX.S16`、判定は `EE.VCMP.GT/LT` + `EE.ANDQ/ORQ` でマスク合成 | ex08（clamp） |
| シェーディング（色×光） | `EE.VMUL.S16`（SAR シフト付き乗算）、`EE.VADDS/VSUBS.S16`（飽和加減） | **ex08** |
| ブレンド／残像 | `EE.ANDQ` + `EE.VMUL.S16`（ones + SAR=1 で >>1）+ `EE.VADDS.S16` = RGB565 ハーフブレンド 5 演算/8px | **ex08** |
| 動き補償（ブロック SAD、ハーフペル） | `|a-b|` は `EE.VMAX/VMIN/VSUBS.S16`（SAD 命令は無い）、合算は `EE.VMULAS.U16.ACCX` を「1のレーン」に対して + 40bit を `RUR.ACCX_0/1` で1回だけ読み出し。ハーフペルは `EE.VADDS.S16`×2（飽和する丸め加算）+ `EE.VMUL.U16`（SAR=1） | **ex10**（未実機） |
| ブロック変換（8×8、分離可能） | `EE.VSMULAS.S16.QACC`（係数1レーンを broadcast、還元インデックス側）+ `EE.SRCMB.S16.QACC`（シフト + 16bit 飽和読み出し） | **ex11**（未実機） |
| 物理・衝突 | 積分は `EE.VADDS.S32`×2 + `EE.VMIN/VMAX.S32`（飽和加減で半陰的オイラー）、AABB は `EE.VCMP.LT/GT.S16` + `EE.ORQ` + `EE.NOTQ`、距離² は `EE.VMULAS.S16.QACC` + `RUR.QACC_L/H_*`（または `SRCMB` の Q16 読み出し） | **ex12**（未実機） |
| スプライト合成 | マスク（`VCMP` + `ANDQ/ORQ`）＋上記ブレンド | 未着手 |
| フレームバッファ転送 | `EE.SRCQ.128.ST.INCP`（非整列寄せ、`SAR_BYTE`）+ GDMA/SPI | 未着手 |
| 音声ミックス | `EE.VMULAS.S16.ACCX`（飽和アキュムレータ）+ `EE.SRS.ACCX`（シフト読み出し） | ex02/ex05 の応用 |
| スペクトル | `EE.FFT.R2BF.S16` / `EE.CMUL.S16` / `EE.FFT.AMS.S16.*` | プリミティブ確定済（ex06）、多段は未着手 |

## 量産リスト（次のサンプル候補）

**番号の繰り下げ（追記）**: 動き補償・8×8 ブロック変換・物理と衝突の3本を先に書いたので、この3本を
**ex10 / ex11 / ex12** として採用し、下の未着手ぶんは **ex13 以降へ一つずつ繰り下げた**（旧 ex09〜ex16 →
新 ex13〜ex20、1対1）。既存の ex01〜ex09 の番号は変わらない（ex09 = QACC 読み出し間合いのプローブ）。

### 採用済み（実装 + ホスト検証済み、実機は未実行）

| 例 | 中身 | 契約の要点 | 状態 |
|---|---|---|---|
| **ex10** motion | ブロック SAD（8 × uint16 レーンを `EE.VMULAS.U16.ACCX` で合算）。`|a-b|` は `EE.VSUBS.S16(EE.VMAX.S16, EE.VMIN.S16)`（SAD も ABS も無い）。ハーフペル行は `out[i] = (uint16_t)(a[i]+b[i]+1) >> 1` を `EE.VADDS.S16`×2 + `EE.VMUL.U16`（SAR=1）で | SAD は `out[0]=ACCX[31:0]`, `out[1]=ACCX[39:32]` の2語で返す（合計は 40bit のまま）。8bit サンプル（0..255）では教科書 SAD と一致、フルレンジ uint16 レーンは契約外で、差は `DATA` 行に出す | ビルド + ホストチェッカー通過 / **実機未実行** |
| **ex11** block8x8 | 8×8 int16 ブロック変換 `out[k][j] = sat16((Σ_i coef[k][i]·block[i][j]) >> shift)`（MP3/JPEG/H.264 の内側の形）。`EE.VSMULAS.S16.QACC` の broadcast 側を還元インデックスに、行内8レーンを自由インデックスに置くので**この向きは転置不要** | coef / block / out は row-major 64 int16（k 行は +16k、i 行は +16i）。もう一方の軸は転置係数表で2回目を呼ぶ。40bit アキュムレータは1行では飽和しない（余裕 6bit）が、16bit 読み出しは飽和する（小さい shift の `DATA` で固定） | 同上 |
| **ex12** physics | 半陰的オイラー（4×int32 レーン = 8物体/呼び出し、`EE.VADDS.S32`×2 + `VMIN/VMAX.S32` クランプ）、AABB 分離軸テスト（8箱ずつ、`VCMP.LT/GT.S16` + `ORQ` ×5 + `NOTQ` ×1）、距離²（8点ペアを `VMULAS.S16.QACC` で各レーンに合算。`RUR.QACC_L/H_*` の厳密読み出し版と `SRCMB` の Q16 読み出し版） | 箱は plane-major（6面 × 16B、96B/8箱、要素は `96*(i/8)+16*p+2*(i%8)`）、点も plane-major（3面 × 16B、48B/8点）。`out` は16バイト整列。`|Δ| > 32767` は飽和、int32 出力は `minu` で 2^31-1 にクランプ | 同上 |

**実機の前に読むこと**: ex11 / ex12 の QACC 系（`EE.VSMULAS.S16.QACC` / `EE.SRCMB.S16.QACC` /
`RUR.QACC_L_*`）は ex09 で間合い（`mac1_min_gap`）がモデルに一致せず（-1）、マニュアルの擬似コード通りに
動く前提で書いてあった。

**2026-09-15 の実機ランで、値の側は確定した**: ex11 は 64 係数が、ex12 は 19 点の距離² と Q16 読み出し
16 ペアが、どちらもファーム内 C 参照とホスト側 Python 参照に一致した（ログ
`/workspace/backups/pie-examples-20260915T032511Z.log`、`checks_ok=29 checks_fail=0` / ホスト側 73/73）。
同じログで ex09 の間合いは `mac1_min_gap=0`・`mac1_model_matched_at_g6=1` になっており、**直前のランで
-1 だった理由は未追跡**（あのランは ex12 がクラッシュする中間ファームだったので、フレーム上端のスロット
上書き = notes/10 の不具合と無関係とは言い切れない）。ex10 は ACCX 系（ex02/ex05 で確定済み）なので
前提は薄い。

### 草案あり（`examples/firmware/main/proposed/`、未統合・未実機）

ex13〜ex21 は 3 分野（コーデック / 2D-3D / VM）で並行に書いている。**この節の番号が正**で、下の未着手ぶんは
ex22 以降へ繰り下げた。

| 例 | 分野 | 中身 |
|---|---|---|
| ex13 | 3D | 透視除算＋逆数表（`LDXQ.32` の 256×int32 表、近平面は `inv==0` の定義済みマーカー）＋クリップマスク |
| ex14 | 2D | スパン塗りラスタライザ（128bit ストアはチャンクが完全に内側のときだけ。頭と尾はスカラー）× DDA の飽和クリップ |
| ex15 | 2D | 非整列フレームバッファ転送（`EE.SRCQ.128.ST.INCP` + `SAR_BYTE`、4つの整列ケース）＋ `copy_row` / `fill_row` の下界 |
| ex16 | codec | CELT IMDCT の pre-rotate（`EE.CMUL.S16` + `VZIP/VUNZIP`、twiddle は `LDXQ.32`）＋ TDAC の窓重ね |
| ex17 | codec | MP3 の 18 点 IMDCT（ACCX 逐次 / QACC 並列の選択理由）＋周波数反転つき窓重ね＋32 サブバンド合成 |
| ex18 | codec | Opus のステレオ MS→L/R（`stereo_merge` の丸め・飽和順序と突き合わせ）＋ピッチコンブフィルタ |
| ex19 | 2D | スプライト合成（マスク付き不透明 / 半透明 / カラーキーを `VCMP` + `ANDQ/ORQ/NOTQ` で分岐レスに）|
| ex20 | 3D | 深度テストの書き込みマスク、辺係数のセットアップ、属性補間 |
| ex21 | VM | `var_buf` の `JS_UNDEFINED` 一括充填、ポインタ配列の NULL 充填、非ゼロ探索、JSValue 一括コピー（**16B 整列の契約が現状の VM に無い**ことを明記する提案）|

### 未着手（繰り下げ後）

1. **ex22 QACC 8 並列**: `EE.SRCMB.S16.QACC` の詰め方（16bit 飽和）を確定し、8 本の FIR/IIR を
   同時に走らせる（8 チャンネル音声、あるいは 8 レーンの行列積）。
2. **ex23 音声ミキサ**: 複数チャンネルの飽和ミックス（`VMULAS.S16.ACCX` + `SRS.ACCX`）と、
   そのまま I2S へ渡せる形の検証。
3. **ex24 FFT 多段**: 8 点 → 32 点。sel2=0/1 のレーン並びは確定済みなので、段ごとに中間を出して積む。
4. **ex25 LCD 転送の実測**: SPI/GDMA で 1 フレームを送る時間（PIE ではなくバスの物差し）。上の表の
   予算を実測値に置き換える。

## サンプルの足し方（1 本あたりの手順）

量産を回すための定型。今回 ex07/ex08 を入れたときに通った道:

1. `examples/firmware/main/exNN_<topic>.S` にカーネル（`.iram1`、関数ごとに `.align 4`、`entry`/`retw.n`）。
   **定数は C 側からポインタで渡す**（`.iram1` から `.rodata` への `l32r` はリンカが拒否する）。
2. `examples.h` に宣言（引数と戻りの意味をコメントで書く）。
3. `main.c` に `exNN()`: 決定論的な入力 → カーネル → C 参照 → `CHECK` → `BENCH` → `DATA` で入出力を
   全部出す（ログ 1 枚で Python が再計算できるように）。
4. `tools/check_examples_log.py` に `check_exNN()`（Python で三度目の計算）と `CHECKS` への登録。
5. `tools/selftest_examples_checker.py` の合成ログに `DATA` 行と、そのフィールドを壊す変異を 1 つ足す。
6. `notes/08` の表と `examples/README.md` の一覧を更新。
7. `bash tools/build_examples.sh` → `tools/host_flash_and_log.sh --examples`。

## 実測で分かっている落とし穴（設計に効く順）

- **128bit アクセスは下位 4bit を落とす**（TRM p49、実機で再現）。非整列の窓は `LD.128.USAR.IP` +
  `SRC.Q`（第 1 オペランドが qs0＝下位）で作るか、整列バッファへ staging する。
- **ACCX も QACC も飽和する**（実機で確認: 2^39-1 で頭打ち）。明るさ加算やミックスで「飽和する前提」の
  レンジ設計が要る。飽和を避けたい所は `VMUL` の SAR で先に落とす。
- **PIE に除算は無い。** 3D の透視除算・正規化は逆数表＋乗算で組む。
- **`VMULAS` 系の accumulator 書き込みは Table 1.7-2 の `def` に無い** → ストール見積りは 0 と言う。
  実際の内側ループは実測で確かめる（ex02 の BENCH がその第 1 例）。
- **`SRCMB.S16.QACC` は「8 レーンをシフトして 16bit 飽和で詰める」**（疑似コードで確認）。QACC 経路の
  読み出しはこれを使う。

### cardputer-adv-pocketjs の実機で分かっていること（同プロジェクト `docs/pie-simd.md` の出典ページ付き）

- **実動作は下限の 1.3〜1.4 倍**（:145, :175。1命令=1サイクルで見積もった 5.28 ms が実測 6.9〜7.4 ms）。
  タスク切替ごとのコプロセッサ3（q0〜q7・ACCX・QACC）の退避・復元ぶん。
- **ビルド間で最大 15% 動く**（:718。波カーネルが 1.30 ms と 1.54 ms を行き来した。関数配置が変わって
  命令キャッシュ 16 KB（両コア共有）の当たり方が変わる）。**数%の差を主張するには同じバイナリ内の
  スイッチで A/B を取る**か、この揺れを超える必要がある。
- **`-Os` では `memcpy` が関数呼び出しのまま残る**。転送のバイトスワップを 32bit ずつにした版は
  16.5 ms → **24.2 ms に悪化**した（:730）。「明らかに速いはず」の変更は必ず測る。
- **計測スクリプト自身が測定対象を変える**。再描画を促す Enter が効果音を鳴らし、その合成がフレーム時間に
  3〜4 割乗っていた（:706）。キー入力で画面を動かす計測は、その入力の副作用ごと測っている。
- **表引きの値段は本数**。`LDXQ.32` は1命令1レーンで、本数を 0〜24 本まで振っても 1 本あたりの追加
  コストは 0（:76-82）。海面カーネルは正弦表を放物線近似に置き換えて 16 本の `ldxq` ＋ 2 本の `vunzip`
  ＋ 2 本のマスクを消し、**62 → 40 命令 / 1.06 → 0.89 ms**（:48。ただしビット一致は捨てる）。
