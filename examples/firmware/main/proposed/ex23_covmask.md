# ex23 (proposed) — テキスト描画のマスク合成（Rust rgb565 の coverage → mask ループ）に PIE が効くか

> **ABI 修正（call8 callee は a10〜a15 を壊してはいけない）**: 本文の計測値・命令数は修正前の `.S` で取ったもの。
> `ex23_covmask.S` に a10..a15 の退避・復帰（prologue の `s32i` と各 `retw.n` 前の `l32i`）を追加したため、
> **関数全体のサイズ・命令数は増えている**（このファイルで 33 命令）。**ループ本体と 1 反復あたりの命令数は不変**（ループ内は無改変）。
> md5: `5fba763d8590a650542a6cd344bcc37c` → `9f2d37b1bb6d563130a7a2ed835fb1af`（tools/check_abi.py / tools/fix_abi.py）

`examples/firmware/main/proposed/ex23_covmask.S` とこの文書は**ビルドに入っていない**。`examples/firmware/main/CMakeLists.txt` は
`ex01`〜`ex12` だけを列挙しており、`proposed/` のどのファイルもそこには無い。呼び出し元も無い。対象のコードは**別リポジトリ**
（`cardputer-adv-pocketjs` の `.cache/pocketjs`、固定 revision `6a0a1b6`）にあり、**そちらは一切触っていない**。

**実機（`/dev/ttyACM0`）は使っていない**（親が占有）。この文書にデバイスの時間実測は 1 つも無く、サイクルとして書いてある数字は
すべて**推定**で、その場にそう書く。測定は「設計」まで。

## 何が確認済みで、何が未確認か (status, honestly)

| 項目 | 状態 |
| --- | --- |
| 実装事実（1画素の処理・除算の本数・scale/density・mask の整列・`sx` の加算化） | **確認済み**。全部 `file:line` 付きで §1 に引く |
| `scale==density` で除算が消えること（issue 要求2） | **確認済み（ホスト実行）**。掃引 130,460 px 不一致 0、負の対照（scale≠density）は 8,400 px 中 7,378 不一致（§2） |
| PIE カーネルの意味（契約との全画素一致） | **確認済み（モデル上）**。**アセンブル済みオブジェクトの objdump を 1 命令ずつ実行**し、契約の Python 参照＋ホスト C 参照と突き合わせ。A: 414,540 px 不一致 0、B: 122,850 px 不一致 0、C 参照との不一致 0（§3.6） |
| アセンブル | **確認済み**。`xtensa-esp32s3-elf-as`（esp-15.2.0_20251204）が通る。警告なし |
| `EE.LD.128.USAR.IP` + `EE.SRC.Q.QUP` の非整列ウィンドウ | **TRM 疑似コードどおりにモデル化**。装置での確認は無い（このリポジトリに前例が無い）。A の本体がこれに依存する（§7） |
| `EE.VZIP.8` / `EE.VUNZIP.8` のバイト順 | モデル上で TRM p294 / p292 を実装。ただし**装置確認はこのリポジトリに無い**（ex22 の §11 が同じことを書いている） |
| サイクル・フレーム時間 | **未計測**。§3.7 は命令数の算術（`docs/pie-simd.md` §3.5 のコストモデル）だけ |
| 実機ラン | **していない**。issue の要求1「まず測る」には §5 の測定設計まで |
| **窓の幅**（1グリフ行あたりの画素数） | **確認済み（コードとフォント生成器から）**。`scale=1` では 6 / 12 / 12 px。**これが「PIE が効くか」を決める**（§1.6） |

---

## 0. 結論を先に

1. **issue の要求1（まず測る）は、いまのエンジンでは外から測れない。** `render_ms` は「ドローリスト走査＋マスク生成＋
   ホストのオーバーレイ」の合計で、マスク生成だけを見る計器がエンジン側に無い。§5 に、どこに `rsr.ccount` を置いて
   何を分けるかを関数名と行番号で書いた（3行の一時的なエンジン改造＋既存 PAINT 行への1項目追加）。
2. **スカラーだけで取れる分は大きい。** `scale==density` のとき `sx` は画素ごとに 1 ずつ進む（§1.5 の式）ので、
   **1画素あたりのハードウェア除算（実測16〜18サイクル、`docs/pie-simd.md` §3.7）が消える**。出力は 130,460 px で完全一致（§2）。
   PIE を使わないので ABI も固定 revision も動かさない。
3. **PIE カーネルは書けて、意味は全画素一致させられる（A/B とも）。ただし issue の構図（16レーンずつ）のままでは、
   いまのフォントではベクタ本体が一度も走らない。** `scale=1` の窓は 6 px（font_small）、12 px（font_large / 日本語 slot 2）で
   16画素ブロックに届かない（§1.6）。モデルの実測でも、12 px の行は **202 命令 / 12 px = 16.8 命令/px、全部スカラー**、
   16 px ブロックが 1 つ走る形だと **60 命令 / 16 px = 3.75 命令/px**（§3.6 [6]）。
4. **効く順**は §9 に整理: (a) 全部 0 の行を捨てる（全行の 20.7%、§1.7）、(b) マスクが 0 の窓はプリスケールだけ
   （`a==255` なら単なるコピー）、(c) `sx` の加算化、(d) それでも足りなければ呼び出し側を広い連続区間に組み替えてから PIE。
5. 差し込み口（要求4）は **`PpaOps` に1メソッド足す形で足りる**が、**`PpaOps` は3メソッドとも既定実装が無い**ので、
   足すなら既定実装付きにする（実装者4つのうち2つはテスト側）。上流への変更は 6 ファイル・概算 66〜97 行、うち約35行は
   機械的な ABI の配管。**固定 revision は上げる必要がある**（§4.3）。

---

## 1. 実装事実（file:line と引用）

### 1.1 問題のループが1画素あたり何をしているか

`cardputer-adv-pocketjs/.cache/pocketjs/engine/backends/rgb565/src/lib.rs`、`try_glyph_run`（`fn` は `:672`）の内側:

```
lib.rs:702   let mask = self.mask_mut();
lib.rs:703   fill_mask_rect(mask, width, rect, 0);          <-- 合成先は毎回 0 で塗り直される
lib.rs:729   for py in y0..y1 {
lib.rs:733       let sy = coverage_index(py - gy * scale, scale, density, coverage_h);
lib.rs:734       let row = &rows[sy * bpr..];
lib.rs:735       for px in x0..x1 {
lib.rs:736           let sx = coverage_index(px - gx * scale, scale, density, coverage_w);
lib.rs:737           let local_x = px - surface.x0 * scale;
lib.rs:738           let local_y = py - surface.y0 * scale;
lib.rs:739           composite_mask(
lib.rs:740               &mut mask[local_y as usize * width as usize + local_x as usize],
lib.rs:741               ((row[sx] as u32 * a + 127) / 255) as u8,
lib.rs:742           );
```
1画素あたり: **1回の可変除数除算**（`:736`）＋被覆率の u8 ロード＋`×a`＋`+127`＋定数255除算（コンパイラが乗算シフトに落とす）
＋マスク u8 のロード／ストア＋`composite_mask` の `(d*(255-s)+127)/255`（同じく定数除算）。

```
lib.rs:1073  #[inline]
lib.rs:1075  fn composite_mask(destination: &mut u8, source: u8) {
lib.rs:1076      let d = *destination as u32;
lib.rs:1077      let s = source as u32;
lib.rs:1078      *destination = (s + (d * (255 - s) + 127) / 255) as u8;
```
```
raster.rs:984  pub fn coverage_index(destination_px: i32, output_scale: i32, atlas_density: i32, limit: i32) -> usize {
raster.rs:994      ((((2 * destination_px + 1) * atlas_density) / (2 * output_scale)).clamp(0, limit - 1)) as usize
```
`:994` の除数 `2 * output_scale` は**実行時の値**（`scale` は `RendererConfig` のフィールド）なので、コンパイラは定数除算の
魔法の乗算に置き換えられず、**ハードウェア除算器**が呼ばれる。この石での値段は **16〜18 サイクル**（`docs/pie-simd.md` §3.7、実機実測）。

### 1.2 除算はどこで何回入るか

| 場所 | 除数 | 回数 | 値段 |
| --- | --- | --- | --- |
| `raster.rs:994`（`lib.rs:736` から、横） | `2*scale`（**実行時**） | **1行につき (x1-x0) 回**＝窓の幅だけ | **HW除算 16〜18 cyc**（実測、§3.7） |
| `raster.rs:994`（`lib.rs:733` から、縦） | `2*scale`（**実行時**） | 1グリフ行につき 1 回 | 同上（本数の比は 1/幅） |
| `lib.rs:741` の `(row*w+127)/255` | 255（**定数**） | 1画素1回 | 魔法の乗算＋シフト（数命令） |
| `lib.rs:1078` の `(d*(255-s)+127)/255` | 255（**定数**） | 1画素1回 | 同上 |
| ソフトウェア・フォールバック版 `engine/core/src/raster.rs:1037,1040` | 同上 | 同上 | 同上 |

**このループの主項は `:994` の可変除数除算**である（16〜18 対 残り 8〜10 程度）。issue の「画素ごとにハードウェア整数除算が1本」は正しい。

### 1.3 `scale` と `density` が実行時にどうなるか

