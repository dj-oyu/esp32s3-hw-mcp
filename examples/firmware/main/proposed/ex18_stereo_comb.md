# ex18 (proposed) — Opus/CELT のステレオ復元とピッチコンブフィルタ（16-bit レーン）

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex18_stereo_comb.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 18 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `00e8f4cd5b37f9e76afa23afc87ce9ce` → `a42e577c94839dc5940758b4cf1fa294`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex18_stereo_comb.S` — コーデック分野の 3 本目です。notes/08-media-3d-perf.md は
コーデック枠に **ex16 = CELT IMDCT の pre-rotate** を計画しており、本ファイルは**その前後に挟まる 2 段**
（ステレオの復元とピッチ後置フィルタ）を書いたものです。この 2 段を選んだのは、16-bit レーンに落とすときに
必ず起きる問い——**丸めと飽和をどの順序で置くと C 実装と一致し、どこで食い違うか**——が、CELT の側では
「丸めも飽和も無い」（1.1）という形で答えを持っているからです。

```c
void ex18_stereo_ms       (const int16_t *mid, const int16_t *side, int16_t *l, int16_t *r,
                           uint32_t n, uint32_t shift);
void ex18_comb_filter     (int16_t *y, const int16_t *x, const int16_t *hist, uint32_t n,
                           uint32_t tapset, uint32_t shift);
void ex18_intensity_stereo(int16_t *x, const int16_t *y, uint32_t n, uint32_t a1, uint32_t a2);
```

**このファイルと付属の証拠はすべて実機を使っていません。** 追試したのは (1) アセンブラ（`xtensa-esp32s3-elf-gcc`
→`objdump`→`data/pie_instructions.json` の命令語図とのフィールド単位照合 90 語）、(2) Python のモデル（TRM の
Operation 疑似コードから書いた層 1 と、`.S` と同じ順序で書いた層 2）、(3) ホストの `gcc` でビルドした C 検証器
（CELT のマクロを `celt/fixed_generic.h` から逐語で持ってきた CELT 側の契約そのもの）、(4) `/tmp` にコピーした
`piesim.py` による解釈実行、の 4 つだけです。**シリコンで確かめていない前提は末尾に全部列挙**してあります。

参照した C は `/workspace/cardputer-adv-pocketjs/.cache/codecs/opus-1.6.1/` の木で、ビルド構成は
`components/opus/CMakeLists.txt` の `FIXED_POINT VAR_ARRAYS=1 DISABLE_FLOAT_API`（`ENABLE_QEXT` は**無い**）。
この 2 点が効きます: `ENABLE_QEXT` が無いので `celt_coef` は `opus_val16`（Q15、`celt/arch.h:193`）で、
タップは Q15 です。そして xtensa は `OPUS_FAST_INT64` が **0**（`celt/arch.h:125-128` は x86_64/LP64/Win64/mips
だけを 64bit 扱いにする）ので、`MULT16_32_Q15` は `celt/fixed_generic.h:57` の**分解形**が走ります。

## 0. この文書の検証段（何が確認済みで、何が未確認か）

| 何を | どう | 結果 |
|---|---|---|
| A の 16-bit レーン実装 vs C 参照 | ホスト `gcc -O2` の C 検証器（CELT のマクロ逐語）と、Python モデルの 12,665 ケース | **202,640 値すべて一致（不一致 0、max\|d\| 0）** |
| A の中心の恒等式 | 総当り 8,388,608 組（全 mid 値 × 8 side 値 × shift 1..16） | **不一致 0、16-bit を出た中間値 0** |
| A の「食い違う形」4 種 | 同じ入力で測った | 飽和先・丸め先・Q31ONE ゲイン・最後にクランプ（下表） |
| B の Python 側の C 読み vs C | 同上 | **144 値すべて一致（0）← 参照の忠実さの検査** |
| B の PIE 実装 vs C | 同上 | 144 中 105 が違う（\|d\|≤3 が 84、それ以上が 21。21 = C の値が int16 を出る件数と一致） |
| 「履歴の先頭で不足するタップを飛ばす」規則 vs C | 同上 | 48 中 4 が違う（max\|d\| 4648） |
| C の 16-bit レーン実装 vs C 参照 | 同上 | 9,600 中 1,006 が違う（C の 32-bit 和が int16 を出る件数と完全に一致） |
| アセンブラの符号化 | `objdump` の `ee.*` 90 語を命令語図で再デコード | **定数フィールド不一致 0**（照合不能な図 0） |
| レジスタ割り当てとポインタ | `piesim.py` の解釈実行 4 本（A ×2、B、C） | **4 本ともモデルと一致**。piesim が `.S` の実バグ 1 件と私の piesim 追加のバグ 1 件を捕まえた |
| 実機 | — | **未使用**（`BENCH` 行は無い） |

**証拠の連鎖**（`.S` と CELT の C の間がどこでつながっているか）:

```
ex18_stereo_comb.S --(xtensa-esp32s3-elf-as)--> 命令語 --> (命令語図で再デコード) 定数フィールド不一致 0
                   --(piesim.py が .S のループ本体を解釈実行)--> モデルと一致（4 本）
                   --(Python モデル = .S と同じ命令列・同じレジスタ)--> C 検証器と突き合わせ
                   --(ホスト gcc の C 検証器 = CELT のマクロ逐語)--> CELT の C 参照
```

つまり「`.S` の算術 == CELT の C」は **(1) アセンブラの符号化**、**(2) .S のテキストを実行する piesim**、
**(3) .S と同じ命令列を書いたモデルと C の突き合わせ**、の 3 段で支えられています。`.S` 自身を実機で
走らせた証拠はありません（9 節）。

## 1. カーネル A — `ex18_stereo_ms`: ミッド/サイドから L/R

### 1.1 C 側に何が書いてあるか（丸めと飽和の順序）

`celt/bands.c:458-466`（`stereo_merge` の最後のループ）がそのまま出発点です。

```c
      celt_norm r, l;
      l = MULT32_32_Q31(mid, X[j]);
      r = Y[j];
      X[j] = VSHR32(MULT32_32_Q31(lgain, SUB32(l,r)), kl-15);
      Y[j] = VSHR32(MULT32_32_Q31(rgain, ADD32(l,r)), kr-15);
```

順序を決めているのは 3 つの事実で、どれも木から読めます:

1. **丸めは無い。** `VSHR32` の正の側は `SHR32`（`celt/fixed_generic.h:118`）= `(a) >> (shift)` です。
   丸める版は隣の `PSHR32`（同 123）で、`stereo_merge` は**使いません**。つまりこのループに丸めは 1 つも無く、
   すべて切り捨て（負方向の floor）です。
2. **飽和も無い。** `SUB32`/`ADD32` は `celt/fixed_generic.h:152-154` の素の 32bit 加減算で、飽和しません。
   ループ内に `SATURATE` は 1 つも出てきません。
3. **狭め方が「無い」。** ここが 16-bit レーンの話の核心です。**この木では `celt_norm` は `opus_val32`**
   （`celt/arch.h:155`、QEXT 期に 32bit 化された）なので、代入で 16bit に落ちることはありません。
   つまり C は 17bit 以上の中間値を**そのまま持ちます**。16-bit レーンの実装はどこかで必ず狭めるしかなく、
   **どこで狭めるかを自分で決めて、その選択を測る**必要があります。このカーネルは「C が保持する値」に
   いちばん近い形（狭めない形）を採り、食い違う形を 4 つ測りました（1.4）。

`MULT32_32_Q31` は `celt/fixed_generic.h:69/71` の 2 分岐のうち、xtensa では**分解形**（71 行）が走ります。

```c
#define MULT32_32_Q31(a,b) ADD32(ADD32(SHL(MULT16_16(SHR((a),16),SHR((b),16)),1), \
                             SHR(MULT16_16SU(SHR((a),16),((b)&0x0000ffff)),15)), \
                             SHR(MULT16_16SU(SHR((b),16),((a)&0x0000ffff)),15))
```

### 1.2 契約と、C からの換算

```
void ex18_stereo_ms(const int16_t *mid, const int16_t *side, int16_t *l, int16_t *r, uint32_t n, uint32_t shift)
  mid[]   C の `l = MULT32_32_Q31(mid_norm, X[j])` を呼び出し側で済ませた値。X は celt_norm（±2^15）で、
          mid_norm は 2^31 以下の Q31 ゲインなので |l| ≤ 32768 —— int16 のレーンに**損失なく**入ります。
  side[]  C の `r = Y[j]`。
  l/r     16 バイト整列、`n % 8 == 0`、`n >= 8`。mid/side と重ならないこと。
  shift   C の (kl-15)。lgain = rgain = 2^31（= 1.0）かつ kl == kr とします。
```

この 3 つの条件（ゲインが等しく 1.0、kl == kr）で C の式はこう畳めます:

```
l = MULT32_32_Q31(mid_norm, X[j]) = mid[j]        （呼び出し側で適用済み）
X[j] = VSHR32(MULT32_32_Q31(2^31, mid[j]-side[j]), shift) = (mid[j]-side[j]) >> shift
Y[j] = VSHR32(MULT32_32_Q31(2^31, mid[j]+side[j]), shift) = (mid[j]+side[j]) >> shift
```

**畳めないものは .md に残します**: `lgain`/`rgain` は `celt_rsqrt_norm32` の出力で、`kl`/`kr` はバンドの
エネルギー（`bands.c:429-456`）から来ます。どちらもスカラー 3 引数の per-sample カーネルには入りません。
カーネルは「ゲインが 1.0・両チャネル同じ正規化」という**特殊化**であって、一般の呼び出しの近似ではありません。

### 1.3 16-bit レーンで (mid ∓ side) >> shift を**厳密に**やる

mid-side と mid+side は 17bit（|a ± b| ≤ 65535）で、16-bit のレーンには入りません。使うのは

```
floor(D / 2^s) = floor( floor(D/2) / 2^(s-1) )            (s >= 1)
```

という入れ子の floor の恒等式と、2 つの補正項です:

| 出力 | 半値（16-bit に収まる） | 補正項 |
|---|---|---|
| mid − side | `A - B - borrow`、`A = mid>>1`, `B = side>>1` | `borrow = Pr - (Pl & Pr)`（= (mid 偶数) & (side 奇数)） |
| mid + side | `A + B + carry`、同上 | `carry = Pl & Pr`（`Pl = mid&1`, `Pr = side&1`） |

補正項の導出は 1 行です: `mid ± side = 2(A ± B) + (Pl ± Pr)` で、`Pl,Pr ∈ {0,1}` なので
`floor((Pl - Pr)/2) = -(Pl==0 & Pr==1)`、`floor((Pl + Pr)/2) = Pl&Pr`。

そして**どちらの半値も int16 に収まること**が要ります（`.S` は `EE.VSUBS.S16`/`EE.VADDS.S16` という
**飽和する**命令を使うので、ここが崩れると静かに飽和します）。境界は総当りで測りました: 8,388,608 組
（全 mid 値 × 8 side 値 × shift 1..16）で**半値が int16 を出た回数 0**。証明も書けます——
`A,B ∈ [-16384, 16383]` なので `A - B - borrow ∈ [-32768, 32767]`、`A + B + carry ∈ [-32768, 32767]`。

シフトそのものは `EE.VMUL.S16`（TRM p198、`(qx*qy) >> SAR`、**算術シフトで切り捨て、飽和しない**。
ex08 がこの符号付き算術シフトを実機で確かめています）で作ります: レーンに 2^14 を入れたレジスタを掛け、
SAR に「欲しいシフト + 13」を置くと `(x*2^14)>>(k+13) = x>>k` です。半値は SAR=15、最後は SAR=shift+13。

```
    wsr.sar a8                      /* SAR = 15 */
    EE.VLD.128.IP q6, a2, 16        /* mid  */
    EE.VLD.128.IP q7, a3, 16        /* side */
    EE.ANDQ q3, q6, q1              /* Pl */
    EE.ANDQ q2, q7, q1              /* Pr */
    EE.VMUL.S16 q5, q6, q0          /* A = mid >> 1   (qz @ M) */
    EE.ANDQ q4, q3, q2              /* carry = Pl & Pr */
    EE.VMUL.S16 q6, q7, q0          /* B = side >> 1  (qz @ M) */
    EE.VSUBS.S16 q2, q2, q4         /* borrow = Pr - carry */
    wsr.sar a9                      /* SAR = shift + 13 */
    EE.VSUBS.S16 q7, q5, q6         /* A - B */
    EE.VSUBS.S16 q7, q7, q2         /* half-diff = floor((mid - side)/2) */
    EE.VADDS.S16 q5, q5, q6         /* A + B */
    EE.VADDS.S16 q3, q5, q4         /* half-sum  = floor((mid + side)/2) */
    EE.VMUL.S16 q7, q7, q0          /* l[j] = half-diff >> (shift-1) */
    EE.VMUL.S16 q3, q3, q0          /* r[j] = half-sum  >> (shift-1) */
    EE.VST.128.IP q7, a4, 16
    EE.VST.128.IP q3, a5, 16
```

**shift == 0 は別ループです。** shift==0 では C の値が 17bit のまま int16 の代入に落ちるので、
**C は下位 16bit を残します（ラップ）**。飽和させると一致しません。このカーネルはラップを再現します:
`2*half + b`（`b = Pl ^ Pr` は和でも差でも同じ下位ビット）を、`EE.VMUL.S16` に SAR=13 を置いて
**32bit で 1 ビット左シフトして下位 16bit を取る**（飽和する 16bit 加算では 2*32767 が入らない）形で作ります。
`EE.VADDS.S16` が最後に飽和しないのは、`2*half` が偶数（≦32766）だからです。

### 1.4 「一致する形」と「食い違う形」— 実測

同じ入力（shift ごとに 16,000 サンプル = 8 レーン × 2 出力 × 1000 群、境界値と乱数）で、C 参照に対する
不一致数と最大差を測った結果です（`/tmp/ex18_model.py` の T2/T2b 節、下の実行結果に生の出力）。

| 形 | shift=1 | shift=8 | 食い違う理由と、何 bit 違うか |
|---|---|---|---|
| **本カーネル（半値 + 切り捨てシフト）** | **0/32000** | **0/32000** | C と一致（D 全体の floor = 入れ子 floor、丸め無し・飽和無し） |
| `EE.VSUBS.S16` を**先**に使う素朴なレーン版 | 8,039/32000 (max 16,207) | 8,045/32000 (max 127) | 17bit の中間値をシフト**前**に ±32767 で飽和。差 = `(\|mid∓side\| - 32767) >> shift` |
| 丸めてからシフト（PSHR32 相当） | 16,076/32000 (max 1) | 16,049/32000 (max 1) | 切り捨てを四捨五入に変えた分。**1 ULP（1）**だけ、切り捨てビットが半分以上のとき |
| ゲインを Q31ONE（2^31-1）にする（= C が実際に使える最大の Q31） | 16,035/32000 (max 1) | 125/32000 (max 1) | `MULT32_32_Q31(2^31-1, v) = v-1`（v ≥ 1）を 2 回通す分。**1 ULP**。CELT は 2^31 を表現できない |
| 最後に `EE.VMIN/VMAX.S16` でクランプ | **0/32000**（shift=1） | **0/32000**（shift=5） | shift ≥ 1 ではシフト後の値が必ず int16 に収まる（証明済み）ので**差ゼロ**。T2b は shift 0,1,2,5,16 を測っており 0 以外はすべて 0 |
| 同上（shift == 0） | 8,069/32000 (max **65,535**) | 8,069/32000 (max 65,535) | ラップする場面でクランプするので、差は 2^16 の倍数（飽和 32767 とラップ -32768 で 65,535） |

「丸めと飽和の順序」への答えはこの表そのものです: **CELT のこの段には丸めも飽和も無い**（丸め無し・飽和無し・
狭めは 32bit のまま）。したがって (a) シフトを丸めに変えると 1 ULP ずれ、(b) 17bit の中間値を先に飽和させると
最大で中間値の飽和分ずれ、(c) 最後のクランプは shift ≥ 1 では無害、(d) shift == 0 のラップだけは再現が要る
（このカーネルは再現している）、という 4 点が「どこで何 bit 違うか」の全部です。

### 1.5 出典（TRM ページ = `data/pie_instructions.json` の `source_page`）

| 命令 | page | 構文 | このカーネルでの役割 |
|---|---|---|---|
| `EE.VMUL.S16` | 198 | `EE.VMUL.S16 qz, qx, qy` | `(s16*s16) >> SAR`、下位 16bit。**切り捨て・飽和なし**（ex08 が実機で符号付き算術シフトを確認）。>>1・<<1・最終シフトの全部 |
| `EE.VSUBS.S16` | 281 | `EE.VSUBS.S16 qa, qx, qy` | レーンごと sat16 減算。半値の範囲では**飽和しない**（1.3 の証明） |
| `EE.VADDS.S16` | 146 | `EE.VADDS.S16 qa, qx, qy` | 同上（加算） |
| `EE.ANDQ` / `EE.XORQ` | 76 / 297 | `EE.ANDQ qa, qx, qy` | 下位ビット `Pl`/`Pr`/`carry` と `b = Pl ^ Pr` |
| `EE.VLD.128.IP` / `EE.VST.128.IP` | 164 / 275 | `EE.VLD.128.IP qu, as, -2048..2032` | 16 バイト読み書き。アドレスは `{as[31:4],4{0}}` に丸められる（TRM p49） |
| `EE.MOVI.32.Q` | 119 | `EE.MOVI.32.Q qu, as, 0..3` | レーン定数。**32bit 語を入れる命令**なので、全レーン同値にするには 32bit 語を 2 回重ねる（下の 6.2 参照） |
| `EE.ZERO.Q` | 299 | `EE.ZERO.Q qa` | 同上 |
| `wsr.sar` / `entry` / `retw.n` / `srli` / `addi` / `bnez` / `beqz` | — | ベース ISA | PIE 章の表には無い（`data/pie_instructions.json` は PIE 220 命令のリスト）ので source_page 無し |

## 2. カーネル B — `ex18_comb_filter`: ピッチコンブフィルタ（3 ゲイン / 5 タップ）

### 2.1 C 側に何が書いてあるか

`celt/celt.c:166-193`（`comb_filter_const_c`、ARM 版でも QEXT 版でもない方）:

```c
   x4 = x[-T-2];  x3 = x[-T-1];  x2 = x[-T];  x1 = x[-T+1];
   for (i=0;i<N;i++) {
      x0=x[i-T+2];
      y[i] = x[i] + MULT_COEF_32(g10,x2) + MULT_COEF_32(g11,ADD32(x1,x3))
                  + MULT_COEF_32(g12,ADD32(x0,x4));
      y[i] = SUB32(y[i], 1);            /* the fixed-point bias */
      y[i] = SATURATE(y[i], SIG_SAT);   /* SIG_SAT = 536870911 = 2^29-1 */
      x4=x3; x3=x2; x2=x1; x1=x0;
   }
```

- `MULT_COEF_32` = `MULT16_32_Q15`（`celt/arch.h:195`）で、xtensa では `celt/fixed_generic.h:57` の**分解形**。
  この形は `(a*b) >> 15`（= floor）と**値が一致する**ことを、C 検証器の prelude 2 で 94,407,129 組について
  確かめています（不一致 0）。つまり**タップごとに 1 回ずつ切り捨て**、3 回の切り捨てを足す形です。
- タップは **3 ゲインだが信号としては 5 本**（外側の 2 対が掛ける前に足されている）。
- `SATURATE(., 2^29-1)` は int16 入力では**絶対に発火しません**。`celt/arch.h:207-215` がその理由
  （フルスケールのトーンを前置フィルタ越しに通すための上限）を書いています。この「C 側に効かないクランプ」が、
  16-bit レーン側では**効いてしまう**のがこのカーネルの要点です（2.5）。
- ゲイン表は `celt/celt.c:246-249` で、`celt.c:267-272` が呼び出しごとに
  `g0k = MULT16_16_P15(g0, gains[tapset0][k])` を計算します。`MULT16_16_P15` は `celt/fixed_generic.h:197` の
  `(16384 + a*b) >> 15`（**丸める**）です。

### 2.2 タップ数と履歴長の契約（設問 (B) の前半）

```
void ex18_comb_filter(int16_t *y, const int16_t *x, const int16_t *hist, uint32_t n,
                      uint32_t tapset, uint32_t shift)
  y      out、n サンプル、16 バイト整列。x と別でも同じでもよい（同じ場合は下記）
  x      in、n サンプル、16 バイト整列。直接タップ x[i] の供給元でもある
  hist   ピッチ遅延信号: hist[j] == x[j-T]。呼び出し側が渡すのは **x - T のポインタ**で、
         このカーネルは T を必要としません（周期そのものが引数に無いのはこのため）。
         16 バイト整列は**不要**（任意の位相で正しく動きます。2.4）
  n      8 の倍数、n >= 8
  tapset 0..2（CELT のゲイン表）。shift は 15 が CELT の Q15 契約（SRCMB の読み出し位置）
