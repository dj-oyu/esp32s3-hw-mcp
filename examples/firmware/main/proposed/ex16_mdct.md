# ex16 (proposed) — CELT/Opus の IMDCT pre-rotate と TDAC 重ね合わせ

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex16_mdct.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 6 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `adf537840a4470cfd1d97c3a6be19561` → `4fbfff6053e0a7073a675dd751467472`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex16_mdct.S` — **ビルドに入っていない新規サンプル**です。`main.c` /
`examples.h` / `examples/firmware/main/CMakeLists.txt` は一切触っていません（このディレクトリは
`examples/firmware/main/CMakeLists.txt` のコンパイル対象外なので、ファイルを隣に移して `ex16()` を足すまで
走っているファームは何も変わりません）。**このファイルとここに貼った証拠はすべて実機を使っていません。**

対象は `celt/mdct.c` の `clt_mdct_backward_c`、CELT/Opus デコーダの最内ループです。このループは 4 段:

1. **PRE-ROTATE**: 鏡像の入力ペアを Q15 トワイドルと複素乗算（N4 回）— mdct.c:304-326
2. N/4 点の複素 FFT（**ビット反転順**で） — 本ファイルの範囲外
3. **POST-ROTATE** — 範囲外
4. **TDAC の窓折り返し**「Mirror on both sides for TDAC」— mdct.c:371-386

本ファイルは 1 と 4 の両端、つまり FFT の前段と後段を扱います。6 つの関数:

| 関数 | 何か | ステータス |
|---|---|---|
| `ex16_prerotate(x, tw, out, n4, shift)` | pre-rotate 本体。CELT の表レイアウト（2 本のラン）をそのまま読む | 出荷する形 |
| `ex16_prerotate_z(x, twz, out, n4)` | 同じ演算を、**複素語に並べ替えた**トワイドル表で | 代案（速い） |
| `ex16_prerotate_idx(x, twz, out, n4)` | 同じ演算を、**EE.LDXQ.32**（唯一の索引ロード）で引いて | 代案（索引ロードの値段） |
| `ex16_tw_zip(tw, twz, n4, shift)` | 上 2 つの表を作る一段（モードごとに 1 回） | 補助 |
| `ex16_unzip_pairs(z, r1, r2, n_pairs)` | 複素語 → 2 本のラン（EE.VUNZIP.16） | 補助 |
| `ex16_overlap_add(a, b, out, n)` | TDAC の重ね合わせ `sat16(a+b)`（EE.VADDS.S16） | 出荷する形 |

モードの前提（`modes.c` から）: 48 kHz / 20 ms、`shortMdctSize = frame_size/nbShortMdcts = 960/8 = 120`
(modes.c:359-360)、`overlap = ((shortMdctSize>>2)<<2) = 120` (modes.c:380、`celt_assert(st->overlap == 120)`
が celt_decoder.c:148 にあります)、`clt_mdct_init(&mode->mdct, 2*shortMdctSize*nbShortMdcts, maxLM)` =
`l->n = 2*120*8 = 1920` (modes.c:423)。デコーダの非 transient 経路は `shift = maxLM - LM`
(celt_decoder.c:442-446) なので **shift = 0 のロング窓で N = 1920, N4 = 480**、transient では
`shift = 3` で `N4 = 60`。本ファイルが想定する N4 はこの 4 値（480, 240, 120, 60）です。

---

## 1. 何が確認済みで、何が未確認か (status, honestly)

* **アセンブル**: 通ります。`xtensa-esp32s3-elf-gcc -c`（ESP-IDF v6.0.1 のツールチェーン、GCC 15.2.0）で
  警告なし、**リテラルプールなし・`l32r` 0 本**、`.iram1` は 1 セクション 0x16a バイト（貼付は §10）。
  `.iram1` から `.rodata` への `l32r` はリンカが拒むので、32767 / -32768 のような 12bit 即値に載らない定数は
  `movi -1; srli 17` と `movi -8; slli 12` で作っています（`ex16_overlap_add` の末尾）。
* **符号化**: 使った PIE 命令 9 種すべてを、マニュアルの命令語図とリポジトリ自身の
  `tools/asm_toolchain.py --check-instruction` で突き合わせて **9/9 `match`**（表は §7）。
* **振る舞い**: **実機ではなく、ホスト上の 2 実装の一致**です。`/workspace/pjs-vm/tools/pie/piesim.py` を
  `/tmp` にコピーし、足りない命令を TRM の擬似コードから足したもの（§9）で **.S の 6 関数を丸ごと解釈実行**
  （プロローグ・ループ制御・テール込み）し、ホストの C 参照実装（`ref.c`、gcc -O2）と突き合わせました:
  **prerotate 6768 レーン・overlap_add 1750 レーン・unzip_pairs 1008 レーン、不一致 0 件**
  （45 + 40 + 4 ケース、貼付は §10）。プリローテートは 3 つの入口（2 本のラン / 並べ替え表 / LDXQ）が
  **互いに 1 レーンも違わない**こと、`ex16_tw_zip` が Python で組み立てた複素語表と
  **6768 語すべて一致**することも確認しました（3 通りに書いた同じ演算が一致する = モデルの読解が
  一箇所に依存していない、という弱いが実のある根拠です）。
* **それが証明しないこと**: モデルは私が書いた .S の第二の読みなので、捕まえられるのは「.S がスカラ式と
  食い違う」ことだけです。シリコンが何をするかは言えません。本ファイルが寄りかかっている未確認の意味論は
  §11 に全部列挙しました（とくに `EE.VZIP.16` / `EE.VUNZIP.16` のレーン順は
  `data/pie_examples_measured.json` の `open_after_this_run` が「未計測」と明記している項目です）。
* **サイクル数は 1 つも測っていません**。以下に出てくる数字はすべて「命令数」か「バイト数」で、
  実機の `BENCH` 行を書けるのは実機ランのときだけです。

---

## 2. このカーネルの形を決めている構造上の事実

このサンプルの本体は「どう速く書くか」ではなく「**この命令セットでは 4 レーン並列の pre-rotate が素直には
書けない**」という発見と、その代価を数字にしたことです。

### 2.1 CELT の pre-rotate は両端から歩く → 4 レーン化には反転が要る

mdct.c:304-326:

```c
      const kiss_fft_scalar * OPUS_RESTRICT xp1 = in;
      const kiss_fft_scalar * OPUS_RESTRICT xp2 = in+stride*(N2-1);
      ...
      for(i=0;i<N4;i++)
      {
         x1 = SHL32_ovflw(*xp1, pre_shift);
         x2 = SHL32_ovflw(*xp2, pre_shift);
         yr = ADD32_ovflw(S_MUL(x2, t[i]), S_MUL(x1, t[N4+i]));
         yi = SUB32_ovflw(S_MUL(x1, t[i]), S_MUL(x2, t[N4+i]));
         yp[2*rev+1] = yr;   yp[2*rev] = yi;
         xp1+=2*stride;      xp2-=2*stride;
      }
