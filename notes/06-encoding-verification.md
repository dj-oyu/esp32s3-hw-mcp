# アセンブラ知識の検証: マニュアルの命令語ダイアグラム vs 実ツールチェーン

作業: `tools/asm_toolchain.py`（新規）＋ `tools/selftest_asm_toolchain.py`（新規, CI入り）
生成物: `data/pie_encoding_errata.json`（不一致だけを記録）

## なぜやるか

TRM 1.8 は各 PIE 命令の命令語を**ビットフィールド図**で示す。抽出器はそれを
`instruction_word` 文字列（例: `11 / qa[2:1] / 1101 / qa[0] / 011 / qy[2:1] / 00 / qx[2:1] / qy[0] / qx[0] / 0100`）
にしている。**この読み取りを目でやると、フィールドの入れ替えやビット幅の取り違えが「それらしい別の命令」に
化けたまま通ってしまう。** そこで、ファームを実際にビルドするのと同じ Espressif binutils
（`xtensa-esp32s3-elf-as` / `objdump`）に答えさせ、マニュアル由来の主張はすべてそれと突き合わせる。

    tools/asm_toolchain.py                                   # ヘルプ
    tools/asm_toolchain.py --check-asm "ld.qr q0, a3, 0"     # アセンブルして符号化を出す
    tools/asm_toolchain.py --decode cd2034                   # 命令語 → ニーモニック（逆方向）
    tools/asm_toolchain.py --check-instruction EE.ANDQ       # マニュアルの図 vs ツールチェーン
    tools/asm_toolchain.py --check-all                       # 全220命令（2オペランド組で）
    tools/asm_toolchain.py --errata data/pie_encoding_errata.json

## 結果: 220命令中 214命令が一致（97%）

「一致」= マニュアル自身の図にオペランドを代入して得た 24/32 ビット語が、アセンブラが出す語と
2つのオペランド組でビット単位で完全一致。**残り6件が不一致で、それがこの検証の成果**（`data/pie_encoding_errata.json`）。

| 命令 | 状態 | 何が起きているか |
|---|---|---|
| `MV.QR` | mismatch | 図のフィールド合計が **23ビット**（命令は24ビット）。`qs[2:1]` と `qs[0]` の間の定数は印刷上 `000` だが、実際は **`0000`**（アセンブラ出力から逆算、複数のレジスタ組で確認） |
| `EE.VLDBC.32.IP` | mismatch | 構文行は即値 `-256..252`（刻み2）。アセンブラが受けるのは **`-512..508`（刻み4）** |
| `EE.ST.ACCX.IP` | assembler_rejected | 構文行は即値 `-512..508`（刻み4）。アセンブラは **8の倍数のみ**を受け、範囲は **`-1024..1016`**。508 は「invalid value」で拒否される |
| `ST.QR` | syntax_names_other_instruction | 構文行のニーモニックが `LD.QR` と印字されている（マニュアルの誤植。図の命令語は ST.QR として正しい） |
| `EE.SRC.Q.LD.IP` | layout_not_machine_readable | 抽出した図がフィールド列になっていない（PDF側の箱が1つのテキストランとして取れている）。**抽出器の要修正** |
| `EE.VMULAS.S8.QACC.LD.IP` | layout_not_machine_readable | 同じく抽出不良。しかも "Operation" の本文が図のトークンに混入している |

`MV.QR` については PDF 303ページを画像化して目視でも確認した（`sources/esp32-s3_technical_reference_manual.pdf`
の 303ページを 200dpi でレンダリング）。**印刷されている図そのものが1ビット足りない**ので、抽出のバグではなく
マニュアルの欠陥。`EE.SRC.Q.LD.IP` / `EE.VMULAS.S8.QACC.LD.IP` の2件は抽出側の問題なので、
`tools/extract_pie.py` の図の取り方を直すまでは符号化を導出しない（推測しない）。

## 副産物: 即値の刻みと符号の規則（これも実測で確定）

PIE のメモリ系命令の即値は、構文行が**バイト変位**で書き、命令語フィールドは**刻みで割った値**を
2の補数で持つ。刻みは「構文行の範囲幅 ÷ (2^フィールド幅 − 1)」で出る。実例:

- `LD.QR q5, a5, -128` → `imm[3:0] = 1000`（−128 ÷ 16 = −8 を4ビット2の補数で）→ 語 `eda854`
- `EE.MOVI.32.A q5, a5, 2` → `sel4[1:0] = 10` → 語 `edf954`
- `EE.ST.ACCX.IP a5, 0` → `imm8 = 0`、`EE.ST.ACCX.IP a5, 512` → `imm8[7] = 1`（512 ÷ 8 = 64 ではない点に注意: 実測は刻み8）

この規則は `check_instruction` が毎回アセンブラに突き合わせている。だから刻みを1つ間違えると
「一致」ではなく mismatch として出る（目視では絶対に気づけない種類の誤り）。

## 次にやるべきこと（優先順）

1. **`EE.SRC.Q.LD.IP` / `EE.VMULAS.S8.QACC.LD.IP` の抽出を直す**（図の箱を1つずつ取る）。直せば
   `--check-all` の分母が220に戻り、残る不一致だけが「マニュアル側の欠陥」として残る
2. **即値の刻みのハードウェア実測**: `EE.ST.ACCX.IP`（マニュアル: 刻み4 / アセンブラ: 刻み8）と
   `EE.VLDBC.32.IP`（同 2 vs 4）は、どちらが実機の挙動か決着していない。`experiments/pie-timing` に
   「`as` のポストインクリメント量を測る」ケースを足せば決まる（アドレスを2回読んで差を見るだけ）。
   これは「PIE カーネルを書くとき実際に何バイト進むか」に直結するので優先度が高い
3. `data/pie_encoding_errata.json` を MCP の `manual_errata` が配信する（実装済み）。CI は
   ツールチェーンが無ければ自己テストを skip する（`selftest_asm_toolchain.py` は skip を明示して 0 で終わる）

## この検証で分かったこと（マニュアルの読み方として）

- TRM の「命令語」図は**そのまま信用してはいけない**。少なくとも `MV.QR` は1ビット足りない
- 「構文行」も信用してはいけない（`ST.QR` のニーモニック誤植、`EE.ST.ACCX.IP`/`EE.VLDBC.32.IP` の即値範囲）
- 一方で、**図が正しい214命令については、オペランドを代入すれば機械的に符号化を再構成できる**。
  つまりアセンブラが対応していない命令でも、図＋この規則で命令語を作れる（＝MCP が
  `encode(name, operands)` を返せる根拠）。実際にそれでファームを書けることは
  `experiments/pie-timing/firmware/main/measure.S` が示している
