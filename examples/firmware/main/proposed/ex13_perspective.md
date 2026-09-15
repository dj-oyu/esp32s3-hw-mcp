# ex13 — 透視除算と逆数表（8頂点並列） / perspective divide with a Q15 reciprocal table

`examples/firmware/main/proposed/ex13_perspective.S` — **提案（proposed）。ビルドに入っていない。**
`main.c` / `examples.h` / `CMakeLists.txt` は触っていない（`proposed/` は
`examples/firmware/main/CMakeLists.txt` のコンパイル対象外）。組み込む手順は最後の節。

`notes/08-media-3d-perf.md` のパイプライン表が「**透視除算: PIE に除算は無い → 逆数表 or ニュートン法
（Q15 の近似乗算）**、未着手」としている段がこれ。頂点変換（`ex07_transform3d.S`）の出力 `w` を受け取り、
`x/w` を Q8 で出し、**レーンごとに「近平面の内側か」を 0 / 非0 のマーカーで返す**。

| カーネル | 何をするか | 1回（8頂点）あたりの命令 |
|---|---|---|
| `ex13_recip_q15` | 逆向き乗算：`inv = table[w >> 8]`、`x_over_w = (num * inv) >> 15`、`w <= eps` はマーカー | 36（`EE.*` 27 + `LD.QR` 2 + スカラ 7） |
| `ex13_clip_quad` | 8頂点の矩形内判定を **0xFFFF / 0** のレーンマスクで返す（分岐なしで三角形を落とせる） | 19（`EE.*` 15 + スカラ 4） |

姉妹サンプル: `proposed/ex14_raster.S`（スパン塗りラスタライザ。このカーネルの**後段**）。
`notes/08` の量産リストは 1 番を「ex09 透視除算＋逆数表」、5 番を「ex13 音声ミキサ」に割り当てている。
ここでは**透視除算を ex13 として書いた**（＝リスト1番の作業を ex13 の番号で実装）ので、統合する人は
どちらかを繰り下げる必要がある（`notes/08` の更新は統合する人の作業。ex10 のときと同じ衝突）。

## この文書が主張すること／しないこと（先に結論）

**主張する**: アセンブルが通ること（下に生ログ）。使った `EE.*` 命令 14 種すべての符号化が
`tools/asm_toolchain.py --check-instruction` で **manual == toolchain** であること（下に生ログ）。
命令レベルモデル（Python）と、ホスト `gcc` でコンパイルした C 参照が、576 ケース
（A: 384ケース = 3072レーン、B: 192ケース = 1536レーン）で**1レーンも食い違わない**こと。
近平面マーカーが `w <= eps` のレーン集合と**厳密に一致**すること（`w` を 0..65535 全数掃引）。
`EE.VMUL` の 16bit レーン書き込みが**決して wrap しない**こと（全 (num, entry) 空間で `|num*entry| < 2^30`
を確認）。表の相対誤差がバケット `i` で `1/(2i+1)` に収まり、絶対誤差が
`|kernel - ideal| <= 1.5 + |ideal|/(2i+1)` に収まること（実測、256バケット全部）。

**主張しない**: **実機（ESP32-S3）では一度も走らせていない。** `/dev/ttyACM0` は計測ランの持ち物で、
この提案の目的は「アセンブラ・リポジトリのツール・モデルが通る範囲で .S と参照を固める」こと。
サイクル数（`BENCH`）は 1 つも測っていない。したがって「シリコンが疑似コードどおりに動く」とは一言も
言わない — 下の「確認できていないこと」に、このカーネルが依存している未確認の前提を全部並べた。

## 設計が `notes/08` のスケッチから離れている点（重要）

`notes/08` は「Q15 の逆数表を **`EE.LD.QR` で引き**、乗算は `VMUL`」と書いている。**そのままでは
per-lane の表引きにならない**ので、この実装は表引きの手段だけ変えている:

* `LD.QR qu, as, imm` は **128bit の連続ロード**（TRM p301、`qu = load128(as + imm)`）で、アドレス
  レジスタは 1 本。**8レーンが別々のインデックスを持つ表引きは原理的にできない**（できるのは
  「8レーン共通のインデックス」のときだけ）。
* PIE で **per-lane のインデックス付きメモリアクセス**を持っているのは `EE.LDXQ.32`（TRM p113）だけ:
  `vaddr_k = as + qs[lane k] * 4` を 8本作り、即値 `sel8` で選んだ**1本だけ**を 32bit ロードし、
  レジスタ `qu` の `sel4` 番目の 32bit セグメントに書く（`qu[32*sel4+31:32*sel4] = dataIn`）。
* そのため表は **32bit エントリ**（`entry i` のアドレス = `table + 4*i`、**両方の16bitハーフに同じ Q15 値**）
  にしてあり、1レーン = 1 `LDXQ.32` で引ける。8本で 4 セグメント×2 レジスタを埋め、even/odd の
  レーンマスクで統合する（§2.3）。
* `LD.QR` は**消していない**。16バイト整列の定数ブロック（eps レーン＋レーン対マスク）を
  2命令で読むために実際に使っている（§1.2）。**「128bit QR ロードは16バイト整列」**の契約はそこに書いた。

同じ結論を「LD.QR だけで書くと何命令になるか」まで含めて §2.3 に置いた（8レーンが同一チャンクに収まる
契約でも `LD.QR` 1 + 選択網 23 = 24命令/8頂点で、採用した 11命令の約2倍）。**判断の根拠は命令数と
TRM の記述で、実測ではない。**

## 1. レイアウト契約（厳密に）

### 1.1 逆数表 `table`（呼び出し側が作る）

```
エントリ数        256
1エントリ         int32（4バイト）。アドレスは table + 4*i。i = 0..255
中身              両方の16bitハーフが同じ値:  table[i] = e | (e << 16)
                  （これが LDXQ.32 を per-lane ルックアップにする仕掛け。§2.3）
e の定義          e = clamp( (2^23 + (w_rep >> 1)) / w_rep, 1, 32767 )   ← 整数除算（floor）
                  w_rep = 256*i + 128        （バケット i の中央値。w は uint16 なので i = w >> 8）
インデックス      i = (uint16_t)w >> 8        （0..255。w は符号なしで読む）
整列              4バイト整列が必須（下位2bitは強制的に落とされる）、16バイト整列を推奨（§1.4）
```

実測した代表値（`entry` は上記の式、`2^23/w_rep` はその連続値）:

```
     i=  0  w     0..  255  w_rep   128  2^23/w_rep   65536.00  entry 32767 (the clamp)
     i=  1  w   256..  511  w_rep   384  2^23/w_rep   21845.33  entry 21845
     i=  2  w   512..  767  w_rep   640  2^23/w_rep   13107.20  entry 13107
     i=  8  w  2048.. 2303  w_rep  2176  2^23/w_rep    3855.06  entry  3855
     i= 32  w  8192.. 8447  w_rep  8320  2^23/w_rep    1008.25  entry  1008
     i=128  w 32768..33023  w_rep 32896  2^23/w_rep     255.00  entry   255
     i=255  w 65280..65535  w_rep 65408  2^23/w_rep     128.25  entry   128
```

**i = 0（w < 256）のエントリは式の値ではなく clamp 32767**（`2^23/128 = 65536` が int16 に入らない）。
つまり `w < 256` のレーンは「逆数」ではなく**飽和値**を受け取る。呼び出し側は **eps >= 256** を渡して
ここに入れない（§2.4 の近平面マーカーがそれを保証する）。

**インデックスの刻み（`>> 8`）は表のサイズとのトレードオフ**で、カーネル側の `movi a9, 8` 1命令と
表の再構築だけで変えられる:

| シフト s | エントリ数 | 表のバイト数 | 1バケットの相対誤差上限 `1/(2i+1)`, i = w>>s |
|---|---|---|---|
| 10 | 64 | 256 B | w=1024 で 33%（下限が粗い） |
| **8（採用）** | **256** | **1 KiB** | **w=256 で 33%、w>=2560 で 5% 以下、w>=65280 で 0.2%** |
| 6 | 1024 | 4 KiB | w=256 で 11%、w>=2560 で 1.3% |
| 4 | 16384 | 64 KiB | w=256 で 3%、w>=2560 で 0.6%（PSRAM 無しのボードでは非現実的） |

（この表は式から出した設計値。§2.6 の実測は s=8 のときの数字。）

### 1.2 定数ブロック `consts`（16バイト整列、32バイト）

```
+0   int16 × 8   近平面しきい値 eps を8レーンぶん（同じ値を8個。uint16 として 0..32767）
+16  int32 × 4   even レーンマスク: 4セグメントすべて 0x0000FFFF
                 （16bit レーン 0,2,4,6 が 0xFFFF。odd マスクは NOTQ で作る）
```

読み出しは `LD.QR q3, a5, 0` と `LD.QR q4, a5, 16` の**2命令だけ**（TRM p301）。
`LD.QR` の即値フィールドは `imm[3:0]`（＝実効オフセットは8の倍数、範囲 -128..112）なので、
「16」は符号化できる。**128bit QR ロードの16バイト整列は、この段では仮定ではなく契約**である:
`LD.QR` の疑似コードは `qu = load128(as + imm)` だけで、`EE.VLD.128.IP` の
`load128({as[31:4],4{0}})` のような下位4bitの強制を書いていない。他の 128bit アクセスで実機再現した
「下位4bitを落とす」規則（TRM p49、ex03）が `LD.QR` にも効くかを測った例は無い（ex04 は `LD.QR` の
**タイミング**だけを測っている）。だから**呼び出し側が16バイト整列を守る**契約にしてある。

### 1.3 入出力

```
ex13_recip_q15(const uint16_t *w, const int32_t *table, const int16_t *num_x8,
               const int16_t *consts, int16_t *inv_out, int16_t *x_over_w_out)

  w            8レーン uint16（16バイト整列）。符号なしで読む（0..65535）
  num_x8       8レーン int16（16バイト整列）。分子（すでにスケールされた x）
  inv_out      8レーン int16。Q15 の逆数。**0 = 近平面**（マーカー）
  x_over_w_out 8レーン int16。num/w の Q8。近平面レーンは 0（クランプした双曲線の裾ではない）

ex13_clip_quad(const int16_t *x8, const int16_t *y8, const int16_t *lo4, const int16_t *hi4,
               uint16_t *mask8)

  lo4          int16 × 4（2バイト整列）: lo4[0] = xmin, lo4[1] = ymin  ← 読むのはこの2つだけ
  hi4          int16 × 4（2バイト整列）: hi4[0] = xmax, hi4[1] = ymax  ← 同じく2つだけ
               lo4[2..3] / hi4[2..3] は z,w 版のための予約で、このカーネルは読まない
  mask8        uint16 × 8（16バイト整列）。0xFFFF = 矩形の内側、0 = 外側
```

**引数は6個まで**にしてある: windowed ABI のレジスタ引数は a2..a7 の6本で、
7個目以降は**呼び出し側のスタックフレーム**から読むことになる。手書きカーネルでそのオフセットを
当てにいくのは（実機が無い状態では）確認不能な賭けなので避けた。そのぶん:
* **8頂点固定**（個数引数なし）— `ex12_integrate` と同じ流儀で、呼び出し側が 8 頂点ずつ回す。
* **`shift`（SAR）は引数ではなく表のスケールに焼き込んだ** — 表の値 `e` は `2^23/w` の近似なので、
  `>> 15` で `num/w` の Q8 になる。表と SAR を別々に渡せる API は「表とスケールの取り違え」を
  表現可能にしてしまうので、この形にした（要求された signature からの唯一の意図的な変更点）。

### 1.4 整列（この ISA の 128bit / 32bit アクセス規則）

* 128bit アクセスはアドレス下位4bitを強制的に 0 にする（TRM p49、`EE.VLD.128.IP` / `EE.VST.128.IP`
  の説明文が明記、ex03 が**実機で再現**）。`inv_out` / `x_over_w_out` / `w` / `num_x8` / `x8` / `y8` /
  `mask8` / `consts` は 16バイト整列、`table` は（推奨として）16バイト整列。
