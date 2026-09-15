# ex24 (proposed) — MP3 ダウンサンプル前の 32 タップ Q14 FIR（`mp3_decode.c:60-72`）を PIE 化できるか

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex24_dsfir.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 6 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `b99b4b4d7ff877116ea3d2b659776ba2` → `2c910575e3abc1cb1df41f19df225f39`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex24_dsfir.S` の 3 本（`ex24_dsfir32` / `ex24_dsfir32_run` /
`ex24_dsfir32_sym`）＋ 1 本の観測用プローブ（`ex24_funnel_probe`）の設計・契約・証拠を 1 枚にまとめた
ものです。**実機は使っていません**（親セッションが `/dev/ttyACM0` を握っているため）。`proposed/` に
あるのでビルドにも入っていません（`examples/firmware/main/CMakeLists.txt` の SRCS に無い）。

対象は **cardputer-adv-pocketjs** の MP3 再生経路:

| 何 | どこ |
|---|---|
| フィルタ本体（32 タップの積和） | `/workspace/pjs-vm/main/pocket/mp3_decode.c:60-72`（`/workspace/cardputer-adv-pocketjs/main/pocket/mp3_decode.c` と **バイト同一**、md5 `6bb09f20…`。両方読んで確認） |
| 係数の生成（窓付き sinc） | 同 `:27-45`（`filter_init`） |
| 44.1/48 kHz でだけ掛かる条件 | 同 `:54` `if(h.rate>24000) filter_init(...)` と `:63` `if(h.rate>24000){...}` → **32 kHz でも掛かる**（`rates[]={44100,48000,32000}`、`:6`） |
| ステレオのダウンミックス | 同 `:61-62`（FIR の**前**。`sample=(sample+d->pcm[i*2+1])/2`） |
| リングバッファと係数の置き場 | `pocket_mp3_decoder_t`（`main/pocket/mp3_decode.h:31-32`）: `int16_t history[32], filter[32];` |
| 走っているタスク | `main/pocket/mp3_feed.c:137` `xTaskCreate(worker,"mp3dec",MP3_STACK,w,6,NULL)` → **優先度 6**（`:11` スタック 24 KiB）。`worker` は `:72-96` の 1 フレーム 1 周ループで、`:95` に `vTaskDelay(1)`（MP3 は 1152 サンプル/フレーム、44.1 kHz で 26.1 ms 周期） |
| 出口（emit） | 同 `:46-66` `output()`。スロット待ちの `vTaskDelay` は `:82/:84` で `blocked_us` として差し引かれる |

---

## 0. この文書の検証段

| 層 | 何をしたか | 結果 |
|---|---|---|
| 係数の実数 | `filter_init` をホスト `cc -O2` で丸ごと複製して実行（`proposed/ex24_tables.c`、float32 + `sinf/cosf/lroundf` そのまま） | **32/44.1/48 kHz の 3 表を数値で取得**。補正は 32k: 0、44.1k: **+2**、48k: 0（§1） |
| アセンブラ | `xtensa-esp32s3-elf-gcc -c`（ESP-IDF v6.0.1 の xtensa-esp-elf 15.2.0） | 通る。`ex24_dsfir32` **73 B / 24 命令**、`ex24_dsfir32_run` 143 B / **ループ本体 33 命令/サンプル**、`ex24_dsfir32_sym` 102 B / **35 命令**、`ex24_funnel_probe` 40 B / 14 命令（objdump 実測） |
| ABI 検査 | リポジトリ付属の `tools/check_abi.py`（`call8` 被呼び出しの a10..a15 = 呼び出し側の a2..a7 規則） | `ex24_dsfir32` / `ex24_funnel_probe` / `ex24_dsfir32_sym` は **0 件**。`ex24_dsfir32_run` の 3 件は検査器側の偽陽性（§7.4 に 5 命令の反例つき） |
| Python モデル | `/workspace/pjs-vm/tools/pie/piesim.py` のコピーに 11 命令 + コア命令を足し（`proposed/patch_piesim.py`）、**`.S` の本文をそのまま食わせて解釈実行** | T1: 1800 窓（3 表 × 600、レーン位相 o=0..7 を各 225）で **シフト意味論と 0 不一致**、`sum/16384` とは **689 不一致**（差は全て floor/trunc、§4）。T3: 512 サンプル × 3 表で **0 不一致**（リング進行込み、33.04 命令/サンプル）。T4: 折り返し版は **対が溢れない窓では 0 不一致**、溢れる窓（正弦波の 68-72%）では全滅（§6） |
| ホスト C 参照 | `proposed/ex24_ref.c`（`cc -O2`）。モデルが吐いた 120 ケース＋3 本の全リング走行をスカラ C で再計算 | **CASE 120 件: trunc/floor/カーネル値すべて 0 不一致。デバイス順の和とカーネル順の和も 0 不一致。RUN 192 サンプル: シフト値と 0 不一致、trunc とは 90 サンプル差** |
| スカラ側の実測（命令数） | 実物の `mp3_decode.c` を `xtensa-esp32s3-elf-gcc -O2 -c`（読むだけ。木は触っていない） | FIR のタップループ = **8 命令 × 32 = 256 命令/サンプル**（`0x375-0x38b`、`loop` ゼロオーバーヘッド）、`sum/16384` の切り捨て補正に **+4 命令/サンプル**（`:39b-3b5`）。関数全体 284 命令 vs `>>14` 版 280 命令（実測差 4） |

**未確認は §9 に全部並べました。実機のサイクル数は 1 つもありません。**

---

## 1. 実装事実の確定（file:line と実数）

### 1.1 ループとリングの歩み

```
mp3_decode.c:64   d->history[d->cursor]=(int16_t)sample;
mp3_decode.c:65   int32_t sum=0;
mp3_decode.c:66   for(unsigned k=0;k<32;k++)
mp3_decode.c:67       sum+=(int32_t)d->history[(d->cursor-k)&31]*d->filter[k];
mp3_decode.c:68   d->cursor=(d->cursor+1)&31;
mp3_decode.c:69   sample=sum/16384;
mp3_decode.c:70-71 if(sample>32767) sample=32767; if(sample< -32768) sample= -32768;
```

- リングは **32**（`:64` が書いて `:68` が 1 進め、`:67` が `& 31` で読む）。`cursor` は「次に書く位置」で、
  タップ k は `history[(cursor-k)&31]`、つまり **メモリ上の並びは時間の降順**（k が増えるほど古い）。
- **1 サンプルあたりの積和は 32**（`:67` のループが 32 周）。ステレオでも 32（ダウンミックスは `:62` で
  済んでおり、FIR はモノラル 1 本にしか掛からない）。
- ダウンミックスは **FIR の前**（`:62`）。`history` に入るのは `(L+R)/2` であって L/R ではない。
- PIE 化の障害は 2 つ: **(a)** `& 31` が窓の連続性を壊す、**(b)** 窓の開始はどのサンプルでも 16bit 単位で
  任意位置（2 サンプルに 1 回しか 8 レーン境界に乗らない）。

### 1.2 係数（実数。`proposed/ex24_tables.c` の出力）

```
44100 Hz (cutoff 10800/44100 = 0.244898), 生の総和 16382 → 補正 +2 を filter[15] に:
  -26,-10,43,25,-93,-64,185,142,-336,-287,579,571,-1038,-1277,2524,7255,
  7253,2524,-1277,-1038,571,579,-287,-336,142,185,-64,-93,25,43,-10,-26
  filter[15]=7255, filter[16]=7253        ← 中心対だけ非対称（差 = 2 = 補正そのもの）
  対称性 filter[i]==filter[31-i]: 不一致は i=15,16 の 2 個だけ。/補正前の生表は完全対称（0 個）。
  総和 16384（厳密）。L1 = Σ|filter| = 28908。
