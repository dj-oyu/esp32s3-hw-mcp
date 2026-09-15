# ex15 (proposed) — 非整列フレームバッファ転送（LCD ウィンドウが奇数のバイトから始まる場合）

`examples/firmware/main/proposed/ex15_fbstore.S` — notes/08-media-3d-perf.md のパイプライン表が
**未着手**と書いている段（`フレームバッファ転送 | EE.SRCQ.128.ST.INCP(非整列寄せ, SAR_BYTE) + GDMA/SPI`）を、
その表が挙げている 3 命令で書いたものです。対象は Cardputer ADV のパネル（135×240 px、16 bpp = 2 B/px、
PSRAM なし）。フレームバッファ全体は 64800 B = **4050 チャンクちょうど**ですが、**1 行は 270 B = 16.875
チャンク**で、ウィンドウはチャンクの途中から始まり得ます。

**このファイルと付属の証拠はすべて実機を使っていません。** 追試したのは (1) アセンブラ（`xtensa-esp32s3-elf-gcc`
→ `objdump` → `data/pie_instructions.json` の命令語図とのフィールド単位照合）、(2) Python のモデル（128 ビット
アクセスの意味を TRM の疑似コードどおりに実装し、memcpy 参照と全バイト照合）、(3) ホストの C 検証器（glibc の
`memcpy` で契約そのものを再計算）の 3 つだけです。**シリコンで確かめていない前提は末尾に全部列挙**してあります。

```c
void ex15_store_window(int16_t *dst, int32_t byte_off, const uint8_t *src, uint32_t n_bytes);
void ex15_copy_row    (int16_t *dst, const int16_t *src, uint32_t n_px);
void ex15_fill_row    (int16_t *dst, int16_t color, uint32_t n_px);
```

## 1. なぜ memcpy では書けないのか

128 ビットの PIE アクセスはアドレスを `{as[31:4], 4{0}}` の形で作る（TRM p49, figure 1.7-1）。**下位 4 ビットは
「無視される」のではなく「落とされる」**ので、バイト 1 から始まるウィンドウへの 16 バイトストアは、バイト 0 に
書いて**ウィンドウの手前のバイトを静かに壊します**（例外にはなりません）。ex03 はこれをロード側で実機再現し、
`data/pie_examples_measured.json` の `vld128_drops_the_low_address_bits` として残しています:

> A 128-bit PIE access forms its address as `{as[31:4], 4{0}}`: the low four address bits are dropped, not
> honoured, so an access off the 16-byte grid silently reads a different 16 bytes (and does not fault).

したがって非整列ウィンドウを動かす手段は 2 つだけで、このカーネルは両方を使います。

1. **入り口で寄せる（shift on the way in）**: `EE.LD.128.USAR.IP` (TRM p93) はポインタを含む整列チャンクを
   ロードして、落としたビットを `SAR_BYTE` に退避します。`EE.SRCQ.128.ST.INCP` (TRM p132) は
   `{qs1[127:0], qs0[127:0]} >> (SAR_BYTE*8)` を `{as[31:4],4{0}}` にストアし、`as` を 16 進めます。
   この 2 命令で任意バイト開始の 16 バイト窓が出て、ストアアドレスは勝手に歩きます。**オペランド順は
   構文行からは読めず、第 1 オペランドが qs0 = 下位**です（実機確定: `src_q_operand_order_and_the_funnel_path`。
   本ファイルの命令語デコーダでも `ee.src.q q7, q5, q6 -> qs0=5 qs1=6` として再確認しています）。
2. **両端の半端チャンクを read-modify-write**: ウィンドウが部分的にしか覆わないチャンクでは、ウィンドウ外の
   バイトを元の値のまま残す必要があります。本カーネルは先頭と末尾の両方を RMW し、**中間は (1) だけ**です
   （16 バイトあたり命令 2 個・メモリ操作 2 個）。

**中間が 2 命令/16 B で済む理由**がこのファイルの設計の核です。先頭の RMW が**宛先側のずれを吸収する**ので、
そこから先の宛先チャンクはすべて 16 バイト境界に載ります。すると効いてくる位相は**ソースポインタの下位 4 ビット
だけ**になり、これは **16 バイト刻みでは変わらない**ので、`SAR_BYTE` は中間ループの最初のロードで 1 回設定すれば
最後まで有効です。3 命令目になるはずの「ずらし直し」が消えるのはこのためです。

## 2. 契約（どの命令列をどの場合に使うか）

`dst` は**呼び出し側が 16 バイト整列を保証**します（整列していないポインタを 128 ビットアクセスに渡しても
診断されず、下の境界から読むだけ — `pie_buffers_must_be_declared_aligned_16`）。`src` は任意バイト整列で
かまいません。`n_bytes == 0` は合法で、**ロードもストアもしません**。

事後条件: `framebuffer[byte_off + j] == src[j]`（0 ≤ j < n_bytes）かつ、**それ以外のバイトは 1 バイトも
変えない** — ウィンドウと最初/最後のチャンクを共有するバイトも含みます。

`k = byte_off & 15`、`h = 16-k`、`b = min(n_bytes, h)`、`D = dst + byte_off`、`E = D + n_bytes` として:

| 場合 | 先頭チャンク | 中間 | 末尾チャンク | .S のラベル |
|---|---|---|---|---|
| `n_bytes == 0` | 何もしない | 何もしない | 何もしない | `beqz a5, .Lsw_done` |
| 16 バイト整列 (`k == 0`) | **不要**（窓が格子に載っている） | `EE.LD.128.USAR.IP` + `EE.SRCQ.128.ST.INCP` | `n_bytes & 15 != 0` なら RMW | `.Lsw_interior` 直行 |
| 2 バイト整列 (`k` 偶数 ≠ 0) | RMW（置換範囲 `[k .. k+b-1]`） | 同じ | RMW（置換 `[0 .. m-1]`） | `.Lsw_bknown` → `.Lsw_pair` → `.Lsw_tail` |
| 奇数 (`k` 奇数) | 同じ命令列（画素をまたぐ） | 同じ | 同じ | 同上 |
| 16 バイト未満が残る (`n_bytes < h`) | **両側マスク付き RMW 1 回だけ**（`[0..k-1]` と `[k+b..15]` を保持） | なし（フルチャンクが無い） | 到達しない | `.Lsw_short` → `.Lsw_mask` |
| 16 バイト未満が 2 チャンクにまたがる (`h < n_bytes < 16`) | RMW | なし | RMW | `.Lsw_bknown` → `.Lsw_tail` |

* **2 バイト整列の場合が LCD 経路の本命**です（16 bpp なので画素境界 = 偶数バイト。例えば 4 px = 8 B ずれた
  ウィンドウ）。この場合、2 つの RMW が置換する範囲は「偶数長のランが偶数オフセットから始まる」形なので、
  **1 つの画素が 2 つのストアに分かれることも、半分だけ書かれることもありません**。
* **奇数オフセット**では画素をまたぎます: バイト `k` は画素 `k-1` の上位バイトです。これは 16 ビットパネル上の
  バイト粒度ウィンドウに固有の性質で、カーネルの性質ではありません（ウィンドウ外のバイトは元の値のまま）。
  パネルの CASET/RASET は画素単位なので、奇数オフセットは「パネルの行」からではなく、パックド 1/8 bpp の
  スプライト、バイト単位のスクロール、回転・斜めウィンドウなどから来ます。
* **読み出しフットプリント**（モデルが実測として報告）: ウィンドウ先頭より最大 **30 バイト手前**、末尾より
  最大 **32 バイト先**（読みのみ・副作用なし）。理由は 2 つで、どちらも「位相合わせのロードはポインタを含む
  整列チャンクを読む」ことから来ます: 先頭の漏斗ポインタは `src-k` で、その整列チャンクは最大 15 B 下から
  始まる（k ≤ 15 なので合計 30 B）、中間ループは 1 チャンク先読みする（32 B）。

## 3. マスクの作り方（と、なぜ EE.SRCI.2Q ではだめか）

RMW のマージは「ウィンドウが上書きするバイト」を選ぶマスク 1 枚と、その補集合で済みます。マスクを作る
候補として真っ先に思いつくのは `EE.SRCI.2Q` (TRM p129) の 256 ビット論理シフトですが、**そのシフト量は
4 ビットの即値フィールド `sar16[3:0]`（シフト = `(sar16+1)*8`）**で、実行時の `k` には使えません。
`EE.SRC.Q` のシフト量は `SAR_BYTE` から来て、`SAR_BYTE` は**毎回の `EE.LD.128.USAR.IP` がそのアドレスの
下位 4 ビットから書く** — つまり実行値です。`qs0` をゼロ、`qs1` を 0xFF のレジスタにすると、

```
EE.SRC.Q qa, qs0=zero, qs1=ones, SAR_BYTE=s   ->   0x00 x (16-s) ++ 0xFF x s
```

つまり **境界は `16-s`**。境界を `b` に置きたいなら `s = 16-b` で、**そのシフトは「下位 4 ビットが 16-b の
アドレスからのロード」で作る**しかありません。これが .S にある 3 つの「poke」ロードの正体です。poke は
**すでに RMW しているチャンク自身**を読むだけなので、バッファ外に出ません:

| 目的 | SAR_BYTE | poke アドレス | 実際に読まれるチャンク |
|---|---|---|---|
| 先頭の置換マスク | `16-k` (= `h`) | `c0 + h` | `floor(c0+h) = c0`（先頭チャンク自身） |
| 窓が先頭チャンク内で終わる場合の 2 枚目 | `16-k-b` | `c0 + (h-b)` | `c0` |
| 末尾の保持マスク | `16-m` | `floor(E) + (16-m)` | `floor(E)`（末尾チャンク自身） |

逆に、**poke ではない**ロード 2 つは「残すべきバイトそのもの」を返しつつ都合よく `SAR_BYTE` を設定します:
`EE.LD.128.USAR.IP q5, D, 0` は `q5 = floor(D)`（先頭チャンクの旧バイト）と `SAR_BYTE = k`、
`EE.LD.128.USAR.IP q5, E, 0` は `q5 = floor(E)`（末尾チャンクの旧バイト）と `SAR_BYTE = m`。

オペランド順は**このマスクの向きを決める**ので、間違えると静かに反転します。**このファイルの最初の版は
それを間違えていました**（境界が `b`、つまりシフト `b`）— ホストのモデルを走らせた瞬間に T1 の 1536 ケース中
1120 ケースが落ちて分かりました。モデルを先に書いて並べて走らせる価値が出た場所です。