* `EE.LDXQ.32` はアドレスを **32bit に整列**する（説明文: "aligns it to 32 bits (its lower 2 bits are
  set to 0)"）。したがって `table` の 4バイト整列は必須で、**破っても例外にならず隣の4バイトを読む**。
* `EE.VLDBC.16` は `load16({as[31:1],1{0}})` なので `lo4` / `hi4` は 2バイト整列で足りる。
* レーン順は「レーン j = ビット [16j, 16j+16)」。32bit セグメント s はレーン (2s, 2s+1)、
  ロードしたワードの**下位16bitがレーン 2s** に入る（リトルエンディアン）。

## 2. カーネル A — `ex13_recip_q15`

### 2.1 命令列（1呼び出し = 8頂点、分岐なし）

```
    entry a1, 32
    LD.QR q4, a5, 16                q4 = レーン対マスク（p301、16バイト整列ブロック+16）
    EE.VLD.128.IP q0, a2, 0         q0 = w の8レーン（p164）
    movi a8, 1
    EE.MOVI.32.Q q1, a8, 0..3       q1 = 「1のレーン×8」（p119）
    movi a9, 8
    ssr a9
    EE.VMUL.U16 q2, q0, q1          q2 = (w * 1) >> 8 = 表インデックス（p204、.U16 は必須）
    EE.ZERO.Q q1                    q1 をゼロベクトルに転用（p299）
    LD.QR q3, a5, 0                 q3 = eps の8レーン（p301）
    EE.LDXQ.32 q5, q2, a3, 0..3, 0/2/4/6    8本の per-lane 表引き（p113）。§2.3
    EE.LDXQ.32 q6, q2, a3, 0..3, 1/3/5/7
    EE.ANDQ q5, q5, q4              even レーンを残す（p76）
    EE.NOTQ q4, q4                  odd マスク（p120）
    EE.ANDQ q6, q6, q4
    EE.ORQ q5, q5, q6               q5[l] = entry[idx[l]]（p121）
    EE.VCMP.GT.S16 q6, q0, q3       w > eps（符号付き、p158）
    EE.VCMP.LT.S16 q7, q0, q1       w < 0 ⇔ w >= 32768（p161）
    EE.ORQ q6, q6, q7               keep = (w > eps) | (w >= 32768)  ← 符号なし比較の厳密解
    EE.ANDQ q5, q5, q6              近平面レーンの逆数を 0 に = マーカー
    EE.VLD.128.IP q7, a4, 0         分子
    movi a10, 15
    ssr a10
    EE.VMUL.S16 q7, q7, q5          q7 = (num * entry) >> 15（p198、.S16 は必須）
    EE.VST.128.IP q5, a6, 0         inv_out
    EE.VST.128.IP q7, a7, 0         x_over_w_out
    retw.n
```

**QR レジスタは q0..q7 の8本しか使えない。** PIE 命令の QR オペランドフィールドは3bitで、
マニュアルの命令語の図もそうなっている（`EE.ANDQ` p76: `qa[2:1] ... qa[0]`、`EE.VLD.128.IP` p164:
`qu[2:1] ... qu[0]`、`EE.LDXQ.32` p113: `qu[2:0]`, `qs[2]`）。アセンブラは q8 以降を
`Error: register number out of range` で拒否する。このカーネルは 8本に収まるようレジスタ配置を
組んであり、`q1`（1のレーン）はインデックス計算が終わった時点でゼロベクトルに転用している
（そうやって初めて 8本に収まる）。

### 2.2 インデックスと逆数のスケール

```
idx   = (uint16_t)w >> 8            （0..255、符号なしレーンの論理シフト = VMUL.U16 + SAR=8）
e(i)  = round(2^23 / w_rep(i))      （§1.1 の整数式）
out   = floor(num * e / 2^15)       （VMUL.S16: 32bit 積 → 算術シフト15 → 16bit レーン書き込み）
```

`e/2^15 = 2^8/w_rep` なので、`out` は `num/w` の Q8（`num/w * 256`）の近似になる。
`2^23 = 2^15 * 2^8` の「8」が Q8 の 8 である。

### 2.3 8レーンの表引き（この節が §0 の設計判断の詳細）

**なぜ `LD.QR` が使えないか（TRM の記述そのもの）**: `LD.QR` の疑似コードは `qu = load128(as + imm)`。
アドレスレジスタは 1本、即値は定数なので、**8レーンが別々のアドレスを指せない**。したがって
`LD.QR` だけで書くと「8レーンのインデックスが同一の16バイトチャンク（8エントリ）に収まる」という
**追加契約**が必要になり、さらにチャンクの8エントリからレーンごとの位置を選ぶ網
（`VCMP.EQ` 8 + `ANDQ` 8 + `ORQ` 7 = 23命令）が要る。チャンクが1つに収まる契約なら
`LD.QR` 1 + 23 = **24命令/8頂点**（採用した 11命令の約2倍）、収まらない場合は必要なチャンク数ぶんの
ロードと選択が要る（最大 8×(1+23)）。三角形の `w` は隣接頂点でも大きく離れ得るので、その契約は
この段では取れない。**数えたのは命令数で、実測ではない。**

**採用した経路（LDXQ.32 の使い方）**: エントリが「両ハーフに同じ値の int32」なので、

```
EE.LDXQ.32 q5, q2, a3, s, 2s      … q5 のセグメント s ← entry[idx[2s]]（両ハーフが同値なので、
                                       レーン 2s は正しい値、レーン 2s+1 は「ついでに」同じ値）
EE.LDXQ.32 q6, q2, a3, s, 2s+1    … q6 のセグメント s ← entry[idx[2s+1]]（レーン 2s+1 が正しい値）
q5 = (q5 & even) | (q6 & odd)     … even レーンは q5 から、odd レーンは q6 から
```

`s = 0..3` の4本ずつ、計8本で全部のレーンが埋まる。**書かれるのは各命令の 32bit セグメント1つだけ**
（疑似コード `qu[32*sel4+31:32*sel4] = dataIn`）なので、8本が4セグメント×2レジスタを**全部**書くように
並べてあり、古いデータが残るレーンは無い。`idx` は 0..255 なので、`qs` の16bitレーンを符号付き/符号なし
どちらで読んでも同じ（＝この点は未確認事項にしない）。

命令ごとの出典は §8、`LDXQ.32` の疑似コードの**誤植**（`qs[63:47]` は `qs[63:48]` のはず）は
未確認事項に挙げた。

### 2.4 近平面の扱い: `w <= eps` に何を返すか（要求 B）

**採用: マーカー**（`inv_out == 0`、`x_over_w_out == 0`）。クランプした双曲線の裾は返さない。

数値的な理由:

1. **真値 `num/w` は `w → 0+` で有界でない。** 16bit の有限値は「近い」値にならない。クランプ値は
   誤差が非有界なだけでなく、**正当な値と区別がつかない**: `w` が小さくても分子が小さい頂点は正当に
   大きな座標へ射影される。クランプは「実在する大きな座標」と「存在しない座標」を同じ表現にしてしまう。
2. **マーカーは正当な経路では作れない。** 有効なエントリは 128..21845（バケット1以降、
   モデルが 256 バケット全部で確認）なので **`inv_out == 0` は「有効な逆数ではない」以外の意味を持ち得ない**。
   呼び出し側は `EE.VCMP.EQ.S16 inv, zero` 1命令で、分岐なしに「近平面のレーン」を復元できる。
   0 はマーカーとして**衝突しない**（テーブル側で保証される）ので、下流のクリップ（`ex13_clip_quad`）と
   同じマスク合成にそのまま流せる。
3. **`x_over_w_out` を 0 にするのも同じ理由**。ここだけ 0 にすると「正当な 0」（分子 0、あるいは
   `|num/w*256| < 1` の切り捨て）と区別できない。モデルはそれを実例で示している:
   `w=65535, num=1` は**正当なレーン**で `inv=128`、**商は 0**（切り捨て）。だから
   **マーカーは必ず `inv_out` から読む**、というのがこの API の契約（§7 の出力の `A5` 行）。
4. 分類は `w` の全値で厳密: ほしいのは符号なし比較 `w > eps` だが、PIE のベクトル比較は
   `.S16/.S32/.S8` の**符号付き**しか無い（`data/pie_instructions.json` に `VCMP.U*` は無い）。
   `w >= 32768` は符号付きで負になるので単独の `VCMP.GT.S16` は**実在する頂点を近平面と誤判定する**。
   そこで `keep = (w_s > eps_s) | (w_s < 0)` の2命令にした（`eps <= 32767` が契約）。
   モデルは `w` を 0..65535 全数掃引して、マーカー集合が `{w : w <= eps}` と**厳密に一致**することを
   eps = 0, 1, 255, 256, 1024, 32767 で確認している。
   **`eps = 0` を渡すと、このテストは「ゼロ除算だけを弾く」になる**（`w = 0` がマーカー）。
5. **0 で割る場合を特別扱いしない。** `eps = 0` のとき `keep = (w > 0)` なので `w = 0` はマーカー、
   それ以外は有効。`w = 0` の「逆数」は表のインデックス 0（= clamp 32767）だが、マスクが 0 に落とすので
   その値は使われない。

**もしクランプが欲しい呼び出し側のために**: `eps = 0` を渡して `inv == 0` を無視すれば、`w >= 1` の全レーンで
`(num * e) >> 15` がそのまま返る（`w < 256` では `e` が clamp 32767 なので `num/w` ではなく `num*0.99997`
に近い値 — つまり**ゴミはゴミのまま返る**）。だから「クランプして下流の範囲判定に任せる」のは
**`ex13_clip_quad` の側**（座標の範囲テスト）でやる設計にしてある。マーカーの復元は呼び出し側で
`EE.VCMP.EQ.S16`（対象レーンが 0xFFFF）1命令と、三角形単位の集約（`ORQ`/`ANDQ`）だけで済む。

### 2.5 飽和・切り捨て: どこで結果が壊れるか（要求 A の後半）

| 命令 | 飽和するか | このカーネルでの影響 |
|---|---|---|
| `EE.VMUL.U16`（インデックス） | 疑似コードは `(qx*qy) >> SAR` を16bitレーンに代入＝**切り捨て**（飽和記述なし、p204） | `w <= 65535` かつ SAR=8 なので結果は 0..255。**切り捨ても飽和も起きない** |
| `EE.VMUL.S16`（逆数の適用） | 同じく**切り捨て**（飽和記述なし、p198） | **16bitレーンに収まるので wrap しない**（下記の証明）。負の積は算術シフト＝floor |
| `EE.ANDQ/ORQ/NOTQ` | ビット演算、飽和の概念なし | なし |
| `EE.VCMP.*.S16` | 0xFFFF / 0 を生成（p158/p161） | なし（分類は厳密） |
| `EE.LDXQ.32` | 32bit のデータ移動 | なし |
| **表の clamp（呼び出し側）** | `e` を [1, 32767] に clamp | **i = 0（w < 256）だけが影響。eps >= 256 なら到達しない** |

**wrap しない証明（モデルで確認済み）**: `|num| <= 32768`、`e <= 32767` なので
`|num * e| <= 32768 * 32767 = 1073709056 < 2^30 = 1073741824`。
`>> 15` した結果は `[-32767, 32766]` に収まり、**16bit レーンからはみ出さない**。
モデルは全256エントリについて `num ∈ {-32768, -32767, 32767}` の積と、その `>>15` を取り、積の最大が
`1073709056`（`num = -32768`、`e(0) = 32767`。これはちょうど `32767 * 2^15`）で `2^30` まで 32768 の
余裕があること、シフト後の値域が `[-32767, 32766]`（int16 の両端から1ずつ内側）であることを
確かめている（出力の `A3` 行）。**`-32768` のレーンは出ない**（出たとしても int16 の下端そのもの）。

