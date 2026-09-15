# ex17 (proposed) — MP3（minimp3）の合成フィルタバンク: 18 点 IMDCT / 窓重ね / 32 サブバンド合成

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex17_mp3synth.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 10 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `c2f303462cbff92a7d519df92241e82f` → `51ac4fa13635d29b2b82fc0748ddac09`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex17_mp3synth.S` の 3 本（`ex17_imdct18` / `ex17_window_overlap` /
`ex17_polyphase`）の設計・契約・証拠を 1 枚にまとめたものです。**実機は使っていません**（親セッションが
`/dev/ttyACM0` を占有しているため）。`proposed/` にあるのでビルドにも入っていません。

対象は **lieff/minimp3**（`MINIMP3_ONLY_MP3` / `MINIMP3_NO_SIMD`、内部演算は **float**、出口だけ int16）。
この 3 本は Q15 の**整数**カーネルなので、**minimp3 の float 経路とビット一致しません**。一致するのは
「同じ DSP 段の同じ線形写像」というレベルの話で、差は §11 で LSB 単位に測ってあります（要求どおり
「一致しない」と書き、どこが何 bit 落ちるかを数字で出しています）。

行番号は `/workspace/cardputer-adv-pocketjs/.cache/codecs/minimp3/minimp3.h`（1865 行、2026-09-15 時点）の
ものです。

## 0. この文書の検証段

| 層 | 何をしたか | 結果 |
|---|---|---|
| アセンブラ | `xtensa-esp32s3-elf-gcc -c`（ESP-IDF v6.0.1 の xtensa-esp-elf 15.2.0） | 通る。`ex17_imdct18` 274 B / 74 命令、`ex17_window_overlap` 262 B / 91 命令、`ex17_polyphase` 107 B / 31 命令（objdump 実測） |
| Python モデル | 命令レベル（TRM の Operation 疑似コードから 1 命令 1 関数）。.S と同じ命令順・同じレジスタ役割で 3 本を書き、整数参照・minimp3 の float 経路（倍精度）・もう 1 つの整数綴りと突き合わせ | **T1: 512 スペクトル 0 不一致**、T5: 1024 サブバンド 0 不一致、T6: 3768 サンプル 0 不一致、T2: 合成行列の線形性 1.8e-15、T3: float 経路との差 max 3.20 LSB |
| ホスト C 参照 | `cc -O2` のスカラ Q15 参照がモデルのケースダンプを再計算 | **A 96 値 / B 200 値 / C 64 値、不一致 0**（初回はこの検査が C 側の `int` 溢れを捕まえた。§8） |
| piesim.py | `/workspace/pjs-vm/tools/pie/piesim.py` を `/tmp` にコピーし、TRM 疑似コードから 3 命令を足して A と C を解釈実行 | **A: 32 バンド 0 不一致 / C: 256 サンプル 0 不一致**（§9） |

**未確認は §12 に全部並べました。** とくに、実機でのサイクル数は 1 つもありません（`BENCH` 行は存在
しません。このファイルは `main.c` に入っていないので、ファームをビルドしてもこの 3 本は走りません）。

## 1. 対象にした minimp3 の段（行番号つき）

| 段 | minimp3.h | 中身 |
|---|---|---|
| 18 点 IMDCT（ロングブロック） | `L3_imdct36` 1087-1142 | バタフライ 1097-1104（`co`/`si` を作る。`si[8-2i]` / `si[7-2i]` は**逆順 index**）、9 点 DCT 2 本 1106-1107（`L3_dct3_9` 1047-1085）、符号反転 1109-1112、twiddle 回転 1136-1137。twiddle 表は `g_twid9`（1090-1092、18 個の 10 進リテラル）。状態は `mdct_overlap[2][9*32]`（**20 行目**、バンドあたり 9 float） |
| 窓掛けと重ね合わせ | 同 1136-1139 | `sum = co*tw[9+i] + si*tw[i]`、`overlap[i] = co*tw[i] - si*tw[i+9]`、`grbuf[i] = ovl*w[i] - sum*w[9+i]`、`grbuf[17-i] = ovl*w[9+i] + sum*w[i]`。窓表は `g_mdct_window[2][18]`（1196-1199） |
| 周波数反転 | `L3_change_sign` 1186-1192 | `b += 2, grbuf += 36` で**奇数サブバンド**だけを回り、`i = 1,3,..,17` を反転＝**奇数サブバンドの奇数サンプルを負にする** |
| 32 サブバンド合成 | `mp3d_synth` 1476-1627、内側ループ 1598-1625 | 窓表 `g_win`（1482-1498、**15 位相 × 16 係数** = 240 float）、脱インタリーブ状態 `lins`（`zlin[4*i - 64*k]`、1600）、`mp3d_synth_granule`（1629-1655）が `lins`（15*64 float、1637/1653）をリングとして持ち回る |

**タスク文の「S[] バッファと 512 タップの窓」は ISO 参照実装の形で、minimp3 はその形を採っていません。**
minimp3 の合成は「32 点 DCT（`mp3d_DCT_II`、1634 で呼ばれる）＋ 16 タップの窓付き再結合」で、窓表は
15 位相 × 16 係数、状態は 15×64 float です。512 タップ窓のほうを素直に組むと（`V[i] = Σ_{j=0..7}
S[i+64j]*D[j]` を 512 個 = 4096 MAC、32 出力の行列積 1024 MAC → **160 MAC/出力サンプル**）ですが、この
算術は**このセッションでは検証していません**（リポジトリに ISO のソースが無い）。検証したのは minimp3 の
ほうの数字（**16 MAC/出力サンプル**、§7 で数え方つき）です。どちらの形でも「8 レーン MAC にして
1 命令/8 MAC」という比較の結論は変わりません。

## 2. A. `ex17_imdct18` — 契約（レイアウトを厳密に）

```
void ex17_imdct18(const int16_t *x, const int16_t *tw, int32_t *acc, uint32_t shift)
```

| 引数 | レイアウト |
|---|---|
| `x` | 24 int16（48 B、16 B 整列）。`x[0..17]` が 1 サブバンドの再量子化スペクトル（Q15）、`x[18..23]` は**読むが使わない**（出力レーン 18..23 の係数列が 0 なので積が 0） |
| `tw` | **56 列 × 8 レーン = 448 int16（896 B、16 B 整列）**。列 (g,k) が出力 index 8g..8g+7 の 8 レーン・タップ k: `tw[144*g + 8*k + j] == Cq[8g+j][k]`、`Cq[j][k] = 0`（j ≥ 18）。**列 54,55 は最後の 2 つの転がりロードが読んで捨てる**ので、表は 56 列必要（中身は何でもよい） |
| `acc` | 24 int32（96 B、16 B 整列）。`acc[j] = (Σ_k x[k]*Cq[j][k]) >> shift`（40bit レーンの厳密値）。`acc[18..23]` は係数 0 なので **0** になる |
| `shift` | `EE.SRCMB.S16.QACC` の `as[5:0]`。Q15 出力なら 15 |

`Cq` は minimp3 の**折り畳み＋twiddle 回転の合成行列**です。`L3_imdct36` の段は x について線形なので、
倍精度に書き下ろした `L3_imdct36`（`L3_dct3_9` 1047-1085 を文単位で写したもの）に**単位ベクトルを 18 本**
通せば厳密に出ます（モデルの T2）。実測: `max|C| = 0.999048`（int16 係数の上限に収まるので **Q15 のまま**
表を作れる）、表の丸め誤差は最大 0.4726 LSB、64 本のランダム探針で `|C@x - fold(x)| ≤ 1.8e-15`。

### 2.1 なぜこの並べ方か（4 レーン×n / 複素寄せ / `EE.CMUL.S16` / ACCX / QACC）

候補は 4 つで、**A は「出力 8 レーン並列 × QACC（並列蓄積）＋ タップを broadcast」**を選びました。
`EE.VSMULAS.S16.QACC qx, qy, sel8`（p269）は `qy[sel8]` の**1 レーンだけ**を全レーンへ配り、
`qx[l] += qx[l]*temp` を**レーンごとの 40bit アキュムレータ**に積みます。つまり

- `qx` = **係数列**（8 レーン = 8 個の出力のタップ k の係数）
- `qy` = **x のレジスタ**、`sel8 = k mod 8` で「どのタップか」を即値で選ぶ

と置くと、18 タップ × 8 出力が 18 命令で終わり、**タップは命令に埋め込まれた即値**（sel8）なので
ロードが 1 本も要りません。

| 候補 | 何が起きるか | 判断 |
|---|---|---|
| **4 レーン × n**（4 バンド並列、レーン = バンド） | 各出力は 18 タップの還元なので、タップ k の**データを 4 バンド分**集める必要があり、x はバンドごとに stride 18（36 B）→ 1 命令で 4 レーン取れない（`EE.LDXQ.32` は 1 命令 1 レーン、p113）。1 タップ = 4 命令 → 18 タップ × 9 出力 = 162 ロード/バンド | 棄却（レイアウトが「タップごとに同じ係数」を要求しないため、gather になる） |
| **複素寄せ + `EE.CMUL.S16`**（(co,si) を複素、twiddle を複素として 1 命令 2 複素積） | 回転そのものは複素積なので**最も短い**（9 複素 = 5 命令程度）。ただし minimp3 の折り畳みは `si[8-2i]` / `si[7-2i]`＝**レーンの逆順**を要求し、220 命令にレーン反転は無い（`VZIP`/`VUNZIP` は偶奇分割だけ p290/p293、`EE.BITREV` は p77 で 1 ワード内ビット反転）。minimp3 自身の SIMD 経路もこのために `VREV` を使っています（**1130 行目**）。逆順を回避するには折り畳み前の並べ替えを 3 レジスタで組む必要がある | 棄却（ex16 が CELT の pre-rotate で CMUL を担当。ここは行列形で「転置不要」を買う） |
| **二重蓄積（ACCX）** `EE.VMULAS.S16.ACCX` + `EE.SRS.ACCX` | 8 レーンの積を **1 本の ACCX に合算**するので、1 命令で出せる出力は 1 個。18 出力 × (1 `ZERO.ACCX` + 3 MAC + 1 `SRS.ACCX`) = **90 命令・読み出し 18 回**。この綴りも実際に走らせて結果が一致することを確認（下の表） | 棄却（同じ MAC 数で読み出しが 6 倍。ただし「1 スカラが欲しい」場合の正解。ex10 の SAD がその例） |
| **並列蓄積（QACC）** `EE.VSMULAS.S16.QACC` + `EE.SRCMB.S16.QACC` | 出力 8 個が同時に進み、読み出しは 8 レーン一括で **3 回**（24 レーン = 3 グループ）。18 出力 × 18 タップ = 324 MAC が **54 MAC 命令**（各命令 8 MAC = 432 レーン MAC、うち有効 324） | **採用** |

### 2.2 逐次蓄積（ACCX）と並列蓄積（QACC）の選択理由（数字で）

| 綴り | MAC 命令 | 読み出し命令 | ゼロクリア | ストア | 合計/バンド | 結果 |
|---|---|---|---|---|---|---|
| QACC（このカーネル） | 54（18 × 3 グループ、`LD.INCP` 融合ロード込み） | 3（`SRCMB.S16.QACC`、8 レーン一括） | 3 | 6（int32 = `L`/`H` × 3） | **74**（objdump 実測） | 512 スペクトルで整数参照と一致 |
| ACCX（比較用に実装して走らせた） | 54（18 × 3、`ACCX.LD.IP` 融合ロード） | 18（`SRS.ACCX`、1 出力ずつ。ACCX を書き戻すので毎回 `ZERO.ACCX` も要る） | 18 | 0（レジスタに 1 個ずつ返る。メモリに置くなら +18 ストア） | **90** | 64 スペクトルで **QACC 版と完全一致（不一致 0）** |

理由の核: **還元（reduction）の向きが「並列化できる向き」と一致しているか**で決まります。

- A は「18 この独立した出力」が欲しい。還元はタップ方向だが、**タップは既にレジスタの中**（broadcast の
  即値で選べる）にあるので、独立な出力 8 個をレーンに置ける。QACC はレーンごとに独立な 40bit を 1 組
  持っているので、この向きにそのまま乗る。
- ACCX は**8 レーンを 1 本に潰す**命令なので、出せる出力が 1 個になり、読み出しも 1 出力ずつ。ex10 の
  ブロック SAD のように **答えが 1 スカラ**のときだけ正しい選択です（notes/08 の表の「音声ミックス」も
  同じ理由で ACCX 側）。
- 40bit の余裕: 18 タップ × 32767² < 2^35 < 2^39 なので、QACC レーンは**どの入力でも飽和しません**
  （モデルで 512 スペクトル中 0 件）。ACCX も同じ 40bit です。

### 2.3 読み出しと、どこで何 bit 落ちるか

`EE.SRCMB.S16.QACC qu, as, 0`（p130）は 8 レーンの 40bit を `as[5:0]` だけ右シフトし、**QACC へ書き戻し**
（`QACC_L[39:0] = temp_shf0[39:0]`、クランプ無し）、`qu` に**16bit 飽和コピー**を入れます。このカーネルは

- `EE.ST.QACC_L.L.128.IP` / `_H_`（p139/p137、各 4 int32）で **QACC 側**を `acc` に書く → `acc` は
  **厳密なシフト済み 40bit 値**（クランプ無し）。`qu` の 16bit コピーは捨てる。
- 捨てたコピーが**どこでクランプするか**は測ってあります: フルスケールのスペクトルで **9216 レーン中
  2810 レーン（30.5%）** が `|shifted| ≥ 2^15`。つまり **int16 の出口に繋ぐなら、その前に必ずスケールを
  落とす必要があります**（このカーネルの契約は int32 出力なので、その損失は acc には乗りません）。

## 3. B. `ex17_window_overlap` — 契約（レイアウトを厳密に）

```
void ex17_window_overlap(const int16_t *blocks, const int16_t *win, int16_t *out, uint32_t n)
```

| 引数 | レイアウト |
|---|---|
| `blocks` | `n` レコード × 32 int16（64 B/レコード、16 B 整列）。plane 0 = `ovl[0..8]`（**状態**: 前フレームの重なり半分 = minimp3 の `mdct_overlap`、1 バンド 9 個）、plane 1 = `sum[0..8]`（このフレームの回転半分 = 1136 行の `sum`）。両 plane のレーン 9..15 は**読むが使わない**（窓のパッドレーンが 0 なので積が 0） |
| `win` | 2 行 × 40 int16（80 B/行、16 B 整列）。行 0 = 偶数サブバンド、行 1 = 奇数サブバンド。`[0..7] A1 = W[0..7]`、`[8..15] A2 = W[9..16]`、`[16..23] Pfirst`、`[24..31] Pmirror`、`[32] W[8]`、`[33] W[17]`、`[34..39] パッド`。行 0 のパターンは全部 +1、行 1 は `Pfirst = (+1,-1,...)`、`Pmirror = (-1,+1,...)`。**A1/A2 は両行で同じ**（反転はパターン側だけ） |
| `out` | `n` レコード × 32 int16（64 B/レコード、16 B 整列）。plane 0 = `out[0..8]`（i = 0..8、レーン 8 はスカラテイル）、plane 1 = `out[16+i]` が**自然 index 17-i のサンプル**（i = 0..8）。両 plane のレーン 9..15 は**書かない** |
| `n` | サブバンド数。**偶数**で、範囲の先頭が**偶数サブバンド**であること（行を (2t, 2t+1) の組で消費し、`L3_change_sign` 1186-1192 が奇数サブバンドだけを反転するため） |

算術（1 サブバンド、i = 0..8、すべて Q15）:

```
present[i] = sat16( ((ovl[i]*W[i]) >> 15) - ((sum[i]*W[9+i]) >> 15) )  * Pfirst
mirror[i]  = sat16( ((ovl[i]*W[9+i]) >> 15) + ((sum[i]*W[i]) >> 15) )  * Pmirror
```

これは minimp3 の 1138-1139 と同じ形ですが 2 点違います: (1) **積を 1 つずつシフトする**（float 側は
先に足して 1 回丸める）、(2) 加減算が**飽和する**（`EE.VSUBS.S16`/`EE.VADDS.S16` がこの集合にある唯一の
ベクトル加減算）。このため **`sat16` の外側**（積の和が ±32767 を越える）では minimp3 の float 経路と
一致しません（§11 でレーン数を出しています）。

**逆順（fold の鏡像）について**: `grbuf[17-i]` は i が小さいほど自然 index が大きいので、レーンをそのまま
並べると**鏡像の側が逆順**になります。220 命令にレーン反転命令が無いため、このカーネルは**鏡像側を別
plane に i 昇順で**出します（`out[16+i]` = 自然 index `17-i`）。minimp3 の SIMD 経路はここで `VREV`
（**1130 行目**）を使っており、その差がそのまま「PIE では買えない 1 命令」です。自然順が欲しい呼び出し側の
変換は `natural[n] = plane0[n] (n=0..8)`、`natural[n] = plane1[17-n] (n=9..17)` です（モデルはこの変換を
通して float 参照と比較しています）。

### 3.1 周波数反転（`L3_change_sign`）の綴り 2 つ

奇数サブバンドの**奇数サンプル**を負にする、が `L3_change_sign` の中身です。このカーネルは
**±1 パターンの乗算**（`EE.VMUL.S16`、SAR=0）で実装しています。PIE に negate 命令は無く、

- `EE.VSUBS.S16 qzero, x`（0 - x）は **飽和する**ので `-32768 → 32767` になり、
- ±1 の乗算は 32bit 積の下位 16bit = **ちょうど 2 の補数の反転**（`-(-32768) = -32768`）

です。もう 1 つの綴り（**窓の値に符号を畳む**：4 本の窓ベクトルが要る）は、負の積を floor するため
**1〜2 LSB ずれる**うえ、飽和した `-32768` と正確な反転が**16bit の両端**に分かれる場所で最大
**65535 LSB** ずれます。モデルの測定（18432 レーン）:

```
0 LSB x6540, 1 LSB x585, 2 LSB x2032, 65535 LSB x59
```

つまり「パターン乗算」は 0 命令増で**正確な方**の綴りです（タスク文の「周波数反転を含む窓掛け」は
この形で実装しました）。

## 4. C. `ex17_polyphase` — 契約（レイアウトを厳密に）

```
void ex17_polyphase(const int16_t *v, const int16_t *win, int32_t *out, uint32_t n)
```

| 引数 | レイアウト |
|---|---|
| `v` | 脱インタリーブ状態、**グループ優先**: `(n/8) グループ × 16 タップ × 8 レーン = n*16 int16`（16 B 整列）。`v[128*g + 8*t + j]` = サンプル `8g+j` のタップ t。**16 タップが 16 B 間隔**なので、融合ロード形（増分が常に +16、p270）が「タップ t の MAC と同時にタップ t+1 のデータを読む」形で回ります。minimp3 の `lins`（タップ優先、`zlin[4*i - 64*k]`、1600）の**転置双子**です。**読み出しフットプリント: 末尾の 32 B を読みます**（使わない 2 ロード。呼び出し側は 32 B を readable に保つこと） |
| `win` | 16 int16（32 B、16 B 整列）。`win[t]` = タップ t の Q15 係数。minimp3 の `g_win` は位相ごとに 15 行 × 16 係数（1482-1498）なので、**どの位相かは呼び出し側がこのベクトルを選ぶ**＝ポインタ渡しです |
| `out` | `n` int32（16 B 整列）。`out[s] = (Σ_{t=0..15} V_t[s]*win[t]) >> 15`（40bit の厳密値、4 int32 ずつ 2 ストア） |
| `n` | 出力サンプル数、8 の倍数 |

### 4.1 1 サンプルあたりの MAC 数と、PIE が何命令で済むか

**「16 タップ」の出どころ**（数え方）: minimp3 の内側ループ（1598-1625）は 15 位相を回り、各位相で
8 グループ × 4 レーン × 「b に 2 乗算・a に 2 乗算」= 8 グループ × 16 乗算 = **128 乗算で 8 出力**、
つまり **16 乗算/出力サンプル**（+ 加算 15）。この 16 がタスク文の「16 タップ級」です。

| 段 | 1 出力サンプルあたりの MAC | 同じ MAC を PIE で組むと | 命令/サンプル |
|---|---|---|---|
| このカーネル（脱インタリーブ 16 タップ） | **16 乗算**（+ 15 加算） | `EE.VSMULAS.S16.QACC.LD.INCP` = 8 MAC/命令 → **16/8 = 2 MAC 命令** | MAC 2 + 読み出し `SRCMB` 1/8 + ストア 2/8（int32 = L/H）+ ループ 2/8 = **2.625**（8 サンプルで 21 命令、objdump 実測と一致） |
| 512 タップ窓（ISO の素直な形） | **160 MAC**（V を 512 個 × 8 タップ = 4096 + 行列積 1024 = 5120 MAC / 32 サンプル。**この算術は本セッション未検証**） | 同 8 MAC/命令 → 20 MAC 命令 | 約 21（このカーネルの 8 倍） |
| スカラ C -O2（64bit 厳密、`long long` アキュムレータ） | 16 乗算 | 16 タップ × **11 命令**（`l16si`×2 + `mull`+`mulsh`+`saltu`+加算 … objdump 実測） = **176 命令**/サンプル（＋関数の残り = 177） | 約 177（このカーネルの **67 倍**） |

式で書くと（このカーネル）:

```
MAC命令/サンプル   = ceil(16 タップ / 8 レーン) = 2
読み出し/サンプル   = 1 本 (SRCMB は 8 レーン一括) / 8 = 0.125
ストア/サンプル     = 2 本 (QACC_L / QACC_H, int32×4) / 8 = 0.25
ループ/サンプル     = 2 本 (addi/bnez) / 8 = 0.25
合計               = 2.625 命令/出力サンプル   （objdump: ループ本体 21 命令 / 8 サンプル）
```

40bit アキュムレータは 16 × 32767² = 1.7e10 < 2^39 なので**飽和しません**（8 レーン × 16 タップを
並列に足しても 34bit に収まる）。出口の `>> 15` は切り捨てなので、倍精度参照との差は **≤ 1.00 LSB**
（平均 0.50、§7）。

**同じ「1 要素あたり」で 3 本を並べると**（すべて objdump 実測。C 側は `cc`/`xtensa-gcc -O2` のスカラ
参照のループ本体で、`long long` を使う＝**正しい** C の綴り）:

| カーネル | 1 要素 | PIE 命令/要素 | C -O2 命令/要素 | 比 |
|---|---|---|---|---|
| A `ex17_imdct18` | 1 出力（18 タップ） | 74/18 = **4.11** | 18 タップ × **11 命令** = 198（＋関数の残り 87-11=76 を 18 出力で割って +4）= **202** | 49 倍 |
| B `ex17_window_overlap` | 1 出力（2 積） | 17/18 = **0.94**（91 命令 / 2 サブバンド / 18 出力） | ループ本体 40 命令 / 2 出力 = 20（＋テイル ≈ 2）= **22** | 23 倍 |
| C `ex17_polyphase` | 1 出力（16 タップ） | 21/8 = **2.63** | 16 タップ × **11 命令** = 176（＋関数の残り 24-11=13 を 16 出力で割って +1）= **177** | 67 倍 |

PIE 側の内訳（これが「何命令で済むか」の答え）:

- **A**: 324 MAC を 54 MAC 命令（1 命令 8 MAC、うち 24 レーン分は係数 0 のパッド）＋ 3 ゼロクリア ＋ 3
  読み出し ＋ 6 ストア ＋ 8 命令の前置/戻り = 74。
- **B**: 1 サブバンドで `EE.VMUL.S16` 4 本（`ovl*A1`, `sum*A2`, `sum*A1`, `ovl*A2`）、`VSUBS` 1 本、
  `VADDS` 1 本、パターン乗算 2 本、窓/パターンのロード 4 本、データロード 2 本、ベクトルストア 2 本
  = **17 命令**（＋ i=8 のスカラテイル ≈ 24 命令）。
- **C**: 16 MAC を 16 `LD.INCP` 融合命令（1 命令 8 MAC）＋ 1 ゼロクリア ＋ 1 読み出し ＋ 2 ストア
  ＋ 2 ループ = **21 命令 / 8 サンプル**。
- **C（融合ロードを使わない綴り、算術）**: タップごとに `EE.VLD.128.IP` 1 本と `EE.VSMULAS.S16.QACC`
  1 本を置き（ロードを 2 命令先に出す）、読み出し/ストア/ループは同じなら
  `16 + 16 + 1 + 2 + 2 = 37 命令 / 8 サンプル = **4.625 命令/サンプル**`。融合形はロードを命令に畳むので
  21 命令です。**どちらの綴りも「ロードの def は M 段、使う側は E 段」の距離 2 を保っているので、モデル
  上のストールは 0**（このカーネルは 2 本の転がりレジスタを交互に使うことで距離 2 を作っています）。
  融合形が速いのは命令数だけの話で、実機のストールは測っていません（§12 の 1 と 8）。

**ACCX にしない理由（この向き）**: `EE.VMULAS.S16.ACCX` は 8 レーンを**1 本に潰す**ので、1 命令で
出せるのは 1 サンプル分です。タップをレーンに載せるには「1 サンプルの 16 タップ」を 1 レジスタに
集める必要がありますが、脱インタリーブ状態ではタップが **16 B 間隔**なので 128bit ロード 1 本では
取れません（`EE.LDXQ.32` は 1 命令 1 レーン、p113）。一方「8 サンプルをレーンに、タップを broadcast」に
すると、**連続 8 サンプルが 1 ロード**（1 命令）＋ 係数は `sel8` の即値で選べる、というこのカーネルの形に
なります。位相の選び方（`win` ポインタ）を呼び出し側に任せているので、`mp3d_synth` の「位相ごとに
係数行が違う」構造はこのカーネルの外側（呼び出し側）の仕事です。

## 5. アセンブラとオブジェクトファイル（実コマンド）

```
$ source /opt/esp-idf/export.sh
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex17.o examples/firmware/main/proposed/ex17_mp3synth.S
（出力なし・終了ステータス 0）