## 4. コスト（全部「発行スロット」と「メモリ操作」で、サイクル実測ではない）

`xtensa-esp32s3-elf-objdump -d` の命令数と、モデルが数えた命令別呼び出し回数から:

| 段 | 発行スロット | PIE 命令 | メモリ操作 | 呼び出しあたりの回数 |
|---|---|---|---|---|
| 先頭 RMW (`k != 0`) | 30 | 17 | 5（4 LD + 1 ST、`n_bytes < h` のとき 6） | 1 |
| 中間（16 B ごと） | **3**（2 チャンク = 6 スロット） | **2**（`USAR.IP` + `SRCQ.128.ST.INCP`） | **2** | `full` チャンク分 |
| 末尾 RMW (`m != 0`) | 22 | 13 | 5（4 LD + 1 ST） | 1 |
| `ex15_copy_row`（16 B ごと） | 3（2 チャンク = 6 スロット） | 2 | 2 | 行全体 |
| `ex15_fill_row`（16 B ごと） | 3（1 チャンク = 3 スロット） | **1** | **1**（読み無し） | 行全体 |

先頭・末尾の RMW は **`k`・`m` に依存しない定数**です（バイト数に比例しません）。中間ループの 3 スロット/16 B は
`addi`+`bnez` の 2 スカラを含むので、4 倍展開すれば 2.5 スロット/16 B まで下がります。**構造的な下限はメモリ操作
の本数**で、中間が 16 B あたり 2、`fill_row` が 1 本です（ここが「呼び出し側が持てる下限」）。

### 奇数オフセットで RMW が必要な理由と、staging との比較（算術）

RMW が要るのは、16 バイト整列ストアが**必ず 16 バイト全部を書く**からです（アドレスは `{as[31:4],4{0}}` —
TRM p49）。ウィンドウがチャンクの途中から始まる／途中で終わると、そのチャンクの残りは「窓の外」なのに
128 ビットストアでは一緒に潰れてしまう。だから両端だけは「古いバイトを読み、窓のバイトと入れ替え、書き戻す」
= RMW になります。中間は窓がチャンクを丸ごと覆うので RMW は不要です。

「整列スクラッチに一度コピーしてから整列ストアすれば RMW が要らない」という代替案との比較（1 フレーム =
64800 B = 4050 チャンク、16 B 単位）:

| | 命令/16 B | メモリ操作/16 B | 1 フレームのメモリ操作 | 1 フレームが触るバイト | 追加 RAM |
|---|---|---|---|---|---|
| 本カーネル（中間） | 2 | **2** | 8100 | 129600（書）+ 端の読み | 0 |
| 整列スクラッチ経由 | 4 | **4** | 16200 | 259200（書 129600 + 読 129600） | 16〜272 B / 同時呼び出し |

しかも **staging は RMW を消せません**: スクラッチへの充填自体が「非整列 → 整列」の貼り付けなので、
スクラッチ側の両端で同じ RMW が必要です（または非整列バイトコピー）。つまり staging は RMW を**消さずに
1 パス増やす**だけで、メモリ操作が 2 倍、触るバイトが 2 倍（1 フレームあたり 259.2 KB 対 129.6 KB）、
共有スクラッチなら再入不可というコストが付きます。SPI で送る 1 フレームが同じ 129.6 KB であることを考えると、
「フレームあたりもう 1 フレーム分のメモリトラフィック」が staging の値段です。だから本カーネルは RMW 側を
採ります。

**行ごとに呼ぶ場合の算術**も出しておきます。270 B の行は 16 の倍数ではないので、行頭の位相は
`270 i mod 16`（270 mod 16 = 14）で 8 周期を回り、値は偶数だけ、`0` になるのは 8 行に 1 行です。
つまり **8 行中 7 行が非整列開始**（`k` 偶数 ≠ 0）で、その行には先頭 RMW（17 PIE、メモリ操作 5）と
末尾 RMW（13 PIE、同 5）が余分に付きます。240 行なら **7200 PIE 命令と 2400 メモリ操作の上乗せ**です。
同じ 270 B をフレーム一括で流す場合はこれが 0 になります（64800 = 4050×16 ちょうどなので、端の半端
チャンクが存在しないから）。**呼び出し側への推奨はこれだけ**: 連続した最大のランを渡すこと。

## 5. Python モデル（このセッションで実際に走らせたもの）

層は 2 つです。層 1 は TRM の疑似コードから書いた命令の意味（1 命令 1 関数、ページ番号はコメントに）。
層 2 は **.S と同じ順序・同じレジスタ役割で書いた 3 つのカーネル**なので、2 つを並べて読めます。層 1 の
呼び出しは全部数えていて、上のコスト表はそこから出ています。

