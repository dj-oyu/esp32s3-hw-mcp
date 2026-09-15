# VM に PIE（EE.*）は刺さるか（兄弟プロジェクトの実コードからの判定）

→ このディレクトリの索引は `README.md`。数値の規約もそちらにある。

対象は **兄弟プロジェクト cardputer-adv-pocketjs の QuickJS 改 VM**（ブランチ `vm/main`、HEAD
`74e704d`、作業ツリー `/workspace/pjs-vm`）。本ノートの中の `components/...` `main/...` `docs/...` は
すべて**同リポジトリ内の相対パス**で、このリポジトリ（esp32s3-hw-mcp）のものではない。`docs/pie-simd.md`
も同プロジェクトの文書。

**読み取り専用の調査**で、向こうのリポジトリには何も変更していない。**実機は使っていない**
（`/dev/ttyACM0` 非接触）。

数字の出所は次の3種のみ。混ぜていない:

- **[obj]** = リポジトリ自身のビルドレシピ（`build_v601_probe/compile_commands.json` の
  `xtensa-esp32s3-elf-gcc -Os`）で生成した命令数（静的カウント）。実機の実行時間ではない。
- **[実測(host)]** = `.cache/vmtest/vmrun-o2` を走らせた出力（JSValue 16 B のホスト。実機は 8 B）。
- **[inferred]** = 上記からの計算。式を併記する。

サイクル・µs に換算した値はすべて **[inferred]** である。実機で測ったものは 1 つも無い。

---

## 1. 結論

**VM の4領域（呼び出し経路／自前アロケータ／自己管理スタック／中止可能プロセスモデル）のいずれにも、
PIE がそのまま刺さる箇所は無い。** 形だけ合う候補は 6 つあるが、全部が同じ 1 つの壁で止まる:

> **実機のフレーム・ブロック・確保したメモリは 4 B 整列しか契約されていない。**
> PIE の 128bit **ストア**は `store128({as[31:4], 4{0}})` で下位 4bit を強制的に 0 にする（TRM p275、
> 非整列ストアの命令は ISA に無い）。整列違反は例外にならず、**黙って 4 バイト手前を壊す**。

`EE.VLD/VST.128.IP` を 1 命令 16 B のコピー・充填に使う筋（スカラーの 3.0〜3.7 倍）は、命令数の上では
正しい。**落ちる理由は性能ではなく整列契約である。**

| 領域 | PIE の余地 | 根拠 |
|---|---|---|
| 呼び出し経路・実行ループ | **無し**（ディスパッチは 1 opcode 9 命令、うち 6 が制御フロー） | `quickjs.c:18811` の `SWITCH(pc)`、`flat_call:` `:21380-21453` は 72 命令、プロローグ `:18748` 約 61 命令 |
| 自前アロケータ | **ほぼ無し**。`guest_malloc` は IDF ヒープに丸投げ（25 命令）、bulk は `calloc`/`realloc` の ROM `memset`/`memcpy` だけ。コーパス1周の節約は全体の 0.3% | `components/pocketjs_guest/src/guest.c:134-202` |
| 自己管理スタック | **定数一括充填だけ**（`var_buf` / `var_refs`）。成長・返却・pop には形が無い（コピーもゼロ埋めも存在しない） | `quickjs-vmstack.h:377-618`、`quickjs.c:18782-18791` |
| 中止可能プロセスモデル | **無し**。セーフポイントは 1 回 11 命令の「ロード→減算→ストア→分岐」でデータ並列が 1 個も無い | `quickjs.c:8836-8843`、`:19800-19805` |

---

## 2. 領域別

### 2.1 呼び出し経路・実行ループ — 不適

- バイトコードディスパッチ（`quickjs.c:18811`）は **1 opcode あたり 9 命令** [obj]。opcode バイト読み・
  テーブル参照・`jx` 間接分岐が 6 命令を占め、うち 3 命令は `ENABLE_DUMPS` のダンプフラグ判定が
  **出荷ビルドでも常時コンパイルされている**ぶん（`quickjs.c:106`）。
- JS→JS 呼び出しの平坦化（`flat_call:` `quickjs.c:21380`）は非選択分岐を除いた実行パスで **72 命令
  （call8 3 本）[obj]**、共有プロローグ `:18748` が約 61 命令、復帰 `resume_caller:` 区間が静的に 141 命令。
  中身はクラス判定の比較・レジスタの退避・`goto` で、**同型の並列演算が無い**。
