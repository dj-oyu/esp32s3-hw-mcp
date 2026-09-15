# ex20 (proposed) — 三角形のセットアップと深度テスト: 書き込みマスク・辺係数・属性補間

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex20_raster_setup.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 10 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `86cd0c19e5c30da2c4f530cbfa0daac5` → `0ff2e593fb940c5ff7dfad6c69aee5e2`（tools/check_abi.py / tools/fix_abi.py）
#                triangle setup and the depth test: write mask, edge coefficients, attribute step

`examples/firmware/main/proposed/ex20_raster_setup.S` — `notes/08-media-3d-perf.md` の量産リストが
ex20 に割り当てた行そのもの（**notes/08 line 103**: `| ex20 | 3D | 深度テストの書き込みマスク、辺係数の
セットアップ、属性補間 |`）を書いたものです。担当するのは、ex07（頂点変換）と ex13（透視除算）の後に来る
**ラスタライザの手前の段**です: 三角形の3頂点から辺係数を作り、走査線ごとの深度テストの書き込みマスクを
作り、スパン方向の属性（z / UV）をそのまま8レーンずつ進める、という3つ。

3 本のカーネル:

| カーネル | 何をするか | 主要命令（1反復あたり） |
|---|---|---|
| `ex20_depth_test` | レーンごとの深度比較 → **8レーンぶんの書き込みマスクを1語に詰めて返す** | 2×VLD.128.IP / ZERO.ACCX / XORQ×2 / VCMP.LT.S16 / ANDQ / VMULAS.U16.ACCX / SRS.ACCX / s16i ＋ ループ3本 = 14命令 |
| `ex20_edge_setup` | 3頂点 → 3辺の (dx, dy) と**その符号**。int32 4レーン = 4本の辺平面（3本使用） | 2×VLD.128 + VLD.L.64 + VLD.H.64 / VSUBS.S32×2 / VUNZIP.32 / VCMP.GT·LT.S32 ×2 / VSUBS.S32 / VST.L.64 + MOVI.32.A + s32i（平面あたり4本） |
| `ex20_attr_interp` | スパン方向の属性の前進（Q0.16、8レーン同時） | 2×VLD / VSUBS.S16 / VMUL.S16（SAR） / VADDS.S16 / VST |

**状態: proposed（提案）。** `examples/firmware/main/CMakeLists.txt` は `SRCS` を明示列挙しているので、
`proposed/` に置いたこのファイルはビルドに入っていない（main.c / examples.h / CMakeLists.txt は触って
いない）。組み込むときは notes/08 line 116-128 の定型（CMakeLists に1行、examples.h に宣言、main.c に
`ex20()`、check_examples_log.py に `check_ex20()`）。

**この文書の数字の出どころ**: アセンブラと objdump の生出力、`data/pie_instructions.json` の
`source_page`、`data/pie_pipeline.json` の段、そして下に全文を貼った Python モデル・C 参照・piesim の
実行結果だけです。**実機は一度も使っていません**（`/dev/ttyACM0` は計測ラン中）。したがって「シリコンが
疑似コードどおりに動く」とは一言も主張していません。

## この文書の検証段（何が確認済みで、何が未確認か）

1. **アセンブラ**: `xtensa-esp32s3-elf-gcc -c` が通り、`objdump` に何が出るか → §5 に生出力を貼った。
   計画した命令を1本ずつ assembly する probe も別に走らせた（§0 と §5.2）。
2. **命令の意味**: 各命令の役割は TRM の疑似コード（`data/pie_instructions.json` の `source_page`）に
   対応づけた。命令ごとの表は §9。**存在しない命令**は「無い」ことをアセンブラのエラーで示した（§0）。
3. **Python モデル**: カーネル本文を `.S` から**読み出して**（書き写さずに）命令単位で解釈実行し、
   意図した意味（C 参照）との等価性を乱数入力で確認。スクリプトと生の出力は §6。
4. **C 参照**: ターゲット（`xtensa-esp32s3-elf-gcc -c -O2`）とホスト（`gcc -O2 -Wall -Wextra`）の両方で
   ビルドが通ること、ホスト実行がモデルと同じ値を出すことを確認。ソースと実行結果は §7。
5. **piesim.py による解釈実行**: プロジェクト側の解釈実行器（`/workspace/pjs-vm/tools/pie/piesim.py`）を
   `/tmp` に写し、ex20 が使う命令を**疑似コードのページ付きで追加**してから、3本とも走らせた。追加した差分
   と出力は §8。**追加した命令の意味は、私が同じ疑似コードを読んだという前提を §6 のモデルと共有します**
   （独立なのは C 参照とアセンブラ）。
6. **マスクのビット配置**: 256通りのレーンマスクを**構成して**（乱数ではなく）通し、パック→展開の往復を
   確認（§1.3）。これは §6 の出力の `A/mask_layout` 行そのもの。

**最後に「確認できていないこと」を全部列挙**した（§12）。ここに書いていないことは主張していません。

## 0. 32bit レーンの命令群 — 何があって、何が無いか（設問への答え）

このタスクの注意書き（「32bit レーンの命令群は 16bit 版と存在が異なる。実際にあるものだけを使って表を作る
こと」）への答えがこの節です。`data/pie_instructions.json` の 220 件を 32bit データセグメントで走査した
結果（`source_page` 付き）:

| 命令 | 何をするか（疑似コード） | source_page |
|---|---|---|
| `EE.VADDS.S32` | `qa[31:0] = min(max(qx+qy, -2^31), 2^31-1)` レーンごと（**飽和**） | 149（.LD.INCP 150 / .ST.INCP 151 も在る） |
| `EE.VSUBS.S32` | `qa[31:0] = min(max(qx-qy, -2^31), 2^31-1)`（**飽和**） | 284（285 / 286） |
| `EE.VMAX.S32` | `(qx >= qy) ? qx : qy` | 183（184 / 185） |
| `EE.VMIN.S32` | `(qx <= qy) ? qx : qy` | 192（193 / 194） |
| `EE.VCMP.EQ.S32` | `(qx == qy) ? 0xFFFFFFFF : 0` | 156 |
| `EE.VCMP.GT.S32` | `(qx > qy) ? 0xFFFFFFFF : 0` | 159 |
| `EE.VCMP.LT.S32` | `(qx < qy) ? 0xFFFFFFFF : 0` | 162 |
| `EE.VSL.32` | `qa[31:0] = qs[31:0] << SAR[5:0]`（**SAR は全命令共通の1本**） | 268 |
| `EE.VSR.32` | `qa[31:0] = qs[31:0] >> SAR[5:0]`（符号は伝播。ex14 が p274 をそう引用） | 274 |
| `EE.VZIP.32` / `EE.VUNZIP.32` | 32bit レーンの偶数/奇数への振り分けと復元 | 294 / 291 |
| `EE.VLDBC.32` | 32bit 1語を4レーンに broadcast（下位2bitを0に丸め） | 173 |
| `EE.LDXQ.32` / `EE.STXQ.32` | 索引付き 32bit ロード/ストア（**1命令1レーン**） | 113 / 145 |
| `EE.MOVI.32.A` / `EE.MOVI.32.Q` | GPR ⇄ 32bit レーン1本 | 118 / 119 |

**存在しないもの**（アセンブラが拒否した生のエラーをそのまま）:

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/absent.o /tmp/absent.S     # 命令1本ずつ
ABSENT : ee.vmul.s32  q0, q1, q2   ->  Error: unknown opcode or format name 'ee.vmul.s32'
ABSENT : ee.vmulas.s32.accx q0, q1 ->  Error: unknown opcode or format name 'ee.vmulas.s32.accx'
ABSENT : ee.vcmp.ge.s32 q0, q1, q2 ->  Error: unknown opcode or format name 'ee.vcmp.ge.s32'
ABSENT : ee.vcmp.gt.u16 q0, q1, q2 ->  Error: unknown opcode or format name 'ee.vcmp.gt.u16'
ABSENT : ee.vmulas.u32.qacc q0, q1 ->  Error: unknown opcode or format name 'ee.vmulas.u32.qacc'
PRESENT: ee.vmax.s16 q0, q1, q2        （同じ形で通る = 拒否は「命令が無い」ことの証拠）
```

つまり:

* **32bit レーンの乗算は存在しません。** `EE.VMUL.*` は S16/U16/S8/U8 のみ（p198-p209）、MAC 系
  （`EE.VMULAS.*` / `EE.VSMULAS.*`）も 16bit レーンのみ。したがって **三角形の符号付き面積（外積）は
  32bit レーンのドメインでは計算できません** — これが `ex20_edge_setup` が符号を「別の平面」として
  出す理由です（§2.5）。
* **`EE.VCMP` は EQ / GT / LT の3つだけ**で、**GE/LE も符号なし形もありません**（p155-p163）。
  だから深度テストの「近い方だけ書く」は LT（カーネル A）か、**データの符号ビットを反転して
  符号なし順序を符号付き順序に写す**（`EE.XORQ` の話、§1.4）のどちらかで綴ります。
* `EE.VMIN.S32` / `EE.VMAX.S32` / `EE.VADDS.S32` / `EE.VSUBS.S32` は**在ります**（16bit 版と同じ名前で
  データ幅だけ違う形）。16bit 版だけにある命令（`VZIP.16` / `VUNZIP.16` / `VRELU.S16` / `VPRELU.S16`）と
  32bit 版だけにある命令（`VSL.32` / `VSR.32` は **32bit レーン専用**）が混在しているので、「S16 を S32 に
  替えるだけ」が通る命令と通らない命令があります。

## 1. カーネル A — `ex20_depth_test`（深度テストの書き込みマスク）

```c
void ex20_depth_test(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask, uint32_t n);
```

### 1.1 契約（レイアウトを厳密に）

| 引数 | 意味と**要求** |
|---|---|
| `z_buf`, `z_new` | `n*8` レーンの uint16 深度（`z_buf` = 既にフレームバッファにある値、`z_new` = 今回のフラグメント）。**16バイト整列**（`EE.VLD.128.IP` は下位4ビットを0に丸める、TRM p49。整列違反は例外にならず静かに隣を読む） |
| `keep_mask` | `n` 語の uint16。**`keep_mask[g]` の bit l が、グループ g のレーン l の keep 判定**。`s16i` で 1語ずつ書くので **2バイト整列**で足りる（展開に使う `EE.VLDBC.16` の要求も同じ2バイト整列、p170） |
| `n` | 8レーングループの数。`n == 0` は何にも触らない |
| 返す値 | `keep_mask[g]` bit l = 1 ⇔ `z_new[8g+l] < z_buf[8g+l]`（**符号なし uint16 として**） |

**レーンとビットの対応**（この1行が契約のすべて）: レーン l は 128bit レジスタの `[16l, 16l+16)`、
すなわち**バイトオフセット `2*(8g+l)` のリトルエンディアン 16bit ワード**（`EE.VLD.128.IP` の疑似コードは
`qu[127:0] = load128({as[31:4],4{0}})`、p164）で、そのレーンがビット l になる（bit 0 = レーン 0 = 先頭の
uint16）。この対応は主張ではなく、§6 の `A/mask_layout` で**256通り全部を構成して**往復させて確認している。

### 1.2 命令列（1グループ = 14命令 + プロローグ21命令）

```
.Ldepth_loop:
    EE.VLD.128.IP  q0, a2, 16            z_buf lanes（def は段 M、pipeline p69）
    EE.ZERO.ACCX                         ACCX = 0。これが「このグループの1語」を作る1命令（p298）
    EE.VLD.128.IP  q1, a3, 16            z_new lanes
    EE.XORQ        q0, q0, q6            q6 = 全レーン 0x8000: 符号なし → 符号付きの読み替え（p297）
    EE.XORQ        q1, q1, q6
    EE.VCMP.LT.S16 q2, q1, q0            q2[l] = (z_new[l] < z_buf[l]) ? 0xFFFF : 0（p161）
    EE.ANDQ        q2, q2, q4            q4 = {1,2,4,…,128}: 残ったレーンを自分の重みに変換（p76）
    EE.VMULAS.U16.ACCX q2, q6            ACCX = 32768 × (残ったレーンの重みの和)（p240）
    addi.n         a5, a5, -1            埋め草1: MAC → SRS の距離 D = 2（p71/p68、§4）
    nop.n                                埋め草2
    EE.SRS.ACCX    a6, a8, 0             a6 = sat32(ACCX >> a8[5:0])、a8 = 15（p134）
    s16i           a6, a4, 0             keep_mask[g] = 詰めた1語（≤ 255）
    addi           a4, a4, 2             次の語へ
    bnez           a5, .Ldepth_loop
```

プロローグ（21命令）は定数を**レジスタ内で**組み立てます: `q6` = 全レーン `0x8000`（`movi`+`slli`+`slli`+`or`
+ `EE.MOVI.32.Q`×4）、`q4` = 重み `{1,2,4,…,128}`（`0x00020001` を2ビットずつ左シフトして4セグメント）。
**この3つの署名にはどれも定数ポインタが無い**ので、定数は命令で作るしかありません（`.iram1` から
`.rodata` への `l32r` はリンカが拒否する、というのが他の例題が定数を C からポインタで受けている理由）。
プロローグはループの外なので、`n` が大きいほど1レーンあたりは 13/8 命令に近づきます（§10）。

### 1.3 マスクのビット配置（設問 A の答え。256通りで実証）

「どのレーンがどのビットか」を実証する方法は3つ走らせました。

1. **モデルの実行**: `.S` の命令列そのものを解釈実行し、`keep_mask[g]` を読み出して
   `bit l = (z_new[8g+l] < z_buf[8g+l])` と比較（600グループ = 4800レーン、`unsigned_mismatches=0`）。
2. **構成した 256 通りのレーンマスク**: 乱数ではなく、**すべての 8bit パターン**を作って通しました。
   レーン l が keep されるべきグループでは `z_buf = 1000, z_new = 999`、されないグループでは
   `z_new = 1001`。出力語は `0x0000, 0x0001, …, 0x00FF` が順に出るはずで、実際にその通りになり
   （`A/mask_layout` の行）、**メモリ上のバイト列もリトルエンディアン**であることを生バイトで確認しています
   （`s16i` の1語 = `pack('<H')`）。
3. **展開との往復**: 下の §1.5 の展開列でレーン形に戻し、元の `0xFFFF/0` ベクトルと一致することを
   256通りすべてで確認（`expand` 行）。ビット配置が違えば往復が崩れるので、これが「配置の証拠」になります。

### 1.4 なぜ `EE.XORQ` が2本要るのか（符号なし深度を符号付き比較で正しく行う）

`EE.VCMP.LT.S16` は**レーンの符号付きの読み**で比較します（p161。VCMP 系に `.U16` はありません、§0）。
uint16 の深度バッファでは「近い = 小さい」は**符号なしの順序**なので、符号付き比較は 32768 をまたぐ
レーンでだけ間違えます。両オペランドの符号ビットを反転すれば順序が一致します:

```
u < v   ⇔   (u ^ 0x8000) <s (v ^ 0x8000)      … すべての u, v ∈ [0, 65535]
```

`EE.XORQ`（p297、128ビット全体のビット演算）2本でこれが成り立ちます。モデルは
**655360 組のレーンペア**（すべての u × 8オフセット + 乱数）で反例 0 を確認（`A/xorq` 行）。
外した場合（＝符号付きのまま）の差は、キーワードが示すとおり「32768 をまたぐレーン」に一致します:

| 入力 | レーン数 | XORQ 無しの差 | またぐレーン |
|---|---|---|---|
| フルレンジ uint16、600グループ | 4800 | 598/600 グループが違う | 2403 |
| 15bit 深度（0..32767）、256グループ | 2048 | 0（差なし） | 0 |
| 32768 近傍を厚くした入力、256グループ | 2048 | 256/256 グループ | 1032 |
| 構成した 256 マスク | 2048 | —（この入力は 15bit 域） | 0 |

つまり「15bit 深度バッファ」なら XORQ を2本消せます（1グループあたり 13 → 11 命令）。フルレンジを
正しく扱うなら2本残す、というのがこの節の数字です。

### 1.5 次の合成段への入力: 展開（`keep_mask` → レーン形）

深度テストの結果を消費する側（合成・ブレンド）が欲しいのは**またレーン形の `0xFFFF/0`**です。展開は
3命令で、命令の意味から書くと:

```
EE.VLDBC.16    q0, a4            a4 が指す 16bit 語（= keep_mask[g]）を8レーンに broadcast（p170、
                                 下位1ビットを0に丸める = 2バイト整列の要求）
EE.ANDQ        q0, q0, q4        ワンホット列 {1,2,4,…,128} と同じ重みベクトル q4 で AND
                                 → 立っているレーンは自分の重み (1..128)、他は 0