```python
#!/usr/bin/env python3
"""ex15_fbstore -- an independent model of the three kernels in ex15_fbstore.S, checked byte-exactly against
memcpy-style references.

Two layers, on purpose:

  layer 1  instruction semantics, one function per PIE instruction, each from the Operation pseudo-code that
           data/pie_instructions.json records (TRM page numbers in the comments). Layer 2 may not use a
           shortcut layer 1 does not provide: the byte masks, for instance, are not written out by hand --
           they are produced by running EE.SRC.Q on a register built by EE.ZERO.Q + EE.NOTQ, exactly as the
           kernel does, so the model can disagree with the kernel about the mask trick (it did: see T6).

  layer 2  the three kernels, written as the SAME sequence of instructions the .S emits -- same order, same
           register roles (q0..q7, a2..a15) -- so the two can be read side by side. Every layer-1 call is
           counted: that is where the operation counts quoted in the .md come from.

What is checked
  T1  lengths 0, 1, 15, 16, 17, 480 x every destination byte offset 0..15 x every source phase 0..15
  T2  offsets that straddle several chunks (17, 31, 51, 133, 255, 271 bytes in), lengths 33/64/200/480,
      source phases 0/1/7/8/15
  T3  400 random (offset, length, phase) triples, lengths including 0..3, 14..18, 31..33, 45..48, 63..65
  T4  a whole frame: 135x240 px at 2 B/px = 64800 bytes = 4050 chunks exactly, at offsets 0 and 270, source
      phases 0/1/15/3
  T5  ex15_copy_row and ex15_fill_row against their references, over the row lengths the panel actually has
  T6  the mask builder the kernel uses (EE.SRC.Q, shift from a load address) against the definition for every
      boundary b, plus why EE.SRCI.2Q cannot do it
  Every case also checks the 32 guard bytes before and after the window: a 128-bit store that wrote its
  rounded-down chunk shows up there, and avoiding that is the whole point of the kernel.

Runner: python3 /tmp/ex15_model.py     (standard library only; also writes the case dump the C verifier reads)
"""
import random

MASK128 = (1 << 128) - 1
CHUNK = 16          # 128-bit access granularity == the forced-alignment granule (TRM p49)

OPS = {}            # instruction -> layer-1 call count, for the cost table


def _op(name):
    OPS[name] = OPS.get(name, 0) + 1


# --------------------------------------------------------------------------- layer 1: instruction semantics
class Mem:
    """A byte array with a base address. A 128-bit access forces as[3:0] to 0 (TRM p49), so addresses are
    modelled explicitly: a read or write outside the buffer is a model error, not a silent wrap."""

    def __init__(self, base, data):
        self.base = base
        self.data = bytearray(data)
        self.reads = []                            # (aligned_addr, 16) for every 128-bit read

    def read16(self, addr):
        a = addr & ~0xF                                        # {as[31:4], 4{0}}
        off = a - self.base
        if off < 0 or off + CHUNK > len(self.data):
            raise IndexError("read16 outside the buffer: addr=%#x base=%#x len=%d"
                             % (a, self.base, len(self.data)))
        self.reads.append((a, CHUNK))
        return bytes(self.data[off:off + CHUNK])

    def write16(self, addr, val):
        a = addr & ~0xF
        off = a - self.base
        if off < 0 or off + CHUNK > len(self.data):
            raise IndexError("write16 outside the buffer: addr=%#x base=%#x" % (a, self.base))
        self.data[off:off + CHUNK] = val[:CHUNK]


def ld128_usar_ip(mem, ptr):
    """EE.LD.128.USAR.IP qu, as, imm   (TRM p93)
       qu[127:0] = load128({as[31:4],4{0}});  SAR_BYTE = as[3:0];  as[31:0] += {20{imm16[7]},imm16[7:0],4{0}}
       The load returns the aligned chunk CONTAINING the pointer and parks the dropped bits in SAR_BYTE. It
       cannot be asked for a different chunk than the one the pointer sits in, and it always disturbs
       SAR_BYTE -- that coupling is what shapes this kernel in both directions: it is how the funnel gets its
       shift, and it is why every shift has to come from an address the kernel already holds."""
    _op("EE.LD.128.USAR.IP")
    return mem.read16(ptr), (ptr & 0xF)


def vld128_ip(mem, ptr):
    """EE.VLD.128.IP qu, as, imm   (TRM p164): qu = load128({as[31:4],4{0}}); as += imm<<4."""
    _op("EE.VLD.128.IP")
    return mem.read16(ptr)


def vst128_ip(mem, ptr, qv):
    """EE.VST.128.IP qv, as, imm   (TRM p275): store128({as[31:4],4{0}}) = qv[127:0]; as += imm<<4."""
    _op("EE.VST.128.IP")
    mem.write16(ptr, qv)


def src_q(qs0, qs1, sar_byte):
    """EE.SRC.Q qa, qs0, qs1   (TRM p125)
       qa[127:0] = {qs1[127:0], qs0[127:0]} >> (SAR_BYTE[3:0] << 3)
       Operand 1 is qs0 = the LOW half: settled on silicon (finding src_q_operand_order_and_the_funnel_path in
       data/pie_examples_measured.json) and re-read out of the emitted word by the .md's word decoder
       (ee.src.q q7, q5, q6 -> qs0=5 qs1=6)."""
    _op("EE.SRC.Q")
    s = int.from_bytes(qs0, "little") | (int.from_bytes(qs1, "little") << 128)
    return ((s >> (8 * sar_byte)) & MASK128).to_bytes(16, "little")


def srcq_128_st_incp(mem, ptr, qs0, qs1, sar_byte):
    """EE.SRCQ.128.ST.INCP qs0, qs1, as   (TRM p132)
       {qs1[127:0], qs0[127:0]} >> (SAR_BYTE << 3)  =>  store128({as[31:4],4{0}});  as += 16
       The shift and the store address come from the same pointer/SAR_BYTE pair, so one instruction publishes
       a 16-byte window that starts off the grid AND advances the address register by 16."""
    _op("EE.SRCQ.128.ST.INCP")
    mem.write16(ptr, src_q(qs0, qs1, sar_byte))
    return ptr + 16


def srci_2q(qs0, qs1, sar16):
    """EE.SRCI.2Q qs1, qs0, sar16   (TRM p129) -- a 256-bit LOGICAL shift by (sar16+1)*8, zero filled,
       written back into both halves. NOT used by the kernel: sar16 is a 4-bit IMMEDIATE field, so a mask
       whose boundary depends on the runtime offset cannot be built with it. T6 shows what it would give."""
    _op("EE.SRCI.2Q")
    s = int.from_bytes(qs0, "little") | (int.from_bytes(qs1, "little") << 128)
    r = s >> (8 * (sar16 + 1))
    return ((r & MASK128).to_bytes(16, "little"), ((r >> 128) & MASK128).to_bytes(16, "little"))


def andq(qx, qy):
    """EE.ANDQ qa, qx, qy   (TRM p76): qa = qx & qy."""
    _op("EE.ANDQ")
    return bytes(a & b for a, b in zip(qx, qy))


def orq(qx, qy):
    """EE.ORQ qa, qx, qy   (TRM p121): qa = qx | qy."""
    _op("EE.ORQ")
    return bytes(a | b for a, b in zip(qx, qy))


def notq(qx):
    """EE.NOTQ qa, qx   (TRM p120): qa = ~qx, byte for byte."""
    _op("EE.NOTQ")
    return bytes((~a) & 0xFF for a in qx)


def zero_q():
    """EE.ZERO.Q qa   (TRM p299): qa = 0."""
    _op("EE.ZERO.Q")
    return bytes(16)


def movi_32_q(q, word, sel4):
    """EE.MOVI.32.Q qu, as, sel4   (TRM p119): qu[32*sel4+31 : 32*sel4] = as."""
    _op("EE.MOVI.32.Q")
    b = bytearray(q)
    b[4 * sel4:4 * sel4 + 4] = (word & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(b)


def mask_from_phase(zero, ones, sar_byte):
    """The one mask-building instruction the kernel uses, in the form the .S uses it:

           EE.SRC.Q qa, qs0=zero, qs1=ones, SAR_BYTE=s      =>    0x00 x (16-s) ++ 0xFF x s

       i.e. "byte j set for j >= 16-s". The mask's BOUNDARY is therefore 16-SAR_BYTE: a mask whose boundary
       must land on b needs SAR_BYTE = 16-b, obtained from a load whose address has those low four bits (the
       "pokes" in the .S).

       The operand order is the whole trick. With qs0 = ones and qs1 = zero the result is the complement
       ("bytes 0..15-s set"), which is not the mask a read-modify-write wants. The .S and this model were both
       written the other way round first (boundary at SAR_BYTE, i.e. shift b); the equivalence run below is
       what caught it -- 1120 of 1536 cases failed -- and it is the reason the model exists at all."""
    return src_q(zero, ones, sar_byte)


# --------------------------------------------------------------------------- layer 2: the three kernels
def ex15_store_window(fb, dst_addr, byte_off, sr, src_addr, n_bytes):
    """void ex15_store_window(int16_t *dst, int32_t byte_off, const uint8_t *src, uint32_t n_bytes)

    Phase by phase, in the .S's order. Registers are named as in the .S: a6 = D, a7 = k, a8 = h, a9 = the
    chunk being read-modify-written, a13 = E, a14 = b then the tail remainder, q0 = zero, q2 = ones, q3 =
    keep, q4 = replace, q5 = the old chunk, q6/q7 = the window pair.
    """
    fb.reads = []
    sr.reads = []
    ops = []
    a6 = dst_addr + byte_off                     # D: the window start (unaligned)
    a13 = a6 + n_bytes                           # E: the window end
    a7 = byte_off & 0xF                          # k   (extui a7, a3, 0, 4)
    a11 = a6                                     # the destination chunk (the k == 0 case)
    a12 = src_addr                               # the interior source pointer (the k == 0 case)
    if n_bytes == 0:
        return ops                               # beqz a5, .Lsw_done
    if a7 == 0:
        _tail.E = a13
        return _interior_tail(fb, a7, sr, src_addr, n_bytes, n_bytes, a11, a12, ops, None)

    # ---- head: read-modify-write of bytes [k .. k+b-1] of floor(D)
    a8 = 16 - a7                                 # h = 16-k
    a9 = a6 - a7                                 # floor(D), the head chunk
    a14 = min(n_bytes, a8)                       # b = min(n_bytes, h)
    a10 = src_addr - a7                          # the window that ends k bytes into the chunk is at src-k
    q5, sar_win = ld128_usar_ip(sr, a10)         # the aligned chunk containing src-k
    q0 = zero_q()                                # EE.ZERO.Q (also the free slot in the .S's schedule)
    q6, _ = ld128_usar_ip(sr, a10 + 16)          # +16 leaves as[3:0] alone: SAR_BYTE is unchanged
    q2 = notq(q0)                                # ones
    q7 = src_q(q5, q6, sar_win)                  # the 16 bytes at src-k: q7[k..15] == src[0 .. 15-k]
    q5, sar_k = ld128_usar_ip(fb, a6)            # floor(D) = the head chunk, and SAR_BYTE = k
    a15 = a9 + a8                                # c0 + h, whose low four bits are h = 16-k
    _, sar_p1 = ld128_usar_ip(fb, a15)           # POKE: SAR_BYTE = 16-k (the data is discarded)
    assert sar_p1 == (16 - a7) & 0xF, "head poke 1 must land SAR_BYTE on 16-k"
    q4 = mask_from_phase(q0, q2, sar_p1)         # replace: bytes k..15 set
    if a14 < a8:                                 # n_bytes < h: the window also ends inside this chunk
        a15 = a9 + (a8 - a14)                    # c0 + (16-k-b)
        _, sar_p2 = ld128_usar_ip(fb, a15)       # POKE: SAR_BYTE = 16-k-b
        assert sar_p2 == (16 - a7 - a14) & 0xF, "head poke 2 must land SAR_BYTE on 16-k-b"
        q1 = mask_from_phase(q0, q2, sar_p2)     # bytes (k+b)..15 set
        q3 = notq(q1)                            # bytes 0..(k+b-1) set
        q4 = andq(q4, q3)                        # replace = bytes [k .. k+b-1] only
    q3 = notq(q4)                                # keep
    q5 = andq(q5, q3)                            # old & keep
    q4 = andq(q7, q4)                            # window & replace
    q5 = orq(q5, q4)
    vst128_ip(fb, a9, q5)                        # the merged head chunk
    ops.append(("head-rmw", a7, a14, sar_win, sar_p1))
    a5 = n_bytes - a14                           # n_bytes -= b
    if a5 == 0:
        return ops                               # the whole window fitted in the head chunk
    _tail.E = a13
    return _interior_tail(fb, a7, sr, src_addr, n_bytes, a5, a9 + 16, src_addr + a14, ops,
                          (a7, a14, sar_win))


def _interior_tail(fb, a7, sr, src_addr, n_bytes_total, a5, a11, a12, ops, head):
    """The interior and the tail, shared by the aligned-destination path and the head path.
       a5 = the bytes still to move, a11 = the first 16-byte-aligned destination chunk, a12 = the source
       pointer whose low four bits define SAR_BYTE for the whole interior loop."""
    a15 = a5 >> 4                                # full chunks (srli a15, a5, 4)
    if a15:
        pairs = a15 >> 1                         # srli a9, a15, 1
        q0, sar = ld128_usar_ip(sr, a12)         # C0
        q1, _ = ld128_usar_ip(sr, a12 + 16)      # C1
        for i in range(pairs):                   # 2 chunks per iteration: store, load, store, load
            a11 = srcq_128_st_incp(fb, a11, q0, q1, sar)
            q0, _ = ld128_usar_ip(sr, a12 + 32 + 32 * i)
            a11 = srcq_128_st_incp(fb, a11, q1, q0, sar)
            q1, _ = ld128_usar_ip(sr, a12 + 48 + 32 * i)
        if a15 & 1:                              # the odd chunk left over: one store, no trailing load
            a11 = srcq_128_st_incp(fb, a11, q0, q1, sar)
        ops.append(("interior", a15, sar))
    return _tail(fb, sr, src_addr, n_bytes_total, a11, ops, head)


def _tail(fb, sr, src_addr, n_bytes_total, a11, ops, head):
    """The tail read-modify-write: bytes [0 .. m-1] of floor(E) are replaced, [m .. 15] keep their old value.
       The .S needs no bookkeeping for it: m = E & 15, the destination is floor(E) = E - m, the source pointer
       is src + n_bytes_total - m (the window's last m bytes), and the keep mask's boundary is m, so the mask's
       shift is 16-m, poked from floor(E) + (16-m)."""
    E = _tail.E
    m = E & 0xF
    if m == 0:
        return ops
    q5, sar_m = ld128_usar_ip(fb, E)             # floor(E): the tail chunk, and SAR_BYTE = m
    q0 = zero_q()
    a9 = E - m                                   # floor(E), the merge's destination
    q2 = notq(q0)                                # ones
    a15 = a9 + (16 - m)
    _, sar_keep = ld128_usar_ip(fb, a15)         # POKE: SAR_BYTE = 16-m (the data is discarded)
    assert sar_keep == (16 - m) & 0xF, "the tail poke must land SAR_BYTE on 16-m"
    q3 = mask_from_phase(q0, q2, sar_keep)       # keep: bytes m..15 set
    q4 = notq(q3)                                # replace: bytes 0..m-1 set
    a10 = src_addr + n_bytes_total - m           # the window's last m bytes
    q6, sar_t = ld128_usar_ip(sr, a10)
    q7, _ = ld128_usar_ip(sr, a10 + 16)
    q1 = src_q(q6, q7, sar_t)                    # q1[0..m-1] = the last m bytes
    q5 = andq(q5, q3)                            # old & keep
    q1 = andq(q1, q4)                            # window & replace
    q5 = orq(q5, q1)
    vst128_ip(fb, E - m, q5)
    ops.append(("tail-rmw", m, sar_t))
    # Two properties the .S relies on rather than asserts, checked here: the tail chunk's own load lands
    # SAR_BYTE on m (it does by construction, and it is what the m-shift would otherwise have to poke), and
    # the tail window's phase equals the interior's, because a 16-byte stride preserves a pointer's low bits.
    assert sar_m == m
    if head is not None:
        assert sar_t == (src_addr + head[1]) & 0xF, "tail window phase != the interior phase"
    return ops


def ex15_copy_row(dst, src, n_px):
    """void ex15_copy_row(int16_t *dst, const int16_t *src, uint32_t n_px)

    The aligned baseline: both pointers 16-byte aligned, one EE.VLD.128.IP + one EE.VST.128.IP per 8 px
    (16 bytes), with the load of the next chunk two issues ahead of the store consuming the previous one,
    which is what keeps the stage-M-to-stage-E interlock off the critical path. n_px must be a multiple of 8;
    a 135-px row (270 B = 16.875 chunks) is not, which the caller solves by padding rows or by calling
    ex15_store_window with a byte count."""
    chunks = n_px >> 3
    off = 0
    if chunks & 1:                               # the unpaired chunk first: one plain load/store pair
        q0 = vld128_ip(src, src.base)
        vst128_ip(dst, dst.base, q0)
        off = 16
    if chunks >> 1 == 0:
        return chunks                            # nothing left: the .S does not even read
    q0 = vld128_ip(src, src.base + off)          # start the pipeline: this chunk is stored next round
    for i in range(chunks >> 1):
        q1 = vld128_ip(src, src.base + off + 16 * (2 * i + 1))
        vst128_ip(dst, dst.base + off + 16 * (2 * i), q0)
        q0 = vld128_ip(src, src.base + off + 16 * (2 * i + 2))
        vst128_ip(dst, dst.base + off + 16 * (2 * i + 1), q1)
    return chunks


def ex15_fill_row(dst, color, n_px):
    """void ex15_fill_row(int16_t *dst, int16_t color, uint32_t n_px)

    The cheapest row write there is: the colour is broadcast into eight int16 lanes once (EXTUI + slli + or
    for the 32-bit pattern, then four EE.MOVI.32.Q), then one EE.VST.128.IP per 8 px and nothing is read."""
    word = (color & 0xFFFF) | ((color & 0xFFFF) << 16)
    q0 = zero_q()
    for sel4 in range(4):
        q0 = movi_32_q(q0, word, sel4)
    assert q0 == (color & 0xFFFF).to_bytes(2, "little") * 8, "colour register must be 8 identical lanes"
    for i in range(n_px >> 3):
        vst128_ip(dst, dst.base + 16 * i, q0)
    return n_px >> 3


# --------------------------------------------------------------------------- references (the memcpy side)
def ref_store_window(fb_data, off, src_bytes, n):
    want = bytearray(fb_data)
    want[off:off + n] = src_bytes[:n]
    return want


def ref_fill_row(n_px, color):
    return bytearray((color & 0xFFFF).to_bytes(2, "little") * n_px)


# --------------------------------------------------------------------------- the run
FB_BASE = 0x3FC90000                                # 16-byte aligned, like a static int16 array must be
FB_LEN = 70000
SRC_BASE = 0x3FCA0000
SRC_PAD = 64
WIN_AT = 512
random.seed(0xE15)
DUMP = open("/tmp/ex15_cases.txt", "w")

cases = bytes_swept = mismatched_bytes = guard_fail = 0
first_mismatch = None
pre_read = post_read = 0
foot_pre = foot_post = None
phase_use = {}
class_count = {}
ops_by_class = {}


def classify(off, n):
    k = off & 0xF
    if n == 0:
        return "empty (n_bytes == 0), no access at all"
    if k == 0 and (n & 15) == 0:
        return "16-byte aligned destination, whole chunks, no partial chunk"
    if k == 0:
        return "16-byte aligned destination, partial tail chunk"
    if n < 16 - k:
        return "less than 16 bytes remaining (whole window inside the head chunk)"
    if n < 16:
        return "less than 16 bytes, spanning two chunks"
    if k & 1:
        return "odd offset (pixel-straddling)"
    return "2-byte aligned offset (pixel-exact)"


def run_case(off, n, src_phase, label):
    global cases, bytes_swept, mismatched_bytes, guard_fail, first_mismatch
    global pre_read, post_read, foot_pre, foot_post
    cases += 1
    fb0 = bytearray(FB_LEN)
    for i in range(FB_LEN):
        fb0[i] = (0xA0 + (i * 7)) & 0xFF                       # deterministic, non-constant
    src_bytes = bytes(((i * 13 + 5) & 0xFF) for i in range(n + 160))
    s_at = SRC_PAD + src_phase
    s_data = bytearray(s_at + len(src_bytes) + SRC_PAD)
    s_data[s_at:s_at + len(src_bytes)] = src_bytes
    sr = Mem(SRC_BASE, s_data)
    src_addr = SRC_BASE + s_at
    fb = Mem(FB_BASE, fb0)
    before = dict(OPS)
    try:
        ops = ex15_store_window(fb, FB_BASE + WIN_AT, off, sr, src_addr, n)
    except IndexError as e:
        print("MODEL ERROR %s off=%d n=%d phase=%d: %s" % (label, off, n, src_phase, e))
        guard_fail += 1
        return 0
    got = bytes(fb.data)
    want = ref_store_window(fb0, WIN_AT + off, src_bytes, n)
    mismatch_here = 0
    for i in range(off, off + n):
        i2 = WIN_AT + i
        bytes_swept += 1
        if got[i2] != want[i2]:
            mismatched_bytes += 1
            mismatch_here += 1
            if first_mismatch is None:
                first_mismatch = "%s: window byte %d (off=%d n=%d phase=%d) want %02x got %02x" % (
                    label, i, off, n, src_phase, want[i2], got[i2])
    for i in list(range(off - 32, off)) + list(range(off + n, off + n + 32)):
        i2 = WIN_AT + i
        if 0 <= i2 < FB_LEN and got[i2] != fb0[i2]:
            guard_fail += 1
            if first_mismatch is None:
                first_mismatch = "%s/guard byte %d (off=%d n=%d phase=%d) want %02x got %02x" % (
                    label, i, off, n, src_phase, fb0[i2], got[i2])
            break
    if sr.reads:
        lo = min(a for a, _ in sr.reads)
        hi = max(a + CHUNK for a, _ in sr.reads)
        pre, post = src_addr - lo, hi - (src_addr + n)
        if pre > pre_read:
            pre_read, foot_pre = pre, {"off": off, "n": n, "phase": src_phase, "pre": pre, "post": post}
        if post > post_read:
            post_read, foot_post = post, {"off": off, "n": n, "phase": src_phase, "pre": pre, "post": post}
    for op in ops:
        phase_use[op[0]] = phase_use.get(op[0], 0) + 1
    cls = classify(off, n)
    class_count[cls] = class_count.get(cls, 0) + 1
    ops_by_class.setdefault(cls, []).append(sum(OPS[k] - before.get(k, 0) for k in OPS))
    if n <= 512:
        lo = max(0, WIN_AT + off - 64)
        hi = min(FB_LEN, WIN_AT + off + n + 64)
        DUMP.write("CASE %d %d %d %d\n" % (off, n, src_phase, lo - WIN_AT))
        DUMP.write("BEFORE %s\n" % bytes(fb0[lo:hi]).hex())
        DUMP.write("SRC %s\n" % src_bytes[:n].hex())
        DUMP.write("AFTER %s\n" % got[lo:hi].hex())
    return mismatch_here


t = {k: [0, 0] for k in ("T1", "T2", "T3", "T4", "T5", "T6")}

for n in (0, 1, 15, 16, 17, 240 * 2):                       # T1
    for off in range(16):
        for ph in range(16):
            t["T1"][0] += 1
            t["T1"][1] += 1 if run_case(off, n, ph, "T1 len=%d" % n) else 0

for off in (17, 31, 51, 133, 255, 271):                     # T2
    for n in (33, 64, 200, 480):
        for ph in (0, 1, 7, 8, 15):
            t["T2"][0] += 1
            t["T2"][1] += 1 if run_case(off, n, ph, "T2 off=%d" % off) else 0

for _ in range(400):                                        # T3
    off = random.randrange(0, 600)
    n = random.choice([0, 1, 2, 3, 14, 15, 16, 17, 18, 31, 32, 33, 45, 46, 47, 48, 63, 64, 65,
                       random.randrange(0, 900)])
    t["T3"][0] += 1
    t["T3"][1] += 1 if run_case(off, n, random.randrange(16), "T3 off=%d" % off) else 0

FRAME = 135 * 240 * 2                                       # T4: a whole frame, 4050 chunks exactly
for off, ph in ((0, 0), (270, 0), (270, 1), (270, 15), (135 * 2, 3)):
    t["T4"][0] += 1
    t["T4"][1] += 1 if run_case(off, FRAME, ph, "T4 frame") else 0

for n_px in (0, 8, 16, 136, 240, 1352):                     # T5
    s_data = bytearray(n_px * 2 + 32)
    for i in range(len(s_data)):
        s_data[i] = (i * 29 + 11) & 0xFF
    srcm = Mem(0x3FCB0000, s_data)
    fb_data = bytearray(64 + n_px * 2 + 64)
    for i in range(len(fb_data)):
        fb_data[i] = (i * 3 + 0x40) & 0xFF
    fbm = Mem(0x3FCC0000, fb_data)
    ex15_copy_row(fbm, srcm, n_px)
    t["T5"][0] += 1
    if bytes(fbm.data[0:n_px * 2]) != bytes(srcm.data[:n_px * 2]):
        t["T5"][1] += 1
    if bytes(fbm.data[n_px * 2:]) != bytes(fb_data[n_px * 2:]):
        t["T5"][1] += 1                                     # chunk-exact: nothing past the row moves
    color = 0xF81F if n_px % 2 else 0x07E0
    fb2_data = bytearray(64 + n_px * 2 + 64)
    for i in range(len(fb2_data)):
        fb2_data[i] = (i * 5 + 7) & 0xFF
    fb2 = Mem(0x3FCD0000, fb2_data)
    ex15_fill_row(fb2, color, n_px)
    t["T5"][0] += 1
    if bytes(fb2.data[0:n_px * 2]) != ref_fill_row(n_px, color):
        t["T5"][1] += 1
    if bytes(fb2.data[n_px * 2:]) != bytes(fb2_data[n_px * 2:]):
        t["T5"][1] += 1

mask_rows = []                                              # T6
zero, ones = zero_q(), notq(zero_q())
for b in range(16):
    m = mask_from_phase(zero, ones, 16 - b) if b >= 1 else None
    inv = mask_from_phase(ones, zero, 16 - b) if b >= 1 else None
    want = (b"\x00" * b) + (b"\xff" * (16 - b))              # "bytes b..15 set"
    ok = (b == 0) or (m == want and inv == notq(want) and inv != m)
    mask_rows.append((b, ok, (m.hex() if m else "-")))
    t["T6"][0] += 1
    t["T6"][1] += 0 if ok else 1
srci_row = srci_2q(ones, zero, 7)[0]        # sar16 = 7 -> shift = (7+1)*8 = 64 -> boundary 8 (complement
                                            # orientation: src_q(ones, zero, s) is ~mask_from_phase(zero,ones,16-s))
srci_ok = srci_row == notq(mask_from_phase(zero, ones, 16 - 8))
t["T6"][0] += 1
t["T6"][1] += 0 if srci_ok else 1

DUMP.close()
dumped = sum(1 for _ in open("/tmp/ex15_cases.txt")) // 4

print("ex15_fbstore model -- instruction semantics from data/pie_instructions.json Operation pseudo-code")
print()
print("T6 the mask builder the kernel uses: EE.SRC.Q, with the shift coming from a load address")
print("       EE.SRC.Q qa, qs0=zero, qs1=ones, SAR_BYTE=16-b   ->   bytes b..15 set")
for b, ok, m in mask_rows:
    print("   b=%2d (shift %2d)  mask = %s  %s" % (b, 16 - b, m, "ok" if ok else "MISMATCH"))
print("   all-ones register from one EE.ZERO.Q + one EE.NOTQ: ok")
print("   the same instruction with the operands swapped gives the complement (checked): it is NOT the mask")
print("   a read-modify-write wants; the first pass of this file had it that way, and T1 went red")
print("   EE.SRCI.2Q (shift = (sar16+1)*8, sar16 a 4-bit IMMEDIATE) can only produce a compile-time boundary;")
print("   with sar16 = 7 it lands on 8, i.e. one of the sixteen cases: %s"
      % ("ok" if srci_ok else "FAIL"))
print()
for k in sorted(t):
    print("%s: %d cases, %d bad" % (k, t[k][0], t[k][1]))
print()
print("cases run            : %d" % cases)
print("window bytes swept   : %d" % bytes_swept)
print("mismatching bytes    : %d" % mismatched_bytes)
print("guard-byte violations: %d   (32 bytes required unchanged before and after the window)" % guard_fail)
print("first mismatch       : %s" % (first_mismatch if first_mismatch else "none"))
print("read footprint       : worst %d bytes before src[0]  %s" % (pre_read, foot_pre))
print("                       worst %d bytes past src[n_bytes-1] (exclusive)  %s" % (post_read, foot_post))
print()
print("contract cases exercised (so no case below is skipped by the sweep):")
for cls in sorted(class_count):
    o = ops_by_class[cls]
    print("   %-58s %5d cases  %4d..%-5d PIE instructions" % (cls, class_count[cls], min(o), max(o)))
print()
print("layer-1 instruction calls over the whole sweep (the cost table's source):")
for k in sorted(OPS):
    print("   %-22s %d" % (k, OPS[k]))
print()
print("case dump for the C verifier: /tmp/ex15_cases.txt (%d cases with n_bytes <= 512)" % dumped)
print()
allbad = sum(v[1] for v in t.values()) + mismatched_bytes + guard_fail
print("ALL CHECKS:", "PASS" if allbad == 0 else "FAIL", "(bad=%d)" % allbad)

```