つまり**このカーネルには飽和が 1 箇所も無い**。壊れる可能性があるのは 2 つだけ:

1. **表の clamp（i = 0）**: `eps >= 256` を守らないと、`w < 256` のレーンが「`num` そのまま」に近い
   値を逆数として受け取る（`num * (32767/32768)`）。これはマーカー（`inv == 0`）では捕まらない
   — **`eps >= 256` は契約であって、カーネルは検出しない**。
2. **商の切り捨て**: `out` は floor なので、`|num/w*256| < 1` のレーンは 0 になる（正当）。
   相対誤差は `1/(2i+1)` に収束するが、**0 に落ちるレーンの相対誤差は 100%**（絶対誤差は 1）。
   だから §2.6 の誤差は「絶対 + 相対」の2項で書いてある。

### 2.6 精度（バケット誤差の実測）

カーネルの値は `floor(num * e / 2^15)`、理想は `floor(num*256/w)`。差は
「バケットの相対誤差 `1/(2i+1)`」＋「`e` の丸め（<= 0.5/2^15）」＋「2つの floor」から来る。
主張（モデルが確認した形）:

```
|kernel - ideal| <= 1.5 + |ideal| / (2i+1),    i = w >> 8
```

実測（全256バケット × 各バケットの全256 `w` × 11個の分子、`num = -32768..32767`）:

```
 バケット i=1   (w 256..511)   : 最悪 |diff| 10923（bound 10924.17）  相対 100.00%（1/(2i+1)=33.33%）
 バケット i=3   (w 768..1023)  : 最悪 |diff|  1561（bound  1561.93）  相対  14.41%（1/(2i+1)=14.29%）
 バケット i=7   (w 1792..2047) : 最悪 |diff|   313（bound   313.63）  相対   6.69%（1/(2i+1)= 6.67%）
 バケット i=15  (w 3840..4095) : 最悪 |diff|    71（bound    71.98）  相対   3.28%（1/(2i+1)= 3.23%）
 バケット i=31  (w 7936..8191) : 最悪 |diff|    18（bound    18.29）  相対   3.12%（1/(2i+1)= 1.59%）
 バケット i=63  (w 16128..16383): 最悪 |diff|     5（bound     5.60）  相対   1.04%（1/(2i+1)= 0.79%）
 バケット i=127 (w 32512..32767): 最悪 |diff|     2（bound     2.52）  相対   1.03%（1/(2i+1)= 0.39%）
 バケット i=255 (w 65280..65535): 最悪 |diff|     1（bound     1.75）  相対   0.78%（1/(2i+1)= 0.20%）
```

読み方: **`num/w` が大きい（＝`w` が小さい）ほど相対誤差が大きい。** これは線形インデックスの表の宿命で、
`w` が小さいほど「1バケットの幅（256）が `w` に対して相対的に大きい」から。実用域（`w >= 2560`）では
相対 5% 以下、`w >= 65280` で 0.2%。もっと精度が要るなら §1.1 の表のとおりシフト `s` を下げて表を
大きくする（`s=6` なら 4 KiB で `w=256` が 33% → 11%）。

## 3. カーネル C — `ex13_clip_quad`

```
    entry a1, 32
    addi a8, a4, 2                  lo4[1] のアドレス（明示）
    addi a9, a5, 2                  hi4[1] のアドレス
    EE.VLDBC.16 q2, a4              q2 = {8{xmin}}（p170）
    EE.VLDBC.16 q3, a8              q3 = {8{ymin}}
    EE.VLDBC.16 q4, a5              q4 = {8{xmax}}
    EE.VLDBC.16 q5, a9              q5 = {8{ymax}}
    EE.VLD.128.IP q0, a2, 0         x の8レーン
    EE.VLD.128.IP q1, a3, 0         y の8レーン
    EE.VCMP.LT.S16 q6, q0, q2       x < xmin → 外側（p161）
    EE.VCMP.GT.S16 q7, q0, q4       x > xmax → 外側（p158）
    EE.ORQ q6, q6, q7
    EE.VCMP.LT.S16 q7, q1, q3       y < ymin
    EE.ORQ q6, q6, q7
    EE.VCMP.GT.S16 q7, q1, q5       y > ymax
    EE.ORQ q6, q6, q7               q6 = 外側のレーンが 0xFFFF
    EE.NOTQ q6, q6                  accept = ~reject（0xFFFF = 内側、0 = 外側）
    EE.VST.128.IP q6, a6, 0
    retw.n
```

* **1つの `NOTQ` で 4 平面ぶんの accept になる**（De Morgan: `¬a ∧ ¬b = ¬(a ∨ b)`）。
  `ANDQ` で「内側」を積む形にすると、各比較に `NOTQ` が要るので `VCMP` 4 + `NOTQ` 4 + `ANDQ` 3 = 11命令に
  なる。採用した形は 8命令（`VCMP` 4 + `ORQ` 3 + `NOTQ` 1）。**`ANDQ` はカーネル A で 3 箇所使っている**
  （even/odd の統合 2 + マーカー適用 1）ので、要求された 4 命令（`VCMP`/`ANDQ`/`ORQ`/`NOTQ`）は
  2カーネル合わせて全部登場する。
* **境界は内側**（`x == xmin` も `x == xmax` も内側）。`EE.VCMP.LT/GT` は厳密な不等号なので
  これはカーネルの性質であって仮定ではない。モデルは 20個の矩形で境界ちょうどのレーンを作って確認
  （160レーン、うち 74 が境界一致、不一致 0）。
* **分岐なしで三角形を落とす**: 3頂点のマスク 24 レーンを呼び出し側で集約すればよい
  （`vmin` = 三角形が完全に外、`vmax` = 完全に内）。カーネル自身は分岐も飽和も持たない。
* `lo4[2..3]` / `hi4[2..3]` は予約（未読）。z/w 版のテストを足すときは `EE.VLDBC.16.IP` の代わりに
  同じ明示アドレス方式で足せる。
* `EE.VLDBC.16.IP`（p171）を使わなかった理由: 疑似コードのポストインクリメントが
  `{24{0},imm2[6:0],0}`、つまり**即値の2倍**で、構文行の `0..254` の読みと食い違って読める。
  明示アドレスはスカラ2命令で、この曖昧さが消える。

## 4. スケジューリング（Table 1.7-2 の D 則）

TRM 1.7.1 の規則: 命令 A が結果を値を SA 段で用意し、命令 B が SB 段でその値を読むなら、
**A は B より `D = max(SA - SB + 1, 0)` サイクル前に発行されなければならない**。

このカーネルのペア（Table 1.7-2 の段、`data/pie_pipeline.json`）:

| 生産者 → 消費者 | SA | SB | D | 実際の間隔（命令数 k、発行間隔 = k+1） |
|---|---|---|---|---|
| `VMUL.U16 q2` → `LDXQ.32`(qs=q2) | 2 (def qz) | 1 (use qs) | 2 | k=2（`ZERO.Q` + `LD.QR`）→ 3 >= 2 ✓ |
| `LDXQ.32`(q5) → `ANDQ`(q5) | 2 (def qu) | 1 (use qx) | 2 | k=4（残り4本の LDXQ）→ 5 ✓ |
| `LDXQ.32`(q6) → `ANDQ`(q6) | 2 | 1 | 2 | k=2（`ANDQ q5` + `NOTQ`）→ 3 ✓ |
| `VLD.128.IP q7` → `VMUL.S16`(q7) | 2 | 1 | 2 | k=2（`movi` + `ssr`）→ 3 ✓ |
| **`VMUL.S16 q7` → `VST.128.IP q7`** | 2 | 1 | 2 | **k=1（`VST q5` が間に入る）→ 2 ✓** |
| `ANDQ q5` → `VST.128.IP q5` | 1 | 1 | 1 | k=4（`VLD` + `movi` + `ssr` + `VMUL`）→ 5 ✓ |
| `NOTQ q6` → `VST.128.IP q6`（カーネルC） | 1 | 1 | 1 | k=0 → 1 >= 1 ✓ |
| `VLD.128.IP q0` → `VCMP`(q0)（カーネルC） | 2 | 1 | 2 | k=1（`VLD q1`）→ 2 ✓ |
| `LD.QR`(表/eps) → 最初の消費 | Table 1.7-2 に**無い**（実測 def = M = 2、notes/07） | 1 | 2 | k >= 9 ✓ |

つまり**この並びは D 則の上ではストールしない**（等号でちょうどのペアが 3 箇所）。
`LD.QR` は Table 1.7-2 に載っていない命令で、段は ex04 の**実測**（`data/pie_timing_measured.json`）に
依っている。**サイクル数は測っていない**ので、この節は紙の上の見積もりである。
ハードウェア資源ハザード（TRM 1.7.2、16bit 乗算器が8本）については、このカーネルは同時に
2本の乗算しか出さず、`LDXQ.32` のアドレス生成は `*4`（シフト）なので乗算器を使わない、と読んでいる。
**これも未確認**（§11）。

## 5. アセンブル結果（生の出力）

```
$ xtensa-esp32s3-elf-gcc --version | head -1
xtensa-esp-elf-gcc (crosstool-NG esp-15.2.0_20251204) 15.2.0

$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex13.o examples/firmware/main/proposed/ex13_perspective.S
rc=0

$ xtensa-esp32s3-elf-nm /tmp/ex13.o
00000070 T ex13_clip_quad
00000000 T ex13_recip_q15

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex13.o
00000070 00000036 T ex13_clip_quad
00000000 00000070 T ex13_recip_q15

$ xtensa-esp32s3-elf-objdump -h /tmp/ex13.o

/tmp/ex13.o:     file format elf32-xtensa-le

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .text         00000000  00000000  00000000  00000034  2**0
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  1 .data         00000000  00000000  00000000  00000034  2**0
                  CONTENTS, ALLOC, LOAD, DATA
  2 .bss          00000000  00000000  00000000  00000034  2**0
                  ALLOC
  3 .iram1        000000a6  00000000  00000000  00000034  2**2
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  4 .xtensa.info  00000038  00000000  00000000  000000da  2**0
                  CONTENTS, READONLY
  5 .xt.prop      0000003c  00000000  00000000  00000112  2**0
                  CONTENTS, RELOC, READONLY

$ xtensa-esp32s3-elf-objdump -d /tmp/ex13.o

/tmp/ex13.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex13_recip_q15>:
   0:	004136        	entry	a1, 32
   3:	ed2154        	ld.qr	q4, a5, 16
   6:	830024        	ee.vld.128.ip	q0, a2, 0
   9:	180c      	movi.n	a8, 1
   b:	cdb284        	ee.movi.32.q	q1, a8, 0
   e:	cdb684        	ee.movi.32.q	q1, a8, 1
  11:	cdba84        	ee.movi.32.q	q1, a8, 2
  14:	cdbe84        	ee.movi.32.q	q1, a8, 3
  17:	890c      	movi.n	a9, 8
  19:	400900        	ssr	a9
  1c:	9e28a4        	ee.vmul.u16	q2, q0, q1
  1f:	cdffa4        	ee.zero.q	q1
  22:	dda054        	ld.qr	q3, a5, 0
  25:	e0758d3e 	ee.ldxq.32	q5, q2, a3, 0, 0
  29:	e07d9d3e 	ee.ldxq.32	q5, q2, a3, 1, 2
  2d:	e175ad3e 	ee.ldxq.32	q5, q2, a3, 2, 4
  31:	e17dbd3e 	ee.ldxq.32	q5, q2, a3, 3, 6
  35:	e0f68d3e 	ee.ldxq.32	q6, q2, a3, 0, 1
  39:	e0fe9d3e 	ee.ldxq.32	q6, q2, a3, 1, 3
  3d:	e1f6ad3e 	ee.ldxq.32	q6, q2, a3, 2, 5
  41:	e1febd3e 	ee.ldxq.32	q6, q2, a3, 3, 7
  45:	edb894        	ee.andq	q5, q5, q4
  48:	ed7f84        	ee.notq	q4, q4
  4b:	fd38c4        	ee.andq	q6, q6, q4
  4e:	edfc94        	ee.orq	q5, q5, q6
  51:	be18c4        	ee.vcmp.gt.s16	q6, q0, q3
  54:	be88f4        	ee.vcmp.lt.s16	q7, q0, q1
  57:	fd7ce4        	ee.orq	q6, q6, q7
  5a:	edbc94        	ee.andq	q5, q5, q6
  5d:	b38044        	ee.vld.128.ip	q7, a4, 0
  60:	fa0c      	movi.n	a10, 15
  62:	400a00        	ssr	a10
  65:	beef84        	ee.vmul.s16	q7, q7, q5
  68:	aa8064        	ee.vst.128.ip	q5, a6, 0
  6b:	ba8074        	ee.vst.128.ip	q7, a7, 0
  6e:	f01d      	retw.n

00000070 <ex13_clip_quad>:
  70:	004136        	entry	a1, 32
  73:	842b      	addi.n	a8, a4, 2
  75:	952b      	addi.n	a9, a5, 2
  77:	dd7344        	ee.vldbc.16	q2, a4
  7a:	ddf384        	ee.vldbc.16	q3, a8
  7d:	ed7354        	ee.vldbc.16	q4, a5
  80:	edf394        	ee.vldbc.16	q5, a9
  83:	830024        	ee.vld.128.ip	q0, a2, 0
  86:	838034        	ee.vld.128.ip	q1, a3, 0
  89:	be10f4        	ee.vcmp.lt.s16	q6, q0, q2
  8c:	bec0c4        	ee.vcmp.gt.s16	q7, q0, q4
  8f:	fd7ce4        	ee.orq	q6, q6, q7
  92:	be99f4        	ee.vcmp.lt.s16	q7, q1, q3
  95:	fd7ce4        	ee.orq	q6, q6, q7
  98:	bec9c4        	ee.vcmp.gt.s16	q7, q1, q5
  9b:	fd7ce4        	ee.orq	q6, q6, q7
  9e:	fd7fc4        	ee.notq	q6, q6
  a1:	ba0064        	ee.vst.128.ip	q6, a6, 0
  a4:	f01d      	retw.n

$ xtensa-esp32s3-elf-objdump -d /tmp/ex13.o | grep -c l32r
0

$ md5sum examples/firmware/main/proposed/ex13_perspective.S
71e2c6b8a7f9b99c52000ebd4c5e7e79  examples/firmware/main/proposed/ex13_perspective.S
```

