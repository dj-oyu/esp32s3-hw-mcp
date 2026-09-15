# ex14 (proposed) — スパン塗りラスタライザ: 128bit で塗り、Q16.16 で辺を歩く

`examples/firmware/main/proposed/ex14_raster.S` — `notes/08-media-3d-perf.md` の量産リスト項目 2
（**「ex10 スパン塗りラスタライザ」**、このリポジトリの採番では ex14）を書いたものです。パイプラインで
言うと、ex07（頂点変換）で座標が出て、ex09 の透視除算の先にある**三角形 → 走査線ごとの水平スパン →
フレームバッファのピクセル**という最後の CPU 段です。ここが終わると、次は
`notes/08` の表の「フレームバッファ転送」（LCD へ送る段）だけが残ります。

2 本のカーネルを書いています。

| カーネル | 何をするか | 主要命令 |
|---|---|---|
| `ex14_span_fill` | 1 走査線上の n 本のスパンを、8 ピクセル（128bit）ずつ `EE.VST.128.IP` で塗る | VLD.128.IP / VST.128.IP / VST.L.64.IP |
| `ex14_edge_dda` | 辺を Q16.16 で歩き、次の走査線の整数スパン x 対を作る（1 ベクタ = int32 4 レーン = 4 本のスパン） | VLDBC.32 / MOVI.32.Q / VADDS.S32 / VSR.32 / VMAX.S32 / VMIN.S32 |

## この文書の検証段（何が確認済みで、何が未確認か）

**実機は一切使っていません。** この文書の数字と一致は次の三つから出ています:

1. `xtensa-esp32s3-elf-gcc` / `nm` / `objdump`（ESP-IDF v6.0.1 のツールチェーン）— 生の出力をそのまま
   貼ってあります（空出力は「空」と書いてあります）。
2. ホストの `gcc -O2` でビルドした C 参照実装（`ex14_ref.c`）と、`.S` の命令 1 つずつを写した
   Python モデル（`ex14_model.py`）の等価性走行 — 乱数スパン・乱数辺・乱数三角形について、
   **フレームバッファの全ワード**を突き合わせています（16/16 PASS、詳細は「Python モデルと
   等価性チェック」節）。
3. `data/pie_instructions.json` の `source_page`（命令ごとに列挙）と `data/pie_hazards.md`（Table 1.7-2）。

確認できていないもの（サイクル数、実機での 128bit ストアの丸め、PIE のパイプライン挙動など）は
最後の「未確認の前提」節に全部並べてあります。ここに書いていないことは主張していません。

## A. `ex14_span_fill` — 契約（レイアウトを厳密に）

```
void ex14_span_fill(int16_t *fb, uint32_t stride_px, uint32_t y, const int32_t *x_pairs,
                    const int16_t *color8, uint32_t n_spans);
```

| 引数 | 意味と**要求** |
|---|---|
| `fb` | 16bpp フレームバッファの先頭。ピクセル `(x, y)` は `fb[y*stride_px + x]`。**16 バイト境界に整列していること** |
| `stride_px` | 行ストライド（**ピクセル数**）。**8 の倍数であること**（8 ピクセル = 16 バイトなので、行頭が必ず 16 バイト境界に乗る） |
| `y` | 走査線番号。範囲チェックはしません（クリップは呼び手の仕事。このペアでは `ex14_edge_dda` のクリップ枠がその役） |
| `x_pairs` | `n_spans` 対の int32 交互配置: `x_pairs[2j]` = x0（含む）、`x_pairs[2j+1]` = x1（含まない）。4 バイト整列（`l32i`） |
| `color8` | int16 × 8。**16 バイト整列、かつ 8 レーンが同じ色であること**（128bit 経路は 8 レーン全部を書き、端の部分はレーン 0 を書くため。T9 がこの契約の重さを示します） |
| `n_spans` | スパン本数 |

各スパンは走査線 `y` の `x0 .. x1-1` だけを塗り、**それ以外のピクセルには一切触りません**。

**バイト算術**（この節がこのカーネルの設計判断そのものです）。ピクセル `p` のアドレスは
`addr(p) = (char *)fb + 2*(y*stride_px + p)` なので、`fb` が 16 バイト整列・`stride_px` が 8 の倍数なら

```
p % 8 == 0  <=>  addr(p) % 16 == 0        (16 バイト = 8 ピクセル)
p % 4 == 0  <=>  addr(p) %  8 == 0        ( 8 バイト = 4 ピクセル)
```

スパン 1 本は 3 つの部分に分かれます。

| 部分 | 範囲 | 命令 | バイト数 |
|---|---|---|---|
| head | `x0 .. head_end-1`、`head_end = min((x0+7) & ~7, x1)` | `s16i`（2 バイトストア） | `2*(head_end-x0)` = 0〜14 バイト |
| body | `head_end` から 8 ピクセル単位、`x+8 <= x1` の間 | `EE.VST.128.IP q6, a12, 16` | 16 バイト / 反復 |
| tail | 残り `r = x1 - (head_end + 8*body)`、0〜7 ピクセル | `r >= 4` なら `EE.VST.L.64.IP`（8 バイト = 4 ピクセル、レーン 0..3）、残り 0〜3 ピクセルは `s16i` | 8 バイト + `2*(r mod 4)` |

```
2*(x1 - x0) = 2*(head_end - x0) + 16*body + [ r >= 4 ? 8 : 0 ] + 2*(r mod 4)
```

この会計が「`[x0, x1)` の外に書かない」ことの検査可能な形です。モデルはこの 3 経路をそのまま実装し、
**ガードバンド**（各行の `[W, stride_px)` は初期センチネルのまま）を含むフレームバッファ全体を C 参照と
突き合わせます（T1/T2/T3/T4）。

### 16 バイトに整列していないスパンで何が起きるか（TRM p49）

`EE.VST.128.IP` の本文はこうです（`data/pie_instructions.json`、`source_page` 275）:

> This instruction forces the lower 4 bits of the access address in register as to 0 and stores the 128
> bits in register qv to memory.

つまり **128bit ストアは部分チャンクを書けません**。ずれたアドレスを渡すと、そのピクセルを含む 16 バイト
チャンク丸ごとを書き、スパンの外のピクセルを壊します（ex03 がロード側で実機再現したのと同じ性質）。
`EE.VST.L.64.IP` / `EE.VST.H.64.IP` は下位 3 ビットを 0 に丸め（p279 / p277）、
`EE.SRCQ.128.ST.INCP` も下位 4 ビットを丸めます（p132）。**`EE.SRC.Q` / `EE.SRCQ.128` は読み出し側の
非整列を直す（2 チャンクのファネルシフト、p125/p132）だけで、ストアアドレスの丸めは変わりません。**
だからこのカーネルは、**部分チャンクを 128bit ストアに任せません**:

* 128bit 経路に入るのは、**チャンク 8 ピクセルが丸ごと `[x0, x1)` に入っているときだけ**。
  そのときアドレスは `2*head_end` で 16 の倍数（`head_end` が 8 の倍数だから）なので、p275 の丸めは
  **恒等**で、この経路がバイト厳密になります。T3 は「チャンク内に収まるスパン」だけを集めて、
  128bit ストアも 64bit ストアも **1 命令も出ない**ことを検査しています（出れば隣を壊すケース）。
* 端（head / tail）は **PIE に 16bit/8bit のストア形が無い**（220 命令の一覧にありません）ので `s16i` で
  正確に塗ります。1 スパンあたり最大 7 + 3 = 10 回のスカラストアで、幅の広いスパンでは
  チャンク 1 個分の 1/4 以下です（コスト表は後半）。

**`EE.ST.128.IP` は存在しません。** 課題では `EE.VST.128` と `EE.ST.128.IP` と書かれていましたが、
`data/pie_instructions.json` の 220 エントリに `EE.ST.128.*` は無く（128bit ストア形は
`EE.VST.128.IP` / `EE.VST.128.XP` / `EE.SRCQ.128.ST.INCP` の 3 つ）、アセンブラも拒否します:

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/probe_st128.o /tmp/probe_st128.S
/tmp/probe_st128.S: Assembler messages:
/tmp/probe_st128.S:8: Error: unknown opcode or format name 'ee.st.128.ip'
exit=1
```

（`probe_st128.S` は `entry a1, 32` / `EE.ST.128.IP q0, a2, 16` / `retw.n` の 8 行です。）
そこで 128bit ストアは `EE.VST.128.IP`、4 ピクセルの端は `EE.VST.L.64.IP` を使っています。

### 棄却の規則（明示的・検査可能）

```
x0 >= x1 のスパンはスキップする。ロードもしない、ストアもしない、フレームバッファに一切触らない。
比較は符号付き（`bge a9, a10`）なので、x1 < x0 も x0 == x1 も同じ経路。
```

T4 は棄却スパンを有効スパンに混ぜた場合と、棄却スパンを除いた場合のバッファが**完全に同一**であること、
そして C 参照とも一致することを検査します（棄却スパン 85 本で 0 ストア）。T5c は
`ex14_edge_dda` のクリップで潰れて `x0 >= x1` になったスパンが、実際にこの棄却経路に落ちることを
確かめています（1 走査線で 63 本）。

## B. `ex14_edge_dda` — 固定小数点の辺歩き、丸め規則と飽和

```
void ex14_edge_dda(int32_t *x_left, int32_t *x_right, int32_t *edge_x, const int32_t *edge_dx,
                   const int32_t *clip, uint32_t n);
