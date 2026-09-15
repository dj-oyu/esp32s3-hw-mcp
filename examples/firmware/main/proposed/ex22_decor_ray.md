# ex22 (proposed) — flower の装飾光線（decorative god rays）のピクセルパスを PIE のレーンに載せる

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex22_decor_ray.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 6 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `fac640dd8c197990d3f9e1aa66d04e7a` → `794725fe388bf5a7915f6d9f21429f8b`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex22_decor_ray.S` とこの文書は**ビルドに入っていない**。`main/CMakeLists.txt` は
`ex01`〜`ex12` だけを列挙しており（`examples/firmware/main/CMakeLists.txt`）、`proposed/` のどのファイル（ex13 以降と本ファイル）も
そこには無い。呼び出し元も無い。対象のスカラーコードは**別リポジトリ**（`cardputer-adv-pocketjs`）にあり、そちらは一切触っていない。

測ったのは**命令数とレーンの意味**であって、時間ではない。実機（`/dev/ttyACM0`）は使っていない（親が占有）。したがってこの文書に
サイクルの実測値は 1 つも無く、サイクルとして書いてある数字はすべて**推定**で、その旨をその場に書く。

## 何が確認済みで、何が未確認か (status, honestly)

| 項目 | 状態 |
| --- | --- |
| アセンブル | **確認済み**。`xtensa-esp32s3-elf-as`（esp-15.2.0_20251204）が通る。警告なし |
| オブジェクトの中身（関数サイズ・命令・定数表） | **確認済み**。`nm`/`objdump` の生出力を §4 に貼る |
| レーンの意味（A/B/C が契約どおりか） | **確認済み（モデル上）**。アセンブラ出力を 1 命令ずつ実行するレーンモデルで、掃引 160,000 px（A/B 各）＋80,000 px（C）＋チャネル別網羅 589,824 ケースが**不一致 0**（§5） |
| 独立した三つ目の意見 | **確認済み**。ホスト C 参照（`gcc -O2`）と Python スカラー参照が同じ入力で一致（§5 の `[1a]`〜`[1c]`） |
| piesim での解釈実行 | **確認済み（ただし /tmp のコピーに 4 点の追加が要る）**。生の `tools/pie/piesim.py` は `NotImplementedError(entry)` で止まる。追加点と差分は §6 |
| `EE.VMUL.U16` の読み方（本カーネルの要） | **実機で確定済み**（ex08 の装置ラン）。ただし**本カーネルの鎖**での確認ではない（§6 の D） |
| `EE.VZIP.16` のレーン順 | **未確認**。TRM p293 の Operation 疑似コードから実装した。装置で確かめた記録はこのリポジトリに無い。カーネル A のカバレッジ展開がこれに依存する（§11） |
| `EE.SRCMB.S16.QACC` の AR シフトと 16bit 読み出し飽和 | **未確認**（このリポジトリで実機ログが無い。`notes/08-media-3d-perf.md` の「実機の前に読むこと」が ex09 待ちと書いている系統）。カーネル B がこれに依存する |
| サイクル・フレーム時間 | **未計測**。§7 は命令数の算術だけ |
| 実機ラン | **していない** |

## 0. 対象: 実コードのどこか、そして ①置き換えられるかの判定

flower の装飾層は**行ごとに 1 回、1 行につき最大 4 回**走る（4 つの opportunity）:

```
flower.c:1532        garden_row_blend(row,py,garden,bloom_garden_old_seed,bloom_garden_mix);   <- strip の行ごと
garden.c:1578-1580   garden_row_blend -> garden_atmosphere_row
garden.c:1571        garden_decor_row(row,y,f);                                               <- 装飾パス全体
garden.c:1466-1481   for(x=lo;x<=hi;x++) { ...profile... row[x]=garden_decor_mix(row[x],light,shadow,d); }
```

そのピクセルループの中身と、行内で何が変わるか:

| 量 | 出典 | 行内で変化するか |
| --- | --- | --- |
| `light = garden_decor_profile(x*256-cx,inv)*gain>>8` | `garden.c:1473` | **ピクセルごとに変わる**（断面プロファイル。中心からの距離が x で動く） |
| `shadow = garden_decor_profile(x*256-shadow_cx,shadow_inv)*gain>>8` | `garden.c:1474` | **ピクセルごとに変わる**（2 本目のプロファイル） |
| 保護マスク `gap`/`protect` | `garden.c:1469-1471` | **ピクセルごとに変わる**。ただし入力は行ごとの `center`/`half`（`garden_shaft(y,f,...)`, `garden.c:1431`、定義は `garden.c:322-327`） |
| ディザ `d = garden_dither(x,y)*64+32` | `garden.c:1479` | **ピクセルごとに変わる** |
| RGB565 合成 `garden_decor_mix` | `garden.c:1480`（定義 `1403-1415`） | ピクセルごと |
| `cloud`（共有ノイズ場） | `garden.c:1449` | **行ごとに一定**（`flower-decor-rays.md`: "Noise is sampled per row, not per pixel"） |
| `cx` / `radius` / `shadow_cx` / `inv,shadow_inv` | `garden.c:1455` / `1458` / `1460` / `1465` | 行ごとに一定。**除算はここだけ**（`flower-decor-cost.md:30` の「Two variable divisions are per active ray-row, not per pixel」） |
| `strength`（cloud × fade × depth × arrival） | `garden.c:1444-1450` | 行ごとに一定 |
| `arrival`（上から開く軟らかい前線） | `garden.c:1441`、定義 `1418-1428`（`duration` での除算は 1423） | 行ごとに一定 |
| `end`（開口部の終端行） | `garden.c:1437` | 行ごとに一定 |
| `fade`・`y>=end`・`arrival==0` の 3 つの continue | `garden.c:1436-1443`、`fade` は `1380-1391` | 行ごとの判定（4 層分） |
| `lo..hi`（クリップ済みスパン） | `garden.c:1461-1464` | 行ごと。1 行の最大は 4 × 203 px（`flower-decor-rays.md`） |

**判定（①）: プロファイルは「ピクセルごとに変わり、パラメータは行ごとに一定」である。** したがって `lo..hi` の内側
（1 層・1 行あたり最大 203 px、約 25 ブロック）は丸ごとレーンに載せられる。載せられないのは行ヘッダ（4 層ぶんのハッシュ・
lifetime 判定・arrival の除算・行ごとの定数の構築・スパンのクリップ）で、こちらは §8 に列挙する。

この「行ごとに一定 / ピクセルごとに変わる」の切り分けが結論を決める。もしプロファイルも行ごとに一定なら、カーネル B は
行あたり 1 回のスカラー評価で済み、レーンに載せる意味はほぼ無い。逆にパラメータがピクセルごとに変わると（`cx` が x で動く
など）、行内でブロードキャストを作り直すことになりこれも割に合わない。実コードは**両方の間の、いちばん都合のよい形**をしている。

## 1. 三つのカーネルと契約

```
void     ex22_decor_blend(int16_t *fb, const uint8_t *coverage, const int16_t *tint8, uint32_t n_px, uint32_t shift);
void     ex22_decor_coverage(int16_t *cov_out, const int16_t *ix, uint32_t n_px, uint32_t shift);
uint32_t ex22_decor_rowmask(const int16_t *cov, const int16_t *protect, uint32_t n_px);
```

A は `garden_decor_mix`（`garden.c:1403-1415`）と、その入力である 2 本のプロファイルの**合成**を担う。B は
`garden_decor_profile`（`garden.c:1397-1402`）を担う。C は `garden.c:1475` のピクセルごとの `continue`
（`if(!light&&!shadow)continue;`）を、行単位の判定へ置き換えるために在る。

契約（`.S` のヘッダにも同じことを書いてある）:

| | A | B | C |
| --- | --- | --- | --- |
| アラインメント | `fb` 16B、`coverage` 8B、`tint8` 16B | `cov_out` 16B、`ix` 16B | `cov` 16B、`protect` 16B |
| `n_px` | 8 の倍数（スカラの頭・尾はカーネルに無い。呼び手が持つ） | 同左 | 8 の倍数、かつ 31 ブロック以下（戻りが 32bit） |
| レーンの意味 | `coverage` は 8bit/px で 0..255（0=無変化）、`tint8` は 16bit レーンに 0..255 | `ix` は**符号付き Q11** の正規化距離。\|ix\| ≤ 2048（2048=縁、0=軸） | `cov` 0..255、`protect` 0..255 |
| `shift` | SAR。**契約は 8** | `EE.SRCMB.S16.QACC` の AR。**契約は 14** | 無し |
| 数式 | `att=256-c; R=min(31,(r*att>>8)+(t>>3)); G=min(63,(g*att>>8)+(t>>4)); B=min(31,(b*att>>8)+(t>>5)); out=(R<<11)\|(G<<5)\|B` | `u=\|ix\|; cov=min(255, 256-(u*u>>14))` | 生存 = `cov>=8 && protect>=32`、bit b = ブロック b に生存レーンあり |

B の正規化は**呼び手のランプの傾きに畳んである**（1 ピクセル 1 除算を消すため）:

```
W = 開口部の半幅（Q8、garden.c の radius*256。18..31 px → 4608..7936）
K = floor(524288 / W)        ← Q11 の傾き
C = floor(cx * K / 256)      ← 軸の位置（cx は Q8）
ix[x] = clip(x*K - C, -2048, 2048)      ← ブロックごとに 1 回の加算（走査線ランプ）
```

`ix` を配列で渡す契約にしてあるが、呼び手がランプを走らせるなら 1 ブロック 1 命令の加算で済む（メモリのロードと同額）。
**傾きを Q7 にすると floor が効きすぎる**（K は 18..31 px で 4..7 にしかならないので 1 の量子化が 15% になる）。これは
モデルが捕まえた 3 件目の欠陥で、Q11 と `EE.VMULAS.U16.QACC`（40bit で厳密）に変えてある。

## 2. 設計判断 — なぜその命令か（設問への答え）

### A) パックされた RGB565 をどう扱ったか: **チャネルごとにマスクして分離した**（16bit 全体に近似係数を掛ける案は成立しない）

最初に試すべき形は「`EE.VMUL.U16` をパック語に 1 回」だが、**それは書けない**。理由は幅の算術で、モデルで裏を取った:

```
(p & 0xF800) = r<<11, r ≤ 31  →  0xF800*255 = 16,189,440 = 16bit レーンの 247 倍
```

R フィールドだけで積が 2^16 を 247 倍超える。TRM p204 の Operation は
`qz[15:0] = (qx[15:0]*qy[15:0]) >> SAR[5:0]` の形をしており、`>> 8` を掛けた位置に R を残すには
積の bit 15:11 を読むことになるが、`(p*att)>>8 = r*att*8 + floor((g*att*32 + b*att)/256)` なので
R フィールドの下位ビットが**繰り上がって**入り、G・B は上位 5〜6bit が別のチャネルの残差で埋まる。
実測（§5 の `[4a]`, (p,c) 393,216 通り）: **一致 23.2%、不一致 76.8%**、チャネル別最大誤差 **57**（G は 63 段のうち）、
誤差の分布は 4〜57 に広がる（丸めではなく情報の消失）。だから分離する。

分離したうえで、**「減衰だけ」の速い経路は厳密**であることも確認した（`[4b]`: 393,216 通りで不一致 0）:

```
R: (p & 0xF800) * att >> 8, そのあと & 0xF800     G: (p & 0x07E0) で同じ    B: (p & 0x001F) で同じ
3 + 3 + 2 命令 + OR 2 本 = 10 命令で、低域への取り出しが要らない
```

**暖色バイアスを足した瞬間にこれが崩れる**ので、A は各チャネルを**低位へ取り出して**から
減衰 → バイアス加算 → クランプ → 再配置、という経路を取る。ループ本体 52 命令のうち、定数ロード 18 本と
ロード/ストア 4 本とループ管理 3 本を引いた**ベクトル命令 27 本**がその中身で、バイアス無しの経路なら
10 本（+定数 5 本）で済む。つまり**暖色バイアスはブロックあたり約 30 命令（本体の 57%）を買っている**。

### 使った命令と、その理由

| 命令 | どこで | なぜそれか |
| --- | --- | --- |
| `EE.VMUL.U16` | **すべてのシフト**と Q8 スケール | 16bit レーンを直接シフトする命令が**無い**（`docs/pie-simd.md:18`）。`x>>8` は乗数 1、`x>>3` は 32、`x<<4` は 4096、`x<<7` は 32768。SAR は関数の先頭で 1 回だけ `ssr`（`:38`） |
| `EE.VSUBS.S16` | `att = 256 - c`（ここだけ） | 16bit 減算はこれしか無い。`att` の範囲 [1,256] は飽和しない。**フィールドを下げるのには使わない**（それは乗算の仕事） |
| `EE.VADDS.S16` | バイアス加算（低位ドメイン） | **パック語には使えない**。VADDS は符号付き飽和加算で、bit15 が立ったレーン（R≥16、実フレームの大半）は負と読まれ `0xFFFF+1` が `0x7FFF` に飽和する＝繰り上がりではなく巨大な色誤差。だからチャネルごとに、配置する**前**に足す。そこでは値が ≤78 なので飽和し得ない |
| `EE.VMIN.S16` | フィールドのクランプ（31/63/31） | スカラ側の `if(r>31)r=31`（`garden.c:1411-1413`）と同じ。各フィールドが自分のビットに収まることを**構造で**保証し、後段の OR が加算として安全になる |
| `EE.VMUL.S16` | **どこにも使わない** | 掛けるレーンはどれも bit15 が立つ（パック RGB565）か、小さな非負（Q8）。符号付き算術シフトは前者で誤り（ex08 の第 1 回ランがまさにこれ）、後者では不要。**1 ファイル 1 つの乗算規則**にした |
| `EE.VZIP.16` | 8bit→8 レーンの展開 | 奇数バイトは `>>8`（乗数 1）、偶数バイトは `& 0x00FF`、それを 16bit レーンごとに交互配置（TRM p293）。`EE.VLDBC.16.IP` は 1 値のブロードキャストで、**QR 内の 1 要素を全レーンへ配る命令は無い**（`docs/pie-simd.md:20`）ので、定数はメモリから、ピクセルごとの値はレーンで来る |
| `EE.VMIN/VMAX.S16` | B の絶対値（`|ix| = max(ix, -ix)`） | PIE に ABS も SAD も無い（`docs/pie-simd.md:8` 節、ex10 の SAD カーネルが同じ形） |
| `EE.VMULAS.U16.QACC` + `EE.SRCMB.S16.QACC` | B の二乗 | `u ≤ 2048` で `u*u ≤ 4.2e6` は 16bit レーンに入らない。QACC は 40bit で厳密、読み出しのシフトは AR（SAR ではない）から来る（TRM p130）。**これで B は `EE.VMUL` の読み方に依存しなくなった** |
| `EE.VCMP.LT.S16` + `EE.ORQ` + `EE.XORQ` | C のしきい値判定 | ピクセルごとの分岐をレーンのマスクに変える。`VEQ` 系は無いが XOR と all-ones で反転できる |
| `EE.VLD.128.IP` / `EE.VST.128.IP` | 行の読み書き | 128bit のロード/ストアはこの 2 本（p164 / p275）。`EE.LD.128.IP` / `EE.ST.128.IP` は**存在しない** |
| `EE.VLD.L.64.IP` | 8 バイトのカバレッジ | `coverage` は 8bit/px なので 8 px = 8 バイト。アドレスは `{as[31:3],3{0}}`（p168）なので 8B 整列が契約 |

### B) CELT の放物線近似の同じ発想が使えるか: **使えるが、同じ関数にはならない**

`docs/pie-simd.md:48` は正弦表を `u(128-|u|)` と補正 1 項で置き換えて 62→40 命令にした例を挙げている。
同じ「表も除算も要らない、2 乗だけで済ませる」発想はここでも効く。使った式はこれ:

```
u   = |ix|                       （ix は Q11、2048 が縁）
cov = min(255, 256 - (u*u >> 14))
```

これは `256*(1-ξ²)`、つまり軸でも縁でも傾き 0 の放物線で、**2 命令**（二乗と、既にある VSUBS）で出る。
スカラ側の `garden_decor_profile`（`garden.c:1397-1402`）は逆数（`garden_recip`, `garden.c:189`）と
`garden_smooth`（`garden.c:52`, `t*t*(768-2*t)/65536`）を使う。**そしてこの 2 つは同じ形ではない**:

```
スカラの窓   W_s(ξ) = (1-ξ)² (1+2ξ)        （smoothstep）
レーンの窓   W_p(ξ) = 1 - ξ²
差           W_s - W_p = -2ξ²(1-ξ)         → ξ=2/3 で -8/27 = 0.2963、つまり 255 段のうち 75.6 段
```

実測（§5 のプロファイル掃引、9,600 点）: **85.6% の点で完全一致、最大差 80 段**。放物線のほうが**広い**（ξ=1/2 で
スカラ 0.5 に対し放物線 0.75）。さらに:
- 傾きの量子化は Q11 にしてから **最大 2 段**に落ちた（Q7 のままなら 15% の伸縮だった）。
- 正確な 3 次を残す道は**塞がっている**。`q*q*(768-2q)` は最大 4.99e7 で 16bit レーンに入らず、
  `EE.VMULAS.U16.QACC` は「同じレーンの x*y を足す」形なので 3 項の積は 2 回の累算で作れない。
- 中間案: `(1-ξ²)²`（もう 1 回二乗）なら **最大差 19 段**まで縮む。1 命令増、QACC 経路なら飽和の心配も無い。

**答え**: PIE 化の手段としては使える。ただしスカラの断面と**同じ絵にはならない**（ピーク付近で最大 30% 広い）。
これは絵の判断であって丸めの話ではない。ラベルは「CELT 近似」ではなく「放物線版の断面」と書くべきで、
もしビット一致が要るなら 3 次をどこかで（ホスト or スカラー）評価する必要がある。

### C) 行マスク: PIE には**レーンをまたぐ縮約が無い**

`docs/pie-simd.md:19-20` のとおり、PIE にギャザーは 1 レーンぶんの `EE.LDXQ.32` だけで、1 要素を全レーンへ配る命令も無い。
水平方向の OR も、レーンから AR レジスタへ読む道（このファイルの命令群には `EE.RUR.*` も `EE.SRS.*` も無い）も無い。
したがって「このブロックの 8 レーンが全部だめか」を**ベクトル内で 1 ビットに畳む方法は無い**。唯一いつでも使えるのは
**128bit ストアでレーンをメモリに落とし、スカラー側で読む**こと:

```
EE.VST.128.IP q2, a1, 0        ← 8 レーン = 16 バイト = 32bit 語 4 本
l32i x4, or x3, beqz          ← 4 本とも OR しないと、読まなかった語の生存レーンが消える
```

これは **1 ブロックあたりスカラー 9 命令**で、このカーネルのループ本体 23 命令の 4 割を占める。それでもブロック内の
8 ピクセルに払っていた約 110 命令 × 8 に比べれば 1/40 で、「ピクセルごとの分岐」は消えている。
**per-block の分岐は消えていない**（`beqz a9` が 1 本残る）。C の戻り値は「bit b = ブロック b に生存レーンあり」で、
`mask == 0` がそのまま「この行は捨ててよい」になる。

（別案として QACC に重み `2^b` を累算して per-block ビットマスクを直接作る方法がある。ストアは 1 回で済むが、
`EE.SRCMB.S16.QACC` の 16bit 読み出しが 32767 で飽和するため 15 ブロックまでしか入らない。**推定**、試していない。）

## 3. 命令ごとの出典 (per instruction: page and quote)

`data/pie_instructions.json` の `source_page`。節番号は TRM 1.8.x。

| 命令 | source_page | 使っている場所 |
| --- | --- | --- |
| `EE.VLD.128.IP` | p164 (§1.8.88) | A: `fb`・`tint8`、C: `cov`・`protect`（`qu[127:0] = load128({as[31:4],4{0}})`, `as += imm`） |
| `EE.VST.128.IP` | p275 (§1.8.192) | A: 結果の書き戻し（ストアポインタは `a6`）、B: `cov_out`、C: スタックへのマスク落とし |
| `EE.VLD.L.64.IP` | p168 (§1.8.92) | A: `coverage` 8 バイト（`qu[63:0] = load64({as[31:3],3{0}})`） |
| `EE.VLDBC.16.IP` | p171 (§1.8.95) | 全カーネルの定数（`qu[127:0] = {8{load16({as[31:1],1{0}})}}`） |
| `EE.VMUL.U16` | p204 (§1.8.128) | A: すべてのシフトと Q8 スケール。B では使わない |
| `EE.VADDS.S16` | p146 (§1.8.70) | A: バイアス加算（低位ドメインのみ） |
| `EE.VSUBS.S16` | p281 (§1.8.198) | A: `att=256-c`。B: `-ix`、`256-…` |
| `EE.VMIN.S16` | p189 (§1.8.113) | A: 31/63/31 クランプ。B: 255 クランプ |
| `EE.VMAX.S16` | p180 (§1.8.104) | B: `|ix|` |
| `EE.ANDQ` | p76 (§1.8.1) | A: フィールドの取り出しとクリーンアップ、偶数バイトの分離 |
| `EE.ORQ` | p121 (§1.8.45) | A: 3 チャネルの合成。C: 2 つの死マスクの和 |
| `EE.XORQ` | p297 (§1.8.214) | C: 死マスク → 生存マスク（all-ones との XOR） |
| `EE.VZIP.16` | p293 (§1.8.212) | A: 8bit → 8 レーン（`qs0 = {qs0[0],qs1[0],qs0[1],qs1[1],qs0[2],qs1[2],qs0[3],qs1[3]}`） |
| `EE.VCMP.LT.S16` | p161 (§1.8.85) | C: しきい値判定（`0xFFFF` / 0） |
| `EE.ZERO.QACC` | p300 (§1.8.217) | B: 二乗の前 |
| `EE.VMULAS.U16.QACC` | p245 (§1.8.163) | B: `QACC += u*u`（40bit、飽和は 2^40-1 でここでは届かない） |
| `EE.SRCMB.S16.QACC` | p130 (§1.8.54) | B: `QACC >> AR`（AR は `a5` = `shift`）、16bit 飽和読み出し |

コア命令（`entry` / `retw.n` / `ssr` / `l32r` / `movi` / `srli` / `slli` / `or` / `l32i` / `addi` / `bnez` / `beqz`）は
PIE ではなく Xtensa の ISA で、`data/pie_instructions.json` には載っていない。ここで使っているのはエンコーディングが
アセンブラに受け入れられた形そのままで、意味はオブジェクトの逆アセンブル（§4）に現れているとおりである。

## 4. アセンブラ出力（生）

```
$ xtensa-esp32s3-elf-as -o /tmp/ex22.o ex22_decor_ray.S
$ xtensa-esp32s3-elf-nm -S /tmp/ex22.o
00000000 000000ae T ex22_decor_blend
000000b0 0000003a T ex22_decor_coverage
000000ec 00000054 T ex22_decor_rowmask