- `scale`: `main/app_session.c:596` `pocketjs_rgb565_renderer_config_defaults(&rc);rc.scale=1;`（`pjs-vm/main/app_session.c:596` も同じ）
- `density`: `main/app_session.c:489` `cc.logical_width=LCD_W;cc.logical_height=LCD_H;cc.raster_density=1;cc.tick_hz=30;`
  これをアトラスのヘッダに入れているのは各フォントの生成器・持ち込み:
  - `tools/make_font.py:55` `atlas(scale,...)`: `struct.pack('<IHH8B', magic, 3, count, 6*scale, 8*scale, 7*scale, 9*scale, slot, 0, 1, 0)`
    → **cell_w=6\*scale, cell_h=8\*scale, density=1**。`font_small=atlas(1,0)`（cell **6×8**）、`font_large=atlas(2,1)`（cell **12×16**）
  - `main/text/jsfont.c:45` `blob[12]=JSFONT_SLOT; blob[13]=0; blob[14]=1; blob[15]=0;   // density 1`、cell は
    `jpfont_cell_w(JPFONT_TEXT)`（`main/text/jsfont.c:29`）。`JSFONT_MAX 160` のコメント（`main/text/jsfont.h:19`）が
    「160 × **152 B**」と書いており 152 = 12×12 + 8 なので **cell 12×12**
  - `coverage_width() = cell_w * raster_density`（`engine/core/src/resources.rs:15-17`）、`bytes_per_row() = coverage_width()`（`:21-22`）
- `pocketjs_native_renderer_render_strip` は `scale != raster_density` を**拒否**する
  （`hosts/esp-idf/native/render-rgb565/src/lib.rs:380-382`）。**つまり実機でこのパスに来るのは常に `scale == density == 1`。**

### 1.4 mask のメモリ配置と、16B 整列が成り立つ条件

```
lib.rs:28       const MASK_ALIGNMENT: usize = 128;
lib.rs:169-170  mask_storage: Vec<u128>, mask_offset: usize,
lib.rs:909-917  fn ensure_mask(&mut self, len: usize) {
                    if self.mask_capacity < len {
                        let bytes = len + MASK_ALIGNMENT - 1;
                        self.mask_storage = alloc::vec![0u128; bytes.div_ceil(16)];
                        let base = self.mask_storage.as_ptr() as usize;
                        self.mask_offset = (MASK_ALIGNMENT - base % MASK_ALIGNMENT) % MASK_ALIGNMENT;
                        self.mask_capacity = len;
                    }
                    self.mask_len = len;
                }
lib.rs:920-927  fn mask_mut(&mut self) -> &mut [u8] {
                    (self.mask_storage.as_mut_ptr() as *mut u8).add(self.mask_offset), self.mask_len
                }
```
- `mask_offset = (-base) mod 128` なので `mask_storage + mask_offset` は **128B 整列**（16B 整列を含む）。ベクタ本体の
  u128 要素も 16B 整列。したがって **mask の base は常に 16B 整列**（実際は 128B の方が強い）。
- 行頭 `mask + y*width` が 16B 整列になる条件は **`width % 16 == 0`**。`width = logical_width * scale`（`lib.rs:344`）で、
  `logical_width=240`・`scale=1` なので **240 % 16 = 0**（`lib.rs:370` `self.ensure_mask(destination.len())`）。
- ただし**グリフの窓の先頭** `local_x = px - surface.x0*scale`（`lib.rs:737`）は `gx*scale` 由来なので任意。だからカーネルの
  契約は「mask は任意整列でよい。カーネル自身が 16B まで頭をスカラーで進める」にしてある（§3.2）。

### 1.5 `scale == density` のとき `sx` が画素ごとに 1 ずつ進むこと（式で）

`sx = coverage_index(p, scale, density, coverage_w)`, `p = px - gx*scale`。`density == scale == d` とすると

```
sx = floor( ((2p+1) * density) / (2 * scale) )       raster.rs:994（clamp 前の生の商）
   = floor( ((2p+1) * d) / (2 * d) )
   = floor( (2p+1) / 2 )                             d は約分で消える
   = floor( p + 1/2 )
   = p                                               p は非負整数
```
**除算が恒等写像になる。** さらに clamp も死ぬ: `px ∈ [x0, x1) ⊆ [gx*scale, (gx+cell_w)*scale)` なので
`p ∈ [0, cell_w*scale)`、`coverage_w = cell_w*density = cell_w*scale`、したがって `p <= coverage_w - 1 = limit - 1`。
`limit-1` より大きい `p` は存在しないので `.clamp(0, limit-1)` は一度も効かない。縦の `sy` も同じ。

掃引（`math_check.py` [1]）: `d=s` について `(s,p)` を 262,144 通り掃いて**不一致 0**。C の対照（§2）でも `clamp` の発火は 0。

### 1.6 窓の幅 — ここが「PIE が効くか」を決める

`n_px = x1 - x0 <= cell_w * scale`（`lib.rs:726-730`）。`scale=1` の実機で:

| フォント | cell | `scale=1` の窓 | 16画素ブロック | 8画素ブロック |
| --- | --- | --- | --- | --- |
| font_small（`app_session.c:491`） | 6×8 | **6 px** | 0 | 0 |
| font_large（`app_session.c:492`） | 12×16 | **12 px** | 0 | 頭の整列が 4 以下のときだけ 1 |
| 日本語 slot 2（`jsfont.c`） | 12×12 | **12 px** | 0 | 同上 |
| font_large を `scale=2` で | 12×16 | 24 px | 1 | 3 |
| font_small を `scale=3` で | 6×8 | 18 px | 1 | 2 |

（`math_check.py` [4] の出力そのまま。`blocks = max_head ( (n_px-head) >> log2(block) )`、`head` は mask の整列で 0..15。）

**`scale=1` では 16画素ブロックを開始できるフォントが 1 つも無い。** 8画素版でも 12 px の窓で最大 1 ブロック
（mask の整列が合ったときだけ）。**これが issue の構図のままでは PIE が効かない、という結論の根拠**（§3.6 [6] のモデル実測も同じ）。

### 1.7 ループの「死んでいる部分」（PIE より先に取れる分）

マスクは合成の前に **0 で塗り直される**（`lib.rs:703`）。`composite_mask(d=0, s) = s + (0*(255-s)+127)/255 = s + 0 = s` なので:

- **被覆率が 0 の画素**は `out = d`（何も変わらない）。それでも現行ループは除算を払う。
- **その窓のマスクがまだ 0**（同じ run の先のグリフが書いていない）なら、行の処理は `mask[i] = (cov[i]*a+127)/255` の
  **プリスケールだけ**になる。`a == 255` のときは `(255c+127)/255 = c` なので**ただのバイトコピー**。除算も乗算も合成も要らない。
- **行が全部 0** なら、その行の合成は恒等写像＝丸ごと捨てられる。

どれくらいあるか（`zero_rows.py`、`tools/make_font.py` の GLYPHS 表＝font_small/font_large の元データから数えた）:

```
font_small (cell 6x8): 95 glyphs, 760 cell rows, 157 all-zero rows (20.7%), zero coverage bytes 3365/4560 (73.8%)
    'Hello World'  rows  88  all-zero rows  18 (20.5%)   pixels  528  zero coverage  375 (71.0%)
    '12:34'        rows  40  all-zero rows  10 (25.0%)   pixels  240  zero coverage  185 (77.1%)
    'A.'           rows  16  all-zero rows   8 (50.0%)   pixels   96  zero coverage   77 (80.2%)
    ' '            rows   8  all-zero rows   8 (100.0%)   pixels   48  zero coverage   48 (100.0%)
font_large (cell 12x16): 95 glyphs, 1520 cell rows, 314 all-zero rows (20.7%), zero coverage bytes 13460/18240 (73.8%)
    'Hello World'  rows 176  all-zero rows  36 (20.5%)   pixels 2112  zero coverage 1500 (71.0%)
    '12:34'        rows  80  all-zero rows  20 (25.0%)   pixels  960  zero coverage  740 (77.1%)
    'A.'           rows  32  all-zero rows  16 (50.0%)   pixels  384  zero coverage  308 (80.2%)
    ' '            rows  16  all-zero rows  16 (100.0%)   pixels  192  zero coverage  192 (100.0%)
```
`'Hello World'`（font_small）で 528 画素のうち **71.0% が被覆率 0**、行の 20.5% が全0行。`.`・空白の多い文字列では全0行が 50〜100%。
（日本語 slot 2 の実データ＝フラッシュのフォント・ブロブは読んでいないので**未測定**。）

---

## 2. スカラーの対照（issue の要求2。PIE と混ぜない）

`/tmp/ex23/scalar_contrast.c`（`gcc -O2`、ホスト実行）。二つの関数は **`sx` の作り方だけが違う**:

- `mask_row_division`: `lib.rs:736` と同じく `coverage_index` を毎画素呼ぶ（`(2p+1)*density/(2*scale)`、clamp 付き）
- `mask_row_increment`: `scale==density` を前提に `sx` を**加算で**進める（初期値 `x0-gx*scale`、1画素ごとに `sx++`）

```
=== [1] scale == density  (the shipped configuration is scale=1, density=1)
    cells swept 4000   pixels swept 130460   mismatches 0
    first mismatch: None
    clamp hits  division 0   increment 0   (the clamp never fires when scale==density)

=== [2] scale != density (the increment version must NOT be used)
    pixels swept 8400   mismatches 7378   first mismatch px=7 at scale=2 density=1

=== [3] the division itself, on the host, for reference only
    (the DEVICE cost of a runtime integer division is the calibrated 16-18 cycles of
     docs/pie-simd.md sec.3.7; a host x86 division is not that number)
    256-px row x 200000 (HOST ticks, not device cycles): division 0.049 s  increment 0.049 s  (1.02x)
```
- **[1] `scale == density`**: `cells 4000 / pixels 130,460 / mismatches 0`。clamp は両版とも 0 回（§1.5 の式どおり）。
- **[2] `scale != density`（負の対照）**: `pixels 8,400 / mismatches 7,378`、先頭は `px=7, scale=2, density=1`。
  **加算版は `scale==density` でしか使えない**（使うなら実行時に等式を検査して分岐する）。