$ xtensa-esp32s3-elf-nm -S /tmp/ex17.o
00000000 00000112 T ex17_imdct18
00000114 00000106 T ex17_window_overlap
0000021c 0000006b T ex17_polyphase
```

- `ex17_imdct18`: 274 B / **74 命令**（1 バンド = 18 出力 = 324 MAC）
- `ex17_window_overlap`: 262 B / **91 命令**（2 サブバンド = 36 出力。ループ本体は 223 B で
  `loopgtz` の 256 B 制限内ですが、このスイートの実測（docs/pie-simd.md §7）が差を出せていない
  `addi`/`bnez` を使っています）
- `ex17_polyphase`: 107 B / **31 命令**（ループ本体 21 命令 / 8 サンプル）

## 6. objdump -d（実コマンド、そのまま）

```
$ xtensa-esp32s3-elf-objdump -d /tmp/ex17.o

/tmp/ex17.o:     file format elf32-xtensa-le


Disassembly of section .iram1:

00000000 <ex17_imdct18>:
   0:	004136        	entry	a1, 32
   3:	057d      	mov.n	a7, a5
   5:	a38124        	ee.vld.128.ip	q5, a2, 16
   8:	b30124        	ee.vld.128.ip	q6, a2, 16
   b:	b38124        	ee.vld.128.ip	q7, a2, 16
   e:	830134        	ee.vld.128.ip	q0, a3, 16
  11:	250844        	ee.zero.qacc
  14:	a30134        	ee.vld.128.ip	q4, a3, 16
  17:	e0f02c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 0
  1b:	e2f12c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 1
  1f:	e0f22c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 2
  23:	e2f32c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 3
  27:	e0f42c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 4
  2b:	e2f52c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 5
  2f:	e0f62c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 6
  33:	e2f72c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 7
  37:	e0703c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 0
  3b:	e2713c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 1
  3f:	e0723c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 2
  43:	e2733c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 3
  47:	e0743c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 4
  4b:	e2753c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 5
  4f:	e0763c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 6
  53:	e2773c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 7
  57:	e0f03c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q7, 0
  5b:	e2f13c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q7, 1
  5f:	cdf274        	ee.srcmb.s16.qacc	q1, a7, 0
  62:	0c0144        	ee.st.qacc_l.l.128.ip	a4, 16
  65:	0d0144        	ee.st.qacc_h.l.128.ip	a4, 16
  68:	250844        	ee.zero.qacc
  6b:	e0f02c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 0
  6f:	e2f12c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 1
  73:	e0f22c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 2
  77:	e2f32c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 3
  7b:	e0f42c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 4
  7f:	e2f52c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 5
  83:	e0f62c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 6
  87:	e2f72c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 7
  8b:	e0703c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 0
  8f:	e2713c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 1
  93:	e0723c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 2
  97:	e2733c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 3
  9b:	e0743c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 4
  9f:	e2753c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 5
  a3:	e0763c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 6
  a7:	e2773c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 7
  ab:	e0f03c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q7, 0
  af:	e2f13c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q7, 1
  b3:	cdf274        	ee.srcmb.s16.qacc	q1, a7, 0
  b6:	0c0144        	ee.st.qacc_l.l.128.ip	a4, 16
  b9:	0d0144        	ee.st.qacc_h.l.128.ip	a4, 16
  bc:	250844        	ee.zero.qacc
  bf:	e0f02c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 0
  c3:	e2f12c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 1
  c7:	e0f22c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 2
  cb:	e2f32c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 3
  cf:	e0f42c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 4
  d3:	e2f52c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 5
  d7:	e0f62c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q5, 6
  db:	e2f72c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q5, 7
  df:	e0703c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 0
  e3:	e2713c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 1
  e7:	e0723c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 2
  eb:	e2733c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 3
  ef:	e0743c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 4
  f3:	e2753c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 5
  f7:	e0763c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q6, 6
  fb:	e2773c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q6, 7
  ff:	e0f03c3e 	ee.vsmulas.s16.qacc.ld.incp	q0, a3, q0, q7, 0
 103:	e2f13c3f 	ee.vsmulas.s16.qacc.ld.incp	q4, a3, q4, q7, 1
 107:	cdf274        	ee.srcmb.s16.qacc	q1, a7, 0
 10a:	0c0144        	ee.st.qacc_l.l.128.ip	a4, 16
 10d:	0d0144        	ee.st.qacc_h.l.128.ip	a4, 16
 110:	f01d      	retw.n
	...