48000 Hz (cutoff 0.225), 生の総和 16384 → 補正 0:
  2,32,11,-63,-53,112,158,-151,-361,123,701,75,-1286,-751,2897,6746,
  6746,2897,-751,-1286,75,701,123,-361,-151,158,112,-53,-63,11,32,2
  対称性: 最終表も生表も 0 不一致（完全対称）。L1 = 27044。
32000 Hz (cutoff 0.3375), 生の総和 16384 → 補正 0: 同じく完全対称、L1 = 27832。
```

**つまり「厳密な対称が崩れている」のは 44.1 kHz だけで、崩し方は `filter[15] += (16384 - 生の総和)` =
+2 の一箇所**。48 kHz と 32 kHz は `filter_init` の丸めがたまたま合って補正 0（＝完全対称）。中心対の値は
`filter[16]` = 7253（44.1k）/ 6746（48k）/ 9066（32k）で、これは補正前の生表の値そのもの。

### 1.3 40bit ACCX に足りるか（最大絶対値の式）

`EE.VMULAS.S16.ACCX`（TRM **p210**）は 8 レーンの 16×16 積を 40bit の ACCX に**飽和加算**する
（Operation 手順 5-7: `sum[40:0] = ACCX + add0..add7`、`ACCX = min(max(sum, -2^39), 2^39-1)`）。
32 タップ = 4 本。入力が取り得る最悪値は

```
|ACCX|max = 32768 * Σ|filter[i]| = 32768 * L1
  44.1 kHz: 32768 * 28908 =   947,257,344      (2^39-1 = 549,755,813,887)
  48.0 kHz: 32768 * 27044 =   886,177,792
  32.0 kHz: 32768 * 27832 =   911,998,976
```

**ACCX は最悪値の 580 倍あり、どんな入力でも飽和しない**（モデルでも 0 件）。ついでに int32 の範囲
（2,147,483,647）にも収まる — つまり 40bit レーンはこのカーネルが必要としているものではなく、
命令が持っているもの。出口の int16 クランプは別の話で、`|sum|max/2^14 = L1/16384 = 1.699〜1.764 ×
32768` なので**到達する**（モデル: 1800 窓中 472 件で発動）。だからクランプは C と同一に書く必要がある
（`min`/`max`、Xtensa コア命令。GCC の `-O2` も同じ 2 命令を出す — §7）。

---

## 2. リングを「連続窓」にする設計（インデックスの数式まで）

### 2.1 案

`history[32]`（64 B、`&31` で折り返し）を、**64 サンプル分を二重に書いた 128 レーンのリング**（256 B）に
置き換える。不変条件は

```
ring[j] = x[ n - ((n - j) mod 64) ]        for j = 0..127
維持は 1 サンプルにつき 2 ストア:
    ring[n mod 64]          = x[n];
    ring[64 + (n mod 64)]   = x[n];        ← 2 枚目のコピー。コピーもシフトもしない
```

出力 n の 32 タップは

```
m = (n - 31) mod 64        窓 = ring[m .. m+31]      ← 常に連続 32 レーン
   i = 0..31 で  ring[m+i] = x[n-31+i]      （m+i が 64 以上なら ring[j-64] の同値コピーが答える）
