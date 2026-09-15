# ex21 (proposed) — QuickJS VM のフレーム立ち上げを PIE に載せる: 定数充填・ゼロ走査・128bit コピー

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex21_vmframe.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 12 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `453d43d7af573c8f3c25fea21dfd21be` → `ba74b9b9ba878deac8b1140997513ce8`（tools/check_abi.py / tools/fix_abi.py）

> **これは提案である。効くとは限らない。効くならここ、という形を示すための例題。**
>
> - 4 つのカーネルのうち、**VM の最ホット経路では 1 つも走らない**ことは既に分かっている。
>   `fib(30)`（`bench_calls.js`）は `var_count == 0` なので、A の充填ループは **0 回**しか呼ばれない
>   （`notes/cardputer-adv-project/vm-pie-fit.md` §2.3 [実測(host)]）。命令数が減るのは「V や R が大きいフレーム」だけである。
> - そして本命の障害は速度ではなく**整列契約**で、**現行のフレームレイアウトではその契約が成立しない**
>   （§2）。この例題の成果物は「速くなる」ではなく、**壊れ方の明文化**と、**契約を満たした場合に何命令に
>   なるか**の実数である。
> - **実機は 1 度も使っていない**（`/dev/ttyACM0` は別エージェントが占有）。サイクルは 1 つも測って
>   いない。§5.4 のサイクル数は「命令数 + 0.6 × ストア数」という**他所の実測式に外挿した推定**である。
> - ファイルは `examples/firmware/main/proposed/ex21_vmframe.S`。`main.c` / `examples.h` /
>   `CMakeLists.txt` は触っていない（`proposed/` はビルドに入っていない）。

対象は兄弟プロジェクト `cardputer-adv-pocketjs` の QuickJS 改 VM（`/workspace/pjs-vm`、ブランチ
`vm/main`）。この文書の中の `components/...` `docs/...` は**あちら側の相対パス**で、このリポジトリの
ものではない。

## 0. 状態（何が確認済みで、何が未確認か）

| 項目 | 状態 | 根拠 |
|---|---|---|
| アセンブル | **通る**。`-Wall -Wextra` で警告 0、literal pool なし、`l32r` 0 | §6 |
| エンコーディング | 使った **12 命令すべて manual == toolchain（24 比較、不一致 0）** | §7 |
| 振る舞い | **Python モデル vs ホスト C 参照で 6437 ケース、不一致 0**（先頭不一致: なし）。A/A2/D 各 427、B 224、C 4932 | §8 |
| 整列契約 | **モデルで破り方を実演**（どの入力でどの 4 バイトが壊れるか）。piesim でも第三者実装で確認 | §2.4, §9 |
| 実機 | **未使用**（1 度も触っていない） | — |
| サイクル | **未計測**。§5.4 は命令数からの推定 | §5 |
| 実機で未確認の意味論 | 一覧にしてある（`EE.SRS.ACCX` の飽和、`movnez`、`EE.VST.L.64.IP` の実機挙動など 13 項目） | §11 |

## 1. なぜ VM のフレーム立ち上げなのか

`notes/cardputer-adv-project/vm-pie-fit.md` の結論（**このリポジトリの前段の調査**）は「VM の 4 領域
（呼び出し経路／自前アロケータ／自己管理スタック／中止可能プロセスモデル）のいずれにも PIE はそのまま
刺さらず、形だけ合う候補は自己管理スタックの**定数一括充填だけ**」だった。壁は 1 つ:

> 実機のフレーム・ブロック・確保したメモリは **4 バイト整列**しか契約されていない。PIE の 128bit
> ストアは `store128({as[31:4], 4{0}})` で下位 4bit を強制的に 0 にする。整列違反は例外にならず、
> **黙って 4 バイト手前を壊す**。

本作はその 2 つの候補（C1 = `var_buf` の `JS_UNDEFINED` 充填、C3 = `var_refs` の NULL 充填）を
実際に書き、加えて notes/cardputer-adv-project/vm-pie-fit.md が「`first-fit` の『何番目か』を返す命令が無い」と切った**ゼロ走査**と、
セグメント間で要る**128bit コピー**を足したものである。対象の C ループ:

```
quickjs.c:18782-18784   for (i = 0; i < b->var_count; i++) var_buf[i] = JS_UNDEFINED;
quickjs.c:18789-18791   for (i = 0; i < b->var_ref_count; i++) sf->var_refs[i] = NULL;
```

実機（NAN boxing、32bit）の値:

* `JSValue` = `uint64_t`（`quickjs.h:155-157`, `:241`）、**8 バイト**
* `JS_UNDEFINED` = `JS_MKVAL(JS_TAG_UNDEFINED, 0)` = `(3 << 32) | 0` = `0x0000_0003_0000_0000`
  （`quickjs.h:175` で `JS_TAG_UNDEFINED = 3`、`:249` が `JS_MKVAL`、`:408` が `JS_UNDEFINED`）。
  **ゼロではない**: 各スロットの上位 32bit が 3、下位が 0。2 スロットぶんの 128bit 定数は
  4 つの 32bit セグメントで `{ 0, 3, 0, 3 }`。
* 1 フレームのブロック = `[JSVMLink][JSStackFrame][slots][var_refs]`（`quickjs.c:18718-18721`）、
  `local_buf = (JSValue *)(sf + 1)`（`:18736`）、`var_buf = local_buf + arg_allocated_size`（`:18778`）。

## 2. 契約 — 16 バイト整列（この文書の本体）

### 2.1 128bit アクセスは下位 4bit を落とす

`data/pie_instructions.json` の Operation（TRM の疑似コード、そのまま）:

```
EE.VLD.128.IP (p164)  qu[127:0] = load128({as[31:4],4{0}})
                      as[31:0]  = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}
EE.VST.128.IP (p275)  qv[127:0] => store128({as[31:4],4{0}})
                      as[31:0]  = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}}
EE.VLD.L.64.IP (p168) qu[63:0]  = load64({as[31:3],3{0}})
EE.VST.L.64.IP (p279) qv[63:0]  => store64({as[31:3],3{0}})
```

下位 4bit（8bit 版は 3bit）は**無視される**。例外は無い。これは ex03 が実機で確認済みで、
`data/pie_examples_measured.json` の `vld128_drops_the_low_address_bits` に

> the low four address bits are dropped, not honoured, so an access off the 16-byte grid silently reads
> a different 16 bytes (and does not fault)

と記録されている（`docs/pie-simd.md` §5 も同じ）。**このファイルの全カーネルは「呼び出し側が 16 バイト
整列を与える」を契約とし、整列していない入力は扱わない**。検出も拒否もしない — 検出には追加のスカラ
命令が要り、それを払うかの判断は呼び出し側の話である（呼び出し側が `(uintptr_t)var_buf & 15` を見て
自分で断るのが正しい分担。`assert` を 1 本置くだけならカーネル側でもできるが、この例題は
「速くする」側の算術を崩さないために入れていない）。

### 2.2 実機のフレームはその契約を持っていない

算術（すべて `file:line` 付きの式から。**実機でアドレスを採取したものではない**）:

```
var_buf = block + JS_VM_FRAME_PREFIX + sizeof(JSStackFrame) + 8*arg_allocated_size
        = block + 4 + 48 + 8*arg_allocated_size            (FLATCALLS ビルド: JS_VM_FRAME_PREFIX = 4)
  52 = 4 + 48 は 16 の倍数ではない  ->  var_buf % 16 は
     arg_allocated_size 偶数 -> 4
     arg_allocated_size 奇数 -> 12
  つまり var_buf % 16 == 0 は arg_allocated_size 偶数かつ block % 16 == 0 のときだけ。
```

* `JS_VM_FRAME_PREFIX = sizeof(JSVMLink)`（`quickjs-vmstack.h:644`）、`JS_VM_FRAME_ALIGN = 4`
  （`:113`、`ESP_PLATFORM` 側）。ブロックは `JS_VM_FRAME_ALIGN` に丸められて前から詰まるので、
  各フレームの `block % 16` は 0/4/8/12 に散る。
* したがって `var_buf` が 16 バイト整列になるのは **約 1/4 のフレーム**（レイアウトからの計算。
  実機で数えたものではない。notes/cardputer-adv-project/vm-pie-fit.md §4 も同じ 1/4 と書いている）。
* セグメントの**ペイロード先頭だけ**は 16 バイト整列（`quickjs-vmstack.h:70-72`, `:110`、
  `js_vm_seg_new` の `:382`）。個々のブロックは 4 バイト整列である。

### 2.3 slot 単位の peel では直せない

`var_buf` が「ずれている」とき、カーネル側で先頭を 1 スロット（8 バイト）だけスカラで処理して
grid に乗る、という回避策は**この配列では成立しない**:

```
4 + 8k  (mod 16)  ∈ {4, 12}    -- 8 の倍数を足しても 4 か 12 のまま。16 には到達しない。
```

つまり 8 バイト要素の配列を 8 バイトずつずらしても 16 の倍数にはならない。直すのは**アロケータ側**
（`var_buf` を align_up するか、`JS_VM_FRAME_PREFIX + sizeof(JSStackFrame)` と `block_size` の両方を
16 の倍数に丸める）しかなく、それは `quickjs.c` / `quickjs-vmstack.h` の変更である。**このリポジトリは
それをしていない**し、この例題もそれは提案しない（アロケータを触る判断はあちら側の設計判断）。

### 2.4 どの入力でどう壊れるか（実演）

モデル（`/tmp/ex21_model.py`、`.S` の命令列をそのまま解釈する）と piesim.py で、**16 バイト grid + 4**
のポインタを与えたときの実測（`/tmp/ex21_misalign.txt`、`/tmp/piesim_ex21.txt`）。配列の 4 バイト手前
には「直前のワード」を置いてある（実機なら `args_buf` の最後の引数のタグワード、あるいは手前の
フレームの末尾）。

モデルの出力（下に全部貼る。元ファイルは `/tmp/ex21_misalign.txt`）:

```
"""

ex21 misalignment report. The MODEL applies the TRM address rule ({as[31:4],4{0}}); the .S is
otherwise the same file the equivalence sweep ran. PREV = 0xdeadbeef, guards = 0xaaaaaaaa.

================================================================================================================
A) ex21_fill_undefined(var_buf, n_values = 8, undef_pair) -- the constant/pointer work of one frame
----------------------------------------------------------------------------------------------------------------
A) ex21_fill_undefined(var_buf, n_values = 8, undef_pair)   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1005f  (64 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1005f
  bytes the kernel actually LOADED  : 0x30000 .. 0x3000f
    0x10020 array word 0       11111111 -> 00000000
    0x10024 array word 1       11111111 -> 00000003
    0x10028 array word 2       11111111 -> 00000000
    0x1002c array word 3       11111111 -> 00000000
    0x10030 array word 4       11111111 -> 00000000
    0x10034 array word 5       11111111 -> 00000003
    0x10038 array word 6       11111111 -> 00000000
    0x1003c array word 7       11111111 -> 00000000
    0x10040 array word 8       11111111 -> 00000000
    0x10044 array word 9       11111111 -> 00000003
    0x10048 array word 10      11111111 -> 00000000
    0x1004c array word 11      11111111 -> 00000000
    0x10050 array word 12      11111111 -> 00000000
    0x10054 array word 13      11111111 -> 00000003
    0x10058 array word 14      11111111 -> 00000000
    0x1005c array word 15      11111111 -> 00000000
----------------------------------------------------------------------------------------------------------------
A) ex21_fill_undefined(var_buf, n_values = 8, undef_pair)   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10063  (64 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1005f
  bytes the kernel actually LOADED  : 0x30000 .. 0x3000f
    0x10020 PREV word (before the array) deadbeef -> 00000000
    0x10024 array word 0       11111111 -> 00000003
    0x10028 array word 1       11111111 -> 00000000
    0x1002c array word 2       11111111 -> 00000000
    0x10030 array word 3       11111111 -> 00000000
    0x10034 array word 4       11111111 -> 00000003
    0x10038 array word 5       11111111 -> 00000000
    0x1003c array word 6       11111111 -> 00000000
    0x10040 array word 7       11111111 -> 00000000
    0x10044 array word 8       11111111 -> 00000003
    0x10048 array word 9       11111111 -> 00000000
    0x1004c array word 10      11111111 -> 00000000
    0x10050 array word 11      11111111 -> 00000000
    0x10054 array word 12      11111111 -> 00000003
    0x10058 array word 13      11111111 -> 00000000
    0x1005c array word 14      11111111 -> 00000000
================================================================================================================
B) ex21_fill_null(ptrs, n_ptrs = 4) -- the constant/pointer work of one frame
----------------------------------------------------------------------------------------------------------------
B) ex21_fill_null(ptrs, n_ptrs = 4)   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1002f  (16 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1002f
    0x10020 array word 0       11111111 -> 00000000
    0x10024 array word 1       11111111 -> 00000000
    0x10028 array word 2       11111111 -> 00000000
    0x1002c array word 3       11111111 -> 00000000
----------------------------------------------------------------------------------------------------------------
B) ex21_fill_null(ptrs, n_ptrs = 4)   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10033  (16 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1002f
    0x10020 PREV word (before the array) deadbeef -> 00000000
    0x10024 array word 0       11111111 -> 00000000
    0x10028 array word 1       11111111 -> 00000000
    0x1002c array word 2       11111111 -> 00000000
================================================================================================================
D) ex21_copy_values(dst, src (aligned), n_values = 4) -- the constant/pointer work of one frame
----------------------------------------------------------------------------------------------------------------
D) ex21_copy_values(dst, src (aligned), n_values = 4)   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1003f  (32 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1003f
  bytes the kernel actually LOADED  : 0x18000 .. 0x1801f
    0x10020 array word 0       22222222 -> 33333330
    0x10024 array word 1       22222222 -> 33333331
    0x10028 array word 2       22222222 -> 33333332
    0x1002c array word 3       22222222 -> 33333333
    0x10030 array word 4       22222222 -> 33333334
    0x10034 array word 5       22222222 -> 33333335
    0x10038 array word 6       22222222 -> 33333336
    0x1003c array word 7       22222222 -> 33333337
----------------------------------------------------------------------------------------------------------------
D) ex21_copy_values(dst, src (aligned), n_values = 4)   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10043  (32 bytes)
  bytes the kernel actually STORED  : 0x10020 .. 0x1003f
  bytes the kernel actually LOADED  : 0x18000 .. 0x1801f
    0x10020 PREV word (before the array) deadbeef -> 33333330
    0x10024 array word 0       22222222 -> 33333331
    0x10028 array word 1       22222222 -> 33333332
    0x1002c array word 2       22222222 -> 33333333
    0x10030 array word 3       22222222 -> 33333334
    0x10034 array word 4       22222222 -> 33333335
    0x10038 array word 5       22222222 -> 33333336
    0x1003c array word 6       22222222 -> 33333337
================================================================================================================
C) ex21_scan_first_nonzero, all 8 words zero
----------------------------------------------------------------------------------------------------------------
C) ex21_scan_first_nonzero(p, n_words = 8)   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1003f  (32 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x1003f
    no word changed (!!)
    returned index                    : 8 (the caller's contract: first non-zero word, or 8 if none)
----------------------------------------------------------------------------------------------------------------
C) ex21_scan_first_nonzero(p, n_words = 8)   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10043  (32 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x10033
    no word changed (!!)
    returned index                    : 8 (the caller's contract: first non-zero word, or 8 if none)
================================================================================================================
C'') THE CASE WHERE THE MISALIGNED SCAN ANSWERS WRONG: the array's word 5 is the first
     non-zero word (expected answer 5), and the 4 bytes before the array hold 0xDEADBEEF.
     On the grid that word is array word 0 (zero), so the two runs differ by exactly the
     thing the contract is about. Watch the returned index and the bytes loaded.
----------------------------------------------------------------------------------------------------------------
C'') ex21_scan_first_nonzero, the first non-zero WORD OF THE ARRAY is index 5   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1003f  (32 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x1003f
    no word changed (!!)
    returned index                    : 5 (the caller's contract: first non-zero word, or 8 if none)
----------------------------------------------------------------------------------------------------------------
C'') ex21_scan_first_nonzero, the first non-zero WORD OF THE ARRAY is index 5   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10043  (32 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x10033
    no word changed (!!)
    returned index                    : 8 (the caller's contract: first non-zero word, or 8 if none)
================================================================================================================
C''') the same question with 16 words, so that phase 1 (the 64-byte OR fold) runs first:
----------------------------------------------------------------------------------------------------------------
C''') ex21_scan_first_nonzero, 16 words, the first non-zero word is index 5   [array ON the 16-byte grid]
  the array the caller meant        : 0x10020 .. 0x1005f  (64 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x1005f
    no word changed (!!)
    returned index                    : 5 (the caller's contract: first non-zero word, or 16 if none)
----------------------------------------------------------------------------------------------------------------
C''') ex21_scan_first_nonzero, 16 words, the first non-zero word is index 5   [array at grid+4]
  the array the caller meant        : 0x10024 .. 0x10063  (64 bytes)
  bytes the kernel actually LOADED  : 0x10020 .. 0x1005f
    no word changed (!!)
    returned index                    : 16 (the caller's contract: first non-zero word, or 16 if none)

```