$ xtensa-esp32s3-elf-objdump -h /tmp/ex22.o

/tmp/ex22.o:     file format elf32-xtensa-le

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 .iram1.literal 0000000c  00000000  00000000  00000034  2**2
                  CONTENTS, ALLOC, LOAD, RELOC, READONLY, CODE
  1 .text         00000000  00000000  00000000  00000040  2**0
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  2 .data         00000000  00000000  00000000  00000040  2**0
                  CONTENTS, ALLOC, LOAD, DATA
  3 .bss          00000000  00000000  00000000  00000040  2**0
                  ALLOC
  4 .iram1        00000170  00000000  00000000  00000040  2**2
                  CONTENTS, ALLOC, LOAD, RELOC, READONLY, CODE
  5 .xtensa.info  00000038  00000000  00000000  000001b0  2**0
                  CONTENTS, READONLY
  6 .xt.lit       00000008  00000000  00000000  000001e8  2**0
                  CONTENTS, RELOC, READONLY
  7 .xt.prop      000000cc  00000000  00000000  000001f0  2**0
                  CONTENTS, RELOC, READONLY

$ xtensa-esp32s3-elf-objdump -d -j .iram1 /tmp/ex22.o

Disassembly of section .iram1:

00000000 <ex22_decor_blend>:
   0:	004136        	entry	a1, 32
   3:	400600        	ssr	a6
   6:	206220        	or	a6, a2, a2
   9:	000071        	l32r	a7, fffc000c <ex22_decor_rowmask+0xfffbff20>
   c:	415350        	srli	a5, a5, 3
   f:	099516        	beqz	a5, ac <ex22_decor_blend+0xac>
  12:	078d      	mov.n	a8, a7
  14:	830124        	ee.vld.128.ip	q0, a2, 16
  17:	898134        	ee.vld.l.64.ip	q1, a3, 8
  1a:	930144        	ee.vld.128.ip	q2, a4, 16
  1d:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  20:	be79a4        	ee.vmul.u16	q6, q1, q7
  23:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  26:	cdbc34        	ee.andq	q1, q1, q7
  29:	fc13b4        	ee.vzip.16	q1, q6
  2c:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  2f:	8eafd4        	ee.vsubs.s16	q1, q7, q1
  32:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  35:	ddbc24        	ee.andq	q3, q0, q7
  38:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  3b:	9efba4        	ee.vmul.u16	q3, q3, q7
  3e:	9eaba4        	ee.vmul.u16	q3, q3, q1
  41:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  44:	9efba4        	ee.vmul.u16	q3, q3, q7
  47:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  4a:	aefaa4        	ee.vmul.u16	q5, q2, q7
  4d:	9ecb64        	ee.vadds.s16	q3, q3, q5
  50:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  53:	9efb54        	ee.vmin.s16	q3, q3, q7
  56:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  59:	9efba4        	ee.vmul.u16	q3, q3, q7
  5c:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  5f:	9efba4        	ee.vmul.u16	q3, q3, q7
  62:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  65:	ed3c24        	ee.andq	q4, q0, q7
  68:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  6b:	ae7ca4        	ee.vmul.u16	q4, q4, q7
  6e:	ae2ca4        	ee.vmul.u16	q4, q4, q1
  71:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  74:	aefaa4        	ee.vmul.u16	q5, q2, q7
  77:	ae4c64        	ee.vadds.s16	q4, q4, q5
  7a:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  7d:	ae7c54        	ee.vmin.s16	q4, q4, q7
  80:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  83:	ae7ca4        	ee.vmul.u16	q4, q4, q7
  86:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  89:	edbc24        	ee.andq	q5, q0, q7
  8c:	aeada4        	ee.vmul.u16	q5, q5, q1
  8f:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  92:	be7aa4        	ee.vmul.u16	q6, q2, q7
  95:	aed564        	ee.vadds.s16	q5, q5, q6
  98:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  9b:	aefd54        	ee.vmin.s16	q5, q5, q7
  9e:	ddf854        	ee.orq	q3, q3, q4
  a1:	ddf874        	ee.orq	q3, q3, q5
  a4:	9a8164        	ee.vst.128.ip	q3, a6, 16
  a7:	550b      	addi.n	a5, a5, -1
  a9:	f65556        	bnez	a5, 12 <ex22_decor_blend+0x12>
  ac:	f01d      	retw.n
	...

