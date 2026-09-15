# あちらの Opus / minimp3 を PIE で組むときに効く事実

出典: 兄弟プロジェクト **cardputer-adv-pocketjs**（`components/opus`、`components/minimp3`、
`main/pocket/mp3_decode.c`）と、**`.cache/codecs/opus-1.6.1` / `.cache/codecs/minimp3`**（このリポジトリの
`components/*/CMakeLists.txt` が pin している取得物）。行番号は 2026-09-15 時点。実機は使っていない
（すべてコードからの事実と、ホスト側モデル/C参照との突き合わせ）。

対応する例題（すべて `examples/firmware/main/proposed/`、**ホスト検証済み・実機未実行**）:
`ex16_mdct.S`（CELT IMDCT）、`ex17_mp3synth.S`（MP3 合成）、`ex18_stereo_comb.S`（ステレオ＋コンブ）。

---

## 1. この木で実際に走るもの／走らないもの

| | 事実 | 出典 |
|---|---|---|
| Opus | **CELT のみ実行される**。`opus_pak_toc_ok` が `config>=16 && (config&3)==3 && !stereo && code==0` を要求 | `main/pocket/opus_feed.h:84-87`、`.c:100-103` |
| SILK | リンクはされるが**実行されない** | 同上 |
| NSQ | `components/opus/CMakeLists.txt:43` の除外 regex で**コンパイルされない**（参照にしてはいけない） | 同 CMakeLists |
| Opus のソース | checkout に **committed されていない**（`.cache/codecs/opus-1.6.1` に tag で存在） | `components/opus/CMakeLists.txt:1-6,16-17` |
| minimp3 | `MINIMP3_ONLY_MP3` + `MINIMP3_NO_SIMD` + 実装マクロ。**内部は float、出口だけ int16** | `components/minimp3/minimp3.c:1-3` |
| 実測（あちらの機体） | CELT WB 20ms mono を 24kHz 出力で復号 = mean 3,208µs / worst 3,670µs、約 1,600 サイクル/出力サンプル | `docs/opus-feasibility.md:143-145, :41` |
| 実測（MP3） | 44.1kHz stereo 128kbps で復号+レート変換+発行 = 5.9〜6.7ms/フレーム | `docs/mp3-implementation.md:41` |

---

## 2. CELT の IMDCT（ex16）で分かったこと

1. **pre-rotate は 4 レーン化できない**（この命令セットでは）。CELT は両端から歩くので、同一反復の
   ペアが同じレーンに来るロードの組は 4 レーン中 1 レーンしか作れない。加えて `EE.VZIP.16` /
   `EE.VUNZIP.16` が生成する置換群を 2 レジスタ 32 バイトスロットで**位数 120 まで全列挙**したが
   4 レーン反転が存在しない。素直なベクトル化は **1112/1112 レーン誤り（100%）** を実測で確認。
   → 契約を「入力は鏡像ペア並び（zipped）」にして、並べ替えパスの代価を算術で示す形にした。
2. **ラン 2 本の 128bit 読みは静かに間違う**。`2*n4 % 16 == 8` になる N4=60 で 8 バイトずれ、128bit
   ロードは下位 4bit を落とすので別の組を掛ける。→ `EE.VLD.L.64.IP`（下位 3bit 強制）に変更して解決。
3. **CELT は飽和しない**。窓折り返しと加算は `SUB32_ovflw` / `ADD32_ovflw` / `ADD32` で **32bit 折り返し**
   （`celt/mdct.c:371-386`、`celt/celt_decoder.c:613-617`、`celt/fixed_generic.h:152-158`）、飽和は IMDCT 出力
   全体への 1 回の `SATURATE(x, SIG_SAT=536870911)` だけ（`celt_decoder.c:504-508`、`arch.h:215`）。
   → `EE.VADDS.S16` を使うと**別演算**になる（実測: 飽和 326 レーンが全件厳密和と不一致、
   in-domain 1,424 レーンは 0 不一致）。**勝手に飽和させないこと。**

## 3. ステレオとコンブ（ex18）