EE.VCMP.GT.S16 q0, q0, q_zero    0 と比べると、重みは正なので 0xFFFF、0 は 0（p158）
```

これは `ex20_depth_test` が内部で作ったマスクと**ビット単位で同じ**になります（C 参照の
`ex20_depth_expand_c` を §7 に置き、§6 のモデルが 256通りすべてで往復一致を確認しています）。
`ex14_span_fill` の色レーンや `ex19` のスプライト合成はこの形をそのまま食えます。

**`ex19_sprite.md` §1.2 との関係（重複しないための注記）**: ex19 は「1ビット/画素のビットマスクは採らない」と
判断し、その理由の1つに「**レーン → ビットの圧縮命令が 220 命令の中に無い**」を挙げています
（ex19 md line 63-66）。この判断とここでのパックは矛盾しません:

* ex19 は**スプライトの色**のマスク（1画素 = 1レーン、消費が即時）の話で、マスクをメモリに出す必要が
  ありません。ex20 の署名は `uint16_t *keep_mask` なので、**グループごとに1語**という形が要求されています。
* 「圧縮命令が1本も無い」は**その通り**です。ここで作ったのは1命令ではなく、`ANDQ` + `VMULAS.U16.ACCX` +
  `SRS.ACCX`（+ `ZERO.ACCX`）の**4命令**で、重みベクトルと 2^15 のスケールを経由する構成です（§1.2）。
* 逆方向（ビット → レーン）は ex19 が「`ANDQ` + `VCMP.GT.S16` + broadcast で 8画素あたり3命令前後」と
  **見積もっていた**もので、この文書はそれを**実測（モデル+C参照）で確認**しました（上の3命令）。
  つまり **圧縮 4命令 / 展開 3命令**で、片道のコストはほぼ同じです。マスクをメモリに置く意味があるかは
  呼び手の判断で、このカーネルは「両方向が命令セット内で往復できる」ことを示したのが成果です。

### 1.6 どこも飽和しない理由

グループ1つぶんの和は最大 255、MAC の中身は 255 × 32768 = 8,355,840 で、ACCX の 40bit クランプ
（ex05 が実機で見た `2^39-1`）にも `EE.SRS.ACCX` の 32bit 飽和にも遠く届きません。`EE.SRS.ACCX` は
シフト後の ACCX を**書き戻します**が、ACCX はグループ先頭で毎回ゼロにするので副作用はありません。
このカーネルについて「飽和」を気にする必要は無い、というのが結論です（ex10 の SAD が 40bit の
読み出しを選んだ理由と同じ計算を、ここでは逆向きに使っています）。

## 2. カーネル B — `ex20_edge_setup`（辺係数とその符号、パディングされた4本目のレーン）

```c
void ex20_edge_setup(const int32_t *v, int32_t *edges);
```

### 2.1 契約 — パディングされた4本目のレーンをどう扱うか（設問 B の答え）

`edges` は **16 int32 = 4本の平面（各4レーン）**で、各平面の**レーン 0..2 が3本の辺**、**レーン 3 が
パディング**です。

| 配列 | 形 | 意味 |
|---|---|---|
| `v` | 8 int32 = `{x0, y0, x1, y1, x2, y2, pad0, pad1}`、**16バイト整列** | 画面空間の3頂点を (x, y) 交互で。`v[0..5]` がデータ、`v[6]` `v[7]` は**パディング**（どんな値でも良い） |
| `edges[0..2]` | dx 平面 | `dx_j = x_{j+1} - x_j`（`x3 := x0`）。符号付き32bit |
| `edges[4..6]` | dy 平面 | `dy_j = y_{j+1} - y_j` |
| `edges[8..10]` | sgn(dx) 平面 | `+1 / -1 / 0` を int32 で |
| `edges[12..14]` | sgn(dy) 平面 | 同上 |
| `edges[3]`, `edges[7]`, `edges[11]`, `edges[15]` | **書かれない** | 呼び手が元々置いていた値がそのまま残る。**読んではいけない**（値はパディング由来） |

**パディングされた4本目のレーンについての2つの保証**（どちらも §6 で数字を出しています）:

1. **書かれない**: 各平面に書くのは `VST.L.64.IP`（レーン 0,1 = 8バイト）+ `s32i`（レーン 2 = 4バイト）の
   12バイトだけで、**レーン 3 は1ビットも触りません**。モデルは 4平面 × 4096三角形 で
   `edges[3] = edges[7] = edges[11] = edges[15] = 0x5A5A5A5A`（番兵）が呼び出し前後で同一であることを
   確認しています（`B/...: sentinel_planes_intact=2048/2048` の行）。平面を `EE.VST.128.IP` で書けば
   このレーンも書いてしまうので、最後の1語だけ `s32i` にしています。
2. **レーン 0..2 に影響しない**: `v[6]` `v[7]` は読まれます（バイト 16..31 のロードに入っている）が、
   **書かれる12語のどれもそれらに依存しません**。カーネルの全命令がレーン並列（レーン間の移動が無い）なので、
   パディングの値は `q4`/`q2` のレーン 3 にしか届きません。モデルは `v[6]` `v[7]` を 6×6 の敵対的な値
   （`0, -1, 0x7FFFFFFF, 0x80000000, 0x12345678, -0x12345678`）で振り、192三角形 × 36通りで
   **書かれた語が1ビットも動かない**こと、そして「パディング語が届く保存レーン」が
   **空集合**であることを確認しています（`B/padding` 行）。

「3要素 + パディングされた4本目のレーン」は、入力側では `v[6]`/`v[7]`（3頂点 + パディング）、出力側では
各平面のレーン 3 です。**どちらもレーン並列性だけが根拠**で、この文書はその根拠をモデルの数字で裏付けています。

### 2.2 命令列（1呼び出し = 33命令、ループ無し）

```
    addi            a10, a2, 8          a10 = &v[2]（バイト8、8バイト整列）
    addi            a11, a2, 16         a11 = &v[4]（バイト16、16バイト整列）
    EE.VLD.128.IP   q0, a2, 16          q0 = {x0, y0, x1, y1}          (バイト 0..15)
    EE.VLD.L.64.IP  q1, a10, 8          q1 下位64ビット = {x1, y1}      (バイト 8..15)
    EE.VLD.128.IP   q3, a11, 0          q3 = {x2, y2, pad0, pad1}      (バイト 16..31)
    EE.VLD.H.64.IP  q1, a11, 8          q1 上位64ビット = {x2, y2}      (バイト 16..23)
    EE.ZERO.Q       q7                  q7 = 0（比較の相手、p299）
    EE.VSUBS.S32    q2, q1, q0          {x1-x0, y1-y0, x2-x1, y2-y1} = {dx0, dy0, dx1, dy1}
    EE.VSUBS.S32    q4, q0, q3          レーン0,1 = {dx2, dy2}、レーン2,3 = パディング由来
    EE.VUNZIP.32    q2, q4              dx 平面と dy 平面に分ける（p291）
    EE.VCMP.GT.S32  q5, q2, q7          (dx > 0) ? 0xFFFFFFFF : 0（p159）
    EE.VCMP.LT.S32  q6, q2, q7          (dx < 0) ? 0xFFFFFFFF : 0（p162）
    EE.VSUBS.S32    q5, q6, q5          sgn(dx) = {+1, -1, 0}（LT マスク − GT マスク）
    EE.VCMP.GT.S32  q6, q4, q7          （dy 平面も同じ3命令）
    EE.VCMP.LT.S32  q1, q4, q7
    EE.VSUBS.S32    q6, q1, q6          sgn(dy)
    ; 以降、平面あたり4命令で12バイトだけ書く（レーン3は書かない）:
    EE.VST.L.64.IP  q2, a3, 8           edges[0], edges[1]（8バイト）; a3 += 8
    EE.MOVI.32.A    q2, a9, 2           a9 = レーン2 = dx2（p118: qs が先、au が後）
    s32i            a9, a3, 0           edges[2] = dx2（a3 は平面+8を指している）
    addi            a3, a3, 8           → 次の平面（+16）
    （以降 q4=dy, q5=sgn(dx), q6=sgn(dy) の3平面ぶん繰り返し）
```

### 2.3 なぜ非整列アクセスも staging も使わないか（`VLD.L.64` / `VLD.H.64`）

3つの差分を一度に作れるのは**交互配置のおかげ**です: バイト8から始まる16バイトの窓は `{x1,y1,x2,y2}` なので、
`{x0,y0,x1,y1}` との 32bit レーン引き算1本が既に (v1−v0, v2−v1) の4値になっています。3本目の辺は残りの
`{x0−x2, y0−y2}` で、バイト16..31 のロードと逆に引くだけです。

問題は「バイト8から始まる窓」の読み出しで、これは16バイト整列していません。ex15 の担当は非整列転送
（`EE.LD.128.USAR.IP` + `EE.SRC.Q` のファネルシフトと `SRCQ.128.ST.INCP`）ですが、**ここではその経路を
使いません**: `EE.VLD.L.64.IP`（p168）と `EE.VLD.H.64.IP`（p166）は**それぞれ下位/上位の64ビットだけを
書き、もう半分には触らない**ので、8バイト整列のアドレス2つから 128bit を組み立てられます。

```
EE.VLD.L.64.IP qu, as, imm   →   qu[63:0]   = load64({as[31:3],3{0}})   … 下位64ビットだけ
EE.VLD.H.64.IP qu, as, imm   →   qu[127:64] = load64({as[31:3],3{0}})   … 上位64ビットだけ
```

バイト8とバイト16は「v が16バイト整列」という契約から**どちらも8バイト整列**なので、p168/p166 の
下位3ビット丸めは**恒等**です。つまりここには**丸めの危険がありません**（128bit アクセスの p49 丸めに
頼っているのは q0 と q3 の2本だけで、そのアドレスは 0 と 16 です）。`EE.SRC.Q` を使えば1命令減らせますが、
`USAR` の設定と「どの2チャンクをファネルするか」が要るうえ、**ex15 の担当範囲と重なります**（§13）。

### 2.4 `EE.VUNZIP.32` の読み方（逐次読みではなく、命令前の値で読む）

`data/pie_instructions.json` の p291 の Operation は行番号付きで並んでいますが、**そのまま逐次に読むと
自己矛盾**します（例: 6行目が `qs1[63:32] = qs0[127:96]` を読むとき、4行目が既に `qs0[127:96]` を
上書きしている）。**すべての読みが命令開始時の値を見る**と読むと、`VUNZIP.32` は正しい de-interleave
（`qs0' = {P0,P2,Q0,Q2}`, `qs1' = {P1,P3,Q1,Q3}`）になり、同じ読みで `VZIP.16` も正しい interleave
になります（ex16 が使う `VZIP/VUNZIP` の並びと一致）。この解釈は**モデルと piesim の両方**で同じに実装し、
`{dx0,dy0,dx1,dy1}` と `{dx2,dy2,*,*}` から `{dx0,dx1,dx2,pad}` / `{dy0,dy1,dy2,pad}` が出ることを
全三角形で確認しています。**この読みが正しいかは実機で確かめていません**（§12 の1番）。

### 2.5 符号 = 2つのマスクの差（`+1 / -1 / 0` を選択なしで作る）

`EE.VCMP.*` の出力は**全ビット 1（= 符号付きで −1）か 0**なので、符号はそのまま引き算で作れます:

```
sgn(v) = LT_mask(v, 0) - GT_mask(v, 0)
```

`LT_mask` は負のレーンで −1、`GT_mask` は正のレーンで −1。両者の差は、負なら −1、正なら +1、0 なら 0
になります（選択もブールの実体化も不要）。`EE.VSUBS.S32` の飽和（±2^31）はオペランドが 0 と −1 なので
絶対に効きません。モデルは `(dx, dy)` の代表的 9×9 の組み合わせ（`0, ±1, ±2, ±32767, -32768, ±2^31-1,
-2^31` を含む 81 通り）で符号平面の不一致 0 を確認（`B/signs` 行）。**`EE.VMIN/VMAX.S32` は使って
いません**（必要が無い: 挟むべきクリップ枠がこの関数には無い。クリップは ex14 の `ex14_edge_dda` の役）。

### 2.6 `EE.VSUBS.S32` の飽和が起きる場所（数字）

`EE.VSUBS.S32`（p284）は ±2^31 で**飽和**します。画面座標（0..320 程度）では決して起きませんが、
フルレンジの int32 では起きます:

| 入力の三角 | 三角形数 | 差が飽和した三角形 | 書かれた語の不一致 |
|---|---|---|---|
| 画面空間（座標 ±4096） | 2048 | 0 | 0 |
| フルレンジ int32 | 2048 | 1552 | 0 |

「不一致 0」は C 参照（`sat32()` で飽和をモデル化）との比較なので、**飽和そのものは実装が意図どおり**
という意味です。飽和が起きると「その三角形の辺係数は数学的な差ではない」ので、呼び手がクリップで
弾く必要があります（ex14 の DDA が同じ飽和を「長い辺が一周しない」ために使っています）。

## 3. カーネル C — `ex20_attr_interp`（Q0.16 の属性を8レーンずつ進める）

```c
void ex20_attr_interp(const int16_t *a_top, const int16_t *a_bot, int16_t *out,
                      uint32_t n, uint32_t shift);
```

### 3.1 契約と Q0.16 の意味

```
out[8g+l] = sat16( a_top[8g+l] + ( (sat16(a_bot[8g+l] - a_top[8g+l]) * l) >> shift ) )
```

* `a_top` / `a_bot` は `n*8` レーンの int16（**16バイト整列**）。**Q0.16**: 16ビットの小数部だけを持つ
  固定小数で、レーンの値 `v` は `v / 65536`、表現範囲は `[-0.5, 0.5)`。スパンの両端の属性（z や UV の
  端値）をそのまま載せられます。
* `shift` は**前進の固定小数点スケール**で、`shift = log2(チャンクの画素数)`。8レーンのチャンクなら
  **3**（1/8 ずつ進む）。`EE.VMUL.S16` は `>> SAR[5:0]` なので**ハードは 6ビットにマスク**します
  （つまり `shift & 63`）。呼び手は 0..15 を渡す前提です。
* 出力は「レーン 0 = `a_top` そのもの、レーン 7 = 8分の7だけ進んだ値」。呼び手は**スパンの端値のベクタを
  持ち、チャンク1つを1呼び出しで歩く**（複数スパンを同時に歩くときは、レーンごとに違うスパンの組を載せる）
  という使い方を想定しています。

### 3.2 命令列（1グループ = 9命令 + プロローグ14命令）

```
    （プロローグ: ランプ q4 = {0,1,2,3,4,5,6,7} を構築し、SAR = shift を設定）
.Lattr_loop:
    EE.VLD.128.IP  q0, a2, 16         a_top レーン（def は段 M）
    EE.VLD.128.IP  q1, a3, 16         a_bot レーン
    addi.n         a5, a5, -1         ループ変数（ロードの飛んでいる間に）
    EE.VSUBS.S16   q2, q1, q0         d = sat16(a_bot - a_top)            ← **飽和する**
    EE.VMUL.S16    q3, q2, q4         (d * l) >> SAR、下位16bitを格納        ← **飽和しない**
    nop.n                             Table 1.7-2: VMUL.S16 の qdef は段 M（p70）、use は E
    EE.VADDS.S16   q2, q0, q3         結果 = sat16(a_top + 前進)            ← **飽和する**
    EE.VST.128.IP  q2, a4, 16
    bnez           a5, .Lattr_loop
