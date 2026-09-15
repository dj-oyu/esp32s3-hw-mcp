# 10 — windowed ABI: call8 のカレーは「a10〜a15 を壊してはいけない」

`examples/firmware/main/*.S` と `proposed/*.S` の手書きカーネルは `main.c` から `call8`（実測では
`callx8` 経由の関数ポインタ呼び出しも同じ）で呼ばれる。このノートは、そのときに**カレーが壊しては
いけないレジスタはどれか**、何を測定で押さえ、何がまだ推論なのかを分けて書く。

## 1. 何を機械的に強制しているか（このリポジトリでの約束）

`tools/check_abi.py` が `.S` を静的に見て、次の 4 つを落とす。`tools/fix_abi.py` は 1 を機械的に直す。

1. **`a10`〜`a15` に書き込みがあるのに、フレームへの退避（`s32i`）と復帰（`l32i`）が対応していない**
   — 違反は `--strict` で異常終了。修正は「prologue に `s32i`、各 `retw.n` の直前に `l32i`」で、
   ループ本体は触らない（`tools/verify_abi_fix.py` が「追加命令以外は逆アセンブルで完全一致」を確認する）。
2. `entry` と `retw.n` が対になっていない（`ret` 単独使用、1 関数に複数の `entry`）— notes/04・05 の罠。
3. `loop`/`loopnez`/`loopgtz` の本体が 256 バイトを超える。
4. フレーム外のスタックアクセス（警告）、フレームが 16 の倍数でない（警告）。
5. **フレーム上端 16 バイト（`[N-16, N)`）をローカルに使っている**（失敗）— §4。実機クラッシュの
   真因だった規則で、オフセット `>= N`（呼び出し元の引数域）は合法。

## 2. 実測したこと（ホスト側、`xtensa-esp32s3-elf-gcc -Os/-O2`）

- **引数は呼び出し側の `a10`〜`a15` に置かれる**: `int h(int,int); int f(void){return h(1,2);}` →
  `movi.n a10, 1 / movi.n a11, 2 / call8 h / mov.n a2, a10`（戻り値も呼び出し側の `a10` に現れる）。
  つまり **カレーの `a2`〜`a7` = 呼び出し側の `a10`〜`a15`**、窓は `call8` で 8 レジスタ回る。
- **GCC は `call8` をまたいで生きている値を `a2`〜`a7` に置き、`a8`〜`a15` は呼び出しのたびに
  フレームへ退避する**: `p` と `n` を `a6`/`a7` に持ったまま `call8` する形、および 12 個の生きた値で
  `a10`〜`a13` に載せてから `s32i` / `l32i` で前後を挟む形を確認。**カレーが壊してよいと
  GCC が考えているのは `a8`〜`a15`**（そのため手書きカーネルが `a10`〜`a15` を無断で使うと、
  「呼び出し側が壊してよいと思っていない側のレジスタ」を触ることになる）。
- 参考: `xtensa_context.S` も「windowed ABI では a14-a15 だけ保存すればよい」と書いており、
  **どの 8 本がカレー側の窓に重なるか**は ABI のバージョン差ではなく実装の差で説明できる。

## 3. 実機の証拠と、原因（2026-09-15 に確定）

`EX ex12 physics CHECK dist2_...` の直後、`print_i16("pt_a", s_phy_pta, 72)`（`main.c:1856`）の
先頭ロードで落ちる。

```
PC  : 0x4200ea3d   A0 : 0x8200697e   A1 : 0x3fc9c3e0      EXCCAUSE: 0x1c (LoadProhibited)
A2  : 0   A3 : 1    A4 : 0    A5 : 0    A6 : 0    A7 : 0   EXCVADDR: 0x00000000
A8  : 0x8200ea3d   A9 : 0x3fc9c390   A10: 0x1a  A11: 0x3c026f00  A12: 0x3c026ef8
A13 : 0x3c0273f0   A14: 1    A15: 0    Backtrace: 0x4200ea3a ... |<-CORRUPTED
```

逆アセンブル（`build_examples/pie_examples.elf`、`app_main` に全部インライン展開されている）:

```
4200e6eb: l32r a7, <s_phy_pta>     ← a7 = &s_phy_pta（以後 s32i/…のベースとして使い続ける）
4200e9d8/ea17/ea3a: call8 printf   ← a7 を生かしたまま printf を何度も呼ぶ
4200ea3d: l16ui a12, a7, 0         ← ここで A7 = 0
```

**原因は §5 に書いた「祖先のフレーム上端 16 バイト」を `ex12_dist2_qacc` 自身がローカルに使っていたこと。**
`ex12_dist2_qacc` は `entry a1, 64` で、`s32i a6, a1, 52`（int32 クランプ上限）と
`s32i a6, a1, 56`（残りペア数）に保存していた。**オフセット 52〜63 = フレーム上端 16 バイトは、
call8 の窓オーバーフロー時に `_WindowOverflow8` が祖先フレームの `a0`〜`a3` を書き込む領域**
（`entry a1, N` はフレーム上端 = 呼び出し元 SP を動かさないので、上端 16 バイトは常にその退避域）。
`printf` を何度も呼ぶうちに窓が回り込み、この 2 つのスロットが退避データで上書きされ、
**`A0`〜`A3` 由来の 0 が `A2`〜`A7` に並ぶ**というパニックダンプの形になる。