- **[3] ホストの時間**: 0.97x（差が出ない）。**ホストの除算はこの石の値段ではない**ので、この行は「ホストでは見えない」ことの
  証拠として置いてある。実機の値段は `docs/pie-simd.md` §3.7 の 16〜18 cyc（除算1本）を使う。
- 逆アセンブルでの裏取り: `mask_row_division` の本体に `div` 系が **8 本**、`mask_row_increment` の本体に **0 本**
  （`objdump -d --no-show-raw-insn scalar_contrast`）。

**期待値（推定、§5 の測定で確かめる）**: 1画素 = 除算 16〜18 + 残り 8〜10 ≈ **25 cyc/px** → 加算版 ≈ **9 cyc/px**。
このループに限れば**約 2.5〜3 倍**。実装は `lib.rs:729-745` の内側で、`scale == density` のときだけ `sx` をカウンタにする
（20行程度、出力は同一）。**PIE を混ぜないので ABI も固定 revision も動かさない。**

---

## 3. PIE カーネルの設計（issue の要求3）

### 3.1 u8 レーン命令の列挙 — 使える形と使えない形

`data/pie_instructions.json`（220命令）を実際に引いた結果。必要なのは、レーンごとに `q = floor((c*d+127)/255)`
（c,d ∈ [0,255] → q ∈ [0,255]）と `out = c + d - q`（∈ [0,255]）。

```
VMUL.U8 with SAR=8 : floor(c*d/256) != floor((c*d+127)/255) for 47056 of 65536 pairs
VMUL.U8 with SAR=0 : (c*d mod 256) != floor((c*d+127)/255) for 64927 of 65536 pairs
  and the max of floor(c*d/256) is 254 -- a full-coverage lane can never reach 255

VMULAS.U8.QACC (p257) accumulates exactly into 16 20-bit lanes (max x = 65152 < 2^20),
  but the readout SRCMB.S8.QACC (p131) saturates to [-127,127]:
  floor((255*255+127)/255) = 255 saturates to 127, i.e. a mask value of 128..255
  is not representable on the way out. ST.QACC_*.IP stores raw bit fields of the 160-bit
  QACC (QACC_L[127:0] etc.), not narrowed lanes, so there is no 8-bit readout for u8 lanes.

VADDS.S8 / VSUBS.S8 (p152/p287) saturate to [-128,127]: out = c + d - q reaches 255.
VZIP.8 (p294) / VUNZIP.8 (p292): the byte<->16-bit-lane widening and compaction -- usable, used.
LDQA.U8.128.IP (p111): 16 bytes -> 16 20-bit QACC lanes, zero-extended: a usable widening load
  for a QACC path, unusable when the result must be written back as bytes (see above).
```
| 命令（page） | 何ができるか | 判定 |
| --- | --- | --- |
| `EE.VMUL.U8` p207 / `EE.VMUL.S8` p201 | `qz[7:0] = (qx[7:0]*qy[7:0]) >> SAR` — **結果フィールドが8bit** | **使えない**。SAR=8 で `floor(cd/256)`（65,536 組中 **47,056 組**で必要値と違う・全被覆でも 254 止まり）、SAR=0 で `cd mod 256`（**64,927 組**違う） |
| `EE.VMULAS.U8.QACC` p257 | 16レーンを**20bit QACC に厳密累算**（x ≤ 65,152 < 2^20） | **累算は使える。読み出しが使えない** |
| `EE.SRCMB.S8.QACC` p131 | QACC を as[4:0] シフトして **s8 に飽和**して読む | **使えない**。255 が 127 に飽和する＝マスク値 128..255 が表現できない |
| `EE.ST.QACC_L/H.*.IP` p136-139 | QACC の**生のビット列**（`QACC_L[127:0]` 等）をストア。20bit レーンの詰め直しはしない | **使えない**（8bit 読み出しが無い） |
| `EE.VADDS.S8` p152 / `EE.VSUBS.S8` p287 | [-128,127] で飽和 | **使えない**（out は 255 まで行く） |
| `EE.LDQA.U8.128.IP` p111 / `EE.MOV.U8.QACC` p117 | 16バイト → 16×20bit の QACC レーンへ**ゼロ拡張で**ロード | 使える（QACC 経路の widening ロード）。ただし上の読み出し問題は残る |
| `EE.VCMP.LT.S8` p163 | 0xFF/0 のマスク | 使える（本カーネルでは不要） |
| **`EE.VZIP.8`** p294 / **`EE.VUNZIP.8`** p292 | 8バイト ⇄ 8本の16bitレーンの**widening / 詰め直し** | **使える。本カーネルはこれで u16 レーンに広げている** |
| `EE.VLDBC.16[.IP]` p170-171 | 16bit 半語を全レーンへブロードキャスト | 使える（定数と、`s`/`255-s` のレーン化） |

**結論: u8 レーンは「広げる」以外では使えない。** この式は 16bit レーン（`EE.VMULAS.U16.QACC` の40bit アキュムレータ＋
`EE.SRCMB.S16.QACC` の s16 読み出し）で計算する。widening は `EE.ZERO.Q` + `EE.VZIP.8` の 2 命令 / 8 バイト、
詰め直しは `EE.VUNZIP.8` の 1 命令 / 16 バイト。

### 3.2 カーネル A の設計と契約

```
void     ex23_cov_to_mask(uint8_t *mask, const uint8_t *cov, const uint8_t *a_row, uint32_t n_px, uint32_t scale_tag);
void     ex23_cov_row_advance(uint8_t *mask, const uint8_t *cov_row, uint8_t s, uint32_t n_px);
uint32_t ex23_cov_to_mask_supported(uint32_t scale_tag);   /* 1 = 実装済みの tag（0 のみ） */
```

| | A | B |
| --- | --- | --- |
| 式 | `mask[i] = s + (mask[i]*(255-s)+127)/255`, `s = (cov[i]*a+127)/255` | 同じだが `s` が行内で一定 |
| `mask` の整列 | 任意。カーネルが 16B まで頭をスカラーで進める | 同じ |
| `cov` の整列 | **任意**（`EE.LD.128.USAR.IP` + `EE.SRC.Q.QUP`） | 契約上 `cov_row[i] == s`。頭と尾だけ読む（本体は読まない） |
| `n_px` | 任意（頭＋16画素ブロック＋尾） | 同じ |
| `scale_tag` | 0 のみ実装。**それ以外は何も書かずに戻る** | — |
| `a == 0` / `s == 0` | **何も書かずに戻る**（`out==d` なので書くのは no-op） | 同じ |
| `s == 255` | — | 255 を書く（スカラーの 255 フィル） |
| 使うレジスタ | q0..q7（8本ちょうど）、SAR は触らない | 同 |
| 本体の命令数（objdump 実測） | **34 命令 / 16 px**（`0x64` = 100 バイト、`loopgtz` 可） | **28 命令 / 16 px**（`0x52` = 82 バイト） |

**A の a==255 高速路は元の式とビット一致する。** `a == 255` のとき `s = (255c+127)/255 = c`（厳密）なので

```
s + floor((d*(255-s)+127)/255) = floor((255s + d*(255-s) + 127)/255)
                               = floor((255(c+d) - c*d + 127)/255)
                               = c + d - floor((c*d+127)/255)          <-- レーンが計算する形
```
（`255c` が 255 の倍数だから `floor((255c+X)/255) = c + floor(X/255)`。全 65,536 組の `(c,d)` で不一致 0、`math_check.py` [3]。
**この畳み込みは `a == 255` でしか成り立たない**: `a ∈ {1,64,128,200,254}` × 65,536 組では **327,680 組中 270,679 組が違う**。
だから `0 < a < 255` はレーンに載せず、A 自身の**スカラーループ**（除算なし・上と同値の2段の丸め）で全部処理する。）

### 3.3 カーネル B（`s` が行内で一定）

`out = s + floor((d*(255-s)+127)/255)`。`s` と `255-s` は呼び出しごとに1回だけレーン化（スタック上の2バイト＋`EE.VLDBC.16`）。
`cov_row` を読むのは頭と尾だけなので、本体は `EE.VLD.128.IP`（マスク）→ widening → 7命令の2段 `/255` × 2 → `VADDS(s)` →
`VUNZIP.8` → `VST.128.IP` で、**レーンに関わる負荷が1本も無い**。`s == 0` は即戻り（何も書かない）、`s == 255` は 255 フィル。

**B の契約の注意**: 本体は `cov_row` を読まないので、**行が一様でないと本体は s を使って誤る**。頭と尾はその行自身のバイトを使う
（モデル §3.6 [4] で、行の 1 バイトだけ 0 にすると頭のバイトは 0 として、本体の 16画素は s として処理されることを示してある）。
一様な行（全0の行、全被覆の行、呼び手が畳んだ行）専用である。

### 3.4 `/255` の厳密化とビット一致の条件