```

つまり反復 i のペアは `x1 = in[2i]`（i とともに**前へ**進む）と `x2 = in[N2-1-2i]`（i とともに**後ろへ**進む）。
トワイドルは 1 本の表 `l->trig` の 2 つのラン `t[i]` と `t[N4+i]`（mdct.c:60-95 の `clt_mdct_init` が作る）です。

4 反復を 1 つの 128bit レジスタでやるには、4 つの `(X1,X2)` が 32bit の複素語として並ぶ必要があります。
ここで効くのが「**128bit ロードはベース b の要素をレーン L に置く**」という単純な事実です:

* 1 回のロードの中で x1 はレーン番号とともに **昇順**（dm/dL = +1/2）、x2 は**降順**（dm/dL = -1/2）。
* x1 用のロード（ベース b1）と x2 用のロード（ベース b2）を組にして、**すべての k で同じレーン番号**を
  要求すると、`b1+L = 2m` かつ `b2+L = 2*n4-1-2m`、すなわち `L = (2*n4-1-b1-b2)/2` が出ます。
  これは **L が 1 つに決まる**という意味です: どんな b1, b2 を選んでも、4 レーン中 1 レーンしか
  同じ反復に当たりません。したがって 4 レーン形には必ず**どこかでレーン反転**が要ります。

### 2.2 そのレーン反転が、この命令セットには無い（探索の実測）

レーン並べ替えの命令は `EE.VZIP.16` / `EE.VUNZIP.16`（と `.8` / `.32`）だけです。これらが生成する置換群を、
2 レジスタ = 32 バイトスロット上で **BFS で全部列挙**しました（ジェネレータ 6 個、バイトスロット単位＝
レーン内のバイト順も保ったまま）:

```
|group| = 120
4x16b reversal -> R1 lanes0..3     reachable=False
4x16b reversal -> R2 lanes0..3     reachable=False
half swap -> R1                    reachable=False
half swap -> R2                    reachable=True    vz16 vz16 vunz32 vunz16
16b swap/word -> R1                reachable=False
16b swap/word -> R2                reachable=False
8x16b full reversal -> R1/R2       reachable=False
```

読み方:

* **4 レーン反転は到達不能**です（どちらのレジスタでも）。8 レーンの完全反転も届きません。
* 64bit 半分部の入れ替え（half swap）は**片方のレジスタにだけ**届きます（`vz16 vz16 vunz32 vunz16` の 4 命令）。
  ただし欲しいのは 4 レーン反転なので、これは役に立ちません。
* 語内 16bit の入れ替え（post-rotate が読む `re = yp[1], im = yp[0]` に必要な形）は 2 レジスタでは届かず、
  **1 レジスタのコピーを許すと届きます**（コピーは `EE.VADDS.S16 q, qsrc, qzero` = ゼロレジスタとの飽和加算。
  PIE には QR→QR の `mov` が無いのでこれが唯一のコピー手段）。つまり post-rotate の re/im 入れ替えは
  「コピー + `VUNZIP.16` + オペランドを入れ替えた `VZIP.16`」の 3 命令で書けます。
  **4 レーン反転はコピーを許しても届きません。**
* 探索は「レジスタ 2 本」に限った全列挙なので、レジスタを増やせば結論が変わる可能性は残ります。ただし
  2.1 節の「1 レーンしか合わない」は**ロードの構造からの証明**で、こちらはレジスタ本数に依存しません。

### 2.3 結論: 入力は「zipped（鏡像ペア並び）」で来る — 代価は測っていない

`ex16_prerotate` は入力 `x` を **複素語 m = (in[2m], in[2*n4-1-2m])** の並び（zipped）で要求します。つまり
CELT の自然な並びから 1 パス分の並べ替えが要ります。**そのパスは書いていません**（推測）: スカラで素直に
書くと語あたり「2 ロード + 2 ストア + 添字計算 2」程度 = 6 命令/語 → N4 = 480 で約 2880 命令。これは
下のカーネル本体（実行命令数 1094）の **2.6 倍**です。したがって正直な結論は「**この命令セットで
pre-rotate だけをベクタ化するのは割に合わない。並べ替えは前段（周波数領域の合成 = CELT の
`denormalise_bands`）に畳み込むか、カーネルごと別の形にする必要がある**」です。前段が鏡像ペアの並びを
直接書ける（バンドごとの固定添字マップなので可能）なら代価は 0 になりますが、それは**設計上の主張**で、
このファイルでは確かめていません。

### 2.4 反例を実測しておく（negative control）

「素直に 4 レーン化すると何が起きるか」も測ってあります。生の CELT 並びを 2 本の 128bit ロードで読み、
`EE.VUNZIP.16` で even/odd に分け、`EE.VZIP.16` で組にして `EE.CMUL.S16` する 7 命令のループを
piesim で実行し、C 参照と突き合わせると:

```
   negative control n4=8 shift=0: 16 of 16 int16 lanes wrong
   negative control n4=60 shift=0: 120 of 120 int16 lanes wrong
   negative control n4=480 shift=0: 960 of 960 int16 lanes wrong
   negative control n4=8 shift=2: 16 of 16 int16 lanes wrong
   negative control over 4 cases: 1112 of 1112 lanes wrong (100.0%)
```

**全部違います。** これは 2.1 節の式が予言するとおりで（レーン k が 2 つの違う反復の値を掛けてしまう）、
「間違ったベクトル化は静かに間違う」ことの実測です。このループは .S には入っていません（正しくないので）。

---

## 3. A) `ex16_prerotate` — pre-rotate 本体

```c
void ex16_prerotate(const int16_t *x, const int16_t *tw, int16_t *out, uint32_t n4, uint32_t shift);
/* x   : n4 個の複素語（= 2*n4 int16、16 バイト整列）。語 m = (re, im) = (in[2m], in[2*n4-1-2m])
 * tw  : レベル 0 のトワイドル表 l->trig（16 バイト整列）。レベルは mdct.c と同じ算段で選ぶ:
 *         N = 4*n4; N0 = N << shift; レベルブロック = tw + (N0-N) int16
 * out : n4 個の複素語（16 バイト整列）。語 m = (yi, yr)
 * n4  : N/4。契約: n4 % 4 == 0      shift : mdct.c が受け取るレベルシフト
 */
```

* **レイアウト契約**: 入力は上記の zipped 並び、`tw` は CELT のままの 2 本のラン。`out` は
  **`(yi, yr)` の順**で、これは mdct.c 自身の保存順です（mdct.c:324 `yp[2*rev] = yi` と 325 のコメント
  「Storing the pre-rotation directly in the bitrev order.」）。**ビット反転順にはしていません** —
  CELT は `2*l->kfft[shift]->bitrev[i]` に書きますが（表は kiss_fft.c:499-502 が作ります）、
  本カーネルは自然順に書くので、**消費側は CELT の DIT ではなく自然順（DIF）の FFT** でなければなりません。
  これは呼び出し側が知るべき唯一の食い違いです。
* **演算**: 命令列はレーンごとに（int32 で計算し、`EE.CMUL.S16` の「シフト結果の下位 16bit」として書く）

```
      yi = (X1*t1 - X2*t2) >> 15        t1 = t[m],  t2 = t[n4+m]
      yr = (X1*t2 + X2*t1) >> 15