実行結果（そのまま貼ります）:

```
$ .venv/bin/python /tmp/ex15_model.py
ex15_fbstore model -- instruction semantics from data/pie_instructions.json Operation pseudo-code

T6 the mask builder the kernel uses: EE.SRC.Q, with the shift coming from a load address
       EE.SRC.Q qa, qs0=zero, qs1=ones, SAR_BYTE=16-b   ->   bytes b..15 set
   b= 0 (shift 16)  mask = -  ok
   b= 1 (shift 15)  mask = 00ffffffffffffffffffffffffffffff  ok
   b= 2 (shift 14)  mask = 0000ffffffffffffffffffffffffffff  ok
   b= 3 (shift 13)  mask = 000000ffffffffffffffffffffffffff  ok
   b= 4 (shift 12)  mask = 00000000ffffffffffffffffffffffff  ok
   b= 5 (shift 11)  mask = 0000000000ffffffffffffffffffffff  ok
   b= 6 (shift 10)  mask = 000000000000ffffffffffffffffffff  ok
   b= 7 (shift  9)  mask = 00000000000000ffffffffffffffffff  ok
   b= 8 (shift  8)  mask = 0000000000000000ffffffffffffffff  ok
   b= 9 (shift  7)  mask = 000000000000000000ffffffffffffff  ok
   b=10 (shift  6)  mask = 00000000000000000000ffffffffffff  ok
   b=11 (shift  5)  mask = 0000000000000000000000ffffffffff  ok
   b=12 (shift  4)  mask = 000000000000000000000000ffffffff  ok
   b=13 (shift  3)  mask = 00000000000000000000000000ffffff  ok
   b=14 (shift  2)  mask = 0000000000000000000000000000ffff  ok
   b=15 (shift  1)  mask = 000000000000000000000000000000ff  ok
   all-ones register from one EE.ZERO.Q + one EE.NOTQ: ok
   the same instruction with the operands swapped gives the complement (checked): it is NOT the mask
   a read-modify-write wants; the first pass of this file had it that way, and T1 went red
   EE.SRCI.2Q (shift = (sar16+1)*8, sar16 a 4-bit IMMEDIATE) can only produce a compile-time boundary;
   with sar16 = 7 it lands on 8, i.e. one of the sixteen cases: ok

T1: 1536 cases, 0 bad
T2: 120 cases, 0 bad
T3: 400 cases, 0 bad
T4: 5 cases, 0 bad
T5: 12 cases, 0 bad
T6: 17 cases, 0 bad

cases run            : 2061
window bytes swept   : 504060
mismatching bytes    : 0
guard-byte violations: 0   (32 bytes required unchanged before and after the window)
first mismatch       : none
read footprint       : worst 30 bytes before src[0]  {'off': 15, 'n': 1, 'phase': 14, 'pre': 30, 'post': 1}
                       worst 32 bytes past src[n_bytes-1] (exclusive)  {'off': 0, 'n': 480, 'phase': 0, 'pre': 0, 'post': 32}

contract cases exercised (so no case below is skipped by the sweep):
   16-byte aligned destination, partial tail chunk               65 cases    13..107   PIE instructions
   16-byte aligned destination, whole chunks, no partial chunk    38 cases     4..12152 PIE instructions
   2-byte aligned offset (pixel-exact)                          450 cases    17..12174 PIE instructions
   empty (n_bytes == 0), no access at all                       266 cases     0..0     PIE instructions
   less than 16 bytes remaining (whole window inside the head chunk)   291 cases    17..17    PIE instructions
   less than 16 bytes, spanning two chunks                      289 cases    13..26    PIE instructions
   odd offset (pixel-straddling)                                662 cases    17..180   PIE instructions

layer-1 instruction calls over the whole sweep (the cost table's source):
   EE.ANDQ                6431
   EE.LD.128.USAR.IP      43493
   EE.MOVI.32.Q           24
   EE.NOTQ                6448
   EE.ORQ                 3070
   EE.SRC.Q               36576
   EE.SRCI.2Q             1
   EE.SRCQ.128.ST.INCP    30114
   EE.VLD.128.IP          223
   EE.VST.128.IP          3508
   EE.ZERO.Q              3078

case dump for the C verifier: /tmp/ex15_cases.txt (2048 cases with n_bytes <= 512)

ALL CHECKS: PASS (bad=0)
```