```
=== [1] coverage_index(destination_px, s, d, limit) with d == s
    raster.rs:984  floor((2p+1)*d / (2*s))  clamp(0, limit-1)
    swept 262144 (s,p) pairs with d=s: mismatches 0
    the algebra: (2p+1)*d/(2*d) = (2p+1)/2 exactly, floor((2p+1)/2) = p
    so the clamp is also dead: p <= cell_w*scale-1 = coverage_w-1 = limit-1

=== [2] floor(x/255) == (x + (x>>8) + 1) >> 8, x <= 65152
    swept 70000 values: mismatches 18 (all of them at x > 65152: True; below the bound: 0, first 65535)
    the kernels' numerators:
      a==255 path  : x = c*d + 127                       max 255*255+127 = 65152
      0<a<255 path : x = c*a + 127 (a<=255)              max 255*255+127 = 65152
                     x = d*(255-s) + 127, s>=0           max 255*255+127 = 65152
    all <= 65152, which is the bound the identity needs

=== [3] the composite: def vs the collapsed form
    def       out = s + floor((d*(255-s) + 127)/255)          (lib.rs:1073-1078)
    collapsed out = c + d - floor((c*d + 127)/255)            (a == 255, so s == c)
    (c,d) pairs swept 65536: mismatches 0
    proof: 255c and 255d are multiples of 255, so
      s + floor((d(255-s)+127)/255) = floor((255s + d(255-s) + 127)/255)
                                   = floor((255(c+d) - cd + 127)/255)
                                   = c + d - floor((cd+127)/255)
    the SAME collapse with a<255: 57001 of 327680 agree, 270679 differ; first (1, 1, 0, 0, 1)
    -> the a<255 case needs two rounded divisions, so the kernel branches on a

=== [4] can a real glyph-row window feed a block?
    cell sizes: tools/make_font.py:55  atlas(1,0) -> 6x8   atlas(2,1) -> 12x16
                main/text/jsfont.c:29 + JSFONT_MAX comment -> jpfont cell 12x12
    window n_px = cell_w * scale (lib.rs:733-737, the cell's own width)
    blocks = (n_px - head) >> log2(block);  head = mask+x0 alignment, and x0 is
    gx*scale for an arbitrary glyph position, so head can be any 0..block-1
    source                                 n_px 16-lane blocks 8-lane blocks
    font_small (scale=1)                      6              0             0
    font_large (scale=1)                     12              0             1
    jsfont slot 2 / Japanese (scale=1)       12              0             1
    font_large (scale=2)                     24              1             3
    font_small (scale=3)                     18              1             2
    font_large (scale=4)                     48              3             6
    -> at the shipped scale=1 NO shipped font can start a 16-lane block:
       the kernel's head scalars consume the whole window. An 8-lane kernel gets
       one block for a 12-px window and only when the mask address lines up
       (head <= 4); a 6-px window gives it none either.

=== [5] the flat cost of one line, so the measurement has a scale to compare to
    11 Latin chars, font_small     528 mask px  x 25 cyc =   13200 cyc =   55.0 us @240MHz   [with the per-pixel division (16-18 + ~8)]
    11 Latin chars, font_small     528 mask px  x  9 cyc =    4752 cyc =   19.8 us @240MHz   [increment spelling (no division)]
    11 Latin chars, font_large    2112 mask px  x 25 cyc =   52800 cyc =  220.0 us @240MHz   [with the per-pixel division (16-18 + ~8)]
    11 Latin chars, font_large    2112 mask px  x  9 cyc =   19008 cyc =   79.2 us @240MHz   [increment spelling (no division)]
    11 Japanese chars, slot 2     1584 mask px  x 25 cyc =   39600 cyc =  165.0 us @240MHz   [with the per-pixel division (16-18 + ~8)]
    11 Japanese chars, slot 2     1584 mask px  x  9 cyc =   14256 cyc =   59.4 us @240MHz   [increment spelling (no division)]
    (16-18 cyc for a runtime division: docs/pie-simd.md sec.3.7, device-measured)

0 checks printed
```
- 使う恒等式: `floor(x/255) = (x + (x>>8) + 1) >> 8`。**x ≤ 65152 で厳密**。掃引 70,000 値の不一致 18 件は**すべて x > 65152**
  （先頭 65535、このリポジトリの他のカーネルと同じ境界）。本カーネルの分子は上界 `255*255+127 = 65152` で**境界に接している**。
- レーンでの実装は 2 パス: 分子を累算 → `EE.SRCMB.S16.QACC` で `t = x>>8`（QACC も更新される）→ 分子＋128 を累算 →
  `EE.SRCMB.S16.QACC` で `(x + t + 1)>>8`。`+127`/`+128` は**全1レーンとの積**として足す（このISAに「ブロードキャスト定数を
  レーンに足す」命令が無いため。ex22 のカーネルも同じ壁に当たって同じ ones レジスタを使っている）。
- **ビット一致の条件**: (a) `a == 255`（A の本体）、(b) 分子 ≤ 65152、(c) マスクが u8 のまま（`VUNZIP.8` の下位バイトを使う）。
- **外れる条件**: (a) `0 < a < 255` は本体を使わない（スカラーで厳密に処理）、(b) 行が一様でないのに B を呼ぶ（契約違反）、
  (c) `EE.SRCMB.S16.QACC` の読み出し飽和 ±32767 に届く値（本カーネルでは `t ≤ 255`、`q ≤ 255`、`c+d ≤ 510` で届かない）。

### 3.5 アセンブラ出力（実物）

```
$ xtensa-esp32s3-elf-nm -S /tmp/ex23/ex23.o
0000014c 00000104 T ex23_cov_row_advance
00000010 0000013b T ex23_cov_to_mask
00000000 0000000e T ex23_cov_to_mask_supported

$ xtensa-esp32s3-elf-objdump -h /tmp/ex23/ex23.o
Idx Name          Size      VMA       LMA       File off  Algn
  0 .iram1.literal 00000008  00000000  00000000  00000034  2**2
  4 .iram1        0000025c  00000000  00000000  0000003c  2**2
  （他は空。合計 .iram1 604 バイト + リテラル 8 バイト）
```

