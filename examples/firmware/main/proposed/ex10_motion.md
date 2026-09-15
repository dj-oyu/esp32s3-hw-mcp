# ex10 — 動画の動き補償まわりの PIE プリミティブ: ブロック SAD とハーフペル行

`examples/firmware/main/proposed/ex10_motion.S` の2カーネル。動画デコードの動き補償・スプライト移動で
最初に要る2つを、8レーン（16bit × 8）単位のベタ書き PIE で書いたもの。

| カーネル | 何をするか | ベクトル命令（1反復あたり） |
|---|---|---|
| `ex10_sad8` | ブロック SAD（8 × uint16 レーン = 1ブロック）。`|a-b|` は `EE.VSUBS.S16(EE.VMAX.S16, EE.VMIN.S16)`（PIE に SAD も ABS も無い）。合算は `EE.VMULAS.U16.ACCX` を「1のレーン」に対して使い、40bit の ACCX を最後に一度だけ `RUR.ACCX_0/ACCX_1` で読む | `VLD`×2, `VMAX`, `VMIN`, `VSUBS`, `VMULAS` = 6 |
| `ex10_halfpel` | ハーフペル補間行 `out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1` | `VLD`×2, `VADDS`×2, `VMUL`, `VST` = 6 |

**状態: proposed（提案）。** `examples/firmware/main/CMakeLists.txt` は `SRCS` を明示列挙しているので、
`proposed/` に置いたこのファイルはビルドに入っていない（main.c / examples.h / CMakeLists.txt は触って
いない）。組み込むときは notes/08 の定型（CMakeLists に1行、examples.h に宣言、main.c に `ex10()`）。

**番号の衝突に注意:** `notes/08-media-3d-perf.md` の量産リストは **ex10 を「スパン塗りラスタライザ」**に
割り当てている。ここでは motion（SAD + ハーフペル）を ex10 として書いたので、どちらかを繰り下げる必要が
ある（notes/08 の更新は組み込む人の作業）。

## この文書の検証段（何が確認済みで、何が未確認か）

1. **アセンブラ**: `xtensa-esp32s3-elf-gcc -c` が通ること、`objdump` に出るエンコーディング → 生の出力を下に貼った。
2. **命令の意味**: 各命令の役割は TRM の疑似コード（`data/pie_instructions.json` の `source_page`）に対応づけた。下の表がその対応表。
3. **Python モデル**: 命令列をそのままモデル化し、意図した意味（C 参照）との等価性を乱数入力で確認。スクリプトと生の出力を貼った。
4. **C 参照**: コンパイル（ターゲットとホストの両方）して、モデルと同じ入力で突き合わせた。

**実機は使っていない**（`/dev/ttyACM0` は計測ラン中）。したがって「シリコンが疑似コードどおりに動く」とは
一言も主張していない。確認できたのは上の1〜4まで。最後に「確認できていないこと」を列挙した。

## カーネル A — `ex10_sad8`

```c
void ex10_sad8(const uint16_t *a, const uint16_t *b, uint32_t n_blocks, uint32_t *out);
/* a, b : n_blocks * 8 レーンずつ（16バイト整列。128bit アクセスは下位4bitを落とす = TRM p49）
 * out  : out[0] = ACCX[31:0], out[1] = ACCX[39:32]
 *        host 側の合計 = ((uint64_t)out[1] << 32) | out[0]                       */
```

1反復（1ブロック = 8レーン）の中身:

```
    EE.VLD.128.IP q0, a2, 16        a の8レーン
    EE.VLD.128.IP q1, a3, 16        b の8レーン
    EE.VMAX.S16 q2, q0, q1          レーンごとの max（符号付き比較）
    EE.VMIN.S16 q3, q0, q1          レーンごとの min
    EE.VSUBS.S16 q4, q2, q3         |a-b|（飽和）
    EE.VMULAS.U16.ACCX q4, q7       ACCX += 8レーンの和（q7 = 1のレーン×8、符号なし）
```

「1のレーン×8」はカーネル内で作る: `movi a9,1; slli a9,a9,16; addi a9,a9,1` で `0x00010001` を作り、
`EE.MOVI.32.Q q7, a9, 0..3` で4つの32bitセグメントに書く。**32bit 定数の `movi` はリテラルプール
（`l32r`）になる**ので使わない — `.iram1` から `.rodata` への `l32r` はリンカが拒否する（他の例題が定数を
C からポインタで受けている理由）。objdump を見ると `.iram1.literal` セクションはできていない
（`movi.n a9,1` + `slli` + `addi` の3命令だけ）。

### 出典（TRM ページ = `data/pie_instructions.json` の `source_page`）

| 命令 | 役割 | source_page |
|---|---|---|
| `EE.MOVI.32.Q` | 32bit GPR を QR の1セグメントに書く（1のレーン生成） | 119 |
| `EE.ZERO.ACCX` | ACCX = 0 | 298 |
| `EE.VLD.128.IP` | 16バイト読み＋アドレス後置インクリメント | 164 |
| `EE.VMAX.S16` | レーンごとの符号付き最大 | 180 |
| `EE.VMIN.S16` | レーンごとの符号付き最小 | 189 |
| `EE.VSUBS.S16` | レーンごとの飽和減算 | 281 |
| `EE.VMULAS.U16.ACCX` | 8レーンの符号なし積和を ACCX に合算 | **240** |
| （RUR）`RUR.ACCX_0` / `RUR.ACCX_1` | ACCX[31:0] / ACCX[39:32]（ゼロ拡張）を読む | TRM p64（1.6.10）※ `data/pie_instructions.json` に項目が無い |
| Table 1.7-2 | `EE.VMULAS.U16.ACCX` の ACCX = stage-2 def | 71（`data/pie_pipeline.json`） |