## 6. ホスト C 検証器（同じケースダンプを memcpy で再計算）

モデルは 1 ケースごとに「呼ぶ前のフレームバッファ領域・ソースバイト列・呼んだ後の領域」を
`/tmp/ex15_cases.txt` に落とします（`n_bytes <= 512` の 2048 ケース）。C 側はそれを読み、
**契約そのもの**（バイト貼り付け）を `memcpy` で作り直して比較します。カーネルの 2 つ目の実装では
ありません。

```c
/* ex15_fbstore -- the host-side (gcc) half of the equivalence run.
 *
 * Reads the case dump written by the Python model (/tmp/ex15_cases.txt) and re-derives every case the
 * memcpy way: build the framebuffer region from the dumped BEFORE bytes, memcpy the source window into it
 * at byte offset 64 of the region (the region starts 64 bytes before the window), and compare against the
 * bytes the model's kernel produced. Guard bytes: the 64 bytes before and the 64 bytes after the window
 * must be byte-identical before and after -- the check that fails for a 128-bit store which silently wrote
 * its rounded-down 16-byte chunk.
 *
 * This is deliberately NOT a second copy of the kernel: it is the contract (a byte paste) and nothing else.
 *
 *   gcc -O2 -o /tmp/ex15_verify /tmp/ex15_verify.c && /tmp/ex15_verify /tmp/ex15_cases.txt
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* hex text starting at `skip` characters into the line -> bytes; returns the byte count, or -1 on bad hex */
static int hex2bin(const char *line, size_t skip, unsigned char *out, size_t max)
{
    const char *p = line + skip;
    size_t len = strcspn(p, "\r\n"), i;
    if (len == 0) return 0;                        /* an empty window: no source bytes at all */
    if (len % 2 || len / 2 > max) return -1;
    for (i = 0; i < len / 2; i++) {
        unsigned v;
        if (sscanf(p + 2 * i, "%2x", &v) != 1) return -1;
        out[i] = (unsigned char)v;
    }
    return (int)(len / 2);
}

static char line[8192];

int main(int argc, char **argv)
{
    FILE *f = fopen(argc > 1 ? argv[1] : "/tmp/ex15_cases.txt", "r");
    if (!f) { perror("open"); return 2; }
    static unsigned char before[8192], src[8192], after[8192], ref[8192];
    long cases = 0, bad_bytes = 0, bad_guard = 0;
    long bytes_checked = 0, guards_checked = 0, bytes_in_windows = 0;
    long first_off = -1, first_n = -1, first_phase = -1;

    while (fgets(line, sizeof line, f)) {
        long off, n, phase, pre;
        int nb, ns, na;
        if (sscanf(line, "CASE %ld %ld %ld %ld", &off, &n, &phase, &pre) != 4) {
            fprintf(stderr, "bad CASE line: %s", line);
            return 3;
        }
        if (!fgets(line, sizeof line, f) || (nb = hex2bin(line, 7, before, sizeof before)) < 0) return 3;
        if (!fgets(line, sizeof line, f) || (ns = hex2bin(line, 4, src,    sizeof src))    < 0) return 3;
        if (!fgets(line, sizeof line, f) || (na = hex2bin(line, 6, after,  sizeof after))  < 0) return 3;
        if (ns != n || nb != na || nb < n) { fprintf(stderr, "size mismatch: off=%ld n=%ld\n", off, n); return 3; }

        memcpy(ref, before, (size_t)nb);
        memcpy(ref + 64, src, (size_t)n);          /* the contract: a byte paste at offset 64 of the region */
        for (long i = 0; i < nb; i++) {
            bytes_checked++;
            if (i >= 64 && i < 64 + n) { bytes_in_windows++; continue; }
            guards_checked++;
            if (ref[i] != after[i]) {
                bad_guard++;
                if (first_off < 0) { first_off = off; first_n = n; first_phase = phase; }
            }
        }
        if (memcmp(ref, after, (size_t)nb) != 0) {
            for (long i = 0; i < nb; i++) if (ref[i] != after[i]) bad_bytes++;
            if (first_off < 0) { first_off = off; first_n = n; first_phase = phase; }
        }
        cases++;
    }
    fclose(f);

    printf("C verifier -- memcpy reference, host gcc -O2\n");
    printf("cases read          : %ld\n", cases);
    printf("region bytes checked: %ld  (guard bytes outside the window: %ld, window bytes: %ld)\n",
           bytes_checked, guards_checked, bytes_in_windows);
    printf("mismatching bytes   : %ld\n", bad_bytes);
    printf("guard-byte failures : %ld\n", bad_guard);
    printf("first mismatch      : %s\n", first_off < 0 ? "none"
           : "off / n / phase / (counts above)");
    printf("VERDICT             : %s\n", (bad_bytes == 0 && bad_guard == 0 && cases > 0) ? "PASS" : "FAIL");
    return (bad_bytes == 0 && bad_guard == 0 && cases > 0) ? 0 : 1;
}

```