```

`m = (n-31) mod 64 ∈ [0,63]` なので `m..m+31 ⊆ [0,94] ⊂ [0,127]` — **二重書きがあれば折り返しは
存在しない**。タップ順はこの窓の**昇順メモリ＝時間の昇順**なので、k（時間の降順）と一致させるには
**係数表を 1 回だけ逆順に用意**する（`filter_rev[i] = filter[31-i]`、32 レーンの反転 1 回。1 サンプル
あたりのコストは 0）。カーネルの契約はこれで `out = Σ window32[i]*filter_rev[i]` になる。

### 2.2 変更量（既存コードへの）

| 箇所 | 変更 | 量 |
|---|---|---|
| `mp3_decode.h:31` | `int16_t history[32]` → 128 レーンのリング（+ `unsigned w`） | 型 1 行 |
| `mp3_decode.h:32` | `int16_t filter[32]` → **逆順表** `filter_rev[32]`（`filter_init` の最後で 1 回 `filter_rev[i]=filter[31-i]`） | `:44` の後ろに 3 行 |
| `mp3_decode.c:64-68` | リング書き込み 2 ストア＋ `w=(w+1)&63`、窓の先頭 `&ring[(w+33)&63]` | 5 行 → 6 行 |
| `mp3_decode.c:66-67` | 32 周ループを `ex24_dsfir32(窓, filter_rev, 14)` に置換 | 2 行 → 1 行 |
| `mp3_feed.c:128` | `calloc` は **4 B 整列**しか保証しない（ESP-IDF のヒープ）ので、リングは `heap_caps_aligned_alloc(16, 272, MALLOC_CAP_INTERNAL)` か、デコーダ構造体に `__attribute__((aligned(16)))` を付けた静的置き場へ | 1 行 |
| 消費側 | `ex24_dsfir32`（1 サンプル 1 呼び出し）または `ex24_dsfir32_run`（n サンプル一括）。**他に消費側はいない**（`history` は `mp3_decode.c` の中だけで使われ、`:75-81` の位相補間は FIR の出力 `sample` を見る） | — |

**16 バイト整列は成立するか** — 成立する。ただし自動では成立しない: `calloc`（`mp3_feed.c:120`）の
整列は 4 B なので、リング用の確保を `heap_caps_aligned_alloc(16, …)` に変えるか、構造体のフィールドに
`aligned(16)` を付ける必要がある。**カーネルは窓の 16 B 整列を要求しない**（USAR + SRC.Q が自分で
合わせる）が、**リングの先頭**は 16 B 整列でなければならない（128bit アクセスは下位 4bit を落とすだけ
なので、ずれていると別の場所を読む）。

### 2.3 読み出しフットプリント（実測）

モデルが読み番地を記録して測った値（`proposed/ex24_model.py` T1b、窓を +0 から +14 バイトまで
動かして 8 通り）:

```
o=0 窓が +0:  読み +0 .. +79   → 整列基から 80 バイト、窓の終わりを 16 バイト超える
o=1 窓が +2:  読み -2 .. +77   → 同じ 80 バイト、超えは 14 バイト
...
o=7 窓が +14: 読み -14 .. +65  → 同じ 80 バイト、超えは 2 バイト
```

つまり **`(窓 & ~15)` から 80 バイトが読めること**が必要（窓 64 B + ファンネルの 16 B の先読み。
先読みのうち使われるのはシフトで残る 2o バイトだけで、残りは捨てられる）。128 レーンのリングなら
最大でもレーン 95（= 190 B）までしか読まないので、**パディングは不要**。

---

## 3. カーネル A: `ex24_dsfir32`（本命）

```
int16_t ex24_dsfir32(const int16_t *window32, const int16_t *filter_rev, uint32_t shift)
  window32   : 32 個の int16、昇順時間（window32[i] = x[n-31+i]）。整列は任意（2 バイト単位）
  filter_rev : 16 B 整列、32 個。filter_rev[i] はデバイス表の filter[31-i]（呼び出し側が 1 回作る）
  shift      : 読み出しシフト（Q14 なら 14）
  返り値     : clamp16( floor(Σ window32[i]*filter_rev[i]) >> shift )
```

### 3.1 非整列窓を 4 命令のファンネルで買う

```
EE.LD.128.USAR.IP q4, a2, 0    (TRM p93)  q4 = 整列チャンク c0, SAR_BYTE = a2 & 15
addi a2, a2, 16
EE.VLD.128.IP q5, a2, 16       (p164)     c1    （a2 の下位 4bit は落ちるだけ: 整列アドレスは c1）
EE.VLD.128.IP q6, a2, 16       (p164)     c2
EE.VLD.128.IP q7, a2, 16       (p164)     c3
EE.SRC.Q q0, q4, q5            (p125)     lanes 0..7  = {c1,c0} >> (o*8 バイト)
EE.SRC.Q q1, q5, q6            (p125)     lanes 8..15
EE.SRC.Q q2, q6, q7            (p125)     lanes 16..23
EE.VLD.128.IP q4, a2, 16       (p164)     c4（窓の終わりを 16 B 超える先読み）
EE.SRC.Q q3, q7, q4            (p125)     lanes 24..31
```

`o = window32 & 15`（実運用は必ず偶数）。`EE.SRC.Q qa, qs0, qs1` は
`qa = {qs1, qs0} >> (SAR_BYTE << 3)`（TRM p125 の Operation そのまま）なので、**qs0 が下位アドレス**で
あることが要る。演算順を入れ替えると `o≠0` で全部壊れる — それを測定として残したのが
`ex24_funnel_probe` で、モデル出力（§7）は

```
o=0: ファンネル=[100..107]（正しい） 素の VLD=[100..107]      交差=[108..115]
o=1: ファンネル=正しい              素の VLD=[0,100..106]    交差=[108..114,0]
...
o=7: ファンネル=正しい              素の VLD=[0,..,0,100]    交差=[108,0,..,0]
```

**素の `EE.VLD.128.IP` と交差オペランドは o=0 でしか一致しない**（8 通り中それぞれ 1 通り）。これが
ex03 が踏んだ罠（`docs` の「42/49 一致」）と、本カーネルのオペランド順の両方を同時に押さえている。

### 3.2 32 タップ = 4 本の MAC（融合ロード付き）

```
EE.VLD.128.IP q4, a3, 16              filter_rev[0..7]      (a3 := +16)
EE.ZERO.ACCX                          (p298)
EE.VMULAS.S16.ACCX.LD.IP q5, a3, 16, q0, q4   ACCX += w0*f0,  q5 = filter_rev[8..15]   (p211)
EE.VMULAS.S16.ACCX.LD.IP q6, a3, 16, q1, q5   ACCX += w1*f1,  q6 = filter_rev[16..23]
EE.VMULAS.S16.ACCX.LD.IP q7, a3, 16, q2, q6   ACCX += w2*f2,  q7 = filter_rev[24..31]
EE.VMULAS.S16.ACCX q3, q7                     ACCX += w3*f3
EE.SRS.ACCX a2, a12, 0                (p134) au = sat32(ACCX >> shift)  ← 算術シフト（floor）
min a2, a2, a7 / max a2, a2, a6       int16 クランプ（C の 2 つの if と同じ値）
retw.n
```

**融合形 `.LD.IP` は「先に累積、後でロード」**（TRM p211 の手順 1-6 → 8-9）なので、ロード先を累積の
相手と同じレジスタにしても壊れない（ex17 の `VSMULAS…QACC.LD.INCP` と同じ使い方）。係数 4 チャンクが
1 ロード+3 融合+1 素、で 5 命令に畳まれる。

### 3.3 objdump（実物）

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex24.o examples/firmware/main/proposed/ex24_dsfir.S
（出力なし・終了ステータス 0）
$ xtensa-esp32s3-elf-nm -S /tmp/ex24.o
00000000 00000049 T ex24_dsfir32          (73 B,  24 命令)
0000004c 00000028 T ex24_funnel_probe     (40 B,  14 命令)
00000074 0000008f T ex24_dsfir32_run      (143 B, 51 命令 = 前後 18 + 33×n)
00000104 00000066 T ex24_dsfir32_sym      (102 B, 35 命令)

00000000 <ex24_dsfir32>:
   0: 004136      entry   a1, 32
   3: 04cd        mov.n   a12, a4
   5: 000071      l32r    a7, …            32767
   8: 000061      l32r    a6, …            -32768
   b: a10024      ee.ld.128.usar.ip q4, a2, 0
   e: 10c222      addi    a2, a2, 16
  11: a38124      ee.vld.128.ip   q5, a2, 16
  14: b30124      ee.vld.128.ip   q6, a2, 16
  17: b38124      ee.vld.128.ip   q7, a2, 16
  1a: ecc304      ee.src.q        q0, q4, q5
  1d: fc5314      ee.src.q        q1, q5, q6
  20: fce324      ee.src.q        q2, q6, q7
  23: a30124      ee.vld.128.ip   q4, a2, 16
  26: ec7334      ee.src.q        q3, q7, q4
  29: a30134      ee.vld.128.ip   q4, a3, 16
  2c: 250804      ee.zero.accx
  2f: f208213e    ee.vmulas.s16.accx.ld.ip q5, a3, 16, q0, q4
  33: f380613e    ee.vmulas.s16.accx.ld.ip q6, a3, 16, q1, q5
  37: f308b13e    ee.vmulas.s16.accx.ld.ip q7, a3, 16, q2, q6
  3b: 1a5b84      ee.vmulas.s16.accx     q3, q7
  3e: 7e12c4      ee.srs.accx    a2, a12, 0
  41: 432270      min     a2, a2, a7
  44: 532260      max     a2, a2, a6
  47: f01d        retw.n
```