00000114 <ex17_window_overlap>:
 114:	004136        	entry	a1, 32
 117:	0fd516        	beqz	a5, 218 <ex17_window_overlap+0x104>
 11a:	036d      	mov.n	a6, a3
 11c:	50c372        	addi	a7, a3, 80
 11f:	f80c      	movi.n	a8, 15
 121:	130380        	wsr.sar	a8
 124:	130c      	movi.n	a3, 1
 126:	113310        	slli	a3, a3, 15
 129:	fe7c      	movi.n	a14, -1
 12b:	e3ea      	add.n	a14, a3, a14
 12d:	603030        	neg	a3, a3
 130:	415150        	srli	a5, a5, 1
 133:	0e1516        	beqz	a5, 218 <ex17_window_overlap+0x104>
 136:	02ad      	mov.n	a10, a2
 138:	089a92        	l16si	a9, a10, 16
 13b:	189ab2        	l16si	a11, a10, 48
 13e:	209682        	l16si	a8, a6, 64
 141:	2196a2        	l16si	a10, a6, 66
 144:	830224        	ee.vld.128.ip	q0, a2, 32
 147:	838224        	ee.vld.128.ip	q1, a2, 32
 14a:	930164        	ee.vld.128.ip	q2, a6, 16
 14d:	938164        	ee.vld.128.ip	q3, a6, 16
 150:	a30164        	ee.vld.128.ip	q4, a6, 16
 153:	a38164        	ee.vld.128.ip	q5, a6, 16
 156:	82c980        	mull	a12, a9, a8
 159:	82dba0        	mull	a13, a11, a10
 15c:	be3084        	ee.vmul.s16	q6, q0, q2
 15f:	beb984        	ee.vmul.s16	q7, q1, q3
 162:	21cfc0        	srai	a12, a12, 15
 165:	21dfd0        	srai	a13, a13, 15
 168:	9e3184        	ee.vmul.s16	q2, q1, q2
 16b:	9eb884        	ee.vmul.s16	q3, q0, q3
 16e:	c0ccd0        	sub	a12, a12, a13
 171:	82d9a0        	mull	a13, a9, a10
 174:	828b80        	mull	a8, a11, a8
 177:	be7ed4        	ee.vsubs.s16	q6, q6, q7
 17a:	be9364        	ee.vadds.s16	q7, q3, q2
 17d:	43cce0        	min	a12, a12, a14
 180:	53cc30        	max	a12, a12, a3
 183:	21dfd0        	srai	a13, a13, 15
 186:	218f80        	srai	a8, a8, 15
 189:	be6684        	ee.vmul.s16	q6, q6, q4
 18c:	beef84        	ee.vmul.s16	q7, q7, q5
 18f:	dd8a      	add.n	a13, a13, a8
 191:	43dde0        	min	a13, a13, a14
 194:	53dd30        	max	a13, a13, a3
 197:	0854c2        	s16i	a12, a4, 16
 19a:	1854d2        	s16i	a13, a4, 48
 19d:	ba0244        	ee.vst.128.ip	q6, a4, 32
 1a0:	ba8244        	ee.vst.128.ip	q7, a4, 32
 1a3:	02ad      	mov.n	a10, a2
 1a5:	089a92        	l16si	a9, a10, 16
 1a8:	189ab2        	l16si	a11, a10, 48
 1ab:	209782        	l16si	a8, a7, 64
 1ae:	2197a2        	l16si	a10, a7, 66
 1b1:	830224        	ee.vld.128.ip	q0, a2, 32
 1b4:	838224        	ee.vld.128.ip	q1, a2, 32
 1b7:	930174        	ee.vld.128.ip	q2, a7, 16
 1ba:	938174        	ee.vld.128.ip	q3, a7, 16
 1bd:	a30174        	ee.vld.128.ip	q4, a7, 16
 1c0:	a38174        	ee.vld.128.ip	q5, a7, 16
 1c3:	82c980        	mull	a12, a9, a8
 1c6:	82dba0        	mull	a13, a11, a10
 1c9:	be3084        	ee.vmul.s16	q6, q0, q2
 1cc:	beb984        	ee.vmul.s16	q7, q1, q3
 1cf:	21cfc0        	srai	a12, a12, 15
 1d2:	21dfd0        	srai	a13, a13, 15
 1d5:	9e3184        	ee.vmul.s16	q2, q1, q2
 1d8:	9eb884        	ee.vmul.s16	q3, q0, q3
 1db:	c0ccd0        	sub	a12, a12, a13
 1de:	82d9a0        	mull	a13, a9, a10
 1e1:	828b80        	mull	a8, a11, a8
 1e4:	be7ed4        	ee.vsubs.s16	q6, q6, q7
 1e7:	be9364        	ee.vadds.s16	q7, q3, q2
 1ea:	43cce0        	min	a12, a12, a14
 1ed:	53cc30        	max	a12, a12, a3
 1f0:	21dfd0        	srai	a13, a13, 15
 1f3:	218f80        	srai	a8, a8, 15
 1f6:	be6684        	ee.vmul.s16	q6, q6, q4
 1f9:	beef84        	ee.vmul.s16	q7, q7, q5
 1fc:	dd8a      	add.n	a13, a13, a8
 1fe:	43dde0        	min	a13, a13, a14
 201:	53dd30        	max	a13, a13, a3
 204:	60d0d0        	neg	a13, a13
 207:	0854c2        	s16i	a12, a4, 16
 20a:	1854d2        	s16i	a13, a4, 48
 20d:	ba0244        	ee.vst.128.ip	q6, a4, 32
 210:	ba8244        	ee.vst.128.ip	q7, a4, 32
 213:	550b      	addi.n	a5, a5, -1
 215:	f1d556        	bnez	a5, 136 <ex17_window_overlap+0x22>
 218:	f01d      	retw.n
	...

0000021c <ex17_polyphase>:
 21c:	004136        	entry	a1, 32
 21f:	0fa062        	movi	a6, 15
 222:	415350        	srli	a5, a5, 3
 225:	05c516        	beqz	a5, 285 <ex17_polyphase+0x69>
 228:	a30134        	ee.vld.128.ip	q4, a3, 16
 22b:	a38134        	ee.vld.128.ip	q5, a3, 16
 22e:	830124        	ee.vld.128.ip	q0, a2, 16
 231:	838124        	ee.vld.128.ip	q1, a2, 16
 234:	250844        	ee.zero.qacc
 237:	e0702c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q4, 0
 23b:	e0796c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q4, 1
 23f:	e0722c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q4, 2
 243:	e07b6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q4, 3
 247:	e0742c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q4, 4
 24b:	e07d6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q4, 5
 24f:	e0762c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q4, 6
 253:	e07f6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q4, 7
 257:	e0f02c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q5, 0
 25b:	e0f96c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q5, 1
 25f:	e0f22c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q5, 2
 263:	e0fb6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q5, 3
 267:	e0f42c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q5, 4
 26b:	e0fd6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q5, 5
 26f:	e0f62c2e 	ee.vsmulas.s16.qacc.ld.incp	q0, a2, q0, q5, 6
 273:	e0ff6c2e 	ee.vsmulas.s16.qacc.ld.incp	q1, a2, q1, q5, 7
 277:	fd7264        	ee.srcmb.s16.qacc	q6, a6, 0
 27a:	0c0144        	ee.st.qacc_l.l.128.ip	a4, 16
 27d:	0d0144        	ee.st.qacc_h.l.128.ip	a4, 16
 280:	550b      	addi.n	a5, a5, -1
 282:	fae556        	bnez	a5, 234 <ex17_polyphase+0x18>
 285:	f01d      	retw.n
```

## 7. Python モデルと等価実行（実出力）

モデルは `/tmp/ex17_model.py`（標準ライブラリのみ。**全文は §14 の付録**）。層 1 は TRM の Operation 疑似コードから書いた
**命令の意味**（1 命令 1 関数、ページ番号は docstring に）、層 2 は **.S と同じ命令順・同じレジスタ役割
(q0..q7, a2..a7) で書いた 3 本**です。層 2 は層 1 の呼び出しを全部数えていて、上のコスト表はそこから
出ています。

参照実装は 3 つ:

1. **整数参照（Q15）** — 同じレイアウトを素直なループで書いたもの（命令順に依存しない）
2. **minimp3 の float 経路（倍精度）** — `L3_dct3_9`（1047-1085）を文単位で写し、`L3_imdct36` の
   バタフライ（1097-1104）・twiddle 回転（1136-1137）・窓（1138-1139）をそのまま実行したもの。
   `g_twid9`（1090-1092）と `g_mdct_window`（1196-1199）はソースの 10 進リテラルを倍精度で読んでいます
   （minimp3 は float として読むので、その差は Q15 の 1 LSB より下です）
3. **もう 1 つの整数綴り** — minimp3 の速い構造（折り畳み＋9 点 DCT＋回転）を整数でやったもの（T4/B の
   綴り違い）

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python /tmp/ex17_model.py
ex17_mp3synth model -- instruction semantics from data/pie_instructions.json Operation pseudo-code

T2 the composite IMDCT matrix (minimp3 L3_imdct36, unit probes in double)
   max |C| = 0.999048 -> table quantized as Q15; max |coefficient| = 32737 (int16 limit 32767)
   table rounding error: max |C*2^15 - round(C*2^15)| = 0.4726  (one LSB of the table = 1)
   64 random probes: max |C @ x - fold(x)| = 1.776e-15  -> the stage really is that matrix
   readout shift for a Q15 output: 15  (acc = (sum x*q) >> 15 = 32768 * float output)

T1 kernel A vs its Q15 integer reference : 512 cases, 0 mismatching cases
T3 kernel A vs minimp3's float path       : 9216 values, max |acc - float*32768| = 3.20 LSB, mean = 0.65 LSB
   the 16-bit register copy of EE.SRCMB would have clamped 2810 of 9216 lanes (acc itself is exact)
   ACCX spelling vs the kernel's QACC path : 64 spectra, 0 disagreeing (same integer result)
T4 kernel A vs the folded Q15 spelling    : 512 cases, 510 differ
   without butterfly saturation (219 cases): max difference = 3 LSB (rounding order only)
   with butterfly saturation (293 cases): the folded spelling puts co/si in int16 LANES, so its
   own butterfly sum overflows before the DCT runs; the matrix form has no such failure mode
   read footprint: x = 48 B at 0x3fca0000; tw = 896 B at 0x3fcb0000 (all 56 columns, incl. the two pad ones)

T5 kernel B vs its Q15 reference          : 1024 subbands, 0 bad
   read footprint per call (2 subbands): blocks = 128 B of 128-bit reads at 0x3fd00000 (the two
      records), win = 144 B at 0x3fd10000: bytes 0..67 of each 80-byte row (bytes 68..79 are never
      read), the last four via the scalar tail's two halfword loads)
   the inversion relation (odd subband = even subband with odd samples negated): 0 bad
   the two spellings of the inversion (pattern multiply vs signs baked into the window):
      512 of 1024 subbands differ; per-lane |difference| histogram over 18432 lanes:
      0 LSB x6540, 1 LSB x585, 2 LSB x2032, 65535 LSB x59
      (0 = the two agree; 1/2 = how many of the two floored window products round the wrong
      way; 65535 = a saturated -32768 against an exact negate, the two ends of the range)
   lanes where the saturating operators clamp, before the +-1 multiply: 262 of 4096
   lanes that are exactly -32768 (a saturating negate would return 32767 there): 118 of 1024
   vs minimp3's float window/overlap, small-magnitude half of the sweep: 4608 values,
      max = 1.94 LSB, mean = 0.66 LSB; the other 256 subbands are full-range (clamping,
      counted separately, not compared)

T6 kernel C vs its Q15 reference          : 3768 output samples, 0 bad
   vs the same 16-tap dot product in double: max |out - float*32768| = 1.00 LSB, mean = 0.50 LSB
   output samples the 16-bit readout copy would have clamped: 351 of 3768
   read footprint: v is 2048 bytes and the rolling pipeline reads 32 bytes past its end

per-call cost, from the model's own layer-1 counters (static counts are in the .md):
   A ex17_imdct18      (1 band,  18 outputs, 324 MACs) : 73 instructions total
         EE.SRCMB.S16.QACC                  3
         EE.SRS.ACCX                        0
         EE.ST.QACC_H.L.128.IP              3
         EE.ST.QACC_L.L.128.IP              3
         EE.VADDS.S16                       0
         EE.VLD.128.IP                      5
         EE.VMUL.S16                        0
         EE.VMULAS.S16.ACCX.LD.IP           0
         EE.VSMULAS.S16.QACC.LD.INCP        54
         EE.VST.128.IP                      0
         EE.VSUBS.S16                       0
         EE.ZERO.ACCX                       0
         EE.ZERO.QACC                       3
         entry                              1
         retw.n                             1
         wsr.sar                            0
   B ex17_window_overlap (2 subbands, 36 outputs)   : 34 instructions total
         EE.SRCMB.S16.QACC                  0
         EE.SRS.ACCX                        0
         EE.ST.QACC_H.L.128.IP              0
         EE.ST.QACC_L.L.128.IP              0
         EE.VADDS.S16                       2
         EE.VLD.128.IP                      12
         EE.VMUL.S16                        12
         EE.VMULAS.S16.ACCX.LD.IP           0
         EE.VSMULAS.S16.QACC.LD.INCP        0
         EE.VST.128.IP                      4
         EE.VSUBS.S16                       2
         EE.ZERO.ACCX                       0
         EE.ZERO.QACC                       0
         entry                              1
         retw.n                             0
         wsr.sar                            1
   C ex17_polyphase    (8 samples, 128 MACs)        : 25 instructions total
         EE.SRCMB.S16.QACC                  1
         EE.SRS.ACCX                        0
         EE.ST.QACC_H.L.128.IP              1
         EE.ST.QACC_L.L.128.IP              1
         EE.VADDS.S16                       0
         EE.VLD.128.IP                      4
         EE.VMUL.S16                        0
         EE.VMULAS.S16.ACCX.LD.IP           0
         EE.VSMULAS.S16.QACC.LD.INCP        16
         EE.VST.128.IP                      0
         EE.VSUBS.S16                       0
         EE.ZERO.ACCX                       0
         EE.ZERO.QACC                       1
         entry                              1
         retw.n                             0
         wsr.sar                            0

case dump for the host C verifier (/tmp/ex17_cases.txt): A=4 spectra, B=8 subbands, C=64 output samples
```

## 8. ホスト C 参照（コンパイル＋実出力）

`/tmp/ex17_ref.c`（**全文は §15 の付録**）は同じ仕様から C のループとして書いたスカラ Q15 参照です。ケースダンプ
（`/tmp/ex17_cases.txt`、A 4 スペクトル・B 4 ペア・C 4 グループ）を読み直して**全値を再計算**し、
モデルの出力と突き合わせます。

**この検査が最初の実行で捕まえたもの**: A と C で不一致が出て、中身は `dump -67036, reference 64036`
＝差がちょうど 2^17 でした。原因は C 側の `int` アキュムレータ（18 タップ × 32767² = 35bit が
32bit を超える）。`long long` にして 0 不一致になりました。**つまり「素直な C」は Q15 でも静かに
溢れ、ハードウェアの 40bit QACC は溢れない**、というのがこの例題で実際に見えた差です（float の
minimp3 には存在しない問題）。

```
$ cc -O2 -o /tmp/ex17_ref /tmp/ex17_ref.c && /tmp/ex17_ref /tmp/ex17_cases.txt

ex17_ref: host C reference vs the Python model's case dump
  A ex17_imdct18       : 4 spectra, 96 values, 0 mismatching values
  B ex17_window_overlap: 8 subbands, 200 values, 0 mismatching values
  C ex17_polyphase     : 4 groups of 16 samples, 64 values, 0 mismatching values
```