カーネル A の意味の出典は **TRM p240 の `EE.VMULAS.U16.ACCX` 疑似コード**（
`sum = ACCX + add0 + ... + add7`、`ACCX = min(max(sum,0), 2^40-1)`、**SAR は掛からない**）＋
p180/p189/p281（MAX/MIN/VSUBS.S16）＋ p64（ACCX の読み出し）。

注意2点:

- **`RUR.ACCX_0` / `RUR.ACCX_1` は `data/pie_instructions.json`（220件 = `EE.*` と LD.QR/ST.QR/MV.QR）に
  入っていない。** プロセッサ制御命令（TRM 1.6.10）なので `tools/asm_toolchain.py` の符号化照合の対象外で、
  マニュアルの図とのビット単位照合はされていない。意味（下位32bit / 上位8bit をゼロ拡張）は TRM p64 の
  記述どおり。アセンブラが受けたことは objdump の `rur.accx_0` / `rur.accx_1` で確認できる。
- **`EE.SRS.ACCX` を使っていない理由**: `EE.SRS.ACCX au, as, 0` は ACCX をシフトして**書き戻し**、結果を
  32bit 符号付きに飽和して返す（TRM p134）。SAD の合計は 2^31 を越えうる（フルレンジのレーンで約8192
  ブロック、8bit サンプルでも約1e6ブロック）ので、そこから先は黙って 2^31-1 で止まる。RUR の2回読みなら
  飽和も副作用も無く 40bit がそのまま取れる。読み出しはループの外で1回だけ。

### 正確さの限界（ここがこのカーネルの要点）

`|a-b| = VSUBS(VMAX.S16, VMIN.S16)` は**符号付きの読み**で計算している。`a >= 32768` のとき
`A = a - 65536` なので、`A - B` は `a - b` と一致するとは限らない:

- **同じ半分**（両方 `<= 32767`、または両方 `>= 32768`）: `A - B = a - b`、つまり結果はちょうど `|a-b|`。
  8bit の輝度・色差サンプル（`0..255`）をゼロ拡張して載せるこの例題の用途はここに入る。
- **32768 をまたぐ**（片方だけ上位半分）: `A - B = (a - b) ± 65536` で、大きさは `65536 - |a-b|`（差の
  **補数**）。

さらに `EE.VSUBS.S16` は飽和する（`min(max(x, -2^15), 2^15-1)`）ので、1レーンの寄与は
`min(<|a-b| または 65536-|a-b|>, 32767)`。この「同じ半分なら `|a-b|`、またぐなら補数、最後に 32767 で
飽和」という規則は、Python モデルで **655360 組のレーンペア**を掃引して反例ゼロで確認している
（下の出力の `A/rule` 行）。

モデルの乱数入力での実測（出力をそのまま引用、下に全文）:

- 8bit サンプル 512 レーン: `kernel_total=41792`、教科書 SAD も `41792`、またぐレーン 0、飽和レーン 0 → **一致**
- フルレンジ uint16 4096 レーン: `kernel_total=78738288` に対し教科書 SAD は `89195816`（またぐレーン
  2026、飽和レーン 1048）→ **一致しない**（カーネルは「同じ半分/補数/飽和」を計算している）

つまり **8bit サンプルのドメインでは厳密**、フルレンジの uint16 レーンは契約外。契約外を検出したいなら
`ex10_sad8_plain_c`（教科書版）と比べて差が出たことを報告する、という使い方を .md は想定している。

### C 参照（ファームが突き合わせる相手）

```c
/* カーネルと同じ意味（命令の意味から書く = 飽和する符号付き距離）。
   これなら CHECK はどの入力でも厳密。8bit サンプルでは下の教科書版と一致する。 */
uint64_t ex10_sad8_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n_blocks * 8; i++) {
        int32_t d = (int32_t)(int16_t)a[i] - (int32_t)(int16_t)b[i];   /* VSUBS(VMAX,VMIN) */
        if (d < 0) {
            d = -d;
        }
        if (d > 32767) {
            d = 32767;                                                 /* VSUBS.S16 の飽和 */
        }
        acc += (uint32_t)d;
    }
    return acc;
}

/* 教科書どおりの SAD（uint16 の読みでクランプ無し）。8bit サンプルではカーネルと一致し、
   フルレンジのレーンでは一致しない — その差を「契約外の入力を見つけた」信号に使う。 */
uint64_t ex10_sad8_plain_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks);
```

完全なソース（この2つと `ex10_halfpel_c`、それにホスト側ドライバ）は下の「C 参照の検証」節にある。

## カーネル B — `ex10_halfpel`

```c
void ex10_halfpel(const int16_t *a, const int16_t *b, const int16_t *ones8, int16_t *out,
                  uint32_t n_lanes);
/* a, b : n_lanes レーン（16バイト整列）。ones8 は8レーンすべて 1（定数は C 側から渡す）
 * out  : n_lanes レーン。n_lanes は8の倍数（端数レーンは触らない）
 * out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1    ← 参照式（シフトの前にキャスト） */
```

### 選んだ命令列と、その理由（この節が設問への答え）

```
    ssr(1)                          SAR = 1（下の論理シフト用）
1   EE.VADDS.S16 q2, q0, q1         q2 = sat16(a + b)              和（飽和）
2   EE.VADDS.S16 q2, q2, q6         q2 = sat16(q2 + 1)             丸め加算（q6 = ones8、飽和）
3   EE.VMUL.U16  q2, q2, q6         q2 = (u16(q2) * 1) >> 1        >> 1（論理シフト）
```