**24 命令/呼び出し**（モデルの実行数も 24 で一致）。うち PIE 命令 8 本（USAR 1 + VLD 5 + SRC.Q 4 +
VMULAS 5 + ZERO.ACCX 1 + SRS.ACCX 1 = 17 —— 正確には VLD 5・SRC.Q 4・VMULAS 4+MULAS 1・USAR 1・
ZERO.ACCX 1・SRS.ACCX 1 で 17 本、残り 7 本がコア命令＋retw.n）。

### 3.4 1 サンプルの値段（命令数であり、サイクルではない）

| 綴り | 命令/サンプル | 備考 |
|---|---|---|
| **ex24_dsfir32（本命）** | **24** | 32 MAC を 4 命令、窓を 9 命令、係数 5 命令、クランプ 2 命令、前後 6 命令 |
| `ex24_dsfir32_run`（一括） | **33**（＋呼び出し前後 18 命令） | 上の 24 から「引数設定 3 ＋ `callx8` 2 ＋ 戻り 1」を外し、リング書き 4 ＋ インデックス 2 ＋ 入出力 4 を足した形 |
| スカラ C（実物 `-O2`、`0x375-0x38b`） | **256**（タップループのみ）＋ 約 28（リング書き 2・カーソル 5・`/16384` の切り捨て補正 9・ループ前後 12） | `mula.aa.ll` の 32bit 積和 1 命令に l16ui 2 本・extui/addx2/add.n 3 本が付く = **8 命令/タップ** |
| 比 | 約 **12 倍** | |

44.1 kHz での割合（**命令数ベース。サイクルは未計測**）: スカラ ≈ 284 × 44100 = 12.5 M 命令/s
（240 MHz の 5.2%）、PIE ≈ 24 × 44100 = 1.06 M 命令/s（0.44%）。このプロジェクトの実測コストモデル
（`notes/cardputer-adv-project/pie-cost-model.md` §3.5）の「下限 = 命令数 + 0.6×ストア数 + ストール、
実動作は下限の 1.3〜1.4 倍」を当てると PIE は **30〜42 サイクル/サンプル ≒ 0.55〜0.8%**。

---

## 4. ビット一致の条件（`sum/16384` vs `EE.SRS.ACCX`）

**結論: `EE.SRS.ACCX` は算術シフト（floor）で、C の `sum/16384` はゼロ方向への切り捨てなので、
`sum < 0` かつ `sum % 16384 != 0` のときだけ **-1 ずれる**（PIE が 1 低い）。それ以外は完全一致。**

モデルの実測（3 表 × 600 窓 = 1800 窓、レーン位相 8 通り全部）:

```
vs C の除算 sum/16384 (trunc) : 689 窓が不一致
vs 算術シフト sum >> 14       :   0 窓が不一致
最初の不一致: 32000 Hz o=0 sum=-174960329 floor=-10679 trunc=-10678 kernel=-10679 want=-10678
sum < 0 かつ sum % 2^14 != 0 の窓: 701 / 1800 (38.9%)
   （701 > 689 なのは、クランプ域に入った窓（472 件）では差が消えるため）
int16 クランプが発動した窓: 472 / 1800
```

ホスト C 側でも同じ（§7）: 192 サンプルの全リング走行で「シフト意味論と 0 不一致、trunc とは 90 サンプル差」。

**揃える方法は 2 つ。どちらを選ぶかは親セッションの判断**（この文書は `mp3_decode.c` を書き換えない）:

1. **C を `sum>>14` に変える**（推奨）。差が消えるだけでなく、C 側が **4 命令/サンプル安くなる**
   （実測: 関数全体 284 → 280 命令。`sum/16384` は `rsr.acclo` ×2 + `blt` ×2 + `l32r` ×3 + `add.n` +
   `movgez` + `srai` の 9 命令、`>>14` は `srai` 1 命令 + `max`/`min` 2 命令）。
   ただし**音声の値は 1 LSB 変わる**（負のサンプルで 44.1 kHz の 39% が 1 だけ下がる）。