```

課題の署名から 1 点だけ変えています: **`edge_x` が `const` ではなくなります。** DDA は状態を持ち越す
ものですから、歩いた結果を書き戻さないと**呼び手が同じ飽和加算を二重に実装する**ことになります
（`edge_x` を `const` にしたままだと、カーネルが 1 走査線ぶん進めた状態を呼び手が別に持たねばならず、
「歩き」の意味が消えます）。書き戻しは `EE.VST.128.IP q0, a4, -16`（ロードで進んだポインタを 16 戻して
格納し、ループ末尾で `addi a4, a4, 16` する）で、余分なのは 8 スパンあたり実質 2 命令です。

**レイアウト（契約）** — `n` 本のスパン、`G = ceil(n/4)` グループ、1 グループ = int32 4 レーン = 4 本:

| 配列 | 形 | 意味 |
|---|---|---|
| `edge_x` | `2*G*4` int32、16 バイト整列、**in place で更新** | 平面 L（左辺）: `edge_x[4g+i]`、平面 R（右辺）: `edge_x[4G + 4g+i]`。Q16.16 |
| `edge_dx` | 同じ形・同じ整列 | 1 走査線あたりの増分（Q16.16）。変更しません |
| `clip` | int32 × 2 = `{clip_lo_px, clip_hi_px}`、4 バイト整列 | `EE.VLDBC.32` が下位 2 ビットを 0 に丸めるので（p173）4 バイト整列が要件。`lo <= hi` |
| `x_left` / `x_right` | `4*G` int32 ずつ、16 バイト整列 | この走査線の整数スパン（`x_left[j]` 含む、`x_right[j]` 含まない）。`j >= n` はパディングレーンの結果で、呼び手は読みません |

**平面が 2 枚に分かれている理由**: 出力が `x_left` / `x_right` の**別配列**なので、128bit ストアを
片方の配列に着地させるには、4 本の左辺をまとめた 128bit ベクタと 4 本の右辺をまとめたベクタが要ります。
演算はすべてレーン並列（レーン間のやり取りが無い）なので、辺の左右をレーンに混ぜても算術は同じですが、
**ストア先が配列で分かれる以上、入力も左右で分けておくのが唯一 128bit で閉じる形**です。

**最終グループのパディングレーン**: 配列は `ceil(n/4)` グループぶんの 4 レーンを持ち、`n` を超える
レーンは入力が不定のまま読まれ、その進んだ値が書き戻されます。すべての命令がレーン並列なので
不定値が有効レーンへ漏れることはありません — T5d は 4 通りの敵対的なパディング
（`0x7FFFFFFF`, `-2^31`, `0x1234ABCD`, `0`）で有効レーンが毎回ビット一致することを検査しています。

**丸め規則**（ヘッダコメントと同じ内容）: 固定小数 `f` は実数 `f / 65536`（Q16.16）を表します。1 走査線の歩みは

```
f' = sat32(f + dx)                    EE.VADDS.S32  (p149: min(max(a+b, -2^31), 2^31-1))
```

スパンの端点は**半開区間のピクセル規則**から出します。

```
ピクセル p がスパン内  <=>  fL/65536 <= p < fR/65536
```

整数ピクセル `p` について `fL <= p < fR  <=>  ceil(fL) <= p < ceil(fR)` なので、**両端とも同じ丸め**です。

```
x = ceil(f / 65536) = floor((f + 0xFFFF) / 65536)
```

カーネルはこれを「飽和バイアス加算 `EE.VADDS.S32`（`0x0000FFFF` の 4 レーン定数）＋ **算術**シフト
`EE.VSR.32`（SAR = 16、p274:「the higher bits are padded with signed bit」）」で評価します。算術シフト
なので負の座標も同じ向きに丸まります（モデルは int64 の厳密な floor 除算と突き合わせて検査）。

**飽和とクリップ**: 歩みの飽和（`VADDS.S32`）が「長い辺が一周しない」ことを保証し、そのあと
**ピクセル指数に対して** `EE.VMAX.S32`（lo のブロードキャスト）→ `EE.VMIN.S32`（hi のブロードキャスト）
でクリップ枠に押し込みます（p183/p192）。クリップはシフトの**後**なので、塗り手は枠の外のスパンを
渡されることがなく、端点が潰れると `x0 >= x1`、つまり A の棄却規則に落ちます。歩みの飽和とクリップの
どちらも**効いている**ことは T5b（飽和が無い＝ラップする実装だと 60 ケース中 60 で別のスパンになる）と
T5c（クリップで潰れたスパンが実際に棄却される）で数字になっています。

**`EE.VSUBS.S32`（p284）は使っていません。** ここには引き算がありません（歩みも ceil も加算だけ）。
ceil の別の書き方 `ceil(f/2^16) = -((-f) >> 16)` は `VSUBS.S32` を平面あたり 2 回使う形で、境界の
1 ピクセル誤差が int32 の下端に移るだけです（グループあたり 2 命令増）。モデルは両方を実装していて、
**誤差はどちらも f の範囲の端（バイアス形は `f > 2^31-1-0xFFFF`、反転形は `f <= -2^31`）だけに出る**
ことを、x の範囲の外れ値を含む 60 ケース・784 レーンで検査しています。

**A と B のつなぎ**: B の出力は左右で分かれた配列なので、A に渡すには `(x_left[j], x_right[j])` を
交互配置に並べる**呼び手側の接着**が必要です（1 対 = 2 語。この .S の外）。実測用の統合テスト
（T6/T7）は、この接着を含む「ドライバ → B（4 本ずつ）→ A（1 本ずつ）」の列を Python で書き、
頂点から有理数で厳密にラスタライズする C 参照と**フレームバッファ全ワード**で比較しています。

## アセンブル結果（生の出力）

課題のコマンドと、その出力をそのまま貼ります（`source /opt/esp-idf/export.sh` の後、
ESP-IDF v6.0.1 / `xtensa-esp32s3-elf-gcc 15.2.0`）。

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex14.o examples/firmware/main/proposed/ex14_raster.S
(no output, exit status 0)

$ xtensa-esp32s3-elf-nm -S /tmp/ex14.o
00000070 00000077 T ex14_edge_dda
00000000 0000006d T ex14_span_fill

$ xtensa-esp32s3-elf-objdump -d /tmp/ex14.o

/tmp/ex14.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex14_span_fill>:
   0:	004136        	entry	a1, 32
   3:	828430        	mull	a8, a4, a3
   6:	1188f0        	slli	a8, a8, 1
   9:	882a      	add.n	a8, a8, a2
   b:	b30064        	ee.vld.128.ip	q6, a6, 0
   e:	0016d2        	l16ui	a13, a6, 0
  11:	056716        	beqz	a7, 6b <ex14_span_fill+0x6b>
  14:	0e0c      	movi.n	a14, 0
  16:	0598      	l32i.n	a9, a5, 0
  18:	15a8      	l32i.n	a10, a5, 4
  1a:	558b      	addi.n	a5, a5, 8
  1c:	46a9a7        	bge	a9, a10, 66 <ex14_span_fill+0x66>
  1f:	8f7c      	movi.n	a15, -8
  21:	b97b      	addi.n	a11, a9, 7
  23:	10bbf0        	and	a11, a11, a15
  26:	022ba7        	blt	a11, a10, 2c <ex14_span_fill+0x2c>
  29:	20baa0        	or	a11, a10, a10
  2c:	11c9f0        	slli	a12, a9, 1
  2f:	cc8a      	add.n	a12, a12, a8
  31:	0919b7        	beq	a9, a11, 3e <ex14_span_fill+0x3e>
  34:	005cd2        	s16i	a13, a12, 0
  37:	cc2b      	addi.n	a12, a12, 2
  39:	991b      	addi.n	a9, a9, 1
  3b:	f529b7        	blt	a9, a11, 34 <ex14_span_fill+0x34>
  3e:	f98b      	addi.n	a15, a9, 8
  40:	092af7        	blt	a10, a15, 4d <ex14_span_fill+0x4d>
  43:	ba01c4        	ee.vst.128.ip	q6, a12, 16
  46:	998b      	addi.n	a9, a9, 8
  48:	ff8b      	addi.n	a15, a15, 8
  4a:	f5aaf7        	bge	a10, a15, 43 <ex14_span_fill+0x43>
  4d:	c0fa90        	sub	a15, a10, a9
  50:	054fa6        	blti	a15, 4, 59 <ex14_span_fill+0x59>
  53:	b401c4        	ee.vst.l.64.ip	q6, a12, 8
  56:	fccff2        	addi	a15, a15, -4
  59:	009f16        	beqz	a15, 66 <ex14_span_fill+0x66>
  5c:	005cd2        	s16i	a13, a12, 0
  5f:	cc2b      	addi.n	a12, a12, 2
  61:	ff0b      	addi.n	a15, a15, -1
  63:	ff5f56        	bnez	a15, 5c <ex14_span_fill+0x5c>
  66:	ee1b      	addi.n	a14, a14, 1
  68:	aa2e77        	blt	a14, a7, 16 <ex14_span_fill+0x16>
  6b:	f01d      	retw.n
  6d:	000000        	ill

00000070 <ex14_edge_dda>:
  70:	004136        	entry	a1, 32
  73:	ed7764        	ee.vldbc.32	q4, a6
  76:	664b      	addi.n	a6, a6, 4
  78:	edf764        	ee.vldbc.32	q5, a6
  7b:	fb7c      	movi.n	a11, -1
  7d:	f5b0b0        	extui	a11, a11, 16, 16
  80:	fd32b4        	ee.movi.32.q	q6, a11, 0
  83:	fd36b4        	ee.movi.32.q	q6, a11, 1
  86:	fd3ab4        	ee.movi.32.q	q6, a11, 2
  89:	fd3eb4        	ee.movi.32.q	q6, a11, 3
  8c:	0c1c      	movi.n	a12, 16
  8e:	400c00        	ssr	a12
  91:	a73b      	addi.n	a10, a7, 3
  93:	41b2a0        	srli	a11, a10, 2
  96:	11abc0        	slli	a10, a11, 4
  99:	8084a0        	add	a8, a4, a10
  9c:	95aa      	add.n	a9, a5, a10
  9e:	043b16        	beqz	a11, e5 <ex14_edge_dda+0x75>
  a1:	20dbb0        	or	a13, a11, a11
  a4:	830144        	ee.vld.128.ip	q0, a4, 16
  a7:	838154        	ee.vld.128.ip	q1, a5, 16
  aa:	8e0874        	ee.vadds.s32	q0, q0, q1
  ad:	ca7f44        	ee.vst.128.ip	q0, a4, -16
  b0:	9e5074        	ee.vadds.s32	q2, q0, q6
  b3:	dd3fa4        	ee.vsr.32	q2, q2
  b6:	9e6234        	ee.vmax.s32	q2, q2, q4
  b9:	9e6a64        	ee.vmin.s32	q2, q2, q5
  bc:	9a0124        	ee.vst.128.ip	q2, a2, 16
  bf:	830184        	ee.vld.128.ip	q0, a8, 16
  c2:	838194        	ee.vld.128.ip	q1, a9, 16
  c5:	8e0874        	ee.vadds.s32	q0, q0, q1
  c8:	ca7f84        	ee.vst.128.ip	q0, a8, -16
  cb:	9e5074        	ee.vadds.s32	q2, q0, q6
  ce:	dd3fa4        	ee.vsr.32	q2, q2
  d1:	9e6234        	ee.vmax.s32	q2, q2, q4
  d4:	9e6a64        	ee.vmin.s32	q2, q2, q5
  d7:	9a0134        	ee.vst.128.ip	q2, a3, 16
  da:	10c442        	addi	a4, a4, 16
  dd:	10c882        	addi	a8, a8, 16
  e0:	dd0b      	addi.n	a13, a13, -1
  e2:	fbed56        	bnez	a13, a4 <ex14_edge_dda+0x34>
  e5:	f01d      	retw.n
```

`ex14_span_fill` = 42 命令 / 109 バイト（`nm -S` の `0x6d`）、`ex14_edge_dda` = 42 命令 / 119 バイト
（`0x77`）。末尾の `ill`（0x6d）は次の関数の `.align 4` パディングです。`mov`/`movi`/`addi`/`l32i` などは
アセンブラが 16bit の `.n` 形に縮めており、`EE.*` はどれも 24bit の 1 語（1 イシュースロット）です。

## 命令あたりのコスト（逆アセンブリからの静的カウント、サイクル実測ではありません）

ループ本体の実測値は逆アセンブリから: **塗りのチャンクループは 4 命令 / 10 バイトで 8 ピクセル**
（`ee.vst.128.ip` + `addi.n` × 2 + `bge`）、**DDA のグループループは 22 命令 / 65 バイトで 4 スパン**
（うち 18 が PIE 命令、残り 4 が `addi`×2 + `addi.n` + `bnez`）。そこからスパン幅ごとの静的コストを
数えるための計算機（静的カウントだけ。サイクルは一切見ていません）:

```python
#!/usr/bin/env python3
"""ex14 static instruction cost per span -- counted from the .S's instruction stream, NOT measured.
The constants are the disassembly's loop bodies (see the .md):
  29 instructions of per-span overhead (rejection, head boundary, body test, tail dispatch, span loop)
   4 per head/tail pixel (s16i, addi.n, addi.n, blt/bnez)
   4 per 8-pixel chunk  (ee.vst.128.ip, addi.n, addi.n, bge)
   1 for the 64-bit tail store when four pixels remain (ee.vst.l.64.ip)"""

BASE, HEAD_PX, CHUNK, V64, TAIL_PX = 29, 4, 4, 1, 4


def count(x0, x1):
    hx = ((x0 + 7) // 8) * 8
    head_end = min(hx, x1)
    n = BASE + HEAD_PX * (head_end - x0)
    st = {"v128": 0, "v64": 0, "scalar": head_end - x0}
    x = head_end
    while x + 8 <= x1:
        n += CHUNK
        st["v128"] += 1
        x += 8
    if x1 - x >= 4:
        n += V64
        st["v64"] += 1
        x += 4
    n += TAIL_PX * (x1 - x)
    st["scalar"] += x1 - x
    return n, st


print("instructions per span, counted from the .S (not a cycle measurement)")
print()
print("| span width | x0 = 8k (aligned) | x0 = 8k+3 (misaligned) |")
print("|---|---|---|")
for w in (1, 2, 4, 7, 8, 16, 32, 64, 128):
    a, sa = count(16, 16 + w)
    b, sb = count(19, 19 + w)
    print("| %3d px | %2d instr: 128b=%d 64b=%d scalar=%d | %2d instr: 128b=%d 64b=%d scalar=%d |"
          % (w, a, sa["v128"], sa["v64"], sa["scalar"], b, sb["v128"], sb["v64"], sb["scalar"]))
print()
for w in (1, 8, 32, 128):
    a, sa = count(16, 16 + w)
    print("%3d px aligned span: %.2f instructions/px, %d 128-bit stores (%.3f per px)"
          % (w, a / w, sa["v128"], sa["v128"] / w))
print()
print("the same span in the DDA: 22 instructions / 65 bytes per group of four spans and scanline")
print("  = 5.50 instructions (16.25 bytes) per span per scanline, 18 of the 22 being PIE ops")
```