```

  `EE.CMUL.S16` の擬似コード（TRM p80）は 32bit 語の下位 = `qx[15:0]*qy[15:0] - qx[31:16]*qy[31:16]`、
  上位 = `qx[15:0]*qy[31:16] + qx[31:16]*qy[15:0]` なので、`qx` の語を `(X1, X2)`、`qy` の語を `(t1, t2)` に
  すると **`(yi, yr)` がそのまま出ます**（`sel4 = 0` が下位 64bit、`sel4 = 1` が上位 64bit）。
  この読みは `data/pie_examples_measured.json` の
  `cmul_complex_multiply_with_sar_shift`（実機確定: 「re = ac - bd, im = ad + bc」「SAR[5:0] の算術右シフト」
  「sel4 は lanes 0..3 / 4..7 を選ぶ」）と一致します。SAR = 15 は Q15 トワイドルのためです
  （`celt_coef` は `opus_val16`、`COEF_ONE` は `Q15ONE` = arch.h:198-200、`S_MUL` は `MULT16_32_Q15`
  = fixed_generic.h:55）。実機で測られた CMUL の SAR は 0 と 12 で、15 は同じ族の外挿です（§11）。
* **どこで CELT と食い違うか（数値の限界）**: CELT は `yi`/`yr` を 32bit で持ち続けますが、このカーネルは
  16bit で書きます。`CMUL` は飽和せず**下位 16bit を残すだけ**（`VMUL.S16` が飽和せず truncate することは
  `data/pie_examples_measured.json` の `vmul_truncates_it_does_not_saturate` で実機確定）なので、
  `-2^30 <= v <= 2^30-1` の範囲では完全一致、外側では折り返します。実行での内訳（入力の値族ごと、
  2256 レーンずつ）:

  | 値族 | `(v>>15)` が int16 を出るレーン |
  |---|---|
  | フルレンジ（`|x| <= 32767`, `|t| <= 32767`） | **62 / 2256** |
  | 小さい入力（`|x| <= 4096`、トワイドルはフルレンジ） | **0 / 2256** |
  | mdct.c の形のトワイドル（`cos(2π(j+0.125)/N)`）、`|x| <= 8192` | **0 / 2256** |

  つまり**フルレンジのランダム入力でだけ折り返し**が出ます。「実コーデックの値域なら 0」は
  この 3 つ目の族（トワイドルだけ本物の形にして入力を小さく振ったもの）での結果で、実際のコーデックが
  どんな値域を出すかは測っていません（推測の域を出ません）。C 参照側の集計行
  `product-form mismatches=62, lanes whose (v>>15) leaves int16=62` が同じ 62 を別経路で数えています。
* **コスト（静的情報）**: ループ本体 9 命令（うち PIE 7）/ 4 複素レーン = **2.25 命令/レーン**。ループ本体は
  24 バイトなので `loopgtz` の 256B 制限には余裕があります。

---

## 4. トワイドル表の引き方の値段（このファイルの主題の片割れ）

「表引きは何で引くか」は、PIE に**索引ロードが `EE.LDXQ.32` 1 つしかなく、しかも 1 命令 1 レーン**という
制約の下での選択です。3 つの形を**全部書いて、全部測って**あります（実行命令数は piesim のカウント、
N4 = 480 = 120 グループ、§10 の貼付と同じ実行）。

| 形 | ループ本体 | 1 グループ | 実行命令数（N4=480、1 呼び出し） | 追加 RAM |
|---|---|---|---|---|
| **`ex16_prerotate`（出荷）**: CELT の 2 本のランを `EE.VLD.L.64.IP` ×2 + `EE.VZIP.16` | 9（PIE 7） | 2.25/レーン | **1094** | 0 |
| **`ex16_prerotate_z`**: 並べ替え済み表を `EE.VLD.128.IP` ×1 | 7（PIE 5） | 1.75/レーン | **846** | 4*n4 B（= 1920 B）+ 構築 732 命令/モード |
| **`ex16_prerotate_idx`**: 同じ表を `EE.LDXQ.32` ×4 | 11（PIE 9、うち LDXQ 4） | 2.75/レーン | **1341** | 同上 |

* **索引ロードの値段は 1 レーン 1 命令**です。4 レーン分のトワイドルを引くのに 4 命令かかり、
  128bit ロード 1 本（`ex16_prerotate_z`）に対して **+3 命令/グループ = +495 命令/フレーム（+58%）**。
  逐次ウォークの表では **LDXQ は使う理由がありません**。使う理由があるのは
  (a) 索引がデータ依存（ビット反転順、バンド順、transient の `stride != 1` 形）のとき、
  (b) 表が 32bit 複素語で、どのランから来るかが実行時に決まるとき、です。
  本ファイルは (b) の形を 1 つ書いて測ってあります。
* **`ex16_prerotate_z` は 1 フレームあたり 248 命令（1094 → 846）得**します。代価はモードごとに 1 回の
  732 命令と 1920 バイト。**3 フレーム程度で元が取れます**が、RAM が厳しい（このプロジェクトは libopus の
  static DIRAM 0 バイトを自慢にしている）なら出荷形の 1094 のままにする判断もあります。
* **却下した形: 2 本のランを 128bit で読む。** 2 つの理由があり、どちらも算術です:
  1. **位相**: 2 本のランは `2*n4` バイト離れています。これが 16 の倍数になるのは `n4 % 8 == 0` のときだけで、
     transient の **N4 = 60 では 120 バイト = 8 バイトずれ**ます。128bit ロードはアドレス下位 4bit を
     落とす（TRM p49、実機確定: `vld128_drops_the_low_address_bits`）ので、**静かに隣の 16 バイトを読んで
     違う組を掛けます**。64bit ロードの強制は下位 3bit なので、この問題が起きません（本ファイルはこちらを
     採用）。なおバイト粒度のファネル（`EE.LD.128.USAR.IP` + `EE.SRC.Q`、実機確定:
     `src_q_operand_order_and_the_funnel_path`）でも直せますが、16 バイトの窓に**2 ロード + 1 命令**が要り、
     下の 2 と合わせて割に合いません。
  2. **半歩**: 128bit ロードは 1 本のランから 8 エントリ取りますが、4 レーンのグループが消費するのは 4 です。
     残り 4 はレジスタの上位半分にあり、**半分部を下げる手段が無い**（2.2 節の探索）ので捨てるしかなく、
     次のグループのためには 8 バイト進めたいのにロードの整列が 16 バイト刻みになります。

---

## 5. B) `ex16_overlap_add` — 窓掛けと TDAC の重ね合わせ

```c
void ex16_overlap_add(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n);
/* out[i] = sat16(a[i] + b[i])   a, b, out は 16 バイト整列。n は任意（n % 8 はスカラテール） */
```

`a` と `b` は**すでに窓を掛けられた 2 つの半分部**（前フレームの下降側の裾と、現フレームの上昇側の頭）です。
**窓はこのカーネルには入っていません**（そういうシグネチャだからだけでなく、正しい切り分けだからです）:
mdct.c では窓は折り返しと同じ場所で入ります（mdct.c:383
`*yp1++ = SUB32_ovflw(S_MUL(x2, *wp2), S_MUL(x1, *wp1))`、同じ形が celt_decoder.c:613-617 の
`prefilter_and_fold` にもあります）。そして PIE には**16bit レーンのシフト命令がありません**（
`docs/pie-simd.md`）ので、Q15 窓を掛けたものをカーネル内で戻すことはできません。窓と乗算は呼び出し側に
あり、ここは純粋な和 — だからこそ**飽和の問いに答えられます**。

### CELT の C 実装は飽和するか → **しない**（出典付き）

* 窓折り返しと IMDCT の加減算は `SUB32_ovflw` / `ADD32_ovflw`（mdct.c:371-386 の TDAC ループ）と `ADD32`
  （celt_decoder.c:613-617）。**このツリーがコンパイルしている唯一のビルド = FIXED_POINT**
  （`components/opus/CMakeLists.txt:67` の `OPUS_BUILD FIXED_POINT VAR_ARRAYS=1 DISABLE_FLOAT_API`）では、
  これらは**32bit で折り返す**演算です: `ADD32_ovflw(a,b) = (opus_val32)((opus_uint32)a+(opus_uint32)b)`
  (fixed_generic.h:157-158)、`ADD32` は素の `+` (fixed_generic.h:152)。**飽和しません。**
* IMDCT 経路で唯一飽和するのは、**IMDCT 出力全体に対する 1 回の 32bit クランプ**です:
  celt_decoder.c:504-508「Saturate IMDCT output so that we can't overflow in the pitch postfilter ...」の
  `SATURATE(out_syn[c][i], SIG_SAT)`、`SIG_SAT = 536870911 = 2^29-1` (arch.h:215)。これは
  `clt_mdct_backward` が戻った後で、積ごとでも半分部ごとでもありません。
* そのビルドでは `celt_sig` は `opus_val32` (arch.h:144) なので、CELT が足している 2 つの半分部は 32bit で、
  int16 の入力ペアではそもそも 32bit を溢れようがありません。

**したがって `EE.VADDS.S16`（±32767/-32768 でレーンごとに飽和、TRM p146）は「CELT の足し算を飽和命令で
書いたもの」ではありません。** 真の和が int16 に収まる範囲では CELT と一致し、外側では CELT がそのまま
32bit で持ち回る値をクランプします。その食い違いを数字にしてあります（モデル実行、`n ∈ {1, 3, 7, 8, 16, 60, 120, 135}` の 40 ケース、
1750 レーン）:

* **飽和したレーン: 326**（フルレンジ族 + `{32767, -32768, ±30000, ±16384}` の端ケース族）。
  326 レーンすべてで int16 の結果は厳密な和と違います（当たり前ですが、数えたのは「飽和した」ではなく
  「食い違った」の数です）。
* **in-domain のレーン: 1424、うち不一致 0**。`|a|,|b| <= 16383` の族では 1 レーンも飽和しません。
* 最悪の例（算術）: `a = b = 32767` → 厳密な和 65534、カーネルは **32767**、CELT の読みは **65534**
  （32bit に収まり `SIG_SAT` 未満なのでクランプされない）。
* スカラテール（`n % 8`）は `l16si`/`add`/`min`/`max`/`s16i` でベクタ経路と同じクランプを書いてあり、
  in-domain の 1424 レーンでベクタ経路とテールが一致しています（`n = 1, 3, 7` のケースを含む）。
* **クランプを避けたい呼び出し側**は 32bit 経路を持てます: `EE.VADDS.S32`（4 レーンの int32、TRM p149）で
  レーン幅が半分になりますが、CELT と同じ「折り返す 32bit 和 + 最後に 1 回だけ `SIG_SAT`」を再現できます。

---

## 6. 補助の 2 つ

* **`ex16_tw_zip(tw, twz, n4, shift)`**: レベル 0 の表から「複素語に並べ替えた」トワイドル表を作ります
  （モードごとに 1 回）。本体は `EE.VLD.L.64.IP` ×2 + `EE.VZIP.16` + `VST` = 6 命令（PIE 4）/ 4 語。
  N4 = 480 で 732 命令、表は 1920 バイト。これが `ex16_prerotate_z` の代価です。
* **`ex16_unzip_pairs(z, r1, r2, n_pairs)`**: 複素語を 2 本のランに戻します。`EE.VUNZIP.16` 1 命令で
  2 レジスタ分（8 語）が割れます（TRM p290: qs0 側が lanes 0,2,4,6、qs1 側が 1,3,5,7）。7 命令（PIE 5）/
  8 語 = 0.875 命令/語。**de-interleave は 1 命令でできるのに、逆向き（順序が逆の 2 本を組にする）は
  できない** — 2.2 節の非対称性がこの 2 つの関数で具体的になっています。

---

## 7. 命令ごとの出典 (per instruction: page and quote)

`data/pie_instructions.json` の `source_page`（マニュアルの命令節の開始ページ）と、リポジトリ自身の
`tools/asm_toolchain.py --check-instruction` による命令語図との突き合わせ（`manual == toolchain` は
アセンブラとマニュアルのビット図が一致を意味します）:

| 命令 | TRM | 構文 | Operation（引用） | manual vs toolchain |
|---|---|---|---|---|
| `EE.CMUL.S16` | p80 | `EE.CMUL.S16 qz, qx, qy, 0..3` | `qz[15:0] = (qx[15:0]*qy[15:0] - qx[31:16]*qy[31:16]) >> SAR[5:0]; qz[31:16] = (qx[15:0]*qy[31:16] + qx[31:16]*qy[15:0]) >> SAR[5:0]; ...` | match, manual aede24 == toolchain aede24 |
| `EE.VZIP.16` | p293 | `EE.VZIP.16 qs0, qs1` | `qs0[15:0]=qs0[15:0]; qs0[31:16]=qs1[15:0]; qs0[47:32]=qs0[31:16]; qs0[63:48]=qs1[31:16]; ...` | match, manual fc53b4 == toolchain fc53b4 |
| `EE.VUNZIP.16` | p290 | `EE.VUNZIP.16 qs0, qs1` | `qs0[31:16]=qs0[47:32]; qs0[47:32]=qs0[79:64]; qs0[79:64]=qs1[15:0]; ...` | match, manual fc5384 == toolchain fc5384 |
| `EE.VLD.128.IP` | p164 | `EE.VLD.128.IP qu, as, -2048..2032` | `qu[127:0] = load128({as[31:4],4{0}}); as += {20{imm16[7]},imm16[7:0],4{0}}` | match, manual e38064 == toolchain e38064 |
| `EE.VLD.L.64.IP` | p168 | `EE.VLD.L.64.IP qu, as, -1024..1016` | `qu[63:0] = load64({as[31:3],3{0}}); as += {21{imm8[7]},imm8[7:0],3{0}}` | match, manual e98064 == toolchain e98064 |
| `EE.VST.128.IP` | p275 | `EE.VST.128.IP qv, as, -2048..2032` | `qv[127:0] => store128({as[31:4],4{0}}); as += ...` | match, manual ea8064 == toolchain ea8064 |
| `EE.LDXQ.32` | p113 | `EE.LDXQ.32 qu, qs, as, 0..3, 0..7` | `vaddr0 = as + qs[15:0]*4; ... dataIn[31:0] = load32({vaddrN[31:2],2{0}}); qu[32*sel4+31:32*sel4] = dataIn` | match, manual e1759d7f == toolchain e1759d7f |
| `EE.VADDS.S16` | p146 | `EE.VADDS.S16 qa, qx, qy` | `qa[15:0] = min(max(qx[15:0] + qy[15:0], -2^15), 2^15-1); ...` | match, manual aede64 == toolchain aede64 |
| `EE.VLDBC.32` | p173 | `EE.VLDBC.32 qu, as` | `qu[127:0] = {4{load32({as[31:2],2{0}})}}` | match, manual edf764 == toolchain edf764 |

補足:

* `EE.VLD.L.64.IP`（と `EE.VZIP.16` / `EE.VUNZIP.16`）は、**このリポジトリでビルドに入っている 13 個の
  `examples/firmware/main/*.S` には使用例がありません**（`grep -l VLD.L.64 examples/firmware/main/*.S`、
  `grep -l 'VZIP\|VUNZIP' examples/firmware/main/*.S` がどちらも 0 件）。8 バイトの整列強制（下位 3bit）は、
  実機で確かめられている 128bit 形の 16 バイト強制と同じ種類の挙動ですが、64bit 形そのものは未計測です（§11）。
  （`proposed/` には同時期に足された別のサンプルが 64bit ロードや VZIP を使っていますが、それらも実機未実行で、
  本ファイルの意味論の根拠にはしていません。）
* `EE.LDXQ.32` は 220 命令中**唯一の索引ロード**です（索引ストアは `EE.STXQ.32`、TRM p145、本ファイルでは
  未使用）。`data/pie_examples_measured.json` の timing 側は「索引ロードも含めて 1 命令 1 サイクル」と
  pjs-vm 側が報告していますが、**レーンの意味論**はマニュアル由来です。

---

## 8. 実測コスト（オブジェクトファイルから）

`xtensa-esp32s3-elf-objdump -d` の命令数とバイト数:

```
ex16_prerotate: total 26 instructions, 7 EE.*
   loop body 0x02c..0x043: 9 instructions (7 EE.*), 24 bytes     <- 4 複素レーン/回
ex16_prerotate_z: total 13 instructions, 5 EE.*
   loop body 0x055..0x066: 7 instructions (5 EE.*), 18 bytes
ex16_prerotate_idx: total 32 instructions, 11 EE.*
   loop body 0x0a0..0x0c1: 11 instructions (9 EE.*), 34 bytes
ex16_tw_zip: total 21 instructions, 4 EE.*
   loop body 0x0f0..0x0fe: 6 instructions (4 EE.*), 15 bytes
ex16_unzip_pairs: total 11 instructions, 5 EE.*
   loop body 0x10c..0x11d: 7 instructions (5 EE.*), 18 bytes     <- 8 語/回
ex16_overlap_add: total 27 instructions, 4 EE.*
   loop body 0x12c..0x13a: 6 instructions (4 EE.*), 15 bytes     <- 8 レーン/回
   loop body 0x14c..0x165: 11 instructions (0 EE.*), 26 bytes    <- スカラテール
```

（`ex16_prerotate` と `ex16_tw_zip` には 7 バイトの 3 命令のループがもう 1 つ出ます = `shift` から
`N0 = N << shift` を作るループです。）

実行命令数（piesim のカウンタ、N4 = 480, shift = 0 = 120 グループ、プロローグ込み）:

```
   ex16_prerotate          1094 instructions =  9.12 per four-lane group (120 groups)
   ex16_tw_zip              732 instructions =  6.10 per four-lane group (120 groups)
   ex16_prerotate_z         846 instructions =  7.05 per four-lane group (120 groups)
   ex16_prerotate_idx      1341 instructions = 11.18 per four-lane group (120 groups)
   ex16_unzip_pairs         424 instructions =  7.07 per four-lane group (60 groups, 480 words)
```

**これらは命令数であってサイクル数ではありません。** このリポジトリの pjs-vm 側の実測則
（`docs/pie-simd.md`: PIE は命令種別を問わず 1 命令 1 サイクル発行、128bit ストアだけ +0.6、
実動作は 1.3〜1.4 倍）を当てにいくなら、`ex16_prerotate` は 120 グループで 1094 + 120*0.6 ≈ 1166 サイクル
相当が下限で、フレーム周期 20 ms × 48 kHz × 2 ch に対して小さい値です。ただし**静的依存の検出
（`stalls.py`）は C のインラインアセンブリを読む道具で、.S には使えない**ので、本ファイルの並びの
ストールは「M 段の def を直後の E 段で使わない」ように置いただけの目視です（§11）。

---

## 9. モデルと参照実装（比べた 3 つ）

### `ref.c` — スカラ参照（ホスト gcc -O2、`main.c` に入れるならこれ）

```c
/* ex16_prerotate_c: 命令列がやることそのもの。v_i = X1*t1 - X2*t2, v_r = X1*t2 + X2*t1 を int32 で出し、
 * CMUL と同じく「シフト結果の下位 16bit」として書く（飽和ではない）。レベルブロックは
 * tw + 4*n4*((1<<shift)-1) int16 —— mdct.c の "for (i=0;i<shift;i++){N>>=1; trig+=N;}" を telescope した形。 */