読み方（数字はすべてこの実行のもの）:

| カーネル | grid 上 | grid+4（実機のフレームが作る位置） |
|---|---|---|
| A `fill_undefined` (n=8) | 配列の 64 バイトだけが `{0,3,0,3,...}` になり、外は無傷 | **手前 4 バイトが壊れる**（`deadbeef` → `00000000`。実機なら直前の JSValue のタグワードが 0 = `JS_TAG_INT` に化ける）。配列は 4 バイトずれ、`slot0 = {low=3, high=0}` すなわち **`undefined` が整数 3 として読める**。配列末尾 4 バイトは**書かれないまま**残る |
| B `fill_null` (n=4) | 16 バイトが 0 | 手前 4 バイトが壊れ、**4 本目のポインタが NULL にならない**（`0x11111111` のまま = 実機なら野良ポインタを `var_refs` として辿る） |
| C `scan_first_nonzero` (n=8) | 5（正しい。ロード範囲も配列内） | **8 = 「無い」と誤答**（正解は 5）。ロード範囲は 4 バイト手前から始まり、末尾 16 バイトを読み残す |
| D `copy_values` (n=4) | 32 バイトが正しく写る | 手前 4 バイトが壊れ、コピー全体が 1 ワードずれる。末尾 4 バイトは写らない |

C が「例外ではなく**静かに誤答**する」ことがこの表の要点である: 検出は 4 バイト手前のチャンクで行い、
最終的な語の取り出しは呼び出し側のポインタ（4 バイト後ろ）から行うので、**見ている場所と読む場所が
食い違う**。その結果、配列の手前に非ゼロがあると**検出だけが早く当たり**、答えは「無い」になる
（C'' の行、`returned index 8`）。

piesim.py（`/workspace/pjs-vm/tools/pie/piesim.py` の**無改変コピー**、別プロジェクトの解釈実行器）でも
同じ規則が出る（§9 に全出力）:

```
PROBE: ee.vst.128.ip q0(zero), as = 0x84
  the caller asked for 16 bytes at 0x84..0x93 (as[3:0] = 4)
  the store landed at      : 0x080..0x08f
  0x80..0x83 (before the asked-for range) : 00 00 00 00
  0x90..0x93 (the asked-for tail)         : 22 22 22 22
  fault / trap                            : none
```

### 2.5 契約を満たす側のコスト（これは速くする話の前に払う）

`var_buf` を 16 バイト整列にするには、アロケータ側で次のどちらかが要る（**金額はレイアウトからの
算術で、実機メモリ使用量を測ったものではない**）:

| 案 | 追加コスト | 備考 |
|---|---|---|
| (a) `var_buf` だけ align_up | 1 フレームあたり **最大 12 バイト**（0/4/8/12 のずれを埋める） | `var_buf` を返す計算（`quickjs.c:18778`）の後に丸めを入れる。`sf->var_buf` / `sf->arg_buf` の整合を保つ必要がある |
| (b) prefix + `sizeof(JSStackFrame)` と `block_size` を 16 の倍数に | prefix が 4 → 16（**+12 バイト/フレーム**）、ブロックサイズの丸めで **さらに最大 12 バイト/フレーム** | 「全フレームが grid 上に載る」ので、他の 128bit 用途（将来のコピーやマーク）にも効く |

実機の `frame_max` は **84〜188 バイト**（`docs/vm-L2-design.md` §13.2、notes/cardputer-adv-project/vm-pie-fit.md §2.3 経由）なので、
(b) は **+6〜29% のメモリ**である。セグメントは上限 4096 バイト（`quickjs-vmstack.h` の `JS_VM_SEG_*`）
なので、フレーム数の上限がその割合だけ減る。**この交換が割に合うかはこの例題では判定できない**
（実機でフレームメモリの到達量を測る必要がある。§11）。

## 3. 4 つのカーネル

すべて leaf（`entry a1, 32` … `retw.n`）、`.iram1`、`.align 4`、定数は呼び出し側からポインタ渡し
（`.iram1` から `.rodata` へ `l32r` は届かないので、この例題集の規則）。`windowed ABI` なので
引数は a2, a3, a4、返り値は a2。

### A. `ex21_fill_undefined(int16_t *var_buf, uint32_t n_values, const int16_t *undef_pair)`

```
a2 = var_buf     n_values スロット（8 バイトずつ）、16 バイト整列
a3 = n_values    スロット数（quickjs.c:18783 の b->var_count）
a4 = undef_pair  2 × JS_UNDEFINED = 16 バイト、16 バイト整列 { 0, 3, 0, 3 }
```

* 1 反復で 16 バイト（2 スロット）を `EE.VST.128.IP`。ポインタは命令自身が 16 ずつ進める。
* **`n_values` が奇数**のときは最後の 1 スロットを `EE.VST.L.64.IP`（8 バイト、TRM p279）で書く。
  奇数スロットのアドレスは `8*i`（i 偶数）なので 16 バイト整列でもあるから、**そこに 128bit ストアを
  してはいけない**: 配列の後ろ 8 バイト（フレームブロックでは `var_refs` の先頭 = `quickjs.c:18787`）を
  壊すことになる。この 8 バイトがこの命令を選んだ理由そのもの。
* ポインタの歩み: 16 × `floor(n/2)` + 奇数なら 8 → `a2` は必ず「書いた範囲の直後」で終わる
  （§8 で 427+427+427 ケース検査、不一致 0）。
* `ex21_fill_undefined_inline`（第 2 エントリ）は同じ本体で、定数を `movi` ×2 + `EE.MOVI.32.Q` ×4
  で組む版（**+6 命令/呼び出し**）。呼び出し側に 16 バイト定数を置く場所が無いときのため。
  依頼時のスケッチは 2 引数（`var_buf`, `n_values`）だったが、「定数はポインタ渡し」の規約に沿った
  3 引数版を主、2 引数版を副として両方入れてある。
* 厳密性: 常に厳密。`JS_UNDEFINED` は呼び出し側が渡した 16 バイトを**そのまま**書く（定数の内容が
  `JS_UNDEFINED` であることはカーネルは知らない・検証しない。`inline` 版だけが `{0,3,0,3}` を組む）。

### B. `ex21_fill_null(uint32_t *ptrs, uint32_t n_ptrs)`

```
a2 = ptrs    n_ptrs 本のポインタ（4 バイトずつ）、16 バイト整列
a3 = n_ptrs  本数（quickjs.c:18790 の b->var_ref_count）
```

* 値は 0 一色なので `EE.ZERO.Q` 1 回、あとは `EE.VST.128.IP` が 4 本ずつ書く。ロードは 1 本も無い。
* 末尾 `n_ptrs % 4`（1〜3 本）は `s32i`。ここで 128bit ストアを使うと**ブロックの外**（次のフレームか
  セグメントの空き）まで書く。`var_refs` はブロックの最後の配列なので特に危ない。
* ポインタの歩み: `a2 + 4*n_ptrs`（224 ケース検査、不一致 0）。
* 厳密性: 常に厳密（NULL 充填に値の依存が無い）。

### C. `uint32_t ex21_scan_first_nonzero(const uint32_t *p, uint32_t n_words)`

```
a2 = p        16 バイト整列、a3 = n_words
返り値        最初の非ゼロ語のインデックス。全語ゼロなら n_words。
```

**返り値の契約**（依頼で明記が要るとされた点）:

* 「見つからなかった」は **`n_words`** を返す（最後の有効インデックス + 1 = 番兵）。
  したがって呼び出し側の判定は `i = ex21_scan_first_nonzero(p, n); if (i == n) { ...not found... }`。
  大きさだけでは「`n-1` で当たった」と区別できないので、**必ず n と比較する**こと。
* `n_words == 0` は `0`（= `n_words`）を返す。空配列と「見つからない」は同じ答えに畳んである。
* 番兵が欲しければ `if (i == n) i = ~0u;` の 2 命令で作れる（カーネル内で分岐を増やさない）。

3 段構えで、段の選択理由は §4:

1. **64 バイト（16 語）ずつ**: `EE.VLD.128.IP` ×4 を `EE.ORQ` ×3 で畳み、その OR の 8 個の u16 レーンを
   ACCX に合算（`EE.ZERO.ACCX` + `EE.VMULAS.U16.ACCX` + `EE.SRS.ACCX`）して分岐。非負レーンの和が 0
   ⟺ 全レーンが 0、なので比較もマスクも要らない。1 反復 14 命令 / 16 語。
2. **当たった 64 バイトの中で 16 バイトずつ**: `EE.VCMP.EQ.S32` + `EE.NOTQ` + `EE.ANDQ`（TRM p156/p120/p76）
   で「非ゼロ語の**個数**」を ACCX に作って分岐（0〜4）。最大 3 反復。呼び出しごとに 1 回だけ通る。
3. **当たった 4 語（または末尾 0〜3 語）**: `l32i` ×4 と **分岐の無い `movnez` 4 連鎖**。
   大きいインデックスから書いていくので、最後に発火したものが最小インデックスになる。初期値を
   `n_words` にしておくことで「見つからない」が clamp 無しで出る。

* 厳密性: 常に厳密。**飽和しない**: 段 1 の ACCX は最大 `8 × 65535 = 524280` < 2^40 − 1、段 2 は 0〜4。
  `EE.SRS.ACCX` の 32bit 飽和（TRM p134）は段 2 の 0〜4 では絶対に起きない。
* 読み出しの範囲: 整列入力では**配列の外を 1 バイトも読まない**（4932 ケースで厳密に検査、0 件）。
  末尾は `movi` で 0 を埋めたレジスタを使い、足りない語は読みに行かない。
* ポインタの歩みは段ごとに違う（段 1 は 64 バイト先、段 2 は 16 バイト先まで進んでから戻す）ので
  §8 のポインタ検査は A/B/D のみに掛けている。

### D. `ex21_copy_values(int16_t *dst, const int16_t *src, uint32_t n_values)`

```
a2 = dst, a3 = src  両方 16 バイト整列、a4 = n_values（8 バイトのスロット数）
```

* 16 バイト（2 スロット）ずつ。奇数個の末尾は `EE.VLD.L.64.IP` + `EE.VST.L.64.IP`（TRM p168/p279）。
  両ポインタとも書き写した範囲の直後で終わる。
* **パイプライン化しない**（これは判断）。次のグループを先読みする形にすると、ロードの stage-M def を
  distance 2 で消費できて 1 サイクル/グループのインターロックが消えるが（`data/pie_timing_measured.json`
  の実測アンカー: distance 1 で 1.000 サイクル、distance 2 で 0.000）、load-after-store のパイプラインは
  **領域の終わりを 16 バイト越えて読む**。このカーネルの領域はフレームブロックで、その終端はセグメント
  境界かもしれない。`ex15_copy_row`（行バッファ、自分が所有する）は逆を選んでいる。ここは越えて読まない
  代わりに 32 バイトあたり 2 サイクル（推定）を払う。

## 4. 命令列の選び方（設問への答え: 何を検討し、何を使ったか）

### 4.1 使った命令（実際に出荷した命令列）

C の段 2 が依頼の形そのもの、**比較とマスク合成だけで件数を数える**形である:

```
EE.VLD.128.IP      q2, a2, 16    ; 4 語
EE.VCMP.EQ.S32     q3, q2, q0    ; 語が 0 のレーンに 0xFFFFFFFF
EE.NOTQ            q3, q3        ; 語が非 0 のレーンに 0xFFFFFFFF  (= マスク)
EE.ANDQ            q3, q3, q6    ; q6 = 各 32bit レーンに 0x00000001
EE.ZERO.ACCX
EE.VMULAS.U16.ACCX q3, q1        ; q1 = u16 レーンが全て 1 → ACCX = 非 0 語の個数 (0..4)
EE.SRS.ACCX        a7, a5, 0     ; a7 = その個数 (a5 = 0)
bnez               a7, ...
```

* `a7` は「この 16 バイトに非ゼロ語が何個あるか」そのもの（bool ではない）。0 なら次へ、非 0 なら
  答えはこの 4 語の中。
* 分岐レスに近い形は段 3 が担う: `movnez` 4 連鎖（+ `l32i` 4・`addi` 3）に**分岐が 1 つも無い**。
  段 1・段 2 の分岐はそれぞれ **64 バイト / 16 バイトにつき 1 本**（スカラで 1 語ごとに 1 本だったもの）。

### 4.2 検討して採らなかった形

| 形 | 命令列 | 採らなかった理由 |
|---|---|---|
| 水平 min で最初の非ゼロレーンを出す | 候補を作る（`NOTQ`+`ANDQ`+`ANDQ`+`ORQ` = 4）→ `EE.VUNZIP.32 q,q`（p291、レーンを `[x0,x2,x0,x2]/[x1,x3,x1,x3]` に分ける）→ `EE.VMIN.S32`（p192）→ もう一度 `EE.VUNZIP.32`+`VMIN.S32` → `EE.MOVI.32.A` ×2 → スカラ `min`。加えて「レーン番号ベクトル {0,1,2,3}」を `movi`+`EE.MOVI.32.Q`×4（5 命令、1 回）で作る | 合計 12〜14 命令 + 1 回きりの定数 5 命令。**`movnez` 4 連鎖（4 `addi` + 4 `movnez` = 8 命令 + `l32i` 4）と同等かそれ以上**で、検証する命令が増える。PIE の「水平 min」は存在しないので、これは「レーンを分けて比較を 2 段」で作る必要がある — 作れはするが得が無い |
| 重み付き和でインデックスを直接出す | q1 の代わりに u16 レーン `{0,0,1,0,2,0,3,0}` の重みベクトルを使い `ACCX = Σ i·nz_i`。**非ゼロ語がちょうど 1 個のとき** `SRS.ACCX` がその位置そのものを返す | 一般には min ではない（2 個以上で和になる）。個数 `c` も同時に取って `c==1` のときだけ採用する形にすると、条件移動か分岐が要る。**当たりは 1 回しか通らないので、節約できるのは 1 回きりの 12 命令**で、そのためにレジスタ 1 本（重み、5 命令で構築）と分岐が増える。`first non-zero` の一般解は chain のままで、速い道を足す価値が無い |
| 段 1 も `VCMP` で行う | `VCMP.EQ.S32` + `NOTQ` + `ANDQ` + `VMULAS` | 段 1 の目的は「この 64 バイトが全部ゼロか」だけ。**非負レーンの和が 0 ⟺ 全レーンが 0** なので、生の OR をそのまま `VMULAS` に掛ければ足りる（1 反復あたり 3 命令節約）。段 1 は 16 語ごとに回るので、節約はここが一番効く |
| 段 2 から `VCMP` を外して 4 命令削る | `VMULAS(q2, q1)` + `SRS` の 2 命令でゼロ判定 | できる（同じ「和が 0」論法）。だが段 2 は**呼び出しあたり最大 3 回**しか回らないので削減は 1 回きりの 12 命令。それより「非ゼロ語の個数」という**呼び出し側が欲しがる情報**（このグループに空きが何個あるか）を同じ命令で出すほうが良い |
| `wsr.sar` + `EE.VSL.32`（p268）で q1 を q6 から作る | `movi`(16) + `wsr.sar` + `EE.VSL.32` + `EE.ORQ` = 4 命令（q6 の構築 5 命令と合わせて 9） | `movi`/`slli`/`addi` + `MOVI.32.Q`×4 の 7 命令より 2 命令安いだけ。**SAR は VMUL 系と共有**（`docs/pie-simd.md` §2「SAR は先頭で 1 回」）で、ここはその原則を崩してまで払う額ではない |
| 奇数末尾も 128bit で書く | `EE.VST.128.IP q0, a2, 0` | **配列の後ろを壊す**（§3-A） |
| 奇数末尾をスカラ 2 命令で書く | `l32i`+`l32i`+`s32i`+`s32i`（4 命令） | 64bit 版 2 命令のほうが安い。ただし `EE.VST.L.64.IP` は **notes/cardputer-adv-project/vm-pie-fit.md §4 が「piesim 未実装で意味を検証していない」と明記した命令**である。本作は TRM p279 の疑似コードからモデルを書き、C 参照と 1078 ケース（奇数末尾を含む A/A2/D の全ケース）で一致させた＝**算術としての意味は押さえた**が、実機の挙動と piesim は依然として空白のまま（§9, §11） |
| ループを `loopgtz` / `loop` にする | A・B の 1 グループ = 3 命令 → 1 命令（`addi`+`bnez` が消える）＝ **JSValue あたり 1.5 → 0.5 命令** | この例題集には `loop*` を使った前例が無く、`loop` 本体の**外へ分岐する**形（C の「当たったとき脱出」）はまさに ex04 が測る種類の未確認事項。ここでは採らず、**次の一手として明記**しておく（§11） |
| コピーのパイプライン化 | `ex15_copy_row` の形（2 レジスタ交互、distance 2、ストール 0） | **領域の終わりを 16 バイト越えて読む**。フレームブロックの終端はセグメント境界かもしれない（§3-D） |

`EE.VLDBC.32`（p173、1 語を 4 レーンに配る）は A の定数には使えない: `JS_UNDEFINED` は「下位 0・上位 3」
の**2 語**で、32bit をひとつ配ると 4 レーンが同じ値になってしまう（p173 の疑似コード:
`qu[127:0] = {4{load32(...)}}`）。

## 5. コスト

### 5.1 オブジェクトファイルからの静的な数（`/tmp/ex21_cost.py /tmp/ex21.o` の出力そのまま。
`<-` で始まる注釈だけが手で足したもの。objdump の生の出力は §6）

```
function                      insns   EE.*  ld/st   loop bodies (start..end)
ex21_fill_undefined              21      7      0
  loop                            3      1      0   0x029..0x02e      <- 1 グループ = 16 バイト
ex21_fill_undefined_inline        8      4      0   (エントリブロックのみ)
ex21_fill_null                   16      2      1
  loop                            3      1      0   0x048..0x04d      <- 1 グループ = 16 バイト
  loop                            4      0      1   0x054..0x05a      <- 末尾 1 ポインタ (s32i)
ex21_scan_first_nonzero          79     26      7
  loop                           14     10      0   0x095..0x0bb      <- 64 バイト / 16 語
  loop                           10      7      0   0x0c0..0x0da      <- 16 バイト / 4 語
ex21_copy_values                 20      8      0
  loop                            6      4      0   0x148..0x156      <- 32 バイト
```

`.iram1` は **0x163 = 355 バイト**（5 エントリポイント）。`l32r` は 0（定数を `.rodata` から読んでいない）。

### 5.2 比べる相手を同じツールチェーンで測る

`notes/cardputer-adv-project/vm-pie-fit.md` は「6〜7 命令/JSValue [obj]」と書いているが、それは in-tree のビルドの数え方である。
**同じループの形を独立関数として同じ gcc（crosstool-NG esp-15.2.0）で組んで数えた**のが下（生の
objdump は §6 の末尾、`/tmp/ex21_baseline.c`）:

| C ループ | -Os（VM のビルド。notes/cardputer-adv-project/vm-pie-fit.md §4） | -O2（この例題集の C 参照のビルド） | ex21 | 比（-Os 相手） |
|---|---|---|---|---|
| `var_buf[i] = JS_UNDEFINED` | **4 命令 / 8 バイトスロット** | 3 命令 / スロット（`loop` で足場 0） | **1.5 命令 / スロット**（1 ストア / 16 バイト + 足場） | 2.7 倍 |
| `var_refs[i] = NULL` | **3 命令 / 4 バイトポインタ** | `memset` 呼び出しに化ける | **0.75 命令 / ポインタ** | 4.0 倍 |
| `for (i...) if (p[i]) return i;` | **5 命令 / 語** | 4〜5 命令 / 語 | **0.875 命令 / 語**（段 1）+ 固定費 | 5.7 倍（長いゼロ区間で） |
| `dst[i] = src[i]` | **8 命令 / 8 バイトスロット**（16 命令 / 16 バイト） | 6 命令 / スロット（12 命令 / 16 バイト） | **3 命令 / 16 バイト** | 5.3 倍 |

`notes/cardputer-adv-project/vm-pie-fit.md` の「JSValue あたり 6〜7」との差は、in-tree ではループが `JS_CallInternal` の文脈に埋まって
いて周辺のインデックス計算・レジスタ圧を払うためと見られる（**推測**。私は in-tree の `.s` を再生成
していない）。

### 5.3 1 呼び出しあたりの命令数（オブジェクトからの算術。実行して数えたものではない）

```
A  (V = n_values)   = 9 + 3*floor(V/2) + (V 奇数 ? 1 : 0)      V=0 -> 3 (早期 return)
A' (定数を組む版)   = 15 + 3*floor(V/2) + (V 奇数 ? 1 : 0)
B  (R = n_ptrs)     = 9 + 3*floor(R/4) + (R%4 ? 1 + 4*(R%4) : 0)
C  (n 語, 全ゼロ)   = 20 + 1 + 14*floor(n/16) + 1 + 10*((n>>2)%4) + 10〜17
D  (V = n_values)   = 10 + (floor(V/2) 奇数 ? 2 : 0) + 6*floor(floor(V/2)/2) + (V 奇数 ? 2 : 0)
```

| 呼び出し | ex21 | 同じ形のスカラ (-Os) | 差 |
|---|---|---|---|
| A, V=2 | 12 | 13 | 1 命令 |
| A, V=8 | 21 | 37 | 16 命令（2.6 命令/スロット） |
| A, V=16 | 33 | 69 | 36 命令（2.1 命令/スロット） |
| A, V=1 | 10 | 9 | −1（**負ける**） |
| B, R=4 | 12 | 16 | 4 命令 |
| B, R=16 | 21 | 52 | 31 命令 |
| B, R=3 | 22 | 13 | −9（**負ける**） |
| C, 全ゼロ n=64 | 86 | 325 | 3.8 倍 |
| C, 全ゼロ n=256 | 254 | 1285 | 5.1 倍 |
| C, 当たりが語 5（n=8） | 55 | 29 | −26（**負ける**） |
| D, V=4 | 16 | 36 | 20 命令 |
| D, V=8 | 22 | 68 | 46 命令 |

* 損益分岐: A は **V ≥ 2**（呼び出しの `call8` を数えれば V ≥ 3 くらい）、B は **R ≥ 4**、
  C は **n ≈ 8 語以上**（`29 = 4.125n` を解いた値）かつ**当たりが 8 語目以降**（手前で当たるならスカラの
  早期脱出が圧勝）、D は **V ≥ 2**。
  A/B/D の損益分岐は notes/cardputer-adv-project/vm-pie-fit.md の「V ≳ 5」（in-tree の 6〜7 命令/スロット基準）より甘い。基準が違う
  だけで、どちらも算術である。
* **ホスト実測の最ホット経路では効かない**: `fib(30)` は `var_count = 0` なので A は 3 命令で即 return
  する（= 何もしない）。クロージャを持たない関数は R=0 で B も 3 命令。したがって**この例題の価値は
  実アプリの速度ではなく、契約・壊れ方・命令数の実数のほう**にある。

### 5.4 サイクル（**推定**）

`docs/pie-simd.md` §3.5 は（別のファームウェアの実機実測として）「PIE は 1 命令 1 サイクル、唯一の例外は
128bit ストアで **+0.6 サイクル**」と結論している。それを外挿すると:

```
A: 1 グループ (16 バイト) = 3 命令 + 0.6 = 3.6 サイクル = JSValue あたり 1.8
B: 1 グループ (16 バイト) = 3 + 0.6       = 3.6 サイクル = ポインタあたり 0.9
C: 64 バイト = 14 命令 + ストア 0         = 14 サイクル  = 語あたり 0.875
D: 32 バイト = 6 命令 + ストア 2*(0.6) + インターロック 2 (distance 1 の 2 組)
                              = 9.2 サイクル = 16 バイトあたり 4.6
```

* 240 MHz で A の V=8 は **約 28 サイクル ≈ 0.12 µs**（命令 21 + ストア 4×0.6）。これは「実機で測った」
  ではない。PIE はコプロセッサ 3 なので**タスク切り替えで 8 本の QR を退避・復元**する（`docs/pie-simd.md`
  §1）。VM タスクに常時 PIE を使わせた場合のスイッチコストは**未計測**で、この 0.12 µs を食い潰し得る。
* ex04 が実機で測った距離アンカー（`LD.QR` の def は M、distance 0 で 1.000 サイクル、distance 1 で
  0.000）は「ステージ M の def を E で食うと distance 1 で 1 サイクル」の根拠で、D のパイプライン化の
  判断（§3-D）はこれに依っている。

## 6. アセンブラの証拠（生の出力）

`ex21_vmframe.S`:

```
$ xtensa-esp32s3-elf-gcc -c -Wall -Wextra -o /tmp/ex21.o examples/firmware/main/proposed/ex21_vmframe.S

rc=0

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex21.o
0000012c 00000037 T ex21_copy_values
00000038 00000027 T ex21_fill_null
00000000 00000038 T ex21_fill_undefined
0000000c 00000015 T ex21_fill_undefined_inline
00000060 000000ca T ex21_scan_first_nonzero
rc=0

$ xtensa-esp32s3-elf-objdump -h /tmp/ex21.o
Idx Name          Size      VMA       LMA       File off  Algn
  3 .iram1        00000163  00000000  00000000  00000034  2**2
rc=0

$ xtensa-esp32s3-elf-objdump -d /tmp/ex21.o

/tmp/ex21.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex21_fill_undefined>:
   0:	004136        	entry	a1, 32
   3:	f3ac      	beqz.n	a3, 36 <ex21_fill_undefined_inline+0x2a>
   5:	830044        	ee.vld.128.ip	q0, a4, 0
   8:	000546        	j	21 <ex21_fill_undefined_inline+0x15>
	...

0000000c <ex21_fill_undefined_inline>:
   c:	004136        	entry	a1, 32
   f:	33ac      	beqz.n	a3, 36 <ex21_fill_undefined_inline+0x2a>
  11:	050c      	movi.n	a5, 0
  13:	360c      	movi.n	a6, 3
  15:	cd3254        	ee.movi.32.q	q0, a5, 0
  18:	cd3a54        	ee.movi.32.q	q0, a5, 2
  1b:	cd3664        	ee.movi.32.q	q0, a6, 1
  1e:	cd3e64        	ee.movi.32.q	q0, a6, 3
  21:	045030        	extui	a5, a3, 0, 1
  24:	414130        	srli	a4, a3, 1
  27:	648c      	beqz.n	a4, 31 <ex21_fill_undefined_inline+0x25>
  29:	8a0124        	ee.vst.128.ip	q0, a2, 16
  2c:	440b      	addi.n	a4, a4, -1
  2e:	ff7456        	bnez	a4, 29 <ex21_fill_undefined_inline+0x1d>
  31:	158c      	beqz.n	a5, 36 <ex21_fill_undefined_inline+0x2a>
  33:	840124        	ee.vst.l.64.ip	q0, a2, 8
  36:	f01d      	retw.n

00000038 <ex21_fill_null>:
  38:	004136        	entry	a1, 32
  3b:	e39c      	beqz.n	a3, 5d <ex21_fill_null+0x25>
  3d:	cd7fa4        	ee.zero.q	q0
  40:	145030        	extui	a5, a3, 0, 2
  43:	414230        	srli	a4, a3, 2
  46:	648c      	beqz.n	a4, 50 <ex21_fill_null+0x18>
  48:	8a0124        	ee.vst.128.ip	q0, a2, 16
  4b:	440b      	addi.n	a4, a4, -1
  4d:	ff7456        	bnez	a4, 48 <ex21_fill_null+0x10>
  50:	958c      	beqz.n	a5, 5d <ex21_fill_null+0x25>
  52:	060c      	movi.n	a6, 0
  54:	0269      	s32i.n	a6, a2, 0
  56:	224b      	addi.n	a2, a2, 4
  58:	550b      	addi.n	a5, a5, -1
  5a:	ff6556        	bnez	a5, 54 <ex21_fill_null+0x1c>
  5d:	f01d      	retw.n
	...

00000060 <ex21_scan_first_nonzero>:
  60:	004136        	entry	a1, 32
  63:	034d      	mov.n	a4, a3
  65:	02cd      	mov.n	a12, a2
  67:	050c      	movi.n	a5, 0
  69:	cd7fa4        	ee.zero.q	q0
  6c:	1e0c      	movi.n	a14, 1
  6e:	11ee00        	slli	a14, a14, 16
  71:	ee1b      	addi.n	a14, a14, 1
  73:	cdb2e4        	ee.movi.32.q	q1, a14, 0
  76:	cdb6e4        	ee.movi.32.q	q1, a14, 1
  79:	cdbae4        	ee.movi.32.q	q1, a14, 2
  7c:	cdbee4        	ee.movi.32.q	q1, a14, 3
  7f:	1f0c      	movi.n	a15, 1
  81:	fd32f4        	ee.movi.32.q	q6, a15, 0
  84:	fd36f4        	ee.movi.32.q	q6, a15, 1
  87:	fd3af4        	ee.movi.32.q	q6, a15, 2
  8a:	fd3ef4        	ee.movi.32.q	q6, a15, 3
  8d:	416230        	srli	a6, a3, 2
  90:	418260        	srli	a8, a6, 2
  93:	78ac      	beqz.n	a8, be <ex21_scan_first_nonzero+0x5e>
  95:	930124        	ee.vld.128.ip	q2, a2, 16
  98:	938124        	ee.vld.128.ip	q3, a2, 16
  9b:	a30124        	ee.vld.128.ip	q4, a2, 16
  9e:	a38124        	ee.vld.128.ip	q5, a2, 16
  a1:	dd7464        	ee.orq	q2, q2, q3
  a4:	ed78a4        	ee.orq	q4, q4, q5
  a7:	dd7844        	ee.orq	q2, q2, q4
  aa:	250804        	ee.zero.accx
  ad:	0a0a84        	ee.vmulas.u16.accx	q2, q1
  b0:	7e1754        	ee.srs.accx	a7, a5, 0
  b3:	05d756        	bnez	a7, 114 <ex21_scan_first_nonzero+0xb4>
  b6:	fcc662        	addi	a6, a6, -4
  b9:	880b      	addi.n	a8, a8, -1
  bb:	fd6856        	bnez	a8, 95 <ex21_scan_first_nonzero+0x35>
  be:	b69c      	beqz.n	a6, dd <ex21_scan_first_nonzero+0x7d>
  c0:	930124        	ee.vld.128.ip	q2, a2, 16
  c3:	9e82a4        	ee.vcmp.eq.s32	q3, q2, q0
  c6:	ddff54        	ee.notq	q3, q3
  c9:	ddbc54        	ee.andq	q3, q3, q6
  cc:	250804        	ee.zero.accx
  cf:	0a0b84        	ee.vmulas.u16.accx	q3, q1
  d2:	7e1754        	ee.srs.accx	a7, a5, 0
  d5:	043756        	bnez	a7, 11c <ex21_scan_first_nonzero+0xbc>
  d8:	660b      	addi.n	a6, a6, -1
  da:	fe2656        	bnez	a6, c0 <ex21_scan_first_nonzero+0x60>
  dd:	070c      	movi.n	a7, 0
  df:	080c      	movi.n	a8, 0
  e1:	090c      	movi.n	a9, 0
  e3:	0a0c      	movi.n	a10, 0
  e5:	14b030        	extui	a11, a3, 0, 2
  e8:	4bac      	beqz.n	a11, 110 <ex21_scan_first_nonzero+0xb0>
  ea:	0278      	l32i.n	a7, a2, 0
  ec:	bb0b      	addi.n	a11, a11, -1
  ee:	6b8c      	beqz.n	a11, f8 <ex21_scan_first_nonzero+0x98>
  f0:	1288      	l32i.n	a8, a2, 4
  f2:	bb0b      	addi.n	a11, a11, -1
  f4:	0b8c      	beqz.n	a11, f8 <ex21_scan_first_nonzero+0x98>
  f6:	2298      	l32i.n	a9, a2, 8
  f8:	c0b2c0        	sub	a11, a2, a12
  fb:	41b2b0        	srli	a11, a11, 2
  fe:	cb3b      	addi.n	a12, a11, 3
 100:	934ca0        	movnez	a4, a12, a10
 103:	db2b      	addi.n	a13, a11, 2
 105:	934d90        	movnez	a4, a13, a9
 108:	cb1b      	addi.n	a12, a11, 1
 10a:	934c80        	movnez	a4, a12, a8
 10d:	934b70        	movnez	a4, a11, a7
 110:	042d      	mov.n	a2, a4
 112:	f01d      	retw.n
 114:	c0c222        	addi	a2, a2, -64
 117:	ffe8c6        	j	be <ex21_scan_first_nonzero+0x5e>
 11a:	00          	.byte	00
 11b:	00          	.byte	00
 11c:	f0c222        	addi	a2, a2, -16
 11f:	0278      	l32i.n	a7, a2, 0
 121:	1288      	l32i.n	a8, a2, 4
 123:	2298      	l32i.n	a9, a2, 8
 125:	32a8      	l32i.n	a10, a2, 12
 127:	fff346        	j	f8 <ex21_scan_first_nonzero+0x98>
	...

0000012c <ex21_copy_values>:
 12c:	004136        	entry	a1, 32
 12f:	045040        	extui	a5, a4, 0, 1
 132:	414140        	srli	a4, a4, 1
 135:	04ac      	beqz.n	a4, 159 <ex21_copy_values+0x2d>
 137:	046040        	extui	a6, a4, 0, 1
 13a:	417140        	srli	a7, a4, 1
 13d:	005616        	beqz	a6, 146 <ex21_copy_values+0x1a>
 140:	830134        	ee.vld.128.ip	q0, a3, 16
 143:	8a0124        	ee.vst.128.ip	q0, a2, 16
 146:	f78c      	beqz.n	a7, 159 <ex21_copy_values+0x2d>
 148:	830134        	ee.vld.128.ip	q0, a3, 16
 14b:	838134        	ee.vld.128.ip	q1, a3, 16
 14e:	8a0124        	ee.vst.128.ip	q0, a2, 16
 151:	8a8124        	ee.vst.128.ip	q1, a2, 16
 154:	770b      	addi.n	a7, a7, -1
 156:	fee756        	bnez	a7, 148 <ex21_copy_values+0x1c>
 159:	458c      	beqz.n	a5, 161 <ex21_copy_values+0x35>
 15b:	890134        	ee.vld.l.64.ip	q0, a3, 8
 15e:	840124        	ee.vst.l.64.ip	q0, a2, 8
 161:	f01d      	retw.n
rc=0

$ xtensa-esp32s3-elf-objdump -d /tmp/ex21.o | grep -c l32r
0
```

§5.2 の比較に使ったスカラ C（`/tmp/ex21_baseline.c`）を **-Os**（VM のビルド、notes/cardputer-adv-project/vm-pie-fit.md §4）と
**-O2**（この例題集の C 参照のビルド）で組んだもの。数えたループ本体は、それぞれ
`c_fill_undefined` = 4 命令/スロット(-Os)・3 命令/スロット(-O2、`loop` なので足場 0)、
`c_fill_null` = 3 命令/ポインタ(-Os)・`memset` 呼び出し(-O2)、`c_scan_first_nonzero` = 5 命令/語(-Os)・
4〜5 命令/語(-O2)、`c_copy_values` = 8 命令/スロット(-Os)・6 命令/スロット(-O2):

```
**-Os**

/tmp/base-Os.o:     file format elf32-xtensa-le


Disassembly of section .literal:

00000000 <.literal>:
   0:	00000000 	

Disassembly of section .text:

00000000 <c_fill_undefined>:
   0:	004136        	entry	a1, 32
   3:	b03320        	addx8	a3, a3, a2
   6:	080c      	movi.n	a8, 0
   8:	390c      	movi.n	a9, 3
   a:	000146        	j	13 <c_fill_undefined+0x13>
   d:	0289      	s32i.n	a8, a2, 0
   f:	1299      	s32i.n	a9, a2, 4
  11:	228b      	addi.n	a2, a2, 8
  13:	f69237        	bne	a2, a3, d <c_fill_undefined+0xd>
  16:	f01d      	retw.n

00000018 <c_fill_null>:
  18:	004136        	entry	a1, 32
  1b:	a03320        	addx4	a3, a3, a2
  1e:	080c      	movi.n	a8, 0
  20:	000106        	j	28 <c_fill_null+0x10>
  23:	00          	.byte	00
  24:	0289      	s32i.n	a8, a2, 0
  26:	224b      	addi.n	a2, a2, 4
  28:	f89237        	bne	a2, a3, 24 <c_fill_null+0xc>
  2b:	f01d      	retw.n
  2d:	000000        	ill

00000030 <c_scan_first_nonzero>:
  30:	004136        	entry	a1, 32
  33:	028d      	mov.n	a8, a2
  35:	020c      	movi.n	a2, 0
  37:	000286        	j	45 <c_scan_first_nonzero+0x15>
  3a:	00          	.byte	00
  3b:	00          	.byte	00
  3c:	a09280        	addx4	a9, a2, a8
  3f:	0998      	l32i.n	a9, a9, 0
  41:	39cc      	bnez.n	a9, 48 <c_scan_first_nonzero+0x18>
  43:	221b      	addi.n	a2, a2, 1
  45:	f39237        	bne	a2, a3, 3c <c_scan_first_nonzero+0xc>
  48:	f01d      	retw.n
	...

0000004c <c_copy_values>:
  4c:	004136        	entry	a1, 32
  4f:	1144d0        	slli	a4, a4, 3
  52:	080c      	movi.n	a8, 0
  54:	000386        	j	66 <c_copy_values+0x1a>
  57:	00          	.byte	00
  58:	938a      	add.n	a9, a3, a8
  5a:	09a8      	l32i.n	a10, a9, 0
  5c:	19b8      	l32i.n	a11, a9, 4
  5e:	928a      	add.n	a9, a2, a8
  60:	09a9      	s32i.n	a10, a9, 0
  62:	19b9      	s32i.n	a11, a9, 4
  64:	888b      	addi.n	a8, a8, 8
  66:	ee9847        	bne	a8, a4, 58 <c_copy_values+0xc>
  69:	f01d      	retw.n
	...

0000006c <run_all>:
  6c:	004136        	entry	a1, 32
  6f:	04bd      	mov.n	a11, a4
  71:	02ad      	mov.n	a10, a2
  73:	000025        	call8	74 <run_all+0x8>
  76:	04bd      	mov.n	a11, a4
  78:	03ad      	mov.n	a10, a3
  7a:	000025        	call8	7c <run_all+0x10>
  7d:	04bd      	mov.n	a11, a4
  7f:	03ad      	mov.n	a10, a3
  81:	000025        	call8	84 <run_all+0x18>
  84:	0abd      	mov.n	a11, a10
  86:	0000a1        	l32r	a10, fffc0088 <run_all+0xfffc001c>
  89:	0c0c      	movi.n	a12, 0
  8b:	000025        	call8	8c <run_all+0x20>
  8e:	04cd      	mov.n	a12, a4
  90:	02bd      	mov.n	a11, a2
  92:	20a330        	or	a10, a3, a3
  95:	000025        	call8	98 <run_all+0x2c>
  98:	f01d      	retw.n
**-O2**

/tmp/base-O2.o:     file format elf32-xtensa-le


Disassembly of section .literal:

00000000 <.literal>:
   0:	00000000 	

Disassembly of section .text:

00000000 <c_fill_undefined>:
   0:	004136        	entry	a1, 32
   3:	739c      	beqz.n	a3, 1e <c_fill_undefined+0x1e>
   5:	1183d0        	slli	a8, a3, 3
   8:	f8c882        	addi	a8, a8, -8
   b:	418380        	srli	a8, a8, 3
   e:	0a0c      	movi.n	a10, 0
  10:	390c      	movi.n	a9, 3
  12:	01c882        	addi	a8, a8, 1
  15:	058876        	loop	a8, 1e <c_fill_undefined+0x1e>
  18:	02a9      	s32i.n	a10, a2, 0
  1a:	1299      	s32i.n	a9, a2, 4
  1c:	228b      	addi.n	a2, a2, 8
  1e:	f01d      	retw.n

00000020 <c_fill_null>:
  20:	004136        	entry	a1, 32
  23:	02ad      	mov.n	a10, a2
  25:	738c      	beqz.n	a3, 30 <c_fill_null+0x10>
  27:	11c3e0        	slli	a12, a3, 2
  2a:	00a0b2        	movi	a11, 0
  2d:	000025        	call8	30 <c_fill_null+0x10>
  30:	f01d      	retw.n
	...

00000034 <c_scan_first_nonzero>:
  34:	004136        	entry	a1, 32
  37:	028d      	mov.n	a8, a2
  39:	032d      	mov.n	a2, a3
  3b:	339c      	beqz.n	a3, 52 <c_scan_first_nonzero+0x1e>
  3d:	0a0c      	movi.n	a10, 0
  3f:	039d      	mov.n	a9, a3
  41:	078976        	loop	a9, 4c <c_scan_first_nonzero+0x18>
  44:	08b8      	l32i.n	a11, a8, 0
  46:	884b      	addi.n	a8, a8, 4
  48:	4bcc      	bnez.n	a11, 50 <c_scan_first_nonzero+0x1c>
  4a:	aa1b      	addi.n	a10, a10, 1
  4c:	000086        	j	52 <c_scan_first_nonzero+0x1e>
  4f:	00          	.byte	00
  50:	0a2d      	mov.n	a2, a10
  52:	f01d      	retw.n

00000054 <c_copy_values>:
  54:	004136        	entry	a1, 32
  57:	949c      	beqz.n	a4, 74 <c_copy_values+0x20>
  59:	1184d0        	slli	a8, a4, 3
  5c:	f8c882        	addi	a8, a8, -8
  5f:	418380        	srli	a8, a8, 3
  62:	01c882        	addi	a8, a8, 1
  65:	0b8876        	loop	a8, 74 <c_copy_values+0x20>
  68:	03a8      	l32i.n	a10, a3, 0
  6a:	13b8      	l32i.n	a11, a3, 4
  6c:	02a9      	s32i.n	a10, a2, 0
  6e:	12b9      	s32i.n	a11, a2, 4
  70:	338b      	addi.n	a3, a3, 8
  72:	228b      	addi.n	a2, a2, 8
  74:	f01d      	retw.n
	...

00000078 <run_all>:
  78:	004136        	entry	a1, 32
  7b:	04bd      	mov.n	a11, a4
  7d:	02ad      	mov.n	a10, a2
  7f:	000025        	call8	80 <run_all+0x8>
  82:	04bd      	mov.n	a11, a4
  84:	03ad      	mov.n	a10, a3
  86:	000025        	call8	88 <run_all+0x10>
  89:	04bd      	mov.n	a11, a4
  8b:	03ad      	mov.n	a10, a3
  8d:	000025        	call8	90 <run_all+0x18>
  90:	0abd      	mov.n	a11, a10
  92:	0000a1        	l32r	a10, fffc0094 <run_all+0xfffc001c>
  95:	0c0c      	movi.n	a12, 0
  97:	000025        	call8	98 <run_all+0x20>
  9a:	04cd      	mov.n	a12, a4
  9c:	02bd      	mov.n	a11, a2
  9e:	20a330        	or	a10, a3, a3
  a1:	000025        	call8	a4 <run_all+0x2c>
  a4:	f01d      	retw.n

`0x11a` の 2 バイトは `.byte 00`（アセンブラが分岐緩和のために置いた埋め草で、`j` の後ろなので実行されない）。
`.iram1` のアラインメントは `2**2`（`.align 4` どおり）、`.rodata` も literal pool も無い。
```