```

**ランプ `{0,1,2,3,4,5,6,7}` はレジスタ内で組み立てます**（定数ポインタが署名に無いため）。鍵は
「32bit セグメント = 16bit レーン2本」をそのまま 32bit 整数として扱うことで、`0x00010000` = レーン
`{0,1}`、セグメントの歩み `0x00020002` = 両レーンに {2,2} を足す、なので 4セグメントは
`0x00010000 + k*0x00020002`:

```
0x00010000 = {0,1}   0x00030002 = {2,3}   0x00050004 = {4,5}   0x00070006 = {6,7}
```

GPR は `movi` + `slli` + `addi` + `add`×3 の7命令、そこに `EE.MOVI.32.Q` 4命令（p119）。**32bit の `add` は
16bit レーンをまたいで桁上げする**ので、鎖は `0x00010000`（レーン0が0）から始める必要があります —
`0x00010001` から始めると `{2,3}` ではなく `{3,3}` になります（§11 の「検査が捕まえたもの」）。

### 3.3 飽和する場所と、しない場所（設問 C の答え）

| 段 | 命令 | 振る舞い | どこで壊れるか |
|---|---|---|---|
| 1 | `EE.VSUBS.S16`（p281） | **飽和** `min(max(a_bot-a_top, -2^15), 2^15-1)` | `a_bot - a_top` が int16 に収まらないとき（Q0.16 で「両端が半単位以上離れているとき」）。この段がこのカーネルの唯一の実用的な限界 |
| 2 | `EE.VMUL.S16`（p198） | **飽和しない**。疑似コードは `qz[l] = (qx[l] * qy[l]) >> SAR[5:0]`、積は **32bit**、シフトは**算術**、格納は**下位16ビット（切り捨て）** | 積は 32bit で計算されるので `d*l`（最大 ±32768×7 = ±229376）で溢れません。**`>> shift` した結果が int16 を超えるのは `shift < 3` のときだけ**で、そのときは飽和せず**ラップした値**が黙って入ります |
| 3 | `EE.VADDS.S16`（p146） | **飽和** `min(max(a_top+step, -2^15), 2^15-1)` | `a_top` が範囲の端に近く、前進が外側へ効くレーン（Q0.16 で飽和＝範囲の端） |
| 4 | `>> shift` そのもの | 下位 `shift` ビットの切り捨て（算術シフトなので床方向） | これは誤差ではなく、Q0.16 の量子化そのもの |

**一段目が「飽和する唯一の場所」で、二段目は「飽和しないが落ちる場所」**というのがこの節の結論です。
数字で:

* 小さい差（|a_bot − a_top| ≤ 4000、shift=3、2048レーン）: **テキストブックの lerp と1レーンも
  違いません**（`divergences_from_the_lerp=0`）。これが想定用途（スパンの端値は近い）です。
* フルレンジ int16、shift=3（2048レーン）: 差の飽和 497レーン、そこから**425レーンが食い違う**。
  **食い違いのうち VSUBS の飽和で説明できないものは 0**、`VMUL_out_of_int16` も 0。
  つまり「shift ≥ 3 のとき、食い違いは全部『差の飽和』から来る」が検査済みの主張です。
* shift=0 / 2: `VMUL_out_of_int16` が 1143 / 327 レーン出て、説明できない食い違いが 777 / 142 レーン。
  **小さい shift では2段目の切り捨てが効き始める**という、命令の疑似コードどおりの結果です。

さらに `EE.VMUL.S16` の切り捨てを **d の全域 × 8レーンで総当たり**したのが §6 の `C/mul_wrap` の行です
（shift=0 で 288826 レーン、shift=1 で 184436、shift=2 で 63039、**shift=3 で 0**）。shift ≥ 3 について
は総当たりの代わりに算術の上界 `|d|*7/2^shift ≤ 32768*7/8 = 28672 < 32768` を使っています（shift に
単調なので、境界の 0..3 を総当たりすれば十分）。

## 4. スケジューリング（Table 1.7-2 の D 則）

TRM 1.7.1 の規則: 命令 A が結果を SA 段で用意し、命令 B が SB 段で読むなら、**A は B より
`D = max(SA - SB + 1, 0)` サイクル前に発行**されなければならない。段は `data/pie_pipeline.json` から。

3本のカーネルに出てくる停留点（`k` = 間に挟まる命令数、発行間隔 = k+1）:

| 生産者 → 消費者 | SA | SB | D | 実際の間隔 |
|---|---|---|---|---|
| `VLD.128.IP q0` → `XORQ(q0)`（A） | 2 (def qu) | 1 | 2 | k=2（`ZERO.ACCX`, `VLD q1`）→ 3 ✓ |
| `VLD.128.IP q1` → `XORQ(q1)`（A） | 2 | 1 | 2 | k=1（`XORQ q0`）→ 2 ✓ |
| `VMULAS.U16.ACCX`(ACCX) → `SRS.ACCX`(ACCX) | 2 (sdef) | 1 (suse) | **2** | k=1（`addi.n` + `nop.n`）→ 2 ✓ **この `nop.n` は表から出た必要量** |
| `SRS.ACCX a6` → `s16i a6` | 1 (def au) | 1 | 1 | k=0 → 1 ✓ |
| `VLD.128.IP q0/q3` → `VSUBS.S32`（B） | 2 | 1 | 2 | k=4 / k=3 → 5 / 4 ✓ |
| `VLD.L.64.IP q1` → `VSUBS.S32(q1)`（B） | 2 | 1 | 2 | k=3（`VLD q3`, `VLD.H q1`, `ZERO.Q`）→ 4 ✓ |
| `VLD.H.64.IP q1` → `VSUBS.S32(q1)`（B） | 2 | 1 | 2 | k=1（`ZERO.Q`）→ 2 ✓ |
| `VUNZIP.32` → `VCMP.S32`（B） | 1 | 1 | 1 | k=0 → 1 ✓ |
| `VCMP.LT.S32 q6` → `VSUBS.S32 q5`（B） | 1 | 1 | 1 | k=0 → 1 ✓ |
| `VMUL.S16 q3` → `VADDS.S16(q3)`（C） | 2 (def qz) | 1 | **2** | k=1（`nop.n`）→ 2 ✓ **この `nop.n` も表から出た必要量** |
| `VLD.128.IP q0/q1` → `VSUBS.S16`（C） | 2 | 1 | 2 | k=2（`addi.n` を挟んで）/ k=1 → 3 / 2 ✓ |
| `ssr a6` → `VMUL.S16`（C） | SAR は `suse SAR 1` | 1 | 1 | k=3（`VLD`, `VLD`, `addi.n`, `VSUBS`）→ 4 ✓ |

**つまりこの並びは D 則の上では待ちがありません**（等号でちょうど限界のペアが5箇所あり、`nop.n` 2本は
その必要量そのものです）。`EE.SRS.ACCX` を選んだ理由もここです: ex10 が使った `RUR.ACCX_0` は 220 命令にも
`data/pie_pipeline.json` にも**行が無い**ので、あちらは距離を当て推量しています（4 `nop.n`）。
このカーネルは `SRS.ACCX`（p134、表では p68）に置き換えて、**距離が表から決まる形**にしました。

ハードウェア資源（16bit 乗算器が8本、TRM 1.7.2）は、A が 1グループ1回、C が 1グループ1回の
MAC/乗算なので、同時に2本以上出ません。**サイクルは測っていません**（§10）。

## 5. アセンブル結果（生の出力）

```
$ . /opt/esp-idf/export.sh
$ xtensa-esp32s3-elf-gcc --version | head -1
xtensa-esp-elf-gcc (crosstool-NG esp-15.2.0_20251204) 15.2.0

$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex20.o examples/firmware/main/proposed/ex20_raster_setup.S
(no output, exit status 0)            ← 成功時にアセンブラは何も出さない

$ md5sum examples/firmware/main/proposed/ex20_raster_setup.S
86cd0c19e5c30da2c4f530cbfa0daac5  examples/firmware/main/proposed/ex20_raster_setup.S
（この md5 と下のサイズは **ABI 修正前**の版。修正後は `0ff2e593fb940c5ff7dfad6c69aee5e2`、サイズは
`ex20_depth_test` 0x68 / `ex20_edge_setup` 0x62 / `ex20_attr_interp` 0x47 — 冒頭の注記を参照。）

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex20.o
000000c4 0000003f T ex20_attr_interp
00000000 00000066 T ex20_depth_test
00000068 0000005a T ex20_edge_setup

$ xtensa-esp32s3-elf-objdump -h /tmp/ex20.o

/tmp/ex20.o:     file format elf32-xtensa-le

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .text         00000000  00000000  00000000  00000034  2**0
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  1 .data         00000000  00000000  00000000  00000034  2**0
                  CONTENTS, ALLOC, LOAD, DATA
  2 .bss          00000000  00000000  00000000  00000034  2**0
                  ALLOC
  3 .iram1        00000103  00000000  00000000  00000034  2**2
                  CONTENTS, ALLOC, LOAD, RELOC, READONLY, CODE
  4 .xtensa.info  00000038  00000000  00000000  00000137  2**0
                  CONTENTS, READONLY
  5 .xt.prop      00000084  00000000  00000000  0000016f  2**0
                  CONTENTS, RELOC, READONLY

$ xtensa-esp32s3-elf-objdump -d /tmp/ex20.o        → 下の全文
```

`.iram1.literal` セクションは存在しません（32bit 定数の `l32r` を作っていない = 定数はすべて命令で
組むか、C からポインタで受けている）。

### 5.1 `objdump -d` の全文

```
/tmp/ex20.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex20_depth_test>:
   0:	004136        	entry	a1, 32
   3:	05d516        	beqz	a5, 64 <ex20_depth_test+0x64>
   6:	f80c      	movi.n	a8, 15
   8:	190c      	movi.n	a9, 1
   a:	119910        	slli	a9, a9, 15
   d:	11a900        	slli	a10, a9, 16
  10:	2099a0        	or	a9, a9, a10
  13:	fd3294        	ee.movi.32.q	q6, a9, 0
  16:	fd3694        	ee.movi.32.q	q6, a9, 1
  19:	fd3a94        	ee.movi.32.q	q6, a9, 2
  1c:	fd3e94        	ee.movi.32.q	q6, a9, 3
  1f:	02a092        	movi	a9, 2
  22:	119900        	slli	a9, a9, 16
  25:	01c992        	addi	a9, a9, 1
  28:	ed3294        	ee.movi.32.q	q4, a9, 0
  2b:	11a9e0        	slli	a10, a9, 2
  2e:	ed36a4        	ee.movi.32.q	q4, a10, 1
  31:	11a9c0        	slli	a10, a9, 4
  34:	ed3aa4        	ee.movi.32.q	q4, a10, 2
  37:	11a9a0        	slli	a10, a9, 6
  3a:	ed3ea4        	ee.movi.32.q	q4, a10, 3
  3d:	830124        	ee.vld.128.ip	q0, a2, 16
  40:	250804        	ee.zero.accx
  43:	838134        	ee.vld.128.ip	q1, a3, 16
  46:	cd3d04        	ee.xorq	q0, q0, q6
  49:	cdbd14        	ee.xorq	q1, q1, q6
  4c:	9e01f4        	ee.vcmp.lt.s16	q2, q1, q0
  4f:	dd3844        	ee.andq	q2, q2, q4
  52:	0a5284        	ee.vmulas.u16.accx	q2, q6
  55:	550b      	addi.n	a5, a5, -1
  57:	f03d      	nop.n
  59:	7e1684        	ee.srs.accx	a6, a8, 0
  5c:	005462        	s16i	a6, a4, 0
  5f:	442b      	addi.n	a4, a4, 2
  61:	fd8556        	bnez	a5, 3d <ex20_depth_test+0x3d>
  64:	f01d      	retw.n
	...

00000068 <ex20_edge_setup>:
  68:	004136        	entry	a1, 32
  6b:	a28b      	addi.n	a10, a2, 8
  6d:	10c2b2        	addi	a11, a2, 16
  70:	830124        	ee.vld.128.ip	q0, a2, 16
  73:	8981a4        	ee.vld.l.64.ip	q1, a10, 8
  76:	9380b4        	ee.vld.128.ip	q3, a11, 0
  79:	8881b4        	ee.vld.h.64.ip	q1, a11, 8
  7c:	fdffa4        	ee.zero.q	q7
  7f:	9e21e4        	ee.vsubs.s32	q2, q1, q0
  82:	ae38e4        	ee.vsubs.s32	q4, q0, q3
  85:	ec2394        	ee.vunzip.32	q2, q4
  88:	aedad4        	ee.vcmp.gt.s32	q5, q2, q7
  8b:	be7a04        	ee.vcmp.lt.s32	q6, q2, q7
  8e:	aeeee4        	ee.vsubs.s32	q5, q6, q5
  91:	be5cd4        	ee.vcmp.gt.s32	q6, q4, q7
  94:	8efc04        	ee.vcmp.lt.s32	q1, q4, q7
  97:	be71e4        	ee.vsubs.s32	q6, q1, q6
  9a:	940134        	ee.vst.l.64.ip	q2, a3, 8
  9d:	dd7994        	ee.movi.32.a	q2, a9, 2
  a0:	0399      	s32i.n	a9, a3, 0
  a2:	338b      	addi.n	a3, a3, 8
  a4:	a40134        	ee.vst.l.64.ip	q4, a3, 8
  a7:	ed7994        	ee.movi.32.a	q4, a9, 2
  aa:	0399      	s32i.n	a9, a3, 0
  ac:	338b      	addi.n	a3, a3, 8
  ae:	a48134        	ee.vst.l.64.ip	q5, a3, 8
  b1:	edf994        	ee.movi.32.a	q5, a9, 2
  b4:	0399      	s32i.n	a9, a3, 0
  b6:	338b      	addi.n	a3, a3, 8
  b8:	b40134        	ee.vst.l.64.ip	q6, a3, 8
  bb:	fd7994        	ee.movi.32.a	q6, a9, 2
  be:	0399      	s32i.n	a9, a3, 0
  c0:	f01d      	retw.n
	...

000000c4 <ex20_attr_interp>:
  c4:	004136        	entry	a1, 32
  c7:	65bc      	beqz.n	a5, 101 <ex20_attr_interp+0x3d>
  c9:	190c      	movi.n	a9, 1
  cb:	119900        	slli	a9, a9, 16
  ce:	ed3294        	ee.movi.32.q	q4, a9, 0
  d1:	a91b      	addi.n	a10, a9, 1
  d3:	11baf0        	slli	a11, a10, 1
  d6:	99ba      	add.n	a9, a9, a11
  d8:	ed3694        	ee.movi.32.q	q4, a9, 1
  db:	99ba      	add.n	a9, a9, a11
  dd:	ed3a94        	ee.movi.32.q	q4, a9, 2
  e0:	99ba      	add.n	a9, a9, a11
  e2:	ed3e94        	ee.movi.32.q	q4, a9, 3
  e5:	400600        	ssr	a6
  e8:	830124        	ee.vld.128.ip	q0, a2, 16
  eb:	838134        	ee.vld.128.ip	q1, a3, 16
  ee:	550b      	addi.n	a5, a5, -1
  f0:	9e21d4        	ee.vsubs.s16	q2, q1, q0
  f3:	9ee284        	ee.vmul.s16	q3, q2, q4
  f6:	f03d      	nop.n
  f8:	9e1864        	ee.vadds.s16	q2, q0, q3
  fb:	9a0144        	ee.vst.128.ip	q2, a4, 16
  fe:	fe6556        	bnez	a5, e8 <ex20_attr_interp+0x24>
 101:	f01d      	retw.n
```

### 5.2 probe（命令の受容とエンコーディング、`/tmp/ex20_probe.S`）

ex20 が使う命令を1本ずつ別ファイルで assembly したもの。`data/pie_instructions.json` に載っている
命令名・オペランド順がそのままツールチェーンに入っていることの確認です。

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex20_probe.o /tmp/ex20_probe.S
(no output, exit status 0)

$ xtensa-esp32s3-elf-objdump -d /tmp/ex20_probe.o
/tmp/ex20_probe.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex20_probe_mask>:
   0:	004136        	entry	a1, 32
   3:	250804        	ee.zero.accx
   6:	0a5c84        	ee.vmulas.u16.accx	q4, q7
   9:	0a0484        	ee.vmulas.u16.accx	q4, q0
   c:	7e1684        	ee.srs.accx	a6, a8, 0
   f:	8e9af4        	ee.vcmp.lt.s16	q1, q2, q3
  12:	8e9ac4        	ee.vcmp.gt.s16	q1, q2, q3
  15:	8e9a94        	ee.vcmp.eq.s16	q1, q2, q3
  18:	cdb464        	ee.andq	q1, q2, q3
  1b:	cdf464        	ee.orq	q1, q2, q3
  1e:	cdb564        	ee.xorq	q1, q2, q3
  21:	cdff44        	ee.notq	q1, q2
  24:	edffa4        	ee.zero.q	q5
  27:	edb294        	ee.movi.32.q	q5, a9, 0
  2a:	e36000        	rur.accx_0	a6
  2d:	e37010        	rur.accx_1	a7
  30:	005362        	s16i	a6, a3, 0
  33:	cd7324        	ee.vldbc.16	q0, a2
  36:	cd3804        	ee.andq	q0, q0, q4
  39:	8e18c4        	ee.vcmp.gt.s16	q0, q0, q3
  3c:	f01d      	retw.n
	...

00000040 <ex20_probe_lane32>:
  40:	004136        	entry	a1, 32
  43:	8e31e4        	ee.vsubs.s32	q0, q1, q2
  46:	8e1174        	ee.vadds.s32	q0, q1, q2
  49:	8e3134        	ee.vmax.s32	q0, q1, q2
  4c:	8e3164        	ee.vmin.s32	q0, q1, q2
  4f:	9ec0d4        	ee.vcmp.gt.s32	q3, q0, q4
  52:	9ee004        	ee.vcmp.lt.s32	q3, q0, q4
  55:	9ec0a4        	ee.vcmp.eq.s32	q3, q0, q4
  58:	cc8394        	ee.vunzip.32	q0, q1
  5b:	cc83c4        	ee.vzip.32	q0, q1
  5e:	cdbf84        	ee.vsr.32	q0, q1
  61:	cdbf04        	ee.vsl.32	q0, q1
  64:	888124        	ee.vld.h.64.ip	q1, a2, 8
  67:	898124        	ee.vld.l.64.ip	q1, a2, 8
  6a:	940134        	ee.vst.l.64.ip	q2, a3, 8
  6d:	cd7724        	ee.vldbc.32	q0, a2
  70:	e0704d2e 	ee.ldxq.32	q0, q1, a2, 0, 0
  74:	0459      	s32i.n	a5, a4, 0
  76:	f01d      	retw.n

00000078 <ex20_probe_attr>:
  78:	004136        	entry	a1, 32
  7b:	400600        	ssr	a6
  7e:	9e21d4        	ee.vsubs.s16	q2, q1, q0
  81:	9ee284        	ee.vmul.s16	q3, q2, q4
  84:	9e1864        	ee.vadds.s16	q2, q0, q3
  87:	830124        	ee.vld.128.ip	q0, a2, 16
  8a:	9a0144        	ee.vst.128.ip	q2, a4, 16
  8d:	f01d      	retw.n
	...

00000090 <ex20_probe_movi_a>:
  90:	004136        	entry	a1, 32
  93:	dd7954        	ee.movi.32.a	q2, a5, 2
  96:	f01d      	retw.n
```

probe が捕まえた**オペランド順の1件**: `EE.MOVI.32.A` は**QR が先、GPR が後**（`ee.movi.32.a q2, a5, 2` = `a5 = q2[2]`）。
マニュアルの構文行（`EE.MOVI.32.A qs, au, 0..3`）どおりで、逆に書くと
`Error: bad register name: a5 / bad register name: q2` で落ちます。`EE.MOVI.32.Q` も QR が先（`qs, as, sel`）なので、
**どちらも「QR が第1オペランド」**で覚えられます。