- **1つの定数レジスタで2と3を兼ねる**: 1のレーンは「足す1」と「掛ける1」の両方に使える。
- **3が `EE.VMUL.U16` で `EE.VMUL.S16` でない理由**: 参照式はシフトの前に `uint16_t` にキャストして
  いるので、レーンは**符号なし**として読まれる。`.U16` はレーンの符号なし値を論理シフトするので、
  `0x8000..0xFFFF` は `0x4000..0x7FFF` になり、`((uint16_t)x) >> 1` と一致する。`.S16` は先に符号拡張
  するので上位半分のレーンが違う値になる（ex08_shift_sign が実機で見せたのと同じ差）。PIE に素の
  右シフト命令は無いので「1を掛けて SAR=1」がレーンごとの `>> 1` の綴り。
- **丸め加算が飽和する件（設問の指摘どおり、`VADDS.S16` は結果を壊しうる）**: `EE.VADDS.S16` は
  `[-32768, 32767]` にクランプする（TRM p146）。したがって**参照式と一致するのは `a+b+1` が符号付き
  16bit に収まるレーンだけ**で、収まらないレーンでは必ず食い違う（モデルは「食い違い集合 = 飽和が起きた
  レーン集合」を等号で確認している）:

  | 条件 | カーネル | 参照式 `((uint16_t)(a+b+1)) >> 1` |
  |---|---|---|
  | `a+b+1 <= 32767`（かつ `>= -32768`） | 一致 | 一致 |
  | `a+b+1 > 32767` | 和が 32767 で止まり **16383** | 16bit で切り詰めて 16384..32767 |
  | `a+b+1 < -32768` | 和が -32767 で止まり、符号なし読みで 32769 → **16384** | 先に mod 65536 で折り返して小さい正の値 |

  **8bit サンプル（0..255、`a+b+1 <= 511`）ではこの2行はどちらも到達しない**ので、このカーネルは用途
  ドメインで厳密。そして**加算の順序を入れ替えても直らない**: PIE のベクトル加算は飽和する `VADDS` だけで、
  折り返す（mod 16bit の）レーン加算命令が無い。飽和する加算を並べ替えても、飽和する入力は再現できない。
  フルレンジの int16 レーンは契約外で、モデルの実行は「食い違ったレーン数」を数えるに留めている。

### 出典（TRM ページ = `data/pie_instructions.json` の `source_page`）

| 命令 | 役割 | source_page |
|---|---|---|
| `EE.VLD.128.IP` | 16バイト読み（ones8 と a, b） | 164 |
| `EE.VADDS.S16` | レーンごとの飽和加算（和と丸め加算の2回） | **146** |
| `EE.VMUL.U16` | レーンごとの符号なし積 >> SAR（`ones` で >> 1） | **204** |
| `EE.VST.128.IP` | 16バイト書き | 275 |
| Table 1.7-2 | `VADDS.S16` 68 / `VMUL.U16` 70 / `VLD.128.IP` 69 / `VST.128.IP` 73 | （`data/pie_pipeline.json`） |

`SAR` を設定する `ssr` は Xtensa コアの命令（PIE の 220 件には入っていない）。

### C 参照

```c
/* out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1  — キャストはシフトの前 */
void ex10_halfpel_c(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n_lanes)
{
    for (uint32_t i = 0; i < n_lanes; i++) {
        out[i] = (int16_t)((uint16_t)((int32_t)a[i] + (int32_t)b[i] + 1) >> 1);
    }
}
```

## アセンブル結果（生の出力）

```
$ cd /workspace/esp32s3-hw-mcp && . /opt/esp-idf/export.sh
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex10.o examples/firmware/main/proposed/ex10_motion.S
$ echo $?
0
```

**実際の出力は空**（成功時にアセンブラは何も出さない）。`/tmp/ex10.stderr` のバイト数は 0。つまり
「貼るべき出力」はこの空出力そのもので、証拠は下の逆アセンブル。ツールチェーンは
`xtensa-esp-elf-gcc (crosstool-NG esp-15.2.0_20251204) 15.2.0`。

```
$ xtensa-esp32s3-elf-nm /tmp/ex10.o
00000048 T ex10_halfpel
00000000 T ex10_sad8
```

`.iram1.literal` セクションは存在しない（32bit 定数の `l32r` を作っていない = 定数はすべて命令で組むか
C からポインタで受ける）。

```
$ xtensa-esp32s3-elf-objdump -h /tmp/ex10.o
Idx Name          Size      VMA       LMA       File off  Algn
  3 .iram1        00000072  00000000  00000000  00000034  2**2
```

```
$ xtensa-esp32s3-elf-objdump -d /tmp/ex10.o
Disassembly of section .iram1:

00000000 <ex10_sad8>:
   0:	004136        	entry	a1, 32
   3:	190c      	movi.n	a9, 1
   5:	119900        	slli	a9, a9, 16
   8:	01c992        	addi	a9, a9, 1
   b:	fdb294        	ee.movi.32.q	q7, a9, 0
   e:	fdb694        	ee.movi.32.q	q7, a9, 1
  11:	fdba94        	ee.movi.32.q	q7, a9, 2
  14:	fdbe94        	ee.movi.32.q	q7, a9, 3
  17:	250804        	ee.zero.accx
  1a:	016416        	beqz	a4, 34 <ex10_sad8+0x34>
  1d:	830124        	ee.vld.128.ip	q0, a2, 16
  20:	838134        	ee.vld.128.ip	q1, a3, 16
  23:	9e2824        	ee.vmax.s16	q2, q0, q1
  26:	9ea854        	ee.vmin.s16	q3, q0, q1
  29:	ae3ad4        	ee.vsubs.s16	q4, q2, q3
  2c:	0a5c84        	ee.vmulas.u16.accx	q4, q7
  2f:	440b      	addi.n	a4, a4, -1
  31:	fe8456        	bnez	a4, 1d <ex10_sad8+0x1d>
  34:	f03d      	nop.n
  36:	f03d      	nop.n
  38:	f03d      	nop.n
  3a:	f03d      	nop.n
  3c:	e36000        	rur.accx_0	a6
  3f:	e37010        	rur.accx_1	a7
  42:	0569      	s32i.n	a6, a5, 0
  44:	1579      	s32i.n	a7, a5, 4
  46:	f01d      	retw.n

00000048 <ex10_halfpel>:
  48:	004136        	entry	a1, 32
  4b:	416360        	srli	a6, a6, 3
  4e:	170c      	movi.n	a7, 1
  50:	400700        	ssr	a7
  53:	b30044        	ee.vld.128.ip	q6, a4, 0
  56:	016616        	beqz	a6, 70 <ex10_halfpel+0x28>
  59:	830124        	ee.vld.128.ip	q0, a2, 16
  5c:	838134        	ee.vld.128.ip	q1, a3, 16
  5f:	9e0864        	ee.vadds.s16	q2, q0, q1
  62:	9e5264        	ee.vadds.s16	q2, q2, q6
  65:	9e72a4        	ee.vmul.u16	q2, q2, q6
  68:	9a0154        	ee.vst.128.ip	q2, a5, 16
  6b:	660b      	addi.n	a6, a6, -1
  6d:	fe8656        	bnez	a6, 59 <ex10_halfpel+0x11>
  70:	f01d      	retw.n
```