const int16_t *prerotate_level(const int16_t *tw, uint32_t n4, uint32_t shift);
void ex16_prerotate_c(const int16_t *x, const int16_t *tw, int16_t *out, uint32_t n4, uint32_t shift)
{
    const int16_t *t = prerotate_level(tw, n4, shift);
    for (uint32_t m = 0; m < n4; m++) {
        int32_t x1 = x[2*m], x2 = x[2*m+1], t1 = t[m], t2 = t[n4+m];
        out[2*m]     = (int16_t)((x1*t1 - x2*t2) >> 15);
        out[2*m + 1] = (int16_t)((x1*t2 + x2*t1) >> 15);
    }
}
/* 32bit のままの読み（CELT が持っている値）と比べて、16bit に書いて失われるレーンを数える */
int ex16_prerotate_exact_c(const int16_t *x, const int16_t *tw, uint32_t n4, uint32_t shift,
                           int32_t *yi, int32_t *yr);
/* テキストブックの式（64bit、飽和）で書いた版。命令列との差 = §3 の 62 レーン */
void ex16_prerotate_formula_c(const int16_t *x, const int16_t *tw, int16_t *out, uint32_t n4, uint32_t shift);

void ex16_overlap_add_c(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n)
{ for (uint32_t i = 0; i < n; i++) out[i] = sat16_ll((int32_t)a[i] + b[i]); }   /* EE.VADDS.S16 */