**貼るべき出力はこの空出力そのもの**（成功時にアセンブラは何も出さない）。証拠は逆アセンブル。
`.iram1.literal` セクションは存在せず、`l32r` も 0 件 = **`.rodata` への 32bit 定数ロードを作っていない**
（定数は命令で組むか、C からポインタで受けるという約束どおり）。
対応する `.S` の md5（この出力を出した版）: `71e2c6b8a7f9b99c52000ebd4c5e7e79`

## 6. Python モデルと等価性チェック（生の出力）

スクリプトは下（実行したのは `/tmp/ex13_model.py`、C 参照は `/tmp/ex13_ref.c` を `gcc -O2` でビルド）：

```
$ gcc -O2 -o /tmp/ex13_ref /tmp/ex13_ref.c      # rc=0（下の §7 にそのまま）
$ python3 /tmp/ex13_model.py /tmp/ex13_ref      # 出力は /tmp/ex13_model_out.txt に取った
```

出力（そのまま）:

```
ex13_perspective.S host check: instruction-level model (Python) vs ex13_ref.c (host gcc -O2)
inputs: deterministic PRNG, seed 0xE13C0DE

A4 table accuracy: the kernel's quotient is floor(num * entry(i) / 2^15), so its error
   against the ideal floor(num*256/w) is the bucket's error plus the entry's rounding and
   the two floors:
     |kernel - ideal| <= 1.5 + |ideal| / (2i+1),   i = w >> 8
   measured over all 256 w of each bucket x 11 numerators (num = -32768 .. 32767):
     bucket i=  1 (w   256..  511): worst |diff| = 10923 at w=  256 num=-32768 -> -21845 vs ideal -32768 (bound 10924.17); worst rel 100.00% vs 1/(2i+1) = 33.33%
     bucket i=  3 (w   768.. 1023): worst |diff| =  1561 at w=  768 num=-32768 ->  -9362 vs ideal -10923 (bound  1561.93); worst rel  14.41% vs 1/(2i+1) = 14.29%
     bucket i=  7 (w  1792.. 2047): worst |diff| =   313 at w= 1792 num=-32768 ->  -4369 vs ideal  -4682 (bound   313.63); worst rel   6.69% vs 1/(2i+1) =  6.67%
     bucket i= 15 (w  3840.. 4095): worst |diff| =    71 at w= 3840 num=-32768 ->  -2114 vs ideal  -2185 (bound    71.98); worst rel   3.28% vs 1/(2i+1) =  3.23%
     bucket i= 31 (w  7936.. 8191): worst |diff| =    18 at w= 7936 num=-32768 ->  -1040 vs ideal  -1058 (bound    18.29); worst rel   3.12% vs 1/(2i+1) =  1.59%
     bucket i= 63 (w 16128..16383): worst |diff| =     5 at w=16128 num=-32768 ->   -516 vs ideal   -521 (bound     5.60); worst rel   1.04% vs 1/(2i+1) =  0.79%
     bucket i=127 (w 32512..32767): worst |diff| =     2 at w=32512 num=-32768 ->   -257 vs ideal   -259 (bound     2.52); worst rel   1.03% vs 1/(2i+1) =  0.39%
     bucket i=255 (w 65280..65535): worst |diff| =     1 at w=65280 num=-32768 ->   -128 vs ideal   -129 (bound     1.75); worst rel   0.78% vs 1/(2i+1) =  0.20%
   every one of the 256 buckets is inside that bound: True
   the additive part alone, |diff| - |ideal|/(2i+1), max over all 256 buckets = 1.42
     (bucket i=168, w=43008, num=32766 -> 193 vs ideal 195); the stated 1.5 covers all of them: True
   relative-error-only sweep (buckets i >= 1, lanes with |ideal| >= 4096): worst relative
     error  33.34% in bucket i=1 (bound 1/(2i+1) = 33.33%)
   sample entries (the caller's table; 2^23 = 8388608, entry = round(2^23 / w_rep)):
     i=  0  w     0..  255  w_rep   128  2^23/w_rep   65536.00  entry 32767 (the clamp)
     i=  1  w   256..  511  w_rep   384  2^23/w_rep   21845.33  entry 21845 
     i=  2  w   512..  767  w_rep   640  2^23/w_rep   13107.20  entry 13107 
     i=  8  w  2048.. 2303  w_rep  2176  2^23/w_rep    3855.06  entry  3855 
     i= 32  w  8192.. 8447  w_rep  8320  2^23/w_rep    1008.25  entry  1008 
     i=128  w 32768..33023  w_rep 32896  2^23/w_rep     255.00  entry   255 
     i=255  w 65280..65535  w_rep 65408  2^23/w_rep     128.25  entry   128 

A3 no-wrap proof (checked, not assumed): max |num * entry| over all 256 entries x {num = -32768, -32767, 32767}
   = 1073709056 at num=-32768 entry(0)=32767, which is 32768 below 2^30 = 1073741824
   so (num*entry) >> 15 lies in [-32767, 32766] and always fits the 16-bit lane -- EE.VMUL does not saturate,
   but it truncates into the lane, and that truncation is unreachable here.

A2 marker: entries outside bucket 0 are 128..21845, so 0 is unreachable by a valid lane.
   eps=    0: marker on      1 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0
   eps=    1: marker on      2 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0
   eps=  255: marker on    256 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0
   eps=  256: marker on    257 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0
   eps= 1024: marker on   1025 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0
   eps=32767: marker on  32768 of  65536 lanes (all w in 0..65535 swept); every unmasked lane has inv != 0

A5 contract: w=65535, num=1 -> inv=128, quotient=0: a REAL lane truncates to 0 too, so the
   near-plane marker must be read from inv_out and never from the quotient lane.

B1 clip: 20 rectangles x 8 vertices = 160 lanes, boundaries included (x == xmin/xmax, y == ymin/ymax:
   74 vertex/axis boundaries landed exactly on a bound), mask values seen [0, 65535], mismatches 0

C cross-check against ex13_ref.c over 576 cases (384 A = 3072 lanes, 192 B = 1536 lanes):
   mismatches: 0   first mismatch: None
   A lanes with inv == 0 (the near-plane marker): 543; valid lanes whose quotient is 0 by truncation: 97
   valid (unmasked) lanes per eps value: 0:650, 255:335, 256:406, 512:328, 1024:450, 4096:222, 32767:138
RESULT model_checks ok=1 fail=0
```

モデルが何を「命令」として写しているかはスクリプトのヘルパ名と docstring に1命令1関数で書いてある
（`vmul_u16` = p204、`ldxq32` = p113、`vcmp_gt_s16` = p158 …）。**モデルは自分自身の読みを
オラクルにできない**ので、比較相手は同じ契約を別の言語で書いた C 参照（`ex13_ref.c`、ホスト `gcc -O2`）
である。両者が食い違えばどちらかが .S と違う。

スクリプト全文（実行したものと同一）:

```python
#!/usr/bin/env python3
"""ex13_perspective.S -- a Python instruction-level model of the two kernels, checked off-hardware.

Every helper below is one PIE instruction with the pseudo-code from the TRM page named in
ex13_perspective.md (data/pie_instructions.json says which page), applied to lane lists:

  EE.VLD.128.IP  p164   qu = load128({as[31:4],4{0}}); as += imm   (the low 4 address bits are dropped)
  EE.MOVI.32.Q   p119   one 32-bit segment of a QR register from an AR
  EE.VMUL.U16    p204   qz[l] = (u16(x[l]) * u16(y[l])) >> SAR, written into the 16-bit lane
  EE.VMUL.S16    p198   qz[l] = (s16(x[l]) * s16(y[l])) >> SAR, written into the 16-bit lane
  EE.ZERO.Q      p299   qa = 0
  EE.VCMP.GT/    p158/  qa[l] = (x[l] > y[l]) ? 0xFFFF : 0   (SIGNED 16-bit compare)
  EE.VCMP.LT     p161
  EE.ANDQ/ORQ/NOTQ p76/p121/p120   whole-register bitwise logic
  EE.VLDBC.16    p170   qu = {8{load16({as[31:1],1{0}})}}
  EE.LDXQ.32     p113   vaddr_k = as + qs[lane k]*4 (k = 0..7); q u[32*sel4+31:32*sel4] = the word
                        load32({vaddr_sel8[31:2],2{0}}) -- ONE per-lane indexed load per instruction
  LD.QR          p301   qu = load128(as + imm)        (not an EE.* instruction)
  EE.VST.128.IP  p275   store128({as[31:4],4{0}}); as += imm

The point of the file is to make each kernel's semantics falsifiable without silicon:
  A1  the model reproduces the scalar C reference (ex13_ref.c, host gcc) on every lane
  A2  the reciprocal lane is 0 EXACTLY on the lanes with w <= eps, and no valid entry is 0
  A3  no lane of the quotient can wrap: |num * entry| < 2^30 for the whole (num, entry) space
  A4  the table's relative error inside bucket i is <= 1/(2i+1), measured bucket by bucket
  A5  the quotient lane is 0 on a near-plane lane AND can be 0 on a real lane by truncation, so the
      marker has to be read from inv_out (this is a contract, not a bug -- the model shows both)
  B1  the clip masks are exactly 0xFFFF / 0 and equal the scalar rectangle test, including on the
      boundaries (x == xmin, x == xmax, y == ymin, y == ymax)

Run:  python3 ex13_model.py [path-to-the-compiled-C-driver]
      (without the argument it still runs A2..A5 and B1, and says so)
"""
import random
import subprocess
import sys

M16 = 0xFFFF


# --------------------------------------------------------------------------- shared scalars
def s16(u):
    """the SIGNED reading of a 16-bit lane: what E E.VCMP.*.S16 and EE.VMUL.S16 see (p155/p198)"""
    return u - 65536 if u >= 32768 else u


def h16(v):
    """the 16-bit lane write: a value is truncated into the field, it is never clamped"""
    return v & M16


def floor_div(a, b):
    """the arithmetic shift `>> SAR` of the pseudo-code, for a signed product (floors, not toward 0)"""
    q = a // b if b > 0 else None
    assert b > 0
    return a // b


def entry(i):
    """the table's half-value, exactly as the .md defines it (integer arithmetic, stated once)"""
    w_rep = 256 * i + 128
    r = (8388608 + (w_rep >> 1)) // w_rep          # 2^23 = 8388608
    return max(1, min(32767, r))


def table_build():
    """256 int32 words, both 16-bit halves the same -> a 32-bit indexed load can land on either half"""
    return [(e | (e << 16)) for e in (entry(i) for i in range(256))]


# --------------------------------------------------------------------------- memory helpers
def load16(mem, addr):
    return int.from_bytes(mem[(addr & ~1) & 0xFFFFFFFF:((addr & ~1) + 2) & 0xFFFFFFFF],
                          "little", signed=False)


def load32(mem, addr):
    a = addr & ~3
    return int.from_bytes(mem[a:a + 4], "little", signed=False)


def vld128(mem, addr):
    """EE.VLD.128.IP: {as[31:4],4{0}} -- the low four address bits are forced to 0 (p164, TRM p49)"""
    a = addr & ~15
    return [load16(mem, a + 2 * i) for i in range(8)]


def ldxq32(mem, table_base, idx, sel4, sel8):
    """EE.LDXQ.32 qu, qs, as, sel4, sel8 (p113): the address of lane k is as + qs[lane k]*4, ONE of
    the eight addresses is loaded (sel8), the word is written to the 32-bit segment sel4."""
    vaddr = table_base + idx[sel8] * 4                 # 16-bit lane value * 4
    return load32(mem, vaddr)                          # load32 forces vaddr[31:2]


# --------------------------------------------------------------------------- instruction models
def vmul_u16(x, y, sar):
    """EE.VMUL.U16 qz, qx, qy (p204)"""
    return [h16(((x[i] & M16) * (y[i] & M16)) >> sar) for i in range(8)]


def vmul_s16(x, y, sar):
    """EE.VMUL.S16 qz, qx, qy (p198): signed product, arithmetic shift, 16-bit lane write"""
    return [h16(floor_div(s16(x[i]) * s16(y[i]), 1 << sar)) for i in range(8)]


def vcmp_gt_s16(x, y):
    """EE.VCMP.GT.S16 qa, qx, qy (p158)"""
    return [M16 if s16(x[i]) > s16(y[i]) else 0 for i in range(8)]


def vcmp_lt_s16(x, y):
    """EE.VCMP.LT.S16 qa, qx, qy (p161)"""
    return [M16 if s16(x[i]) < s16(y[i]) else 0 for i in range(8)]


def v_and(x, y):
    return [x[i] & y[i] for i in range(8)]


def v_or(x, y):
    return [x[i] | y[i] for i in range(8)]


def v_not(x):
    return [(~x[i]) & M16 for i in range(8)]


# --------------------------------------------------------------------------- kernel A, as written
def model_recip_q15(mem_table, w8, num8, consts, shift=15):
    """ex13_recip_q15, instruction by instruction, against the memory image `mem_table`.

    consts are the 8 eps lanes (as int16 words, read with LD.QR -- unsigned 0..32767) and the
    lane-pair mask word sequence (four 32-bit segments of 0x0000FFFF).
    Returns (inv_out, x_over_w_out, trace) where trace carries the intermediates the .md quotes.
    """
    # LD.QR q4, a5, 16 -- the lane-pair mask: four 32-bit segments of 0x0000FFFF
    mask = []
    for seg in range(4):
        w32 = consts["mask32"][seg]
        mask += [w32 & M16, (w32 >> 16) & M16]
    # EE.VLD.128.IP q0, a2, 0 -- the eight uint16 w lanes
    q0 = w8
    # q1 = eight lanes of 1 (EE.MOVI.32.Q x4, p119)
    ones = [1] * 8
    # ssr 8 ; EE.VMUL.U16 q2, q0, q1 -> the table index
    q2 = vmul_u16(q0, ones, 8)
    # EE.ZERO.Q q1 (p299)  -- q1 becomes the zero vector used by the compare below
    q1 = [0] * 8
    # LD.QR q3, a5, 0 -- the eight eps lanes
    q3 = consts["eps8"]
    # the eight EE.LDXQ.32: segment s of q5 gets entry[idx[2s]], segment s of q6 gets entry[idx[2s+1]]
    q5 = [None] * 8
    q6 = [None] * 8
    for s in range(4):
        v = ldxq32(mem_table, 0, q2, s, 2 * s)
        q5[2 * s], q5[2 * s + 1] = v & M16, (v >> 16) & M16
    for s in range(4):
        v = ldxq32(mem_table, 0, q2, s, 2 * s + 1)
        q6[2 * s], q6[2 * s + 1] = v & M16, (v >> 16) & M16
    # EE.ANDQ / EE.NOTQ / EE.ANDQ / EE.ORQ -- the lane merge
    q5 = v_and(q5, mask)
    odd = v_not(mask)
    q6 = v_and(q6, odd)
    q5 = v_or(q5, q6)                     # q5[l] = entry[idx[l]]
    # the near-plane test: keep = (w > eps) signed, OR (w < 0) signed == (w >= 32768) unsigned
    q6 = vcmp_gt_s16(q0, q3)
    q7 = vcmp_lt_s16(q0, q1)
    q6 = v_or(q6, q7)                     # keep
    q5 = v_and(q5, q6)                    # near-plane lanes: the reciprocal becomes 0 (the marker)
    # ssr 15 ; EE.VMUL.S16 q7, q7, q5 -- q7 = (num * entry) >> 15
    q7 = vmul_s16(num8, q5, shift)
    return q5, q7, {"idx": q2, "keep": q6}


# --------------------------------------------------------------------------- kernel C, as written
def model_clip_quad(x8, y8, lo4, hi4):
    """ex13_clip_quad, instruction by instruction."""
    # EE.VLDBC.16 q2/q3/q4/q5, as (p170): {8{load16({as[31:1],1{0}})}} -- a 2-byte-aligned scalar
    q2 = [lo4[0] & M16] * 8
    q3 = [lo4[1] & M16] * 8
    q4 = [hi4[0] & M16] * 8
    q5 = [hi4[1] & M16] * 8
    q0 = x8
    q1 = y8
    q6 = vcmp_lt_s16(q0, q2)                        # x < xmin
    q7 = vcmp_gt_s16(q0, q4)                        # x > xmax
    q6 = v_or(q6, q7)
    q7 = vcmp_lt_s16(q1, q3)                        # y < ymin
    q6 = v_or(q6, q7)
    q7 = vcmp_gt_s16(q1, q5)                        # y > ymax
    q6 = v_or(q6, q7)
    return v_not(q6)                                # the accept mask


# --------------------------------------------------------------------------- the C reference side
def c_case_a(eps, w8, num8):
    line = "A %d %s %s" % (eps, " ".join(str(x) for x in w8), " ".join(str(s16(x)) for x in num8))
    return line


def c_case_b(lo4, hi4, x8, y8):
    return "B %d %d %d %d %s %s" % (lo4[0], lo4[1], hi4[0], hi4[1],
                                    " ".join(str(s16(x)) for x in x8),
                                    " ".join(str(s16(y)) for y in y8))


def run_c(binpath, cases):
    out = subprocess.run([binpath], input="\n".join(cases) + "\n", capture_output=True, text=True,
                         check=True).stdout.strip().split("\n")
    return out


# --------------------------------------------------------------------------- checks
def bucket_bound_check():
    """A4: for every bucket i, the kernel's quotient against the ideal floor(num*256/w).

    The kernel computes floor(num * entry(i) / 2^15) where entry(i) is the reciprocal of the bucket's
    midpoint, so the error has two parts: the bucket's own relative error (entry(i)/2^15 vs 1/w,
    bounded by 1/(2i+1)) and the one unit of Q8 that the floor can lose. The claim verified here is

        |kernel - ideal| <= 1 + |ideal| / (2i+1),      i = w >> 8

    and, for lanes where the floor is not the dominant term, the relative error alone is back inside
    1/(2i+1). Returns per-bucket {relative, relative_big, diff, case_diff, case_rel}.
    """
    out = {}
    for i in range(256):
        e = entry(i)
        rec = {"relative": 0.0, "relative_big": 0.0, "diff": 0, "excess": 0.0,
               "case_diff": None, "case_rel": None, "case_excess": None}
        for w in range(256 * i, 256 * i + 256):
            for num in (-32768, -32767, -12345, -1000, -1, 0, 1, 1000, 12345, 32766, 32767):
                got = s16(h16(floor_div(num * e, 1 << 15)))
                ideal = floor_div(num * 256, w) if w else 0
                if ideal == 0:
                    continue
                err = abs(got - ideal)
                rel = err / abs(ideal)
                bound = 1.5 + abs(ideal) / (2 * i + 1)
                excess = err - abs(ideal) / (2 * i + 1)
                if rec["case_diff"] is None or err > rec["diff"]:
                    rec["diff"] = err
                    rec["case_diff"] = (w, num, got, ideal, bound)
                if rec["case_excess"] is None or excess > rec["excess"]:
                    rec["excess"] = excess
                    rec["case_excess"] = (w, num, got, ideal, excess)
                if rec["case_rel"] is None or rel > rec["relative"]:
                    rec["relative"] = rel
                    rec["case_rel"] = (w, num, got, ideal)
                if abs(ideal) >= 4096:
                    if i >= 1:      # bucket 0's entry is the CLAMP, outside the formula (see the .md)
                        assert rel <= 1.0 / (2 * i + 1) + 1.5 / abs(ideal) + 1e-12, \
                            ("relative bound", i, w, num, got, ideal)
                    if rel > rec["relative_big"]:
                        rec["relative_big"] = rel
        out[i] = rec
    return out


def no_wrap_check():
    """A3: the extreme of |num * entry| over the whole space, and the shifted result's range."""
    maxp = 0
    maxr = 0
    minr = 0
    argmax = None
    for i in range(256):
        e = entry(i)
        for num in (-32768, -32767, 32767):
            p = num * e
            if abs(p) > maxp:
                maxp, argmax = abs(p), (num, i, e)
            r = floor_div(p, 1 << 15)
            maxr = max(maxr, r)
            minr = min(minr, r)
    return maxp, argmax, minr, maxr


def main(argv):
    rng = random.Random(0xE13C0DE)
    print("ex13_perspective.S host check: instruction-level model (Python) vs ex13_ref.c (host gcc -O2)")
    print("inputs: deterministic PRNG, seed 0xE13C0DE")
    print("")
    tbl = table_build()
    mem = bytearray(4 * 256)
    for i, v in enumerate(tbl):
        mem[4 * i:4 * i + 4] = v.to_bytes(4, "little")

    # ---- A4: the table's error bound, bucket by bucket
    worst = bucket_bound_check()
    by_idx = [1, 3, 7, 15, 31, 63, 127, 255]
    print("A4 table accuracy: the kernel's quotient is floor(num * entry(i) / 2^15), so its error")
    print("   against the ideal floor(num*256/w) is the bucket's error plus the entry's rounding and")
    print("   the two floors:")
    print("     |kernel - ideal| <= 1.5 + |ideal| / (2i+1),   i = w >> 8")
    print("   measured over all 256 w of each bucket x 11 numerators (num = -32768 .. 32767):")
    for i in by_idx:
        w, num, got, ideal, bound = worst[i]["case_diff"]
        print("     bucket i=%3d (w %5d..%5d): worst |diff| = %5d at w=%5d num=%6d -> %6d vs ideal %6d"
              " (bound %8.2f); worst rel %6.2f%% vs 1/(2i+1) = %5.2f%%"
              % (i, 256 * i, 256 * i + 255, worst[i]["diff"], w, num, got, ideal, bound,
                 100 * worst[i]["relative"], 100.0 / (2 * i + 1)))
    assert all(worst[i]["diff"] <= worst[i]["case_diff"][4] + 1e-12 for i in range(256)), \
        "additive bucket bound violated"
    print("   every one of the 256 buckets is inside that bound: True")
    ex = max(range(256), key=lambda i: worst[i]["excess"])
    w, num, got, ideal, excess = worst[ex]["case_excess"]
    print("   the additive part alone, |diff| - |ideal|/(2i+1), max over all 256 buckets = %.2f"
          % excess)
    print("     (bucket i=%d, w=%d, num=%d -> %d vs ideal %d); the stated 1.5 covers all of them: %s"
          % (ex, w, num, got, ideal, all(worst[i]["excess"] <= 1.5 for i in range(256))))
    big = max(range(1, 256), key=lambda i: worst[i]["relative_big"])
    w, num, got, ideal = worst[big]["case_rel"]
    print("   relative-error-only sweep (buckets i >= 1, lanes with |ideal| >= 4096): worst relative")
    print("     error %6.2f%% in bucket i=%d (bound 1/(2i+1) = %5.2f%%)"
          % (100 * worst[big]["relative_big"], big, 100.0 / (2 * big + 1)))
    assert all(worst[i]["excess"] <= 1.5 for i in range(256))
    print("   sample entries (the caller's table; 2^23 = 8388608, entry = round(2^23 / w_rep)):")
    for i in [0, 1, 2, 8, 32, 128, 255]:
        print("     i=%3d  w %5d..%5d  w_rep %5d  2^23/w_rep %10.2f  entry %5d %s"
              % (i, 256 * i, 256 * i + 255, 256 * i + 128, 8388608.0 / (256 * i + 128), entry(i),
                 "(the clamp)" if i == 0 else ""))

    # ---- A3: no lane can wrap
    maxp, argmax, minr, maxr = no_wrap_check()
    print("")
    print("A3 no-wrap proof (checked, not assumed): max |num * entry| over all 256 entries x "
          "{num = -32768, -32767, 32767}")
    print("   = %d at num=%d entry(%d)=%d, which is %d below 2^30 = %d"
          % (maxp, argmax[0], argmax[1], argmax[2], (1 << 30) - maxp, 1 << 30))
    print("   so (num*entry) >> 15 lies in [%d, %d] and always fits the 16-bit lane -- EE.VMUL does"
          " not saturate," % (minr, maxr))
    print("   but it truncates into the lane, and that truncation is unreachable here.")
    assert maxp < (1 << 30) and maxr <= 32767 and minr >= -32768

    # ---- A2: the marker
    eps_words = [256] * 8
    inv_min = min(entry(i) for i in range(1, 256))
    inv_max = max(entry(i) for i in range(1, 256))
    print("")
    print("A2 marker: entries outside bucket 0 are %d..%d, so 0 is unreachable by a valid lane."
          % (inv_min, inv_max))
    for eps in (0, 1, 255, 256, 1024, 32767):
        marked = 0
        lanes = 0
        consts = {"eps8": [eps] * 8, "mask32": [0x0000FFFF] * 4}
        for base in range(0, 65536, 8):
            w8 = [(base + j) for j in range(8)]
            num8 = [1] * 8
            inv, xow, _ = model_recip_q15(mem, w8, num8, consts)
            for j in range(8):
                lanes += 1
                if w8[j] <= eps:
                    assert inv[j] == 0 and xow[j] == 0
                    marked += 1
                else:
                    assert inv[j] != 0
        print("   eps=%5d: marker on %6d of %6d lanes (all w in 0..65535 swept); every unmasked lane"
              " has inv != 0" % (eps, marked, lanes))

    # ---- the trivial-lane contract: a real lane can have a 0 quotient by truncation
    consts = {"eps8": [256] * 8, "mask32": [0x0000FFFF] * 4}
    w8 = [65535] * 8
    num8 = [1] * 8
    inv, xow, _ = model_recip_q15(mem, w8, num8, consts)
    print("")
    print("A5 contract: w=65535, num=1 -> inv=%d, quotient=%d: a REAL lane truncates to 0 too, so the"
          % (s16(inv[0]), s16(xow[0])))
    print("   near-plane marker must be read from inv_out and never from the quotient lane.")

    # ---- B1: the clip kernel
    bad_b = 0
    mask_values = set()
    vld_lines = 0
    for t in range(20):
        xmin = rng.randint(-2000, 2000)
        ymin = rng.randint(-2000, 2000)
        xmax = xmin + rng.randint(0, 4000)
        ymax = ymin + rng.randint(0, 4000)
        lo4 = [xmin, ymin, 0, 0]
        hi4 = [xmax, ymax, 0, 0]
        x8 = [rng.choice([xmin, xmax, xmin - 1, xmax + 1, rng.randint(-32768, 32767)]) for _ in range(8)]
        y8 = [rng.choice([ymin, ymax, ymin - 1, ymax + 1, rng.randint(-32768, 32767)]) for _ in range(8)]
        got = model_clip_quad(x8, y8, lo4, hi4)
        want = [M16 if (s16(x8[i]) >= xmin and s16(x8[i]) <= xmax and s16(y8[i]) >= ymin and
                        s16(y8[i]) <= ymax) else 0 for i in range(8)]
        mask_values |= set(got)
        vld_lines += len(set(x8) & {xmin, xmax}) + len(set(y8) & {ymin, ymax})
        assert len(got) == 8 and all(v in (0, M16) for v in got)
        if got != want:
            bad_b += 1
    print("")
    print("B1 clip: 20 rectangles x 8 vertices = 160 lanes, boundaries included (x == xmin/xmax,"
          " y == ymin/ymax:")
    print("   %d vertex/axis boundaries landed exactly on a bound), mask values seen %s, mismatches %d"
          % (vld_lines, sorted(mask_values), bad_b))
    assert bad_b == 0 and 0 in mask_values and M16 in mask_values

    # ---- the random equivalence run against the C reference
    if len(argv) > 1:
        cases = []          # (kind, line, payload)
        for _ in range(256):                                    # A: full-range random
            eps = rng.choice([0, 255, 256, 512, 1024, 4096, 32767])
            w8 = [rng.randint(0, 65535) for _ in range(8)]
            num8 = [rng.randint(-32768, 32767) for _ in range(8)]
            cases.append(("A", c_case_a(eps, w8, num8), (eps, w8, num8)))
        for _ in range(128):                                    # A: the boundary corpus
            eps = rng.choice([0, 256, 1024])
            w8 = [rng.choice([0, 1, 255, 256, 257, 32767, 32768, 65534, 65535]) for _ in range(8)]
            num8 = [rng.choice([-32768, -32767, -1, 0, 1, 32766, 32767]) for _ in range(8)]
            cases.append(("A", c_case_a(eps, w8, num8), (eps, w8, num8)))
        for _ in range(128):                                    # B: random rectangles
            xmin = rng.randint(-32768, 32767)
            ymin = rng.randint(-32768, 32767)
            xmax = rng.randint(-32768, 32767)
            ymax = rng.randint(-32768, 32767)
            x8 = [rng.randint(-32768, 32767) for _ in range(8)]
            y8 = [rng.randint(-32768, 32767) for _ in range(8)]
            lo4, hi4 = [xmin, ymin, 0, 0], [xmax, ymax, 0, 0]
            cases.append(("B", c_case_b(lo4, hi4, x8, y8), (lo4, hi4, x8, y8)))
        for _ in range(64):                                     # B: every lane on a bound
            lo4, hi4 = [-100, -100, 0, 0], [100, 100, 0, 0]
            x8 = [rng.choice([-101, -100, 0, 100, 101]) for _ in range(8)]
            y8 = [rng.choice([-101, -100, 0, 100, 101]) for _ in range(8)]
            cases.append(("B", c_case_b(lo4, hi4, x8, y8), (lo4, hi4, x8, y8)))
        ref_lines = run_c(argv[1], [c[1] for c in cases])
        assert len(ref_lines) == len(cases), (len(ref_lines), len(cases))
        mism = 0
        first = None
        a_lanes = b_lanes = 0
        marked = 0
        quot_zero_real = 0
        inv_nonzero_by_eps = {}
        for (kind, _line, p), rl in zip(cases, ref_lines):
            f = rl.split()
            if kind == "A":
                eps, w8, num8 = p
                consts = {"eps8": [eps] * 8, "mask32": [0x0000FFFF] * 4}
                inv, xow, _ = model_recip_q15(mem, w8, num8, consts)
                want_inv = [int(x, 16) for x in f[1:9]]
                want_xow = [int(x, 16) for x in f[9:17]]
                for j in range(8):
                    a_lanes += 1
                    if inv[j] != want_inv[j] or xow[j] != want_xow[j]:
                        mism += 1
                        if first is None:
                            first = ("A", eps, w8[j], s16(num8[j]), inv[j], want_inv[j], xow[j],
                                     want_xow[j])
                    if inv[j] == 0:
                        marked += 1
                    else:
                        inv_nonzero_by_eps[eps] = inv_nonzero_by_eps.get(eps, 0) + 1
                        if xow[j] == 0 and s16(num8[j]) != 0:
                            quot_zero_real += 1
            else:
                lo4, hi4, x8, y8 = p
                got = model_clip_quad(x8, y8, lo4, hi4)
                want = [int(x, 16) for x in f[1:9]]
                b_lanes += 8
                if got != want:
                    mism += 1
                    if first is None:
                        first = ("B", lo4, hi4, got, want)
        print("")
        print("C cross-check against ex13_ref.c over %d cases (%d A = %d lanes, %d B = %d lanes):"
              % (len(cases), sum(1 for c in cases if c[0] == "A"), a_lanes,
                 sum(1 for c in cases if c[0] == "B"), b_lanes))
        print("   mismatches: %d   first mismatch: %s" % (mism, first))
        print("   A lanes with inv == 0 (the near-plane marker): %d; valid lanes whose quotient is 0"
              " by truncation: %d" % (marked, quot_zero_real))
        print("   valid (unmasked) lanes per eps value: %s"
              % ", ".join("%d:%d" % (k, v) for k, v in sorted(inv_nonzero_by_eps.items())))
        assert mism == 0
    else:
        print("")
        print("(pass the path of the compiled C driver as argv[1] to cross-check it lane by lane)")
    print("RESULT model_checks ok=1 fail=0")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

## 7. C 参照（ソース、コンパイル、突き合わせ）

ファームに組み込むときに `main.c` の `CHECK` が比較する相手。**ターゲット用にコンパイルが通ること**
（組み込める形か）と、**ホストで実行したときに Python モデルと同じ値を出すこと**の両方を確認した。
これが無いと「.md に貼った C が動く」は主張でしかなくなる。

```
$ gcc -O2 -o /tmp/ex13_ref /tmp/ex13_ref.c
rc=0

