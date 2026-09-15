# flower シーンの重さと、次に手を入れる順

出典: **cardputer-adv-pocketjs**（`main/scene/flower.c` 1678 行、`main/scene/garden.c`、
`main/ui/shell.c`、`main/ui/kasane/`）。flower.c の最終変更は `1fea03f`(2026-09-12)。
実機の数字はツリー内の既存ログ（`.cache/hosttests/logs/boot-device.log` = `vm-L1-42-g983eabe`、
`boot-v601-probe.log` = `vm-L1-47-gb54ceff`）からの引用で、**この調査で新たに測ったものではない**。
3 ワークツリー（main / vm/main / vm/design-contracts）で `flower.c`・`garden.c`・`overlay.c`・`shell.c`・
`render_accel.c` はバイト一致（読んだ内容は 3 本に共通）。

**種は valley のみ**（ログの全サンプルが `species=0 view=0 parts=31`）。他の 13 種の実機値は無い。

---

## 1. いまの実測（valley、60 フレーム平均）

`background: PERF mode=3`（fps は 18.1–20.4、FPS 表示は OFF）:

| 項目 | 実測 | 中身 |
|---|---:|---|
| draw | 46.7 – 52.9 ms | フレーム全体 |
| loop | 35.8 – 41.5 ms | |
| **kernel** | **35.7 – 41.4 ms** | **FLOWER の `kernel=` はベクターカーネルではない**。`flower_scene_draw` の戻り値 = `flower_draw` + `glass_rain_draw` の実サイクル（`shell.c:504-509`）。ocean/wave と意味が違う |
| hud | 1.93 – 2.11 ms | うち `ovl=1.50-1.62` `fmt=0.28-0.32` |
| send | 7.66 – 7.84 ms | SPI の物理下限 |

`garden: SPLIT`:

| 項目 | 実測 | 中身 |
|---|---:|---|
| total | 35.4 – 37.4 | `flower_draw` の行本体 |
| garden | 20.3 – 22.0 | `garden_row_blend` |
| pixels | 6.13 – 6.16 | PIE 画素カーネル + 2 本のオクターブ行 + モート（**モートは入れ子**で SPLIT に書かれていない） |
| **decor** | **14.1 – 15.9** | `garden − pixels`。**幹・林冠・草・装飾光線・行の足場が混ざったまま**。一度も分割されていない（`flower.c:1585-1586` が自分でそう書いている） |
| **ray** | **15.2 – 15.4** | `total − garden` = flower 自身（`ray_row` がほぼ全部） |
| visits / hits | 6,793–7,038 / 1,867–1,888 | |
| sqrt（楕円体側） | 1.00–1.02 ms（1,537–1,549 calls） | `flower.c:1364` |
| bell:sqrt | 1.17–1.19 ms（2,520–2,564 calls） | `flower.c:1172`（**bell と足さない**） |
| shade | 5.33–5.37 ms（677–689 cy/hit） | |
| bell | 4.41–4.44 | |
| span | 14.05–14.28 | |

検算: `20.27 + 15.15 = 35.42` ✓ / `span + scan + pre + rest = ray` ✓。
**`sqrtf` は 4,051–4,113 calls/フレーム = 2.14–2.23 ms = フレームの 5.8–6.1%**（実機で数えられる唯一の
大きな「名前の付いた」費用）。

---

## 2. 計器の穴（いま一番安い改善）

**この節の2件は 2026-09-15 に実機で実施・計測済み。結果は §2.1 / §2.2 に追記した。**

1. **`decor` が未分割**。命令数の床では、装飾光線 3.9 ms + 林冠 2.0 ms + 幹 0.30 ms + 草 0.12 ms =
   **6.3 ms しか説明できない**。実測は 14–17 ms。**穴の正体（ロード・分岐・行の足場・遷移時の 2 回走査）
   を割らないと、次にどこへ手を入れるかが決まらない。**
   → 一手: `garden_row_blend`（`garden.c:1578`）に `vegetation` と `rays` の 2 つの `rsr.ccount`
   ブラケットを足す（`garden_prof_pixels()` と同形）。副作用ゼロ・スイッチ不要。
   同時に `bloom_garden_mix<256`（遷移中か）も出す — `FLOWER_SHOTS` は 1 枚・`hold=40 s`・
   `FLOWER_GARDEN_FADE_S=3 s` なので、**フレームの約 7.5% は林冠を 2 回走る**（`garden.c:1585-1587`）。
   60 フレーム平均の `decor=` は 2 つの違う仕事量の平均になっている。