/* CELT 側の読み: 折り返す 32bit 和 + 最後に 1 回だけ SIG_SAT (536870911) のクランプ */
void ex16_overlap_celt_c(const int16_t *a, const int16_t *b, int32_t *out, uint32_t n);

void ex16_unzip_pairs_c(const int16_t *z, int16_t *r1, int16_t *r2, uint32_t n_pairs);  /* 8 語単位 */
```

### `piesim.py` に足した命令（本体は編集せず `/tmp/ex16check/` にコピー）

`/workspace/pjs-vm/tools/pie/piesim.py` は未対応命令で `NotImplementedError` で止まる約束なので、
TRM の Operation から足しました。足したのは:

| 足したもの | 出典 | 中身 |
|---|---|---|
| `ee.vzip.16` | p293 | `qs0' = [s0_0,s1_0,s0_1,s1_1,s0_2,s1_2,s0_3,s1_3]`、`qs1'` は lanes 4..7 |
| `ee.cmul.s16` | p80 | `sel4[0]` が 64bit 半分部、`sel4[1]` が共役形、`>> SAR` の下位 16bit |
| `ee.vldbc.32` | p173 | 32bit 語を 4 レーンへブロードキャスト |
| `entry` / `retw.n` / `movi` / `slli` / `srli` / `extui` / `add` / `sub` / `min` / `max` / `beqz` / `l16si` / `s16i` / `s32i` / `or` | Xtensa コア | プロローグとスカラテールを実行するため |

足した 2 つの PIE 命令は、マニュアルの Operation をそのまま Python にしたものです:

```python
            elif op == 'ee.vzip.16':
                # TRM 1.8.213 (p293): qs0' = [s0_0,s1_0,s0_1,s1_1,s0_2,s1_2,s0_3,s1_3],
                #                     qs1' = [s0_4,s1_4,s0_5,s1_5,s0_6,s1_6,s0_7,s1_7]
                s0, s1 = list(Q[qi(a[0])]), list(Q[qi(a[1])])
                Q[qi(a[0])] = [z for k in range(4) for z in (s0[k], s1[k])]
                Q[qi(a[1])] = [z for k in range(4, 8) for z in (s0[k], s1[k])]
            elif op == 'ee.cmul.s16':
                # TRM 1.8.13 (p80): sel4[0] = the 64-bit half, sel4[1] = the conjugate form;
                # qz' = (product sum/difference) >> SAR[5:0], kept as the low 16 bits.
                qz, qx, qy, sel4 = qi(a[0]), list(Q[qi(a[1])]), list(Q[qi(a[2])]), int(a[3])
                z = list(Q[qz])
                for w in ((0, 1) if (sel4 & 1) == 0 else (2, 3)):
                    al, ah = s16(qx[2*w]), s16(qx[2*w + 1])
                    bl, bh = s16(qy[2*w]), s16(qy[2*w + 1])
                    if (sel4 & 2) == 0:
                        lo, hi = al*bl - ah*bh, al*bh + ah*bl
                    else:
                        lo, hi = al*bl + ah*bh, al*bh - ah*bl
                    z[2*w] = (lo >> self.sar) & 0xFFFF
                    z[2*w + 1] = (hi >> self.sar) & 0xFFFF
                Q[qz] = z