実行結果:

```
instructions per span, counted from the .S (not a cycle measurement)

| span width | x0 = 8k (aligned) | x0 = 8k+3 (misaligned) |
|---|---|---|
|   1 px | 33 instr: 128b=0 64b=0 scalar=1 | 33 instr: 128b=0 64b=0 scalar=1 |
|   2 px | 37 instr: 128b=0 64b=0 scalar=2 | 37 instr: 128b=0 64b=0 scalar=2 |
|   4 px | 30 instr: 128b=0 64b=1 scalar=0 | 45 instr: 128b=0 64b=0 scalar=4 |
|   7 px | 42 instr: 128b=0 64b=1 scalar=3 | 57 instr: 128b=0 64b=0 scalar=7 |
|   8 px | 33 instr: 128b=1 64b=0 scalar=0 | 61 instr: 128b=0 64b=0 scalar=8 |
|  16 px | 37 instr: 128b=2 64b=0 scalar=0 | 65 instr: 128b=1 64b=0 scalar=8 |
|  32 px | 45 instr: 128b=4 64b=0 scalar=0 | 73 instr: 128b=3 64b=0 scalar=8 |
|  64 px | 61 instr: 128b=8 64b=0 scalar=0 | 89 instr: 128b=7 64b=0 scalar=8 |
| 128 px | 93 instr: 128b=16 64b=0 scalar=0 | 121 instr: 128b=15 64b=0 scalar=8 |

  1 px aligned span: 33.00 instructions/px, 0 128-bit stores (0.000 per px)
  8 px aligned span: 4.12 instructions/px, 1 128-bit stores (0.125 per px)
 32 px aligned span: 1.41 instructions/px, 4 128-bit stores (0.125 per px)
128 px aligned span: 0.73 instructions/px, 16 128-bit stores (0.125 per px)

the same span in the DDA: 22 instructions / 65 bytes per group of four spans and scanline
  = 5.50 instructions (16.25 bytes) per span per scanline, 18 of the 22 being PIE ops
```

読み方:

* **16 バイト（8 ピクセル）境界に乗った広いスパンでは 128bit ストア 1 命令で 8 ピクセル**なので、
  幅 32 ピクセルで 1.41 命令/px、幅 128 で 0.73 命令/px。`notes/08` のフレーム予算（30 fps で
  CPU 側に約 67 サイクル/px）に対して、塗り段そのものは十分に安い側です。
* **いちばん高いのは非整列の狭いスパン**: 幅 8 ピクセルで `x0 = 8k+3` だと head 5 + tail 3 = 8 回の
  スカラストアで 61 命令です（同じ 8 ピクセルが `x0 = 8k` なら 33 命令 = 1 チャンク）。
  チャンク内に収まるスパン（スプライトの典型）は 4 命令/px で、これは PIE に 16bit ストア形が無い
  ことのコストです。呼び手がスパン開始 x を 8 の倍数に丸めれば、この差はそのまま消えます。
* DDA はスパン幅に依らず **5.5 命令/スパン/走査線**（4 スパンで 22 命令）。幅 8 のスパンで
  0.7 命令/px 相当なので、塗り段と同じ桁です。狭いスパンが多い絵ではこちらが効きます。

## Python モデルと等価性チェック（生の出力）

モデルは `.S` の**命令列そのもの**を写しています（`s16i` の head ループ、`EE.VST.128.IP` のチャンク
ループ、`EE.VST.L.64.IP` の 4 ピクセル、そして `VADDS.S32` の飽和 → バイアス加算 → `VSR.32` の算術
シフト → `VMAX.S32` → `VMIN.S32`）。参照側は `ex14_ref.c`（別言語・別構造のスカラ実装、下の節）で、
`subprocess` で 1 回の走行にまとめて駆動しています。乱数はこのリポジトリと同じ LCG（seed は出力の
1 行目に出ます）。

