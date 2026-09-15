# ex19 (proposed) — スプライト合成（マスク付き不透明 / 半透明 / カラーキー）

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex19_sprite.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 18 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `5e3c4b2d73401bc8bd7934c2ab583115` → `ebf60e3d0ebba7789f5406b861a05c78`（tools/check_abi.py / tools/fix_abi.py）
#                sprite compositing: masked opaque blit, half-transparent blit, colour key

`examples/firmware/main/proposed/ex19_sprite.S` — notes/08-media-3d-perf.md の量産リストが ex19 に割り当てた段
（`スプライト合成（マスク付き不透明 / 半透明 / カラーキーを VCMP + ANDQ/ORQ/NOTQ で分岐レスに）`、notes/08 line 102）を、
その行が挙げている命令で書いたものです。ベースになっているのは notes/08 line 65 の
`| スプライト合成 | マスク（VCMP + ANDQ/ORQ）＋上記ブレンド | 未着手 |`。

```c
void ex19_sprite_opaque  (int16_t *dst, const int16_t *src, const uint16_t *mask, uint32_t n_px);
void ex19_sprite_blend   (int16_t *dst, const int16_t *src, const uint16_t *mask,
                          const int16_t *ones8, uint32_t n_px);
void ex19_sprite_colorkey(int16_t *dst, const int16_t *src, int16_t key, uint32_t n_px);
```

**このファイルと付属の証拠はすべて実機を使っていません。** 実行したのは (1) アセンブラ（`xtensa-esp32s3-elf-gcc`
esp-15.2.0_20251204 → `objdump` → `data/pie_instructions.json` の命令語図とのフィールド単位照合、30 行すべて一致）、
(2) Python の命令レベルモデル（1 命令ずつ .S を読んだもの）と、ホストの C 参照実装（`ref.c`、main.c に入る形のもの）との
**900 ケース全数照合**（不一致 0）、(3) `/workspace/pjs-vm/tools/pie/piesim.py` に .S のテキストそのものを食わせた
**解釈実行**（225 ケース、17074 命令、不一致 0）の 3 つだけです。**シリコンで確かめていない前提は §8 に全部列挙**
しました。このファイルはビルドに入っていません（`CMakeLists.txt` / `examples.h` / `main.c` は触っていません）。

## 1. 決定: マスクは「レーンごとの全域マスク」であって、ビットマスクではない

**採用した形**: `mask` は `const uint16_t *` で、**1 画素 = 1 レーン**、値は `0xFFFF`（不透明＝スプライト画素で置換）
または `0x0000`（透明＝背景をそのまま保持）。1 ビット/画素のビットマスクは採りませんでした。

### 1.1 契約（レーン意味論、ここを外すと全部ずれる）

```
画素 i の位置（3 つのバッファで同一）:
    dst  : オフセット 2*i からの 16bit リトルエンディアン
    src  : 同じ
    mask : 同じ  ← mask[i] の 1 レーンが画素 i を決める。ビット位置でもバイト位置でもない。

n_px は「画素（= 16bit レーン）の数」。1 呼び出しが触るのは 3 バッファ各 2*n_px バイト。
src / dst / mask はすべて 16 バイト整列（§3）。n_px == 0 は何にも触らない。
```

* レーン i の 128bit レジスタ内の位置は `[16i, 16i+16)`、すなわち**バイトオフセット 2*i のリトルエンディアン
  16bit ワード**です（`.md` のこの 1 行がレーン規約のすべて）。ESP32-S3 はリトルエンディアンで、`EE.VLD.128.IP`
  の疑似コードは `qu[127:0] = load128({as[31:4],4{0}})`（TRM p164）＝ アドレス順に 16bit が並ぶだけなので、
  「レジスタのレーン i = メモリの i 番目の 16bit ワード」になります。この割り当ては**既に実機が確定させています**:
  ex08 の `ex08_half_blend` は 8 画素ぶんの出力を main.c の**画素ごとの C ループ**と `memcmp` で比べて通っており
  （`data/pie_examples_measured.json` の run_summary: ex01–ex09 の 45/45 チェックが on-device で一致）、
  そこで一致した並びがこれ以外にありません。本ファイルの 900 ケース照合も同じ規約で書いてあるので、
  モデルと C 参照が一致すること自体がこの規約の再確認になっています。
* mask の値は `0x0000` / `0xFFFF` の 2 値が契約です。**契約外の値でも壊れません**が、意味が変わります:
  選択が `dst ^ ((dst ^ src) & mask)` という**ビット単位**の式なので、例えば `0x00FF` のレーンは
  「16bit の下位 8bit だけ src、上位 8bit は dst」という**レーン内のビット合成**になります
  （ベクタ経路とスカラーの端数処理は同じ式なので両者で一致します）。この性質は 272 レーンぶんの
  任意マスクを与えて検査してあり、§5 の実行結果に「arbitrary mask lanes fed」として出ています。
* **`0x0000` のレーンは「元のビットそのもの」**が戻ります（算術を通らないので、同じスプライトを何度重ねても
  背景がずれません）。これは式が `dst ^ (… & 0)` = `dst` であることの帰結で、丸め誤差もドリフトもありません。

### 1.2 なぜビットマスク（1 ビット/画素）を採らないか

ビットマスクのほうが帯域は 16 分の 1 です（2 B/画素 → 0.125 B/画素）。それでも採らなかった理由は 3 つです。

1. **`0xFFFF` / `0x0000` のレーン形は、この命令セットが比較の出力としてそのまま吐く形**です。
   `EE.VCMP.*.S16`（TRM p155/p158/p161）は「条件成立なら `0xFFFF`、そうでなければ 0」を**レーンに**書きます。
   つまりカラーキー（kernel C）はマスクを**メモリに一切持たず**、比較 1 命令 + `EE.NOTQ` 1 命令で作れます。
   ビットマスクにすると、この natively な形を毎回「レーン → ビット」へ圧縮する必要が出ますが、
   **その圧縮命令が 220 命令の中にありません**（PEXT 相当も、レーンの内容を選ぶ `VSEL` 相当もありません。
   参考: §7 の候補命令一覧）。逆方向（ビット → レーン）は可能ですが、どちらか一方でも欠けると
   「CASET で 1 ビット/画素マスクを持つ入力」と「VCMP が吐くマスク」が命令セット内で往復できません。
2. **ex12_sat_masks の出力と同じ形**だからです（`examples/firmware/main/ex12_physics.S` はクリップ判定を
   `uint16` の `0xFFFF`/`0` マスクとして書き出します）。同じ形なら AABB クリップ → 今回の合成、と
   **変換なしで繋がります**。ビットマスクだとその間に新しいパスが 1 本入ります。
3. **レーン形は 1 回の 128bit ロードで 8 画素ぶんそろう**ので、展開ステップが 0 命令です。
   ビットマスクだと 8 画素ごとに展開が要ります。実現可能な最小の展開は
   `EE.ANDQ`（マスク語をワンホット列と AND）+ `EE.VCMP.GT.S16`（0 と比較してレーン形へ）で、
   マスク語のブロードキャスト（`EE.VLDBC.16` 系）と合わせて 8 画素あたり 3 命令前後が上乗せになります
   （**これは命令の意味から数えた見積もりで、実装も計測もしていません**。このファイルの 3 カーネルは
   レーン形だけを使います）。

**`EE.VZIP.16` / `EE.VUNZIP.16` を評価して、この契約では使わないと決めました。** 疑似コードは TRM p290/p293 にあり
（§7 に引用）、実機でも意味が確定しています（`docs/pie-simd.md` の波カーネルは 32bit 表エントリ
`lo | (hi<<16)` を `EE.VUNZIP.16` で 2 レーンに分けています — 同ファイル line 125）。しかし**どちらも
「16 レーンを偶数/奇数に振り分ける（あるいは戻す）」命令**であって、**1 ビットを 1 レーンに展開する命令ではありません**。
`{q1,q0}` の 16 レーン `s0..s15` に対して `VUNZIP.16` は `q0' = {s0,s2,…s14}`, `q1' = {s1,s3,…s15}` を返すので
（p290 の Operation をそのまま読んだ結果）、ビットマスクの展開には効きません。

**では `VZIP/VUNZIP` が要るのはどんな形か**: マスクを 16bit レーン 2 本ずつ 32bit ワードに詰めた「ワイドマスク」
（`mask32[j] = mask[2j] | (mask[2j+1] << 16)`）なら、**`EE.VUNZIP.16` 1 命令で 2 レーンに分離できます**。
8 画素 = 32bit ワード 4 本 → `VLD.128` 1 回 + `VUNZIP.16` 2 回で 8 レーン。今回この形にしなかったのは、
マスクの作り手（C 側のフレームワーク）が `uint16` 配列を持つ方が自然で、かつその 1 命令の節約が
帯域を 1 バイトも減らさないためです（32bit 詰めは 4 B/8 画素 = レーン形と同じ）。**詰め形のマスクを
持っている呼び手にとっては `VUNZIP.16` が正解**で、この 2 命令を「候補として検討したがこの契約では
不要」と記録しておきます。§7 に両方の疑似コードを引用しました。

## 2. 3 つのカーネル

3 つとも「選択は 3 命令」という同じ形です。分岐は 1 つもありません（ループの `bnez` だけ）。

```
out = dst ^ ((dst ^ src) & mask)      EE.XORQ / EE.ANDQ / EE.XORQ     (TRM p297 / p76 / p297)
    mask = 0xFFFF -> out = src        スプライト画素で置換
    mask = 0x0000 -> out = dst        背景をビットそのまま保持
```

`mask` を `EE.NOTQ`（p120）で反転して `(src & mask) | (dst & ~mask)` と書く同じ選択が
**4 命令**になるので（`NOTQ` + `ANDQ` ×2 + `ORQ`）、XOR 形の 3 命令を採っています。量産リストが
`ANDQ/ORQ/NOTQ` と書いているこの 3 命令は、`NOTQ` が kernel C のマスク生成に、`ANDQ` が選択と
kernel B の 0xF7DE マスクに、`ORQ` が（不採用の）4 命令形に出てきます。

### 2.1 A: `ex19_sprite_opaque`

```
8 画素/反復。a2=dst は immediate 0 でロードし、同じレジスタに immediate 16 でストアする。
.EIP のポストインクリメントはストアのアドレスを確定した後に走るので (TRM p275 の疑似コード
`qv[127:0] => store128({as[31:4],4{0}}); as[31:0] = as[31:0] + …`)、
1 本の AR が「今のチャンクのロード」と「今のチャンクのストア」を兼ね、反復ごとに 1 回だけ進む。
```

実行された本体（objdump、§6 に全体）:

```
9:   ee.vld.128.ip q0, a2, 0     dst（進めない）
c:   ee.vld.128.ip q1, a3, 16    src
f:   ee.vld.128.ip q2, a4, 16    mask
12:  ee.xorq  q3, q0, q1
15:  ee.andq  q3, q3, q2
18:  ee.xorq  q3, q0, q3
1b:  ee.vst.128.ip q3, a2, 16    dst（ここで a2 += 16）
1e:  addi   a8, a8, -1
21:  bnez   a8, 9
```

**7 PIE 命令 / 8 画素 = 0.875 PIE 命令/画素**（ループ制御 2 を入れて 9 命令 / 8 画素）。
`n_px & 7` の端数は §3 のスカラー処理で 1 画素ずつ同じ式を適用します。