2. **カーネル側で trunc を再現する**（C を触らない場合）。`x = ACCX` を int32 で取り出してから
   `t = (x + ((x>>31) & 16383)) >> 14` を作る綴りで **+4 命令/サンプル**（24 → 28）:
   `EE.SRS.ACCX a_full, a_zero, 0` → `srai a_s, a_full, 31` → `extui a_s, a_s, 0, 14` →
   `add a_full, a_full, a_s` → `sra a2, a_full, a_shift`（シフトがレジスタ値なら `sra` は可変シフト）。
   `|sum| ≤ 947,257,344 < 2^31` なので 1 命令目のクリップ（SRS.ACCX の int32 飽和）は噛まない。
   なお**ACCX の中にバイアスを入れる形（VMULAS 1 本）は取れない**: バイアスは和の**符号**に依存し、
   符号は和が確定するまで分からない（`EE.LD/ST.ACCX.IP`・`SRS.ACCX` 以外に ACCX を触る命令が無い）。

---

## 5. カーネル C: `ex24_dsfir32_run`（n サンプル一括）

```
int16_t *ex24_dsfir32_run(const int16_t *in, int16_t *out, uint32_t n,
                          const int16_t *filter_rev, int16_t *state)
  state: 16 B 整列、136 int16（272 B）
      state[0] = w（書き込み位置 0..63、呼び出し側がリングをゼロ埋めして 0 で渡す）
      state[1] = 読み出しシフト（14）
      state[2..7] = 予約、state[8..135] = 128 レーンのリング
  返り値: out + n
```

1 サンプルあたり **33 命令**（objdump 実測: ループ本体 33 命令。モデルの実行数も 16914/512 = 33.04）。
中身は A の 15 命令 ＋ リング書き 2 ストアとアドレス 4 命令（`addx2` 2 本・`extui`/`addi`）＋
入力ロード 2 ＋ 出力ストア 2 ＋ ループ制御 2。

**係数ポインタの巻き戻しは融合ロードの負の即値で買う**: `EE.VMULAS.S16.ACCX.LD.IP` の即値は
`-512..496`（16 バイト単位）なので、3 本目の融合ロードに `-48` を置くと、読み終わった時点で
ポインタが表の先頭に戻る（実測: `ee.vmulas.s16.accx.ld.ip q7, a5, -48, q2, q6` に符号化される）。
「表の先頭を別レジスタに保持して毎サンプル `mov` で戻す」綴りが 1 命令/サンプル消える。

**不要と結論するか** — **A を毎サンプル呼ぶ形とほぼ同じ値段**で、しかも呼び出し側のループ（位相補間と
`emit`、`mp3_decode.c:75-81`）は置き換えられない。A＋呼び出し側の見積り: A の 24 ＋ 窓ポインタ 3 ＋
リング書き 4 ＋ カーソル 2 ＋ 引数設定 3 ＋ `callx8` 2 = **38 命令/サンプル**なので、run 版（33）は
1 割ほど軽いが、その差は周辺の付け替え次第で消える（`-mlongcalls` の有無、レジスタ割り当て）。
**この 1 本の存在理由は「n サンプルまとめて 1 呼び出し」というサイクル測定の単位**であること — 実機で
FIR の時間を測る親セッションにとって、`rsr.ccount` で囲める塊がこれ 1 つあれば足りる（§8）。

モデル検証（T3）: 512 サンプル × 3 表で、**デバイスの 32 深リングと同一出力（シフト意味論で 0 不一致）**。
リングの折り返し・二重書き・窓インデックスの代数が端から端まで通っていることの証拠。

---

## 6. カーネル B: `ex24_dsfir32_sym`（対称折り返し）— **書いたが、使えない**

```
int16_t ex24_dsfir32_sym(const int16_t *history32, const int16_t *filter_half,
                         int16_t mid_coef, uint32_t shift)
  history32   : 16 B 整列、64 int16。[0..31] = 窓（昇順時間）、[32..63] = 鏡像窓（lane i = 窓[31-i]）
  filter_half : 16 B 整列、16 int16 = 折り返し係数 [filter[0..14], filter[16]]
  mid_coef    : 中心タップの**補正量**（filter[15]-filter[16]。実測 44.1k: +2、48k/32k: 0）
  shift       : 読み出しシフト
```

### 6.1 折り返しの恒等式（厳密）

`c[i] = filter[31-i]` として `Σ w[i]*c[i]` を対 (k, 31-k) でまとめると

```
Σ_{i=0..31} w[i]c[i] = Σ_{k=0..14} (w[k]+w[31-k])·filter[k]
                     + (w[15]+w[16])·filter[16]
                     + w[16]·(filter[15]-filter[16])
```

（k ≤ 14 は `filter[31-k] == filter[k]` が実測で厳密に成立。k=15 だけ補正ぶんが乗る。§1.2 の表）
この式のままなら fold 版の表は **半分（16 値＋mid_coef）で足りる**。

### 6.2 まず、折り返しは PIE では**速くならない**

- MAC は 32 個 → 16 個に減るが、`EE.VMULAS.S16.ACCX` は 1 命令 8 MAC なので **4 命令 → 2 命令**。
  その代わり対の和 `w[k]+w[31-k]` を作る `EE.VADDS.S16`（TRM p146）が 2 命令要る。**4 → 4 で差引ゼロ。**
- 対の和を作るには 32 レーンの**鏡像置換**が必要で、220 命令の PIE に**レーン反転命令が無い**
  （レーンを並べ替える命令は `EE.VZIP.16` p293 / `EE.VUNZIP.16` p290 だけで、どちらも偶奇分割。
  `EE.BITREV` p77 は FFT 用のワード内ビット反転）。だから鏡像は**逆向きに書く 2 枚目のリング**
  （`ring_B[(C-n) mod 64] = x[n]` は昇順アドレスが時間の**降順**になる）で買うしかなく、それは
  1 サンプル 2 ストア＋256 B の追加。呼び出し側のコストも倍近い。

### 6.3 それ以上に、**int16 に収まらない**

`w[k]+w[31-k]` は |w| ≤ 32768 のとき **±65536** に届く。int16 のレーンは 17 bit を運べず、
`EE.VADDS.S16` は飽和加算（TRM p146、`min(max(...))`）。つまり **折り返し版は原理的にビット一致しない**。
モデルの実測（3 表 × 400 窓、レーン位相 8 通り）:

```
|w| ≤ 14000（どの対も溢れ得ない）: 134 窓, 溢れた対 0, スカラと不一致 0   ← 算術そのものは正しい
フルスケール乱数               : 133 窓, 溢れた対あり 133, 不一致 133
1 kHz フルスケール正弦         : 133 窓, 溢れた対あり 90-96, 不一致 90-96（68-72%）
溢れた窓での |カーネル - スカラ| 最大: 15666（丸めではなく桁の話）
命令数: 35/呼び出し（A は 24。MAC は 4 → 3 命令になるが、2 枚目の窓を取りに行く 10 命令が乗る）
```

**扱い（要求への回答）**: `filter[15]` の補正（44.1 kHz で +2）は `mid_coef` として
`w[16]*mid_coef` の形で **40bit ACCX の中に**足す綴りにした（`extui` + `ZERO.Q` + `MOVI.32.Q` +
`VLDBC.16` + `VMULAS` = 5 命令）。これは**正しく効いている**（|w| ≤ 14000 の 134 窓で 0 不一致、
mid_coef=0 にするとフルスケールで最悪 19699 ずれる）。**それでも折り返し版は使えない** — 理由は (1) 対の和が int16 に
入らない（実オーディオの 68-72% で溢れる）、(2) 速くならない（4 → 4 命令）、(3) 鏡像リングが要る。
**使うのは A。**

---

## 7. 証拠（実コマンドと実出力）

### 7.1 アセンブラと objdump

§3.3 のとおり。`xtensa-esp32s3-elf-nm -S` のサイズと objdump の命令数、モデルの実行命令数
（A 24 / run 33.04 / sym 35）が 3 者で一致している。

### 7.2 モデル（`.S` の本文をそのまま解釈実行）

`python3 proposed/ex24_model.py`（`proposed/patch_piesim.py` が `piesim.py` のコピーに足す 11 命令:
`EE.LD.128.USAR.IP` p93・`EE.SRC.Q` p125・`EE.ZERO.ACCX` p298・`EE.VMULAS.S16.ACCX` p210・
`EE.VMULAS.S16.ACCX.LD.IP` p211・`EE.SRS.ACCX` p134・`EE.VLDBC.16` p170・`EE.ZERO.Q` p299・
`EE.MOVI.32.Q` p119・`EE.MOVI.32.A` p118、＋ コア命令 `addx2`/`extui`/`movi`/`l32r`/`l16si`/`l16ui`/
`s16i`/`s32i`/`l32i`/`min`/`max`/`beqz`/`entry`/`retw.n`。各ハンドラは
`data/pie_instructions.json` の Operation 疑似コードから書いた）:

```
T0 係数: 32 kHz 補正 0 / 44.1 kHz 補正 +2 / 48 kHz 補正 0。L1 27832 / 28908 / 27044。
        ACCX 上限 911,998,976 / 947,257,344 / 886,177,792（2^39-1 = 549,755,813,887）。
T1 ex24_dsfir32 vs mp3_decode.c:64-71、1800 窓（o=0..7 各 225）
   vs 除算 (trunc): 689 不一致      vs 算術シフト: 0 不一致
   最初の不一致 32000 Hz o=0: sum=-174960329 floor=-10679 trunc=-10678 kernel=-10679
   クランプ発動 472/1800、sum<0 かつ端数あり 701/1800、命令数 24/呼び出し
T1b 読み出しフットプリント: 8 通りすべてで「整列基から 80 バイト」（窓 64 + 先読み 16）
T2 ファンネル: 8/8 で正しい。素の VLD と交差オペランドは o=0 だけで一致
T3 ex24_dsfir32_run: 512 サンプル × 3 表、シフト意味論と 0 不一致（= リング代数が正しい）
   16914 命令/呼び出し = 33.04/サンプル、state[0] の w も正しく戻る
T4 ex24_dsfir32_sym: |w|≤14000 で 0 不一致（134 窓）/ フルスケールで全滅（§6.3）、35 命令/呼び出し
```

### 7.3 ホスト C 参照（第 3 の意見）

```
$ cc -O2 -o /tmp/ex24_ref proposed/ex24_ref.c && /tmp/ex24_ref /tmp/ex24_cases.txt
ex24_ref: the host C scalar loop vs the Python model's case dump (and the kernel dumps)
  CASE cases                  : 120
    dumped trunc value wrong  : 0
    dumped floor value wrong  : 0
    dumped KERNEL value wrong : 0
    device-order sum != kernel-order sum : 0
  RUN whole-ring runs         : 3
    dumped samples != the C loop's trunc value : 90
    dumped samples != the C loop's shift value : 0
```

「デバイス順の和 == カーネル順の和」が 120 件で 0 なのが、**係数を逆順に持つ契約**の正当化。

### 7.4 ABI 検査（`tools/check_abi.py`）

```
$ .venv/bin/python tools/check_abi.py examples/firmware/main/proposed/ex24_dsfir.S
summary: 1 files, 4 functions, 126 instructions, 3 violations, 0 warnings
  FAIL  ex24_dsfir.S:193 ex24_dsfir32_run: writes a10 (the caller's a2) but never reloads it
        (spilled at frame offset [0], no reload after instruction 51)
  FAIL  ex24_dsfir.S:195 ex24_dsfir32_run: writes a11 ... (offset [4], no reload after 50)
  FAIL  ex24_dsfir.S:186 ex24_dsfir32_run: writes a12 ... (offset [8], no reload after 49)
```

`ex24_dsfir32` / `ex24_funnel_probe` / `ex24_dsfir32_sym` は **0 件**（a2..a9 だけで書いてある）。
`ex24_dsfir32_run` の 3 件は**検査器側の偽陽性**で、反例はこれだけ:

```
$ cat /tmp/abimin.S            # 教科書どおりの spill→use→reload→retw
abimin:
    entry a1, 32
    s32i a10, a1, 0
    mov a10, a3
    l32i a10, a1, 0
    retw.n

$ .venv/bin/python tools/check_abi.py /tmp/abimin.S
  FAIL  abimin.S:8 abimin: writes a10 (the caller's a2) but never reloads it
        (spilled at frame offset [0], no reload after instruction 3)
```