アセンブラが拒否した3つ（§11 にも書きました）:

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex20.o ...          # s16i a6, a4, -2 の版
.../ex20_raster_setup.S:355: Error: operand 3 of 's16i' has invalid value '4294967294'
$ xtensa-esp32s3-elf-gcc -c -o /tmp/probe.o ...         # ee.srs.accx a6, a8, 15 の版
/tmp/ex20_probe.S:20: Error: operand 3 of 'ee.srs.accx' has invalid value '15'
$ xtensa-esp32s3-elf-gcc -c -o /tmp/probe.o /tmp/p2.S   # ee.movi.32.a a5, q2, 2 の版
/tmp/p2.S:6: Error: bad register name: a5 / bad register name: q2
```

（`s16i` の即値は**偶数 0..510 のみ**。0,2,4,8,16,32,64,510 は通り、-2, -1, 1, 3, 63, 255, 512 は落ちる —
この範囲も probe で測りました。だからカーネルは「ストアしてからポインタを進める」順にしています。）

## 6. Python モデルと等価性チェック（生の出力）

カーネル本文は**書き写していません**。`/tmp/ex20_model.py` は `.S` を読み、ブロックコメント・
ディレクティブ・C ヘッダを落として（**ラベルは残す**）命令列を取り出し、`M` クラスで命令単位に実行します。
各命令の意味は TRM の疑似コード（`.S` の注記と §9 のページ）から起こしたもので、`M` の中でも
ページ番号をコメントしています。`.S` の命令列とモデルがずれれば全部の検査が無意味になるので、
実行した行数を頭に出しています（`ex20_depth_test: 38 lines` など）。

モデルが確かめている主張（出力の行と対応）:

* `A/...`: パック語が `bit l = レーン l` であること、符号なし比較であること、XORQ を外した版との差が
  **「32768 をまたぐレーン」ちょうど**であること、`keep_mask` の外に1バイトも書かないこと（前後に
  0xA5 の番兵）。
* `A/mask_layout`: **構成した 256 通り**のレーンマスクが `0x0000..0x00FF` にパックされ、メモリ上の
  バイト列がリトルエンディアンであること、そして §1.5 の展開列が元のレーン形を**そのまま**戻すこと。
* `A/xorq`: `u < v ⇔ (u^0x8000) <s (v^0x8000)` を 655360 組で確認。
* `B/...`: 3辺とその符号が C 参照と一致し、`edges[3,7,11,15]` が**番兵のまま**であること。
* `B/padding`: 敵対的な `v[6]`/`v[7]` でも書かれた語が動かず、**パディング語が届く保存レーンが無い**こと。
* `B/signs`: `(dx, dy)` の代表 9×9 = 81 通りで符号平面が一致。
* `C/...`: 命令の意味どおりの C 参照と全レーン一致し、**テキストブックの lerp との食い違いがどの段から
  来るか**が分類されること（shift ≥ 3 では「差の飽和」以外の食い違いが 0）。
* `C/mul_wrap`: `EE.VMUL.S16` の下位16ビット切り捨てが **d の全域 × 8レーン**で何レーン出るか
  （shift 0/1/2/3 で 288826 / 184436 / 63039 / **0**）。

実行:

```
$ gcc -O2 -Wall -Wextra -o /tmp/ex20_ref /tmp/ex20_ref.c        # §7 のソース
$ python3 /tmp/ex20_model.py /tmp/ex20_ref
```

出力（そのまま）:

```
ex20_raster_setup.S model -- deterministic (random.Random seeds below)
kernels read from the .S (not retyped):
  ex20_depth_test: 38 lines executed by the model
  ex20_edge_setup: 33 lines executed by the model
  ex20_attr_interp: 26 lines executed by the model
Kernel A: ex20_depth_test  (XORQ relabel -> VCMP.LT.S16 -> ANDQ -> VMULAS.U16.ACCX -> SRS.ACCX)
  A/full-range uint16, 600 groups: groups=600 lanes=4800 steps=8422 unsigned_mismatches=0 signed_variant_mismatches=598 straddling_lanes=2403 mask_bytes_untouched=True
  A/full-range uint16, 600 groups: C reference agrees on mask/signed/gt+(600 words); expand round-trip = 4800 lanes; lanes equal in z (LT^GT = 1 there) = 0
  A/15-bit depths, 256 groups: groups=256 lanes=2048 steps=3606 unsigned_mismatches=0 signed_variant_mismatches=0 straddling_lanes=0 mask_bytes_untouched=True
  A/15-bit depths, 256 groups: C reference agrees on mask/signed/gt+(256 words); expand round-trip = 2048 lanes; lanes equal in z (LT^GT = 1 there) = 0
  A/boundary-heavy, 256 groups: groups=256 lanes=2048 steps=3606 unsigned_mismatches=0 signed_variant_mismatches=256 straddling_lanes=1032 mask_bytes_untouched=True
  A/boundary-heavy, 256 groups: C reference agrees on mask/signed/gt+(256 words); expand round-trip = 2048 lanes; lanes equal in z (LT^GT = 1 there) = 233
  A/mask_layout: 256 constructed lane masks -> words 0x0000..0x00ff, steps=3606, memory bytes little-endian, VLDBC.16+ANDQ+VCMP.GT.S16 expansion rebuilds all 2048 lanes, guard bands untouched=True
  A/xorq: 655360 lane pairs swept (all u x 8 offsets, plus random): counterexamples to u<v <=> (u^0x8000)<s(v^0x8000) = 0
Kernel B: ex20_edge_setup  (VLD.128/L.64/H.64 -> VSUBS.S32 -> VUNZIP.32 -> VCMP+SUBS.S32)
  B/screen-space triangles, 2048: triangles=2048 stored_words_checked=24576 mismatches=0 sentinel_planes_intact=2048/2048 triangles_with_a_saturating_difference=0
  B/screen-space triangles, 2048: C reference agrees on 64 triangles (16 words each, incl. the four padded lanes the kernel did not write)
  B/full-range int32 triangles, 2048: triangles=2048 stored_words_checked=24576 mismatches=0 sentinel_planes_intact=2048/2048 triangles_with_a_saturating_difference=1552
  B/full-range int32 triangles, 2048: C reference agrees on 64 triangles (16 words each, incl. the four padded lanes the kernel did not write)
  B/padding: triangles=192 pad pairs per triangle=36 differences_in_stored_words=0 ; stored lanes a pad word can reach = none
  B/signs: (dx,dy) combinations=81 sign_plane_mismatches=0
  B/example: v=[10, 20, 200, 20, 100, 180, 0, 0] -> steps=33 edges=000000be,ffffff9c,ffffffa6,5a5a5a5a,00000000,000000a0,ffffff60,5a5a5a5a,00000001,ffffffff,ffffffff,5a5a5a5a,00000000,00000001,ffffffff,5a5a5a5a
  B/example: dx=[190, -100, -90] dy=[0, 160, -160] sgn(dx)=[1, -1, -1] sgn(dy)=[0, 1, -1] padded lanes=['0x5a5a5a5a', '0x5a5a5a5a', '0x5a5a5a5a', '0x5a5a5a5a']
Kernel C: ex20_attr_interp  (VSUBS.S16 -> VMUL.S16 with the lane ramp, SAR=shift -> VADDS.S16)
  C/small deltas, shift=3 (the 8-pixel chunk): lanes=2048 steps=2319 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=0 | input lanes: VSUBS.S16_clamped=0 VMUL_out_of_int16=0 VADDS.S16_clamped=0 | divergences_not_from_the_VSUBS_clamp=0
  C/small deltas, shift=3 (the 8-pixel chunk): C reference agrees on 2048 lanes (shift=3)
  C/full-range int16, shift=3: lanes=2048 steps=2319 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=425 | input lanes: VSUBS.S16_clamped=497 VMUL_out_of_int16=0 VADDS.S16_clamped=0 | divergences_not_from_the_VSUBS_clamp=0
  C/full-range int16, shift=3: C reference agrees on 2048 lanes (shift=3)
  C/full-range int16, shift=0: lanes=2048 steps=2319 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=1202 | input lanes: VSUBS.S16_clamped=497 VMUL_out_of_int16=1143 VADDS.S16_clamped=1102 | divergences_not_from_the_VSUBS_clamp=777
  C/full-range int16, shift=0: C reference agrees on 2048 lanes (shift=0)
  C/full-range int16, shift=2: lanes=2048 steps=2319 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=567 | input lanes: VSUBS.S16_clamped=497 VMUL_out_of_int16=327 VADDS.S16_clamped=146 | divergences_not_from_the_VSUBS_clamp=142
  C/full-range int16, shift=2: C reference agrees on 2048 lanes (shift=2)
  C/full-range int16, shift=15: lanes=2048 steps=2319 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=425 | input lanes: VSUBS.S16_clamped=497 VMUL_out_of_int16=0 VADDS.S16_clamped=0 | divergences_not_from_the_VSUBS_clamp=0
  C/full-range int16, shift=15: C reference agrees on 2048 lanes (shift=15)
  C/extremal endpoints, shift=3: lanes=512 steps=591 model_vs_C_reference_mismatches=0 divergences_from_the_lerp=120 | input lanes: VSUBS.S16_clamped=131 VMUL_out_of_int16=0 VADDS.S16_clamped=0 | divergences_not_from_the_VSUBS_clamp=0
  C/extremal endpoints, shift=3: C reference agrees on 512 lanes (shift=3)
  C/mul_wrap: shift=0 exhaustive over all 65536 int16 d x 8 ramp lanes -> results outside int16 = 288826 (worst |result| = 229376)
  C/mul_wrap: shift=1 exhaustive over all 65536 int16 d x 8 ramp lanes -> results outside int16 = 184436 (worst |result| = 114688)
  C/mul_wrap: shift=2 exhaustive over all 65536 int16 d x 8 ramp lanes -> results outside int16 = 63039 (worst |result| = 57344)
  C/mul_wrap: shift=3 exhaustive over all 65536 int16 d x 8 ramp lanes -> results outside int16 = 0 (worst |result| = 28672)
  C/mul_wrap: for shift >= 3 the bound is |d|*7/2^shift <= 32768*7/8 = 28672 < 32768, so the low-16 truncation is unreachable for EVERY int16 d (the exhaustive cases above are the boundary shifts; the bound is monotone in shift)
RESULT model_checks ok=1 fail=0
```

スクリプト全文（実行したものと同一）:

```python
#!/usr/bin/env python3
"""ex20_raster_setup.S -- an instruction-level model of the three kernels, plus the equivalence runs.

The point of this file is to make the kernels' semantics falsifiable off-hardware.  The three kernel
bodies are NOT retyped here: they are read out of examples/firmware/main/proposed/ex20_raster_setup.S
(comments, directives and the C header stripped, labels kept), and executed instruction by instruction by
the `M` class below.  Each instruction's semantics is transcribed from the TRM pseudo-code cited in the
.S header and named here next to the implementation (data/pie_instructions.json `source_page`).

What the run establishes:
  A1  the depth kernel's packed word is bit l = lane l, in memory as the little-endian uint16 s16i writes
  A2  that packing is exact over ALL 256 lane masks (constructed masks, not random ones), and the
      documented expansion sequence (VLDBC.16 + ANDQ + VCMP.GT.S16) rebuilds the lane mask exactly
  A3  the two-EE.XORQ relabel makes the signed EE.VCMP.LT.S16 an exact unsigned compare, over a sweep
  A4  the signed variant (no XORQ) diverges exactly on the lanes that straddle 32768 -- counted
  A5  the kernel writes exactly the n*2 bytes of keep_mask and nothing else (guard bands)
  B1  the edge kernel's 12 stored words match the C reference on random triangles, exactly
  B2  lane 3 of every plane is never written (sentinels) and never depends on v[6]/v[7] (adversarial pads)
  B3  EE.VSUBS.S32's saturation is where the comment says (full-range coordinates), and is reproduced
  C1  for the eight-lane chunk (shift >= 3) the kernel is EXACTLY the C reference on every input
  C2  the divergences from the textbook lerp are exactly the saturating steps, classified and counted
  C3  EE.VMUL.S16's low-16 truncation is unreachable for shift >= 3 (exhaustive over d for shift 0..3,
      arithmetic bound beyond), and reachable for shift < 3

Run:  python3 ex20_model.py [path/to/ex20_ref]      (the compiled C reference is optional; without it
                                                    the C cross-checks are skipped and said to be)
"""
import os
import random
import re
import struct
import subprocess
import sys
import tempfile

S_PATH = ("/workspace/esp32s3-hw-mcp/examples/firmware/main/proposed/ex20_raster_setup.S")
MASK16 = 0xFFFF


# --------------------------------------------------------------------------- the .S, read not retyped

def strip_comments(text):
    return re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)


def load_kernels(path):
    """Return {function_name: [lines]}: the instruction lines of each function in the .S, in order,
    with block/inline comments removed, directives dropped and labels kept (the model needs them to
    jump).  If this returns something other than what the .S contains, every check below is void, which
    is why the run prints the line count and a digest of the text it executed."""
    src = strip_comments(open(path, encoding='utf-8').read())
    out, name = {}, None
    for raw in src.splitlines():
        line = raw.split('#')[0].strip()
        if not line:
            continue
        if line.endswith(':'):                 # a label (they start with .L in this file too)
            if name is None:
                name = line[:-1]
                out[name] = []
            else:
                out[name].append(line)
            continue
        if line.startswith('.'):
            if line.startswith('.global'):
                name = None if name else name      # a new .global starts a new function
            continue
        if name is not None:
            out[name].append(line)
    return out


# --------------------------------------------------------------------------- the machine

def s16(u):
    u &= 0xFFFF
    return u - 0x10000 if u & 0x8000 else u


def s32(u):
    u &= 0xFFFFFFFF
    return u - (1 << 32) if u & 0x80000000 else u


def sat16(x):
    return max(-32768, min(32767, x))


def sat32(x):
    return max(-(1 << 31), min((1 << 31) - 1, x))