- タグ判定（`JS_VALUE_GET_TAG = v >> 32`、NAN boxing）はスカラー 2 命令。128bit ロードで 2 個のタグを
  同時に比べても、**どのレーンが立ったかの復元**（`EE.MOVI.32.A` ＋ スカラー分岐）で要素あたり 4 命令以上に
  なる＝判定が判定される仕事より高くつく。
- 引数は**コピーしない**（`arg_allocated_size == 0` のとき `sf->arg_buf` は呼び出し元のオペランド
  スロットを指すだけ。`:21418-21421`）。走る場合も 1 要素ごとに `js_dup`（`call8`）が要る。
- 解放ループ（`:21603-21605`、`:21759-21761`）は 1 要素 **11 命令＋`call8` 1 本**。しかも典型の反復数は
  1 桁（`stack_size`）。

### 2.2 自前アロケータ — ほぼ不適

- `guest_malloc`（`guest.c:134-152`）はヘッダ 16 B を前置して `heap_caps_malloc` に渡すだけ（**25 命令
  [obj]**）。探索も結合もしない（それは IDF の tlsf 側）。この層に一括メモリ操作は無い。
- 唯一 bulk を持つのは `guest_realloc` の `memcpy`（`:191`）と `guest_calloc` の `memset`（`:161`）で、
  **どちらも ROM 関数**（`0x400011f4` / `0x400011e8`）。ROM `memcpy` の整列済み経路は **11 命令/16 B**、
  `memset` は **6 命令/16 B**（ROM を逆アセンブルして数えた）。PIE 版はそれぞれ 3 命令/16 B、2 命令/16 B
  になるので**ループ内は 3.0〜3.7 倍**。
- しかし: [実測(host)] コーパス 61 ファイル + `--profile device` で `mallocs=3,486,691  reallocs=63,321`。
  realloc のコピー長は平均 **74.4 B**（85.7% が 32 B 以上）で、`8m − 1` 命令の節約を積むと**全体 0.81e6
  命令**。アロケータ仕事の総量（`malloc` 3.5M × 56 命令 ≒ 195e6 ＋ `free` ≒ 91e6）に対して **0.3%**。
  `docs/pie-simd.md` §9 が「ビルドが変われば同じコードの実測も 15% 動く」と書いている世界では、
  **この差は主張できない**。
- ビットマップ集合演算（`ANDQ/ORQ/XORQ/NOTQ`）の対象は**存在しない**。`adapter_segment.c`・
  `adapter_estalloc.c`・`quickjs-vmstack.h`・`guest.c` を `bitmap|_bits|popcount` で検索して全部 0。
  唯一のビットマップはホスト比較用 estalloc の **16 bit FLI / 8 bit SLI** で、128 bit には桁が足りない。
  GC のマークは**オブジェクト 1 個 1 bit**（`quickjs.c:1120`）でビットマップ配列が無い。
- フリーリスト走査（`adapter_segment.c:361-368`）は `l->next` のポインタ追跡。`EE.VMIN/VMAX.S32` が要する
  「連続した 32bit レーン配列」が無く、first-fit の「何番目か」を返す命令も無い（`EE.ACCX` はアキュム
  レータ）。`seg_lower_bound`（`:181-188`）は二分探索なので線形 SIMD 化は**改悪**。

### 2.3 自己管理スタック（フレームセグメント）— 候補は定数充填のみ

- 実装は `quickjs-vmstack.h` のバンプアロケータ。push の速い経路は **15〜17 命令 [obj]** の
  ポインタ／整数演算だけで、`JS_VM_POISON` は**実機では空マクロ**（ASan ビルド専用）。
- **成長時にコピーもゼロ埋めもしない**（非移動の設計。`:488-491` が「caller has to zero ではない」と明記）。
  したがって「セグメントの伸長を速くする」対象が無い。
- セグメントの確保は [実測(host)] `closures.js` 2 回、`deep_recursion_device.js` 1,796 回。一方
  `bench_calls.js` のフレーム push は **2,692,538 回**。**最適化の対象は後者**。
- 1 フレームのサイズ（実機、NAN boxing）:
  `frame_total = round_up(4 + 48 + 8*(arg_allocated_size + var_count + stack_size) + 4*var_ref_count, 4)`。
  `sizeof(JSValue)=8 / sizeof(JSStackFrame)=48 / sizeof(JSVMLink)=4` はいずれも実機で確認した
  （`:640-644`、`quickjs.c:18674-18776`）。実機の `frame_max` は 84〜188 B（`docs/vm-L2-design.md` §13.2）。