原因は `l32i` が**書き込み**として数えられること（`data/pie_instructions.json` の疑似コードの代入先が
`aT`）。すると「最後の書き込み」がリロード自身になり、その後にリロードを要求し続ける＝**この規則は
リロードで終わる関数では満たしようがない**。run 版は a10..a12 をフレームに退避して `retw.n` の直前に
戻しており、機能的には規則の意図どおり（呼び出し側の a2..a7 は壊さない）。**`tools/check_abi.py` は
この文書では触っていない**（新規ファイルは `proposed/` だけ、という縛りのため）。修正は 1 行
（`reloads` の条件からリロード自身を除く、または `l32i` を writes から外す）で済む種類のものです。

### 7.5 スカラ側の実物（読むだけ）

```
$ xtensa-esp32s3-elf-gcc -O2 -c -o /tmp/mp3_decode.o main/pocket/mp3_decode.c \
      -I main/pocket -I .cache/codecs/minimp3
$ xtensa-esp32s3-elf-objdump -d /tmp/mp3_decode.o     （pocket_mp3_decode のみ抜粋）

 332: 009922    l16si  a2, a9, 0          sample = d->pcm[i*ch]
 338: 142966    bnei   a9, 2, 350          if (channels==2)
 33b..34d        （ダウンミックス 6 命令: addx4/l16si/add/extui/add/srai）
 361: 005922    s16i   a2, a9, 0          d->history[d->cursor] = sample
 369: 1310a0    wsr.acclo a10             sum = 0（32bit アキュムレータ）
 375: 148a76    loop   a10, 38d           ← 32 回。本体は下の 8 命令（ゼロオーバーヘッド）
 378: 4420f0    extui  a2, a15, 0, 5      (cursor-k)&31
 37b: 22ba      add.n  a2, a2, a11
 37d: 9022c0    addx2  a2, a2, a12
 380: 001982    l16ui  a8, a9, 0          filter[k]
 383: 001222    l16ui  a2, a2, 0          history[(cursor-k)&31]
 386: ff0b      addi.n a15, a15, -1
 388: 780284    mula.aa.ll a2, a8        sum += (int32)a2 * (uint32)a8
 38b: 992b      addi.n a9, a9, 2
 38d: 272182    l32i   a8, a1, 156
 390: 000091    l32r   a9, …              （ここから sum/16384 の切り捨て補正）
 393: 881b      addi.n a8, a8, 1
 395: 448080    extui  a8, a8, 0, 5       d->cursor = (cursor+1)&31
 398: 266482    s32i   a8, a4, 152
 39b: 031080    rsr.acclo a8
 39e: 162987    blt    a9, a8, 3b8
 3a1: 000081    l32r   a8, …
 3a4: 031090    rsr.acclo a9
 3a7: 132987    blt    a9, a8, 3be
 3aa: 000021    l32r   a2, …
 3ad: 292a      add.n  a2, a9, a2
 3af: b32990    movgez a2, a9, a9
 3b2: 212e20    srai   a2, a2, 14       ← ここで初めて 14 シフト
```

**タップループは 8 命令 × 32 = 256 命令/サンプル**、切り捨て補正が 9 命令、リングとカーソルが 7 命令。
`sum/16384` を `sum>>14` に変えたコピーを同じフラグでコンパイルすると、**関数全体が 284 → 280 命令**
（= 4 命令/サンプル安く、かつ PIE とビット一致する。§4）。

---

## 8. 測定の設計（親セッションが実機でやる分。関数名と行番号まで）

issue の「まず測る」にそのまま使える形で。**同一バイナリ内のスイッチで 3 モード**にするのが肝
（`notes/cardputer-adv-project/pie-cost-model.md` §7: 同じカーネルでもビルド間で 15% 動くので、
数 % の差は同一バイナリの A/B でしか主張できない）。

### 8.1 用意するもの

```c
/* main/pocket/mp3_decode.c の先頭付近（:19 の後ろ） */
int pocket_mp3_fir_mode = 1;      /* 0 = FIR を掛けない, 1 = 現行のスカラ, 2 = ex24_dsfir32 */
```

- **モード 0**（FIR off）: `:63` の `if(h.rate>24000)` を `if(pocket_mp3_fir_mode==1 && h.rate>24000)`
  に置き換えるだけ。**FIR の総コスト = モード1 − モード0**（他が全部同じなので差分がそのまま FIR）。
- **モード 1**（現行）: 何もしない（ベースライン）。
- **モード 2**（PIE）: `:64-71` を
  `sample=ex24_dsfir32(&d->history[(d->cursor+33)&63], d->filter_rev, 14);`
  ＋ `d->cursor=(d->cursor+1)&63;` に置換（§2.2 のリング変更込み）。
- **副作用の注意**: モード 0 だけは音が変わる（アンチエイリアスが無い）。時間の比較は「モード 0 vs 1」
  と「モード 1 vs 2」の**同じ曲・同じ位置**で行う（`:95` の `vTaskDelay(1)` があるので 1 フレーム 1 ms 以上
  空く。1 フレーム = 1152 サンプル = 26.1 ms @44.1 kHz）。

### 8.2 時間の測り方（既存の計器をそのまま使う）

既存: `mp3_feed.c:81` `start=esp_timer_get_time()` / `:83` の `pocket_mp3_decode` / `:84` `dt` /
`:89` `total_us, worst_us` / `:111-118` の `ESP_LOGI("mp3","MP3DEC packets=… mean_us=… worst_us=…")`。
**この mean_us がそのまま 3 モード分の比較表になる**（スロット待ちは `:82/:84` で除外されている）。

サイクルで見たい場合（μs より分解能が要る）:

```c
/* mp3_feed.c:81-84 を置き換え */
uint32_t c0=esp_cpu_get_cycle_count();            /* esp_cpu.h。CCOUNT はこのコアのもの */
bool ok=pocket_mp3_decode(&w->decoder,w->frame,h.bytes,output,w);
uint32_t dc=esp_cpu_get_cycle_count()-c0;         /* 1 フレーム分（26 ms なので 2^32 周回に当たらない） */
w->cycles+=dc;                                    /* ログに cycles/frame と cycles/sample を足す */
```