$ xtensa-esp32s3-elf-gcc -c -O2 -o /tmp/ex13_ref.o /tmp/ex13_ref.c
rc=0

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex13_ref.o
         U __getreent
000000b0 0000003b T ex13_clip_quad_c
00000088 00000027 T ex13_ideal_q8_c
00000030 00000057 T ex13_recip_q15_c
00000000 00000030 T ex13_table_build_c
         U fgets
00000000 0000025b T main
         U printf
         U putchar
00000000 00000400 b s_table
         U strtol
```

`ex13_table_build_c` / `ex13_recip_q15_c` / `ex13_clip_quad_c` / `ex13_ideal_q8_c` がターゲットで
コンパイルできている（`main` と `printf` は**ホスト用ドライバ**なので、実機に組み込むときは
このファイルから参照関数だけを取る）。

```c
/* ex13 -- the scalar C references for examples/firmware/main/proposed/ex13_perspective.md.
 *
 * These are the functions the firmware's ex13() would compare the kernels against, kept in one
 * standalone file so they can be compiled for the target (does it build?) and for the host (does it
 * compute what the Python instruction-level model says?). Nothing here is part of the example build:
 * examples/firmware/main/CMakeLists.txt lists its sources and is not touched by the ex13 proposal.
 *
 * Three functions:
 *   ex13_table_build_c -- the reciprocal table, by the integer formula the .md states. The host side
 *                         (Python) and the firmware side must produce the same 256 words, which is
 *                         why the formula is written with integer division and a stated rounding step
 *                         rather than with a floating-point reciprocal.
 *   ex13_recip_q15_c  -- the kernel's contract: marker at w <= eps, else the Q15 entry and
 *                         floor(num * entry / 2^15) in Q8.
 *   ex13_clip_quad_c  -- the reject/accept test as a plain rectangle test.
 *
 * Plus ex13_ideal_q8_c, the number the caller actually wanted (floor(num*256/w) in 32-bit, no table),
 * which is what the .md's accuracy statement is measured against.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define EX13_ENTRIES 256

/* floor(a / b) for b > 0, for any sign of a -- the C `/` truncates toward zero, the PIE shift does
 * not (a right shift of a negative two's-complement value is an arithmetic shift, i.e. floors). The
 * model and the reference both use this so the two agree on negative numerators. */