- **候補 C1/C2**: `var_buf` の `JS_UNDEFINED` 充填（`quickjs.c:18782-18784`）は実機 [obj] で
  **6〜7 命令 / 8 B スロット**。[inferred] `5.5·V − 2` 命令の節約（V = スロット数、V≳5 で黒字、V≤3 は負け）。
- **候補 C3**: `var_refs` の NULL 充填（`:18789-18791`）は 6 命令 / 4 B。`EE.ZERO.Q` + `EE.VST.128.IP` で
  4 ポインタ/命令になるが、**R ≥ 4 でしか 1 命令も減らない**。クロージャを持たない関数は R=0。
- **整列が壁**: セグメントの payload 先頭だけが 16 B 整列（`:382`）。個々のブロックは **4 B 整列**
  （`JS_VM_FRAME_ALIGN 4`、`:112-116`。設計 D7 が「8 B を必須にしない」と決めている）。
  `FLATCALLS` on では `local_buf = block + 4 + 48` なので `var_buf mod 16 ∈ {0,4,8,12}` がほぼ一様
  ＝ **16 B 整列は約 1/4 のフレームでしか成立しない** [inferred]。
- **壊れ方の実演**（piesim で実行）: 宛先 `0x84` に向けた `ee.vst.128.ip` は実際には `0x80` に書き、
  `0x80..0x83` を書き換えて 4 バイト手前を破壊した。例外は上がらない。

### 2.4 中止可能プロセスモデル — 不適

- セーフポイントは class-A の 7 地点（`goto`/`goto16`/`goto8`/`if_true`/`if_false`/`if_true8`/`if_false8`）で、
  実ビルドの `.s` に `call8 js_poll_safepoint` がちょうど 7 回出ることを確認した [obj]。
- 1 回のチェックは **11 命令**（呼び出し側 3 ＋ `js_poll_safepoint` 本体のファストパス 8）。中身は
  `js_poll_safepoint`（`quickjs.c:8836-8843`）の
  `if (unlikely(--ctx->interrupt_counter <= 0))` で、カウンタは `JSContext+288` の
  **メモリ上のロード/加算/ストア**。データ並列が 1 個も無い。`-Os` では `static inline` が展開されず
  `call8` が残る。
- スローパス `__js_poll_interrupts`（`:8781-8821`、42 命令）→ `js_vm_safepoint`
  （`quickjs-vm.c:153-166`、no-gap/no-force 経路 11 命令）。L2c 段階1・2 の時点では armed 中でも
  返り値は「ジョブを殺す」だけで、`JS_VMSuspended` は常に 0 を返す（`quickjs-vm.c:223-226`）。
- ドレインの毎ジョブ判定（`vm_sched.c:68-93`）は床未満 19 命令／床以降 30 命令 [obj]。条件 4 つは
  `vm_budget_t` の離れた offset に散在し、真のときの動作がそれぞれ違う（返り値 3 種）。ベクタ化は
  「gather（1 命令 1 レーン）」＋「レーンごとのマスク取り出し」になり損。
- タイマ（`pocket_app.c:737-746`、`APP_SLEEPS=2`）・購読表（`pocket_api.c:295-320`、4〜8 スロット）・
  Promise スロット（`:498-524`、72 B ストライド ×4）は、**要素数が 2〜8**。`EE.LDXQ.32` は 1 命令
  1 レーンなので集めるだけで命令数が増える。しかも 64bit の `due_us`/`ns` を比べる PIE 命令は
  **存在しない**（`EE.VCMP.*` は S8/S16/S32 のみ、U 版・64bit 版なし）。
- 実行ループ内で唯一 SIMD の形をしているのは probe 専用の
  `pocketjs_guest_vmprobe_drain_calls`（`guest.c:64-75`、uint16 × 128 = 256 B のコピー）。
  実ビルドで 5 命令/要素 → PIE 4 命令/16 B ＝ **8.97 倍（-574 命令/フレーム）[obj]、
  約 2.4 µs/フレーム [inferred]**。ただし `CONFIG_POCKET_VM_PROBE` 専用コードで、出荷ビルドでは消える。

---

## 3. この調査で出た「PIE 以外の」実利（スカラ側・命令数の算術のみ）

PIE が刺さらない以上、VM 側で次に効くのはスカラの手である。**いずれも効果は未実測**:

1. `js_poll_safepoint` / `js_vm_stack_over_budget` / `js_vm_stack_push` / `js_vm_pop_frame` /
   `js_vm_stack_pop` は `static inline` 指定にもかかわらず **`-Os` で `call8` のまま**（`quickjs-vmstack.h:280`,
   `:492`）。呼び出し規約（レジスタ窓・退避）のコストで、1 回あたり十数命令。