**FIR だけを分離したい場合**（1 ループの中の内訳が要る）は `pocket_mp3_decode` の中に 2 つの
カウンタを置く（`:60` の前で `c0`、`:72` の直後で `c1`、`c1-c0` が FIR ブロック。`if(h.rate>24000)` の
分岐 1 命令も入る。`:57` の前後で取れば minimp3 本体、`:60-82` 全体なら位相補間込み）。1 サンプル
あたり 1〜2 命令の計器なので、**「まずおおまかにどのくらいか」を見るには mode 差分のほうが正確**
（計器が測定対象を変える、という同プロジェクトの教訓がある）。

### 8.3 何が出るはずか（命令数からの予想。**サイクルは未計測**）

| 測定量 | 期待 |
|---|---|
| `dt`（1 フレーム）のモード 1 − モード 0 | スカラ FIR の実コスト。命令数では 284 × 1152 = **327k 命令/フレーム**。240 MHz で 1 命令 1 サイクルなら 1.36 ms/フレーム = 5.2% |
| モード 1 − モード 2 | 削減量。PIE は 24 × 1152 = 27.6k 命令/フレーム（1/12）。コストモデルの 1.3〜1.4 倍を掛けて **0.15〜0.2 ms/フレーム** |
| FIR の割合（デコード本体比） | 1 フレーム 26.1 ms のうち、スカラ FIR が約 1.4 ms。minimp3 本体（`mp3dec_decode_frame`）が何 ms かは未計測 — そこが issue の「デコード本体と比べて大きいのか」の答え |
| 44.1 kHz と 48 kHz の差 | 表もサンプル数も違う（48k は補正 0 = 完全対称）。両方測る |

**注意**: `rsr.ccount` / `esp_cpu_get_cycle_count()` は ISR と他タスクの時間も含む。1 フレーム
26 ms の塊で見れば十分（分解能 4 ns）。同じファイル・同じ開始位置・同じビルドで比較すること。
`docs/vm-L0-report.md` の「音声はフレーム時間を動かさない」は**この FIR を含む全体**の話なので、
モード 0/1/2 の差はその中に入っている（動かさないほど小さい、という確認にもなる）。

---

## 9. 未確認・前提（正直に）

1. **実機のサイクル数は 1 つも無い。** 本カーネルはこの文書の時点でビルドにも入っていない
   （`proposed/` は CMake の SRCS に無い）。§8 が親向けの設計。
2. サイクルの見積り（30〜42 サイクル/サンプル）は**命令数＋既存のコストモデル**からの換算であって、
   ストールを測っていない。TRM 1.7.1 の `D = max(SA - SB + 1, 0)` を当てると、このカーネルは
   `EE.VLD.128.IP`（def 段 M = 2）の直後に `EE.SRC.Q` が来る箇所が 4 つあるので +4 前後、USAR の直後にも
   1 つ。実測の 1.3〜1.4 倍はタスク切替時のコプロセッサ 3 の退避・復元込みの話なので、1 サンプル呼びでは
   むしろ重い側に出る可能性がある（`pie-cost-model.md` §3.5 の「ループ外の足場は別に数える」）。
3. **i-cache の当たり方**: 16 KB を両コアで共有（同 §7）。FIR は 73 B なので収まるが、モード 0/1/2 の
   A/B は同一バイナリで取ること。
4. **`filter_init` の値はホストの float32 で再現した。** Xtensa の `sinf`/`cosf` は libm の実装依存で、
   ESP-IDF の newlib とホスト glibc が同一の LSB を出す保証は無い。ここで測った対称性（生表が完全対称、
   補正は 44.1 kHz で +2）がそのまま実機で成り立つとは限らない。**実機で表をログに出す 1 行を足すのが
   一番安い確認**（`filter_init` の直後に `ESP_LOGI` で 32 値＋総和）。
5. `mp3dec_decode_frame`（minimp3）のコストは本セッションでは測っていない（親の担当）。
   本カーネルとの比は §8.3 の表の空欄のまま。
6. `pocket_mp3_decode` の `-O2` は ESP-IDF の実ビルドと一致しない可能性がある（プロジェクトは
   `-Og` が既定。`examples/firmware/main/CMakeLists.txt` は比較用に `-O2` を明示している）。
   実ビルドが `-Og` ならスカラ側の命令数はもっと多い（= PIE 化の見返りはもっと大きい）。
7. リングの `heap_caps_aligned_alloc(16, …)` が ESP-IDF v6.0.1 で確実に 16 B を返すことは
   このセッションでは確認していない（API の存在は既知）。代替は `__attribute__((aligned(16)))` の
   静的バッファ。

---

## 10. 付録: 再現用ファイル（すべて `examples/firmware/main/proposed/` に置いた）

| ファイル | 役割 |
|---|---|
| `ex24_dsfir.S` | 本体（A / run / B / プローブ） |
| `ex24_dsfir.md` | この文書 |
| `ex24_tables.c` | `filter_init` のホスト複製。3 レートの表・補正・L1・対称性を数値で出す |
| `ex24_model.py` | `.S` をそのまま解釈実行するモデル（T0-T4）。`/tmp/piesim_ex24.py` が無ければ `patch_piesim.py` を自分で走らせる |
| `patch_piesim.py` | `piesim.py` のコピーに 11 命令＋コア命令を足す（各ハンドラは `data/pie_instructions.json` の疑似コードから、TRM ページ付き） |
| `ex24_ref.c` | ホスト C 参照（ケースダンプの再計算＝第 3 の意見） |

```sh
# 1) 表
cc -O2 -o /tmp/ex24_tables proposed/ex24_tables.c -lm && /tmp/ex24_tables > /tmp/ex24_tables.txt
# 2) アセンブラ
xtensa-esp32s3-elf-gcc -c -o /tmp/ex24.o proposed/ex24_dsfir.S && xtensa-esp32s3-elf-objdump -d /tmp/ex24.o
# 3) モデル（piesim のコピーを自分で生成してから走る）
python3 proposed/ex24_model.py
# 4) ホスト C
cc -O2 -o /tmp/ex24_ref proposed/ex24_ref.c && /tmp/ex24_ref /tmp/ex24_cases.txt
```

数字はすべて (a) アセンブラ／objdump、(b) モデルの実行出力、(c) ホスト C の出力、(d) 実物
`mp3_decode.c` の `-O2` objdump、のいずれかから来ています。推測は「推測」と書きました。