## 9. piesim.py での解釈実行（実出力）

`/workspace/pjs-vm/tools/pie/piesim.py` は **C のインライン asm 文字列**を読む作り（`extract_asm`）なので、
`.S` ファイルはそのままでは読めません。そこで

1. `/tmp/piesim_ex17.py` にコピーし、**この 3 本が使うのに未対応だった 3 命令**を TRM の疑似コードから
   足しました: `EE.VSMULAS.S16.QACC.LD.INCP`（p270）、`EE.ST.QACC_L.L.128.IP`（p139）、
   `EE.ST.QACC_H.L.128.IP`（p137）。README のとおり、未対応命令は `NotImplementedError` で止まるので、
   足すべき命令はそれで分かりました。
2. カーネルの**本体の命令列**をそのまま渡し、スカラ前置（`movi`/`srli`/`beqz`）は**その効果**（AR と SAR）を
   シミュレータの `ar` 辞書で与えました。piesim が持っているベース ISA は `mov`/`addi`/`bnez`/`loopgtz`/
   `wsr.sar` だけなので、これは「前置の意味を外から与えた」ものです（カーネルの仕様ではありません）。

足した 3 命令はこれだけです（TRM の疑似コードをそのまま Python にしたもの。`s16`/`self.ldq`/`arset` は
piesim 側の既存ヘルパ）:

```python
elif op == 'ee.vsmulas.s16.qacc.ld.incp':
    # EE.VSMULAS.S16.QACC.LD.INCP qu, as, qx, qy, sel8   (TRM p270)
    #   temp = qy[sel8]; QACC[l] = clamp(QACC[l] + qx[l]*temp, -2^39, 2^39-1)
    #   qu = load128({as[31:4],4{0}}); as += 16
    qu, asx, x, y, sel8 = qi(a[0]), a[1], Q[qi(a[2])], Q[qi(a[3])], int(a[4])
    temp = s16(y[sel8])
    self.qacc = [max(-(1 << 39), min((1 << 39) - 1, self.qacc[i] + s16(x[i]) * temp))
                 for i in range(8)]
    Q[qu] = self.ldq(arv(asx))
    arset(asx, arv(asx) + 16)
elif op == 'ee.st.qacc_l.l.128.ip':
    # EE.ST.QACC_L.L.128.IP as, imm   (TRM p139): QACC_L[127:0] (low 32 bits of lanes 0..3)
    a0 = arv(a[0]) & ~15
    for i in range(4):
        v = self.qacc[i] & 0xFFFFFFFF
        for n in range(4):
            self.mem[a0 + 4 * i + n] = (v >> (8 * n)) & 0xFF
    arset(a[0], arv(a[0]) + int(a[1]))
elif op == 'ee.st.qacc_h.l.128.ip':
    # EE.ST.QACC_H.L.128.IP as, imm   (TRM p137): the same for lanes 4..7
    a0 = arv(a[0]) & ~15
    for i in range(4):
        v = self.qacc[4 + i] & 0xFFFFFFFF
        for n in range(4):
            self.mem[a0 + 4 * i + n] = (v >> (8 * n)) & 0xFF
    arset(a[0], arv(a[0]) + int(a[1]))
```

（この 3 つは `elif op.endswith('.ld.incp')` の**前**に置く必要があります。後ろだと `VADDS/VSUBS/VMUL` の
融合形ハンドラが先に食います。最初はそれで `IndexError` になりました。）

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python /tmp/ex17_piesim.py
piesim.py (patched with three instructions from the TRM pseudo-code) vs this session's model
  ex17_polyphase : 256 output samples interpreted, 0 disagreeing
  ex17_imdct18   : 32 bands interpreted, 0 disagreeing
  (README.md of tools/pie: unsupported instructions raise NotImplementedError, which is how
   the three additions above were found -- the .S uses forms the simulator did not carry)
```

B（窓重ね）は piesim では走らせていません: スカラテイルが `mull`/`srai`/`min`/`max`/`l16si`/`s16i`/
`neg`（ベース ISA）を使い、piesim がそれを持たないためです。

## 10. 命令ごとの TRM `source_page`（`data/pie_instructions.json` より）

| 命令 | source_page | 構文 | Operation（抜粋） |
|---|---|---|---|
| `EE.VSMULAS.S16.QACC` | 269 | `EE.VSMULAS.S16.QACC qx, qy, sel8` | 1 temp[15:0] = qy[sel8*16+15:sel8*16] 2 QACC_L[ 39: 0] = min(max(QACC_L[ 39: 0] + qx[ 15: 0] * temp[15:0], -2^{39}), 2^{39}-1) 3 QACC_L[ 79: 40] = min(max(QACC_L[ 79: 40] + qx[ 31: 16] * temp[15:0], -2^{39}), 2^{39}-1) 4 QACC_L[119: 80] = min(max(QACC_L[119: 80] + qx[ 47: 32] * temp[15:0], -2^{39}), 2^{39}-1) 5 QACC_L[159:120] = min(max(QACC_L[159:120] + qx[ 63: 48] * temp[15:0], -2^{39}), 2^{39}-1) 6 QACC_H[ 39:  ... |
| `EE.VSMULAS.S16.QACC.LD.INCP` | 270 | `EE.VSMULAS.S16.QACC.LD.INCP qu, as, qx, qy, sel8` | 1 temp[15:0] = qy[sel8*16+15:sel8*16] 2 QACC_L[ 39: 0] = min(max(QACC_L[ 39: 0] + qx[ 15: 0] * temp[15:0], -2^{39}), 2^{39}-1) 3 QACC_L[ 79: 40] = min(max(QACC_L[ 79: 40] + qx[ 31: 16] * temp[15:0], -2^{39}), 2^{39}-1) 4 QACC_L[119: 80] = min(max(QACC_L[119: 80] + qx[ 47: 32] * temp[15:0], -2^{39}), 2^{39}-1) 5 QACC_L[159:120] = min(max(QACC_L[159:120] + qx[ 63: 48] * temp[15:0], -2^{39}), 2^{39}-1) 6 QACC_H[ 39:  ... |
| `EE.SRCMB.S16.QACC` | 130 | `EE.SRCMB.S16.QACC qu, as, 0` | 1 temp0[39:0] = QACC_L[ 39: 0] 2 ... 3 temp3[39:0] = QACC_L[159:120] 4 temp4[39:0] = QACC_H[ 39: 0] 5 ... 6 temp7[39:0] = QACC_H[159:120] 7 8 temp_shf0[39:0] = temp0[39:0] >> as[5:0] 9 temp_shf1[39:0] = temp1[39:0] >> as[5:0] 10 ... 11 temp_shf7[39:0] = temp7[39:0] >> as[5:0] 12 13 QACC_L[ 39: 0] = temp_shf0[39:0] 14 ... 15 QACC_L[159:120] = temp_shf3[39:0] 16 QACC_H[ 39: 0] = temp_shf4[39:0] 17 ... 18 QACC_H[159: ... |
| `EE.ST.QACC_L.L.128.IP` | 139 | `EE.ST.QACC_L.L.128.IP as, -2048..2032` | 1 QACC_L[127:0] => store128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {23{imm4[7]},imm4[7:0],2{0}} |
| `EE.ST.QACC_H.L.128.IP` | 137 | `EE.ST.QACC_H.L.128.IP as, -2048..2032` | 1 QACC_H[127: 0] => store128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {23{imm4[7]},imm4[7:0],2{0}} |
| `EE.VLD.128.IP` | 164 | `EE.VLD.128.IP qu, as, -2048..2032` | 1 qu[127:0] = load128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} |
| `EE.VST.128.IP` | 275 | `EE.VST.128.IP qv, as, -2048..2032` | 1 qv[127:0] => store128({as[31:4],4{0}}) 2 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} |
| `EE.VMUL.S16` | 198 | `EE.VMUL.S16 qz, qx, qy` | 1 qz[ 15: 0] = (qx[ 15: 0] * qy[ 15: 0]) >> SAR[5:0] 2 qz[ 31: 16] = (qx[ 31: 16] * qy[ 31: 16]) >> SAR[5:0] 3 qz[ 47: 32] = (qx[ 47: 32] * qy[ 47: 32]) >> SAR[5:0] 4 qz[ 63: 48] = (qx[ 63: 48] * qy[ 63: 48]) >> SAR[5:0] 5 qz[ 79: 64] = (qx[ 79: 64] * qy[ 79: 64]) >> SAR[5:0] 6 qz[ 95: 80] = (qx[ 95: 80] * qy[ 95: 80]) >> SAR[5:0] 7 qz[111: 96] = (qx[111: 96] * qy[111: 96]) >> SAR[5:0] 8 qz[127:112] = (qx[127:112] ... |
| `EE.VSUBS.S16` | 281 | `EE.VSUBS.S16 qa, qx, qy` | 1 qa[ 15: 0] = min(max(qx[ 15: 0] - qy[ 15: 0], -2^{15}), 2^{15}-1) 2 qa[ 31: 16] = min(max(qx[ 31: 16] - qy[ 31: 16], -2^{15}), 2^{15}-1) 3 ... |
| `EE.VADDS.S16` | 146 | `EE.VADDS.S16 qa, qx, qy` | 1 qa[ 15: 0] = min(max(qx[ 15: 0] + qy[ 15: 0], -2^{15}), 2^{15}-1) 2 qa[ 31: 16] = min(max(qx[ 31: 16] + qy[ 31: 16], -2^{15}), 2^{15}-1) 3 ... |
| `EE.ZERO.QACC` | 300 | `EE.ZERO.QACC` | 1 QACC_L = 0 2 QACC_H = 0 |
| `EE.VMULAS.S16.ACCX` | 210 | `EE.VMULAS.S16.ACCX qx, qy` | 1 add0[31:0] = qx[ 15: 0] * qy[ 15: 0] 2 add1[31:0] = qx[ 31: 16] * qy[ 31: 16] 3 ... 4 add7[31:0] = qx[127:112] * qy[127:112] 5 sum[40:0] = ACCX[39:0] + ad[31:0]d0[31:0] + ad[31:0]d1[31:0] + ... + ad[31:0]d 7[31:0] 6 7 ACCX[39:0] = min(max(sum[40:0], -2^{39}), 2^{39}-1) |
| `EE.VMULAS.S16.ACCX.LD.IP` | 211 | `EE.VMULAS.S16.ACCX.LD.IP qu, as, -512..496, qx, qy` | 1 add0[31:0] = qx[ 15: 0] * qy[ 15: 0] 2 add1[31:0] = qx[ 31: 16] * qy[ 31: 16] 3 ... 4 add7[31:0] = qx[127:112] * qy[127:112] 5 sum[40:0] = ACCX[39:0] + ad[31:0]d0[31:0] + ad[31:0]d1[31:0] + ... + ad[31:0]d 7[31:0] 6 ACCX[39:0] = min(max(sum[40:0], -2^{39}), 2^{39}-1) 7 8 qu[127:0] = load128({as[31:4],4{0}}) 9 as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} |
| `EE.SRS.ACCX` | 134 | `EE.SRS.ACCX au, as, 0` | 1 temp_shf[39:0] = ACCX[39:0] >> as[5:0] 2 ACCX = temp_shf[39:0] 3 au = min(max(temp_shf[39:0], -2^{31}), 2^{31}-1) |
| `EE.ZERO.ACCX` | 298 | `EE.ZERO.ACCX` | 1 ACCX = 0 |
| `EE.MOVI.32.Q` | 119 | `EE.MOVI.32.Q qu, as, 0..3` | 1 if sel4 == 0: 2 qu[ 31: 0] = as 3 if sel4 == 1: 4 qu[ 63: 32] = as 5 if sel4 == 2: 6 qu[ 95: 64] = as 7 if sel4 == 3: 8 qu[127: 96] = as |

`entry` / `retw.n` / `mov` / `addi` / `add` / `sub` / `neg` / `min` / `max` / `mull` / `srai` / `slli` /
`srli` / `l16si` / `s16i` / `extui` / `beqz` / `bnez` / `wsr.sar` はベース ISA なので
`data/pie_instructions.json`（PIE 章の 220 命令）に `source_page` はありません。`min`/`max`/`mull` は
実機で確認していません（§12）。

## 11. 期待値について: minimp3 の float 経路と一致しません（どこが何 bit 落ちるか）

**Q15 の整数カーネルなので、minimp3 の float 出力とはビット一致しません。** 一致を主張できるのは
「同じ線形写像の同じ段」であることまでで、差は次のとおり測ってあります（すべてモデルの実測）。

| カーネル | float 経路との差 | 何が差を作っているか |
|---|---|---|
| A | **max 3.20 LSB / 平均 0.65 LSB**（512 スペクトル × 18 値 = 9216 値、フルスケール入力） | ① 係数表の Q15 丸め（表自身の丸め誤差 ≤ 0.4726 LSB、`max|C| = 0.999048` なのでスケール段は不要）② 係数の積和を 1 回の `>> 15` で丸める（float は各積を丸めずに足す）。**構造上の飽和は無い**（40bit に 18 タップが収まる） |
| A（もう 1 つの整数綴り） | 飽和が無い 219 ケースで **max 3 LSB**（丸め順序だけの差）。ただし折り畳みを int16 レーンでやると**バタフライが飽和**し、512 ケース中 **293 ケース**で桁が変わる | 折り畳み形は `co`/`si` を 16bit レーンに置くので、`x1+x2` が 16bit を超えると自分のバタフライで壊れる。行列形はその失敗モードが無い（これが行列形を選んだ実益の 1 つ） |
| B | **max 1.94 LSB / 平均 0.66 LSB**（小振幅 256 サブバンド × 18 値 = 4608 値） | 積を 1 つずつシフトする（float は足してから 1 回丸める）。フルスケール側では**飽和加減算が 4096 レーン中 262 レーン**でクランプするので、そこは比較せず件数だけ出しています |
| C | **max 1.00 LSB / 平均 0.50 LSB**（3768 サンプル） | 16 タップを 40bit で厳密に足した後 `>> 15` を切り捨てる（float は丸める）。**丸め以外の損失は無い**（飽和しない） |

**ビット一致を要求するなら「一致しない」が答え**です。一致させるには (a) float を捨てて整数経路を
正とし、(b) 丸め順序（積ごとに丸めるか、和を取ってから 1 回丸めるか）を仕様として固定し、(c) 16bit の
出口に繋ぐ前にスケールを決める、の 3 つが要ります。この文書はその材料（どこで何 LSB 動くか）を出した
ところまでです。

## 12. 未確認の前提（正直な一覧）

**実機は使っていません。** 以下はすべて「動くと仮定しているが、シリコンで確かめていない」ものです。

1. **融合ロード形の意味**: `EE.VSMULAS.S16.QACC.LD.INCP qu, as, qx, qy, sel8`（p270）が「① MAC（ロード前の
   `qx` を読む）② `qu = load128` ③ `as += 16`」の順であること（TRM の疑似コードの順序）。**A と C の
   転がりレジスタはこの順序に依存**しています。逆順（ロードが先）なら両カーネルは別の計算になります。
2. **`EE.SRCMB.S16.QACC` が QACC を書き戻すこと**（p130 の `QACC_L[39:0] = temp_shf0[39:0]`）。この
   カーネルは「書き戻しを 2 つ目のシフトとして使う」ので、書き戻さない実装だと `acc` がシフト前の値に
   なります。ex09 (`srcmb_wb`) がこの点を実機で測るプローブです。
3. **`EE.ST.QACC_L.L.128.IP` / `_H_` の 4 int32 の並び**（p139/p137 の `QACC_L[127:0]`）と `as += imm`
   （`imm4` は 4 バイト刻み。この 2 本は `16`＝4 刻みを使っています）。A と C はこの 2 本でしか
   int32 を出していません。
4. **QACC 系の間合い**（`VSMULAS.QACC` → `SRCMB`）: このカーネルは間に他の命令が入る形
   （グループ境界で `ZERO.QACC`/`SRCMB`/ストア）なので、ex11 の 4 `nop` のような明示的な間隔は置いて
   いません。notes/08 の「実機の前に読むこと」のとおり、**QACC の読み出し間合いは ex09 の実機ログでまだ
   確定していない**（`mac1_min_gap=-1`）。A/C の正しさはこの点に依存しません（間合いが足りなければ
   遅くなるだけ）が、速さはそこに依存します。
5. **`min`/`max`/`mull`/`neg`/`l16si`/`s16i` のベース ISA の振る舞い**（B のスカラテイル）。とくに
   `neg` が 2 の補数反転（`-(-32768) = -32768`）であることは当たり前すぎて測っていません。
6. **読み出しフットプリント**: C の転がりパイプラインが `v` の末尾 32 B を読みます（使わない）。境界を
   またぐバッファで呼ぶときは呼び出し側が端を別扱いしてください。A は x の 48 B 全部と `tw` の 896 B
   全部を読みます（パッド列 2 本を含む）。B は行ごとに 0..67 B を読み、68..79 B（パッド）は読みません。
7. **`shift` の契約**: A の `shift` は 0..63（`as[5:0]`）。15 を渡すと Q15 出力になりますが、40bit が
   32bit の int32 に入るかは shift 次第で、**`acc` はシフト後の値の下位 32bit** です（上位 8bit は
   捨てています。§2.3 の 16bit クランプ件数とは別の話）。
8. **サイクル数は 1 つもありません。** `BENCH` 行は存在せず、上のコストはすべて**命令数**（objdump と
   モデルのカウンタ）です。`docs/pie-simd.md` の式（1 命令 1 サイクル + 0.6×ストア + ストール）を当てる
   なら、A ≈ 74 + 0.6×6 = 77.6、C ≈ 21 + 0.6×2 = 22.2 per 8 samples ですが、**転がりロードのストールは
   このスイートの静的表（Table 1.7-2）では 2 スロット空けてあるので 0 と見積もっているだけ**です。
9. **16bit の出口に繋ぐときのスケール**は決めていません（§2.3 の 30.5% クランプ）。Q15 のまま int16 に
   落とすと、フルスケールでは半分近くが飽和します。

## 13. ビルドへの入れ方（この文書では**適用していません**）

`proposed/` にあるので `examples/firmware/main/CMakeLists.txt` の `SRCS` に入っていません。本採用する
なら既存の例題と同じ手順（notes/08 の「サンプルの足し方」）:

1. `examples.h` に 3 つの宣言（引数とレイアウトをコメントで。上の契約表をそのまま移す）。
2. `main.c` に `ex17()`: 決定論的入力 → カーネル → C 参照 → `CHECK` → `BENCH` → `DATA`。
   `DATA` には **A は x と 18 値の acc、B は 2 サブバンドのレコードと win 行、C は状態と 16 サンプル**を
   出せば、ログ 1 枚でホスト側が再計算できます（§7 のケースダンプと同じ形）。
3. `tools/check_examples_log.py` に `check_ex17()`（この文書の参照実装と同じ Q15 ループ）と `CHECKS` への
   登録。
4. `tools/selftest_examples_checker.py` の合成ログに `DATA` 行と、それを壊す変異（例えば `sel8` を 1 つ
   ずらす）を 1 つ足す。
5. `CMakeLists.txt` の `SRCS` に 1 行、notes/08 の表と `examples/README.md` の一覧を更新。

## 14. 付録: モデル全文（`/tmp/ex17_model.py`）

層 1（命令の意味、§10 のページ番号つき）と層 2（.S と同じ命令順の 3 本）、整数参照・minimp3 の
float 経路（倍精度）・もう 1 つの整数綴り、T1〜T7 の掃引とケースダンプを 1 ファイルにしたものです。
`python3 ex17_model.py` で走ります（標準ライブラリのみ）。

```python
#!/usr/bin/env python3
"""ex17_mp3synth -- an independent model of the three kernels in ex17_mp3synth.S.

Two layers, on purpose:

  layer 1  instruction semantics, one function per PIE instruction, written from the Operation
           pseudo-code that data/pie_instructions.json records (TRM page numbers in the docstrings).
           The two properties that matter most are modelled explicitly: the 40-bit QACC lanes SATURATE,
           and EE.SRCMB.S16.QACC shifts QACC IN PLACE and clamps only the register copy.

  layer 2  the three kernels, written as the SAME sequence of instructions the .S emits -- same order,
           same register roles (q0..q7, a2..a7) -- so the two can be read side by side. Every layer-1
           call is counted; the cost table in ex17_mp3synth.md comes from those counters.

What is checked, and against what:

  T1  kernel A vs its own Q15 integer reference, over random and structured spectra
  T2  the composite IMDCT matrix: derived from unit probes through a DOUBLE copy of minimp3's scalar
      fold (L3_imdct36, minimp3.h:1087-1142), checked for linearity, then quantized
  T3  kernel A vs minimp3's float path (error in LSB), and the ACCX spelling of the same matrix run for
      real so the two accumulator choices are compared on numbers and not on prose
  T4  kernel A vs a SECOND integer spelling of the same stage (the folded Q15 butterfly path)
  T5  kernel B vs its Q15 reference, the exact frequency-inversion relation between an even and an odd
      subband, and the two spellings of the inversion (pattern multiply vs signs baked into the window)
  T6  kernel C vs its Q15 reference, vs the same 16-tap dot product in double, and the readout clamps
  T7  the read footprint of every kernel (how far past the declared buffers the rolling loads reach)

Runner: python3 /tmp/ex17_model.py    (standard library only; also writes the case dump that
/tmp/ex17_ref.c reads and that ex17_mp3synth.md pastes from)
"""
import random