```

加えて 2 つの「転記の正規化」を入れています（意味論ではなく入力の書き方）: 分岐先の末尾コロンを
任意にした点と、`op.lower()`（.S はマニュアルの大文字表記、pjs-vm のカーネルは小文字）。
`ee.ld.128.usar.ip` / `ee.src.q` はもともと入っていたので（後者は当初 `SAR_BYTE*8` バイトで
シフトしていました — マニュアルは `<<3` の**ビット**表記です。私のコピーでは直しました）、
ファネル経路の検討中に使いました。

### `check.py` — 何をどう比べたか

1. `.S` から 6 関数のアセンブリ本文を抜き出し（複数行コメントは C の規則どおり外して）piesim に丸ごと
   食わせる（プロローグ・ループ・テールまで実行）。
2. 同じ入力で `ref.c` を走らせ、**命令レベルモデル vs スカラ参照**を比べる。
3. プリローテートの 3 入口を**互いに**比べる（同じ演算の 3 通り）。
4. `ex16_tw_zip` の出力を Python で組み立てた複素語表と比べる。
5. **negative control**（生レイアウトの素直なベクトル化、§2.4）を実行して何レーン違うか数える。

ケースは決定的な PRNG（seed `0xE16C0DE`）で、`n4 ∈ {4, 8, 60, 120, 240, 480}` × `shift ∈ 0..3` の
41 組から現実的なものだけを残した 15 組（1 つのモードで意味があるのは (480,0) (240,1) (120,2) (60,3)、
このうちケースに入っているのは (480,0) と (60,3) —— **この 2 つが「ラン 2 の位相が 0」と「8 バイトずれる」
両方の整列クラスを踏むので、§4 の設計判断が実行で確かめられています**）× 3 つの値族
（フルレンジ / 小さい値 / mdct.c の `cos(2π(j+0.125)/N)` 形の実トワイドル）、
`overlap_add` は `n ∈ {1, 3, 7, 8, 16, 60, 120, 135}` × 5 族（うち 2 族は飽和を狙った端の値）、
`unzip_pairs` は `n_pairs ∈ {3, 8, 16, 480}` です。

---

## 10. 実行結果（貼付）

### 10.1 アセンブラとオブジェクトファイル

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex16.o examples/firmware/main/proposed/ex16_mdct.S
rc=0

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex16.o
00000124 00000046 T ex16_overlap_add
00000000 00000048 T ex16_prerotate
0000006c 0000005a T ex16_prerotate_idx
00000048 00000023 T ex16_prerotate_z
000000c8 0000003b T ex16_tw_zip
00000104 0000001e T ex16_unzip_pairs

$ xtensa-esp32s3-elf-objdump -h /tmp/ex16.o | grep -E 'Idx|iram1|literal'
Idx Name          Size      VMA       LMA       File off  Algn
  3 .iram1        0000016a  00000000  00000000  00000034  2**2

$ xtensa-esp32s3-elf-objdump -d /tmp/ex16.o | grep -c l32r
0
```

### 10.2 `objdump -d` の該当箇所（3 つのプリローテート入口）

```
00000000 <ex16_prerotate>:
   0:  004136          entry       a1, 32
   3:  1175e0          slli        a7, a5, 2
   6:  208770          or          a8, a7, a7
   9:  008616          beqz        a6, 15 <ex16_prerotate+0x15>
   c:  1188f0          slli        a8, a8, 1
   f:  ffc662          addi        a6, a6, -1
  12:  ff6656          bnez        a6, c <ex16_prerotate+0xc>
  15:  c08870          sub         a8, a8, a7
  18:  1188f0          slli        a8, a8, 1
  1b:  338a            add.n       a3, a3, a8
  1d:  1195f0          slli        a9, a5, 1
  20:  a39a            add.n       a10, a3, a9
  22:  f60c            movi.n      a6, 15
  24:  130360          wsr.sar     a6
  27:  418250          srli        a8, a5, 2
  2a:  889c            beqz.n      a8, 46 <ex16_prerotate+0x46>
  2c:  830124          ee.vld.128.ip  q0, a2, 16
  2f:  898134          ee.vld.l.64.ip q1, a3, 8
  32:  9901a4          ee.vld.l.64.ip q2, a10, 8
  35:  dc13b4          ee.vzip.16     q1, q2
  38:  9e8804          ee.cmul.s16    q3, q0, q1, 0
  3b:  9e8814          ee.cmul.s16    q3, q0, q1, 1
  3e:  9a8144          ee.vst.128.ip  q3, a4, 16
  41:  880b            addi.n      a8, a8, -1
  43:  fe5856          bnez        a8, 2c <ex16_prerotate+0x2c>
  46:  f01d            retw.n

00000048 <ex16_prerotate_z>:
  48:  004136          entry       a1, 32
  4b:  f60c            movi.n      a6, 15
  4d:  130360          wsr.sar     a6
  50:  418250          srli        a8, a5, 2
  53:  289c            beqz.n      a8, 69 <ex16_prerotate_z+0x21>
  55:  830124          ee.vld.128.ip  q0, a2, 16
  58:  838134          ee.vld.128.ip  q1, a3, 16
  5b:  9e8804          ee.cmul.s16    q3, q0, q1, 0
  5e:  9e8814          ee.cmul.s16    q3, q0, q1, 1
  61:  9a8144          ee.vst.128.ip  q3, a4, 16
  64:  880b            addi.n      a8, a8, -1
  66:  feb856          bnez        a8, 55 <ex16_prerotate_z+0xd>
  69:  f01d            retw.n
```