```
$ xtensa-esp32s3-elf-objdump -d -j .iram1 /tmp/ex23/ex23.o     (ex23_cov_to_mask の全体)
  10:	004136        	entry	a1, 32
  13:	132656        	bnez	a6, 149 <ex23_cov_to_mask+0x139>
  16:	12f516        	beqz	a5, 149 <ex23_cov_to_mask+0x139>
  19:	0004a2        	l8ui	a10, a4, 0
  1c:	129a16        	beqz	a10, 149 <ex23_cov_to_mask+0x139>
  1f:	000071        	l32r	a7, fffc0020 <ex23_cov_row_advance+0xfffbfed4>
  22:	8d0c      	movi.n	a13, 8
  24:	ffa0e2        	movi	a14, 255
  27:	7fa0f2        	movi	a15, 127
  2a:	ffa092        	movi	a9, 255
  2d:	021a97        	beq	a10, a9, 33 <ex23_cov_to_mask+0x23>
  30:	003786        	j	112 <ex23_cov_to_mask+0x102>
  33:	fdf374        	ee.vldbc.16	q7, a7
  36:	609020        	neg	a9, a2
  39:	349090        	extui	a9, a9, 0, 4
  3c:	639950        	minu	a9, a9, a5
  3f:	c05590        	sub	a5, a5, a9
  42:	79ac      	beqz.n	a9, 6d <ex23_cov_to_mask+0x5d>
  44:	0003b2        	l8ui	a11, a3, 0
  47:	0002c2        	l8ui	a12, a2, 0
  4a:	8bca      	add.n	a8, a11, a12
  4c:	82bbc0        	mull	a11, a11, a12
  4f:	bbfa      	add.n	a11, a11, a15
  51:	41c8b0        	srli	a12, a11, 8
  54:	bbca      	add.n	a11, a11, a12
  56:	bb1b      	addi.n	a11, a11, 1
  58:	41b8b0        	srli	a11, a11, 8
  5b:	c088b0        	sub	a8, a8, a11
  5e:	004282        	s8i	a8, a2, 0
  61:	01c222        	addi	a2, a2, 1
  64:	01c332        	addi	a3, a3, 1
  67:	ffc992        	addi	a9, a9, -1
  6a:	fd6956        	bnez	a9, 44 <ex23_cov_to_mask+0x34>
  6d:	41c450        	srli	a12, a5, 4
  70:	345050        	extui	a5, a5, 0, 4
  73:	06ec16        	beqz	a12, e5 <ex23_cov_to_mask+0xd5>
  76:	02bd      	mov.n	a11, a2
  78:	810134        	ee.ld.128.usar.ip	q0, a3, 16
  7b:	818134        	ee.ld.128.usar.ip	q1, a3, 16
  7e:	872b      	addi.n	a8, a7, 2
  80:	cc8724        	ee.src.q.qup	q2, q0, q1
  83:	818134        	ee.ld.128.usar.ip	q1, a3, 16
  86:	ddffa4        	ee.zero.q	q3
  89:	dca3d4        	ee.vzip.8	q2, q3
  8c:	a30124        	ee.vld.128.ip	q4, a2, 16
  8f:	edffa4        	ee.zero.q	q5
  92:	ecc3d4        	ee.vzip.8	q4, q5
  95:	b50184        	ee.vldbc.16.ip	q6, a8, 2
  98:	250844        	ee.zero.qacc
  9b:	0a6284        	ee.vmulas.u16.qacc	q2, q4
  9e:	0a7e84        	ee.vmulas.u16.qacc	q6, q7
  a1:	fd72d4        	ee.srcmb.s16.qacc	q6, a13, 0
  a4:	b50184        	ee.vldbc.16.ip	q6, a8, 2
  a7:	0a6284        	ee.vmulas.u16.qacc	q2, q4
  aa:	0a7e84        	ee.vmulas.u16.qacc	q6, q7
  ad:	fd72d4        	ee.srcmb.s16.qacc	q6, a13, 0
  b0:	9e4264        	ee.vadds.s16	q2, q2, q4
  b3:	be72d4        	ee.vsubs.s16	q6, q2, q6
  b6:	950184        	ee.vldbc.16.ip	q2, a8, 2
  b9:	250844        	ee.zero.qacc
  bc:	0a6b84        	ee.vmulas.u16.qacc	q3, q5
  bf:	0a7a84        	ee.vmulas.u16.qacc	q2, q7
  c2:	dd72d4        	ee.srcmb.s16.qacc	q2, a13, 0
  c5:	950184        	ee.vldbc.16.ip	q2, a8, 2
  c8:	0a6b84        	ee.vmulas.u16.qacc	q3, q5
  cb:	0a7a84        	ee.vmulas.u16.qacc	q2, q7
  ce:	dd72d4        	ee.srcmb.s16.qacc	q2, a13, 0
  d1:	ae4b64        	ee.vadds.s16	q4, q3, q5
  d4:	9e34d4        	ee.vsubs.s16	q2, q4, q2
  d7:	dc63a4        	ee.vunzip.8	q6, q2
  da:	ba01b4        	ee.vst.128.ip	q6, a11, 16
  dd:	cc0b      	addi.n	a12, a12, -1
  df:	f9bc56        	bnez	a12, 7e <ex23_cov_to_mask+0x6e>
  e2:	e0c332        	addi	a3, a3, -32
  e5:	060516        	beqz	a5, 149 <ex23_cov_to_mask+0x139>
  e8:	0003b2        	l8ui	a11, a3, 0
  eb:	0002c2        	l8ui	a12, a2, 0
  ee:	8bca      	add.n	a8, a11, a12
  f0:	82bbc0        	mull	a11, a11, a12
  f3:	bbfa      	add.n	a11, a11, a15
  f5:	41c8b0        	srli	a12, a11, 8
  f8:	bbca      	add.n	a11, a11, a12
  fa:	bb1b      	addi.n	a11, a11, 1
  fc:	41b8b0        	srli	a11, a11, 8
  ff:	c088b0        	sub	a8, a8, a11
 102:	004282        	s8i	a8, a2, 0
 105:	221b      	addi.n	a2, a2, 1
 107:	331b      	addi.n	a3, a3, 1
 109:	550b      	addi.n	a5, a5, -1
 10b:	fd9556        	bnez	a5, e8 <ex23_cov_to_mask+0xd8>
 10e:	f01d      	retw.n
 110:	00          	.byte	00
 111:	00          	.byte	00
 112:	059d      	mov.n	a9, a5
 114:	0003b2        	l8ui	a11, a3, 0
 117:	0002c2        	l8ui	a12, a2, 0
 11a:	82bba0        	mull	a11, a11, a10
 11d:	bbfa      	add.n	a11, a11, a15
 11f:	4188b0        	srli	a8, a11, 8
 122:	bb8a      	add.n	a11, a11, a8
 124:	bb1b      	addi.n	a11, a11, 1
 126:	41b8b0        	srli	a11, a11, 8
 129:	c08eb0        	sub	a8, a14, a11
 12c:	8288c0        	mull	a8, a8, a12
 12f:	88fa      	add.n	a8, a8, a15
 131:	41c880        	srli	a12, a8, 8
 134:	88ca      	add.n	a8, a8, a12
 136:	881b      	addi.n	a8, a8, 1
 138:	418880        	srli	a8, a8, 8
 13b:	88ba      	add.n	a8, a8, a11
 13d:	004282        	s8i	a8, a2, 0
 140:	221b      	addi.n	a2, a2, 1
 142:	331b      	addi.n	a3, a3, 1
 144:	990b      	addi.n	a9, a9, -1
 146:	fca956        	bnez	a9, 114 <ex23_cov_to_mask+0x104>
 149:	f01d      	retw.n
	...
```
ベクタ本体（A）は `0x7e`〜`0xdf` の **34 命令 / 100 バイト**、ストアは `EE.VST.128.IP` 1 本。B の本体は `0x1c4`〜`0x213` の
**28 命令 / 82 バイト**、ストア 1 本。両方 256 バイト未満なので `loopgtz` が使える（**使っていない**。`docs/pie-simd.md` §7 の
ハードウェアループレジスタの注意に従い、既存カーネルと同じ `addi`/`bnez` のまま）。

読み方の要点:
- `EE.LD.128.USAR.IP q0, a3, 16` / `q1` の2回 = 非整列リーダの prologue。`SAR_BYTE = a3 & 15` がロードごとに同じ値に再設定される
  （アドレスが常に 16 進むので下位4bitは不変）。
- `EE.SRC.Q.QUP q2, q0, q1` = 16バイトのウィンドウ抽出＋`q0 = q1` のローテート。**宛先を `q0` にしてはいけない**
  （TRM p128 の疑似コードは「まず `qa` を書き、次に `qs0 = qs1`」なので、`qa == qs0` だとウィンドウが消える）。
- 定数は表（`1,127,128,127,128`）を `EE.VLDBC.16.IP` で歩き、ブロックごとに `addi a8, a7, 2` で巻き戻す。**歩く順序が本体の
  ロード順そのもの**（入れ替えると定数が静かに入れ替わり、アセンブルは通る）。
- 頭と尾のスカラーは `mull` + 2段シフトの `/255` で、**除算命令を使わない**（A: 16命令/px、B: 13命令/px）。

### 3.6 モデルによる等価実行（掃引・不一致件数・先頭不一致）

`/tmp/ex23/lane_model.py` は **objdump のテキストを解析して 1 命令ずつ実行**する（`tools/pie/piesim.py` のコピーではなく自前。
理由: 本カーネルは piesim が持たない `EE.LD.128.USAR.IP` / `EE.SRC.Q.QUP` / `EE.VUNZIP.8` と、頭と尾の Xtensa コア命令
（`l8ui`/`s8i`/`mull`/`extui`/`minu`/`neg`）を使う。意味は `data/pie_instructions.json` の `source_page` の疑似コードから実装し、
未実装命令は例外にする）。`/tmp/ex23/drive.py` が契約の Python 参照と、ホスト C 参照（`ref.c`, `gcc -O2`）と突き合わせる。

```
====================================================================================================
ex23 lane harness -- the kernels in /tmp/ex23/ex23.o (.iram1), disassembled and executed instruction by instruction
  ex23_cov_to_mask       114 instructions in the object
  ex23_cov_row_advance   96 instructions in the object
  model table at 0x3100; mask 0x1000; cov 0x2000; frame 0x0100
====================================================================================================

[1] kernel A, ONE 16-pixel block, a = 255: the lanes against the contract, byte by byte
     cov        0  17  34  51  68  85 102 119 136 153 170 187 204 221 238 255
     mask in    0   7 200 255   0   7 200 255   0   7 200 255   0   7 200 255   0   7 200 255   0   7 200 255   0   7 200 255   0   7 200 255
     lanes      0  24 207 255  68  90 222 255 136 156 237 255 204 222 251 255
     contract   0  24 207 255  68  90 222 255 136 156 237 255 204 222 251 255
     mismatch 0 of 16; 60 instructions executed (3.75 per pixel), 1 stores
     c=  0 d=  0   source s + floor((d*(255-s)+127)/255) =   0   lanes c + d - floor((c*d+127)/255) =   0
     c= 85 d=  7   source s + floor((d*(255-s)+127)/255) =  90   lanes c + d - floor((c*d+127)/255) =  90
     c=255 d=255   source s + floor((d*(255-s)+127)/255) = 255   lanes c + d - floor((c*d+127)/255) = 255
     c=  0 d=  0   source s + floor((d*(255-s)+127)/255) =   0   lanes c + d - floor((c*d+127)/255) =   0
     c=128 d=128   source s + floor((d*(255-s)+127)/255) = 192   lanes c + d - floor((c*d+127)/255) = 192

[2] kernel A sweep (lane model vs the contract), every mask alignment, unaligned coverage
    cases 22932   pixels swept 414540   mismatches 0 (of which outside the requested range: 0)   first None

[3] kernel B sweep (a uniform coverage row: cov_row[i] == s by contract)
    cases 8505   pixels swept 122850   mismatches 0 (of which outside the requested range: 0)   first None

[4] kernel B with a NON-uniform cov_row (the contract violated on purpose): which bytes
    are read from the row at all?  (head and tail read it, the body does not)
    head=5 (bytes 0..4 scalar)  blocks=2  tail=3
    out[0]=0  out[20]=100  out[39]=100  (the contract would give 100 where the row byte was zeroed)
    -> the head and tail honour the row's own bytes; the body's 16-pixel groups use s. That
       is the documented contract: a uniform row, or the ends may differ.

[5] the host C reference (gcc -O2) on the same vectors: a third implementation
    cases 54   disagreements (asm vs C, and C vs contract): 0   first None

[6] instruction counts the model actually executed (the static count in the .md is this
    number minus the per-call prologue)
    A one block, aligned                     60 instructions for  16 px ( 3.75/px)
    A one block, head 3                     262 instructions for  16 px (16.38/px)
    A two blocks                             94 instructions for  32 px ( 2.94/px)
    A 12 px (a real font_large row)         202 instructions for  12 px (16.83/px)
    A 6 px (a real font_small row)          112 instructions for   6 px (18.67/px)
    B one block                              56 instructions for  16 px ( 3.50/px)
    B 12 px                                 207 instructions for  12 px (17.25/px)

ALL CHECKS PASSED
```
読み方:
- **[1]** 1ブロック（16 px）の手計算: レーンと契約が 1 バイトも違わない。最後の5行が `a==255` の畳み込み（元の式と
  `c+d-floor((cd+127)/255)` が同じ値になること）の実例。