```

信号としてのタップは 5 本 `hist[i-2 .. i+2]`、ゲインは 3 本。**要求する履歴長は T+2** です:
最初に触るのは `hist[-2]`（i=0 の g2 タップ）、最後に触るのは `hist[n-1]`（i=n-1 の g0 タップ）で、
`hist[j] = x[j-T]` なので、読み出しの範囲は `[hist[-2], hist[n-1]]` —— **フレームの先へは出ません**。

### 2.3 履歴の先頭でタップが足りないとき: **ゼロ埋めもしないし、飛ばしもしない**（設問 (B) の後半）

CELT の答えは「**無条件に読む。足りないタップという状況が存在しない**」です:

- `celt.c:273-276`（`comb_filter` 本体）は `x[-T1+1] .. x[-T1-2]` を条件分岐なしで読みます。
- 読めるのは呼び出し側のバッファがそうなっているからで、`celt/celt.h:236` の
  `COMBFILTER_MAXPERIOD = 1024` サンプルが `decode_mem`/`prefilter_mem` として状態の中に取られ、
  **初期化でゼロクリア**されます。`celt_decoder.c:599` が `decode_mem[c] = st->_decode_mem + ...`、
  フレームはその中の `decode_buffer_size-N` から始まるので、`x[-T-2]` は必ず実在の（そして
  ストリーム先頭では 0 の）サンプルです。
- `T` は `COMBFILTER_MINPERIOD = 15` 以上にクランプされます（`celt.c:265-266`）。`T+2 >= 17 > 0` なので
  「先頭で 2 本足りない」状況は `hist` を正しく渡せば起こりません。

したがってこのカーネルの契約は **「呼び出し側が連続した有効な履歴を渡す。足りないタップを内部で作らない」** です。
そして「では履歴が本当に無い場合（ストリームの最初のフレーム）はどうするのか」の答えは
**ゼロ**です（実機の状態がゼロクリアされているから）。**飛ばす（skip）実装は CELT の挙動ではありません**——
どれだけ食い違うかを測りました:

| 規則 | 48 サンプル（3 tapset × n=16、hist[-2]=hist[-1]=0）での C との差 |
|---|---|
| ゼロ埋め済みの履歴を渡す（= C が同じメモリを読む） | **一致（定義どおり。C はメモリを読むだけ）** |
| 先頭で範囲外になったタップを**飛ばす** | 4 サンプルが食い違う（i=0,1 のみ。max\|d\| = 4,648 = g2 のタップ 1 本分） |

### 2.4 5 本の窓をどう作るか（と、なぜ 10 ロードなのか）

128 ビットアクセスはアドレスを `{as[31:4],4{0}}` に丸める（TRM p49）ので、格子から外れた窓は ex15 の
道具立てで作ります: `EE.LD.128.USAR.IP`（TRM p93）がポインタを含む整列チャンクを読み、落とした下位 4bit を
`SAR_BYTE` に残す。`EE.SRC.Q`（TRM p125）が `{qs1,qs0} >> (SAR_BYTE*8)` を返す。したがって
アドレス `A` の窓には「`floor(A)` のチャンク」と「`floor(A)+16` のチャンク」の対が要り、
**+16 は下位 4bit を変えないので 2 つのロードは同じ SAR_BYTE を立てます**。1 窓あたり

```
    EE.LD.128.USAR.IP q2, P_m, 16    /* q2 = floor(A_m), SAR_BYTE = A_m & 15 */
    EE.LD.128.USAR.IP q3, Q_m, 16    /* q3 = floor(A_m)+16, SAR_BYTE = A_m & 15 （同じ値） */
    EE.SRC.Q q1, q2, q3              /* q1 = A_m からの 16 バイト = hist[i+k+m] */
```

で、`A_m = hist + i + m`、`P_m = hist + m`、`Q_m = P_m + 16`。**10 本のポインタは全部 `.IP` の後置
インクリメントで 16 バイトずつ自分で歩く**ので、ループ本体にポインタ演算は 1 命令もありません。
これが **`hist` の整列を一切仮定しない**理由です: 周期 T は任意なので `hist = x - T` の位相も任意で、
位相に依存する分岐を書かずに済む形を選びました。

**より安い 4 ロード版は採っていません**（理由を残します）: 位相が 16 通りあるうち 12 通りでは、
共有した 1 対 `{floor(hist+i-2), +16}` と 4 本の「SAR_BYTE を立てるだけの poke ロード」だけで
3 ロード + 4 poke + 4 SRC.Q + 1 生窓 = 12 命令/8 サンプルになります。しかし残り 4 通り
（`(hist+i)&15 <= 1` のとき m=-2,-1 が**前の**チャンクに落ちる、`>= 14` のとき m=+1,+2 が**次の次**の
チャンクに落ちる）では対がもう 1 つ要り、その選択は**ループ本体の SRC.Q のオペランドを位相で選ぶ**ことになるので
ループが 2〜3 本に増えます。**先に正しいカーネル**を選び、値段はこの節に出しました
（10 ロード版は 28 発行スロット/8 サンプル、4 ロード版は 12 発行スロット/8 サンプル + 位相分岐）。

**アキュムレータの余裕**: `|QACC| ≤ Σ|g_k|·|tap| ≤ 26208·32768 + 8784·65536 + 4248·65536 = 1.71e9 < 2^39 = 5.5e11`。
`EE.VSMULAS.S16.QACC` の 40bit レーンはどの入力でも飽和**しません**。

### 2.5 それでも一致しない 2 つの理由（実測）

`AA.VMULAS.S16.ACCX` ではなく `EE.VSMULAS.S16.QACC` を採った理由は、ACCX が 8 レーンの積を**合算**して
1 個のアキュムレータにする（`data/pie_examples_measured.json :: vmulas_s16_accx_sums_the_lanes`）ため、
8 出力を並べるコムフィルタには使えないからです。QACC は 8 本独立で、読み出しの `EE.SRCMB.S16.QACC`
（TRM p130）が「レーンごとに `>> shift` して **sat16** を書き出す」——16-bit レーンの飽和 MAC と読み出しの対です。

代償は **シフトの配り方**です: CELT はタップごとに 1 回 floor を取って足し、このカーネルは 40bit で
厳密に足してから 1 回 floor を取ります。差は `floor(Σ/2^15) - Σ floor(g_k D_k/2^15)` で、**タップ数で抑えられ
（0..2）、実測でもその範囲に収まっています**（下表の `|d|<=3` の列）。

| tapset | n | サンプル | 不一致 | \|d\|≤3 | \|d\|>3 | max\|d\| | C の値が int16 を出る件数 |
|---|---|---|---|---|---|---|---|
| 0 | 8 | 320 | 269 | 230 | 39 | 18,239 | 39 |
| 0 | 16 | 640 | 553 | 483 | 70 | 21,177 | 70 |
| 0 | 24 | 960 | 812 | 697 | 115 | 24,761 | 115 |
| 0 | 64 | 2,560 | 2,170 | 1,899 | 271 | 24,378 | 271 |
| 1 | 8/16/24/64 | 4,480 | 2,583 | 1,907 | 676 | 24,834 | 676 |
| 2 | 8/16/24/64 | 4,480 | 2,948 | 2,033 | 915 | 30,737 | 915 |

（tapset 1/2 の行は 1 行にまとめました。生の出力は下に全部あります。）
**`|d| > 3` の件数が「C の値が int16 を出る件数」と完全に一致している**のがこの表の読み所です。
つまり不一致は 2 種類しかありません: (a) シフトの配り方（≤3）、(b) **16-bit レーンの飽和**。
(b) が起きるのは、CELT のタップ和が 17bit を要るからで、
`x[i] + Σ ≤ 32768 + 52297 = 85065` —— **C の出力自体が int16 に入りません**（CELT 側の `y[]` は
`opus_val32` なので、C はそれをそのまま保持します）。ホスト C 検証器は、この場面でカーネルが
**クランプ**した件数（11）と、int16 の**丸め付き代入ならラップした**件数（0）を別々に出しています。
カーネルがクランプ位置を 3 つ（SRCMB の読み出し・`+x` の加算・`-1` の減算）持つのに対し、C は
`celt.c:186` の 1 つだけで、しかもそれは発火しません（「どちらでもない」10 件がこの差です）。

### 2.6 ゲインについての 1 LSB の例外

`tapset 2` の第 1 タップは表の **26208** ですが、CELT が `g = Q15ONE` で計算する値は
`MULT16_16_P15(32767, 26208) = (16384 + 32767*26208) >> 15 = 26207` ——**1 LSB 低い**です。
このカーネルは表の値をそのまま使うので、この 1 点だけ CELT の `g = Q15ONE` 呼び出しと 1 LSB ずれます
（0.008% のゲイン誤差）。他の 8 エントリは一致します（C 検証器の prelude 1 に生の出力）。

### 2.7 出典

| 命令 | page | 構文 | 役割 |
|---|---|---|---|
| `EE.VSMULAS.S16.QACC` | 269 | `EE.VSMULAS.S16.QACC qx, qy, sel8` | **16-bit レーンの飽和 MAC**。`qy` の 1 レーン（sel8）を係数として `qx` の 8 レーンに掛け、8 本独立の 40bit レーンに飽和加算 |
| `EE.VMULAS.S16.QACC` | 215 | `EE.VMULAS.S16.QACC qx, qy` | 同上だが係数はレーンごと（このカーネルでは不要） |
| `EE.VMULAS.S16.ACCX` | 210 | `EE.VMULAS.S16.ACCX qx, qy` | **使っていない**: 8 積を合算して 1 個の ACCX に入る（実測記録あり）ので 8 出力にならない |
| `EE.SRCMB.S16.QACC` | 130 | `EE.SRCMB.S16.QACC qu, as, 0` | 各 40bit レーンを `as[5:0]` だけ算術シフトし、**sat16 を書き出す**（QACC にも書き戻す） |
| `EE.SRS.ACCX` | 134 | `EE.SRS.ACCX au, as, 0` | **使っていない**: ACCX（40bit 1 本）をシフトして 32bit に飽和、スカラレジスタへ |
| `EE.ZERO.QACC` | 300 | `EE.ZERO.QACC` | QACC_L/QACC_H をクリア |
| `EE.LD.128.USAR.IP` | 93 | `EE.LD.128.USAR.IP qu, as, -2048..2032` | 窓の下側チャンク + `SAR_BYTE = as[3:0]` |
| `EE.SRC.Q` | 125 | `EE.SRC.Q qa, qs0, qs1` | `{qs1,qs0} >> (SAR_BYTE*8)` = 任意位相の 16 バイト窓 |
| `EE.VLD.128.IP` / `EE.VST.128.IP` | 164 / 275 | 同上 | x の窓・y の書き出し |
| `EE.MOVI.32.Q` / `EE.ZERO.Q` | 119 / 299 | 同上 | ゲイン 3 本と定数 1 のレーン配置 |
| `EE.VADDS.S16` / `EE.VSUBS.S16` | 146 / 281 | 同上 | `+x[i]` と `-1`（C の `SUB32(.,1)`） |

## 3. カーネル C — `ex18_intensity_stereo`: 強度ステレオ（片チャネルのスケール適用）

`celt/bands.c:398-402`:

```c
   for (j=0;j<N;j++)
      X[j] = ADD32(MULT16_32_Q15(a1, X[j]), MULT16_32_Q15(a2, Y[j]));
```

`a1`,`a2` は `bands.c:387-397` でバンドのエネルギーから作られる Q15 のスカラで、カーネルは受け取るだけです。
C の 1 サンプルは **2 回の切り捨て**（`floor(a1*X/2^15)` と `floor(a2*Y/2^15)`）と **飽和しない 32bit 加算**です。
各項は int16 に収まります（|a| ≤ 2^15、|X| ≤ 32768）が、**その和は 17bit を要ります**——`celt_norm` は
この木では `opus_val32`（`celt/arch.h:155`）なので C はそのまま持ちます。`a1^2 + a2^2 = 2^30` なので
`a1 + a2` は 2^15·√2 = 46,341 まで行き得るので、実際に和が int16 を出ます。

16-bit レーンの実装はどこかで狭めるしかないので、**PIE がタダで与える飽和**（`EE.VADDS.S16`）を採り、
その発火を測りました:

| a1 | a2 | サンプル | C との不一致 | レーンの飽和が起きた件数 |
|---|---|---|---|---|
| 23170 | 23170 | 1,600 | 154 | 154 |
| 32767 | 0 | 1,600 | **0** | 0 |
| 0 | 32767 | 1,600 | **0** | 0 |
| 30000 | 30000 | 1,600 | 323 | 323 |
| 20000 | 26000 | 1,600 | 136 | 136 |
| 32767 | 32767 | 1,600 | 393 | 393 |

片側だけの適用（a1 か a2 が 0）では**完全一致**します。両側にゲインが乗る場合だけ、C の 17bit 和が
16bit レーンから溢れ、差は「溢れた分」そのものです（max|d| = 31,414）。C 検証器はさらに
「int16 の丸め付き代入（ラップ）なら C と一致した件数」も出していて、それは 8,594/9,600 = 溢れなかった分
ちょうどです。**ラップを選ぶ実装もあり得ます**（1.5 の A の shift==0 と同じ理屈）が、このカーネルは
CELT の「飽和しない 32bit 和」に意味の近い**飽和**を選び、その件数を数字で残しました。

出典: `EE.VMUL.S16` p198（Q15 の切り捨て乗算 2 回）、`EE.VADDS.S16` p146（和の飽和）、
`EE.VLD.128.IP` p164 / `EE.VST.128.IP` p275、`wsr.sar`（ベース ISA、SAR=15）。

**注意（`.S` の 1 行）**: このカーネルは x を**読み書き両方**に使うので、ループ内で唯一
「`.IP` ロードの増分を 0 にする」場所があります（`EE.VLD.128.IP q2, a2, 0` で読み、`EE.VST.128.IP q2, a2, 16`
で書く）。最初の版は両方 16 にしており、読みが x+i、書きが x+i+8 になっていました——**piesim がこれを
捕まえました**（下の 8 節）。

## 4. アセンブラの証拠（実機は使っていない）

```
$ source /opt/esp-idf/export.sh && cd /workspace/esp32s3-hw-mcp
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex18/ex18.o examples/firmware/main/proposed/ex18_stereo_comb.S
(出力なし・終了ステータス 0)

$ xtensa-esp32s3-elf-nm -S /tmp/ex18/ex18.o
000000c0 000000f2 T ex18_comb_filter
000001b4 00000052 T ex18_intensity_stereo
00000000 000000bf T ex18_stereo_ms

$ xtensa-esp32s3-elf-objdump -h /tmp/ex18/ex18.o | grep -E "iram1|literal"
  3 .iram1        00000206  00000000  00000000  00000034  2**2          <- .iram1 だけ。literal 節は無い

$ xtensa-esp32s3-elf-objdump -d /tmp/ex18/ex18.o | grep -c l32r
0                                                                        <- 定数はすべて in-core 合成
```

```text
OBJECT-DISASSEMBLY-BEGIN

/tmp/ex18/ex18.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex18_stereo_ms>:
   0:	004136        	entry	a1, 32
   3:	416360        	srli	a6, a6, 3
   6:	0b3616        	beqz	a6, bd <ex18_stereo_ms+0xbd>
   9:	084c      	movi.n	a8, 64
   b:	118880        	slli	a8, a8, 8
   e:	11b800        	slli	a11, a8, 16
  11:	2088b0        	or	a8, a8, a11
  14:	cd3284        	ee.movi.32.q	q0, a8, 0
  17:	cd3684        	ee.movi.32.q	q0, a8, 1
  1a:	cd3a84        	ee.movi.32.q	q0, a8, 2
  1d:	cd3e84        	ee.movi.32.q	q0, a8, 3
  20:	190c      	movi.n	a9, 1
  22:	11b900        	slli	a11, a9, 16
  25:	2099b0        	or	a9, a9, a11
  28:	cdb294        	ee.movi.32.q	q1, a9, 0
  2b:	cdb694        	ee.movi.32.q	q1, a9, 1
  2e:	cdba94        	ee.movi.32.q	q1, a9, 2
  31:	cdbe94        	ee.movi.32.q	q1, a9, 3
  34:	97db      	addi.n	a9, a7, 13
  36:	f80c      	movi.n	a8, 15
  38:	da0c      	movi.n	a10, 13
  3a:	b7bc      	beqz.n	a7, 79 <ex18_stereo_ms+0x79>
  3c:	130380        	wsr.sar	a8
  3f:	b30124        	ee.vld.128.ip	q6, a2, 16
  42:	b38134        	ee.vld.128.ip	q7, a3, 16
  45:	ddb0e4        	ee.andq	q3, q6, q1
  48:	dd30f4        	ee.andq	q2, q7, q1
  4b:	aea684        	ee.vmul.s16	q5, q6, q0
  4e:	ed3454        	ee.andq	q4, q3, q2
  51:	be2784        	ee.vmul.s16	q6, q7, q0
  54:	9e62d4        	ee.vsubs.s16	q2, q2, q4
  57:	130390        	wsr.sar	a9
  5a:	bef5d4        	ee.vsubs.s16	q7, q5, q6
  5d:	beb7d4        	ee.vsubs.s16	q7, q7, q2
  60:	aed564        	ee.vadds.s16	q5, q5, q6
  63:	9ec564        	ee.vadds.s16	q3, q5, q4
  66:	bea784        	ee.vmul.s16	q7, q7, q0
  69:	9ea384        	ee.vmul.s16	q3, q3, q0
  6c:	ba8144        	ee.vst.128.ip	q7, a4, 16
  6f:	9a8154        	ee.vst.128.ip	q3, a5, 16
  72:	660b      	addi.n	a6, a6, -1
  74:	fc4656        	bnez	a6, 3c <ex18_stereo_ms+0x3c>
  77:	f01d      	retw.n
  79:	130380        	wsr.sar	a8
  7c:	930124        	ee.vld.128.ip	q2, a2, 16
  7f:	938134        	ee.vld.128.ip	q3, a3, 16
  82:	ed3064        	ee.andq	q4, q2, q1
  85:	edb074        	ee.andq	q5, q3, q1
  88:	be2284        	ee.vmul.s16	q6, q2, q0
  8b:	fdb8a4        	ee.andq	q7, q4, q5
  8e:	9e2384        	ee.vmul.s16	q2, q3, q0
  91:	ddb9a4        	ee.xorq	q3, q4, q5
  94:	1303a0        	wsr.sar	a10
  97:	ae7dd4        	ee.vsubs.s16	q4, q5, q7
  9a:	aeb6d4        	ee.vsubs.s16	q5, q6, q2
  9d:	aee5d4        	ee.vsubs.s16	q5, q5, q4
  a0:	be1664        	ee.vadds.s16	q6, q6, q2
  a3:	be5e64        	ee.vadds.s16	q6, q6, q7
  a6:	aea584        	ee.vmul.s16	q5, q5, q0
  a9:	be2684        	ee.vmul.s16	q6, q6, q0
  ac:	660b      	addi.n	a6, a6, -1
  ae:	ae9d64        	ee.vadds.s16	q5, q5, q3
  b1:	be1e64        	ee.vadds.s16	q6, q6, q3
  b4:	aa8144        	ee.vst.128.ip	q5, a4, 16
  b7:	ba0154        	ee.vst.128.ip	q6, a5, 16
  ba:	fbb656        	bnez	a6, 79 <ex18_stereo_ms+0x79>
  bd:	f01d      	retw.n
	...