（`ex16_prerotate_idx` / `ex16_tw_zip` / `ex16_unzip_pairs` / `ex16_overlap_add` は
`xtensa-esp32s3-elf-objdump -d /tmp/ex16.o` の出力にそのまま入っています。LDXQ の 4 連は
`ee.ldxq.32 q1, q7, a3, 0, 0` … `3, 3` の 4 命令、`ex16_overlap_add` のベクタ本体は
`ee.vld.128.ip` ×2 / `ee.vadds.s16` / `ee.vst.128.ip` の 15 バイト、そのあとスカラテールが
`l16si`/`add`/`min`/`max`/`s16i` で 26 バイト続きます。）

### 10.3 ホストでの等価実行（piesim vs C 参照、`check.py` の出力そのまま）

```
extracted asm text: prerotate=615 B  prerotate_z=330 B  prerotate_idx=739 B  tw_zip=490 B  unzip_pairs=292 B  overlap_add=625 B
REF cases: P=45 O=40 U=4
REF prerotate: product-form mismatches=62, lanes whose (v>>15) leaves int16=62
REF overlap: saturated lanes=326 (of which the int16 image differs from the exact sum: 326), in-domain lanes=1424 (mismatches among them: 0), max|a|=32768 max|b|=32768
   negative control n4=8 shift=0: 16 of 16 int16 lanes wrong
   negative control n4=60 shift=0: 120 of 120 int16 lanes wrong
   negative control n4=480 shift=0: 960 of 960 int16 lanes wrong
   negative control n4=8 shift=2: 16 of 16 int16 lanes wrong

ex16_mdct.S host check: the .S executed by piesim vs the C reference in ref.c
cases: P(prerotate x3) = 45, O(overlap_add) = 40, U(unzip_pairs) = 4
lanes compared: prerotate 6768, overlap 1750, unzip 1008
mismatches: prerotate(model vs C ref) 0, overlap 0, unzip 0
prerotate entry points against each other: two-run(CELT layout) vs zipped table 0, two-run vs LDXQ gather 0
ex16_tw_zip vs the Python-built complex table: 0 mismatching cases out of 6768 words
prerotate lanes whose exact (v >> 15) does not fit int16, by value family (Python count, independent of ref.c):
   full      62 of  2256 lanes
   small      0 of  2256 lanes
   real       0 of  2256 lanes
negative control over 4 cases: 1112 of 1112 lanes wrong (100.0%)

instructions EXECUTED for a full call (n4 = 480, shift = 0; the interpreter's count, which includes the prologue and the per-group loop overhead):
   ex16_prerotate          1094 instructions =  9.12 per four-lane group (120 groups)
   ex16_tw_zip              732 instructions =  6.10 per four-lane group (120 groups)
   ex16_prerotate_z         846 instructions =  7.05 per four-lane group (120 groups)
   ex16_prerotate_idx      1341 instructions = 11.18 per four-lane group (120 groups)
   ex16_unzip_pairs         424 instructions =  7.07 per four-lane group (60 groups)
```

`REF prerotate: product-form mismatches=62` は「命令列の読み（16bit truncate）」と「テキストブックの式
（64bit 飽和）」の**食い違いを数えたもの**で、この 2 つは一致しません（§3 の 62 レーンと同じ数）。
`REF overlap` の行も同じで、飽和したレーン数と、そのうち厳密な和と食い違うレーン数を分けて出しています。

### 10.4 命令語図との突き合わせ（§7 の表の元出力）

```
EE.CMUL.S16      p80   match  asm=EE.CMUL.S16 q5, q6, q7, 2      manual=aede24 toolchain=aede24
EE.VZIP.16       p293  match  asm=EE.VZIP.16 q5, q6              manual=fc53b4 toolchain=fc53b4
EE.VUNZIP.16     p290  match  asm=EE.VUNZIP.16 q5, q6            manual=fc5384 toolchain=fc5384
EE.VLD.128.IP    p164  match  asm=EE.VLD.128.IP q5, a6, -2048    manual=e38064 toolchain=e38064
EE.VST.128.IP    p275  match  asm=EE.VST.128.IP q5, a6, -2048    manual=ea8064 toolchain=ea8064
EE.VLD.L.64.IP   p168  match  asm=EE.VLD.L.64.IP q5, a6, -1024   manual=e98064 toolchain=e98064
EE.LDXQ.32       p113  match  asm=EE.LDXQ.32 q5, q6, a7, 2, 2    manual=e1759d7f toolchain=e1759d7f
EE.VADDS.S16     p146  match  asm=EE.VADDS.S16 q5, q6, q7        manual=aede64 toolchain=aede64
EE.VLDBC.32      p173  match  asm=EE.VLDBC.32 q5, a6             manual=edf764 toolchain=edf764
```

---

## 11. 検査が捕まえたもの (what the model-vs-reference run actually caught)

**5 件すべて、貼ってある実行の前に直しました。3 件は .S 側の本物のバグです。**

1. **複数行コメントが命令を飲んでいた。** `ex16_prerotate_idx` のインデックスベクタの初期化で、
   `s16i a6, a1, 0` の後ろに開いた `/* ... */` を次の行の `movi` / `s16i` / `movi` の後ろまで伸ばして
   しまい、C のコメント規則どおり**その 3 命令が消えていました**（アセンブラも同じ規則なので
   エラーにならず、黙って「index = [0, x, y, 3]」になっていた）。piesim は .S のテキストを C の規則で
   コメント除去してから実行するので、**初回実行で MISMATCH として出ました**。
2. **LDXQ のインデックスは 16bit レーンであって 32bit 語ではない。** `EE.LDXQ.32 qu, qs, as, sel4, sel8`
   は `qs[16bit レーン sel8] * 4` を加算するので、4 つのグループ添字は**連続する 16bit レーン**に
   置く必要があります（4 つの 32bit 語の下位に置くと `[0, 0, 1, 0]` を読む）。これは実装当初の誤りで、
   同じくモデル実行が捕まえました（`q7 index vector: [4, 5, 6, 7, ...]` を出力させて確認）。
3. **ファネルのポインタが 2 倍進んでいた。** 128bit の窓をバイト粒度で読む案（`LD.128.USAR.IP` ×2 +
   `SRC.Q`）では、グループごとに窓は 16 バイト進むのに 2 本のロードがそれぞれ 16 進むので 32 バイト
   進んでいました。**n4 = 4（1 グループ）では気づかず、n4 = 8（最初の 2 グループ）で落ちました** —
   端だけのケースでは通ってしまう種類のバグで、複数グループのケースを最初から入れておいたことが効きました。
   （この案自体はその後、位相と半歩の問題で 64bit ロード案に置き換えています。§4。）