- **[2] A の掃引: 22,932 ケース / 414,540 px / 不一致 0**。mask の整列 0〜20、cov の不整列 0〜15、
  `a ∈ {255,254,200,128,1,0}`、`tag ∈ {0,1}`、`n_px ∈ {1,2,3,7,8,15,16,17,23,31,32,33,47}` を全部混ぜてある。
  **要求された範囲の外に 1 バイトも書いていない**ことも同時に検査している（「outside」が 0）。
- **[3] B の掃引: 8,505 ケース / 122,850 px / 不一致 0**（`s ∈ {0,1,2,63,127,128,200,254,255}`、整列 0〜20、`n_px` 1〜33）。
  `s==0` は「何も書かない」ことまで一致。
- **[4]** B の契約違反（行が一様でない）の可視化: 頭と尾は行のバイトを、本体は `s` を使う。
- **[5] ホスト C 参照（`gcc -O2`）との一致: 54 ケース / 不一致 0**（アセンブラ・C・契約の三者一致）。
- **[6] モデルが実際に実行した命令数**。ここが §1.6 の帰結の実測: **12 px の行は 202 命令 = 16.8 命令/px で全部スカラー**、
  16 px ブロックが 1 つ走ると **60 命令 = 3.75 命令/px**、2 ブロックで 94 命令 = 2.94 命令/px。
  A の prologue（入口の判定・頭の整列計算・非整列リーダの2ロード）は約 26 命令、ブロック 1 つは 34 命令。

### 3.7 命令数とサイクルの一次見積り（**推定**）

`docs/pie-simd.md` §3.5 のモデル（1命令=1サイクル、`EE.VST.128.IP` のみ +0.6、実動作は下限の 1.3〜1.4 倍）をそのまま当てる。

```
A: ベクタ本体 34 命令 + 0.6×1 + ストール ~2 = 36.6 cyc / 16 px = 2.3 cyc/px
   ×1.35（タスク切替の退避・復元）= 約 3.1 cyc/px                          [推定]
B: 28 + 0.6 + ~1 = 29.6 cyc / 16 px = 1.85 cyc/px → 約 2.5 cyc/px          [推定]
現行ループ: 除算 16〜18 + 残り 8〜10 = 約 25 cyc/px                         [§3.7 の実測 + 推定]
スカラー加算版（§2）: 約 9 cyc/px                                          [推定]
A の頭/尾スカラー（モデル実測 16.4 命令/px、12 px の窓）: 約 16 cyc/px      [推定]

→ ベクタ本体が走る区間だけ見れば、現行の約 8 倍、スカラー加算版の約 3 倍。
   ただし scale=1・現行フォントではその区間が存在しない（§1.6）。
```
ストールの数え方は段の表の当てはめを**していない**（cardputer-adv-pocketjs の `tools/pie/stalls.py` に相当する検査は
やっていない）。だから §3.7 のサイクルは**下限に近い推定**（§7-5）。

### 3.8 検証が捕まえたもの

1. **非整列リーダのポインタが消費バイトより 32 進んでいた。** ベクタ本体の後、尾のスカラーが読む `cov` の位置が
   2チャンク（32 バイト）ずれていた（prologue 2本 + ブロックごと 1本のロードで `a3` が先行するため）。モデルの掃引が
   `n_px=17` で 1 バイトの不一致として捕まえた。修正は `addi a3, a3, -32` の 1 命令。**1ブロックのテストだけでは出ない**
   （本体 1 つ・尾 0 のときにしか通らない）。
2. **`andi` はこのISAに無い**（`n_px & 15` は `extui a5, a5, 0, 4`）。アセンブラが弾いたので静かな誤りにはならなかった。
3. **`EE.SRC.Q.QUP` の宛先を `qs0` と同一にしてはいけない**（TRM の代入順）。設計時に気づいて `q2` に逃がしてある。

---

## 4. 差し込み口（issue の要求4）

### 4.1 `PpaOps` の実構造

```
engine/backends/rgb565/src/lib.rs:80   pub trait PpaOps {
lib.rs:82     fn fill_rgb565(...) -> bool;
lib.rs:93     fn blend_a8_rgb565(...) -> bool;
lib.rs:107    fn srm_psm5650_to_rgb565(...) -> bool;
lib.rs:119    }
```
**3つとも既定実装が無い**（全部必須）。実装者は4つ:

| 実装者 | 位置 |
| --- | --- |
| C アクセラレータへの橋 | `hosts/esp-idf/native/render-rgb565/src/lib.rs:32`（`impl PpaOps for CAccelerator`、`:33` fill / `:60` blend / `:94` srm） |
| テスト | `engine/backends/rgb565/tests/strip_parity.rs:28`（`NoPpa`）, `:68`（`SrmOnlyPpa`） |
| ライブラリ内のモック | `engine/backends/rgb565/src/lib.rs:1268`（`impl PpaOps for MockPpa`、struct は `:1251`） |

→ **メソッドを足すなら既定実装付き**（`fn cov_row_to_mask(...) -> bool { false }`）にするのが最小変更。既定が `false`＝「断る」
なので、既存の実装者は1行も変えなくても壊れない（`false` のときエンジンは自分のスカラーループを回す＝現状の挙動）。

### 4.2 callback の形で足りるか — **足りる。ただし呼ぶ側の粒度を合わせる必要がある**

呼び出し点は `try_glyph_run`（`lib.rs:672`）の二重ループの内側（`lib.rs:735-743`）で、渡せるものは全部そこにある:

| callback に渡すもの | 出典 |
| --- | --- |
| `mask + local_y*width + local_x` | `lib.rs:737-740` |
| `row` の先頭（＝`rows[sy*bpr..]`、`cov`） | `lib.rs:734` |
| `x0 - gx*scale`（＝`cov` の相対オフセット） | `lib.rs:736` |
| `n_px = x1 - x0` | `lib.rs:730` |
| `alpha`（`channels(color)` の a） | `lib.rs:685` |
| 「連続サンプリングか」＝ `scale == density` | `lib.rs:686` と `self.config.scale` |

契約は既存の `PpaOps` と同じ形（`bool` を返し、`false` は「書いていない」＝エンジンがスカラーで描き直す）にする。
`fill_mask_rect`（`lib.rs:703`）と `blend_a8_rgb565`（`lib.rs:746`）はそのまま。**足りないのは粒度だけ**: いまのループは
「グリフ1行」ごとに呼ぶ形なので、窓が 16 px 未満だと §3.6 のとおりベクタ本体は走らない。PIE を効かせるには、
**呼び出し側を「run 全体の連続した行」に組み替える**（§4.4）。

### 4.3 上流エンジンへの変更範囲の見積り

| ファイル | 変更 | 行数（概算） |
| --- | --- | --- |
| `engine/backends/rgb565/src/lib.rs` | trait に既定実装付きメソッド1つ（~6行）＋ `lib.rs:735-743` の内側を callback 呼び出しに（~6行）＋ `RenderStats` に1カウンタ（任意、~3行） | **10〜15** |
| `engine/backends/rgb565/tests/strip_parity.rs` | **0**（既定実装があるため） | 0 |
| `hosts/esp-idf/native/abi/src/lib.rs:220-236` | `NativeAccelerator` に fn ポインタ1本＋`const` ABI アサート（size 20→24 / offset 20 / align 4。64bit 側は 40→48） | **+6** |
| `hosts/esp-idf/native/render-rgb565/src/lib.rs:32-137` | `impl PpaOps` に転送メソッド1つ（`fill` の実装が26行なので同程度の定型） | **+20〜26** |
| `hosts/esp-idf/components/pocketjs_render_rgb565/include/pocketjs/render_types.h:86-100` | typedef 1本＋フィールド1つ＋`PJS_ABI_ASSERT`（size 24 / offset 20） | **+5** |
| `main/scene/render_accel.c:294-300`（アプリ側） | フック本体（スカラ参照＋本カーネル呼び出し）＋`.cov_row_to_mask = accel_cov_mask,` | **+25〜45** |
| 合計 | | **約 66〜97 行 / 6 ファイル**（うち約35行は ABI の配管） |

**ビルドへの影響**: Rust 側は `tools/build_native.sh:17` の cargo ビルド（`--target xtensa-esp32s3-none-elf`）で
`hosts/esp-idf/native/<component>/` を1本ずつ。構造体のサイズが変わるので `pocketjs_render_rgb565` の static lib と
それを使うアプリ（`main/`）を**同時にビルドし直す必要がある**。ビルド手順そのものは増えない。落とし穴:
`render-rgb565/src/lib.rs:384-386` は

```rust
let accelerator = accelerator.as_ref()
    .filter(|value| value.struct_size >= core::mem::size_of::<NativeAccelerator>());
```
なので、**古いヘッダ（サイズ 20）でビルドされたアプリを新しいエンジンに差すと、アクセラレータ全体が「無し」になる**
（エラーではなく全部スカラーに落ちる＝静かに遅くなる）。ヘッダと Rust の `const` アサートを同時に上げること。