Q15 = 32768
MASK16 = 0xFFFF


# --------------------------------------------------------------------------- layer 1: instructions
def s16(v):
    v &= MASK16
    return v - 0x10000 if v & 0x8000 else v


def sat16(v):
    return max(-32768, min(32767, v)) & MASK16


def clamp40(v):
    return max(-(1 << 39), min((1 << 39) - 1, v))


class Mem:
    """A byte buffer with a base address. A 128-bit PIE access forces as[3:0] to 0 (TRM p49), so the
    model rounds and RECORDS every access: the footprint report is this list."""

    def __init__(self, base, data):
        self.base = base
        self.data = bytearray(data)
        self.reads = []
        self.writes = []
        self.w32 = []

    def _check(self, a, n):
        if a < self.base or a + n > self.base + len(self.data):
            raise IndexError("access outside the buffer: addr=%#x n=%d base=%#x len=%d"
                             % (a, n, self.base, len(self.data)))
        return a - self.base

    def load16b(self, ptr):
        a = ptr & ~0xF
        off = self._check(a, 16)
        self.reads.append((a, 16))
        return bytes(self.data[off:off + 16])

    def store16b(self, ptr, val):
        a = ptr & ~0xF
        off = self._check(a, 16)
        self.writes.append((a, 16))
        self.data[off:off + 16] = bytes(val[:16])

    def store32b(self, ptr, val):
        a = ptr & ~0xF
        off = self._check(a, 16)
        self.w32.append((a, 16))
        self.data[off:off + 16] = bytes(val[:16])


def lanes_of(b):
    """16 bytes -> 8 unsigned 16-bit lanes, lane 0 = bits 15:0 (little-endian: the order the EE.VLD.128
    forms read memory in, the same convention ex10/ex11/ex15 use)."""
    return [b[2 * i] | (b[2 * i + 1] << 8) for i in range(8)]


def bytes_of(lanes):
    out = bytearray()
    for v in lanes:
        v &= MASK16
        out += bytes((v & 0xFF, v >> 8))
    return bytes(out)


def vld128_ip(mem, ptr, imm):
    """EE.VLD.128.IP qu, as, imm   (TRM p164): qu = load128({as[31:4],4{0}}); as += imm[7:0]<<4."""
    return lanes_of(mem.load16b(ptr)), ptr + imm


def vst128_ip(mem, ptr, lanes, imm):
    """EE.VST.128.IP qv, as, imm   (TRM p275)."""
    mem.store16b(ptr, bytes_of(lanes))
    return ptr + imm


def vsmulas_s16_qacc_ld_incp(mem, ptr, qx, qy, sel8, qacc):
    """EE.VSMULAS.S16.QACC.LD.INCP qu, as, qx, qy, sel8   (TRM p270)
       1  temp[15:0] = qy[sel8*16+15 : sel8*16]                  (ONE lane, broadcast)
       2  QACC[l] = clamp(QACC[l] + qx[l]*temp, -2^39, 2^39-1)   per lane, l = 0..7 (L for 0..3, H for 4..7)
       3  qu = load128({as[31:4],4{0}});  as += 16               (no immediate: ALWAYS 16)
       The MAC is steps 1-2 and the load is step 3, which is what makes the rolling register legal: the
       instruction reads the value its own load is about to replace."""
    temp = s16(qy[sel8])
    for l in range(8):
        qacc[l] = clamp40(qacc[l] + s16(qx[l]) * temp)
    return lanes_of(mem.load16b(ptr)), ptr + 16


def vmulas_s16_qacc(qx, qy, qacc):
    """EE.VMULAS.S16.QACC qx, qy   (TRM p215): the lane-by-lane MAC without the broadcast and without
    the fused load."""
    for l in range(8):
        qacc[l] = clamp40(qacc[l] + s16(qx[l]) * s16(qy[l]))
    return qacc


def vmulas_s16_accx(qx, qy, accx):
    """EE.VMULAS.S16.ACCX qx, qy   (TRM p210): ACCX = clamp(ACCX + sum over l of s16(qx[l])*s16(qy[l]))."""
    return clamp40(accx + sum(s16(qx[l]) * s16(qy[l]) for l in range(8)))


def vmulas_s16_accx_ld_ip(mem, ptr, qx, qy, accx, imm):
    """EE.VMULAS.S16.ACCX.LD.IP qu, as, imm, qx, qy   (TRM p211): the same sum, then
       qu = load128({as[31:4],4{0}}); as += imm."""
    accx = vmulas_s16_accx(qx, qy, accx)
    return lanes_of(mem.load16b(ptr)), ptr + imm, accx


def srs_accx(accx, shift):
    """EE.SRS.ACCX au, as, 0   (TRM p134): ACCX = ACCX >> as[5:0]; au = clamp32(ACCX) (32-bit clamp, and
    ACCX itself is written back)."""
    v = accx >> (shift & 63)
    return max(-(1 << 31), min((1 << 31) - 1, v))


def srcmb_s16_qacc(qacc, shift):
    """EE.SRCMB.S16.QACC qu, as, 0   (TRM p130)
       1  QACC[l] = QACC[l] >> as[5:0]            (in place, 40-bit lanes, no clamp)
       2  qu[l] = clamp(QACC[l], -2^15, 2^15-1)   (only the register copy is clamped)"""
    shifted = [v >> (shift & 63) for v in qacc]
    return [sat16(v) for v in shifted], shifted


def st_qacc_l_l_128_ip(mem, ptr, qacc, imm):
    """EE.ST.QACC_L.L.128.IP as, imm   (TRM p139): the LOW 32 bits of QACC lanes 0..3 -> four little
    endian int32 at {as[31:4],4{0}}, then as += imm."""
    b = bytearray()
    for l in range(4):
        b += (qacc[l] & 0xFFFFFFFF).to_bytes(4, "little")
    mem.store32b(ptr, bytes(b))
    return ptr + imm


def st_qacc_h_l_128_ip(mem, ptr, qacc, imm):
    """EE.ST.QACC_H.L.128.IP as, imm   (TRM p137): the same for lanes 4..7."""
    b = bytearray()
    for l in range(4, 8):
        b += (qacc[l] & 0xFFFFFFFF).to_bytes(4, "little")
    mem.store32b(ptr, bytes(b))
    return ptr + imm


def vmul_s16(qx, qy, sar):
    """EE.VMUL.S16 qz, qx, qy   (TRM p198): qz[l] = (s16(qx[l]) * s16(qy[l])) >> SAR[5:0], TRUNCATED to
    16 bits -- no saturation, no rounding."""
    return [((s16(qx[l]) * s16(qy[l])) >> sar) & MASK16 for l in range(8)]


def vsubs_s16(qx, qy):
    """EE.VSUBS.S16 (TRM p281): saturating signed subtract."""
    return [sat16(s16(qx[l]) - s16(qy[l])) for l in range(8)]


def vadds_s16(qx, qy):
    """EE.VADDS.S16 (TRM p146): saturating signed add."""
    return [sat16(s16(qx[l]) + s16(qy[l])) for l in range(8)]


def zero_qacc():
    return [0] * 8


OPS = {}


def _op(name, k=1):
    OPS[name] = OPS.get(name, 0) + k


# --------------------------------------------------------------------------- layer 2: the kernels
def ex17_imdct18(x, tw, shift, want_reads=False):
    """The .S, instruction by instruction.
       x: 24 int16; tw: 56 columns x 8 lanes = 448 int16; returns (acc[24 int32], footprint)."""
    xm = Mem(0x3FCA0000, bytes_of(x))
    twm = Mem(0x3FCB0000, bytes_of(tw))
    accm = Mem(0x3FCC0000, bytes(96))
    a2, a3, a4, a7 = xm.base, twm.base, accm.base, shift
    _op("entry")
    q5, a2 = vld128_ip(xm, a2, 16); _op("EE.VLD.128.IP")
    q6, a2 = vld128_ip(xm, a2, 16); _op("EE.VLD.128.IP")
    q7, a2 = vld128_ip(xm, a2, 16); _op("EE.VLD.128.IP")
    q0, a3 = vld128_ip(twm, a3, 16); _op("EE.VLD.128.IP")
    qacc = zero_qacc(); _op("EE.ZERO.QACC")
    q4, a3 = vld128_ip(twm, a3, 16); _op("EE.VLD.128.IP")
    for group in range(3):
        if group:
            qacc = zero_qacc(); _op("EE.ZERO.QACC")
        for k in range(18):
            src = q5 if k < 8 else (q6 if k < 16 else q7)
            sel8 = k if k < 8 else (k - 8 if k < 16 else k - 16)
            if k % 2 == 0:
                q0, a3 = vsmulas_s16_qacc_ld_incp(twm, a3, q0, src, sel8, qacc)
            else:
                q4, a3 = vsmulas_s16_qacc_ld_incp(twm, a3, q4, src, sel8, qacc)
            _op("EE.VSMULAS.S16.QACC.LD.INCP")
        q1, qacc = srcmb_s16_qacc(qacc, a7); _op("EE.SRCMB.S16.QACC")
        a4 = st_qacc_l_l_128_ip(accm, a4, qacc, 16); _op("EE.ST.QACC_L.L.128.IP")
        a4 = st_qacc_h_l_128_ip(accm, a4, qacc, 16); _op("EE.ST.QACC_H.L.128.IP")
    _op("retw.n")
    acc = [int.from_bytes(accm.data[4 * i:4 * i + 4], "little", signed=True) for i in range(24)]
    return (acc, xm.reads, twm.reads, accm.w32) if want_reads else acc