000000c0 <ex18_comb_filter>:
  c0:	004136        	entry	a1, 32
  c3:	415350        	srli	a5, a5, 3
  c6:	0e6516        	beqz	a5, 1b0 <ex18_comb_filter+0xf0>
  c9:	c69c      	beqz.n	a6, e9 <ex18_comb_filter+0x29>
  cb:	180c      	movi.n	a8, 1
  cd:	391687        	beq	a6, a8, 10a <ex18_comb_filter+0x4a>
  d0:	66a082        	movi	a8, 102
  d3:	118880        	slli	a8, a8, 8
  d6:	60c882        	addi	a8, a8, 96
  d9:	c90c      	movi.n	a9, 12
  db:	119980        	slli	a9, a9, 8
  de:	01d992        	addmi	a9, a9, 0x100
  e1:	d0c992        	addi	a9, a9, -48
  e4:	0a0c      	movi.n	a10, 0
  e6:	000c86        	j	11c <ex18_comb_filter+0x5c>
  e9:	782c      	movi.n	a8, 39
  eb:	118880        	slli	a8, a8, 8
  ee:	40c882        	addi	a8, a8, 64
  f1:	b91c      	movi.n	a9, 27
  f3:	119980        	slli	a9, a9, 8
  f6:	01d992        	addmi	a9, a9, 0x100
  f9:	c8c992        	addi	a9, a9, -56
  fc:	0a1c      	movi.n	a10, 16
  fe:	11aa80        	slli	a10, a10, 8
 101:	01daa2        	addmi	a10, a10, 0x100
 104:	98caa2        	addi	a10, a10, -104
 107:	000446        	j	11c <ex18_comb_filter+0x5c>
 10a:	b83c      	movi.n	a8, 59
 10c:	118880        	slli	a8, a8, 8
 10f:	60c882        	addi	a8, a8, 96
 112:	292c      	movi.n	a9, 34
 114:	119980        	slli	a9, a9, 8
 117:	50c992        	addi	a9, a9, 80
 11a:	0a0c      	movi.n	a10, 0
 11c:	11b900        	slli	a11, a9, 16
 11f:	2088b0        	or	a8, a8, a11
 122:	edffa4        	ee.zero.q	q5
 125:	edb284        	ee.movi.32.q	q5, a8, 0
 128:	11ba00        	slli	a11, a10, 16
 12b:	20bab0        	or	a11, a10, a11
 12e:	edb6b4        	ee.movi.32.q	q5, a11, 1
 131:	180c      	movi.n	a8, 1
 133:	11b800        	slli	a11, a8, 16
 136:	2088b0        	or	a8, a8, a11
 139:	fd3284        	ee.movi.32.q	q6, a8, 0
 13c:	fd3684        	ee.movi.32.q	q6, a8, 1
 13f:	fd3a84        	ee.movi.32.q	q6, a8, 2
 142:	fd3e84        	ee.movi.32.q	q6, a8, 3
 145:	fcc482        	addi	a8, a4, -4
 148:	fec492        	addi	a9, a4, -2
 14b:	a42b      	addi.n	a10, a4, 2
 14d:	b44b      	addi.n	a11, a4, 4
 14f:	c4cb      	addi.n	a12, a4, 12
 151:	0ec4d2        	addi	a13, a4, 14
 154:	12c4e2        	addi	a14, a4, 18
 157:	14c4f2        	addi	a15, a4, 20
 15a:	10c462        	addi	a6, a4, 16
 15d:	830134        	ee.vld.128.ip	q0, a3, 16
 160:	250844        	ee.zero.qacc
 163:	910184        	ee.ld.128.usar.ip	q2, a8, 16
 166:	9181c4        	ee.ld.128.usar.ip	q3, a12, 16
 169:	dca314        	ee.src.q	q1, q2, q3
 16c:	9e69c4        	ee.vsmulas.s16.qacc	q1, q5, 2
 16f:	910194        	ee.ld.128.usar.ip	q2, a9, 16
 172:	9181d4        	ee.ld.128.usar.ip	q3, a13, 16
 175:	dca314        	ee.src.q	q1, q2, q3
 178:	8ee9c4        	ee.vsmulas.s16.qacc	q1, q5, 1
 17b:	910144        	ee.ld.128.usar.ip	q2, a4, 16
 17e:	918164        	ee.ld.128.usar.ip	q3, a6, 16
 181:	dca314        	ee.src.q	q1, q2, q3
 184:	8e69c4        	ee.vsmulas.s16.qacc	q1, q5, 0
 187:	9101a4        	ee.ld.128.usar.ip	q2, a10, 16
 18a:	9181e4        	ee.ld.128.usar.ip	q3, a14, 16
 18d:	dca314        	ee.src.q	q1, q2, q3
 190:	8ee9c4        	ee.vsmulas.s16.qacc	q1, q5, 1
 193:	9101b4        	ee.ld.128.usar.ip	q2, a11, 16
 196:	9181f4        	ee.ld.128.usar.ip	q3, a15, 16
 199:	dca314        	ee.src.q	q1, q2, q3
 19c:	9e69c4        	ee.vsmulas.s16.qacc	q1, q5, 2
 19f:	550b      	addi.n	a5, a5, -1
 1a1:	cdf274        	ee.srcmb.s16.qacc	q1, a7, 0
 1a4:	8e8164        	ee.vadds.s16	q1, q1, q0
 1a7:	8ef1d4        	ee.vsubs.s16	q1, q1, q6
 1aa:	8a8124        	ee.vst.128.ip	q1, a2, 16
 1ad:	fac556        	bnez	a5, 15d <ex18_comb_filter+0x9d>
 1b0:	f01d      	retw.n
	...

000001b4 <ex18_intensity_stereo>:
 1b4:	004136        	entry	a1, 32
 1b7:	414340        	srli	a4, a4, 3
 1ba:	046416        	beqz	a4, 204 <ex18_intensity_stereo+0x50>
 1bd:	f45050        	extui	a5, a5, 0, 16
 1c0:	117500        	slli	a7, a5, 16
 1c3:	205570        	or	a5, a5, a7
 1c6:	cd3254        	ee.movi.32.q	q0, a5, 0
 1c9:	cd3654        	ee.movi.32.q	q0, a5, 1
 1cc:	cd3a54        	ee.movi.32.q	q0, a5, 2
 1cf:	cd3e54        	ee.movi.32.q	q0, a5, 3
 1d2:	f46060        	extui	a6, a6, 0, 16
 1d5:	117600        	slli	a7, a6, 16
 1d8:	206670        	or	a6, a6, a7
 1db:	cdb264        	ee.movi.32.q	q1, a6, 0
 1de:	cdb664        	ee.movi.32.q	q1, a6, 1
 1e1:	cdba64        	ee.movi.32.q	q1, a6, 2
 1e4:	cdbe64        	ee.movi.32.q	q1, a6, 3
 1e7:	0fa072        	movi	a7, 15
 1ea:	130370        	wsr.sar	a7
 1ed:	930024        	ee.vld.128.ip	q2, a2, 0
 1f0:	938134        	ee.vld.128.ip	q3, a3, 16
 1f3:	9e2284        	ee.vmul.s16	q2, q2, q0
 1f6:	9eab84        	ee.vmul.s16	q3, q3, q1
 1f9:	9e1a64        	ee.vadds.s16	q2, q2, q3
 1fc:	9a0124        	ee.vst.128.ip	q2, a2, 16
 1ff:	440b      	addi.n	a4, a4, -1
 201:	fe8456        	bnez	a4, 1ed <ex18_intensity_stereo+0x39>
 204:	f01d      	retw.n
OBJECT-DISASSEMBLY-END
```

`EE.*` の語（90 個、14 種類）を `data/pie_instructions.json` の命令語図でフィールド単位にデコードした結果
（`/tmp/ex18_words.py`）。図の分割フィールド（`qa[2:1]` + `qa[0]` など）はビット位置を戻して 1 つの数に
組み直し、図と一致した定数フィールドは表示していません（一致しなければ `CONSTANT FIELD MISMATCH` と出ます）。

```text
WORD-DECODE-BEGIN
ee.* words in the object file: 90  (distinct mnemonics: 14)

mnemonic                 operands as written        word      TRM   fields decoded from the word
ee.andq                  ee.andq	q3, q6, q1         ddb0e4  p76   qa=3 qx=6 qy=1
ee.andq                  ee.andq	q7, q4, q5         fdb8a4  p76   qa=7 qx=4 qy=5
ee.andq                  ee.andq	q4, q3, q2         ed3454  p76   qa=4 qx=3 qy=2
ee.andq                  ee.andq	q5, q3, q1         edb074  p76   qa=5 qx=3 qy=1
ee.andq                  ee.andq	q2, q7, q1         dd30f4  p76   qa=2 qx=7 qy=1
ee.andq                  ee.andq	q4, q2, q1         ed3064  p76   qa=4 qx=2 qy=1
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q2, a11, 16 9101b4  p93   as=11 imm16=1 qu=2
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q2, a9, 16 910194  p93   as=9 imm16=1 qu=2
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q2, a8, 16 910184  p93   as=8 imm16=1 qu=2
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q2, a4, 16 910144  p93   as=4 imm16=1 qu=2
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q3, a14, 16 9181e4  p93   as=14 imm16=1 qu=3
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q2, a10, 16 9101a4  p93   as=10 imm16=1 qu=2
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q3, a13, 16 9181d4  p93   as=13 imm16=1 qu=3
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q3, a6, 16 918164  p93   as=6 imm16=1 qu=3
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q3, a15, 16 9181f4  p93   as=15 imm16=1 qu=3
ee.ld.128.usar.ip        ee.ld.128.usar.ip	q3, a12, 16 9181c4  p93   as=12 imm16=1 qu=3
ee.movi.32.q             ee.movi.32.q	q1, a6, 2     cdba64  p119  as=6 qu=1 sel4=2
ee.movi.32.q             ee.movi.32.q	q6, a8, 2     fd3a84  p119  as=8 qu=6 sel4=2
ee.movi.32.q             ee.movi.32.q	q1, a6, 1     cdb664  p119  as=6 qu=1 sel4=1
ee.movi.32.q             ee.movi.32.q	q5, a11, 1    edb6b4  p119  as=11 qu=5 sel4=1
ee.movi.32.q             ee.movi.32.q	q0, a5, 2     cd3a54  p119  as=5 qu=0 sel4=2
ee.movi.32.q             ee.movi.32.q	q0, a8, 0     cd3284  p119  as=8 qu=0 sel4=0
ee.movi.32.q             ee.movi.32.q	q1, a6, 0     cdb264  p119  as=6 qu=1 sel4=0
ee.movi.32.q             ee.movi.32.q	q0, a5, 3     cd3e54  p119  as=5 qu=0 sel4=3
ee.movi.32.q             ee.movi.32.q	q1, a9, 0     cdb294  p119  as=9 qu=1 sel4=0
ee.movi.32.q             ee.movi.32.q	q5, a8, 0     edb284  p119  as=8 qu=5 sel4=0
ee.movi.32.q             ee.movi.32.q	q1, a6, 3     cdbe64  p119  as=6 qu=1 sel4=3
ee.movi.32.q             ee.movi.32.q	q0, a8, 1     cd3684  p119  as=8 qu=0 sel4=1
ee.movi.32.q             ee.movi.32.q	q0, a5, 1     cd3654  p119  as=5 qu=0 sel4=1
ee.movi.32.q             ee.movi.32.q	q6, a8, 3     fd3e84  p119  as=8 qu=6 sel4=3
ee.movi.32.q             ee.movi.32.q	q0, a5, 0     cd3254  p119  as=5 qu=0 sel4=0
ee.movi.32.q             ee.movi.32.q	q6, a8, 0     fd3284  p119  as=8 qu=6 sel4=0
ee.movi.32.q             ee.movi.32.q	q1, a9, 3     cdbe94  p119  as=9 qu=1 sel4=3
ee.movi.32.q             ee.movi.32.q	q0, a8, 2     cd3a84  p119  as=8 qu=0 sel4=2
ee.movi.32.q             ee.movi.32.q	q6, a8, 1     fd3684  p119  as=8 qu=6 sel4=1
ee.movi.32.q             ee.movi.32.q	q1, a9, 2     cdba94  p119  as=9 qu=1 sel4=2
ee.movi.32.q             ee.movi.32.q	q0, a8, 3     cd3e84  p119  as=8 qu=0 sel4=3
ee.movi.32.q             ee.movi.32.q	q1, a9, 1     cdb694  p119  as=9 qu=1 sel4=1
ee.src.q                 ee.src.q	q1, q2, q3        dca314  p125  qa=1 qs0=2 qs1=3
ee.srcmb.s16.qacc        ee.srcmb.s16.qacc	q1, a7, 0 cdf274  p130  as=7 qu=1
ee.vadds.s16             ee.vadds.s16	q5, q5, q3    ae9d64  p146  qa=5 qx=5 qy=3
ee.vadds.s16             ee.vadds.s16	q3, q5, q4    9ec564  p146  qa=3 qx=5 qy=4
ee.vadds.s16             ee.vadds.s16	q2, q2, q3    9e1a64  p146  qa=2 qx=2 qy=3
ee.vadds.s16             ee.vadds.s16	q6, q6, q7    be5e64  p146  qa=6 qx=6 qy=7
ee.vadds.s16             ee.vadds.s16	q5, q5, q6    aed564  p146  qa=5 qx=5 qy=6
ee.vadds.s16             ee.vadds.s16	q6, q6, q2    be1664  p146  qa=6 qx=6 qy=2
ee.vadds.s16             ee.vadds.s16	q6, q6, q3    be1e64  p146  qa=6 qx=6 qy=3
ee.vadds.s16             ee.vadds.s16	q1, q1, q0    8e8164  p146  qa=1 qx=1 qy=0
ee.vld.128.ip            ee.vld.128.ip	q2, a2, 0    930024  p164  as=2 imm16=0 qu=2
ee.vld.128.ip            ee.vld.128.ip	q0, a3, 16   830134  p164  as=3 imm16=1 qu=0
ee.vld.128.ip            ee.vld.128.ip	q2, a2, 16   930124  p164  as=2 imm16=1 qu=2
ee.vld.128.ip            ee.vld.128.ip	q6, a2, 16   b30124  p164  as=2 imm16=1 qu=6
ee.vld.128.ip            ee.vld.128.ip	q7, a3, 16   b38134  p164  as=3 imm16=1 qu=7
ee.vld.128.ip            ee.vld.128.ip	q3, a3, 16   938134  p164  as=3 imm16=1 qu=3
ee.vmul.s16              ee.vmul.s16	q6, q2, q0     be2284  p198  qx=2 qy=0 qz=6
ee.vmul.s16              ee.vmul.s16	q6, q7, q0     be2784  p198  qx=7 qy=0 qz=6
ee.vmul.s16              ee.vmul.s16	q7, q7, q0     bea784  p198  qx=7 qy=0 qz=7
ee.vmul.s16              ee.vmul.s16	q2, q3, q0     9e2384  p198  qx=3 qy=0 qz=2
ee.vmul.s16              ee.vmul.s16	q3, q3, q0     9ea384  p198  qx=3 qy=0 qz=3
ee.vmul.s16              ee.vmul.s16	q3, q3, q1     9eab84  p198  qx=3 qy=1 qz=3
ee.vmul.s16              ee.vmul.s16	q6, q6, q0     be2684  p198  qx=6 qy=0 qz=6
ee.vmul.s16              ee.vmul.s16	q5, q5, q0     aea584  p198  qx=5 qy=0 qz=5
ee.vmul.s16              ee.vmul.s16	q2, q2, q0     9e2284  p198  qx=2 qy=0 qz=2
ee.vmul.s16              ee.vmul.s16	q5, q6, q0     aea684  p198  qx=6 qy=0 qz=5
ee.vsmulas.s16.qacc      ee.vsmulas.s16.qacc	q1, q5, 2 9e69c4  p269  qx=1 qy=5 sel8=2
ee.vsmulas.s16.qacc      ee.vsmulas.s16.qacc	q1, q5, 1 8ee9c4  p269  qx=1 qy=5 sel8=1
ee.vsmulas.s16.qacc      ee.vsmulas.s16.qacc	q1, q5, 0 8e69c4  p269  qx=1 qy=5 sel8=0
ee.vst.128.ip            ee.vst.128.ip	q1, a2, 16   8a8124  p275  as=2 imm16=1 qv=1
ee.vst.128.ip            ee.vst.128.ip	q2, a2, 16   9a0124  p275  as=2 imm16=1 qv=2
ee.vst.128.ip            ee.vst.128.ip	q7, a4, 16   ba8144  p275  as=4 imm16=1 qv=7
ee.vst.128.ip            ee.vst.128.ip	q6, a5, 16   ba0154  p275  as=5 imm16=1 qv=6
ee.vst.128.ip            ee.vst.128.ip	q5, a4, 16   aa8144  p275  as=4 imm16=1 qv=5
ee.vst.128.ip            ee.vst.128.ip	q3, a5, 16   9a8154  p275  as=5 imm16=1 qv=3
ee.vsubs.s16             ee.vsubs.s16	q1, q1, q6    8ef1d4  p281  qa=1 qx=1 qy=6
ee.vsubs.s16             ee.vsubs.s16	q5, q5, q4    aee5d4  p281  qa=5 qx=5 qy=4
ee.vsubs.s16             ee.vsubs.s16	q4, q5, q7    ae7dd4  p281  qa=4 qx=5 qy=7
ee.vsubs.s16             ee.vsubs.s16	q5, q6, q2    aeb6d4  p281  qa=5 qx=6 qy=2
ee.vsubs.s16             ee.vsubs.s16	q7, q5, q6    bef5d4  p281  qa=7 qx=5 qy=6
ee.vsubs.s16             ee.vsubs.s16	q7, q7, q2    beb7d4  p281  qa=7 qx=7 qy=2
ee.vsubs.s16             ee.vsubs.s16	q2, q2, q4    9e62d4  p281  qa=2 qx=2 qy=4
ee.xorq                  ee.xorq	q3, q4, q5         ddb9a4  p297  qa=3 qx=4 qy=5
ee.zero.q                ee.zero.q	q5               edffa4  p299  qa=5
ee.zero.qacc             ee.zero.qacc               250844  p300  

words checked: 83   constant-field mismatches: 0   not decodable from the JSON: 0

Two notes on the fields that matter to ex18:
  * EE.LD.128.USAR.IP: `as=` IS the register whose low four bits become SAR_BYTE (TRM p93),
    and the printed immediate is the byte step (imm16 = bytes/16: 16 -> imm16 = 1).
  * EE.SRC.Q / EE.SRCQ.128.ST.INCP: operand 1 is qs0 = the LOW half, operand 2 is qs1.
```

## 5. 発行スロット（サイクル実測ではない）

`objdump -d` のループ本体（`bnez` の飛び先から `bnez` まで）と、モデルの層 1 呼び出し数（`n=8` と `n=16` の
差を取ってプロローグを落としたもの）は一致します。

| ループ本体 | 命令数 | EE.* | うちスカラ | 8 サンプルあたり | 1 サンプルあたり |
|---|---|---|---|---|---|
| A `shift >= 1` | 20 | 16 | 4（`wsr.sar`×2、`addi`、`bnez`） | 20 | 2.50 |
| A `shift == 0` | 23 | 19 | 4 | 23 | 2.88 |
| B comb | 28 | 26 | 2 | 28 | 3.50 |
| C intensity | 8 | 6 | 2 | 8 | 1.00 |

プロローグ（呼び出しあたり 1 回、8 サンプルより長いフレームでは消える）: A は 22 命令（うち 15 がレーン定数の
合成 = `movi`/`slli`/`or` と 12 本の `EE.MOVI.32.Q`）、B は 59 命令（タップセットの 3 分岐と、各ゲインを
`movi`/`slli`/`addi` 3 命令で合成する 9 命令 + `EE.MOVI.32.Q` 2 本 + 10 本のポインタ初期化）、
C は 19 命令（2 つの Q15 を 4 レーンに同報する 8 本の `EE.MOVI.32.Q` を含む）。

スケジュールの根拠は `data/pie_hazards.md`（TRM Table 1.7-2、印刷ページ 65–75）です。このファイルで効く行:

```
EE.VLD.128.IP        | as 1 | qu 2, as 1 | —    | —
EE.VST.128.IP        | qv 1, as 1 | as 1 | —   | —
EE.LD.128.USAR.IP    | as 1 | qu 2, as 1 | —   | SAR_BYTE 1
EE.SRC.Q             | qs0 1, qs1 1 | qa 1 | SAR_BYTE 1 | —
EE.VMUL.S16          | qx 1, qy 1 | qz 2 | SAR 1 | —
EE.VSUBS.S16 / EE.VADDS.S16 / EE.ANDQ / EE.XORQ | qx 1, qy 1 | qa 1 | — | —
EE.VSMULAS.S16.QACC  | qx 1, qy 1 | —    | QACC_H 2, QACC_L 2 | —
EE.SRCMB.S16.QACC    | as 1 | qu 1 | QACC_H 1, QACC_L 1 | —
EE.ZERO.QACC         | —    | —    | —    | QACC_L 1, QACC_H 1
```

- **`qu` の def は M(2)、`qv`/`qs` の use は E(1)** なので、ロード直後のストア・直後の SRC.Q は
  **2 スロット**離します（実測アンカー `anchor_qs_M_to_E` = 距離 1 で 1.000 サイクル、距離 2 で 0）。
- **QACC も M(2) で def、SRCMB は E(1) で use** なので、最後の MAC と SRCMB の間は **2 スロット**です
  （B のループでは間に `addi` を置いています）。
- `E→E`（VADDS→VST など）は距離 1 で 0。
- `SAR_BYTE` の def も E(1) なので、poke ロードと直後の `SRC.Q` は距離 1 で足ります。
- **ベース ISA（`wsr.sar` / `addi` / `bnez`）の段は TRM に無い**ので、`wsr.sar` の位置は仮定です
  （`notes/03-interlock-model.md` の項目 3 と同じ扱い）。

### 5.1 レジスタ圧とスピル（「疲労の一覧」に当たる節）

`tools/pie/spills.py` は**コンパイラが出した ELF** のスタックトラフィックを測る道具なので、
手書きの `.S` にはスピルが**構造的に 0**（レジスタ割り当てをコンパイラがしないので、そもそも
「割り当てに失敗する」という失敗モードが無い）です。代わりに手書き側で同じリスクを持つのは
**設計時の本数**なので、実配分を書いておきます（`objdump` のオペランドから数えた事実）:

| カーネル | q レジスタ | a レジスタ | 備考 |
|---|---|---|---|
| A `stereo_ms` | **8 本すべて**（q0/q1 = 定数、q2..q7 = 作業） | 8 本（a2..a6, a8..a10） | a2..a5 が入出力、a6 がチャンク数、a8/a9/a10 が SAR の 3 値 |
| B `comb_filter` | 6 本（q0,q1,q2,q3,q5,q6。q4/q7 は未使用） | **14 本すべて（a2..a15）** | **これがこの設計の縛り**: 10 本が窓のポインタ、a5 がカウンタ、a7 が SRCMB のシフト。だから「安い 4 ロード版」はポインタを増やす方向には進めず、位相で分岐して同じ本数に収める設計になっています |
| C `intensity_stereo` | 4 本（q0/q1 = ゲイン、q2/q3 = 窓） | 3 本（a2..a4） | いちばん軽い |

「q レジスタを 8 本、a レジスタを 14 本しか使えない」ことが、B の形（1 窓 2 ロード）を選んだ
**物理的な理由**です。カーネル内で値を保つためにスタックへ退避する操作は 1 つもありません
（`objdump` に `s32i`/`l32i` が出てこないことがそれです）。

## 6. Python モデル（実物）と実行結果

層は 2 つです。層 1 は TRM の Operation 疑似コードから書いた命令の意味（1 命令 1 関数、ページ番号はコメントに）。
層 2 は **`.S` と同じ順序・同じレジスタ役割で書いた 3 つのカーネル**なので、2 つを並べて読めます。
層 1 の呼び出しは全部数えていて、5 節のコスト表はそこから出ています。

```python
MODEL-SOURCE-BEGIN
#!/usr/bin/env python3
"""ex18_stereo_comb -- an independent model of the three kernels in ex18_stereo_comb.S, swept against a
Python transliteration of the CELT C code they came from.

Two layers, on purpose:

  layer 1  instruction semantics, one function per PIE instruction, each written from the Operation
           pseudo-code that data/pie_instructions.json records (TRM page numbers in the comments).
           Layer 2 may not use a shortcut layer 1 does not provide: the sums, the masks, the lane
           truncations and the QACC accumulation all go through the layer-1 functions, so the model
           can disagree with the kernel about an instruction's meaning (it did: see V1 below).

  layer 2  the three kernels, written as the SAME sequence of instructions the .S emits -- same
           order, same register roles (q0..q7, a2..a15) -- so the two can be read side by side.
           Every layer-1 call is counted: that is where the operation counts in the .md come from.

What is checked, and against what
  T1  ex18_stereo_ms vs ref_stereo_ms (bands.c:458-466 with lgain = rgain = Q31ONE and kl = kr):
      every shift 0..16, 8-lane groups of boundary values and random values.
  T2  the SAME kernel vs the three lane builds that are supposed to DISAGREE with the C
      (saturate-before-shift, round-before-shift, clamp-after-shift), with the difference measured.
  T3  ex18_comb_filter (the QACC single-shift form) vs ref_comb (celt.c:166-193, one truncation per
      tap) -- the difference is the shift distribution, counted and bounded.
  T4  the C-exact form (per-tap truncation, no int16 narrowing) vs ref_comb: 0 mismatches, which is
      what says the C reference is a faithful reading of the source.
  T5  the missing-tap rule: a zero-filled history (CELT's start-of-stream state) vs a "skip the taps
      that fall off the head" variant, both against the C reading the same memory.
  T6  ex18_intensity_stereo vs ref_intensity (bands.c:398-402): the clamp the C does not have, and the
      wrap it does (as a store), counted.
  T7  the model's own instruction count per 8 samples, for the cost table.

Runner: python3 /tmp/ex18_model.py     (standard library only; also writes the case dump the C
verifier reads: /tmp/ex18_cases.txt)
"""
import json
import random