### 2.2 B: `ex19_sprite_blend` — ex08 のハーフブレンドを 1 ビットも変えずに、境界を数字で書く

マスクが立っている画素だけ ex08 のハーフブレンドにし、立っていない画素は `dst` を**ビットそのまま**残します。
ブレンド値 `hb` は ex08_half_blend（`examples/firmware/main/ex08_media.S:53-57`）と同じ 5 命令の列です:

```
    EE.ANDQ      q3, q1, q6       src & 0xF7DE          0xF7DE はレーン定数（q6）
    EE.ANDQ      q4, q0, q6       dst & 0xF7DE
    EE.VMUL.U16  q3, q3, q7       (src & 0xF7DE) >> 1    q7 = 呼び手の ones8、SAR = 1
    EE.VMUL.U16  q4, q4, q7       (dst & 0xF7DE) >> 1
    EE.VADDS.S16 q3, q3, q4       hb = sat16(half(src) + half(dst))
```

`EE.U16` と `EE.S16` の選択は ex08 が実機で確定させた理由をそのまま引き継ぎます
（`data/pie_examples_measured.json` の finding `vmul_s16_is_arithmetic_and_vmul_u16_is_logical`:
ones と SAR=1 のとき `EE.VMUL.S16` は**符号付き**レーンを算術シフトするので、bit 15 が立っているレーンは
符号拡張される。RGB565 は**符号なし**なので `EE.VMUL.U16` が正しい）。
`0xF7DE` = `~0x0821` で、クリアされるのは **bit 11 / 5 / 0 = R / G / B 各チャネルの LSB** です。

**丸め（このセクションの主張は全部、下の検算スクリプトの出力で確認済み）**: 上式の各チャネル値は

```
    hb_ch = floor( (a_ch + b_ch - LSB(a_ch) - LSB(b_ch)) / 2 )
```

と**厳密に一致**します。つまり「各オペランドの各チャネルの LSB を先に捨ててから平均する」という丸めで、
**四捨五入でも切り上げでもなく、LSB を落とした値の床平均**です。真の床平均 `floor((a_ch+b_ch)/2)` と違うのは
両方の LSB が 1 のときだけで、そこでは 1 小さくなります。チャネル間キャリーが無いこと（3 チャネル同時でも
この式がそのまま成り立つこと）も同じスクリプトで見ています。

```python
$ /workspace/esp32s3-hw-mcp/.venv/bin/python halfblend_channels.py    # 全数 + 一様乱択
1) チャネル別（R 1024 組 + G 4096 組 + B 1024 組、他チャネルは 0）: 違反 0
2) 真の床平均との不一致（R 1024 組のうち、両方の LSB が 1 のときだけ）: 256
3) 全チャネル同時: 8x8x8 の極値パレット同士 64 組で違反 0、一様乱択 300000 組で違反 0
4) 一様乱択 200000 組: 和が 0x8000 以上 = 93745（46.9%）、天井未満 = 106255 で違反 0
5) 白 + 白: ex08 の式 = 0xf7de（R/G/B = (30, 62, 30)）、真の平均 = 0xffff（R/G/B = (31, 63, 31)）
6) half の最大値 = 0x7bef、その和 = 0xf7de（天井 0x7FFF を超える）
```

白 + 白（`0xFFFF` と `0xFFFF`）が `0xF7DE`（R=30, G=62, B=30）で真の平均 `0xFFFF` より各チャネル 1 小さいのが、
この式の解像度の代償です（ex08 の式をそのまま使う以上ついて回ります）。検算スクリプト本体:

```python
#!/usr/bin/env python3
"""ex19_sprite_blend のブレンド式（ex08 の 5 命令）が、チャネルごとに何を計算しているかを全数で確かめる。

ex08 のハーフブレンドは `((v & 0xF7DE) >> 1)` を 2 つ足す。0xF7DE = ~0x0821 は R/G/B 各チャネルの
LSB（bit 11/5/0）を落とすマスクなので、式の意味は「各チャネルの LSB を落としてから平均」に見える。
それが本当に厳密に成り立つのか、そしてチャネル間でキャリーが起きないのかを、ここで全数（と全域乱択）で
確かめる。ここで出た数字だけを ex19_sprite.md §2.2 に書いている。
"""
import itertools
import random

MASK = 0xF7DE


def hb(a, b):                    # EE.ANDQ x2 + EE.VMUL.U16 x2 + EE.VADDS.S16（飽和なし）
    return (((a & MASK) >> 1) + ((b & MASK) >> 1)) & 0xFFFF


def hb_sat(a, b):                # EE.VADDS.S16 は符号飽和。2 つの半分は < 0x8000 なので天井は 0x7FFF
    return min((((a & MASK) >> 1) + ((b & MASK) >> 1)), 0x7FFF)


def f2(x, y):                    # 「各チャネルの LSB を先に落としてから床平均」
    return (x + y - (x & 1) - (y & 1)) // 2


def ch(v):                       # RGB565 の 3 チャネル
    return (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F


def mk(r, g, b):
    return (r << 11) | (g << 5) | b


def main():
    # ---- 1. チャネルごとの全数（他のチャネルは 0 に固定）
    bad1 = 0
    for a in range(32):
        for b in range(32):
            if ch(hb(mk(a, 0, 0), mk(b, 0, 0)))[0] != f2(a, b):
                bad1 += 1
    for a in range(64):
        for b in range(64):
            if ch(hb(mk(0, a, 0), mk(0, b, 0)))[1] != f2(a, b):
                bad1 += 1
    for a in range(32):
        for b in range(32):
            if ch(hb(mk(0, 0, a), mk(0, 0, b)))[2] != f2(a, b):
                bad1 += 1
    print(f"1) チャネル別（R 1024 組 + G 4096 組 + B 1024 組、他チャネルは 0）: 違反 {bad1}")

    # ---- 2. 真の床平均 floor((a+b)/2) と違う割合（R チャネル、全 1024 組）
    diff = sum(1 for a in range(32) for b in range(32)
               if ch(hb(mk(a, 0, 0), mk(b, 0, 0)))[0] != (a + b) // 2)
    print(f"2) 真の床平均との不一致（R 1024 組のうち、両方の LSB が 1 のときだけ）: {diff}")

    # ---- 3. チャネル間にキャリーが無いこと（全 3 チャネルを同時に動かす）
    extra = [mk(r, g, b) for r in (0, 31) for g in (0, 63) for b in (0, 31)]
    n3 = bad3 = 0
    for a in extra:
        for b in extra:
            n3 += 1
            if ch(hb(a, b)) != tuple(f2(ch(a)[i], ch(b)[i]) for i in range(3)):
                bad3 += 1
    rnd = random.Random(11)
    n4 = bad4 = 0
    for _ in range(300000):
        a, b = rnd.randint(0, 0xFFFF), rnd.randint(0, 0xFFFF)
        n4 += 1
        if ch(hb(a, b)) != tuple(f2(ch(a)[i], ch(b)[i]) for i in range(3)):
            bad4 += 1
    print(f"3) 全チャネル同時: 8x8x8 の極値パレット同士 {n3} 組で違反 {bad3}、"
          f"一様乱択 {n4} 組で違反 {bad4}")

    # ---- 4. 天井（0x8000）に届く割合と、届かない領域での一致
    rnd = random.Random(7)
    N = 200000
    over = below = below_bad = 0
    for _ in range(N):
        a, b = rnd.randint(0, 0xFFFF), rnd.randint(0, 0xFFFF)
        h = ((a & MASK) >> 1) + ((b & MASK) >> 1)
        if h > 0x7FFF:
            over += 1
        else:
            below += 1
            if ch(hb_sat(a, b)) != tuple(f2(ch(a)[i], ch(b)[i]) for i in range(3)):
                below_bad += 1
    print(f"4) 一様乱択 {N} 組: 和が 0x8000 以上 = {over}（{100.0 * over / N:.1f}%）、"
          f"天井未満 = {below} で違反 {below_bad}")
    print(f"5) 白 + 白: ex08 の式 = {hb(0xFFFF, 0xFFFF):#06x}（R/G/B = {ch(hb(0xFFFF, 0xFFFF))}）、"
          f"真の平均 = {0xFFFF:#06x}（R/G/B = {ch(0xFFFF)}）")
    print(f"6) half の最大値 = {(0xFFFF & MASK) >> 1:#06x}、その和 = {2 * ((0xFFFF & MASK) >> 1):#06x}"
          f"（天井 0x7FFF を超える）")


if __name__ == "__main__":
    main()
```

**`EE.VADDS.S16` の飽和（ここが ex08 の C 参照と食い違う唯一の点）**: 2 つの半分はどちらも `≦ 0x7BEF < 0x8000`
なので符号付きでも正、したがって飽和点は `+32767 = 0x7FFF` です。ところが**両方の画素が明るいと
`half(src) + half(dst)` が `0x8000` 以上になります**（上限は `2 × 0x7BEF = 0xF7DE`）。一様に引いた 16bit 値の
20 万組では **93745 組（46.9%）** がこの領域に入り、そのときカーネルは `0x7FFF` に張り付きます。
ex08 の C 参照（`main.c` の `c_half_blend`: 32bit で足して 16bit に切り詰める＝**ラップ**）は同じ入力で
`0xF7DE` を返すので、**そこだけ 2 つの参照は一致しません**。今回の 900 ケースでは
**641 レーンが食い違い、1212 レーンが天井に到達**しました（§5）。
ex08 自身の検査が通っている理由も数字で説明できます: ex08 のデータは `main.c:762-763` の
`s_pa[i] = rnd16(0, 0x7fff)` / `s_pb[i] = rnd16(0x2000, 0x7fff)` と `-30000` のレーンだけで、
`half` の最大値は `0x3BEF`（`0x7FFF` から）と `0x4168`（`0x8AD0` から）なので
**最悪和は `0x7D57 < 0x8000`**、つまり ex08 のデータはこの領域に決して入りません。
本ファイルのモデルは**飽和する側**（ハードウェアの `EE.VADDS.S16`）を参照に採り、ラップする側も
`ex19_sprite_blend_wrap_c` として同じ 900 ケースに通して差分数を出しています。

> 参考: pjs-vm の描画パスはこれとは別の式 `(a & b) + (((a ^ b) & 0xF7DE) >> 1)`
> （`/workspace/pjs-vm/main/scene/flower.c:1401`）を使っており、こちらは**真のチャネル別床平均と全数一致**します
> （同じ 1024/4096/1024 組で違反 0）。ただしこの式の加算は `0xFFFF` まで届くので、
> **この命令セットでは書けません**（非飽和のベクタ加算が存在せず、`EE.VADDS.S16` は符号飽和。
> `0xFFFF` のレーンは符号付きで -1 と読まれる）。ex08 の式を維持するのはそのためで、
> 明るい画素で 1 段暗くなる代わりに 1 命令列で書き切れる、というトレードオフだと理解しています。