```c
$ gcc -O2 -o /tmp/ex15_verify /tmp/ex15_verify.c && /tmp/ex15_verify /tmp/ex15_cases.txt
C verifier -- memcpy reference, host gcc -O2
cases read          : 2048
region bytes checked: 436105  (guard bytes outside the window: 262144, window bytes: 173961)
mismatching bytes   : 0
guard-byte failures : 0
first mismatch      : none
VERDICT             : PASS
```

## 7. アセンブラの証拠（実機は使っていない）

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex15.o examples/firmware/main/proposed/ex15_fbstore.S
(出力なし・終了ステータス 0)

$ xtensa-esp32s3-elf-nm -S /tmp/ex15.o
00000000 000000e2 T ex15_store_window
000000e4 0000002c T ex15_copy_row
00000110 0000002a T ex15_fill_row

$ xtensa-esp32s3-elf-objdump -d /tmp/ex15.o

/tmp/ex15.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex15_store_window>:
   0:	004136        	entry	a1, 32
   3:	0d9516        	beqz	a5, e0 <ex15_store_window+0xe0>
   6:	623a      	add.n	a6, a2, a3
   8:	d65a      	add.n	a13, a6, a5
   a:	347030        	extui	a7, a3, 0, 4
   d:	053d      	mov.n	a3, a5
   f:	06bd      	mov.n	a11, a6
  11:	04cd      	mov.n	a12, a4
  13:	061716        	beqz	a7, 78 <ex15_store_window+0x78>
  16:	081c      	movi.n	a8, 16
  18:	c08870        	sub	a8, a8, a7
  1b:	069d      	mov.n	a9, a6
  1d:	c09970        	sub	a9, a9, a7
  20:	20e880        	or	a14, a8, a8
  23:	02b587        	bgeu	a5, a8, 29 <ex15_store_window+0x29>
  26:	20e550        	or	a14, a5, a5
  29:	c0a470        	sub	a10, a4, a7
  2c:	a180a4        	ee.ld.128.usar.ip	q5, a10, 0
  2f:	cd7fa4        	ee.zero.q	q0
  32:	10caf2        	addi	a15, a10, 16
  35:	b100f4        	ee.ld.128.usar.ip	q6, a15, 0
  38:	dd7f04        	ee.notq	q2, q0
  3b:	fc5374        	ee.src.q	q7, q5, q6
  3e:	a18064        	ee.ld.128.usar.ip	q5, a6, 0
  41:	f98a      	add.n	a15, a9, a8
  43:	9180f4        	ee.ld.128.usar.ip	q3, a15, 0
  46:	dc0344        	ee.src.q	q4, q0, q2
  49:	10b587        	bgeu	a5, a8, 5d <ex15_store_window+0x5d>
  4c:	c0f8e0        	sub	a15, a8, a14
  4f:	f9fa      	add.n	a15, a9, a15
  51:	9180f4        	ee.ld.128.usar.ip	q3, a15, 0
  54:	dc0314        	ee.src.q	q1, q0, q2
  57:	ddff14        	ee.notq	q3, q1
  5a:	ed34a4        	ee.andq	q4, q4, q3
  5d:	ddff84        	ee.notq	q3, q4
  60:	edb4b4        	ee.andq	q5, q5, q3
  63:	ed38d4        	ee.andq	q4, q7, q4
  66:	edf894        	ee.orq	q5, q5, q4
  69:	aa8094        	ee.vst.128.ip	q5, a9, 0
  6c:	c055e0        	sub	a5, a5, a14
  6f:	06d516        	beqz	a5, e0 <ex15_store_window+0xe0>
  72:	10c9b2        	addi	a11, a9, 16
  75:	80c4e0        	add	a12, a4, a14
  78:	41f450        	srli	a15, a5, 4
  7b:	2fac      	beqz.n	a15, a1 <ex15_store_window+0xa1>
  7d:	4191f0        	srli	a9, a15, 1
  80:	8101c4        	ee.ld.128.usar.ip	q0, a12, 16
  83:	8181c4        	ee.ld.128.usar.ip	q1, a12, 16
  86:	f98c      	beqz.n	a9, 99 <ex15_store_window+0x99>
  88:	cc8eb4        	ee.srcq.128.st.incp	q0, q1, a11
  8b:	8101c4        	ee.ld.128.usar.ip	q0, a12, 16
  8e:	990b      	addi.n	a9, a9, -1
  90:	cc1eb4        	ee.srcq.128.st.incp	q1, q0, a11
  93:	8181c4        	ee.ld.128.usar.ip	q1, a12, 16
  96:	fee956        	bnez	a9, 88 <ex15_store_window+0x88>
  99:	0490f0        	extui	a9, a15, 0, 1
  9c:	198c      	beqz.n	a9, a1 <ex15_store_window+0xa1>
  9e:	cc8eb4        	ee.srcq.128.st.incp	q0, q1, a11
  a1:	34e0d0        	extui	a14, a13, 0, 4
  a4:	8ebc      	beqz.n	a14, e0 <ex15_store_window+0xe0>
  a6:	a180d4        	ee.ld.128.usar.ip	q5, a13, 0
  a9:	cd7fa4        	ee.zero.q	q0
  ac:	c09de0        	sub	a9, a13, a14
  af:	dd7f04        	ee.notq	q2, q0
  b2:	0f1c      	movi.n	a15, 16
  b4:	c0ffe0        	sub	a15, a15, a14
  b7:	f9fa      	add.n	a15, a9, a15
  b9:	8180f4        	ee.ld.128.usar.ip	q1, a15, 0
  bc:	dc0334        	ee.src.q	q3, q0, q2
  bf:	ed7f54        	ee.notq	q4, q3
  c2:	c0a3e0        	sub	a10, a3, a14
  c5:	80a4a0        	add	a10, a4, a10
  c8:	b100a4        	ee.ld.128.usar.ip	q6, a10, 0
  cb:	10caf2        	addi	a15, a10, 16
  ce:	b180f4        	ee.ld.128.usar.ip	q7, a15, 0
  d1:	fce314        	ee.src.q	q1, q6, q7
  d4:	edb4b4        	ee.andq	q5, q5, q3
  d7:	cdb814        	ee.andq	q1, q1, q4
  da:	edf0b4        	ee.orq	q5, q5, q1
  dd:	aa8094        	ee.vst.128.ip	q5, a9, 0
  e0:	f01d      	retw.n
	...

000000e4 <ex15_copy_row>:
  e4:	004136        	entry	a1, 32
  e7:	414340        	srli	a4, a4, 3
  ea:	046040        	extui	a6, a4, 0, 1
  ed:	415140        	srli	a5, a4, 1
  f0:	468c      	beqz.n	a6, f8 <ex15_copy_row+0x14>
  f2:	830134        	ee.vld.128.ip	q0, a3, 16
  f5:	8a0124        	ee.vst.128.ip	q0, a2, 16
  f8:	259c      	beqz.n	a5, 10e <ex15_copy_row+0x2a>
  fa:	830134        	ee.vld.128.ip	q0, a3, 16
  fd:	838134        	ee.vld.128.ip	q1, a3, 16
 100:	8a0124        	ee.vst.128.ip	q0, a2, 16
 103:	830134        	ee.vld.128.ip	q0, a3, 16
 106:	8a8124        	ee.vst.128.ip	q1, a2, 16
 109:	550b      	addi.n	a5, a5, -1
 10b:	fee556        	bnez	a5, fd <ex15_copy_row+0x19>
 10e:	f01d      	retw.n

00000110 <ex15_fill_row>:
 110:	004136        	entry	a1, 32
 113:	f45030        	extui	a5, a3, 0, 16
 116:	116500        	slli	a6, a5, 16
 119:	205560        	or	a5, a5, a6
 11c:	cd7fa4        	ee.zero.q	q0
 11f:	cd3254        	ee.movi.32.q	q0, a5, 0
 122:	cd3654        	ee.movi.32.q	q0, a5, 1
 125:	cd3a54        	ee.movi.32.q	q0, a5, 2
 128:	cd3e54        	ee.movi.32.q	q0, a5, 3
 12b:	414340        	srli	a4, a4, 3
 12e:	648c      	beqz.n	a4, 138 <ex15_fill_row+0x28>
 130:	8a0124        	ee.vst.128.ip	q0, a2, 16
 133:	440b      	addi.n	a4, a4, -1
 135:	ff7456        	bnez	a4, 130 <ex15_fill_row+0x20>
 138:	f01d      	retw.n