OPS = {}
DUMP = open("/tmp/ex18_cases.txt", "w")
MASK40 = (1 << 40) - 1


def _op(name):
    OPS[name] = OPS.get(name, 0) + 1


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def s40(v):
    v &= MASK40
    return v - (1 << 40) if v & (1 << 39) else v


def sat16(v):
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def sat40(v):
    return (1 << 39) - 1 if v > (1 << 39) - 1 else (-(1 << 39) if v < -(1 << 39) else v)


# --------------------------------------------------------------------------- layer 1: instruction semantics
class Mem:
    """A byte array with a base address. A 128-bit access forces as[3:0] to 0 (TRM p49), so addresses
    are explicit and a read outside the buffer is a model error, not a silent wrap."""

    def __init__(self, base, data):
        self.base = base
        self.data = bytearray(data)
        self.reads = []

    def _align(self, addr):
        a = addr & ~0xF
        off = a - self.base
        if off < 0 or off + 16 > len(self.data):
            raise IndexError("128-bit access outside the buffer: addr=%#x base=%#x len=%d"
                             % (a, self.base, len(self.data)))
        return a, off

    def read16(self, addr):
        a, off = self._align(addr)
        self.reads.append(a)
        return list(self.data[off:off + 16])

    def write16(self, addr, val):
        a, off = self._align(addr)
        self.data[off:off + 16] = bytes(val[:16])


def lanes(addr, mem, count=8):
    """The eight 16-bit lanes a 128-bit load returns, lane i = bits 16i+15:16i (little-endian)."""
    b = mem.read16(addr)
    return [b[2 * i] | (b[2 * i + 1] << 8) for i in range(count)]


def vld128_ip(mem, ptr):
    """EE.VLD.128.IP qu, as, imm   (TRM p164): qu = load128({as[31:4],4{0}}); as += imm (bytes)."""
    _op("EE.VLD.128.IP")
    return lanes(ptr, mem)


def vst128_ip(mem, ptr, qv):
    """EE.VST.128.IP qv, as, imm   (TRM p275): store128({as[31:4],4{0}}) = qv[127:0]; as += imm."""
    _op("EE.VST.128.IP")
    mem.write16(ptr, bytes(v & 0xFF for v in
                           [b for v in qv for b in (v & 0xFF, (v >> 8) & 0xFF)]))


def ld128_usar_ip(mem, ptr):
    """EE.LD.128.USAR.IP qu, as, imm   (TRM p93):
       qu[127:0] = load128({as[31:4],4{0}});  SAR_BYTE = as[3:0];  as += imm.
       The load returns the aligned chunk CONTAINING the pointer and parks the dropped bits in
       SAR_BYTE -- the coupling that both gives the funnel its shift and forces every shift to come
       from an address the kernel already holds."""
    _op("EE.LD.128.USAR.IP")
    return lanes(ptr, mem), ptr & 0xF


def src_q(qs0, qs1, sar_byte):
    """EE.SRC.Q qa, qs0, qs1   (TRM p125): qa = {qs1[127:0], qs0[127:0]} >> (SAR_BYTE[3:0] << 3).
       Operand 1 is qs0 = the LOW half (settled on silicon: src_q_operand_order_and_the_funnel_path,
       and re-read out of the emitted word by /tmp/ex18_words.py: ee.src.q q3, q1, q2 -> qs0=1 qs1=2)."""
    _op("EE.SRC.Q")
    v = int.from_bytes(b"".join(v.to_bytes(2, "little") for v in qs0), "little") \
        | (int.from_bytes(b"".join(v.to_bytes(2, "little") for v in qs1), "little") << 128)
    v = (v >> (8 * sar_byte)) & ((1 << 128) - 1)
    b = v.to_bytes(16, "little")
    return [b[2 * i] | (b[2 * i + 1] << 8) for i in range(8)]


def andq(qx, qy):
    """EE.ANDQ qa, qx, qy   (TRM p76)."""
    _op("EE.ANDQ")
    return [x & y for x, y in zip(qx, qy)]


def xorq(qx, qy):
    """EE.XORQ qa, qx, qy   (TRM p297)."""
    _op("EE.XORQ")
    return [x ^ y for x, y in zip(qx, qy)]


def zero_q():
    """EE.ZERO.Q qa   (TRM p299)."""
    _op("EE.ZERO.Q")
    return [0] * 8


def movi32_q(q, word, sel4):
    """EE.MOVI.32.Q qu, as, sel4   (TRM p119): qu[32*sel4+31:32*sel4] = as."""
    _op("EE.MOVI.32.Q")
    q = list(q)
    q[2 * sel4] = word & 0xFFFF
    q[2 * sel4 + 1] = (word >> 16) & 0xFFFF
    return q


def vadds_s16(qx, qy):
    """EE.VADDS.S16 qa, qx, qy   (TRM p146): qa[lane] = sat16(qx[lane] + qy[lane])."""
    _op("EE.VADDS.S16")
    return [sat16(s16(x) + s16(y)) & 0xFFFF for x, y in zip(qx, qy)]


def vsubs_s16(qx, qy):
    """EE.VSUBS.S16 qa, qx, qy   (TRM p281): qa[lane] = sat16(qx[lane] - qy[lane])."""
    _op("EE.VSUBS.S16")
    return [sat16(s16(x) - s16(y)) & 0xFFFF for x, y in zip(qx, qy)]


def vmul_s16(qx, qy, sar):
    """EE.VMUL.S16 qz, qx, qy   (TRM p198): qz[lane] = (signed qx[lane] * signed qy[lane]) >> SAR[5:0],
       the low 16 bits of the result. ARITHMETIC shift of the signed lane and TRUNCATION, not
       saturation (marked "measured" in ex08: examples/README.md row ex08, and ex08_media.S:15-21)."""
    _op("EE.VMUL.S16")
    return [(((s16(x) * s16(y)) >> sar) & 0xFFFF) for x, y in zip(qx, qy)]


def zero_qacc():
    """EE.ZERO.QACC   (TRM p300): QACC_L = QACC_H = 0."""
    _op("EE.ZERO.QACC")
    return [0] * 8


def vsmulas_s16_qacc(qx, qy, sel8, qacc):
    """EE.VSMULAS.S16.QACC qx, qy, sel8   (TRM p269):
       temp = qy[sel8*16+15 : sel8*16];  QACC_lane[i] = sat40(QACC_lane[i] + qx[i] * temp).
       Eight INDEPENDENT 40-bit lanes, saturating at +-2^39. This is the "16-bit lane saturating MAC"
       the kernel is built on; EE.VMULAS.S16.ACCX is the other family member and SUMS the eight
       products into one accumulator instead (data/pie_examples_measured.json ::
       vmulas_s16_accx_sums_the_lanes), which is why it is not used here."""
    _op("EE.VSMULAS.S16.QACC")
    t = s16(qy[sel8])
    return [sat40(qacc[i] + s16(qx[i]) * t) for i in range(8)]


def srcmb_s16_qacc(sar, qacc):
    """EE.SRCMB.S16.QACC qu, as, 0   (TRM p130): every 40-bit lane is shifted right by as[5:0] and
       WRITTEN BACK, and qu[lane] = sat16(shifted lane)."""
    _op("EE.SRCMB.S16.QACC")
    q = [s40(v) >> sar for v in qacc]
    return [sat16(v) & 0xFFFF for v in q], q


# --------------------------------------------------------------------------- layer 2: kernel A
def ex18_stereo_ms(mem_mid, mem_side, mem_l, mem_r, mid_a, side_a, l_a, r_a, n, shift):
    """void ex18_stereo_ms(const int16_t *mid, const int16_t *side, int16_t *l, int16_t *r,
                          uint32_t n, uint32_t shift)

    In the .S's order. q0 = 2^14 in every lane, q1 = 1 in every lane (the low-bit mask), q2 = 2 in
    every lane for the shift == 0 path; a8 = 15 (the SAR that turns a multiply into a >>1), a9 =
    shift+13, a10 = 0. Returns the number of layer-1 calls."""
    before = dict(OPS)
    chunks = n >> 3
    if chunks == 0:
        return 0
    q0 = [0] * 8
    for sel in range(4):
        q0 = movi32_q(q0, 0x40004000, sel)      # EE.MOVI.32.Q stores a 32-bit WORD: the same 16-bit
    q1 = [0] * 8                                # value has to be in BOTH halves of it to reach every
    for sel in range(4):                        # lane of the register
        q1 = movi32_q(q1, 0x00010001, sel)
    mid_p, side_p, l_p, r_p = mid_a, side_a, l_a, r_a
    if shift == 0:
        for _ in range(chunks):
            q2 = vld128_ip(mem_mid, mid_p)          # mid window
            mid_p += 16
            q3 = vld128_ip(mem_side, side_p)        # side window
            side_p += 16
            q4 = andq(q2, q1)                       # Pl
            q5 = andq(q3, q1)                       # Pr
            q6 = vmul_s16(q2, q0, 15)               # A = mid >> 1   (SAR = 15)
            q7 = andq(q4, q5)                       # carry
            q2 = vmul_s16(q3, q0, 15)               # B = side >> 1
            q3 = xorq(q4, q5)                       # b = Pl ^ Pr: the low bit of BOTH outputs
            q4 = vsubs_s16(q5, q7)                  # borrow = Pr - carry
            q5 = vsubs_s16(q6, q2)                  # A - B
            q5 = vsubs_s16(q5, q4)                  # half-diff = floor((mid - side)/2)
            q6 = vadds_s16(q6, q2)                  # A + B
            q6 = vadds_s16(q6, q7)                  # half-sum  = floor((mid + side)/2)
            q5 = vmul_s16(q5, q0, 13)               # 2*half-diff mod 2^16  (SAR = 13: x<<1)
            q6 = vmul_s16(q6, q0, 13)               # 2*half-sum  mod 2^16
            q5 = vadds_s16(q5, q3)                  # l[j] = 2*half-diff + b
            q6 = vadds_s16(q6, q3)                  # r[j] = 2*half-sum  + b
            vst128_ip(mem_l, l_p, q5)
            l_p += 16
            vst128_ip(mem_r, r_p, q6)
            r_p += 16
    else:
        sar_final = shift + 13
        for _ in range(chunks):
            q6 = vld128_ip(mem_mid, mid_p)
            mid_p += 16
            q7 = vld128_ip(mem_side, side_p)
            side_p += 16
            q3 = andq(q6, q1)                       # Pl
            q2_ = andq(q7, q1)                      # Pr
            q5 = vmul_s16(q6, q0, 15)               # A
            q4 = andq(q3, q2_)                      # carry
            q6 = vmul_s16(q7, q0, 15)               # B
            q2_ = vsubs_s16(q2_, q4)                # borrow = Pr - carry
            q7 = vsubs_s16(q5, q6)                  # A - B
            q7 = vsubs_s16(q7, q2_)                 # half-diff
            q5 = vadds_s16(q5, q6)                  # A + B
            q3 = vadds_s16(q5, q4)                  # half-sum
            q7 = vmul_s16(q7, q0, sar_final)        # l[j] = half-diff >> (shift-1)
            q3 = vmul_s16(q3, q0, sar_final)        # r[j] = half-sum  >> (shift-1)
            vst128_ip(mem_l, l_p, q7)
            l_p += 16
            vst128_ip(mem_r, r_p, q3)
            r_p += 16
    return sum(OPS[k] - before.get(k, 0) for k in OPS)


# --------------------------------------------------------------------------- layer 2: kernel B
GAINS = ((10048, 7112, 4248), (15200, 8784, 0), (26208, 3280, 0))


def gain_reg(tapset):
    """q5 = the three Q15 gains in lanes 0,1,2 (EE.ZERO.Q + two EE.MOVI.32.Q in the .S)."""
    q = zero_q()
    q = movi32_q(q, (GAINS[tapset][1] << 16) | GAINS[tapset][0], 0)
    q = movi32_q(q, GAINS[tapset][2], 1)
    return q


def ex18_comb_filter(mem_y, mem_x, mem_hist, y_a, x_a, hist_a, n, tapset, shift):
    """void ex18_comb_filter(int16_t *y, const int16_t *x, const int16_t *hist, uint32_t n,
                            uint32_t tapset, uint32_t shift)

    In the .S's order. `hist_a` is the address of hist[0]; the kernel reaches hist[-2] and
    hist[n+1], so the caller's buffer must cover [floor(hist-2), floor(hist+n+1)+32) bytes. Each of
    the five windows is a {low chunk, high chunk, SRC.Q} triple: the two USAR loads take their
    aligned chunk (floor of the address, and that +16) and both set SAR_BYTE to the address's low
    four bits, which adding 16 preserves -- so one SRC.Q per window works at ANY alignment of hist.
    Returns the number of layer-1 calls."""
    before = dict(OPS)
    chunks = n >> 3
    if chunks == 0:
        return 0
    q5 = gain_reg(tapset)
    q6 = zero_q()
    for sel in range(4):
        q6 = movi32_q(q6, 0x00010001, sel)          # the +1 of the fixed-point bias, every lane
    p = [hist_a + 2 * m for m in (-2, -1, 0, 1, 2)]     # P_m
    q = [a + 16 for a in p]                            # Q_m
    for _ in range(chunks):
        q0 = vld128_ip(mem_x, x_a)                  # the direct tap x[i..i+7]
        x_a += 16
        qacc = zero_qacc()
        for m in (-2, -1, 0, 1, 2):                 # five windows, in the .S's order
            q2, sb2 = ld128_usar_ip(mem_hist, p[m + 2])
            q3, sb3 = ld128_usar_ip(mem_hist, q[m + 2])
            assert sb2 == sb3, "adding 16 must preserve the low four address bits"
            p[m + 2] += 16
            q[m + 2] += 16
            q1 = src_q(q2, q3, sb2)                 # the taps hist[i+k+m]
            qacc = vsmulas_s16_qacc(q1, q5, {0: 0, -2: 2, 2: 2, -1: 1, 1: 1}[m], qacc)
        q1, qacc = srcmb_s16_qacc(shift, qacc)      # sat16(40-bit lane >> shift)
        q1 = vadds_s16(q1, q0)                      # + x[i+k]
        q1 = vsubs_s16(q1, q6)                      # - 1  (celt.c:184)
        vst128_ip(mem_y, y_a, q1)
        y_a += 16
    return sum(OPS[k] - before.get(k, 0) for k in OPS)


# --------------------------------------------------------------------------- layer 2: kernel C
def ex18_intensity_stereo(mem_x, mem_y, x_a, y_a, n, a1, a2):
    """void ex18_intensity_stereo(int16_t *x, const int16_t *y, uint32_t n, uint32_t a1, uint32_t a2)

    bands.c:398-402 in 16-bit lanes: the two Q15 multiplies truncate exactly as MULT16_32_Q15 does,
    and the sum is a 16-bit SATURATING add where the C keeps 17 bits. Returns layer-1 calls."""
    before = dict(OPS)
    chunks = n >> 3
    if chunks == 0:
        return 0
    w1 = ((a1 & 0xFFFF) << 16) | (a1 & 0xFFFF)
    w2 = ((a2 & 0xFFFF) << 16) | (a2 & 0xFFFF)
    q0 = [0] * 8
    for sel in range(4):
        q0 = movi32_q(q0, w1, sel)
    q1 = [0] * 8
    for sel in range(4):
        q1 = movi32_q(q1, w2, sel)
    for _ in range(chunks):
        q2 = vld128_ip(mem_x, x_a)
        q3 = vld128_ip(mem_y, y_a)
        y_a += 16
        q2 = vmul_s16(q2, q0, 15)
        q3 = vmul_s16(q3, q1, 15)
        q2 = vadds_s16(q2, q3)
        vst128_ip(mem_x, x_a, q2)
        x_a += 16
    return sum(OPS[k] - before.get(k, 0) for k in OPS)


# --------------------------------------------------------------------------- the C, in Python
Q31ONE = 2147483647
SIG_SAT = 536870911
Q15ONE = 32767


def c_mult32_32_q31(a, b):
    """fixed_generic.h:69 (OPUS_FAST_INT64 path; the value is what the decomposed line 71 computes)."""
    return ((a * b) >> 31)


def c_mult16_16_p15(a, b):
    """fixed_generic.h:197: (16384 + a*b) >> 15 -- ROUNDED, used only for the per-call tap gains."""
    return (16384 + a * b) >> 15


def c_mult16_32_q15(a, b):
    """fixed_generic.h:57, the OPUS_FAST_INT64 == 0 branch (xtensa has no int64: arch.h:125-128) --
    ADD32(SHL(MULT16_16(a, SHR(b,16)),1), SHR(MULT16_16SU(a, b&0xffff),15)), whose value is
    floor(a*b / 2^15). Verified against the int64 form by the host C program."""
    return (a * b) >> 15


def c_mult16_32_q15_decomposed(a, b):
    """fixed_generic.h:57 verbatim -- the branch xtensa takes (OPUS_FAST_INT64 == 0):
       ADD32(SHL(MULT16_16(a, SHR(b,16)), 1), SHR(MULT16_16SU(a, (b & 0xffff)), 15))
       ADD32 is a plain 32-bit add, MULT16_16SU is a signed x unsigned 16-bit product, SHR is an
       arithmetic shift. Comparing this against the int64 form floor(a*b/2^15) is what licenses the
       kernel's "one floor per tap" reading (T4b)."""
    a = s16(a)
    hi = b >> 16
    lo = b & 0xFFFF
    left = s32(a * hi) << 1
    right = (a * lo) >> 15
    return s32(left + right)


def c_vshr32(a, sh):
    """fixed_generic.h:125: (((shift)>0) ? SHR32(a,shift) : SHL32(a,-(shift))), SHR32 = plain >>."""
    return (a >> sh) if sh > 0 else (a << -sh)


def c_saturate(x, a):
    return a if x > a else (-a if x < -a else x)


def ref_stereo_ms(mid, side, shift):
    """bands.c:458-466 with UNIT gains and kl = kr = 15 + shift -- the specialisation this kernel's
    contract is (the two real gains and the two ilogs come from the band energies, bands.c:429-456,
    and are the caller's business here).

      l = MULT32_32_Q31(mid, X[j])   with mid = 2^31  ->  exactly X[j], so l = the caller's mid[j]
      X[j] = VSHR32(MULT32_32_Q31(2^31, SUB32(l,r)), kl-15) = VSHR32(l-r, shift)
      Y[j] = VSHR32(l+r, shift)

    and the C's narrowing store is the model's int16 conversion (wrapping, gcc/xtensa semantics).
    NOTE the C cannot express the unit gain: its largest Q31 is Q31ONE = 2^31-1, for which
    MULT32_32_Q31 is v-1 whenever v >= 1 -- that is variant_q31one_gain below, and it is one of the
    forms that disagrees with this kernel."""
    out = []
    for m, sd in zip(mid, side):
        out.append((s16(c_vshr32(m - sd, shift)), s16(c_vshr32(m + sd, shift))))
    return out