**固定 revision を上げる必要はあるか**: **ある**。エンジン本体（`.cache/pocketjs`）に手を入れるので、
1. 上流（`pocket-stack/pocketjs`）にコミットする、
2. `tools/prepare_dependencies.py:8` の `REVISION = '6a0a1b6c91a506c473fc37a0256a47b12eceeca8'` を新しい SHA に上げる、
3. `CMakeLists.txt:2`（`POCKETJS_SOURCE_DIR "${CMAKE_SOURCE_DIR}/.cache/pocketjs"`）はパス固定なのでそのまま。
の3点。加えて `dependencies.lock:36,47,62` が `.cache/pocketjs/hosts/esp-idf/components/...` を path 依存として指しているので、
**lock の再生成（`idf.py reconfigure`）が必要**。

### 4.4 それでも PIE を効かせるなら（呼び出し側の組み替え）

PIE の本体が走るには **16 px 以上の連続区間**が要る。いまの窓は 6〜12 px なので、次のいずれか:

1. **`scale >= 2`**（拡大表示）。`font_large` の 12 px セルが 24 px になり、16画素ブロックが 1 つ走る（§1.6）。
2. **run 単位の行ストリップ**: `global_rect`（全グリフの和）の幅で行を処理する。手順は「(a) その行に重なるグリフの被覆率行を
   スクラッチ行へコピー（1グリフあたり 6〜12 バイトの `memcpy`）、(b) スクラッチ行 → マスク行を A で合成」。
   **同じ画素に2つのグリフが重ならない**ことが前提（重なる場合はグリフ単位の従来経路に落とす）。`lib.rs:729-745` の
   ループ入れ替えで、行ヘッダの仕事はスカラーのまま。推定では、11文字・font_small の run（66 px 幅）で 1 行あたり 5 ブロック
   = 約 170 命令 + コピー ≈ 11×10 命令、現行の 11×6 px×16.4 命令 = 1082 命令に対して**約 2 倍弱**
   （命令数の算術であって時間ではない）。

---

## 5. 測定の設計（issue の要求1。実機は親がやる）

**現状**: `render_ms` は app_session.c の外側ループが測っていて（`main/app_session.c:552` の `began`、`:568-575` の集計）、
内訳は `kernel_ms`（PIE カーネル自身が `render_accel_cycles` に積む。`main/scene/render_accel.c:262, 290` と
`main/app_session.c:564`）しか無い。**マスク生成の計器はエンジン側に存在しない**ので、3つに分けるにはエンジンに一時的な
カウンタを入れる（`docs/pie-simd.md` §9 の「最適化とその計測を同じコミットに入れない」に従い、計測は別コミット）。

### 手順（親がそのまま実行できる形で）

1. **ベースライン（コード無変更）**: 既存の計測で `PAINT` 行を取る（`main/app_session.c:571-575`）:
   `turn_ms` / `render_ms` / `kernel_ms` / `send_ms` / `accel` / `software`。`render_ms − kernel_ms` が
   「ドローリスト走査＋マスク生成＋ホストのオーバーレイ（`pet_assets_overlay`、`pocket_text_overlay`）」の合計である。
2. **マスク生成だけを切り出す（エンジンに3行 + アプリに2行、計測専用）**:
   - `.cache/pocketjs/engine/backends/rgb565/src/lib.rs` の `try_glyph_run` の二重ループ（`:729`）の直前に
     `let t_mask0 = pocketjs_cycle_count();`、`for` を閉じた後（`:744`、`blend_a8_rgb565` の前）に
     `unsafe { POCKETJS_GLYPH_MASK_CYCLES += pocketjs_cycle_count() - t_mask0; }` を足し、クレートのトップに
     `#[no_mangle] pub static mut POCKETJS_GLYPH_MASK_CYCLES: u32 = 0;` と
     `extern "C" { fn pocketjs_cycle_count() -> u32; }` を置く（`pocketjs_cycle_count()` はアプリ側の C で
     `esp_cpu_get_cycle_count()` を返す1行。Rust を C で汚したくない場合はインライン asm 1 本
     `rsr.ccount a2` でよい — **この綴りは `xtensa-esp32s3-elf-as` が通ることを確認済み**（`/tmp/ex23/rsr.S`、
     `objdump` で `rsr.ccount a2` を確認））。
   - アプリ側 `main/app_session.c` に `extern uint32_t POCKETJS_GLYPH_MASK_CYCLES;` を足し、`:564` の隣で
     `mask_sum += POCKETJS_GLYPH_MASK_CYCLES; POCKETJS_GLYPH_MASK_CYCLES = 0;`、`:571` の `PAINT` 行に
     `mask_ms=%.2f`（`mask_sum/30/240000.0`）を追加する。
   - これで `render_ms = 走査 + mask_ms + kernel_ms + オーバーレイ` に**4分割**できる（残差が走査＋オーバーレイ）。
3. **スカラー加算版の A/B（§2 の対照）**: `lib.rs:736` の `sx` 計算を、`scale == density` のときだけ `sy` ごとに初期化した
   カウンタに置き換える版を作る。**同じバイナリ内のスイッチ**にする（`docs/pie-simd.md` §9 の「同じカーネルがビルド間で
   15% 動く」への対策）。実行時に一度だけ読むグローバル（コンソールフラグ）で版を選び、**30フレーム窓の `render_ms` と
   `mask_ms` を両版で取る**。
4. **PIE 版（§4 の callback）を入れて同じ窓を取る**。見る数字は `mask_ms`（走査と混ざらない）と `accel`/`software`
   （`main/app_session.c:571-575` のカウンタ。callback が `false` を返した回数が見える）。
5. **前提の検査**: `docs/pie-simd.md` §9 の作法（変更していないビルドのサイズが動いていないか、
   `find main components apps -newermt`）と、設定値の起動時ログ（`board.c` の `LCD SPI %d Hz`）。
   **計測スクリプトが再描画を促すためにキーを押すと効果音が鳴り 3〜4 割水増しされる**（同節が実際にやらかしている）ので、
   キー入力で画面を動かさない（`main/app_session.c:115` の `app_force_redraw()` を使う）。

**まだ分からないこと（issue の要求1の本体）**: このループが `render_ms` の何 ms か。測る前は何も言えない。
`docs/pie-simd.md` §9 は「render_ms の4割弱が PIE カーネル、残りは Rust 側のマスク生成」と書いているが、
同節自身が「関数単位で測ったものではない」と断っており、**ドローリスト走査とオーバーレイも同じ残りに入っている**。

---

## 6. kasane 経路との関係（追加観点）

**結論: kasane のテキスト描画は、issue #4 が対象にしている RGB565 の coverage→mask ループを通らない。それどころか、
kasane 経路では今日 文字が1画素も描かれない。**

### 6.1 二つの描画経路は排他

```
main/app_session.c:587   if(!overlay_session && !pocket_kasane_active()) {   <- PocketJS/DrawList 経路
                             pocketjs_rgb565_renderer_config_defaults(&rc);rc.scale=1;
                             pocketjs_rgb565_renderer_create(&rc,&renderer); ... }
main/app_session.c:919   if(pocket_kasane_active()) {                        <- kasane 経路
                             ksn_result result = pocket_kasane_present(&port,&stats); ... }
```
`pocket_kasane_active()`（`main/pocket/pocket_kasane.c:873`）が真のフレームでは kasane が自分のコマンドバッファから
自前で帯を合成する（`main/ui/kasane/ksn_render.c:150 ksn_render_rects`）。カーネル（`render_accel`）も PocketJS の
rgb565 バックエンドも**呼ばれない**。アプリはセッション単位でどちらかを選ぶ。

### 6.2 kasane 側の文字は「保存されるが描かれない」

- `KSN_TEXT` は第5の種別（`main/ui/kasane/ksn_types.h:16`）で、検証（`ksn_core.c:136-140`）、文字列領域への保存
  （`ksn_core.c:171-180`）、damage 判定での文字列比較（`ksn_core.c:535-538`）まで実装されている。
- しかし**レンダラは `KSN_TEXT` を拒否する**:
  ```
  ksn_render.c:163  if(command.draw.kind<KSN_RECT||command.draw.kind>KSN_GRADIENT){
  ksn_render.c:164      ksn_core_defer_repair(core,frame.ticket);return KSN_UNSUPPORTED;
  ```
  `KSN_RECT..KSN_GRADIENT` は 0..3、`KSN_TEXT` は 4。**TEXT コマンドが1つでもあるとフレーム丸ごと `KSN_UNSUPPORTED`**
  （修復に回る）。`covers()`（`ksn_render.c:18-33`）も TEXT を `default:return false` で落とす。
- 唯一の kasane シーン `apps/kasane/demo.js` は `rect` と `group`/`modal`/`cache.create` だけで、
  **`text` 呼び出しが 0 個**（`grep -c "\.text(\|text:" apps/kasane/demo.js` → 0）。
- ロードマップも未実装と書いている: `docs/kasane-roadmap.md:52`「文字 | coreのTEXT保存、容量、setText/setReveal |
  **jpfont renderer**、JS spec/ref、SYSTEM textfieldとの合成」、`docs/design-composition.md:313` の残件一覧に
  「**text/image renderer**」。

### 6.3 kasane 側に「別のマスク合成ループ」はあるか — ある。ただしマスクを持たない