2. `SWITCH(pc)` の `DUMP_BYTECODE_OR_DONT` は `ENABLE_DUMPS` 配下だが**出荷ビルドでも 3 命令/opcode** を
   払っている（`quickjs.c:106`、`:18606`）。切った場合の差は**未計測**。
3. `vm_sched_drain` の `(n % budget->stride)` は stride=1 でも `remu` として残る。stride==1 の特別扱いは
   測定可能な候補（**サイクルは未計測。速くなるとは主張しない**）。

---

## 4. 主張しないこと（未確認の前提）

- **実機は 1 度も使っていない。** サイクル・µs の換算はすべて `[inferred]`（240 MHz・PIE 1 IPC の仮定。
  `docs/pie-simd.md` §3.5 は実機で「PIE は 1 命令 1 サイクル、VST.128.IP のみ +0.6」と確認しているが、
  **それをアロケータ／VM に外挿しただけ**）。
- 命令数は `build_v601_probe` のレシピ（`-Os`、`CONFIG_POCKET_VM_{PROBE,SCHED,SEGFRAMES,FLATCALLS}=y`）で
  再コンパイルした `.s` からの静的カウント。**出荷ビルドと同一のプリプロセッサ状態であることは
  確認していない**（`compile_commands.json` は存在しない `/opt/esp-idf-6.0.1` を指していた）。
- `var_count` / `var_ref_count` の実機分布は**未計測**。C1 の損益分岐（V≳5）はこの分布に依存する。
  ホスト実測の `frame_max` からの逆算では V は 1〜3 程度と見られる（**推測**）。
- 16 B 整列の成立確率（約 1/4）はレイアウトからの計算であって、実機でアドレスを採取したものではない。
  `guest_malloc` の戻りも「16 B 整列の保証は無い」（`main/scene/scene_mem.c:11` が "malloc gives 8 at
  best" と書いている）までしか言えない。
- PIE はコプロセッサ3 でタスク切替時に退避・復元される（`docs/pie-simd.md` §1）。**VM タスクに常時
  PIE を使わせた場合のスイッチコストは未計測**。
- `EE.VST.L/H.64.IP`（8 B 単位、下位 3bit を 0）は C1 の整列問題を緩め得るが、**piesim 未実装で意味を
  検証していない**。

## 5. 確かめるなら（このリポジトリの作法）

`docs/vm-L2-design.md` §1 が「判定手段の無い完了条件は満たしたと言えない」と決めている。候補 C1〜C3 を
主張するには、次の 3 点が要る:

1. そのコードを切るコンパイル時スイッチを同じコミットに入れる（`main/Kconfig.projbuild` の作法）。
2. **同じツリー・1 個の define の差で 2 本焼く**（`docs/pie-simd.md` §9 の「ビルド間 15% ぶれ」を跨ぐ
   比較は無効）。
3. 命令数ではなく実機のターン時間（`turn_ms`）で差を取る。アンカー／ノイズ床の作法は
   `experiments/pie-timing` と `tools/parse_pie_timing.py` が既に持っている
   （有効性ゲートを通らない測定は派生値を 1 つも出さない）。

---

## 出典

- `components/quickjs-ng/quickjs-ng/quickjs.c`（`JS_CallInternal`、`js_poll_safepoint`、プロローグ／
  `flat_call:`／`resume_caller:`、`var_buf`/`var_refs` 充填）
- `components/quickjs-ng/quickjs-ng/quickjs-vmstack.h`（バンプアロケータ、整列契約、`JS_VM_POISON`）
- `components/quickjs-ng/quickjs-ng/quickjs-vm.c` / `quickjs-vm.h`（セーフポイント、G5、L2c の受け口）
- `components/pocketjs_guest/src/guest.c`（`GUEST_ALLOCATOR`、`drain_jobs`、probe）
- `components/pocketjs_guest/src/vm_sched.c` / `include/pocketjs/vm_sched.h`（ドレイン予算）
- `main/pocket/pocket_api.c` / `main/pocket/pocket_app.c`（スロット表、sleeps）
- `tools/vmalloc/adapter_segment.c`（ホスト検証用アロケータ。**実機には載っていない**）
- `docs/vm-L2-design.md`（D7 の整列決定、§3.1/§13.2 の frame_max、§1 の判定作法）
- TRM のページ番号は `esp32s3-hw-mcp` の `data/pie_instructions.json`（220 命令）と
  `data/pie_pipeline.json` から引いた。PIE の実測制約は `docs/pie-simd.md`。