2. **`fmt=0.28–0.32 ms/frame` を捨てている**: `shell.c:545` の `snprintf(meter, ...)` が FPS 表示 OFF
   でも毎フレーム走っている → `if (show_fps)` の中へ移す。
3. **`ovl` の自己検査が壊れている**: `shell.c:75-81` は「FLOWER の overlay は NULL だから 0.00 のはず」
   と書いているが、`shell.c:572` の `overlay_paint` も 17 回呼ばれ、実機では overlay アプリが
   running（`boot-device.log:81`）なので **`ovl=1.50-1.62 ms` が実際に計上されている**。
   `hud_ovl_cy` のブラケットが `overlay_paint` を含むため。

### 2.1 実装と実測（2026-09-15、cardputer-adv-pocketjs @ vm/design-contracts + ブラケット）

`garden.c` に `garden_prof_vegetation()` / `garden_prof_rays()` / `garden_prof_dissolve()` を追加し、
`garden_atmosphere_row` の `garden_decor_row` と `garden_vegetation_row` の3呼び出しを括った。
出力は **SPLIT3**（SPLIT / SPLIT2 はこのリポジトリ自身の規約で凍結されているので触らない）。

計測条件: `vm-L1-60-g8779e3e` + ブラケット、MODE 3 FLOWER（fps 表示 OFF）、ホストからBACKを送って
overlay を抜いた状態で 100 秒、60 フレーム窓 ×36（species 1 / 10 / 11 / 12）。
ログと読み出し: `cardputer-adv-pocketjs` の `.cache/hosttests/logs/flower-instrument-20260915.log`、
`python tools/flower_instrument_summary.py <log>`。**実機計測**。

| 項目 | 実測 (ms/frame) | cy/row | 命令数の床 |
|---|---:|---:|---:|
| rays（装飾光線の補助パス） | 7.6–13.7（代表 ~10） | 13,450–24,300 | 3.9 |
| vegetation（林冠＋幹＋草） | 5.0–5.9 | 8,900–10,460 | 2.4 |
| rest（行の足場＋dissolve の memcpy/mix） | 0.24–0.36 | — | 数えていない |

分かったこと:

- **穴は「4つ目の仕事」ではなかった。** rays と vegetation がそれぞれ命令数の床の **約2.4倍** かかって
  いる。つまり decor の未説明 8–10 ms はスカラー行コードの命令単価（分岐・ロード・キャッシュ）で、
  行の足場は 0.24 ms しかない。**「1命令1サイクル」の床はこのコードでは約2.4倍甘い**（実測値）。
- **装飾光線が decor の最大項**（代表 ~10 ms）。`docs/flower-decor-cost.md` の「6..12 ms」という
  見積もりは、実測 7.6–13.7 ms でバンドの上端に当たった。PIE 化の上限はこの実測が上限になる。
- **遷移（dissolve）は別の仕事量**: 135行中105行が dissolve 中の窓では veg 9.61 ms / 240 passes、
  rest 3.37 ms（定常は 5.7 ms / 135 passes / 0.26 ms）。`dissolve_rows` を出さないと 60 フレーム平均が
  2つの違う仕事量の平均になる、という §2.1 の懸念は実測で裏が取れた。

### 2.2 `snprintf(meter, ...)` の移動

`shell.c` の `char meter[16]; meter[0]=0; if(show_fps) snprintf(...)` に変更（FPS OFF では書かない）。
同じランの PERF（mode=3、n=50）: **`fmt=0.000`（全50サンプル、min=max=0.000）**、変更前の
`fmt=0.28–0.32` が消えた。`hud` は 1.24–1.42 ms。ただし**このランは overlay が走っていない**
（ホストから BACK を送って HOME に戻した）ので `ovl=0.06–0.19`、`hud` の総量を当時の
1.93–2.11 ms と直接比べてはいけない（当時は overlay アプリ running = `ovl≈1.5`）。
`fmt` の 0 だけがこの変更の効果で、それは全サンプルで確認できた。