- `stereo_merge`（`celt/bands.c:458-466`）は xtensa では**丸めも飽和もしない**（`SHR32` は切り捨て、
  `ADD32/SUB32` は飽和せず、この木の `celt_norm` は `opus_val32`）。17bit の中間値を 16bit レーンで
  厳密に扱うための恒等式（半値＋borrow/carry、`floor(D/2^s)=floor(floor(D/2)/2^(s-1))`）で
  **202,640 値すべて一致**。壊れる綴りは 4 種あり、それぞれ実測で示した（先に飽和させる素朴形は最大
  16,207 の差、PSHR32 丸めは 1 ULP、ゲイン `Q31ONE` は 1 ULP、最後のクランプは shift=0 で 65,535）。
- コンブフィルタの契約は **3 ゲイン / 5 タップ / 履歴 T+2**、読みは `hist[-2]`〜`hist[n-1]`、`hist = x − T`。
  **不足タップはゼロ埋めも飛ばしもしない**（`celt/celt.c:273-276` を無条件に読み、`celt/celt.h:236` の
  1024 サンプル状態が存在を保証する）。「飛ばす」規則は C と 48 中 4 サンプル食い違う（最大 4,648）。
- ここでも **ACCX は使えない**（8 レーンを 1 本に潰す合算型）。レーンごとの独立出力には
  `EE.VSMULAS.S16.QACC` + `EE.SRCMB.S16.QACC` を使う。

## 4. MP3（ex17）

- minimp3 は**内部 float** なので、Q15 の整数カーネルは**ビット一致しない**。実測差は
  IMDCT で max 3.20 / 平均 0.65 LSB、窓重ねで max 1.94 / 0.66 LSB、polyphase で max 1.00 / 0.50 LSB。
  さらに `EE.SRCMB` の 16bit レジスタコピーは IMDCT で **30.5%（2810/9216 レーン）がクランプ**するので、
  int16 出口に繋ぐ前にスケール設計が要る。
- **ACCX と QACC の選択は「還元の向き」で決まる**。18 個の独立出力が欲しい場合（IMDCT）は出力をレーンに
  置ける QACC。8 レーンを 1 本に潰す ACCX は 1 命令 1 出力になる。両綴りを実装して実測:
  QACC 版 74 命令 / ACCX 版 90 命令（MAC 54 は同じ、読み出しとゼロが 18 本ずつ増える）で**結果は完全一致**。
- polyphase は `ceil(16 タップ/8 レーン)=2 MAC + 読み出し 0.125 + ストア 0.25 + ループ 0.25` =
  **2.625 命令/出力サンプル**（objdump: ループ本体 21 命令 / 8 サンプル）。比較のため素直な 512 タップ窓は
  ≈160 MAC/サンプル（**未検証**）、`-O2` スカラ C は 16 タップ × 11 命令 = 177 命令/サンプル。
- **32bit `int` で書いたスカラ参照は溢れて 0 不一致にならない**（この検査が実際に捕まえた）。C 参照は
  `long long` で書くこと。

## 5. あちらの FIR / マスク系で次に効く対象（issue #5, #4 として起票済み）

- **MP3 ダウンサンプル前の 32 タップ FIR**（`main/pocket/mp3_decode.c:60-72`）: リングバッファの `& 31` が
  連続ロードを妨げる。履歴 2 倍長＋連続窓の設計が要る。Q14、`sum/16384` は負数でゼロ方向切り捨て。
- **テキスト描画の coverage→mask**（`.cache/pocketjs/engine/backends/rgb565/src/lib.rs:715-745`）:
  `coverage_index` の除算が画素ごとに 1 本入る。`scale=1, density=1` のときは `sx` が +1 ずつ進むので
  除算が消える（スカラーの対照が先）。kasane（描画エンジン側）の経路も同じ調査対象。

## 6. 測る時の注意（あちらの実測から）

- **このプロジェクトの examples ファームは 160 MHz で動いている**（`CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ 160`、
  起動ログの `cpu freq: 160000000 Hz`）。サイクル数を µs に換算するときは**クロックを書き添える**
  （あちらの `docs/pie-simd.md:726` が「測定対象の設定値を起動時にログへ出す」と同じ理由）。
- カーネル実測と実動作は **1.3〜1.4 倍**違う（タスク切替のコプロセッサ退避）。詳細は `pie-cost-model.md`。