## 7. 命令ごとの TRM 出典（`data/pie_instructions.json` の `source_page` と Operation）

この 4 カーネルが実際に発行する 12 命令。`manual == toolchain` は `tools/asm_toolchain.py
--check-instruction` がマニュアルのビット図とツールチェーンのエンコードを突き合わせた結果で、各命令
2 ケース（即値範囲の両端）を回している:

| 命令 | TRM ページ | アセンブラ構文 | Operation（`data/pie_instructions.json` より） | マニュアル vs ツールチェーン |
|---|---|---|---|---|
| `EE.VLD.128.IP` | p164 | `EE.VLD.128.IP qu, as, -2048..2032` | qu[127:0] = load128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} | match（e38064==e38064 937f34==937f34） |
| `EE.VST.128.IP` | p275 | `EE.VST.128.IP qv, as, -2048..2032` | qv[127:0] => store128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} | match（ea8064==ea8064 9a7f34==9a7f34） |
| `EE.VLD.L.64.IP` | p168 | `EE.VLD.L.64.IP qu, as, -1024..1016` | qu[ 63: 0] = load64({as[31:3],3{0}}); as[31:0] = as[31:0] + {21{imm8[7]},imm8[7:0],3{0}} | match（e98064==e98064 997f34==997f34） |
| `EE.VST.L.64.IP` | p279 | `EE.VST.L.64.IP qv, as, -1024..1016` | qv[ 63: 0] => store64({as[31:3],3{0}}); as[31:0] = as[31:0] + {21{imm8[7]},imm8[7:0],3{0}} | match（e48064==e48064 947f34==947f34） |
| `EE.MOVI.32.Q` | p119 | `EE.MOVI.32.Q qu, as, 0..3` | if sel4 == 0:; qu[ 31: 0] = as; if sel4 == 1:; qu[ 63: 32] = as; if sel4 == 2:; qu[ 95: 64] = as; if sel4 == 3:; qu[127: 96] = as | match（edba64==edba64 dd3634==dd3634） |
| `EE.ZERO.Q` | p299 | `EE.ZERO.Q qa` | qa = 0 | match（edffa4==edffa4 dd7fa4==dd7fa4） |
| `EE.NOTQ` | p120 | `EE.NOTQ qa, qx` | qa = ~qx | match（edffc4==edffc4 dd7f54==dd7f54） |
| `EE.ORQ` | p121 | `EE.ORQ qa, qx, qy` | qa = qx \| qy | match（edfce4==edfce4 dd7854==dd7854） |
| `EE.ZERO.ACCX` | p298 | `EE.ZERO.ACCX` | ACCX = 0 | match（250804==250804 250804==250804） |
| `EE.VMULAS.U16.ACCX` | p240 | `EE.VMULAS.U16.ACCX qx, qy` | add0[31:0] = qx[ 15: 0] * qy[ 15: 0]; add1[31:0] = qx[ 31: 16] * qy[ 31: 16]; ...; add7[31:0] = qx[127:112] * qy[127:112]; sum[40:0] = ACCX[39:0] + ad[31:0]d0[31:0] + ad[31:0]d1[31:0] + ... + ad[31:0]d 7[31:0]; 7 ACCX[39:0] = min(max(sum[40:0], 0), 2^{40}-1) | match（0a5584==0a5584 0a1a84==0a1a84） |
| `EE.SRS.ACCX` | p134 | `EE.SRS.ACCX au, as, 0` | temp_shf[39:0] = ACCX[39:0] >> as[5:0]; ACCX = temp_shf[39:0]; au = min(max(temp_shf[39:0], -2^{31}), 2^{31}-1) | match（7e1564==7e1564 7e1234==7e1234） |
| `EE.VCMP.EQ.S32` | p156 | `EE.VCMP.EQ.S32 qa, qx, qy` | qa[ 31: 0] = (qx[ 31: 0]==qy[ 31: 0]) ? 0x{}FFFFFFFF : 0; qa[ 63: 32] = (qx[ 63: 32]==qy[ 63: 32]) ? 0x{}FFFFFFFF : 0; ...; qa[127: 96] = (qx[127: 96]==qy[127: 96]) ? 0x{}FFFFFFFF : 0 | match（aedea4==aedea4 9e43a4==9e43a4） |

```
EE.VLD.128.IP|164|match|2|all match|e38064==e38064 937f34==937f34|EE.VLD.128.IP q5, a6, -2048|EE.VLD.128.IP q2, a3, 2032
EE.VST.128.IP|275|match|2|all match|ea8064==ea8064 9a7f34==9a7f34|EE.VST.128.IP q5, a6, -2048|EE.VST.128.IP q2, a3, 2032
EE.VLD.L.64.IP|168|match|2|all match|e98064==e98064 997f34==997f34|EE.VLD.L.64.IP q5, a6, -1024|EE.VLD.L.64.IP q2, a3, 1016
EE.VST.L.64.IP|279|match|2|all match|e48064==e48064 947f34==947f34|EE.VST.L.64.IP q5, a6, -1024|EE.VST.L.64.IP q2, a3, 1016
EE.MOVI.32.Q|119|match|2|all match|edba64==edba64 dd3634==dd3634|EE.MOVI.32.Q q5, a6, 2|EE.MOVI.32.Q q2, a3, 1
EE.ZERO.Q|299|match|2|all match|edffa4==edffa4 dd7fa4==dd7fa4|EE.ZERO.Q q5|EE.ZERO.Q q2
EE.NOTQ|120|match|2|all match|edffc4==edffc4 dd7f54==dd7f54|EE.NOTQ q5, q6|EE.NOTQ q2, q3
EE.ORQ|121|match|2|all match|edfce4==edfce4 dd7854==dd7854|EE.ORQ q5, q6, q7|EE.ORQ q2, q3, q4
EE.ZERO.ACCX|298|match|2|all match|250804==250804 250804==250804|EE.ZERO.ACCX |EE.ZERO.ACCX 
EE.VMULAS.U16.ACCX|240|match|2|all match|0a5584==0a5584 0a1a84==0a1a84|EE.VMULAS.U16.ACCX q5, q6|EE.VMULAS.U16.ACCX q2, q3
EE.SRS.ACCX|134|match|2|all match|7e1564==7e1564 7e1234==7e1234|EE.SRS.ACCX a5, a6, 0|EE.SRS.ACCX a2, a3, 0
EE.VCMP.EQ.S32|156|match|2|all match|aedea4==aedea4 9e43a4==9e43a4|EE.VCMP.EQ.S32 q5, q6, q7|EE.VCMP.EQ.S32 q2, q3, q4

total comparisons: 24  all match: True
```

`movnez` / `min` / `extui` / `slli` / `l32i` / `s32i` は PIE ではなく Xtensa コアの命令なので
`data/pie_instructions.json`（220 命令）には入っていない。オペランド順は §10 でツールチェーンの
コード生成から固定した。

`EE.SRS.ACCX au, as, 0` の第 3 オペランドはマニュアル固定の即値 `0` で、命令語に対応するフィールドを
持たない（`tools/asm_toolchain.py` の `match_operands` がそう扱う）。シフト量は `as[5:0]` から来る
（本作は `a5 = 0` を渡す）。

## 8. Python モデルとホスト C 参照の等価実行

方針: **モデルは `.S` を読んで命令を 1 つずつ実行する**（写経ではない）。
`/tmp/ex21_model.py` は `ex21_vmframe.S` のテキストを解析し、その中に出てくるニーモニックだけを実装し、
メモリは「ハードウェアと同じく下位ビットを落とす」形で持つ。だから**ポインタの歩みと整列の扱いまで
含めて**、リポジトリにあるファイルそのものが検査対象になる。C 側は仕様（`JS_UNDEFINED` は
`(3<<32)|0`、NULL 充填、最初の非ゼロ語、要素ごとのコピー）で、ホストの `gcc -O2` でビルドする。

`/tmp/ex21_model.py`:

```python
#!/usr/bin/env python3
"""Instruction-level model of examples/firmware/main/proposed/ex21_vmframe.S.

This is NOT a transcription of the kernels: it reads the .S file, decodes the mnemonics that file
contains, and executes them one at a time against a flat address space. So the thing being checked is
the instruction stream that is actually in the repository, including its pointer walk.

Semantics come from data/pie_instructions.json (the TRM's Operation pseudo-code) and from the TRM's
address rule for the 128-bit forms: {as[31:4], 4{0}}, i.e. THE LOW FOUR ADDRESS BITS ARE DROPPED, and
the same rule with 3 bits for the 64-bit forms. Memory is a bytearray, so a store off the grid corrupts
whatever really lives there -- which is exactly what the misaligned cases are for.

Supported mnemonics (everything the file uses, plus the .n-relaxed spellings the assembler may emit):
    entry, retw.n, mov, movi, addi, sub, srli, extui, movnez, j, beqz, bnez,
    l32i, s32i,
    ee.vld.128.ip, ee.vst.128.ip, ee.vld.l.64.ip, ee.vst.l.64.ip,
    ee.zero.q, ee.notq, ee.orq, ee.vcmp.eq.s32,
    ee.zero.accx, ee.vmulas.u16.accx, ee.srs.accx, ee.movi.32.q
Anything else raises NotImplementedError, on purpose.
"""
import re

MASK32 = 0xFFFFFFFF
MASK40 = (1 << 40) - 1
MASK128 = (1 << 128) - 1


def _strip(text):
    return re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)


def parse(path):
    """Return (program, labels). A program entry is ('lbl', name) or ('op', mnemonic, [operands])."""
    prog, labels = [], {}
    for raw in _strip(open(path, encoding='utf-8').read()).splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z_.$][A-Za-z0-9_.$]*):\s*(.*)$', line)
        if m:                                   # a label (may be followed by an instruction)
            labels[m.group(1)] = len(prog)
            prog.append(('lbl', m.group(1)))
            line = m.group(2).strip()
            if not line:
                continue
        if line.startswith('.'):                # a directive (.section/.align/.global/.type/.size)
            continue
        parts = line.split(None, 1)
        op = parts[0].lower()
        ops = [x.strip() for x in parts[1].split(',')] if len(parts) > 1 else []
        prog.append(('op', op, ops))
    return prog, labels


class CPU:
    def __init__(self, prog, labels, mem):
        self.prog, self.labels, self.mem = prog, labels, mem
        self.a = [0] * 16
        self.q = [0] * 8
        self.accx = 0
        self.trace = []                 # ('ld'|'st', address as the bus sees it, nbytes)

    # ---------------------------------------------------------------- memory, with the address rules
    def ld(self, addr, nbytes):
        if nbytes == 16:
            addr &= ~15
        elif nbytes == 8:
            addr &= ~7
        elif nbytes == 4:
            addr &= ~3
        elif nbytes == 2:
            addr &= ~1
        self.trace.append(('ld', addr, nbytes))
        v = 0
        for i in range(nbytes):
            v |= self.mem[addr + i] << (8 * i)
        return v

    def st(self, addr, nbytes, value):
        if nbytes == 16:
            addr &= ~15
        elif nbytes == 8:
            addr &= ~7
        elif nbytes == 4:
            addr &= ~3
        elif nbytes == 2:
            addr &= ~1
        self.trace.append(('st', addr, nbytes))
        for i in range(nbytes):
            self.mem[addr + i] = (value >> (8 * i)) & 0xFF

    # ---------------------------------------------------------------- lane helpers (32-bit segments)
    def seg(self, qi, s):
        return (self.q[qi] >> (32 * s)) & MASK32

    def setseg(self, qi, s, v):
        self.q[qi] = (self.q[qi] & ~(MASK32 << (32 * s)) | ((v & MASK32) << (32 * s))) & MASK128

    @staticmethod
    def s32(v):
        return v - (1 << 32) if v & (1 << 31) else v

    # ---------------------------------------------------------------- run
    def run(self, entry, args, max_steps=200000):
        pc = self.labels[entry]
        for i, v in enumerate(args):
            self.a[2 + i] = v & MASK32
        steps = 0
        while True:
            if steps > max_steps:
                raise RuntimeError('step limit: probably a runaway loop')
            kind, *rest = self.prog[pc]
            pc += 1
            if kind == 'lbl':
                continue
            op, ops = rest
            steps += 1
            a = self.a

            def reg(t):                       # an AR operand or a hex/decimal literal
                t = t.strip()
                if t.startswith('a') and t[1:].isdigit():
                    return a[int(t[1:])]
                return int(t, 0) & MASK32

            def Q(t):
                return self.q[int(t.strip()[1:])]

            if op in ('entry', 'nop', 'nop.n'):
                continue
            if op == 'retw.n' or op == 'retw':
                return steps
            if op in ('mov', 'mov.n'):
                a[int(ops[0][1:])] = reg(ops[1])
                continue
            if op in ('movi', 'movi.n'):
                a[int(ops[0][1:])] = int(ops[1], 0) & MASK32
                continue
            if op in ('addi', 'addi.n'):
                a[int(ops[0][1:])] = (reg(ops[1]) + int(ops[2], 0)) & MASK32
                continue
            if op == 'sub':
                a[int(ops[0][1:])] = (reg(ops[1]) - reg(ops[2])) & MASK32
                continue
            if op == 'srli':
                a[int(ops[0][1:])] = (reg(ops[1]) >> int(ops[2], 0)) & MASK32
                continue
            if op == 'slli':
                a[int(ops[0][1:])] = (reg(ops[1]) << int(ops[2], 0)) & MASK32
                continue
            if op == 'extui':                 # extui aR, aS, shift, width
                v = (reg(ops[1]) >> int(ops[2], 0)) & ((1 << int(ops[3], 0)) - 1)
                a[int(ops[0][1:])] = v
                continue
            if op == 'movnez':                # if the THIRD operand != 0 then the first gets the second
                if reg(ops[2]) != 0:
                    a[int(ops[0][1:])] = reg(ops[1])
                continue
            if op == 'j':
                pc = self.labels[ops[0]]
                continue
            if op in ('beqz', 'beqz.n'):
                if reg(ops[0]) == 0:
                    pc = self.labels[ops[1]]
                continue
            if op in ('bnez', 'bnez.n'):
                if reg(ops[0]) != 0:
                    pc = self.labels[ops[1]]
                continue
            if op in ('l32i', 'l32i.n'):
                a[int(ops[0][1:])] = self.ld((reg(ops[1]) + int(ops[2], 0)) & MASK32, 4)
                continue
            if op in ('s32i', 's32i.n'):
                self.st((reg(ops[1]) + int(ops[2], 0)) & MASK32, 4, reg(ops[0]))
                continue

            # ------------------------------------------------------------------ the PIE instructions
            if op == 'ee.vld.128.ip':
                self.q[int(ops[0][1:])] = self.ld(reg(ops[1]), 16)
                a[int(ops[1][1:])] = (reg(ops[1]) + int(ops[2], 0)) & MASK32
                continue
            if op == 'ee.vst.128.ip':
                self.st(reg(ops[1]), 16, Q(ops[0]))
                a[int(ops[1][1:])] = (reg(ops[1]) + int(ops[2], 0)) & MASK32
                continue
            if op == 'ee.vld.l.64.ip':
                qn = int(ops[0][1:])
                self.q[qn] = (Q(ops[0]) & ~((1 << 64) - 1) | self.ld(reg(ops[1]), 8)) & MASK128
                a[int(ops[1][1:])] = (reg(ops[1]) + int(ops[2], 0)) & MASK32
                continue
            if op == 'ee.vst.l.64.ip':
                self.st(reg(ops[1]), 8, Q(ops[0]) & ((1 << 64) - 1))
                a[int(ops[1][1:])] = (reg(ops[1]) + int(ops[2], 0)) & MASK32
                continue
            if op == 'ee.zero.q':
                self.q[int(ops[0][1:])] = 0
                continue
            if op == 'ee.notq':
                self.q[int(ops[0][1:])] = (~Q(ops[1])) & MASK128
                continue
            if op == 'ee.orq':
                self.q[int(ops[0][1:])] = Q(ops[1]) | Q(ops[2])
                continue
            if op == 'ee.andq':
                self.q[int(ops[0][1:])] = Q(ops[1]) & Q(ops[2])
                continue
            if op == 'ee.vcmp.eq.s32':
                v = 0
                for s in range(4):
                    if self.seg(int(ops[1][1:]), s) == self.seg(int(ops[2][1:]), s):
                        v |= MASK32 << (32 * s)
                self.q[int(ops[0][1:])] = v
                continue
            if op == 'ee.movi.32.q':
                self.setseg(int(ops[0][1:]), int(ops[2], 0), reg(ops[1]))
                continue
            if op == 'ee.zero.accx':
                self.accx = 0
                continue
            if op == 'ee.vmulas.u16.accx':
                x, y = Q(ops[0]), Q(ops[1])
                total = self.accx
                for i in range(8):
                    total += ((x >> (16 * i)) & 0xFFFF) * ((y >> (16 * i)) & 0xFFFF)
                self.accx = min(max(total, 0), MASK40)
                continue
            if op == 'ee.srs.accx':
                t = self.accx >> (reg(ops[1]) & 63)
                self.accx = t & MASK40
                a[int(ops[0][1:])] = min(max(t, -(1 << 31)), (1 << 31) - 1) & MASK32
                continue
            raise NotImplementedError(op)
```

`/tmp/ex21_ref.c`（ホスト C 参照。配列の前後にガード語を置き、**配列外への書き込みが不一致として
出る**ようにしてある）:

```c
/* Host C reference for ex21_vmframe.S's four kernels, at -O2 (the build's rule for C references:
 * examples/firmware/main/CMakeLists.txt adds -O2 for exactly this reason).
 *
 * The arithmetic here is the *specification*, not a second reading of the assembly: JS_UNDEFINED with
 * NAN boxing is (3 << 32) | 0 (quickjs.h:175, :249, :408), a JSVarRef* fill is NULL, "first non-zero
 * word" is a loop, and the copy is element by element.
 *
 * Every case gets an ARENA: 4 guard words, the array, 4 guard words. The guards are part of the
 * comparison, so an out-of-array write is a mismatch rather than a silent pass.
 *
 * Protocol: one case per line on stdin, result per line on stdout.
 *   A  <n> <2n words>   var_buf fill (ex21_fill_undefined,      JS_UNDEFINED, pointer constant)
 *   A2 <n> <2n words>   the same fill  (ex21_fill_undefined_inline)
 *   B  <n> <n words>    pointer fill   (ex21_fill_null)
 *   C  <n> <n words>    scan           (ex21_scan_first_nonzero)   -> prints the index
 *   D  <n> <2n words>   copy           (ex21_copy_values)
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXW 8192
#define GUARD_LO 0xAAAAAAAAu
#define GUARD_HI 0xBBBBBBBBu

static _Alignas(16) uint32_t dst_arena[4 + MAXW + 4];
static _Alignas(16) uint32_t src_arena[4 + MAXW + 4];

static void c_fill_undefined(uint64_t *v, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++)
        v[i] = ((uint64_t)3 << 32) | 0;          /* JS_UNDEFINED (NAN boxing) */
}

static void c_fill_null(uint32_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++)
        p[i] = 0;
}

static uint32_t c_scan_first_nonzero(const uint32_t *p, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++)
        if (p[i] != 0)
            return i;
    return n;
}

static void c_copy_values(uint64_t *dst, const uint64_t *src, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++)
        dst[i] = src[i];
}

static void gua_lo(uint32_t *w) { for (int i = 0; i < 4; i++) w[i] = GUARD_LO; }
static void gua_hi(uint32_t *w, int nw) { for (int i = 0; i < 4; i++) w[nw + 4 + i] = GUARD_HI; }

int main(void)
{
    static char line[1 << 20];
    while (fgets(line, sizeof line, stdin)) {
        char kind[8];
        unsigned n;
        char *p = line;
        int used = 0;
        if (sscanf(p, "%7s %u%n", kind, &n, &used) != 2)
            continue;
        p += used;
        unsigned nw = (!strcmp(kind, "A") || !strcmp(kind, "A2") || !strcmp(kind, "D")) ? 2u * n : n;
        gua_lo(dst_arena);
        gua_hi(dst_arena, nw);
        for (unsigned i = 0; i < nw; i++) {
            unsigned long v = strtoul(p, &p, 16);
            dst_arena[4 + i] = (uint32_t)v;
        }
        if (!strcmp(kind, "A")) {
            c_fill_undefined((uint64_t *)(dst_arena + 4), n);
            printf("A %u", n);
            for (unsigned i = 0; i < nw + 8; i++) printf(" %08x", dst_arena[i]);
            printf("\n");
        } else if (!strcmp(kind, "A2")) {
            c_fill_undefined((uint64_t *)(dst_arena + 4), n);
            printf("A2 %u", n);
            for (unsigned i = 0; i < nw + 8; i++) printf(" %08x", dst_arena[i]);
            printf("\n");
        } else if (!strcmp(kind, "B")) {
            c_fill_null(dst_arena + 4, n);
            printf("B %u", n);
            for (unsigned i = 0; i < nw + 8; i++) printf(" %08x", dst_arena[i]);
            printf("\n");
        } else if (!strcmp(kind, "C")) {
            uint32_t r = c_scan_first_nonzero(dst_arena + 4, n);
            printf("C %u %u\n", n, r);
        } else if (!strcmp(kind, "D")) {
            gua_lo(src_arena);
            gua_hi(src_arena, nw);
            for (unsigned i = 0; i < nw; i++) src_arena[4 + i] = dst_arena[4 + i];
            memset(dst_arena + 4, 0x5A, 4 * (size_t)nw);
            c_copy_values((uint64_t *)(dst_arena + 4), (const uint64_t *)(src_arena + 4), n);
            printf("D %u", n);
            for (unsigned i = 0; i < nw + 8; i++) printf(" %08x", dst_arena[i]);
            printf("\n");
        }
    }
    return 0;
}
```

`/tmp/ex21_run.py`（ケース生成・モデルと C の突き合わせ・ポインタ歩みと配列外アクセスの検査）:

```python
#!/usr/bin/env python3
"""Drive ex21_vmframe.S's four kernels through ex21_model.py and compare with the C reference.

  python3 ex21_run.py            # the equivalence sweep (aligned cases)
  python3 ex21_run.py misalign   # the misalignment demonstrations
"""
import random
import subprocess
import sys

sys.path.insert(0, '/tmp')
from ex21_model import parse, CPU                                   # noqa: E402

ASM = '/workspace/esp32s3-hw-mcp/examples/firmware/main/proposed/ex21_vmframe.S'
REF_C = '/tmp/ex21_ref.c'
REF_BIN = '/tmp/ex21_ref'
ARENA, SRC_ARENA = 0x10000, 0x20000
ARR = ARENA + 16                    # the array always starts after 4 guard words
SRC_ARR = SRC_ARENA + 16
CONST = 0x30000                     # the caller's 2 x JS_UNDEFINED (16 bytes, 16-byte aligned)
GLO, GHI, PREFILL = 0xAAAAAAAA, 0xBBBBBBBB, 0x5A5A5A5A

PROG, LABELS = parse(ASM)


def put32(mem, addr, v):
    for i in range(4):
        mem[addr + i] = (v >> (8 * i)) & 0xFF


def put_words(mem, addr, ws):
    for i, v in enumerate(ws):
        put32(mem, addr + 4 * i, v)


def get_words(mem, addr, count):
    out = []
    for i in range(count):
        v = 0
        for b in range(4):
            v |= mem[addr + 4 * i + b] << (8 * b)
        out.append(v)
    return out


def fresh():
    mem = bytearray(1 << 20)
    put_words(mem, CONST, [0, 3, 0, 3])          # 2 x JS_UNDEFINED, NAN boxing: (3 << 32) | 0
    return CPU(PROG, LABELS, mem)


def model_case(kind, n, words):
    """Returns (result_words, final_a2). Mirrors ex21_ref.c's arena exactly."""
    cpu = fresh()
    mem = cpu.mem
    nw = 2 * n if kind in ('A', 'A2', 'D') else n
    put_words(mem, ARENA, [GLO] * 4)
    put_words(mem, ARR + 4 * nw, [GHI] * 4)
    if kind in ('A', 'A2', 'B'):
        put_words(mem, ARR, words)
        entry = {'A': 'ex21_fill_undefined', 'A2': 'ex21_fill_undefined_inline',
                 'B': 'ex21_fill_null'}[kind]
        cpu.run(entry, [ARR, n, CONST])
        return get_words(mem, ARENA, nw + 8), cpu.a[2], cpu.a[3]
    if kind == 'C':
        put_words(mem, ARR, words)
        cpu.run('ex21_scan_first_nonzero', [ARR, n])
        return get_words(mem, ARENA, nw + 8), cpu.a[2], cpu.trace[:]
    if kind == 'D':
        put_words(mem, SRC_ARENA, [GLO] * 4)
        put_words(mem, SRC_ARR, words)
        put_words(mem, SRC_ARR + 4 * nw, [GHI] * 4)
        put_words(mem, ARR, [PREFILL] * nw)
        cpu.run('ex21_copy_values', [ARR, SRC_ARR, n])
        return get_words(mem, ARENA, nw + 8), cpu.a[2], cpu.a[3]
    raise ValueError(kind)


def cases():
    rnd = random.Random(20260915)
    out = []
    sizes_a = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 23, 24, 31, 32, 33, 47, 63, 64, 65, 100]
    sizes_a += [rnd.randrange(0, 400) for _ in range(400)]
    for n in sizes_a:
        ws = [rnd.getrandbits(32) for _ in range(2 * n)]
        if n and rnd.random() < .5:
            ws[0], ws[1] = 0, 3                          # a slot that is already JS_UNDEFINED
        out += [('A', n, ws), ('A2', n, list(ws)), ('D', n, list(ws))]
    sizes_b = list(range(0, 13)) + [15, 16, 17, 23, 24, 25, 31, 32, 33, 100, 101]
    sizes_b += [rnd.randrange(0, 400) for _ in range(200)]
    for n in sizes_b:
        out.append(('B', n, [rnd.getrandbits(32) for _ in range(n)]))
    sizes_c = list(range(0, 22)) + [31, 32, 33, 47, 48, 49, 63, 64, 65, 66, 100, 127, 128, 129, 255, 256, 257]
    sizes_c += [rnd.randrange(0, 600) for _ in range(150)]
    for n in sizes_c:
        z = [0] * n
        out.append(('C', n, list(z)))                                   # all zero: the natural case
        for k in range(n):                                              # a single non-zero word
            if n > 64 and k not in (0, 1, 2, 3, 4, 7, 8, 15, 16, 17, 31, 32, 47, 48, 63, 64, n - 1):
                continue
            w = list(z)
            w[k] = rnd.choice([1, 0x80000000, 0xFFFFFFFF, 0x00010000 if k % 2 else 0x00000001])
            out.append(('C', n, w))
        if n:
            out.append(('C', n, [0xFFFFFFFF] * n))                       # everything non-zero
            for _ in range(3):
                out.append(('C', n, [0 if rnd.random() < .9 else rnd.getrandbits(32) for _ in range(n)]))
                out.append(('C', n, [rnd.getrandbits(1) for _ in range(n)]))
    return out


def c_line(kind, n, words):
    return "%s %u %s" % (kind, n, " ".join("%08x" % w for w in words))


def main():
    subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-o", REF_BIN, REF_C], check=True)
    cs = cases()
    stdin = "".join(c_line(*c) + "\n" for c in cs)
    ref = subprocess.run([REF_BIN], input=stdin, capture_output=True, text=True, check=True).stdout.splitlines()
    assert len(ref) == len(cs), (len(ref), len(cs))

    cases_run = mismatches = out_of_bounds = 0
    first = None
    per_kind = {}
    walk_checks = []
    for (kind, n, words), line in zip(cs, ref):
        tok = line.split()
        rk, rn, rest = tok[0], int(tok[1]), tok[2:]
        assert rk == kind and rn == n
        got, a2, extra = model_case(kind, n, words)
        cases_run += 1
        # the alignment contract, checked on the ALIGNED cases: no access may leave the array. A/B/D
        # are covered by the guard words in `got`; C writes nothing, so its loads are checked here.
        if kind == 'C':
            for _k, a, nb in extra:
                if not (ARR <= a and a + nb <= ARR + 4 * n):
                    out_of_bounds += 1
                    break
        walk_checks.append((kind, n, a2))
        per_kind[kind] = per_kind.get(kind, 0) + 1
        if kind == 'C':
            want = int(rest[0])          # the C prints the index in decimal
            ok = (a2 == want) and (got == [GLO] * 4 + words + [GHI] * 4)
            want_repr, got_repr = "index %d" % want, "index %d" % a2
        else:
            want = [int(x, 16) for x in rest]
            ok = (got == want)
            if kind in ('A', 'A2') and n:
                ok = ok and (got[4:4 + 2 * n] == [0, 3] * n)     # JS_UNDEFINED really landed
            want_repr = " ".join("%08x" % w for w in want[:18])
            got_repr = " ".join("%08x" % w for w in got[:18])
        if not ok:
            mismatches += 1
            if first is None:
                first = (kind, n, words, want_repr, got_repr)
    bad_walk = 0
    for kind, n, a2 in walk_checks:
        if kind in ('A', 'A2'):
            want = ARR + 16 * (n // 2) + (8 if n % 2 else 0)
        elif kind == 'B':
            want = ARR + 4 * n
        elif kind == 'D':
            want = ARR + 16 * (n // 2) + (8 if n % 2 else 0)
        else:
            continue
        bad_walk += (a2 != want)
    print("cases run                 :", cases_run)
    print("A/B/D pointer walk wrong  :", bad_walk, "(the .IP increments must land exactly on")
    print("                            var_buf/var_refs/dst + the bytes written)")
    print("C cases reading outside   :", out_of_bounds, "of the caller's array (aligned cases only)")
    print("  per kernel              :", per_kind)
    print("mismatches (model vs C)   :", mismatches)
    if first:
        kind, n, words, want_repr, got_repr = first
        print("first mismatch            : kernel", kind, "n =", n)
        print("  input words             :", " ".join("%08x" % w for w in words[:16]))
        print("  C reference             :", want_repr)
        print("  model                   :", got_repr)
    else:
        print("first mismatch            : none")
    return 0 if not mismatches else 1


if __name__ == '__main__':
    sys.exit(main())
```

実行（`/tmp/ex21_run.txt`。`gcc -O2` で C 参照をビルドし、6437 ケースを両方で走らせる）:

```
cases run                 : 6437
A/B/D pointer walk wrong  : 0 (the .IP increments must land exactly on
                            var_buf/var_refs/dst + the bytes written)
C cases reading outside   : 0 of the caller's array (aligned cases only)
  per kernel              : {'A': 427, 'A2': 427, 'D': 427, 'B': 224, 'C': 4932}
mismatches (model vs C)   : 0
first mismatch            : none
```

**モデルが捕まえた実バグ（1 件）**: 最初の実装では段 1 が当たったとき `a2` が 64 バイト進んだまま
段 2 に入っており、`n_words = 16`・語 0 が非ゼロのケースで「見つからない（16）」を返していた
（段 2 が**次の** 64 バイトを走査していた）。モデルは書き上げた直後の掃引でこれを落とした。修正は
`.Lsc_hot` の `addi a2, a2, -64` 1 命令である（この経緯は `.S` のコメントにも書いてある）。

### 8.1 整列契約の検査

* **A/A2/B/D のポインタ歩み**: `.IP` の後置インクリメントが「書いた範囲の直後」に来ることを
  カーソル値で検査 → **不一致 0**（A 427 + A2 427 + D 427 + B 224 ケース）。
* **C の読み出し範囲**: 整列入力の 4932 ケースで、**配列の外を読んだアクセスは 0 件**。
* **配列外への書き込み**: ガード語が比較に入っているので、128bit/64bit ストアが 1 バイトでも外に出れば
  不一致になる → **0 件**。

## 9. piesim.py（第三実装）による解釈実行

`/workspace/pjs-vm/tools/pie/piesim.py` を**無改変でコピーして**走らせた（sha256 一致:
`8b016191243e58a472310b41ec0a413ae4fb080baadcbed36081f55c3f3f5a88`）。piesim は別プロジェクトが
TRM の疑似コードから書いた命令レベル解釈実行器で、**この文書の中で唯一「私が書いていない」128bit
メモリ意味論**である。その docstring 自身が「Alignment is *forced* the way the hardware does it (low
address bits cleared), so a misaligned pointer silently reads the wrong place here too」と書いている。

piesim が知っているのは `mov/addi/bnez/loopgtz/wsr.sar` と `ee.vld.128.ip` / `ee.vst.128.ip` /
`ee.vld.l.64.ip` / `ee.zero.q` などで、**`ee.vst.l.64.ip`・`s32i`・`extui`・`srli`・ACCX 系
（`ee.zero.accx` / `ee.vmulas.u16.accx` / `ee.srs.accx`）・`ee.vcmp.eq.s32` は持っていない**。
したがって:

* 契約の実演（下の PROBE）は**無改変 piesim でそのまま**。
* ループ本体（A/B/D のグループループ）は piesim の命令部分集合に書き下した**同じ命令列**を走らせて
  いる。ループ回数のシフトは C 側（`+r` オペランド）で計算している — そこは piesim に `srli` が
  無いためで、検査したい中身ではない。
* C のカーネル（ACCX と比較）は piesim では走らない。ex21 のモデル（§8）が担当する。

`/tmp/piesim_ex21.c`:

```c
/* Harness for the /tmp copy of pjs-vm's tools/pie/piesim.py (an instruction-level interpreter written
 * by another project). It exists to put a THIRD implementation of the PIE semantics -- not mine -- under
 * two claims ex21_vmframe.S makes:
 *
 *   1. a 128-bit store forms its address as {as[31:4],4{0}}: a destination off the 16-byte grid is
 *      written 4 bytes early and the 4 bytes before it are destroyed, with no fault;
 *   2. the group loops of the kernels store exactly 16 bytes per group, at the addresses the contract
 *      says, and nothing outside them.
 *
 * The instruction text below is the loop body of the corresponding kernel in ex21_vmframe.S, spelled in
 * piesim's instruction subset: it knows mov/addi/bnez/loopgtz for the core and ee.vld.128.ip /
 * ee.vst.128.ip / ee.vld.l.64.ip / ee.zero.q for the vector unit. It does NOT know beqz, extui, srli,
 * s32i, ee.vst.l.64.ip or the ACCX/compare machinery ex21_scan_first_nonzero needs, so:
 *   - the shifts that compute the loop counts are done by the C caller, not inside the asm (that is what
 *     the "+r" operands below are for);
 *   - the odd-slot tails and ex21_scan_first_nonzero are NOT covered here; they are covered by ex21's
 *     own model (ex21_model.py), which decodes the .S itself.
 */

/* 1. ex21_fill_null's group loop: groups = n_ptrs >> 2, one 16-byte store per group. */
void piesim_fill_null_groups(char *ptrs, int groups)
{
    __asm__ volatile(
        "  ee.zero.q q0\n"
        "  loopgtz %[g], 9f\n"
        "  ee.vst.128.ip q0, %[p], 16\n"
        "9:\n"
        : [p] "+r"(ptrs), [g] "+r"(groups)
        :
        : "memory");
}

/* 2. ex21_fill_undefined's group loop: groups = n_values >> 1, the constant from the caller's pointer. */
void piesim_fill_undefined_groups(char *var_buf, int groups, const char *undef_pair)
{
    __asm__ volatile(
        "  ee.vld.128.ip q0, %[c], 0\n"
        "  loopgtz %[g], 9f\n"
        "  ee.vst.128.ip q0, %[p], 16\n"
        "9:\n"
        : [p] "+r"(var_buf), [g] "+r"(groups)
        : [c] "r"(undef_pair)
        : "memory");
}

/* 3. ex21_copy_values' pair loop: pairs = (n_values >> 1) >> 1, 32 bytes per iteration, two registers. */
void piesim_copy_pairs(char *dst, const char *src, int pairs)
{
    __asm__ volatile(
        "  loopgtz %[g], 9f\n"
        "  ee.vld.128.ip q0, %[s], 16\n"
        "  ee.vld.128.ip q1, %[s], 16\n"
        "  ee.vst.128.ip q0, %[d], 16\n"
        "  ee.vst.128.ip q1, %[d], 16\n"
        "9:\n"
        : [d] "+r"(dst), [g] "+r"(pairs)
        : [s] "r"(src)
        : "memory");
}

/* 4. the contract probe: one 16-byte store of zeros at whatever pointer the caller passes. */
void piesim_probe_store128(char *dst)
{
    __asm__ volatile(
        "  ee.zero.q q0\n"
        "  ee.vst.128.ip q0, %[d], 0\n"
        : [d] "+r"(dst)
        :
        : "memory");
}
```

`/tmp/piesim_ex21.py`（ドライバ。実行した結果がそのまま下に出る）:

```python
#!/usr/bin/env python3
"""Run /tmp/piesim.py (an unmodified copy of /workspace/pjs-vm/tools/pie/piesim.py) on four probes.

piesim is another project's interpreter of the ESP32-S3 PIE instructions, so it is the only *third*
implementation of the 128-bit memory semantics in this exercise: ex21_model.py is mine written from the
TRM pseudo-code, and the kernels are mine too. Its ldq/stq force the low address bits to zero the way the
TRM says the hardware does, and its docstring says so: "Alignment is *forced* the way the hardware does it
(low address bits cleared), so a misaligned pointer silently reads the wrong place here too".
"""
import sys

sys.path.insert(0, '/tmp')
import piesim                                                     # noqa: E402

C = '/tmp/piesim_ex21.c'


def arena(size=0x400):
    """A flat bytearray with 0x11 everywhere, so every written byte is visible."""
    return bytearray([0x11] * size)


def footprint(before, after, base=0):
    ch = [i for i in range(len(before)) if before[i] != after[i]]
    if not ch:
        return "no byte changed"
    return "bytes 0x%03x..0x%03x changed (%d bytes)" % (base + min(ch), base + max(ch), len(ch))


def main():
    print("piesim.py (copy in /tmp, unmodified): %s" % piesim.__file__)
    print()

    # ---- 4. the probe: a 16-byte zero store to 0x84 (off the grid by 4) -------------------------
    mem = arena()
    for i in range(0x80, 0x94):
        mem[i] = 0x22                 # what is in memory before the store
    sim = piesim.Sim(mem)
    sim.run(piesim.extract_asm(C, 'piesim_probe_store128'), {'d': 0x84})
    got = bytes(sim.mem)
    print("PROBE: ee.vst.128.ip q0(zero), as = 0x84")
    print("  the caller asked for 16 bytes at 0x84..0x93 (as[3:0] = 4)")
    print("  the store landed at      : 0x%03x..0x%03x" %
          (min(i for i in range(0x70, 0xa0) if got[i] == 0), max(i for i in range(0x70, 0xa0) if got[i] == 0)))
    print("  0x80..0x83 (before the asked-for range) : %s" % " ".join("%02x" % got[i] for i in range(0x80, 0x84)))
    print("  0x90..0x93 (the asked-for tail)         : %s" % " ".join("%02x" % got[i] for i in range(0x90, 0x94)))
    print("  fault / trap                            : none (piesim has no fault path for this, and the ISA")
    print("                                             has no unaligned 128-bit store to take one)")
    print()

    # ---- 1. ex21_fill_null's group loop -------------------------------------------------------
    mem = arena()
    for i in range(0x40, 0x40 + 48):
        mem[i] = 0x33
    before = bytearray(mem)
    sim = piesim.Sim(mem)
    steps = sim.run(piesim.extract_asm(C, 'piesim_fill_null_groups'), {'p': 0x40, 'g': 3})
    got = bytes(sim.mem)
    print("ex21_fill_null group loop (n_ptrs = 12 -> groups = 3) at 0x40:")
    print("  instructions executed   : %d (3 groups x 1 store + loop control)" % steps)
    print("  %s" % footprint(before[:0x70], got[:0x70]))
    print("  a[2]-style pointer walk : p = 0x%02x (started at 0x40, +16 per group)" % sim.ar['p'])
    print("  all 12 words are zero   : %s" % all(got[i] == 0 for i in range(0x40, 0x70)))
    print()

    # ---- 2. ex21_fill_undefined's group loop --------------------------------------------------
    mem = arena()
    for i in range(0x40, 0x40 + 32):
        mem[i] = 0x33
    const = 0x300                                  # 2 x JS_UNDEFINED, written by hand below
    # exact JS_UNDEFINED pair: slot = (3 << 32) | 0 -> little endian bytes 00 00 00 00 03 00 00 00
    for k in range(2):
        for b in range(8):
            mem[const + 8 * k + b] = (0x0000000300000000 >> (8 * b)) & 0xFF
    before = bytearray(mem)
    sim = piesim.Sim(mem)
    steps = sim.run(piesim.extract_asm(C, 'piesim_fill_undefined_groups'), {'p': 0x40, 'g': 2, 'c': const})
    got = bytes(sim.mem)
    print("ex21_fill_undefined group loop (n_values = 4 -> groups = 2) at 0x40, constant at 0x300:")
    print("  instructions executed   : %d" % steps)
    print("  %s" % footprint(before[:0x70], got[:0x70]))
    print("  the 4 slots read back   : " + " ".join(
        "%016x" % int.from_bytes(got[0x40 + 8 * k:0x48 + 8 * k], 'little') for k in range(4)))
    print("  == JS_UNDEFINED (3<<32) : %s" % all(
        int.from_bytes(got[0x40 + 8 * k:0x48 + 8 * k], 'little') == 0x0000000300000000
        for k in range(4)))
    print()

    # ---- 3. ex21_copy_values' pair loop ------------------------------------------------------
    mem = arena()
    src = 0x80
    for i in range(0x40, 0x80):
        mem[i] = 0x33                                        # prefill the whole 64-byte destination
    before = bytearray(mem)
    sim = piesim.Sim(mem)
    steps = sim.run(piesim.extract_asm(C, 'piesim_copy_pairs'), {'d': 0x40, 's': src, 'g': 2})
    got = bytes(sim.mem)
    print("ex21_copy_values pair loop (32 bytes per iteration, 2 iterations = 64 bytes) at 0x40 <- 0x80:")
    print("  instructions executed   : %d" % steps)
    print("  %s" % footprint(before[:0x90], got[:0x90]))
    print("  d = 0x%02x, s = 0x%02x (each walked +32 per iteration)" % (sim.ar['d'], sim.ar['s']))


if __name__ == '__main__':
    main()
```

実行（`/tmp/piesim_ex21.txt`）:

```
piesim.py (copy in /tmp, unmodified): /tmp/piesim.py

PROBE: ee.vst.128.ip q0(zero), as = 0x84
  the caller asked for 16 bytes at 0x84..0x93 (as[3:0] = 4)
  the store landed at      : 0x080..0x08f
  0x80..0x83 (before the asked-for range) : 00 00 00 00
  0x90..0x93 (the asked-for tail)         : 22 22 22 22
  fault / trap                            : none (piesim has no fault path for this, and the ISA
                                             has no unaligned 128-bit store to take one)

ex21_fill_null group loop (n_ptrs = 12 -> groups = 3) at 0x40:
  instructions executed   : 5 (3 groups x 1 store + loop control)
  bytes 0x040..0x06f changed (48 bytes)
  a[2]-style pointer walk : p = 0x70 (started at 0x40, +16 per group)
  all 12 words are zero   : True

ex21_fill_undefined group loop (n_values = 4 -> groups = 2) at 0x40, constant at 0x300:
  instructions executed   : 4
  bytes 0x040..0x05f changed (32 bytes)
  the 4 slots read back   : 0000000300000000 0000000300000000 0000000300000000 0000000300000000
  == JS_UNDEFINED (3<<32) : True

ex21_copy_values pair loop (32 bytes per iteration, 2 iterations = 64 bytes) at 0x40 <- 0x80:
  instructions executed   : 9
  bytes 0x040..0x07f changed (64 bytes)
  d = 0x80, s = 0xc0 (each walked +32 per iteration)
```

## 10. `movnez` のオペランド順（`b ? a : 7` のコード生成で固定する）

`movnez` は PIE ではなく Xtensa コアの命令で、`data/pie_instructions.json` にも
`data/pie_hazards.md` にも無い。本作は「`addi`/`movnez` の連鎖で、3 番目のオペランドが非ゼロのとき
2 番目を 1 番目へ写す」という形に依存しているので、その順序を**コンパイラ自身の出力**で押さえた:

```c
int mv(int a, int b) { return b ? a : 7; }
int mn(int a, int b) { return a < b ? a : b; }
unsigned u(unsigned a, unsigned b) { return a < b ? a : b; }
int fz(unsigned a) { return a ? 1 : 0; }
```

```
$ xtensa-esp32s3-elf-gcc -O2 -S -o - /tmp/mv.c
	.file	"mv.c"
	.text
	.align	4
	.global	mv
	.type	mv, @function
mv:
	entry	sp, 32
	movi.n	a8, 7
	moveqz	a2, a8, a3
	retw.n
	.size	mv, .-mv
	.align	4
	.global	mn
	.type	mn, @function
mn:
	entry	sp, 32
	min	a2, a3, a2
	retw.n
	.size	mn, .-mn
	.align	4
	.global	u
	.type	u, @function
u:
	entry	sp, 32
	minu	a2, a3, a2
	retw.n
	.size	u, .-u
	.align	4
	.global	fz
	.type	fz, @function
fz:
	entry	sp, 32
	movi.n	a8, 1
	movnez	a2, a8, a2
	retw.n
	.size	fz, .-fz
	.ident	"GCC: (crosstool-NG esp-15.2.0_20251204) 15.2.0"
```

`int fz(unsigned a) { return a ? 1 : 0; }` が `movi.n a8, 1` + `movnez a2, a8, a2` になる。`a2` は
引数 `a` を保持し、結果は `a` が非ゼロなら 1、ゼロなら 0（= `a` のまま）でなければならない。したがって
**3 番目が条件、非ゼロのとき 2 番目を 1 番目へ**で確定する（`min` も `minu`/`max` も存在することも
同時に見える）。PIE ではないので**実機での確認記録はこのリポジトリには無い**（§11）。

## 11. 未確認の前提（実機で確かめていないことの一覧）

1. **PIE 命令の意味論そのもの**: すべて TRM の Operation 疑似コード（`data/pie_instructions.json` の
   `source_page` 付き、§7）から写した。実機で確認済みなのは、このリポジトリが既に持っている 2 つの
   finding（`EE.VLD.128.IP` の丸め・`ACCX` の飽和）だけである。この 4 カーネルは装置で 1 度も走って
   いない。
2. **`EE.VMULAS.U16.ACCX` の飽和値**: TRM は `clamp(ACX + Σ, 0, 2^40-1)`、notes/cardputer-adv-project/vm-pie-fit.md は S16 版が実機で
   `2^39-1` で止まったと書いている。**本作の和は最大 524280（段 1）と 4（段 2）なので、どちらの上限でも
   到達しない**（この不一致は本作の正しさに影響しない。これが「不確かなものを範囲外に置く」という形）。
3. **`EE.SRS.ACCX` の読み出し**: `au = sat32(ACCX >> as[5:0])` を実機で確かめたのは ex05（シフト
   0/5/8、値は 1.6e12 級）。本作は**小さい値（≤524280）+ シフト 0** でしか使わないので飽和しないが、
   「この規模で実機が同じ」を測った人はいない。書き戻し（`ACCX = ACCX >> 0`）は毎回
   `EE.ZERO.ACCX` で潰すので、書き戻しの有無に依存しない。
4. **`EE.VCMP.EQ.S32` が符号付き比較であること**: 等値比較なので符号の解釈は結果に影響しない
   （＝この不確かさは踏めない）。
5. **`movnez` のオペランド順**: `if (aS != 0) aR = aT`（3 番目が条件）を**ツールチェーン自身の
   コード生成**で固定した（§10: `a ? 1 : 0` が `movi.n a8,1; movnez a2,a8,a2` になる）。PIE ではなく
   Xtensa コア命令で、コンパイラがあらゆる場所で使っている。ただし**この順序を実機で確かめた記録は
   このリポジトリには無い**。
6. **`EE.VST.L/H.64.IP`（8 バイト、下位 3bit を落とす）**: notes/cardputer-adv-project/vm-pie-fit.md §4 が名指しで「piesim 未実装で
   意味を検証していない」と書いた命令。本作は TRM p168/p279 からモデルを書き、C 参照と 1078 ケース
   （奇数末尾を含む）で一致させた＝**算術としてのバイト配置は押さえた**。実機の挙動と piesim は空白の
   まま（piesim は `ee.vst.l.64.ip` を知らない）。
7. **実機の `var_buf` / `var_refs` が本当に 16 バイト未整列か**: §2.2 は `quickjs.c` /
   `quickjs-vmstack.h` の式からの**算術**であって、実機のアドレスを採取したものではない。アロケータが
   たまたま grid に載せていれば、この失敗は再現しない（数えた人はいない）。
8. **`sizeof(JSStackFrame) = 48`・`sizeof(JSVMLink) = 4`**: notes/cardputer-adv-project/vm-pie-fit.md §2.3 の値（実機ビルドで確認したと
   書かれている）を引いた。私は再測定していない。
9. **`frame_max` = 84〜188 バイト**: `docs/vm-L2-design.md` §13.2 からの引用。§2.5 の「+6〜29%」は
   この分布の上端・下端に対する算術である。
10. **サイクル**: §5.4 は別ファームウェアの実測式への外挿。PIE のタスク切り替えコスト（QR 8 本の
    退避・復元）は未計測で、**この規模のカーネルでは支配的になり得る**。
11. **`loopgtz`/`loop` 化**: §4.2 の最後に書いた「次の一手」（JSValue あたり 1.5 → 0.5 命令）。
    `loop` 本体の外へ分岐する形（C の当たり脱出）が LX7 でどう振る舞うかは未確認なので採らなかった。
12. **`.iram1` の消費**: 5 エントリポイントで **355 バイト**。実機の IRAM にこの余裕があるかは
    確認していない（`proposed/` なので今は入っていない）。
13. **呼び出し規約のオーバーヘッド**: 私は「9 命令（entry/早退/retw を含む）」を数えているが、呼び出し
    側の `call8` + レジスタ窓の退避は in-tree のインライン版には無く、本作にはある。§5.3 の損益分岐は
    この 1〜2 命令を見込んでいない（V=2 では差が 1 命令なので効く）。

## 12. ビルドへ入れるなら（今回は**触っていない**）

`main.c` / `examples.h` / `examples/firmware/main/CMakeLists.txt` は 1 バイトも変更していない。
`proposed/` はビルドに入っていない（`CMakeLists.txt` は `SRCS` を明示列挙している）。入れる手順:

1. `ex21_vmframe.S` を `examples/firmware/main/` に移し、`CMakeLists.txt` の `SRCS` に足す。
2. `main.c` に `ex21()` セクションを足し、C 参照（`ex21_*_c`）と突き合わせて `CHECK` / `RESULT` を出す
   （この例題集の作法。`DATA first_mismatch` で先頭不一致を出す）。
3. `BENCH` 行は `V=0` のフレームしか測れないことに注意（**ホスト実測で最ホット経路が V=0**）。
   `BENCH` を出すなら、`var_count`/`var_ref_count` を人為的に大きくしたフレームを作るしかない。
4. **実機に載せる前に §2.2 の整列契約をどう満たすか決める**必要がある。満たさないまま呼ぶと、
   §2.4 の表のとおり**静かに壊れる**（例外は出ない）。
5. このリポジトリの作法（notes/cardputer-adv-project/vm-pie-fit.md §5）: 主張するには (a) 同じコミットにコンパイル時スイッチ、
   (b) **同じツリー・1 個の define の差で 2 本焼く**（`docs/pie-simd.md` §9 の「ビルド間 15% ぶれ」を
   跨ぐ比較は無効）、(c) 命令数ではなく**実機のターン時間**で差を取る、の 3 点が要る。

## 出典

* `examples/firmware/main/proposed/ex21_vmframe.S`（この例題の本体、5 エントリポイント）
* `data/pie_instructions.json`（220 命令の Operation と `source_page`）、`data/pie_hazards.md`
  （Table 1.7-2）、`data/pie_timing_measured.json`（実機の距離アンカー）、
  `data/pie_examples_measured.json`（ex03 の整列 finding）
* `notes/cardputer-adv-project/vm-pie-fit.md`（VM 分野の前段調査。C1/C3 の候補、4 バイト整列の壁、1/4 の見積り）
* `/workspace/pjs-vm` 側: `components/quickjs-ng/quickjs-ng/quickjs.c`（`:18674`, `:18718-18791`,
  `:21422-21425`）、`quickjs-vmstack.h`（`:70-72`, `:110-115`, `:644`）、`docs/pie-simd.md`
  （PIE の実測制約、§3.5 の 1 命令 1 サイクル）、`docs/vm-L2-design.md` §13.2（`frame_max`）
* `/workspace/pjs-vm/tools/pie/piesim.py`（無改変コピーを /tmp で実行。第三実装として §9）