def ex17_imdct18_accx(x, twr, shift):
    """The ACCX spelling of the same 18x18 matrix, RUN so the .md can compare results and not only
    instruction counts: one output lane at a time, three 8-lane MACs per output (the table rows are 24
    lanes wide, lanes 18..23 zero), one EE.SRS.ACCX per output.
       twr: 18 rows x 24 lanes, row-major, 16-byte aligned (48-byte stride)."""
    xm = Mem(0x3FCA0000, bytes_of(x))
    tm = Mem(0x3FCB0000, bytes_of([v & MASK16 for v in twr]))
    _op("entry")
    q5, a2 = vld128_ip(xm, a2 := xm.base, 16); _op("EE.VLD.128.IP")
    q6, a2 = vld128_ip(xm, a2, 16); _op("EE.VLD.128.IP")
    q7, a2 = vld128_ip(xm, a2, 16); _op("EE.VLD.128.IP")
    out = []
    for j in range(18):
        accx = 0; _op("EE.ZERO.ACCX")
        for c, qx in enumerate((q5, q6, q7)):
            a = tm.base + 2 * (24 * j + 8 * c)
            qc, _ = vld128_ip(tm, a, 16)
            _op("EE.VMULAS.S16.ACCX.LD.IP")
            accx = vmulas_s16_accx(qx, qc, accx)
        out.append(srs_accx(accx, shift)); _op("EE.SRS.ACCX")
    _op("retw.n")
    return out


def ex17_window_overlap(blocks, win, n, want_reads=False):
    """The .S, instruction by instruction.
       blocks: n records of 32 int16; win: 2 rows of 40 int16; returns out (n records of 32 int16)."""
    bbytes = bytearray()
    for rec in blocks:
        bbytes += bytes_of(rec)
    wbytes = bytearray()
    for row in win:
        wbytes += bytes_of(row)
    bm = Mem(0x3FD00000, bytes(bbytes))
    wm = Mem(0x3FD10000, bytes(wbytes))
    om = Mem(0x3FD20000, bytes(64 * len(blocks)))
    a2, a3, a4, a5 = bm.base, wm.base, om.base, n
    _op("entry")
    if a5 == 0:
        return ([], bm.reads, wm.reads)
    a6 = a3                                   # row 0
    a7 = a3 + 80                              # row 1
    _op("wsr.sar")
    a8 = 1
    a3 = (a8 << 15) & 0xFFFFFFFF
    a14 = (a3 - 1) & 0xFFFFFFFF               # 32767
    a3 = (-a3) & 0xFFFFFFFF                   # -32768 (as a 32-bit register value)
    if a14 >= 0x80000000:
        a14 -= 1 << 32
    if a3 >= 0x80000000:
        a3 -= 1 << 32
    a5 >>= 1
    while a5:
        for rowptr in (a6, a7):
            base = a2
            ovl8 = int.from_bytes(bm.data[base - bm.base + 16:base - bm.base + 18], "little", signed=True)
            sum8 = int.from_bytes(bm.data[base - bm.base + 48:base - bm.base + 50], "little", signed=True)
            w8 = int.from_bytes(wm.data[rowptr - wm.base + 64:rowptr - wm.base + 66], "little", signed=True)
            w17 = int.from_bytes(wm.data[rowptr - wm.base + 66:rowptr - wm.base + 68], "little", signed=True)
            q0, a2 = vld128_ip(bm, a2, 32); _op("EE.VLD.128.IP")
            q1, a2 = vld128_ip(bm, a2, 32); _op("EE.VLD.128.IP")
            q2, _ = vld128_ip(wm, rowptr, 16); _op("EE.VLD.128.IP")
            q3, _ = vld128_ip(wm, rowptr + 16, 16); _op("EE.VLD.128.IP")
            q4, _ = vld128_ip(wm, rowptr + 32, 16); _op("EE.VLD.128.IP")
            q5, _ = vld128_ip(wm, rowptr + 48, 16); _op("EE.VLD.128.IP")
            tp = (ovl8 * w8) >> 15
            tq = (sum8 * w17) >> 15
            q6 = vmul_s16(q0, q2, 15); _op("EE.VMUL.S16")
            q7 = vmul_s16(q1, q3, 15); _op("EE.VMUL.S16")
            q2 = vmul_s16(q1, q2, 15); _op("EE.VMUL.S16")
            q3 = vmul_s16(q0, q3, 15); _op("EE.VMUL.S16")
            tail_present = tp - tq
            tq = (ovl8 * w17) >> 15
            t8 = (sum8 * w8) >> 15
            q6 = vsubs_s16(q6, q7); _op("EE.VSUBS.S16")
            q7 = vadds_s16(q3, q2); _op("EE.VADDS.S16")
            tail_present = max(-32768, min(32767, tail_present))
            tail_mirror = tq + t8
            q6 = vmul_s16(q6, q4, 0); _op("EE.VMUL.S16")
            q7 = vmul_s16(q7, q5, 0); _op("EE.VMUL.S16")
            tail_mirror = max(-32768, min(32767, tail_mirror))
            if rowptr == a7:
                tail_mirror = s16(-tail_mirror)           # two's complement negate: -(-32768) = -32768
            off = a4 - om.base
            om.data[off + 16:off + 18] = (tail_present & MASK16).to_bytes(2, "little")
            om.data[off + 48:off + 50] = (tail_mirror & MASK16).to_bytes(2, "little")
            a4 = vst128_ip(om, a4, q6, 32); _op("EE.VST.128.IP")
            a4 = vst128_ip(om, a4, q7, 32); _op("EE.VST.128.IP")
        a5 -= 1
    out = [[int.from_bytes(om.data[64 * r + 2 * i:64 * r + 2 * i + 2], "little", signed=True)
            for i in range(32)] for r in range(len(blocks))]
    return (out, bm.reads, wm.reads) if want_reads else out


def ex17_polyphase(v, win, n, want_reads=False):
    """The .S, instruction by instruction. v: n*16 int16 (group-major), win: 16 int16, out: n int32."""
    vm = Mem(0x3FE00000, bytes_of(v) + bytes(32))   # the contract: 32 readable bytes past the end
    wm = Mem(0x3FE10000, bytes_of(win))
    om = Mem(0x3FE20000, bytes(4 * n))
    a2, a3, a4, a5, a6 = vm.base, wm.base, om.base, n, 15
    _op("entry")
    a5 >>= 3
    if a5 == 0:
        return ([], vm.reads)
    q4, a3 = vld128_ip(wm, a3, 16); _op("EE.VLD.128.IP")
    q5, a3 = vld128_ip(wm, a3, 16); _op("EE.VLD.128.IP")
    q0, a2 = vld128_ip(vm, a2, 16); _op("EE.VLD.128.IP")
    q1, a2 = vld128_ip(vm, a2, 16); _op("EE.VLD.128.IP")
    while a5:
        qacc = zero_qacc(); _op("EE.ZERO.QACC")
        for t in range(16):
            creg = q4 if t < 8 else q5
            sel8 = t if t < 8 else t - 8
            if t % 2 == 0:
                q0, a2 = vsmulas_s16_qacc_ld_incp(vm, a2, q0, creg, sel8, qacc)
            else:
                q1, a2 = vsmulas_s16_qacc_ld_incp(vm, a2, q1, creg, sel8, qacc)
            _op("EE.VSMULAS.S16.QACC.LD.INCP")
        q6, qacc = srcmb_s16_qacc(qacc, a6); _op("EE.SRCMB.S16.QACC")
        a4 = st_qacc_l_l_128_ip(om, a4, qacc, 16); _op("EE.ST.QACC_L.L.128.IP")
        a4 = st_qacc_h_l_128_ip(om, a4, qacc, 16); _op("EE.ST.QACC_H.L.128.IP")
        a5 -= 1
    out = [int.from_bytes(om.data[4 * i:4 * i + 4], "little", signed=True) for i in range(n)]
    return (out, vm.reads) if want_reads else out


# ------------------------------------------------- the minimp3 stage in double (the float reference)
G_TWID9 = [0.73727734, 0.79335334, 0.84339145, 0.88701083, 0.92387953, 0.95371695, 0.97629601,
           0.99144486, 0.99904822, 0.67559021, 0.60876143, 0.53729961, 0.46174861, 0.38268343,
           0.30070580, 0.21643961, 0.13052619, 0.04361938]


def dct3_9(y):
    """L3_dct3_9, minimp3.h:1047-1085, statement for statement (minimp3 stores y[4] early and never
    overwrites it -- the same order is used here)."""
    y = list(y)
    s0, s2, s4, s6, s8 = y[0], y[2], y[4], y[6], y[8]
    t0 = s0 + s6 * 0.5
    s0 -= s6
    t4 = (s4 + s2) * 0.93969262
    t2 = (s8 + s2) * 0.76604444
    s6 = (s4 - s8) * 0.17364818
    s4 += s8 - s2
    s2 = s0 - s4 * 0.5
    y4 = s4 + s0
    s8 = t0 - t2 + s6
    s0 = t0 - t4 + t2
    s4 = t0 + t4 - s6
    s1, s3, s5, s7 = y[1], y[3], y[5], y[7]
    s3 *= 0.86602540
    t0 = (s5 + s1) * 0.98480775
    t4 = (s5 - s7) * 0.34202014
    t2 = (s1 + s7) * 0.64278761
    s1 = (s1 - s5 - s7) * 0.86602540
    s5 = t0 - s3 - t2
    s7 = t4 - s3 - t0
    s3 = t4 + s3 - t2
    return [s4 - s7, s2 + s1, s0 - s3, s8 + s5, y4, s8 - s5, s0 + s3, s2 - s1, s4 + s7]


def imdct_fold(x, overlap):
    """minimp3's L3_imdct36 (1087-1142) WITHOUT its window: the butterfly (1097-1104), the two 9-point
    DCTs (1106-1107), the sign flips (1109-1112) and the twiddle rotation (1136-1137). Returns
    (new_overlap, sum): the two 9-element vectors its window stage (1138-1139) consumes."""
    co = [0.0] * 9
    si = [0.0] * 9
    co[0] = -x[0]
    si[0] = x[17]
    for i in range(4):
        si[8 - 2 * i] = x[4 * i + 1] - x[4 * i + 2]
        co[1 + 2 * i] = x[4 * i + 1] + x[4 * i + 2]
        si[7 - 2 * i] = x[4 * i + 4] - x[4 * i + 3]
        co[2 + 2 * i] = -(x[4 * i + 3] + x[4 * i + 4])
    co = dct3_9(co)
    si = dct3_9(si)
    for i in (1, 3, 5, 7):
        si[i] = -si[i]
    new_ovl = [0.0] * 9
    summ = [0.0] * 9
    for i in range(9):
        summ[i] = co[i] * G_TWID9[9 + i] + si[i] * G_TWID9[i]
        new_ovl[i] = co[i] * G_TWID9[i] - si[i] * G_TWID9[9 + i]
    return new_ovl, summ


def imdct_matrix():
    C = [[0.0] * 18 for _ in range(18)]
    for k in range(18):
        e = [0.0] * 18
        e[k] = 1.0
        ovl, summ = imdct_fold(e, [0.0] * 9)
        for j in range(9):
            C[j][k] = ovl[j]
            C[9 + j][k] = summ[j]
    return C


def imdct_ref_float(x_q15):
    """The float path for a fresh state (overlap = 0): directly comparable with kernel A's output."""
    return imdct_fold([v / Q15 for v in x_q15], [0.0] * 9)


def window_ref_float(ovl_q15, sum_q15, win_unscaled):
    """minimp3's 1138-1139 in double, natural index order."""
    gr = [0.0] * 18
    for i in range(9):
        ovl = ovl_q15[i] / Q15
        s = sum_q15[i] / Q15
        gr[i] = ovl * win_unscaled[i] - s * win_unscaled[9 + i]
        gr[17 - i] = ovl * win_unscaled[9 + i] + s * win_unscaled[i]
    return gr


# --------------------------------------------------------------------------- Q15 references
def ref_imdct18(x, cq, shift):
    """The scalar Q15 reference for kernel A: the same integer dot products, no instruction order.
    Lanes 18..23 (the pad lanes of the kernel) are zero, like the kernel's zero coefficient columns."""
    return [(sum(s16(x[k]) * (cq[j][k] if j < 18 else 0) for k in range(18))) >> shift for j in range(24)]


def ref_window(ovl, summ, win, parity):
    """The scalar Q15 reference for kernel B, natural index order, same saturating semantics."""
    r = [0] * 18
    for i in range(9):
        pres = s16(sat16(((s16(ovl[i]) * win[i]) >> 15) - ((s16(summ[i]) * win[9 + i]) >> 15)))
        mirr = s16(sat16(((s16(ovl[i]) * win[9 + i]) >> 15) + ((s16(summ[i]) * win[i]) >> 15)))
        if parity == 1:
            if i % 2 == 1:
                pres = s16(-pres)          # exact: -(-32768) = -32768, as EE.VMUL.S16 by -1 gives
            if i % 2 == 0:
                mirr = s16(-mirr)
        r[i] = pres
        r[17 - i] = mirr
    return r


def ref_window_winbaked(ovl, summ, win, parity):
    """The OTHER spelling of the inversion: the signs baked into the window values, which is what a
    kernel would do if it could afford four window vectors per row."""
    r = [0] * 18
    for i in range(9):
        w_lo, w_hi = win[i], win[9 + i]
        sgn_p = -1 if (parity == 1 and i % 2 == 1) else 1
        sgn_m = -1 if (parity == 1 and i % 2 == 0) else 1
        pres = s16(sat16(((sgn_p * s16(ovl[i]) * w_lo) >> 15) - ((sgn_p * s16(summ[i]) * w_hi) >> 15)))
        mirr = s16(sat16(((sgn_m * s16(ovl[i]) * w_hi) >> 15) + ((sgn_m * s16(summ[i]) * w_lo) >> 15)))
        r[i] = pres
        r[17 - i] = mirr
    return r