対応する .S の md5（この出力を出した版）: `55be996820c008cabacb63e75f0a6918`

## Python モデルと等価性チェック（生の出力）

命令列をそのままモデル化した（`EE.VMAX/VMIN/VSUBS/VADDS.S16`、`EE.VMULAS.U16.ACCX`、`EE.VMUL.U16`、
`RUR.ACCX_0/1` は上の表のページの疑似コードどおり）。モデルは

- カーネル A: レーン寄与の規則（同じ半分なら `|a-b|`、またぐなら補数、32767 で飽和）、
  ACCX からの 40bit 復元、飽和参照との一致、教科書 SAD との差、
- カーネル B: 8bit サンプルで参照式と全レーン一致、フルレンジで「食い違い集合 = 丸め加算が飽和した
  レーン集合」であること、
- 追加で、コンパイル済み C 参照（argv[1]）との突き合わせ

を assert で確かめる。スクリプトは下に全文（実行したのは `/tmp/ex10_motion_model.py`）。

実行:

```
$ gcc -O2 -o /tmp/ex10_ref /tmp/ex10_ref.c          # 下の「C 参照の検証」のソース
$ python3 /tmp/ex10_motion_model.py /tmp/ex10_ref
```

出力（そのまま）:

```
ex10_motion.S model -- deterministic (random.Random(0x10ADA5))
Kernel A: ex10_sad8  (VMAX/VMIN/VSUBS.S16 -> VMULAS.U16.ACCX against ones, read out once)
  A/8-bit samples, 64 blocks: lanes=512 kernel_total=41792 reference=41792 ACCX=ok (out0=0x0000a340 out1=0x00)
  A/8-bit samples, 64 blocks: textbook_SAD=41792 straddling_lanes=0 clamped_lanes=0 kernel==textbook: True
  A/full-range uint16, 512 blocks: lanes=4096 kernel_total=78738288 reference=78738288 ACCX=ok (out0=0x04b17370 out1=0x00)
  A/full-range uint16, 512 blocks: textbook_SAD=89195816 straddling_lanes=2026 clamped_lanes=1048 kernel==textbook: False
  A/rule: 655360 lane pairs swept (a in 0..65535 x 8 offsets, plus 131072 random): counterexamples to |a-b| same-half / 65536-|a-b| straddling = 0; pairs where that differs from the textbook |a-b| = 65959
Kernel B: ex10_halfpel  (VADDS.S16 -> VADDS.S16(+ones8) -> VMUL.U16(ones8, SAR=1))
  B/8-bit samples, 1024 lanes: lanes=1024 matching_reference=1024 mismatching=0 rounding_add_saturates=0 (pos=0 neg=0) first_mismatch=None
  B/full-range int16, 1024 lanes: lanes=1024 matching_reference=749 mismatching=275 rounding_add_saturates=275 (pos=132 neg=143) first_mismatch=[0, 1]
  B/full-range: the 275 diverging lanes are exactly the lanes where the rounding add saturates (132 positive -> 16383, 143 negative -> 16384) and nothing else diverges
C references (compiled from the .md's source):
  C/8-bit samples 512 lanes: ex10_sad8_c=41792 (model 41792) ex10_sad8_plain_c=41792 (model 41792) ex10_halfpel_c: 512 lanes = reference formula, kernel_model_diverges_on=0
  C/full-range uint16 4096 lanes: ex10_sad8_c=78738288 (model 78738288) ex10_sad8_plain_c=89195816 (model 89195816) ex10_halfpel_c: 4096 lanes = reference formula, kernel_model_diverges_on=1031
  C/8-bit samples 1024 lanes: ex10_sad8_c=87490 (model 87490) ex10_sad8_plain_c=87490 (model 87490) ex10_halfpel_c: 1024 lanes = reference formula, kernel_model_diverges_on=0
  C/full-range int16 1024 lanes: ex10_sad8_c=19041210 (model 19041210) ex10_sad8_plain_c=21935349 (model 21935349) ex10_halfpel_c: 1024 lanes = reference formula, kernel_model_diverges_on=275
RESULT model_checks ok=1 fail=0
```

スクリプト全文（実行したものと同一）:

```python
#!/usr/bin/env python3
"""ex10_motion.S -- a Python model of the two kernels, checked against the reference semantics.

The point of this file is to make the two kernels' semantics falsifiable off-hardware: each instruction is
transcribed straight from the TRM pseudo-code cited in ex10_motion.S (EE.VMAX/VMIN/VSUBS/VADDS.S16,
EE.VMULAS.U16.ACCX, EE.VMUL.U16, RUR.ACCX_0/1), each kernel is modelled as its sequence of those steps, and
the result is compared against the C reference expressions in ex10_motion.md.

The assertions are the claims the .S comments make:
  A1  the 40-bit readout pair reconstructs ACCX, and ACCX stays far below the clamp
  A2  the kernel equals the saturating signed-distance reference on both input domains
  A3  the per-lane contribution is min(<|a-b| or 65536-|a-b|>, 32767): |a-b| when the two lanes are in the
      same half of the 16-bit range, the complement when they straddle 32768, clamped at 32767
  A4  on 8-bit sample data that reduces to the textbook SAD, and it does NOT on full-range lanes
  B1  the kernel equals the C reference on 8-bit sample data
  B2  on full-range lanes it diverges only where the rounding add saturates, and returns 16383 there

Run:  python3 ex10_motion_model.py
"""
import random
from itertools import chain

MASK16 = 0xFFFF
UINT40_MAX = (1 << 40) - 1


def sat16(x):
    """min(max(x, -2^15), 2^15-1) -- the clamp shared by EE.VADDS.S16 and EE.VSUBS.S16"""
    return -32768 if x < -32768 else (32767 if x > 32767 else x)


def s16(u):
    """the SIGNED reading of a 16-bit lane (VMAX/VMIN/VSUBS are the .S16 forms)"""
    return u - 65536 if u >= 32768 else u


def vmax_s16(x, y):
    """EE.VMAX.S16 qa, qx, qy (TRM p180): signed per-lane maximum"""
    return [max(s16(a), s16(b)) for a, b in zip(x, y)]


def vmin_s16(x, y):
    """EE.VMIN.S16 qa, qx, qy (TRM p189): signed per-lane minimum"""
    return [min(s16(a), s16(b)) for a, b in zip(x, y)]


def vsubs_s16(x, y):
    """EE.VSUBS.S16 qa, qx, qy (TRM p281): qa[l] = sat16(qx[l] - qy[l])"""
    return [sat16(a - b) for a, b in zip(x, y)]


def vadds_s16(x, y):
    """EE.VADDS.S16 qa, qx, qy (TRM p146): qa[l] = sat16(qx[l] + qy[l])"""
    return [sat16(a + b) for a, b in zip(x, y)]


def vmul_u16(x, y, sar):
    """EE.VMUL.U16 qz, qx, qy (TRM p204): qz[l] = (u16(x[l]) * u16(y[l])) >> SAR, truncated to the lane"""
    return [(((x[i] & MASK16) * (y[i] & MASK16)) >> sar) & MASK16 for i in range(8)]


def vmulas_u16_accx(accx, x, y):
    """EE.VMULAS.U16.ACCX qx, qy (TRM p240):
       ACCX = min(max(ACCX + sum_l u16(x[l]) * u16(y[l]), 0), 2^40-1)  -- no SAR in this form"""
    s = accx + sum((x[i] & MASK16) * (y[i] & MASK16) for i in range(8))
    return min(max(s, 0), UINT40_MAX)


# --------------------------------------------------------------------------- kernel A model

def kernel_sad_lane(a, b):
    """one lane through the vector unit, exactly as the assembly runs it"""
    return vsubs_s16(vmax_s16([a], [b]), vmin_s16([a], [b]))[0] & MASK16


def kernel_sad8(a, b, n_blocks):
    """ex10_sad8: per block, |a-b| = VSUBS(VMAX, VMIN), then VMULAS.U16.ACCX against eight lanes of ones.
       Returns (total, out0, out1); out0/out1 are what RUR.ACCX_0 / RUR.ACCX_1 store."""
    ones = [1] * 8
    accx = 0
    for k in range(n_blocks):
        q4 = [kernel_sad_lane(a[8 * k + l], b[8 * k + l]) for l in range(8)]
        accx = vmulas_u16_accx(accx, q4, ones)
    return accx, accx & 0xFFFFFFFF, (accx >> 32) & 0xFF


def ex10_sad8_c(a, b, n_blocks):
    """the scalar C reference the firmware compares against: the instructions' semantics, i.e. the
       saturating distance between the SIGNED readings of the lanes (not the textbook |a-b|)"""
    total = 0
    for i in range(n_blocks * 8):
        d = abs(s16(a[i]) - s16(b[i]))          # VSUBS.S16(VMAX.S16, VMIN.S16)
        total += min(d, 32767)                  # VSUBS.S16 saturates
    return total


def ex10_sad8_plain_c(a, b, n_blocks):
    """textbook SAD over the UNSIGNED reading of the lanes (the uint16_t view) -- what the kernel computes
       on 8-bit sample data, and deliberately not what it computes on full-range lanes"""
    return sum(abs((a[i] & MASK16) - (b[i] & MASK16)) for i in range(n_blocks * 8))


def predicted_lane(a, b):
    """the characterization asserted in the .S comment"""
    if (a < 32768) == (b < 32768):              # same half of the range
        return min(abs(a - b), 32767)
    return min(65536 - abs(a - b), 32767)       # straddling 32768: the complement


# --------------------------------------------------------------------------- kernel B model

def kernel_halfpel(a, b, n_lanes):
    """ex10_halfpel: VADDS(a,b) -> VADDS(+ones8) -> VMUL.U16(ones8) with SAR=1 (logical >> 1).
       a and b are raw 16-bit lane patterns (the signed reading is what the .S16 instructions see)."""
    ones = [1] * 8
    out = [0] * n_lanes
    for k in range(n_lanes // 8):
        q0 = [s16(v) for v in a[8 * k:8 * k + 8]]
        q1 = [s16(v) for v in b[8 * k:8 * k + 8]]
        q2 = vadds_s16(q0, q1)                                   # sat16(a + b)
        q2 = vadds_s16(q2, ones)                                 # sat16(q2 + 1): the rounding add
        out[8 * k:8 * k + 8] = vmul_u16(q2, ones, 1)             # (u16 * 1) >> 1, logical
    return out


def ex10_halfpel_c(a, b, n_lanes):
    """the C reference: out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1 (the cast before the shift), with a[i]
       and b[i] read as int16_t, i.e. the signed reading of the lane"""
    return [((s16(a[i]) + s16(b[i]) + 1) & MASK16) >> 1 for i in range(n_lanes)]


# --------------------------------------------------------------------------- checks

def characterize_lane_pairs(seed=11, random_pairs=131072):
    """The same-half / complement rule the .S comment states, over a sweep: every lane value a in 0..65535
       against eight b values chosen to sit in both halves and across the 32768 boundary, plus random
       pairs. Returns (pairs_tested, counterexamples_to_the_RULE, pairs_where_rule_differs_from_plain)."""
    offsets = [0, 1, 255, 32767, 32768, 32769, 65534, 65535]
    tested = 0
    rule_bad = 0
    differs_from_plain = 0
    rng = random.Random(seed)
    pairs = (((a + off) & MASK16, a) for a in range(65536) for off in offsets)
    extra = ((rng.randrange(65536), rng.randrange(65536)) for _ in range(random_pairs))
    for a, b in chain(pairs, extra):
        tested += 1
        got = kernel_sad_lane(a, b)
        want = predicted_lane(a, b)
        if got != want:
            rule_bad += 1
        if want != min(abs(a - b), 32767):
            differs_from_plain += 1
    return tested, rule_bad, differs_from_plain

def check_sad(name, a, b, expect_textbook):
    n_blocks = len(a) // 8
    total, out0, out1 = kernel_sad8(a, b, n_blocks)
    got = (out1 << 32) | out0                                        # A1: the readout pair
    ref = ex10_sad8_c(a, b, n_blocks)
    plain = ex10_sad8_plain_c(a, b, n_blocks)
    lanes = n_blocks * 8
    straddle = sum(1 for i in range(lanes) if (a[i] < 32768) != (b[i] < 32768))
    clamped = sum(1 for i in range(lanes) if abs(s16(a[i]) - s16(b[i])) > 32767)
    mism_kernel_pred = [i for i in range(lanes)
                        if kernel_sad_lane(a[i], b[i]) != predicted_lane(a[i], b[i])]
    assert got == total                                             # A1
    assert total < (1 << 39), "ACCX stays below the measured 2^39-1 clamp"
    assert total == ref                                             # A2
    assert not mism_kernel_pred, f"{name}: per-lane model != |a-b|/complement characterization"   # A3
    print(f"  A/{name}: lanes={lanes} kernel_total={total} reference={ref} "
          f"ACCX={'ok' if got == total else 'FAIL'} (out0=0x{out0:08x} out1=0x{out1:02x})")
    print(f"  A/{name}: textbook_SAD={plain} straddling_lanes={straddle} clamped_lanes={clamped} "
          f"kernel==textbook: {total == plain}")
    if expect_textbook:                                             # A4
        assert total == plain and straddle == 0 and clamped == 0
    else:
        assert total != plain
    return total, ref


def check_halfpel(name, a, b, expect_exact):
    n = len(a)
    got = kernel_halfpel(a, b, n)
    ref = ex10_halfpel_c(a, b, n)
    bad = [i for i in range(n) if got[i] != ref[i]]
    cond = set(i for i in range(n) if not (-32768 <= s16(a[i]) + s16(b[i]) + 1 <= 32767))
    assert set(bad) == cond, f"{name}: the mismatch set is not the rounding-add saturation set"    # B2
    pos = [i for i in bad if s16(a[i]) + s16(b[i]) + 1 > 32767]
    neg = [i for i in bad if s16(a[i]) + s16(b[i]) + 1 < -32768]
    assert all(got[i] == 16383 for i in pos), "a positive-saturation lane is not 16383"
    assert all(got[i] == 16384 for i in neg), "a negative-saturation lane is not 16384"
    print(f"  B/{name}: lanes={n} matching_reference={n - len(bad)} mismatching={len(bad)} "
          f"rounding_add_saturates={len(cond)} (pos={len(pos)} neg={len(neg)}) "
          f"first_mismatch={bad[:2] if bad else None}")
    if expect_exact:                                                # B1
        assert not cond and not bad
    else:
        assert bad
    return len(bad), len(pos), len(neg)


def c_cross_check(binpath, cases):
    """The C references in ex10_motion.md, compiled and executed, must compute what the model says on
       exactly the inputs the model used -- otherwise the .md would be quoting an unverified C snippet."""
    import os
    import struct
    import subprocess
    import tempfile
    tmp = tempfile.gettempdir()
    for name, a, b in cases:
        pa = os.path.join(tmp, "ex10_a.bin")
        pb = os.path.join(tmp, "ex10_b.bin")
        with open(pa, "wb") as f:
            f.write(struct.pack(f"<{len(a)}H", *[x & MASK16 for x in a]))
        with open(pb, "wb") as f:
            f.write(struct.pack(f"<{len(b)}H", *[x & MASK16 for x in b]))
        out = subprocess.run([binpath, pa, pb], capture_output=True, text=True, check=True).stdout
        fields = dict(line.split("=", 1) for line in out.strip().split("\n"))
        c_sad = int(fields["sad8_c"])
        c_plain = int(fields["sad8_plain_c"])
        c_hp = [int(x, 16) for x in fields["halfpel"].split(",")]
        m_sad, _, _ = kernel_sad8(a, b, len(a) // 8)
        m_plain = ex10_sad8_plain_c(a, b, len(a) // 8)
        m_ref_hp = ex10_halfpel_c(a, b, len(a))     # the same formula, in Python
        m_hp = kernel_halfpel(a, b, len(a))         # the instruction sequence
        assert c_sad == m_sad, f"{name}: C ex10_sad8_c {c_sad} != model {m_sad}"
        assert c_plain == m_plain, f"{name}: C ex10_sad8_plain_c {c_plain} != model {m_plain}"
        assert c_hp == m_ref_hp, f"{name}: C ex10_halfpel_c is not the documented reference formula"
        diverged = sum(1 for i in range(len(c_hp)) if c_hp[i] != m_hp[i])
        print(f"  C/{name}: ex10_sad8_c={c_sad} (model {m_sad}) ex10_sad8_plain_c={c_plain} "
              f"(model {m_plain}) ex10_halfpel_c: {len(c_hp)} lanes = reference formula, "
              f"kernel_model_diverges_on={diverged}")


def main(argv):
    rng = random.Random(0x10ADA5)                    # deterministic: the run is reproducible
    print("ex10_motion.S model -- deterministic (random.Random(0x10ADA5))")
    print("Kernel A: ex10_sad8  (VMAX/VMIN/VSUBS.S16 -> VMULAS.U16.ACCX against ones, read out once)")

    # domain 1: 8-bit samples zero-extended into the lanes -- the domain the primitive is for
    a8 = [rng.randrange(0, 256) for _ in range(64 * 8)]
    b8 = [rng.randrange(0, 256) for _ in range(64 * 8)]
    check_sad("8-bit samples, 64 blocks", a8, b8, expect_textbook=True)

    # domain 2: full-range uint16 lanes -- the boundary of the signed reading and of the saturating VSUBS
    aw = [rng.randrange(0, 65536) for _ in range(512 * 8)]
    bw = [rng.randrange(0, 65536) for _ in range(512 * 8)]
    check_sad("full-range uint16, 512 blocks", aw, bw, expect_textbook=False)

    tested, rule_bad, plain_diff = characterize_lane_pairs()
    print(f"  A/rule: {tested} lane pairs swept (a in 0..65535 x 8 offsets, plus 131072 random): "
          f"counterexamples to |a-b| same-half / 65536-|a-b| straddling = {rule_bad}; "
          f"pairs where that differs from the textbook |a-b| = {plain_diff}")
    assert rule_bad == 0

    print("Kernel B: ex10_halfpel  (VADDS.S16 -> VADDS.S16(+ones8) -> VMUL.U16(ones8, SAR=1))")

    ha = [rng.randrange(0, 256) for _ in range(1024)]
    hb = [rng.randrange(0, 256) for _ in range(1024)]
    check_halfpel("8-bit samples, 1024 lanes", ha, hb, expect_exact=True)

    wa = [rng.randrange(-32768, 32768) for _ in range(1024)]
    wb = [rng.randrange(-32768, 32768) for _ in range(1024)]
    bad, pos, neg = check_halfpel("full-range int16, 1024 lanes", wa, wb, expect_exact=False)
    print(f"  B/full-range: the {bad} diverging lanes are exactly the lanes where the rounding add "
          f"saturates ({pos} positive -> 16383, {neg} negative -> 16384) and nothing else diverges")

    if len(argv) > 1:                                # optional: C cross-check on the same inputs
        print("C references (compiled from the .md's source):")
        c_cross_check(argv[1], [("8-bit samples 512 lanes", a8, b8),
                                ("full-range uint16 4096 lanes", aw, bw),
                                ("8-bit samples 1024 lanes", ha, hb),
                                ("full-range int16 1024 lanes", wa, wb)])
    else:
        print("(pass the path of the compiled C reference driver as argv[1] to cross-check it)")
    print("RESULT model_checks ok=1 fail=0")


if __name__ == "__main__":
    import sys
    main(sys.argv)

```