def variant_q31one_gain(mid, side, shift):
    """bands.c:458-466 with lgain = rgain = mid = Q31ONE (2^31-1) exactly as the C's macros compute
    it: MULT32_32_Q31(Q31ONE, v) = floor(v*(2^31-1)/2^31) = v-1 for v >= 1 and v for v <= 0, applied
    TWICE per output (once for the mid normalisation, once for the gain). A 4th disagreeing form, and
    the one a real CELT call is closest to."""
    out = []
    for m, sd in zip(mid, side):
        l = c_mult32_32_q31(Q31ONE, m)                          # MULT32_32_Q31(mid, X[j])
        dl = c_mult32_32_q31(Q31ONE, l - sd)                    # MULT32_32_Q31(lgain, SUB32(l,r))
        dr = c_mult32_32_q31(Q31ONE, l + sd)                    # MULT32_32_Q31(rgain, ADD32(l,r))
        out.append((s16(c_vshr32(dl, shift)), s16(c_vshr32(dr, shift))))
    return out


def ref_comb_gains(tapset):
    """celt.c:267-272 with g0 = g1 = Q15ONE: g0k = MULT16_16_P15(Q15ONE, gains[tapset][k])."""
    return tuple(c_mult16_16_p15(Q15ONE, g) for g in GAINS[tapset])


def ref_comb(x, hlist, n, tapset, shift, off=2):
    """celt.c:166-193 (comb_filter_const_c, the non-ARM, non-QEXT branch) for i in [0,n):
       y[i] = x[i] + MULT16_32_Q15(g10, x[i-T]) + MULT16_32_Q15(g11, x[i-T+1]+x[i-T-1])
                  + MULT16_32_Q15(g12, x[i-T+2]+x[i-T-2]) - 1, then SATURATE(., SIG_SAT).
       `hlist` is a list whose element 0 is hist[-off] (off = 2), i.e. the buffer the caller hands the
       kernel with its first two samples already behind hist[0]. hist[j] plays x[j-T]: T appears
       nowhere else, which is exactly the kernel's contract. The earliest sample the C touches is
       hlist[0] = hist[-2] and the latest hlist[off+n+1] = hist[n-1]."""
    g0, g1, g2 = ref_comb_gains(tapset)
    assert shift == 15, "the C's Q15 form is shift = 15"
    out = []
    for i in range(n):
        t = x[i]
        t += c_mult16_32_q15(g0, hlist[off + i])
        t += c_mult16_32_q15(g1, hlist[off + i + 1] + hlist[off + i - 1])
        t += c_mult16_32_q15(g2, hlist[off + i + 2] + hlist[off + i - 2])
        t -= 1
        out.append(c_saturate(t, SIG_SAT))
    return out


def ref_comb_exact(x, hist, n, tapset):
    """The same arithmetic WITHOUT the 16-bit narrowing: the C's own value, as an integer that may
    exceed int16. This is the reference the kernel's saturating lanes are compared against."""
    return ref_comb(x, hist, n, tapset, 15)


def ref_intensity(X, Y, a1, a2):
    """bands.c:398-402: X[j] = ADD32(MULT16_32_Q15(a1, X[j]), MULT16_32_Q15(a2, Y[j]))."""
    return [c_mult16_32_q15(a1, x) + c_mult16_32_q15(a2, y) for x, y in zip(X, Y)]


# --------------------------------------------------------------------------- the disagreeing lane builds
def variant_saturate_then_shift(mid, side, shift):
    """A) the naive 16-bit lane build: EE.VSUBS.S16 / EE.VADDS.S16 of the two windows FIRST, then the
    shift. The 17-bit intermediate saturates at +-32767 before the shift, so the result is wrong
    wherever |mid -+ side| > 32767. This is the form the .S does NOT use."""
    out = []
    for m, sd in zip(mid, side):
        out.append((s16(sat16(s16(m) - s16(sd))) >> shift, s16(sat16(s16(m) + s16(sd))) >> shift))
    return out


def variant_round_then_shift(mid, side, shift):
    """B) rounding instead of truncation: PSHR32 (fixed_generic.h:123) adds 1<<(shift-1) before the
    shift. bands.c uses VSHR32, i.e. NO rounding, so this differs by 1 LSB on the ties."""
    out = []
    for m, sd in zip(mid, side):
        if shift == 0:
            out.append((s16(s16(m) - s16(sd)), s16(s16(m) + s16(sd))))
        else:
            out.append((s16((s16(m) - s16(sd) + (1 << (shift - 1))) >> shift),
                        s16((s16(m) + s16(sd) + (1 << (shift - 1))) >> shift)))
    return out


def variant_clamp_after_shift(mid, side, shift):
    """C) the exact shifted value, then an int16 CLAMP (EE.VMIN/VMAX.S16) instead of the C's
    narrowing store. For shift >= 1 the value already fits, so this is a no-op; for shift == 0 it
    turns every wrap into a clamp."""
    out = []
    for m, sd in zip(mid, side):
        if shift == 0:
            out.append((sat16(s16(m) - s16(sd)), sat16(s16(m) + s16(sd))))
        else:
            out.append((s16((s16(m) - s16(sd)) >> shift), s16((s16(m) + s16(sd)) >> shift)))
    return out


def variant_skip_missing_taps(x, hlist, n, tapset, off=2):
    """T5: the "skip the taps that fall off the head of the history" rule -- the alternative to
    CELT's unconditional read. At i = 0 the hist[i-2] and hist[i-1] taps are dropped (not zeroed)
    and at i = 1 the hist[i-2] tap is dropped, which is what a kernel that avoids the out-of-range
    address would do. CELT never does this: it reads x[-T-2] and the state is zero-initialised."""
    g0, g1, g2 = ref_comb_gains(tapset)
    out = []
    for i in range(n):
        t = x[i]
        t += c_mult16_32_q15(g0, hlist[off + i])
        if i - 1 >= 0:
            t += c_mult16_32_q15(g1, hlist[off + i + 1] + hlist[off + i - 1])
        if i - 2 >= 0:
            t += c_mult16_32_q15(g2, hlist[off + i + 2] + hlist[off + i - 2])
        t -= 1
        out.append(c_saturate(t, SIG_SAT))
    return out


# --------------------------------------------------------------------------- sweep helpers
def mk(values, pad=32):
    """values (signed ints) -> (Mem, address of values[0]). Addresses are real so that a 128-bit
    access off the grid lands where the hardware would put it."""
    base = 0x3F800000
    data = bytearray(pad * 2)
    for v in values:
        data += (v & 0xFFFF).to_bytes(2, "little")
    data += bytearray(pad * 2)
    return Mem(base, bytes(data)), base + pad * 2


def read16_at(mem, addr, n):
    off = addr + 0 - mem.base
    return [mem.data[off + 2 * i] | (mem.data[off + 2 * i + 1] << 8) for i in range(n)]


def first_diff(got, want):
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return i, a, b
    return None


def quantise(vals):
    return [s16(v) for v in vals]


results = {}
random.seed(0xE18)


def case_A(n, shift, mids, sides, label, tag):
    """Run kernel A over one 8-sample group and the reference, compare, and dump for the C verifier."""
    mm, ma = mk(mids)
    sm, sa = mk(sides)
    lm, la = mk([0] * n)
    rm, ra = mk([0] * n)
    ex18_stereo_ms(mm, sm, lm, rm, ma, sa, la, ra, n, shift)
    got_l = [s16(v) for v in read16_at(lm, la, n)]
    got_r = [s16(v) for v in read16_at(rm, ra, n)]
    ref = ref_stereo_ms(mids, sides, shift)
    want_l = quantise([x[0] for x in ref])
    want_r = quantise([x[1] for x in ref])
    d = first_diff(got_l, want_l)
    if d is None:
        d = first_diff(got_r, want_r)
    bad = sum(1 for a, b in zip(got_l, want_l) if a != b) + sum(1 for a, b in zip(got_r, want_r) if a != b)
    if bad:
        results.setdefault("T1_first", "%s: l[%d] got %d want %d (shift=%d)" % (label, d[0], d[1], d[2], shift))
    if tag and n <= 24:
        DUMP.write("A %d %d %s %s %s %s\n" % (n, shift, hexof(mids), hexof(sides), hexof(got_l), hexof(got_r)))
    return bad


def hexof(vals):
    return b"".join((v & 0xFFFF).to_bytes(2, "little") for v in vals).hex()


def hexof32(vals):
    """The C reference's own values are opus_val32 (17 bits and more): the dump has to carry them at
    full width or the comparison against the C would be made against a narrowed copy."""
    return b"".join((v & 0xFFFFFFFF).to_bytes(4, "little") for v in vals).hex()


def case_C(n, a1, a2, xs, ys, tag):
    xm, xa = mk(xs)
    ym, ya = mk(ys)
    ex18_intensity_stereo(xm, ym, xa, ya, n, a1, a2)
    got = [s16(v) for v in read16_at(xm, xa, n)]
    exact = ref_intensity(xs, ys, a1, a2)
    want = [s16(v) for v in exact]
    bad = sum(1 for a, b in zip(got, want) if a != b)
    clamped = sum(1 for b, g in zip(exact, got) if g != s16(b) and g == sat16(b))
    d = first_diff(got, want)
    if bad:
        results.setdefault("T6_first", "got %d want %d at %d (a1=%d a2=%d) exact=%d"
                           % (d[1], d[2], d[0], a1, a2, exact[d[0]]))
    if tag and n <= 24:
        DUMP.write("C %d %d %d %s %s %s\n" % (n, a1, a2, hexof(xs), hexof(ys), hexof(got)))
    return bad, clamped


# =========================================================================================== the runs
t = {k: [0, 0] for k in ("T1", "T2", "T3", "T4", "T5", "T6")}

print("ex18_stereo_comb model -- instruction semantics from data/pie_instructions.json Operation")
print()

# ---- T1: kernel A (shift >= 1 and shift == 0) vs the C, over the whole input range boundaries ----
BOUND = (-32768, -32767, -16385, -16384, -1, 0, 1, 8191, 16383, 16384, 32766, 32767)
for shift in range(0, 17):
    groups = []
    groups.append(([BOUND[i % len(BOUND)] for i in range(8)], [BOUND[(i * 5 + 3) % len(BOUND)] for i in range(8)]))
    for m in BOUND:
        for sd in BOUND:
            groups.append(([m] * 8, [sd] * 8))
    for _ in range(600):
        groups.append(([random.randint(-32768, 32767) for _ in range(8)],
                       [random.randint(-32768, 32767) for _ in range(8)]))
    for i, (mids, sides) in enumerate(groups):
        t["T1"][0] += 1
        bad = case_A(8, shift, mids, sides, "T1 shift=%d" % shift, tag=True)
        t["T1"][1] += 1 if bad else 0
t1_groups = t["T1"][0]

# ---- T1b: the half-value identity, exhaustively over every mid value -------------------------------
exh_bad = exh_cases = exh_range = 0
for shift in range(1, 17):
    for sd in (-32768, -32767, -16384, -1, 0, 1, 8191, 32767):
        for m in range(-32768, 32768):
            l_ref = s16(c_vshr32(m - sd, shift))
            r_ref = s16(c_vshr32(m + sd, shift))
            a, b = m >> 1, sd >> 1
            pl, pr = m & 1, sd & 1
            carry = pl & pr
            borrow = pr - carry
            hd = a - b - borrow
            hs = a + b + carry
            exh_cases += 1
            if not (-32768 <= hd <= 32767 and -32768 <= hs <= 32767):
                exh_range += 1
            if s16(((hd * (1 << 14)) >> (shift + 13)) & 0xFFFF) != l_ref or \
               s16(((hs * (1 << 14)) >> (shift + 13)) & 0xFFFF) != r_ref:
                exh_bad += 1
                if "T1_exh" not in results:
                    results["T1_exh"] = ("m=%d sd=%d shift=%d hd=%d hs=%d" % (m, sd, shift, hd, hs))
print("T1b exhaustive half-value identity: %d (mid, side, shift) triples, %d disagree,"
      % (exh_cases, exh_bad))
print("     %d half-values that left int16  [every mid value, 8 side values, shifts 1..16]" % exh_range)
print()

# ---- T2: the three lane builds that must disagree, measured -----------------------------------------
t2 = {}
for shift in (0, 1, 2, 3, 5, 8, 11, 16):
    for name, fn in (("saturate-then-shift (EE.VSUBS.S16 first)", variant_saturate_then_shift),
                     ("round-then-shift (PSHR32)", variant_round_then_shift),
                     ("gains = Q31ONE (2^31-1), not unity", variant_q31one_gain)):
        cases = bad_l = bad_r = 0
        maxd = 0
        worst = None
        for _ in range(2000):
            mids = [random.randint(-32768, 32767) for _ in range(8)]
            sides = [random.randint(-32768, 32767) for _ in range(8)]
            ref = ref_stereo_ms(mids, sides, shift)
            try:
                var = fn(mids, sides, shift)
            except Exception:
                continue
            cases += 16                      # two outputs (l and r) per sample
            for i in range(8):
                bl = abs(s16(var[i][0]) - s16(ref[i][0]))
                br = abs(s16(var[i][1]) - s16(ref[i][1]))
                bad_l += 1 if bl else 0
                bad_r += 1 if br else 0
                if max(bl, br) > maxd:
                    maxd = max(bl, br)
                    j = 0 if bl >= br else 1
                    worst = (mids[i], sides[i], s16(var[i][j]), s16(ref[i][j]))
        t2[(shift, name)] = (cases, bad_l + bad_r, maxd, worst)

print("T2  the DISAGREEING forms, 16000 samples per shift (8-lane groups, random):")
print("     %-38s %-8s %-10s %-8s %s" % ("form", "shift", "differ", "max|d|", "largest differ (mid, side, got, ref)"))
for (shift, name), (cases, bad, maxd, worst) in sorted(t2.items()):
    w = ("m=%d sd=%d got=%d ref=%d" % worst[0:4]) if worst else "-"
    print("     %-38s %-8d %-10s %-8d %s" % (name, shift, "%d/%d" % (bad, cases), maxd, w))
print()

# ---- T2b: clamp-after-shift is a no-op for shift >= 1 and 2^16 per wrap at shift == 0 --------------
clamp_rows = []
for shift in (0, 1, 2, 5, 16):
    bad = cases = 0
    maxd = 0
    for _ in range(2000):
        mids = [random.randint(-32768, 32767) for _ in range(8)]
        sides = [random.randint(-32768, 32767) for _ in range(8)]
        ref = ref_stereo_ms(mids, sides, shift)
        for i in range(8):
            cases += 2
            m, sd = mids[i], sides[i]
            if shift == 0:
                cl = sat16(s16(m) - s16(sd))
                cr = sat16(s16(m) + s16(sd))
            else:
                cl = s16((s16(m) - s16(sd)) >> shift)
                cr = s16((s16(m) + s16(sd)) >> shift)
            bad += 1 if cl != s16(ref[i][0]) else 0
            bad += 1 if cr != s16(ref[i][1]) else 0
            maxd = max(maxd, abs(cl - s16(ref[i][0])), abs(cr - s16(ref[i][1])))
    clamp_rows.append((shift, bad, cases, maxd))
print("T2b clamp-after-shift (EE.VMIN/VMAX.S16 on the exact shifted value) vs the C's narrowing store:")
for shift, bad, cases, maxd in clamp_rows:
    print("     shift=%-3d %d/%d differ, max|d| = %d%s"
          % (shift, bad, cases, maxd, "   (2^16 per wrap: the C keeps the low 16 bits)" if shift == 0 else ""))
print()

# ---- T3/T4: the comb filter ---------------------------------------------------------------------
comb_rows = []
for tapset in (0, 1, 2):
    for n in (8, 16, 24, 64):
        differ = small = large = maxd = oor = cases = 0
        for _ in range(40):
            x = [random.randint(-32768, 32767) for _ in range(n)]
            hist = [random.randint(-32768, 32767) for _ in range(n + 8)]
            ym, ya = mk([0] * n)
            xm, xa = mk(x)
            hm, ha = mk(hist)
            ex18_comb_filter(ym, xm, hm, ya, xa, ha + 4, n, tapset, 15)
            got = [s16(v) for v in read16_at(ym, ya, n)]
            want = ref_comb(x, [s16(v) for v in hist], n, tapset, 15)
            if _ == 0 and n <= 24:
                DUMP.write("B %d %d %d %s %s %s %s\n" % (n, tapset, 15, hexof(x), hexof(hist),
                                                          hexof(got), hexof32(want)))
            for g, w in zip(got, want):
                cases += 1
                d = abs(g - w)
                if d:
                    differ += 1
                    if d <= 3:
                        small += 1
                    else:
                        large += 1
                    maxd = max(maxd, d)
                if w > 32767 or w < -32768:
                    oor += 1
        comb_rows.append((tapset, n, cases, differ, small, large, maxd, oor, 0))
print("T3  ex18_comb_filter (ONE shift at the readout, 16-bit lanes) vs celt.c:166-193 (one truncation")
print("    PER TAP, 17-bit sums, clamp at SIG_SAT = 2^29-1 which never fires on this data):")
print("     %-7s %-4s %-8s %-8s %-9s %-9s %-8s %s"
      % ("tapset", "n", "samples", "differ", "|d|<=3", "|d|>3", "max|d|", "C value outside int16"))
for tapset, n, cases, differ, small, large, maxd, oor, _c in comb_rows:
    print("     %-7d %-4d %-8d %-8d %-9d %-9d %-8d %d"
          % (tapset, n, cases, differ, small, large, maxd, oor))
print("     |d| <= 3 is the shift distribution: CELT floors three times (once per tap) and this kernel")
print("     floors once, so the two differ by the rounding residue of the shift, bounded by the tap")
print("     count. |d| > 3 is the 16-bit lane: the C's tap sum needs 17 bits (its own output reaches")
print("     x + 52297 = 85065 for full-scale input) and this kernel clamps at SRCMB's readout and at")
print("     the +x and -1 adds. The .md counts both.")
print()

# T3b/T4b: the decomposed MULT16_32_Q15 vs the int64 form, and the C-exact model vs the reference
dec_cases = dec_bad = 0
for _ in range(200000):
    a = random.randint(-32768, 32767)
    b = random.randint(-65536, 65536)
    dec_cases += 1
    if c_mult16_32_q15_decomposed(a, b) != c_mult16_32_q15(a, b):
        dec_bad += 1
exact_cases = exact_bad = 0
for _ in range(400):
    n = 16
    tapset = random.randrange(3)
    x = [random.randint(-32768, 32767) for _ in range(n)]
    hist = [random.randint(-32768, 32767) for _ in range(n + 8)]
    want = ref_comb(x, [s16(v) for v in hist], n, tapset, 15)
    got = []
    g0, g1, g2 = ref_comb_gains(tapset)
    for i in range(n):
        acc = x[i]
        acc += c_mult16_32_q15_decomposed(g0, hist[2 + i])
        acc += c_mult16_32_q15_decomposed(g1, hist[2 + i + 1] + hist[2 + i - 1])
        acc += c_mult16_32_q15_decomposed(g2, hist[2 + i + 2] + hist[2 + i - 2])
        acc -= 1
        got.append(c_saturate(acc, SIG_SAT))
    for a_, b_ in zip(got, want):
        exact_cases += 1
        exact_bad += 1 if a_ != b_ else 0
print("T3b the decomposed MULT16_32_Q15 (fixed_generic.h:57, the branch xtensa takes) vs the int64 form")
print("    floor(a*b/2^15): %d (a,b) pairs, %d disagree  [this is what licenses 'one floor per tap']"
      % (dec_cases, dec_bad))
print("T4b the C-exact form (per-tap floor, 17-bit sums, SIG_SAT clamp) written from the DECOMPOSED")
print("    macro, vs the reference: %d values, %d disagree  [0 is what says the reference is faithful]"
      % (exact_cases, exact_bad))
print()

# T4: the C-exact form (per-tap truncation) vs the C reference, and the two C variants of the gain
gain_rows = []
for tapset in (0, 1, 2):
    table = GAINS[tapset]
    rounded = ref_comb_gains(tapset)
    gain_rows.append((tapset, table, rounded))
print("T4  the per-call gain multiply celt.c:267-272 with g0 = g1 = Q15ONE, MULT16_16_P15(Q15ONE, g):")
for tapset, table, rounded in gain_rows:
    print("     tapset %d  table %-22s -> C %-22s %s"
          % (tapset, table, rounded, "same" if tuple(table) == rounded
             else "DIFFERS by %d on tap %d" % (rounded[0] - table[0],
                                               [i for i in range(3) if rounded[i] != table[i]][0])))
print()