def vstate(v, s, t):
    """The state layout of the contract: group-major, v[128*g + 8*t + j] = tap t of sample 8g+j."""
    return s16(v[128 * (s // 8) + 8 * t + (s % 8)])


def ref_polyphase(v, win, n):
    return [(sum(vstate(v, s, t) * s16(win[t]) for t in range(16))) >> 15 for s in range(n)]


# --------------------------------------------------------------------------- table helpers
def make_win_rows(win):
    """The win table kernel B reads: two rows of 40 lanes; row 1 carries the inversion patterns."""
    ones = [1] * 8
    p_first = [0xFFFF if i % 2 == 1 else 1 for i in range(8)]
    p_mirror = [0xFFFF if i % 2 == 0 else 1 for i in range(8)]
    common = ([s16(win[i]) & MASK16 for i in range(8)]
              + [s16(win[9 + i]) & MASK16 for i in range(8)])
    tails = [s16(win[8]) & MASK16, s16(win[17]) & MASK16] + [0] * 6
    row0 = common + ones + ones + tails
    row1 = common + p_first + p_mirror + tails
    return row0, row1


def block_record(ovl, summ):
    return ([(s16(v) & MASK16) for v in ovl] + [0x5A5A] * 7
            + [(s16(v) & MASK16) for v in summ] + [0xA5A5] * 7)


def natural_of_record(rec):
    """The documented reordering: plane0[n] = natural n (n = 0..8), plane1[i] = natural 17-i."""
    nat = [0] * 18
    for n in range(9):
        nat[n] = rec[n]
    for i in range(9):
        nat[17 - i] = rec[16 + i]
    return nat


def mdct_window_long():
    """minimp3's g_mdct_window[0] (line 1197): the long-block window, as Q15 int16."""
    w = [0.99904822, 0.99144486, 0.97629601, 0.95371695, 0.92387953, 0.88701083, 0.84339145,
         0.79335334, 0.73727734, 0.04361938, 0.13052619, 0.21643961, 0.30070580, 0.38268343,
         0.46174861, 0.53729961, 0.60876143, 0.67559021]
    return [max(-32768, min(32767, int(round(v * Q15)))) for v in w]


def rand16(rng, full=False):
    return rng.randint(-32768, 32767) if full else rng.randint(-4000, 4000)


def run():
    rng = random.Random(0x17E17)
    print("ex17_mp3synth model -- instruction semantics from data/pie_instructions.json Operation pseudo-code")
    print()

    # ------------------------------------------------------------------ T2: the composite matrix
    C = imdct_matrix()
    cmax = max(abs(c) for row in C for c in row)
    scale = 15
    while cmax * (1 << scale) > 32767 and scale > 0:
        scale -= 1
    shift = scale                      # acc = (sum x*q) >> scale == 32768 * (float output)
    Cq = [[int(round(C[j][k] * (1 << scale))) for k in range(18)] for j in range(18)]
    qerr = max(abs(C[j][k] * (1 << scale) - Cq[j][k]) for j in range(18) for k in range(18))
    lin = 0.0
    for _ in range(64):
        xf = [rand16(rng, True) / Q15 for _ in range(18)]
        ovl, summ = imdct_fold(xf, [0.0] * 9)
        for j in range(18):
            pred = sum(C[j][k] * xf[k] for k in range(18))
            lin = max(lin, abs(pred - (ovl[j] if j < 9 else summ[j - 9])))
    print("T2 the composite IMDCT matrix (minimp3 L3_imdct36, unit probes in double)")
    print("   max |C| = %.6f -> table quantized as Q%d; max |coefficient| = %d (int16 limit 32767)"
          % (cmax, scale, max(abs(c) for row in Cq for c in row)))
    print("   table rounding error: max |C*2^%d - round(C*2^%d)| = %.4f  (one LSB of the table = 1)"
          % (scale, scale, qerr))
    print("   64 random probes: max |C @ x - fold(x)| = %.3e  -> the stage really is that matrix" % lin)
    print("   readout shift for a Q15 output: %d  (acc = (sum x*q) >> %d = 32768 * float output)" % (shift, shift))
    print()

    # ------------------------------------------------------------------ T1/T3/T4: kernel A
    t1_n = t1_bad = 0
    f_err = []
    clamp16 = 0
    fold_n = fold_bad = 0
    fold_max = 0
    fold_sat = 0
    fold_clean_n = 0
    accx_bad = 0
    foot_tw = 0
    dump = open("/tmp/ex17_cases.txt", "w")
    cases = {"A": 0, "B": 0, "C": 0}
    for trial in range(512):
        if trial < 256:
            x = [rand16(rng, True) for _ in range(18)]
        elif trial < 288:
            x = [0] * 18
            x[rng.randrange(18)] = rng.choice([-32768, 32767, 1, -1])
        elif trial < 320:
            x = [(32767 if i % 2 else -32768) for i in range(18)]
        else:
            x = [rand16(rng) for _ in range(18)]
        tw = [0] * (56 * 8)
        for g in range(3):
            for k in range(18):
                for j in range(8):
                    jj = 8 * g + j
                    tw[144 * g + 8 * k + j] = Cq[jj][k] & MASK16 if jj < 18 else 0
        for i in range(54 * 8, 56 * 8):
            tw[i] = 0x7EE7
        acc, xr, twr, aw = ex17_imdct18(x + [0x5A5A] * 6, tw, shift, want_reads=True)
        want = ref_imdct18(x, Cq, shift)
        t1_n += 1
        if want != acc:
            t1_bad += 1
            if t1_bad == 1:
                print("   FIRST T1 MISMATCH x=%s want=%s got=%s" % (x[:3], want[:3], acc[:3]))
        ovl_f, summ_f = imdct_ref_float(x)
        for j in range(9):
            f_err.append(abs(acc[j] - ovl_f[j] * Q15))
            f_err.append(abs(acc[9 + j] - summ_f[j] * Q15))
        clamp16 += sum(1 for j in range(18) if abs(acc[j]) > 32767)
        # the folded Q15 spelling of the same stage
        sat = any(abs(a + b) > 32767 or abs(a - b) > 32767 or abs(c + d) > 32767 or abs(d - c) > 32767
                  for (a, b, c, d) in ((s16(x[4 * i + 1]), s16(x[4 * i + 2]), s16(x[4 * i + 3]),
                                        s16(x[4 * i + 4])) for i in range(4)))
        fo, fs = fold_q15_stage(x, Cq, scale, shift)
        fold_n += 1
        d = max([abs(fo[i] - acc[i]) for i in range(9)] + [abs(fs[i] - acc[9 + i]) for i in range(9)])
        if fo != acc[:9] or fs != acc[9:18]:
            fold_bad += 1
        if sat:
            fold_sat += 1
        else:
            fold_clean_n += 1
            fold_max = max(fold_max, d)
        foot_tw = max(foot_tw, (twr[-1][0] - 0x3FCB0000) + 16)
    # the ACCX spelling, on the same 512 spectra (row-major table, 24 lanes per row)
    twr_flat = [0] * (18 * 24)
    for j in range(18):
        for k in range(18):
            twr_flat[24 * j + k] = Cq[j][k] & MASK16
    for trial in range(64):
        x = [rand16(rng, True) for _ in range(18)]
        acc_q = ex17_imdct18(x + [0x5A5A] * 6, make_tw_columns(Cq), shift)
        acc_a = ex17_imdct18_accx(x + [0x5A5A] * 6, twr_flat, shift)
        if acc_q[:18] != acc_a:
            accx_bad += 1
    print("T1 kernel A vs its Q15 integer reference : %d cases, %d mismatching cases" % (t1_n, t1_bad))
    print("T3 kernel A vs minimp3's float path       : %d values, max |acc - float*32768| = %.2f LSB,"
          " mean = %.2f LSB" % (len(f_err), max(f_err), sum(f_err) / len(f_err)))
    print("   the 16-bit register copy of EE.SRCMB would have clamped %d of %d lanes (acc itself is exact)"
          % (clamp16, 18 * t1_n))
    print("   ACCX spelling vs the kernel's QACC path : 64 spectra, %d disagreeing (same integer result)"
          % accx_bad)
    print("T4 kernel A vs the folded Q15 spelling    : %d cases, %d differ" % (fold_n, fold_bad))
    print("   without butterfly saturation (%d cases): max difference = %d LSB (rounding order only)"
          % (fold_clean_n, fold_max))
    print("   with butterfly saturation (%d cases): the folded spelling puts co/si in int16 LANES, so its"
          % fold_sat)
    print("   own butterfly sum overflows before the DCT runs; the matrix form has no such failure mode")
    print("   read footprint: x = 48 B at %#x; tw = %d B at %#x (all 56 columns, incl. the two pad ones)"
          % (xr[0][0], foot_tw, twr[0][0]))
    print()

    # ------------------------------------------------------------------ T5: kernel B
    winq = mdct_window_long()
    win_float = [v / Q15 for v in winq]
    row0, row1 = make_win_rows(winq)
    b_n = b_bad = 0
    inv_bad = 0
    spelling_bad = 0
    spell_hist = {}
    sat_count = 0
    neg_count = 0
    b_out_domain = 0
    b_err = []
    for trial in range(512):
        small = trial < 256
        if small:
            ovl = [rand16(rng) for _ in range(9)]
            summ = [rand16(rng) for _ in range(9)]
        elif trial < 384:
            ovl = [rng.choice([-32768, 32767, 0, 1]) for _ in range(9)]
            summ = [rng.choice([-32768, 32767, 0, -1]) for _ in range(9)]
        else:
            ovl = [rand16(rng) for _ in range(9)]
            summ = [rand16(rng) for _ in range(9)]
        recs = [block_record(ovl, summ), block_record(ovl, summ)]
        out = ex17_window_overlap(recs, [row0, row1], 2)
        got_even = natural_of_record(out[0])
        got_odd = natural_of_record(out[1])
        b_n += 2
        if got_even != ref_window(ovl, summ, winq, 0) or got_odd != ref_window(ovl, summ, winq, 1):
            b_bad += 1
            if b_bad == 1:
                print("   FIRST T5 MISMATCH even=%s\n     want %s\n     got  %s"
                      % (ovl[:3], ref_window(ovl, summ, winq, 0)[:6], got_even[:6]))
        # the exact frequency-inversion relation: an odd subband must be the even result with its odd
        # SAMPLES negated (this is L3_change_sign, minimp3.h:1186-1192)
        for j in range(18):
            want_j = got_even[j] if j % 2 == 0 else s16(-got_even[j])
            if want_j != got_odd[j]:
                inv_bad += 1
                break
        # the two spellings of the inversion, and where they part company
        wb = ref_window_winbaked(ovl, summ, winq, 1)
        if got_odd != wb:
            spelling_bad += 1
        for j in range(18):
            d = abs(got_odd[j] - wb[j])
            spell_hist[d] = spell_hist.get(d, 0) + 1
        # the saturating operators: how many lanes hit the clamp before the +-1 multiply
        for j in range(9):
            p = ((s16(ovl[j]) * winq[j]) >> 15) - ((s16(summ[j]) * winq[9 + j]) >> 15)
            m = ((s16(ovl[j]) * winq[9 + j]) >> 15) + ((s16(summ[j]) * winq[j]) >> 15)
            sat_count += (abs(p) > 32767) + (abs(m) > 32767)
        # the value a saturating negate (EE.VSUBS.S16(0,x)) would lose: -32768 cannot be produced by it
        neg_count += sum(1 for j in range(18) if got_even[j] == -32768)
        if small:                       # |ovl|,|sum| <= 4000: no term can reach the clamp, so the
            fl = window_ref_float(ovl, summ, win_float)   # comparison is about rounding only
            for j in range(18):
                b_err.append(abs(got_even[j] - fl[j] * Q15))
        else:
            b_out_domain += 1
    print("T5 kernel B vs its Q15 reference          : %d subbands, %d bad" % (b_n, b_bad))
    bfp = ex17_window_overlap([block_record([0] * 9, [0] * 9)] * 2, [row0, row1], 2, want_reads=True)
    print("   read footprint per call (2 subbands): blocks = %d B of 128-bit reads at %#x (the two"
          % (2 * 64, bfp[1][0][0]))
    print("      records), win = %d B at %#x: bytes 0..67 of each 80-byte row (bytes 68..79 are never"
          % (max(a for a, _ in bfp[2]) - min(a for a, _ in bfp[2]) + 16, min(a for a, _ in bfp[2])))
    print("      read), the last four via the scalar tail's two halfword loads)")
    print("   the inversion relation (odd subband = even subband with odd samples negated): %d bad"
          % inv_bad)
    print("   the two spellings of the inversion (pattern multiply vs signs baked into the window):")
    print("      %d of %d subbands differ; per-lane |difference| histogram over %d lanes:"
          % (spelling_bad, b_n, 18 * b_n))
    print("      %s" % ", ".join("%d LSB x%d" % (k, spell_hist[k]) for k in sorted(spell_hist)))
    print("      (0 = the two agree; 1/2 = how many of the two floored window products round the wrong")
    print("      way; 65535 = a saturated -32768 against an exact negate, the two ends of the range)")
    print("   lanes where the saturating operators clamp, before the +-1 multiply: %d of %d"
          % (sat_count, 4 * b_n))
    print("   lanes that are exactly -32768 (a saturating negate would return 32767 there): %d of %d"
          % (neg_count, b_n))
    print("   vs minimp3's float window/overlap, small-magnitude half of the sweep: %d values,"
          % len(b_err))
    print("      max = %.2f LSB, mean = %.2f LSB; the other %d subbands are full-range (clamping,"
          % (max(b_err) if b_err else 0, sum(b_err) / len(b_err) if b_err else 0, b_out_domain))
    print("      counted separately, not compared)")
    print()

    # ------------------------------------------------------------------ T6: kernel C
    winv = [max(-32768, min(32767, int(round(0.5 * math.cos(3.14159265358979 * t / 16) * Q15))))
            for t in range(16)]
    c_n = c_bad = 0
    c_clamp = 0
    c_err = []
    foot_v = 0
    for trial in range(128):
        n = rng.choice([8, 16, 64])
        if trial < 64:
            v = [rand16(rng, True) for _ in range(16 * n)]
        else:
            v = [rand16(rng) for _ in range(16 * n)]
        out, vr = ex17_polyphase(v, winv, n, want_reads=True)
        want = ref_polyphase(v, winv, n)
        c_n += n
        if want != out:
            c_bad += n
            if c_bad == n:
                print("   FIRST T6 MISMATCH n=%d want=%s got=%s" % (n, want[:3], out[:3]))
        for s in range(n):
            fl = sum((vstate(v, s, t) / Q15) * (winv[t] / Q15) for t in range(16))
            c_err.append(abs(out[s] - fl * Q15))
        c_clamp += sum(1 for s in range(n) if abs(out[s]) > 32767)
        foot_v = max(foot_v, (vr[-1][0] - 0x3FE00000) + 16 - 32 * n)
    print("T6 kernel C vs its Q15 reference          : %d output samples, %d bad" % (c_n, c_bad))
    print("   vs the same 16-tap dot product in double: max |out - float*32768| = %.2f LSB, mean = %.2f LSB"
          % (max(c_err), sum(c_err) / len(c_err)))
    print("   output samples the 16-bit readout copy would have clamped: %d of %d" % (c_clamp, c_n))
    print("   read footprint: v is %d bytes and the rolling pipeline reads %d bytes past its end"
          % (16 * 64 * 2, foot_v))
    print()
    dump.write("A %d\n" % shift)
    for t in range(4):
        x = [rand16(rng, True) for _ in range(18)]
        twc = make_tw_columns(Cq)
        acc = ex17_imdct18(x + [-23130] * 6, twc, shift)
        dump.write("A_TW %s\n" % ",".join(str(s16(v)) for v in twc))
        dump.write("A_X %s\n" % ",".join(str(v) for v in x + [-23130] * 6))
        dump.write("A_ACC %s\n" % ",".join(str(v) for v in acc))
        cases["A"] += 1
    for t in range(4):
        ovl = [rand16(rng, True) for _ in range(9)]
        summ = [rand16(rng, True) for _ in range(9)]
        recs = [block_record(ovl, summ), block_record(ovl, summ)]
        out = ex17_window_overlap(recs, [row0, row1], 2)
        dump.write("B_BLOCKS %s\n" % ",".join(str(s16(v)) for r in recs for v in r))
        dump.write("B_WIN %s\n" % ",".join(str(s16(v)) for v in row0 + row1))
        dump.write("B_OUT %s\n" % ",".join(str(s16(v)) for r in out for v in r))
        cases["B"] += 2
    for t in range(4):
        n = 16
        v = [rand16(rng, True) for _ in range(16 * n)]
        out = ex17_polyphase(v, winv, n)
        dump.write("C_V %s\n" % ",".join(str(s16(x)) for x in v))
        dump.write("C_WIN %s\n" % ",".join(str(x) for x in winv))
        dump.write("C_OUT %s\n" % ",".join(str(x) for x in out))
        cases["C"] += n
    dump.close()
    print("per-call cost, from the model's own layer-1 counters (static counts are in the .md):")
    for label, fn in (("A ex17_imdct18      (1 band,  18 outputs, 324 MACs)",
                       lambda: ex17_imdct18([0] * 24, make_tw_columns(Cq), 15)),
                      ("B ex17_window_overlap (2 subbands, 36 outputs)  ",
                       lambda: ex17_window_overlap([block_record([0] * 9, [0] * 9)] * 2,
                                                   [row0, row1], 2)),
                      ("C ex17_polyphase    (8 samples, 128 MACs)       ",
                       lambda: ex17_polyphase([0] * 128, winv, 8))):
        before = dict(OPS)
        fn()
        d = {k: OPS[k] - before.get(k, 0) for k in OPS}
        print("   %s : %d instructions total" % (label, sum(d.values())))
        for k in sorted(d):
            print("         %-34s %d" % (k, d[k]))
    print()
    print("case dump for the host C verifier (/tmp/ex17_cases.txt): "
          "A=%d spectra, B=%d subbands, C=%d output samples" % (cases["A"], cases["B"], cases["C"]))


def make_tw_columns(Cq):
    """The kernel's tw table: column (g,k) at 144*g + 8*k, with the two pad columns present."""
    tw = [0] * (56 * 8)
    for g in range(3):
        for k in range(18):
            for j in range(8):
                jj = 8 * g + j
                tw[144 * g + 8 * k + j] = Cq[jj][k] & MASK16 if jj < 18 else 0
    return tw


def fold_q15_stage(xs, Cq, scale, shift):
    """The SECOND integer spelling of kernel A's stage: minimp3's fold done in Q15 lanes (saturating
    butterflies, the two 9-point DCTs, the twiddle rotation), with the twiddle table the same matrix was
    built from, so the two spellings differ only in WHERE the rounding happens. Returns (ovl, sum) in the
    same units as kernel A's acc (the <<scale makes the comparison value by value)."""
    return dct3_9_q15_q15([s16(v) for v in xs])


def dct3_9_q15_q15(xs):
    """The fold + 9-point DCT + rotation in Q15, in the same shape minimp3 writes it, with the twiddle
    table quantized from G_TWID9. co/si are int16 lanes (the butterfly saturates), the DCT constants and
    the twiddle are Q15 multiplies rounded at the shift, and the result is left in the same units as
    kernel A's acc (i.e. shifted by `scale` so the two can be compared value by value)."""
    co = [0] * 9
    si = [0] * 9
    co[0] = s16(-xs[0])
    si[0] = xs[17]
    for i in range(4):
        a, b = xs[4 * i + 1], xs[4 * i + 2]
        si[8 - 2 * i] = s16(sat16(a - b))
        co[1 + 2 * i] = s16(sat16(a + b))
        c, d = xs[4 * i + 3], xs[4 * i + 4]
        si[7 - 2 * i] = s16(sat16(d - c))
        co[2 + 2 * i] = s16(sat16(-(c + d)))
    co = dct3_9_q15(co)
    si = dct3_9_q15(si)
    for i in (1, 3, 5, 7):
        si[i] = -si[i]
    tw = [max(-32768, min(32767, int(round(v * Q15)))) for v in G_TWID9]
    ovl = [0] * 9
    summ = [0] * 9
    for i in range(9):
        # co/si are Q15-lane values; the products are Q30 -> one >> 15 brings them back, and the
        # remaining factor of 2^scale is the table scale the kernel's shift undoes
        ovl[i] = (co[i] * tw[i] - si[i] * tw[9 + i]) >> 15
        summ[i] = (co[i] * tw[9 + i] + si[i] * tw[i]) >> 15
    return ovl, summ


def dct3_9_q15(y):
    """L3_dct3_9 with its constants quantized to Q15 and every product shifted at the multiply, i.e. the
    integer spelling a PIE version of minimp3's fast DCT would have to use."""
    q = lambda c: max(-32768, min(32767, int(round(c * Q15))))
    c05, c939, c766, c173 = q(0.5), q(0.93969262), q(0.76604444), q(0.17364818)
    c866, c984, c342, c642 = q(0.86602540), q(0.98480775), q(0.34202014), q(0.64278761)
    y = list(y)
    s0, s2, s4, s6, s8 = y[0], y[2], y[4], y[6], y[8]
    t0 = s0 + ((s6 * c05) >> 15)
    s0 = s0 - s6
    t4 = ((s4 + s2) * c939) >> 15
    t2 = ((s8 + s2) * c766) >> 15
    s6 = ((s4 - s8) * c173) >> 15
    s4 = s4 + s8 - s2
    s2 = s0 - ((s4 * c05) >> 15)
    y4 = s4 + s0
    s8 = t0 - t2 + s6
    s0 = t0 - t4 + t2
    s4 = t0 + t4 - s6
    s1, s3, s5, s7 = y[1], y[3], y[5], y[7]
    s3 = (s3 * c866) >> 15
    t0 = ((s5 + s1) * c984) >> 15
    t4 = ((s5 - s7) * c342) >> 15
    t2 = ((s1 + s7) * c642) >> 15
    s1 = ((s1 - s5 - s7) * c866) >> 15
    s5 = t0 - s3 - t2
    s7 = t4 - s3 - t0
    s3 = t4 + s3 - t2
    return [s4 - s7, s2 + s1, s0 - s3, s8 + s5, y4, s8 - s5, s0 + s3, s2 - s1, s4 + s7]


import math

if __name__ == "__main__":
    run()
```

## 15. 付録: ホスト C 検証器（`/tmp/ex17_ref.c`）

```c
/* ex17_ref.c -- the host C side of the ex17 equivalence run.
 *
 * Two jobs:
 *   1. read the case dump the Python model writes (/tmp/ex17_cases.txt) and recompute every case with a
 *      scalar Q15 implementation written from the SAME specification the .S is written from, then report
 *      the number of values that disagree. This is the "third opinion": the model executes the kernels
 *      instruction by instruction, this program does the arithmetic in a plain C loop.
 *   2. provide the reference loops whose -O2 xtensa instruction counts ex17_mp3synth.md quotes next to
 *      the kernels' own counts.
 *
 * Build and run (host cc):        cc -O2 -o /tmp/ex17_ref /tmp/ex17_ref.c && /tmp/ex17_ref /tmp/ex17_cases.txt
 * Build for counting (xtensa):    xtensa-esp32s3-elf-gcc -O2 -c -o /tmp/ex17_ref.o /tmp/ex17_ref.c
 *                                  xtensa-esp32s3-elf-objdump -d /tmp/ex17_ref.o
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXV 4096

static int sat16(int v) { return v > 32767 ? 32767 : (v < -32768 ? -32768 : v); }
static int s16(int v) { v &= 0xFFFF; return v >= 0x8000 ? v - 0x10000 : v; }

/* ---------------------------------------------------------------- the three references
 * ref_imdct18: acc[j] = (sum over k of x[k]*tw[j][k]) >> shift, tw in the kernel's grouped layout.
 * The C reference walks the same layout so that a mistake in the layout shows up here as a mismatch. */
void ref_imdct18(const short *x, const short *tw, int *acc, int shift)
{
    int g, k, j;
    for (g = 0; g < 3; g++)
        for (j = 0; j < 8; j++) {
            int jj = 8 * g + j;
            long long s = 0;              /* NOT int: 18 x 32767 x 32767 needs 35 bits. A 32-bit
                                           * accumulator wraps silently here (the first run of this
                                           * verifier did, and the case dump caught it); the hardware's
                                           * 40-bit QACC lane does not, which is the whole point of it. */
            for (k = 0; k < 18; k++)
                s += (jj < 18) ? (long long)x[k] * (long long)tw[144 * g + 8 * k + j] : 0;
            acc[jj] = (int)(s >> shift);
        }
    for (j = 18; j < 24; j++) acc[j] = 0;        /* the pad lanes have zero coefficients */
}

/* ref_window_band: one subband of kernel B. row = the 40-lane window row for this subband's parity.
 * present[i] / mirror[i] exactly as the .S's header comment defines them; the kernel's negation is the
 * exact two's complement one (a multiply by -1), not a saturating subtract. */
void ref_window_band(const short *ovl, const short *sum, const short *row, short *present, short *mirror)
{
    const short *a1 = row, *a2 = row + 8, *pf = row + 16, *pm = row + 24;
    int i;
    for (i = 0; i < 8; i++) {
        int p = (((int)s16(ovl[i]) * (int)a1[i]) >> 15) - (((int)s16(sum[i]) * (int)a2[i]) >> 15);
        int m = (((int)s16(ovl[i]) * (int)a2[i]) >> 15) + (((int)s16(sum[i]) * (int)a1[i]) >> 15);
        p = sat16(p);
        m = sat16(m);
        present[i] = (short)(p * (int)pf[i]);             /* +-1: exact, including -32768 */
        mirror[i] = (short)(m * (int)pm[i]);
    }
    /* the ninth pair, the kernel's scalar tail (window values at row[32], row[33]) */
    {
        int w8 = row[32], w17 = row[33];
        int p = (((int)s16(ovl[8]) * w8) >> 15) - (((int)s16(sum[8]) * w17) >> 15);
        int m = (((int)s16(ovl[8]) * w17) >> 15) + (((int)s16(sum[8]) * w8) >> 15);
        present[8] = (short)sat16(p);
        m = sat16(m);
        mirror[8] = (short)(pm[0] == -1 ? -m : m);        /* the odd row's tail negates: natural 9 is odd */
    }
}

/* ref_polyphase_sample: one output sample of kernel C, the group-major state layout of the contract. */
int ref_polyphase_sample(const short *v, const short *win, int s)
{
    int t;
    long long acc = 0;                    /* 16 x 32767 x 32767 needs 34 bits: not an int (see above) */
    int g = s >> 3, j = s & 7;
    for (t = 0; t < 16; t++)
        acc += (long long)v[128 * g + 8 * t + j] * (long long)win[t];
    return (int)(acc >> 15);
}

/* ---------------------------------------------------------------- the case dump reader */
static int v[MAXV];
static int nv;

static void parse(const char *line)
{
    const char *p = strchr(line, ' ');
    nv = 0;
    if (!p) return;
    p++;
    if (*p == '\n' || !*p) {                       /* a bare "A 15" line: one number */
        v[nv++] = atoi(p);
        return;
    }
    while (*p && *p != '\n' && nv < MAXV) {
        v[nv++] = atoi(p);
        p = strchr(p, ',');
        if (!p) break;
        p++;
    }
}

int main(int argc, char **argv)
{
    FILE *f = fopen(argc > 1 ? argv[1] : "/tmp/ex17_cases.txt", "r");
    char line[65536];
    static short x[24], tw[56 * 8], rec[64], row[80], pv[256], pw[16];
    static int a_got[24], a_want[24], c_got[16], c_want[16];
    static short b_got[64], b_want[64];
    int a_shift = 15;
    long a_cases = 0, a_bad = 0, b_cases = 0, b_bad = 0, c_cases = 0, c_bad = 0;
    long a_vals = 0, b_vals = 0, c_vals = 0;
    int have_x = 0, i;

    if (!f) { perror("open"); return 1; }
    while (fgets(line, sizeof line, f)) {
        if (!strncmp(line, "A ", 2)) {
            parse(line); a_shift = v[0];
        } else if (!strncmp(line, "A_X ", 4)) {
            parse(line);
            for (i = 0; i < 24; i++) x[i] = (short)v[i];
            have_x = nv;
        } else if (!strncmp(line, "A_ACC ", 6)) {
            parse(line);
            for (i = 0; i < 24 && i < nv; i++) a_got[i] = v[i];
            a_cases++;
            ref_imdct18(x, tw, a_want, a_shift);
            for (i = 0; i < 24; i++) {
                a_vals++;
                if (a_got[i] != a_want[i]) {
                    if (a_bad < 8)
                        printf("A  mismatch lane %2d: dump %d, reference %d\n", i, a_got[i], a_want[i]);
                    a_bad++;
                }
            }
            have_x = 0;
        } else if (!strncmp(line, "A_TW ", 5)) {
            parse(line);
            for (i = 0; i < 56 * 8 & i < nv; i++) tw[i] = (short)v[i];
        } else if (!strncmp(line, "B_BLOCKS ", 9)) {
            parse(line);
            for (i = 0; i < 64 && i < nv; i++) rec[i] = (short)v[i];
        } else if (!strncmp(line, "B_WIN ", 6)) {
            parse(line);
            for (i = 0; i < 80 && i < nv; i++) row[i] = (short)v[i];
        } else if (!strncmp(line, "B_OUT ", 6)) {
            parse(line);
            for (i = 0; i < 64 && i < nv; i++) b_got[i] = (short)v[i];
            for (int band = 0; band < 2; band++) {
                short present[9], mirror[9];
                const short *r = &rec[32 * band], *rr = &row[40 * band];
                ref_window_band(r, r + 16, rr, present, mirror);
                for (i = 0; i < 9; i++) {
                    b_want[32 * band + i] = present[i];
                    b_want[32 * band + 16 + i] = mirror[i];
                }
                b_cases++;
            }
            for (i = 0; i < 64; i++) {
                if (i % 32 >= 9 && i % 32 < 16) continue;   /* the pad lanes are not written */
                b_vals++;
                if (b_got[i] != b_want[i]) {
                    if (b_bad < 8)
                        printf("B  mismatch record %d lane %2d: dump %d, reference %d\n",
                               i / 32, i % 32, b_got[i], b_want[i]);
                    b_bad++;
                }
            }
        } else if (!strncmp(line, "C_V ", 4)) {
            parse(line);
            for (i = 0; i < 256 && i < nv; i++) pv[i] = (short)v[i];
        } else if (!strncmp(line, "C_WIN ", 6)) {
            parse(line);
            for (i = 0; i < 16 && i < nv; i++) pw[i] = (short)v[i];
        } else if (!strncmp(line, "C_OUT ", 6)) {
            parse(line);
            for (i = 0; i < 16 && i < nv; i++) c_got[i] = v[i];
            c_cases++;
            for (i = 0; i < 16; i++) {
                c_want[i] = ref_polyphase_sample(pv, pw, i);
                c_vals++;
                if (c_got[i] != c_want[i]) {
                    if (c_bad < 8)
                        printf("C  mismatch sample %2d: dump %d, reference %d\n", i, c_got[i], c_want[i]);
                    c_bad++;
                }
            }
        }
    }
    fclose(f);
    printf("\nex17_ref: host C reference vs the Python model's case dump\n");
    printf("  A ex17_imdct18       : %ld spectra, %ld values, %ld mismatching values\n",
           a_cases, a_vals, a_bad);
    printf("  B ex17_window_overlap: %ld subbands, %ld values, %ld mismatching values\n",
           b_cases, b_vals, b_bad);
    printf("  C ex17_polyphase     : %ld groups of 16 samples, %ld values, %ld mismatching values\n",
           c_cases, c_vals, c_bad);
    return (a_bad || b_bad || c_bad) ? 1 : 0;
}
```

---

**この文書の数字はすべて** (a) アセンブラとオブジェクトファイル（§5/§6）、(b) `/tmp/ex17_model.py` の
実行出力（§7）、(c) `cc -O2` でビルドした C 検証器の出力（§8）、(d) `piesim.py` の解釈実行（§9）の
いずれかから来ています。Q15 と float の差は「一致しない」と書いて LSB で出しました（§11）。