```python
#!/usr/bin/env python3
"""ex14_raster -- the two kernels modelled from the pseudo-code, and a real equivalence run.

The model implements what the .S IMPLEMENTS, instruction by instruction (the byte-exact store path of
ex14_span_fill, the saturating walk + bias + arithmetic shift + clamp of ex14_edge_dda), and the
counter-models implement the mistakes the .md claims the kernel avoids. The reference side is
ex14_ref.c, compiled with the host gcc and driven over stdin -- a separate, clause-by-clause reading in
another language, so a shared misreading of the manual is unlikely to survive both.

    python3 ex14_model.py [path-to-ex14_ref]        (default /tmp/ex14_ref)

What is checked, and what each check is worth:

  T1 aligned spans      whole 16-byte chunks only: is the 128-bit path right, and does the sentinel
                        guard band (every word outside [x0, x1)) survive?
  T2 unaligned spans    arbitrary x0/x1, one-pixel spans included: the head/tail byte arithmetic.
  T3 inside one chunk   x1-x0 <= 8 inside a single 16-byte chunk: no 128-bit and no 64-bit store may
                        be issued at all (that is the case that would clobber a neighbour if the kernel
                        let the store's address rounding decide).
  T4 rejection          x0 >= x1 mixed with valid spans: the buffer must be identical to the same case
                        with the rejected spans removed, and zero stores must be issued for them.
  T5a DDA normal range  the walk, the ceil, the clip box: model vs an int64-exact reference, the whole
                        padded state array included.
  T5b DDA extremes      int32-edge coordinates and huge slopes: saturation instead of wrap, the
                        documented bias-add boundary, and what a wrapping accumulator would have done.
  T5c clip collapse     edges parked outside the box: x0 >= x1, and the filler writes nothing.
  T5d padding lanes     hostile junk in the padding lanes of the last group must not change the valid
                        lanes' results.
  T6 triangles (exact)  batches of four triangles walked in lockstep with 4-lane DDA calls (and a batch
                        of three, to exercise the padding lane); dy is a power of two, which makes the
                        Q16.16 slope exact, so the kernel pipeline must equal the analytic rational
                        rasteriser pixel for pixel.
  T7 triangles (quant.) the same with an arbitrary dy, where the driver's Q16.16 slope is quantised:
                        model == fixed-point reference exactly, and the gap against the analytic
                        reference is reported (that gap is the driver's, not the kernel's).
  T8 counter-models     the same comparison with the two mistakes the kernels are built to avoid (a
                        128-bit store on a partially covered chunk, and a wrapping accumulator): both
                        must be CAUGHT, or the checks above prove nothing.
  T9 colour contract    non-flat color8: the vector path writes all eight lanes, the sub-chunk edges
                        write lane 0 -- the divergence is the documented caller requirement.
"""
import subprocess
import sys

REF = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ex14_ref"

SAT32_MIN, SAT32_MAX = -(1 << 31), (1 << 31) - 1
PARK = 0x7FFFFFFF                      # how the driver parks a finished edge: far outside any box
COLORS = (0x0011, 0x00AA, 0x07E0, 0x7BDE, 0xF800)


def s32(v):
    """EE.VADDS.S32 / EE.VSUBS.S32 lane semantics (p149 / p284): saturating 32-bit."""
    return SAT32_MIN if v < SAT32_MIN else (SAT32_MAX if v > SAT32_MAX else v)


def fb_init(stride, H):
    return [(0x1200 + (i & 0xFF)) for i in range(stride * H)]


class Rnd:
    """The suite's deterministic LCG (the same shape the firmware uses), so the run is reproducible."""

    def __init__(self, seed):
        self.s = seed

    def r(self, n):
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s % n

    def span(self, lo, hi):
        return lo + self.r(hi - lo + 1)


# ---------------------------------------------------------------------------- the kernel models
def fill_head(x0, x1):
    """hx = (x0 + 7) & ~7, head_end = min(hx, x1) -- the head boundary of the byte math in the .S."""
    hx = (x0 + 7) & ~7
    return min(hx, x1)


def kernel_fill(fb, stride, y, pairs, color8, n, stats=None, store_mode="kernel"):
    """ex14_span_fill as the .S implements it: the scalar head loop, the EE.VST.128.IP chunk loop, one
    EE.VST.L.64.IP for four pixels, then the scalar tail.  store_mode="rounddown" is the counter-model:
    the same kernel written naively, i.e. the first 128-bit store address rounded down to the chunk and
    one chunk per eight pixels from there (what a kernel that trusted the address rounding would do)."""
    row = y * stride
    for j in range(n):
        x0, x1 = pairs[2 * j], pairs[2 * j + 1]
        if x0 >= x1:                                   # the rejection rule: no store of any width
            if stats is not None:
                stats["rejected"] += 1
            continue
        if stats is not None:
            stats["spans"] += 1
            stats["pixels"] += x1 - x0
        if store_mode == "rounddown":
            x = x0 & ~7
            while x < x1:
                for k in range(8):
                    fb[row + x + k] = color8[k] & 0xFFFF
                x += 8
                if stats is not None:
                    stats["v128"] += 1
            continue
        x = x0
        head_end = fill_head(x0, x1)
        while x < head_end:                            # s16i, lane 0, 0..7 pixels (0..14 bytes)
            fb[row + x] = color8[0] & 0xFFFF
            x += 1
            if stats is not None:
                stats["scalar"] += 1
        while x + 8 <= x1:                             # EE.VST.128.IP: 8 pixels = 16 bytes
            for k in range(8):
                fb[row + x + k] = color8[k] & 0xFFFF
            x += 8
            if stats is not None:
                stats["v128"] += 1
        if x1 - x >= 4:                                # EE.VST.L.64.IP: lanes 0..3 = 8 bytes
            for k in range(4):
                fb[row + x + k] = color8[k] & 0xFFFF
            x += 4
            if stats is not None:
                stats["v64"] += 1
        while x < x1:                                  # s16i, lane 0, the last 0..3 pixels
            fb[row + x] = color8[0] & 0xFFFF
            x += 1
            if stats is not None:
                stats["scalar"] += 1


def kernel_dda(xl, xr, ex, edx, clip, n, wrap=False, negate_ceil=False):
    """ex14_edge_dda as the .S implements it: for every group (padding lanes included) and both planes
    the sequence VLD, VLD, VADDS (the walk, written back in place), VADDS (+0xFFFF), VSR.32 (>>16
    arithmetic), VMAX, VMIN, VST.  wrap=True is the counter-model of an accumulator that wraps instead
    of saturating; negate_ceil=True is the alternative ceil spelling ceil(f/2^16) = -((-f) >> 16),
    which needs two saturating subtracts per plane instead of the bias add."""
    G = (n + 3) // 4
    for g in range(G):
        for side in (0, 1):
            base = 0 if side == 0 else 4 * G
            out = xl if side == 0 else xr
            for i in range(4):
                k = base + 4 * g + i
                f = ex[k] + edx[k]
                f = ((f + (1 << 31)) % (1 << 32)) - (1 << 31) if wrap else s32(f)
                ex[k] = f                                        # the in-place write-back store
                if negate_ceil:
                    v = s32(0 - f)                               # EE.VSUBS.S32: zero - f
                    v = v >> 16                                  # EE.VSR.32 (arithmetic)
                    v = s32(0 - v)                               # EE.VSUBS.S32: zero - shifted
                else:
                    v = s32(f + 0xFFFF) >> 16                    # EE.VADDS.S32 with the bias, then VSR
                v = max(v, clip[0])                              # EE.VMAX.S32
                v = min(v, clip[1])                              # EE.VMIN.S32
                out[4 * g + i] = v


def ref_ceil_q16(f):
    """ceil(f/65536) exactly, the way ex14_ref.c reads the contract (int64 floor division)."""
    return (f + 65535) // 65536


# ---------------------------------------------------------------------------- the reference harness
class Ref:
    """Drives ex14_ref (host gcc) over stdin: one response per command, parsed in order."""

    def __init__(self, path):
        self.path = path
        self.cmds = []

    def fill(self, W, H, stride, y, color, pairs):
        s = "FILL %d %d %d %d %d %d" % (W, H, stride, y, color, len(pairs))
        for x0, x1 in pairs:
            s += " %d %d" % (x0, x1)
        self.cmds.append(s)

    def dda(self, n, clip, ex, edx):
        s = "DDA %d %d %d" % (n, clip[0], clip[1])
        s += "".join(" %d" % v for v in ex) + "".join(" %d" % v for v in edx)
        self.cmds.append(s)

    def tri(self, kind, W, H, stride, tris):
        s = "%s %d %d %d %d" % (kind, W, H, stride, len(tris))
        for t in tris:
            s += " " + " ".join("%d" % v for v in t)
        self.cmds.append(s)

    def run(self):
        p = subprocess.run([self.path], input="\n".join(self.cmds) + "\nQUIT\n",
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit("reference exited %d: %s" % (p.returncode, p.stderr.strip()))
        tok = p.stdout.split()
        out = []
        i = 0
        while i < len(tok):
            if tok[i] == "FB":
                n = int(tok[i + 1])
                assert i + 2 + n <= len(tok), "truncated FB block"
                out.append(("FB", [int(t, 16) for t in tok[i + 2:i + 2 + n]]))
                i += 2 + n
            elif tok[i] == "XL":
                n = int(tok[i + 1])
                xl = [int(t) for t in tok[i + 2:i + 2 + n]]
                assert tok[i + 2 + n] == "XR", "XR expected"
                assert int(tok[i + 3 + n]) == n, "XR count differs from the XL count"
                xr = [int(t) for t in tok[i + 4 + n:i + 4 + 2 * n]]
                assert tok[i + 4 + 2 * n] == "N", "N expected"
                assert int(tok[i + 5 + 2 * n]) == 2 * ((n + 3) // 4) * 4, "N count is not 2*G*4"
                assert tok[i + 6 + 2 * n] == "ST", "ST expected"
                N = int(tok[i + 7 + 2 * n])
                st = [int(t) for t in tok[i + 8 + 2 * n:i + 8 + 2 * n + N]]
                out.append(("DDA", (xl, xr, st)))
                i += 8 + 2 * n + N
            elif tok[i].startswith("BADSPAN"):
                raise SystemExit("reference rejected a span the model did not (driver bug)")
            else:
                raise SystemExit("unparsable reference output at token %d: %r" % (i, tok[i:i + 4]))
        assert len(out) == len(self.cmds), (len(out), len(self.cmds))
        return out


RESULTS = []


def check(name, ok, detail):
    RESULTS.append(bool(ok))
    print("%-24s %s  %s" % (name, "PASS" if ok else "FAIL", detail))


def first_mismatch(a, b, stride):
    for i in range(len(a)):
        if a[i] != b[i]:
            return "first mismatch at word %d (row %d, col %d): model=%04x ref=%04x" % (
                i, i // stride, i % stride, a[i], b[i])
    return "no mismatch (but the buffers are not equal in length?)"


def zero_stats():
    return {"spans": 0, "pixels": 0, "scalar": 0, "v128": 0, "v64": 0, "rejected": 0}


# ---------------------------------------------------------------------------- T1/T2/T3
def t_fill_spans(kind, rnd, cases):
    ref = Ref(REF)
    probs = []
    for _ in range(cases):
        W = 8 * rnd.span(2, 22)                    # 16..176 px, always a multiple of 8 (the stride rule)
        stride = W + 8                             # the 8-pixel sentinel guard band in every row
        H = rnd.span(3, 6)
        y = rnd.r(H)
        color = COLORS[rnd.r(len(COLORS))]
        pairs = []
        nchunks = W // 8
        for _s in range(rnd.span(1, 6)):
            if kind == "aligned":
                a = rnd.r(nchunks)
                pairs.append((8 * a, 8 * min(nchunks, a + rnd.span(1, 8))))
            elif kind == "unaligned":
                x0 = rnd.r(W)
                pairs.append((x0, min(W, x0 + rnd.span(1, 40))))
            else:                                  # entirely inside ONE 16-byte chunk
                c = 8 * rnd.r(nchunks)
                a = rnd.span(0, 7)
                pairs.append((c + a, c + rnd.span(a + 1, 8)))
        stats = zero_stats()
        fb = fb_init(stride, H)
        kernel_fill(fb, stride, y, [v for p in pairs for v in p], [color] * 8, len(pairs), stats)
        ref.fill(W, H, stride, y, color, pairs)
        probs.append((stride, pairs, fb, stats))

    tot = zero_stats()
    cases_n = 0
    for (stride, pairs, fb, stats), (_, rfb) in zip(probs, ref.run()):
        if fb != rfb:
            check("T %s spans" % kind, False, first_mismatch(fb, rfb, stride))
            return None
        for k in tot:
            tot[k] += stats[k]
        cases_n += 1
    extra = ""
    if kind == "chunk":
        extra = "; no whole-chunk store was issued for a span inside one chunk: %s" % all(
            p[3]["v128"] == 0 and p[3]["v64"] == 0 for p in probs)
    if kind == "aligned":
        extra = "; no scalar and no 64-bit store either: %s" % (tot["scalar"] == 0 and tot["v64"] == 0)
    check("T %s spans" % kind, True,
          "cases=%d spans=%d pixels=%d stores: 128-bit=%d 64-bit=%d scalar=%d; guard band intact,"
          " every word of every framebuffer equal to the C reference%s"
          % (cases_n, tot["spans"], tot["pixels"], tot["v128"], tot["v64"], tot["scalar"], extra))
    return tot


# ---------------------------------------------------------------------------- T4
def t_rejection(rnd, cases):
    ref = Ref(REF)
    probs = []
    for _ in range(cases):
        W = 8 * rnd.span(2, 22)
        stride = W + 8
        H = rnd.span(2, 5)
        y = rnd.r(H)
        color = 0x7BDE
        spans = []
        for _s in range(rnd.span(2, 8)):
            x0 = rnd.r(W)
            x1 = min(W, x0 + rnd.span(1, 30))
            spans.append((x0, x1))
            kind = rnd.r(4)
            if kind == 0:
                spans.append((x1, x1))                 # empty
            elif kind == 1:
                spans.append((x1, x0))                 # inverted
            elif kind == 2:
                spans.append((x1 + 5, max(0, x1 - 7)))  # negative width
            else:
                spans.append((x1, min(W, x1 + 3)))
        valid = [(a, b) for a, b in spans if a < b]
        stats, stats_valid = zero_stats(), zero_stats()
        fb, fb_valid = fb_init(stride, H), fb_init(stride, H)
        kernel_fill(fb, stride, y, [v for p in spans for v in p], [color] * 8, len(spans), stats)
        kernel_fill(fb_valid, stride, y, [v for p in valid for v in p], [color] * 8, len(valid),
                    stats_valid)
        ref.fill(W, H, stride, y, color, spans)
        ref.fill(W, H, stride, y, color, valid)
        probs.append((stride, spans, valid, fb, fb_valid, stats, stats_valid))

    nrej = 0
    nswept = 0
    for (stride, spans, valid, fb, fb_valid, stats, stats_valid), blk in zip(probs, _pairs(ref.run())):
        if fb != blk[0][1] or fb_valid != blk[1][1]:
            check("T rejection", False, "the span list with rejects differs from the reference")
            return
        if fb != fb_valid:
            check("T rejection", False, "removing the rejected spans changed the framebuffer")
            return
        nrej += stats["rejected"]
        nswept += stats_valid["pixels"]
    check("T rejection", nrej > 0,
          "cases=%d rejected spans=%d pixels swept=%d: the buffer filled with the rejected spans is"
          " identical to the same case without them, and identical to the C reference -- a rejected"
          " span issues zero stores of any width" % (cases, nrej, nswept))


def _pairs(blocks):
    return [(blocks[2 * i], blocks[2 * i + 1]) for i in range(len(blocks) // 2)]


# ---------------------------------------------------------------------------- T5a
def t_dda_normal(rnd, cases):
    ref = Ref(REF)
    probs = []
    for _ in range(cases):
        n = rnd.span(1, 11)
        G = (n + 3) // 4
        lo = rnd.span(0, 120)
        clip = (lo, lo + rnd.span(0, 80))
        ex = [rnd.span(-60 * 65536, 300 * 65536) for _k in range(2 * G * 4)]
        edx = [rnd.span(-3 * 65536, 3 * 65536) for _k in range(2 * G * 4)]
        xl, xr = [0] * (4 * G), [0] * (4 * G)
        ex_m = list(ex)
        kernel_dda(xl, xr, ex_m, edx, clip, n)
        ref.dda(n, clip, ex, edx)
        probs.append((n, clip, ex, edx, xl, xr, ex_m))
    nedge = 0
    for (n, clip, ex, edx, xl, xr, ex_m), (_, (rxl, rxr, rst)) in zip(probs, ref.run()):
        if xl[:n] != rxl or xr[:n] != rxr:
            j = [j for j in range(n) if xl[j] != rxl[j] or xr[j] != rxr[j]][0]
            check("T5a DDA random", False,
                  "lane %d: model=(%d,%d) ref=(%d,%d)" % (j, xl[j], rxl[j], xr[j], rxr[j]))
            return
        if ex_m != rst:
            check("T5a DDA random", False, "the written-back state differs from the reference")
            return
        nedge += 2 * n
    check("T5a DDA random", True,
          "cases=%d edges=%d: the pixel pairs AND the whole padded state array (padding lanes included)"
          " equal the int64-exact reference" % (len(probs), nedge))


# ---------------------------------------------------------------------------- T5b
def t_dda_extremes(rnd, cases):
    ref = Ref(REF)
    probs = []
    wrap_diff = 0
    for _ in range(cases):
        n = rnd.span(1, 8)
        G = (n + 3) // 4
        clip = (rnd.span(0, 60), rnd.span(61, 200))
        ex, edx = [], []
        for _k in range(2 * G * 4):
            pick = rnd.r(5)
            if pick == 0:
                ex.append(rnd.span(0x7FF00000, 0x7FFFFFFF))
                edx.append(rnd.span(0, 0x0FFFFFFF))
            elif pick == 1:
                ex.append(rnd.span(0x7FFFFFFF - 0xFFFF, 0x7FFFFFFF))
                edx.append(rnd.span(0, 4))
            elif pick == 2:
                ex.append(-rnd.span(0x7FF00000, 0x7FFFFFFF))
                edx.append(-rnd.span(0, 0x0FFFFFFF))
            elif pick == 3:
                ex.append(0x7FFFFFFF if (len(ex) & 1) == 0 else SAT32_MIN)
                edx.append(rnd.span(-65536, 0x0FFFFFFF))
            else:
                ex.append(rnd.span(-3 * 65536, 3 * 65536))
                edx.append(rnd.span(-(1 << 27), 1 << 27))
        xl, xr = [0] * (4 * G), [0] * (4 * G)
        ex_m = list(ex)
        kernel_dda(xl, xr, ex_m, edx, clip, n)
        wxl, wxr = [0] * (4 * G), [0] * (4 * G)
        ex_w = list(ex)
        kernel_dda(wxl, wxr, ex_w, edx, clip, n, wrap=True)
        if (xl, xr) != (wxl, wxr):
            wrap_diff += 1
        # the raw ceil of the same states, WITHOUT the box: where the two spellings' saturation shows
        exact = [ref_ceil_q16(ex_m[k]) for k in range(2 * G * 4)]
        bias = [s32(ex_m[k] + 0xFFFF) >> 16 for k in range(2 * G * 4)]
        neg = [s32(0 - (s32(0 - ex_m[k]) >> 16)) for k in range(2 * G * 4)]
        # the claim in the .S: bias-form artifacts sit at the top of the range, negate-form at the
        # bottom, and nowhere else.  Check it instead of asserting it.
        bad_bias = [k for k in range(2 * G * 4)
                    if bias[k] != exact[k] and not ex_m[k] > SAT32_MAX - 0xFFFF]
        bad_neg = [k for k in range(2 * G * 4)
                   if neg[k] != exact[k] and not ex_m[k] <= SAT32_MIN]
        nbias = sum(1 for k in range(2 * G * 4) if bias[k] != exact[k])
        nneg = sum(1 for k in range(2 * G * 4) if neg[k] != exact[k])
        ref.dda(n, clip, ex, edx)
        probs.append((n, clip, xl, xr, bad_bias, bad_neg, nbias, nneg, 2 * G * 4))
    bad_bias_total = 0
    bad_neg_total = 0
    nbias_total = 0
    nneg_total = 0
    nlane = 0
    for (n, clip, xl, xr, bad_bias, bad_neg, nbias, nneg, lanes), (_, (rxl, rxr, rst)) in zip(probs, ref.run()):
        if xl[:n] != rxl or xr[:n] != rxr:
            j = [j for j in range(n) if xl[j] != rxl[j] or xr[j] != rxr[j]][0]
            check("T5b DDA extremes", False,
                  "lane %d: model=(%d,%d) ref=(%d,%d)" % (j, xl[j], rxl[j], xr[j], rxr[j]))
            return
        bad_bias_total += len(bad_bias)
        bad_neg_total += len(bad_neg)
        nbias_total += nbias
        nneg_total += nneg
        nlane += lanes
    check("T5b DDA extremes", wrap_diff > 0 and bad_bias_total == 0 and bad_neg_total == 0,
          "cases=%d lanes=%d: inside the clip box the model equals the int64-exact reference even at the"
          " int32 edges; a WRAPPING accumulator would have produced different spans in %d of the cases"
          " (the saturation is load-bearing); of %d lanes the bias-add ceil differs from the exact"
          " ceil on %d and the negate spelling on %d, and every one of those sits at the documented end"
          " of the range (bias add: f > 2^31-1-0xFFFF only, %d violations of that claim; negate:"
          " f <= -2^31 only, %d violations)"
          % (len(probs), nlane, wrap_diff, nlane, nbias_total, nneg_total, bad_bias_total,
             bad_neg_total))


# ---------------------------------------------------------------------------- T5c
def t_clip_collapse(rnd, cases):
    ref = Ref(REF)
    probs = []
    for _ in range(cases):
        n = rnd.span(1, 8)
        G = (n + 3) // 4
        clip = (rnd.span(0, 20), rnd.span(21, 200))
        ex, edx = [], []
        for _k in range(2 * G * 4):
            pick = rnd.r(3)
            if pick == 0:
                ex.append(PARK)                          # parked: outside the box on the right
                edx.append(0)
            elif pick == 1:
                ex.append(-0x7FFFFFFF)                   # outside on the left
                edx.append(0)
            else:
                ex.append(rnd.span(clip[0] * 65536, clip[1] * 65536))
                edx.append(0)
        xl, xr = [0] * (4 * G), [0] * (4 * G)
        ex_m = list(ex)
        kernel_dda(xl, xr, ex_m, edx, clip, n)
        ref.dda(n, clip, ex, edx)
        probs.append((n, clip, ex_m, xl, xr))
    empty = 0
    swept = 0
    want = 0
    for (n, clip, ex_m, xl, xr), (_, (rxl, rxr, rst)) in zip(probs, ref.run()):
        if xl[:n] != rxl or xr[:n] != rxr:
            check("T5c clip collapse", False, "a clamped span differs from the reference")
            return
        pairs = [v for j in range(n) for v in (xl[j], xr[j])]
        stats = zero_stats()
        fstride = clip[1] + 8                          # the row is as wide as the box (plus a guard)
        fb = fb_init(fstride, 2)
        kernel_fill(fb, fstride, 0, pairs, [0x0011] * 8, n, stats)
        empty += sum(1 for j in range(n) if xl[j] >= xr[j])
        swept += stats["pixels"]
        want += sum(max(0, xr[j] - xl[j]) for j in range(n))
    check("T5c clip collapse", empty > 0 and swept == want,
          "cases=%d collapsed spans (x0 >= x1 after the clamp)=%d: the filler wrote %d pixels, exactly"
          " the surviving widths, and the collapsed spans reached the rejection path"
          % (len(probs), empty, swept))


# ---------------------------------------------------------------------------- T5d
def t_padding_lanes(rnd, cases):
    ok = True
    npad = 0
    for _ in range(cases):
        n = rnd.span(1, 11)
        G = (n + 3) // 4
        clip = (0, 200)
        base_ex = [rnd.span(-50 * 65536, 300 * 65536) for _k in range(2 * G * 4)]
        base_edx = [rnd.span(-2 * 65536, 2 * 65536) for _k in range(2 * G * 4)]
        results = []
        for junk in (0x7FFFFFFF, SAT32_MIN, 0x1234ABCD, 0x00000000):
            ex, edx = list(base_ex), list(base_edx)
            for side in (0, 1):
                for g in range(G):
                    for i in range(4):
                        if 4 * g + i >= n:                     # a padding lane of the last group
                            k = (4 * G if side else 0) + 4 * g + i
                            ex[k], edx[k] = junk, junk
                            npad += 1
            xl, xr = [0] * (4 * G), [0] * (4 * G)
            kernel_dda(xl, xr, ex, edx, clip, n)
            results.append((xl[:n], xr[:n]))
        if any(r != results[0] for r in results):
            ok = False
    check("T5d padding lanes", ok,
          "cases=%d padding-lane writes=%d: four different junk patterns (0x7FFFFFFF, -2^31,"
          " 0x1234ABCD, 0) in the padding lanes, the valid lanes bit-identical every time -- every"
          " instruction in the loop is lane-wise" % (cases, npad))


# ---------------------------------------------------------------------------- T6/T7
def gen_tri(rnd, W, H, aligned):
    """A triangle with a horizontal base: V0 the apex, V1 and V2 on the row y1.  All coordinates are
    Q16.16 multiples of 65536 (pixel aligned) and the height is 2^k pixel rows, so the per-scanline
    slope is an EXACT Q16.16 integer: dx = dX_px * 65536 / 2^k.  aligned=True keeps every edge on a
    multiple of 8 px, i.e. every span starts and ends on a 16-byte chunk boundary."""
    dy = 1 << rnd.span(0, 4)                      # 1..16 rows
    y0 = rnd.span(0, max(0, H - 1 - dy))
    y1 = y0 + dy
    if aligned:
        apex = 8 * rnd.span(-W // 16, (W + W // 2) // 8)
        off1 = -8 * dy * rnd.span(0, max(1, W // (8 * dy)))
        off2 = 8 * dy * rnd.span(1, max(2, W // (8 * dy)))
    else:
        apex = rnd.span(-W // 2, W + W // 2)
        off1 = -rnd.span(0, W)
        off2 = rnd.span(1, W + W // 2)
    return {"color": COLORS[rnd.r(len(COLORS))], "y0": y0, "y1": y1, "X0": apex * 65536,
            "dxL": off1 * 65536 // dy, "dxR": off2 * 65536 // dy,
            "verts": (apex * 65536, y0 * 65536, (apex + off1) * 65536, y1 * 65536,
                      (apex + off2) * 65536, y1 * 65536)}


def drive(rnd, batch, W, H, aligned):
    """The driver: arm the DDA at each apex, one ex14_edge_dda call per scanline for the whole batch,
    then one ex14_span_fill call per span (the filler takes one colour per call, i.e. one call per
    triangle).  Returns the framebuffer and the span statistics."""
    stride = W + 8
    G = (batch + 3) // 4
    clip = (0, W)
    tris = [gen_tri(rnd, W, H, aligned) for _t in range(batch)]
    ex, edx = [PARK] * (2 * G * 4), [0] * (2 * G * 4)
    fb = fb_init(stride, H)
    stats = zero_stats()
    for y in range(H):
        for t in range(batch):
            if y == tris[t]["y0"]:                  # the step this call takes lands on the apex row
                ex[t] = tris[t]["X0"] - tris[t]["dxL"]
                edx[t] = tris[t]["dxL"]
                ex[4 * G + t] = tris[t]["X0"] - tris[t]["dxR"]
                edx[4 * G + t] = tris[t]["dxR"]
            elif y > tris[t]["y1"] - 1:             # finished: parked far outside the box
                ex[t] = PARK
                edx[t] = 0
                ex[4 * G + t] = PARK
                edx[4 * G + t] = 0
        xl, xr = [0] * (4 * G), [0] * (4 * G)
        kernel_dda(xl, xr, ex, edx, clip, batch)
        for t in range(batch):
            kernel_fill(fb, stride, y, [xl[t], xr[t]], [tris[t]["color"]] * 8, 1, stats)
    return tris, stride, fb, stats


def t_triangles(rnd, cases, batch, aligned, kind):
    ref = Ref(REF)
    probs = []
    for _ in range(cases):
        W = 8 * rnd.span(2, 22)
        H = rnd.span(4, 28)
        tris, stride, fb, stats = drive(rnd, batch, W, H, aligned)
        if kind == "exact":
            ref.tri("TRIEXACT", W, H, stride,
                    [tuple([t["color"]] + list(t["verts"])) for t in tris])
        else:
            ref.tri("TRIFIX", W, H, stride,
                    [tuple([t["color"], t["y0"], t["y1"] - 1, t["X0"], t["dxL"], t["X0"], t["dxR"]])
                     for t in tris])
        probs.append((W, stride, tris, fb, stats))
    tot = zero_stats()
    ntri = 0
    for (W, stride, tris, fb, stats), (_, rfb) in zip(probs, ref.run()):
        if fb != rfb:
            name = "T6 exact" if kind == "exact" else "T7 fixed-point"
            check(name, False, first_mismatch(fb, rfb, stride))
            return None
        for k in tot:
            tot[k] += stats[k]
        ntri += len(tris)
    name = {("exact", True): "T6 exact %d aligned" % batch,
            ("exact", False): "T6 exact %d unaligned" % batch,
            ("fixed", True): "T7 fixed-point (aligned)",
            ("fixed", False): "T7 fixed-point (un)"}[(kind, aligned)]
    extra = ""
    if kind == "exact" and aligned:
        extra = "; scalar/64-bit stores: %d" % (tot["scalar"] + tot["v64"])
    if kind == "exact" and not aligned:
        extra = "; scalar=%d 64-bit=%d stores" % (tot["scalar"], tot["v64"])
    check(name, True,
          "cases=%d triangles=%d batch=%d spans=%d rejected=%d pixels=%d%s"
          % (len(probs), ntri, batch, tot["spans"], tot["rejected"], tot["pixels"], extra))
    return None


def t_quantised(rnd, cases):
    """Arbitrary dy: the driver's Q16.16 slope is quantised, so the kernel pipeline and the analytic
    reference must differ.  The check is that the kernel equals the FIXED-POINT reference exactly, and
    the number reported is the driver's quantisation gap."""
    ref_fix = Ref(REF)
    ref_ext = Ref(REF)
    probs = []
    ntri = 0
    pixels = 0
    batch = 4
    quant_units = 0
    quant_frac = 0
    for _ in range(cases):
        W = 8 * rnd.span(2, 22)
        H = rnd.span(6, 28)
        stride = W + 8
        clip = (0, W)
        tris = []
        for _t in range(batch):
            y0 = rnd.span(0, H - 3)
            dy = rnd.span(2, min(12, H - 1 - y0))
            apex = rnd.span(-W, 2 * W)
            off1 = rnd.span(-4 * W, 4 * W)
            off2 = off1 + rnd.span(1, 8 * W)
            tris.append({"color": 0x7BDE, "y0": y0, "y1": y0 + dy, "X0": apex * 65536,
                         "dy": dy, "off": off1,
                         "dxL": off1 * 65536 // dy, "dxR": off2 * 65536 // dy,
                         "verts": (apex * 65536, y0 * 65536, (apex + off1) * 65536, (y0 + dy) * 65536,
                                   (apex + off2) * 65536, (y0 + dy) * 65536)})
        for t in tris:
            # the driver's slope quantisation: dx = off*65536 // dy, so the position drift after m
            # rows is m*frac/dy Q16.16 units with frac = off*65536 - dy*dx in [0, dy-1]
            frac = t["off"] * 65536 - t["dy"] * t["dxL"]
            m = t["dy"] - 1
            quant_units = max(quant_units, 0 if frac == 0 else (m * frac) // t["dy"] + 1)
            quant_frac = max(quant_frac, frac)
        ex, edx = [PARK] * 8, [0] * 8
        fb = fb_init(stride, H)
        stats = zero_stats()
        for y in range(H):
            for t in range(batch):
                if y == tris[t]["y0"]:
                    ex[t] = tris[t]["X0"] - tris[t]["dxL"]
                    edx[t] = tris[t]["dxL"]
                    ex[4 + t] = tris[t]["X0"] - tris[t]["dxR"]
                    edx[4 + t] = tris[t]["dxR"]
                elif y > tris[t]["y1"] - 1:
                    ex[t] = PARK
                    edx[t] = 0
                    ex[4 + t] = PARK
                    edx[4 + t] = 0
            xl, xr = [0] * 4, [0] * 4
            kernel_dda(xl, xr, ex, edx, clip, batch)
            for t in range(batch):
                kernel_fill(fb, stride, y, [xl[t], xr[t]], [tris[t]["color"]] * 8, 1, stats)
        ref_fix.tri("TRIFIX", W, H, stride,
                    [tuple([t["color"], t["y0"], t["y1"] - 1, t["X0"], t["dxL"], t["X0"], t["dxR"]])
                     for t in tris])
        ref_ext.tri("TRIEXACT", W, H, stride,
                    [tuple([t["color"]] + list(t["verts"])) for t in tris])
        ntri += batch
        pixels += stats["pixels"]
        probs.append((stride, fb))
    gap = 0
    worst = 0
    maxdiff = 0
    for (stride, fb), (_, rfix), (_, rext) in zip(probs, ref_fix.run(), ref_ext.run()):
        if fb != rfix:
            check("T7 quantised", False, "model != fixed-point reference: " + first_mismatch(fb, rfix, stride))
            return
        d = sum(1 for a, b in zip(fb, rext) if a != b)
        gap += d
        worst = max(worst, d)
        maxdiff = max(maxdiff, max([abs(a - b) for a, b in zip(fb, rext)] or [0]))
    check("T7 quantised", True,
          "triangles=%d pixels swept=%d: the model equals the FIXED-POINT reference on every word;"
          " against the analytic reference %d words differ (worst %d in one framebuffer).  The driver's"
          " slope quantisation is bounded by %d/65536 units per step and %d units = %.6f px accumulated"
          " over the longest edge in the run, i.e. it cannot move a pixel (that would need the true"
          " edge to sit within that window above an integer boundary)"
          % (ntri, pixels, gap, worst, quant_frac, quant_units, quant_units / 65536.0))


# ---------------------------------------------------------------------------- T8
def t_counter_models(rnd, cases):
    bad_store = 0
    wrap_cases = 0
    store_example = None
    wrap_example = None
    for _ in range(cases):
        W = 8 * rnd.span(2, 22)
        stride = W + 8
        H = 3
        y = 1
        pairs = []
        for _s in range(4):
            x0 = rnd.r(W)
            pairs.append((x0, min(W, x0 + rnd.span(2, 30))))
        fb = fb_init(stride, H)
        kernel_fill(fb, stride, y, [v for p in pairs for v in p], [0x07E0] * 8, len(pairs),
                    store_mode="rounddown")
        fb_ok = fb_init(stride, H)
        kernel_fill(fb_ok, stride, y, [v for p in pairs for v in p], [0x07E0] * 8, len(pairs))
        if fb != fb_ok:
            bad_store += 1
            if store_example is None:
                store_example = (stride, first_mismatch(fb, fb_ok, stride))
        n, clip = 4, (0, W)
        ex = [0x7FFFF000 if (k & 1) == 0 else 0x00100000 for k in range(8)]
        edx = [0x00200000] * 8
        xl, xr = [0] * 8, [0] * 8
        ex_s = list(ex)
        kernel_dda(xl, xr, ex_s, edx, clip, n)
        wxl, wxr = [0] * 8, [0] * 8
        ex_w = list(ex)
        kernel_dda(wxl, wxr, ex_w, edx, clip, n, wrap=True)
        if (xl, xr) != (wxl, wxr):
            wrap_cases += 1
            if wrap_example is None:
                j = next(j for j in range(n) if xl[j] != wxl[j] or xr[j] != wxr[j])
                wrap_example = ("span %d: the saturating kernel says (%d, %d), the wrapping one (%d, %d)"
                                % (j, xl[j], xr[j], wxl[j], wxr[j]))
    check("T8 counter-models", bad_store > 0 and wrap_cases > 0,
          "the rounding-store counter-model disagrees with the correct kernel in %d of %d cases, and"
          " the wrapping-accumulator counter-model in %d -- T1/T2/T4/T5b do detect the two mistakes the"
          " kernels are written to avoid; first mismatches they report: [%s] / [%s]"
          % (bad_store, cases, wrap_cases, store_example[1], wrap_example))


# ---------------------------------------------------------------------------- T9
def t_colour_contract():
    stride = 32
    fb = fb_init(stride, 2)
    nonflat = [0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007, 0x0008]
    kernel_fill(fb, stride, 1, [3, 12], nonflat, 1)
    row = stride
    written = fb[row + 3:row + 12]
    ok = written[0] == 1 and written[4] == 1 and written[5:9] == [1, 2, 3, 4] and len(set(written)) > 1
    check("T9 colour contract", ok,
          "with eight DIFFERENT lanes, the unaligned span 3..11 renders %s: the scalar head wrote lane 0"
          " for pixels 3..7, then EE.VST.L.64.IP wrote lanes 0..3 for pixels 8..11 -- color8 must be"
          " eight copies of one colour, which is what every other case uses" % written)


# ---------------------------------------------------------------------------- main
def main():
    rnd = Rnd(0xE14)
    print("ex14_raster model vs %s" % REF)
    print("LCG seed 0xE14; host gcc reference; no hardware was used for anything in this run.")
    print()
    t_fill_spans("aligned", rnd, 40)
    t_fill_spans("unaligned", rnd, 60)
    t_fill_spans("chunk", rnd, 40)
    t_rejection(rnd, 25)
    t_dda_normal(rnd, 60)
    t_dda_extremes(rnd, 60)
    t_clip_collapse(rnd, 25)
    t_padding_lanes(rnd, 40)
    t_triangles(rnd, 12, 4, True, "exact")
    t_triangles(rnd, 12, 4, False, "exact")
    t_triangles(rnd, 8, 3, False, "exact")
    t_triangles(rnd, 8, 4, True, "fixed")
    t_triangles(rnd, 8, 4, False, "fixed")
    t_quantised(rnd, 8)
    t_counter_models(rnd, 40)
    t_colour_contract()
    print()
    print("ALL CHECKS:", "PASS" if all(RESULTS) else "FAIL", "(%d/%d)" % (sum(RESULTS), len(RESULTS)))


if __name__ == "__main__":
    main()
```