000000b0 <ex22_decor_coverage>:
  b0:	004136        	entry	a1, 32
  b3:	000071        	l32r	a7, fffc00b4 <ex22_decor_rowmask+0xfffbffc8>
  b6:	414340        	srli	a4, a4, 3
  b9:	02b416        	beqz	a4, e8 <ex22_decor_coverage+0x38>
  bc:	078d      	mov.n	a8, a7
  be:	830134        	ee.vld.128.ip	q0, a3, 16
  c1:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  c4:	8ea7d4        	ee.vsubs.s16	q1, q7, q0
  c7:	8ea824        	ee.vmax.s16	q1, q0, q1
  ca:	250844        	ee.zero.qacc
  cd:	0a2984        	ee.vmulas.u16.qacc	q1, q1
  d0:	dd7254        	ee.srcmb.s16.qacc	q2, a5, 0
  d3:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  d6:	9e37d4        	ee.vsubs.s16	q2, q7, q2
  d9:	b58184        	ee.vldbc.16.ip	q7, a8, 2
  dc:	9e7a54        	ee.vmin.s16	q2, q2, q7
  df:	9a0124        	ee.vst.128.ip	q2, a2, 16
  e2:	ffc442        	addi	a4, a4, -1
  e5:	fd3456        	bnez	a4, bc <ex22_decor_coverage+0xc>
  e8:	f01d      	retw.n
	...