4. **2 本のランの 128bit 読みは静かに間違う**（設計時の解析で発見）: `2*n4 % 16 == 8` になる
   N4 = 60 で 2 本目のランが 16 バイト格子から 8 バイト外れ、128bit ロードは下位 4bit を落とすので
   **隣の 16 バイトを読んで別の組を掛けます**。実機で確かめられている
   `vld128_drops_the_low_address_bits` と同じ罠で、**この場合は例外にも警告にもならず、出力だけが
   静かに違います**。64bit ロード（強制は下位 3bit）に変えて解決。
5. **ツール側のバグ 2 件**（.S ではなく私の検証器）: 私の `ee.cmul.s16` モデルが `sel4` の
   半分部と共役のビットを取り違えていた点（`sel4[0]` が半分部）、`ee.src.q` がシフト量を
   `SAR_BYTE*8` **バイト**として扱っていた点（マニュアルの `<<3` はビットなので、バイト変位は
   `SAR_BYTE` そのもの）。どちらも「3 通りの入口が一致しない」ことで見つかりました。

---

## 12. 未確認の前提（実機で確かめていない意味論）

ここに挙げたものは全部「このカーネルが寄りかかっている依存」です。実機ラン（`ex09` 方式）が来たら
片付くものから順に並べます。

1. **`EE.VZIP.16` / `EE.VUNZIP.16` のレーン順**（本ファイルの twiddle 組み立てと `unzip_pairs` の全部）。
   `data/pie_examples_measured.json` の `open_after_this_run` が「**レーンの意味論は未計測**」と
   名指ししている項目そのものです。本ファイルは TRM p290/p293 の擬似コード
   （`VUNZIP`: qs0 側が lanes 0,2,4,6）をそのまま使っています。ここが違えば twiddle の並びが崩れ、
   pre-rotate は静かに違う値を出します。**次に測るべき 1 番目の項目**です。
2. **`EE.VLD.L.64.IP` の整列強制と上位半分の扱い。** 擬似コードは `qu[63:0] = load64({as[31:3],3{0}})`
   で、上位 64bit には触れていません。本カーネルは `VZIP.16` が読む lanes 0..3 しか使わないので
   上位が何であっても影響しませんが、「上位が保存される」ことは仮定です。64bit ロードは
   ビルドに入っている .S に使用例が無い（§7 の注）ので、実機で確かめる価値があります。
3. **`EE.CMUL.S16` の SAR = 15 と、本ファイルのオペランド配置。** 実機で確定しているのは
   sel4 = 0/1 × SAR = 0/12 の 4 ケース（`cmul_complex_multiply_with_sar_shift`）で、
   「re = ac - bd, im = ad + bc」「SAR の算術右シフト」「sel4 が 32bit 半分部を選ぶ」までは一致しています。
   SAR = 15 と、`qx` の語を `(X1, X2)` にする向きは同じ族の外挿です。**向きを逆にすると `(yi, yr)` が
   入れ替わり、`out` のメモリ順が CELT と食い違います**（§3）。
4. **`wsr.sar` を保存せずに戻る点。** カーネルは SAR = 15 を設定し、呼び出し側に戻すときに復元しません。
   C コンパイラが SAR を跨いで期待するのは「1 つの可変シフト式の中」だけなので通常は問題になりませんが、
   呼び出し側が SAR を跨いで期待するなら自分で保存する必要があります（CELT 自身のカーネルも同じ流儀）。
5. **ビット反転順に書いていないこと**（§3）。CELT は `2*bitrev[i]` に書きます。本カーネルの出力は自然順
   なので、消費側は DIF（自然順入力）の FFT である必要があります。ここを混ぜると **MDCT の結果は
   完全に別物**になります（静かに違う、ではなく、FFT が別の並びを読む）。
6. **zipped 入力と「並べ替えは前段に畳み込む」という設計判断**（§2.3）。レイアウトは私の設計で、
   マニュアルの事実ではありません。前段が鏡像ペア順で直接書けるかは確かめていません。
7. **ストール/サイクル**。`stalls.py` は C のインラインアセンブリを読む道具で .S には使えません。
   ループ本体の並びは「M 段の def（ロード）を直後の E 段で使わない」ように置いてありますが、
   `EE.CMUL.S16` の段、`VZIP` の段、`EE.VADDS.S16 q7, q7, q6`（索引ベクタの更新）の段は
   表 1.7-2 に無いので、**サイクル数の主張は 1 つもしていません**（命令数だけ）。
8. **`EE.VLDBC.32` の整列強制**（下位 2bit）。索引ベクタの +4 定数はフレームの 4 バイト整列スロット
   から読んでいますが、ブロードキャスト形そのものは未計測です（`ex12` の `VLDBC.16/32` と同じ扱い）。
9. **テールとベクタ経路の一致は「モデルの中だけ」**。`ex16_overlap_add` のスカラテールは
   ベクタと同じクランプを書いてあり、in-domain の 1424 レーンで一致しますが、これは C 参照と
   私のモデルの一致であって、シリコンの話ではありません。

---

## 13. ビルドへの入れ方（このファイルでは適用していません）

```c
/* examples/firmware/main/examples.h */
void ex16_prerotate(const int16_t *x, const int16_t *tw, int16_t *out, uint32_t n4, uint32_t shift);
void ex16_prerotate_z(const int16_t *x, const int16_t *twz, int16_t *out, uint32_t n4);
void ex16_prerotate_idx(const int16_t *x, const int16_t *twz, int16_t *out, uint32_t n4);
void ex16_tw_zip(const int16_t *tw, int16_t *twz, uint32_t n4, uint32_t shift);
void ex16_unzip_pairs(const int16_t *z, int16_t *r1, int16_t *r2, uint32_t n_pairs);
void ex16_overlap_add(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n);

/* main.c, ex16(): この例題集の DATA/CHECK/BENCH の形。
 *   - 入力 (x, tw) は決定的な LCG で作り、print_i16 で全部出す（ホスト側チェッカーが再計算できるように）
 *   - 参照は ex16_prerotate_c / ex16_overlap_add_c（この .md の §9）をファームに置いて CHECK
 *   - 3 つの入口 (prerotate / _z / _idx) の出力が全部一致することを 1 つの CHECK にする
 *   - BENCH prerotate n4=480 cycles_pie=... cycles_c=... （実機ランで初めて数字になる）
 * 注意: x / tw / twz / out は 16 バイト整列の配列で渡すこと（__attribute__((aligned(16)))）。
 *       tw はレベル 0 の表 = clt_mdct_init が作った l->trig。CELT の l->trig は malloc なので
 *       16 バイト整列を保証するには呼び出し側でコピーするか、モード初期化時に整列確保が要ります。
 */
```

* ホスト側チェッカー（`tools/check_examples_log.py`）に足すなら `check_ex16()` が §9 の `ref.c` を
  移植する形になります。`tools/selftest_examples_checker.py` には `DATA` の変異を 1 つ足すのが
  この例題集の流儀です。
* 本ファイルは `proposed/` にあり、`examples/firmware/main/CMakeLists.txt` は明示列挙なので
  **ビルドに入っていません**。隣に移して `ex16()` を足すまで、走っているファームは何も変わりません。