---

## 3. 既に入った最適化（消えた量つき）

| 何を | 出典 | 消えた量（実測） |
|---|---|---|
| `shade` の `floorf`×2 → `ifloor`（`flower.c:447-453`） | `flower-perf-handoff.md:23` | `prep: petals=` **2,710 → 2,395 cy/part**（−0.033 ms/frame）。`floorf` は約 78 cy |
| `flower_prepare` の `floorf`2+`ceilf`2（`flower.c:729-732`） | 同上 | 最大の種で 224 本/フレーム。いま `flower.s` に `floorf`/`ceilf` は **0 本** |
| 帯ごとの `1.0f/a` → `petal_reciprocals`（`flower.c:409-424`） | `pie-simd.md:242-250` | calla で `bell` **9.05-9.55 → 7.74-8.20 ms**（−1.33 ms/frame）。消えた除算 4,770 本/フレーム、1 本 **55〜67 cy** |
| 帯境界と直線係数の巻き上げ（`flower.c:755-776`） | `pie-simd.md:313-317` | visit 1,989 → 1,580 cy（ただし同じコミットに `bell_reject` が同居＝**どちらが何を出したか未分割**） |
| `bell_reject`（`flower.c:1039-1058`） | `flower-perf-handoff.md:122-127` | visit の **43.6%** を落とす（取りこぼしゼロ）が、**実機の効果は計測と同時出荷で復元不能** |
| 庭の画素ループ PIE 化（`garden.c:994`） | `pie-simd.md:677-679` | `kernel=` 66.4 → 48.9 ms（1.36×、予測 38〜44 は外れ） |
| 融合ロード（`garden.h:250-263`） | — | ブロックあたり 135 → 114 命令 |
| 林冠の除算を `garden_recip` へ（`garden.c:1519-1533`） | — | 約 10,450 画素/フレームの除算を楕円行ごとの 1 本に |
| frost span の PIE 化（**kasane 系統。flower は kasane 未使用**） | `pjs-render/docs/design-device-probe.md:228-247` | 同一バイナリ A/B で **2.486×**（5,659.7 → 2,276.4 µs） |

**この 2.486× を flower に移植してはいけない**（違う仕事量・違う入力・違う呼び出し形。
§3.8 の「倍率は毎回外れる」の実例が 5 件ある）。

---

## 4. ノウハウを flower に当てる（§3.8〜§3.13）

- **拒否テストの経済学**: 行レベルの棄却（装飾の `!fade`/`y>=end`/`arrival==0` で 540 層行の **59.1%** が
  画素ループに到達しない）は**率どおりに効く**（行ごと丸ごと消える）。画素レベルの棄却は効かない:
  保護判定 `gap<=0` は visit の 17.4% を落とすが、落ちるのは 15 命令だけで前置部 24 命令は走る。
  楕円体の `d<0` は 5,741 visit 中 4,192（61.7%）を抜くが、**それが最安のピクセル**。
- **overlay の 17 回**（`shell.c:557,564,572`）: FLOWER 自身の overlay は NULL だが `overlay_paint` が
  17 回走り、実機で 1.5–1.6 ms 計上されている（上記 2-3）。「入れ子の見えない計器」の現在進行形。
- **重なる走査の非線形性**: 装飾 4 層は visit 21,123 に対して**異なり画素 14,520**（1 画素 1.45 visit、
  最大 4）。§3.9 の表（1/2/3/4 掃引 = 1.00/1.33/1.62/2.06）に対し **1.33 のケースよりわずかに上**。
- **半径は歩幅で買う**: 現状**ゼロ円**。材料は `#if FLOWER_HORROR` の中だけ（既定 0、実機も
  `horror=0.000`）。有効化するときにだけ効く規則（極を上げるのではなくタップ間隔を上げる。
  7/8 は FIR 化＝PIE 化の道を閉じる）。