実行（`/tmp/ex14_model.py` と `/tmp/ex14_ref`、Python はリポジトリの `.venv`、標準ライブラリだけで動きます）:

```
$ gcc -O2 -Wall -Wextra -o /tmp/ex14_ref /tmp/ex14_ref.c
$ /workspace/esp32s3-hw-mcp/.venv/bin/python /tmp/ex14_model.py /tmp/ex14_ref
ex14_raster model vs /tmp/ex14_ref
LCG seed 0xE14; host gcc reference; no hardware was used for anything in this run.

T aligned spans          PASS  cases=40 spans=138 pixels=3624 stores: 128-bit=453 64-bit=0 scalar=0; guard band intact, every word of every framebuffer equal to the C reference; no scalar and no 64-bit store either: True
T unaligned spans        PASS  cases=60 spans=218 pixels=3806 stores: 128-bit=327 64-bit=77 scalar=882; guard band intact, every word of every framebuffer equal to the C reference
T chunk spans            PASS  cases=40 spans=115 pixels=288 stores: 128-bit=0 64-bit=0 scalar=288; guard band intact, every word of every framebuffer equal to the C reference; no whole-chunk store was issued for a span inside one chunk: True
T rejection              PASS  cases=25 rejected spans=85 pixels swept=1384: the buffer filled with the rejected spans is identical to the same case without them, and identical to the C reference -- a rejected span issues zero stores of any width
T5a DDA random           PASS  cases=60 edges=730: the pixel pairs AND the whole padded state array (padding lanes included) equal the int64-exact reference
T5b DDA extremes         PASS  cases=60 lanes=784: inside the clip box the model equals the int64-exact reference even at the int32 edges; a WRAPPING accumulator would have produced different spans in 60 of the cases (the saturation is load-bearing); of 784 lanes the bias-add ceil differs from the exact ceil on 400 and the negate spelling on 151, and every one of those sits at the documented end of the range (bias add: f > 2^31-1-0xFFFF only, 0 violations of that claim; negate: f <= -2^31 only, 0 violations)
T5c clip collapse        PASS  cases=25 collapsed spans (x0 >= x1 after the clamp)=63: the filler wrote 2795 pixels, exactly the surviving widths, and the collapsed spans reached the rejection path
T5d padding lanes        PASS  cases=40 padding-lane writes=520: four different junk patterns (0x7FFFFFFF, -2^31, 0x1234ABCD, 0) in the padding lanes, the valid lanes bit-identical every time -- every instruction in the loop is lane-wise
T6 exact 4 aligned       PASS  cases=12 triangles=48 batch=4 spans=218 rejected=654 pixels=10160; scalar/64-bit stores: 0
T6 exact 4 unaligned     PASS  cases=12 triangles=48 batch=4 spans=200 rejected=592 pixels=9428; scalar=600 64-bit=61 stores
T6 exact 3 unaligned     PASS  cases=8 triangles=24 batch=3 spans=59 rejected=334 pixels=2318; scalar=138 64-bit=27 stores
T7 fixed-point (aligned) PASS  cases=8 triangles=32 batch=4 spans=117 rejected=395 pixels=4880
T7 fixed-point (un)      PASS  cases=8 triangles=32 batch=4 spans=114 rejected=454 pixels=3350
T7 quantised             PASS  triangles=32 pixels swept=4609: the model equals the FIXED-POINT reference on every word; against the analytic reference 0 words differ (worst 0 in one framebuffer).  The driver's slope quantisation is bounded by 8/65536 units per step and 8 units = 0.000122 px accumulated over the longest edge in the run, i.e. it cannot move a pixel (that would need the true edge to sit within that window above an integer boundary)
T8 counter-models        PASS  the rounding-store counter-model disagrees with the correct kernel in 38 of 40 cases, and the wrapping-accumulator counter-model in 40 -- T1/T2/T4/T5b do detect the two mistakes the kernels are written to avoid; first mismatches they report: [first mismatch at word 144 (row 1, col 0): model=07e0 ref=1290] / [span 0: the saturating kernel says (136, 136), the wrapping one (0, 0)]
T9 colour contract       PASS  with eight DIFFERENT lanes, the unaligned span 3..11 renders [1, 1, 1, 1, 1, 1, 2, 3, 4]: the scalar head wrote lane 0 for pixels 3..7, then EE.VST.L.64.IP wrote lanes 0..3 for pixels 8..11 -- color8 must be eight copies of one colour, which is what every other case uses

ALL CHECKS: PASS (16/16)
```