## C 参照の検証（コンパイル＋モデルとの突き合わせ）

上の C 断片をそのまま置いたファイルが下。**ターゲット用にコンパイルが通ること**（ファームに組み込める形か）
と、**ホストで実行したときに Python モデルと同じ値を出すこと**の両方を確認した。これが無いと「.md に貼った
C が動く」は主張でしかなくなる。

```
$ xtensa-esp32s3-elf-gcc -c -O2 -o /tmp/ex10_ref.o /tmp/ex10_ref.c
$ echo $?
0
$ gcc -O2 -o /tmp/ex10_ref /tmp/ex10_ref.c
$ echo $?
0
```

突き合わせの結果は上の Python 出力の `C/...` 行:

- `ex10_sad8_c` は4ケースすべてでモデルと一致（512 / 4096 / 1024 / 1024 レーン）。
- `ex10_sad8_plain_c` も一致（フルレンジでは `ex10_sad8_c` と別の値になることも含めて）。
- `ex10_halfpel_c` は「参照式そのもの」であることを 4ケースで確認。カーネルモデルとの食い違いは
  フルレンジの2ケースだけ（1031 レーン / 275 レーン）で、これは上の表の飽和ゾーンと一致する。

```c
/* ex10 -- the scalar C references from examples/firmware/main/proposed/ex10_motion.md.
 *
 * These are the functions the firmware's ex10() would compare the kernels against. They are kept in a
 * standalone file so they can be compiled for the target (does it build?) and for the host (does it
 * compute what the Python model says?), which is what the .md's "C reference check" section records.
 *
 * This file is NOT part of the example build: examples/firmware/main/CMakeLists.txt lists its sources and
 * is not modified by the ex10 proposal.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define EX10_MAX_LANES 8192

/* Kernel A reference. The instructions are .S16 (EE.VMAX.S16 / EE.VMIN.S16 / EE.VSUBS.S16 over the SIGNED
 * readings of the lanes) and EE.VSUBS.S16 saturates, so this reference is written the same way rather than
 * as the textbook |a-b|: it is then exact for any input, and the two agree on 8-bit sample data. */
uint64_t ex10_sad8_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n_blocks * 8; i++) {
        int32_t d = (int32_t)(int16_t)a[i] - (int32_t)(int16_t)b[i];
        if (d < 0) {
            d = -d;
        }
        if (d > 32767) {
            d = 32767;
        }
        acc += (uint32_t)d;
    }
    return acc;
}

/* The textbook SAD (no per-lane clamp), for the host-side comparison: identical to the kernel on 8-bit
 * sample data, and deliberately not identical on full-range uint16 lanes. */
uint64_t ex10_sad8_plain_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n_blocks * 8; i++) {
        int32_t d = (int32_t)a[i] - (int32_t)b[i];
        acc += (uint64_t)(d < 0 ? -d : d);
    }
    return acc;
}

/* Kernel B reference: out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1, the cast before the shift. */
void ex10_halfpel_c(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n_lanes)
{
    for (uint32_t i = 0; i < n_lanes; i++) {
        out[i] = (int16_t)((uint16_t)((int32_t)a[i] + (int32_t)b[i] + 1) >> 1);
    }
}

/* ------------------------------------------------------------------ the host/cross-check driver */

static uint16_t s_a[EX10_MAX_LANES];
static uint16_t s_b[EX10_MAX_LANES];
static int16_t s_out[EX10_MAX_LANES];

static size_t slurp(const char *path, uint16_t *dst)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(2);
    }
    size_t n = fread(dst, 2, EX10_MAX_LANES, f);
    fclose(f);
    return n;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s a.bin b.bin\n", argv[0]);
        return 2;
    }
    size_t n = slurp(argv[1], s_a);
    if (slurp(argv[2], s_b) != n) {
        fprintf(stderr, "input length mismatch\n");
        return 2;
    }
    printf("lanes=%u\n", (unsigned)n);
    printf("sad8_c=%llu\n", (unsigned long long)ex10_sad8_c(s_a, s_b, (uint32_t)(n / 8)));
    printf("sad8_plain_c=%llu\n", (unsigned long long)ex10_sad8_plain_c(s_a, s_b, (uint32_t)(n / 8)));
    ex10_halfpel_c((const int16_t *)s_a, (const int16_t *)s_b, s_out, (uint32_t)n);
    printf("halfpel=");
    for (size_t i = 0; i < n; i++) {
        printf("%s%04x", i ? "," : "", (unsigned)(uint16_t)s_out[i]);
    }
    printf("\n");
    return 0;
}

```