- **ピクセルに見えてフレーム 1 回でよかった除算**: **画素ループには浮動も整数も除算が残っていない**
  （`flower_draw` の整数除算 17 本は全部 SPLIT の `printf`。`shade` は 0 本。`ray_row` の `/` は
  `DIVR`＝逆数乗算で、除数は全部巻き上げ済み）。残るのは `petal_reciprocals` の
  **11 本/部品/フレーム**（31 部品で 341 本 ≈ 0.078–0.095 ms = `petals=` の約 30%）。
  次に除算を探すなら「部品ごとの幾何定数」の側。
- **`floorf` の条件付き置換**: 完了。`ifloor`/`iceil` は表現についての恒等式（2^32 総当たりで証明、
  `tools/test_flower_floor.c`）。
- **FPU の種命令（§3.13）**: `sqrtf` 合計 2.14–2.23 ms が対象。**ただし `decor` の 14–17 ms を説明しない**
  ので順序は後。in-situ 111 cy と単体計測 174–188 cy は別物なので商に混ぜない。

---

## 5. PIE 候補（床つき・優先度順）

床 = `命令数 × 1 cycle ÷ 240 MHz`（ロード・分岐・キャッシュ抜き）。実動作はこの 1.3〜1.4 倍。

| 順 | 対象 | file:line | 内側の形 | 床 | 判定 |
|---|---|---|---|---|---|
| P0 | **`decor` の分割**（PIE ではなく計器） | `garden.c:1578` | — | — | これを割らないと順序が決まらない |
| P1 | 林冠 `garden_canopy_row` | `garden.c:245-255`（呼び出し `:1533`） | 46 命令/画素 × **10,462 画素**/フレーム、8 画素/ブロック。ソース自身が「Secondary PIE candidate」と印 | **2.0 ms**（係数込み 2.6–2.8） | SIMD の形は既に用意されている（`q=clamp(qbase-t,0)` が分岐を消す、`garden.c:238-240`） |
| P2 | 装飾光線の x ループ | `garden.c:1466-1481` | 本体 52 命令、visit 21,123 / profile 17,438 / 書いた画素 13,159、layer-row 221/フレーム | **3.9 ms**（係数込み 5.1–5.5） | 効くが **run が 91 画素以下＝足場が相対的に高い**。P1 より先にやる理由が無い |
| P3 | 楕円体の棄却 | `flower.c:1353-1355` | 5,741 visit（61.7% が `d<0`） | 0.53 ms | 棄却で落ちる visit は平均より安い（§3.8）。**順序は最後** |
| P4 | 幹（15 画素スパン×405 本）/ 草（4 画素） | `garden.c:1497-1505, 1537-1560` | 8 レーンに満たない | 0.30 / 0.12 ms | `garden.c:227` 自身が「neither is worth eight lanes」と書いている |
| P4 | `shade` | `flower.c:902-991` | 512 命令の素材別分岐、float 23 ld/st | — | 実測 677–689 cy/hit = 1.3 cy/命令で既にほぼ下限。SIMD の形が無い |

---

## 6. 一手

- **今すぐ**（副作用ゼロ）: `garden_row_blend` に `vegetation`/`rays` の 2 ブラケット＋`bloom_garden_mix`
  を出し、`snprintf(meter,...)` を `if (show_fps)` の中へ移す。
- **測ってから決める**: その 2 つの数字を見て、`rays`（床 5.1–5.5 ms）か `vegetation`（床 3.2–3.4 ms、
  うち林冠 2.6–2.8 ms）のどちらにカーネルを書くかを決め、`tools/pie/` の 3 層を通し、
  **同一バイナリ・1 define の A/B** で実機に聞く。それまで倍率は書かない。

## 7. この調査で見つけた道具側の穴

`/workspace/pjs-vm/tools/pie/piesim.py` の `EE.VMUL.U16` は `((x*y) & 0xFFFF) >> SAR` と実装されて
いるが、装置側の記録（`backups/pie-examples-20260914T183007Z.log:162`、45/45 通過、
30000×300>>8 = 0x8954）は「**先にシフトしてからマスク**」を示す。この読みだと ex22 のカーネルは 8/8 で
崩れる。**要検証**（piesim 側の修正候補）。