000000ec <ex22_decor_rowmask>:
  ec:	008136        	entry	a1, 64
  ef:	000061        	l32r	a6, fffc00f0 <ex22_decor_rowmask+0xfffc0004>
  f2:	414340        	srli	a4, a4, 3
  f5:	00a072        	movi	a7, 0
  f8:	040416        	beqz	a4, 13c <ex22_decor_rowmask+0x50>
  fb:	01a052        	movi	a5, 1
  fe:	068d      	mov.n	a8, a6
 100:	830124        	ee.vld.128.ip	q0, a2, 16
 103:	838134        	ee.vld.128.ip	q1, a3, 16
 106:	b58184        	ee.vldbc.16.ip	q7, a8, 2
 109:	9e58f4        	ee.vcmp.lt.s16	q2, q0, q7
 10c:	b58184        	ee.vldbc.16.ip	q7, a8, 2
 10f:	9ed9f4        	ee.vcmp.lt.s16	q3, q1, q7
 112:	dd7464        	ee.orq	q2, q2, q3
 115:	b58184        	ee.vldbc.16.ip	q7, a8, 2
 118:	dd3d64        	ee.xorq	q2, q2, q7
 11b:	9a0014        	ee.vst.128.ip	q2, a1, 0
 11e:	0198      	l32i.n	a9, a1, 0