static int32_t floor_div(int32_t a, int32_t b)
{
    int32_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) {
        q -= 1;
    }
    return q;
}

static int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* entry(i) = clamp((2^23 + (w_rep(i) >> 1)) / w_rep(i), 1, 32767),  w_rep(i) = 256*i + 128.
 * The division is integer division (floor) of two positive numbers. */
static int32_t ex13_entry(int i)
{
    int32_t w_rep = 256 * i + 128;
    int32_t r = (8388608 + (w_rep >> 1)) / w_rep;      /* 2^23 = 8388608 */
    return clamp_i32(r, 1, 32767);
}

/* The table as the kernel loads it: 256 int32 words, both 16-bit halves equal to entry(i), entry i
 * at byte offset 4*i. 4-byte aligned (and the caller should 16-byte align it: the .md says why). */
void ex13_table_build_c(int32_t *table)
{
    for (int i = 0; i < EX13_ENTRIES; i++) {
        int32_t e = ex13_entry(i);
        table[i] = e | (e << 16);
    }
}

/* The kernel's contract, scalar. w is UNSIGNED 0..65535, eps is unsigned 0..32767. */
void ex13_recip_q15_c(const uint16_t *w, const int32_t *table, const int16_t *num_x8, uint32_t eps,
                      int16_t *inv_out, int16_t *xow_out)
{
    for (int i = 0; i < 8; i++) {
        uint32_t wi = (uint32_t)w[i];
        if (wi <= eps) {
            inv_out[i] = 0;                        /* the near-plane marker, and ... */
            xow_out[i] = 0;                        /* ... the quotient lane is the same marker */
        } else {
            int32_t half = table[wi >> 8] & 0xFFFF;    /* both halves of the entry are equal */
            half = (int16_t)half;                      /* the signed reading, as VMUL.S16 reads it */
            inv_out[i] = (int16_t)half;
            /* VMUL.S16: the 32-bit product of the signed numerator and the entry, >> 15 (SAR), the
             * result written into the 16-bit lane. floor_div is that arithmetic shift. */
            xow_out[i] = (int16_t)floor_div((int32_t)(int16_t)num_x8[i] * half, 32768);
        }
    }
}

/* floored num/w in Q8 (the value the caller wanted). 64-bit so the 32-bit product is not in the way. */
int32_t ex13_ideal_q8_c(int16_t num, uint16_t w)
{
    if (w == 0) {
        return 0x7FFFFFFF;
    }
    return floor_div((int32_t)((int64_t)num << 8), (int32_t)w);
}

/* The rectangle test. lo4 = {xmin, ymin, ...}, hi4 = {xmax, ymax, ...}; only [0] and [1] are read. */
void ex13_clip_quad_c(const int16_t *x8, const int16_t *y8, const int16_t *lo4, const int16_t *hi4,
                      uint16_t *mask8)
{
    for (int i = 0; i < 8; i++) {
        int inside = (x8[i] >= lo4[0]) && (x8[i] <= hi4[0]) &&
                     (y8[i] >= lo4[1]) && (y8[i] <= hi4[1]);
        mask8[i] = inside ? 0xFFFF : 0;
    }
}

/* --------------------------------------------------------------------------- host driver */

#define EX13_MAX_W 4096

static int32_t s_table[EX13_ENTRIES];

static int hex4(int v)
{
    return v & 0xFFFF;
}

int main(int argc, char **argv)
{
    ex13_table_build_c(s_table);
    if (argc > 1 && argv[1][0] == 't') {                 /* "--table": dump it for the Python side */
        for (int i = 0; i < EX13_ENTRIES; i++) {
            printf("%s%08x", i ? " " : "", (unsigned)s_table[i]);
        }
        printf("\n");
        return 0;
    }
    char line[16384];
    while (fgets(line, sizeof line, stdin)) {
        char *p = line;
        if (*p == 'A') {
            long v[17];
            for (int i = 0; i < 17; i++) {
                v[i] = strtol(p + 1, &p, 10);
            }
            uint16_t w[8];
            int16_t num[8], inv[8], xow[8];
            int32_t ideal[8];
            for (int i = 0; i < 8; i++) {
                w[i] = (uint16_t)v[1 + i];
                num[i] = (int16_t)v[9 + i];
            }
            ex13_recip_q15_c(w, s_table, num, (uint32_t)v[0], inv, xow);
            for (int i = 0; i < 8; i++) {
                ideal[i] = ex13_ideal_q8_c(num[i], w[i]);
            }
            printf("A");
            for (int i = 0; i < 8; i++) {
                printf(" %04x", hex4(inv[i]));
            }
            for (int i = 0; i < 8; i++) {
                printf(" %04x", hex4(xow[i]));
            }
            for (int i = 0; i < 8; i++) {
                printf(" %08x", (unsigned)ideal[i]);
            }
            printf("\n");
        } else if (*p == 'B') {
            long v[20];
            for (int i = 0; i < 20; i++) {
                v[i] = strtol(p + 1, &p, 10);
            }
            int16_t lo4[4] = {(int16_t)v[0], (int16_t)v[1], 0, 0};
            int16_t hi4[4] = {(int16_t)v[2], (int16_t)v[3], 0, 0};
            int16_t x8[8], y8[8];
            uint16_t mask8[8];
            for (int i = 0; i < 8; i++) {
                x8[i] = (int16_t)v[4 + i];
                y8[i] = (int16_t)v[12 + i];
            }
            ex13_clip_quad_c(x8, y8, lo4, hi4, mask8);
            printf("B");
            for (int i = 0; i < 8; i++) {
                printf(" %04x", hex4(mask8[i]));
            }
            printf("\n");
        } else {
            continue;
        }
    }
    (void)EX13_MAX_W;
    return 0;
}
```

## 8. 命令ごとの出典（TRM ページ + 符号化照合）

`data/pie_instructions.json` の `source_page`（マニュアルの命令節の開始ページ）と、その節の Operation
疑似コードの**逐語引用**（長いものは 96 文字で切って `…` を付けた。全文は JSON 側にある）。
符号化照合はリポジトリ自身のツール
（`.venv/bin/python tools/asm_toolchain.py --check-instruction <NAME>`）で、**manual の命令語図と
ツールチェーンの符号化が一致するか**を見たもの。ここに出る命令は**この .S が実際に出力した
14種すべて**で、14/14 が `match`:

| 命令 | このカーネルでの役割 | TRM ページ (`source_page`) | Operation（逐語、長いものは …） | manual vs toolchain |
|---|---|---|---|---|
| `LD.QR` | 16バイト定数ブロック（eps レーンとレーン対マスク）を1回で読み込む。**128bit QR ロード** | **p301** | `qu = load128(as + imm)` | match |
| `EE.LDXQ.32` | **per-lane の表引き**（レーン k のアドレス = as + qs[lane k]*4）。8頂点に8回 | **p113** | `vaddr0[31:0] = as[31:0] + qs[ 15: 0] * 4 vaddr1[31:0] = as[31:0] + qs[ 31: 16] * 4 vaddr2[31:0]  …` | match |
| `EE.VMUL.U16` | 表インデックス `idx = w >> 8`（符号なしレーンの論理シフト） | **p204** | `qz[ 15: 0] = (qx[ 15: 0] * qy[ 15: 0]) >> SAR[5:0] qz[ 31: 16] = (qx[ 31: 16] * qy[ 31: 16]) >>  …` | match |
| `EE.VMUL.S16` | 逆数の適用 `(num * entry) >> 15`（符号付き積＋算術シフト） | **p198** | `qz[ 15: 0] = (qx[ 15: 0] * qy[ 15: 0]) >> SAR[5:0] qz[ 31: 16] = (qx[ 31: 16] * qy[ 31: 16]) >>  …` | match |
| `EE.MOVI.32.Q` | 「1のレーン×8」を GPR から作る（VMUL のシフト用） | **p119** | `if sel4 == 0: qu[ 31: 0] = as if sel4 == 1: qu[ 63: 32] = as if sel4 == 2: qu[ 95: 64] = as if s …` | match |
| `EE.ZERO.Q` | ゼロベクトル（符号なし比較の分割に使う） | **p299** | `qa = 0` | match |
| `EE.VCMP.GT.S16` | 近平面判定の第1項（w > eps、符号付き）とクリップ判定 | **p158** | `qa[ 15: 0] = (qx[ 15: 0]>qy[ 15: 0]) ? 0x{}FFFF : 0 qa[ 31: 16] = (qx[ 31: 16]>qy[ 31: 16]) ? 0x …` | match |
| `EE.VCMP.LT.S16` | 近平面判定の第2項（w < 0 ⇔ w ≥ 32768）とクリップ判定 | **p161** | `qa[ 15: 0] = (qx[ 15: 0]<qy[ 15: 0]) ? 0x{}FFFF : 0 qa[ 31: 16] = (qx[ 31: 16]<qy[ 31: 16]) ? 0x …` | match |
| `EE.ANDQ` | 逆数レーンのマスク合成（even/odd の統合、マーカー適用） | **p76** | `qa = qx & qy` | match |
| `EE.ORQ` | マスクの和（De Morgan 用の reject 合成） | **p121** | `qa = qx \| qy` | match |
| `EE.NOTQ` | odd レーンマスク、accept マスク | **p120** | `qa = ~qx` | match |
| `EE.VLD.128.IP` | w / num_x8 / x8 / y8 の16バイト読み | **p164** | `qu[127:0] = load128({as[31:4],4{0}}) as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}` | match |
| `EE.VST.128.IP` | inv_out / x_over_w_out / mask8 の16バイト書き | **p275** | `qv[127:0] => store128({as[31:4],4{0}}) as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}` | match |
| `EE.VLDBC.16` | クリップ境界（xmin, ymin, xmax, ymax）を8レーンへ broadcast | **p170** | `qu[127:0] = {8{load16({as[31:1],1{0}})}}` | match |

```
EE.VLD.128.IP      p164  match   asm='EE.VLD.128.IP q5, a6, -2048'  manual=e38064  toolchain=e38064
EE.MOVI.32.Q       p119  match   asm='EE.MOVI.32.Q q5, a6, 2'  manual=edba64  toolchain=edba64
EE.VMUL.U16        p204  match   asm='EE.VMUL.U16 q5, q6, q7'  manual=aefea4  toolchain=aefea4
EE.ZERO.Q          p299  match   asm='EE.ZERO.Q q5'  manual=edffa4  toolchain=edffa4
EE.LDXQ.32         p113  match   asm='EE.LDXQ.32 q5, q6, a7, 2, 2'  manual=e1759d7f  toolchain=e1759d7f
EE.ANDQ            p76   match   asm='EE.ANDQ q5, q6, q7'  manual=edbce4  toolchain=edbce4
EE.NOTQ            p120  match   asm='EE.NOTQ q5, q6'  manual=edffc4  toolchain=edffc4
EE.ORQ             p121  match   asm='EE.ORQ q5, q6, q7'  manual=edfce4  toolchain=edfce4
EE.VCMP.GT.S16     p158  match   asm='EE.VCMP.GT.S16 q5, q6, q7'  manual=aedec4  toolchain=aedec4
EE.VCMP.LT.S16     p161  match   asm='EE.VCMP.LT.S16 q5, q6, q7'  manual=aedef4  toolchain=aedef4
EE.VMUL.S16        p198  match   asm='EE.VMUL.S16 q5, q6, q7'  manual=aefe84  toolchain=aefe84
EE.VST.128.IP      p275  match   asm='EE.VST.128.IP q5, a6, -2048'  manual=ea8064  toolchain=ea8064
EE.VLDBC.16        p170  match   asm='EE.VLDBC.16 q5, a6'  manual=edf364  toolchain=edf364
LD.QR              p301  match   asm='LD.QR q5, a6, -128'  manual=eda864  toolchain=eda864
```

`LD.QR` は `EE.*` ではないが `data/pie_instructions.json` に入っている（p301）ので同じ照合ができる。
`RUR.*` / `ssr` / `movi` は PIE 命令ではないのでこの表に無い。

## 9. 静的コスト（オブジェクトファイルから）

```
ex13_recip_q15: 36 命令（うち EE.* 27、LD.QR 2、スカラ 7） / 8頂点 = 4.50 命令/頂点
    内訳: VLD.128.IP 2, MOVI.32.Q 4, VMUL 2, ZERO.Q 1, LDXQ.32 8, ANDQ 3, NOTQ 1, ORQ 2,
          VCMP 2, VST.128.IP 2, LD.QR 2, movi 3, ssr 2, entry/retw 2