class M:
    """A PIE machine: eight QR (each eight unsigned 16-bit lanes, lane 0 = bits 15:0), ACCX, SAR, a
    GPR file and a flat byte array.  128/64-bit accesses force the low address bits to 0 exactly as the
    hardware does (TRM p49), so a misaligned pointer reads the wrong chunk here too."""

    def __init__(self, size=1 << 20):
        self.a = {}
        self.q = [[0] * 8 for _ in range(8)]
        self.accx = 0
        self.sar = 0
        self.mem = bytearray(size)

    # ---- memory
    def ld16(self, addr):
        return self.mem[addr] | (self.mem[addr + 1] << 8)

    def ld32(self, addr):
        return self.ld16(addr) | (self.ld16(addr + 2) << 16)

    def st16(self, addr, v):
        self.mem[addr] = v & 0xFF
        self.mem[addr + 1] = (v >> 8) & 0xFF

    def st32(self, addr, v):
        for i in range(4):
            self.mem[addr + i] = (v >> (8 * i)) & 0xFF

    def ldq(self, addr):
        a = addr & ~15                                   # TRM p49: the low four bits are forced to 0
        return [self.ld16(a + 2 * i) for i in range(8)]

    def stq(self, addr, lanes):
        a = addr & ~15
        for i in range(8):
            self.st16(a + 2 * i, lanes[i])

    def ld64(self, addr):
        a = addr & ~7                                    # p168/p166: the low THREE bits are forced to 0
        return [self.ld16(a + 2 * i) for i in range(4)]  # four 16-bit lanes = one 64-bit half

    def st64lo(self, addr, lanes):
        a = addr & ~7
        for i in range(4):
            self.st16(a + 2 * i, lanes[i])

    # ---- 32-bit lane views (a 32-bit lane is two 16-bit lanes, low first)
    def l32(self, qi):
        q = self.q[qi]
        return [q[2 * i] | (q[2 * i + 1] << 16) for i in range(4)]

    def s32lanes(self, qi):
        return [s32(v) for v in self.l32(qi)]

    def set32(self, qi, vals):
        q = self.q[qi]
        for i, v in enumerate(vals):
            v &= 0xFFFFFFFF
            q[2 * i] = v & 0xFFFF
            q[2 * i + 1] = v >> 16

    # ---- run
    def g(self, name):
        return self.a[name]

    def step(self, op, args, labels):
        q, a = self.q, self.a
        if op == 'entry':
            return
        if op == 'nop':
            return
        if op == 'retw':
            return
        # ------------------------------------------------------------------ core / GPR
        if op == 'movi':
            a[args[0]] = int(args[1], 0) & 0xFFFFFFFF
            return
        if op == 'addi':
            a[args[0]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'add':                                  # add aX, aY, aZ  (32-bit add, wraps)
            a[args[0]] = (a[args[1]] + a[args[2]]) & 0xFFFFFFFF
            return
        if op == 'or':
            a[args[0]] = a[args[1]] | a[args[2]]
            return
        if op == 'slli':
            a[args[0]] = (a[args[1]] << int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ssr':
            self.sar = a[args[0]] & 63
            return
        if op == 's16i':
            self.st16(a[args[1]] + int(args[2], 0), a[args[0]] & 0xFFFF)
            return
        if op == 's32i':
            self.st32(a[args[1]] + int(args[2], 0), a[args[0]])
            return
        if op == 'beqz':
            if a[args[0]] == 0:
                raise _Jump(labels[args[1]])
            return
        if op == 'bnez':
            if a[args[0]] != 0:
                raise _Jump(labels[args[1]])
            return
        # ------------------------------------------------------------------ memory / whole-vector
        if op == 'ee.vld.128.ip':                        # TRM p164, def at M; as += imm (p49 rounding)
            q[int(args[0][1])] = self.ldq(a[args[1]])
            a[args[1]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ee.vst.128.ip':                        # TRM p275
            self.stq(a[args[1]], q[int(args[0][1])])
            a[args[1]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ee.vld.l.64.ip':                       # TRM p168: qu[63:0] = load64(align8(as))
            qi = int(args[0][1])
            q[qi][0:4] = self.ld64(a[args[1]])
            a[args[1]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ee.vld.h.64.ip':                       # TRM p166: qu[127:64] = load64(align8(as))
            qi = int(args[0][1])
            q[qi][4:8] = self.ld64(a[args[1]])
            a[args[1]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ee.vst.l.64.ip':                       # TRM p279: store64(align8(as)) = qv[63:0]
            self.st64lo(a[args[1]], q[int(args[0][1])][0:4])
            a[args[1]] = (a[args[1]] + int(args[2], 0)) & 0xFFFFFFFF
            return
        if op == 'ee.vldbc.16':                          # TRM p170: {8{load16(align2(as))}}
            q[int(args[0][1])] = [self.ld16(a[args[1]] & ~1)] * 8
            return
        if op == 'ee.vldbc.32':                          # TRM p173
            v = self.ld32(a[args[1]] & ~3)
            self.set32(int(args[0][1]), [v] * 4)
            return
        # ------------------------------------------------------------------ 16-bit lane ops
        if op == 'ee.zero.q':                            # TRM p299
            q[int(args[0][1])] = [0] * 8
            return
        if op == 'ee.notq':                              # TRM p120
            q[int(args[0][1])] = [(~v) & 0xFFFF for v in q[int(args[1][1])]]
            return
        if op == 'ee.andq':                              # TRM p76
            q[int(args[0][1])] = [x & y for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.xorq':                              # TRM p297
            q[int(args[0][1])] = [x ^ y for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vcmp.lt.s16':                       # TRM p161 (SIGNED lanes)
            q[int(args[0][1])] = [0xFFFF if s16(x) < s16(y) else 0
                                  for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vcmp.gt.s16':                       # TRM p158
            q[int(args[0][1])] = [0xFFFF if s16(x) > s16(y) else 0
                                  for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vcmp.eq.s16':                       # TRM p155
            q[int(args[0][1])] = [0xFFFF if x == y else 0
                                  for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vsubs.s16':                         # TRM p281 (SATURATES)
            q[int(args[0][1])] = [sat16(s16(x) - s16(y)) & 0xFFFF
                                  for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vadds.s16':                         # TRM p146 (SATURATES)
            q[int(args[0][1])] = [sat16(s16(x) + s16(y)) & 0xFFFF
                                  for x, y in zip(q[int(args[1][1])], q[int(args[2][1])])]
            return
        if op == 'ee.vmul.s16':                          # TRM p198: 32-bit product, ARITHMETIC >> SAR,
            qz, qx, qy = int(args[0][1]), q[int(args[1][1])], q[int(args[2][1])]   # low 16 bits stored
            q[qz] = [(((s16(x) * s16(y)) >> self.sar) & 0xFFFF) for x, y in zip(qx, qy)]
            return
        # ------------------------------------------------------------------ 32-bit lane ops
        if op == 'ee.vsubs.s32':                         # TRM p284 (SATURATES)
            out = [sat32(x - y) for x, y in zip(self.s32lanes(int(args[1][1])),
                                                self.s32lanes(int(args[2][1])))]
            self.set32(int(args[0][1]), out)
            return
        if op == 'ee.vadds.s32':                         # TRM p149 (SATURATES)
            out = [sat32(x + y) for x, y in zip(self.s32lanes(int(args[1][1])),
                                                self.s32lanes(int(args[2][1])))]
            self.set32(int(args[0][1]), out)
            return
        if op == 'ee.vcmp.gt.s32':                       # TRM p159
            out = [0xFFFFFFFF if x > y else 0 for x, y in zip(self.s32lanes(int(args[1][1])),
                                                              self.s32lanes(int(args[2][1])))]
            self.set32(int(args[0][1]), out)
            return
        if op == 'ee.vcmp.lt.s32':                       # TRM p162
            out = [0xFFFFFFFF if x < y else 0 for x, y in zip(self.s32lanes(int(args[1][1])),
                                                              self.s32lanes(int(args[2][1])))]
            self.set32(int(args[0][1]), out)
            return
        if op == 'ee.vunzip.32':                         # TRM p291, old-value reading (see the .md)
            p, r = q[int(args[0][1])], q[int(args[1][1])]
            P = [p[2 * i] | (p[2 * i + 1] << 16) for i in range(4)]
            R = [r[2 * i] | (r[2 * i + 1] << 16) for i in range(4)]
            self.set32(int(args[0][1]), [P[0], P[2], R[0], R[2]])
            self.set32(int(args[1][1]), [P[1], P[3], R[1], R[3]])
            return
        if op == 'ee.movi.32.q':                         # TRM p119: qu[sel] = as
            qi, sel = int(args[0][1]), int(args[2])
            q[qi][2 * sel] = a[args[1]] & 0xFFFF
            q[qi][2 * sel + 1] = (a[args[1]] >> 16) & 0xFFFF
            return
        if op == 'ee.movi.32.a':                         # TRM p118: au = qs[sel]  (syntax: qs first)
            a[args[1]] = self.l32(int(args[0][1]))[int(args[2])]
            return
        # ------------------------------------------------------------------ accumulator
        if op == 'ee.zero.accx':                         # TRM p298
            self.accx = 0
            return
        if op == 'ee.vmulas.u16.accx':                   # TRM p240: ACCX = clamp(ACCX + sum, 0, 2^40-1)
            x, y = q[int(args[0][1])], q[int(args[1][1])]
            s = self.accx + sum(x[i] * y[i] for i in range(8))
            self.accx = max(0, min(s, (1 << 40) - 1))
            return
        if op == 'ee.srs.accx':                          # TRM p134: ACCX = ACCX >> as[5:0] (write back),
            sh = a[args[1]] & 63                         #         au = sat32(that)
            v = self.accx >> sh
            self.accx = v
            a[args[0]] = sat32(v) & 0xFFFFFFFF
            return
        raise NotImplementedError(text_unknown(op, args))


class _Jump(Exception):
    def __init__(self, pc):
        self.pc = pc


def text_unknown(op, args):
    return ' '.join([op] + args)


# patch M.run to honour beqz/bnez jumps raised as exceptions (kept simple and explicit)
def run_lines(mach, lines, a_init, trace=False):
    mach.a = dict(a_init)
    labels = {t[:-1]: i for i, t in enumerate(lines) if t.endswith(':')}
    pc, steps = 0, 0
    while pc < len(lines):
        text = lines[pc]
        pc += 1
        if text.endswith(':'):
            continue
        steps += 1
        tok = text.replace(',', ' ').split()
        op, args = tok[0].lower(), tok[1:]
        if op.endswith('.n'):
            op = op[:-2]
        if trace:
            print('    %-34s a=%s' % (text, {k: hex(v) for k, v in sorted(mach.a.items())}))
        try:
            mach.step(op, args, labels)
        except _Jump as j:
            pc = j.pc + 1
        if op == 'retw':
            break
    return steps


# --------------------------------------------------------------------------- the C reference

def c_run(binpath, mode, data, n=0, shift=0):
    """Run the compiled C reference on `data` (bytes) and return {key: value} with the lists parsed."""
    tmp = tempfile.gettempdir()
    p = os.path.join(tmp, 'ex20_in.bin')
    with open(p, 'wb') as f:
        f.write(data)
    argv = [binpath, mode, p, str(n), str(shift)]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    res = {}
    for line in out.strip().splitlines():
        k, v = line.split('=', 1)
        res[k] = [int(x, 16) for x in v.split(',')] if v else []
    return res


def pack16(vals):
    return b''.join(struct.pack('<H', v & 0xFFFF) for v in vals)


def pack32(vals):
    return b''.join(struct.pack('<I', v & 0xFFFFFFFF) for v in vals)


# --------------------------------------------------------------------------- A: the depth test

ADDR_ZB, ADDR_ZN, ADDR_MASK = 0x1000, 0x4000, 0x8000
ADDR_V, ADDR_EDGES = 0x9000, 0xA000
ADDR_AT, ADDR_AB, ADDR_OUT = 0xB000, 0xC000, 0xD000


def run_depth(kern, zb, zn, n, guard=0xA5):
    m = M()
    m.mem[ADDR_ZB:ADDR_ZB + 2 * len(zb)] = pack16(zb)
    m.mem[ADDR_ZN:ADDR_ZN + 2 * len(zn)] = pack16(zn)
    m.mem[ADDR_MASK - 16:ADDR_MASK + 2 * n + 16] = bytes([guard]) * (2 * n + 32)
    steps = run_lines(m, kern, {'a2': ADDR_ZB, 'a3': ADDR_ZN, 'a4': ADDR_MASK, 'a5': n})
    words = [m.ld16(ADDR_MASK + 2 * g) for g in range(n)]
    before = bytes([guard]) * 16
    head = bytes(m.mem[ADDR_MASK - 16:ADDR_MASK])
    tail = bytes(m.mem[ADDR_MASK + 2 * n:ADDR_MASK + 2 * n + 16])
    return words, steps, (head == before and tail == before), m


def py_depth_mask(zb, zn, n, signed=False, gt=False):
    out = []
    for g in range(n):
        word = 0
        for l in range(8):
            x, y = zb[8 * g + l], zn[8 * g + l]
            if signed:
                keep = s16(y) < s16(x)
            elif gt:
                keep = y > x
            else:
                keep = y < x
            if keep:
                word |= 1 << l
        out.append(word)
    return out


def check_depth(kern, name, zb, zn, n, cbin=None, expect_signed_diff=None):
    words, steps, untouched, m = run_depth(kern, zb, zn, n)
    ref_u = py_depth_mask(zb, zn, n)
    ref_s = py_depth_mask(zb, zn, n, signed=True)
    ref_g = py_depth_mask(zb, zn, n, gt=True)
    bad_u = [g for g in range(n) if words[g] != ref_u[g]]
    bad_s = [g for g in range(n) if words[g] != ref_s[g]]
    straddle = sum(1 for i in range(n * 8) if (zb[i] < 32768) != (zn[i] < 32768))
    print(f"  A/{name}: groups={n} lanes={n*8} steps={steps} "
          f"unsigned_mismatches={len(bad_u)} signed_variant_mismatches={len(bad_s)} "
          f"straddling_lanes={straddle} mask_bytes_untouched={untouched}")
    assert not bad_u, f"{name}: the kernel is not the unsigned compare at group {bad_u[:3]}"
    assert untouched, f"{name}: the kernel wrote outside keep_mask"
    # the signed variant (no XORQ) diverges exactly on the straddling lanes
    diff_lanes = 0
    for g in range(n):
        d = ref_u[g] ^ ref_s[g]
        diff_lanes += bin(d).count('1')
        assert d == sum(1 << l for l in range(8)
                        if ((zb[8*g+l] < 32768) != (zn[8*g+l] < 32768))), \
            f"{name}: the signed-variant difference is not the straddling set at group {g}"
    assert diff_lanes == straddle
    if expect_signed_diff is not None:
        assert (diff_lanes > 0) == expect_signed_diff
    if cbin:
        res = c_run(cbin, 'depth', pack16(zb + zn), n)
        assert res['mask'] == words, f"{name}: C reference mask != model"
        assert res['signed_mask'] == ref_s and res['gt_mask'] == ref_g
        # the expansion sequence must rebuild the lane mask the LT produced
        lanes = [0xFFFF if (words[g] >> l) & 1 else 0 for g in range(n) for l in range(8)]
        assert res['expand'] == lanes, f"{name}: C expansion != the lane mask"
        # and the GT mask is NOT the complement of the LT mask (the equal lanes differ)
        eq = sum(1 for g in range(n) for l in range(8) if zb[8*g+l] == zn[8*g+l])
        print(f"  A/{name}: C reference agrees on mask/signed/gt+({n} words); "
              f"expand round-trip = {len(lanes)} lanes; lanes equal in z (LT^GT = 1 there) = {eq}")
    return steps


def check_mask_layout(kern, cbin=None):
    """T2: every one of the 256 lane masks, constructed (not random), through the kernel, plus the
    little-endian byte order of the s16i word and the expansion round trip."""
    zb, zn = [], []
    for m in range(256):
        for l in range(8):
            # lane l keeps <=> z_new < z_buf; make it 1 lower or 1 higher
            zb += [1000]
            zn += [999 if (m >> l) & 1 else 1001]
    n = 256
    words, steps, untouched, mm = run_depth(kern, zb, zn, n)
    assert words == list(range(256)), f"constructed masks: got {words[:16]} ..."
    raw = bytes(mm.mem[ADDR_MASK:ADDR_MASK + 2 * n])
    assert raw == pack16(list(range(256))), "s16i byte order is not little-endian"
    lanes = [0xFFFF if (words[g] >> l) & 1 else 0 for g in range(n) for l in range(8)]
    if cbin:
        res = c_run(cbin, 'depth', pack16(zb + zn), n)
        assert res['mask'] == words and res['expand'] == lanes
    print(f"  A/mask_layout: 256 constructed lane masks -> words 0x0000..0x00ff, steps={steps}, "
          f"memory bytes little-endian, VLDBC.16+ANDQ+VCMP.GT.S16 expansion rebuilds all "
          f"{len(lanes)} lanes, guard bands untouched={untouched}")
    return words


def sweep_xorq(pairs=655360):
    """T3: u < v  <=>  (u ^ 0x8000) <s (v ^ 0x8000), over a sweep of lane pairs."""
    rng = random.Random(0x20D3)
    offs = [0, 1, 255, 32767, 32768, 32769, 65534, 65535]
    tested = bad = 0
    for u in range(65536):
        for o in offs:
            v = (u + o) & MASK16
            tested += 1
            if not ((u < v) == (s16(u ^ 0x8000) < s16(v ^ 0x8000))):
                if bad < 3:
                    print(f"    counterexample u={u} v={v}")
                bad += 1
    for _ in range(pairs - tested):
        u, v = rng.randrange(65536), rng.randrange(65536)
        tested += 1
        if not ((u < v) == (s16(u ^ 0x8000) < s16(v ^ 0x8000))):
            bad += 1
    return tested, bad


# --------------------------------------------------------------------------- B: the edge setup

def run_edge(kern, v, edges_init):
    m = M()
    m.mem[ADDR_V:ADDR_V + 32] = pack32(v)
    m.mem[ADDR_EDGES:ADDR_EDGES + 64] = pack32(edges_init)
    steps = run_lines(m, kern, {'a2': ADDR_V, 'a3': ADDR_EDGES})
    return ([m.ld32(ADDR_EDGES + 4 * i) for i in range(16)], steps)


def py_edge_setup(v):
    """The C reference's arithmetic, in Python (saturating int32 differences)."""
    dx, dy = [], []
    for j in range(3):
        k = (j + 1) % 3
        dx.append(sat32(v[2 * k] - v[2 * j]))
        dy.append(sat32(v[2 * k + 1] - v[2 * j + 1]))
    e = [0] * 16
    for j in range(3):
        e[j] = dx[j] & 0xFFFFFFFF
        e[4 + j] = dy[j] & 0xFFFFFFFF
        e[8 + j] = ((1 if dx[j] > 0 else 0) - (1 if dx[j] < 0 else 0)) & 0xFFFFFFFF
        e[12 + j] = ((1 if dy[j] > 0 else 0) - (1 if dy[j] < 0 else 0)) & 0xFFFFFFFF
    return e


SENT = 0x5A5A5A5A


def check_edge(kern, name, tris, cbin=None):
    mism = 0
    sentinels_ok = 0
    sat_cases = 0
    for t in tris:
        got, steps = run_edge(kern, t, [SENT] * 16)
        want = py_edge_setup(t)
        # the four padded lanes hold the sentinel: never written
        assert all(got[i] == SENT for i in (3, 7, 11, 15)), f"{name}: lane 3 written: {got}"
        sentinels_ok += 1
        for i in range(16):
            if i in (3, 7, 11, 15):
                continue
            if got[i] != want[i]:
                mism += 1
        for j in range(3):
            k = (j + 1) % 3
            if abs(t[2 * k] - t[2 * j]) > 2147483647 or abs(t[2 * k + 1] - t[2 * j + 1]) > 2147483647:
                sat_cases += 1
                break
    print(f"  B/{name}: triangles={len(tris)} stored_words_checked={len(tris)*12} "
          f"mismatches={mism} sentinel_planes_intact={sentinels_ok}/{len(tris)} "
          f"triangles_with_a_saturating_difference={sat_cases}")
    assert mism == 0
    if cbin:
        for t in tris[:64]:
            got, _ = run_edge(kern, t, [SENT] * 16)
            res = c_run(cbin, 'edge', pack32(t))
            assert res['edges'] == got, f"{name}: C reference != model on v={t}"
        print(f"  B/{name}: C reference agrees on {min(len(tris),64)} triangles (16 words each, "
              f"incl. the four padded lanes the kernel did not write)")
    return mism


def check_edge_padding(kern):
    """B2: v[6], v[7] over adversarial values must not move a single stored word, and lane 3 of every
    plane must still hold the sentinel.  The last line reports which STORED lane each pad word reaches
    (the answer is meant to be none)."""
    rng = random.Random(0x20ED)
    pads = [0, -1, 0x7FFFFFFF, -0x80000000, 0x12345678, -0x12345678]
    tris = 192
    bad = 0
    lanes_touched = set()
    stored_idx = [i for i in range(16) if i not in (3, 7, 11, 15)]
    for _ in range(tris):
        base = [rng.randrange(-2000, 2000) for _ in range(6)]
        ref = None
        for p in pads:
            for q in pads:
                got, _ = run_edge(kern, base + [p, q], [SENT] * 16)
                stored = [got[i] for i in stored_idx]
                if ref is None:
                    ref = stored
                elif stored != ref:
                    bad += 1
                if any(got[i] != SENT for i in (3, 7, 11, 15)):
                    bad += 1
        # which stored lane can a pad word move?  (pad = 0 vs pad = 1 on each word in turn)
        base0, _ = run_edge(kern, base + [0, 0], [SENT] * 16)
        for idx in (6, 7):
            probe = base + [0, 0]
            probe[idx] = 1
            g2, _ = run_edge(kern, probe, [SENT] * 16)
            for i in stored_idx:
                if g2[i] != base0[i]:
                    lanes_touched.add(i)
    print(f"  B/padding: triangles={tris} pad pairs per triangle={len(pads)**2} "
          f"differences_in_stored_words={bad} ; stored lanes a pad word can reach = "
          f"{sorted(lanes_touched) if lanes_touched else 'none'}")
    assert bad == 0
    assert not lanes_touched, f"a pad word reached a stored lane: {sorted(lanes_touched)}"


def check_edge_signs(kern):
    """B4: sgn() over the representative deltas, all 8x8 (dx, dy) combinations."""
    deltas = [0, 1, -1, 2, -2, 32767, -32768, 2147483647, -2147483648]
    bad = 0
    n = 0
    for dx in deltas:
        for dy in deltas:
            t = [0, 0, dx, dy, 0, 0, 0, 0]          # v0=(0,0) v1=(dx,dy) v2=(0,0)
            got, _ = run_edge(kern, t, [SENT] * 16)
            want_dx = (1 if dx > 0 else 0) - (1 if dx < 0 else 0)
            want_dy = (1 if dy > 0 else 0) - (1 if dy < 0 else 0)
            if got[8] != (want_dx & 0xFFFFFFFF) or got[12] != (want_dy & 0xFFFFFFFF):
                bad += 1
            n += 1
    print(f"  B/signs: (dx,dy) combinations={n} sign_plane_mismatches={bad}")
    assert bad == 0


# --------------------------------------------------------------------------- C: the attribute step

def run_attr(kern, at, ab, n, shift):
    m = M()
    m.mem[ADDR_AT:ADDR_AT + 2 * len(at)] = pack16(at)
    m.mem[ADDR_AB:ADDR_AB + 2 * len(ab)] = pack16(ab)
    steps = run_lines(m, kern, {'a2': ADDR_AT, 'a3': ADDR_AB, 'a4': ADDR_OUT, 'a5': n, 'a6': shift})
    return [m.ld16(ADDR_OUT + 2 * i) for i in range(n * 8)], steps


def py_attr_kernel(at, ab, n, shift):
    """The instructions' semantics (what the C reference computes)."""
    sar = shift & 63
    out = []
    for i in range(n * 8):
        d = sat16(s16(ab[i]) - s16(at[i]))                  # EE.VSUBS.S16
        p = s16(d) * (i & 7)                                # the ramp, 32-bit product
        sh = p >> sar                                       # arithmetic >>
        low = s16(sh & 0xFFFF)                              # low 16 bits: truncation
        out.append(sat16(s16(at[i]) + low) & 0xFFFF)        # EE.VADDS.S16
    return out


def ideal_lane(a_top, a_bot, l, shift):
    """One lane of the textbook lerp: a_top + (a_bot - a_top) * l / 2^shift, floored, saturated."""
    num = (s16(a_bot) - s16(a_top)) * l
    return sat16(s16(a_top) + (num >> shift)) & 0xFFFF


def py_attr_ideal(at, ab, n, shift):
    """The textbook lerp the kernel approximates (what the Q0.16 lane pair means), one lane at a time:
    the exact rational a_top + (a_bot - a_top) * l / 2^shift, floored, with the same final 16-bit
    saturation the kernel's last EE.VADDS.S16 has."""
    return [ideal_lane(at[i], ab[i], i & 7, shift) for i in range(n * 8)]


def classify_attr(at, ab, n, shift, got):
    """Which input lanes hit each of the three named steps, and how many DIVERGE from the textbook
    lerp (with the same final saturation, which the kernel and the lerp share).

    Returns (n_lanes, div, subsat_input, wrap_input, addsat_input, div_not_subsat):
      subsat_input  lanes where the difference is clamped by EE.VSUBS.S16 (p281)
      wrap_input    lanes where (d * l) >> shift is outside int16, so the low-16 store truncates (p198)
      addsat_input  lanes where a_top + step is outside int16, clamped by EE.VADDS.S16 (p146)
      div_not_subsat  divergent lanes that are NOT explained by the VSUBS clamp -- the claim is 0 for
                      shift >= 3, i.e. every divergence comes from the saturating difference."""
    sar = shift & 63
    n_lanes = n * 8
    div = subsat_input = wrap_input = addsat_input = div_not_subsat = 0
    for i in range(n_lanes):
        l = i & 7
        d_raw = s16(ab[i]) - s16(at[i])
        d = sat16(d_raw)
        sh = (d * l) >> sar
        ideal = ideal_lane(at[i], ab[i], l, shift)
        bad = got[i] != ideal
        clamped = d != d_raw
        wrapped = not (-32768 <= sh <= 32767)
        addsat = not (-32768 <= s16(at[i]) + sh <= 32767)
        div += bad
        subsat_input += clamped
        wrap_input += wrapped
        addsat_input += addsat
        if bad and not clamped:
            div_not_subsat += 1
    return n_lanes, div, subsat_input, wrap_input, addsat_input, div_not_subsat


def check_attr(kern, name, at, ab, n, shift, cbin=None, expect_exact=False):
    got, steps = run_attr(kern, at, ab, n, shift)
    ref = py_attr_kernel(at, ab, n, shift)
    bad = [i for i in range(n * 8) if got[i] != ref[i]]
    (n_lanes, div, subsat, wrap, addsat, div_not_sub) = classify_attr(at, ab, n, shift, got)
    print(f"  C/{name}: lanes={n_lanes} steps={steps} model_vs_C_reference_mismatches={len(bad)} "
          f"divergences_from_the_lerp={div} | input lanes: VSUBS.S16_clamped={subsat} "
          f"VMUL_out_of_int16={wrap} VADDS.S16_clamped={addsat} | divergences_not_from_the_VSUBS_clamp"
          f"={div_not_sub}")
    assert not bad, f"{name}: model != instruction-semantics reference at {bad[:3]}"
    if shift >= 3:
        assert div_not_sub == 0 and wrap == 0, f"{name}: a divergence/tuncation the contract forbids"
    if expect_exact:
        assert div == 0, f"{name}: expected exactness, {div} divergences"
    if cbin:
        res = c_run(cbin, 'attr', pack16(at + ab), n, shift)
        assert res['out'] == got, f"{name}: C reference != model"
        print(f"  C/{name}: C reference agrees on {n_lanes} lanes (shift={shift})")
    return div, subsat, wrap, addsat


def sweep_mul_wrap():
    """C3: is the shifted product ever outside int16?  Exhaustive over d for the boundary shifts."""
    res = {}
    for shift in (0, 1, 2, 3):
        bad = 0
        worst = 0
        for d in range(-32768, 32768):
            for l in range(8):
                v = (d * l) >> shift
                worst = max(worst, abs(v))
                if not (-32768 <= v <= 32767):
                    bad += 1
        res[shift] = (bad, worst)
    return res


# --------------------------------------------------------------------------- main

def main(argv):
    cbin = argv[1] if len(argv) > 1 else None
    kern = load_kernels(S_PATH)
    print("ex20_raster_setup.S model -- deterministic (random.Random seeds below)")
    print("kernels read from the .S (not retyped):")
    for name in ('ex20_depth_test', 'ex20_edge_setup', 'ex20_attr_interp'):
        assert name in kern, f"{name} not found in {S_PATH}"
        print(f"  {name}: {len(kern[name])} lines executed by the model")

    rng = random.Random(0x20DEAD)
    print("Kernel A: ex20_depth_test  (XORQ relabel -> VCMP.LT.S16 -> ANDQ -> VMULAS.U16.ACCX -> SRS.ACCX)")
    zb = [rng.randrange(65536) for _ in range(600 * 8)]
    zn = [rng.randrange(65536) for _ in range(600 * 8)]
    check_depth(kern['ex20_depth_test'], "full-range uint16, 600 groups", zb, zn, 600, cbin)
    b15 = [rng.randrange(32768) for _ in range(256 * 8)]
    n15 = [rng.randrange(32768) for _ in range(256 * 8)]
    check_depth(kern['ex20_depth_test'], "15-bit depths, 256 groups", b15, n15, 256, cbin,
                expect_signed_diff=False)
    # boundary-heavy: depths around 32768 so the straddling lanes are dense
    bb = [rng.choice([0, 1, 32767, 32768, 32769, 65535, rng.randrange(65536)]) for _ in range(256 * 8)]
    nn = [rng.choice([0, 1, 32767, 32768, 32769, 65535, rng.randrange(65536)]) for _ in range(256 * 8)]
    check_depth(kern['ex20_depth_test'], "boundary-heavy, 256 groups", bb, nn, 256, cbin,
                expect_signed_diff=True)
    check_mask_layout(kern['ex20_depth_test'], cbin)
    tested, bad = sweep_xorq()
    print(f"  A/xorq: {tested} lane pairs swept (all u x 8 offsets, plus random): counterexamples to "
          f"u<v <=> (u^0x8000)<s(v^0x8000) = {bad}")
    assert bad == 0

    print("Kernel B: ex20_edge_setup  (VLD.128/L.64/H.64 -> VSUBS.S32 -> VUNZIP.32 -> VCMP+SUBS.S32)")
    tris = [[rng.randrange(-4096, 4096) for _ in range(6)] + [0, 0] for _ in range(2048)]
    check_edge(kern['ex20_edge_setup'], "screen-space triangles, 2048", tris, cbin)
    wide = [[rng.randrange(-(1 << 31), 1 << 31) for _ in range(6)] + [0, 0] for _ in range(2048)]
    check_edge(kern['ex20_edge_setup'], "full-range int32 triangles, 2048", wide, cbin)
    check_edge_padding(kern['ex20_edge_setup'])
    check_edge_signs(kern['ex20_edge_setup'])
    v = [10, 20, 200, 20, 100, 180, 0, 0]
    got, steps = run_edge(kern['ex20_edge_setup'], v, [SENT] * 16)
    print(f"  B/example: v={v} -> steps={steps} edges=" +
          ",".join(f"{x:08x}" for x in got))
    print(f"  B/example: dx={[s32(got[i]) for i in [0,1,2]]} dy={[s32(got[i]) for i in [4,5,6]]} "
          f"sgn(dx)={[s32(got[i]) for i in [8,9,10]]} sgn(dy)={[s32(got[i]) for i in [12,13,14]]} "
          f"padded lanes={[hex(got[i]) for i in (3,7,11,15)]}")

    print("Kernel C: ex20_attr_interp  (VSUBS.S16 -> VMUL.S16 with the lane ramp, SAR=shift -> VADDS.S16)")
    at = [rng.randrange(-26000, 26000) for _ in range(256 * 8)]
    ab = [at[i] + rng.randrange(-4000, 4000) for i in range(256 * 8)]
    check_attr(kern['ex20_attr_interp'], "small deltas, shift=3 (the 8-pixel chunk)", at, ab, 256, 3,
               cbin, expect_exact=True)
    at2 = [rng.randrange(-32768, 32768) for _ in range(256 * 8)]
    ab2 = [rng.randrange(-32768, 32768) for _ in range(256 * 8)]
    check_attr(kern['ex20_attr_interp'], "full-range int16, shift=3", at2, ab2, 256, 3, cbin)
    check_attr(kern['ex20_attr_interp'], "full-range int16, shift=0", at2, ab2, 256, 0, cbin)
    check_attr(kern['ex20_attr_interp'], "full-range int16, shift=2", at2, ab2, 256, 2, cbin)
    check_attr(kern['ex20_attr_interp'], "full-range int16, shift=15", at2, ab2, 256, 15, cbin)
    q = [rng.choice([-32768, -32767, -1, 0, 1, 32767]) for _ in range(64 * 8)]
    qb = [rng.choice([-32768, 32767, 0, -1, 1]) for _ in range(64 * 8)]
    check_attr(kern['ex20_attr_interp'], "extremal endpoints, shift=3", q, qb, 64, 3, cbin)
    wrap = sweep_mul_wrap()
    for shift, (bad, worst) in wrap.items():
        print(f"  C/mul_wrap: shift={shift} exhaustive over all 65536 int16 d x 8 ramp lanes -> "
              f"results outside int16 = {bad} (worst |result| = {worst})")
    assert wrap[3][0] == 0, "shift=3 must never truncate"
    assert wrap[0][0] > 0 and wrap[1][0] > 0 and wrap[2][0] > 0
    print("  C/mul_wrap: for shift >= 3 the bound is |d|*7/2^shift <= 32768*7/8 = 28672 < 32768, so the "
          "low-16 truncation is unreachable for EVERY int16 d (the exhaustive cases above are the "
          "boundary shifts; the bound is monotone in shift)")

    if not cbin:
        print("(pass the path of the compiled C reference ex20_ref as argv[1] for the C cross-checks)")
    print("RESULT model_checks ok=1 fail=0")


if __name__ == '__main__':
    main(sys.argv)
```

## 7. C 参照（ソース、コンパイル、突き合わせ）

`ex20_ref.c` はファームに組み込むときの比較相手で、**命令の意味から**書いてあります（飽和する所は飽和、
切り捨てる所は切り捨て）。そのぶん CHECK はどの入力でも厳密になり、「テキストブックの式との差」は
別に数える、という ex10 と同じ作りです。

**ターゲット用にコンパイルが通る**こと（ファームに組み込める形か）と、**ホストで実行したときに Python
モデルと同じ値を出す**ことの両方を確認しました。これが無いと「.md に貼った C が動く」は主張でしか
ありません。

```
$ gcc -O2 -Wall -Wextra -o /tmp/ex20_ref /tmp/ex20_ref.c
exit=0（警告なし）

$ xtensa-esp32s3-elf-gcc -c -O2 -o /tmp/ex20_ref.o /tmp/ex20_ref.c
exit=0

$ /tmp/ex20_ref depth /tmp/ex20_in.bin 600 0 | head -2
mask=00ff,...
signed_mask=...

$ /tmp/ex20_ref edge /tmp/edge.bin
edges=000000be,ffffff9c,ffffffa6,5a5a5a5a,...
v=0000000a,00000014,...

$ /tmp/ex20_ref attr /tmp/attr.bin 256 3
shift=3
out=...
```

突き合わせの結果は §6 の出力の `C/...` 行です（カーネル A: 600+256+256 グループ、カーネル B: 64三角形、
カーネル C: 6ケース 2048+2048+2048+2048+2048+512 レーン）。**すべて一致**しています。

```c
/* ex20 -- the scalar C references for examples/firmware/main/proposed/ex20_raster_setup.S.
 *
 * These are the functions a firmware ex20() would compare the kernels against.  They are kept in a
 * standalone file so they can be compiled for the TARGET (does it build for the ESP32-S3?) and for the
 * HOST (does it compute what the Python model says on the same inputs?).
 *
 * The references follow the INSTRUCTIONS' semantics (saturating where the instruction saturates,
 * truncating where it truncates), not a textbook formula: that way a CHECK against them can be exact on
 * any input, and the model run reports the divergence from the textbook formula separately.  Every
 * saturation in here is named with the instruction it comes from.
 *
 * This file is NOT part of the example build: examples/firmware/main/CMakeLists.txt lists its sources
 * and is not modified by the ex20 proposal.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EX20_MAX_LANES 8192

/* ------------------------------------------------------------------ A: the depth test's write mask */

/* keep = (z_new < z_buf) read as UNSIGNED 16-bit depths -- what the kernel computes with the two
 * EE.XORQ relabels in place.  keep_mask[g] bit l = the decision for lane l of group g. */
void ex20_depth_test_c(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask, uint32_t n)
{
    for (uint32_t g = 0; g < n; g++) {
        uint32_t m = 0;
        for (uint32_t l = 0; l < 8; l++) {
            if ((uint32_t)z_new[8 * g + l] < (uint32_t)z_buf[8 * g + l]) {
                m |= 1u << l;
            }
        }
        keep_mask[g] = (uint16_t)m;
    }
}

/* The same kernel WITHOUT the EE.XORQ pair, i.e. the signed reading of the lanes (EE.VCMP.LT.S16 on
 * the raw values).  This is the 15-bit-depth-buffer variant; the model run counts how it differs from
 * the unsigned one (the straddling lanes). */
void ex20_depth_test_signed_c(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask,
                              uint32_t n)
{
    for (uint32_t g = 0; g < n; g++) {
        uint32_t m = 0;
        for (uint32_t l = 0; l < 8; l++) {
            if ((int32_t)(int16_t)z_new[8 * g + l] < (int32_t)(int16_t)z_buf[8 * g + l]) {
                m |= 1u << l;
            }
        }
        keep_mask[g] = (uint16_t)m;
    }
}

/* The mirrored fill rule: keep = (z_new > z_buf), i.e. EE.VCMP.GT.S16.  Same kernel with one mnemonic
 * swapped; the model run reports where it differs from the complement of the LT result (the equal
 * lanes), which is the reason the LT form is the one the kernel implements. */
void ex20_depth_test_gt_c(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask, uint32_t n)
{
    for (uint32_t g = 0; g < n; g++) {
        uint32_t m = 0;
        for (uint32_t l = 0; l < 8; l++) {
            if ((uint32_t)z_new[8 * g + l] > (uint32_t)z_buf[8 * g + l]) {
                m |= 1u << l;
            }
        }
        keep_mask[g] = (uint16_t)m;
    }
}

/* The expansion the composite stage uses, transcribed from the instruction sequence in the .md:
 *   EE.VLDBC.16   q, as        -- the 16-bit mask word broadcast to all eight lanes (p170)
 *   EE.ANDQ       q, q, w      -- lane l keeps bit l's weight 2^l, or 0 (p76)
 *   EE.VCMP.GT.S16 q, q, zero  -- GT against 0 turns a set weight into 0xFFFF, a cleared one into 0
 *                                 (p158)  -> the same 0xFFFF/0 vector EE.VCMP.LT.S16 produced
 * `lanes` gets 8 uint16 per group. */
void ex20_depth_expand_c(const uint16_t *keep_mask, uint16_t *lanes, uint32_t n)
{
    static const uint16_t w[8] = {1, 2, 4, 8, 16, 32, 64, 128};
    for (uint32_t g = 0; g < n; g++) {
        uint16_t word = keep_mask[g];
        for (uint32_t l = 0; l < 8; l++) {
            uint16_t a = (uint16_t)(word & w[l]);            /* the broadcast AND the weight */
            lanes[8 * g + l] = (int16_t)a > 0 ? 0xFFFFu : 0u; /* EE.VCMP.GT.S16 against 0 */
        }
    }
}

/* ------------------------------------------------------------------ B: the edge coefficients */

static int32_t sat32(int64_t x)
{
    return x > 2147483647 ? 2147483647 : (x < -2147483648 ? (-2147483647 - 1) : (int32_t)x);
}

/* v[0..5] = {x0,y0,x1,y1,x2,y2}; v[6],v[7] are padding and are NOT read here -- the kernel reads them
 * (they are inside the bytes 16..31 load) but no output word depends on them, which is the claim the
 * model run checks by driving them over adversarial values.
 *
 * Only lanes 0..2 of each plane are written: edges[3], edges[7], edges[11], edges[15] are untouched. */
void ex20_edge_setup_c(const int32_t *v, int32_t *edges)
{
    int32_t dx[3], dy[3];
    for (int j = 0; j < 3; j++) {
        int k = (j + 1) % 3;
        dx[j] = sat32((int64_t)v[2 * k + 0] - (int64_t)v[2 * j + 0]);   /* EE.VSUBS.S32 (p284) */
        dy[j] = sat32((int64_t)v[2 * k + 1] - (int64_t)v[2 * j + 1]);
    }
    for (int j = 0; j < 3; j++) {
        edges[0 + j] = dx[j];
        edges[4 + j] = dy[j];
        edges[8 + j] = (dx[j] > 0) - (dx[j] < 0);     /* EE.VCMP.GT/LT.S32 + EE.VSUBS.S32 */
        edges[12 + j] = (dy[j] > 0) - (dy[j] < 0);
    }
}

/* ------------------------------------------------------------------ C: span-direction attribute step */

static int16_t sat16i(int32_t x)
{
    return (int16_t)(x > 32767 ? 32767 : (x < -32768 ? -32768 : x));
}

/* out[8g+l] = sat16( a_top + (sat16(a_bot - a_top) * l) >> shift ), the lane ramp {0..7} being l.
 * The steps named: EE.VSUBS.S16 (saturating difference), EE.VMUL.S16 (32-bit product, arithmetic
 * shift by SAR[5:0], LOW 16 BITS stored = a truncation, never a clamp), EE.VADDS.S16 (saturating
 * sum).  shift must be 0..15 for the reference to be the same integer arithmetic the instruction
 * performs; the kernel masks shift into SAR[5:0] (p198). */
void ex20_attr_interp_c(const int16_t *a_top, const int16_t *a_bot, int16_t *out, uint32_t n,
                        uint32_t shift)
{
    uint32_t sar = shift & 63;
    for (uint32_t i = 0; i < n * 8; i++) {
        int32_t d = (int32_t)a_bot[i] - (int32_t)a_top[i];
        d = d > 32767 ? 32767 : (d < -32768 ? -32768 : d);          /* EE.VSUBS.S16 */
        int32_t p = d * (int32_t)(i & 7);                            /* the ramp, in 32 bits */
        int32_t sh = p >> sar;                                       /* arithmetic shift */
        int32_t low = (int16_t)(sh & 0xFFFF);                        /* low 16 bits, no saturation */
        out[i] = sat16i((int32_t)a_top[i] + low);                    /* EE.VADDS.S16 */
    }
}

/* ------------------------------------------------------------------ the host/cross-check driver */

static uint16_t s_mask[EX20_MAX_LANES / 8];
static uint16_t s_expand[EX20_MAX_LANES];
static int32_t  s_v[8];
static int32_t  s_edges[16];
static int16_t  s_out[EX20_MAX_LANES];
static uint8_t  s_in[4 * EX20_MAX_LANES];       /* both input arrays of one call, side by side */

static size_t slurp(const char *path, void *dst, size_t max_bytes)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        exit(2);
    }
    size_t n = fread(dst, 1, max_bytes, f);
    fclose(f);
    return n;
}

static void dump16(const char *tag, const uint16_t *v, size_t n)
{
    printf("%s=", tag);
    for (size_t i = 0; i < n; i++) {
        printf("%s%04x", i ? "," : "", (unsigned)v[i]);
    }
    printf("\n");
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s depth|edge|attr <file> [n] [shift]\n", argv[0]);
        return 2;
    }
    const char *mode = argv[1];
    if (!strcmp(mode, "depth")) {
        size_t n = (size_t)(argc > 3 ? atoi(argv[3]) : 0);
        size_t bytes = n * 8 * 2;
        if (slurp(argv[2], s_in, sizeof s_in) != 2 * bytes) {
            fprintf(stderr, "the depth input must be 2*n*16 bytes\n");
            return 2;
        }
        const uint16_t *zb = (const uint16_t *)s_in;
        const uint16_t *zn = (const uint16_t *)(s_in + bytes);
        ex20_depth_test_c(zb, zn, s_mask, (uint32_t)n);
        dump16("mask", s_mask, n);
        ex20_depth_test_signed_c(zb, zn, s_mask, (uint32_t)n);
        dump16("signed_mask", s_mask, n);
        ex20_depth_test_gt_c(zb, zn, s_mask, (uint32_t)n);
        dump16("gt_mask", s_mask, n);
        ex20_depth_test_c(zb, zn, s_mask, (uint32_t)n);
        ex20_depth_expand_c(s_mask, s_expand, (uint32_t)n);
        dump16("expand", s_expand, n * 8);
    } else if (!strcmp(mode, "edge")) {
        if (slurp(argv[2], s_v, sizeof s_v) != sizeof s_v) {
            fprintf(stderr, "edge needs 8 int32\n");
            return 2;
        }
        memset(s_edges, 0x5A, sizeof s_edges);          /* sentinels in every lane, incl. lane 3 */
        ex20_edge_setup_c(s_v, s_edges);
        printf("edges=");
        for (int i = 0; i < 16; i++) {
            printf("%s%08x", i ? "," : "", (unsigned)s_edges[i]);
        }
        printf("\n");
        printf("v=");
        for (int i = 0; i < 8; i++) {
            printf("%s%08x", i ? "," : "", (unsigned)s_v[i]);
        }
        printf("\n");
    } else if (!strcmp(mode, "attr")) {
        size_t n = (size_t)(argc > 3 ? atoi(argv[3]) : 0);
        uint32_t shift = (uint32_t)(argc > 4 ? atoi(argv[4]) : 0);
        size_t bytes = n * 8 * 2;
        if (slurp(argv[2], s_in, sizeof s_in) != 2 * bytes) {
            fprintf(stderr, "the attr input must be 2*n*16 bytes\n");
            return 2;
        }
        const int16_t *at = (const int16_t *)s_in;
        const int16_t *ab = (const int16_t *)(s_in + bytes);
        ex20_attr_interp_c(at, ab, s_out, (uint32_t)n, shift);
        printf("shift=%u\n", (unsigned)shift);
        dump16("out", (const uint16_t *)s_out, n * 8);
    } else {
        fprintf(stderr, "unknown mode %s\n", mode);
        return 2;
    }
    return 0;
}
```

## 8. piesim.py による解釈実行（生の出力）

`/workspace/pjs-vm/tools/pie/piesim.py` を `/tmp/piesim20.py` に写し、ex20 が使う命令を**疑似コードの
ページ付きで追加**してから、3本とも走らせました（piesim 自身のヘッダが「使う前にその命令を TRM の
疑似コードから追加せよ」と言っている手順です）。追加した差分の全文:

```diff
--- /workspace/pjs-vm/tools/pie/piesim.py	2026-09-13 05:48:15.895028696 +0000
+++ /tmp/piesim20.py	2026-09-15 01:31:39.825149617 +0000
@@ -44,6 +44,27 @@
     return max(-32768, min(32767, v)) & 0xFFFF
 
 
+# --- ex20 additions: the 32-bit lane view and its helpers (TRM 1.8 pseudo-code, pages in the branches
+# --- of Sim.run below).  A 32-bit lane is two 16-bit lanes, low first (lane 2i and 2i+1).
+def s32(v):
+    v &= 0xFFFFFFFF
+    return v - (1 << 32) if v & 0x80000000 else v
+
+
+def sat32(v):
+    return max(-(1 << 31), min((1 << 31) - 1, v)) & 0xFFFFFFFF
+
+
+def lanes32(q):
+    return [q[2 * i] | (q[2 * i + 1] << 16) for i in range(4)]
+
+
+def set_lanes32(q, vals):
+    for i, v in enumerate(vals):
+        q[2 * i] = v & 0xFFFF
+        q[2 * i + 1] = (v >> 16) & 0xFFFF
+
+
 ALU = ('ee.vadds.s16', 'ee.vsubs.s16', 'ee.vmax.s16', 'ee.vmin.s16', 'ee.vcmp.lt.s16',
        'ee.andq', 'ee.orq', 'ee.xorq', 'ee.vmul.s16', 'ee.vmul.u16')
 
@@ -53,6 +74,7 @@
         self.mem = mem
         self.q = [[0] * 8 for _ in range(8)]
         self.qacc = [0] * 8
+        self.accx = 0
         self.sar = 0
         self.ar = {}
         self.count = 0
@@ -129,6 +151,8 @@
                 continue
             self.count += 1
             op, *a = t.replace(',', ' ').split()
+            if op.endswith('.n'):      # ex20: the narrow forms (addi.n/movi.n/nop.n/beqz.n/retw.n)
+                op = op[:-2]
             Q = self.q
 
             def qi(x):
@@ -148,7 +172,7 @@
                 arset(a[0], arv(a[1]) + int(a[2]))
             elif op == 'bnez':
                 if arv(a[0]) != 0:
-                    pc = labels[a[1][:-1]] + 1
+                    pc = labels[a[1].rstrip(':')] + 1
             elif op == 'loopgtz':
                 n, lbl = arv(a[0]), a[1][:-1]
                 if n <= 0:
@@ -216,6 +240,88 @@
                 n = arv(a[1]) & 63
                 self.qacc = [v >> n for v in self.qacc]
                 Q[qi(a[0])] = [sat16(v) for v in self.qacc]
+
+            # ---------------- ex20 additions (each from its TRM pseudo-code) ----------------
+            elif op == 'entry' or op == 'retw':
+                pass
+            elif op == 'nop':
+                pass
+            elif op == 'movi':
+                arset(a[0], int(a[1], 0) & 0xFFFFFFFF)
+            elif op == 'slli':
+                arset(a[0], (arv(a[1]) << int(a[2], 0)) & 0xFFFFFFFF)
+            elif op == 'add':
+                arset(a[0], (arv(a[1]) + arv(a[2])) & 0xFFFFFFFF)
+            elif op == 'or':
+                arset(a[0], arv(a[1]) | arv(a[2]))
+            elif op == 'ssr':                            # ssr as  -> SAR = as[5:0]
+                self.sar = arv(a[0]) & 63
+            elif op == 's16i':                           # TRM: store16(as + imm) = at[15:0]
+                v = arv(a[0]) & 0xFFFF
+                ad = arv(a[1]) + int(a[2], 0)
+                self.mem[ad] = v & 0xFF
+                self.mem[ad + 1] = (v >> 8) & 0xFF
+            elif op == 's32i':                           # TRM: store32(as + imm) = at
+                v, ad = arv(a[0]), arv(a[1]) + int(a[2], 0)
+                for n in range(4):
+                    self.mem[ad + n] = (v >> (8 * n)) & 0xFF
+            elif op == 'beqz':
+                if arv(a[0]) == 0:
+                    pc = labels[a[1].rstrip(':')] + 1
+            elif op == 'ee.vld.h.64.ip':                 # TRM 1.8.92 (p166): qu[127:64] = load64(align8)
+                ad, qi2 = arv(a[1]) & ~7, qi(a[0])
+                for i in range(4):
+                    Q[qi2][4 + i] = self.ld16(ad + 2 * i)
+                arset(a[1], arv(a[1]) + int(a[2], 0))
+            elif op == 'ee.vst.l.64.ip':                 # TRM 1.8.196 (p279): store64(align8) = qv[63:0]
+                ad, qi2 = arv(a[1]) & ~7, qi(a[0])
+                for i in range(4):
+                    v = Q[qi2][i]
+                    self.mem[ad + 2 * i] = v & 0xFF
+                    self.mem[ad + 2 * i + 1] = (v >> 8) & 0xFF
+                arset(a[1], arv(a[1]) + int(a[2], 0))
+            elif op == 'ee.notq':                        # TRM 1.8.42 (p120): qa = ~qx
+                Q[qi(a[0])] = [(~v) & 0xFFFF for v in Q[qi(a[1])]]
+            elif op == 'ee.zero.accx':                   # TRM 1.8.216 (p298)
+                self.accx = 0
+            elif op == 'ee.vmulas.u16.accx':             # TRM 1.8.157 (p240)
+                x, y = Q[qi(a[0])], Q[qi(a[1])]
+                s = self.accx + sum(x[i] * y[i] for i in range(8))
+                self.accx = max(0, min(s, (1 << 40) - 1))
+            elif op == 'ee.srs.accx':                    # TRM 1.8.56 (p134): ACCX >>= as[5:0]; au = sat32
+                sh = arv(a[1]) & 63
+                v = self.accx >> sh
+                self.accx = v
+                arset(a[0], sat32(v))
+            elif op == 'ee.movi.32.q':                   # TRM 1.8.32 (p119): qu[sel4] = as
+                qi2, sel = qi(a[0]), int(a[2])
+                Q[qi2][2 * sel] = arv(a[1]) & 0xFFFF
+                Q[qi2][2 * sel + 1] = (arv(a[1]) >> 16) & 0xFFFF
+            elif op == 'ee.movi.32.a':                   # TRM 1.8.31 (p118): au = qs[sel4]
+                arset(a[1], lanes32(Q[qi(a[0])])[int(a[2])])
+            elif op == 'ee.vcmp.gt.s16':                 # TRM 1.8.80 (p158)
+                x, y = Q[qi(a[1])], Q[qi(a[2])]
+                Q[qi(a[0])] = [0xFFFF if s16(x[i]) > s16(y[i]) else 0 for i in range(8)]
+            elif op == 'ee.vcmp.eq.s16':                 # TRM 1.8.77 (p155)
+                x, y = Q[qi(a[1])], Q[qi(a[2])]
+                Q[qi(a[0])] = [0xFFFF if x[i] == y[i] else 0 for i in range(8)]
+            elif op == 'ee.vcmp.gt.s32':                 # TRM 1.8.81 (p159)
+                x, y = lanes32(Q[qi(a[1])]), lanes32(Q[qi(a[2])])
+                set_lanes32(Q[qi(a[0])], [0xFFFFFFFF if s32(x[i]) > s32(y[i]) else 0 for i in range(4)])
+            elif op == 'ee.vcmp.lt.s32':                 # TRM 1.8.84 (p162)
+                x, y = lanes32(Q[qi(a[1])]), lanes32(Q[qi(a[2])])
+                set_lanes32(Q[qi(a[0])], [0xFFFFFFFF if s32(x[i]) < s32(y[i]) else 0 for i in range(4)])
+            elif op == 'ee.vsubs.s32':                   # TRM 1.8.201 (p284), saturating
+                x, y = lanes32(Q[qi(a[1])]), lanes32(Q[qi(a[2])])
+                set_lanes32(Q[qi(a[0])], [sat32(s32(x[i]) - s32(y[i])) for i in range(4)])
+            elif op == 'ee.vadds.s32':                   # TRM 1.8.74 (p149), saturating
+                x, y = lanes32(Q[qi(a[1])]), lanes32(Q[qi(a[2])])
+                set_lanes32(Q[qi(a[0])], [sat32(s32(x[i]) + s32(y[i])) for i in range(4)])
+            elif op == 'ee.vunzip.32':                   # TRM 1.8.210 (p291), old-value reading:
+                p, r = Q[qi(a[0])], Q[qi(a[1])]          # qs0 = {P0,P2,Q0,Q2}, qs1 = {P1,P3,Q1,Q3}
+                P, R = lanes32(p), lanes32(r)
+                set_lanes32(Q[qi(a[0])], [P[0], P[2], R[0], R[2]])
+                set_lanes32(Q[qi(a[1])], [P[1], P[3], R[1], R[3]])
             else:
                 raise NotImplementedError(op)
         return self.count
```

追加した命令の一覧と出典（差分の中のコメントと同じ）: `ssr` / `movi` / `slli` / `add` / `or` / `s16i` /
`s32i` / `beqz` / `entry`/`retw` / `nop`（Xtensa コア）、`ee.vld.h.64.ip`(p166) / `ee.vst.l.64.ip`(p279) /
`ee.notq`(p120) / `ee.zero.accx`(p298) / `ee.vmulas.u16.accx`(p240) / `ee.srs.accx`(p134) /
`ee.movi.32.q`(p119) / `ee.movi.32.a`(p118) / `ee.vcmp.gt.s16`(p158) / `ee.vcmp.eq.s16`(p155) /
`ee.vcmp.gt.s32`(p159) / `ee.vcmp.lt.s32`(p162) / `ee.vsubs.s32`(p284) / `ee.vadds.s32`(p149) /
`ee.vunzip.32`(p291)。`32bit レーン＝16bit レーン2本`という内部表現に合わせるため、`lanes32()` /
`set_lanes32()` のヘルパも足しています。

```
$ python3 /tmp/ex20_piesim_run.py /tmp/ex20_ref
ex20 through piesim.py (extended copy at /tmp/piesim20.py):
  A/ex20_depth_test: groups=64 instructions=918 words=64 mask_ok=True first_words=['0xc6', '0x20', '0xe8', '0xbc', '0x42', '0xe9']
  A/ex20_depth_test: C reference mask_ok=True signed_variant_differs_on=64 groups
  B/ex20_edge_setup: instructions=33 edges=000000be,ffffff9c,ffffffa6,5a5a5a5a,00000000,000000a0,ffffff60,5a5a5a5a,00000001,ffffffff,ffffffff,5a5a5a5a,00000000,00000001,ffffffff,5a5a5a5a
  B/ex20_edge_setup: padded lanes untouched=True
  B/ex20_edge_setup: dx=[190, -100, -90] dy=[0, 160, -160]
  B/ex20_edge_setup: C reference edges_ok=True
  C/ex20_attr_interp: lanes=256 shift=3 instructions=303 matches_the_lerp=True
  C/ex20_attr_interp: C reference out_ok=True
RESULT piesim_runs ok=1 fail=0
```

**この実行の効き目と限界**: piesim は**カーネル本文のテキストをそのまま**（私のモデルとは別の実装で）実行
するので、レジスタの取り違え・ポストインクリメントの誤り・レーンの並びの誤りはメモリの差として出ます。
逆に、**私が追加した命令の意味については独立ではありません**（同じ疑似コードを私が読んだという前提を
§6 のモデルと共有しています）。独立なのは C 参照とアセンブラです。

## 9. 使った命令と TRM のページ（`data/pie_instructions.json` の `source_page`）

| 命令 | 役割 | source_page |
|---|---|---|
| `EE.VLD.128.IP` | 16バイト読み + 後置インクリメント | 164 |
| `EE.VLD.L.64.IP` / `EE.VLD.H.64.IP` | 下位/上位64ビットだけ読み（8バイト整列に丸め） | 168 / 166 |
| `EE.VLDBC.16` | 16bit 1語を8レーンに broadcast（2バイト整列に丸め） | 170 |
| `EE.VST.128.IP` | 16バイト書き | 275 |
| `EE.VST.L.64.IP` | 下位64ビット書き（8バイト整列に丸め） | 279 |
| `EE.ZERO.Q` / `EE.NOTQ` | QR を 0 に / ビット反転 | 299 / 120 |
| `EE.ANDQ` / `EE.ORQ` / `EE.XORQ` | 128bit ビット演算 | 76 / 121 / 297 |
| `EE.VCMP.LT.S16` / `GT.S16` / `EQ.S16` | レーン比較 → `0xFFFF`/0（**符号付きのみ**） | 161 / 158 / 155 |
| `EE.VCMP.GT.S32` / `LT.S32` / `EQ.S32` | 32bit レーン比較 → `0xFFFFFFFF`/0 | 159 / 162 / 156 |
| `EE.VSUBS.S16` / `VADDS.S16` | 飽和加減算（±32767） | 281 / 146 |
| `EE.VSUBS.S32` / `VADDS.S32` | 飽和加減算（±2^31） | 284 / 149 |
| `EE.VMUL.S16` | 32bit 積 → 算術 SAR シフト → **下位16ビット** | 198 |
| `EE.VMULAS.U16.ACCX` | 8レーンの符号なし積和を ACCX（40bit、飽和）に | 240 |
| `EE.ZERO.ACCX` / `EE.SRS.ACCX` | ACCX を 0 に / `ACCX >> as[5:0]` を 32bit 飽和で取り出す | 298 / 134 |
| `EE.VUNZIP.32` | 32bit レーンの de-interleave（偶数/奇数） | 291 |
| `EE.MOVI.32.Q` / `EE.MOVI.32.A` | GPR → 32bit レーン / 32bit レーン → GPR | 119 / 118 |
| `LD.QR`/`ST.QR`/`MV.QR` | **使っていません**（`LD.QR` は索引ロードではない。索引は `LDXQ.32` のみ、p113） | 301 / 302 / 303 |
| `Table 1.7-2`（段） | `EE.VLD.128.IP` 69 / `EE.VST.128.IP` 73 / `EE.VCMP.*.S16` 69 / `EE.VCMP.*.S32` 69 / `EE.ANDQ` 66 / `EE.XORQ` 73 / `EE.VMUL.S16` 70 / `EE.VMULAS.U16.ACCX` 71 / `EE.SRS.ACCX` 68 / `EE.ZERO.ACCX` 73 / `EE.ZERO.Q` 74 / `EE.NOTQ` 68 / `EE.MOVI.32.Q` 68 / `EE.VUNZIP.32` 73 / `EE.VSUB*.S32` 73 | `data/pie_pipeline.json` |
| Table 1.7-2 に**無い**命令 | `RUR.ACCX_0` / `RUR.ACCX_1`（使っていない）、`LD.QR`/`ST.QR`/`MV.QR` | — |
| `ssr` / `movi` / `slli` / `add` / `addi` / `or` / `s16i` / `s32i` / `beqz` / `bnez` | Xtensa コア（PIE の 220 件には入っていない） | — |

## 10. 静的コスト（逆アセンブリからのカウント。**サイクル実測ではありません**）

| カーネル | 命令数（全体） | ループ本体 | プロローグ | バイト数 | 1要素あたり |
|---|---|---|---|---|---|
| `ex20_depth_test` | 36 | 14命令 / 39バイト | 21命令 | 0x66 = 102 | 14/8 = **1.75命令 / 8深度レーン** |
| `ex20_edge_setup` | 33 | ループ無し | （全て直列） | 0x5a = 90 | **33命令 / 三角形**（4平面 × 12バイトを書く） |
| `ex20_attr_interp` | 24 | 9命令 / 25バイト | 14命令 | 0x3f = 63 | 9/8 = **1.125命令 / 8属性レーン** |

`BENCH` 行は**1本もありません**（実機を走らせていないので、この文書に性能の主張はありません）。main.c に
`ex20()` を足すときは、既存の例題と同じ `ex07_ccount()` で挟んで
`BENCH ex20_depth_test groups=...` / `BENCH ex20_edge_setup tris=...` / `BENCH ex20_attr_interp lanes=...`
を出し、`notes/08` のフレーム予算表（135×240、16bpp、SPI 転送 7.7〜15.7 ms、1.3〜1.4 倍係数）に足すのが
次の段です。参考までに、notes/08 の 30 fps 予算（~67〜95 サイクル/px）に対して、`ex20_attr_interp` の
9命令/8レーンは**1レーンあたり1.125命令**、深度テストは**1レーンあたり1.75命令**という**命令数**の
下限を持つ、というのがこの表で言える唯一のことです（サイクルはこれより少なくなりません）。

## 11. 検査が捕まえたもの（正直な記録）

この提案を書く間に、**モデルとアセンブラが実際に間違いを捕まえました**。どちらも「読めば自明」では
ない類なので記録します。

1. **`EE.NOTQ` の出力を「1」のつもりで MAC に渡していた。** 最初の版は
   `EE.ZERO.Q q5` + `EE.NOTQ q5, q5` で「8レーンの 1」を作り、`EE.VMULAS.U16.ACCX q2, q5` としていました。
   `NOTQ(0)` は **0xFFFF（= 65535）**であって 1 ではありません（p120: `qa = ~qx`）。結果は
   `ACCX = Σ w × 65535` で、`SRS.ACCX a6, a8, 0` の読み出しは**マスク `0xff01`**になりました
   （モデルの `A/full-range uint16, 600 groups` 行が `unsigned_mismatches=598` で落ちた）。
   直し方は「1のベクトルを作る」ではなく「**既にある 0x8000 を MAC の被乗数にし、2^15 を `SRS.ACCX` の
   シフトで割る**」で、命令数も2本減りました（§1.2）。
2. **32bit の `add` は 16bit レーンをまたいで桁上げする。** ランプの鎖を `0x00010001` から始めて
   `+0x00020002` していたので、2セグメント目が `{2,3}` ではなく **`{3,3}`** になり、q4 を読むと
   `{0,1,3,3,5,5,7,7}` でした（`C/small deltas` が 767レーン不一致で落ちた）。`0x00010000` から始めれば
   桁上げが起きません（レーンの下位フィールドが 0,2,4,6 で 0xFFFF を越えない）。§3.2 に注記済み。
3. **アセンブラが拒否した3件**（いずれも「書きたい形」が存在しないことの証拠として §5.2 に生出力を貼りました）:
   `s16i` の負オフセット（即値は偶数のみ、負は不可）、`EE.SRS.ACCX` の第3オペランド（**固定 0 フィールド**で、
   シフトはレジスタ側）、`EE.MOVI.32.A` のオペランド順（QR が先）。

1と2は**私の側の誤り**で、命令は疑似コードどおりでした（ex10 / ex12 が記録したのと同じ種類の失敗です）。

## 12. 確認できていないこと（正直な一覧）

実機（ESP32-S3）では**一度も走らせていません**。したがって次は未確認です。

1. **`EE.VUNZIP.32` の意味**（§2.4）。「すべての読みが命令前の値を見る」という読みで
   `VUNZIP.32`/`VZIP.16` が自己矛盾なく de-interleave/interleave になりますが、**この読みを実機で
   確かめていません**。ここが違えば `ex20_edge_setup` の dx/dy 平面が入れ替わるので、**実機で最初に
   確かめるべき1件**です（1呼び出しで `edges` の12語を `DATA` に出せば判定できます）。
2. **`EE.VLD.L.64.IP` / `EE.VLD.H.64.IP` が「もう半分を壊さない」こと**（p168/p166 の Operation には
   `qu[63:0] = …` / `qu[127:64] = …` としか書いておらず、「他方の半分はそのまま」は**記述から読んだ**
   結論です）。ここが違えば `q1` の組み立てが壊れます。同じく実機で1命令ずつ確かめられます。
3. **`EE.VMUL.S16` の「32bit 積 → 算術 SAR → 下位16ビット格納」**（p198 の説明文と Operation）。
   説明文は "The eight 32-bit data results ... is arithmetically right-shifted ... Then, the lower
   16-bit da[ta]" で、Operation は `(qx*qy) >> SAR[5:0]` と書いてあります。**積が 32bit で計算される**
   という私の読み（＝ §3.3 の「shift ≥ 3 なら切り捨ては起きない」）は、この2つの記述の組み合わせから
   出したもので、実機では確かめていません。もし積が 16bit で切り捨てられるとすれば、シフトが 1 でも
   結果が変わり、結論が変わります。
4. **`EE.SRS.ACCX` の書き戻しとシフト順**（p134: シフト結果を ACCX に書き戻し、32bit 飽和した値を au へ）。
   このカーネルは ACCX をグループごとにゼロにするので書き戻しの影響は受けませんが、その前提も実機未確認。
5. **Table 1.7-2 の段**（§4）。`data/pie_pipeline.json` をそのまま読み、D 則を適用しただけで、
   サイクルは測っていません。`EE.VMULAS.*` の ACCX 書き込みが表の def に載っていない（notes/07 の
   既知の穴）こともあり、MAC → SRS の D=2 は表から出した値であって実測ではありません。
6. **`EE.XORQ` を使った符号なし比較の等価性**は 655360 組で反例 0 ですが、**実機のビット演算が
   128bit 全体で正しいか**は確認していません（意味としては p297 の `qa = qx ^ qy` そのものです）。
7. **16バイト整列契約**: `z_buf`/`z_new`/`v`/`edges`/`a_top`/`a_bot`/`out` は 16バイト整列が前提で、
   非整列を渡したときの結果は未検証です（というより、p49 の丸めで**静かに隣を読む**ことは ex03 が
   実機で再現済み）。`keep_mask` は2バイト整列で足ります。
8. **`n` の上限**: `keep_mask` は1グループ1語なので `n` 語。`n` が 0 のときは何にも触りません（両カーネル）。
   途中で `n` が巨大なときのカウンタ（`addi.n a5, a5, -1`）のオーバーフローは考えていません。
9. **`keep_mask` のパック形を使う側**（合成段）は、この提案では書いていません（§1.5 の展開列が
   `ANDQ` + `VCMP.GT.S16` + broadcast の3命令という**意味の確認**までです。実際に合成カーネルに
   組み込んだときの命令数・レジスタ繰りは未検証）。
10. **ABI**: 3本とも leaf 関数（`entry a1, 32` / `retw.n`）で、a2..a5 を引数、a6..a11 を作業用に使い、
    **a12..a15 は触っていません**（ex12/ex14/ex15 は a12 以降も使っていますが、ここではその必要が
    無かったので避けました）。QR は保存していません（この例題集の他のカーネルと同じ。qr0-qr7 が
    タスク切替でどう扱われるかは `docs/pie-simd.md` の「1.3〜1.4 倍」の話で、**未確認**）。

## 13. 既存例題との関係（重複しないために）

この分野では ex13 / ex14 / ex15 が別の担当で既に書かれています（`notes/08` line 96-104 のリスト）。
ex20 は**セットアップと深度テスト**に絞ってあり、重なる所は次のように切り分けています。

| 例 | 担当 | ex20 と重ならない理由 |
|---|---|---|
| **ex13**（透視除算＋逆数表） | 頂点の `w` で割る、近平面の扱い、クリップ枠 | ex20 は**割り算を1つも持ちません**（`VSUBS`/`VADDS`/`VMUL` だけ）。索引ロード（`LDXQ.32`）も表も使いません |
| **ex14**（スパン塗りラスタライザ＋DDA） | スパンをフレームバッファに塗る、辺を Q16.16 で歩く、クリップ | ex20 は**フレームバッファに1バイトも書きません**。ex20 が出すのは**歩き手が使う係数**（dx, dy, 符号）で、`ex14_edge_dda` の入力側に当たります。ex14 が持つ `VADDS/VMAX/VMIN.S32` のクリップ枠は ex20 にはありません |
| **ex15**（非整列フレームバッファ転送） | `EE.SRCQ.128.ST.INCP` + `SAR_BYTE`、非整列ストア、RMW | ex20 は**非整列アクセスを使いません**。8バイト整列の窓は `VLD.L.64`/`VLD.H.64`（8バイト整列に入る）で作り、`EE.SRC.Q`/`USAR` には触れません（ex15 の .md は `USAR` を17箇所で扱っており、そこが担当の中心です）。ex14/ex15 を含む他の proposed 例題は `VLD.L.64`/`VLD.H.64` を**使っていません**（ex14 は `VST.L.64` をテールのストアに使うだけ）。ストアは 12バイト/平面の整列ストアのみ |
| **ex19**（スプライト合成） | 色のマスク（レーン形）で不透明/半透明/カラーキー | 上の §1.5 に書いたとおり、ex19 は**レーン形**のまま、ex20 は**深度**の判定とその**パック語**（+展開の実測） |
| **ex12**（物理・衝突） | `VCMP.LT/GT.S16` + `ORQ` + `NOTQ` で AABB の分離軸テスト | ex20 の `VCMP` は**深度の順序判定**（XORQ で符号なし化）と**辺の符号**（S32）で、ex12 の箱の判定とは入力も出力も別 |

`notes/08` line 58-66 のパイプライン表では「スプライト合成」と「フレームバッファ転送」の2行が未着手と
なっていますが、これは ex19 / ex15 が担っており、ex20 は表の「3D」の段（頂点変換 ex07 → 透視除算 ex13 →
**セットアップと深度テスト ex20** → スパン塗り ex14）を埋めるものです。

## 14. ビルドへの入れ方（この文書では**適用していません**）

`notes/08` line 116-128 の定型そのままです。この提案では main.c / examples.h / CMakeLists.txt /
tools/ を**一切触っていません**。

1. `examples/firmware/main/proposed/ex20_raster_setup.S` を `examples/firmware/main/ex20_raster.S` に移す。
2. `examples/firmware/main/CMakeLists.txt` の `SRCS` に1行足す。
3. `examples/firmware/main/examples.h` に3つの宣言（引数と戻りの意味をコメントで）。
4. `main.c` に `ex20()`: 決定論的な入力 → カーネル → C 参照（§7 の3つ + `ex20_depth_expand_c`）→
   `CHECK` → `BENCH` → `DATA`（**入力と出力を全部出す**。§6 のモデルは `.S` を読んで再計算するので、
   実機ログに同じ `DATA` 行があれば `tools/check_examples_log.py` に `check_ex20()` を足すだけで
   三つ目の計算ができます）。
5. `tools/check_examples_log.py` に `check_ex20()` と `CHECKS` への登録、`tools/selftest_examples_checker.py`
   の合成ログに `DATA` 行と変異を1つ。
6. `notes/08` の表と `examples/README.md` の一覧を更新。
7. `bash tools/build_examples.sh` → `tools/host_flash_and_log.sh --examples`。

**実機で最初に確かめるべきは §12 の1と2**（`VUNZIP.32` の意味、`VLD.L/H.64` がもう半分を壊さないこと）です。
この2つは「カーネルが正しいか」を決める前提で、どちらも1呼び出しで判定できます。