実機で確かめた 2 点（どちらも examples ファームの走行で再現）:

1. `ex12_dist2_qacc` の 52/56 を 36/40 に移す（`entry a1, 64` のまま）と、ex12 のクラッシュは消える。
2. 逆に、`tools/fix_abi.py` の**最初の版**は新しい退避スロットを「元のフレームの直後」=
   **新しいフレームの上端 16 バイト**に置いたため、クラッシュが ex12 から ex02 へ移った
   （`EX ex02 matmul16 BEGIN` 直後に `IllegalInstruction` → double exception）。
   `ex02_matmul16` のスロットだけを 32/36/40 から 0/4/8 へ移すと ex02 は通り、次は ex03 が落ちた。
   → **「スロットを上端に置くと必ず落ちる」が 2 例で確定。**

## 4. 規則（`tools/check_abi.py` の 5 番目の検査）

> **フレーム上端 16 バイト（相対オフセット `[N-16, N)`）をローカルに使ってはいけない。**

- `entry a1, N` はフレーム上端（= 呼び出し元 SP）を動かさないので、この 16 バイトは常に
  窓の退避域。オフセット `>= N` は呼び出し元の引数域なので合法（7 個目以降の引数はそこにある）。
- `tools/check_abi.py` は rule 5 として失敗させる。`tools/fix_abi.py` は
  `new = old + 4n + 16`（16 の倍数へ切上げ）で**上端に 16 バイトの予備を残してから**スロットを置く。
- 実際に踏んでいたのは 2 箇所だけ: `ex12_dist2_qacc`（52/56 → 36/40）と、
  ドラフトの `ex16_prerotate_idx`（16 → 12、`entry a1, 32`）。
- 手書きカーネルを書くとき/読むときの一般則として、`notes/` 側にも書く価値がある:
  **`entry a1, 32` は「下 16 バイトがローカル、上 16 バイトが窓の退避域」**という分割だと思うとよい
  （GCC 自身も callee-saved を `sp+0`〜`sp+12` に退避し、上端は使わない — `lay.c` で確認）。

## 4b. 未確定（プローブは今は不要）

「カレーの `a10`〜`a15` が直の呼び出し元の `a2`〜`a7` に重なる」のか「窓が回り込んだ祖先のレジスタ
だけか」は、**クラッシュの説明には必要なかった**。実機の A2〜A7 = 0 はフレーム上端の破壊で説明でき、
rule 1 の退避は「呼び出し側が壊してよいと考えていないレジスタを触らない」防御として残す
（挙動不変は `tools/verify_abi_fix.py` が逆アセンブルで証明済み）。切り分けたい場合の最小プローブは
下のとおり（`tools/device_experiment.sh` の枠に載せられる）:

```asm
/* probes/abi_probe.S — a15 を壊して戻るだけの関数 */
    .section .iram1,"ax",@progbits
    .align 4
    .global abi_clobber_a15
    .type abi_clobber_a15,@function
abi_clobber_a15:
    entry a1, 32
    movi a15, 0
    retw.n
    .size abi_clobber_a15, .-abi_clobber_a15
```

```c
/* 呼び出し側は GCC に「a7 に生きた値を持たせる」形にする（t1 と同じ形） */
extern void abi_clobber_a15(void);
static void abi_probe(const int16_t *p, int n) {
    int s = 0;
    for (int i = 0; i < n; i++) { s += p[i]; abi_clobber_a15(); }
    printf("ABI a7 survived: %d of %d\n", s, n);   /* a7 が 0 なら LoadProhibited */
}
```

呼び出し側が本当に `a7` に `p` を置くことは、**ビルド後に逆アセンブルで確認してから**実機に載せる
（そうでないと「何も起きない」が何の証拠にもならない）。このプローブで
`ABI a7 survived: ...` が出れば「カレーの `a10`〜`a15` は直の呼び出し元の `a2`〜`a7` を壊せる」＝
本ノートの 1 の規則がそのまま必要、LoadProhibited なら逆（＝ §3 の別原因を追う）。

## 5. 関連

- 窓の退避/復元の実装: `components/xtensa/xtensa_vectors.S`（`_WindowOverflow4/8/12`,
  `_WindowUnderflow4/8/12`）— 祖先の `a0`-`a3` は**子の SP 直下 16 バイト**、`a4`-`a7` は
  **祖先のフレーム上端 16 バイト**（`call[j-1].sp - 32`）に退避される。
- 既存の窓まわりの罠: `notes/04-hardware-experiment.md` §「ここまでで潰した罠」、
  `notes/05-resume.md`（`ret` で戻っていた生成コードの話）。
- ツール: `tools/check_abi.py`（検査）、`tools/fix_abi.py`（機械修正）、
  `tools/verify_abi_fix.py`（修正が追加命令以外を変えていないことの逆アセンブル証明）。