```

`EE.*` の語（37 種類）を `data/pie_instructions.json` の命令語図でフィールド単位にデコードした結果。
図の分割フィールド（`qu[2:1]` + `qu[0]`）は 1 つの数に戻し、図と一致した定数フィールドは表示していません
（一致しなければ `CONST-MISMATCH` と出ます）:

```
$ .venv/bin/python /tmp/ex15_words.py /tmp/ex15.objdump.txt
$ xtensa-esp32s3-elf-objdump -d /tmp/ex15.o     (only the EE.* words are shown here)

mnemonic                 operands as written    word      TRM    fields decoded from the word
ee.andq                  q4, q4, q3             ed34a4   p76    qa=4 qx=4 qy=3   [x1]
ee.andq                  q5, q5, q3             edb4b4   p76    qa=5 qx=5 qy=3   [x2]
ee.andq                  q4, q7, q4             ed38d4   p76    qa=4 qx=7 qy=4   [x1]
ee.andq                  q1, q1, q4             cdb814   p76    qa=1 qx=1 qy=4   [x1]
ee.ld.128.usar.ip        q5, a10, 0             a180a4   p93    as=10 imm16=0 qu=5   [x1]
ee.ld.128.usar.ip        q6, a15, 0             b100f4   p93    as=15 imm16=0 qu=6   [x1]
ee.ld.128.usar.ip        q5, a6, 0              a18064   p93    as=6 imm16=0 qu=5   [x1]
ee.ld.128.usar.ip        q3, a15, 0             9180f4   p93    as=15 imm16=0 qu=3   [x2]
ee.ld.128.usar.ip        q0, a12, 16            8101c4   p93    as=12 imm16=1 qu=0   [x2]
ee.ld.128.usar.ip        q1, a12, 16            8181c4   p93    as=12 imm16=1 qu=1   [x2]
ee.ld.128.usar.ip        q5, a13, 0             a180d4   p93    as=13 imm16=0 qu=5   [x1]
ee.ld.128.usar.ip        q1, a15, 0             8180f4   p93    as=15 imm16=0 qu=1   [x1]
ee.ld.128.usar.ip        q6, a10, 0             b100a4   p93    as=10 imm16=0 qu=6   [x1]
ee.ld.128.usar.ip        q7, a15, 0             b180f4   p93    as=15 imm16=0 qu=7   [x1]
ee.movi.32.q             q0, a5, 0              cd3254   p119   as=5 qu=0 sel4=0   [x1]
ee.movi.32.q             q0, a5, 1              cd3654   p119   as=5 qu=0 sel4=1   [x1]
ee.movi.32.q             q0, a5, 2              cd3a54   p119   as=5 qu=0 sel4=2   [x1]
ee.movi.32.q             q0, a5, 3              cd3e54   p119   as=5 qu=0 sel4=3   [x1]
ee.notq                  q2, q0                 dd7f04   p120   qa=2 qx=0   [x2]
ee.notq                  q3, q1                 ddff14   p120   qa=3 qx=1   [x1]
ee.notq                  q3, q4                 ddff84   p120   qa=3 qx=4   [x1]
ee.notq                  q4, q3                 ed7f54   p120   qa=4 qx=3   [x1]
ee.orq                   q5, q5, q4             edf894   p121   qa=5 qx=5 qy=4   [x1]
ee.orq                   q5, q5, q1             edf0b4   p121   qa=5 qx=5 qy=1   [x1]
ee.src.q                 q7, q5, q6             fc5374   p125   qa=7 qs0=5 qs1=6   [x1]
ee.src.q                 q4, q0, q2             dc0344   p125   qa=4 qs0=0 qs1=2   [x1]
ee.src.q                 q1, q0, q2             dc0314   p125   qa=1 qs0=0 qs1=2   [x1]
ee.src.q                 q3, q0, q2             dc0334   p125   qa=3 qs0=0 qs1=2   [x1]
ee.src.q                 q1, q6, q7             fce314   p125   qa=1 qs0=6 qs1=7   [x1]
ee.srcq.128.st.incp      q0, q1, a11            cc8eb4   p132   as=11 qs0=0 qs1=1   [x2]
ee.srcq.128.st.incp      q1, q0, a11            cc1eb4   p132   as=11 qs0=1 qs1=0   [x1]
ee.vld.128.ip            q0, a3, 16             830134   p164   as=3 imm16=1 qu=0   [x3]
ee.vld.128.ip            q1, a3, 16             838134   p164   as=3 imm16=1 qu=1   [x1]
ee.vst.128.ip            q5, a9, 0              aa8094   p275   as=9 imm16=0 qv=5   [x2]
ee.vst.128.ip            q0, a2, 16             8a0124   p275   as=2 imm16=1 qv=0   [x3]
ee.vst.128.ip            q1, a2, 16             8a8124   p275   as=2 imm16=1 qv=1   [x1]
ee.zero.q                q0                     cd7fa4   p299   qa=0   [x3]

distinct PIE words in the object file: 37

For the two SAR_BYTE-carrying forms the as= field above IS the register SAR_BYTE is taken from:
   ee.ld.128.usar.ip        q0, a12, 16            as=12 imm16=1 qu=0
   ee.ld.128.usar.ip        q1, a12, 16            as=12 imm16=1 qu=1
   ee.ld.128.usar.ip        q1, a15, 0             as=15 imm16=0 qu=1
   ee.ld.128.usar.ip        q3, a15, 0             as=15 imm16=0 qu=3
   ee.ld.128.usar.ip        q5, a10, 0             as=10 imm16=0 qu=5
   ee.ld.128.usar.ip        q5, a13, 0             as=13 imm16=0 qu=5
   ee.ld.128.usar.ip        q5, a6, 0              as=6 imm16=0 qu=5
   ee.ld.128.usar.ip        q6, a10, 0             as=10 imm16=0 qu=6
   ee.ld.128.usar.ip        q6, a15, 0             as=15 imm16=0 qu=6
   ee.ld.128.usar.ip        q7, a15, 0             as=15 imm16=0 qu=7
   ee.src.q                 q1, q0, q2             qa=1 qs0=0 qs1=2
   ee.src.q                 q1, q6, q7             qa=1 qs0=6 qs1=7
   ee.src.q                 q3, q0, q2             qa=3 qs0=0 qs1=2
   ee.src.q                 q4, q0, q2             qa=4 qs0=0 qs1=2
   ee.src.q                 q7, q5, q6             qa=7 qs0=5 qs1=6
   ee.srcq.128.st.incp      q0, q1, a11            as=11 qs0=0 qs1=1
   ee.srcq.128.st.incp      q1, q0, a11            as=11 qs0=1 qs1=0