| ループ | 位置 | 1画素あたり | 除算 | マスク配置・整列 |
| --- | --- | --- | --- | --- |
| 通常の矩形ブレンド | `ksn_render.c:198-202` | `covers()`（矩形/角丸/枠の内外判定、除算なし）→ `blend()`（`ksn_render.c:140-149`）: `a=((src&255)*opacity+127)/255`、各チャネル `(src*a + d8*(255-a)+127)/255`、必要なら 4×4 Bayer 量子化 | **定数除算のみ**（255。魔法の乗算） | **マスク無し**。`pixels[(py-y)*240+x]` に**毎回 2バイトの RMW**。16B 整列の契約は無い |
| グループ合成（グループ不透明度） | `ksn_render.c:84-137`（画素ループは `:111-134`） | 64×1 の premultiplied RGBA タイル（`ksn_render.c:87`、256 バイト）へ `premultiply_over`（`:54-61`）、その後 `group_over`（`:73-83`）で本体へ | 同上（255 のみ） | タイルはスタック。整列の契約なし |
| frosted（すりガラス） | `ksn_frost.c:83-119` + **`ksn_frost_kernel.h:14 ksn_frost_cell_pie`** | **既に PIE カーネル**（`EE.VMULAS.U16.QACC` + `EE.SRCMB.S16.QACC` の `/255`、SAR=3 の `EE.VMUL.D16`） | 同上 | 30×17 の固定タイル（`ksn_frost.c:83` 付近） |

つまり kasane 側には**マスク（coverage プレーン）という中間表現が無い**。文字を通すときも行タイルに直接 premultiplied 合成する
形になり（グループ経路がその雛形）、issue #4 の PIE カーネル（coverage→mask）はそのままは流用できない。
**kasane に文字を通すなら要るのは「グリフの被覆率→RGB565 行」の合成**で、`ksn_render.c:198-202` の per-pixel RMW を
16B 整列の行単位に組み替えるのが先になる（`pixels[(py-y)*240+x]` は `x` が任意なので、いまの形では
`EE.VLD.128.IP`/`EE.VST.128.IP` が使えない）。

### 6.4 kasane のテキストが1フレームに何回・何画素か

- **今日の実測値は 0 画素**（§6.2: レンダラが TEXT を拒否し、唯一のシーンも TEXT を使わない）。
  **kasane 側の文字はまだ性能問題になっていない。**
- 参考の上限見積り: `tools/kasane_contract/use_cases.c` の用例は1ビューあたり **TEXT 2〜3件**（`:12`、`:25-30`、`:38`）。
  文字サイズは `docs/design-system.md`（caption 6×8 / body 6×12・日本語 12×12 / display 12×16）で、日本語は jpfont の
  **12×12 セル**（§1.3）。**「1フレームの文字画素 = 行あたり (グリフ数 × セル幅) × セル高」まではコードから書けるが、
  実際の画面（ペット画面など）が何文字描くかは実装が無いので未測定**。
- 既存の実測: `tools/kasane_device_test.py:45` が `KASANE_PAINT turn_ms/render_ms/send_ms` を拾って3窓の平均を出す
  （`main/app_session.c:943` が出す）。**この行に描画内訳（走査/合成/転送）は無い**ので、§5 と同じ作法で計器を足す必要がある
  （転送は `ksn_display_port` の `present`（`main/app_session.c:121-125`）の前後で既に分離できる）。

### 6.5 kasane 経路に効く順（§9 の kasane 版）

1. **未実装の文字レンダラを、最初から行単位の形で書く**（`ksn_render.c:198-202` を真似ない）。`ksn_frost_kernel.h` に
   PIE の作法と `/255` の厳密化（`ksn_frost_kernel.h:9-11` のコメント）が既にあるので、文字もその形で設計できる。
   **設計段階ならタダ。**
2. **frost と同じ「1命令1サイクル」の見積りで先に命令数を出す**（`tools/pie/test_frost.py` が既に `extract_asm` で
   ヘッダの asm を引いている → 同じ経路で文字カーネルも検査できる）。
3. **グループ不透明度のタイル経路（`ksn_render.c:111-134`）は 64 画素ごとにタイルを作り直す**ので、文字をそこへ載せると
   2パスになる。1パスで行に直接合成する形の方が安い（未測定の推定）。
4. 測定: `KASANE_PAINT`（`main/app_session.c:943`）に「合成 / 転送」の2分割を足す（`ksn_render.c:204` の
   `display->present` の前後）。**kasane の文字はまだ描かれないので、いま測れるのは矩形とグループの側だけ。**

---

## 7. 未確認の前提 (semantics this kernel depends on that I could NOT confirm)

1. **`EE.LD.128.USAR.IP` + `EE.SRC.Q.QUP` の非整列ウィンドウ。** TRM p93 / p128 の疑似コードで実装し、モデル上で
   `cov` の整列 0〜15 を全部掃いて契約と一致することを確かめた（§3.6 [2]）。ただし**装置での確認はこのリポジトリに無い**。
   もし `SRC.Q.QUP` の `qs0 = qs1` が「シフト前の値」を掴む実装なら（順序が逆）、ウィンドウが 16 バイトずれる。優先度は高い。
2. **`EE.VZIP.8` / `EE.VUNZIP.8` のバイト順**（TRM p294 / p292）。ex22 の §11 が `EE.VZIP.16` について同じことを書いている。
3. **`EE.SRCMB.S16.QACC` の AR シフトと s16 飽和読み出し**は TRM p130 のままで、本カーネルの鎖での装置確認は無い。
4. **`EE.VMULAS.U16.QACC` の「同じレーンを 40bit に累算する」形**（TRM p245）。本カーネルは 1 ブロックにつき 8 本の
   QACC 累算を鎖で使う。飽和上限 2^40-1 には届かない（最大 65152）が、それは計算であって装置の確認ではない。
5. **QACC の連続依存（`ZERO.QACC` の直後の `VMULAS`）のストール数**。§3.7 は段の表の当てはめをしていない
   （cardputer-adv-pocketjs の `tools/pie/stalls.py` に相当する検査はやっていない）。
6. **非整列リーダの読み越し**: カーネルは `cov` の手前と後ろに最大 15 バイト読み越す（§3.2 の契約）。アトラスの行末で
   隣のグリフに食い込む分は読めるが、**最後のグリフの最後の行の後ろ**は呼び手が保証しなければならない。
   いまのアトラスの確保の仕方は確認していない（`engine/core/src/text.rs:171` の `glyph_rows`）。
7. **`0 < a < 255` の行がどれくらいあるか**（本カーネルはその行を全部スカラーで処理する）。アプリの色の分布は測っていない。
8. **実機ランが無い。** issue の要求1（まず測る）は §5 の設計まで。

---

## 8. ビルドへの入れ方（適用していない）

`proposed/` に置いてあるだけで、`examples/firmware/main/CMakeLists.txt` にも `main.c` にも触っていない。入れるなら:

1. `examples/firmware/main/CMakeLists.txt` の `SRCS` に `proposed/ex23_covmask.S` を足す（ex13〜ex22 と同じ扱い）。
2. `main.c` に C 参照（`ref.c` 相当の2関数）と `DATA` 行を足す。§3.6 のモデルが検査している性質（範囲外を書かない・
   `a==0`/`s==0`/`tag!=0` は何も書かない）を、そのまま装置側の検査項目にする。
3. 実機で確かめる順序: (a) `EE.SRC.Q.QUP` の非整列ウィンドウ（1ブロックの出力を `DATA` に出す）、(b) `EE.VZIP.8`/`EE.VUNZIP.8` の
   バイト順、(c) `EE.SRCMB.S16.QACC` の AR シフト、(d) それから A/B の全数比較。
4. そのうえでエンジン側は §4 の callback を入れる前に、§2 のスカラー加算版を先に出す（計測は別コミット）。

---

## 9. 効く順（まとめ）

| 順 | 何 | 効き方 | リスク | 状態 |
| --- | --- | --- | --- | --- |
| 1 | **全部 0 の行を捨てる**（`lib.rs:735` の内側に入る前に、行の最大被覆率が 0 なら `continue`） | 行の 20.7% が全0（`'Hello World'` で 20.5%、`'12:34'` で 25%）。その行の除算・合成が丸ごと消える | 無（`composite(d,0)==d` で出力は同一） | 未実装・未計測（§1.7 の数え上げのみ） |
| 2 | **マスクが 0 の窓ではプリスケールだけ／`a==255` ならバイトコピー**（同じ run の先のグリフが書いていない窓） | 除算が消え、乗算も消える（`memcpy` 相当）。run の非重複グリフ全部 | 中（「まだ 0」の判定を run 内で持つ必要） | 未実装。§3.6 [1] の出力が性質の証拠（`mask_in=0` の画素は `cov` がそのまま出る） |
| 3 | **`sx` の加算化**（`scale==density`） | 1画素1除算（16〜18 cyc）が消える。推定 25 → 9 cyc/px | 低（等式を実行時に検査して分岐、20行、掃引 130,460 px で一致） | 対照はホスト実行済み（§2）。エンジン未変更 |
| 4 | **PIE カーネル A/B（本ファイル）** | 本体が走る区間で 2.3〜3.1 cyc/px（推定）。ただし `scale=1` の現行フォントでは本体が走らない（窓 6〜12 px < 16） | 中（ABI 6ファイル・固定 revision・非整列リーダの未確認前提） | 意味はモデルで全数一致（A 414,540 px / B 122,850 px）。**効かせるには §4.4 の組み替えが要る** |
| 5 | **kasane の文字レンダラを最初から行単位で書く** | kasane は今日文字を1画素も描かない（§6.2）。設計段階なので、いま形を決めれば PIE が素直に載る | 低（新規実装） | 未実装（`ksn_render.c:163` が TEXT を拒否） |