```

## 5. モデルと参照実装、実行結果（生）

三つが独立に同じ答えを出すことを要求した: **アセンブラ出力を 1 命令ずつ実行するレーンモデル**（`/tmp` の
`lane_model.py`。objdump のテキストを解析し、`.n` 形式の別名を戻し、`l32r` のリテラルだけ呼び手が与える）と、
**ホスト C 参照**（`ref.c`, `gcc -O2`）と、**Python スカラー参照**（契約そのもの）。

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python drive.py
====================================================================================================
ex22 lane harness -- object /tmp/ex22.o, .iram1 image 368 bytes, tables at {'blend': '0x1140', 'cov': '0x1164', 'mask': '0x116a'}
====================================================================================================

[0] the assembler's constant tables, read back from .iram1 at the offsets the model found
    blend @0x1140: 0001 00ff 0100 f800 0020 0001 0020 001f 8000 1000 07e0 0008 0010 003f 2000 001f 0008 001f   OK
    cov   @0x1164: 0000 0100 00ff   OK
    mask  @0x116a: 0008 0020 ffff   OK

[1a] blend, one block (8 pixels), lanes 0..7
     p     c    t      out     ref
   f800    0    0  f800  f800  ok
   07e0   32    8  0ee0  0ee0  ok
   001f   64   16  1037  1037  ok
   ffff  128   32  9c30  9c30  ok
   0000  192   64  4082  4082  ok
   8410  224  128  9186  9186  ok
   1234  240  200  c9a7  c9a7  ok
   abcd  255  255  f9e7  f9e7  ok

[1b] coverage, one block (Q11: 2048 is the profile's edge)
      ix    out   ref
   -2048      0     0  ok
   -1040    190   190  ok
     -16    255   255  ok
       0    255   255  ok
      16    255   255  ok
    1008    194   194  ok
    1536    112   112  ok
    2048      0     0  ok

[1c] rowmask, one block: got 0x00000001 ref 0x00000001 (bit 0 = block 0 has a live lane)
     two blocks, second dead: got 0x00000001 ref 0x00000001
     both dead (the row can be dropped): got 0x00000000

[2] sweeps, lane model vs the scalar contract
    A blend      VMUL = full product >> SAR (the device's reading)
                 pixels swept 160000   mismatches 0   first None   max channel error 0 @ None
    A blend      VMUL = low 16 of the product >> SAR (what tools/pie/piesim.py implements)
                 pixels swept 16000   mismatches 15967   first (58037, 170, 234, 174, 64174)   max channel error 56 @ (53073, 17, 247, 246, 65526)
    B coverage   pixels swept 160000   mismatches 0   first None   max |d| 0 @ None
    C rowmask    pixels swept 80000   mismatches 0   first None

[3] exhaustive, one channel at a time (the same arithmetic the kernel runs per lane)
    R: swept 262144 lane cases   mismatches 0   max error 0 @ None
    G: swept 262144 lane cases   mismatches 0   max error 0 @ None
    B: swept 65536 lane cases   mismatches 0   max error 0 @ None

[4a] the whole-word variant: ONE EE.VMUL.U16 of the packed RGB565 word (no channel masks)
     (bias term set to zero in both the kernel model and the contract, so this is the
      channel-bleed error alone: what 'multiply the 16-bit word by a Q8 factor' costs)
     (p, c) pairs swept: 393216   exact 91392 (23.2%)   wrong 301824 (76.8%)
     pairs with the R field wrong 40940, the G field wrong 252704, the B field wrong 251904  (each out of 393216)
     per-channel error histogram (error >= 4): {4: 4192, 5: 2816, 6: 3328, 7: 4596, 8: 20536, 9: 3616, 10: 2048, 11: 2048, 12: 5820, 13: 2048, 14: 2048, 15: 5396, 16: 68080, 17: 8256, 18: 2048, 19: 2048, 20: 5360, 21: 2048, 22: 2048, 23: 3632, 24: 22464, 25: 4228, 26: 2048, 27: 2048, 28: 2928, 29: 2048, 30: 2048, 31: 4588, 32: 66784, 33: 4404, 39: 796, 40: 3744, 41: 1312, 47: 388, 48: 22880, 49: 2092, 56: 800, 57: 372}
     worst per-channel error 57 @ (p=0x0825, c=32, got 0x0720, contract 0x0004)
     why: (p*att)>>8 = r*att*8 + floor((g*att*32 + b*att)/256), so the R field survives
     only as bits 15:11 of a sum whose low bits carry INTO it, and the G field's own bits
     15:11 hold 8*r*att mod 2048, i.e. r's low bits. 16.2e6 (0xF800*255, the R field times
     the largest att) is 247 times a 16-bit lane: the information is gone, not rounded.

[4b] the same masking discipline with the bias dropped: the ATTENUATION-ONLY fast path
     (ANDQ, VMUL, ANDQ per channel -- 3+3+2 ops and two ORs, no low-domain extraction)
     (p, c) pairs swept: 393216   mismatches 0   max channel error 0
     (exact, so the whole cost of the warm bias in kernel A is the extraction: 27 vector ops
      against 10 -- 8 per channel plus the two ORs -- and 18 constant loads against 5)

[5] the `shift` parameter (SAR): the contract is 8
    shift=7: 0/8 lanes agree with the contract   out[0]=0xefdf contract 0xd3d2
    shift=8: 8/8 lanes agree with the contract   out[0]=0xd3d2 contract 0xd3d2
    shift=9: 0/8 lanes agree with the contract   out[0]=0x0c89 contract 0xd3d2
    shift=12: 3/8 lanes agree with the contract   cov[0]=-768 contract 0
    shift=14: 8/8 lanes agree with the contract   cov[0]=0 contract 0
    shift=16: 3/8 lanes agree with the contract   cov[0]=192 contract 0
```

`[4a]` が設問の「16bit 全体に近似係数を掛けた場合の誤差の列挙」にあたる。誤差の分布は上のヒストグラムで、
**どの値で出るか**: R フィールドが誤るのは 393,216 通りのうち 40,940（10.4%）で、これは
`r*att*8` の下位が 2048 を超えて R フィールドへ繰り上がるとき。G と B はそれぞれ 252,704（64%）・251,904（64%）で、
上位ビットが隣のチャネルの残差に置き換わる。最大は 57（G の 63 段）。