```

`as=` の欄が `EE.LD.128.USAR.IP` の `SAR_BYTE` の出どころです（TRM p93 の `SAR_BYTE = as[3:0]`）。
`q0, a12, 16` が `as=12 imm16=1` であることが「印刷した即値 16 がそのまま 16 バイトの歩幅」の確認で、
これは実機で確定済みです（`data/pie_examples_measured.json` の `post_increment_step_accx_forms`:
`printed_immediate_16.ld128_usar = 16`、status `confirmed`）。`ee.srcq.128.st.incp q0, q1, a11` が
`qs0=0 qs1=1`、`q1, q0, a11` が `qs0=1 qs1=0` であることが、交互ペアのストアで下位/上位が入れ替わる
（= 窓が 16 バイトずつ進む）ことの符号化側の確認です。

## 8. 使った命令と TRM のページ（`data/pie_instructions.json` より）

| 命令 | source_page | 構文 | Operation（抜粋） |
|---|---|---|---|
| `EE.LD.128.USAR.IP` | 93 | `EE.LD.128.USAR.IP qu, as, -2048..2032` | 1 qu[127:0] = load128({as[31:4],4{0}}) 2 SAR_BYTE = as[3:0] 3 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4 |
| `EE.SRC.Q` | 125 | `EE.SRC.Q qa, qs0, qs1` | 1 qa[127: 0] = {qs1[127: 0], qs0[127: 0]} >> {SAR_BYTE[3:0] << 3} |
| `EE.SRCQ.128.ST.INCP` | 132 | `EE.SRCQ.128.ST.INCP qs0, qs1, as` | 1 {qs1[127: 0], qs0[127: 0]} >> {SAR_BYTE[3:0] << 3} => store128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + 16 |
| `EE.VLD.128.IP` | 164 | `EE.VLD.128.IP qu, as, -2048..2032` | 1 qu[127:0] = load128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} |
| `EE.VST.128.IP` | 275 | `EE.VST.128.IP qv, as, -2048..2032` | 1 qv[127:0] => store128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} |
| `EE.ZERO.Q` | 299 | `EE.ZERO.Q qa` | 1 qa = 0 |
| `EE.NOTQ` | 120 | `EE.NOTQ qa, qx` | 1 qa = ~qx |
| `EE.ANDQ` | 76 | `EE.ANDQ qa, qx, qy` | 1 qa = qx & qy |
| `EE.ORQ` | 121 | `EE.ORQ qa, qx, qy` | 1 qa = qx | qy |
| `EE.MOVI.32.Q` | 119 | `EE.MOVI.32.Q qu, as, 0..3` | 1 if sel4 == 0: 2 qu[ 31: 0] = as 3 if sel4 == 1: 4 qu[ 63: 32] = as 5 if sel4 == 2: 6 qu[ 95: 64] = as 7 if s |

`extui` / `srli` / `sub` / `add` / `bgeu` / `bltu` / `beqz` / `bnez` / `retw.n` / `entry` は PIE ではなく
ベース ISA なので `data/pie_instructions.json` に `source_page` はありません（このファイルは PIE 章の
220 命令のリストです）。スケジュールの根拠にしたストール表は `data/pie_hazards.md`（TRM 1.7、印刷ページ
65–75、逐語）で、本ファイルが使う行は:

```
EE.LD.128.USAR.IP   | as 1 | qu 2, as 1 | —            | SAR_BYTE 1
EE.SRC.Q            | qs0 1, qs1 1 | qa 1 | SAR_BYTE 1 | —
EE.SRCQ.128.ST.INCP | qs0 1, qs1 1, as 1 | as 1 | SAR_BYTE 1 | —
EE.VLD.128.IP       | as 1 | qu 2, as 1 | —            | —
EE.VST.128.IP       | qv 1, as 1 | as 1 | —            | —
```

この表から出る 2 つの帰結が .S の命令順を決めています。(1) **`SAR_BYTE` は M/E どちらも 1 段**なので、
それを書く `USAR.IP` と読む `EE.SRC.Q` / `SRCQ.128.ST.INCP` は **1 スロット以上離す**必要がある（距離 1 で
あれば満たす）。(2) **`qu` の def は 2 (M)、`qv`/`qs` の use は 1 (E)** なので、ロード直後のストアは
**距離 2 以上**必要 — 実測アンカー（`data/pie_timing_measured.json` の `anchor_qs_M_to_E` = 距離 1 で
1.000 サイクル、距離 2 で 0.000）どおりです。中間ループが `SRCQ → LD → addi → SRCQ → LD → bnez` の
6 命令/2 チャンクなのはこのためで、ロードとその消費者が必ず 2 スロット離れます。

## 9. リポジトリ内の文書が食い違っている箇所（と、既に実機が決めていること）

1. **`EE.SRC.Q.LD.IP` は `data/pie_encoding_errata.json` で `layout_not_machine_readable`** です
   （命令語図が純粋なフィールド列になっていないので符号化を導けない）。つまり**漏斗ロードの融合形だけは
   このリポジトリのデータでは検証できません**。本カーネルが `EE.LD.128.USAR.IP` + `EE.SRC.Q` の 2 命令に
   分けているのはこの理由もあって、この 2 つ（と ストア側の `SRCQ.128.ST.INCP`）は図がきれいに読めます
   （上の節で実際に読んでいます）。同じ理由で使えない命令: `EE.VMULAS.S8.QACC.LD.IP`。
2. **`EE.VMULAS.S8.QACC.LD.IP` の errata エントリは、その `unsupported_tokens` の中に TRM p49 の規則を
   そのまま引用しています**（"During the operation, the lower 4 bits of the access address in register as
   are forced to be 0..."）。つまり「下位 4 ビットを落とす」規則は、リポジトリの別々の抽出（命令ページと
   図のテキスト）で一致していて、ex03 が実機で再現済みです。ここは食い違いではなく**三重の一致**で、
   RMW が要る根拠になっています。
3. **128 ビット形の後置インクリメントの刻み**は `data/pie_encoding_errata.json` の
   `EE.ST.ACCX.IP`（構文行 `/512..508` = 4 刻み、Operation `as += imm8` = 1 刻み、アセンブラ = 8 刻み）で
   文書が 3 通りに割れていましたが、ex01 が生バイトで決着させ、`data/pie_examples_measured.json` の
   `post_increment_step_accx_forms` が **`EE.LD.128.USAR.IP` を含めて「印刷した即値がそのまま歩幅」**と
   確定しています（`printed_immediate_16.ld128_usar = 16`）。本カーネルの中間ループはこの結論に依存して
   います（`USAR.IP ..., 16` が 16 バイト歩く）。逆に **`EE.SRCQ.128.ST.INCP` 自身の `as += 16` は
   実機未確認**です（下の節）。
4. **QACC の読み出し間隔について、同シリーズのドラフトとデータが食い違っています**:
   `examples/docs/ex11_block8x8.md` は ex07/ex09 の QACC 結果を「実機では確定していない（4 スロットの間隔は
   このスイートの慣習であって測定ではない）」と書いている一方、`data/pie_examples_measured.json` の
   `qacc_accumulate_to_readout_needs_no_gap` は `status` フィールドを持たず（= confirmed ではない）
   「間隔 0 で一致する」と主張しています。ex15 は ACCX も QACC も使わないので、この不一致に依存しません
   （踏まないように選んでいる、というのが正確です）。

## 10. 実機で確かめていないこと（先に叩くべき順）

**最初に実機で叩くべきは `EE.SRCQ.128.ST.INCP` です。** 理由: (1) リポジトリ自身が未測定として名指しして
いる唯一の命令だから（`data/pie_examples_measured.json` の `open_after_this_run`:
"EE.LDQA/EE.STQA (the QACC memory forms) and EE.SRCQ.128.ST.INCP: the accumulate-into-memory traffic has
not been measured."）、(2) 中間ループ = フレームのバイト数のほぼ全部をこの命令が運ぶから、(3) 壊れ方が
**例外ではなく静かな誤り**（`SAR_BYTE` の解釈を 1 つ間違えると窓が 16 バイトずれた場所に書かれ、ガードバイト
検査だけが気づく）だからです。プローブは 3 つで足ります:

1. 同一 2 チャンク、`SAR_BYTE = 0` と `SAR_BYTE = 5` で `SRCQ.128.ST.INCP` を 1 回ずつ。ストアされた
   16 バイトが「`{qs1,qs0} >> 5*8` が `floor(as)` に書かれたもの」と一致するか（= シフト方向と、
   ストアアドレスが丸められることの確認）。
2. 同じ命令の後で `as` の値を読む（`mov` して戻り値に）。**+16 バイト**進んでいるか（+16 の刻みは
   未測定）。
3. 1 回の呼び出しで `n_bytes = 33`、`byte_off = 3` を流し、ウィンドウの 32 バイト手前と 32 バイト先の
   ガードを突き合わせる（RMW 2 つと中間 2 チャンクが同時に検証される）。

**未確認の意味論（全部）**:

- `EE.SRCQ.128.ST.INCP` の意味そのもの: `{qs1,qs0} >> (SAR_BYTE*8)` を `floor(as)` にストアし、
  `as += 16`。Operation 疑似コードからの読みで、**実機の証拠はゼロ**（未測定リストに明記）。
- `EE.SRC.Q` / `SRCQ.128.ST.INCP` が `SAR_BYTE` を「M/E のどの段で」読むか。Table 1.7-2 は両方 1 段と
  書いていますが、この表は `EE.VMULAS` のアキュムレータ書き込みを載せていないなど穴があるので、
  **距離 1 で足りるかは仮定**です（距離 1 で足りなければ 1 サイクル/16 B 遅くなるだけで、正しさは変わりません）。
- スケジュールの「発行スロット」は**サイクル数ではありません**。PIE のパイプライン挙動（`USAR.IP` の
  ロードレイテンシ、`SRCQ.128.ST.INCP` のストアスループット、IRAM からの発行が 1 サイクル 1 命令という
  前提）を測っていません。`BENCH` 行は存在しません（ex15 は `main.c` に入っていない）。
- `byte_off` に負の値や 2^31 以上を渡した場合の挙動（`int32_t` の契約は 0 以上のみ。`add` は 32 ビットで
  ラップするので、契約外の入力は静かに別の場所を書きます）。
- 「`k` 偶数なら画素を壊さない」という主張は**バイト単位の算術**であって、パネル側のタイミング
  （SPI 経由で 1 行の途中から送る場合の CASET/RASET の扱い）の確認ではありません。
- このファイルは 135×240 のフレームバッファについて何も前提にしていません（`dst`・`byte_off`・`src`・
  `n_bytes` だけの契約です）。行末のパディング（270 B が 16 の倍数でないこと）は**呼び出し側の仕事**です。
- モデルの読み出しフットプリント（手前 30 B、先 32 B）は**モデル内のアドレス範囲**の話で、実機のページ
  境界をまたがないことは保証していません。境界をまたぐバッファで呼ぶなら、呼び出し側が端を別扱いして
  ください（同じ注意はエスプレッソの漏斗コードにもあります）。
- **FreeRTOS のタスクスイッチで PIE の文脈（COP3 保存域）が保たれるか**は、このスイートの未解決事項の
  ままです（`open_after_this_run`）。本カーネルは文脈保存に依存しませんが、`SAR_BYTE` は特殊レジスタ
  なのでプリエンプションが入るなら確認が要ります。

## 11. この例題が確定させること（もし流せたら）

- 非整列の**ストア**経路が `EE.SRCQ.128.ST.INCP` だけで組めること（読み側は ex03 で確定済み）。
- 中間ループが **16 バイトあたり 2 メモリ操作**で回ること（C の -O2 版と比べた `BENCH` 付きで）。
- 先頭/末尾の RMW のコストが**バイト数に依存しない**こと（`k` を 1..15 で回して `cycles/call` が
  平坦であること）。これはモデルが予言していて、実機で測れる形の予言です。

## 12. 追加するときに触るファイル（今回は触っていない）

このファイルは `proposed/` にあり、`examples/firmware/main/CMakeLists.txt` の `SRCS` に入っていません。
本採用するなら、既存の例題と同じ手順（notes/08 の「サンプルの足し方」）で:

1. `examples.h` に 3 つの宣言（引数と戻りの意味をコメントで）。
2. `main.c` に `ex15()`: 決定論的入力 → カーネル → C 参照 → `CHECK` → `BENCH` → `DATA`（入力も出す）。
   行の例として、`n_bytes = 33 / byte_off = 3`、`byte_off = 8`（画素境界）、`byte_off = 0` の 3 ケースと、
   ガードバイト 32+32 の `DATA` 行。
3. `tools/check_examples_log.py` に `check_ex15()`（Python で三度目の計算 = このファイルのモデルと
   同じ参照実装）と `CHECKS` への登録。
4. `tools/selftest_examples_checker.py` の合成ログに `DATA` 行と、そのフィールドを壊す変異（例えば
   ガードバイトを 1 バイト変える）を追加。
5. `CMakeLists.txt` の `SRCS` に 1 行、notes/08 の表と `examples/README.md` の一覧を更新。

**注意（リポジトリの構成が作業中に動きました）**: 着手時は `examples/firmware/main/proposed/` に
ex10〜ex12 のドラフトが居ましたが、作業中に `ex10_motion.S`/`ex11_block8x8.S`/`ex12_physics.S` が
`examples/firmware/main/` へ、それぞれの `.md` が `examples/docs/` へ移動されました。本ファイルは
指示どおり `proposed/` に置いてあります（= ビルドに入りません）。本採用時は同じ移動で構いません。

---

**実機は使っていません。** この文書の数値はすべて (a) アセンブラとオブジェクトファイル、(b) Python の
モデルとその実行出力、(c) ホストの `gcc` でビルドした C 検証器の出力、のいずれかから来ています。
シリコンの証拠として引用しているのは `data/*.json` の既存の実測（ex01/ex03 の結果）だけで、
本カーネル自身の実測はありません。