検査ごとの意味（何を捕まえるための検査か）:

| 検査 | 何を確定させるか |
|---|---|
| T1 整列スパン | 128bit 経路そのもの。ガードバンドが無傷＝`[x0, x1)` の外に書いていない。スカラストアも 64bit ストアも 1 回も出ない |
| T2 非整列スパン | head/tail のバイト会計。幅 1 ピクセルのスパン、チャンクを跨ぐスパンを含む |
| T3 チャンク内スパン | `x1-x0 <= 8` が 1 チャンクに収まる場合、**128bit ストアも 64bit ストアも出さない**（出せば隣のスパンを壊すケース）。115 スパンで 0 回 |
| T4 棄却 | `x0 >= x1` を混ぜた場合と除いた場合でバッファが完全一致＋C 参照とも一致。棄却 85 本に 0 ストア |
| T5a DDA 通常域 | 歩き・ceil・クリップ枠が int64 厳密参照と一致。書き戻した状態配列（パディングレーン込み）も一致 |
| T5b DDA 極値 | int32 の端の座標と巨大な傾き。**飽和していなければ別のスパンになる**（60/60 ケース）。2 通りの ceil の書き方の境界誤差が文書化した端だけに出る（主張違反 0） |
| T5c クリップ潰れ | 枠外に停めた辺が `x0 >= x1` になり、A の棄却経路に落ちる。書いたピクセル数が残存幅の合計と厳密一致 |
| T5d パディングレーン | 不定値が有効レーンに漏れない（敵対的 4 パターン、520 レーン） |
| T6 三角形（厳密） | ドライバ → DDA（4 レーン、および 3 レーンのパディング経路）→ 塗り、の全体が**有理数の厳密ラスタライザと同じ絵**になる（dy が 2 の冪なので Q16.16 の傾きが厳密に表現できる。整列生成と非整列生成を別々に） |
| T7 三角形（量子化） | 傾きが量子化される dy でも、カーネル列は**固定小数点参照**と全ワード一致。解析参照との差は 0 ワードで、量子化の上限（8/65536 単位 = 0.000122 px）も出している |
| T8 反例モデル | このカーネルが避けている 2 つの間違い（部分チャンクへの 128bit ストア＝アドレス丸め任せ、ラップする累算器）を**検出できる**ことの確認。40 ケース中 38 / 40 で食い違う |
| T9 color8 契約 | 8 レーンが違う色だと、チャンク経路（レーン 0..7）と端の経路（レーン 0）で結果が変わる。`color8` が 8 個同一であることの根拠 |