## 確認できていないこと（正直な一覧）

実機（ESP32-S3）では**一度も走らせていない**。`/dev/ttyACM0` は計測ラン中で、この提案の目的は
「アセンブラとモデルが通る範囲で .S と参照を固める」こと。したがって次は未確認:

1. **`RUR.ACCX_0` / `RUR.ACCX_1` の実機の値**。意味は TRM p64（1.6.10）の記述に依拠している。この2命令は
   `data/pie_instructions.json`（220件）に項目が無いので、**`tools/asm_toolchain.py` の符号化照合を受けて
   いない**（アセンブラが通ったことと objdump のニーモニックまでは確認済み）。また ACCX を読んだときに
   値が書き戻らないか・stall が要らないかは、まさに ex09 が解こうとしている問題で未解決。
2. **`EE.MOVI.32.Q` で1のレーンを組む部分**（TRM p119）。4セグメントすべてを書いているので、疑似コード
   （選んだセグメントだけ書く）から「他のセグメントは不定のまま使う」という危険は無い、という読みだが、
   実機で確認はしていない。
3. **最後の MAC → RUR の4 `nop.n`**。Table 1.7-2 は `EE.VMULAS.U16.ACCX` の ACCX を stage-2 def と
   している（`data/pie_pipeline.json` の p71）が、**RUR.ACCX_* の行は表に無い**ので、必要な距離は
   「文書からは決まらない」。4スロットは安全側の当て推量（ループの外なのでコストは 0）で、**実測根拠は無い**。