# T5: the missing-tap rule: zero-filled history (CELT's state) vs skipping the taps -----------------
t5 = {}
for tapset in (0, 1, 2):
    n = 16
    x = [random.randint(-32768, 32767) for _ in range(n)]
    hist = [0, 0] + [random.randint(-32768, 32767) for _ in range(n)]        # hist[-2] = hist[-1] = 0
    hist += [0, 0]
    hlist = [s16(v) for v in hist]
    ref = ref_comb(x, hlist, n, tapset, 15)
    skip = variant_skip_missing_taps(x, hlist, n, tapset)
    d = [abs(a - b) for a, b in zip(ref, skip)]
    t5[tapset] = (sum(1 for v in d if v), max(d), d[:2])
    DUMP.write("S %d %d %d %s %s %s %s\n" % (n, tapset, 15, hexof(x), hexof(hist),
                                              hexof32(skip), hexof32(ref)))
print("T5  the missing taps at the head of the history (n = 16, hist[-2] = hist[-1] = 0, 3 tapsets):")
print("     zero-filled history == the C reading the same memory: by construction (the C reads memory)")
for tapset, (nd, mx, head) in t5.items():
    print("     tapset %d: 'skip the missing taps' differs on %d of 16 samples, max|d| = %d, |d| at i=0,1 = %s"
          % (tapset, nd, mx, head))
print()

# ---- T6: intensity stereo ------------------------------------------------------------------------
t6_rows = []
for (a1, a2) in ((23170, 23170), (32767, 0), (0, 32767), (30000, 30000), (20000, 26000), (32767, 32767)):
    bad = cases = clamp = 0
    for _ in range(200):
        xs = [random.randint(-32768, 32767) for _ in range(8)]
        ys = [random.randint(-32768, 32767) for _ in range(8)]
        b, c = case_C(8, a1, a2, xs, ys, tag=True)
        cases += 8
        bad += b
        clamp += c
    t6_rows.append((a1, a2, cases, bad, clamp))
print("T6  ex18_intensity_stereo (16-bit lane saturating add) vs bands.c:398-402 (32-bit ADD32):")
print("     %-8s %-8s %-9s %-8s %s" % ("a1", "a2", "samples", "differ", "where the lane clamps"))
for a1, a2, cases, bad, clamp in t6_rows:
    print("     %-8d %-8d %-9d %-8s %d" % (a1, a2, cases, "%d/%d" % (bad, cases), clamp))
print()

# ---- T7: instruction counts per 8 samples --------------------------------------------------------
counts = {}
for shift in (0, 3):
    tot = {}
    for n in (8, 16):
        mm, ma = mk([0] * n)
        sm, sa = mk([0] * n)
        lm, la = mk([0] * n)
        rm, ra = mk([0] * n)
        tot[n] = ex18_stereo_ms(mm, sm, lm, rm, ma, sa, la, ra, n, shift)
    counts["stereo_ms shift=%d (loop body)" % shift] = tot[16] - tot[8]
    counts["stereo_ms shift=%d (whole call, 8 samples)" % shift] = tot[8]
for tapset in (0,):
    tot = {}
    for n in (8, 16):
        ym, ya = mk([0] * n)
        xm, xa = mk([0] * n)
        hm, ha = mk([0] * (n + 40))
        tot[n] = ex18_comb_filter(ym, xm, hm, ya, xa, ha + 4, n, 0, 15)
    counts["comb_filter (loop body)"] = tot[16] - tot[8]
    counts["comb_filter (whole call, 8 samples)"] = tot[8]
tot = {}
for n in (8, 16):
    xm, xa = mk([0] * n)
    ym, ya = mk([0] * n)
    tot[n] = ex18_intensity_stereo(xm, ym, xa, ya, n, 23170, 23170)
counts["intensity_stereo (whole call, 8 samples)"] = tot[8]
counts["intensity_stereo (loop body)"] = tot[16] - tot[8]
print("T7  layer-1 calls, from the model (the loop-body figure is the difference between an 8- and")
print("    a 16-sample call, so the prologue drops out; the objdump's loop body is the same count):")
for k, v in counts.items():
    print("     %-22s %d" % (k, v))
print()

DUMP.close()
print("Python model vs the CELT C reference, in Python:")
print("     T1  stereo_ms vs bands.c:458-466        %d groups, %d bad" % (t["T1"][0], t["T1"][1]))
print("     T1b the half-value identity, exhaustive %d triples, %d bad" % (exh_cases, exh_bad))
print("     T2  the four disagreeing forms, measured       (see the table)")
print("     T3b MULT16_32_Q15 decomposed vs int64           %d pairs, %d bad" % (dec_cases, dec_bad))
print("     T4b the C-exact form vs the reference           %d values, %d bad" % (exact_cases, exact_bad))
print("     T3  comb_filter vs celt.c:166-193       %d samples, %d differ (|d|<=3 shift distribution,"
      % (sum(r[2] for r in comb_rows), sum(r[3] for r in comb_rows)))
print("         |d|>3 = the 16-bit lane clamp: see the table)")
print("     T5  the 'skip missing taps' rule vs the C  %d of 48 affected samples (see the table)"
      % sum(v[0] for v in t5.values()))
print("     T6  intensity_stereo vs bands.c:398-402 %d samples, %d bad (the C's 32-bit sum does not"
      % (sum(r[2] for r in t6_rows), sum(r[3] for r in t6_rows)))
print("         fit a 16-bit lane)")
print()
print("first mismatches:")
for k in sorted(results):
    print("     %-10s %s" % (k, results[k]))
print()
print("layer-1 instruction calls over the whole sweep (the .md's cost table source):")
for k in sorted(OPS):
    print("     %-22s %d" % (k, OPS[k]))
print()
print("case dump for the C verifier: /tmp/ex18_cases.txt (%d lines)"
      % sum(1 for _ in open("/tmp/ex18_cases.txt")))
```

実行結果（そのまま貼ります）:

```text
ex18_stereo_comb model -- instruction semantics from data/pie_instructions.json Operation

T1b exhaustive half-value identity: 8388608 (mid, side, shift) triples, 0 disagree,
     0 half-values that left int16  [every mid value, 8 side values, shifts 1..16]

T2  the DISAGREEING forms, 16000 samples per shift (8-lane groups, random):
     form                                   shift    differ     max|d|   largest differ (mid, side, got, ref)
     gains = Q31ONE (2^31-1), not unity     0        19987/32000 2        m=26300 sd=4972 got=21326 ref=21328
     round-then-shift (PSHR32)              0        0/32000    0        -
     saturate-then-shift (EE.VSUBS.S16 first) 0        7975/32000 65534    m=-23768 sd=-9002 got=-32768 ref=32766
     gains = Q31ONE (2^31-1), not unity     1        16035/32000 1        m=20911 sd=-25135 got=23022 ref=23023
     round-then-shift (PSHR32)              1        16076/32000 1        m=26191 sd=21940 got=2126 ref=2125
     saturate-then-shift (EE.VSUBS.S16 first) 1        8039/32000 16207    m=-32642 sd=-32540 got=-16384 ref=-32591
     gains = Q31ONE (2^31-1), not unity     2        8047/32000 1        m=4279 sd=-26553 got=7707 ref=7708
     round-then-shift (PSHR32)              2        16053/32000 1        m=4188 sd=17583 got=5443 ref=5442
     saturate-then-shift (EE.VSUBS.S16 first) 2        7938/32000 8137     m=-32661 sd=-32655 got=-8192 ref=-16329
     gains = Q31ONE (2^31-1), not unity     3        4051/32000 1        m=-7210 sd=-28970 got=2719 ref=2720
     round-then-shift (PSHR32)              3        16061/32000 1        m=-11078 sd=-23661 got=1573 ref=1572
     saturate-then-shift (EE.VSUBS.S16 first) 3        7977/32000 4055     m=-32601 sd=-32607 got=-4096 ref=-8151
     gains = Q31ONE (2^31-1), not unity     5        965/32000  1        m=31207 sd=17913 got=1534 ref=1535
     round-then-shift (PSHR32)              5        16023/32000 1        m=-16206 sd=120 got=-510 ref=-511
     saturate-then-shift (EE.VSUBS.S16 first) 5        7975/32000 1021     m=-32733 sd=32682 got=-1024 ref=-2045
     gains = Q31ONE (2^31-1), not unity     8        125/32000  1        m=30424 sd=24104 got=212 ref=213
     round-then-shift (PSHR32)              8        16049/32000 1        m=-11790 sd=-22199 got=41 ref=40
     saturate-then-shift (EE.VSUBS.S16 first) 8        8045/32000 127      m=-32461 sd=-32722 got=-128 ref=-255
     gains = Q31ONE (2^31-1), not unity     11       17/32000   1        m=-3328 sd=-23808 got=9 ref=10
     round-then-shift (PSHR32)              11       16013/32000 1        m=29520 sd=-1427 got=14 ref=13
     saturate-then-shift (EE.VSUBS.S16 first) 11       8040/32000 16       m=-32105 sd=32488 got=-16 ref=-32
     gains = Q31ONE (2^31-1), not unity     16       2/32000    1        m=31196 sd=31196 got=-1 ref=0
     round-then-shift (PSHR32)              16       16083/32000 1        m=28793 sd=-4337 got=1 ref=0
     saturate-then-shift (EE.VSUBS.S16 first) 16       0/32000    0        -

T2b clamp-after-shift (EE.VMIN/VMAX.S16 on the exact shifted value) vs the C's narrowing store:
     shift=0   8069/32000 differ, max|d| = 65535   (2^16 per wrap: the C keeps the low 16 bits)
     shift=1   0/32000 differ, max|d| = 0
     shift=2   0/32000 differ, max|d| = 0
     shift=5   0/32000 differ, max|d| = 0
     shift=16  0/32000 differ, max|d| = 0

T3  ex18_comb_filter (ONE shift at the readout, 16-bit lanes) vs celt.c:166-193 (one truncation
    PER TAP, 17-bit sums, clamp at SIG_SAT = 2^29-1 which never fires on this data):
     tapset  n    samples  differ   |d|<=3    |d|>3     max|d|   C value outside int16
     0       8    320      269      230       39        18239    39
     0       16   640      553      483       70        21177    70
     0       24   960      812      697       115       24761    115
     0       64   2560     2170     1899      271       24378    271
     1       8    320      204      141       63        23759    63
     1       16   640      394      281       113       24834    113
     1       24   960      568      422       146       20390    146
     1       64   2560     1417     1063      354       23483    354
     2       8    320      219      156       63        24668    63
     2       16   640      423      291       132       26696    132
     2       24   960      629      440       189       24110    189
     2       64   2560     1677     1146      531       30737    531
     |d| <= 3 is the shift distribution: CELT floors three times (once per tap) and this kernel
     floors once, so the two differ by the rounding residue of the shift, bounded by the tap
     count. |d| > 3 is the 16-bit lane: the C's tap sum needs 17 bits (its own output reaches
     x + 52297 = 85065 for full-scale input) and this kernel clamps at SRCMB's readout and at
     the +x and -1 adds. The .md counts both.

T3b the decomposed MULT16_32_Q15 (fixed_generic.h:57, the branch xtensa takes) vs the int64 form
    floor(a*b/2^15): 200000 (a,b) pairs, 0 disagree  [this is what licenses 'one floor per tap']
T4b the C-exact form (per-tap floor, 17-bit sums, SIG_SAT clamp) written from the DECOMPOSED
    macro, vs the reference: 6400 values, 0 disagree  [0 is what says the reference is faithful]

T4  the per-call gain multiply celt.c:267-272 with g0 = g1 = Q15ONE, MULT16_16_P15(Q15ONE, g):
     tapset 0  table (10048, 7112, 4248)    -> C (10048, 7112, 4248)    same
     tapset 1  table (15200, 8784, 0)       -> C (15200, 8784, 0)       same
     tapset 2  table (26208, 3280, 0)       -> C (26207, 3280, 0)       DIFFERS by -1 on tap 0

T5  the missing taps at the head of the history (n = 16, hist[-2] = hist[-1] = 0, 3 tapsets):
     zero-filled history == the C reading the same memory: by construction (the C reads memory)
     tapset 0: 'skip the missing taps' differs on 2 of 16 samples, max|d| = 1387, |d| at i=0,1 = [599, 1387]
     tapset 1: 'skip the missing taps' differs on 1 of 16 samples, max|d| = 4648, |d| at i=0,1 = [4648, 0]
     tapset 2: 'skip the missing taps' differs on 1 of 16 samples, max|d| = 2685, |d| at i=0,1 = [2685, 0]

T6  ex18_intensity_stereo (16-bit lane saturating add) vs bands.c:398-402 (32-bit ADD32):
     a1       a2       samples   differ   where the lane clamps
     23170    23170    1600      154/1600 154
     32767    0        1600      0/1600   0
     0        32767    1600      0/1600   0
     30000    30000    1600      323/1600 323
     20000    26000    1600      136/1600 136
     32767    32767    1600      393/1600 393