## C 参照（`ex14_ref.c`、ホストの gcc でビルド）

`ex14_ref.c` は 3 つの独立な読みです: 契約を 1 ピクセルずつ素直に読んだ `ref_span_fill`（チャンクも
整列規則も無い）、int64 で歩いてから int32 に飽和させる厳密な `ref_dda`（ceil は floor 除算であって
バイアス加算ではない）、そして三角形の 2 通り（固定小数点の漸化式を int64 で回す `ref_tri_fixed` と、
頂点から**有理数のまま**辺 x を求めて交差辺の ceil の min/max を取る `ref_tri_exact` = 解析的
ラスタライザ）。フレームバッファは最初に `fb[i] = 0x1200 + (i & 0xFF)` のセンチネルで埋めるので、
`[x0, x1)` の外への書き込みはセンチネルが変わったワードとして必ず出ます。

```
$ gcc -O2 -Wall -Wextra -o /tmp/ex14_ref /tmp/ex14_ref.c
(no output, exit status 0)
```

```c
/* ex14_ref.c -- the scalar references the ex14 kernels are checked against, host gcc only.
 *
 * Three independent readings of the same contract (the suite's MANUAL -> C -> Python discipline: the
 * Python model in ex14_model.py reads the pseudo-code one way, these read it another, and the .S is
 * the third):
 *
 *   ref_span_fill  the clause-by-clause reading of ex14_span_fill: one pixel at a time, no chunking,
 *                  no alignment rule, straight from the contract paragraph. It is deliberately NOT
 *                  written in the kernel's structure -- if it were, a layout mistake would agree.
 *   ref_dda        the exact reading of ex14_edge_dda: the walk is computed in int64 and THEN clamped
 *                  to int32 (the documented saturating accumulator), and the ceiling is an exact
 *                  floor-division by 65536 rather than the kernel's saturating bias-add, so the two
 *                  can only agree if the kernel's shortcut is right.
 *   ref_tri_*      two triangle references for the integration run: one walks the fixed-point edges
 *                  with the int64 recurrence (tri_fixed), one does exact rational edge interpolation
 *                  from the vertices (tri_exact, with min/max over the crossing edges -- the analytic
 *                  rasterisation the fixed-point pipeline is being compared against).
 *
 * Commands on stdin, results on stdout (token-based, one command per query):
 *   FILL <W> <H> <stride> <y> <color> <nspans> <x0> <x1> ...
 *   DDA  <n> <clip_lo> <clip_hi> <2*G*4 edge_x words> <2*G*4 edge_dx words>
 *   TRIFIX   <W> <H> <stride> <ntri> then <color> <yfirst> <ylast> <fxL0> <dxL> <fxR0> <dxR>
 *   TRIEXACT <W> <H> <stride> <ntri> then <color> <x0q> <y0q> <x1q> <y1q> <x2q> <y2q>
 * Every framebuffer starts from the same sentinel pattern the model uses,
 *      fb[i] = 0x1200 + (i & 0xFF)
 * so a store outside [x0, x1) -- or a whole-chunk store that ignores the span -- shows up as a sentinel
 * that changed, in the compare the model does on the whole buffer.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define FBW 2048
#define FBH 512
static int16_t fb[FBW * FBH];
static char cmd[64];

static void fb_init(int stride, int H)
{
    for (int i = 0; i < stride * H; i++)
        fb[i] = (int16_t)(0x1200 + (i & 0xFF));
}

static void fb_dump(int stride, int H)
{
    int n = stride * H;
    printf("FB %d\n", n);
    for (int i = 0; i < n; i++) {
        printf("%04x ", (unsigned)(uint16_t)fb[i]);
        if ((i & 15) == 15)
            printf("\n");
    }
    if (n & 15)
        printf("\n");
}

/* ---------------------------------------------------------------- ex14_span_fill's reference */
static void ref_span_fill(int stride, int y, const int32_t *pairs, int nspans, int16_t color)
{
    int16_t *row = fb + (long)y * stride;

    for (int j = 0; j < nspans; j++) {
        int32_t x0 = pairs[2 * j], x1 = pairs[2 * j + 1];
        if (x0 >= x1)
            continue;                       /* the rejection rule: not one store, nothing at all */
        if (x0 < 0 || x1 > stride) {
            printf("BADSPAN %d %d\n", x0, x1);
            continue;
        }
        for (int32_t x = x0; x < x1; x++)
            row[x] = color;                 /* exactly the pixels x0 .. x1-1 */
    }
}

/* ---------------------------------------------------------------- ex14_edge_dda's reference */
static int64_t sat32(int64_t v)
{
    return v > 2147483647LL ? 2147483647LL : (v < -2147483648LL ? -2147483648LL : v);
}

static int64_t floor_div(int64_t a, int64_t d)      /* d > 0, exact floor for either sign of a */
{
    return a >= 0 ? a / d : -(((-a) + d - 1) / d);
}

static int64_t ceil_q16(int64_t f)                  /* exact ceil(f / 65536) */
{
    return floor_div(f + 65535, 65536);
}

static void ref_dda(int32_t *xl, int32_t *xr, int32_t *st, const int32_t *ex, const int32_t *edx,
                    const int32_t *clip, int n)
{
    int G = (n + 3) / 4, N = 2 * G * 4;

    /* the documented walk: sat32(f + dx), computed in int64 so it cannot wrap on the way */
    for (int k = 0; k < N; k++)
        st[k] = (int32_t)sat32((int64_t)ex[k] + edx[k]);

    for (int j = 0; j < n; j++) {
        int g = j / 4, i = j % 4;
        int64_t a = ceil_q16(st[4 * g + i]);
        int64_t b = ceil_q16(st[4 * G + 4 * g + i]);
        if (a < clip[0]) a = clip[0];
        if (a > clip[1]) a = clip[1];
        if (b < clip[0]) b = clip[0];
        if (b > clip[1]) b = clip[1];
        xl[j] = (int32_t)a;
        xr[j] = (int32_t)b;
    }
}

/* ---------------------------------------------------------------- triangle references */
static int64_t ceil_div(int64_t num, int64_t den)   /* den > 0 */
{
    if (num >= 0)
        return (num + den - 1) / den;
    return -((-num) / den);
}

static void clamp_fill_row(int y, int stride, int64_t a, int64_t b, const int32_t *clip, int16_t color)
{
    if (a < clip[0]) a = clip[0];
    if (a > clip[1]) a = clip[1];
    if (b < clip[0]) b = clip[0];
    if (b > clip[1]) b = clip[1];
    if (a >= b)
        return;                             /* empty span -> rejected, exactly as the filler does */
    int16_t *row = fb + (long)y * stride;
    for (int64_t x = a; x < b; x++)
        row[x] = color;
}

/* exact fixed-point recurrence; the kernel accumulates the same value in int32 with saturation */
static void ref_tri_fixed(int H, int stride, const int32_t *clip, int ntri, const long *t)
{
    for (int k = 0; k < ntri; k++) {
        const long *r = t + 7 * k;                  /* color yfirst ylast fxL0 dxL fxR0 dxR */
        int yfirst = (int)r[1], ylast = (int)r[2];
        int64_t fxL0 = r[3], dxL = r[4], fxR0 = r[5], dxR = r[6];
        for (int y = yfirst; y <= ylast && y < H; y++) {
            int64_t m = y - yfirst;
            int64_t fl = sat32(fxL0 + m * dxL), fr = sat32(fxR0 + m * dxR);
            clamp_fill_row(y, stride, ceil_q16(fl), ceil_q16(fr), clip, (int16_t)r[0]);
        }
    }
}

/* analytic: for every pixel row, take the edges that cross the line Y = y and use ceil of min/max */
static void ref_tri_exact(int H, int stride, const int32_t *clip, int ntri, const long *t)
{
    for (int k = 0; k < ntri; k++) {
        const long *v = t + 7 * k;                  /* color x0 y0 x1 y1 x2 y2 (Q16.16) */
        int16_t color = (int16_t)v[0];
        for (int y = 0; y < H; y++) {
            int64_t yq = (int64_t)y * 65536;
            int64_t lo = 0, hi = 0;
            int nx = 0;
            for (int e = 0; e < 3; e++) {
                int a = e, b = (e + 1) % 3;
                int64_t Xa = v[1 + 2 * a], Ya = v[2 + 2 * a];
                int64_t Xb = v[1 + 2 * b], Yb = v[2 + 2 * b];
                if (!((Ya <= yq && yq < Yb) || (Yb <= yq && yq < Ya)))
                    continue;                       /* this edge does not cross the row */
                int64_t den = Yb - Ya;                            /* Q16.16 all through */
                int64_t num = Xa * den + (Xb - Xa) * (yq - Ya);   /* x_q16 = num/den, exactly */
                if (den < 0) { num = -num; den = -den; }
                int64_t xc = ceil_div(num, den * 65536);          /* ceil(x/65536): the PIXEL index */
                if (nx == 0) { lo = hi = xc; }                    /* the ceils = ceil of min/max */
                else if (xc < lo) lo = xc;
                else if (xc > hi) hi = xc;
                nx++;
            }
            if (nx >= 2)
                clamp_fill_row(y, stride, lo, hi, clip, color);
        }
    }
}

/* ---------------------------------------------------------------- command interpreter */
int main(void)
{
    int32_t pairs[4096], ex[4096], edx[4096], clip[2], xl[4096], xr[4096], st[4096];
    long tri[64 * 7];

    while (scanf("%63s", cmd) == 1) {
        if (!strcmp(cmd, "FILL")) {
            int W, H, stride, y, nspans, n = 0;
            long color;
            if (scanf("%d %d %d %d %ld %d", &W, &H, &stride, &y, &color, &nspans) != 6) break;
            for (int j = 0; j < nspans; j++) {
                if (scanf("%d %d", &pairs[2 * j], &pairs[2 * j + 1]) != 2) { n = 1; break; }
            }
            if (n) break;
            fb_init(stride, H);
            ref_span_fill(stride, y, pairs, nspans, (int16_t)color);
            fb_dump(stride, H);
        } else if (!strcmp(cmd, "DDA")) {
            int n, G, N;
            if (scanf("%d %d %d", &n, &clip[0], &clip[1]) != 3) break;
            G = (n + 3) / 4; N = 2 * G * 4;
            for (int k = 0; k < N; k++) if (scanf("%d", &ex[k]) != 1) break;
            for (int k = 0; k < N; k++) if (scanf("%d", &edx[k]) != 1) break;
            for (int k = 0; k < 4096; k++) xl[k] = xr[k] = 0;
            ref_dda(xl, xr, st, ex, edx, clip, n);
            printf("XL %d", n);
            for (int j = 0; j < n; j++) printf(" %d", xl[j]);
            printf("\nXR %d", n);
            for (int j = 0; j < n; j++) printf(" %d", xr[j]);
            printf("\nN %d\nST %d", N, N);
            for (int k = 0; k < N; k++) printf(" %d", st[k]);
            printf("\n");
        } else if (!strcmp(cmd, "TRIFIX") || !strcmp(cmd, "TRIEXACT")) {
            int W, H, stride, ntri, exact = (cmd[3] == 'E');
            if (scanf("%d %d %d %d", &W, &H, &stride, &ntri) != 4) break;
            clip[0] = 0; clip[1] = W;                 /* the box the kernel is given */
            for (int k = 0; k < ntri; k++)
                for (int f = 0; f < 7; f++) {
                    if (scanf("%ld", &tri[7 * k + f]) != 1) { ntri = k; break; }
                }
            fb_init(stride, H);
            if (exact)
                ref_tri_exact(H, stride, clip, ntri, tri);
            else
                ref_tri_fixed(H, stride, clip, ntri, tri);
            fb_dump(stride, H);
        } else if (!strcmp(cmd, "QUIT")) {
            break;
        } else {
            printf("UNKNOWN %s\n", cmd);
            break;
        }
    }
    return 0;
}
```