**`0xF7DE` をどう手に入れるか**: この関数の引数に `mask8` が無い（B の署名は `ones8` だけ）ので、
`movi` + `slli` + `or` でレジスタに組み、**自分のフレームに置いて `EE.VLDBC.32` で 8 レーンにブロードキャスト**
します（TRM p173: `qu[127:0] = {4{load32({as[31:2],2{0}})}}`）。`movi` の即値は 12bit なので
`0xF7DEF7DE` は直接書けず、`0xF7` と `0xDE` の 2 つから組み立てています（7 命令、呼び出しごとに 1 回）。
`l32r` を一切使いません（`.iram1` から `.rodata` への `l32r` はリンカが
`dangerous relocation: l32r: literal target out of range` で拒否するため。§6 の実行結果で
`l32r` は **0 件**、`.literal` セクションも出ていません）。スカラーの端数処理も同じフレームの
`a1+4`（`0xF7DE`）と `a1+6`（`0x7FFF`）を `l16ui` で読みます。

**1 反復 = 12 PIE 命令 / 8 画素 = 1.5 命令/画素**（+ ループ制御 2 命令、計 14 命令 / 8 画素）。

### 2.3 C: `ex19_sprite_colorkey` — マスクをメモリに持たない

```
fa:  ee.vld.128.ip q0, a3, 16      src
fd:  ee.vld.128.ip q1, a2, 0       dst（進めない）
100: ee.vcmp.eq.s16 q2, q0, q7     0xFFFF where src == key        (TRM p155)
103: ee.notq      q2, q2           0xFFFF where src != key = 不透明マスク (TRM p120)
106: ee.xorq      q3, q1, q0       dst ^ src
109: ee.andq      q3, q3, q2       … & mask
10c: ee.xorq      q3, q1, q3       dst ^ (…) = (不透明 ? src : dst)
10f: ee.vst.128.ip q3, a2, 16
```

**8 PIE 命令 / 8 画素 = 1.0 命令/画素**、マスク用のバッファは不要、比較も選択も分岐なし。
`key` は AR のスカラーで来るので、`s16i` でフレームに落として `EE.VLDBC.16`（TRM p170）で 8 レーンに
ブロードキャストします（AR → QR の移動命令は存在しない。ex12_physics.S が lo/hi で同じ組を使っています）。

* 等値比較は**ビットパターンの比較**なので、画素を符号付きと読むか符号なしと読むかは結果に影響しません
  （`0x0000`/`0xFFFF` の -0 は無い）。ただし**比較命令自身の符号性はまだ実機で測られていません**（§8）。
* 端数処理は**キープマスク形**で書いています: `xor a9, src, key` → `minu a9, a9, 1` → `addi a9, a9, -1`
  で「src == key のとき `0xFFFF`」を作り、`out = src ^ ((src ^ dst) & keep)`。
  ベクタ経路の `dst ^ ((dst ^ src) & mask)` とは**担体（carrier）が違うだけで同じ関数**です
  （`mask = ~keep` なので同値）。§5 の 900 ケース照合はこの 2 つの綴りを**ビット単位で突き合わせて**おり、
  カーネル A の 300 ケースのうちベクタ経路が 228 ケース、端数経路（`n_px & 7 ≠ 0`）が 271 ケース、
  `n_px == 0` が 5 ケースという内訳で、どの経路も一致しています（0 不一致）。

## 3. 128bit ストアの整列契約と「範囲外を書かない」保証

**整列契約**: `dst` / `src` / `mask` はすべて 16 バイト整列。理由は「128bit アクセスがアドレスを
`{as[31:4],4{0}}` の形で作る」からで（TRM p49 figure 1.7-1、`EE.VLD.128.IP` p164 / `EE.VST.128.IP` p275 の
疑似コードにも同じ式）、**下位 4bit は無視されるのではなく落とされます**（例外は出ません）。実機で確定済みの
finding `vld128_drops_the_low_address_bits` がそのまま当てはまり、`dst` がずれていれば
**1 つ下のチャンクへ書いて、行の手前の画素を静かに壊します**。

**範囲外を書かないことの保証**（引数 `n_px` が何であっても）:

1. ベクタループは 1 反復 = 1 個の**完全な 16 バイトチャンク**しか書かず、書く位置は
   `dst + 16*k`（`k = 0 … (n_px>>3)-1`）なので、すべて `[dst, dst + 2*n_px)` の内側です。
   `n_px>>3` 反復で覆うのは `16*(n_px>>3) ≦ 2*n_px` バイト。
2. 端数の `n_px & 7` 画素は**スカラーの `s16i` 1 命令ずつ**で書き、アドレスは `dst + 2*i`（`i < n_px`）。
   端数経路には 128bit アクセスが 1 つもありません。
3. したがって**「最後の半端な 16 バイトチャンク」は決して書かれません**。`n_px` が 8 の倍数でない限り、
   `2*n_px` から次の 16 バイト境界までのバイトは元の値のまま残ります（これは caller にとって重要で、
   スプライト行の後ろに別のデータがあっても壊しません）。
4. `n_px < 8` のときはベクタ経路に入らないので、**整列も必要ありません**（小さい矩形のブリットは
   整列に依存しない経路で完結します）。

この 4 点は文章ではなく**検査**です: §5 のモデルは 3 つのバッファを 16 バイト整列したうえで
**前後にガード領域を持つ bytearray の中に置き**、900 ケースすべてで「`[dst, dst+2*n_px)` の外のバイトが
1 バイトも変わっていない」ことを確認しています（結果: `bytes written outside the n_px pixels: 0`）。
さらに**契約違反の対照実験**も同じモデルで走らせ、`dst` を 16 バイト格子から 2 バイトずらすと
**画素の範囲外 2 バイト（`dst-2`, `dst-1`）が上書きされる**ことを数字で出しています（§5 の最後）。

## 4. コスト（発行スロット数であって、サイクルではない）