T7  layer-1 calls, from the model (the loop-body figure is the difference between an 8- and
    a 16-sample call, so the prologue drops out; the objdump's loop body is the same count):
     stereo_ms shift=0 (loop body) 19
     stereo_ms shift=0 (whole call, 8 samples) 27
     stereo_ms shift=3 (loop body) 16
     stereo_ms shift=3 (whole call, 8 samples) 24
     comb_filter (loop body) 26
     comb_filter (whole call, 8 samples) 34
     intensity_stereo (whole call, 8 samples) 14
     intensity_stereo (loop body) 6

Python model vs the CELT C reference, in Python:
     T1  stereo_ms vs bands.c:458-466        12665 groups, 0 bad
     T1b the half-value identity, exhaustive 8388608 triples, 0 bad
     T2  the four disagreeing forms, measured       (see the table)
     T3b MULT16_32_Q15 decomposed vs int64           200000 pairs, 0 bad
     T4b the C-exact form vs the reference           6400 values, 0 bad
     T3  comb_filter vs celt.c:166-193       13440 samples, 9335 differ (|d|<=3 shift distribution,
         |d|>3 = the 16-bit lane clamp: see the table)
     T5  the 'skip missing taps' rule vs the C  4 of 48 affected samples (see the table)
     T6  intensity_stereo vs bands.c:398-402 9600 samples, 1006 bad (the C's 32-bit sum does not
         fit a 16-bit lane)

first mismatches:
     T6_first   got -32768 want 28463 at 2 (a1=23170 a2=23170) exact=-37073

layer-1 instruction calls over the whole sweep (the .md's cost table source):
     EE.ANDQ                38013
     EE.LD.128.USAR.IP      16830
     EE.MOVI.32.Q           113860
     EE.SRC.Q               8415
     EE.SRCMB.S16.QACC      1683
     EE.VADDS.S16           29724
     EE.VLD.128.IP          29431
     EE.VMUL.S16            53090
     EE.VSMULAS.S16.QACC    8415
     EE.VST.128.IP          28228
     EE.VSUBS.S16           39696
     EE.XORQ                748
     EE.ZERO.Q              964
     EE.ZERO.QACC           1683

case dump for the C verifier: /tmp/ex18_cases.txt (13877 lines)
```

## 7. ホスト C 検証器（実物）と実行結果

`/tmp/ex18_cases.txt`（モデルが書いたケースダンプ、13,877 行）を読み、**CELT のマクロを
`celt/fixed_generic.h` から逐語で持ってきた C 側の契約**で全ケースを再計算して比較します。
カーネルの 2 つ目の実装ではありません。A/B/S/C の 4 種類の行があり、B/S 行の参照値は
**32bit のままダンプ**してあるので（`hexof32`）、比較が「狭めた値」に対するものにならないようにしています。

```c
VERIFY-SOURCE-BEGIN
/* ex18_stereo_comb -- the host-side (gcc) half of the equivalence run.
 *
 * Reads the case dump written by the Python model (/tmp/ex18_cases.txt) and re-derives every case
 * the CELT way, from macros copied out of the opus 1.6.1 tree this example was written against
 * (/workspace/cardputer-adv-pocketjs/.cache/codecs/opus-1.6.1):
 *
 *   celt/fixed_generic.h:55-57  MULT16_32_Q15, both branches (OPUS_FAST_INT64 on/off)
 *   celt/fixed_generic.h:69-71  MULT32_32_Q31
 *   celt/fixed_generic.h:118-125 SHR32 / PSHR32 / VSHR32 / SATURATE
 *   celt/fixed_generic.h:197    MULT16_16_P15
 *   celt/arch.h:110-111,125-128,155,193-199,204-215  ADD32/SUB32, which branch this build takes,
 *                               celt_norm's type, celt_coef, SIG_SAT
 *   celt/bands.c:458-466        stereo_merge's inner loop
 *   celt/bands.c:398-402        intensity_stereo's inner loop
 *   celt/celt.c:166-193         comb_filter_const_c (non-ARM, non-QEXT)
 *   celt/celt.c:246-249,265-272 the comb filter's gain table and its per-call gain multiply
 *
 * It is deliberately NOT a second copy of the PIE kernels: it is the C contract, and the only thing
 * it shares with the model is the case dump's byte layout. The three kernel models' outputs are in
 * the dump as `got`; the Python model's own reading of the C is in the dump as `want`. So each line
 * tests two things at once:
 *
 *   want vs this file   the Python model's reading of the C == the C        (must be 0)
 *   got  vs this file   the PIE kernel's arithmetic      vs the C          (measured)
 *
 * Build and run:
 *   gcc -O2 -o /tmp/ex18_verify /tmp/ex18_verify.c && /tmp/ex18_verify /tmp/ex18_cases.txt
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ the opus macros, verbatim */
typedef int16_t opus_val16;
typedef int32_t opus_val32;
typedef uint32_t opus_uint32;
typedef uint16_t opus_uint16;

#define QCONST16(x, bits) ((opus_val16)(.5 + (x) * (((opus_val32)1) << (bits))))
#define Q15ONE 32767
#define Q31ONE 2147483647
#define SIG_SAT (536870911)
#define SHR32(a, shift) ((a) >> (shift))
#define PSHR32(a, shift) (SHR32((a) + ((opus_val32)((opus_val32)1 << (shift)) >> 1), shift))
#define VSHR32(a, shift) (((shift) > 0) ? SHR32(a, shift) : ((a) << -(shift)))
#define MULT16_16(a, b) (((opus_val32)(opus_val16)(a)) * ((opus_val32)(opus_val16)(b)))
#define MULT16_16SU(a, b) ((opus_val32)(opus_val16)(a) * (opus_val32)(opus_uint16)(b))
#define MULT16_16_P15(a, b) (SHR(ADD32(16384, MULT16_16((a), (b))), 15))
#define ADD32(a, b) ((opus_val32)(a) + (opus_val32)(b))
#define SUB32(a, b) ((opus_val32)(a) - (opus_val32)(b))
#define SATURATE(x, a) (((x) > (a)) ? (a) : (((x) < -(a)) ? -(a) : (x)))
#define SHR(a, b) ((a) >> (b))
#if defined(__x86_64__) || defined(__LP64__) || defined(_WIN64) || defined(__mips)
#define OPUS_FAST_INT64 1
#else
#define OPUS_FAST_INT64 0
#endif

/* fixed_generic.h:55-57: the two branches. On the xtensa build OPUS_FAST_INT64 is 0 (arch.h:125),
 * i.e. the decomposed form is what runs. Both are computed here so the claim "they agree" can be
 * tested rather than assumed. */
static opus_val32 mult16_32_q15_dec(opus_val16 a, opus_val32 b)
{
    return ADD32((opus_val32)(MULT16_16(a, SHR(b, 16)) << 1),
                 (opus_val32)(MULT16_16SU(a, ((b) & 0x0000ffff)) >> 15));
}

static opus_val32 mult16_32_q15_i64(opus_val16 a, opus_val32 b)
{
    return (opus_val32)(((int64_t)a * (int64_t)b) >> 15);
}

/* fixed_generic.h:69-71 */
static opus_val32 mult32_32_q31(opus_val32 a, opus_val32 b)
{
    return (opus_val32)(((int64_t)a * (int64_t)b) >> 31);
}

/* ------------------------------------------------------------------ bands.c:458-466 (stereo_merge)
 * With the unit gain the C computes l = MULT32_32_Q31(2^31, X[j]) = X[j] exactly, and the two gains
 * are 2^31: lgain = rgain = 2^31. 2^31 is not representable in Q31, which is why the Q31ONE variant
 * below exists (the C's own largest gain) and differs by one LSB on positive values. */
static int16_t ref_stereo_ms_l(int16_t mid, int16_t side, int shift, int q31one)
{
    opus_val32 l, r, d, g;
    if (q31one) {
        l = mult32_32_q31(Q31ONE, mid);                     /* MULT32_32_Q31(mid, X[j]) */
        d = SUB32(l, side);                                 /* SUB32(l, r) */
        g = mult32_32_q31(Q31ONE, d);                       /* MULT32_32_Q31(lgain, ...) */
    } else {
        l = mid;                                            /* the unit normalisation */
        d = SUB32(l, side);
        g = d;                                              /* the unit gain */
    }
    return (int16_t)VSHR32(g, shift);                        /* + the narrowing store into celt_norm */
}

static int16_t ref_stereo_ms_r(int16_t mid, int16_t side, int shift, int q31one)
{
    opus_val32 l, r, d, g;
    if (q31one) {
        l = mult32_32_q31(Q31ONE, mid);
        d = ADD32(l, side);                                 /* ADD32(l, r) */
        g = mult32_32_q31(Q31ONE, d);
    } else {
        l = mid;
        d = ADD32(l, side);
        g = d;
    }
    return (int16_t)VSHR32(g, shift);
}

/* ------------------------------------------------------------------ bands.c:398-402 (intensity_stereo) */
static opus_val32 ref_intensity(int16_t X, int16_t Y, opus_val16 a1, opus_val16 a2)
{
    return ADD32(mult16_32_q15_dec(a1, X), mult16_32_q15_dec(a2, Y));
}

/* --------------------------------------------------------- celt.c:166-193 (comb_filter_const_c) */
static const opus_val16 gains[3][3] = {
    {QCONST16(0.3066406250f, 15), QCONST16(0.2170410156f, 15), QCONST16(0.1296386719f, 15)},
    {QCONST16(0.4638671875f, 15), QCONST16(0.2680664062f, 15), QCONST16(0.f, 15)},
    {QCONST16(0.7998046875f, 15), QCONST16(0.1000976562f, 15), QCONST16(0.f, 15)}};

static void ref_comb_gains(int tapset, opus_val16 g[3], opus_val16 post_gain)
{
    /* celt.c:267-272 with g0 == g1 == the postfilter gain */
    int k;
    for (k = 0; k < 3; k++)
        g[k] = MULT16_16_P15(post_gain, gains[tapset][k]);
}

/* h[0] is hist[-2], so hist[j] == h[j+2]. Returns the C's 32-bit value before any int16 narrowing
 * (the C's own y[] is opus_val32 and its clamp is at SIG_SAT, which never fires on this data). */
static opus_val32 ref_comb(const int16_t *x, const int16_t *h, int i, int tapset, opus_val16 g[3])
{
    opus_val32 t;
    t = x[i];
    t = ADD32(t, mult16_32_q15_dec(g[0], h[2 + i]));
    t = ADD32(t, mult16_32_q15_dec(g[1], ADD32(h[2 + i + 1], h[2 + i - 1])));
    t = ADD32(t, mult16_32_q15_dec(g[2], ADD32(h[2 + i + 2], h[2 + i - 2])));
    t = SUB32(t, 1);                                        /* celt.c:184 */
    return SATURATE(t, SIG_SAT);                            /* celt.c:186 */
}

/* ------------------------------------------------------------------ dump parsing */
static char line[65536];
static unsigned char hbuf[65536];

static int n32(const char *hexfield, int32_t *out, int max)
{
    size_t len = strlen(hexfield), i;
    if (len == 0) return 0;
    if (len % 2 || (len / 2) % 4) return -1;
    for (i = 0; i < len / 8; i++) {
        unsigned v;
        int32_t w = 0;
        int b;
        if ((int)i >= max) return -1;
        for (b = 0; b < 4; b++) {
            if (sscanf(hexfield + 8 * i + 2 * b, "%2x", &v) != 1) return -1;
            w |= (int32_t)((unsigned)v << (8 * b));
        }
        out[i] = w;
    }
    return (int)(len / 8);
}

static int n16(const char *hexfield, int16_t *out, int max)
{
    size_t len = strlen(hexfield), i;
    if (len == 0) return 0;
    if (len % 2 || (int)(len / 2) % 2) return -1;
    for (i = 0; i < len / 2; i++) {
        unsigned v;
        if (sscanf(hexfield + 2 * i, "%2x", &v) != 1) return -1;
        if ((int)(i / 2) >= max) return -1;
        if (i % 2 == 0) out[i / 2] = (int16_t)(unsigned short)v;
        else out[i / 2] |= (int16_t)((unsigned short)v << 8);
    }
    return (int)(len / 4);
}

#define NEXT() do { if (!fgets(line, sizeof line, f)) return 3; \
                    line[strcspn(line, "\r\n")] = 0; } while (0)

int main(int argc, char **argv)
{
    FILE *f = fopen(argc > 1 ? argv[1] : "/tmp/ex18_cases.txt", "r");
    char tag[8], f1[262144], f2[262144], f3[262144], f4[262144];
    long a_cases = 0, a_vals = 0, a_bad = 0, a_bad_q31one = 0, a_maxd = 0;
    long b_cases = 0, b_vals = 0, b_refbad = 0, b_bad = 0, b_small = 0, b_large = 0, b_maxd = 0;
    long b_oor = 0, b_clamp = 0, b_wrapmatch = 0, b_noclamp = 0;
    long s_cases = 0, s_vals = 0, s_bad = 0, s_maxd = 0, s_firsti = -1;
    long c_cases = 0, c_vals = 0, c_bad = 0, c_clamp = 0, c_wrap = 0, c_maxd = 0;
    char first[512] = "";
    int16_t mid[4096], side[4096], ol[4096], orr[4096], xx[4096], hh[8192], oo[4096], ww[4096];
    int16_t xx2[4096], yy2[4096], oi[4096];
    int32_t ww32[4096], oo32[4096];

    if (!f) { perror("open"); return 2; }

    while (fgets(line, sizeof line, f)) {
        int n, i;
        long shift, tapset, a1, a2;
        int nm, ns, nl, nr;
        line[strcspn(line, "\r\n")] = 0;
        if (strlen(line) < 2) continue;
        if (sscanf(line, "%7s", tag) != 1) continue;

        if (tag[0] == 'A') {
            if (sscanf(line, "%*s %d %ld %262143s %262143s %262143s %262143s",
                       &n, &shift, f1, f2, f3, f4) != 6) return 3;
            nm = n16(f1, mid, 4096); ns = n16(f2, side, 4096);
            nl = n16(f3, ol, 4096);   nr = n16(f4, orr, 4096);
            if (nm != n || ns != n || nl != n || nr != n) { fprintf(stderr, "A size\n"); return 3; }
            for (i = 0; i < n; i++) {
                int16_t cl = ref_stereo_ms_l(mid[i], side[i], (int)shift, 0);
                int16_t cr = ref_stereo_ms_r(mid[i], side[i], (int)shift, 0);
                int16_t ql = ref_stereo_ms_l(mid[i], side[i], (int)shift, 1);
                int16_t qr = ref_stereo_ms_r(mid[i], side[i], (int)shift, 1);
                a_vals += 2;
                if (ol[i] != cl || orr[i] != cr) {
                    a_bad++;
                    a_maxd = (a_maxd > labs((long)ol[i] - cl)) ? a_maxd : labs((long)ol[i] - cl);
                    if (!first[0])
                        snprintf(first, sizeof first,
                                 "A shift=%ld mid=%d side=%d: model l=%d r=%d, C l=%d r=%d",
                                 shift, mid[i], side[i], ol[i], orr[i], cl, cr);
                }
                if (ol[i] != ql || orr[i] != qr) a_bad_q31one++;
            }
            a_cases++;
        } else if (tag[0] == 'B' || tag[0] == 'S') {
            if (sscanf(line, "%*s %d %ld %ld %262143s %262143s %262143s %262143s",
                       &n, &tapset, &shift, f1, f2, f3, f4) != 7) return 3;
            {
                opus_val16 g[3];
                int hist_n;
                nm = n16(f1, xx, 4096);
                hist_n = n16(f2, hh, 8192);
                nl = (tag[0] == 'S') ? n32(f3, oo32, 4096) : n16(f3, oo, 4096);
                nr = n32(f4, ww32, 4096);
                if (nm != n || nl != n || nr != n || hist_n < n + 4) {
                    fprintf(stderr, "%s size (n=%d hist=%d)\n", tag, n, hist_n);
                    return 3;
                }
                ref_comb_gains((int)tapset, g, Q15ONE);
                for (i = 0; i < n; i++) {
                    opus_val32 cv = ref_comb(xx, hh, i, (int)tapset, g);
                    int16_t narrowed = (int16_t)cv;            /* what an int16 store would keep */
                    int16_t clamped = cv > 32767 ? 32767 : (cv < -32768 ? -32768 : (int16_t)cv);
                    long d2 = labs((long)ww32[i] - (long)cv);  /* want vs the C */
                    long d = labs((long)oo[i] - (long)cv);     /* got  vs the C */
                    if (tag[0] == 'B') {
                        b_vals++;
                        if (d2) { b_refbad++; if (!first[0]) snprintf(first, sizeof first,
                            "B ref tapset=%ld i=%d python=%d C=%ld", tapset, i, ww[i], (long)cv); }
                        if (d) { b_bad++; if (d <= 3) b_small++; else b_large++;
                                 if (b_maxd < d) b_maxd = d; }
                        if (cv > 32767 || cv < -32768) {
                            b_oor++;
                            if (oo[i] == clamped) b_clamp++;
                            else if (oo[i] == narrowed) b_wrapmatch++;
                            else b_noclamp++;
                        }
                    } else {
                        long d32 = labs((long)oo32[i] - (long)cv);   /* both sides at 32 bits */
                        s_vals++;
                        if (d32) { s_bad++; if (s_firsti < 0) s_firsti = i;
                                   if (s_maxd < d32) s_maxd = d32; }
                    }
                }
                if (tag[0] == 'B') b_cases++; else s_cases++;
            }
        } else if (tag[0] == 'C') {
            if (sscanf(line, "%*s %d %ld %ld %262143s %262143s %262143s",
                       &n, &a1, &a2, f1, f2, f3) != 6) return 3;
            nm = n16(f1, xx2, 4096); ns = n16(f2, yy2, 4096); nl = n16(f3, oi, 4096);
            if (nm != n || ns != n || nl != n) { fprintf(stderr, "C size\n"); return 3; }
            for (i = 0; i < n; i++) {
                opus_val32 cv = ref_intensity(xx2[i], yy2[i], (opus_val16)a1, (opus_val16)a2);
                long d = labs((long)oi[i] - (long)cv);
                c_vals++;
                if (d) c_bad++;
                if (cv > 32767 || cv < -32768) c_clamp++;
                if ((int16_t)cv == oi[i]) c_wrap++;
                if (c_maxd < d) c_maxd = d;
            }
            c_cases++;
        } else {
            fprintf(stderr, "unknown tag %s\n", tag);
            return 3;
        }
    }
    fclose(f);

    /* ---- prelude: the three facts every other number in this file rests on ---------------- */
    printf("prelude 1 -- the per-call gain multiply, MULT16_16_P15(Q15ONE, gains[tapset][k]):\n");
    for (int tt = 0; tt < 3; tt++)
        for (int kk = 0; kk < 3; kk++)
            printf("   tapset %d tap %d: table %5d -> C %5d%s\n", tt, kk, gains[tt][kk],
                   MULT16_16_P15(Q15ONE, gains[tt][kk]),
                   MULT16_16_P15(Q15ONE, gains[tt][kk]) == gains[tt][kk] ? "" : "   <-- one LSB below");
    printf("prelude 2 -- the two MULT16_32_Q15 branches over every 16-bit a and a 17-bit b range:\n");
    {
        long checked = 0, bad = 0;
        for (int a = -32768; a <= 32767; a += 7)
            for (int b = -65536; b <= 65536; b += 13) {
                checked++;
                if (mult16_32_q15_dec((opus_val16)a, (opus_val32)b) != mult16_32_q15_i64((opus_val16)a, (opus_val32)b))
                    bad++;
            }
        printf("   %ld (a,b) pairs, %ld disagree  (so 'one floor per tap' is the value on xtensa too)\n",
               checked, bad);
    }
    printf("prelude 3 -- the int16 narrowing the C's store performs when the value does not fit:\n");
    printf("   (int16_t)85065 = %d, (int16_t)(-85065) = %d, (int16_t)32767 = %d  (gcc: the low 16 bits)\n",
           (int16_t)85065, (int16_t)(-85065), (int16_t)32767);
    printf("   the C's own SIG_SAT = %d, i.e. the clamp at celt.c:186 cannot fire on 16-bit input\n\n",
           SIG_SAT);

    printf("C verifier -- the CELT macros and the three functions, host gcc -O2\n");
    printf("(MULT16_32_Q15 = the DECOMPOSED fixed_generic.h:57 branch, i.e. the one xtensa takes:\n");
    printf(" OPUS_FAST_INT64 is %d on this host, so both branches exist here)\n", OPUS_FAST_INT64);
    printf("\nA  ex18_stereo_ms vs bands.c:458-466 with unit gains\n");
    printf("   cases %ld, values compared %ld, model != C: %ld, max|d| %ld\n",
           a_cases, a_vals, a_bad, a_maxd);
    printf("   (the same model vs the C with lgain = rgain = Q31ONE instead of unity: %ld of %ld values differ)\n",
           a_bad_q31one, a_vals);
    printf("\nB  ex18_comb_filter vs celt.c:166-193 (QP15 gains, g = Q15ONE)\n");
    printf("   cases %ld, values compared %ld\n", b_cases, b_vals);
    printf("   the model's own reading of the C (`want`) != this C: %ld   <- must be 0\n", b_refbad);
    printf("   the PIE kernel (`got`) != this C            : %ld  (|d| <= 3: %ld, |d| > 3: %ld, max|d| %ld)\n",
           b_bad, b_small, b_large, b_maxd);
    printf("   the C's own value is outside int16 in          : %ld samples\n", b_oor);
    printf("   there the kernel produced the CLAMPED value   : %ld  (an int16 narrowing store would have\n",
           b_clamp);
    printf("     produced the WRAPPED one, which the kernel did on %ld, neither on %ld)\n",
           b_wrapmatch, b_noclamp);
    printf("\nS  the 'skip the taps that fall off the head' variant vs the same C\n");
    printf("   (both sides at 32 bits here: the variant is a rule, not a 16-bit kernel)\n");
    printf("   cases %ld, values compared %ld, differ: %ld, max|d| %ld, first at i=%ld\n",
           s_cases, s_vals, s_bad, s_maxd, s_firsti);
    printf("\nC  ex18_intensity_stereo vs bands.c:398-402\n");
    printf("   cases %ld, values compared %ld, kernel != C: %ld, max|d| %ld\n",
           c_cases, c_vals, c_bad, c_maxd);
    printf("   the C's 32-bit sum is outside int16 in %ld of those; the kernel saturates there\n", c_clamp);
    printf("   (an int16 wrapping store would have agreed with the C on %ld of them instead)\n", c_wrap);
    printf("\nfirst mismatch: %s\n", first[0] ? first : "none");
    return 0;
}
```

```text
$ gcc -O2 -o /tmp/ex18_verify /tmp/ex18_verify.c && /tmp/ex18_verify /tmp/ex18_cases.txt
prelude 1 -- the per-call gain multiply, MULT16_16_P15(Q15ONE, gains[tapset][k]):
   tapset 0 tap 0: table 10048 -> C 10048
   tapset 0 tap 1: table  7112 -> C  7112
   tapset 0 tap 2: table  4248 -> C  4248
   tapset 1 tap 0: table 15200 -> C 15200
   tapset 1 tap 1: table  8784 -> C  8784
   tapset 1 tap 2: table     0 -> C     0
   tapset 2 tap 0: table 26208 -> C 26207   <-- one LSB below
   tapset 2 tap 1: table  3280 -> C  3280
   tapset 2 tap 2: table     0 -> C     0
prelude 2 -- the two MULT16_32_Q15 branches over every 16-bit a and a 17-bit b range:
   94407129 (a,b) pairs, 0 disagree  (so 'one floor per tap' is the value on xtensa too)
prelude 3 -- the int16 narrowing the C's store performs when the value does not fit:
   (int16_t)85065 = 19529, (int16_t)(-85065) = -19529, (int16_t)32767 = 32767  (gcc: the low 16 bits)
   the C's own SIG_SAT = 536870911, i.e. the clamp at celt.c:186 cannot fire on 16-bit input

C verifier -- the CELT macros and the three functions, host gcc -O2
(MULT16_32_Q15 = the DECOMPOSED fixed_generic.h:57 branch, i.e. the one xtensa takes:
 OPUS_FAST_INT64 is 1 on this host, so both branches exist here)

A  ex18_stereo_ms vs bands.c:458-466 with unit gains
   cases 12665, values compared 202640, model != C: 0, max|d| 0
   (the same model vs the C with lgain = rgain = Q31ONE instead of unity: 17135 of 202640 values differ)

B  ex18_comb_filter vs celt.c:166-193 (QP15 gains, g = Q15ONE)
   cases 9, values compared 144
   the model's own reading of the C (`want`) != this C: 0   <- must be 0
   the PIE kernel (`got`) != this C            : 105  (|d| <= 3: 84, |d| > 3: 21, max|d| 15185)
   the C's own value is outside int16 in          : 21 samples
   there the kernel produced the CLAMPED value   : 11  (an int16 narrowing store would have
     produced the WRAPPED one, which the kernel did on 0, neither on 10)

S  the 'skip the taps that fall off the head' variant vs the same C
   (both sides at 32 bits here: the variant is a rule, not a 16-bit kernel)
   cases 3, values compared 48, differ: 4, max|d| 4648, first at i=0

C  ex18_intensity_stereo vs bands.c:398-402
   cases 1200, values compared 9600, kernel != C: 1006, max|d| 31414
   the C's 32-bit sum is outside int16 in 1006 of those; the kernel saturates there
   (an int16 wrapping store would have agreed with the C on 8594 of them instead)

first mismatch: none
```

## 8. `piesim.py` の解釈実行

`/workspace/pjs-vm/tools/pie/piesim.py` を `/tmp/pie/` にコピーし、**足りない 3 命令だけを TRM の
Operation 疑似コードから追加**して（`ee.ld.128.usar.ip` p93、`ee.src.q` p125、`ee.vsmulas.s16.qacc` p269。
追加箇所は `# ADDED` で明示）、`.S` から抽出したループ本体を実行します。

- **実行するのはループ本体だけ**です。`movi` / `slli` / `or` は piesim がモデル化していないベース ISA なので、
  プロローグが作るレジスタ（`q0` = 2^14、`q1` = 1、`q5` = ゲイン 3 本、`q6` = 1、`SAR`）は**モデルの
  同じコードで**用意しました（1.3 と 6.2 の「レーン定数の作り方」は C 検証器とモデルで確認済み）。
- piesim はコメントを**行単位**でしか落とさないので、複数行コメントはブロックで除去しています。
  分岐は pjs-vm のカーネル自身の書き方（`1:` と `bnez aN, 1b`）に合わせています。**綴りの違いだけ**で、
  ニーモニックとオペランドは抽出したままです。

```python
PIESIM-SOURCE-BEGIN
#!/usr/bin/env python3
"""ex18 -- interpretive run of the three kernels' LOOP BODIES through piesim.py (TRM-conformant
instruction model, /workspace/pjs-vm/tools/pie/piesim.py, copied to /tmp with four additions).

What this checks and what it does not:
  * it executes the .S's loop body text, instruction by instruction, against the same lane semantics
    the TRM pseudo-code gives (piesim's own reason to exist: "it proves that the assembly actually
    implements that arithmetic with the registers it claims");
  * it does NOT execute the prologue (movi / or / slli are base-ISA instructions piesim does not
    model). The q registers the prologue builds are set here by the model's own movi32_q code path,
    and the prologue itself is covered by the objdump + /tmp/ex18_words.py field decode instead;
  * three PIE instructions the comb filter needs are NOT in piesim's supported list
    (ee.ld.128.usar.ip, ee.src.q, ee.vsmulas.s16.qacc). They are added below, each written from the
    Operation pseudo-code in data/pie_instructions.json with its TRM page, and marked ADDED. That
    makes the comb filter's run a check of MY reading of those three, not of silicon.

  python3 /tmp/ex18_piesim.py
"""
import re
import sys

sys.path.insert(0, "/tmp/pie")

SRC = "/workspace/esp32s3-hw-mcp/examples/firmware/main/proposed/ex18_stereo_comb.S"


def load_sim():
    """Copy piesim.py's Sim class and add what ex18 needs. The additions are textually inserted so
    the diff against the original tool is visible."""
    text = open("/tmp/pie/piesim.py").read()
    add = '''
            elif op == 'ee.ld.128.usar.ip':                     # ADDED -- TRM p93 (1.8.x)
                # 1 qu[127:0] = load128({as[31:4],4{0}})  2 SAR_BYTE = as[3:0]  3 as += imm
                Q[qi(a[0])] = self.ldq(arv(a[1]))
                self.sar_byte = arv(a[1]) & 15
                arset(a[1], arv(a[1]) + int(a[2]))
            elif op == 'ee.src.q':                              # ADDED -- TRM p125
                # qa[127:0] = {qs1[127:0], qs0[127:0]} >> (SAR_BYTE[3:0] << 3)
                lo, hi = Q[qi(a[1])], Q[qi(a[2])]
                v = 0
                for i in reversed(range(8)):
                    v = (v << 16) | hi[i]                       # qs1 first: it ends up HIGH
                for i in reversed(range(8)):
                    v = (v << 16) | lo[i]                       # then qs0 = the LOW half (operand 1)
                v >>= 8 * self.sar_byte
                Q[qi(a[0])] = [(v >> (16 * i)) & 0xFFFF for i in range(8)]
            elif op == 'ee.vsmulas.s16.qacc':                   # ADDED -- TRM p269
                # temp[15:0] = qy[sel8*16+15 : sel8*16];  QACC_lane[i] += qx[i] * temp  (sat +-2^39)
                x, y = Q[qi(a[0])], Q[qi(a[1])]     # qx, qy, then the 8-bit lane selector
                t = s16(y[int(a[2])])
                self.qacc = [self.sat40(self.qacc[i] + s16(x[i]) * t) for i in range(8)]
'''
    text = text.replace("            else:\n                raise NotImplementedError(op)",
                        add + "            else:\n                raise NotImplementedError(op)")
    text = text.replace('''    def ld16(self, a):''','''    def sat40(self, v):
        return max(-(1 << 39), min((1 << 39) - 1, v))

    def ld16(self, a):''')
    text = text.replace("        self.sar = 0\n", "        self.sar = 0\n        self.sar_byte = 0\n")
    # the copy lives in /tmp: make it importable without touching the original tool
    text = text.replace("import re", "import re", 1)
    open("/tmp/pie/piesim_ex18.py", "w").write(text)
    import importlib.util
    spec = importlib.util.spec_from_file_location("piesim_ex18", "/tmp/pie/piesim_ex18.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def loop_body(path, label):
    """The text of one loop body, spelled the way piesim.py expects.

    Two adaptations, both spelling only:
      * piesim strips comments LINE BY LINE (`raw.split('/*')[0]`), so a /* ... */ that spans several
        lines leaks its continuation lines into the instruction stream. The comments are removed here
        as blocks instead.
      * piesim's branch handling does `labels[a[1][:-1]]` (the last character of the operand is
        dropped) and skips any line that ends with ':' as a label, so the natural spelling is the one
        the pjs-vm kernels themselves use: a numeric label `1:` at the top and `bnez aN, 1b` at the
        bottom. The .S's own branch targets are symbolic; that is a naming difference, not a semantic
        one, and the extracted mnemonics/operands are otherwise verbatim.
    """
    text = open(path).read()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)          # multi-line comments, in blocks
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith(label + ":"))
    out = []
    for l in lines[start + 1:]:
        out.append(l)
        if re.match(r"\s*bnez\s+a\d+,\s*%s\s*$" % re.escape(label), l.strip()):
            break
    else:
        raise SystemExit("no closing branch for %s" % label)
    body = "\n".join(x.strip() for x in out if x.strip())
    body = re.sub(r"bnez\s+(a\d+),\s*%s" % re.escape(label), r"bnez \1, 1b", body)
    return ("1:\n" + body).lower()


def mem_for(values, base=0x10000, pad=64):
    """A flat byte array; `base` is the address of values[0] and `pad` bytes of room are left before
    and after it."""
    m = bytearray(base + pad * 2 + 2 * len(values) + pad * 2)
    for i, v in enumerate(values):
        off = base + pad + 2 * i
        m[off] = v & 0xFF
        m[off + 1] = (v >> 8) & 0xFF
    return m, base + pad


def rd16(mem, addr, n):
    return [mem[addr + 2 * i] | (mem[addr + 2 * i + 1] << 8) for i in range(n)]


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


if __name__ == "__main__":
    pm = load_sim()
    sys.path.insert(0, "/tmp")
    src = open("/tmp/ex18_model.py").read().split("# ========================================================================================== the runs")[0] \
        if False else open("/tmp/ex18_model.py").read().split("# =========================================================================================== the runs")[0]
    g = {}
    exec(compile(src, "model_head", "exec"), g)
    import random
    random.seed(0x18)

    print("ex18 -- piesim.py (TRM pseudo-code) against the Python model, loop bodies only")
    print()

    # ------------------------------------------------------------------ A: ex18_stereo_ms
    n = 16
    mids = [random.randint(-32768, 32767) for _ in range(n)]
    sides = [random.randint(-32768, 32767) for _ in range(n)]
    asm = loop_body(SRC, ".Lms_loop")
    for shift, label in ((3, ".Lms_loop"), (0, ".Lms_shift0")):
        asm = loop_body(SRC, label)
        mm, ma = mem_for(mids)
        sm, sa = mem_for(sides)
        lm, la = mem_for([0] * n)
        rm, ra = mem_for([0] * n)
        sim = pm.Sim(mm)
        sim.mem = mm
        sim.q[0] = [0x4000] * 8                      # the prologue's constant (see the .md)
        sim.q[1] = [1] * 8
        sim.mem_l, sim.mem_s, sim.mem_r, sim.mem_rm = mm, sm, lm, rm   # the kernel needs four arrays
        ar = {"a2": ma, "a3": sa, "a4": la, "a5": ra, "a6": n // 8, "a8": 15, "a9": shift + 13, "a10": 13}
        # piesim's Sim has one flat memory, so the four buffers share it: run the loop body with the
        # address registers pointing into that one array (offsets chosen so nothing overlaps)
        sim = pm.Sim(mm)
        base = 0x10000
        mm2 = bytearray(0x30000)
        for i, v in enumerate(mids):
            mm2[base + 2 * i] = v & 0xFF
            mm2[base + 2 * i + 1] = (v >> 8) & 0xFF
        for i, v in enumerate(sides):
            mm2[base + 0x1000 + 2 * i] = v & 0xFF
            mm2[base + 0x1000 + 2 * i + 1] = (v >> 8) & 0xFF
        sim.mem = mm2
        sim.q[0] = [0x4000] * 8
        sim.q[1] = [1] * 8
        ar = {"a2": base, "a3": base + 0x1000, "a4": base + 0x2000, "a5": base + 0x3000,
              "a6": n // 8, "a8": 15, "a9": shift + 13, "a10": 13}
        cnt = sim.run(asm, dict(ar))
        got_l = [s16(v) for v in rd16(mm2, base + 0x2000, n)]
        got_r = [s16(v) for v in rd16(mm2, base + 0x3000, n)]
        gm, gma = g["mk"](mids)
        gs, gsa = g["mk"](sides)
        gl, gla = g["mk"]([0] * n)
        gr, gra = g["mk"]([0] * n)
        g["ex18_stereo_ms"](gm, gs, gl, gr, gma, gsa, gla, gra, n, shift)
        mod_l = [g["s16"](v) for v in g["read16_at"](gl, gla, n)]
        mod_r = [g["s16"](v) for v in g["read16_at"](gr, gra, n)]
        ok = (got_l == mod_l and got_r == mod_r)
        print("A  ex18_stereo_ms shift=%d: %d instructions, piesim vs model: %s"
              % (shift, cnt, "MATCH" if ok else "MISMATCH"))
        if not ok:
            print("   piesim l:", got_l)
            print("   model  l:", mod_l)
            print("   piesim r:", got_r)
            print("   model  r:", mod_r)

    # ------------------------------------------------------------------ B: ex18_comb_filter
    n = 16
    x = [random.randint(-20000, 20000) for _ in range(n)]
    hist = [random.randint(-20000, 20000) for _ in range(n + 8)]
    asm = loop_body(SRC, ".Lcf_loop")
    base = 0x10000
    m2 = bytearray(0x40000)
    for i, v in enumerate(x):
        m2[base + 0x8000 + 2 * i] = v & 0xFF
        m2[base + 0x8000 + 2 * i + 1] = (v >> 8) & 0xFF
    hbase = base + 0x10000 + 4                      # hist[0] sits 4 bytes into the second buffer
    for i, v in enumerate(hist):
        off = hbase + 2 * i
        m2[off] = v & 0xFF
        m2[off + 1] = (v >> 8) & 0xFF
    sim = pm.Sim(m2)
    sim.q[4] = [0x0000] * 8                          # unused
    sim.q[5] = g["gain_reg"](0)                      # the prologue's gain register (lanes 0,1,2)
    sim.q[6] = g["zero_q"]()                         # placeholder, replaced below
    q6 = g["zero_q"]()
    for sel in range(4):
        q6 = g["movi32_q"](q6, 0x00010001, sel)
    sim.q[6] = q6
    ar = {"a2": base, "a3": base + 0x8000, "a4": hbase, "a6": hbase + 16, "a5": n // 8, "a7": 15,
          "a8": hbase - 4, "a9": hbase - 2, "a10": hbase + 2, "a11": hbase + 4,
          "a12": hbase + 12, "a13": hbase + 14, "a14": hbase + 18, "a15": hbase + 20}
    cnt = sim.run(asm, dict(ar))
    got = [s16(v) for v in rd16(m2, base, n)]
    ym, ya = g["mk"]([0] * n)
    xm, xa = g["mk"](x)
    hm, ha = g["mk"]([0, 0] + hist)                  # element 2 is hist[0], matching hbase above
    g["ex18_comb_filter"](ym, xm, hm, ya, xa, ha + 4, n, 0, 15)
    mod = [s16(v) for v in g["read16_at"](ym, ya, n)]
    print("B  ex18_comb_filter tapset=0 shift=15: %d instructions, piesim vs model: %s"
          % (cnt, "MATCH" if got == mod else "MISMATCH"))
    if got != mod:
        print("   piesim:", got)
        print("   model :", mod)

    # ------------------------------------------------------------------ C: ex18_intensity_stereo
    n = 16
    xs = [random.randint(-32768, 32767) for _ in range(n)]
    ys = [random.randint(-32768, 32767) for _ in range(n)]
    asm = loop_body(SRC, ".Lis_loop")
    base = 0x10000
    m3 = bytearray(0x20000)
    for i, v in enumerate(xs):
        m3[base + 2 * i] = v & 0xFF
        m3[base + 2 * i + 1] = (v >> 8) & 0xFF
    for i, v in enumerate(ys):
        m3[base + 0x8000 + 2 * i] = v & 0xFF
        m3[base + 0x8000 + 2 * i + 1] = (v >> 8) & 0xFF
    sim = pm.Sim(m3)
    a1, a2 = 23170, 23170
    q0 = [0] * 8
    for sel in range(4):
        q0 = g["movi32_q"](q0, ((a1 & 0xFFFF) << 16) | (a1 & 0xFFFF), sel)
    q1 = [0] * 8
    for sel in range(4):
        q1 = g["movi32_q"](q1, ((a2 & 0xFFFF) << 16) | (a2 & 0xFFFF), sel)
    sim.q[0], sim.q[1] = q0, q1
    sim.sar = 15                                     # the prologue's wsr.sar a7 (not executed here)
    ar = {"a2": base, "a3": base + 0x8000, "a4": n // 8, "a5": a1, "a6": a2, "a7": 15}
    cnt = sim.run(asm, dict(ar))
    got = [s16(v) for v in rd16(m3, base, n)]
    xm, xa = g["mk"](xs)
    ym, ya = g["mk"](ys)
    g["ex18_intensity_stereo"](xm, ym, xa, ya, n, a1, a2)
    mod = [s16(v) for v in g["read16_at"](xm, xa, n)]
    print("C  ex18_intensity_stereo a1=a2=23170: %d instructions, piesim vs model: %s"
          % (cnt, "MATCH" if got == mod else "MISMATCH"))
    if got != mod:
        print("   piesim:", got)
        print("   model :", mod)
    print()
    print("The three ADDED instructions are mine (TRM p93 / p125 / p269 pseudo-code); everything else")
    print("in the loop bodies is piesim.py as shipped. The prologue is not executed here.")
```

```text
$ .venv/bin/python /tmp/ex18_piesim.py
ex18 -- piesim.py (TRM pseudo-code) against the Python model, loop bodies only

A  ex18_stereo_ms shift=3: 40 instructions, piesim vs model: MATCH
A  ex18_stereo_ms shift=0: 46 instructions, piesim vs model: MATCH
B  ex18_comb_filter tapset=0 shift=15: 56 instructions, piesim vs model: MATCH
C  ex18_intensity_stereo a1=a2=23170: 16 instructions, piesim vs model: MATCH

The three ADDED instructions are mine (TRM p93 / p125 / p269 pseudo-code); everything else
in the loop bodies is piesim.py as shipped. The prologue is not executed here.
```

## 9. 未確認の前提（全部）

実機で確かめていないものを、影響の大きい順に並べます。

1. **`EE.SRCMB.S16.QACC` / `EE.VSMULAS.S16.QACC` の読み書きの段**。TRM Table 1.7-2 は QACC を
   MAC の def 2(M)・SRCMB の use 1(E) と書いているので「MAC と SRCMB を 2 スロット離す」としていますが、
   この表は特殊レジスタへ書く命令の扱いに穴があることが知られています（`notes/07-pie-examples.md`）。
   実際に距離 1 で足りれば 1 サイクル/8 サンプル安くなるだけで、**正しさは変わりません**。
2. **`EE.VMUL.S16` の SAR が読まれる段**（use SAR 1(E)）。`wsr.sar` 自体の段は TRM に無い（ベース ISA）ので、
   `wsr.sar` → `VMUL` の距離（このカーネルでは 1 スロット）は**仮定**です。足りなければ 1 サイクル遅くなるだけで、
   値は変わりません。
3. **`EE.LD.128.USAR.IP` の後置インクリメントが 16 バイト**であること。ex15 が
   `data/pie_examples_measured.json :: post_increment_step_accx_forms`（`printed_immediate_16.ld128_usar = 16`、
   status `confirmed`）を根拠に「印刷した即値がそのまま歩幅」と確定していますが、**本カーネル自身は実機で
   測っていません**（B はこの命令で 10 本のポインタを歩かせています）。
4. **`EE.LD.128.USAR.IP` + `EE.SRC.Q` の窓の意味**（下位 4bit を落とす規則、`SAR_BYTE` の定義、
   `SRC.Q` の第 1 オペランドが qs0 = 下位）。ex03 がロード側を実機で再現し、ex15 がオペランド順を実機で
   確定しています（`src_q_operand_order_and_the_funnel_path`）。**B は「+16 しても下位 4bit が変わらないので
   2 つのロードが同じ SAR_BYTE を立てる」ことに依存**しており、この性質自体は実機で確かめていません。
5. **A のゲインの畳み込み**: `lgain = rgain = 2^31`（= 1.0）と `kl == kr` は特殊化です。CELT のゲインは
   `celt_rsqrt_norm32` の出力で、2^31 は Q31 では表現できません（最大は 2^31-1 = Q31ONE。測ったとおり
   1 ULP ずれます）。一般のゲインを扱うには引数が 2 つ足りません。
6. **A の `kl < 15`（C では左シフト）は契約外**です。呼び出し側が `2^(15-kl)` を先に掛ける前提です。
   `shift` は `uint32_t` なので負のシフトは渡せません。
7. **`shift` の意味はカーネルが検証しません**（B では 15 が CELT の値、A では kl-15）。間違えた `shift` は
   静かなゲイン誤差になります。A は `shift == 0` で別ループに分岐しますが、それ以外は分岐しません。
8. **タップゲインの 1 LSB**（2.6）。`tapset 2` の第 1 タップは表の 26208 を使うので、CELT の
   `g = Q15ONE` 呼び出し（26207）と 1 LSB 違います。一般の `g` を渡す引数はありません。
9. **B の読み出しフットプリント**: 窓は「アドレスを含む整列チャンク」と「その次のチャンク」で作るので、
   1 呼び出しで `[floor(hist-2), floor(hist+n+1)+32)` バイトを読みます（**読みのみ**。`hist-2` の手前
   最大 16 バイト、最後のタップ窓の先 48 バイト）。モデルは実際に読んだ範囲を報告しますが、
   実機のページ境界をまたがないことは保証していません。`hist` の手前 2 サンプルと、末尾の先の領域は
   呼び出し側の責任です（CELT では `decode_mem`/`prefilter_mem` + 同じ確保域内の MDCT オーバーラップが
   それを満たします）。
10. **B を `y == x` で呼ぶ場合**: 直接タップ `x[i..i+7]` をその反復のストアより前にロードしているので
   エイリアスしても壊れませんが、`hist` が `x` の遅延コピーである前提（`hist[j] == x[j-T]`）は
    呼び出し側の契約です。カーネルは検証しません。
11. **A と C の出力の狭め方**: A は shift==0 で**ラップ**（C の代入と同じ）、shift≥1 では値が必ず収まるので
    どちらでも同じ。C (intensity) は**飽和**を選んでいます（CELT の 32bit 和に対する意味の近さで選んだ
    設計判断で、測った件数を 3 節に残しました）。**どちらが「正しい」かは呼び出し側の型で決まります**。
12. **`n % 8 == 0` と 16 バイト整列は契約**です。端数はカーネルが黙って捨てます（`srli` で 8 の倍数に
    落とす）。CELT のバンド長 `N = M*(eBands[i+1]-eBands[i])` は 8 未満もあり得るので、その場合は
    呼び出し側がまとめるか、スカラ版を用意する必要があります。
13. **サイクル数は 1 つも測っていません**。5 節の数は発行スロットとメモリ操作の数で、`BENCH` 行は
    存在しません（ex18 は `main.c` に入っていません）。
14. **piesim の 3 命令の追加は私のもの**です（TRM の Operation 疑似コードから）。B の解釈実行は
    「その 3 命令に関する私の読み」を検査しているのであって、シリコンの証拠ではありません。

## 10. 検査が捕まえたもの（この文書の値打ち）

- **T1 が shift==0 の経路のバグを捕まえた**: 最初の版は `2*(mid>>1) + b` を計算していました。
  正しい半値は「**差の**半分」`floor((mid-side)/2)` です。12,665 群のうち 12,542 群が落ちて
  1 分以内に分かりました。
- **`EE.MOVI.32.Q` は 32bit 語を入れる命令**であること。`0x00004000` を入れると 16bit レーンは
  `[0x4000, 0x0000, 0x4000, 0x0000, ...]` になり、レーン 1 本おきが 0 になります。全レーン同値には
  32bit 語の両半分に同じ値を入れる必要があります（`ex15_fill_row` が色で同じことをしています）。
  T1 がこれも捕まえました（奇数レーンだけ 0 になっていた）。
- **piesim が `ex18_intensity_stereo` の実バグを捕まえた**: x を読み書き両方に使うのに、
  ロードの `.IP` を 16 にしてしまい、ストアが x+i+8 に書いていました。
- **piesim が私の piesim 追加のバグを捕まえた**: `EE.SRC.Q` の 256bit 組み立てで、`qs1` を先に
  詰める必要があるのに `qs0` を先に詰めていました（結果が 8 サンプルずれる）。モデルは正しく、
  追加側が間違っていました——**モデルと別実装を並べる価値が出た場所**です。
- **C のマクロの写し間違いを prelude 2 が捕まえた**: `MULT16_16SU` を `(opus_uint32)` で書くと
  乗算が unsigned になり `SHR` が**論理**シフトになります。木の定義は
  `((opus_val32)(opus_val16)(a)*(opus_val32)(opus_uint16)(b))`（`celt/fixed_generic.h:37`）で、
  書き直したら 94,407,129 組の不一致が 0 になりました。**「1 タップ 1 floor」という読みはこの 1 行に乗っています**。

## 11. 本採用するときに触るファイル（今回は触っていない）

このファイルは `proposed/` にあり、`examples/firmware/main/CMakeLists.txt` の `SRCS` に入っていません。
本採用するなら既存の例題と同じ手順（notes/08 の「サンプルの足し方」）で:

1. `examples.h` に 3 つの宣言（引数と戻りの意味をコメントで）。
2. `main.c` に `ex18()`: 決定論的入力 → カーネル → C 参照 → `CHECK` → `BENCH` → `DATA`。
   行の例として、A は `shift = 0` と `shift = 3`（境界値と、mid∓side が int16 を出る入力）、
   B は 3 tapset × 窓の位相 0..15、C は `a1=a2=23170` と片側ゼロの 2 ケース。DATA 行には
   不一致件数と max|d|、および B の「C の値が int16 を出た件数」を出します。
3. `tools/check_examples_log.py` に `check_ex18()`（Python で三度目の計算 = この文書のモデルと同じ参照）と
   `CHECKS` への登録。
4. `tools/selftest_examples_checker.py` の合成ログに `DATA` 行と、そのフィールドを壊す変異
   （例: A の `shift` を 1 つずらす、B の `tapset` を差し替える）を追加。
5. `CMakeLists.txt` の `SRCS` に 1 行、notes/08 の表と `examples/README.md` の一覧を更新。

**実機で最初に叩くべきは `EE.SRCMB.S16.QACC`（TRM p130）**です。理由: (1) B の全出力がこの 1 命令の
読み出しを通る、(2) 40bit レーンの sat16 が**どこで**飽和するかが 2.5 の数字を左右する、
(3) リポジトリの `open_after_this_run` が QACC のメモリ形（`EE.LDQA`/`EE.STQA`）を未測定として名指ししており、
その読み出し側の兄弟にあたるから。プローブは 3 つで足ります:

1. QACC の 1 レーンに 32767*32767 を 1 回入れ、`SRCMB.S16.QACC qu, as(=15)` の `qu` が
   sat16(32767*32767/32768) = 32766 になるか（飽和位置と切り捨ての確認）。
2. 同じ値で `as = 0` を渡し、`qu` が 32767 に**飽和**するか（40bit → int16 のクランプ位置）。
3. 1 呼び出しで `n = 24`、`tapset = 2`、`hist` の位相を 0..15 で回し、出力と「C の値が int16 を出た件数」を
   `DATA` 行で突き合わせる（位相非依存と飽和件数が同時に検証されます）。

---

**実機は使っていません。** この文書の数値はすべて (a) アセンブラとオブジェクトファイル、(b) Python の
モデルとその実行出力、(c) ホストの `gcc -O2` でビルドした C 検証器の出力、(d) `/tmp` にコピーした
`piesim.py` の解釈実行、のいずれかから来ています。シリコンの証拠として引用しているのは
`data/*.json` の既存の実測（ex01/ex03/ex08/ex15 の結果）だけで、本カーネル自身の実測はありません。