## 命令ごとの出典（`data/pie_instructions.json` の `source_page`）

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python - <<'PY'
import json
d = json.load(open('data/pie_instructions.json'))
by = {x['name']: x for x in d}
used = ['EE.VLD.128.IP','EE.VST.128.IP','EE.VST.L.64.IP','EE.VLDBC.32','EE.MOVI.32.Q',
        'EE.VADDS.S32','EE.VSR.32','EE.VMAX.S32','EE.VMIN.S32']
print('%-22s %-11s %s' % ('instruction', 'source_page', 'assembler_syntax'))
for n in used:
    e = by[n]
    print('%-22s %-11d %s' % (n, e['source_page'], e['assembler_syntax']))
print('-- referenced but NOT used in the shipped kernel:')
for n in ['EE.VSUBS.S32', 'EE.SRCQ.128.ST.INCP', 'EE.SRC.Q']:
    e = by[n]
    print('%-22s %-11d %s' % (n, e['source_page'], e['assembler_syntax']))
PY
instruction            source_page assembler_syntax
EE.VLD.128.IP          164         EE.VLD.128.IP qu, as, -2048..2032
EE.VST.128.IP          275         EE.VST.128.IP qv, as, -2048..2032
EE.VST.L.64.IP         279         EE.VST.L.64.IP qv, as, -1024..1016
EE.VLDBC.32            173         EE.VLDBC.32 qu, as
EE.MOVI.32.Q           119         EE.MOVI.32.Q qu, as, 0..3
EE.VADDS.S32           149         EE.VADDS.S32 qa, qx, qy
EE.VSR.32              274         EE.VSR.32 qa, qs
EE.VMAX.S32            183         EE.VMAX.S32 qa, qx, qy
EE.VMIN.S32            192         EE.VMIN.S32 qa, qx, qy
-- referenced but NOT used in the shipped kernel:
EE.VSUBS.S32           284         EE.VSUBS.S32 qa, qx, qy
EE.SRCQ.128.ST.INCP    132         EE.SRCQ.128.ST.INCP qs0, qs1, as
EE.SRC.Q               125         EE.SRC.Q qa, qs0, qs1
```

`VLD.128.IP` / `VST.128.IP` / `VST.L.64.IP` の本文から引いておくと:

* **p164 `EE.VLD.128.IP`** — `qu[127:0] = load128({as[31:4],4{0}})`, `as += {20{imm16[7]},imm16[7:0],4{0}}`。
  16 バイト後置インクリメントと下位 4bit の丸め。
* **p275 `EE.VST.128.IP`** — `qv[127:0] => store128({as[31:4],4{0}})`, `as += {20{imm16[7]},…}`。
  この丸めが「部分チャンクは書けない」の根拠。
* **p279 `EE.VST.L.64.IP`** — `qv[63:0] => store64({as[31:3],3{0}})`, `as += imm8<<3`。4 ピクセルの端に
  使っています（アドレスは 8 バイト整列なので丸めは恒等）。
* **p149 `EE.VADDS.S32`** — 4 レーンの飽和加算（`min(max(…, -2^31), 2^31-1)`）。歩きと ceil バイアス。
* **p274 `EE.VSR.32`** — SAR[5:0] ぶんの**算術**右シフト（符号で埋める）。Q16.16 の >>16 がこれ。
* **p183/p192 `EE.VMAX.S32` / `EE.VMIN.S32`** — レーンごとの大小比較。クリップ枠。
* **p173 `EE.VLDBC.32`** — 32bit を 4 レーンにブロードキャスト（下位 2 アドレスビットを 0 に丸める）。
* **p119 `EE.MOVI.32.Q`** — GPR を Q レジスタの 1 レーンに代入。`0x0000FFFF` の 4 レーン定数を
  `.rodata` から `l32r` せずに作るため（`.iram1` から `.rodata` への `l32r` はリンカが拒否します）。

Table 1.7-2（`data/pie_hazards.md`、TRM 印刷ページ 65–75 の逐語）の該当行:

```
instruction         | Use                | Def        | SR use     | SR def
EE.VADDS.S32        | qx 1, qy 1 | qa 1 | — | —
EE.VMIN.S32         | qx 1, qy 1 | qa 1 | — | —
EE.VMAX.S32         | qx 1, qy 1 | qa 1 | — | —
EE.VLD.128.IP       | as 1 | qu 2, as 1 | — | —
EE.VST.128.IP       | qv 1, as 1 | as 1 | — | —
EE.VST.L.64.IP      | qv 1, as 1 | as 1 | — | —
EE.VLDBC.32         | as 1 | qu 2 | — | —
EE.MOVI.32.Q        | as 1 | qu 1 | — | —
EE.VSR.32           | qs 1 | qa 1 | SAR 1 | —
EE.VSUBS.S32        | qx 1, qy 1 | qa 1 | — | —
EE.SRCQ.128.ST.INCP | qs0 1, qs1 1, as 1 | as 1 | SAR_BYTE 1 | —
```

`VADDS.S32` / `VMIN.S32` / `VMAX.S32` は def も use も `—`（表に段が無い）ので、静的なストール見積りは
0 になります。`VLD.128.IP` の `qu` は M 段、`VST.128.IP` の `as` 更新は M 段です。**この表は
`EE.ZERO.QACC` や `VSMULAS` のアキュムレータ書き込みを載せていない**という穴が ex07/ex09 で問題に
なったのと同じ種類の穴で、ここで使う命令も `VSR.32` の SAR 以外は段が書かれていません。実測はまだ
ありません（BENCH 行が無い理由は最後の節）。

## フレーム予算（`notes/08-media-3d-perf.md`）との関係

`notes/08` の表では、135×240（64800 px）を 30 fps で送ると転送に 13〜16 ms、CPU 側に残るのが
約 18 ms = **約 67 サイクル/px**（240 MHz）です。この段の静的コストは:

| 対象 | 静的コスト |
|---|---|
| 塗り、幅 128 px の整列スパン | 0.73 命令/px（128bit ストア 16 回） |
| 塗り、幅 32 px の整列スパン | 1.41 命令/px |
| 塗り、幅 8 px の整列スパン | 4.12 命令/px |
| 塗り、チャンク内の幅 1 px スパン | 33 命令/px（head のスカラストア 1 回のみ） |
| 塗り、非整列の幅 8 px | 61 命令（8 回のスカラストア） |
| DDA | 5.5 命令/スパン/走査線（幅 8 px なら 0.7 命令/px 相当） |

つまり「広くて 8 の倍数に乗ったスパン」なら 1 px あたり 1 命令前後で、予算に対しては余裕があります。
危ないのは**非整列の狭いスパン**（スプライトの端、細い三角形の先端）で、ここは 1 px あたり数十命令に
なります。対策は 2 つあって、(a) 呼び手がスパン開始 x を 8 の倍数に丸める（8 px の差が 61 → 33 命令に
なる）、(b) 端のチャンクを read-modify-write（`EE.VLD.128.IP` でチャンクを読み、`ANDQ`/`ORQ` で
マスクと合成して書き戻す）にする — 後者はマスクの生成に定数ベクトルが要るのでこの .S には入れていません。
どちらも**サイクルは測っていない**ので、判断は `BENCH` 行を作ってからです。

## 未確認の前提（正直な一覧）

* **実機は使っていません。** この文書の一致は「アセンブラ」「ホスト gcc の C 参照」「Python モデル」の
  三者だけで、PIE の実シリコンは一切見ていません（`proposed/` はビルドに入っていないので、
  `main.c` を触らずに実機へ載せる方法は無い、というのが正しい状況です）。
* **サイクル数は不明。** 命令数とバイト数は逆アセンブリからの静的カウントで、イシューが 1 サイクルに
  1 命令という仮定すら置いていません。`EE.VST.128.IP` のストアが D バスで何サイクルか、`VADDS.S32` の
  レイテンシ、`VSR.32` の SAR 依存（Table 1.7-2 では SAR use = 1 段）はどれも未計測です。
* **128bit ストアの丸めは文書の記述を信じています。** 「下位 4bit を 0 に強制する」は p49/p275 の記述で、
  ロード側は ex03 が実機で再現しましたが、**ストア側**が実機でどう振る舞うかは（このカーネルは
  丸めが恒等なアドレスしか渡さないので）確定していません。丸めが「書き込み自体を止める」ような実装
  だった場合でも、このカーネルの正しさは変わりません（丸めが恒等だから）。
* **`EE.VST.L.64.IP` のレーン対応**（「lower 64 bits」= レーン 0..3 がメモリの低アドレス側）は p279 の
  記述で、モデルはそれに従っています。実機で確認していません。
* **`mull`（MUL32）の存在はアセンブラが受け付けることだけ**が根拠です（ESP32-S3 の Xtensa 構成に
  MUL32 がある前提）。行アドレスは `y*stride_px*2` の 32bit 乗算 1 回で、オーバーフロー検査はありません
  （フレームバッファが 2 GB を超えることはないので実害はありませんが、契約として書いておきます）。
* **`edge_dx` が `const` でない理由**（in place 書き戻し）は設計判断で、課題の署名からの意図的な逸脱です。
  同じく `x_left`/`x_right` は `4*ceil(n/4)` 語を要求します（最終グループのパディングレーンぶん）。
  これは実機で測った話ではなく、モデルと論証だけの話です。
* **ドライバ側は共有物**です。統合テスト（T6/T7）の「頂点 → Q16.16 の初期値と傾き」は
  Python と C が同じ数値を受け取る形にしてあり（カーネルそのものではありません）、交互配置の接着も
  呼び手の仕事です。この 2 つは測っていません。
* **クリップ枠の外の x を渡すと** A はそのまま書きます（範囲チェックはしません）。B のクリップ枠を通す
  限り起こりませんが、契約として明示しておきます。
* **`EE.VSUBS.S32` を使っていない**ことは上に書いたとおりで、これは課題が挙げた命令リストの一部を
  「必要ないので使わない」と判断した結果です（代替形の誤差の出方だけモデルで示しています）。

## ビルドへの入れ方（この文書では**適用していません**）

`notes/08-media-3d-perf.md` の「サンプルの足し方」の手順のうち、`main.c` / `examples.h` /
`CMakeLists.txt` / `tools/` / `data/` / `notes/` に触る部分は**やっていません**（課題の禁止事項）。
入れるときは:

1. `proposed/ex14_raster.S` を `examples/firmware/main/` へ移し（ex10/ex11/ex12 が実際にそうやって
   量産リストから本編へ上がりました。今の CMakeLists の SRCS には ex12 までが載っています）、
   SRCS に `"ex14_raster.S"` を 1 行足す。`proposed/` はビルドに入っていないので、今のところ
   このファイルはリンクされていません（`nm` で見えているのは単体オブジェクトだけです）。
2. `examples.h` に宣言（引数の意味と、`fb`/`stride_px`/`color8` の整列・同一レーンの要件）。
3. `main.c` に `ex14()`: 決定論的な三角形 → ドライバ → `ex14_edge_dda` → `ex14_span_fill` →
   `ref_span_fill` との `CHECK` → `BENCH ex14_span_fill px=…` / `BENCH ex14_edge_dda span=…` →
   `DATA` で入力（頂点、色、スパン対）と結果を全部出す。ログ 1 枚で Python が再計算できるように。
4. `tools/check_examples_log.py` に `check_ex14()`（三度目の計算＝この `ex14_model.py` の関数を
   そのまま移植）、`tools/selftest_examples_checker.py` の合成ログに `DATA` 行と変異 1 つ。
5. 実機では、まず**整列スパンだけ**を流して T1 に相当する一致を取り、そのあと非整列・棄却・
   チャンク内の順に足す（この文書の T1→T2→T3→T4 と同じ順）。`VST.128.IP` のストア丸めが本当に
   文書どおりかを確かめたいなら、**わざと非整列アドレスに 128bit ストアを打つ probe** を別に用意して、
   隣のピクセルが壊れることを実機で見るのが ex03 と同じやり方です（このカーネルはそれを踏みません）。