ex13_clip_quad: 19 命令（うち EE.* 15、スカラ 4） / 8頂点 = 2.375 命令/頂点
    内訳: VLDBC.16 4, VLD.128.IP 2, VCMP 4, ORQ 3, NOTQ 1, VST.128.IP 1, addi 2, entry/retw 2
```

これは**命令数**であってサイクル数ではない（Table 1.7-2 でストールが予測されない並びではある、§4）。
`ex07_ccount()` で挟んで `BENCH recip vertices=8 cycles_pie=... cycles_c=...` を出し、`notes/08` の
フレーム予算表に足すのが次の段（この .md には性能の主張は 1 つも無い）。

C 参照の 1 レーンは「配列アクセス（表引き）＋ 32bit 積 ＋ floor ＋ 近平面判定の分岐」で、8頂点ぶんを
回すと 8 回の配列アクセスと 8 個の分岐になる。**レーンごとの表引きが 1 命令（`LDXQ.32`）で済む**のが
このカーネルの要点で、まさに PIE が苦手な形（gather）を 1 命令に落とせたことが収穫である。

## 10. 検査が捕まえたもの（正直な記録）

1. **最初の精度の主張は間違っていた。** 「相対誤差 <= 1/(2i+1)」と書いてモデルに検査させたところ、
   バケット i=1（w=256）で **100%**（`num=1` が 0 に floor）が出て assert が落ちた。
   原因は「floor が 1 単位（Q8）を落とす」項を忘れていたこと。**絶対＋相対の2項**
   （`|diff| <= 1.5 + |ideal|/(2i+1)`）に直し、`1.5` が全256バケットで成り立つことも確認した
   （実測の最大は 1.42）。**主張を弱めたのは検査の結果であって、都合ではない。**
2. **QR レジスタは q0..q7 しか使えない**ことは、最初のカーネル（q8..q15 を使っていた）を
   アセンブルしようとして
   `Error: register number out of range` で分かった。マニュアルの命令語の図（`qa[2:1] ... qa[0]`）を
   見て、**命令の性質ではなく ISA の制約**だと確定し、8本に収まるようレジスタ配置を組み直した
   （`q1` をインデックス計算のあとゼロベクトルに転用する、などの工夫はこの制約から出ている）。
   **今の .S と下のログはこの組み直しの後に取ったもの**。
   （引数を6個に抑えたこと — `n` を「8頂点固定」にし、`shift` を表のスケールに焼き込んだこと — は
   これとは別の判断で、windowed ABI のレジスタ引数が a2..a7 の6本であることが理由。§1.3。）
3. モデル自身のバグが 2 つ（レーン対マスクのセグメント配置の式、`case_diff` の初期化）—
   どちらも「メモリ像 → レーン」の写し方で、.S 側は正しかった。**レイアウトの写し間違いが
   このカーネルの最大のリスク**なので、`ldxq32()` は C 参照と同じ式で書いてある。

## 11. 確認できていないこと（正直な一覧）

実機は使っていない（`/dev/ttyACM0` は計測ランの持ち物）。したがって:

1. **`EE.LDXQ.32` の実機の意味**。意味は TRM p113 の疑似コード（8本の vaddr のうち `sel8` の
   1本だけを `load32`）に依拠している。挙動で未確認の点:
   (a) 疑似コードに**誤植**がある（`vaddr3` の式が `qs[63:47]`、正しくは `qs[63:48]`）;
   (b) 8本の vaddr を**全部**使って何かするのか（例: 8個のロードをまとめて行い、1つだけ使う）か;
   (c) 書き込みが `sel4` の 32bit セグメントだけか（他セグメントが保持されるか）— この実装は
   4セグメント全部を書くので、(c) が「全部書き換わる」でも「選んだセグメントだけ」でも結果は同じ。
   ここが違えば「表から引いた値」ではなくなるので、**最初に測るべき命令**である。
2. **`LDXQ.32` のインターロック**。Table 1.7-2 は `qu` の def を段2としているので、§4 の D 則では
   ストールしない並びにしてある。だが**ロード命令の結果が 1 命令後ろで使えるか**（ex04 が `LD.QR` で
   測った「距離1で0サイクル」に相当するか）は測っていない。ここが 2 サイクル以上なら、
   最初の `ANDQ q6` のところで 1〜2 サイクル待つ。
3. **`EE.VMUL.S16/U16` の `>> SAR` が負の積で算術シフト（floor）であること**。疑似コードは
   `(qx*qy) >> SAR` を16bitフィールドに代入するだけなので、負の積で「floor」か「0方向切り捨て」かは
   文書から読み切れない。モデルは floor を仮定し、`ex06` の `CMUL.S16` が SAR=12 で実機と一致した
   こと（notes/07 が「算術シフト」と記録）を傍証にしている。
4. **`EE.VMUL.S16/U16` の16bitレーン書き込みが切り捨て（wrap）であること**。§2.5 の「wrap しない」
   証明はこの読みの上に成り立っている。もしハードウェアが飽和するなら、（このカーネルでは到達しない
   ので）結論は変わらないが、**証明の前提は変わる**。
5. **`EE.VCMP.*.S16` が符号付き比較であること**。名前と `data/pie_instructions.json` の疑似コードは
   そう読めるが、実機で測った例は無い（ex12 も同じ前提を未確認事項に挙げている）。このカーネルは
   「符号なし比較は符号付き比較2命令＋ORQで作る」という設計なので、もし符号なし比較命令が
   存在して `.S16` が実は符号なしだったら、`w >= 32768` の頂点が近平面と誤判定される。
6. **`LD.QR` の 128bit ロードの整列規則**（§1.2）。`LD.QR` の疑似コードには下位4bitの強制が書かれて
   いない。16バイト整列を契約にしているが、**破ったときに何が起きるかは測っていない**
   （他の 128bit アクセスでは「下位4bitを落とす」が ex03 で実機再現している）。
7. **`LD.QR` の段**は Table 1.7-2 に無く、ex04 の実測（def = M）に依っている。この .md では
   §4 の D 則で `LD.QR` の消費者が十分遠いことだけを確認している。
8. **サイクル数は 1 つも測っていない**。`BENCH` 行は無い。§9 の数字は命令数。
9. **近平面の `eps` の値の妥当性**。カーネルは `eps <= 32767` を要求するだけで、「eps をいくつに
   すべきか」はカメラのパラメータ（`notes/08` のフレーム予算・視野の話）で、この例題の外。
   `eps >= 256` を守らない場合の「表の clamp が返る」挙動も、契約違反として記述するに留める。
10. **`ex13_clip_quad` の `lo4[2..3]` / `hi4[2..3]`** は予約として書いたが、z/w 平面版の
    カーネルは書いていない（未着手）。また `mask8` を三角形単位に集約する側（呼び出し側）も
    この提案には含まれない。
11. **C 参照は `main.c` に組み込んでいない**ので、ファームの `CHECK pie=... ref=...` 行は存在しない。
    上の C は単体でコンパイル（ターゲット・ホスト）してモデルと突き合わせただけ。
12. **`notes/08` の量産リストとこの番号（ex13）の衝突**は §0 に書いた。`notes/08` / `examples/README.md` /
    `tools/check_examples_log.py` / `tools/selftest_examples_checker.py` の更新は統合する人の作業
    （この提案は `proposed/` の2ファイルだけを作る）。

## 12. ビルドへの入れ方（この提案では適用しない）

```c
/* examples/firmware/main/examples.h */
/* ex13: 透視除算（逆数表 + Q15 乗算）。8頂点固定。w は符号なし、eps <= 32767。
 * table は 256×int32（両ハーフ同値。ex13_table_build_c の式で作る）、16バイト整列が推奨。
 * consts は 32バイト 16バイト整列（+0 eps×8、+16 0x0000FFFF×4）。
 * inv_out の 0 が近平面マーカー（x_over_w_out の 0 では判定できない）。 */
void ex13_recip_q15(const uint16_t *w, const int32_t *table, const int16_t *num_x8,
                    const int16_t *consts, int16_t *inv_out, int16_t *x_over_w_out);
void ex13_clip_quad(const int16_t *x8, const int16_t *y8, const int16_t *lo4, const int16_t *hi4,
                    uint16_t *mask8);

/* main.c ex13(): スイートの DATA/CHECK/BENCH の形（下書き）
 *   - 入力: w（uint16 8レーン。決定論的。境界も入れる: 0, 1, 255, 256, 32767, 32768, 65535）
 *   - ex13_recip_q15 → ex13_recip_q15_c と memcmp → CHECK recip_matches_C
 *   - inv_out == 0 のレーン数と、w <= eps のレーン数の一致を CHECK（マーカーの厳密性）
 *   - ex13_clip_quad → ex13_clip_quad_c と memcmp → CHECK clip_matches_C
 *   - print_i16 で w / num / inv / x_over_w / masks を全部 DATA に出す（ログ1枚で再計算できる）
 *   - ex07_ccount() で挟んで BENCH recip vertices=8 cycles_pie=... cycles_c=...
 *   - 8頂点ずつループする側（呼び出し側）のコストも BENCH に含める
 */
```

`tools/check_examples_log.py` に `check_ex13()`（`ex13_table_build_c` の式を Python で再実装し、
`DATA` から入力と出力を取り出して同じ値を再計算）、`tools/selftest_examples_checker.py` の合成ログに
`DATA` 行と 1 フィールドの変異を足すのが、統合時の残りの作業。