プロファイル側の誤差:

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python profile_error.py
swept 9600 (radius, cx, x) triples: radius 18/20/22/25/24.5/31 px, cx 0/32/120/203/239.5 px
  the shipped parabola vs the scalar cubic+Q24-reciprocal: identical at 8218 (85.6%), max |difference| 80 levels of 255
    worst: radius 25 px, cx 0 px, x -17: scalar 61, lane 141
  the same parabola at the ramp's EXACT position (no K rounding): max |difference| 80 @ radius 25 px, x -17 (scalar 61, lane 141)
  -> so the ramp's integer slope is worth 80 levels out of 80: with the
     shipped K = floor(524288/radius) the shape difference dominates completely.
  the K rounding alone (the cubic at the lane's position against the scalar): max 2 @ radius 22 px, x -14 (scalar 78, cubic-at-lane 76)
  the quartic (1-xi^2)^2 instead: max |difference| 19 @ radius 25 px, x -13 (scalar 120, quartic 139)
  histogram of the shipped difference (difference: count), first 20: {0: 8218, 1: 50, 2: 28, 3: 2, 4: 40, 5: 18, 6: 20, 7: 10, 8: 18, 9: 12, 10: 6, 11: 18, 12: 25, 13: 3, 14: 20, 15: 4, 16: 12, 17: 24, 18: 14, 20: 18}

  the analytic shape difference: W_s(xi) - W_p(xi) = -2 xi^2 (1-xi), extreme -8/27 at
  xi = 2/3 -> 0.2963 * 255 = 75.6 levels, which is the 75-80 band above. At xi = 1/2 the
  cubic is 0.5 of full scale and the parabola 0.75: the parabola is the WIDER profile.

  what the exact cubic would cost in lanes: q*q*(768-2q) reaches 4.99e7, no 16-bit lane holds
  it, and EE.VMULAS.U16.QACC accumulates x*y for the same lane so a THREE-term product is not
  reachable in two accumulates. Estimate, not measured: 4-6 more instructions per block, and
  the intermediate q*q needs care (it saturates EE.SRCMB.S16.QACC's 16-bit readout above 32767,
  which is what garden.c:117-123 records for its own dither squaring).
  The quartic above is the cheap middle: one more square (the QACC path again, or a 16-bit lane
  multiply for v <= 255 whose product 65025 fits a lane) and a difference of 19 levels instead of 80 .
```

静的な数え上げ（ループ本体の命令数・バイト数・stage-2 の間合い。規則は `docs/pie-simd.md:553-563`）:

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python count.py
ex22_decor_blend       loop body: 52 instructions, 155 bytes (loopgtz-able, < 256B), stage-2 slots followed immediately by their consumer: 23
                         ee.vldbc.16.ip -> ee.vmul.u16, ee.vldbc.16.ip -> ee.andq, ee.vldbc.16.ip -> ee.vsubs.s16, ee.vldbc.16.ip -> ee.andq, ee.vldbc.16.ip -> ee.vmul.u16, ee.vmul.u16 -> ee.vmul.u16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vmul.u16 -> ee.vadds.s16, ee.vldbc.16.ip -> ee.vmin.s16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vldbc.16.ip -> ee.andq, ee.vldbc.16.ip -> ee.vmul.u16, ee.vmul.u16 -> ee.vmul.u16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vmul.u16 -> ee.vadds.s16, ee.vldbc.16.ip -> ee.vmin.s16, ee.vldbc.16.ip -> ee.vmul.u16, ee.vldbc.16.ip -> ee.andq, ee.vldbc.16.ip -> ee.vmul.u16, ee.vmul.u16 -> ee.vadds.s16, ee.vldbc.16.ip -> ee.vmin.s16
                         stores: 1 (+0.6 cycle each), first-order cycles: 52 + 23 + 0.6 = 75.6
ex22_decor_coverage    loop body: 15 instructions,  45 bytes (loopgtz-able, < 256B), stage-2 slots followed immediately by their consumer: 3
                         ee.vldbc.16.ip -> ee.vsubs.s16, ee.vldbc.16.ip -> ee.vsubs.s16, ee.vldbc.16.ip -> ee.vmin.s16
                         stores: 1 (+0.6 cycle each), first-order cycles: 15 + 3 + 0.6 = 18.6
ex22_decor_rowmask     loop body: 23 instructions,  63 bytes (loopgtz-able, < 256B), stage-2 slots followed immediately by their consumer: 3
                         ee.vldbc.16.ip -> ee.vcmp.lt.s16, ee.vldbc.16.ip -> ee.vcmp.lt.s16, ee.vldbc.16.ip -> ee.xorq
                         stores: 1 (+0.6 cycle each), first-order cycles: 23 + 3 + 0.6 = 26.6
```

## 6. piesim による解釈実行（生）

`/workspace/pjs-vm/tools/pie/piesim.py` を `/tmp` にコピーして使った（リポジトリ側は無変更）。**生のままでは
`NotImplementedError(entry)` で止まる**ので、4 点だけ足した。差分:

```diff
--- /tmp/ex22/piesim_orig.py	2026-09-15 01:39:07.989826862 +0000
+++ /tmp/ex22/piesim_ext.py	2026-09-15 01:39:07.990024339 +0000
@@ -49,8 +49,9 @@
 
 
 class Sim:
-    def __init__(self, mem):
+    def __init__(self, mem, vmul_lowbits=False):
         self.mem = mem
+        self.vmul_lowbits = vmul_lowbits   # True = the reading the device does NOT do
         self.q = [[0] * 8 for _ in range(8)]
         self.qacc = [0] * 8
         self.sar = 0
@@ -95,9 +96,15 @@
             elif op == 'ee.xorq':
                 out.append(x[i] ^ y[i])
             elif op == 'ee.vmul.s16':      # 32-bit product, arithmetic >> SAR, low 16
-                out.append(((s16(x[i]) * s16(y[i])) >> self.sar) & 0xFFFF)
+                if self.vmul_lowbits:
+                    out.append((((s16(x[i]) * s16(y[i])) & 0xFFFF) >> self.sar))
+                else:
+                    out.append(((s16(x[i]) * s16(y[i])) >> self.sar) & 0xFFFF)
             elif op == 'ee.vmul.u16':      # 32-bit product, logical >> SAR, low 16
-                out.append(((x[i] * y[i]) >> self.sar) & 0xFFFF)
+                if self.vmul_lowbits:
+                    out.append((((x[i] * y[i]) & 0xFFFF) >> self.sar))
+                else:
+                    out.append(((x[i] * y[i]) >> self.sar) & 0xFFFF)
             else:
                 raise NotImplementedError(op)
         return out
@@ -176,6 +183,30 @@
                 v = self.ld32((base + Q[qs][sel8] * 4) & ~3)
                 Q[qu][2 * sel4] = v & 0xFFFF
                 Q[qu][2 * sel4 + 1] = v >> 16
+            elif op == 'ee.vzip.16':
+                a0, a1_ = Q[qi(a[0])], Q[qi(a[1])]
+                lo = [v for k in range(4) for v in (a0[k], a1_[k])]
+                hi = [v for k in range(4, 8) for v in (a0[k], a1_[k])]
+                Q[qi(a[0])], Q[qi(a[1])] = lo, hi
+            elif op in ('entry', 'retw.n', 'retw', 'nop'):
+                pass
+            elif op == 'ssr':
+                self.sar = arv(a[0]) & 63
+            elif op == 'movi':
+                arset(a[0], int(a[1], 0) & 0xFFFFFFFF)
+            elif op == 'srli':
+                arset(a[0], arv(a[1]) >> int(a[2], 0))
+            elif op == 'slli':
+                arset(a[0], (arv(a[1]) << int(a[2], 0)) & 0xFFFFFFFF)
+            elif op == 'or':
+                arset(a[0], arv(a[1]) | arv(a[2]))
+            elif op == 'l32i':
+                arset(a[0], self.ld32(arv(a[1]) + int(a[2], 0)))
+            elif op == 'l32r':
+                arset(a[0], getattr(self, 'l32r_labels', {})[a[1]])
+            elif op == 'beqz':
+                if arv(a[0]) == 0:
+                    pc = labels[a[1][:-1]] + 1
             elif op == 'ee.vunzip.16':
                 both = Q[qi(a[0])] + Q[qi(a[1])]
                 Q[qi(a[0])], Q[qi(a[1])] = both[0::2], both[1::2]
```

足したものと、その根拠:
1. `EE.VMUL.S16/U16` を `((x*y) >> SAR) & 0xFFFF` に（**修正**。既定の piesim は `((x*y) & 0xFFFF) >> SAR`）。
   装置の答えは前者で、ex08 の装置ラン（`backups/pie-examples-20260914T183007Z.log:162`、`CHECK
   tint_matches_the_truncating_reference pie=1 ref=1 ok`、45/45）と、このリポジトリのホストモデル
   `tools/check_examples_log.py:364`（`as_i16(((x * 300) >> 8) & 0xFFFF)`）がどちらもこちらを実装している。
   a=0x7530 (=30000)、tint=300、SAR=8 で装置は 0x8954 (=35156) を返した。他方の読みなら
   `(9000000 mod 65536) >> 8 = 84` になる。
2. `EE.VZIP.16`（TRM p293 の Operation 疑似コードから）。piesim は `VUNZIP.16` と `VZIP.8` は持っているがこれを持たない。
3. `ssr`（SAR を AR から。piesim の `wsr.sar` と同じこと）、`entry`/`retw.n`、AR 演算（`movi`/`srli`/`slli`/`or`/`l32i`/`l32r`）、
   `beqz`。**PIE ではなく Xtensa コアの命令**で、意味はアセンブラが受理した形のまま。
4. それ以外は piesim の元のコード（レーン算術、整列の強制、QACC、`.LD.INCP` の扱い）をそのまま使っている。

実行結果:

```
$ /workspace/esp32s3-hw-mcp/.venv/bin/python run_piesim.py
========================================================================================================
piesim (a /tmp copy of tools/pie/piesim.py, patched as documented) running the assembled kernels
========================================================================================================

A ex22_decor_blend  : 163 instructions executed for 24 pixels (6.79/pixel), 3 blocks
   outputs      : f800 0ee0 1037 9c30 4082 9186 c9a7 f9e7
   C reference  : f800 0ee0 1037 9c30 4082 9186 c9a7 f9e7
   mismatches 0 of 24   

B ex22_decor_coverage: 50 instructions executed for 24 pixels (2.08/pixel)
   ix   -2048 -1536 -1024  -512   -16     0    16   512  1024  1536  2032  2048  (Q11: 2048 = the edge)
   cov      0   112   192   240   255   255   255   240   192   112     4     0
   ref      0   112   192   240   255   255   255   240   192   112     4     0
   mismatches 0 of 24   

C ex22_decor_rowmask: 75 instructions executed for 24 pixels (3.12/pixel)
   got 0x00000001   reference 0x00000001   ok   (cov/prot given: only block 0 has a lane above both thresholds -> bit 0)
   the 16-byte scratch the kernel leaves at the stack pointer: ['0x0', '0x0', '0x0', '0x0', '0x0', '0x0', '0x0', '0x0']

D the same block with EE.VMUL.U16 read as 'the low 16 bits of the product, then >> SAR'
   outputs      : 0000 00e0 0037 0030 0082 0086 00a7 00e7
   C reference  : f800 0ee0 1037 9c30 4082 9186 c9a7 f9e7
   mismatches 8 of 8

E the UNPATCHED tools/pie/piesim.py stops at NotImplementedError(entry): the first instruction it has no model for. It has no `ssr` (it has wsr.sar), no entry/retw.n, no
  AR ops (movi/srli/or/l32i/l32r), no beqz, no EE.VZIP.16 -- and its EE.VMUL.U16 is the
  other reading of the TRM's Operation line (see D).
```

`D` が「もし TRM の Operation 行をもう一方に読むと、このカーネルは 8/8 で崩れる」という証拠で、
`E` が「生の piesim はどこで止まるか」である。

## 7. 節約の見積り（命令数の算術。**推定**）

`flower-decor-cost.md:31-35` の数字は**推定**である（同ファイルが「engineering estimates with substantial
uncertainty, not calibrated timings」と明記している）。その推定値から出発して、命令数だけを数える:

```
スカラ（flower-decor-cost.md:14-16 の wide rays + events の 1 フレーム）
  candidate pixel visits      21,123
  profile を評価するピクセル   17,437
  RGB565 合成                 13,160
  スカラ命令数                 1,500,000 .. 2,000,000（推定）
  → visit あたり 71..95 命令、合成ピクセルあたり 114..152 命令

このファイルのカーネル（1 ブロック = 8 px、objdump で数えた静的命令数）
  A 52 命令/ブロック → 6.5 命令/px
  B 15 命令/ブロック → 1.9 命令/px（profile 1 本ぶん。スカラは 2 本/px 呼ぶので 2 回走らせる）
  C 23 命令/ブロック → 2.9 命令/px

1 フレームに置き換えると
  B × 2（light と shadow）  17,437 × 2 × 1.9 = 66,260
  C（visit 全域）           21,123 × 2.9     = 61,257
  A（合成ピクセル）         13,160 × 6.5     = 85,540
  合計                                       ≈ 213,000 命令/フレーム
  → スカラ推定 1.5..2.0M に対して 7..9 倍少ない（**推定**。命令数の算術であって時間ではない）
```

サイクルの一次下限も同じ形で出せる（`docs/pie-simd.md:86` の `命令数 + 0.6×ストア数 + ストール数` に、
`:553-563` の段の表から数えた stage-2 の間合いを足したもの）:

```
A: 52 + 23 + 0.6 = 75.6 サイクル/ブロック（9.5/px）
B: 15 +  3 + 0.6 = 18.6 サイクル/ブロック（2.3/px）
C: 23 +  3 + 0.6 = 26.6 サイクル/ブロック（3.3/px）
1 フレーム: 13,160×9.5 + 17,437×2×2.3 + 21,123×3.3 ≈ 276,000 サイクル ≈ 1.15 ms @240MHz（**下限・推定**）
```

`flower-decor-cost.md:33-34` は補助パス全体を 6..12 ms/frame と見積もっている。そのうちピクセルパスがどれだけかは
**この文書では分からない**ので、比（7〜9 倍）は「命令数がこれだけ減る」という主張に留める。実動作は
`docs/pie-simd.md:110-114` のとおり下限の 1.3〜1.4 倍、`docs/pie-simd.md:145` のとおり行あたりの前処理が
別勘定で乗る。**実機で測るまで ms の主張はしない。**

## 8. 置き換えられない部分と、その理由

| 残るもの | 出典 | なぜレーンに載らないか |
| --- | --- | --- |
| 行ヘッダ（4 層ぶんのハッシュ → side/radius/lifetime/fade/slope） | `garden.c:1380-1391` | 行あたり 4 回の**スカラー判定**（`garden_hash`・lifetime 比較・`garden_smooth`）。8 レーンに載せる形が無く、載せても 1 行 4 要素では割に合わない |
| `arrival`（上から開く前線） | `garden.c:1418-1428` | 行ごとに `duration` で割る（1423）。除算は実測 16〜18 サイクル（`docs/pie-simd.md:261`）で、行あたり 4 回なら無視できる。レーン化の利得が無い |
| 行ごとの 2 本の逆数 `inv`/`shadow_inv` | `garden.c:1465` | `flower-decor-cost.md:30` が「per active ray-row, not per pixel」と明記している部分。ここを動かす必要は無い |
| スパンのクリップ `lo..hi` と 3 つの continue | `garden.c:1436-1443`, `1461-1464` | 行ヘッダの仕事。レーン版は**クリップ後の連続スパン**を受け取る契約にしてある（`n_px` は 8 の倍数、頭と尾は呼び手が持つ） |
| ブロック境界 | 同上 | スパン長は 203 px のような半端な数で、8 の倍数ではない。カーネルにスカラの頭・尾を入れると 1 行あたり 2 か所の分岐が戻り、「ピクセルごとの分岐を消す」という目的が濁る。だから契約で呼び手に押し出した |
| インスキャタリングの**ピクセル追従項** | `garden.c:1408-1410` の `(light*(r+6))>>1` など | A は「減衰 + ピクセルごとに一定の暖色バイアス」であり、`r` に比例する項は入れていない。正確に戻すにはチャネルごとに 1 本ずつ乗算が要る（**推定** +6 命令/ブロック = +12%）。ただし戻す先はある: その項も `r` についてアフィンなので、`(256-e) + light/2` を掛けて `3*light + d` を足す形にすれば同じ構造で書ける |
| ディザ | `garden.c:1479`（定義 `106-132`） | `d` は (x,y) のハッシュで決まるピクセルごとの値。カーネルは `tint8` を入力として受け取るだけなので、呼び手がこれを畳むか、別途レーンで作る（2 乗 2 回＋乗算＋XOR で **推定** 4〜6 命令/ブロック） |
| 層が 4 つあること | `garden.c:1432` | 各層が自分のスパンと自分の定数を持つので、行あたり**最大 8 回**（4 層 × プロファイル 2 本）カーネルを呼ぶ。呼び出しの足場（`mov a8,a7` と `loopgtz` 立ち上げ）がそのたびに乗る。`docs/pie-simd.md:147-151` が「行を run に切るカーネルは第 2 項が第 1 項と同じ桁になる」と書いているのと同じ構造 |

## 9. 拒否テストの経済学（`docs/pie-simd.md:301-302`）

> 節約 = 棄却率 x 棄却された visit のコスト / 「安かったから棄却できた」もので、平均ではない

このカーネルがこの罠に嵌まらない理由: **レーンのカーネルには、安いから抜けられるピクセルが 1 つも無い。**
A/B のループは分岐がゼロで、行内のどのピクセルも同じ命令数を踏む（プロファイルの値が 0 でも同じ 15 命令を走る）。
したがって「落とす仕事のコスト」は**平均そのもの**で、`flower-rejection` の 27〜50% が 0.2 ms/30 ms だった例
（`docs/pie-simd.md:311`）のような「安い訪問を落としていた」構造が無い。C の行マスクが落とすのは**行まるごと**で、
節約は `(落ちた行数) × (その行の A+B の全コスト)` と厳密に書ける。棄却率を時間の比に翻訳する必要が無い。
加えて C 自身のコスト（26.6 サイクル/ブロック、**推定**）は常に払う側に計上してあり、「マスクが浮かせる分」と
相殺した数字を主張していない。

## 10. 検査が捕まえたもの

1. **ストアポインタが読めていた。** 最初の A は `EE.VLD.128.IP` のポインタ（`a2`）でそのままストアしていた。
   `.IP` はロードで 16 進めるので、ストア先は**常に 1 ブロック先**になる（最初のブロックは次のブロックの画素に書かれ、
   最後のブロックは行の外に書かれる）。1 ブロックのテストで即座に出た（出力が入力と一致した）。修正は
   `mov a6, a2` でストア用のポインタを持つこと（`shift` を `ssr` で消費した後なので `a6` が空く）。
2. **行マスクが 16 バイトのうち 8 バイトしか読んでいなかった。** C の最初の版は落としたマスクの 2 語だけを
   OR していた（`l32i a1,0` と `l32i a1,8`）。生存レーンが読まなかった語にあるブロックが「死」と判定され、
   2 ブロック目のテストで出た（`0x1` 対 `0x3`）。修正は 4 語すべての OR。
3. **プロファイルの傾きが粗すぎた。** Q7 の傾き（`K = floor(32768/W)`）は K が 4..7 にしかならないため、
   1 の量子化が**最大 15% の伸縮**になる。プロファイル掃引がこれを「ランプ由来の不一致 35 段」として可視化した。
   Q11（`K = floor(524288/W)`）＋ QACC の厳密な二乗に変えて、ランプ由来の誤差は**最大 2 段**まで落ちた。
   命令数は 15 のまま変わっていない。

## 11. 未確認の前提 (semantics this kernel depends on that I could NOT confirm)

1. **`EE.VZIP.16` のレーン順。** TRM p293 の Operation 疑似コードの読みで実装してある。ex08 も piesim もこれを使って
   いないので、実機の裏付けはこのリポジトリに無い。もし交互配置が逆なら、カーネル A はカバレッジをブロック内で
   入れ替えて使う（絵はブロック単位で崩れる）。装置で確かめるのが最優先。
2. **`EE.SRCMB.S16.QACC` の AR シフトと 16bit 飽和読み出し。** ex09 の装置ランで確定していない系統
   （`notes/08-media-3d-perf.md` の「実機の前に読むこと」）。B は `u ≤ 2048` なので読み出しは 32767 に届かない計算だが、
   それは「届かない」という主張であって、命令の意味そのものの確認ではない。
3. **`EE.VMUL.U16` の読み方が本カーネルの鎖でも成り立つこと。** ex08 は `p*300` の積で確かめた。本カーネルは
   `(r<<11)*32`（積 2.1e6）と `x*32768`（積 1.0e6）という**もっと大きい積**で同じ読みに依存する。
   同じ命令なので同じはずだが、確かめた入力の範囲は違う。
4. **`EE.VLDBC.16.IP` のブロードキャスト**（p171）は TRM のままで、このカーネルでの装置確認は無い。
5. **間合い（ストール）の数え方**は `docs/pie-simd.md:553-563` の段の表による。§7 のサイクル数は
   この表から出した**推定**で、実測ではない。カーネルはロードを直後に使う形を 23 箇所持っている
   （`count.py` が列挙している）。`docs/pie-simd.md:733-742` のチェックリスト 5 番はこれを避けろと言っており、
   融合ロード（`.LD.INCP`）に置き換えれば理論上 18 本ぶんが 0 になる見込み（**推定**）。やっていない。
6. **ループ本体が 256B 以下**（A 155B / B 45B / C 63B）なので `loopgtz` が使える。使えば 1 ブロック 2 命令
   減る（A 52→50）が、`docs/pie-simd.md:610` の注意（ハードウェアループレジスタ）と ex08 の前例に従って
   `addi`/`bnez` のままにしてある。**やっていない節約**として数えておく。
7. **`tint8` の型。** シグネチャは `const int16_t *` で、各レーンが 0..255 の 8bit 値だと定めた。呼び手の契約。
8. **`protect` の作り方。** C は入力として受け取るだけ。スカラ側の `garden_smooth(gap*255/24)` をレーンで作る道は
   あるが、本ファイルには入れていない（B と同じ形の別カーネルになる）。
9. **実機ランが無い。** 装置で走らせていないので、命令の意味のうち上に挙げたものは全部「文書どおり」の状態にある。

## 12. ビルドへの入れ方（適用していない）

`proposed/` に置いてあるだけで、`examples/firmware/main/CMakeLists.txt`（`ex01`〜`ex12` の列挙）にも
`main.c` にも触っていない。入れるなら:

1. `CMakeLists.txt` の `SRCS` に `proposed/ex22_decor_ray.S` を足す（`proposed/` の他のファイルと同じ扱い）。
2. `main.c` に C 参照（`ref.c` の 3 関数）と `BENCH` 行の相手を足す。現行の `ex10`〜`ex12` と同じ形で、
   ブロック数を入力に出し、`DATA` で全出力を出してホスト側チェッカーが再計算できるようにする。
3. 実機で確かめる順序: (a) `EE.VZIP.16` のレーン順（1 ブロックのカバレッジを `DATA` に出す）、
   (b) `EE.SRCMB.S16.QACC` の AR シフト、(c) それから A/B/C の全数比較。
4. そのうえで `cardputer-adv-pocketjs` 側の `garden_decor_row` に、`GARDEN_DECOR_RAYS` と同じ形の
   コンパイル時スイッチ（`GARDEN_DECOR_PIE`）を付けて、行ヘッダはそのまま・ピクセルパスだけ差し替える。
   `docs/pie-simd.md:385-392` のとおり**最適化とその計測を同じコミットに入れない**こと。