ex19_sprite_opaque: 27 instructions total, 7 of them EE.* PIE ops
    loop body 0x9..0x21: 9 instructions (7 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer
    loop body 0x29..0x46: 12 instructions (0 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer
ex19_sprite_blend: 56 instructions total, 14 of them EE.* PIE ops
    loop body 0x80..0xa6: 14 instructions (12 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer
    loop body 0xae..0xe5: 21 instructions (0 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer
ex19_sprite_colorkey: 33 instructions total, 9 of them EE.* PIE ops
    loop body 0xfa..0x115: 10 instructions (8 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer
    loop body 0x124..0x145: 13 instructions (0 EE.* PIE ops, rest scalar/branch)
      stalls under D = max(SA-SB+1,0): none: every stage-2 producer is >= 2 instructions ahead of its consumer

上の 2 つ目の「loop body」（例: A の 0x29..0x46）は**スカラーの端数ループ**で、PIE 命令が 0 本です
（1 画素ずつであることがそのまま見えています）。1 つ目がベクタループ = 8 画素ぶんの本体です。

ストールの判定は TRM 表 1.7-2（`data/pie_pipeline.json`）の use/def ステージで、
`D = max(SA - SB + 1, 0)` を最小発行間隔として数えました。`EE.VLD.128.IP` と `EE.VMUL.*` が
def=2 / use=1 なので、**その消費者を直後に置くと 1 サイクル止まります**（`/workspace/pjs-vm/docs/pie-simd.md:557-565`
が表と実測で同じことを言っています。**別リポジトリの資料 = measured-elsewhere** として引用し、
本リポジトリの `data/pie_pipeline.json` の数値と併記しています）。3 つのループ本体はこれを踏まえて並べてあり、
`EE.VMUL.U16` と `EE.VADDS.S16` の間に**ベクタ鎖に依存しない `addi a8, a8, -1` を挟んで**あります
（ex08 の綴りから 1 命令だけ動かしたのはこの 1 か所です）。上の報告のとおり、どのループ本体にも
ストールはありません。ただし**これは表からの計算であって実測ではありません**:
サイクル数は実機なしには主張しません（`data/pie_timing_measured.json` の tier は「実測」なので、
ここで混ぜていません）。

## 5. 実行した検証

### 5.1 命令レベルモデル（Python）とホスト C 参照の全数照合

モデルは .S の命令列を 1 命令ずつ Python に写したもので、`EE.VLD.128.IP` の下位 4bit 落とし・
`EE.VMUL.U16` の論理シフト・`EE.VADDS.S16` の符号飽和・`EE.VCMP.EQ.S16` の `0xFFFF` 出力など、
TRM の疑似コードどおりに実装しています。参照は `ref.c`（main.c に入る形のスカラー実装、ホスト gcc -O2）。
両者はケースファイルというテキスト 1 つだけを共有します。

#### モデル（`/tmp/ex19check/model.py`、走らせたものそのまま）

```python
#!/usr/bin/env python3
"""ex19_sprite.S -- host model + check.

The model below is written as the *instruction sequence* of ex19_sprite.S, one Python function per kernel,
with each op applied to lane lists using the semantics quoted from the TRM:

    EE.VLD.128.IP   qu[127:0] = load128({as[31:4],4{0}}); as += sign_extend(imm16,4)   (p164)
    EE.VST.128.IP   qv[127:0] => store128({as[31:4],4{0}}); as += sign_extend(imm16,4) (p275)
    EE.VLDBC.16/32  broadcast the 16/32-bit word at the forced-aligned address           (p170/p173)
    EE.XORQ         qa = qx ^ qy                                                        (p297)
    EE.ANDQ         qa = qx & qy                                                        (p76)
    EE.NOTQ         qa = ~qx                                                            (p120)
    EE.VCMP.EQ.S16  qa[lane] = (qx[lane] == qy[lane]) ? 0xFFFF : 0                      (p155)
    EE.VMUL.U16     qz[lane] = (qx[lane] * qy[lane]) >> SAR[5:0], low 16             (p204)
    EE.VADDS.S16    qa[lane] = sat16_signed(qx[lane] + qy[lane])                        (p146)

Lane convention (the contract the .md states): a 128-bit register holds eight 16-bit lanes and LANE i IS
THE 16-BIT WORD AT BYTE OFFSET 2*i of the address, read little-endian -- the same order the examples suite
has measured for EE.VLD.128 (finding vld128_drops_the_low_address_bits) and the same order ex08_half_blend
relies on when its output is memcmp'd against the per-pixel C loop in main.c. There is no second convention
anywhere in this file: src[i], mask[i] and dst[i] are the same lane number.

The references are the `ex19_*_c` scalar code in ref.c (compiled with the host gcc), which is what would go
into main.c. Case inputs and reference outputs are exchanged through a text file, so the two implementations
share nothing but that format.

What this can and cannot show is stated in ex19_sprite.md: it is a model-vs-reference check (my reading of
my own instruction sequence against the scalar formula), not a silicon run.
"""
import random
import subprocess
import sys

M16 = 0xFFFF
M32 = 0xFFFFFFFF
M8 = 0xFF


# ---------------------------------------------------------------- the scalar references (as in ref.c)
def sat16(v):
    return max(-32768, min(32767, v))


def half565(v):
    """ex08_half_blend's (a & 0xF7DE) >> 1, done the way the kernel does it: EE.ANDQ with the 0xF7DE lane
    then EE.VMUL.U16 by ones with SAR = 1 -- an UNSIGNED logical shift, so an unsigned read of the lane."""
    return ((v & 0xF7DE) >> 1) & M16


def ref_opaque_c(d, s, m, n):
    out = list(d)
    for i in range(n):
        out[i] = (d[i] ^ ((d[i] ^ s[i]) & m[i])) & M16
    return out


def ref_opaque_spec_c(d, s, m, n):
    """The contract, not the kernel: 0xFFFF -> the sprite pixel, 0x0000 -> the background bit for bit."""
    out = list(d)
    for i in range(n):
        if m[i] == M16:
            out[i] = s[i]
        elif m[i] == 0:
            pass
        else:
            out[i] = (d[i] ^ ((d[i] ^ s[i]) & m[i])) & M16
    return out


def ref_blend_c(d, s, m, n):
    """The kernel: the two halves added and then saturated to 16-bit signed, the way EE.VADDS.S16 does."""
    out = list(d)
    for i in range(n):
        h = half565(s[i]) + half565(d[i])
        hb = M16 if h > 0x7FFF else h
        out[i] = (d[i] ^ ((d[i] ^ hb) & m[i])) & M16
    return out


def ref_blend_wrap_c(d, s, m, n):
    """ex08's reference (main.c c_half_blend): the same two halves added in 32 bits and truncated."""
    out = list(d)
    for i in range(n):
        hb = (half565(s[i]) + half565(d[i])) & M16
        out[i] = (d[i] ^ ((d[i] ^ hb) & m[i])) & M16
    return out


def ref_colorkey_c(d, s, key, n):
    out = list(d)
    for i in range(n):
        if s[i] != key:
            out[i] = s[i]
    return out


# ---------------------------------------------------------------- memory, exactly as the PIE ops see it
def ld16u(mem, addr):
    return mem[addr] | (mem[addr + 1] << 8)


def st16(mem, addr, v):
    mem[addr] = v & M8
    mem[addr + 1] = (v >> 8) & M8


def vld128(mem, addr):
    """EE.VLD.128.IP: {as[31:4],4{0}} -- the low four address bits are DROPPED (TRM p49, p164)."""
    a = addr & ~15
    return [ld16u(mem, a + 2 * i) for i in range(8)]


def vst128(mem, addr, lanes):
    a = addr & ~15
    for i, v in enumerate(lanes):
        st16(mem, a + 2 * i, v)


def vldbc32(mem, addr):
    """EE.VLDBC.32: qu[127:0] = {4{load32({as[31:2],2{0}})}} -- four 32-bit lanes, i.e. eight equal 16-bit
    lanes (TRM p173)."""
    a = addr & ~3
    w = ld16u(mem, a) | (ld16u(mem, a + 2) << 16)
    return [(w & M16), (w >> 16)] * 4


def vldbc16(mem, addr):
    """EE.VLDBC.16: qu[127:0] = {8{load16({as[31:1],1{0}})}} (TRM p170)."""
    a = addr & ~1
    v = ld16u(mem, a)
    return [v] * 8


def v_and(x, y):
    return [a & b for a, b in zip(x, y)]


def v_xor(x, y):
    return [a ^ b for a, b in zip(x, y)]


def v_not(x):
    return [(~a) & M16 for a in x]


def v_vcmp_eq16(x, y):
    """EE.VCMP.EQ.S16 (TRM p155): 0xFFFF per lane where the two 16-bit patterns are equal. Equality is the
    same test signed or unsigned."""
    return [M16 if a == b else 0 for a, b in zip(x, y)]


def v_vmul_u16(x, y, sar):
    """EE.VMUL.U16 (TRM p204): 32-bit product, LOGICAL >> SAR[5:0], low 16 bits kept."""
    return [((a * b) >> sar) & M16 for a, b in zip(x, y)]


def v_vadds_s16(x, y):
    """EE.VADDS.S16 (TRM p146): min(max(qx+qy, -2^15), 2^15-1) per lane, on the SIGNED reading."""
    sx = [v - 0x10000 if v & 0x8000 else v for v in x]
    sy = [v - 0x10000 if v & 0x8000 else v for v in y]
    return [sat16(a + b) & M16 for a, b in zip(sx, sy)]


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def s32(v):
    v &= M32
    return v - (1 << 32) if v & 0x80000000 else v


# ---------------------------------------------------------------- A: ex19_sprite_opaque
def model_opaque(mem, dst, src, mask, n_px):
    """ex19_sprite_opaque.S, instruction by instruction."""
    d, s, m = dst, src, mask
    for _ in range(n_px >> 3):                       # srli a8, a5, 3
        q0 = vld128(mem, d)                          # EE.VLD.128.IP q0, a2, 0     (imm 0: no advance)
        q1 = vld128(mem, s); s += 16                 # EE.VLD.128.IP q1, a3, 16
        q2 = vld128(mem, m); m += 16                 # EE.VLD.128.IP q2, a4, 16
        q3 = v_xor(q0, q1)                           # EE.XORQ q3, q0, q1
        q3 = v_and(q3, q2)                           # EE.ANDQ q3, q3, q2
        q3 = v_xor(q0, q3)                           # EE.XORQ q3, q0, q3
        vst128(mem, d, q3); d += 16                  # EE.VST.128.IP q3, a2, 16
    for _ in range(n_px & 7):                        # extui a8, a5, 0, 3 + scalar tail
        a11 = ld16u(mem, d)
        a10 = ld16u(mem, s) ^ a11                    # xor a10, a11, a10
        a9 = ld16u(mem, m)                           # l16ui a9, a4, 0
        a10 &= a9                                    # and a10, a10, a9
        a10 ^= a11                                   # xor a10, a11, a10
        st16(mem, d, a10)                            # s16i a10, a2, 0
        d += 2; s += 2; m += 2
    return d, s, m


# ---------------------------------------------------------------- B: ex19_sprite_blend
def model_blend(mem, dst, src, mask, ones, n_px):
    """ex19_sprite_blend.S, instruction by instruction -- including the prologue that builds 0xF7DE."""
    frame = bytearray(32)                            # `entry a1, 32`
    a9 = (0xF7 << 8) & M32                           # movi a9, 0xF7 ; slli a9, a9, 8
    a9 |= 0xDE                                       # movi a10, 0xDE ; or a9, a9, a10
    frame[4:6] = (a9 & M16).to_bytes(2, "little")    # s16i a9, a1, 4
    a9 |= (a9 << 16) & M32                           # slli a10, a9, 16 ; or a9, a9, a10
    frame[0:4] = a9.to_bytes(4, "little")            # s32i a9, a1, 0
    q6 = vldbc32(frame, 0)                           # EE.VLDBC.32 q6, a1   -> {8{0xF7DE}}
    a9 = ((-1) & M32) >> 0 & 0x7FFF                  # movi a9, -1 ; extui a9, a9, 0, 15
    frame[6:8] = (a9 & M16).to_bytes(2, "little")    # s16i a9, a1, 6
    q7 = vld128(mem, ones)                           # EE.VLD.128.IP q7, a5, 0  (the caller's ones8)
    sar = 1                                          # movi a9, 1 ; ssr a9
    d, s, m = dst, src, mask
    for _ in range(n_px >> 3):
        q0 = vld128(mem, d)                          # EE.VLD.128.IP q0, a2, 0
        q1 = vld128(mem, s); s += 16                 # EE.VLD.128.IP q1, a3, 16
        q2 = vld128(mem, m); m += 16                 # EE.VLD.128.IP q2, a4, 16
        q3 = v_and(q1, q6)                           # EE.ANDQ q3, q1, q6
        q4 = v_and(q0, q6)                           # EE.ANDQ q4, q0, q6
        q3 = v_vmul_u16(q3, q7, sar)                 # EE.VMUL.U16 q3, q3, q7
        q4 = v_vmul_u16(q4, q7, sar)                 # EE.VMUL.U16 q4, q4, q7
        q3 = v_vadds_s16(q3, q4)                     # EE.VADDS.S16 q3, q3, q4
        q4 = v_xor(q0, q3)                           # EE.XORQ q4, q0, q3
        q4 = v_and(q4, q2)                           # EE.ANDQ q4, q4, q2
        q4 = v_xor(q0, q4)                           # EE.XORQ q4, q0, q4
        vst128(mem, d, q4); d += 16                  # EE.VST.128.IP q4, a2, 16
    for _ in range(n_px & 7):
        a9 = ld16u(mem, m)                           # mask word
        a10 = ld16u(mem, s)                          # src word
        a11 = ld16u(mem, d)                          # dst word
        a12 = frame[4] | (frame[5] << 8)             # l16ui a12, a1, 4   (0xF7DE)
        a10 = (a10 & a12) >> 1                       # and ; srli 1
        a11 = (a11 & a12) >> 1                       # and ; srli 1
        a10 = (a10 + a11) & M32                      # add a10, a10, a11
        a12 = frame[6] | (frame[7] << 8)             # l16ui a12, a1, 6   (0x7FFF)
        a10 = min(a10, a12)                          # minu a10, a10, a12  == EE.VADDS.S16's clamp
        a11 = ld16u(mem, d)                          # l16ui a11, a2, 0 (dst again)
        a10 = (a10 ^ a11) & a9                       # xor ; and a10, a10, a9
        a10 ^= a11                                   # xor a10, a10, a11
        st16(mem, d, a10)                            # s16i a10, a2, 0
        d += 2; s += 2; m += 2
    return d, s, m


# ---------------------------------------------------------------- C: ex19_sprite_colorkey
def model_colorkey(mem, dst, src, key, n_px):
    """ex19_sprite_colorkey.S, instruction by instruction."""
    frame = bytearray(32)                            # `entry a1, 32`
    st16(frame, 0, key & M16)                        # s16i a4, a1, 0
    q7 = vldbc16(frame, 0)                           # EE.VLDBC.16 q7, a1 -> {8{key}}
    d, s = dst, src
    for _ in range(n_px >> 3):
        q0 = vld128(mem, s); s += 16                 # EE.VLD.128.IP q0, a3, 16  (src)
        q1 = vld128(mem, d)                          # EE.VLD.128.IP q1, a2, 0   (dst)
        q2 = v_vcmp_eq16(q0, q7)                     # EE.VCMP.EQ.S16 q2, q0, q7
        q2 = v_not(q2)                               # EE.NOTQ q2, q2
        q3 = v_xor(q1, q0)                           # EE.XORQ q3, q1, q0
        q3 = v_and(q3, q2)                           # EE.ANDQ q3, q3, q2
        q3 = v_xor(q1, q3)                           # EE.XORQ q3, q1, q3
        vst128(mem, d, q3); d += 16                  # EE.VST.128.IP q3, a2, 16
    if n_px & 7:
        a12 = frame[0] | (frame[1] << 8)             # l16ui a12, a1, 0  (key, zero-extended)
        a14 = 1                                      # movi a14, 1
        for _ in range(n_px & 7):
            a11 = ld16u(mem, d)                      # dst word
            a10 = ld16u(mem, s)                      # src word
            a9 = a10 ^ a12                           # xor a9, a10, a12
            a9 = min(a9, a14)                        # minu a9, a9, a14
            a9 = (a9 - 1) & M32                      # addi a9, a9, -1  -> 0xFFFF where src == key
            a11 ^= a10                               # xor a11, a11, a10   (dst ^ src)
            a11 &= a9                                # and a11, a11, a9   & keep
            a10 ^= a11                               # xor a10, a10, a11   src ^ (...)
            st16(mem, d, a10)                        # s16i a10, a2, 0
            d += 2; s += 2
    return d, s


KERNEL = {"A": model_opaque, "B": model_blend, "C": model_colorkey}


# ---------------------------------------------------------------- guarded memory, so "no write outside
# the n_px pixels" is checked rather than asserted in prose
GUARD = 64
REGION = 0x200


class Buf:
    def __init__(self, seed):
        self.mem = bytearray(GUARD + 4 * REGION + GUARD)
        for i in range(len(self.mem)):
            self.mem[i] = (i * 7 + seed) & M8
        self.base = {"dst": GUARD, "src": GUARD + REGION, "mask": GUARD + 2 * REGION,
                     "ones": GUARD + 3 * REGION}
        # the caller's ones8: eight identical int16 lanes of 1 (ex08's constant, passed by pointer)
        for i in range(8):
            st16(self.mem, self.base["ones"] + 2 * i, 1)
        self.snapshot = bytes(self.mem)

    def put(self, name, words):
        a = self.base[name]
        for i, v in enumerate(words):
            st16(self.mem, a + 2 * i, v)

    def get(self, name, n):
        a = self.base[name]
        return [ld16u(self.mem, a + 2 * i) for i in range(n)]

    def changed(self):
        """Every byte index that differs from the snapshot."""
        return [i for i, (x, y) in enumerate(zip(self.snapshot, self.mem)) if x != y]

    def legal(self, n_px):
        """The byte indices the contract says may change: the n_px pixels of dst, and nothing else."""
        lo = self.base["dst"]
        return set(range(lo, lo + 2 * n_px))


# ---------------------------------------------------------------- case generation
def gen_cases(rnd, n_cases=300, maxn=27):
    cases = []
    for t in range(n_cases):
        n = rnd.randint(0, maxn)
        # the mask: mostly the two legal values, with a slice of arbitrary lanes (the documented degrade)
        partial = (t % 10 == 0)
        mask = []
        for i in range(n):
            if partial and rnd.random() < 0.35:
                mask.append(rnd.randint(0, M16))
            else:
                mask.append(M16 if rnd.random() < 0.5 else 0)
        # ---- A: full-range 16-bit sprite and background
        d = [rnd.randint(0, M16) for _ in range(n)]
        s = [rnd.randint(0, M16) for _ in range(n)]
        cases.append(("A", n, d, s, mask, None))
        # ---- B: three families, so the saturating add is exercised and the wrap reference does NOT agree
        fam = t % 3
        if fam == 0:
            d = [rnd.randint(0, M16) for _ in range(n)]
            s = [rnd.randint(0, M16) for _ in range(n)]
        elif fam == 1:                                    # both ends of RGB565: near-white + near-black
            d = [rnd.choice([0xFFFF, 0xFFDE, 0xF7DE, 0xF800, 0x07E0, 0x001F, 0x0000, 0x7FFF]) for _ in range(n)]
            s = [rnd.choice([0xFFFF, 0xFFDE, 0xF7DE, 0xF800, 0x07E0, 0x001F, 0x0000, 0x7FFF]) for _ in range(n)]
        else:                                             # ex08's own ranges (main.c: rnd16(0,0x7fff))
            d = [rnd.randint(0, 0x7FFF) for _ in range(n)]
            s = [rnd.randint(0x2000, 0x7FFF) for _ in range(n)]
        cases.append(("B", n, d, s, mask, None))
        # ---- C: the key is drawn from the sprite's own pixels half the time
        d = [rnd.randint(0, M16) for _ in range(n)]
        s = [rnd.randint(0, M16) for _ in range(n)]
        if n and rnd.random() < 0.5:
            for i in range(n):
                if rnd.random() < 0.3:
                    s[i] = 0x1234
            key = rnd.choice([0x1234, rnd.randint(0, M16)])
        else:
            key = rnd.randint(0, M16)
        cases.append(("C", n, d, s, None, key))
    return cases


def write_cases(path, cases):
    with open(path, "w") as f:
        for kind, n, d, s, mask, key in cases:
            row = [f"{kind}", f"{n:x}"]
            if kind == "C":
                row.append(f"{key:04x}")
            row += [f"{v:04x}" for v in d] + [f"{v:04x}" for v in s]
            if kind != "C":
                row += [f"{v:04x}" for v in mask]
            f.write(" ".join(row) + "\n")


def main():
    rnd = random.Random(0xE190C0DE)
    cases = gen_cases(rnd)
    write_cases("/tmp/ex19check/cases.txt", cases)

    proc = subprocess.run(["/tmp/ex19check/ref"], stdin=open("/tmp/ex19check/cases.txt", "rb"),
                          stdout=subprocess.PIPE, check=True)
    ref_lines = proc.stdout.decode().strip().split("\n")
    assert len(ref_lines) == len(cases), (len(ref_lines), len(cases))

    bad = []
    n_a = n_b = n_c = 0
    lanes = {"A": 0, "B": 0, "C": 0}
    outside = 0                     # bytes the model wrote outside the n_px pixels
    partial_lanes = 0
    spec_bad = 0
    wrap_diff = 0                    # lanes where the saturating kernel and ex08's 32-bit reference differ
    max_half_sum = 0
    sat_lanes = 0
    key_hit = 0
    keep_lanes = {"A": 0, "B": 0, "C": 0}
    hit_lanes = {"A": 0, "B": 0, "C": 0}
    # the misaligned counter-example: run kernel A's vector path with dst 2 bytes off the grid
    misaligned_escape = None

    for idx, ((kind, n, d, s, mask, key), rline) in enumerate(zip(cases, ref_lines)):
        r = rline.split()
        assert r[0] == kind
        vals = [int(x, 16) for x in r[1:]]
        buf = Buf(seed=idx & M8)
        buf.put("dst", d)
        buf.put("src", s)
        if mask is not None:
            buf.put("mask", mask)
        buf.snapshot = bytes(buf.mem)                # the guards are checked against the case data
        ones = [1] * 8
        f = KERNEL[kind]
        if kind == "A":
            n_a += 1
            f(buf.mem, buf.base["dst"], buf.base["src"], buf.base["mask"], n)
        elif kind == "B":
            n_b += 1
            f(buf.mem, buf.base["dst"], buf.base["src"], buf.base["mask"], buf.base["ones"], n)
        else:
            n_c += 1
            f(buf.mem, buf.base["dst"], buf.base["src"], key, n)

        got = buf.get("dst", n)
        want = vals[:n]
        if got != want:
            bad.append((kind, idx, n, got, want))
        lanes[kind] += n
        if kind == "A":
            want_spec = vals[n:2 * n]
            if want_spec != want:
                spec_bad += 1
            if mask is not None:
                partial_lanes += sum(1 for m in mask if m not in (0, M16))
                keep_lanes["A"] += sum(1 for m in mask if m == 0)
                hit_lanes["A"] += sum(1 for m in mask if m == M16)
        if kind == "B":
            want_wrap = vals[n:2 * n]
            for i in range(n):
                h = half565(s[i]) + half565(d[i])
                max_half_sum = max(max_half_sum, h)
                if h > 0x7FFF:
                    sat_lanes += 1
                if want[i] != want_wrap[i]:
                    wrap_diff += 1
            keep_lanes["B"] += sum(1 for m in mask if m == 0)
            hit_lanes["B"] += sum(1 for m in mask if m == M16)
            partial_lanes += sum(1 for m in mask if m not in (0, M16))
        if kind == "C":
            for i in range(n):
                if s[i] == key:
                    key_hit += 1
                    keep_lanes["C"] += 1
                else:
                    hit_lanes["C"] += 1
        # the write-set check
        illegal = set(buf.changed()) - buf.legal(n)
        outside += len(illegal)
        if illegal:
            bad.append((kind + "-outside", idx, sorted(illegal)[:8], "", ""))

        # the misaligned counter-example, on the first A case with a whole group of eight
        if kind == "A" and n >= 8 and misaligned_escape is None:
            buf2 = Buf(seed=idx & M8)
            # an all-opaque sprite, so every lane of every chunk is written: the escape cannot hide behind
            # a transparent lane that happens to write the same byte back
            allmask = [M16] * n
            buf2.put("dst", d)
            buf2.put("src", s)
            buf2.put("mask", allmask)
            buf2.snapshot = bytes(buf2.mem)                # the guards are checked against the case data
            base = buf2.base["dst"]
            buf2.base["dst"] = base + 2                    # 2 bytes off the 16-byte grid
            model_opaque(buf2.mem, buf2.base["dst"], buf2.base["src"], buf2.base["mask"], n)
            esc = sorted(set(buf2.changed()) - buf2.legal(n))
            misaligned_escape = (idx, n, len(esc), [x - (base + 2) for x in esc[:6]], [x - (base + 2) for x in esc[-3:]])

    # ---- facts derived from the instruction semantics, reported as numbers
    hi_half = max(half565(v) for v in range(0x10000))
    max_ex08 = max(half565(v) for v in range(0, 0x8000))          # main.c: rnd16(0, 0x7fff)
    max_ex08b = half565(0x8AD0)                                    # main.c's s_pa[i] = -30000 case
    print("ex19_sprite.S host check: instruction-level model (Python) vs ex19_*_c (host gcc -O2)")
    print("inputs: deterministic PRNG, seed 0xE190C0DE; 300 A cases, 300 B cases, 300 C cases,")
    print("        n_px drawn from 0..27 so the scalar tail (n_px & 7) and the empty case are both hit")
    print("        buffers 16-byte aligned inside a guarded bytearray: a write outside the n_px pixels")
    print("        is a failure, not an assumption")
    print("")
    print(f"kernel A (ex19_sprite_opaque):   {lanes['A']} pixels over {n_a} calls "
          f"({hit_lanes['A']} opaque lanes, {keep_lanes['A']} transparent lanes)")
    print(f"   model vs ex19_sprite_opaque_c   mismatches: {sum(1 for b in bad if b[0] == 'A')}")
    print(f"   model vs ex19_sprite_opaque_spec_c (the contract, not the .S): "
          f"{'equal on every case' if spec_bad == 0 else str(spec_bad) + ' cases differ'}")
    print(f"   arbitrary mask lanes fed (the documented bitwise degrade): {partial_lanes}")
    print(f"   bytes written outside the n_px pixels: {outside}")
    print("")
    print(f"kernel B (ex19_sprite_blend):    {lanes['B']} pixels over {n_b} calls "
          f"({hit_lanes['B']} blended lanes, {keep_lanes['B']} kept lanes)")
    print(f"   model vs ex19_sprite_blend_c    mismatches: {sum(1 for b in bad if b[0] == 'B')}")
    print(f"   lanes where the saturating kernel differs from ex08's 32-bit wrapping reference "
          f"(c_half_blend): {wrap_diff}")
    print(f"   lanes where half(src) + half(dst) reached the 0x8000 ceiling: {sat_lanes}"
          f"  (largest sum seen: 0x{max_half_sum:04X})")
    print(f"   largest half() over all 65536 lane values: 0x{hi_half:04X} (so two halves can reach "
          f"0x{2 * hi_half:04X})")
    print(f"   ex08's own data cannot reach the ceiling: max half over [0,0x7FFF] = 0x{max_ex08:04X}, "
          f"and the -30000 lane (0x8AD0) halves to 0x{max_ex08b:04X} -> worst sum 0x{max_ex08 + max_ex08b:04X}")
    print("")
    print(f"kernel C (ex19_sprite_colorkey): {lanes['C']} pixels over {n_c} calls "
          f"({key_hit} pixels equal to the key, {lanes['C'] - key_hit} replaced)")
    print(f"   model vs ex19_sprite_colorkey_c mismatches: {sum(1 for b in bad if b[0] == 'C')}")
    print("")
    print(f"TOTAL model-vs-reference mismatches over {n_a + n_b + n_c} cases: "
          f"{sum(1 for b in bad if not b[0].endswith('-outside'))}")
    for b in bad[:4]:
        print("   MISMATCH", b[0], b[1], str(b[2])[:100], str(b[3])[:100])
    if misaligned_escape:
        print("")
        idx, n, cnt, first, last = misaligned_escape
        print(f"the alignment contract is a real constraint, not a formality: the same kernel A model run "
              f"on case {idx} (n_px={n}, so the vector path runs)")
        print(f"   with dst 2 bytes off the 16-byte grid writes {cnt} bytes outside the n_px pixels: "
              f"the first are at dst offsets {first} and the last at {last}")
        print("   -- the low four address bits are dropped, so every store lands on the chunk below and the "
              "last chunk's tail spills past the row. The 900-case check above")
        print("      runs the same kernel only on 16-byte-aligned bases, which is the contract the .md states.")
    return 1 if any(not b[0].endswith("-outside") for b in bad) or outside else 0


if __name__ == "__main__":
    sys.exit(main())
```

#### C 参照（`/tmp/ex19check/ref.c`、main.c に入る形）

```c
/* ex19_sprite reference + case driver (host gcc). This is the code that would go into main.c's ex19()
 * section; here it is compiled on the host and fed the same cases the Python model is.
 *
 * Protocol (stdin, one case per line, all values hex, uppercase-free):
 *   A n d0..d(n-1) s0..s(n-1) m0..m(n-1)
 *   B n d0.. s0.. m0..
 *   C n key d0.. s0..
 * stdout, one line per case:
 *   A <n words of ex19_sprite_opaque_c> <n words of ex19_sprite_opaque_spec_c>
 *   B <n words of ex19_sprite_blend_c>  <n words of ex19_sprite_blend_wrap_c>
 *   C <n words of ex19_sprite_colorkey_c>
 *
 * The buffers are 16-byte aligned and surrounded by guard words: -DGUARD build checks that neither
 * reference writes outside [0, n_px).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define MAXN  64
#define GUARD 16

static uint8_t   g_dst[16 + (MAXN + GUARD) * 2] __attribute__((aligned(16)));
static uint8_t   g_src[16 + (MAXN + GUARD) * 2] __attribute__((aligned(16)));
static uint8_t   g_msk[16 + (MAXN + GUARD) * 2] __attribute__((aligned(16)));
static uint8_t   g_ref[16 + (MAXN + GUARD) * 2] __attribute__((aligned(16)));

static int16_t  *dst  = (int16_t *)(g_dst + 16);   /* 16-byte aligned, guard words on both sides */
static int16_t  *src  = (int16_t *)(g_src + 16);
static uint16_t *msk  = (uint16_t *)(g_msk + 16);
static int16_t  *rf   = (int16_t *)(g_ref + 16);

static uint16_t half565(uint16_t v)
{
    /* ex08_half_blend's first two ops: EE.ANDQ with 0xF7DE then EE.VMUL.U16 by ones with SAR = 1.
     * The .S does the >>1 with EE.VMUL.U16 -- the UNSIGNED shift -- for the reason ex08 measured
     * (finding vmul_s16_is_arithmetic_and_vmul_u16_is_logical): EE.VMUL.S16 would sign-extend a lane
     * whose bit 15 is set. So the reference works on uint16_t and masks with 0xFFFF. */
    return (uint16_t)((v & 0xF7DEu) >> 1);
}

/* ---- A: opaque. The kernel's own expression, per lane. */
void ex19_sprite_opaque_c(int16_t *d, const int16_t *s, const uint16_t *m, uint32_t n_px)
{
    for (uint32_t i = 0; i < n_px; i++) {
        uint16_t dw = (uint16_t)d[i], sw = (uint16_t)s[i], mw = m[i];
        d[i] = (int16_t)(uint16_t)(dw ^ ((dw ^ sw) & mw));
    }
}

/* ---- A: the contract read off ex19_sprite.md, not off the .S: an opaque lane is replaced by the sprite
 * pixel, a transparent lane keeps the background BIT FOR BIT, and a lane that is neither is the
 * documented bitwise degrade. A caller is promised this; the model is checked against the .S. */
void ex19_sprite_opaque_spec_c(int16_t *d, const int16_t *s, const uint16_t *m, uint32_t n_px)
{
    for (uint32_t i = 0; i < n_px; i++) {
        uint16_t dw = (uint16_t)d[i], sw = (uint16_t)s[i], mw = m[i];
        if (mw == 0xFFFFu) {
            d[i] = s[i];                                   /* opaque */
        } else if (mw == 0x0000u) {
            /* transparent: d[i] keeps its value -- no store at all */
        } else {
            d[i] = (int16_t)(uint16_t)(dw ^ ((dw ^ sw) & mw));   /* partial lane: bitwise, documented */
        }
    }
}

/* ---- B: half-transparent sprite. hb = sat16(half(src) + half(dst)), exactly ex08_half_blend's value,
 * and EE.VADDS.S16 saturates: both halves are <= 0x7BEF = 31727, i.e. positive as signed 16-bit, so the
 * signed saturation point is +32767. */
void ex19_sprite_blend_c(int16_t *d, const int16_t *s, const uint16_t *m, uint32_t n_px)
{
    for (uint32_t i = 0; i < n_px; i++) {
        uint32_t h = (uint32_t)half565((uint16_t)s[i]) + (uint32_t)half565((uint16_t)d[i]);
        uint16_t hb = (uint16_t)(h > 0x7FFFu ? 0x7FFFu : h);      /* EE.VADDS.S16 */
        uint16_t dw = (uint16_t)d[i], mw = m[i];
        d[i] = (int16_t)(uint16_t)(dw ^ ((dw ^ hb) & mw));
    }
}

/* ---- B: the same halves added in 32 bits and truncated. This is main.c's c_half_blend (ex08's
 * reference) verbatim in formula: it is what the firmware compares the PIE kernel against today, and it
 * agrees with the kernel exactly while no lane's two halves sum to 0x8000 or more. */
void ex19_sprite_blend_wrap_c(int16_t *d, const int16_t *s, const uint16_t *m, uint32_t n_px)
{
    for (uint32_t i = 0; i < n_px; i++) {
        uint16_t hb = (uint16_t)((uint32_t)half565((uint16_t)s[i]) + (uint32_t)half565((uint16_t)d[i]));
        uint16_t dw = (uint16_t)d[i], mw = m[i];
        d[i] = (int16_t)(uint16_t)(dw ^ ((dw ^ hb) & mw));
    }
}

/* ---- C: colour key. dst keeps its value where src == key. */
void ex19_sprite_colorkey_c(int16_t *d, const int16_t *s, int16_t key, uint32_t n_px)
{
    for (uint32_t i = 0; i < n_px; i++) {
        if (s[i] != key) {
            d[i] = s[i];
        }
    }
}

static int hexw(const char *t, uint16_t *out)
{
    char *end;
    unsigned long v = strtoul(t, &end, 16);
    if (end == t) return 0;
    *out = (uint16_t)v;
    return 1;
}

int main(void)
{
    static char line[1 << 20];
    while (fgets(line, sizeof line, stdin)) {
        char *tok, *save = NULL;
        tok = strtok_r(line, " \t\r\n", &save);
        if (!tok) continue;
        char kind = tok[0];
        tok = strtok_r(NULL, " \t\r\n", &save);
        uint32_t n = (uint32_t)strtoul(tok, NULL, 16);
        for (int i = -(int)GUARD / 2; i < (int)(MAXN + GUARD / 2); i++) {
            dst[i] = (int16_t)0x5A5A; src[i] = (int16_t)0x5A5A;
            msk[i] = (uint16_t)0xA5A5; rf[i] = (int16_t)0x5A5A;
        }
        uint16_t key = 0;
        if (kind == 'C') { tok = strtok_r(NULL, " \t\r\n", &save); hexw(tok, &key); }
        for (uint32_t i = 0; i < n; i++) { tok = strtok_r(NULL, " \t\r\n", &save); hexw(tok, (uint16_t *)&dst[i]); }
        for (uint32_t i = 0; i < n; i++) { tok = strtok_r(NULL, " \t\r\n", &save); hexw(tok, (uint16_t *)&src[i]); }
        if (kind != 'C')
            for (uint32_t i = 0; i < n; i++) { tok = strtok_r(NULL, " \t\r\n", &save); hexw(tok, &msk[i]); }

        printf("%c", kind);
        if (kind == 'A') {
            memcpy(rf, dst, MAXN * 2);
            ex19_sprite_opaque_c(dst, src, msk, n);
            ex19_sprite_opaque_spec_c(rf, src, msk, n);
            for (uint32_t i = 0; i < n; i++) printf(" %04X", (uint16_t)dst[i]);
            for (uint32_t i = 0; i < n; i++) printf(" %04X", (uint16_t)rf[i]);
        } else if (kind == 'B') {
            memcpy(rf, dst, MAXN * 2);
            ex19_sprite_blend_c(dst, src, msk, n);
            ex19_sprite_blend_wrap_c(rf, src, msk, n);
            for (uint32_t i = 0; i < n; i++) printf(" %04X", (uint16_t)dst[i]);
            for (uint32_t i = 0; i < n; i++) printf(" %04X", (uint16_t)rf[i]);
        } else {
            ex19_sprite_colorkey_c(dst, src, (int16_t)key, n);
            for (uint32_t i = 0; i < n; i++) printf(" %04X", (uint16_t)dst[i]);
        }
        printf("\n");
    }
    return 0;
}
```

```bash
# 実行（このリポジトリの外、/tmp で）
$ gcc -O2 -Wall -Wextra -o ref ref.c
$ .venv/bin/python model.py
```

```
ex19_sprite.S host check: instruction-level model (Python) vs ex19_*_c (host gcc -O2)
inputs: deterministic PRNG, seed 0xE190C0DE; 300 A cases, 300 B cases, 300 C cases,
        n_px drawn from 0..27 so the scalar tail (n_px & 7) and the empty case are both hit
        buffers 16-byte aligned inside a guarded bytearray: a write outside the n_px pixels
        is a failure, not an assumption

kernel A (ex19_sprite_opaque):   4308 pixels over 300 calls (2084 opaque lanes, 2088 transparent lanes)
   model vs ex19_sprite_opaque_c   mismatches: 0
   model vs ex19_sprite_opaque_spec_c (the contract, not the .S): equal on every case
   arbitrary mask lanes fed (the documented bitwise degrade): 272
   bytes written outside the n_px pixels: 0

kernel B (ex19_sprite_blend):    4308 pixels over 300 calls (2084 blended lanes, 2088 kept lanes)
   model vs ex19_sprite_blend_c    mismatches: 0
   lanes where the saturating kernel differs from ex08's 32-bit wrapping reference (c_half_blend): 641
   lanes where half(src) + half(dst) reached the 0x8000 ceiling: 1212  (largest sum seen: 0xF7DE)
   largest half() over all 65536 lane values: 0x7BEF (so two halves can reach 0xF7DE)
   ex08's own data cannot reach the ceiling: max half over [0,0x7FFF] = 0x3BEF, and the -30000 lane (0x8AD0) halves to 0x4168 -> worst sum 0x7D57

kernel C (ex19_sprite_colorkey): 4308 pixels over 300 calls (278 pixels equal to the key, 4030 replaced)
   model vs ex19_sprite_colorkey_c mismatches: 0

TOTAL model-vs-reference mismatches over 900 cases: 0

the alignment contract is a real constraint, not a formality: the same kernel A model run on case 3 (n_px=13, so the vector path runs)
   with dst 2 bytes off the 16-byte grid writes 2 bytes outside the n_px pixels: the first are at dst offsets [-2, -1] and the last at [-2, -1]
   -- the low four address bits are dropped, so every store lands on the chunk below and the last chunk's tail spills past the row. The 900-case check above
      runs the same kernel only on 16-byte-aligned bases, which is the contract the .md states.
```

**不一致 0 / 900 ケース**です。うちカーネル A は契約（`0xFFFF`→src、`0x0000`→dst）を書いた
`ex19_sprite_opaque_spec_c` とも全ケース一致、任意マスク 272 レーンも含みます。

### 5.2 実際の .S を解釈実行する（piesim.py）

`/workspace/pjs-vm/tools/pie/piesim.py` を `/tmp/ex19check/piesim_ex19.py` にコピーし、
**このファイルが使う命令のうち元の実装が持っていなかったものだけ**を追加して、`.S` のテキストを
そのまま解釈実行しました（コメント／ディレクティブを落として小文字化しただけ。ラベルはそのまま）。
**未対応命令は `NotImplementedError` で落ちるので、黙って通ることはありません。**

* 追加した PIE 命令（TRM の疑似コードから）: `EE.VCMP.EQ.S16`（p155）、`EE.VCMP.GT.S16`（p158）、
  `EE.NOTQ`（p120）、`EE.VLDBC.32`（p173）
* 追加したコア（スカラー）命令: `entry`, `retw.n`, `movi`, `srli`, `slli`, `extui`, `ssr`, `or`, `and`,
  `xor`, `add`, `minu`, `s32i`, `l32i`, `l16ui`, `s16i`, `beqz`
  —— **これらは TRM の外**です（TRM はベース ISA を定めておらず、Xtensa ISA リファレンスは
  Espressif から PDF で落とせない）。ループを回すための自明なスカラー命令で、**本モデルの追加分**である
  ことを明記します（PIE 命令は疑似コード由来、スカラーはこの 17 個という切り分け）。
* 元のファイルとの差はこの 2 点だけです: (a) 分岐先の解決。元は `bnez ar, 1b` の数値ラベル前提で
  最後の 1 文字を落とす実装なので、`.S` の名前付きラベル（`.lop_loop` 等）も通るようにしました。
  (b) `ssr` を元の `wsr.sar` と同じ扱い（SAR[5:0]）にしました。
* ハードウェアとの**意図的なずれ**: `entry a1, 32` は no-op で、`a1` は 16 バイト整列した 32 バイトの
  作業領域を最初から指しています（3 カーネルとも `a1+0..a1+7` しか触らないので、フレームポインタだけで足ります）。

```
ex19_sprite.S through the instruction interpreter (/workspace/pjs-vm/tools/pie/piesim.py,
copied to /tmp/ex19check/piesim_ex19.py and extended for the ops it did not carry)
cases: every fourth case of the model's 900 = 225, compared against the compiled C reference

  A: 31 instruction lines extracted from the .S, 19 distinct mnemonics
  B: 60 instruction lines extracted from the .S, 29 distinct mnemonics
  C: 37 instruction lines extracted from the .S, 24 distinct mnemonics

result: instructions executed / pixels written / mismatches against the C reference
  A (ex19_sprite_opaque    ):   4209 instructions,  1031 pixels, 0 mismatches over 75 cases (53 with the vector loop, 66 with a scalar tail, 0 empty)
      first case's pointer walk: dst +4 bytes, src +4 bytes, mask +4 bytes, pixels left in the counter: 0
  B (ex19_sprite_blend     ):   8288 instructions,  1031 pixels, 0 mismatches over 75 cases (55 with the vector loop, 67 with a scalar tail, 3 empty)
      first case's pointer walk: dst +26 bytes, src +26 bytes, mask +26 bytes, pixels left in the counter: 0
  C (ex19_sprite_colorkey  ):   4577 instructions,   997 pixels, 0 mismatches over 75 cases (56 with the vector loop, 67 with a scalar tail, 1 empty)
      first case's pointer walk: dst +2 bytes, src +2 bytes (the key is not a pointer), pixels left in the counter: 0

TOTAL: 17074 instructions interpreted, 3059 pixels, mismatches: 0

the interpreter raises NotImplementedError on any op it does not carry, so 'it ran' means every
instruction this file executes was modelled from the TRM pseudo-code or (for the scalar
bookkeeping) from the core Xtensa ISA: add, addi, and, beqz, bnez, ee.andq, ee.notq, ee.vadds.s16, ee.vcmp.eq.s16, ee.vld.128.ip, ee.vldbc.16, ee.vldbc.32, ee.vmul.u16, ee.vst.128.ip, ee.xorq, entry, extui, l16ui, minu, movi, or, retw.n, s16i, s32i, slli, srli, ssr, xor
```
**この 2 つの検査は別々のものではなく、組み合わせで意味を持ちます**: `model.py`（.S の命令列を読んだもの）と
`.S` のテキスト（piesim が解釈実行するもの）は**直接には突き合わせていません**。両者が共有しているのは
C 参照（`ref.c`）だけなので、§5.1（モデル ≡ C 参照）と §5.2（.S ≡ C 参照）が**両方成り立って初めて
.S ≡ モデルが言えます**。piesim を通す意味はそこにあります（レジスタ割り当て・ポインタの歩き・
ループの回り方を、私の写しではなく実際のテキストで確かめる）。


## 6. アセンブラの証拠（実機は使っていない）

```
$ xtensa-esp32s3-elf-gcc --version | head -1
xtensa-esp-elf-gcc (crosstool-NG esp-15.2.0_20251204) 15.2.0

$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex19.o examples/firmware/main/proposed/ex19_sprite.S
rc=0

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex19.o
0000004c 0000009e T ex19_sprite_blend
000000ec 0000005e T ex19_sprite_colorkey
00000000 0000004b T ex19_sprite_opaque

$ xtensa-esp32s3-elf-objdump -h /tmp/ex19.o
Idx Name          Size      VMA       LMA       File off  Algn
  3 .iram1        0000014a  00000000  00000000  00000034  2**2

$ xtensa-esp32s3-elf-objdump -d /tmp/ex19.o | grep -c l32r
0

$ xtensa-esp32s3-elf-objdump -r /tmp/ex19.o | grep -c R_XTENSA_SLOT0_OP  (intra-.iram1 branch fixups)
12
```

命令語の照合は 2 段構えです。**(a)** `tools/asm_toolchain.py --check-instruction` が
各命令の命令語図とアセンブラの出力を 2 通りのオペランドで突き合わせ、**採用した 10 命令すべて `match`**
（§7 の表の最終列が manual word == toolchain word）。**(b)** このファイルが実際に出す 30 行について、
マニュアルの図から **このファイルのオペランドで符号化した語**と `as` の出力語を比較して **30/30 一致**
（下に抜粋、`EE.VLD.128.IP q0, a2, 0` → `830024`、`EE.VCMP.EQ.S16 q2, q0, q7` → `9e5894` など）。

```
$ # このファイルが出す 30 行、マニュアルの命令語図から符号化した語 vs as の出力語
EE.VLD.128.IP    q0, a2, 0              manual=830024  as=830024  match
EE.VLD.128.IP    q1, a3, 16             manual=838134  as=838134  match
EE.VLD.128.IP    q2, a4, 16             manual=930144  as=930144  match
EE.XORQ          q3, q0, q1             manual=ddb124  as=ddb124  match
EE.ANDQ          q3, q3, q2             manual=ddb454  as=ddb454  match
EE.XORQ          q3, q0, q3             manual=ddb524  as=ddb524  match
EE.VST.128.IP    q3, a2, 16             manual=9a8124  as=9a8124  match
EE.VLDBC.32      q6, a1                 manual=fd7714  as=fd7714  match
EE.VLD.128.IP    q7, a5, 0              manual=b38054  as=b38054  match
EE.VLD.128.IP    q0, a2, 0              manual=830024  as=830024  match
EE.VLD.128.IP    q1, a3, 16             manual=838134  as=838134  match
EE.VLD.128.IP    q2, a4, 16             manual=930144  as=930144  match
EE.ANDQ          q3, q1, q6             manual=ddbc14  as=ddbc14  match
EE.ANDQ          q4, q0, q6             manual=ed3c04  as=ed3c04  match
EE.VMUL.U16      q3, q3, q7             manual=9efba4  as=9efba4  match
EE.VMUL.U16      q4, q4, q7             manual=ae7ca4  as=ae7ca4  match
EE.VADDS.S16     q3, q3, q4             manual=9ec364  as=9ec364  match
EE.XORQ          q4, q0, q3             manual=ed3524  as=ed3524  match
EE.ANDQ          q4, q4, q2             manual=ed3484  as=ed3484  match
EE.XORQ          q4, q0, q4             manual=ed3904  as=ed3904  match
EE.VST.128.IP    q4, a2, 16             manual=aa0124  as=aa0124  match
EE.VLDBC.16      q7, a1                 manual=fdf314  as=fdf314  match
EE.VLD.128.IP    q0, a3, 16             manual=830134  as=830134  match
EE.VLD.128.IP    q1, a2, 0              manual=838024  as=838024  match
EE.VCMP.EQ.S16   q2, q0, q7             manual=9e5894  as=9e5894  match
EE.NOTQ          q2, q2                 manual=dd7f44  as=dd7f44  match
EE.XORQ          q3, q1, q0             manual=ddb114  as=ddb114  match
EE.ANDQ          q3, q3, q2             manual=ddb454  as=ddb454  match
EE.XORQ          q3, q1, q3             manual=ddb534  as=ddb534  match
EE.VST.128.IP    q3, a2, 16             manual=9a8124  as=9a8124  match
30 lines: all match
```

## 7. 使った命令と TRM のページ（`data/pie_instructions.json` より）

| 命令 | TRM ページ | アセンブラ構文 | Operation（マニュアルからの引用・レーン 1 本ぶん） | 命令語図 vs アセンブラ |
|---|---|---|---|---|
| `EE.ANDQ` | p76 | `EE.ANDQ qa, qx, qy` | `qa = qx & qy` | match: edbce4 == edbce4 |
| `EE.VCMP.EQ.S16` | p155 | `EE.VCMP.EQ.S16 qa, qx, qy` | `qa[ 15: 0] = (qx[ 15: 0]==qy[ 15: 0]) ? 0x{}FFFF : 0 … (レーン 2..8 も同じ位置関係)` | match: aede94 == aede94 |
| `EE.NOTQ` | p120 | `EE.NOTQ qa, qx` | `qa = ~qx` | match: edffc4 == edffc4 |
| `EE.VLD.128.IP` | p164 | `EE.VLD.128.IP qu, as, -2048..2032` | `qu[127:0] = load128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}` | match: e38064 == e38064 |
| `EE.VLDBC.16` | p170 | `EE.VLDBC.16 qu, as` | `qu[127:0] = {8{load16({as[31:1],1{0}})}}` | match: edf364 == edf364 |
| `EE.VLDBC.32` | p173 | `EE.VLDBC.32 qu, as` | `qu[127:0] = {4{load32({as[31:2],2{0}})}}` | match: edf764 == edf764 |
| `EE.VMUL.U16` | p204 | `EE.VMUL.U16 qz, qx, qy` | `qz[ 15: 0] = (qx[ 15: 0] * qy[ 15: 0]) >> SAR[5:0] … (レーン 2..8 も同じ位置関係)` | match: aefea4 == aefea4 |
| `EE.VADDS.S16` | p146 | `EE.VADDS.S16 qa, qx, qy` | `qa[ 15: 0] = min(max(qx[ 15: 0] + qy[ 15: 0], -2^{15}), 2^{15}-1) … (レーン 2..8 も同じ位置関係)` | match: aede64 == aede64 |
| `EE.VST.128.IP` | p275 | `EE.VST.128.IP qv, as, -2048..2032` | `qv[127:0] => store128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}` | match: ea8064 == ea8064 |
| `EE.XORQ` | p297 | `EE.XORQ qa, qx, qy` | `qa = qx ^ qy` | match: edbde4 == edbde4 |

引用のみ（**このファイルは 1 度も出しません**）:
* `EE.VCMP.GT.S16`（TRM p158、`EE.VCMP.GT.S16 qa, qx, qy`）: `qa[ 15: 0] = (qx[ 15: 0]>qy[ 15: 0]) ? 0x{}FFFF : 0 … (レーン 2..8 も同じ位置関係)` — 比較の候補（`.S16` の符号性が未計測なので `.EQ` だけ使用。§8-1）
* `EE.VCMP.LT.S16`（TRM p161、`EE.VCMP.LT.S16 qa, qx, qy`）: `qa[ 15: 0] = (qx[ 15: 0]<qy[ 15: 0]) ? 0x{}FFFF : 0 … (レーン 2..8 も同じ位置関係)` — 同義の比較の候補（同上）。pjs-vm の blend はこれを使っていますが、別リポジトリの資料です
* `EE.ORQ`（TRM p121、`EE.ORQ qa, qx, qy`）: `qa = qx | qy` — `(src & mask) | (dst & ~mask)` という 4 命令形にだけ要る。3 命令の XOR 形を採用（§2）
* `EE.VMUL.S16`（TRM p198、`EE.VMUL.S16 qz, qx, qy`）: `qz[ 15: 0] = (qx[ 15: 0] * qy[ 15: 0]) >> SAR[5:0] … (レーン 2..8 も同じ位置関係)` — ex08 が実機で否定した側（符号拡張）。`.U16` を採用（§2.2）
* `EE.VZIP.16`（TRM p293、`EE.VZIP.16 qs0, qs1`）: `qs0[ 15: 0] = qs0[ 15: 0] … (レーン 2..8 も同じ位置関係)` — 16 レーンを偶数/奇数に戻す命令。レーン形マスクでは不要（§1.2）
* `EE.VUNZIP.16`（TRM p290、`EE.VUNZIP.16 qs0, qs1`）: `qs0[ 15: 0] = qs0[ 15: 0] … (レーン 2..8 も同じ位置関係)` — 16 レーンを偶数/奇数に振り分ける命令。詰め形マスク（16bit マスク 2 本 / 32bit ワード）なら要るが、この契約では不要（§1.2）

採用した 3 カーネルは上の 10 命令と、ループ制御・端数処理のスカラー命令だけです
（`entry` / `retw.n` / `srli` / `extui` / `beqz` / `bnez` / `addi` / `l16ui` / `s16i` / `s32i` / `l16si` /
`movi` / `slli` / `or` / `and` / `xor` / `minu` / `ssr`）。**`EE.ORQ`（p121）と `EE.VZIP.16`（p293）/
`EE.VUNZIP.16`（p290）は引用のみで、このファイルは 1 度も出しません**（理由は §1.2 と §2）。

## 8. 実機で確かめていないこと（先に叩くべき順）

1. **`EE.VCMP.*.S16` の符号性と `0xFFFF` 出力**（kernel C のマスク全体）。TRM は名前も記述も `.S16` /
   「数値を比較」と言い、疑似コード p155 は `0xFFFF`/`0` を書くと書いています。**しかしこの命令の
   実測はありません**: `data/pie_examples_measured.json` の実測 run は ex01–ex09 までで、`VCMP` を使う
   ex12（`examples/firmware/main/ex12_physics.S:160-181`）は ex12 自身の .md が「実機では未実行」と
   書いています。符号無し比較だった場合、負の画素でマスクが反転します。`pjs-vm/docs/pie-simd.md` の
   blend は `EE.VCMP.LT.S16` を使っていますが、これも**別リポジトリの資料**（measured-elsewhere tier）で
   このスイートの実測ではありません。
2. **ex08 のハーフブレンドが明るい画素で `0x7FFF` に張り付くという結論**。これは `EE.VADDS.S16` の
   符号飽和（p146 の `min(max(…, -2^15), 2^15-1)`）から**導いた**もので、`EE.VADDS.S16` 自体の飽和は
   ex05/ex08 の文脈では測られていますが**この入力域（`half+half ≧ 0x8000`）では測っていません**。
   もし飽和せずにラップするなら、§2.2 の 641 レーンは「ラップ側の値」になります。
3. **`EE.VCMP.EQ.S16` の等値がビット一致であること**（署名の意図）。EQ は符号に関係ないので
   安全な方の仮定ですが、`-0` のような例外が無いことは確認していません。
4. **128bit のアクセスが整列を強制すること**。ex03 が実機で確定させた finding
   （`vld128_drops_the_low_address_bits`）をそのまま前提にしています。**ストア側は測っていません**
   （ex15 も同じ前提を置いています）。§3 の対照実験はモデルの中の話です。
5. **`EE.VLDBC.32` / `EE.VLDBC.16` の整列強制**（p173/p170 の下位 2bit/1bit 落とし）。ex12 が同じ
   組を使っていますが、ブロードキャスト自体の強制は未計測です。このファイルは
   4 バイト整列したフレームスロット（`entry a1, 32` の `a1+0`/`a1+4`/`a1+6`）からのみ読むので、
   ずれたときの症状は「隣の定数を読む」です。
6. **サイクル数**。§4 は発行スロット数の計算で、表に載っていないオペランド（ループ制御や端数処理の
   スカラー命令 = `addi`/`bnez`/`l16ui`/`s16i`/`xor`/`minu`/`movi`/`ssr` など）は
   「def = use = 1」と仮定しています（`EE.NOTQ` や `EE.ORQ` は表に載っています: def 1 / use 1）。
   `ex19_sprite_blend` は 14 命令/8 画素なので、135×240 の全画面スプライト（32400 画素）を
   1 枚合成すると **発行スロットだけで約 56700**。notes/08 の 30 fps 予算（画素あたり約 67 サイクル）に
   対して、この規模の計算では**足りる/足りないの判断はできません**（実測が要ります）。
7. **データ配置は設計であって、マニュアルの事実ではありません**（`uint16` レーン形マスク、
   レーン i = バイト 2i、`src`/`dst`/`mask` の 16 バイト整列）。§5 の照合は「.S とモデルと C 参照が
   この配置で一致する」ことしか言えず、**ハードウェアについては何も言っていません**。
   配線するときに最初に疑うべきはここです。

## 9. ビルドへの入れ方（今回は触っていない）

```c
/* examples/firmware/main/examples.h */
void ex19_sprite_opaque  (int16_t *dst, const int16_t *src, const uint16_t *mask, uint32_t n_px);
void ex19_sprite_blend   (int16_t *dst, const int16_t *src, const uint16_t *mask,
                          const int16_t *ones8, uint32_t n_px);
void ex19_sprite_colorkey(int16_t *dst, const int16_t *src, int16_t key, uint32_t n_px);

/* main.c, ex19(): DATA/CHECK/BENCH の形（notes/08 の「サンプルの足し方」に従う） */
static void ex19(void)
{
    const char *ex = "ex19", *name = "sprite";
    section_begin(ex, name);
    /* バッファは __attribute__((aligned(16)))。mask は 0xFFFF/0x0000 の uint16 レーン */
    ex19_sprite_opaque(s_dst, s_src, s_mask, N_PX);
    ex19_sprite_opaque_c(s_dst, s_src, s_mask, N_PX);          /* ref.c の実装 */
    check(ex, name, "opaque_matches_C", memcmp(s_dst, s_dst_ref, N_PX * 2) == 0, 1);
    ex19_sprite_blend(s_dst, s_src, s_mask, s_ones8, N_PX);
    ex19_sprite_colorkey(s_dst, s_src, 0x1234, N_PX);
    printf("BENCH sprite pixels=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", …);
    section_end(ex, name, fail == 0 ? 1 : 0, fail);
}
```

* ホスト側チェッカー（`tools/check_examples_log.py`）には `check_ex19()` を足して、
  ログが運ぶ入力から 3 つを再計算します（`ex19_*_c` の移植 + マスクの 2 値性の検査）。
* `tools/selftest_examples_checker.py` の DATA 変異も 1 つ要ります。
* 実機で最初に見るべきは §8 の 1 と 2 です（`VCMP` の符号性と、明るい画素での `VADDS` 飽和）。

## 10. 検査が捕まえたもの（この草案を書いている間に実際に落ちたもの）

1. **kernel C の端数処理のマスク極性が逆でした**（`addi a9, a9, -1` の後を「差分が非ゼロのとき
   `0xFFFF`」と読み違えていた）。モデルと C 参照を突き合わせた最初の実行で
   **271/900 ケースが不一致**になり、原因は「`minu` で 0/1 に畳んだ後の `-1` は
   **等しいとき**に `0xFFFF`（＝キープマスク）になる」ことでした。現在は
   `out = src ^ ((src ^ dst) & keep)` という担体違いの同値な綴りになっており、
   この 2 つの綴りがビット単位で一致すること自体が検査項目になっています。
2. **「範囲外を書かない」テストが最初は空振りしました**。`dst` を 2 バイトずらした対照実験で
   最初に使ったマスクはランダムな `0xFFFF`/`0x0000` で、たまたま透明レーンが
   「読んだ値を書き戻す」ため、はみ出した 2 バイトが元の値と一致して**検出できませんでした**。
   全レーン不透明に固定して初めて `dst-2`, `dst-1` が上書きされるのが見えました
   （透明レーンは**同じ値を書き戻す**ので、バグがデータに隠れる、という良い教訓です）。
3. **piesim に食わせるテキストの作り方で 2 回踏みました**。ブロックコメントが
   命令行の次の行まで続く書き方をしていたため、コメント剥がしが行単位だと
   `NotImplementedError: the` で落ちました（piesim 自身の `_strip_comments` と同じく
   テキスト全体に対して剥がす必要がある）。またラベル行（`.lop_tail:`）は
   ディレクティブと同じ「`.` で始まる行」なので、最初の抽出は分岐先を全部落としていました。
   **どちらもモデルの話ではなくハーネスの話**ですが、同じ罠は次に誰かが踏みます。

## 11. この例題が確定させること（もし流せたら）

* マスク付き合成の 3 カーネルが、**分岐なし・マスク 1 命令生成**（カラーキーは 8 命令/8 画素）で書けること。
* `EE.VCMP.*.S16` が**レーン形マスクを吐く**ことの実機確認（= §8 の 1 が消える）。
  これは ex12 のクリップマスクとこの合成を**変換なしで繋げる**ための前提です。
* 半透明合成の上限（明るい画素で `0x7FFF` に張り付く）が実機でもそうなること。
  ここが違えば、ex08 のハーフブレンドの位置づけを量産リスト側で見直す必要があります。