4. **`EE.VMULAS.U16.ACCX` の ACCX 飽和値**。TRM p240 の疑似コードは `[0, 2^40-1]` だが、ex05 が実機で
   測った（S16 版の）飽和は `2^39-1`。U16 版がどちらかは未確認。SAD では到達しない（1ブロック最大
   8×32767 = 262136、`2^39-1` まで約 2.1e6 ブロック）ので実害は無い。
5. **サイクル数（BENCH）は一切測っていない**。この .md に性能の主張は1つも無い。main.c に `ex10()` を
   足すとき、既存の例題と同じ `ex07_ccount()` で挟んで `BENCH ex10_sad8 blocks=... cycles_pie/cycles_c`
   を出し、notes/08 のフレーム予算表に足すのが次の段。
6. **C 参照は main.c に組み込んでいない**ので、ファームの `CHECK pie=... ref=...` 行は存在しない。
   上の C は単体でコンパイル・実行してモデルと突き合わせただけ（それでも「貼ったコードが動かない」
   よりは強い）。
7. **`ex10_halfpel` の端数レーン**。`n_lanes` が8の倍数でない場合、末尾は書かない（契約）。C 側が
   端数を期待するなら別途スカラで処理が要る。
8. **アラインメント契約**: どちらのカーネルも 16 バイト整列のポインタを前提にしている（128bit アクセスは
   下位4bitを落とす、TRM p49 = 実機で再現済み）。非整列を渡した場合の結果は未検証（というより
   静かに隣を読む）。
9. **`ex10_halfpel` の入力の型**: 参照式が `int16_t` を仮定している（`a[i]+b[i]+1` が int で計算され、
   16bit に切り詰められてから `>> 1`）。8bit サンプルをゼロ拡張で載せる前提で書いている。
